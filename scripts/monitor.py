#!/usr/bin/env python3
"""Local read-only live monitor for the Codex-Claude bridge."""

from __future__ import annotations

import copy
import http.server
import json
import math
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from collections import deque
from pathlib import Path
from typing import Any, Callable

if __package__:
    from .control import ControlRequestError, ReviewControl
else:
    from control import ControlRequestError, ReviewControl


CONFIGURED_CONTEXT_WINDOWS = {
    "deepseek-v4-pro[1m]": 1_000_000,
}
MAX_EVENTS = 1_000
MAX_SEEN_EVENT_IDS = 2_000
TEST_COMMAND_RE = re.compile(
    r"(?:^|\s)(?:python\d*(?:\.\d+)?\s+-m\s+pytest|pytest|npm\s+test|"
    r"npm\s+run\s+test)(?:\s|$)",
    re.IGNORECASE,
)


def _valid_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_positive_int(value: Any) -> bool:
    return _valid_non_negative_int(value) and value > 0


def _valid_non_negative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def format_elapsed_zh(seconds: Any, *, tenths: bool) -> str:
    """Format a non-negative elapsed duration with natural Chinese units."""
    if not _valid_non_negative_number(seconds):
        seconds = 0
    if tenths:
        total_tenths = max(0, int(round(float(seconds) * 10)))
        hours, remainder = divmod(total_tenths, 36_000)
        minutes, remainder = divmod(remainder, 600)
        seconds_text = f"{remainder / 10:.1f}秒"
    else:
        total_seconds = max(0, int(float(seconds)))
        hours, remainder = divmod(total_seconds, 3_600)
        minutes, remainder = divmod(remainder, 60)
        seconds_text = f"{remainder}秒"
    if hours:
        return f"{hours}小时{minutes}分{seconds_text}"
    if minutes:
        return f"{minutes}分{seconds_text}"
    return seconds_text


def _find_pre_tokens(value: Any) -> int | None:
    if isinstance(value, dict):
        for key in ("preTokens", "pre_tokens"):
            candidate = value.get(key)
            if _valid_non_negative_int(candidate):
                return candidate
        for candidate in value.values():
            found = _find_pre_tokens(candidate)
            if found is not None:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_pre_tokens(candidate)
            if found is not None:
                return found
    return None


def _codex_activity_label(item_type: Any) -> str:
    """Return a fixed public label without exposing Codex item payloads."""
    labels = {
        "command_execution": "命令执行",
        "agent_message": "审查消息",
        "file_change": "文件变更",
        "mcp_tool_call": "工具调用",
        "web_search": "资料检索",
        "reasoning": "审查步骤",
    }
    return labels.get(item_type, "审查步骤")


