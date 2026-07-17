# Bridge protocol

## Roles

- Human: defines the goal and approves the first repository-specific plan.
- Codex: independently reviews repository evidence and returns structured PASS, FAIL, or NEEDS_INPUT.
- Claude Code with DeepSeek: implements only the approved plan or Codex's targeted correction.
- Bridge: transports messages, persists session identifiers, enforces bounds, and records sanitized logs.

## State machine

`APPROVED -> CLAUDE_RUNNING -> VERIFYING`

When an approved command returns nonzero or times out:

`VERIFYING -> VERIFICATION_FAILED -> CLAUDE_RUNNING`

This path does not call Codex. When all approved commands pass:

`VERIFYING -> CODEX_REVIEWING -> PASS`

On a review failure with rounds remaining:

`CODEX_REVIEWING -> CLAUDE_RUNNING -> VERIFYING`

When Codex cannot safely decide without the human:

`CODEX_REVIEWING -> AWAITING_INPUT`

The process exits normally and releases the lock. It must not call Claude again until a credential-free answer file is supplied. Resume moves to the next bounded round and reuses both stored session IDs:

`AWAITING_INPUT -> CLAUDE_RUNNING -> CODEX_REVIEWING`

The loopback control surface may request an orderly user stop from any active
model or verification phase:

`CLAUDE_RUNNING|VERIFYING|CODEX_REVIEWING -> USER_STOPPING -> USER_STOPPED`

The Bridge terminates only its registered child process group, persists the
terminal state, releases the lock, and does not continue automatically. An
explicit `--resume` reuses both stored model sessions and the selected review
configuration.

An approved verification command's nonzero exit or timeout is repairable and returns to Claude. A command that cannot start, a bridge I/O failure, invalid structured output, active lock, exhausted implementation/review limit, or ambiguous repository condition leads to `STOPPED`.

## Durable files

The bridge stores files under `<repo>/.codex-bridge/` and adds that directory to `.git/info/exclude`:

- `state.json`: phase, round, session IDs, timestamps, and final status.
- `events.jsonl`: sanitized command and phase events.
- `prompts/`: exact sanitized prompts exchanged in each round.
- `outputs/`: sanitized model results and repository evidence.
- `outputs/round-NN-claude-stream.jsonl`: complete sanitized Claude stream-json plus sanitized stderr records.
- `outputs/round-NN-codex-review-RR-start-SS.jsonl`: one complete sanitized
  Codex stream per logical review start; a browser-triggered replacement gets a
  new `SS` file and never overwrites the interrupted stream.
- `outputs/attempt-NN-verification.json`: bounded sanitized results from the bridge-owned approved command gate.
- The default loopback monitor serves a browser page from `127.0.0.1` on a random port and records `monitor_url` in state when available. `--no-monitor` disables it; `--no-open-browser` serves it without launching a browser.
- `lock`: single-task exclusion lock containing the bridge PID.

These files are local evidence, not project deliverables. Do not commit them.

## Session handling

- Start Claude with a generated UUID and reuse it with `--resume` for corrections.
- Start Codex with `codex exec --json`; persist
  `thread.started.thread_id` immediately and reuse it with `codex exec resume`.
  An active browser model switch may terminate and replace a logical review at
  most once, using that same thread ID and byte-identical prompt. Interrupted or
  structurally invalid output does not consume a completed-review count.
- In WSL, prefer a native Linux ELF from the installed VS Code Codex extension. Refuse launchers that forward to Windows `codex.exe`; do not translate the repository to a UNC path.
- Run Codex with `--sandbox read-only` and `--ask-for-approval never` on the first review.
- Run Claude with `acceptEdits`, stream-json, verbose partial messages, and an explicit tool allowlist. Do not use bypass mode.
- Pass an explicit disallowed-tool list; `Agent` is disabled by default so an implementation session cannot create unreviewed Claude subagents. Change this only when the approved repository plan expressly permits them.
- Drain Claude stdout and stderr concurrently. Sanitize before writing or displaying every event.
- Drain Codex JSONL stdout and stderr concurrently during review and retain the complete sanitized JSONL for audit. The terminal and browser show one semantic review status, aggregate item activity into counters, and suppress individual item start/completion rows. After a handoff, the browser displays the complete sanitized Claude report and complete structured Codex decision, including evidence, remaining issues, targeted instructions, and NEEDS_INPUT fields. Never display hidden reasoning.
- Render Claude partial text in one mutable transcript row. Replace that row
  with the complete sanitized Claude handoff instead of appending a duplicate.
  Render one Codex review-status row while work is active and replace it with
  the complete validated decision. Merge each Claude tool start and terminal
  result into one row. Keep the transcript as the only AI-output surface; do
  not add secondary summary cards. Never display hidden reasoning.
