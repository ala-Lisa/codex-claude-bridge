from __future__ import annotations

import json
import os
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from scripts import bridge as bridge_module


SKILL_ROOT = Path(__file__).resolve().parents[1]
BRIDGE = SKILL_ROOT / "scripts" / "bridge.py"
SECRET_TOKEN = "sk-abcdefghijklmnop"
ENV_SECRET = "plain-environment-credential"


MOCK_CLAUDE = r'''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path(os.environ["MOCK_ROOT"])
counter = root / "claude-count"
n = int(counter.read_text() or "0") + 1 if counter.exists() else 1
counter.write_text(str(n))
(root / f"claude-active-pid-{n}").write_text(str(os.getpid()))
(root / f"claude-args-{n}.json").write_text(json.dumps(sys.argv[1:]))
(root / f"claude-prompt-{n}.txt").write_text(sys.stdin.read())
(root / f"compact-{n}.txt").write_text(
    os.environ.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "<missing>")
)

if os.environ.get("MOCK_STOP_STAGE") == "claude":
    child = subprocess.Popen([
        sys.executable, "-c", "import time; time.sleep(60)",
    ])
    (root / "claude-parent-pid").write_text(str(os.getpid()))
    (root / "claude-child-pid").write_text(str(child.pid))
    while True:
        time.sleep(1)

if os.environ.get("MOCK_BLOCK") == "1" and n == 1:
    (root / "claude-blocked").write_text("1")
    release = root / "release"
    deadline = time.time() + 10
    while not release.exists() and time.time() < deadline:
        time.sleep(0.02)

def emit(value):
    print(json.dumps(value), flush=True)

emit({
    "type": "system", "subtype": "init", "session_id": "claude-session",
    "uuid": "init-" + str(n), "model": "deepseek-v4-pro[1m]",
    "claude_code_version": "2.1.208",
})
emit({
    "type": "stream_event",
    "uuid": "stream-1-" + str(n),
    "event": {
        "type": "content_block_delta",
        "delta": {
            "type": "text_delta",
            "text": "live text OPENAI_API_KEY=" + "sk-abcdefghijklmnop"
            + " raw=" + os.environ["MOCK_ENV_SECRET"],
        },
    },
})
emit({
    "type": "stream_event",
    "uuid": "stream-2-" + str(n),
    "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": " continues"},
    },
})
(root / f"first-stream-{n}").write_text("1")
time.sleep(float(os.environ.get("MOCK_STREAM_DELAY", "0")))
sys.stderr.write("stderr-secret=" + "sk-abcdefghijklmnop" + "\n" + ("x" * 200000))
sys.stderr.flush()
emit({
    "type": "assistant", "uuid": "assistant-" + str(n),
    "message": {
        "usage": {
            "input_tokens": 100,
            "cache_creation_input_tokens": 2000,
            "cache_read_input_tokens": 113900,
            "output_tokens": 10,
        },
        "content": [
            {
                "type": "text",
                "text": "live text OPENAI_API_KEY=sk-abcdefghijklmnop"
                + " raw=" + os.environ["MOCK_ENV_SECRET"] + " continues",
            },
            {
                "type": "tool_use", "id": "tool-1", "name": "Bash",
                "input": {"command": "python -m pytest -q TOKEN=sk-abcdefghijklmnop"},
            },
        ]},
})
emit({
    "type": "user",
    "message": {"content": [{
        "type": "tool_result", "tool_use_id": "tool-1",
        "content": "tests passed SECRET=sk-abcdefghijklmnop", "is_error": False,
    }]},
})
emit({
    "type": "user",
    "message": {"content": [{
        "type": "tool_result", "tool_use_id": "tool-2",
        "content": "controlled failure", "is_error": True,
    }]},
})
emit({
    "type": "system", "subtype": "compact_boundary",
    "compact_metadata": {"trigger": "auto", "preTokens": 50000},
})
emit({
    "type": "result", "subtype": "success", "is_error": False,
    "uuid": "result-" + str(n),
    "result": "implementation report " + str(n) + " Bearer sk-abcdefghijklmnop",
    "total_cost_usd": 1.25,
    "duration_api_ms": 2000,
    "usage": {"output_tokens": 100},
    "modelUsage": {
        "deepseek-v4-pro[1m]": {"contextWindow": 1000000},
    },
})
(root / f"claude-finished-{n}").write_text("1")
'''


MOCK_CODEX = r'''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

root = Path(os.environ["MOCK_ROOT"])
counter = root / "codex-count"
n = int(counter.read_text() or "0") + 1 if counter.exists() else 1
counter.write_text(str(n))
(root / f"codex-active-pid-{n}").write_text(str(os.getpid()))
args = sys.argv[1:]
(root / f"codex-args-{n}.json").write_text(json.dumps(args))
(root / f"codex-prompt-{n}.txt").write_text(sys.stdin.read())

out = Path(args[args.index("-o") + 1])
schema_path = Path(args[args.index("--output-schema") + 1])
schema = json.loads(schema_path.read_text())
if "oneOf" in schema or set(schema.get("required", [])) != set(schema["properties"]):
    print("unsupported output schema", file=sys.stderr)
    raise SystemExit(9)
mode = os.environ.get("MOCK_CODEX_MODE", "pass")
if mode == "fail" or (mode == "fail-pass" and n == 1):
    result = {
        "status": "FAIL", "evidence": ["checked"],
        "remaining_issues": ["fix one thing"],
        "next_instructions": "Apply the targeted correction.",
        "question": "", "reason": "", "options": [],
    }
elif mode == "needs-input-pass" and n == 1:
    result = {
        "status": "NEEDS_INPUT", "evidence": ["ambiguous requirement"],
        "remaining_issues": ["human choice required"],
        "next_instructions": "",
        "question": "Choose output mode?",
        "reason": "The approved plan permits only one mutually exclusive mode.",
        "options": ["compact", "expanded"],
    }
else:
    result = {
        "status": "PASS", "evidence": ["all checks passed"],
        "remaining_issues": [], "next_instructions": "",
        "question": "", "reason": "", "options": [],
    }
result["evidence"].append("OPENAI_API_KEY=" + "sk-abcdefghijklmnop")
out.write_text(json.dumps(result))
print(json.dumps({"type": "thread.started", "thread_id": "codex-session"}), flush=True)
restart_block = os.environ.get("MOCK_RESTART_BLOCK")
if restart_block == "all" or (restart_block == "first" and n == 1):
    blocked = root / f"codex-restart-blocked-{n}"
    blocked.write_text("1")
    release = root / f"release-codex-{n}"
    while not release.exists():
        __import__("time").sleep(0.02)
if os.environ.get("MOCK_STOP_STAGE") == "codex":
    child = subprocess.Popen([
        sys.executable, "-c", "import time; time.sleep(60)",
    ])
    (root / "codex-parent-pid").write_text(str(os.getpid()))
    (root / "codex-child-pid").write_text(str(child.pid))
    while True:
        time.sleep(1)
print(json.dumps({
    "type": "item.started",
    "item": {"id": "review-1", "type": "command_execution"},
}), flush=True)
import time
time.sleep(float(os.environ.get("MOCK_CODEX_DELAY", "0")))
print(json.dumps({
    "type": "item.completed",
    "item": {"id": "review-1", "type": "command_execution"},
}), flush=True)
print(json.dumps({"type": "turn.completed"}), flush=True)
'''