class MonitorState:
    """Reduce sanitized bridge streams into a bounded monitor snapshot."""

    def __init__(
        self,
        repo: Path,
        sanitizer: Callable[[Any], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repo = Path(repo)
        self._sanitize = sanitizer
        self._clock = clock
        self._condition = threading.Condition()
        self._started_at = clock()
        self._revision = 0
        self._event_sequence = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._running_tools: dict[str, dict[str, Any]] = {}
        self._active_claude_text_event: dict[str, Any] | None = None
        self._latest_claude_text_event: dict[str, Any] | None = None
        self._active_codex_review_event: dict[str, Any] | None = None
        self._last_streamed_claude_text: str | None = None
        self._round_output_tokens = 0
        self._phase = "INITIALIZING"
        self._round: int | None = None
        self._status = "RUNNING"
        self._bridge: dict[str, Any] = {
            "claude_model": None,
            "codex_model": None,
            "codex_reasoning_effort": None,
            "autocompact_threshold": None,
            "max_rounds": None,
            "implementation_attempts": 0,
            "codex_reviews": 0,
            "max_implementation_attempts": None,
            "max_codex_reviews": None,
        }
        self._claude: dict[str, Any] = {
            "model": None,
            "version": None,
            "context_used_tokens": None,
            "context_window_tokens": None,
            "context_window_source": "unknown",
            "context_percent": None,
            "context_warning": None,
            "prompt_cache_remaining_seconds": None,
            "rss_bytes": None,
            "pid": None,
            "total_cost_usd": None,
            "round_cost_usd": None,
            "output_tokens_total": 0,
            "output_tokens_per_second": None,
            "output_tokens_peak_per_second": None,
        }
        self._last_assistant_at: float | None = None
        self._tools: dict[str, Any] = {
            "started": 0,
            "completed": 0,
            "failed": 0,
            "last_command": None,
            "last_command_is_test": False,
        }
        self._compaction: dict[str, Any] = {
            "count": 0,
            "pre_tokens": None,
        }
        self._codex: dict[str, Any] = {
            "status": "idle",
            "thread_id": None,
            "last_item_type": None,
            "activity_started": 0,
            "activity_completed": 0,
        }
        self._git: dict[str, Any] = {
            "available": None,
            "branch": None,
            "changed_files": None,
            "untracked_files": None,
            "additions": None,
            "deletions": None,
            "paths": [],
        }
        self._configuration: dict[str, Any] = {
            "available": None,
            "claude_md": None,
            "rules": None,
            "hooks": None,
        }
        self._memory: dict[str, Any] = {
            "host_used_bytes": None,
            "host_total_bytes": None,
        }
        self._awaiting_input: dict[str, Any] | None = None

    def _touch_locked(self) -> None:
        self._revision += 1
        self._condition.notify_all()

    def notify_control_change(self) -> None:
        """Wake SSE clients without adding control noise to the event feed."""
        with self._condition:
            self._touch_locked()

    def _remember_event_locked(self, event: dict[str, Any]) -> bool:
        event_id = event.get("uuid")
        if not isinstance(event_id, str) or not event_id:
            return True
        if event_id in self._seen_ids:
            return False
        self._seen_ids.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > MAX_SEEN_EVENT_IDS:
            oldest = self._seen_order.popleft()
            self._seen_ids.discard(oldest)
        return True

    def _append_event_locked(
        self,
        kind: str,
        message: str,
        *,
        level: str = "info",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._event_sequence += 1
        elapsed = round(self._clock() - self._started_at, 3)
        item: dict[str, Any] = {
            "sequence": self._event_sequence,
            "kind": kind,
            "level": level,
            "message": message,
            "elapsed_seconds": elapsed,
            "elapsed_display": format_elapsed_zh(elapsed, tenths=True),
        }
        if details:
            item["details"] = details
        self._events.append(item)
        return item

    def _replace_event_locked(
        self,
        event: dict[str, Any],
        kind: str,
        message: str,
        *,
        level: str = "info",
        details: dict[str, Any] | None = None,
    ) -> bool:
        if not any(candidate is event for candidate in self._events):
            return False
        elapsed = round(self._clock() - self._started_at, 3)
        sequence = event["sequence"]
        event.clear()
        event.update({
            "sequence": sequence,
            "kind": kind,
            "level": level,
            "message": message,
            "elapsed_seconds": elapsed,
            "elapsed_display": format_elapsed_zh(elapsed, tenths=True),
        })
        if details:
            event["details"] = details
        return True

    def _append_claude_delta_locked(self, text: str) -> None:
        if self._active_claude_text_event is None:
            self._active_claude_text_event = self._append_event_locked(
                "claude_text",
                text,
                details={"streaming": True},
            )
            self._latest_claude_text_event = self._active_claude_text_event
            return
        self._active_claude_text_event["message"] += text
        self._active_claude_text_event.setdefault("details", {})["streaming"] = True

    def _finalize_claude_text_locked(self, text: str | None = None) -> None:
        active = self._active_claude_text_event
        if active is not None:
            current = active["message"]
            if isinstance(text, str) and text:
                if text.startswith(current) or current.startswith(text):
                    active["message"] = text if len(text) >= len(current) else current
                elif text != current:
                    active["message"] = text
            active.setdefault("details", {})["streaming"] = False
            self._last_streamed_claude_text = active["message"]
            self._latest_claude_text_event = active
            self._active_claude_text_event = None
            return
        if not isinstance(text, str) or not text:
            return
        if text == self._last_streamed_claude_text:
            return
        self._latest_claude_text_event = self._append_event_locked(
            "claude_text",
            text,
            details={"streaming": False},
        )
        self._last_streamed_claude_text = text

    def _update_context_percent_locked(self) -> None:
        used = self._claude["context_used_tokens"]
        window = self._claude["context_window_tokens"]
        if _valid_non_negative_int(used) and _valid_positive_int(window):
            self._claude["context_percent"] = round(used * 100 / window, 2)
        else:
            self._claude["context_percent"] = None

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        safe = self._sanitize(payload)
        if not isinstance(safe, dict):
            safe = {}
        message = safe.get("message")
        if not isinstance(message, str):
            message = kind.replace("_", " ")
        level = safe.get("level")
        if level not in {"info", "success", "warning", "error"}:
            level = "info"
        details = {key: value for key, value in safe.items()
                   if key not in {"message", "level"}}
        append_event = True
        with self._condition:
            if kind == "bridge_started":
                for key in ("claude_model", "codex_model", "codex_reasoning_effort"):
                    value = safe.get(key)
                    self._bridge[key] = value if isinstance(value, str) else None
                compact = safe.get("claude_autocompact_pct_override")
                self._bridge["autocompact_threshold"] = (
                    compact if isinstance(compact, str) else None
                )
                max_rounds = safe.get("max_rounds")
                self._bridge["max_rounds"] = (
                    max_rounds if _valid_positive_int(max_rounds) else None
                )
                for key in (
                    "implementation_attempts", "codex_reviews",
                    "max_implementation_attempts", "max_codex_reviews",
                ):
                    value = safe.get(key)
                    valid = (
                        _valid_non_negative_int(value)
                        if key in {"implementation_attempts", "codex_reviews"}
                        else _valid_positive_int(value)
                    )
                    if valid:
                        self._bridge[key] = value
            elif kind == "implementation_attempt_started":
                attempt = safe.get("attempt")
                maximum = safe.get("max_attempts")
                if _valid_positive_int(attempt):
                    self._bridge["implementation_attempts"] = attempt
                if _valid_positive_int(maximum):
                    self._bridge["max_implementation_attempts"] = maximum
            elif kind == "codex_review_started":
                maximum = safe.get("max_reviews")
                if _valid_positive_int(maximum):
                    self._bridge["max_codex_reviews"] = maximum
                append_event = False
            elif kind == "codex_review_completed":
                review = safe.get("review")
                maximum = safe.get("max_reviews")
                if _valid_positive_int(review):
                    self._bridge["codex_reviews"] = review
                if _valid_positive_int(maximum):
                    self._bridge["max_codex_reviews"] = maximum
                append_event = False
            elif kind == "claude_handoff":
                self._finalize_claude_text_locked(message)
                target = self._latest_claude_text_event
                if target is not None and self._replace_event_locked(
                    target,
                    kind,
                    message,
                    level=level,
                    details=details,
                ):
                    append_event = False
                self._latest_claude_text_event = None
            elif kind == "codex_decision":
                target = self._active_codex_review_event
                if target is not None and self._replace_event_locked(
                    target,
                    kind,
                    message,
                    level=level,
                    details=details,
                ):
                    append_event = False
                self._active_codex_review_event = None
            elif kind in {"task_passed", "codex_handoff"}:
                append_event = False
            elif kind == "input_required":
                question = safe.get("question")
                reason = safe.get("reason")
                options = safe.get("options")
                self._awaiting_input = {
                    "question": question if isinstance(question, str) else "",
                    "reason": reason if isinstance(reason, str) else "",
                    "options": [
                        item for item in options
                        if isinstance(item, str)
                    ] if isinstance(options, list) else [],
                }
                append_event = False
            elif kind == "user_answer_received":
                self._awaiting_input = None
            if append_event:
                self._append_event_locked(
                    kind, message, level=level, details=details)
            self._touch_locked()

    def set_phase(self, phase: str, round_no: int | None = None) -> None:
        safe_phase = self._sanitize(phase)
        if not isinstance(safe_phase, str):
            safe_phase = "UNKNOWN"
        with self._condition:
            if (
                safe_phase == "CLAUDE_RUNNING"
                and round_no != self._round
            ):
                self._round_output_tokens = 0
                self._claude["round_cost_usd"] = None
                self._last_streamed_claude_text = None
                self._active_claude_text_event = None
                self._latest_claude_text_event = None
            self._phase = safe_phase
            self._round = round_no
            self._append_event_locked(
                "phase", f"Bridge phase: {safe_phase}",
                details={"round": round_no},
            )
            self._touch_locked()

    def set_status(self, status: str) -> None:
        safe_status = self._sanitize(status)
        if not isinstance(safe_status, str):
            safe_status = "UNKNOWN"
        with self._condition:
            self._status = safe_status
            self._touch_locked()

    def set_claude_pid(self, pid: int | None) -> None:
        if pid is not None and not _valid_positive_int(pid):
            pid = None
        with self._condition:
            self._claude["pid"] = pid
            if pid is None:
                self._claude["rss_bytes"] = None
            self._touch_locked()

    def update_rss(self, rss_bytes: int | None) -> None:
        if rss_bytes is not None and not _valid_non_negative_int(rss_bytes):
            rss_bytes = None
        with self._condition:
            self._claude["rss_bytes"] = rss_bytes
            self._touch_locked()

    def update_memory(self, used_bytes: int | None, total_bytes: int | None) -> None:
        if used_bytes is not None and not _valid_non_negative_int(used_bytes):
            used_bytes = None
        if total_bytes is not None and not _valid_positive_int(total_bytes):
            total_bytes = None
        if used_bytes is not None and total_bytes is not None and used_bytes > total_bytes:
            used_bytes = None
        with self._condition:
            self._memory = {
                "host_used_bytes": used_bytes,
                "host_total_bytes": total_bytes,
            }
            self._touch_locked()

    def update_git(self, value: dict[str, Any]) -> None:
        safe = self._sanitize(value)
        if not isinstance(safe, dict):
            safe = {"available": False}
        with self._condition:
            self._git = {
                "available": safe.get("available") is True,
                "branch": safe.get("branch") if isinstance(
                    safe.get("branch"), str) else None,
                "changed_files": safe.get("changed_files") if _valid_non_negative_int(
                    safe.get("changed_files")) else None,
                "untracked_files": safe.get("untracked_files") if _valid_non_negative_int(
                    safe.get("untracked_files")) else None,
                "additions": safe.get("additions") if _valid_non_negative_int(
                    safe.get("additions")) else None,
                "deletions": safe.get("deletions") if _valid_non_negative_int(
                    safe.get("deletions")) else None,
                "paths": [item for item in safe.get("paths", [])
                          if isinstance(item, str)][:100],
            }
            self._touch_locked()

    def update_configuration(self, value: dict[str, Any]) -> None:
        safe = self._sanitize(value)
        if not isinstance(safe, dict):
            safe = {"available": False}
        with self._condition:
            self._configuration = {
                "available": safe.get("available") is True,
                "claude_md": safe.get("claude_md") if _valid_non_negative_int(
                    safe.get("claude_md")) else None,
                "rules": safe.get("rules") if _valid_non_negative_int(
                    safe.get("rules")) else None,
                "hooks": safe.get("hooks") if _valid_non_negative_int(
                    safe.get("hooks")) else None,
            }
            self._touch_locked()

    def _consume_usage_locked(
        self,
        usage: Any,
        *,
        include_output: bool = False,
    ) -> None:
        if not isinstance(usage, dict):
            return
        values = [usage.get(key) for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )]
        if not all(_valid_non_negative_int(value) for value in values):
            return
        self._claude["context_used_tokens"] = sum(values)
        self._last_assistant_at = self._clock()
        self._update_context_percent_locked()
        if not include_output:
            return
        output_tokens = usage.get("output_tokens")
        if not _valid_non_negative_int(output_tokens):
            return
        self._round_output_tokens += output_tokens
        self._claude["output_tokens_total"] += output_tokens

    def _consume_result_telemetry_locked(self, event: dict[str, Any]) -> None:
        cost = event.get("total_cost_usd")
        model_usage = event.get("modelUsage")
        if not _valid_non_negative_number(cost) and isinstance(model_usage, dict):
            model_costs = [
                value.get("costUSD")
                for value in model_usage.values()
                if isinstance(value, dict)
            ]
            if model_costs and all(
                _valid_non_negative_number(value) for value in model_costs
            ):
                cost = sum(float(value) for value in model_costs)
        if _valid_non_negative_number(cost):
            round_cost = float(cost)
            self._claude["round_cost_usd"] = round_cost
            total_cost = self._claude["total_cost_usd"]
            self._claude["total_cost_usd"] = (
                round_cost
                if not _valid_non_negative_number(total_cost)
                else round(float(total_cost) + round_cost, 10)
            )

        usage = event.get("usage")
        result_output = (
            usage.get("output_tokens") if isinstance(usage, dict) else None
        )
        if (
            not _valid_non_negative_int(result_output)
            and isinstance(model_usage, dict)
        ):
            model_outputs = [
                value.get("outputTokens")
                for value in model_usage.values()
                if isinstance(value, dict)
            ]
            if model_outputs and all(
                _valid_non_negative_int(value) for value in model_outputs
            ):
                result_output = sum(model_outputs)
        if _valid_non_negative_int(result_output):
            missing = max(0, result_output - self._round_output_tokens)
            self._round_output_tokens += missing
            self._claude["output_tokens_total"] += missing
        duration_ms = event.get("duration_api_ms")
        if (
            _valid_non_negative_int(result_output)
            and _valid_positive_int(duration_ms)
        ):
            rate = round(result_output * 1000 / duration_ms, 2)
            self._claude["output_tokens_per_second"] = rate
            peak = self._claude["output_tokens_peak_per_second"]
            self._claude["output_tokens_peak_per_second"] = (
                rate if not _valid_non_negative_number(peak)
                else max(float(peak), rate)
            )

    def _consume_model_usage_locked(self, model_usage: Any) -> None:
        model = self._claude.get("model")
        if not isinstance(model_usage, dict) or not isinstance(model, str):
            return
        value = model_usage.get(model)
        if not isinstance(value, dict):
            return
        reported = value.get("contextWindow")
        if not _valid_positive_int(reported):
            return
        configured = CONFIGURED_CONTEXT_WINDOWS.get(model)
        self._claude["context_warning"] = (
            "context_window_mismatch"
            if configured is not None and configured != reported else None
        )
        self._claude["context_window_tokens"] = reported
        self._claude["context_window_source"] = "claude_reported"
        self._update_context_percent_locked()

    def consume_claude(self, event: dict[str, Any]) -> None:
        safe = self._sanitize(event)
        if not isinstance(safe, dict):
            return
        with self._condition:
            if not self._remember_event_locked(safe):
                return
            event_type = safe.get("type")
            subtype = safe.get("subtype")
            if event_type == "system" and subtype == "init":
                model = safe.get("model")
                version = safe.get("claude_code_version")
                if isinstance(model, str):
                    self._claude["model"] = model
                    configured = CONFIGURED_CONTEXT_WINDOWS.get(model)
                    self._claude["context_window_tokens"] = configured
                    self._claude["context_window_source"] = (
                        "configured" if configured is not None else "unknown")
                    self._claude["context_warning"] = None
                    self._update_context_percent_locked()
                if isinstance(version, str):
                    self._claude["version"] = version
                self._append_event_locked("claude_init", "Claude 已连接")
            elif subtype == "compact_boundary":
                self._finalize_claude_text_locked()
                self._compaction["count"] += 1
                self._compaction["pre_tokens"] = _find_pre_tokens(safe)
                self._append_event_locked(
                    "compact", "Claude context compacted",
                    level="warning",
                    details={"preTokens": self._compaction["pre_tokens"]},
                )
            elif event_type == "stream_event":
                inner = safe.get("event")
                if isinstance(inner, dict):
                    message = inner.get("message")
                    if isinstance(message, dict):
                        self._consume_usage_locked(message.get("usage"))
                    delta = inner.get("delta")
                    if isinstance(delta, dict) and delta.get("type") == "text_delta":
                        text = delta.get("text")
                        if isinstance(text, str) and text:
                            self._append_claude_delta_locked(text)
                    if inner.get("type") == "content_block_stop":
                        self._finalize_claude_text_locked()
            elif event_type == "assistant":
                message = safe.get("message")
                if isinstance(message, dict):
                    self._consume_usage_locked(
                        message.get("usage"),
                        include_output=True,
                    )
                    content = message.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text":
                                text = block.get("text")
                                if isinstance(text, str) and text:
                                    self._finalize_claude_text_locked(text)
                            elif block.get("type") == "tool_use":
                                self._finalize_claude_text_locked()
                                tool_id = block.get("id")
                                name = block.get("name")
                                if not isinstance(tool_id, str) or not tool_id:
                                    continue
                                safe_name = name if isinstance(name, str) else "unknown"
                                self._tools["started"] += 1
                                inputs = block.get("input")
                                command = inputs.get("command") if isinstance(inputs, dict) else None
                                if isinstance(command, str) and command:
                                    self._tools["last_command"] = command
                                    self._tools["last_command_is_test"] = bool(
                                        TEST_COMMAND_RE.search(command))
                                tool_event = self._append_event_locked(
                                    "tool_started", f"工具开始：{safe_name}",
                                    details={"tool_id": tool_id, "name": safe_name,
                                             "command": command},
                                )
                                self._running_tools[tool_id] = {
                                    "name": safe_name,
                                    "command": command,
                                    "event": tool_event,
                                }
            elif event_type == "user":
                self._finalize_claude_text_locked()
                message = safe.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_result":
                            continue
                        tool_id = block.get("tool_use_id")
                        if not isinstance(tool_id, str):
                            continue
                        running = self._running_tools.pop(tool_id, None)
                        name = (
                            running.get("name")
                            if isinstance(running, dict)
                            and isinstance(running.get("name"), str)
                            else "unknown"
                        )
                        command = (
                            running.get("command")
                            if isinstance(running, dict)
                            and isinstance(running.get("command"), str)
                            else None
                        )
                        failed = block.get("is_error") is True
                        counter = "failed" if failed else "completed"
                        self._tools[counter] += 1
                        kind = "tool_failed" if failed else "tool_completed"
                        tool_message = f"工具{'失败' if failed else '完成'}：{name}"
                        tool_details = {
                            "tool_id": tool_id,
                            "name": name,
                            "command": command,
                        }
                        target = (
                            running.get("event")
                            if isinstance(running, dict) else None
                        )
                        if not isinstance(target, dict) or not self._replace_event_locked(
                            target,
                            kind,
                            tool_message,
                            level="error" if failed else "success",
                            details=tool_details,
                        ):
                            self._append_event_locked(
                                kind,
                                tool_message,
                                level="error" if failed else "success",
                                details=tool_details,
                            )
            elif event_type == "result":
                self._finalize_claude_text_locked()
                self._consume_model_usage_locked(safe.get("modelUsage"))
                self._consume_result_telemetry_locked(safe)
            self._touch_locked()

    def consume_codex(self, event: dict[str, Any]) -> None:
        safe = self._sanitize(event)
        if not isinstance(safe, dict):
            return
        event_type = safe.get("type")
        with self._condition:
            if event_type == "thread.started":
                thread_id = safe.get("thread_id")
                model = safe.get("model")
                self._codex["status"] = "reviewing"
                self._codex["thread_id"] = (
                    thread_id if isinstance(thread_id, str) else None)
                self._codex["activity_started"] = 0
                self._codex["activity_completed"] = 0
                if isinstance(model, str) and model:
                    self._codex["model"] = model
                    self._bridge["codex_model"] = model
                if self._active_codex_review_event is None:
                    self._active_codex_review_event = self._append_event_locked(
                        "codex_started",
                        "Codex 正在审查…",
                        details={
                            "streaming": True,
                            "activity_started": 0,
                            "activity_completed": 0,
                            "latest_activity": "等待首个审查步骤",
                        },
                    )
            elif event_type in {"item.started", "item.completed"}:
                item = safe.get("item")
                item_type = item.get("type") if isinstance(item, dict) else None
                model = item.get("model") if isinstance(item, dict) else None
                if isinstance(model, str) and model:
                    self._codex["model"] = model
                    self._bridge["codex_model"] = model
                safe_type = item_type if isinstance(item_type, str) else "item"
                self._codex["last_item_type"] = safe_type
                suffix = "started" if event_type.endswith("started") else "completed"
                counter = f"activity_{suffix}"
                self._codex[counter] = int(self._codex[counter]) + 1
                label = _codex_activity_label(item_type)
                activity = "开始" if suffix == "started" else "完成"
                completed = int(self._codex["activity_completed"])
                message = (
                    f"Codex 正在审查… 已完成 {completed} 项；"
                    f"最近：{label}{activity}"
                )
                details = {
                    "streaming": True,
                    "activity_started": int(self._codex["activity_started"]),
                    "activity_completed": completed,
                    "latest_activity": label,
                }
                target = self._active_codex_review_event
                if isinstance(target, dict):
                    self._replace_event_locked(
                        target,
                        "codex_started",
                        message,
                        details=details,
                    )
            elif event_type == "turn.completed":
                self._codex["status"] = "completed"
                target = self._active_codex_review_event
                if isinstance(target, dict):
                    self._replace_event_locked(
                        target,
                        "codex_started",
                        "Codex 审查输出已完成，正在验证结构化结论…",
                        details={
                            "streaming": False,
                            "activity_started": int(
                                self._codex["activity_started"]),
                            "activity_completed": int(
                                self._codex["activity_completed"]),
                            "latest_activity": _codex_activity_label(
                                self._codex.get("last_item_type")),
                        },
                    )
            else:
                return
            self._touch_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            remaining: int | None = None
            if self._last_assistant_at is not None:
                remaining = max(
                    0, min(300, int(300 - (self._clock() - self._last_assistant_at))))
            claude = copy.deepcopy(self._claude)
            claude["prompt_cache_remaining_seconds"] = remaining
            elapsed = round(self._clock() - self._started_at, 3)
            result = {
                "revision": self._revision,
                "phase": self._phase,
                "round": self._round,
                "status": self._status,
                "elapsed_seconds": elapsed,
                "elapsed_display": format_elapsed_zh(
                    elapsed, tenths=False),
                "bridge": copy.deepcopy(self._bridge),
                "workspace": {
                    "name": self.repo.name,
                    "path": str(self.repo),
                },
                "claude": claude,
                "codex": copy.deepcopy(self._codex),
                "memory": copy.deepcopy(self._memory),
                "tools": {
                    **copy.deepcopy(self._tools),
                    "running": [
                        {
                            "id": tool_id,
                            "name": data.get("name", "unknown"),
                        }
                        for tool_id, data in self._running_tools.items()
                    ],
                },
                "compaction": copy.deepcopy(self._compaction),
                "git": copy.deepcopy(self._git),
                "configuration": copy.deepcopy(self._configuration),
                "awaiting_input": copy.deepcopy(self._awaiting_input),
                "events": copy.deepcopy(list(self._events)),
            }
            return result

    def wait_for_revision(
        self, after: int, timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        with self._condition:
            if self._revision <= after:
                self._condition.wait_for(
                    lambda: self._revision > after,
                    timeout=max(0.0, timeout),
                )
            return self._revision, self.snapshot()


class MonitorSampler:
    """Periodically collect bounded read-only local HUD information."""

    def __init__(
        self,
        state: MonitorState,
        repo: Path,
        *,
        interval_seconds: float = 3.0,
        claude_config_dir: Path | None = None,
        proc_root: Path = Path("/proc"),
        git_executable: str = "git",
    ) -> None:
        self.state = state
        self.repo = Path(repo)
        self.interval_seconds = max(0.01, float(interval_seconds))
        self.claude_config_dir = (
            Path(claude_config_dir) if claude_config_dir is not None
            else Path.home() / ".claude"
        )
        self.proc_root = Path(proc_root)
        self.git_executable = git_executable
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _git(self, arguments: list[str]) -> str:
        result = subprocess.run(
            [self.git_executable, "-C", str(self.repo), *arguments],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if result.returncode:
            raise OSError("git sampling failed")
        return result.stdout

    @staticmethod
    def _status_path(line: str) -> str | None:
        if len(line) < 4:
            return None
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        return path or None

    def _sample_git(self) -> None:
        try:
            branch = self._git(["branch", "--show-current"]).strip()
            status_lines = [line for line in self._git(
                ["status", "--porcelain=v1"]).splitlines() if line]
            numstat_lines = [line for line in self._git(
                ["diff", "--numstat", "--"]).splitlines() if line]
            additions = 0
            deletions = 0
            for line in numstat_lines:
                columns = line.split("\t", 2)
                if len(columns) < 3:
                    continue
                if columns[0].isdigit():
                    additions += int(columns[0])
                if columns[1].isdigit():
                    deletions += int(columns[1])
            paths = [
                path for path in (self._status_path(line) for line in status_lines)
                if path is not None
            ]
            self.state.update_git({
                "available": True,
                "branch": branch or "detached",
                "changed_files": len(status_lines),
                "untracked_files": sum(
                    1 for line in status_lines if line.startswith("??")),
                "additions": additions,
                "deletions": deletions,
                "paths": paths,
            })
        except (OSError, subprocess.SubprocessError, ValueError):
            self.state.update_git({"available": False})

    @staticmethod
    def _count_hook_commands(value: Any) -> int:
        if isinstance(value, dict):
            count = 1 if value.get("type") == "command" and isinstance(
                value.get("command"), str) else 0
            return count + sum(
                MonitorSampler._count_hook_commands(item)
                for key, item in value.items() if key != "command"
            )
        if isinstance(value, list):
            return sum(MonitorSampler._count_hook_commands(item) for item in value)
        return 0

    def _sample_configuration(self) -> None:
        try:
            claude_files = {
                self.repo / "CLAUDE.md",
                self.repo / ".claude" / "CLAUDE.md",
                self.claude_config_dir / "CLAUDE.md",
            }
            claude_count = sum(path.is_file() for path in claude_files)
            rule_dirs = (
                self.repo / ".claude" / "rules",
                self.claude_config_dir / "rules",
            )
            rule_count = sum(
                1 for directory in rule_dirs if directory.is_dir()
                for path in directory.rglob("*.md") if path.is_file()
            )
            settings_paths = (
                self.claude_config_dir / "settings.json",
                self.claude_config_dir / "settings.local.json",
                self.repo / ".claude" / "settings.json",
                self.repo / ".claude" / "settings.local.json",
            )
            hook_count = 0
            for path in settings_paths:
                if not path.is_file():
                    continue
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    hook_count += self._count_hook_commands(parsed.get("hooks"))
            self.state.update_configuration({
                "available": True,
                "claude_md": claude_count,
                "rules": rule_count,
                "hooks": hook_count,
            })
        except (OSError, UnicodeError, ValueError, TypeError):
            self.state.update_configuration({"available": False})

    def _sample_memory(self) -> None:
        host_used: int | None = None
        host_total: int | None = None
        try:
            meminfo = (self.proc_root / "meminfo").read_text(encoding="utf-8")
            values: dict[str, int] = {}
            for line in meminfo.splitlines():
                columns = line.split()
                if len(columns) >= 2 and columns[0].rstrip(":") in {
                    "MemTotal", "MemAvailable",
                } and columns[1].isdigit():
                    values[columns[0].rstrip(":")] = int(columns[1]) * 1024
            if "MemTotal" in values and "MemAvailable" in values:
                host_total = values["MemTotal"]
                host_used = host_total - values["MemAvailable"]
        except (OSError, UnicodeError, ValueError):
            pass
        self.state.update_memory(host_used, host_total)

        snapshot = self.state.snapshot()
        pid = snapshot["claude"].get("pid")
        if not _valid_positive_int(pid):
            self.state.update_rss(None)
            return
        try:
            status = (self.proc_root / str(pid) / "status").read_text(
                encoding="utf-8")
            rss_bytes: int | None = None
            for line in status.splitlines():
                if not line.startswith("VmRSS:"):
                    continue
                columns = line.split()
                if len(columns) >= 2 and columns[1].isdigit():
                    rss_bytes = int(columns[1]) * 1024
                break
            self.state.update_rss(rss_bytes)
        except (OSError, UnicodeError, ValueError):
            self.state.update_rss(None)

    def sample_once(self) -> None:
        self._sample_git()
        self._sample_configuration()
        self._sample_memory()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample_once()
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="codex-claude-bridge-monitor-sampler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 0.2))

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class _MonitorHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        state: MonitorState,
        asset: bytes,
        stopping: threading.Event,
        review_control: ReviewControl | None,
        control_changed: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        self.monitor_state = state
        self.monitor_asset = asset
        self.monitor_stopping = stopping
        self.review_control = review_control
        self.control_changed = control_changed
        self._delivery_condition = threading.Condition()
        self._active_event_clients = 0
        self._delivered_revision = -1
        super().__init__(server_address, _MonitorRequestHandler)
        host, port = self.server_address[:2]
        self.monitor_origin = f"http://{host}:{port}"

    def event_client_started(self) -> None:
        with self._delivery_condition:
            self._active_event_clients += 1

    def event_client_stopped(self) -> None:
        with self._delivery_condition:
            self._active_event_clients = max(
                0, self._active_event_clients - 1)
            self._delivery_condition.notify_all()

    def event_revision_delivered(self, revision: int) -> None:
        with self._delivery_condition:
            self._delivered_revision = max(
                self._delivered_revision, revision)
            self._delivery_condition.notify_all()

    def wait_for_delivery(self, revision: int, timeout: float) -> None:
        with self._delivery_condition:
            if self._active_event_clients == 0:
                return
            self._delivery_condition.wait_for(
                lambda: (
                    self._active_event_clients == 0
                    or self._delivered_revision >= revision
                ),
                timeout=max(0.0, timeout),
            )


class _MonitorRequestHandler(http.server.BaseHTTPRequestHandler):
    server: _MonitorHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'",
        )

    def _empty_error(self, status: int) -> None:
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json_response(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _control_authorized(self) -> bool:
        control = self.server.review_control
        if control is None:
            return False
        supplied = self.headers.get("X-Bridge-Control-Token")
        if not isinstance(supplied, str) or not secrets.compare_digest(
            supplied,
            control.token,
        ):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin == self.server.monitor_origin

    def _read_control_json(self) -> dict[str, Any] | None:
        self._control_response_sent = False
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            return None
        if length < 0:
            return None
        if length > 2_048:
            self.close_connection = True
            self._json_response(413, {"error": "控制请求过大"})
            self._control_response_sent = True
            return None
        try:
            raw = self.rfile.read(length)
            if self.headers.get_content_type() != "application/json":
                return None
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            body = self.server.monitor_asset
            self.send_response(200)
            self._security_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/events":
            self._serve_events()
            return
        if path == "/control/bootstrap" and self.server.review_control is not None:
            self._json_response(200, self.server.review_control.bootstrap())
            return
        self._empty_error(404)

    def _serve_events(self) -> None:
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        revision = -1
        self.server.event_client_started()
        try:
            while True:
                next_revision, snapshot = self.server.monitor_state.wait_for_revision(
                    revision, 0.25)
                if next_revision <= revision:
                    if not self.server.monitor_stopping.is_set():
                        self.wfile.write(b": heartbeat\n\n")
                else:
                    revision = next_revision
                    control = self.server.review_control
                    if control is not None:
                        snapshot["control"] = control.public_snapshot()
                    payload = json.dumps(
                        snapshot, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                    self.wfile.write(
                        f"id: {revision}\nevent: snapshot\ndata: ".encode("ascii")
                        + payload + b"\n\n"
                    )
                self.wfile.flush()
                if next_revision == revision:
                    self.server.event_revision_delivered(revision)
                if self.server.monitor_stopping.is_set():
                    break
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            return
        finally:
            self.close_connection = True
            self.server.event_client_stopped()

    def do_POST(self) -> None:
        control = self.server.review_control
        if control is None:
            self._empty_error(405)
            return
        path = urllib.parse.urlsplit(self.path).path
        if path not in {"/control/review-config", "/control/stop"}:
            self._json_response(405, {"error": "不支持此操作"})
            return
        if not self._control_authorized():
            self._json_response(403, {"error": "控制请求被拒绝"})
            return
        payload = self._read_control_json()
        if payload is None:
            if not self._control_response_sent:
                self._json_response(400, {"error": "控制请求无效"})
            return
        try:
            if path == "/control/review-config":
                if set(payload) != {"model", "effort"}:
                    raise ControlRequestError("控制请求无效", 400)
                result = control.request_selection(
                    payload.get("model"),
                    payload.get("effort"),
                    persist=self.server.control_changed,
                )
            else:
                if payload:
                    raise ControlRequestError("控制请求无效", 400)
                result = control.request_stop(
                    persist=self.server.control_changed)
        except ControlRequestError as exc:
            if exc.status_code == 400:
                self._json_response(400, {"error": "控制请求无效"})
            else:
                self._json_response(exc.status_code, {"error": str(exc)})
            return
        except (OSError, RuntimeError, ValueError, TypeError):
            self._json_response(500, {"error": "控制状态保存失败"})
            return
        self.server.monitor_state.notify_control_change()
        self._json_response(202, result)

    def do_PUT(self) -> None:
        self._empty_error(405)

    def do_PATCH(self) -> None:
        self._empty_error(405)

    def do_DELETE(self) -> None:
        self._empty_error(405)


class LiveMonitor:
    """Serve a loopback-only browser HUD for one bridge process."""

    def __init__(
        self,
        repo: Path,
        sanitizer: Callable[[Any], Any],
        *,
        open_browser: bool = True,
        browser_opener: Callable[..., bool] = webbrowser.open,
        sample_interval_seconds: float = 3.0,
        asset_path: Path | None = None,
        review_control: ReviewControl | None = None,
        control_changed: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.repo = Path(repo)
        self.state = MonitorState(self.repo, sanitizer)
        self.open_browser = open_browser
        self.browser_opener = browser_opener
        self.review_control = review_control
        self.control_changed = control_changed
        self.asset_path = (
            Path(asset_path) if asset_path is not None
            else Path(__file__).resolve().parents[1] / "assets" / "monitor.html"
        )
        self.sampler = MonitorSampler(
            self.state,
            self.repo,
            interval_seconds=sample_interval_seconds,
        )
        self._stopping = threading.Event()
        self._server: _MonitorHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._browser_thread: threading.Thread | None = None
        self._url: str | None = None

    def _open_browser(self) -> None:
        if self._url is None:
            return
        opened = False
        try:
            opened = self.browser_opener(self._url + "/", new=1)
        except (OSError, RuntimeError, webbrowser.Error):
            opened = False
        if not opened:
            self.state.publish(
                "browser_open_failed",
                {"message": "Browser did not open automatically", "level": "warning"},
            )
            print(f"[Bridge monitor] Open {self._url}/", flush=True)

    def start(self) -> str:
        if self._server is not None and self._url is not None:
            return self._url
        asset = self.asset_path.read_bytes()
        self._stopping.clear()
        server = _MonitorHTTPServer(
            ("127.0.0.1", 0),
            self.state,
            asset,
            self._stopping,
            self.review_control,
            self.control_changed,
        )
        self._server = server
        host, port = server.server_address[:2]
        self._url = f"http://{host}:{port}"
        self._server_thread = threading.Thread(
            target=server.serve_forever,
            name="codex-claude-bridge-monitor-http",
            daemon=True,
        )
        self._server_thread.start()
        self.sampler.start()
        self.state.publish(
            "monitor_started",
            {"message": "Live monitor started"},
        )
        print(f"[Bridge monitor] {self._url}/", flush=True)
        if self.open_browser:
            self._browser_thread = threading.Thread(
                target=self._open_browser,
                name="codex-claude-bridge-monitor-browser",
                daemon=True,
            )
            self._browser_thread.start()
        return self._url

    def stop(self, final_status: str | None = None) -> None:
        server = self._server
        if server is None:
            if final_status is not None:
                self.state.set_status(final_status)
            return
        if final_status is not None:
            self.state.set_status(final_status)
        self.state.publish(
            "monitor_stopped",
            {"message": "Live monitor connection ended"},
        )
        final_revision = self.state.snapshot()["revision"]
        server.wait_for_delivery(final_revision, 0.5)
        self._stopping.set()
        self.sampler.stop()
        server.shutdown()
        server.server_close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=1)
        if self._browser_thread is not None:
            self._browser_thread.join(timeout=1)
        self._server = None

    def is_alive(self) -> bool:
        return (
            self._server is not None
            and self._server_thread is not None
            and self._server_thread.is_alive()
        )
