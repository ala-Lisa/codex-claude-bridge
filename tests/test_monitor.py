from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from scripts.control import (
    DEFAULT_CODEX_SELECTION,
    CodexModelOption,
    ReviewControl,
)
from scripts.monitor import LiveMonitor, MonitorSampler, MonitorState


SECRET = "sk-monitor-secret-123456"


def sanitize(value):
    return json.loads(json.dumps(value).replace(SECRET, "[REDACTED]"))


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class MonitorStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-monitor-state-")
        self.repo = Path(self.temp.name)
        self.clock = FakeClock()
        self.state = MonitorState(self.repo, sanitize, clock=self.clock)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_deepseek_context_is_configured_then_claude_confirmed(self) -> None:
        self.state.consume_claude({
            "type": "system",
            "subtype": "init",
            "uuid": "init-1",
            "model": "deepseek-v4-pro[1m]",
            "claude_code_version": "2.1.208",
        })
        self.state.consume_claude({
            "type": "assistant",
            "uuid": "assistant-1",
            "message": {
                "usage": {
                    "input_tokens": 100,
                    "cache_creation_input_tokens": 2_000,
                    "cache_read_input_tokens": 113_900,
                    "output_tokens": 10,
                },
                "content": [],
            },
        })
        snapshot = self.state.snapshot()
        claude = snapshot["claude"]
        self.assertEqual(claude["model"], "deepseek-v4-pro[1m]")
        self.assertEqual(claude["version"], "2.1.208")
        self.assertEqual(claude["context_used_tokens"], 116_000)
        self.assertEqual(claude["context_window_tokens"], 1_000_000)
        self.assertEqual(claude["context_window_source"], "configured")
        self.assertEqual(claude["context_percent"], 11.6)

        self.state.consume_claude({
            "type": "result",
            "uuid": "result-1",
            "modelUsage": {
                "deepseek-v4-pro[1m]": {"contextWindow": 1_000_000},
            },
            "result": "done",
        })
        confirmed = self.state.snapshot()["claude"]
        self.assertEqual(confirmed["context_window_tokens"], 1_000_000)
        self.assertEqual(confirmed["context_window_source"], "claude_reported")
        self.assertIsNone(confirmed["context_warning"])

    def test_reported_context_wins_and_unknown_model_is_not_guessed(self) -> None:
        self.state.consume_claude({
            "type": "system", "subtype": "init", "uuid": "init-1",
            "model": "unknown-model", "claude_code_version": "2.1.208",
        })
        unknown = self.state.snapshot()["claude"]
        self.assertIsNone(unknown["context_window_tokens"])
        self.assertIsNone(unknown["context_percent"])
        self.assertEqual(unknown["context_window_source"], "unknown")

        self.state.consume_claude({
            "type": "system", "subtype": "init", "uuid": "init-2",
            "model": "deepseek-v4-pro[1m]", "claude_code_version": "2.1.208",
        })
        self.state.consume_claude({
            "type": "result", "uuid": "result-2",
            "modelUsage": {
                "deepseek-v4-pro[1m]": {"contextWindow": 900_000},
            },
            "result": "done",
        })
        mismatch = self.state.snapshot()["claude"]
        self.assertEqual(mismatch["context_window_tokens"], 900_000)
        self.assertEqual(mismatch["context_window_source"], "claude_reported")
        self.assertEqual(mismatch["context_warning"], "context_window_mismatch")

    def test_runtime_model_names_follow_later_stream_events(self) -> None:
        self.state.consume_claude({
            "type": "system", "subtype": "init", "uuid": "init-deepseek",
            "model": "deepseek-v4-pro[1m]",
        })
        self.state.consume_claude({
            "type": "system", "subtype": "init", "uuid": "init-other-api",
            "model": "other-api-model-v2",
        })
        self.state.consume_codex({
            "type": "thread.started", "thread_id": "review-1",
            "model": "gpt-5.6-sol",
        })
        self.state.consume_codex({
            "type": "thread.started", "thread_id": "review-2",
            "model": "future-review-model",
        })

        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["claude"]["model"], "other-api-model-v2")
        self.assertEqual(snapshot["codex"]["model"], "future-review-model")
        self.assertEqual(
            snapshot["bridge"]["codex_model"], "future-review-model")

    def test_invalid_usage_values_do_not_create_a_percentage(self) -> None:
        self.state.consume_claude({
            "type": "system", "subtype": "init", "uuid": "init-1",
            "model": "deepseek-v4-pro[1m]", "claude_code_version": "2.1.208",
        })
        for index, usage in enumerate((
            {"input_tokens": True, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0},
            {"input_tokens": -1, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0},
            {"input_tokens": "100", "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0},
        )):
            self.state.consume_claude({
                "type": "assistant", "uuid": f"bad-{index}",
                "message": {"usage": usage, "content": []},
            })
        claude = self.state.snapshot()["claude"]
        self.assertIsNone(claude["context_used_tokens"])
        self.assertIsNone(claude["context_percent"])

    def test_tools_compaction_logs_and_secrets_are_reduced_safely(self) -> None:
        command = f"python -m pytest -q TOKEN={SECRET}"
        tool_event = {
            "type": "assistant",
            "uuid": "tool-start-event",
            "message": {
                "usage": {
                    "input_tokens": 1,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                },
                "content": [
                    {"type": "text", "text": f"running {SECRET}"},
                    {"type": "tool_use", "id": "tool-1", "name": "Bash",
                     "input": {"command": command}},
                ],
            },
        }
        self.state.consume_claude(tool_event)
        self.state.consume_claude(tool_event)
        self.state.consume_claude({
            "type": "user", "uuid": "tool-result-event",
            "message": {"content": [{
                "type": "tool_result", "tool_use_id": "tool-1",
                "content": f"passed {SECRET}", "is_error": False,
            }]},
        })
        compact = {
            "type": "system", "subtype": "compact_boundary",
            "uuid": "compact-1", "compact_metadata": {"preTokens": 50000},
        }
        self.state.consume_claude(compact)
        self.state.consume_claude(compact)

        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["tools"]["started"], 1)
        self.assertEqual(snapshot["tools"]["completed"], 1)
        self.assertEqual(snapshot["tools"]["failed"], 0)
        self.assertEqual(snapshot["tools"]["running"], [])
        self.assertTrue(snapshot["tools"]["last_command_is_test"])
        self.assertIn("[REDACTED]", snapshot["tools"]["last_command"])
        self.assertEqual(snapshot["compaction"]["count"], 1)
        self.assertEqual(snapshot["compaction"]["pre_tokens"], 50000)
        self.assertNotIn(SECRET, json.dumps(snapshot))

    def test_codex_progress_excludes_reasoning_and_wait_is_revision_based(self) -> None:
        revision = self.state.snapshot()["revision"]
        self.state.publish("bridge_started", {
            "claude_model": "deepseek-v4-pro[1m]",
            "codex_model": "gpt-5.6-sol",
            "codex_reasoning_effort": "high",
        })
        self.state.consume_codex({
            "type": "thread.started", "thread_id": "thread-1",
            "reasoning": f"hidden {SECRET}",
        })
        self.state.consume_codex({
            "type": "item.started",
            "item": {"id": "item-1", "type": "command_execution",
                     "command": f"git status TOKEN={SECRET}"},
        })
        next_revision, snapshot = self.state.wait_for_revision(revision, 0.01)
        self.assertGreater(next_revision, revision)
        encoded = json.dumps(snapshot)
        self.assertNotIn("hidden", encoded)
        self.assertNotIn(SECRET, encoded)
        self.assertIn("Codex", encoded)
        self.assertEqual(snapshot["bridge"]["codex_model"], "gpt-5.6-sol")
        self.assertEqual(snapshot["bridge"]["codex_reasoning_effort"], "high")

    def test_codex_item_traffic_updates_counts_without_growing_event_feed(self) -> None:
        self.state.consume_codex({
            "type": "thread.started", "thread_id": "thread-traffic",
        })
        for index in range(100):
            item = {"id": f"item-{index}", "type": "command_execution"}
            self.state.consume_codex({"type": "item.started", "item": item})
            self.state.consume_codex({"type": "item.completed", "item": item})
        self.state.consume_codex({"type": "turn.completed"})

        snapshot = self.state.snapshot()
        codex_events = [
            item for item in snapshot["events"]
            if item["kind"].startswith("codex_")
        ]
        self.assertEqual(
            [(item["kind"], item["message"]) for item in codex_events],
            [("codex_started", "Codex 审查输出已完成，正在验证结构化结论…")],
        )
        self.assertEqual(codex_events[0]["details"]["activity_completed"], 100)
        self.assertEqual(codex_events[0]["details"]["latest_activity"], "命令执行")
        self.assertEqual(snapshot["codex"]["activity_started"], 100)
        self.assertEqual(snapshot["codex"]["activity_completed"], 100)
        self.assertEqual(snapshot["codex"]["status"], "completed")

    def test_handoff_events_preserve_full_structured_content(self) -> None:
        claude_report = "第一段\n\n- 完整列表\n- 第二项"
        self.state.publish("claude_handoff", {
            "message": claude_report,
            "attempt": 2,
        })
        self.state.publish("codex_decision", {
            "message": "Codex FAIL",
            "attempt": 2,
            "review": 1,
            "status": "FAIL",
            "evidence": ["证据一", "证据二"],
            "remaining_issues": ["问题一"],
            "next_instructions": "完整修复指令\n第二行",
            "question": "",
            "reason": "",
            "options": [],
        })

        snapshot = self.state.snapshot()
        handoffs = [
            item for item in snapshot["events"]
            if item["kind"] in {"claude_handoff", "codex_decision"}
        ]
        self.assertEqual(len(handoffs), 2)
        self.assertEqual(handoffs[0]["message"], claude_report)
        self.assertEqual(handoffs[0]["details"]["attempt"], 2)
        self.assertEqual(handoffs[1]["message"], "Codex FAIL")
        self.assertEqual(handoffs[1]["details"], {
            "attempt": 2,
            "review": 1,
            "status": "FAIL",
            "evidence": ["证据一", "证据二"],
            "remaining_issues": ["问题一"],
            "next_instructions": "完整修复指令\n第二行",
            "question": "",
            "reason": "",
            "options": [],
        })

        for status, question, reason, options in (
            ("PASS", "", "", []),
            (
                "NEEDS_INPUT",
                "是否扩大范围？",
                "需要用户授权。",
                ["保持", "扩大"],
            ),
        ):
            self.state.publish("codex_decision", {
                "message": f"Codex {status}",
                "attempt": 3 if status == "PASS" else 4,
                "review": 2 if status == "PASS" else 3,
                "status": status,
                "evidence": [f"{status} 证据"],
                "remaining_issues": [],
                "next_instructions": "",
                "question": question,
                "reason": reason,
                "options": options,
            })
        decisions = [
            item for item in self.state.snapshot()["events"]
            if item["kind"] == "codex_decision"
        ]
        self.assertEqual(
            [item["details"]["status"] for item in decisions],
            ["FAIL", "PASS", "NEEDS_INPUT"],
        )
        self.assertEqual(
            decisions[-1]["details"]["question"], "是否扩大范围？")
        self.assertEqual(
            decisions[-1]["details"]["options"], ["保持", "扩大"])

    def test_claude_handoff_replaces_the_streamed_report_in_place(self) -> None:
        report = "完整实施报告\n\n- 测试全部通过\n- 未提交"
        self.state.consume_claude({
            "type": "stream_event",
            "uuid": "claude-delta-1",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "完整实施"},
            },
        })
        self.state.consume_claude({
            "type": "assistant",
            "uuid": "claude-final-1",
            "message": {
                "usage": {"input_tokens": 1, "output_tokens": 3},
                "content": [{"type": "text", "text": report}],
            },
        })
        before = [
            item for item in self.state.snapshot()["events"]
            if item["kind"] == "claude_text"
        ]
        self.assertEqual(len(before), 1)
        sequence = before[0]["sequence"]

        self.state.publish("claude_handoff", {
            "message": report,
            "attempt": 1,
        })

        outputs = [
            item for item in self.state.snapshot()["events"]
            if item["kind"] in {"claude_text", "claude_handoff"}
        ]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["kind"], "claude_handoff")
        self.assertEqual(outputs[0]["sequence"], sequence)
        self.assertEqual(outputs[0]["message"], report)
        self.assertEqual(outputs[0]["details"]["attempt"], 1)

    def test_codex_decision_replaces_the_single_thinking_row_in_place(
        self,
    ) -> None:
        self.state.consume_codex({
            "type": "thread.started", "thread_id": "thread-1",
        })
        thinking = [
            item for item in self.state.snapshot()["events"]
            if item["kind"] == "codex_started"
        ]
        self.assertEqual(len(thinking), 1)
        sequence = thinking[0]["sequence"]
        for index in range(5):
            item = {
                "id": f"internal-{index}",
                "type": "command_execution",
                "command": f"SECRET_COMMAND_{index}",
                "reasoning": f"SECRET_REASONING_{index}",
            }
            self.state.consume_codex({"type": "item.started", "item": item})
            self.state.consume_codex({"type": "item.completed", "item": item})

        progress = [
            item for item in self.state.snapshot()["events"]
            if item["kind"] == "codex_started"
        ]
        self.assertEqual(len(progress), 1)
        self.assertEqual(progress[0]["sequence"], sequence)
        self.assertEqual(
            progress[0]["message"],
            "Codex 正在审查… 已完成 5 项；最近：命令执行完成",
        )
        self.assertEqual(progress[0]["details"]["activity_completed"], 5)
        self.assertNotIn("SECRET_COMMAND", json.dumps(progress))
        self.assertNotIn("SECRET_REASONING", json.dumps(progress))

        self.state.consume_codex({"type": "turn.completed"})
        completed = [
            item for item in self.state.snapshot()["events"]
            if item["kind"] == "codex_started"
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["sequence"], sequence)
        self.assertEqual(
            completed[0]["message"],
            "Codex 审查输出已完成，正在验证结构化结论…",
        )

        self.state.publish("codex_decision", {
            "message": "Codex FAIL",
            "attempt": 1,
            "review": 1,
            "status": "FAIL",
            "evidence": ["测试失败"],
            "remaining_issues": ["修复边界条件"],
            "next_instructions": "补测试后修复。",
            "question": "",
            "reason": "",
            "options": [],
        })

        outputs = [
            item for item in self.state.snapshot()["events"]
            if item["kind"] in {"codex_started", "codex_decision"}
        ]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["kind"], "codex_decision")
        self.assertEqual(outputs[0]["sequence"], sequence)
        self.assertEqual(outputs[0]["details"]["status"], "FAIL")
        self.assertEqual(
            outputs[0]["details"]["next_instructions"],
            "补测试后修复。",
        )

    def test_tool_completion_updates_one_command_row_in_place(self) -> None:
        self.state.consume_claude({
            "type": "assistant",
            "uuid": "tool-start",
            "message": {
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "content": [{
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Bash",
                    "input": {"command": "python -m pytest -q"},
                }],
            },
        })
        started = [
            item for item in self.state.snapshot()["events"]
            if item["kind"].startswith("tool_")
        ]
        self.assertEqual(len(started), 1)
        sequence = started[0]["sequence"]

        self.state.consume_claude({
            "type": "user",
            "uuid": "tool-result",
            "message": {"content": [{
                "type": "tool_result",
                "tool_use_id": "tool-1",
                "content": "95 passed",
                "is_error": False,
            }]},
        })

        completed = [
            item for item in self.state.snapshot()["events"]
            if item["kind"].startswith("tool_")
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["sequence"], sequence)
        self.assertEqual(completed[0]["kind"], "tool_completed")
        self.assertEqual(completed[0]["message"], "工具完成：Bash")
        self.assertEqual(
            completed[0]["details"]["command"], "python -m pytest -q")

    def test_legacy_outcome_events_do_not_duplicate_handoff_cards(self) -> None:
        self.state.publish("claude_handoff", {
            "message": "完整 Claude 报告",
            "attempt": 1,
        })
        self.state.publish("codex_decision", {
            "message": "Codex NEEDS_INPUT",
            "attempt": 1,
            "review": 1,
            "status": "NEEDS_INPUT",
            "evidence": ["证据"],
            "remaining_issues": ["待决定"],
            "next_instructions": "",
            "question": "选择哪个方案？",
            "reason": "两个方案互斥。",
            "options": ["A", "B"],
        })
        before = len(self.state.snapshot()["events"])
        self.state.publish("task_passed", {
            "message": "Codex 审核通过",
            "evidence": ["证据"],
        })
        self.state.publish("codex_handoff", {
            "message": "完整修复指令",
            "issues": ["问题"],
        })
        self.state.publish("input_required", {
            "message": "需要用户审核",
            "question": "选择哪个方案？",
            "reason": "两个方案互斥。",
            "options": ["A", "B"],
        })
        snapshot = self.state.snapshot()
        self.assertEqual(len(snapshot["events"]), before)
        self.assertEqual(snapshot["awaiting_input"], {
            "question": "选择哪个方案？",
            "reason": "两个方案互斥。",
            "options": ["A", "B"],
        })

        self.state.consume_claude({
            "type": "result",
            "uuid": "telemetry-only-result",
            "total_cost_usd": 0.5,
            "duration_api_ms": 1_000,
            "usage": {"output_tokens": 25},
            "result": "不得生成第二张交接卡",
        })
        after_result = self.state.snapshot()
        self.assertFalse(any(
            item["kind"] == "claude_result"
            for item in after_result["events"]
        ))
        self.assertEqual(after_result["claude"]["total_cost_usd"], 0.5)

    def test_elapsed_time_uses_natural_chinese_units(self) -> None:
        cases = (
            (59.9, "59.9秒", "59秒"),
            (60.0, "1分0.0秒", "1分0秒"),
            (3_599.9, "59分59.9秒", "59分59秒"),
            (3_600.0, "1小时0分0.0秒", "1小时0分0秒"),
            (3_723.4, "1小时2分3.4秒", "1小时2分3秒"),
        )
        for elapsed, event_display, total_display in cases:
            with self.subTest(elapsed=elapsed):
                clock = FakeClock(100.0)
                state = MonitorState(self.repo, sanitize, clock=clock)
                clock.value += elapsed
                state.publish("test", {"message": "timed"})
                snapshot = state.snapshot()
                self.assertEqual(
                    snapshot["events"][-1].get("elapsed_display"),
                    event_display,
                )
                self.assertEqual(
                    snapshot.get("elapsed_display"), total_display)

    def test_snapshot_is_independent_and_event_log_is_bounded(self) -> None:
        for index in range(1_050):
            self.state.publish("test", {"message": f"event-{index}"})
        first = self.state.snapshot()
        self.assertEqual(len(first["events"]), 1_000)
        first["events"].clear()
        self.assertEqual(len(self.state.snapshot()["events"]), 1_000)


class MonitorSamplerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-monitor-sampler-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Monitor Test"],
            cwd=self.repo, check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "monitor@example.invalid"],
            cwd=self.repo, check=True,
        )
        (self.repo / "tracked.txt").write_text("one\ntwo\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.repo, check=True)
        self.clock = FakeClock()
        self.state = MonitorState(self.repo, sanitize, clock=self.clock)
        self.claude_dir = self.root / "claude"
        self.proc_root = self.root / "proc"
        self.claude_dir.mkdir()
        self.proc_root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def sampler(self, **kwargs) -> MonitorSampler:
        return MonitorSampler(
            self.state,
            self.repo,
            interval_seconds=0.02,
            claude_config_dir=self.claude_dir,
            proc_root=self.proc_root,
            **kwargs,
        )

    def test_git_summary_is_read_only_and_counts_tracked_and_untracked(self) -> None:
        (self.repo / "tracked.txt").write_text(
            "one\nchanged\nthree\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("untracked secret body\n", encoding="utf-8")
        sampler = self.sampler()
        sampler.sample_once()
        git = self.state.snapshot()["git"]
        self.assertTrue(git["available"])
        self.assertEqual(git["branch"], "master")
        self.assertEqual(git["changed_files"], 2)
        self.assertEqual(git["untracked_files"], 1)
        self.assertEqual(git["additions"], 2)
        self.assertEqual(git["deletions"], 1)
        self.assertEqual(set(git["paths"]), {"tracked.txt", "new.txt"})
        self.assertNotIn("untracked secret body", json.dumps(git))

    def test_git_failure_is_unavailable_without_error_text(self) -> None:
        sampler = self.sampler(git_executable="missing-monitor-git")
        sampler.sample_once()
        git = self.state.snapshot()["git"]
        self.assertFalse(git["available"])
        self.assertIsNone(git["branch"])
        self.assertNotIn("missing-monitor-git", json.dumps(git))

    def test_memory_and_configuration_counts_do_not_store_contents(self) -> None:
        pid_dir = self.proc_root / "4321"
        pid_dir.mkdir()
        (pid_dir / "status").write_text(
            "Name:\tmock-claude\nVmRSS:\t2468 kB\nSECRET:\tvalue\n",
            encoding="utf-8",
        )
        self.state.set_claude_pid(4321)
        (self.proc_root / "meminfo").write_text(
            "MemTotal:       8000 kB\nMemAvailable:   2000 kB\n",
            encoding="utf-8",
        )
        (self.repo / "CLAUDE.md").write_text("PROJECT_SECRET", encoding="utf-8")
        (self.claude_dir / "CLAUDE.md").write_text("USER_SECRET", encoding="utf-8")
        project_rules = self.repo / ".claude" / "rules"
        project_rules.mkdir(parents=True)
        (project_rules / "one.md").write_text("RULE_SECRET", encoding="utf-8")
        user_rules = self.claude_dir / "rules"
        user_rules.mkdir()
        (user_rules / "two.md").write_text("RULE_TWO_SECRET", encoding="utf-8")
        (self.claude_dir / "settings.json").write_text(json.dumps({
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "SECRET_HOOK"},
                ]}],
                "PostToolUse": [{"hooks": [
                    {"type": "command", "command": "SECOND_SECRET_HOOK"},
                ]}],
            },
        }), encoding="utf-8")
        sampler = self.sampler()
        sampler.sample_once()
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["claude"]["rss_bytes"], 2_468 * 1024)
        self.assertEqual(snapshot["memory"]["host_total_bytes"], 8_000 * 1024)
        self.assertEqual(snapshot["memory"]["host_used_bytes"], 6_000 * 1024)
        self.assertEqual(snapshot["configuration"], {
            "available": True,
            "claude_md": 2,
            "rules": 2,
            "hooks": 2,
        })
        encoded = json.dumps(snapshot)
        for secret in (
            "PROJECT_SECRET", "USER_SECRET", "RULE_SECRET",
            "RULE_TWO_SECRET", "SECRET_HOOK", "SECOND_SECRET_HOOK",
        ):
            self.assertNotIn(secret, encoded)

    def test_partial_text_updates_one_row_and_final_assistant_is_not_duplicated(
        self,
    ) -> None:
        for index, text in enumerate(("所有", "检查", "均已通过。")):
            self.state.consume_claude({
                "type": "stream_event",
                "uuid": f"partial-{index}",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": text},
                },
            })
        streaming = [
            item for item in self.state.snapshot()["events"]
            if item["kind"] == "claude_text"
        ]
        self.assertEqual(len(streaming), 1)
        self.assertEqual(streaming[0]["message"], "所有检查均已通过。")
        self.assertTrue(streaming[0]["details"]["streaming"])

        self.state.consume_claude({
            "type": "assistant",
            "uuid": "assistant-final-text",
            "message": {
                "usage": {
                    "input_tokens": 1,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "output_tokens": 8,
                },
                "content": [{
                    "type": "text",
                    "text": "所有检查均已通过。",
                }],
            },
        })
        completed = [
            item for item in self.state.snapshot()["events"]
            if item["kind"] == "claude_text"
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["message"], "所有检查均已通过。")
        self.assertFalse(completed[0]["details"]["streaming"])

        self.state.consume_claude({
            "type": "stream_event",
            "uuid": "partial-replaced",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "旧的局部文本"},
            },
        })
        self.state.consume_claude({
            "type": "assistant",
            "uuid": "assistant-replaced",
            "message": {
                "usage": {
                    "input_tokens": 1,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "output_tokens": 4,
                },
                "content": [{"type": "text", "text": "最终权威文本"}],
            },
        })
        replaced = [
            item for item in self.state.snapshot()["events"]
            if item["kind"] == "claude_text"
        ]
        self.assertEqual(len(replaced), 2)
        self.assertEqual(replaced[-1]["message"], "最终权威文本")
        self.assertFalse(replaced[-1]["details"]["streaming"])

    def test_result_cost_output_speed_and_input_prompt_are_snapshot_fields(
        self,
    ) -> None:
        self.state.publish("bridge_started", {
            "message": "started",
            "claude_autocompact_pct_override": "50",
            "max_rounds": 12,
            "max_implementation_attempts": 12,
            "max_codex_reviews": 8,
        })
        self.state.publish("implementation_attempt_started", {
            "message": "attempt", "attempt": 3, "max_attempts": 12,
        })
        self.state.publish("codex_review_started", {
            "message": "review", "review": 1, "max_reviews": 8,
        })
        self.assertEqual(self.state.snapshot()["bridge"]["codex_reviews"], 0)
        self.state.publish("codex_review_completed", {
            "message": "review complete", "review": 1, "max_reviews": 8,
        })
        self.state.set_phase("CLAUDE_RUNNING", 1)
        self.state.consume_claude({
            "type": "result",
            "uuid": "result-telemetry",
            "total_cost_usd": 1.25,
            "duration_api_ms": 2_000,
            "usage": {"output_tokens": 100},
            "result": "done",
        })
        self.state.set_phase("AWAITING_INPUT", 1)
        self.state.publish("input_required", {
            "message": "input required",
            "question": "选择证据展示方式？",
            "reason": "两个方案互斥，不能安全代替用户决定。",
            "options": ["紧凑", "展开"],
        })
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["bridge"]["autocompact_threshold"], "50")
        self.assertEqual(snapshot["bridge"]["max_rounds"], 12)
        self.assertEqual(snapshot["bridge"]["implementation_attempts"], 3)
        self.assertEqual(snapshot["bridge"]["codex_reviews"], 1)
        self.assertEqual(
            snapshot["bridge"]["max_implementation_attempts"], 12)
        self.assertEqual(snapshot["bridge"]["max_codex_reviews"], 8)
        self.assertEqual(snapshot["claude"]["total_cost_usd"], 1.25)
        self.assertEqual(snapshot["claude"]["round_cost_usd"], 1.25)
        self.assertEqual(snapshot["claude"]["output_tokens_total"], 100)
        self.assertEqual(snapshot["claude"]["output_tokens_per_second"], 50.0)
        self.assertEqual(snapshot["claude"]["output_tokens_peak_per_second"], 50.0)
        self.assertEqual(snapshot["awaiting_input"]["question"], "选择证据展示方式？")
        self.assertEqual(snapshot["awaiting_input"]["options"], ["紧凑", "展开"])
        self.assertEqual(snapshot["workspace"]["name"], self.repo.name)
        self.assertEqual(snapshot["workspace"]["path"], str(self.repo))

    def test_missing_or_malformed_proc_status_is_unavailable(self) -> None:
        sampler = self.sampler()
        self.state.set_claude_pid(999)
        sampler.sample_once()
        self.assertIsNone(self.state.snapshot()["claude"]["rss_bytes"])
        pid_dir = self.proc_root / "999"
        pid_dir.mkdir()
        (pid_dir / "status").write_text("VmRSS:\tnot-a-number kB\n")
        sampler.sample_once()
        self.assertIsNone(self.state.snapshot()["claude"]["rss_bytes"])

    def test_prompt_cache_ttl_is_clamped(self) -> None:
        self.state.consume_claude({
            "type": "assistant", "uuid": "assistant-1",
            "message": {"usage": {
                "input_tokens": 1,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
            }, "content": []},
        })
        self.assertEqual(
            self.state.snapshot()["claude"]["prompt_cache_remaining_seconds"],
            300,
        )
        self.clock.value += 125.4
        self.assertEqual(
            self.state.snapshot()["claude"]["prompt_cache_remaining_seconds"],
            174,
        )
        self.clock.value += 500
        self.assertEqual(
            self.state.snapshot()["claude"]["prompt_cache_remaining_seconds"],
            0,
        )

    def test_sampler_thread_stops_cleanly(self) -> None:
        sampler = self.sampler()
        sampler.start()
        deadline = time.time() + 2
        while self.state.snapshot()["git"]["available"] is None and time.time() < deadline:
            time.sleep(0.01)
        sampler.stop()
        self.assertFalse(sampler.is_alive())

    def test_default_sampler_interval_is_three_seconds(self) -> None:
        sampler = MonitorSampler(self.state, self.repo)
        self.assertEqual(sampler.interval_seconds, 3.0)


class LiveMonitorServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-monitor-http-")
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.monitors: list[LiveMonitor] = []

    def tearDown(self) -> None:
        for monitor in self.monitors:
            monitor.stop("TEST_FINISHED")
        self.temp.cleanup()

    def start_monitor(self, **kwargs) -> tuple[LiveMonitor, str]:
        monitor = LiveMonitor(
            self.repo,
            sanitize,
            open_browser=False,
            sample_interval_seconds=0.05,
            **kwargs,
        )
        self.monitors.append(monitor)
        return monitor, monitor.start()

    @staticmethod
    def read_sse_snapshot(response) -> dict:
        deadline = time.time() + 3
        while time.time() < deadline:
            raw = response.readline()
            if not raw:
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise AssertionError("SSE snapshot was not received")

    def test_page_is_loopback_only_self_contained_and_hardened(self) -> None:
        monitor, url = self.start_monitor()
        self.assertTrue(url.startswith("http://127.0.0.1:"), url)
        self.assertNotEqual(url.rsplit(":", 1)[1], "0")
        with urllib.request.urlopen(url + "/", timeout=2) as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
            self.assertEqual(
                response.headers.get("X-Content-Type-Options"), "nosniff")
            csp = response.headers.get("Content-Security-Policy", "")
            self.assertIn("default-src 'none'", csp)
            self.assertIn("connect-src 'self'", csp)
        self.assertIn("桥接任务控制台", body)
        self.assertIn('class="bridge-mark"', body)
        self.assertIn('id="reviewAlert"', body)
        self.assertIn("等待用户审核", body)
        self.assertIn("data-series=\"cost\"", body)
        self.assertIn("data-series=\"speed\"", body)
        self.assertIn("setInterval(sampleTelemetry, 3000)", body)
        self.assertIn("3 秒采样 · 60 秒窗口", body)
        self.assertIn("histories[name].length > 20", body)
        self.assertIn("实施 / 审查", body)
        self.assertIn("实施 ${implementationAttempts}", body)
        self.assertIn("审查 ${codexReviews}", body)
        self.assertIn("eventRows.get", body)
        self.assertIn("elapsed_display", body)
        self.assertNotIn("toFixed(1)}秒", body)
        self.assertIn("codex_handoff", body)
        self.assertIn("Claude 完整输出", body)
        self.assertIn("Codex 完整审查输出", body)
        self.assertIn("审核证据", body)
        self.assertIn("剩余问题", body)
        self.assertIn("给 Claude 的指令", body)
        self.assertIn("复制完整内容", body)
        self.assertIn("tool_completed: '执行完成'", body)
        self.assertIn("tool_failed: '执行失败'", body)
        self.assertNotIn("handoff-card", body)
        self.assertNotIn("handoffOpenState", body)
        self.assertNotIn("function handoffShouldOpen", body)
        self.assertNotIn("<details", body)
        self.assertIn("EventSource", body)
        self.assertIn('id="codexModel"', body)
        self.assertIn('id="rulesCount"', body)
        self.assertIn('id="hooksCount"', body)
        self.assertIn('id="repoAdditions"', body)
        self.assertIn('id="repoDeletions"', body)
        self.assertIn('id="memorySub"', body)
        self.assertNotIn("https://", body)
        self.assertNotIn("http://", body)
        self.assertNotIn("localStorage", body)
        self.assertNotIn("document.cookie", body)
        self.assertNotIn("innerHTML", body)
        self.assertIn("textContent", body)
        self.assertTrue(monitor.is_alive())

    def test_page_uses_approved_single_screen_operations_layout(self) -> None:
        body = (
            Path(__file__).resolve().parents[1] / "assets" / "monitor.html"
        ).read_text(encoding="utf-8")
        self.assertLess(body.index('class="stream-pane"'), body.index('class="telemetry"'))
        self.assertLess(body.index('class="telemetry"'), body.index('id="phaseList"'))
        for value in (
            "grid-template-columns: minmax(0, 1fr) 640px",
            "grid-template-columns: minmax(0, 1fr);",
            "grid-template-columns: repeat(2, minmax(0, 1fr))",
            "grid-template-rows: repeat(3, minmax(0, 1fr))",
            'data-phase="CLAUDE_RUNNING"',
            'data-phase="VERIFYING"',
            'data-phase="CODEX_REVIEWING"',
            'document.body.dataset.phase = snapshot.phase || \'\';',
            'body[data-phase="PASS"] .state',
            'grid-template-rows: minmax(0, 1fr) 142px 112px',
            "var(--claude)",
            "var(--blue)",
            "var(--violet)",
            "#phaseList { width: min(190px, 100%); margin-inline: auto; }",
            '.phase-row.current[data-phase="CLAUDE_RUNNING"]',
            '.phase-row.current[data-phase="VERIFYING"]',
            '.phase-row.current[data-phase="CODEX_REVIEWING"]',
            'class="repo-metrics"',
            'row.append(dot, label);',
            'class="header-cell progress-cell"',
            'class="header-cell status-cell"',
            '<label>执行模型 · Claude</label>',
            '<label>审查模型 · Codex</label>',
            'claude.model || bridge.claude_model',
            'bridge.codex_model || codex.model',
            '.header-cell { text-align: center; }',
            '.header-cell.progress-cell b { white-space: nowrap; }',
            '.state { white-space: nowrap; }',
            '.header-cell.status-cell { padding-inline: 10px; }',
            "? `/${maxImplementationAttempts}`",
            "? `/${maxCodexReviews}`",
        ):
            self.assertIn(value, body)
        self.assertNotIn(".phase-row.done i", body)
        self.assertNotIn("row.append(dot, label, at);", body)
        self.assertNotIn('id="diffSummary"', body)
        self.assertRegex(
            body,
            r"\.feed\s*\{[^}]*overflow:\s*auto",
        )
        self.assertRegex(
            body,
            r"\.inspector\s*\{[^}]*overflow:\s*hidden",
        )
        self.assertRegex(
            body,
            r"\.shell\s*\{[^}]*position:\s*fixed;[^}]*inset:\s*0;",
        )

    def test_sse_emits_initial_and_live_snapshot_before_producer_finishes(self) -> None:
        monitor, url = self.start_monitor()
        finished = threading.Event()

        def delayed_publish() -> None:
            time.sleep(0.15)
            monitor.state.publish("claude_text", {"message": "live-before-finish"})
            time.sleep(0.2)
            finished.set()

        producer = threading.Thread(target=delayed_publish)
        producer.start()
        with urllib.request.urlopen(url + "/events", timeout=2) as response:
            self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            initial = self.read_sse_snapshot(response)
            live = initial
            deadline = time.time() + 2
            while "live-before-finish" not in json.dumps(live) and time.time() < deadline:
                live = self.read_sse_snapshot(response)
            self.assertGreater(live["revision"], initial["revision"])
            self.assertIn("live-before-finish", json.dumps(live))
            self.assertFalse(finished.is_set())
        producer.join(timeout=2)
        self.assertFalse(producer.is_alive())
        self.assertTrue(monitor.is_alive())

    def test_awaiting_input_snapshot_is_sent_before_monitor_shutdown(self) -> None:
        monitor, url = self.start_monitor()
        with urllib.request.urlopen(url + "/events", timeout=2) as response:
            self.read_sse_snapshot(response)
            monitor.state.set_phase("AWAITING_INPUT", 2)
            monitor.state.publish("input_required", {
                "message": "input required",
                "question": "是否扩大当前任务范围？",
                "reason": "需要新的用户授权。",
                "options": ["保持范围", "扩大范围"],
            })
            stopper = threading.Thread(
                target=monitor.stop,
                args=("AWAITING_INPUT",),
            )
            stopper.start()
            final = None
            deadline = time.time() + 3
            while time.time() < deadline:
                snapshot = self.read_sse_snapshot(response)
                if (
                    snapshot["status"] == "AWAITING_INPUT"
                    and snapshot["awaiting_input"] is not None
                ):
                    final = snapshot
                    break
            stopper.join(timeout=2)
        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final["phase"], "AWAITING_INPUT")
        self.assertEqual(
            final["awaiting_input"]["question"],
            "是否扩大当前任务范围？",
        )
        self.assertEqual(
            final["awaiting_input"]["options"],
            ["保持范围", "扩大范围"],
        )
        self.assertFalse(stopper.is_alive())
        self.assertFalse(monitor.is_alive())

    def test_unsupported_path_and_mutating_method_are_controlled(self) -> None:
        _, url = self.start_monitor()
        with self.assertRaises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(url + "/missing", timeout=2)
        self.assertEqual(missing.exception.code, 404)
        missing.exception.close()
        request = urllib.request.Request(url + "/", data=b"x", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as mutation:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(mutation.exception.code, 405)
        mutation.exception.close()

    def test_browser_open_failure_is_nonfatal_and_recorded(self) -> None:
        called: list[str] = []

        def fail_open(url: str, new: int = 0) -> bool:
            called.append(url)
            return False

        monitor = LiveMonitor(
            self.repo,
            sanitize,
            open_browser=True,
            browser_opener=fail_open,
            sample_interval_seconds=0.05,
        )
        self.monitors.append(monitor)
        url = monitor.start()
        deadline = time.time() + 2
        while not called and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(called, [url + "/"])
        deadline = time.time() + 2
        while "browser_open_failed" not in json.dumps(
            monitor.state.snapshot()) and time.time() < deadline:
            time.sleep(0.01)
        self.assertIn("browser_open_failed", json.dumps(monitor.state.snapshot()))
        self.assertTrue(monitor.is_alive())

    def test_stop_closes_server_and_worker(self) -> None:
        monitor, url = self.start_monitor()
        monitor.stop("PASS")
        self.assertFalse(monitor.is_alive())
        self.assertEqual(monitor.state.snapshot()["status"], "PASS")
        with self.assertRaises((urllib.error.URLError, TimeoutError, OSError)):
            urllib.request.urlopen(url + "/", timeout=0.2)


class LiveMonitorControlTests(LiveMonitorServerTests):
    def setUp(self) -> None:
        super().setUp()
        self.control = ReviewControl(
            (
                CodexModelOption(
                    "gpt-5.6-sol",
                    "GPT-5.6 Sol",
                    ("low", "high", "max"),
                ),
                CodexModelOption(
                    "gpt-5.6-terra",
                    "GPT-5.6 Terra",
                    ("medium", "high"),
                ),
            ),
            DEFAULT_CODEX_SELECTION,
            token="control-test-token",
        )

    def start_control_monitor(self) -> tuple[LiveMonitor, str]:
        return self.start_monitor(review_control=self.control)

    @staticmethod
    def request_json(
        url: str,
        path: str,
        payload: object,
        *,
        token: str = "control-test-token",
        origin: str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": content_type,
            "X-Bridge-Control-Token": token,
        }
        if origin is not None:
            headers["Origin"] = origin
        request = urllib.request.Request(
            url + path,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                body = json.loads(exc.read())
                return exc.code, body

    def test_bootstrap_and_sse_never_expose_token_in_public_snapshot(self) -> None:
        _, url = self.start_control_monitor()

        with urllib.request.urlopen(
            url + "/control/bootstrap", timeout=2
        ) as response:
            bootstrap = json.loads(response.read())
        self.assertEqual(bootstrap["token"], "control-test-token")
        self.assertEqual(bootstrap["models"][0]["slug"], "gpt-5.6-sol")
        with urllib.request.urlopen(url + "/events", timeout=2) as response:
            snapshot = self.read_sse_snapshot(response)
        self.assertEqual(snapshot["control"]["effective"]["model"], "gpt-5.6-sol")
        self.assertNotIn("token", snapshot["control"])
        self.assertNotIn("control-test-token", json.dumps(snapshot))

    def test_same_origin_token_selection_and_one_restart_contract(self) -> None:
        _, url = self.start_control_monitor()
        self.control.mark_review_started(4)

        status, body = self.request_json(
            url,
            "/control/review-config",
            {"model": "gpt-5.6-sol", "effort": "high"},
            origin=url,
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["mode"], "restart")
        status, body = self.request_json(
            url,
            "/control/review-config",
            {"model": "gpt-5.6-sol", "effort": "max"},
            origin=url,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "本轮审核已重启过一次"})

    def test_control_change_callback_receives_public_snapshot_without_token(
        self,
    ) -> None:
        observed: list[dict] = []
        _, url = self.start_monitor(
            review_control=self.control,
            control_changed=observed.append,
        )

        status, _ = self.request_json(
            url,
            "/control/review-config",
            {"model": "gpt-5.6-sol", "effort": "high"},
            origin=url,
        )

        self.assertEqual(status, 202)
        self.assertEqual(observed[-1]["pending"], {
            "model": "gpt-5.6-sol", "effort": "high",
        })
        self.assertNotIn("token", observed[-1])
        self.assertNotIn("control-test-token", json.dumps(observed[-1]))

    def test_control_persistence_failure_rolls_back_selection_and_stop(
        self,
    ) -> None:
        def fail_persistence(snapshot: dict) -> None:
            raise OSError("SECRET_PERSISTENCE")

        _, url = self.start_monitor(
            review_control=self.control,
            control_changed=fail_persistence,
        )
        select_status, select_body = self.request_json(
            url,
            "/control/review-config",
            {"model": "gpt-5.6-sol", "effort": "high"},
            origin=url,
        )
        self.assertEqual(select_status, 500)
        self.assertEqual(select_body, {"error": "控制状态保存失败"})
        self.assertIsNone(self.control.public_snapshot()["pending"])
        self.assertIsNone(self.control.take_action())

        stop_status, stop_body = self.request_json(
            url, "/control/stop", {}, origin=url)
        self.assertEqual(stop_status, 500)
        self.assertEqual(stop_body, {"error": "控制状态保存失败"})
        snapshot = self.control.public_snapshot()
        self.assertFalse(snapshot["stop_requested"])
        self.assertEqual(snapshot["phase"], "IDLE")
        self.assertIsNone(self.control.take_action())
        self.assertNotIn(
            "SECRET_PERSISTENCE", json.dumps([select_body, stop_body]))

    def test_token_and_origin_fail_with_fixed_403(self) -> None:
        _, url = self.start_control_monitor()
        for token, origin in (
            ("wrong", url),
            ("control-test-token", "http://attacker.invalid"),
        ):
            with self.subTest(token=token, origin=origin):
                status, body = self.request_json(
                    url,
                    "/control/review-config",
                    {"model": "gpt-5.6-sol", "effort": "max"},
                    token=token,
                    origin=origin,
                )
                self.assertEqual(status, 403)
                self.assertEqual(body, {"error": "控制请求被拒绝"})

    def test_invalid_json_fields_model_and_content_type_are_fixed_400(self) -> None:
        _, url = self.start_control_monitor()
        cases = (
            ({"model": "gpt-5.6-sol"}, "application/json"),
            ({"model": "SECRET", "effort": "max"}, "application/json"),
            ({"model": "gpt-5.6-sol", "effort": "max", "x": 1},
             "application/json"),
            ({"model": "gpt-5.6-sol", "effort": "max"}, "text/plain"),
        )
        for payload, content_type in cases:
            with self.subTest(payload=payload, content_type=content_type):
                status, body = self.request_json(
                    url,
                    "/control/review-config",
                    payload,
                    origin=url,
                    content_type=content_type,
                )
                self.assertEqual(status, 400)
                self.assertEqual(body, {"error": "控制请求无效"})
                self.assertNotIn("SECRET", json.dumps(body))

        request = urllib.request.Request(
            url + "/control/review-config",
            data=b"{not-json",
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Control-Token": "control-test-token",
                "Origin": url,
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        with caught.exception:
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(
                json.loads(caught.exception.read()),
                {"error": "控制请求无效"},
            )

    def test_body_limit_and_unrelated_mutation_are_rejected(self) -> None:
        _, url = self.start_control_monitor()
        request = urllib.request.Request(
            url + "/control/review-config",
            data=b"x" * 2049,
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Control-Token": "control-test-token",
                "Origin": url,
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as too_large:
            urllib.request.urlopen(request, timeout=2)
        with too_large.exception:
            self.assertEqual(too_large.exception.code, 413)
            self.assertEqual(
                json.loads(too_large.exception.read()),
                {"error": "控制请求过大"},
            )

        status, body = self.request_json(url, "/unrelated", {}, origin=url)
        self.assertEqual(status, 405)
        self.assertEqual(body, {"error": "不支持此操作"})

    def test_stop_is_idempotent_and_overrides_pending_restart(self) -> None:
        _, url = self.start_control_monitor()
        self.control.mark_review_started(1)
        self.request_json(
            url,
            "/control/review-config",
            {"model": "gpt-5.6-sol", "effort": "high"},
            origin=url,
        )

        first_status, first = self.request_json(
            url, "/control/stop", {}, origin=url)
        second_status, second = self.request_json(
            url, "/control/stop", {}, origin=url)

        self.assertEqual(first_status, 202)
        self.assertEqual(second_status, 202)
        self.assertEqual(first, second)
        action = self.control.take_action()
        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.kind, "stop")


class LiveMonitorControlPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = (
            Path(__file__).resolve().parents[1] / "assets" / "monitor.html"
        ).read_text(encoding="utf-8")

    def test_page_contains_fixed_model_reasoning_and_stop_controls(self) -> None:
        for value in (
            'id="controlModel"',
            'id="controlEffort"',
            'id="controlApply"',
            'id="controlStop"',
            'id="controlConfirmation"',
            "审查控制",
            "立即重启审核",
            "应用到下次审核",
            "终止任务",
            "最高",
            "审核质量优先，通常消耗更多额度",
            'id="controlResume"',
            "使用原 run 命令追加 --resume",
            "/control/bootstrap",
            "/control/review-config",
            "/control/stop",
            "X-Bridge-Control-Token",
        ):
            self.assertIn(value, self.body)
        self.assertNotIn("innerHTML", self.body)
        self.assertNotIn("localStorage", self.body)
        self.assertNotIn("document.cookie", self.body)
        self.assertNotIn('type="text" name="model"', self.body)

    def test_streaming_badge_is_white_and_cursor_matches_actor(self) -> None:
        self.assertIn(
            "border: 1px solid rgba(255, 255, 255, .72);",
            self.body,
        )
        self.assertIn("color: #fff;", self.body)
        self.assertIn(
            '.event[data-kind="codex"].is-streaming .message::after {\n'
            "      background: var(--violet);\n"
            "    }",
            self.body,
        )
        self.assertIn(
            ".event.is-streaming .message::after {",
            self.body,
        )
        self.assertIn("background: var(--claude);", self.body)

    @unittest.skipUnless(shutil.which("node"), "node is unavailable")
    def test_control_state_helper_covers_review_restart_and_stopped_states(
        self,
    ) -> None:
        match = re.search(
            r"// CONTROL_STATE_HELPER_START(.*?)// CONTROL_STATE_HELPER_END",
            self.body,
            re.S,
        )
        self.assertIsNotNone(match)
        assert match is not None
        script = match.group(1) + r'''
const assert = (condition, message) => {
  if (!condition) throw new Error(message);
};
let value = controlButtonState({phase: 'IDLE', restart_used: false,
  stop_requested: false, stopped: false});
assert(formatControlModelLabel('GPT-5.6-Sol') === '⚡ GPT-5.6 Sol',
  'sol display label');
assert(formatControlModelLabel('GPT-5.6-Terra') === '⚡ GPT-5.6 Terra',
  'terra display label');
assert(formatControlModelLabel('GPT-5.6-Luna') === '⚡ GPT-5.6 Luna',
  'luna display label');
assert(formatControlModelLabel('GPT-5.5') === '⚡ GPT-5.5',
  'plain display label');
assert(value.applyLabel === '应用到下次审核', 'idle label');
assert(value.applyDisabled === false, 'idle enabled');
value = controlButtonState({phase: 'CODEX_REVIEWING', restart_used: false,
  stop_requested: false, stopped: false});
assert(value.applyLabel === '立即重启审核', 'review label');
assert(value.applyDisabled === false, 'review enabled');
value = controlButtonState({phase: 'CODEX_REVIEWING', restart_used: true,
  stop_requested: false, stopped: false});
assert(value.applyDisabled === true, 'restart budget');
assert(value.statusLabel === '本轮切换次数已用完', 'budget label');
value = controlButtonState({phase: 'USER_STOPPING', restart_used: false,
  stop_requested: true, stopped: false});
assert(value.applyDisabled && value.stopDisabled, 'stopping disabled');
assert(value.statusLabel === '正在终止任务', 'stopping label');
value = controlButtonState({phase: 'USER_STOPPED', restart_used: false,
  stop_requested: true, stopped: true});
assert(value.applyDisabled && value.stopDisabled, 'stopped disabled');
assert(value.statusLabel === '任务已终止', 'stopped label');
'''
        result = subprocess.run(
            [shutil.which("node") or "node", "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
