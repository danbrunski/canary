"""The brain job store: a job record, its identity and the append-only log (E04.4, R29.13).

Split out of :mod:`src.control.brain_jobs`, which still exports every name here.
A job's id is its kind plus canonical parameters (:func:`job_id_for`); every
transition is one line appended to ``jobs.jsonl`` under the vehicle's brain
directory, and the latest line per id is the job. The per-path lock table and
the incremental read index are defined here and nowhere else, so every importer
shares one of each.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from src.config import CanaryConfig
from src.control.brain_state import brain_dir
from src.control.status_error import StatusError
from src.jsonl_store import TailIndex, append_jsonl_line_locked, path_lock

JobKind = Literal[
    "twin_session_evaluation",
    "memory_consolidation",
    "candidate_evaluation",
    "candidate_promotion",
]
JobState = Literal["queued", "running", "deferred", "pending", "completed", "failed"]
JOB_KINDS: tuple[str, ...] = (
    "twin_session_evaluation",
    "memory_consolidation",
    "candidate_evaluation",
    "candidate_promotion",
)
JOB_STATES: tuple[str, ...] = ("queued", "running", "deferred", "pending", "completed", "failed")
#: States the scheduler picks work from, oldest first.
_RUNNABLE = ("queued", "deferred", "pending")

JOBS_FILE = "jobs.jsonl"
RESULTS_DIR = "results"


class BrainJobRefusedError(StatusError[dict[str, Any]]):
    """A submission refused before anything was queued. ``status`` is the HTTP
    code the route answers with; ``detail`` its JSON body."""


def _now() -> datetime:
    return datetime.now(UTC)


def job_id_for(kind: str, params: dict[str, Any]) -> str:
    """The job's identity: its kind and canonical parameters, nothing else."""
    text = json.dumps({"kind": kind, "params": params}, sort_keys=True, separators=(",", ":"))
    return "bj-" + hashlib.sha256(text.encode()).hexdigest()[:20]


