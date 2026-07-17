# Codex Claude Bridge

Coordinate **Codex** as technical lead/reviewer and **Claude Code** as implementer through their local CLIs — no manual copy-paste between tools.

## How it works

```
Plan ──▶ Codex approves plan ──▶ Claude implements ──▶ Manifest checks pass?
                                                              │
                                              ┌─ YES ──▶ Codex review
                                              │              │
                                              │    ┌─ PASS ──▶ Done
                                              │    ├─ FAIL ──▶ Back to Claude
                                              │    └─ NEEDS_INPUT ──▶ Pause for human
                                              │
                                              └─ NO  ──▶ Back to Claude (no Codex call)
```

1. You approve a **repository-specific implementation plan** (hard gate — never starts without it).
2. Claude implements; the bridge runs your **verification manifest** after each attempt.
3. Only when manifest checks pass does Codex review the diff. Codex `FAIL` sends targeted corrections back to Claude. Codex `PASS` ends the task.
4. Safety bounds (`--max-implementation-attempts`, `--max-codex-reviews`) prevent runaway loops.

## Quick start

```bash
# 1. Write an approved plan
# 2. Write a verification manifest (credential-free JSON)
# 3. Run the bridge:
python3 ~/.codex/skills/codex-claude-bridge/scripts/bridge.py run \
  --repo /path/to/repo \
  --task-file /path/to/task.md \
  --plan-file /path/to/approved-plan.md \
  --verification-file /path/to/verification.json \
  --approved \
  --max-implementation-attempts 12 \
  --max-codex-reviews 8
```

## Live monitor

A browser-based HUD shows Claude/Codex activity in real time — single-screen Chinese console with event tape, telemetry wall, and review controls. Terminal stream and sanitized JSONL remain available as fallback.

```bash
# Headless mode
--no-monitor

# Serve without opening browser
--no-open-browser
```

## Key safety rules

- Codex review is **read-only** (never writes code)
- Never commits, pushes, or alters git history
- Never logs credentials or copies API keys into prompts
- Preserves unrelated changes; stops on ambiguous overlap

## Outcomes

| Status | Meaning |
|--------|---------|
| `PASS` | Codex confirmed success conditions satisfied |
| `FAIL` | Codex returned targeted corrections → back to Claude |
| `VERIFICATION_FAILED` | Manifest command failed → back to Claude (no Codex) |
| `AWAITING_INPUT` | Codex needs a human decision → bridge paused |
| `USER_STOPPED` | Browser stop control terminated the loop |
| `STOPPED` | Safety limit reached or fatal error |

## Repository structure

```
├── SKILL.md              # Full skill definition and instructions
├── scripts/
│   ├── bridge.py         # Main bridge orchestrator
│   ├── monitor.py        # Browser-based live monitor
│   └── control.py        # Loopback control surface
├── assets/
│   └── monitor.html      # Monitor frontend
├── references/           # Design docs and implementation plans
├── tests/                # Test suite
└── agents/               # Agent configuration
```

## Resuming and status

```bash
# Check state
python3 ~/.codex/skills/codex-claude-bridge/scripts/bridge.py status --repo /path/to/repo

# Resume interrupted run
python3 ... --resume

# Resume after AWAITING_INPUT (requires answer file)
python3 ... --resume --user-answer-file /path/to/answer.md
```
