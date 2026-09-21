# Archived roadmap documents

Finished plans. Every task in these is complete; they are kept for the
reasoning behind what they built, not as live work. Code docstrings that cite a
phase of one of these point here.

| document | what it built | closed |
|---|---|---|
| [`dashboard-ux.md`](dashboard-ux.md) | surfaced built-but-invisible backend: replay banner, `vehicle_state` badge, Sessions panel, job results | 2026-07 |
| [`mcp-and-power-hardening.md`](mcp-and-power-hardening.md) | MCP session hardening and the UPS power path, all four phases deployed | 2026-07-19 |
| [`tuning-analysis-toolkit.md`](tuning-analysis-toolkit.md) | `read_channels` / `read_aligned`, delivery error, clock alignment, tune-revision registry, tuning digest — 18 tasks, 5 phases | 2026-08-05 |

## What did not move, and why

A document with every box ticked is not automatically finished:

- `OVERNIGHT-QUEUE.md` / `OVERNIGHT-LOG.md` — an empty queue is not an obsolete
  one. AGENTS.md names it as the work order and as the home of pre-made
  decisions that must never be re-opened, and `/overnight` reads it.
- `llm-assist-and-compute-roadmap.md` — explicitly future and optional; its
  four ticked boxes were only the first sequencing step.
- `online-training-strategy.md` — design rationale that thirty source files
  cite. It is an architecture document filed under roadmap, not a finished
  plan.

Four more are one small decision from archivable — `refactoring.md` (item 2.2
awaits sign-off), `multi-pid-polling.md`, `ml-correctness-hardening.md` and
`eval-longitudinal-and-dtc.md` — and `data-collection-plan.md` is complete but
is linked from the top-level README as an entry point, so it needs a new link
target rather than just a move.
