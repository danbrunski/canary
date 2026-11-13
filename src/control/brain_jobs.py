"""The second brain's heavier work, run on the Pi while collection is inactive (E04.4).

Session evaluation and memory consolidation read whole sessions or the whole
ledger, which is the same kind of work as the trip-end digest. They run the
same way (AGENTS.md, "No analysis on the Pi while it collects"):

1. **Only while collection is inactive.** :func:`collection_activity` names why
   not: a capture, a replay, or any verdict but ``no_data`` from the live
   vehicle-state classifier. A parked truck whose bus still talks is
   collecting; "parked" is never read as "idle". Unknown (no live source) is
   treated as collecting.
2. **One child, the digest's.** The job runs in the digest's single-use
   ``spawn`` child under its ``RLIMIT_AS`` ceiling, never beside a digest or a
   workbench analysis: the scheduler waits while either is in flight and
   **yields** — terminates its child and defers the job — within
   ``poll_seconds`` of a capture, collection, digest or workbench request
   starting.
3. **Durable, identified jobs.** A job's id is its kind plus canonical
   parameters (:func:`job_id_for`), so submitting the same work twice is one
   job. Every transition is appended to ``jobs.jsonl`` under the vehicle's
   brain directory (:func:`src.control.brain_state.brain_dir`, under
   ``state_dir``). A job running when the daemon stopped reopens ``deferred``
   (``interrupted_by_restart``) and runs again in the next idle window; its
   evidence is not duplicated, because an identical record appended twice is
   one record on the ledger.
4. **Pending is not failed.** Work whose inputs are not complete yet — a
   session still open (no trip-end digest and no newer session) — stays
   ``pending`` and is re-checked every idle window; it is never forced through.

States: ``queued`` → ``running`` → ``completed`` | ``failed``, with
``deferred`` (blocked or yielded; the reason names which) and ``pending``

**Layout** (R29.13). This module is the facade: every name it exported before
the split is still importable from here. The job record, its identity and the
append-only log (with the one lock table and read index) are
:mod:`src.control.brain_jobs_store`; submission validation, the kind runners the
child executes and :func:`precheck` are :mod:`src.control.brain_jobs_runners`;
:func:`collection_activity` and :class:`BrainScheduler` are
:mod:`src.control.brain_jobs_scheduler`. A test that patches what the scheduler
looks up (``any_digest_in_flight``, ``any_workbench_job_in_flight``,
``precheck``) patches that module, where the name is read at call time.
"""

from __future__ import annotations

from src.control.brain_jobs_runners import (
    Precheck,
    candidate_store,
    canonical_params,
    enqueue_after_digest,
    precheck,
    run_brain_job,
    twin_command_recorded,
)
from src.control.brain_jobs_scheduler import MIN_TICK_SECONDS, BrainScheduler, collection_activity
from src.control.brain_jobs_store import (
    _INDEXES,
    JOB_KINDS,
    JOB_STATES,
    JOBS_FILE,
    MAX_BREAKS,
    RESULTS_DIR,
    BrainJob,
    BrainJobRefusedError,
    BrainJobStore,
    JobKind,
    JobState,
    job_id_for,
)

__all__ = [
    "JOBS_FILE",
    "JOB_KINDS",
    "JOB_STATES",
    "MAX_BREAKS",
    "MIN_TICK_SECONDS",
    "RESULTS_DIR",
    "_INDEXES",
    "BrainJob",
    "BrainJobRefusedError",
    "BrainJobStore",
    "BrainScheduler",
    "JobKind",
    "JobState",
    "Precheck",
    "canonical_params",
    "candidate_store",
    "collection_activity",
    "enqueue_after_digest",
    "job_id_for",
    "precheck",
    "run_brain_job",
    "twin_command_recorded",
]
