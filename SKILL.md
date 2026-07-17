---
name: codex-claude-bridge
description: Coordinate an approved coding task between Codex and Claude Code backed by DeepSeek, with live sanitized Claude output, bounded Codex review cycles, and a safe pause for required human decisions. Use when the user wants Codex to plan and review while Claude implements without manually copying messages between the tools. Require one human approval of the repository-specific implementation plan, then run until PASS, AWAITING_INPUT, or a safety stop.
---

# Codex Claude Bridge

Coordinate Codex as technical lead/reviewer and Claude Code as implementer through their local CLIs.

## Hard gate

Do not start the bridge until all of the following are true:

1. Inspect the target repository, relevant files, tests, current diff, and `git status`.
2. Write a concrete implementation plan with scope, non-goals, verification commands, and stop conditions.
3. Present the plan to the user.
4. Receive explicit approval for that plan.

Approval of this Skill's general design is not approval of a future repository task.

## Start an approved task

Write the approved task and plan to files outside the repository or under `.codex-bridge/inputs/`. When the plan contains executable checks, also write a credential-free verification manifest and obtain approval for all three inputs together. Never include API keys or other secrets.

Use this exact verification manifest shape. Each inner array is executed as an
argument list with `shell=False`; do not put shell operators in it.

```json
{
  "version": 1,
  "commands": [
    [".venv/bin/python", "-m", "pytest", "-q"],
    [".venv/bin/python", "-m", "pytest", "-q", "-W", "error"],
    [".venv/bin/python", "-m", "pip", "check"],
    ["git", "diff", "--check"]
  ]
}
```

Run:

```bash
python3 ~/.codex/skills/codex-claude-bridge/scripts/bridge.py run \
  --repo /absolute/path/to/repo \
  --task-file /absolute/path/to/task.md \
  --plan-file /absolute/path/to/approved-plan.md \
  --verification-file /absolute/path/to/verification.json \
  --approved \
  --max-implementation-attempts 12 \
  --max-codex-reviews 8 \
  --codex-reasoning-effort max
```

After each Claude result, the bridge independently runs the approved commands.
A nonzero result or timeout is returned to the same Claude session without
calling Codex. Codex is called only after the full manifest passes. Codex
`FAIL` returns automatically to the same Claude session, which must pass the
full manifest again before another review. `PASS` can only come from Codex;
successful commands mean only that the candidate is ready for review.

Implementation attempts and Codex reviews have separate safety bounds. The
defaults are 12 and 8. `--max-rounds` remains a compatibility alias for
`--max-codex-reviews`; new tasks should use the explicit options. These are
runaway safety stops, not approval batches, so normal repair continues without
human intervention.

The bridge explicitly disables Claude's `Agent` tool by default so the
implementation stays in the approved single-agent loop. Override only when the
repository-specific plan explicitly authorizes Claude subagents:

```text
--claude-disallowed-tools <comma-separated tool names>
```

The bridge refuses a dirty worktree by default. If pre-existing changes are intentional, stop and ask the user before adding `--allow-dirty`; record which changes predated the bridge.

The default Bash allowlist is intentionally narrow. When a repository uses a
virtual-environment executable such as `.venv/bin/python`, extend
`--claude-tools` for the exact approved commands instead of granting broad
`Bash(*)`.

By default the bridge starts a read-only loopback browser monitor and opens it in
the default browser. The page combines the live Claude/Codex event tape with
phase, round, model, context, tools, compaction, Git, cache, RSS, and
configuration-count HUD data. Its single-screen Chinese console keeps one
scrollable transcript on the left, a two-column by three-row telemetry wall on
the right, and phase, tool, review-control, and repository panels beneath the
telemetry. Claude partial text updates one row in place; the final handoff
replaces that row with the complete sanitized report instead of adding a second
copy. A tool start and its completion or failure also share one row. Codex
internal item traffic updates bounded counters without adding feed rows or
terminal lines. While review is active, the page shows one `Codex 正在审查…`
row; the complete structured decision replaces it only after validation. The
decision includes status, evidence, remaining issues, targeted instructions,
and NEEDS_INPUT fields when present. Claude is orange, Codex is violet, and
Bridge/tool events are blue. Raw hidden reasoning is never displayed.
Complete sanitized Codex JSONL remains available under `outputs/` for audit.
Elapsed times use Chinese hour, minute, and second units. The terminal uses the
same semantic filtering. The page has a deliberately narrow control surface:
select a visible cached Codex model and reasoning effort for the next review,
restart one active review once with the same session and byte-identical prompt,
or terminate Bridge-owned model/verification process groups. It cannot submit
shell commands or human answers. Control requests are loopback-only and require
an in-memory per-run token that is never persisted or sent through SSE. Use
`--no-monitor` for headless execution, or `--no-open-browser` to serve it without
launching a browser.

In WSL, the bridge automatically selects the newest executable native Linux Codex bundled with the VS Code Codex extension before consulting `PATH`. It refuses a shell wrapper that forwards to Windows `codex.exe`, because Windows Codex cannot independently execute against the WSL worktree. Use `--codex-bin` or `CODEX_BRIDGE_CODEX_BIN` only for an intentional executable override.

New tasks default to the visible cached model `gpt-5.6-sol` with reasoning
effort `max` (`最高`). The browser catalog is loaded from
`~/.codex/models_cache.json` and presents the fixed visible 5.6 family in this
order: Sol, Terra, Luna. The browser presents only `max` (`最高`), `xhigh`
(`极高`), and `high` (`高`), in that order when supported. Hidden models,
older visible models, and lower efforts are not offered in the browser;
explicit CLI compatibility remains.
CLI overrides are:

```text
--claude-model <Claude model name>
--codex-model <Codex model name>
--codex-reasoning-effort low|medium|high|xhigh|max
```

`high` is a Codex reasoning-effort value, not a Claude model name. Selected and
pending values are stored in `state.json`, reused on resume, and conflicting
resume values are rejected before either model is called. `xhigh` is shown as
`极高`; `max` is shown as `最高`. Higher effort may reduce correction rounds but
is not guaranteed to reduce cost or latency.

## Watch live Claude activity

Keep the bridge in the foreground to see sanitized Claude text, tool names, commands, tool success/failure, and automatic compaction events as they happen. The full sanitized stream is stored at:

```text
<repo>/.codex-bridge/outputs/round-NN-claude-stream.jsonl
```

The bridge passes `--output-format stream-json --verbose --include-partial-messages` to Claude and drains stdout and stderr concurrently. Do not redirect around the bridge's sanitizer when handling credentials.

Codex review JSONL is also drained concurrently. The browser monitor receives
only safe event metadata while review is running; hidden reasoning and raw
environment values are never displayed. The terminal stream and sanitized
JSONL remain the fallback if the browser cannot open or disconnects.

The top telemetry strip shows actual values only when the local streams or
read-only samplers provide them. Claude result events can supply cumulative
cost and API output-token rate; absent values remain unavailable instead of
being estimated. Context, cost, output rate, tool activity, memory, and cache
history charts sample the current snapshot every 3 seconds and retain a
60-second window. Local Git, configuration, memory, and process metrics are
sampled every 3 seconds. Claude
and Codex stream events remain immediate and are not delayed by local sampling. Model labels
update when the corresponding Claude or Codex stream reports a model.

During Codex review, the monitor keeps one purple progress row and updates it
in place with a completed-activity count and a fixed translated lifecycle
label. It never displays hidden reasoning, raw command payloads, or one row per
internal event. The final structured PASS, FAIL, or NEEDS_INPUT decision
replaces that same row.

Claude automatic compaction defaults to 50 percent through a child-process-only environment value:

```text
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=50
```

Set that variable before starting the bridge to override the default. The bridge preserves an explicitly supplied value, records only the actual threshold in `state.json` and `bridge_started`, and never modifies shell, Claude, or project configuration. A `compact_boundary` event is shown live with `preTokens` when Claude provides it; the bridge does not claim a context percentage that stream-json does not expose.

For the exact model name `deepseek-v4-pro[1m]`, the HUD starts with the
explicit configured 1,000,000-token window and labels it `configured`. When
Claude returns `modelUsage.contextWindow`, it switches to `Claude reported`; a
mismatch is shown as a warning and the reported value wins. Other models stay
`unknown` until an actual context window is reported.

## Monitor and resume

The foreground command prints phase changes. In another terminal, inspect durable state with:

```bash
python3 ~/.codex/skills/codex-claude-bridge/scripts/bridge.py status \
  --repo /absolute/path/to/repo
```

If the process is interrupted, rerun the original `run` command with `--resume`. Do not start a second task in the same repository while a lock is active.

The browser `终止任务` action stops only the process group currently owned by
this Bridge run, saves `USER_STOPPED`, releases the single-task lock, and leaves
the final monitor page visible. Resume it with the original `run` command plus
`--resume`; both stored model sessions and the last selected Codex model/effort
are reused. A stopped task never resumes itself.

When `status` reports `AWAITING_INPUT`, read its single question, reason, and options. Write a credential-free answer to a file, then resume with the original arguments plus:

```bash
python3 ~/.codex/skills/codex-claude-bridge/scripts/bridge.py run \
  --repo /absolute/path/to/repo \
  --task-file /absolute/path/to/task.md \
  --plan-file /absolute/path/to/approved-plan.md \
  --approved --resume \
  --user-answer-file /absolute/path/to/answer.md
```

Do not resume an `AWAITING_INPUT` task without an answer file. The bridge reuses both model session IDs and marks the answer explicitly in the next Claude instruction.
The browser monitor changes the task state, review phase, and review card to
amber, labels the task `等待用户审核`, and shows the sanitized question, reason,
and options. This is a notification only; resume still requires the explicit
answer-file command above.

## Interpret outcomes

- `PASS`: Codex found the success conditions satisfied from the actual diff, tests, and repository state.
- `FAIL`: Codex returned a complete targeted correction for all blocking in-scope issues found in that pass; the bridge sends it back to the same Claude session if safety bounds remain.
- `VERIFICATION_FAILED`: an approved command returned nonzero or timed out; the bridge sends its bounded sanitized output directly to the same Claude session without calling Codex.
- `AWAITING_INPUT`: Codex returned `NEEDS_INPUT`; the bridge released its lock and paused without calling Claude again.
- `USER_STOPPED`: the browser stop control terminated the Bridge-owned active process group, preserved files and logs, saved state, and released the lock; explicit `--resume` is required.
- `STOPPED`: A command failed, output was invalid, the timeout or round limit was reached, or a safety condition requires human input.

Read [references/protocol.md](references/protocol.md) when diagnosing a stopped run or changing the protocol.

## Safety rules

- Keep Codex review read-only.
- Never add `--dangerously-bypass-approvals-and-sandbox` or Claude `bypassPermissions`.
- Never commit, push, merge, or alter Git history.
- Never log credentials or copy API keys into prompts.
- Preserve unrelated changes and stop on ambiguous overlap.
- Treat repeated backend permission failures as a stop, not as a reason to weaken permissions.
- Report commands that were not executed as unverified.
