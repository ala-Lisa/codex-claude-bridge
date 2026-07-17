#!/usr/bin/env python3
"""Bounded local coordinator for Codex review and Claude implementation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .control import (
        DEFAULT_CODEX_SELECTION,
        CodexControlSelection,
        CodexModelOption,
        ControlConfigError,
        ReviewControl,
        browser_codex_model_catalog,
        load_codex_model_catalog,
    )
    from .monitor import LiveMonitor
else:
    from control import (
        DEFAULT_CODEX_SELECTION,
        CodexControlSelection,
        CodexModelOption,
        ControlConfigError,
        ReviewControl,
        browser_codex_model_catalog,
        load_codex_model_catalog,
    )
    from monitor import LiveMonitor


DEFAULT_CLAUDE_TOOLS = (
    "Read,Edit,Write,Glob,Grep,"
    "Bash(git status *),Bash(git diff *),Bash(git ls-files *),"
    "Bash(rtk git status *),Bash(rtk git diff *),Bash(rtk git ls-files *),"
    "Bash(python3 -m pytest *),Bash(pytest *),"
    "Bash(npm test *),Bash(npm run test *),Bash(npm run lint *),"
    "Bash(npm run build *)"
)
DEFAULT_CLAUDE_DISALLOWED_TOOLS = "Agent"
REDACTIONS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret)\s*[=:]\s*)[^\s,;\"']+"), r"\1[REDACTED]"),
    (re.compile(
        r"(?i)(\b[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|"
        r"CREDENTIAL|AUTHORIZATION)[A-Z0-9_]*\b\s*[=:]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    ), r"\1[REDACTED]"),
    (re.compile(r"\b(?:sk|dsk)-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_TOKEN]"),
)
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|token|secret|password|passwd|credential|"
    r"authorization)(?:$|[_-])"
)
MAX_USER_ANSWER_BYTES = 65_536
MAX_VERIFICATION_FILE_BYTES = 65_536
MAX_VERIFICATION_COMMANDS = 20
MAX_VERIFICATION_ARGUMENTS = 64
MAX_VERIFICATION_OUTPUT_CHARS = 20_000
DEFAULT_MAX_IMPLEMENTATION_ATTEMPTS = 12
DEFAULT_MAX_CODEX_REVIEWS = 8
DEFAULT_VERIFICATION_TIMEOUT = 1_800
SHELL_CONTROL_ARGUMENTS = frozenset({
    "&&", "||", ";", "|", ">", ">>", "<", "<<", "&",
})


class BridgeError(RuntimeError):
    pass


class AwaitingInputError(BridgeError):
    """A resume attempt cannot proceed without a safe human answer."""


class UserStopRequested(BridgeError):
    """The user requested a normal, durable browser stop."""


class ReviewRestartRequested(BridgeError):
    """The active Codex review must resume with a new configuration."""

    def __init__(self, selection: CodexControlSelection) -> None:
        super().__init__("Codex review restart requested")
        self.selection = selection


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(text: str) -> str:
    environment_secrets = {
        value
        for key, value in os.environ.items()
        if value and SENSITIVE_KEY_RE.search(key)
    }
    for secret in sorted(environment_secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED_ENV]")
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_value(value: Any, *, key: str | None = None) -> Any:
    """Return a JSON-compatible value with credential-like content removed."""
    if key is not None and SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def atomic_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_verification_manifest(
    path_value: str,
) -> tuple[str, tuple[tuple[str, ...], ...]]:
    """Load one approved, credential-free command manifest."""
    if not isinstance(path_value, str) or not path_value.strip():
        raise BridgeError("verification file is invalid")
    try:
        path = Path(path_value).expanduser().resolve()
        if not path.is_file():
            raise BridgeError("verification file is invalid")
        raw = path.read_bytes()
    except BridgeError:
        raise
    except (OSError, ValueError) as exc:
        raise BridgeError("verification file is invalid") from exc
    if not raw or len(raw) > MAX_VERIFICATION_FILE_BYTES:
        raise BridgeError("verification file is invalid")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError("verification file is invalid") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"version", "commands"}:
        raise BridgeError("verification file is invalid")
    if type(parsed["version"]) is not int or parsed["version"] != 1:
        raise BridgeError("verification file is invalid")
    raw_commands = parsed["commands"]
    if (
        not isinstance(raw_commands, list)
        or not 1 <= len(raw_commands) <= MAX_VERIFICATION_COMMANDS
    ):
        raise BridgeError("verification file is invalid")
    commands: list[tuple[str, ...]] = []
    for raw_command in raw_commands:
        if (
            not isinstance(raw_command, list)
            or not 1 <= len(raw_command) <= MAX_VERIFICATION_ARGUMENTS
        ):
            raise BridgeError("verification file is invalid")
        command: list[str] = []
        for argument in raw_command:
            if (
                not isinstance(argument, str)
                or not argument
                or not argument.strip()
                or "\x00" in argument
                or "\n" in argument
                or "\r" in argument
                or argument in SHELL_CONTROL_ARGUMENTS
                or redact(argument) != argument
            ):
                raise BridgeError("verification file is invalid")
            command.append(argument)
        commands.append(tuple(command))
    return hashlib.sha256(raw).hexdigest(), tuple(commands)


def _command_path(command: str, environ: dict[str, str]) -> Path | None:
    expanded = Path(command).expanduser()
    if expanded.parent != Path(".") or expanded.is_absolute():
        return expanded.resolve() if expanded.exists() else None
    located = shutil.which(command, path=environ.get("PATH"))
    return Path(located).resolve() if located else None


def _is_windows_codex_launcher(path: Path) -> bool:
    if path.suffix.lower() == ".exe":
        return True
    try:
        prefix = path.read_bytes()[:8192]
    except OSError:
        return False
    if prefix.startswith(b"\x7fELF"):
        return False
    text = prefix.decode("utf-8", errors="ignore").lower()
    return "codex.exe" in text and ("/mnt/" in text or "\\" in text)


def _native_vscode_codex_candidates(home: Path) -> list[Path]:
    candidates: list[Path] = []
    extension_root = home / ".vscode-server" / "extensions"
    for path in extension_root.glob("openai.chatgpt-*/bin/linux-*/codex"):
        try:
            if path.is_file() and os.access(path, os.X_OK) and path.read_bytes()[:4] == b"\x7fELF":
                candidates.append(path.resolve())
        except OSError:
            continue
    return candidates


def resolve_codex_binary(
    explicit: str | None,
    *,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
) -> str:
    """Select a native Codex executable and refuse Windows launchers in WSL."""
    environment = dict(os.environ if environ is None else environ)
    in_wsl = bool(environment.get("WSL_DISTRO_NAME"))
    requested = explicit or environment.get("CODEX_BRIDGE_CODEX_BIN")
    if requested:
        path = _command_path(requested, environment)
        if path is None or not path.is_file() or not os.access(path, os.X_OK):
            raise BridgeError("Codex executable is unavailable")
        if in_wsl and _is_windows_codex_launcher(path):
            raise BridgeError("native Linux Codex executable is required in WSL")
        return str(path)

    if in_wsl:
        candidates = _native_vscode_codex_candidates(home or Path.home())
        if candidates:
            return str(max(
                candidates,
                key=lambda path: (path.stat().st_mtime_ns, str(path)),
            ))

    path = _command_path("codex", environment)
    if path is None:
        raise BridgeError("Codex executable is unavailable")
    if in_wsl and _is_windows_codex_launcher(path):
        raise BridgeError("native Linux Codex executable is required in WSL")
    return str(path)


class Bridge:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.repo = Path(args.repo).expanduser().resolve()
        self.root = self.repo / ".codex-bridge"
        self.state_path = self.root / "state.json"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / "lock"
        self.prompts = self.root / "prompts"
        self.outputs = self.root / "outputs"
        self.claude_bin = (
            args.claude_bin
            or os.environ.get("CODEX_BRIDGE_CLAUDE_BIN")
            or "claude"
        )
        self.codex_bin = ""
        self.requested_codex_bin = args.codex_bin
        self.claude_model = args.claude_model
        self.codex_model = args.codex_model
        self.codex_reasoning_effort = args.codex_reasoning_effort
        self.codex_catalog: tuple[CodexModelOption, ...] = ()
        self.review_control: ReviewControl | None = None
        self.requested_max_codex_reviews = self._requested_codex_limit()
        self.requested_max_implementation_attempts = (
            args.max_implementation_attempts
        )
        self.verification_commands: tuple[tuple[str, ...], ...] = ()
        self.compact_threshold = os.environ.get(
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "50")
        self.state: dict[str, Any] = {}
        self.lock_acquired = False
        self.lock_owner: str | None = None
        self.user_answer: str | None = None
        self.answer_context: dict[str, Any] | None = None
        self.monitor: LiveMonitor | None = None
        self.monitor_url: str | None = None
        self.monitor_failed = False
        self._claude_terminal_open = False
        self._claude_terminal_text = ""
        self._claude_terminal_last_text: str | None = None
        self._process_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._active_process: subprocess.Popen[str] | None = None
        self._active_process_kind: str | None = None

    def _requested_codex_limit(self) -> int | None:
        legacy = self.args.max_rounds
        explicit = self.args.max_codex_reviews
        if legacy is not None and explicit is not None and legacy != explicit:
            raise BridgeError("Codex review limits conflict")
        return explicit if explicit is not None else legacy

    def _select_new_codex_configuration(self) -> None:
        model = self.codex_model or DEFAULT_CODEX_SELECTION.model
        effort = (
            self.codex_reasoning_effort or DEFAULT_CODEX_SELECTION.effort
        )
        option = next(
            (item for item in self.codex_catalog if item.slug == model),
            None,
        )
        if option is None or effort not in option.reasoning_efforts:
            raise BridgeError("Codex 模型配置无效")
        self.codex_model = model
        self.codex_reasoning_effort = effort

    def initialize(self) -> None:
        if not self.args.approved:
            raise BridgeError("repository-specific plan is not approved; pass --approved only after approval")
        self._validate_runtime_options()
        try:
            self.codex_catalog = load_codex_model_catalog(
                Path.home() / ".codex" / "models_cache.json")
        except ControlConfigError as exc:
            raise BridgeError(str(exc)) from exc
        if not self.args.resume:
            self._select_new_codex_configuration()
        self.codex_bin = resolve_codex_binary(self.requested_codex_bin)
        probe = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--is-inside-work-tree"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if probe.returncode or probe.stdout.strip() != "true":
            raise BridgeError(f"not a Git repository: {self.repo}")
        self.root.mkdir(exist_ok=True)
        self.prompts.mkdir(exist_ok=True)
        self.outputs.mkdir(exist_ok=True)
        self._exclude_local_state()
        self.acquire_lock()
        if self.args.resume:
            if not self.state_path.exists():
                raise BridgeError("cannot resume: state.json does not exist")
            self.state = read_json(self.state_path)
            if self.state.get("status") == "PASS":
                raise BridgeError("task already passed")
            self._restore_limits_and_verification()
            self._restore_runtime_options()
            self.state["claude_autocompact_pct_override"] = redact(
                self.compact_threshold)
            if self.state.get("status") == "AWAITING_INPUT":
                if not self.args.user_answer_file:
                    raise AwaitingInputError(
                        "user answer file is required while awaiting input")
                self.answer_context = self._validate_awaiting_input()
                self.user_answer = self._read_user_answer(
                    self.args.user_answer_file)
                self.state.pop("awaiting_input", None)
                self.state.pop("stop_reason", None)
                self.state["status"] = "APPROVED"
                self.save_state()
                self.event("user_answer_received", round=self.state.get("round"))
            elif self.args.user_answer_file:
                raise BridgeError(
                    "user answer file is only valid while awaiting input")
        else:
            if self.args.user_answer_file:
                raise BridgeError("user answer file requires --resume")
            if self.state_path.exists() and read_json(self.state_path).get("status") not in {"PASS", "STOPPED"}:
                raise BridgeError("an unfinished task state already exists; use --resume")
            dirty = self.git(["status", "--porcelain=v1"])
            if dirty.strip() and not self.args.allow_dirty:
                raise BridgeError("worktree is dirty; preserve existing work or obtain approval for --allow-dirty")
            task = Path(self.args.task_file).expanduser().resolve().read_text(encoding="utf-8")
            plan = Path(self.args.plan_file).expanduser().resolve().read_text(encoding="utf-8")
            verification_digest: str | None = None
            if self.args.verification_file is not None:
                verification_digest, self.verification_commands = (
                    load_verification_manifest(self.args.verification_file)
                )
            max_codex_reviews = (
                self.requested_max_codex_reviews
                if self.requested_max_codex_reviews is not None
                else DEFAULT_MAX_CODEX_REVIEWS
            )
            max_implementation_attempts = (
                self.requested_max_implementation_attempts
                if self.requested_max_implementation_attempts is not None
                else DEFAULT_MAX_IMPLEMENTATION_ATTEMPTS
            )
            self.state = {
                "version": 3,
                "task_id": str(uuid.uuid4()),
                "repo": str(self.repo),
                "status": "APPROVED",
                "round": 0,
                "max_rounds": max_codex_reviews,
                "implementation_attempts": 0,
                "codex_reviews": 0,
                "codex_review_restarts": 0,
                "active_review_restart_used": False,
                "active_codex_review_no": None,
                "max_implementation_attempts": max_implementation_attempts,
                "max_codex_reviews": max_codex_reviews,
                "verification_manifest_sha256": verification_digest,
                "verification_commands": sanitize_value(
                    self.verification_commands),
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "claude_session_id": str(uuid.uuid4()),
                "codex_session_id": None,
                "task": redact(task),
                "approved_plan": redact(plan),
                "baseline_status": redact(dirty),
                "claude_autocompact_pct_override": redact(
                    self.compact_threshold),
                "claude_model": self.claude_model,
                "codex_model": self.codex_model,
                "codex_reasoning_effort": self.codex_reasoning_effort,
                "codex_binary": self.codex_bin,
            }
            self.save_state()
        self.state.setdefault("codex_review_restarts", 0)
        self.state.setdefault("active_review_restart_used", False)
        self.state.setdefault("active_codex_review_no", None)
        if not isinstance(self.codex_model, str) or not isinstance(
            self.codex_reasoning_effort, str
        ):
            raise BridgeError("Codex 模型配置无效")
        try:
            control_catalog = browser_codex_model_catalog(self.codex_catalog)
            self.review_control = ReviewControl(
                control_catalog,
                CodexControlSelection(
                    self.codex_model,
                    self.codex_reasoning_effort,
                ),
                allow_legacy_effective=(
                    self.args.resume
                    or all(
                        item.slug != self.codex_model
                        for item in control_catalog
                    )
                ),
            )
        except (ControlConfigError, RuntimeError) as exc:
            raise BridgeError("Codex 模型配置无效") from exc
        pending_model = self.state.get("pending_codex_model")
        pending_effort = self.state.get("pending_codex_reasoning_effort")
        if pending_model is not None or pending_effort is not None:
            try:
                self.review_control.request_selection(
                    pending_model,
                    pending_effort,
                )
            except RuntimeError as exc:
                raise BridgeError("saved Codex model configuration is invalid") from exc

    def _restore_limits_and_verification(self) -> None:
        saved_codex_limit = self.state.get("max_codex_reviews")
        if type(saved_codex_limit) is not int or saved_codex_limit < 1:
            legacy_limit = self.state.get("max_rounds")
            if type(legacy_limit) is not int or legacy_limit < 1:
                raise BridgeError("saved review limit is invalid")
            saved_codex_limit = legacy_limit
        saved_attempt_limit = self.state.get("max_implementation_attempts")
        if type(saved_attempt_limit) is not int or saved_attempt_limit < 1:
            saved_attempt_limit = max(
                DEFAULT_MAX_IMPLEMENTATION_ATTEMPTS,
                saved_codex_limit * 2,
            )
        if (
            self.requested_max_codex_reviews is not None
            and self.requested_max_codex_reviews != saved_codex_limit
        ):
            self._raise_resume_configuration_error(
                "resume Codex review limit does not match saved state")
        if (
            self.requested_max_implementation_attempts is not None
            and self.requested_max_implementation_attempts != saved_attempt_limit
        ):
            self._raise_resume_configuration_error(
                "resume implementation limit does not match saved state")
        attempts = self.state.get("implementation_attempts")
        if type(attempts) is not int or attempts < 0:
            legacy_round = self.state.get("round", 0)
            attempts = legacy_round if type(legacy_round) is int else 0
        reviews = self.state.get("codex_reviews")
        if type(reviews) is not int or reviews < 0:
            legacy_round = self.state.get("round", 0)
            reviews = legacy_round if type(legacy_round) is int else 0

        saved_commands = self.state.get("verification_commands", [])
        if not isinstance(saved_commands, list) or not all(
            isinstance(command, list) and command and all(
                isinstance(argument, str) and argument
                for argument in command
            )
            for command in saved_commands
        ):
            raise BridgeError("saved verification commands are invalid")
        saved_digest = self.state.get("verification_manifest_sha256")
        if saved_digest is not None and (
            not isinstance(saved_digest, str) or len(saved_digest) != 64
        ):
            raise BridgeError("saved verification manifest is invalid")
        if self.args.verification_file is not None:
            supplied_digest, supplied_commands = load_verification_manifest(
                self.args.verification_file)
            if saved_digest is None or supplied_digest != saved_digest:
                self._raise_resume_configuration_error(
                    "resume verification manifest does not match saved state")
            if tuple(tuple(item) for item in saved_commands) != supplied_commands:
                self._raise_resume_configuration_error(
                    "resume verification manifest does not match saved state")
        self.verification_commands = tuple(
            tuple(argument for argument in command)
            for command in saved_commands
        )
        self.state.update({
            "version": 3,
            "implementation_attempts": attempts,
            "codex_reviews": reviews,
            "max_implementation_attempts": saved_attempt_limit,
            "max_codex_reviews": saved_codex_limit,
            "max_rounds": saved_codex_limit,
            "verification_commands": sanitize_value(
                self.verification_commands),
            "verification_manifest_sha256": saved_digest,
        })
        self.save_state()

    def _raise_resume_configuration_error(self, message: str) -> None:
        if self.state.get("status") == "AWAITING_INPUT":
            raise AwaitingInputError(message)
        raise BridgeError(message)

    def _validate_runtime_options(self) -> None:
        for value in (self.claude_model, self.codex_model):
            if value is not None and (not value.strip() or value != value.strip()):
                raise BridgeError("model selection is invalid")

    def _restore_runtime_options(self) -> None:
        for state_key, attribute in (
            ("claude_model", "claude_model"),
            ("codex_model", "codex_model"),
            ("codex_reasoning_effort", "codex_reasoning_effort"),
        ):
            saved = self.state.get(state_key)
            requested = getattr(self, attribute)
            if saved is not None and requested is not None and saved != requested:
                error = "resume model configuration does not match saved state"
                if self.state.get("status") == "AWAITING_INPUT":
                    raise AwaitingInputError(error)
                raise BridgeError(error)
            setattr(self, attribute, saved if saved is not None else requested)
        self.state["claude_model"] = self.claude_model
        self.state["codex_model"] = self.codex_model
        self.state["codex_reasoning_effort"] = self.codex_reasoning_effort
        self.state["codex_binary"] = self.codex_bin

    def _validate_awaiting_input(self) -> dict[str, Any]:
        value = self.state.get("awaiting_input")
        if not isinstance(value, dict):
            raise AwaitingInputError("awaiting-input state is invalid")
        question = value.get("question")
        reason = value.get("reason")
        options = value.get("options")
        if not isinstance(question, str) or not question.strip():
            raise AwaitingInputError("awaiting-input state is invalid")
        if not isinstance(reason, str) or not reason.strip():
            raise AwaitingInputError("awaiting-input state is invalid")
        if not isinstance(options, list) or not all(
            isinstance(item, str) and item.strip() for item in options
        ):
            raise AwaitingInputError("awaiting-input state is invalid")
        return {"question": question, "reason": reason, "options": options}

    def _read_user_answer(self, answer_path: str) -> str:
        try:
            path = Path(answer_path).expanduser().resolve()
            if not path.is_file() or path.stat().st_size > MAX_USER_ANSWER_BYTES:
                raise AwaitingInputError("user answer file is invalid")
            answer = path.read_text(encoding="utf-8")
        except AwaitingInputError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise AwaitingInputError("user answer file is invalid") from exc
        if not answer.strip():
            raise AwaitingInputError("user answer file is empty")
        if redact(answer) != answer:
            raise AwaitingInputError(
                "user answer file contains credential-like content")
        return answer

    def _exclude_local_state(self) -> None:
        resolved = self.git(["rev-parse", "--git-path", "info/exclude"]).strip()
        exclude = Path(resolved)
        if not exclude.is_absolute():
            exclude = self.repo / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        entry = ".codex-bridge/"
        if entry not in {line.strip() for line in current.splitlines()}:
            with exclude.open("a", encoding="utf-8") as fh:
                if current and not current.endswith("\n"):
                    fh.write("\n")
                fh.write(entry + "\n")

    def acquire_lock(self) -> None:
        owner = f"{os.getpid()}:{uuid.uuid4()}"
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            owner = self.lock_path.read_text(encoding="utf-8", errors="replace").strip()
            raise BridgeError(f"bridge lock already exists (owner {owner or 'unknown'})") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(owner + "\n")
        self.lock_owner = owner
        self.lock_acquired = True

    def release_lock(self) -> None:
        if not self.lock_acquired:
            return
        try:
            current = self.lock_path.read_text(
                encoding="utf-8", errors="replace").strip()
            if current == self.lock_owner:
                self.lock_path.unlink()
        except FileNotFoundError:
            pass
        self.lock_acquired = False
        self.lock_owner = None

    def start_monitor(self) -> None:
        if self.args.no_monitor or self.monitor_failed:
            return
        try:
            monitor = LiveMonitor(
                self.repo,
                sanitize_value,
                open_browser=not self.args.no_open_browser,
                review_control=self.review_control,
                control_changed=self._persist_control_snapshot,
            )
            self.monitor_url = monitor.start()
            self.monitor = monitor
            self.state["monitor_url"] = self.monitor_url
            self.save_state()
        except (OSError, RuntimeError, ValueError) as exc:
            self.monitor = None
            self.monitor_failed = True
            self.monitor_url = None
            message = redact(str(exc))
            self.event("monitor_failed", reason="live monitor unavailable")
            print(
                f"[Bridge monitor] unavailable ({message})",
                file=sys.stderr,
                flush=True,
            )

    def _monitor_phase(self, phase: str, round_no: int | None = None) -> None:
        if self.review_control is not None and phase != "CODEX_REVIEWING":
            self.review_control.set_phase(phase)
        if self.monitor is None:
            return
        try:
            self.monitor.state.set_phase(phase, round_no)
        except (OSError, RuntimeError, ValueError, TypeError):
            self.monitor_failed = True

    def _monitor_claude(self, event: dict[str, Any]) -> None:
        if self.monitor is None or self.monitor_failed:
            return
        try:
            self.monitor.state.consume_claude(event)
        except (OSError, RuntimeError, ValueError, TypeError):
            self.monitor_failed = True

    def _monitor_codex(self, event: dict[str, Any]) -> None:
        if self.monitor is None or self.monitor_failed:
            return
        try:
            self.monitor.state.consume_codex(event)
        except (OSError, RuntimeError, ValueError, TypeError):
            self.monitor_failed = True

    def _monitor_claude_pid(self, pid: int | None) -> None:
        if self.monitor is None or self.monitor_failed:
            return
        try:
            self.monitor.state.set_claude_pid(pid)
        except (OSError, RuntimeError, ValueError, TypeError):
            self.monitor_failed = True

    def finish_monitor(self, status: str) -> None:
        monitor = self.monitor
        if monitor is None:
            return
        try:
            monitor.stop(status)
        except (OSError, RuntimeError, ValueError, TypeError):
            print(
                "[Bridge monitor] shutdown failed",
                file=sys.stderr,
                flush=True,
            )
        finally:
            self.monitor = None

    def save_state(self) -> None:
        with self._state_lock:
            self.state["updated_at"] = utc_now()
            atomic_json(self.state_path, self.state)

    def _persist_control_snapshot(self, snapshot: dict[str, Any]) -> None:
        pending = snapshot.get("pending")
        with self._state_lock:
            if isinstance(pending, dict):
                model = pending.get("model")
                effort = pending.get("effort")
                if not isinstance(model, str) or not isinstance(effort, str):
                    raise ValueError("invalid pending control selection")
                self.state["pending_codex_model"] = model
                self.state["pending_codex_reasoning_effort"] = effort
            else:
                self.state.pop("pending_codex_model", None)
                self.state.pop("pending_codex_reasoning_effort", None)
            self.save_state()

    def event(self, kind: str, **details: Any) -> None:
        record = sanitize_value({"at": utc_now(), "event": kind, **details})
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[{record['at']}] {kind}", flush=True)
        if self.monitor is not None and not self.monitor_failed:
            try:
                self.monitor.state.publish(kind, record)
            except (OSError, RuntimeError, ValueError, TypeError):
                self.monitor_failed = True

    def git(self, arguments: list[str]) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            raise BridgeError(f"git {' '.join(arguments)} failed: {redact(result.stderr.strip())}")
        return result.stdout

    def _ensure_process_start_allowed(self) -> None:
        if self.state.get("status") in {"USER_STOPPING", "USER_STOPPED"}:
            raise UserStopRequested("user requested task stop")
        control = self.review_control
        if control is not None and control.public_snapshot()["stop_requested"]:
            self._begin_user_stop()
            raise UserStopRequested("user requested task stop")

    def _register_active_process(
        self,
        process: subprocess.Popen[str],
        kind: str,
    ) -> None:
        with self._process_lock:
            active = self._active_process
            if active is not None and active.poll() is None:
                self._terminate_process_group(process)
                raise BridgeError("another Bridge subprocess is still active")
            self._active_process = process
            self._active_process_kind = kind

    def _clear_active_process(self, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            if self._active_process is process:
                self._active_process = None
                self._active_process_kind = None

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        except OSError as exc:
            raise BridgeError("Bridge subprocess could not be terminated") from exc
        try:
            process.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=1.0)
        except ProcessLookupError:
            return
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError("Bridge subprocess could not be terminated") from exc

    def _begin_user_stop(self) -> None:
        if self.state.get("status") != "USER_STOPPING":
            self.state["status"] = "USER_STOPPING"
            self.state["last_control_action"] = "user_stop"
            self.save_state()
            self._monitor_phase("USER_STOPPING", self.state.get("round"))
            self.event(
                "user_stop_requested",
                message="用户请求终止任务",
                level="warning",
            )
        with self._process_lock:
            process = self._active_process
        if process is not None:
            self._terminate_process_group(process)

    def _take_control_action(self) -> Any:
        control = self.review_control
        if control is None:
            return None
        action = control.take_action()
        if action is None:
            return None
        if action.kind == "stop":
            self._begin_user_stop()
        return action

    def _poll_user_stop(self) -> bool:
        action = self._take_control_action()
        if action is None:
            return False
        if action.kind == "stop":
            return True
        raise BridgeError("unexpected control action outside Codex review")

    def _raise_if_user_stopping(self) -> None:
        if self.state.get("status") == "USER_STOPPING":
            raise UserStopRequested("user requested task stop")

    def execute(
        self,
        command: list[str],
        prompt: str,
        label: str,
        *,
        review_no: int | None = None,
        on_started: Any = None,
    ) -> tuple[str, str]:
        self._ensure_process_start_allowed()
        self.event("command_started", label=label, executable=Path(command[0]).name)
        try:
            process = subprocess.Popen(
                command,
                cwd=self.repo,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=os.environ.copy(),
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise BridgeError(f"{label} could not start") from exc
        self._register_active_process(process, "codex")
        if on_started is not None:
            callback_completed = False
            try:
                on_started()
                callback_completed = True
            finally:
                if not callback_completed:
                    self._terminate_process_group(process)
                    self._clear_active_process(process)
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            self._clear_active_process(process)
            raise BridgeError(f"{label} did not expose required pipes")

        messages: queue.Queue[tuple[str, str | None]] = queue.Queue()
        writer_errors: list[str] = []

        def read_pipe(source: str, pipe: Any) -> None:
            try:
                for line in iter(pipe.readline, ""):
                    messages.put((source, line))
            finally:
                messages.put((source, None))
                pipe.close()

        def write_prompt() -> None:
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                writer_errors.append(redact(str(exc)))

        readers = [
            threading.Thread(
                target=read_pipe, args=("stdout", process.stdout), daemon=True),
            threading.Thread(
                target=read_pipe, args=("stderr", process.stderr), daemon=True),
        ]
        writer = threading.Thread(target=write_prompt, daemon=True)
        for thread in readers:
            thread.start()
        writer.start()

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        ended: set[str] = set()
        stream_error: str | None = None
        restart_selection: CodexControlSelection | None = None
        restart_requested_at: float | None = None
        deadline = time.monotonic() + self.args.timeout
        events_path = self.outputs / f"{label}.jsonl"
        with events_path.open("w", encoding="utf-8") as events_file:
            while len(ended) < 2 or process.poll() is None:
                action = self._take_control_action()
                if action is not None:
                    if action.kind == "stop":
                        break
                    if (
                        action.kind != "restart"
                        or review_no is None
                        or action.review_no != review_no
                        or action.selection is None
                    ):
                        stream_error = "Codex review control state is invalid"
                        self._terminate_process_group(process)
                        break
                    restart_selection = action.selection
                    restart_requested_at = time.monotonic()
                if (
                    restart_selection is not None
                    and isinstance(self.state.get("codex_session_id"), str)
                    and self.state["codex_session_id"]
                    and restart_requested_at is not None
                    and time.monotonic() - restart_requested_at >= 0.1
                ):
                    self._terminate_process_group(process)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stream_error = (
                        f"{label} timed out after {self.args.timeout} seconds")
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
                try:
                    source, line = messages.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    ended.add(source)
                    continue
                stripped = line.rstrip("\r\n")
                if not stripped:
                    continue
                safe_line = redact(stripped)
                if source == "stderr":
                    stderr_lines.append(safe_line)
                    continue
                stdout_lines.append(safe_line)
                try:
                    parsed = json.loads(safe_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    events_file.write(
                        json.dumps(sanitize_value(parsed), ensure_ascii=False)
                        + "\n")
                    events_file.flush()
                    self._monitor_codex(parsed)
                    event_type = parsed.get("type")
                    if event_type == "thread.started":
                        thread_id = parsed.get("thread_id")
                        if not isinstance(thread_id, str) or not thread_id:
                            stream_error = "Codex did not provide a valid session ID"
                            self._terminate_process_group(process)
                            break
                        saved_thread = self.state.get("codex_session_id")
                        if saved_thread is not None and saved_thread != thread_id:
                            stream_error = "Codex session changed during review"
                            self._terminate_process_group(process)
                            break
                        if saved_thread is None:
                            self.state["codex_session_id"] = thread_id
                            self.save_state()
                        print("[Codex] 正在审查…", flush=True)

        if process.poll() is None:
            process.kill()
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            self._clear_active_process(process)
            raise BridgeError(f"{label} did not terminate") from exc
        for thread in readers:
            thread.join(timeout=1)
        writer.join(timeout=1)
        self._clear_active_process(process)
        self._raise_if_user_stopping()
        if restart_selection is not None:
            self.state["codex_review_restarts"] = int(
                self.state.get("codex_review_restarts", 0)) + 1
            self.state["active_review_restart_used"] = True
            self.state["last_control_action"] = "codex_review_restart"
            self.save_state()
            self.event(
                "codex_review_restarted",
                message="Codex 审核正在按新配置重启",
                review=review_no,
                restart=self.state["codex_review_restarts"],
            )
            raise ReviewRestartRequested(restart_selection)
        if writer_errors and stream_error is None:
            stream_error = f"{label} could not receive its prompt"
        if stream_error is not None:
            raise BridgeError(stream_error)
        stdout = "\n".join(stdout_lines)
        stderr = "\n".join(stderr_lines)
        if return_code:
            (self.outputs / f"{label}-error.txt").write_text(
                stdout + "\nSTDERR:\n" + stderr, encoding="utf-8")
            raise BridgeError(
                f"{label} exited {return_code}; see sanitized error output")
        self.event("command_finished", label=label)
        return stdout, stderr

    def _claude_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.setdefault(
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "50")
        return environment

    @staticmethod
    def _find_pre_tokens(value: Any) -> int | None:
        if isinstance(value, dict):
            for key in ("preTokens", "pre_tokens"):
                candidate = value.get(key)
                if isinstance(candidate, int) and not isinstance(candidate, bool):
                    return candidate
            for candidate in value.values():
                found = Bridge._find_pre_tokens(candidate)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for candidate in value:
                found = Bridge._find_pre_tokens(candidate)
                if found is not None:
                    return found
        return None

    def _display_claude_event(self, event: dict[str, Any], round_no: int) -> None:
        event_type = event.get("type")
        subtype = event.get("subtype")
        if subtype == "compact_boundary":
            self._finish_claude_terminal_text()
            pre_tokens = self._find_pre_tokens(event)
            suffix = "unknown" if pre_tokens is None else str(pre_tokens)
            print(
                f"[Claude compact] compact_boundary preTokens={suffix}",
                flush=True,
            )
            self.event(
                "claude_compact_boundary", round=round_no,
                preTokens=pre_tokens)
            return

        if event_type == "stream_event":
            inner = event.get("event")
            if isinstance(inner, dict):
                delta = inner.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        if not self._claude_terminal_open:
                            print("[Claude] ", end="", flush=True)
                            self._claude_terminal_open = True
                            self._claude_terminal_text = ""
                        print(text, end="", flush=True)
                        self._claude_terminal_text += text
                if inner.get("type") == "content_block_stop":
                    self._finish_claude_terminal_text()
            return

        if event_type == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text:
                            if self._claude_terminal_open:
                                current = self._claude_terminal_text
                                if text.startswith(current):
                                    print(text[len(current):], end="", flush=True)
                                elif text != current:
                                    self._finish_claude_terminal_text()
                                    print(f"[Claude] {text}", flush=True)
                                    self._claude_terminal_last_text = text
                                self._finish_claude_terminal_text()
                            elif text != self._claude_terminal_last_text:
                                print(f"[Claude] {text}", flush=True)
                                self._claude_terminal_last_text = text
                    elif block.get("type") == "tool_use":
                        self._finish_claude_terminal_text()
                        name = block.get("name")
                        safe_name = name if isinstance(name, str) else "unknown"
                        print(f"[Claude tool] {safe_name}", flush=True)
                        inputs = block.get("input")
                        command = inputs.get("command") if isinstance(inputs, dict) else None
                        if isinstance(command, str) and command:
                            print(f"[Claude command] {command}", flush=True)
            return

        if event_type == "user":
            self._finish_claude_terminal_text()
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    label = "failed" if block.get("is_error") is True else "completed"
                    print(f"[Claude tool {label}]", flush=True)
            return

        if event_type == "result":
            self._finish_claude_terminal_text()
            result = event.get("result")
            if isinstance(result, str) and result:
                print(f"[Claude result] {result}", flush=True)

    def _finish_claude_terminal_text(self) -> None:
        if not self._claude_terminal_open:
            return
        print("", flush=True)
        self._claude_terminal_open = False
        self._claude_terminal_last_text = self._claude_terminal_text
        self._claude_terminal_text = ""

    def execute_claude_stream(
        self,
        command: list[str],
        prompt: str,
        label: str,
        round_no: int,
    ) -> dict[str, Any]:
        """Run Claude while draining and displaying its stream-json output."""
        self._ensure_process_start_allowed()
        self._finish_claude_terminal_text()
        self._claude_terminal_last_text = None
        self.event("command_started", label=label, executable=Path(command[0]).name)
        stream_path = self.outputs / f"round-{round_no:02d}-claude-stream.jsonl"
        try:
            process = subprocess.Popen(
                command,
                cwd=self.repo,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self._claude_environment(),
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise BridgeError(f"{label} could not start") from exc
        self._register_active_process(process, "claude")

        self._monitor_claude_pid(process.pid)

        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            self._clear_active_process(process)
            raise BridgeError(f"{label} did not expose required pipes")

        messages: queue.Queue[tuple[str, str | None]] = queue.Queue()
        writer_errors: list[str] = []

        def read_pipe(source: str, pipe: Any) -> None:
            try:
                for line in iter(pipe.readline, ""):
                    messages.put((source, line))
            finally:
                messages.put((source, None))
                pipe.close()

        def write_prompt() -> None:
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                writer_errors.append(redact(str(exc)))

        readers = [
            threading.Thread(
                target=read_pipe, args=("stdout", process.stdout), daemon=True),
            threading.Thread(
                target=read_pipe, args=("stderr", process.stderr), daemon=True),
        ]
        writer = threading.Thread(target=write_prompt, daemon=True)
        for thread in readers:
            thread.start()
        writer.start()

        deadline = time.monotonic() + self.args.timeout
        ended: set[str] = set()
        final_event: dict[str, Any] | None = None
        observed_session_id: str | None = None
        stream_error: str | None = None
        stderr_text: list[str] = []

        with stream_path.open("w", encoding="utf-8") as stream_file:
            while len(ended) < 2 or process.poll() is None:
                if self._poll_user_stop():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stream_error = (
                        f"{label} timed out after {self.args.timeout} seconds")
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
                try:
                    source, line = messages.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    continue
                if line is None:
                    ended.add(source)
                    continue
                stripped = line.rstrip("\r\n")
                if not stripped:
                    continue
                if source == "stderr":
                    safe_stderr = redact(stripped)
                    stderr_text.append(safe_stderr)
                    record = {"type": "bridge_stderr", "text": safe_stderr}
                    stream_file.write(
                        json.dumps(record, ensure_ascii=False) + "\n")
                    stream_file.flush()
                    continue
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    record = {
                        "type": "bridge_invalid_stream_json",
                        "text": redact(stripped),
                    }
                    stream_file.write(
                        json.dumps(record, ensure_ascii=False) + "\n")
                    stream_file.flush()
                    stream_error = "Claude returned invalid stream-json"
                    process.terminate()
                    continue
                safe_event = sanitize_value(parsed)
                if not isinstance(safe_event, dict):
                    stream_error = "Claude stream-json event is not an object"
                    process.terminate()
                    continue
                stream_file.write(
                    json.dumps(safe_event, ensure_ascii=False) + "\n")
                stream_file.flush()
                self._display_claude_event(safe_event, round_no)
                self._monitor_claude(safe_event)
                session_id = safe_event.get("session_id")
                if isinstance(session_id, str) and session_id:
                    observed_session_id = session_id
                if safe_event.get("type") == "result":
                    final_event = safe_event

        if process.poll() is None:
            process.kill()
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            self._clear_active_process(process)
            raise BridgeError(f"{label} did not terminate") from exc
        for thread in readers:
            thread.join(timeout=1)
        writer.join(timeout=1)
        self._finish_claude_terminal_text()
        self._monitor_claude_pid(None)
        self._clear_active_process(process)
        self._raise_if_user_stopping()

        if writer_errors and stream_error is None:
            stream_error = f"{label} could not receive its prompt"
        if stream_error is not None:
            raise BridgeError(stream_error)
        if return_code:
            error_path = self.outputs / f"{label}-error.txt"
            error_path.write_text(
                "STDERR:\n" + "\n".join(stderr_text), encoding="utf-8")
            raise BridgeError(
                f"{label} exited {return_code}; see sanitized error output")
        if final_event is None:
            raise BridgeError("Claude stream did not contain a final result event")
        if not final_event.get("session_id") and observed_session_id is not None:
            final_event["session_id"] = observed_session_id
        self.event("command_finished", label=label)
        return final_event

    @staticmethod
    def _bounded_verification_output(value: str) -> str:
        safe = redact(value)
        if len(safe) <= MAX_VERIFICATION_OUTPUT_CHARS:
            return safe
        return (
            f"[TRUNCATED TO LAST {MAX_VERIFICATION_OUTPUT_CHARS} CHARACTERS]\n"
            + safe[-MAX_VERIFICATION_OUTPUT_CHARS:]
        )

    def _execute_verification_command(
        self, argv: tuple[str, ...], command_index: int,
    ) -> dict[str, Any]:
        self.event(
            "verification_command_started",
            command_index=command_index,
            executable=Path(argv[0]).name,
        )
        self._ensure_process_start_allowed()
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=self.repo,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=os.environ.copy(),
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise BridgeError("verification command could not start") from exc
        self._register_active_process(process, "verification")
        assert process.stdout is not None and process.stderr is not None
        raw_limit = MAX_VERIFICATION_OUTPUT_CHARS * 2
        buffers = {"stdout": "", "stderr": ""}
        reader_errors: list[BaseException] = []

        def drain(name: str, pipe: Any) -> None:
            try:
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    buffers[name] = (buffers[name] + chunk)[-raw_limit:]
            except (OSError, ValueError) as exc:
                reader_errors.append(exc)
            finally:
                try:
                    pipe.close()
                except OSError:
                    pass

        readers = [
            threading.Thread(
                target=drain, args=("stdout", process.stdout), daemon=True),
            threading.Thread(
                target=drain, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        deadline = time.monotonic() + self.args.verification_timeout
        try:
            while process.poll() is None:
                if self._poll_user_stop():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    raise subprocess.TimeoutExpired(list(argv), 0)
                try:
                    process.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        except (OSError, ValueError) as exc:
            try:
                process.kill()
            except OSError:
                pass
            raise BridgeError("verification command communication failed") from exc
        finally:
            for reader in readers:
                reader.join(timeout=2)
            self._clear_active_process(process)
        self._raise_if_user_stopping()
        if any(reader.is_alive() for reader in readers) or reader_errors:
            raise BridgeError("verification command communication failed")
        result = {
            "command_index": command_index,
            "executable": Path(argv[0]).name,
            "returncode": process.returncode,
            "timed_out": timed_out,
            "stdout": self._bounded_verification_output(buffers["stdout"]),
            "stderr": self._bounded_verification_output(buffers["stderr"]),
        }
        self.event(
            "verification_command_finished",
            command_index=command_index,
            returncode=process.returncode,
            timed_out=timed_out,
        )
        return result

    def run_verification(self, attempt_no: int) -> tuple[bool, str]:
        if not self.verification_commands:
            return True, "No bridge-owned verification manifest configured."
        results: list[dict[str, Any]] = []
        failed: dict[str, Any] | None = None
        for command_index, argv in enumerate(
            self.verification_commands, start=1,
        ):
            result = self._execute_verification_command(argv, command_index)
            results.append(result)
            if result["timed_out"] or result["returncode"] != 0:
                failed = result
                break
        output_path = (
            self.outputs / f"attempt-{attempt_no:02d}-verification.json")
        try:
            atomic_json(output_path, {
                "attempt": attempt_no,
                "passed": failed is None,
                "commands": sanitize_value(results),
            })
        except OSError as exc:
            raise BridgeError("verification result could not be saved") from exc
        if failed is None:
            summary = "Bridge verification passed:\n" + "\n".join(
                f"- command {item['command_index']}: exit code 0"
                for item in results
            )
            return True, summary
        correction = (
            "VERIFICATION FAILED\n"
            f"command index: {failed['command_index']}\n"
            f"executable: {failed['executable']}\n"
            f"exit code: {failed['returncode']}\n"
            f"timed out: {str(failed['timed_out']).lower()}\n"
            "sanitized stdout:\n"
            f"{failed['stdout']}\n"
            "sanitized stderr:\n"
            f"{failed['stderr']}"
        )
        return False, correction

    def run_claude(self, instruction: str, round_no: int) -> str:
        prompt = self.claude_prompt(instruction, round_no)
        (self.prompts / f"round-{round_no:02d}-claude.md").write_text(redact(prompt), encoding="utf-8")
        command = [
            self.claude_bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            self.args.claude_tools,
            "--disallowedTools",
            self.args.claude_disallowed_tools,
        ]
        if self.claude_model is not None:
            command.extend(["--model", self.claude_model])
        if round_no == 1:
            command.extend(["--session-id", self.state["claude_session_id"]])
        else:
            command.extend(["--resume", self.state["claude_session_id"]])
        payload = self.execute_claude_stream(
            command, prompt, f"round-{round_no:02d}-claude", round_no)
        atomic_json(
            self.outputs / f"round-{round_no:02d}-claude.json", payload)
        session_id = payload.get("session_id")
        if session_id:
            self.state["claude_session_id"] = session_id
        result = payload.get("result")
        if not isinstance(result, str) or not result.strip():
            raise BridgeError("Claude JSON did not contain a non-empty result")
        self.save_state()
        return result

    def claude_prompt(self, instruction: str, round_no: int) -> str:
        return f"""You are the implementation agent in round {round_no} of a bounded workflow.

