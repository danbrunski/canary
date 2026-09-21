# Archived roadmap documents

Finished plans. Every task in these is complete; they are kept for the
reasoning behind what they built, not as live work. Code docstrings that cite a
phase of one of these point here.

| document | what it built | closed |
|---|---|---|
| [`dashboard-ux.md`](dashboard-ux.md) | surfaced built-but-invisible backend: replay banner, `vehicle_state` badge, Sessions panel, job results | 2026-07 |
| [`mcp-and-power-hardening.md`](mcp-and-power-hardening.md) | MCP session hardening and the UPS power path, all four phases deployed | 2026-07-19 |
| [`tuning-analysis-toolkit.md`](tuning-analysis-toolkit.md) | `read_channels` / `read_aligned`, delivery error, clock alignment, tune-revision registry, tuning digest — 18 tasks, 5 phases | 2026-08-05 |
| [`data-collection-plan.md`](data-collection-plan.md) | the acquisition plan from "the rig works" to a trainable corpus — 13 items | 2026-09 |
| [`refactoring.md`](refactoring.md) | 24 of 25 dedup/structure tasks; 2.2 continues as D03.23 | 2026-07-20 |
| [`multi-pid-polling.md`](multi-pid-polling.md) | batched Mode 01 polling, Phases 1–2; verification leftovers continue as D03.24 | 2026-07-26 |
| [`ml-correctness-hardening.md`](ml-correctness-hardening.md) | 39 ML-correctness tasks; the retrain continues as D03.25 | 2026-07-19 |
| [`eval-longitudinal-and-dtc.md`](eval-longitudinal-and-dtc.md) | evaluation benchmarks, longitudinal trends, DTC handling — 42 tasks; ledger backfill continues as D03.26 | 2026-07-20 |

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

The rule used for the second batch (2026-09-21): a document with a small live
remainder is archived once that remainder has a home. Each carried item lives
in [`../tasks/D03-open-work.md`](../tasks/D03-open-work.md) under *Carried
from archived roadmaps*, names the document it came from, and is marked in
the archived document at the point it left. Unticked boxes that are standing
acceptance criteria for every phase — not tasks — were left as they are and
say so in the document's banner.
