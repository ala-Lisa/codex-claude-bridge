---
name: codex-claude-bridge
description: Coordinate repository work where Codex plans and reviews while Claude Code backed by DeepSeek implements. Use to automate their handoffs without copy-paste. Require explicit approval of the repository-specific task, plan, and verification commands, then run until PASS, AWAITING_INPUT, USER_STOPPED, or a safety stop.
---

# Codex Claude Bridge

Use Codex as the read-only technical lead and Claude as the implementer.

## Approve before running

1. Inspect the repository, relevant code and tests, `git status`, and current diff.
2. Write a scoped task, implementation plan, success conditions, stop conditions, and credential-free verification manifest.
3. Present all inputs to the user.
4. Run the bridge only after the user explicitly approves this repository-specific work.

Never treat approval of the bridge itself as approval of a coding task.

Use argument arrays in the verification manifest; never infer commands from prose or use shell operators:

```json
{"version":1,"commands":[["python3","-m","pytest","-q"],["git","diff","--check"]]}
```

## Run

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-claude-bridge/scripts/bridge.py" run \
  --repo /absolute/repo \
  --task-file /absolute/task.md \
  --plan-file /absolute/plan.md \
  --verification-file /absolute/verification.json \
  --approved
```

The bridge loops automatically:

```text
Claude -> verification -> Codex -> PASS
              |             |
              +-- failure --+-> Claude
                            +-> NEEDS_INPUT -> AWAITING_INPUT
```

Verification failure returns directly to the same Claude session. Codex `FAIL` returns targeted corrections to Claude. Only Codex can return `PASS`.

## Observe and resume

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-claude-bridge/scripts/bridge.py" status \
  --repo /absolute/repo
```

Resume an interrupted or user-stopped run by repeating the original command with `--resume`.

For `AWAITING_INPUT`, answer the displayed question in a credential-free file, then repeat the original command with:

```text
--resume --user-answer-file /absolute/answer.md
```

Reuse both saved model sessions. Never resume `AWAITING_INPUT` without an answer.

## Safety

- Keep Codex read-only.
- Never bypass permissions, grant broad Bash access, commit, push, merge, or alter Git history.
- Never put credentials in prompts, manifests, answers, state, or logs.
- Refuse a dirty worktree unless the user explicitly authorizes `--allow-dirty`; preserve all unrelated changes.
- Stop on ambiguous scope, missing authority, invalid output, exhausted bounds, or unsafe overlap.
- Report checks that did not run as unverified.

Use `bridge.py run --help` for options. Read [references/protocol.md](references/protocol.md) only when diagnosing a stopped run, changing the protocol, or reviewing security-sensitive behavior.
