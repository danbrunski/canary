# CANary — Claude Code context

All project orientation and conventions live in `AGENTS.md` (shared by
every agent tool) — read it first:

@AGENTS.md

The rest of this file is Claude Code-specific.

## Memory

The operator's preferences and project state are kept in
`~/.claude/projects/-home-dan-code-canary/memory/MEMORY.md`. Read it
when you join a conversation; update it when you learn something
durable. Don't write secrets there. Notable entries behind the
AGENTS.md conventions: `feedback_main_branch_only`,
`feedback_replay_to_test_models`,
`feedback_fix_source_not_runtime_tolerance`.

## MCP session quirk

The `canary` MCP server (tools namespaced `mcp__canary__*`) can die
when `canary-mcp` is restarted — the server invalidates session ids and
the client doesn't auto-reconnect. Fall back to the HTTP counterparts
described in AGENTS.md; restarting `claude` re-establishes MCP cleanly.

## Overnight automation (`.claude/`)

Unattended roadmap implementation runs via the `/overnight` command
(standing rules + orchestrator protocol) with a `/goal` condition; work
order comes from `docs/roadmap/OVERNIGHT-QUEUE.md`.

- **Guard hook** (`.claude/hooks/guard.sh`): armed only while
  `.claude/overnight-mode` exists. While armed it denies
  ssh/scp/rsync/sudo/systemctl/`git push`, recursive `rm` outside
  `/tmp`, and destructive tree-wide git (`reset --hard`, `clean`,
  `checkout .`/`restore .`, `stash`). If a tool call is denied with an
  "overnight guard" message, that flag is armed — `rm
  .claude/overnight-mode` disarms it (a morning task, not yours).
- **Postedit hook** (`.claude/hooks/postedit.sh`): auto-runs
  `ruff check --fix` (F401 excluded) + `ruff format` on every Python
  edit and feeds remaining diagnostics back. Don't fight it — fix the
  diagnostics.
- **Subagents**: `implementer` (writes one roadmap task to a green
  gate; never commits) and `verifier` (adversarial per-task check;
  write-less). Keep them separate — the orchestrator owns commits,
  checkboxes, and blocked calls.

## Drive sessions (`/drive`)

Autonomous sessions against the live Pi while the operator drives run as
`/loop /drive` followed by that drive's OUTCOME and DRIVE FACTS. The
standing protocol (reaching the Pi, first-wake-up checks, health line,
capture mechanics, hard rules, wrap-up) is `.claude/commands/drive.md`;
the experiment itself is the newest `data/analyses/*-deploy-plan.md`. When
a drive teaches something durable about running drives, edit `drive.md` —
don't grow the pasted prompt.