Task:
{self.state['task']}

Approved plan:
{self.state['approved_plan']}

Current implementation or correction instruction:
{instruction}

Rules:
- Modify only files required by the approved task.
- Preserve unrelated user changes. Stop and report if safe separation is impossible.
- Run the plan's specified checks when permitted and report exact commands and results.
- Do not commit, push, merge, or alter Git history.
- Do not expose credentials or weaken permissions.
- Finish with a concise implementation and verification report.
"""

    def evidence(self, *, include_diff: bool) -> str:
        status = self.git(["status", "--short"])
        diff_stat = self.git(["diff", "--stat"])
        changed_paths = self.git(["diff", "--name-only", "--"])
        value = (
            f"git status --short:\n{status}"
            f"\ngit diff --stat:\n{diff_stat}"
            f"\nChanged paths:\n{changed_paths}"
        )
        if include_diff:
            diff = self.git(["diff", "--no-ext-diff", "--"])
            limit = self.args.max_diff_chars
            if len(diff) > limit:
                diff = (
                    diff[:limit]
                    + f"\n[DIFF TRUNCATED AT {limit} CHARACTERS]\n")
            value += f"\ngit diff:\n{diff}"
        return value

    def run_codex(
        self,
        claude_result: str,
        attempt_no: int,
        review_no: int,
        verification_evidence: str,
    ) -> dict[str, Any]:
        has_previous_review = isinstance(
            self.state.get("last_review"), dict)
        if has_previous_review and not self.state.get("codex_session_id"):
            raise BridgeError("resumed Codex review requires a session ID")
        evidence = self.evidence(include_diff=not has_previous_review)
        prompt = self.codex_prompt(
            claude_result,
            evidence,
            review_no,
            verification_evidence,
        )
        (self.prompts / f"round-{attempt_no:02d}-codex.md").write_text(
            redact(prompt), encoding="utf-8")
        schema = self.root / "review-schema.json"
        atomic_json(schema, {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["PASS", "FAIL", "NEEDS_INPUT"],
                },
                "evidence": {"type": "array", "items": {"type": "string"}},
                "remaining_issues": {"type": "array", "items": {"type": "string"}},
                "next_instructions": {"type": "string"},
                "question": {"type": "string"},
                "reason": {"type": "string"},
                "options": {
                    "type": "array", "items": {"type": "string"},
                    "maxItems": 5,
                },
            },
            "required": [
                "status", "evidence", "remaining_issues", "next_instructions",
                "question", "reason", "options",
            ],
            "additionalProperties": False,
        })
        last_message = (
            self.outputs / f"round-{attempt_no:02d}-codex-result.json")
        control = self.review_control
        if control is None:
            raise BridgeError("Codex review control is unavailable")
        selection = control.selection_for_next_review()
        start_no = 1
        while True:
            command = [
                self.codex_bin,
                "--model", selection.model,
                "-c", f'model_reasoning_effort="{selection.effort}"',
                "--sandbox", "read-only",
                "--ask-for-approval", "never",
                "-C", str(self.repo),
                "exec",
            ]
            if self.state.get("codex_session_id"):
                command.extend([
                    "resume", self.state["codex_session_id"], "-",
                    "--json", "--output-schema", str(schema),
                    "-o", str(last_message),
                ])
            else:
                command.extend([
                    "-", "--json", "--output-schema", str(schema),
                    "-o", str(last_message),
                ])

            def apply_effective() -> None:
                self.codex_model = selection.model
                self.codex_reasoning_effort = selection.effort
                self.state["codex_model"] = selection.model
                self.state["codex_reasoning_effort"] = selection.effort
                self.state.pop("pending_codex_model", None)
                self.state.pop("pending_codex_reasoning_effort", None)
                control.mark_effective(selection)
                self.save_state()

            label = (
                f"round-{attempt_no:02d}-codex-review-"
                f"{review_no:02d}-start-{start_no:02d}"
            )
            try:
                stdout, _ = self.execute(
                    command,
                    prompt,
                    label,
                    review_no=review_no,
                    on_started=apply_effective,
                )
                shutil.copyfile(
                    self.outputs / f"{label}.jsonl",
                    self.outputs / f"round-{attempt_no:02d}-codex-events.jsonl",
                )
                control.set_phase("CODEX_VALIDATING")
                break
            except ReviewRestartRequested as restart:
                selection = restart.selection
                start_no += 1
                if start_no > 2:
                    raise BridgeError("Codex review restart limit was exceeded")
                continue
        if not self.state.get("codex_session_id"):
            self.state["codex_session_id"] = extract_codex_session(stdout)
        try:
            raw_review = json.loads(last_message.read_text(encoding="utf-8"))
        except OSError as exc:
            raise BridgeError("Codex did not produce valid structured review output") from exc
        except json.JSONDecodeError as exc:
            atomic_json(last_message, {
                "error": "Codex did not produce valid structured review output",
            })
            raise BridgeError("Codex did not produce valid structured review output") from exc
        review = sanitize_value(raw_review)
        atomic_json(last_message, review)
        validate_review(review)
        if review["status"] == "PASS" and (review["remaining_issues"] or review["next_instructions"].strip()):
            raise BridgeError("Codex returned inconsistent PASS output")
        self.save_state()
        return review

    def codex_prompt(
        self,
        claude_result: str,
        evidence: str,
        review_no: int,
        verification_evidence: str,
    ) -> str:
        common = """Inspect the actual repository independently. Return PASS only if all success conditions are met, necessary checks actually passed, the diff is in scope, and no known blocker remains. Do not modify files. On FAIL, report every blocking in-scope issue found in this pass and give one complete targeted correction; do not stop intentionally after the first issue and do not add speculative out-of-scope work. Return NEEDS_INPUT instead of guessing when a requirement is ambiguous, a product or scope choice is required, requirements conflict, or new authority is required. NEEDS_INPUT must ask exactly one clear question, explain why it cannot be decided safely, and give a few mutually exclusive options when useful. The schema requires every field: for PASS/FAIL set question and reason to empty strings and options to []; for NEEDS_INPUT set next_instructions to an empty string. Return only the required JSON object."""
        if not isinstance(self.state.get("last_review"), dict):
            return f"""Act as the independent read-only reviewer for review {review_no}.

