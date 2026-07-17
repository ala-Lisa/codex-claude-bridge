# Monitor Codex Controls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a secure loopback control surface that defaults Codex to GPT-5.6 Sol with maximum reasoning, can restart one active review in the same session, and can stop every Bridge-owned background process.

**Architecture:** A new standard-library `control.py` owns the frozen model catalog and thread-safe control state. `monitor.py` exposes token-protected fixed JSON endpoints and renders the controls. `bridge.py` supervises process groups, persists early Codex thread IDs, restarts one review with the same prompt/session, and handles a user stop as a distinct terminal state.

**Tech Stack:** Python 3 standard library, `subprocess`, `threading`, loopback `http.server`, server-sent events, vanilla HTML/CSS/JavaScript, `unittest`, temporary Git repositories, mock executables.

## Global Constraints

- Modify only `/home/a8/.codex/skills/codex-claude-bridge`.
- Do not call real Claude, DeepSeek, or Codex APIs.
- Do not add dependencies.
- Do not commit, push, amend, rebase, or modify Git history.
- Preserve dirty-worktree protection, owner-token locking, redaction, bounded attempts, and read-only Codex review.
- Bind all browser endpoints to `127.0.0.1` and never accept arbitrary commands, paths, PIDs, prompts, or environment values.
- Default new tasks to `gpt-5.6-sol` with `model_reasoning_effort="max"`.
- Allow one user-requested Codex restart per review number; count only complete validated conclusions in `codex_reviews`.
- User stop takes precedence over model switching and ends in `USER_STOPPED`.

---

### Task 1: Frozen Codex Model Catalog and Defaults

**Files:**
- Create: `scripts/control.py`
- Modify: `scripts/bridge.py`
- Test: `tests/test_control.py`
- Test: `tests/test_bridge.py`

**Interfaces:**
- Produces: `CodexModelOption`, `CodexControlSelection`, `load_codex_model_catalog(path)`, `DEFAULT_CODEX_SELECTION`.
- Consumes: `~/.codex/models_cache.json` supplied as an explicit `Path`; no environment enumeration.

- [ ] **Step 1: Write failing catalog and default tests**

  Add tests proving that only `visibility="list"` models survive, hidden models are excluded, reasoning levels are frozen, malformed cache values raise a fixed `ControlConfigError`, and the default is exactly `gpt-5.6-sol/max`. Add a bridge argument test expecting `--model gpt-5.6-sol` and `model_reasoning_effort="max"` when no override is passed.

- [ ] **Step 2: Run the focused tests and verify RED**

  Run:

  ```bash
  python3 -m unittest tests.test_control tests.test_bridge.BridgeTests.test_default_codex_selection_is_sol_max -v
  ```

  Expected: import failure for `scripts.control` or missing explicit default arguments.

- [ ] **Step 3: Implement the immutable catalog and defaults**

  Parse only `slug`, `display_name`, `visibility`, and
  `supported_reasoning_levels[].effort`. Require nonempty strings, unique visible
  slugs, and supported values from `{low, medium, high, xhigh, max}`. Do not expose
  `ultra`. Limit the browser catalog to the list-visible Sol, Terra, and Luna
  entries in that fixed order. Validate Sol and max before a new task starts.
  Extend the CLI reasoning choices to include `max`; explicit user arguments
  still override defaults.

- [ ] **Step 4: Run focused tests and verify GREEN**

  Run the Step 2 command and expect all tests to pass.

### Task 2: Thread-safe Control State and HTTP Contract

**Files:**
- Modify: `scripts/control.py`
- Modify: `scripts/monitor.py`
- Test: `tests/test_control.py`
- Test: `tests/test_monitor.py`

**Interfaces:**
- Produces: `ReviewControl.bootstrap()`, `request_selection(model, effort)`, `request_stop()`, `mark_review_started(review_no)`, `mark_effective(selection)`, and fixed `ControlResponse` results.
- Consumes: immutable catalog and callbacks supplied to `LiveMonitor`.

- [ ] **Step 1: Write failing state-machine and HTTP tests**

  Cover same-origin/token success; 403 token/origin failures; 400 invalid JSON,
  fields, model, and effort; 413 body limit; 405 unrelated mutation; no token in
  snapshots or logs; next-review selection with no active review; one immediate
  restart; second restart 409; idempotent stop; and stop-over-switch precedence.

