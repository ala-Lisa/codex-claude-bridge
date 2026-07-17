# Codex Claude Bridge Live Monitor Design

**Date:** 2026-07-16

## Goal

Add an automatically opened, local-browser monitoring window to the bridge. The page must show both a real-time sanitized Claude/Codex event stream and a continuously updated HUD without becoming part of the execution or decision path.

## Scope

The monitor covers one running bridge task and all of its bounded review rounds. It displays phase, model, context, tools, compaction, process, repository, configuration-count, and review information. It remains read-only and local to the machine.

The monitor does not add a way to execute commands, answer `NEEDS_INPUT`, modify state, alter permissions, or change model configuration. It does not modify Claude HUD, Claude global settings, shell configuration, or the target repository.

## Architecture

### Monitor service

Create `scripts/monitor.py` as a focused component responsible for:

- maintaining a sanitized in-memory snapshot;
- consuming sanitized bridge, Claude, and Codex events;
- sampling read-only Git and child-process resource data;
- serving a local page and Server-Sent Events stream;
- handling browser launch as a non-fatal convenience.

The service binds only to `127.0.0.1` on an operating-system-assigned port. It has no mutating HTTP endpoint. Client disconnects and monitor failures cannot stop or change the bridge workflow.

### Browser page

Create `assets/monitor.html` as a self-contained page with inline CSS and JavaScript. It must not load CDNs, fonts, analytics, or any other external resource. A restrictive Content Security Policy permits only the local document and its local SSE connection.

The page uses one viewport-filling console with three coordinated regions:

1. A compact header showing task identity, state, Claude and Codex models, Claude Code version, automatic-compaction threshold, rule/hook counts, round, and elapsed time.
2. A telemetry strip and main event tape showing sanitized Claude text, tool names, commands, tool results, test activity, Codex review activity, and terminal outcomes.
3. A fixed inspector showing execution phases, tool counts, Git scope, and the current human-review question when input is required.

Claude partial text updates one existing event row until its content block ends.
The final assistant message updates or finalizes that row instead of adding a
duplicate. The terminal similarly keeps partial chunks on one live line. The
log auto-scrolls by default and offers local filtering, search, and command
copying. These controls affect only page presentation.

The visual language is a dense Chinese operations console: cyan denotes Codex
and normal execution, orange denotes Claude, and amber is reserved for
`AWAITING_INPUT`. The animated bridge mark uses visible closed tracks so every
moving particle remains attached to a rendered orbit.

### Bridge integration

`scripts/bridge.py` starts the monitor only after plan approval, repository validation, and lock acquisition succeed. It forwards already-sanitized events to the monitor while preserving the existing terminal output and JSONL files.

The monitor starts before the first model subprocess. The bridge attempts to open the page in the default browser. If opening fails, it prints the local URL and continues. The bridge records monitor startup and failure events without exposing an environment dump.

The monitor receives phase changes for Claude execution, Codex review, correction rounds, PASS, AWAITING_INPUT, and STOPPED. It cannot call either model directly.

## Data Sources and Accuracy

### Context window

The live numerator is computed only from Claude usage fields actually present in stream-json assistant events:

```text
input_tokens + cache_creation_input_tokens + cache_read_input_tokens
```

For the exact initialized model name `deepseek-v4-pro[1m]`, the HUD may use `1,000,000` as an immediately available configured denominator and labels it `configured`. Existing sanitized bridge streams independently show Claude reporting `modelUsage.contextWindow = 1,000,000` for this model.

When a result event supplies `modelUsage.contextWindow`, that reported value replaces the configured value and the label becomes `Claude reported`. A mismatch produces a visible warning and the reported value wins. Models without either a recognized configured window or a reported value show an unknown denominator and no fabricated percentage.

### Other HUD fields

- Model and Claude Code version come from the stream-json initialization event.
- Cost comes only from Claude result `total_cost_usd`, or the sum of reported
  per-model `costUSD` values when the total is absent.
- Output speed is the result output-token count divided by
  `duration_api_ms`; it is an API-response average, not an invented
  instantaneous rate.
- Prompt-cache TTL is a labeled countdown from the latest assistant response timestamp using the existing 300-second HUD convention; it is not represented as an API guarantee.
- Tool running, completed, and failed counts come from tool-use and tool-result events.
- Current command comes from a sanitized tool input command.
- Compaction count and `preTokens` come from `compact_boundary` events.
- Claude process RSS comes from `/proc/<pid>/status`; failure displays unavailable.
- Git branch, changed paths, and line totals come from read-only Git commands and refresh periodically; failure displays unavailable.
- CLAUDE.md, rules, and hook counts are counts only. The monitor does not read or transmit secret configuration values.
- Codex phase and outcome come from bridge state and structured review output. Codex hidden reasoning is never displayed.
- Context, cost, output speed, tools, memory, and cache time series are browser
  snapshots sampled every 10 seconds. No random or interpolated points are
  generated.

## Security

- Reuse the bridge's existing environment-secret and credential-pattern redaction before data reaches monitor state.
- Never send environment variables, raw credential fields, or unsanitized subprocess output to the page.
- Bind only to loopback and use a random port.
- Supply security headers including a restrictive CSP, `X-Content-Type-Options: nosniff`, and `Cache-Control: no-store`.
- Keep all endpoints read-only and reject unsupported methods.
- Do not weaken Claude permissions, Codex read-only review, dirty-worktree protection, locks, timeouts, or round bounds.
- Do not add dependencies.

## Lifecycle and Degradation

The normal sequence is validation, lock acquisition, monitor startup, browser-open attempt, Claude streaming, Codex review, and terminal outcome. All review rounds reuse the same page and cumulative counters.

Browser-open failure, SSE disconnect, Git sampling failure, resource sampling failure, and internal monitor failure are non-fatal. The existing sanitized terminal stream remains available. The affected field displays unavailable rather than a guessed value.

On PASS, AWAITING_INPUT, or STOPPED, the page retains its last rendered state and marks the monitor connection ended when the bridge exits. AWAITING_INPUT switches the status, phase, and review card to amber, displays the stored question, reason, and options, and provides no answer submission control.

## Verification

Use only temporary repositories and simulated Claude/Codex executables for monitor workflow tests. Do not call real Claude, DeepSeek, or Codex APIs for this feature verification.

Tests must prove:

- the page and SSE stream are available before the simulated Claude process exits;
- real-time text, tools, commands, results, tests, and review phases appear;
- stream-json partial text becomes one updating browser row and one continuous
  terminal line without a duplicate final assistant message;
- result cost and API output rate appear only from reported numeric fields;
- `AWAITING_INPUT` exposes the sanitized question, reason, and options to the
  read-only page and makes no model call;
- the DeepSeek configured 1M value, Claude-confirmed value, and mismatch warning behave as specified;
- tool, compaction, Git, memory, cache, and configuration-count fields update;
- secrets do not appear in HTML, SSE, snapshots, terminal output, or event files;
- browser-open failure and monitor-client disconnect do not affect the existing FAIL-then-PASS flow;
- NEEDS_INPUT, answer resume, session reuse, locking, timeouts, and round limits remain intact;
- syntax checks, strict-warning tests, skill validation, whitespace checks, and scope checks pass;
- no cache artifacts, target-project changes, global configuration changes, commits, or pushes are produced.
