# Codex Claude Bridge Live Monitor Implementation Plan

> **Execution constraint:** Implement inline in this session. Do not delegate, commit, push, or modify Git history. Use failing-test-first cycles and preserve every existing bridge safety control.

**Goal:** Add an automatically opened, loopback-only browser page combining a live sanitized Claude/Codex event stream with an accurate operational HUD.

**Architecture:** `scripts/monitor.py` owns thread-safe monitor state, event reduction, read-only sampling, HTTP/SSE serving, and browser launch. `assets/monitor.html` renders the state. `scripts/bridge.py` forwards sanitized events and process lifecycle changes without granting the monitor control over models or repository state.

**Tech stack:** Python standard library, HTML/CSS/vanilla JavaScript, and existing unittest mock executables.

## Global constraints

- Bind only to `127.0.0.1` on an operating-system-assigned port.
- Load no browser resource from outside the loopback server.
- Sanitize before publishing model or command data.
- Keep every HTTP endpoint read-only.
- Preserve streaming, compaction, NEEDS_INPUT, session reuse, locks, dirty-worktree protection, timeouts, round limits, and Codex read-only review.
- Do not call real Claude, DeepSeek, or Codex APIs in monitor tests.
- Add no dependency and modify no global or project configuration.
- Do not commit, push, amend, rebase, or alter history.

---

### Task 1: Thread-safe monitor state and event reduction

**Files:**
- Create: `scripts/monitor.py`
- Create: `tests/test_monitor.py`

**Interfaces:**
- `MonitorState(repo, sanitizer, clock=time.monotonic)`
- `publish(kind, payload)`
- `consume_claude(event)` and `consume_codex(event)`
- `set_phase(phase, round_no=None)` and `set_claude_pid(pid)`
- `snapshot()` and `wait_for_revision(after, timeout)`

- [ ] Write failing tests for `deepseek-v4-pro[1m]`, configured `1_000_000`, assistant usage totaling `116_000`, reported context replacement, mismatch warnings, unknown models, and malformed numeric values.
- [ ] Run `python3 -W error -m unittest tests.test_monitor.MonitorStateTests -v`; expect RED because the module is missing.
- [ ] Implement state with `threading.Condition`, monotonic revisions, deep-copy snapshots, and `CONFIGURED_CONTEXT_WINDOWS = {"deepseek-v4-pro[1m]": 1_000_000}`.
- [ ] Compute context only from non-bool non-negative integer `input_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens`. A positive reported denominator wins and changes source to `claude_reported`.
- [ ] Add failing tests for tool start/completion/failure, duplicate UUIDs, test-command classification, compaction and `preTokens`, sanitized text/commands, and safe Codex progress without reasoning content.
- [ ] Implement reducers with at most 1,000 log entries and 2,000 deduplication IDs. Never retain environments, full tool results, thinking, or Codex reasoning.
- [ ] Re-run `MonitorStateTests`; expect all pass with zero warnings.

---

### Task 2: Read-only Git, configuration, cache, and memory sampling

**Files:**
- Modify: `scripts/monitor.py`
- Modify: `tests/test_monitor.py`

**Interfaces:**
- `MonitorSampler(state, repo, interval_seconds=1.0)`
- `start()` and `stop()`

- [ ] Write failing temporary-repository tests for branch, changed-file count, tracked additions/deletions, untracked-file count, and fixed unavailable state on Git failure.
- [ ] Add tests for `/proc/<pid>/status` `VmRSS`, missing/malformed process data, CLAUDE.md/rule/hook counts without retaining contents, and prompt-cache TTL clamped to `0..300`.
- [ ] Run `python3 -W error -m unittest tests.test_monitor.MonitorSamplerTests -v`; expect RED.
- [ ] Implement argument-list, `shell=False`, short-timeout calls for `git branch --show-current`, `git status --porcelain=v1`, and `git diff --numstat --`. Never store stderr.
- [ ] Contain all sampling errors, read only the selected PID, and ensure `stop()` joins its worker.
- [ ] Re-run `MonitorSamplerTests`; expect all pass, zero warnings, and no leaked thread.

---

### Task 3: Loopback HTTP/SSE service and HUD page

**Files:**
- Modify: `scripts/monitor.py`
- Create: `assets/monitor.html`
- Modify: `tests/test_monitor.py`

**Interfaces:**
- `LiveMonitor(repo, sanitizer, open_browser=True)`
- `start() -> str`, `stop(final_status=None)`, and public `state`

