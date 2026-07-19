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

**Reading the roadmap docs:** `file:line` anchors in task text date
from the 2026-07-18 review and drift as work lands — locate code by
the cited symbol/function name (Grep), treating line numbers as hints
only. When a task says a decision was made ("operator-approved", "see
OVERNIGHT-QUEUE.md"), implement that option — never re-open the
choice.

You are the **orchestrator**: a thin loop that selects a task, dispatches
it to the `implementer`, gets it independently checked by the `verifier`,
and disposes of the result (commit, checkbox, log). You keep all judgment
— task ordering, blocked calls, conflict handling, commits — and push the
heavy implementation churn into the `implementer` subagent so your own
context survives the whole queue.

**Per-task protocol:**
1. Restate the task and its acceptance in one line.
2. Dispatch the `implementer` agent with the task text + acceptance.
   It writes the code + tests, drives the whole-repo gate to green, and
   returns `IMPLEMENTED` (diff-stat + changelog + gate output) or
   `BLOCKED: <reason>`. On BLOCKED, treat it like a blocked task
   (step 5). **Echo the returned diff-stat and changelog into your own
   transcript** — the /goal evaluator and the verifier both read from
   here, so the evidence must land in the main thread, not stay in the
   subagent. Run implementers strictly one at a time: they share one
   working tree and the other session may be editing it, so concurrent
   implementers will conflict.
3. Re-confirm the gate yourself in the main session (`ruff check . &&
   ruff format --check . && mypy src && pytest`, plus `pnpm test` and
   `pnpm typecheck` in `src/dashboard/static` for FE changes; re-run
   `TuningFeedback.test.ts` once on flake). The implementer claims
   green — verify it is, and inspect its diff before proceeding. Vet any
   `NEW DEP:` it flagged: accept only if the task warrants it, and log
   it in the overnight log.
4. Spawn the `verifier` agent with the task text + diff. It is a
   distinct, deliberately write-less agent so it cannot fix-and-pass its
   own target — keep it separate from the implementer. Fix what it
   confirms (dispatch a follow-up to the implementer, or edit directly
   for a one-liner); re-verify. Two consecutive FAILs on the same task →
   mark it blocked and move on.
   - **If the verifier call errors or stalls mid-stream** ("Response
     stalled mid-stream", terminal API error, no verdict returned):
     resume it via `SendMessage` to the returned agentId asking it to
     finish the check. If no agentId came back (the agent never
     started), redispatch a fresh `verifier` with the same task text +
     diff. Do not treat a transport failure as a PASS or a FAIL, and
     do not skip verification — a stalled call is an infra hiccup, not
     a verdict.
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