@dataclass(frozen=True)
class BrainJob:
    job_id: str
    kind: str
    params: dict[str, Any]
    state: JobState
    created_at: str
    updated_at: str
    attempts: int = 0
    #: Why the job is deferred, pending or failed (a named reason).
    reason: str | None = None
    detail: str | None = None
    #: Persisted progress: the last stage reached.
    progress: dict[str, Any] = field(default_factory=dict)
    #: Small completion summary (evidence ids, result file); the full result
    #: is the file under ``results/``.
    result: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrainJob:
        state = str(data["state"])
        if state not in JOB_STATES:
            raise ValueError(f"unknown brain job state {state!r}")
        return cls(
            job_id=str(data["job_id"]),
            kind=str(data["kind"]),
            params=dict(data.get("params") or {}),
            state=cast(JobState, state),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            attempts=int(data.get("attempts", 0)),
            reason=data.get("reason"),
            detail=data.get("detail"),
            progress=dict(data.get("progress") or {}),
            result=data.get("result"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
        )


@dataclass
class _JobIndex:
    tail: TailIndex = field(default_factory=TailIndex)
    jobs: dict[str, BrainJob] = field(default_factory=dict)


_INDEXES: dict[Path, _JobIndex] = {}


#: Abrupt child deaths (``BrokenProcessPool``) a job survives before it is
#: ``failed``: the first is read as a signal from outside (a service stop, the
#: cgroup's OOM killer), the second as the job itself killing its child.
MAX_BREAKS = 2


def _breaks(job: BrainJob) -> int:
    value = job.progress.get("breaks", 0)
    return int(value) if isinstance(value, int) else 0


class BrainJobStore:
    """The vehicle's brain jobs: an append-only transition log, latest per id."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / JOBS_FILE
        self.results = self.root / RESULTS_DIR

    @classmethod
    def for_config(cls, config: CanaryConfig) -> BrainJobStore:
        return cls(brain_dir(config))

    # -- reads ------------------------------------------------------------

    def jobs(self) -> dict[str, BrainJob]:
        """Latest state per job. Incremental: each call stats the file and
        parses only the bytes appended since the last read (by any instance in
        this process); a file that shrank or was replaced is re-read whole."""
        with path_lock(self.path):
            index = _INDEXES.setdefault(self.path.resolve(), _JobIndex())
            # Only whole lines; a torn tail is re-read once completed.
            read = index.tail.read_new(self.path)
            if read.reset:
                index.jobs = {}
            for line in read.lines:
                if not line.strip():
                    continue
                try:
                    job = BrainJob.from_dict(json.loads(line))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    # A corrupt line from a power cut: the transition before
                    # it stands, and the job reruns from there.
                    continue
                index.jobs[job.job_id] = job
            return dict(index.jobs)

    def ordered(self) -> list[BrainJob]:
        return sorted(self.jobs().values(), key=lambda job: (job.created_at, job.job_id))

    def get(self, job_id: str) -> BrainJob | None:
        return self.jobs().get(job_id)

    def result_path(self, job_id: str) -> Path:
        return self.results / f"{job_id}.json"

    def read_result(self, job_id: str) -> dict[str, Any] | None:
        path = self.result_path(job_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None

    # -- writes -----------------------------------------------------------

    def _append(self, job: BrainJob) -> BrainJob:
        append_jsonl_line_locked(self.path, job.to_dict())
        return job

    def submit(
        self, kind: str, params: dict[str, Any], *, rerun: bool = False, now: datetime | None = None
    ) -> tuple[BrainJob, bool]:
        """Queue ``kind`` over canonical ``params``; ``(job, created)``.

        The same work already known is returned as it stands — queued, running,
        deferred, pending or completed — not queued again. A failed job, or any
        finished one with ``rerun``, is queued again under the same id."""
        if kind not in JOB_KINDS:
            raise BrainJobRefusedError(
                400, {"error": "unknown_kind", "message": f"unknown brain job {kind!r}"}
            )
        job_id = job_id_for(kind, params)
        stamp = (now or _now()).isoformat()
        with path_lock(self.path):
            existing = self.get(job_id)
            if existing is not None:
                finished = existing.state in ("completed", "failed")
                if not finished or (existing.state == "completed" and not rerun):
                    return existing, False
                return self.transition(
                    existing, "queued", reason=None, detail=None, progress={}, now=now
                ), True
            job = BrainJob(
                job_id=job_id,
                kind=kind,
                params=params,
                state="queued",
                created_at=stamp,
                updated_at=stamp,
            )
            return self._append(job), True

    def transition(
        self, job: BrainJob, state: JobState, *, now: datetime | None = None, **changes: Any
    ) -> BrainJob:
        updated = replace(job, state=state, updated_at=(now or _now()).isoformat(), **changes)
        return self._append(updated)

    def recover(self, *, now: datetime | None = None) -> list[BrainJob]:
        """Reopen after a restart: a job left ``running`` is deferred, named.

        So is a ``failed: child_failed`` job that has not had its one automatic
        retry: a stop signals the whole cgroup, so the child can die (and be
        recorded broken) before the scheduler sees the shutdown. Written before
        that rule existed, such a record carries no break count."""
        recovered = []
        for job in self.ordered():
            if job.state == "failed" and job.reason == "child_failed" and _breaks(job) < MAX_BREAKS:
                recovered.append(
                    self.transition(
                        job,
                        "deferred",
                        reason="child_interrupted",
                        detail="the child was killed abruptly (a stop or restart signals the "
                        "whole service); it reruns once in the next idle window",
                        progress={**job.progress, "breaks": _breaks(job) + 1},
                        finished_at=None,
                        now=now,
                    )
                )
                continue
            if job.state == "running":
                recovered.append(
                    self.transition(
                        job,
                        "deferred",
                        reason="interrupted_by_restart",
                        detail="the daemon stopped while the job ran; it reruns in the next "
                        "idle window and its evidence is not duplicated",
                        now=now,
                    )
                )
        return recovered