- [ ] **Step 2: Run focused tests and verify RED**

  ```bash
  python3 -m unittest tests.test_control tests.test_monitor.LiveMonitorControlTests -v
  ```

  Expected: missing control state and endpoints.

- [ ] **Step 3: Implement minimal control state and endpoints**

  Use `secrets.token_urlsafe`, `threading.Condition`, strict 2 KiB reads, exact
  JSON key sets, and fixed JSON responses. Add `GET /control/bootstrap`,
  `POST /control/review-config`, and `POST /control/stop`. Disable `BaseHTTPRequestHandler`
  request logging and apply existing security headers. Never echo rejected input.

- [ ] **Step 4: Run focused tests and verify GREEN**

  Run the Step 2 command and expect all tests to pass.

### Task 3: Active Process Supervision and User Stop

**Files:**
- Modify: `scripts/bridge.py`
- Modify: `scripts/control.py`
- Test: `tests/test_bridge.py`

**Interfaces:**
- Produces: Bridge-owned active-process registration, bounded process-group termination, `UserStopRequested`, `USER_STOPPING`, and `USER_STOPPED`.
- Consumes: `ReviewControl` stop actions.

- [ ] **Step 1: Write failing subprocess stop tests**

  Add mock executables that spawn a child and block. Exercise stop during Claude,
  Codex, and verification. Assert parent and child exit, state becomes
  `USER_STOPPED`, final SSE delivery occurs, lock is released, files/session IDs
  remain, repeated stop is idempotent, and no later model process starts.

- [ ] **Step 2: Run the stop tests and verify RED**

  ```bash
  python3 -m unittest tests.test_bridge.BridgeControlTests.test_user_stop_during_claude tests.test_bridge.BridgeControlTests.test_user_stop_during_codex tests.test_bridge.BridgeControlTests.test_user_stop_during_verification -v
  ```

  Expected: no active-process control or USER_STOPPED state.

- [ ] **Step 3: Implement process supervision**

  Launch all Bridge-owned children with a separate POSIX session, register one
  active process under a lock, and clear it by owner identity. Add bounded
  SIGTERM/SIGKILL termination. Make all execution loops check the stop action and
  raise `UserStopRequested` instead of ordinary BridgeError. Persist and flush the
  final snapshot before releasing the owner lock and stopping the server.

- [ ] **Step 4: Run focused tests and verify GREEN**

  Run the Step 2 command and expect all tests to pass with no living mock child.

### Task 4: Same-session Immediate Codex Restart

**Files:**
- Modify: `scripts/bridge.py`
- Modify: `scripts/control.py`
- Test: `tests/test_bridge.py`

**Interfaces:**
- Produces: `ReviewRestartRequested`, early thread-ID persistence, unique review-start stream names, completed-only `codex_reviews`, and persisted restart metadata.
- Consumes: validated selection actions and the exact original Codex prompt.

- [ ] **Step 1: Write the failing restart lifecycle test**

  Use a mock Codex that emits `thread.started`, blocks, records arguments/prompt,
  and exits when terminated. Request Sol/high to Sol/max through the HTTP control,
  then assert the second process uses `resume <same-id>`, receives the byte-identical
  prompt, uses max, preserves both JSONL streams, records one interruption, and
  increments `codex_reviews` only after the second process returns PASS.

- [ ] **Step 2: Add failure and race tests**

  Cover request before `thread.started`, missing thread ID, restart launch failure,
  termination failure, second switch 409, stop racing with switch, and state/event
  redaction. Assert only explicit user restart gets the free retry; ordinary
  process failures STOP.

- [ ] **Step 3: Run restart tests and verify RED**

  ```bash
  python3 -m unittest tests.test_bridge.BridgeControlTests.test_review_restart_reuses_thread_and_prompt tests.test_bridge.BridgeControlTests.test_second_review_restart_is_rejected tests.test_bridge.BridgeControlTests.test_stop_wins_restart_race -v
  ```

  Expected: current review exits as BridgeError or cannot resume with the same ID.

- [ ] **Step 4: Implement the restart loop**

  Persist `thread.started` immediately. Move `codex_reviews` increment until after
  schema validation. Within one logical review number, allow at most two process
  starts: original plus one user restart. Name streams with review and start
  numbers. On the restart signal, mark the first stream interrupted, update the
  effective selection, and call `codex exec resume` with the original prompt.

- [ ] **Step 5: Run restart tests and verify GREEN**

  Run the Step 3 command and expect all tests to pass.

### Task 5: Browser Controls and Final-state UX