- Run approved verification commands as argument lists with `shell=False`, repository cwd, bounded sanitized output, and concurrent stdout/stderr draining. Do not infer commands from prose.
- Set `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50` only in the Claude child environment unless the invoking environment already supplied a value. Record the actual value, not the entire environment.
- Display `compact_boundary` with its provided `preTokens`; do not infer an unavailable context percentage.
- Use the explicit 1,000,000-token configured denominator only for `deepseek-v4-pro[1m]` until Claude reports `modelUsage.contextWindow`; unknown models do not receive a guessed denominator.
- New tasks default to visible cached `gpt-5.6-sol` plus reasoning effort `max`.
  Load browser choices from the local Codex model cache, then expose only the
  list-visible 5.6 family in fixed Sol, Terra, Luna order. In the browser allow
  only `max`, `xhigh`, and `high`, in that order. Lower efforts remain available
  only through existing explicit CLI configuration. Older list-visible models
  likewise remain usable through the CLI but do not appear in the browser.
  Forward selections as argument-list values. Persist effective and pending
  selections in state, reuse them on resume, and reject conflicting resume
  values before starting a model process.

## Review contract

Codex must return JSON matching this shape:

PASS and FAIL retain the existing fields:

```json
{
  "status": "PASS or FAIL",
  "evidence": ["observed fact"],
  "remaining_issues": ["unresolved issue"],
    "next_instructions": "targeted instructions; empty on PASS",
    "question": "",
    "reason": "",
    "options": []
}
```

Use NEEDS_INPUT only for a genuine requirement ambiguity, mutually exclusive product/scope choice, conflict, or missing authority:

```json
{
  "status": "NEEDS_INPUT",
  "evidence": ["observed fact"],
  "remaining_issues": ["what remains blocked"],
  "next_instructions": "",
  "question": "one clear question",
  "reason": "why the bridge cannot decide safely",
  "options": ["small", "mutually exclusive", "option set"]
}
```

The flat schema keeps all seven fields required because Codex structured output does not accept top-level `oneOf`; fields for the inactive branch must be empty as shown. Options may be empty when they do not apply. NEEDS_INPUT is not FAIL and consumes no correction loop before the user answers.

PASS is valid only when the approved success conditions are met, bridge-owned checks actually passed, the diff is in scope, and there is no known blocker. On FAIL, report all blocking in-scope issues found in that pass rather than intentionally stopping after the first. Environment-limited checks remain unverified and normally require FAIL or a human stop.

## Verification manifest and counters

- Validate the approved UTF-8 JSON manifest before either model starts.
- Store its SHA-256 digest and sanitized immutable command arrays in state.
- Reject a conflicting manifest on resume before either model starts.
- Count each Claude process as one implementation attempt.
- Count Codex only after a complete structured review conclusion is read and
  validated. Process starts, interrupted replacements, and malformed output do
  not increment `codex_reviews`.
- Default bounds are 12 implementation attempts and 8 Codex reviews.
- Preserve `--max-rounds` as an alias for the Codex-review bound.
- Migrate older active state without lowering its saved review bound.

## Human-answer resume

- Persist the question, reason, and options under `awaiting_input` and show them through `status`.
- Reject `--resume` without `--user-answer-file` while awaiting input.
- Reject missing, empty, oversized, non-UTF-8, or credential-like answer files without changing `AWAITING_INPUT` or calling either model.
- Do not persist the answer in `state.json` or events. Put its sanitized, explicitly marked text only in the next Claude prompt.
- Clear `awaiting_input` after accepting the answer and keep the original Claude and Codex session IDs.
- Continue to enforce the original maximum rounds, timeouts, permissions, dirty-worktree policy, and lock.

## Loopback review controls

- Bind the monitor to loopback only. Generate one high-entropy control token in
  memory for the run; never write it to state, events, output JSONL, logs, URLs,
  or SSE snapshots.
- Bootstrap the token only through the monitor document's same-origin request.
  Every state-changing POST requires the token header, exact same Origin when
  present, JSON content, a body no larger than 2 KiB, and the exact documented
  field set. Reject arbitrary model strings and hidden catalog entries.
- An idle selection is pending for the next review and must survive an explicit
  resume. During `CODEX_REVIEWING`, one selection change may terminate and
  restart that logical review. A second change returns conflict without touching
  the process.
- Stop has priority over a simultaneous model switch. It is idempotent, moves
  through `USER_STOPPING` to `USER_STOPPED`, and terminates only the Bridge-owned
  active process group with bounded TERM/KILL escalation.
- Keep the stopped browser DOM visible, disable further controls, and show only
  the generic recovery shape: append `--resume` to the original run command. Do
  not render task paths or credentials in this recovery hint.

## Threat model and limits

The bridge does not read or automate VS Code chat Webviews. Both agents run as child CLI processes in WSL. A broad Bash permission is intentionally not granted by default; the allowlist covers Git inspection (including the ECC hook's read-only `rtk git` rewrite) and common Python/Node test, lint, and build commands. It does not disable ECC Gateguard. Extend it only for a specific approved task, for example the exact `.venv/bin/python -m pytest *` pattern used by a repository. Keep `Agent` in the disallowed list unless the approved plan explicitly authorizes subagents.

Redaction removes credential-shaped text, sensitive JSON fields, and exact values inherited through credential-named environment variables before terminal or file output. It is defense in depth, not a credential vault: prompts must never contain secrets. If output reveals a token, stop, remove the log, and rotate the credential.

The lock contains an owner token. A process may remove only the lock it acquired; a competing process must not overwrite state or remove the active owner's lock.
