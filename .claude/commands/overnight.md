---
description: Arm the overnight guard and load the standing rules for unattended roadmap implementation. Run this, then set the condition with /goal.
---

You are starting an unattended overnight implementation run against the
roadmap docs. Do the following now, then wait for the operator's /goal.

**First action:** `touch .claude/overnight-mode` — this arms the
PreToolUse guard (no ssh/scp/sudo/systemctl/push, no destructive
rm/git). Tell the operator to `rm .claude/overnight-mode` in the
morning to disarm it.

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

You are the **orchestrator**: a thin loop that selects a checklist,
dispatches it to the `implementer`, gets it independently checked by the
`verifier`, and disposes of the result (full gate, commit, log). You keep
all judgment — ordering, blocked calls, conflict handling, commits — and
push the heavy implementation churn into the `implementer` subagent so
your own context survives the whole queue.

**Unit of work is a checklist file** (`docs/roadmap/tasks/<id>.md`), or a
named subset of its items when the file mixes desk work with truck work.
One commit per checklist file, so the implementer's report and the
commit line up.

**Per-checklist protocol:**
1. Restate the checklist and which items are in scope in one line.
   Items the docs mark as needing the truck, the Pi, deployment, or an
   unresolved operator decision are still in scope: the implementer
   finishes the rest and marks them `[~]`.
2. Dispatch the `implementer` with the checklist text, the item scope,
   and **the areas any other implementer you have running owns** (files
   or modules). It writes the code + tests, runs a targeted gate,
   records `[x]`/`[~]` evidence in the checklist itself, and returns
   `IMPLEMENTED` or `BLOCKED: <reason>` with the structured report
   (Files / Checkbox state / Gate / Behaviour changes / Operator
   decisions needed / Unverified observations). On BLOCKED, treat it
   like a blocked checklist (step 6). **Echo the diff-stat, checkbox
   state and any operator decisions into your own transcript** — the
   /goal evaluator and the verifier both read from here.
   - **Concurrency.** Implementers may run in parallel when their
     checklists touch disjoint areas — name each one's area to the
     others, and never run two on the same file. A file with hunks from
     two agents is staged by hunk, not whole. Serial is the default
     when you cannot name a clean partition.
   - **Operator decisions.** An item that hinges on one goes `[~]`
     with the options recorded; note it for the morning and never
     resolve it yourself. Pre-made decisions in the queue are not
     open.
3. Run the full gate yourself, once per checklist, in the main session:
   `ruff check . && ruff format --check . && mypy src` and bare
   `.venv/bin/python -m pytest` (the whole commit gate, `-n auto`;
   ~5–7 min), **plus** `pnpm test` and `pnpm typecheck` in
   `src/dashboard/static` for FE changes. Do not run it while another
   implementer's tests are running — the cadence/daemon tests flake
   under load; a failure that passes when re-run serially (`-n0`, by
   path) is a load flake, and a failure that does not is a defect. Tests
   write into `models/` manifests and `data/analyses/`; restore those
   with explicit paths. Inspect the diff before proceeding. Vet any
   `NEW DEP:` the report flagged: accept only if the checklist warrants
   it, and log it.
4. Spawn the `verifier` with the checklist text (boxes as the
   implementer left them), the diff **and the list of created
   (untracked) files** — `git diff` alone misses new files, which are
   often the main artifact. It is a distinct, deliberately write-less
   agent so it cannot fix-and-pass its own target — keep it separate
   from the implementer. Fix what it confirms (dispatch a follow-up to
   the implementer, or edit directly for a one-liner); re-verify. Two
   consecutive FAILs on the same checklist → mark the affected items
   blocked and move on.
   - **If the verifier call errors or stalls mid-stream** ("Response
     stalled mid-stream", terminal API error, no verdict returned):
     resume it via `SendMessage` to the returned agentId asking it to
     finish the check. If no agentId came back (the agent never
     started), redispatch a fresh `verifier` with the same input. Do
     not treat a transport failure as a PASS or a FAIL, and do not skip
     verification — a stalled call is an infra hiccup, not a verdict.
5. Review the checklist's boxes: the implementer wrote the evidence;
   you confirm each `[x]` has a test name, command, or number behind
   it and each `[~]` names its remainder. Annotate
   `[~] blocked-overnight: <reason>` for anything the verifier or the
   gate stopped.
6. Commit — one commit per checklist file, explicit pathspecs only
   (`git commit -- <paths>`; the tree may hold another session's work),
   gate green, message references the checklist id and names any
   behaviour change from the report. **Never push.** If the pre-commit
   hook fails only on files outside your pathspec (another agent's
   unfinished work), re-run `ruff` and `mypy` on your staged files, then
   commit with `--no-verify` and say so in the log line.
7. Append one line to `docs/roadmap/OVERNIGHT-LOG.md`:
   `<checklist id> — done|partial|blocked — <one-line summary>`.

**Run-level sweep (once per /goal):** the per-checklist gate above is
already the full commit gate, so the end-of-run sweep is one more bare
`.venv/bin/python -m pytest` on a quiet machine (no implementer running)
plus `pnpm test` / `pnpm typecheck`, as the run's done-condition. If it
comes back red: the failing test's area usually points to the culprit
checklist — fix it within the rules if you can, else annotate that item
`[~] blocked-overnight: <reason>`, and log it in `OVERNIGHT-LOG.md` for
morning review. A clean sweep is required before declaring the run done.

**Hard blocks (never attempt):** anything needing the truck, the Pi,
live-daemon MCP write tools, hardware, model *deployment*, or an
operator decision the docs flag as such. A single such item goes `[~]`
with the remainder named and the rest of its checklist still lands; a
checklist made only of such items is skipped and logged.
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
the current checkbox state of the active checklist (grep the doc) — the
/goal evaluator reads the transcript, so surface the evidence it needs.