Original task:
{self.state['task']}

Approved plan and success conditions:
{self.state['approved_plan']}

Claude's report (do not trust it without repository evidence):
{claude_result}

Bridge-owned verification evidence:
{verification_evidence}

Repository evidence captured after implementation:
{evidence}

{common}
"""
        previous = self.state.get("last_review")
        if not isinstance(previous, dict):
            raise BridgeError("incremental Codex review lacks previous review")
        issues = previous.get("remaining_issues", [])
        instructions = previous.get("next_instructions", "")
        return f"""Continue the same independent read-only review as review {review_no}.

Previous blocking issues:
{json.dumps(issues, ensure_ascii=False)}

Previous targeted correction:
{instructions}

Latest Claude correction report:
{claude_result}

Latest bridge-owned verification evidence:
{verification_evidence}

Current incremental repository summary:
{evidence}

Re-read the actual changed files and relevant verification evidence; do not rely only on this summary.

{common}
"""

    def loop(self) -> None:
        instruction = "Implement the approved plan."
        if self.user_answer is not None and self.answer_context is not None:
            instruction = f"""Continue after a required human decision.

NEEDS_INPUT QUESTION:
{self.answer_context['question']}

USER ANSWER (authoritative for this decision):
{self.user_answer}

Apply this answer only within the approved task and plan. Preserve all other constraints.
"""
        elif isinstance(self.state.get("next_instruction"), str):
            instruction = self.state["next_instruction"]
        elif self.args.resume and self.state.get(
            "last_review", {}).get("next_instructions"):
            instruction = self.state["last_review"]["next_instructions"]
        max_attempts = int(self.state["max_implementation_attempts"])
        max_reviews = int(self.state["max_codex_reviews"])
        while int(self.state["implementation_attempts"]) < max_attempts:
            attempt_no = int(self.state["implementation_attempts"]) + 1
            self.state.update(
                status="CLAUDE_RUNNING",
                round=attempt_no,
                implementation_attempts=attempt_no,
            )
            self.state.pop("next_instruction", None)
            self.save_state()
            self.event(
                "implementation_attempt_started",
                attempt=attempt_no,
                max_attempts=max_attempts,
            )
            self._monitor_phase("CLAUDE_RUNNING", attempt_no)
            claude_result = self.run_claude(instruction, attempt_no)
            self.event(
                "claude_handoff",
                message=claude_result,
                attempt=attempt_no,
            )
            self.state["status"] = "VERIFYING"
            self.save_state()
            self._monitor_phase("VERIFYING", attempt_no)
            verification_passed, verification_evidence = (
                self.run_verification(attempt_no))
            self.state["last_verification"] = {
                "attempt": attempt_no,
                "passed": verification_passed,
                "summary": verification_evidence,
            }
            if not verification_passed:
                instruction = verification_evidence
                self.state["status"] = "VERIFICATION_FAILED"
                self.state["next_instruction"] = instruction
                self.save_state()
                self.event(
                    "verification_failed",
                    attempt=attempt_no,
                )
                continue

            reviews_used = int(self.state["codex_reviews"])
            if reviews_used >= max_reviews:
                raise BridgeError(
                    f"maximum Codex reviews reached ({max_reviews})")
            review_no = reviews_used + 1
            resumed_restart_budget = (
                self.state.get("active_review_restart_used") is True
                and self.state.get("active_codex_review_no") == review_no
            )
            self.state.update(
                status="CODEX_REVIEWING",
                active_review_restart_used=resumed_restart_budget,
                active_codex_review_no=review_no,
            )
            self.save_state()
            if self.review_control is None:
                raise BridgeError("Codex review control is unavailable")
            self.review_control.mark_review_started(review_no)
            if resumed_restart_budget:
                self.review_control.mark_review_restart_used(review_no)
            self.event(
                "codex_review_started",
                review=review_no,
                max_reviews=max_reviews,
            )
            self._monitor_phase("CODEX_REVIEWING", attempt_no)
            review = self.run_codex(
                claude_result,
                attempt_no,
                review_no,
                verification_evidence,
            )
            self.state["codex_reviews"] = review_no
            self.state["active_review_restart_used"] = False
            self.state["active_codex_review_no"] = None
            self.save_state()
            self.event(
                "codex_review_completed",
                review=review_no,
                max_reviews=max_reviews,
            )
            self.state["last_review"] = review
            self.event(
                "codex_decision",
                message=f"Codex {review['status']}",
                attempt=attempt_no,
                review=review_no,
                status=review["status"],
                evidence=review["evidence"],
                remaining_issues=review["remaining_issues"],
                next_instructions=review["next_instructions"],
                question=review["question"],
                reason=review["reason"],
                options=review["options"],
            )
            if review["status"] == "PASS":
                self.state["status"] = "PASS"
                self.save_state()
                self._monitor_phase("PASS", attempt_no)
                self.event(
                    "task_passed",
                    message="Codex 审核通过",
                    level="success",
                    attempt=attempt_no,
                    review=review_no,
                    evidence=review["evidence"],
                )
                return
            if review["status"] == "NEEDS_INPUT":
                awaiting = {
                    "question": review["question"],
                    "reason": review["reason"],
                    "options": review["options"],
                }
                self.state["status"] = "AWAITING_INPUT"
                self.state["awaiting_input"] = awaiting
                self.save_state()
                self._monitor_phase("AWAITING_INPUT", attempt_no)
                self.event(
                    "input_required", message="需要用户审核",
                    level="warning",
                    attempt=attempt_no,
                    review=review_no, **awaiting)
                return
            instruction = review["next_instructions"]
            self.state["status"] = "FAIL"
            self.state["next_instruction"] = instruction
            self.save_state()
            self.event(
                "codex_handoff", message=instruction,
                level="warning",
                attempt=attempt_no, review=review_no,
                issues=review["remaining_issues"])
            if review_no >= max_reviews:
                raise BridgeError(
                    f"maximum Codex reviews reached ({max_reviews})")
        raise BridgeError(
            f"maximum implementation attempts reached ({max_attempts})")


def extract_codex_session(jsonl: str) -> str:
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
    raise BridgeError("could not extract Codex session ID from JSONL events")


def validate_review(review: Any) -> None:
    if not isinstance(review, dict):
        raise BridgeError("Codex review is not an object")
    status = review.get("status")
    expected = {
        "status", "evidence", "remaining_issues", "next_instructions",
        "question", "reason", "options",
    }
    if set(review) != expected or status not in {"PASS", "FAIL", "NEEDS_INPUT"}:
        raise BridgeError("Codex review violates the response contract")
    if not isinstance(review["evidence"], list) or not all(
        isinstance(item, str) for item in review["evidence"]
    ) or not isinstance(review["remaining_issues"], list) or not all(
        isinstance(item, str) for item in review["remaining_issues"]
    ):
        raise BridgeError("Codex review evidence/issues must be arrays")
    if not isinstance(review["next_instructions"], str):
        raise BridgeError("Codex next_instructions must be a string")
    if status == "FAIL" and not review["next_instructions"].strip():
        raise BridgeError("Codex FAIL omitted targeted next instructions")
    if status == "NEEDS_INPUT":
        if review["next_instructions"]:
            raise BridgeError("Codex NEEDS_INPUT included correction instructions")
        if not isinstance(review["question"], str) or not review["question"].strip():
            raise BridgeError("Codex NEEDS_INPUT omitted a clear question")
        if not isinstance(review["reason"], str) or not review["reason"].strip():
            raise BridgeError("Codex NEEDS_INPUT omitted its reason")
        options = review["options"]
        if not isinstance(options, list) or len(options) > 5 or not all(
            isinstance(item, str) and item.strip() for item in options
        ):
            raise BridgeError("Codex NEEDS_INPUT options are invalid")
    elif review["question"] or review["reason"] or review["options"]:
        raise BridgeError("Codex PASS/FAIL included NEEDS_INPUT fields")


def status_command(repo: str) -> int:
    path = Path(repo).expanduser().resolve() / ".codex-bridge" / "state.json"
    if not path.exists():
        print("No bridge state found.", file=sys.stderr)
        return 1
    state = read_json(path)
    summary = {key: state.get(key) for key in (
        "task_id", "status", "round", "max_rounds", "created_at", "updated_at",
        "implementation_attempts", "codex_reviews",
        "max_implementation_attempts", "max_codex_reviews",
        "claude_session_id", "codex_session_id",
        "claude_autocompact_pct_override", "claude_model", "codex_model",
        "codex_reasoning_effort", "pending_codex_model",
        "pending_codex_reasoning_effort", "codex_review_restarts",
        "active_review_restart_used", "active_codex_review_no",
        "last_control_action", "user_stopped_at",
        "codex_binary", "awaiting_input", "last_review",
    )}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run or resume an approved task")
    run.add_argument("--repo", required=True)
    run.add_argument("--task-file", required=True)
    run.add_argument("--plan-file", required=True)
    run.add_argument("--approved", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--user-answer-file")
    run.add_argument("--allow-dirty", action="store_true")
    run.add_argument("--max-rounds", type=int)
    run.add_argument("--max-codex-reviews", type=int)
    run.add_argument("--max-implementation-attempts", type=int)
    run.add_argument("--timeout", type=int, default=1800, help="seconds per model call")
    run.add_argument(
        "--verification-timeout", type=int,
        default=DEFAULT_VERIFICATION_TIMEOUT,
        help="seconds per approved verification command",
    )
    run.add_argument("--verification-file")
    run.add_argument("--max-diff-chars", type=int, default=20000)
    run.add_argument("--claude-tools", default=DEFAULT_CLAUDE_TOOLS)
    run.add_argument(
        "--claude-disallowed-tools",
        default=DEFAULT_CLAUDE_DISALLOWED_TOOLS,
        help="Claude tools that must remain unavailable; defaults to Agent",
    )
    run.add_argument("--claude-bin")
    run.add_argument("--codex-bin")
    run.add_argument("--no-monitor", action="store_true")
    run.add_argument("--no-open-browser", action="store_true")
    run.add_argument("--claude-model")
    run.add_argument("--codex-model")
    run.add_argument(
        "--codex-reasoning-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
    )
    status = sub.add_parser("status", help="print durable task state")
    status.add_argument("--repo", required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.command == "status":
        return status_command(args.repo)
    positive_optional = (
        args.max_rounds,
        args.max_codex_reviews,
        args.max_implementation_attempts,
    )
    if (
        any(value is not None and value < 1 for value in positive_optional)
        or args.timeout < 1
        or args.verification_timeout < 1
        or args.max_diff_chars < 1000
        or (
            args.max_rounds is not None
            and args.max_codex_reviews is not None
            and args.max_rounds != args.max_codex_reviews
        )
    ):
        print("Invalid positive bound.", file=sys.stderr)
        return 2
    bridge = Bridge(args)
    try:
        bridge.initialize()
        bridge.start_monitor()
        bridge.event(
            "bridge_started", resume=args.resume,
            claude_autocompact_pct_override=bridge.compact_threshold,
            claude_model=bridge.claude_model,
            codex_model=bridge.codex_model,
            codex_reasoning_effort=bridge.codex_reasoning_effort,
            max_rounds=bridge.state.get("max_rounds"),
            implementation_attempts=bridge.state.get(
                "implementation_attempts"),
            codex_reviews=bridge.state.get("codex_reviews"),
            max_implementation_attempts=bridge.state.get(
                "max_implementation_attempts"),
            max_codex_reviews=bridge.state.get("max_codex_reviews"),
            codex_binary=bridge.codex_bin)
        bridge.loop()
        return 0
    except UserStopRequested:
        if bridge.state:
            bridge.state["status"] = "USER_STOPPED"
            bridge.state["user_stopped_at"] = utc_now()
            bridge.state["last_control_action"] = "user_stop"
            bridge.save_state()
            if bridge.review_control is not None:
                bridge.review_control.mark_stopped()
            bridge._monitor_phase(
                "USER_STOPPED", bridge.state.get("round"))
            bridge.event(
                "user_stopped",
                message="任务已由用户终止",
                level="warning",
            )
        print("USER_STOPPED: task stopped by user", file=sys.stderr)
        return 0
    except AwaitingInputError as exc:
        message = redact(str(exc))
        if bridge.state:
            bridge.event("input_resume_blocked", reason=message)
        print(f"AWAITING_INPUT: {message}", file=sys.stderr)
        return 2
    except BridgeError as exc:
        message = redact(str(exc))
        if bridge.state:
            bridge.state["status"] = "STOPPED"
            bridge.state["stop_reason"] = message
            bridge.save_state()
            bridge.event("bridge_stopped", reason=message)
        print(f"STOPPED: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if bridge.state:
            bridge.state["status"] = "STOPPED"
            bridge.state["stop_reason"] = "interrupted by user"
            bridge.save_state()
        print("STOPPED: interrupted by user", file=sys.stderr)
        return 130
    finally:
        final_status = str(bridge.state.get("status", "STOPPED"))
        bridge.finish_monitor(final_status)
        bridge.release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
