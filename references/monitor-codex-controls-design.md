# Monitor Codex Controls Design

**Date:** 2026-07-17

## Goal

Turn the loopback monitor into a narrowly scoped local control surface that can:

1. choose the Codex model and reasoning effort;
2. immediately interrupt an active Codex review and restart it in the same
   Codex session with the same review material;
3. stop all Bridge-owned AI and verification subprocesses from the browser;
4. preserve the existing redaction, locking, bounded-review, and audit rules.

## Non-goals

- Do not expose arbitrary commands, paths, PIDs, prompts, or environment values.
- Do not bind the monitor outside `127.0.0.1`.
- Do not control unrelated processes.
- Do not add dependencies or call real Claude, DeepSeek, or Codex during tests.
- Do not commit, push, or modify Git history.
- Do not add browser-based task resume or human-answer submission.
- Do not display hidden model reasoning.

## Defaults and supported choices

New tasks explicitly start Codex review with:

```text
model = gpt-5.6-sol
model_reasoning_effort = max
```

The selector reads `~/.codex/models_cache.json` and exposes only list-visible
`gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna`, in that order. Hidden
entries such as `codex-auto-review` and older visible model families are not
available in the browser. Reasoning choices come from the selected model's
`supported_reasoning_levels`; no free-form model or effort input is accepted.

Chinese effort labels are fixed:

| CLI value | Label |
| --- | --- |
| `low` | 轻度 |
| `medium` | 中 |
| `high` | 高 |
| `xhigh` | 极高 |
| `max` | 最高 |

`ultra` is not exposed in this version because it is not present in the
approved UI and can change agent delegation behavior. If Sol or `max` is absent
from the local cache, a new task stops before invoking Codex; it does not
silently downgrade. An older resumed task keeps its persisted effective model
and effort until the user changes them.

## Components

### Control catalog and state

Add `scripts/control.py` with narrowly focused, standard-library-only types:

- `CodexModelOption`: immutable slug, display name, and supported efforts.
- `CodexControlSelection`: immutable model and effort pair.
- `load_codex_model_catalog(path)`: parse, validate, filter, and freeze the
  local cache without returning instructions, descriptions, or other fields.
- `ReviewControl`: thread-safe current/pending selection, one-restart budget,
  stop flag, random control token, and change notifications.
- `ControlAction`: immutable action returned to Bridge code.

The control token is generated per Bridge process. It is never placed in
`state.json`, `events.jsonl`, model prompts, or terminal output.

### Monitor HTTP control surface

`LiveMonitor` accepts a `ReviewControl` instance and exposes:

- `GET /control/bootstrap`: same-origin JSON containing the token, filtered
  choices, effective/pending selections, restart availability, and current
  control state;
- `POST /control/review-config`: request a next-review configuration or an
  immediate active-review restart;
- `POST /control/stop`: request user termination.

POST requests require all of the following:

- loopback server binding already enforced by `LiveMonitor`;
- exact same-origin `Origin` when supplied;
- `Content-Type: application/json`;
- the random token in `X-Bridge-Control-Token`;
- a body no larger than 2 KiB;
- an exact JSON object with no additional fields;
- model and effort present in the frozen catalog.

The server returns fixed Chinese errors and never echoes rejected values.
Unsupported mutating routes remain `404` or `405` as appropriate.

### Active process supervision

Bridge records exactly one active child process under a lock, together with its
kind: `claude`, `codex`, or `verification`. All three launch in their own POSIX
process group. Control code can only signal the process currently registered by
that Bridge instance.

Termination sends `SIGTERM`, waits a short bounded interval, and then uses
`SIGKILL` only when required. Registration is cleared only by the process owner.
Competing requests cannot replace the active process record.

### Immediate Codex review restart

Codex `thread.started` is parsed while JSONL is streaming. The thread ID is
persisted immediately, before review completion. When a switch request arrives:

1. validate and persist the pending selection;
2. wait until the current thread ID is available;
3. mark the current review start as interrupted;
4. terminate only the active Codex process group;
5. retain its sanitized JSONL under a unique start-numbered filename;
6. update the effective selection;
7. invoke `codex exec resume <same-thread-id>` with the same prompt, review
   number, repository evidence, schema, and new model/effort;
8. accept only a complete validated PASS, FAIL, or NEEDS_INPUT object.

Each review number permits one user-requested restart. A second request returns
HTTP 409 and cannot terminate the process. `codex_reviews` increases only after
a complete structured conclusion is validated. `codex_review_restarts` records
the interrupted start separately. Other process failures remain STOPPED rather
than becoming free retries.

### User stop

The red `终止任务` control requires an inline confirmation. On confirmation:

1. atomically set `USER_STOPPING`;
2. make stop override a pending model switch;
3. terminate the active Bridge-owned process group;
4. prevent all later Claude, Codex, and verification starts;
5. persist `USER_STOPPED`, the phase, and stop timestamp;
6. preserve logs, outputs, worktree changes, and both session IDs;
7. deliver a final SSE snapshot;
8. release the owner-token lock;
9. shut down the monitor server and Bridge process.

The already-open browser tab retains the final DOM and shows the original
`--resume` recovery pattern. Reloading the old random-port URL is not promised.
Resume opens a new monitor URL and reuses the stored sessions and selection.

## User interface

The existing single-page console receives a compact control group inside the
runtime panel:

- model selector;
- reasoning selector;
- effective and pending configuration labels;
- `立即重启审核` or `应用到下次审核` button;
- red `终止任务` button.

During review, configuration changes require one inline confirmation explaining
that the current review will stop and that the round has one switch. While
restarting, controls are disabled and the header reports `正在重启审核`. After
the budget is used, the button reports `本轮切换次数已用完`.

The effective label changes only after the new Codex process starts. One
coalesced event reports the old and new configuration; internal Codex item
traffic remains suppressed. `最高` includes the note `审核质量优先，通常消耗更多额度`.

The stop confirmation states that Claude, Codex, or verification may be
terminated, but files and logs are retained. The final page uses a red
`任务已终止` state and shows the resume command shape without embedding paths or
credentials supplied by the user.

## Persistent state and events

Persist only sanitized control metadata:

```text
codex_model
codex_reasoning_effort
codex_review_restarts
active_review_restart_used
last_control_action
user_stopped_at
```

Events record effective selections, restart count, review number, and fixed
status text. They do not record the control token, request headers, full model
cache, environment, hidden model entries, or rejected values.

Each Codex start uses a unique sanitized stream filename so an interrupted
stream is never overwritten. Interrupted output is audit evidence only and is
never parsed as a conclusion.

## Errors and precedence

- Bad token or origin: HTTP 403.
- Invalid JSON, model, effort, or additional key: HTTP 400.
- Oversized body: HTTP 413.
- Restart already used: HTTP 409.
- No active review: save a valid selection for the next review without starting
  a process.
- Thread ID not yet available: show a pending state and restart immediately
  after `thread.started`.
- Thread ID never becomes available: STOPPED; do not open a replacement thread.
- Restart launch or termination failure: STOPPED with fixed public text.
- Concurrent switch and stop: stop wins and no new Codex process starts.
- Repeated stop request: idempotently return the stopped state.
- AssertionError from internal programming errors continues to propagate in
  tests; it is not converted to a business error.

## Verification

All behavior is verified with temporary repositories and mock executables.
Tests must prove:

- explicit Sol + max defaults and CLI arguments;
- filtered model/effort choices and strict request validation;
- live interruption, same-thread resume, same prompt, and unique JSONL files;
- completed-only review counting plus one restart per review;
- user stop during Claude, Codex, and verification;
- process-group cleanup, final SSE delivery, lock release, and safe resume;
- stop-over-switch race precedence;
- terminal, state, event, and browser redaction;
- unchanged FAIL-to-PASS, NEEDS_INPUT, compaction, streaming, and lock behavior;
- Python syntax, strict warnings, JavaScript syntax, quick validation, and
  production safety scans.