MOCK_VERIFY = r'''#!/usr/bin/env python3
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path(os.environ["MOCK_ROOT"])
counter = root / "verify-count"
n = int(counter.read_text() or "0") + 1 if counter.exists() else 1
counter.write_text(str(n))
(root / f"verification-active-pid-{n}").write_text(str(os.getpid()))
mode = os.environ.get("MOCK_VERIFY_MODE", "pass")
if os.environ.get("MOCK_STOP_STAGE") == "verification":
    child = subprocess.Popen([
        sys.executable, "-c", "import time; time.sleep(60)",
    ])
    (root / "verification-parent-pid").write_text(str(os.getpid()))
    (root / "verification-child-pid").write_text(str(child.pid))
    while True:
        time.sleep(1)
print("verification stdout OPENAI_API_KEY=sk-abcdefghijklmnop")
if mode == "sleep":
    __import__("time").sleep(3)
if mode == "large":
    print("o" * 100000)
    print("e" * 100000, file=__import__("sys").stderr)
if mode == "fail" or (mode == "fail-pass" and n == 1):
    print("verification stderr SECRET=sk-abcdefghijklmnop", file=__import__("sys").stderr)
    raise SystemExit(7)
'''


class Harness:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="bridge-test-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.bin = self.root / "bin"
        self.home = self.root / "home"
        self.repo.mkdir()
        self.bin.mkdir()
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "models_cache.json").write_text(json.dumps({
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "visibility": "list",
                    "supported_reasoning_levels": [
                        {"effort": effort}
                        for effort in ("low", "medium", "high", "xhigh", "max")
                    ],
                },
                {
                    "slug": "codex-auto-review",
                    "display_name": "Codex Auto Review",
                    "visibility": "hide",
                    "supported_reasoning_levels": [{"effort": "high"}],
                },
                {
                    "slug": "gpt-test-model",
                    "display_name": "GPT Test Model",
                    "visibility": "list",
                    "supported_reasoning_levels": [{"effort": "high"}],
                },
            ],
        }))
        self.task = self.root / "task.md"
        self.plan = self.root / "plan.md"
        self.task.write_text("Implement the approved mock task.\n")
        self.plan.write_text("Only edit app.txt and run mock checks.\n")
        (self.repo / "README.md").write_text("baseline\n")
        (self.repo / "app.txt").write_text("baseline\n")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Bridge Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "bridge@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=self.repo, check=True)
        self.claude = self._script("mock-claude", MOCK_CLAUDE)
        self.codex = self._script("mock-codex", MOCK_CODEX)
        self.verify = self._script("mock-verify", MOCK_VERIFY)

    def _script(self, name: str, content: str) -> Path:
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)
        return path

    def env(self, **extra: str) -> dict[str, str]:
        value = os.environ.copy()
        value.pop("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", None)
        value.update({
            "HOME": str(self.home),
            "MOCK_ROOT": str(self.root),
            "MOCK_ENV_SECRET": ENV_SECRET,
            "CODEX_BRIDGE_CLAUDE_BIN": str(self.claude),
            "CODEX_BRIDGE_CODEX_BIN": str(self.codex),
        })
        value.update(extra)
        return value

    def command(self, *extra: str, monitor: bool = False) -> list[str]:
        explicit_review_limit = any(
            value in {"--max-rounds", "--max-codex-reviews"}
            for value in extra
        )
        return [
            sys.executable, str(BRIDGE), "run",
            "--repo", str(self.repo),
            "--task-file", str(self.task),
            "--plan-file", str(self.plan),
            "--approved",
            *([] if explicit_review_limit else ["--max-rounds", "3"]),
            "--timeout", "10",
            *([] if monitor else ["--no-monitor"]),
            *extra,
        ]

    def run(
        self, *extra: str, env: dict[str, str] | None = None,
        monitor: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(*extra, monitor=monitor), text=True, capture_output=True,
            timeout=20, env=env or self.env(), check=False,
        )

    def state(self) -> dict:
        return json.loads((self.repo / ".codex-bridge" / "state.json").read_text())

    def verification_file(
        self, commands: list[list[str]] | None = None,
    ) -> Path:
        path = self.root / "verification.json"
        path.write_text(json.dumps({
            "version": 1,
            "commands": commands or [[str(self.verify)]],
        }))
        return path

    def close(self) -> None:
        self.temp.cleanup()


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def tearDown(self) -> None:
        self.h.close()

    def test_default_codex_selection_is_sol_max(self) -> None:
        result = self.h.run(env=self.h.env(MOCK_CODEX_MODE="pass"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = json.loads((self.h.root / "codex-args-1.json").read_text())
        self.assertEqual(args[args.index("--model") + 1], "gpt-5.6-sol")
        config_index = args.index("-c")
        self.assertEqual(
            args[config_index + 1], 'model_reasoning_effort="max"')
        state = self.h.state()
        self.assertEqual(state["codex_model"], "gpt-5.6-sol")
        self.assertEqual(state["codex_reasoning_effort"], "max")

    def test_streams_live_redacts_and_preserves_fail_pass_flow(self) -> None:
        env = self.h.env(MOCK_CODEX_MODE="fail-pass", MOCK_STREAM_DELAY="0.8")
        proc = subprocess.Popen(
            self.h.command(), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )
        assert proc.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        visible: list[str] = []
        deadline = time.time() + 8
        saw_live = False
        while time.time() < deadline and not saw_live:
            for key, _ in selector.select(timeout=0.1):
                chunk = os.read(key.fileobj.fileno(), 4096).decode(
                    "utf-8", errors="replace")
                visible.append(chunk)
                if "live text" in "".join(visible):
                    saw_live = True
                    self.assertFalse((self.h.root / "claude-finished-1").exists())
                    break
        self.assertTrue(saw_live, "Claude text was not displayed before process completion")
        stdout_rest, stderr = proc.communicate(timeout=20)
        terminal = "".join(visible) + stdout_rest + stderr
        self.assertEqual(proc.returncode, 0, terminal)
        self.assertIn("[Claude tool] Bash", terminal)
        self.assertIn("python -m pytest -q", terminal)
        self.assertIn("[Claude tool completed]", terminal)
        self.assertIn("[Claude tool failed]", terminal)
        self.assertIn("compact_boundary", terminal)
        self.assertIn("preTokens=50000", terminal)
        self.assertEqual(terminal.count("[Codex] 正在审查…"), 2)
        self.assertNotIn("[Codex] item.started", terminal)
        self.assertNotIn("[Codex] item.completed", terminal)
        self.assertIn("[Claude] live text OPENAI_API_KEY=[REDACTED]", terminal)
        self.assertIn("continues\n[Claude tool] Bash", terminal)
        self.assertNotIn("\n[Claude] continues", terminal)
        self.assertEqual(terminal.count("[Claude] live text"), 2)
        self.assertNotIn(SECRET_TOKEN, terminal)
        self.assertNotIn(ENV_SECRET, terminal)
        self.assertEqual((self.h.root / "claude-count").read_text(), "2")
        self.assertEqual((self.h.root / "codex-count").read_text(), "2")
        self.assertEqual(self.h.state()["status"], "PASS")
        self.assertEqual(self.h.state()["claude_session_id"], "claude-session")
        self.assertEqual(self.h.state()["codex_session_id"], "codex-session")

        for round_no in (1, 2):
            args = json.loads((self.h.root / f"claude-args-{round_no}.json").read_text())
            self.assertIn("stream-json", args)
            self.assertIn("--verbose", args)
            self.assertIn("--include-partial-messages", args)
            self.assertNotIn("bypassPermissions", args)
            allowed_tools = args[args.index("--allowedTools") + 1]
            self.assertIn("Bash(rtk git status *)", allowed_tools)
            self.assertIn("Bash(rtk git diff *)", allowed_tools)
            self.assertIn("Bash(rtk git ls-files *)", allowed_tools)
            self.assertNotIn("ECC_GATEGUARD=off", allowed_tools)
            disallowed_tools = args[args.index("--disallowedTools") + 1]
            self.assertEqual(disallowed_tools, "Agent")
            stream = self.h.repo / ".codex-bridge" / "outputs" / f"round-{round_no:02d}-claude-stream.jsonl"
            self.assertTrue(stream.is_file())
            text = stream.read_text()
            self.assertNotIn(SECRET_TOKEN, text)
            for line in text.splitlines():
                json.loads(line)
            codex_stream = (
                self.h.repo / ".codex-bridge" / "outputs"
                / f"round-{round_no:02d}-codex-events.jsonl"
            )
            codex_events = [
                json.loads(line) for line in codex_stream.read_text().splitlines()
            ]
            self.assertTrue(any(
                item.get("type") == "item.started" for item in codex_events
            ))
            self.assertTrue(any(
                item.get("type") == "item.completed" for item in codex_events
            ))
        bridge_root = self.h.repo / ".codex-bridge"
        for path in bridge_root.rglob("*"):
            if path.is_file() and path.name != "lock":
                content = path.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn(SECRET_TOKEN, content, str(path))
                self.assertNotIn(ENV_SECRET, content, str(path))
        events = [
            json.loads(line)
            for line in (bridge_root / "events.jsonl").read_text().splitlines()
        ]
        compact_events = [
            item for item in events
            if item["event"] == "claude_compact_boundary"
        ]
        self.assertEqual([item["preTokens"] for item in compact_events], [50000, 50000])
        claude_handoffs = [
            item for item in events
            if item["event"] == "claude_handoff"
        ]
        self.assertEqual(
            [item["attempt"] for item in claude_handoffs], [1, 2])
        self.assertEqual(
            [item["message"] for item in claude_handoffs],
            [
                "implementation report 1 Bearer [REDACTED_TOKEN]",
                "implementation report 2 Bearer [REDACTED_TOKEN]",
            ],
        )
        codex_decisions = [
            item for item in events
            if item["event"] == "codex_decision"
        ]
        self.assertEqual(
            [item["status"] for item in codex_decisions], ["FAIL", "PASS"])
        self.assertEqual(codex_decisions[0]["evidence"], [
            "checked", "OPENAI_API_KEY=[REDACTED]",
        ])
        self.assertEqual(
            codex_decisions[0]["remaining_issues"], ["fix one thing"])
        self.assertEqual(
            codex_decisions[0]["next_instructions"],
            "Apply the targeted correction.",
        )
        self.assertEqual(codex_decisions[0]["question"], "")
        self.assertEqual(codex_decisions[0]["reason"], "")
        self.assertEqual(codex_decisions[0]["options"], [])
        self.assertEqual(codex_decisions[1]["remaining_issues"], [])
        self.assertEqual(codex_decisions[1]["next_instructions"], "")
        for round_no, handoff in enumerate(claude_handoffs, start=1):
            codex_prompt = (
                bridge_root / "prompts" / f"round-{round_no:02d}-codex.md"
            ).read_text()
            self.assertEqual(codex_prompt.count(handoff["message"]), 1)
        second_claude_prompt = (
            self.h.root / "claude-prompt-2.txt"
        ).read_text()
        self.assertEqual(
            second_claude_prompt.count(
                codex_decisions[0]["next_instructions"]),
            1,
        )
        self.assertIn("--session-id", json.loads((self.h.root / "claude-args-1.json").read_text()))
        self.assertIn("--resume", json.loads((self.h.root / "claude-args-2.json").read_text()))
        codex_1 = json.loads((self.h.root / "codex-args-1.json").read_text())
        self.assertLess(codex_1.index("--ask-for-approval"), codex_1.index("exec"))
        codex_2 = json.loads((self.h.root / "codex-args-2.json").read_text())
        self.assertIn("resume", codex_2)
        self.assertIn("codex-session", codex_2)

    def test_live_monitor_is_available_before_claude_finishes(self) -> None:
        import urllib.request

        env = self.h.env(
            MOCK_CODEX_MODE="pass", MOCK_BLOCK="1", MOCK_CODEX_DELAY="0.2")
        proc = subprocess.Popen(
            self.h.command("--no-open-browser", monitor=True),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        assert proc.stdout is not None
        url = None
        deadline = time.time() + 8
        terminal_lines: list[str] = []
        while time.time() < deadline and url is None:
            line = proc.stdout.readline()
            terminal_lines.append(line)
            if line.startswith("[Bridge monitor] http://127.0.0.1:"):
                url = line.split()[-1].rstrip("/")
        self.assertIsNotNone(url, "monitor URL was not printed")
        assert url is not None
        deadline = time.time() + 8
        while not (self.h.root / "claude-blocked").exists() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.h.root / "claude-blocked").exists())

        with urllib.request.urlopen(url + "/events", timeout=2) as response:
            def read_snapshot() -> dict:
                while True:
                    raw = response.readline()
                    self.assertTrue(raw, "monitor SSE ended early")
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data: "):
                        return json.loads(line[6:])

            initial = read_snapshot()
            self.assertEqual(initial["phase"], "CLAUDE_RUNNING")
            self.assertFalse((self.h.root / "claude-finished-1").exists())
            (self.h.root / "release").write_text("1")
            observed = initial
            deadline = time.time() + 8
            while time.time() < deadline:
                observed = read_snapshot()
                encoded = json.dumps(observed)
                if (
                    observed["claude"]["context_window_source"] == "claude_reported"
                    and observed["claude"]["total_cost_usd"] == 1.25
                    and observed["claude"]["output_tokens_per_second"] == 50.0
                    and observed["tools"]["completed"] >= 1
                    and observed["compaction"]["count"] >= 1
                    and observed["bridge"]["codex_reviews"] == 1
                    and "codex" in encoded.lower()
                ):
                    break
            self.assertEqual(observed["claude"]["context_used_tokens"], 116000)
            self.assertEqual(observed["claude"]["context_window_tokens"], 1000000)
            self.assertEqual(
                observed["claude"]["context_window_source"], "claude_reported")
            self.assertEqual(observed["claude"]["total_cost_usd"], 1.25)
            self.assertEqual(observed["claude"]["round_cost_usd"], 1.25)
            self.assertEqual(observed["claude"]["output_tokens_total"], 100)
            self.assertEqual(
                observed["claude"]["output_tokens_per_second"], 50.0)
            self.assertEqual(
                observed["claude"]["output_tokens_peak_per_second"], 50.0)
            self.assertEqual(observed["compaction"]["pre_tokens"], 50000)
            self.assertGreaterEqual(observed["tools"]["completed"], 1)
            self.assertEqual(
                observed["bridge"]["autocompact_threshold"], "50")
            self.assertEqual(observed["bridge"]["max_rounds"], 3)
            self.assertEqual(
                observed["bridge"]["implementation_attempts"], 1)
            self.assertEqual(observed["bridge"]["codex_reviews"], 1)
            self.assertEqual(
                observed["bridge"]["max_implementation_attempts"], 12)
            self.assertEqual(observed["bridge"]["max_codex_reviews"], 3)
            text_events = [
                item for item in observed["events"]
                if item["kind"] == "claude_text"
            ]
            self.assertEqual(len(text_events), 1)
            self.assertIn("continues", text_events[0]["message"])
            self.assertFalse(text_events[0]["details"]["streaming"])
            self.assertNotIn(SECRET_TOKEN, json.dumps(observed))
            self.assertNotIn(ENV_SECRET, json.dumps(observed))

        stdout_rest, stderr = proc.communicate(timeout=20)
        terminal = "".join(terminal_lines) + stdout_rest + stderr
        self.assertEqual(proc.returncode, 0, terminal)
        self.assertEqual(self.h.state()["status"], "PASS")

    def test_live_monitor_aggregates_codex_items_before_review_finishes(self) -> None:
        env = self.h.env(MOCK_CODEX_MODE="pass", MOCK_CODEX_DELAY="1.2")
        proc = subprocess.Popen(
            self.h.command("--no-open-browser", monitor=True),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        assert proc.stdout is not None
        url = None
        deadline = time.time() + 8
        while time.time() < deadline and url is None:
            line = proc.stdout.readline()
            if line.startswith("[Bridge monitor] http://127.0.0.1:"):
                url = line.split()[-1].rstrip("/")
        self.assertIsNotNone(url, "monitor URL was not printed")
        assert url is not None
        with urllib.request.urlopen(url + "/events", timeout=2) as response:
            def read_snapshot() -> dict:
                while True:
                    raw = response.readline()
                    self.assertTrue(raw, "monitor SSE ended early")
                    line = raw.decode("utf-8").rstrip("\r\n")
                    if line.startswith("data: "):
                        return json.loads(line[6:])

            observed = read_snapshot()
            deadline = time.time() + 8
            while time.time() < deadline:
                if observed["codex"].get("activity_started", 0) >= 1:
                    break
                observed = read_snapshot()
            self.assertIn("codex_started", json.dumps(observed))
            self.assertNotIn("codex_item_started", json.dumps(observed))
            self.assertNotIn("codex_item_completed", json.dumps(observed))
            self.assertGreaterEqual(
                observed["codex"]["activity_started"], 1)
            self.assertIsNone(proc.poll(), "Codex stream was not live")
        stdout, stderr = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, stdout + stderr)

    def test_codex_timeout_reaps_stream_threads(self) -> None:
        result = self.h.run(
            "--timeout", "1",
            env=self.h.env(MOCK_CODEX_MODE="pass", MOCK_CODEX_DELAY="3"),
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("timed out", result.stderr)
        self.assertNotIn("ResourceWarning", result.stderr)

    def test_compact_default_and_user_override_are_scoped_and_recorded(self) -> None:
        first = self.h.run(env=self.h.env(MOCK_CODEX_MODE="pass"))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual((self.h.root / "compact-1.txt").read_text(), "50")
        state = self.h.state()
        self.assertEqual(state["claude_autocompact_pct_override"], "50")
        events = [json.loads(line) for line in (self.h.repo / ".codex-bridge" / "events.jsonl").read_text().splitlines()]
        started = next(item for item in events if item["event"] == "bridge_started")
        self.assertEqual(started["claude_autocompact_pct_override"], "50")
        self.assertNotIn("environment", state)

        second_h = Harness()
        try:
            result = second_h.run(env=second_h.env(
                MOCK_CODEX_MODE="pass", CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="63"
            ))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((second_h.root / "compact-1.txt").read_text(), "63")
            self.assertEqual(second_h.state()["claude_autocompact_pct_override"], "63")
            second_events = [
                json.loads(line)
                for line in (
                    second_h.repo / ".codex-bridge" / "events.jsonl"
                ).read_text().splitlines()
            ]
            second_started = next(
                item for item in second_events if item["event"] == "bridge_started"
            )
            self.assertEqual(
                second_started["claude_autocompact_pct_override"], "63")
        finally:
            second_h.close()

    def test_needs_input_pauses_and_safe_answer_reuses_sessions(self) -> None:
        env = self.h.env(MOCK_CODEX_MODE="needs-input-pass")
        first = self.h.run(env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        state = self.h.state()
        self.assertEqual(state["status"], "AWAITING_INPUT")
        self.assertEqual(state["awaiting_input"]["question"], "Choose output mode?")
        self.assertEqual(state["awaiting_input"]["options"], ["compact", "expanded"])
        self.assertEqual((self.h.root / "claude-count").read_text(), "1")
        self.assertEqual((self.h.root / "codex-count").read_text(), "1")
        self.assertFalse((self.h.repo / ".codex-bridge" / "lock").exists())
        events = [
            json.loads(line)
            for line in (
                self.h.repo / ".codex-bridge" / "events.jsonl"
            ).read_text().splitlines()
        ]
        decision = next(
            item for item in events
            if item["event"] == "codex_decision"
        )
        self.assertEqual(decision["status"], "NEEDS_INPUT")
        self.assertEqual(decision["question"], "Choose output mode?")
        self.assertEqual(
            decision["reason"],
            "The approved plan permits only one mutually exclusive mode.",
        )
        self.assertEqual(decision["options"], ["compact", "expanded"])

        status = subprocess.run(
            [sys.executable, str(BRIDGE), "status", "--repo", str(self.h.repo)],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(status.returncode, 0)
        self.assertIn("Choose output mode?", status.stdout)
        self.assertIn("mutually exclusive", status.stdout)
        self.assertIn("compact", status.stdout)

        missing = self.h.run("--resume", env=env)
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(self.h.state()["status"], "AWAITING_INPUT")
        self.assertEqual((self.h.root / "claude-count").read_text(), "1")
        self.assertFalse((self.h.repo / ".codex-bridge" / "lock").exists())

        secret_answer = self.h.root / "secret-answer.md"
        secret_answer.write_text("OPENAI_API_KEY=" + SECRET_TOKEN)
        rejected = self.h.run("--resume", "--user-answer-file", str(secret_answer), env=env)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.h.state()["status"], "AWAITING_INPUT")
        self.assertNotIn(SECRET_TOKEN, rejected.stdout + rejected.stderr)
        self.assertEqual((self.h.root / "claude-count").read_text(), "1")

        answer = self.h.root / "answer.md"
        answer.write_text("Use compact mode.")
        resumed = self.h.run("--resume", "--user-answer-file", str(answer), env=env)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        final = self.h.state()
        self.assertEqual(final["status"], "PASS")
        self.assertEqual(final["claude_session_id"], state["claude_session_id"])
        self.assertEqual(final["codex_session_id"], state["codex_session_id"])
        self.assertNotIn("awaiting_input", final)
        self.assertEqual((self.h.root / "claude-count").read_text(), "2")
        self.assertEqual((self.h.root / "codex-count").read_text(), "2")
        prompt = (self.h.root / "claude-prompt-2.txt").read_text()
        self.assertIn("USER ANSWER", prompt)
        self.assertIn("Use compact mode.", prompt)
        claude_args = json.loads((self.h.root / "claude-args-2.json").read_text())
        self.assertIn("--resume", claude_args)
        self.assertIn(state["claude_session_id"], claude_args)
        codex_args = json.loads((self.h.root / "codex-args-2.json").read_text())
        self.assertIn("resume", codex_args)
        self.assertIn(state["codex_session_id"], codex_args)

    def test_second_process_cannot_replace_lock_or_state(self) -> None:
        env = self.h.env(MOCK_CODEX_MODE="pass", MOCK_BLOCK="1")
        first = subprocess.Popen(
            self.h.command(), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )
        lock = self.h.repo / ".codex-bridge" / "lock"
        state = self.h.repo / ".codex-bridge" / "state.json"
        deadline = time.time() + 8
        while time.time() < deadline:
            if lock.exists() and state.exists() and (self.h.root / "claude-blocked").exists():
                break
            time.sleep(0.02)
        self.assertTrue(lock.exists())
        lock_before = lock.read_bytes()
        state_before = state.read_bytes()

        second = self.h.run(env=env)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(lock.read_bytes(), lock_before)
        self.assertEqual(state.read_bytes(), state_before)

        (self.h.root / "release").write_text("1")
        stdout, stderr = first.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, stdout + stderr)
        self.assertFalse(lock.exists())

    def test_explicit_models_are_forwarded_and_recorded(self) -> None:
        result = self.h.run(
            "--claude-model", "claude-test-model",
            "--codex-model", "gpt-test-model",
            "--codex-reasoning-effort", "high",
            env=self.h.env(MOCK_CODEX_MODE="pass"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        claude_args = json.loads((self.h.root / "claude-args-1.json").read_text())
        codex_args = json.loads((self.h.root / "codex-args-1.json").read_text())
        self.assertEqual(
            claude_args[claude_args.index("--model") + 1], "claude-test-model")
        self.assertEqual(codex_args[codex_args.index("--model") + 1], "gpt-test-model")
        self.assertIn('model_reasoning_effort="high"', codex_args)
        state = self.h.state()
        self.assertEqual(state["claude_model"], "claude-test-model")
        self.assertEqual(state["codex_model"], "gpt-test-model")
        self.assertEqual(state["codex_reasoning_effort"], "high")


class BridgeControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.processes: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        for path in self.h.root.glob("*-active-pid-*"):
            try:
                pid = int(path.read_text())
                os.kill(pid, signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.h.close()

    @staticmethod
    def _pid_running(pid: int) -> bool:
        stat = Path(f"/proc/{pid}/stat")
        try:
            fields = stat.read_text().split()
        except OSError:
            return False
        return len(fields) > 2 and fields[2] != "Z"

    def _start_and_stop(
        self,
        stage: str,
    ) -> tuple[subprocess.Popen[str], str, str]:
        extra: tuple[str, ...] = ()
        if stage == "verification":
            extra = ("--verification-file", str(self.h.verification_file()))
        process = subprocess.Popen(
            self.h.command("--no-open-browser", *extra, monitor=True),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.h.env(MOCK_STOP_STAGE=stage, MOCK_CODEX_MODE="pass"),
        )
        self.processes.append(process)
        assert process.stdout is not None
        url: str | None = None
        terminal: list[str] = []
        deadline = time.time() + 8
        while time.time() < deadline and url is None:
            line = process.stdout.readline()
            terminal.append(line)
            if line.startswith("[Bridge monitor] http://127.0.0.1:"):
                url = line.split()[-1].rstrip("/")
        self.assertIsNotNone(url, "monitor URL was not printed")
        assert url is not None
        child_path = self.h.root / f"{stage}-child-pid"
        deadline = time.time() + 8
        while time.time() < deadline and not child_path.exists():
            if process.poll() is not None:
                break
            time.sleep(0.02)
        self.assertTrue(child_path.exists(), "blocking child was not started")

        with urllib.request.urlopen(
            url + "/control/bootstrap", timeout=2
        ) as response:
            bootstrap = json.loads(response.read())
        request = urllib.request.Request(
            url + "/control/stop",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Control-Token": bootstrap["token"],
                "Origin": url,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 202)
            self.assertEqual(json.loads(response.read())["status"], "stop_requested")
        return process, "".join(terminal), url

    def _start_controlled_bridge(
        self,
        *extra: str,
        env: dict[str, str],
    ) -> tuple[subprocess.Popen[str], str, str]:
        process = subprocess.Popen(
            self.h.command("--no-open-browser", *extra, monitor=True),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.processes.append(process)
        assert process.stdout is not None
        terminal: list[str] = []
        url: str | None = None
        deadline = time.time() + 8
        while time.time() < deadline and url is None:
            line = process.stdout.readline()
            terminal.append(line)
            if line.startswith("[Bridge monitor] http://127.0.0.1:"):
                url = line.split()[-1].rstrip("/")
        self.assertIsNotNone(url, "monitor URL was not printed")
        assert url is not None
        return process, "".join(terminal), url

    @staticmethod
    def _control_token(url: str) -> str:
        with urllib.request.urlopen(
            url + "/control/bootstrap", timeout=2
        ) as response:
            return json.loads(response.read())["token"]

    @staticmethod
    def _post_control(
        url: str,
        token: str,
        path: str,
        payload: dict,
    ) -> tuple[int, dict]:
        request = urllib.request.Request(
            url + path,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Bridge-Control-Token": token,
                "Origin": url,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def _wait_file(self, path: Path, message: str) -> None:
        deadline = time.time() + 8
        while time.time() < deadline and not path.exists():
            time.sleep(0.02)
        self.assertTrue(path.exists(), message)

    def _assert_user_stop(self, stage: str) -> None:
        process, terminal, _ = self._start_and_stop(stage)
        stdout, stderr = process.communicate(timeout=15)
        terminal += stdout + stderr
        self.assertEqual(process.returncode, 0, terminal)
        state = self.h.state()
        self.assertEqual(state["status"], "USER_STOPPED")
        self.assertIn("user_stopped_at", state)
        self.assertFalse((self.h.repo / ".codex-bridge" / "lock").exists())
        for kind in (stage,):
            parent = int((self.h.root / f"{kind}-parent-pid").read_text())
            child = int((self.h.root / f"{kind}-child-pid").read_text())
            deadline = time.time() + 5
            while time.time() < deadline and (
                self._pid_running(parent) or self._pid_running(child)
            ):
                time.sleep(0.02)
            self.assertFalse(self._pid_running(parent), f"parent {parent} survived")
            self.assertFalse(self._pid_running(child), f"child {child} survived")
        events = (
            self.h.repo / ".codex-bridge" / "events.jsonl"
        ).read_text()
        self.assertIn("user_stop_requested", events)
        self.assertIn("user_stopped", events)

    def test_user_stop_during_claude(self) -> None:
        self._assert_user_stop("claude")
        self.assertFalse((self.h.root / "codex-count").exists())
        self.assertFalse((self.h.root / "verify-count").exists())

    def test_user_stop_during_codex(self) -> None:
        self._assert_user_stop("codex")
        self.assertEqual((self.h.root / "claude-count").read_text(), "1")
        self.assertEqual((self.h.root / "codex-count").read_text(), "1")

    def test_user_stop_during_verification(self) -> None:
        self._assert_user_stop("verification")
        self.assertEqual((self.h.root / "claude-count").read_text(), "1")
        self.assertEqual((self.h.root / "verify-count").read_text(), "1")
        self.assertFalse((self.h.root / "codex-count").exists())

    def test_review_restart_reuses_thread_and_prompt(self) -> None:
        process, terminal, url = self._start_controlled_bridge(
            "--codex-reasoning-effort", "high",
            env=self.h.env(
                MOCK_CODEX_MODE="pass",
                MOCK_RESTART_BLOCK="first",
            ),
        )
        self._wait_file(
            self.h.root / "codex-restart-blocked-1",
            "first Codex review did not reach thread.started",
        )
        token = self._control_token(url)
        status, body = self._post_control(
            url,
            token,
            "/control/review-config",
            {"model": "gpt-5.6-sol", "effort": "max"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["mode"], "restart")

        stdout, stderr = process.communicate(timeout=20)
        terminal += stdout + stderr
        self.assertEqual(process.returncode, 0, terminal)
        self.assertEqual((self.h.root / "codex-count").read_text(), "2")
        state = self.h.state()
        self.assertEqual(state["status"], "PASS")
        self.assertEqual(state["codex_session_id"], "codex-session")
        self.assertEqual(state["codex_reviews"], 1)
        self.assertEqual(state["codex_review_restarts"], 1)
        self.assertEqual(state["codex_model"], "gpt-5.6-sol")
        self.assertEqual(state["codex_reasoning_effort"], "max")
        first_args = json.loads((self.h.root / "codex-args-1.json").read_text())
        second_args = json.loads((self.h.root / "codex-args-2.json").read_text())
        self.assertIn('model_reasoning_effort="high"', first_args)
        self.assertIn('model_reasoning_effort="max"', second_args)
        self.assertIn("resume", second_args)
        self.assertIn("codex-session", second_args)
        self.assertEqual(
            (self.h.root / "codex-prompt-1.txt").read_bytes(),
            (self.h.root / "codex-prompt-2.txt").read_bytes(),
        )
        output = self.h.repo / ".codex-bridge" / "outputs"
        self.assertTrue(
            (output / "round-01-codex-review-01-start-01.jsonl").is_file())
        self.assertTrue(
            (output / "round-01-codex-review-01-start-02.jsonl").is_file())
        events = (self.h.repo / ".codex-bridge" / "events.jsonl").read_text()
        self.assertIn("codex_review_restarted", events)

    def test_second_review_restart_is_rejected(self) -> None:
        process, terminal, url = self._start_controlled_bridge(
            "--codex-reasoning-effort", "high",
            env=self.h.env(
                MOCK_CODEX_MODE="pass",
                MOCK_RESTART_BLOCK="all",
            ),
        )
        token = self._control_token(url)
        self._wait_file(
            self.h.root / "codex-restart-blocked-1", "first review did not block")
        first_status, _ = self._post_control(
            url, token, "/control/review-config",
            {"model": "gpt-5.6-sol", "effort": "max"},
        )
        self.assertEqual(first_status, 202)
        self._wait_file(
            self.h.root / "codex-restart-blocked-2", "replacement did not start")
        second_status, body = self._post_control(
            url, token, "/control/review-config",
            {"model": "gpt-5.6-sol", "effort": "xhigh"},
        )
        self.assertEqual(second_status, 409)
        self.assertEqual(body, {"error": "本轮审核已重启过一次"})
        (self.h.root / "release-codex-2").write_text("1")
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, terminal + stdout + stderr)
        self.assertEqual((self.h.root / "codex-count").read_text(), "2")
        self.assertEqual(self.h.state()["codex_reviews"], 1)

    def test_stop_wins_restart_race(self) -> None:
        process, terminal, url = self._start_controlled_bridge(
            "--codex-reasoning-effort", "high",
            env=self.h.env(
                MOCK_CODEX_MODE="pass",
                MOCK_RESTART_BLOCK="first",
            ),
        )
        token = self._control_token(url)
        self._wait_file(
            self.h.root / "codex-restart-blocked-1", "review did not block")
        self._post_control(
            url, token, "/control/review-config",
            {"model": "gpt-5.6-sol", "effort": "max"},
        )
        stop_status, _ = self._post_control(
            url, token, "/control/stop", {})
        self.assertEqual(stop_status, 202)
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, terminal + stdout + stderr)
        self.assertEqual(self.h.state()["status"], "USER_STOPPED")
        self.assertEqual((self.h.root / "codex-count").read_text(), "1")

    def test_user_stopped_resume_reuses_sessions_and_selected_configuration(
        self,
    ) -> None:
        process, terminal, url = self._start_controlled_bridge(
            "--codex-reasoning-effort", "high",
            env=self.h.env(
                MOCK_CODEX_MODE="pass",
                MOCK_RESTART_BLOCK="all",
            ),
        )
        token = self._control_token(url)
        self._wait_file(
            self.h.root / "codex-restart-blocked-1", "first review did not block")
        status, _ = self._post_control(
            url, token, "/control/review-config",
            {"model": "gpt-5.6-sol", "effort": "max"},
        )
        self.assertEqual(status, 202)
        self._wait_file(
            self.h.root / "codex-restart-blocked-2", "replacement did not block")
        stop_status, _ = self._post_control(
            url, token, "/control/stop", {})
        self.assertEqual(stop_status, 202)
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, terminal + stdout + stderr)
        stopped = self.h.state()
        self.assertEqual(stopped["status"], "USER_STOPPED")
        self.assertEqual(stopped["codex_reviews"], 0)
        self.assertEqual(stopped["codex_model"], "gpt-5.6-sol")
        self.assertEqual(stopped["codex_reasoning_effort"], "max")
        claude_session = stopped["claude_session_id"]
        codex_session = stopped["codex_session_id"]
        (self.h.home / ".codex" / "models_cache.json").write_text(json.dumps({
            "models": [{
                "slug": "gpt-5.6-terra",
                "display_name": "GPT-5.6-Terra",
                "visibility": "list",
                "supported_reasoning_levels": [
                    {"effort": "high"}, {"effort": "max"},
                ],
            }],
        }))

        resumed, resumed_terminal, resumed_url = self._start_controlled_bridge(
            "--resume",
            env=self.h.env(
                MOCK_CODEX_MODE="pass",
                MOCK_RESTART_BLOCK="all",
            ),
        )
        self._wait_file(
            self.h.root / "codex-restart-blocked-3",
            "resumed Codex review did not block",
        )
        resumed_token = self._control_token(resumed_url)
        restart_status, restart_body = self._post_control(
            resumed_url,
            resumed_token,
            "/control/review-config",
            {"model": "gpt-5.6-terra", "effort": "high"},
        )
        self.assertEqual(restart_status, 409)
        self.assertEqual(restart_body, {"error": "本轮审核已重启过一次"})
        (self.h.root / "release-codex-3").write_text("1")
        resumed_stdout, resumed_stderr = resumed.communicate(timeout=20)
        self.assertEqual(
            resumed.returncode,
            0,
            resumed_terminal + resumed_stdout + resumed_stderr,
        )
        final = self.h.state()
        self.assertEqual(final["status"], "PASS")
        self.assertEqual(final["codex_reviews"], 1)
        self.assertEqual(final["codex_model"], "gpt-5.6-sol")
        self.assertEqual(final["codex_reasoning_effort"], "max")
        claude_args = json.loads(
            (self.h.root / "claude-args-2.json").read_text())
        codex_args = json.loads(
            (self.h.root / "codex-args-3.json").read_text())
        self.assertIn("--resume", claude_args)
        self.assertIn(claude_session, claude_args)
        self.assertIn("resume", codex_args)
        self.assertIn(codex_session, codex_args)
        self.assertIn('model_reasoning_effort="max"', codex_args)

    def test_idle_browser_selection_is_persisted_and_used_after_resume(
        self,
    ) -> None:
        process, terminal, url = self._start_controlled_bridge(
            env=self.h.env(MOCK_STOP_STAGE="claude", MOCK_CODEX_MODE="pass"),
        )
        self._wait_file(
            self.h.root / "claude-child-pid", "Claude implementation did not block")
        token = self._control_token(url)
        status, body = self._post_control(
            url,
            token,
            "/control/review-config",
            {"model": "gpt-5.6-sol", "effort": "high"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(body["mode"], "next_review")
        pending = self.h.state()
        self.assertEqual(pending["pending_codex_model"], "gpt-5.6-sol")
        self.assertEqual(pending["pending_codex_reasoning_effort"], "high")
        stop_status, _ = self._post_control(
            url, token, "/control/stop", {})
        self.assertEqual(stop_status, 202)
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, terminal + stdout + stderr)

        resumed = self.h.run(
            "--resume",
            env=self.h.env(MOCK_CODEX_MODE="pass"),
        )

        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        codex_args = json.loads(
            (self.h.root / "codex-args-1.json").read_text())
        self.assertIn('model_reasoning_effort="high"', codex_args)
        final = self.h.state()
        self.assertEqual(final["codex_reasoning_effort"], "high")
        self.assertNotIn("pending_codex_model", final)
        self.assertNotIn("pending_codex_reasoning_effort", final)


class VerificationManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def tearDown(self) -> None:
        self.h.close()

    def test_valid_manifest_returns_digest_and_immutable_commands(self) -> None:
        path = self.h.verification_file()
        digest, commands = bridge_module.load_verification_manifest(str(path))
        self.assertEqual(len(digest), 64)
        self.assertEqual(commands, ((str(self.h.verify),),))
        self.assertIsInstance(commands, tuple)
        self.assertIsInstance(commands[0], tuple)

    def test_invalid_manifest_is_rejected_before_models_start(self) -> None:
        path = self.h.root / "bad-verification.json"
        path.write_text(json.dumps({"version": True, "commands": [["pytest", "-q"]]}))
        result = self.h.run("--verification-file", str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.h.root / "claude-count").exists())
        self.assertFalse((self.h.root / "codex-count").exists())

    def test_credential_like_manifest_value_is_rejected(self) -> None:
        path = self.h.verification_file([["pytest", "TOKEN=" + SECRET_TOKEN]])
        result = self.h.run("--verification-file", str(path))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(SECRET_TOKEN, result.stdout + result.stderr)
        self.assertFalse((self.h.root / "claude-count").exists())

    def test_unsafe_manifest_shapes_are_rejected(self) -> None:
        bad_values = [
            [],
            [["pytest", "&&", "echo"]],
            [["pytest", "line\nbreak"]],
            [["pytest", "\x00"]],
            [["pytest", 1]],
            "pytest -q",
        ]
        for index, commands in enumerate(bad_values):
            with self.subTest(commands=commands):
                path = self.h.root / f"bad-{index}.json"
                path.write_text(json.dumps({
                    "version": 1, "commands": commands,
                }))
                with self.assertRaises(bridge_module.BridgeError):
                    bridge_module.load_verification_manifest(str(path))

    def test_conflicting_review_limit_aliases_fail_before_models(self) -> None:
        result = self.h.run(
            "--max-rounds", "2", "--max-codex-reviews", "3")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.h.root / "claude-count").exists())
        self.assertFalse((self.h.root / "codex-count").exists())

    def test_resume_rejects_changed_manifest_before_models(self) -> None:
        verification = self.h.verification_file()
        env = self.h.env(MOCK_CODEX_MODE="needs-input-pass")
        first = self.h.run(
            "--verification-file", str(verification), env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.h.state()["status"], "AWAITING_INPUT")
        verification.write_text(json.dumps({
            "version": 1, "commands": [[str(self.h.verify), "changed"]],
        }))
        answer = self.h.root / "answer-verification.md"
        answer.write_text("Use the approved behavior.")
        resumed = self.h.run(
            "--resume", "--user-answer-file", str(answer),
            "--verification-file", str(verification), env=env,
        )
        self.assertNotEqual(resumed.returncode, 0)
        self.assertEqual((self.h.root / "claude-count").read_text(), "1")
        self.assertEqual((self.h.root / "codex-count").read_text(), "1")
        self.assertEqual(self.h.state()["status"], "AWAITING_INPUT")

    def test_omitted_limits_use_new_defaults(self) -> None:
        command = self.h.command()
        index = command.index("--max-rounds")
        del command[index:index + 2]
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=20,
            env=self.h.env(MOCK_CODEX_MODE="pass"), check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = self.h.state()
        self.assertEqual(state["max_implementation_attempts"], 12)
        self.assertEqual(state["max_codex_reviews"], 8)

    def test_legacy_active_state_migrates_without_lowering_bound(self) -> None:
        env = self.h.env(MOCK_CODEX_MODE="needs-input-pass")
        first = self.h.run("--max-rounds", "4", env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        state = self.h.state()
        for key in (
            "implementation_attempts", "codex_reviews",
            "max_implementation_attempts", "max_codex_reviews",
            "verification_manifest_sha256", "verification_commands",
        ):
            state.pop(key, None)
        state["version"] = 2
        state["round"] = 1
        state["max_rounds"] = 4
        (self.h.repo / ".codex-bridge" / "state.json").write_text(
            json.dumps(state))
        answer = self.h.root / "legacy-answer.md"
        answer.write_text("Continue with the approved choice.")
        resumed = self.h.run(
            "--resume", "--user-answer-file", str(answer),
            "--max-rounds", "4", env=env,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        migrated = self.h.state()
        self.assertEqual(migrated["max_codex_reviews"], 4)
        self.assertEqual(migrated["max_implementation_attempts"], 12)
        self.assertEqual(migrated["implementation_attempts"], 2)
        self.assertEqual(migrated["codex_reviews"], 2)

    def test_legacy_stopped_state_without_completed_review_starts_fresh_codex_thread(self) -> None:
        env = self.h.env(MOCK_CODEX_MODE="needs-input-pass")
        first = self.h.run(env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        state = self.h.state()
        for key in (
            "implementation_attempts", "codex_reviews",
            "max_implementation_attempts", "max_codex_reviews",
            "verification_manifest_sha256", "verification_commands",
            "last_review", "awaiting_input", "codex_session_id",
        ):
            state.pop(key, None)
        state.update({
            "version": 2,
            "status": "STOPPED",
            "round": 1,
            "max_rounds": 3,
        })
        (self.h.repo / ".codex-bridge" / "state.json").write_text(
            json.dumps(state))

        resumed = self.h.run("--resume", env=self.h.env(MOCK_CODEX_MODE="pass"))

        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        migrated = self.h.state()
        self.assertEqual(migrated["status"], "PASS")
        self.assertEqual(migrated["codex_reviews"], 2)
        codex_args = json.loads(
            (self.h.root / "codex-args-2.json").read_text())
        self.assertNotIn("resume", codex_args)
        prompt = (self.h.root / "codex-prompt-2.txt").read_text()
        self.assertIn("Original task:", prompt)
        self.assertIn("Approved plan and success conditions:", prompt)


class VerificationLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def tearDown(self) -> None:
        self.h.close()

    def test_local_failure_repairs_without_codex_then_green_candidate_passes(self) -> None:
        verification = self.h.verification_file()
        result = self.h.run(
            "--verification-file", str(verification),
            "--max-implementation-attempts", "3",
            env=self.h.env(MOCK_VERIFY_MODE="fail-pass", MOCK_CODEX_MODE="pass"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.h.root / "claude-count").read_text(), "2")
        self.assertEqual((self.h.root / "verify-count").read_text(), "2")
        self.assertEqual((self.h.root / "codex-count").read_text(), "1")
        prompt = (self.h.root / "claude-prompt-2.txt").read_text()
        self.assertIn("VERIFICATION FAILED", prompt)
        self.assertIn("exit code: 7", prompt)
        self.assertNotIn(SECRET_TOKEN, prompt)
        self.assertEqual(self.h.state()["status"], "PASS")
        self.assertEqual(self.h.state()["implementation_attempts"], 2)
        self.assertEqual(self.h.state()["codex_reviews"], 1)
        saved = (
            self.h.repo / ".codex-bridge" / "outputs"
            / "attempt-02-verification.json"
        ).read_text()
        self.assertNotIn(SECRET_TOKEN, saved)

    def test_repeated_local_failure_stops_without_codex(self) -> None:
        verification = self.h.verification_file()
        result = self.h.run(
            "--verification-file", str(verification),
            "--max-implementation-attempts", "2",
            env=self.h.env(MOCK_VERIFY_MODE="fail"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.h.root / "claude-count").read_text(), "2")
        self.assertEqual((self.h.root / "verify-count").read_text(), "2")
        self.assertFalse((self.h.root / "codex-count").exists())

    def test_timeout_is_repairable_and_stops_at_attempt_limit(self) -> None:
        verification = self.h.verification_file()
        result = self.h.run(
            "--verification-file", str(verification),
            "--verification-timeout", "1",
            "--max-implementation-attempts", "1",
            env=self.h.env(MOCK_VERIFY_MODE="sleep"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.h.root / "claude-count").read_text(), "1")
        self.assertFalse((self.h.root / "codex-count").exists())
        saved = json.loads((
            self.h.repo / ".codex-bridge" / "outputs"
            / "attempt-01-verification.json"
        ).read_text())
        self.assertTrue(saved["commands"][0]["timed_out"])

    def test_missing_verification_executable_stops_without_traceback(self) -> None:
        verification = self.h.verification_file([
            [str(self.h.root / "missing-verifier")],
        ])
        result = self.h.run(
            "--verification-file", str(verification),
            env=self.h.env(),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("verification command could not start", result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertFalse((self.h.root / "codex-count").exists())

    def test_large_stdout_and_stderr_are_drained_and_bounded(self) -> None:
        verification = self.h.verification_file()
        result = self.h.run(
            "--verification-file", str(verification),
            env=self.h.env(MOCK_VERIFY_MODE="large", MOCK_CODEX_MODE="pass"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        saved = json.loads((
            self.h.repo / ".codex-bridge" / "outputs"
            / "attempt-01-verification.json"
        ).read_text())
        command = saved["commands"][0]
        self.assertLessEqual(len(command["stdout"]), 20100)
        self.assertLessEqual(len(command["stderr"]), 20100)
        self.assertNotIn(SECRET_TOKEN, json.dumps(saved))

    def test_codex_fail_repairs_then_reuses_both_sessions(self) -> None:
        verification = self.h.verification_file()
        result = self.h.run(
            "--verification-file", str(verification),
            "--max-implementation-attempts", "3",
            env=self.h.env(MOCK_VERIFY_MODE="pass", MOCK_CODEX_MODE="fail-pass"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.h.root / "claude-count").read_text(), "2")
        self.assertEqual((self.h.root / "codex-count").read_text(), "2")
        self.assertIn(
            "--resume",
            json.loads((self.h.root / "claude-args-2.json").read_text()),
        )
        self.assertIn(
            "codex-session",
            json.loads((self.h.root / "codex-args-2.json").read_text()),
        )
        events = [
            json.loads(line)
            for line in (
                self.h.repo / ".codex-bridge" / "events.jsonl"
            ).read_text().splitlines()
        ]
        handoffs = [
            item for item in events if item["event"] == "codex_handoff"
        ]
        self.assertEqual(len(handoffs), 1)
        self.assertEqual(
            handoffs[0]["message"], "Apply the targeted correction.")
        claude_prompt = (self.h.root / "claude-prompt-2.txt").read_text()
        self.assertEqual(
            claude_prompt.count(handoffs[0]["message"]), 1)
        self.assertFalse(any(
            item["event"] == "review_failed" for item in events
        ))

    def test_repeated_codex_failure_stops_without_extra_claude(self) -> None:
        verification = self.h.verification_file()
        result = self.h.run(
            "--verification-file", str(verification),
            "--max-codex-reviews", "2",
            "--max-implementation-attempts", "5",
            env=self.h.env(MOCK_CODEX_MODE="fail"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.h.root / "claude-count").read_text(), "2")
        self.assertEqual((self.h.root / "codex-count").read_text(), "2")
        self.assertEqual(self.h.state()["codex_reviews"], 2)


class CodexPromptTierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def tearDown(self) -> None:
        self.h.close()

    def test_second_review_uses_incremental_prompt(self) -> None:
        verification = self.h.verification_file()
        result = self.h.run(
            "--verification-file", str(verification),
            "--max-implementation-attempts", "3",
            env=self.h.env(MOCK_CODEX_MODE="fail-pass"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first = (self.h.root / "codex-prompt-1.txt").read_text()
        second = (self.h.root / "codex-prompt-2.txt").read_text()
        self.assertIn("Original task:", first)
        self.assertIn("Approved plan and success conditions:", first)
        self.assertIn("git diff:", first)
        self.assertNotIn("Original task:", second)
        self.assertNotIn("Approved plan and success conditions:", second)
        self.assertNotIn("git diff:\n", second)
        self.assertIn("fix one thing", second)
        self.assertIn("Apply the targeted correction.", second)
        self.assertIn("Changed paths", second)

    def test_custom_disallowed_tools_are_forwarded(self) -> None:
        result = self.h.run(
            "--claude-disallowed-tools", "Agent,WebFetch",
            env=self.h.env(MOCK_CODEX_MODE="pass"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        claude_args = json.loads(
            (self.h.root / "claude-args-1.json").read_text())
        self.assertEqual(
            claude_args[claude_args.index("--disallowedTools") + 1],
            "Agent,WebFetch",
        )

    def test_resume_reuses_models_and_rejects_conflict_before_model_call(self) -> None:
        env = self.h.env(MOCK_CODEX_MODE="needs-input-pass")
        first = self.h.run(
            "--claude-model", "claude-test-model",
            "--codex-model", "gpt-test-model",
            "--codex-reasoning-effort", "high",
            env=env,
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        answer = self.h.root / "answer-model.md"
        answer.write_text("Use compact mode.")

        conflict = self.h.run(
            "--resume", "--user-answer-file", str(answer),
            "--codex-reasoning-effort", "low", env=env,
        )
        self.assertNotEqual(conflict.returncode, 0)
        self.assertEqual((self.h.root / "claude-count").read_text(), "1")
        self.assertEqual(self.h.state()["status"], "AWAITING_INPUT")

        resumed = self.h.run(
            "--resume", "--user-answer-file", str(answer), env=env)
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        claude_args = json.loads((self.h.root / "claude-args-2.json").read_text())
        codex_args = json.loads((self.h.root / "codex-args-2.json").read_text())
        self.assertEqual(
            claude_args[claude_args.index("--model") + 1], "claude-test-model")
        self.assertEqual(codex_args[codex_args.index("--model") + 1], "gpt-test-model")
        self.assertIn('model_reasoning_effort="high"', codex_args)


class CodexBinaryResolutionTests(unittest.TestCase):
    def _windows_wrapper(self, root: Path) -> Path:
        wrapper = root / "bin" / "codex"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(
            "#!/bin/sh\nexec /mnt/c/Users/A8/AppData/Local/OpenAI/Codex/codex.exe \"$@\"\n"
        )
        wrapper.chmod(0o755)
        return wrapper

    def _native_extension_binary(self, home: Path) -> Path:
        native = (
            home / ".vscode-server" / "extensions" / "openai.chatgpt-test-linux-x64"
            / "bin" / "linux-x86_64" / "codex"
        )
        native.parent.mkdir(parents=True)
        native.write_bytes(b"\x7fELFmock")
        native.chmod(0o755)
        return native

    def test_wsl_prefers_native_extension_over_windows_path_wrapper(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-resolve-") as temp:
            root = Path(temp)
            wrapper = self._windows_wrapper(root)
            native = self._native_extension_binary(root / "home")
            environment = {
                "WSL_DISTRO_NAME": "Ubuntu",
                "PATH": str(wrapper.parent),
            }
            resolved = bridge_module.resolve_codex_binary(
                None, environ=environment, home=root / "home")
            self.assertEqual(Path(resolved), native)

    def test_wsl_rejects_windows_wrapper_when_no_native_binary_exists(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-resolve-") as temp:
            root = Path(temp)
            wrapper = self._windows_wrapper(root)
            environment = {
                "WSL_DISTRO_NAME": "Ubuntu",
                "PATH": str(wrapper.parent),
            }
            with self.assertRaises(bridge_module.BridgeError) as exc:
                bridge_module.resolve_codex_binary(
                    None, environ=environment, home=root / "home")
            self.assertEqual(str(exc.exception), "native Linux Codex executable is required in WSL")

    def test_explicit_windows_wrapper_is_rejected_in_wsl(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-resolve-") as temp:
            root = Path(temp)
            wrapper = self._windows_wrapper(root)
            with mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}, clear=False):
                with self.assertRaises(bridge_module.BridgeError):
                    bridge_module.resolve_codex_binary(str(wrapper))


if __name__ == "__main__":
    unittest.main(verbosity=2)