**Files:**
- Modify: `assets/monitor.html`
- Modify: `scripts/monitor.py`
- Test: `tests/test_monitor.py`

**Interfaces:**
- Consumes: bootstrap JSON and SSE control snapshot.
- Produces: model/effort selectors, restart confirmation, stop confirmation, effective/pending/restarting display, and retained USER_STOPPED final DOM.

- [ ] **Step 1: Write failing page contract tests**

  Assert the self-contained page contains Chinese labels, `max` mapping, selection
  and stop controls, inline confirmations, status messages, custom-token fetches,
  no `innerHTML`, and no arbitrary text input. Extract JavaScript and execute pure
  state helpers under Node to verify button states for idle, reviewing, restarting,
  restart-used, USER_STOPPING, and USER_STOPPED.

- [ ] **Step 2: Run page tests and verify RED**

  ```bash
  python3 -m unittest tests.test_monitor.LiveMonitorControlPageTests -v
  ```

  Expected: missing controls and control-state helpers.

- [ ] **Step 3: Implement the compact runtime controls**

  Build all option and status DOM nodes with `textContent`. Fetch bootstrap once,
  keep the token only in a closure, send a custom header, and coalesce control
  events. Add the fixed warning beside `最高`. Require inline confirmation before
  active restart and stop. On the final USER_STOPPED snapshot, stop reconnecting
  and retain the last page state.

- [ ] **Step 4: Run page tests and JavaScript syntax check**

  ```bash
  python3 -m unittest tests.test_monitor.LiveMonitorControlPageTests -v
  python3 - <<'PY'
  from pathlib import Path
  import re
  body = Path('assets/monitor.html').read_text(encoding='utf-8')
  script = re.search(r'<script>(.*?)</script>', body, re.S)
  assert script
  Path('/tmp/codex-claude-bridge-controls.js').write_text(script.group(1), encoding='utf-8')
  PY
  node --check /tmp/codex-claude-bridge-controls.js
  ```

  Expected: tests pass and Node exits 0.

### Task 6: Resume, Documentation, and Full Verification

**Files:**
- Modify: `SKILL.md`
- Modify: `references/protocol.md`
- Modify: `tests/test_bridge.py`
- Modify: `tests/test_monitor.py`
- Test: `tests/test_control.py`

**Interfaces:**
- Produces: documented controls, USER_STOPPED resume semantics, and complete regression evidence.

- [ ] **Step 1: Add resume and compatibility tests**

  Assert USER_STOPPED resume reuses both model session IDs and selected model/effort,
  old persisted tasks retain their configuration, new tasks use Sol/max, conflicting
  explicit resume flags remain rejected, locks retain owner-token protection, and
  FAIL-to-PASS plus NEEDS_INPUT flows remain unchanged.

- [ ] **Step 2: Update SKILL and protocol documentation**

  Document model labels, new defaults, one restart per review, completed-only review
  counting, stop behavior, final-page retention, security boundary, and exact CLI
  fallback controls. State that max generally consumes more per review and that the
  verification gate—not UI sampling—avoids unnecessary Codex calls.

- [ ] **Step 3: Run fresh complete verification**

  ```bash
  python3 -m py_compile scripts/bridge.py scripts/monitor.py scripts/control.py tests/test_bridge.py tests/test_monitor.py tests/test_control.py
  python3 /home/a8/.codex/skills/.system/skill-creator/scripts/quick_validate.py /home/a8/.codex/skills/codex-claude-bridge
  python3 -m unittest discover -s tests -q
  python3 -W error -m unittest discover -s tests -q
  ```

  Expected: syntax exits 0, quick validation prints `Skill is valid!`, and both
  complete test runs report the same passing test count with no warnings.

- [ ] **Step 4: Run production safety and scope checks**

  ```bash
  rg -n 'except\s+(Exception|BaseException)|bypassPermissions|dangerously-bypass|shell\s*=\s*True|innerHTML' scripts assets
  find . -maxdepth 3 -type f -newermt '2026-07-17 00:00:00' -print | sort
  git -C /home/a8/objects/Mitochondrial_Database status --short
  ```

  Expected: production scan has no matches; changed files stay within the Skill;
  the database repository status matches its pre-task snapshot exactly.

- [ ] **Step 5: Start a mock-only browser demonstration**

  Run a temporary monitor with mock process/control events, verify bootstrap, SSE,
  restart, stop, final state, and redaction through loopback HTTP, then open its URL
  for human visual acceptance. Do not invoke a real model.
