---
description: Arm the overnight guard and load the standing rules for unattended roadmap implementation. Run this, then set the condition with /goal.
---

You are starting an unattended overnight implementation run against the
roadmap docs. Do the following now, then wait for the operator's /goal.

**First action:** `touch .claude/overnight-mode` — this arms the
PreToolUse guard (no ssh/scp/sudo/systemctl/push, no touching
`docs/roadmap/mcp-and-power-hardening.md`). Tell the operator to
`rm .claude/overnight-mode` in the morning to disarm it.

## Standing rules for the run

**Task order:** work `docs/roadmap/OVERNIGHT-QUEUE.md` strictly top to
bottom — its order encodes real cross-doc dependencies, its "Pre-made
decisions" section resolves every operator-decision point (do not
re-litigate them), and its blocked-overnight rules say what to skip.
**Task-level completion truth stays in each roadmap doc's own
checkboxes**; check a queue entry only when its source-doc phase is
fully checked or annotated blocked. Resume wherever the queue left off
— never restart from the top of a completed entry.

**Per-task protocol:**
1. Restate the task and its acceptance in one line.
2. Implement it. Repo conventions: `.venv/bin/python` always; match
   surrounding style; no new dependencies without logging it in the
   overnight log.
3. Run the whole-repo gate (`ruff check . && ruff format --check . &&
   mypy src && pytest`) plus `pnpm test` in `src/dashboard/static` for
   FE changes (re-run `TuningFeedback.test.ts` once on flake before
   treating it as a failure).
4. Spawn the `verifier` agent with the task text + diff. Fix what it
   confirms; re-verify. Two consecutive FAILs on the same task → mark
   it blocked and move on.
5. Check the box in the roadmap doc (`[x]`, one-line note of what
   landed), or annotate `[~] blocked-overnight: <reason>`.
6. Commit — one commit per task, gate green, message references the
   roadmap task id. **Never push.**
7. Append one line to `docs/roadmap/OVERNIGHT-LOG.md`:
   `<task id> — done|blocked — <one-line summary>`.

**Hard blocks (mark blocked-overnight, never attempt):** anything
needing the truck, the Pi, live-daemon MCP write tools, hardware,
model *deployment*, or an operator decision the docs flag as such.
Laptop-side model *training* is allowed if a task requires it, but cap
any single training run at ~30 minutes and prefer reduced-epoch smoke
configs for verification.

**Retrain rule:** if a task changes featurization/segmentation
semantics (ML-hardening Phase 1 does), finish the whole phase's code +
tests, then mark the retrain/deploy task (1.6) blocked-overnight — the
operator retrains and replays in the morning.

**On any unrecoverable confusion** (merge conflict with the other
session's work, corrupted state): stop touching that task, log it,
move to the next.

End every turn by showing: the gate result, `git status --short`, and
the current checkbox state of the active phase (grep the doc) — the
/goal evaluator reads the transcript, so surface the evidence it needs.
