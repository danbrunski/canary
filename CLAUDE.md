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
- **Subagents**: `implementer` (one checklist file per dispatch;
  targeted tests; records its own `[x]`/`[~]` evidence; marks
  truck/Pi/operator-decision items partial rather than blocking; never
  commits) and `verifier` (adversarial per-checklist check; write-less).
  Keep them separate — the orchestrator owns the full gate, commits,
  and blocked calls, and names each running implementer's area (and
  any shared file it may only append to) to the others when it runs
  them in parallel.

### Dispatch tiers

Pass `model` and `effort` on every `implementer` dispatch, from this
table; the frontmatter default (`sonnet` / `high`) is Standard. Pick the
lowest tier that fits; between two, take the higher.

| Tier | `model` | `effort` | Use for |
|---|---|---|---|
| Light | `haiku` | `low` | Mechanical edits the prompt specifies down to file, symbol and before/after: doc and checklist corrections, moves and renames with no behaviour change, splitting a file, appending entries the prompt spells out. No new logic. |
| Standard | `sonnet` | `high` | The usual desk checklist with a worked design: a module + tests, an endpoint + MCP tool + docs, a dashboard panel, threading a config field through. |
| Heavy | `sonnet` | `xhigh` | Wide changes: daemon + dashboard + MCP together, ~10+ files, layering moves (`tests/test_layering.py`), test infrastructure and the push gate. |
| Deep | `opus` | `high` | Measurement and inference: signal comparison and decode/binding, detector, model and preprocessing code (`scripts/ml_paths.txt`), physics-derived channels, UDS/ISO-TP and anything near the non-volatile-write conditions; diagnosing a failure nobody has explained. |

- **Escalate, don't repeat.** A dispatch that returns BLOCKED on
  difficulty, is cut off at `maxTurns`, or fails the verifier on a
  misunderstanding (not a slip) goes out again one tier up (from Deep:
  `opus` / `xhigh`), with what the first attempt learned. Never re-send
  the same prompt at the same tier.
- **Haiku only with a pre-worked prompt.** Never for work that needs
  judgement, and never for decode parameters or catalog values.
- **The `verifier` runs at the implementer's tier, never lower**, and a
  Light dispatch is verified at Standard.
- **Split a big checklist.** `maxTurns` is 250 (past runs: 21–211,
  median ~90). A checklist that looks like more than ~8 code items, or
  one that spans backend and dashboard, goes out as named item subsets.

## Drive sessions (`/drive`)

Autonomous sessions against the live Pi while the operator drives run as
`/loop /drive` followed by that drive's OUTCOME and DRIVE FACTS. The
standing protocol (reaching the Pi, first-wake-up checks, health line,
capture mechanics, hard rules, wrap-up) is `.claude/commands/drive.md`;
the experiment itself is the newest `data/analyses/*-deploy-plan.md`. When
a drive teaches something durable about running drives, edit `drive.md` —
don't grow the pasted prompt.