- [ ] Write failing tests proving a random `127.0.0.1` URL, UTF-8 `GET /`, 404/405 behavior, security headers, initial SSE snapshot, live later event, client disconnect safety, and clean shutdown.
- [ ] Run `python3 -W error -m unittest tests.test_monitor.LiveMonitorServerTests -v`; expect RED.
- [ ] Implement `ThreadingHTTPServer(("127.0.0.1", 0), Handler)` with daemon request threads, no request logging, read-only routes, and quiet handling of broken SSE clients.
- [ ] Apply `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and CSP beginning with `default-src 'none'`.
- [ ] Launch `webbrowser.open(url, new=1)` in a daemon helper. False return or exception publishes `browser_open_failed`, prints the URL, and never fails the workflow.
- [ ] Build a self-contained dark `assets/monitor.html`: sticky phase header; context/tool/Git/cache/memory/compaction/config cards; event list; pause/resume scroll; copy-visible-log; connection state; `textContent` for all data; no forms, remote resources, cookies, or storage.
- [ ] Re-run `LiveMonitorServerTests`; expect all pass, zero warnings, and no open listener.

---

### Task 4: Bridge integration and automatic monitoring

**Files:**
- Modify: `scripts/bridge.py`
- Modify: `tests/test_bridge.py`
- Modify: `tests/test_monitor.py`

**Interfaces:**
- Add `Bridge.start_monitor()` and `Bridge.finish_monitor(status)`.
- Add `--no-monitor` for explicit CI/headless use; monitor remains enabled by default.
- Add `--no-open-browser` to serve without launching a browser.

- [ ] Update legacy harness cases to pass `--no-monitor`, then add failing monitor-enabled tests.
- [ ] Prove monitor startup follows lock acquisition and precedes Claude; page/SSE remains live while delayed mock Claude runs.
- [ ] Prove partial text, tool, command, test, compact, result, Claude PID, `CODEX_REVIEWING`, Codex progress, next round, and PASS appear in order.
- [ ] Prove FAIL-then-PASS uses one URL, NEEDS_INPUT prevents another Claude call, monitor/browser failures are non-fatal, `--no-monitor` opens nothing, locking remains safe, and secrets appear nowhere.
- [ ] Run `python3 -W error -m unittest tests.test_bridge.BridgeTests -v`; expect RED for missing options and lifecycle.
- [ ] Start monitoring only after `initialize()` succeeds. Forward the same sanitized dictionary written by `Bridge.event()` and each sanitized Claude event after its JSONL write. Set/clear Claude PID around `Popen`.
- [ ] Replace Codex one-shot stdout capture with line-oriented `Popen` processing that drains stderr concurrently, preserves sanitized JSONL for session extraction, and forwards only safe metadata. Keep Codex read-only arguments unchanged.
- [ ] Guard monitor calls so an exception disables only monitoring and emits fixed `monitor_failed` data.
- [ ] Publish final PASS/AWAITING_INPUT/STOPPED, send a final snapshot, close threads/listener within one second, and leave the browser DOM showing its last state.
- [ ] Run the bridge class tests and full `unittest discover`; expect all pass, zero warnings, no real model calls, and no leaked resource.

---

### Task 5: Documentation and final verification

**Files:**
- Modify: `SKILL.md`
- Modify: `references/protocol.md`
- Modify: `agents/openai.yaml` only if its user prompt must mention monitoring
- Test: all Skill tests

- [ ] Document automatic loopback monitoring, `--no-monitor`, `--no-open-browser`, all HUD sources, configured versus reported context, unknown fallback, and terminal/JSONL fallback.
- [ ] Run `python3 -m py_compile scripts/bridge.py scripts/monitor.py tests/test_bridge.py tests/test_monitor.py`.
- [ ] Run `python3 -W error -m unittest discover -s tests -p 'test_*.py' -v`; expect exit 0 and zero warnings.
- [ ] Run `quick_validate.py /home/a8/.codex/skills/codex-claude-bridge`; expect `Skill is valid!`.
- [ ] Check forbidden permission/shell/listen patterns, remove `__pycache__`, and perform whitespace checks.
- [ ] Compare against the saved pre-change Skill baseline and verify the database project still has exactly the same four pre-existing changed paths.
- [ ] Report PASS or FAIL, changed files, implementation locations, exact results, unverified items, project status, and no commit/push/global-config change. On FAIL, give only targeted fixes.

---

### Task 6: Approved operations-console refresh

**Files:**
- Modify: `assets/monitor.html`
- Modify: `scripts/monitor.py`
- Modify: `scripts/bridge.py`
- Modify: `tests/test_monitor.py`
- Modify: `tests/test_bridge.py`
- Modify: `SKILL.md`
- Modify: `references/live-monitor-design.md`

- [x] Coalesce Claude `text_delta` chunks into one mutable monitor event and
  suppress the matching final assistant duplicate.
- [x] Keep terminal partial text on one flushed line and finish it before tool,
  result, compact, or phase output.
- [x] Add reported cumulative cost, round cost, total output tokens, and API
  average output-token rate without estimating absent values.
- [x] Render a compact Chinese single-page console with an animated
  orange-Claude/cyan-Codex bridge mark, fixed inspector, functional event
  filters, and 10-second telemetry histories.
- [x] Render `AWAITING_INPUT` as an amber `等待用户审核` state with the sanitized
  question, reason, and options while retaining answer-file-only resume.
- [x] Extend simulated-process and state tests for text coalescing, telemetry,
  compact threshold, max rounds, and human-review data.
