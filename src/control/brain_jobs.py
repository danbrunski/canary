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
(waiting for inputs) in between. Nothing here needs a remote host.
"""

from __future__ import annotations

import asyncio
import fcntl
import functools
import hashlib
import json
import logging
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import BrokenExecutor, Executor
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from src.analysis.candidate_promotion import (
    CandidateDrive,
    PromotionRules,
    evaluate_candidate,
    load_independent_drives,
    promote,
)
from src.analysis.change_evaluation import CHANGE_RESIDUALS_FILE, load_change_residuals
from src.analysis.digest import digest_address_space_bytes, digest_row_budget
from src.analysis.target_phi import (
    TWIN_FILE,
    load_twin_definition,
)
from src.analysis.twin_evidence import TwinSessionIdentity, evaluate_and_mint
from src.atomic_write import atomic_write_text
from src.collection import tuning_profile
from src.config import CanaryConfig
from src.control import child_pool
from src.control.brain_state import brain_dir, reconcile_identity
from src.control.trip_end import (
    DIGEST_TIMEOUT_SECONDS,
    any_digest_in_flight,
    limit_address_space,
)
from src.control.twin_identity import TwinResolver, session_last_read
from src.control.workbench_jobs import any_workbench_job_in_flight
from src.evidence.ledger import memory_ledger_for
from src.evidence.records import VehicleIdentity, recurrence_key
from src.models.candidate_store import CANDIDATES_DIRNAME, CandidateStore, CandidateStoreError
from src.storage.capture_manifest import read_capture_manifest
from src.storage.session_files import count_session_rows
from src.storage.session_products import read_digest, read_digest_skipped
from src.storage.sessions import (
    latest_session,
    recorded_sessions,
    resolve_session,
    session_started_at,
)
from src.vehicle.profiles import active_registry, profile_dir
from src.vehicle_state import (
    QUIET_STATES,
    ReplayStatusSource,
    derive_from_live_source,
    running_replay_id,
)

log = logging.getLogger(__name__)

_T = TypeVar("_T")

__all__ = [
    "JOBS_FILE",
    "JOB_KINDS",
    "JOB_STATES",
    "RESULTS_DIR",
    "BrainJob",
    "BrainJobRefusedError",
    "BrainJobStore",
    "BrainScheduler",
    "collection_activity",
    "enqueue_after_digest",
    "job_id_for",
    "run_brain_job",
]

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


class BrainJobRefusedError(Exception):
    """A submission refused before anything was queued. ``status`` is the HTTP
    code the route answers with; ``detail`` its JSON body."""

    def __init__(self, status: int, detail: dict[str, Any]) -> None:
        super().__init__(str(detail.get("message", detail)))
        self.status = status
        self.detail = detail


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


_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path.resolve(), threading.RLock())


@dataclass
class _JobIndex:
    identity: tuple[int, int] | None = None
    offset: int = 0
    jobs: dict[str, BrainJob] = field(default_factory=dict)

    def reset(self) -> None:
        self.identity = None
        self.offset = 0
        self.jobs = {}


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
        with _lock_for(self.path):
            index = _INDEXES.setdefault(self.path.resolve(), _JobIndex())
            try:
                stat = self.path.stat()
            except FileNotFoundError:
                index.reset()
                return {}
            if (stat.st_ino, stat.st_dev) != index.identity or stat.st_size < index.offset:
                index.reset()
                index.identity = (stat.st_ino, stat.st_dev)
            if stat.st_size > index.offset:
                with self.path.open("rb") as handle:
                    handle.seek(index.offset)
                    chunk = handle.read(stat.st_size - index.offset)
                # Only whole lines; a torn tail is re-read once completed.
                end = chunk.rfind(b"\n") + 1
                for line in chunk[:end].splitlines():
                    if not line.strip():
                        continue
                    try:
                        job = BrainJob.from_dict(json.loads(line))
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        # A corrupt line from a power cut: the transition before
                        # it stands, and the job reruns from there.
                        continue
                    index.jobs[job.job_id] = job
                index.offset += end
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
        self.root.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(job.to_dict(), sort_keys=True, default=str) + "\n").encode()
        with _lock_for(self.path), self.path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0, 2)
            if handle.tell():
                handle.seek(-1, 2)
                if handle.read(1) != b"\n":
                    # A torn tail (power cut mid-line) must not swallow this line.
                    line = b"\n" + line
            handle.write(line)
            handle.flush()
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
        with _lock_for(self.path):
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


# -- submission ---------------------------------------------------------------


def canonical_params(config: CanaryConfig, kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validate and canonicalise a submission; refuse by name."""
    if kind == "memory_consolidation":
        return {}
    if kind in ("candidate_evaluation", "candidate_promotion"):
        return _candidate_params(config, kind, params)
    if kind == "twin_session_evaluation":
        name = params.get("session")
        if not isinstance(name, str) or not name:
            raise BrainJobRefusedError(
                400, {"error": "session_required", "message": "name the session to evaluate"}
            )
        try:
            session = resolve_session(config.collection.storage_path, name)
        except FileNotFoundError as exc:
            raise BrainJobRefusedError(
                404, {"error": "session_not_found", "message": str(exc)}
            ) from exc
        if not (profile_dir(config) / TWIN_FILE).exists():
            raise BrainJobRefusedError(
                400,
                {
                    "error": "no_twin_definition",
                    "message": f"this vehicle's profile has no {TWIN_FILE}",
                },
            )
        return {"session": session.name}
    raise BrainJobRefusedError(
        400, {"error": "unknown_kind", "message": f"unknown brain job {kind!r}"}
    )


# -- candidate evaluation and promotion (E03.6) ----------------------------------


def candidate_store(config: CanaryConfig) -> CandidateStore:
    """The vehicle's references, candidate checkpoints and decisions (state_dir)."""
    return CandidateStore(brain_dir(config) / CANDIDATES_DIRNAME)


def _candidate_params(config: CanaryConfig, kind: str, params: dict[str, Any]) -> dict[str, Any]:
    residual = params.get("residual")
    if not isinstance(residual, str) or not residual:
        raise BrainJobRefusedError(
            400, {"error": "residual_required", "message": "name the change residual"}
        )
    path = profile_dir(config) / CHANGE_RESIDUALS_FILE
    if not path.exists():
        raise BrainJobRefusedError(
            400,
            {
                "error": "no_change_residuals",
                "message": f"this profile has no {CHANGE_RESIDUALS_FILE}",
            },
        )
    try:
        load_change_residuals(path)[0].residual(residual)
    except KeyError as exc:
        raise BrainJobRefusedError(404, {"error": "unknown_residual", "message": str(exc)}) from exc
    store = candidate_store(config)
    if kind == "candidate_evaluation":
        checkpoint = params.get("checkpoint")
        try:
            store.checkpoint(residual, str(checkpoint))
        except CandidateStoreError as exc:
            raise BrainJobRefusedError(
                404, {"error": "checkpoint_not_found", "message": str(exc)}
            ) from exc
        return {"residual": residual, "checkpoint": str(checkpoint)}
    # Promotion is the explicit, separate step (B03: never automatic): it is
    # accepted only for a decision the evaluation recorded as qualified.
    key = params.get("decision")
    found = [d for d in store.decisions(residual) if d["key"] == key]
    if not found:
        raise BrainJobRefusedError(
            404, {"error": "decision_not_found", "message": f"no decision {key!r} for {residual}"}
        )
    if found[0]["status"] != "qualified":
        raise BrainJobRefusedError(
            409,
            {
                "error": "not_qualified",
                "message": f"decision is {found[0]['status']}: {'; '.join(found[0]['reasons'])}",
            },
        )
    return {"residual": residual, "decision": str(key)}


def _candidate_context(
    config: CanaryConfig, residual: str
) -> tuple[CandidateStore, Any, VehicleIdentity]:
    definition = load_change_residuals(profile_dir(config) / CHANGE_RESIDUALS_FILE)[0].residual(
        residual
    )
    store = candidate_store(config)
    reference = store.active(residual)
    assert config.scoped_profile is not None  # brain_dir refused an unscoped config
    vehicle = VehicleIdentity(
        profile=config.scoped_profile,
        vin=config.vehicle.vin or None,
        program_id=None,
        calibration=None if reference is None else reference.calibration,
    )
    return store, definition, vehicle


def _run_candidate_evaluation(
    config: CanaryConfig, params: dict[str, Any], job_id: str
) -> dict[str, Any]:
    residual = str(params["residual"])
    store, definition, vehicle = _candidate_context(config, residual)
    reference = store.active(residual)
    checkpoint, _spec = store.checkpoint(residual, str(params["checkpoint"]))
    excluded: dict[str, str] = {}
    drives: list[CandidateDrive] = []
    if reference is not None and checkpoint.valid_to is not None:
        drives, excluded = load_independent_drives(
            config.collection.storage_path,
            definition,
            store.policy(residual, reference.fingerprint),
            registry=active_registry(config),
            calibration=reference.calibration,
            after=checkpoint.valid_to,
        )
    decision = evaluate_candidate(
        memory_ledger_for(config),
        store,
        residual,
        checkpoint.fingerprint,
        drives,
        channels=definition.channels,
        vehicle=vehicle,
        rules=PromotionRules(),
    )
    result: dict[str, Any] = {
        "job_id": job_id,
        "kind": "candidate_evaluation",
        "computed_at": _now().isoformat(),
        "decision": decision.as_dict(),
        "decision_key": decision.key,
        "excluded_sessions": excluded,
        "evidence": [o["evidence"] for o in decision.outcomes.values()],
        "sessions_seen": _sessions_after(config, checkpoint.valid_to),
    }
    if decision.status == "pending":
        result["pending"] = {
            "reason": decision.reasons[0].split(":")[0] if decision.reasons else "pending",
            "detail": "; ".join(decision.reasons),
        }
    return result


def _run_candidate_promotion(
    config: CanaryConfig, params: dict[str, Any], job_id: str
) -> dict[str, Any]:
    residual = str(params["residual"])
    store, definition, vehicle = _candidate_context(config, residual)
    outcome = promote(
        memory_ledger_for(config),
        store,
        residual,
        str(params["decision"]),
        vehicle=vehicle,
        channels=definition.channels,
    )
    return {
        "job_id": job_id,
        "kind": "candidate_promotion",
        "computed_at": _now().isoformat(),
        "changed": outcome.changed,
        "reference": outcome.reference,
        "previous": outcome.previous,
        "evidence": [outcome.record],
    }


def _sessions_after(config: CanaryConfig, after: datetime | None) -> int:
    """Recorded sessions that started after ``after`` (names only: cheap)."""
    if after is None:
        return 0
    return sum(
        1
        for s in recorded_sessions([config.collection.storage_path])
        if (started := session_started_at(s)) is not None and started > after
    )


def twin_command_recorded(config: CanaryConfig, session: Path) -> bool:
    """Whether ``session``'s capture profile recorded the twin's command word
    (its reference ``mem:`` channel). Read from the capture manifest's profile
    snapshot, never guessed from the profile's name; a plain drive has no
    memory channels and is not the twin's to evaluate. A session holding the
    command but missing an input still evaluates: its evidence names the
    missing input and the capture that would supply it."""
    manifest = read_capture_manifest(session)
    if manifest is None:
        return False
    definition, _sha = load_twin_definition(profile_dir(config))
    reference = definition.reference.channel
    if not reference.startswith("mem:"):
        return False
    raw = manifest.profile_snapshot.get("memory") or ()
    memory = {str(name) for name in raw} if isinstance(raw, list | tuple) else set()
    return reference.removeprefix("mem:") in memory


def enqueue_after_digest(config: CanaryConfig, session: Path) -> BrainJob | None:
    """After a trip-end digest: queue the twin's evaluation of ``session`` when
    its capture recorded the twin's command word; ``None`` otherwise. Blocking
    (manifest and profile reads): call it off the loop."""
    if config.scoped_profile is None or not (profile_dir(config) / TWIN_FILE).exists():
        return None
    if not twin_command_recorded(config, session):
        return None
    params = canonical_params(config, "twin_session_evaluation", {"session": session.name})
    job, _created = BrainJobStore.for_config(config).submit("twin_session_evaluation", params)
    return job


# -- the child ------------------------------------------------------------------


def _run_twin(config: CanaryConfig, params: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Evaluate a session per identity segment (one, unless it spans a flash)
    and per slot, resolved by the same :class:`TwinResolver` the live twin
    uses, so the two paths read the same calibration, program and table."""
    session = resolve_session(config.collection.storage_path, str(params["session"]))
    resolver = TwinResolver(config)
    segments = resolver.segments(session, first_read=None, until=session_last_read(session))
    ledger = memory_ledger_for(config)
    assert config.scoped_profile is not None  # brain_dir refused an unscoped config
    out: dict[str, list[Any]] = {"evidence": [], "evaluations": [], "refusals": []}
    for segment in segments:
        identity = segment.identity
        name = None if len(segments) == 1 else (identity.calibration or "unknown")
        minted = evaluate_and_mint(
            ledger,
            session,
            definition=resolver.definition,
            definition_sha256=resolver.definition_sha256,
            identity=TwinSessionIdentity(
                profile=config.scoped_profile,
                vin=config.vehicle.vin or None,
                calibration=identity.calibration,
                calibration_source=identity.source,
                calibration_conflict=identity.conflict,
            ),
            program_id=identity.program_id,
            table_for_slot=functools.partial(resolver.table, identity),
            window=segment.window(),
            segment=name,
        )
        for key in out:
            out[key].extend(minted[key])
    first = segments[0].identity
    return {
        "job_id": job_id,
        "kind": "twin_session_evaluation",
        "session": session.name,
        "calibration": first.calibration,
        "calibration_source": first.source,
        "calibration_conflict": first.conflict,
        "image_sha256": first.image_sha256,
        "program_id": first.program_id,
        "segments": [segment.to_dict() for segment in segments],
        "computed_at": _now().isoformat(),
        **out,
    }


def _run_consolidation(config: CanaryConfig, job_id: str) -> dict[str, Any]:
    """Read the whole ledger once: counts, open investigations, recurrences.
    Appends nothing; a recomputation, never a second copy of memory."""
    ledger = memory_ledger_for(config)
    entries = ledger.entries() if ledger.path.exists() else []
    current = [e for e in entries if e.current]
    recurrences = Counter(key for e in current if (key := recurrence_key(e.record)) is not None)
    investigations = [
        {"id": e.record.id, "title": getattr(e.record, "title", ""), "status": e.record.status}
        for e in current
        if e.record.kind == "investigation" and getattr(e.record, "status", None) == "open"
    ]
    return {
        "job_id": job_id,
        "kind": "memory_consolidation",
        "computed_at": _now().isoformat(),
        "records": len(entries),
        "current": len(current),
        "by_kind": dict(sorted(Counter(e.record.kind for e in current).items())),
        "superseded": sum(1 for e in entries if not e.current and e.retracted_by is None),
        "retracted": sum(1 for e in entries if e.retracted_by is not None),
        "recurring": {k: n for k, n in sorted(recurrences.items()) if n > 1},
        "open_investigations": investigations,
    }


def run_brain_job(
    kind: str,
    params: dict[str, Any],
    config: CanaryConfig,
    *,
    job_id: str,
    results_dir: Path,
    address_space_bytes: int | None = None,
) -> dict[str, Any]:
    """The child-process entry point: cap, compute, mint, write the result.

    Module-level and picklable (``spawn`` imports it by name). Evidence is
    minted before the result file is written, so a crash between the two is a
    rerun whose evidence the ledger already holds (one record, not two).
    """
    if address_space_bytes is not None:
        limit_address_space(address_space_bytes)
    if kind == "twin_session_evaluation":
        result = _run_twin(config, params, job_id)
    elif kind == "memory_consolidation":
        result = _run_consolidation(config, job_id)
    elif kind == "candidate_evaluation":
        result = _run_candidate_evaluation(config, params, job_id)
    elif kind == "candidate_promotion":
        result = _run_candidate_promotion(config, params, job_id)
    else:
        raise ValueError(f"unknown brain job {kind!r}")
    path = Path(results_dir) / f"{job_id}.json"
    atomic_write_text(path, json.dumps(result, indent=1, sort_keys=True, default=str), mkdir=True)
    summary = {
        "result_file": path.name,
        "evidence": list(result.get("evidence", [])),
        "computed_at": result["computed_at"],
    }
    if "pending" in result:
        # Not enough independent evidence yet (E03.6): the job waits, it does
        # not complete, and the precheck re-runs it only once new drives exist.
        summary["pending"] = result["pending"]
        summary["sessions_seen"] = result.get("sessions_seen")
    return summary


# -- scheduling ---------------------------------------------------------------


def collection_activity(
    *, live_source: Any, replay_controller: ReplayStatusSource | None
) -> str | None:
    """Why heavy work must wait now, or ``None`` when collection is inactive.

    Only a quiet verdict (``QUIET_STATES``: ``parked``, ``bench`` or ``no_data``, neither
    bus fresh) is inactive. ``engine_off``
    (CAN fresh, key out), ``ignition_only``, ``passive`` and ``unknown`` are all
    still collecting: a parked vehicle whose bus talks is being recorded.
    """
    if tuning_profile.is_active():
        return "capture_active"
    if running_replay_id(replay_controller) is not None:
        return "replay_active"
    if live_source is None:
        return "collection_state_unknown"
    state = derive_from_live_source(live_source, replay_controller=replay_controller).state
    return None if state in QUIET_STATES else f"collecting:{state}"


Precheck = tuple[Literal["run", "pending", "failed"], str | None, str | None]


def _session_finished(config: CanaryConfig, session: Path) -> bool:
    """Finished = its trip-end digest ran (or said why not), or a newer session exists."""
    if read_digest(session) is not None or read_digest_skipped(session) is not None:
        return True
    latest = latest_session(config.collection.storage_path)
    return latest is not None and latest.resolve() != session.resolve()


def _candidate_precheck(config: CanaryConfig, job: BrainJob) -> Precheck:
    """Names and pointers only: never reads a session's rows."""
    residual = str(job.params["residual"])
    store = candidate_store(config)
    if job.kind == "candidate_promotion":
        return "run", None, None
    if store.active_fingerprint(residual) is None:
        return (
            "pending",
            "no_frozen_reference",
            f"no reference is frozen for {residual} (E03.3): there is nothing to replace",
        )
    try:
        checkpoint, _spec = store.checkpoint(residual, str(job.params["checkpoint"]))
    except CandidateStoreError as exc:
        return "failed", "checkpoint_not_found", str(exc)
    latest = latest_session(config.collection.storage_path)
    if latest is not None and not _session_finished(config, latest):
        return "pending", "session_open", "the newest session is still open"
    seen = _sessions_after(config, checkpoint.valid_to)
    if job.state == "pending" and job.progress.get("sessions_seen") == seen:
        return (
            "pending",
            job.reason or "held_out_insufficient",
            job.detail,  # unchanged: re-run only once a new drive is recorded
        )
    return "run", None, None


def precheck(config: CanaryConfig, job: BrainJob) -> Precheck:
    """Cheap, in-process: may the job run now, wait, or never?"""
    if job.kind in ("candidate_evaluation", "candidate_promotion"):
        return _candidate_precheck(config, job)
    if job.kind != "twin_session_evaluation":
        return "run", None, None
    try:
        session = resolve_session(config.collection.storage_path, str(job.params["session"]))
    except FileNotFoundError as exc:
        return "failed", "session_not_found", str(exc)
    if not _session_finished(config, session):
        return (
            "pending",
            "session_open",
            "waiting for the session's trip-end digest or a newer session; an open session "
            "is not evaluated as if complete",
        )
    budget = digest_row_budget(config)
    rows = count_session_rows(session)
    if budget and rows is not None and rows > budget:
        return (
            "failed",
            "over_budget",
            f"{rows:,} rows exceeds the {budget:,}-row analysis budget",
        )
    return "run", None, None


#: Shortest scheduler tick: longer than the reaper's grace, so a yielded child
#: is gone (SIGTERM, then SIGKILL) before the next window can start another.
#: ``blocked`` also refuses while the previous child is alive; this keeps the
#: idle poll from spinning on that refusal.
MIN_TICK_SECONDS = child_pool.CHILD_EXIT_BOUND_SECONDS


class BrainScheduler:
    """Runs brain jobs one at a time, only while collection is inactive.

    Nothing that touches the disk runs on the event loop: the job log, the
    precheck (session listing, Parquet footers), identity reconcile and every
    transition go through :func:`asyncio.to_thread`. On the loop stay only the
    in-memory checks (:meth:`blocked`)."""

    def __init__(
        self,
        config: CanaryConfig,
        *,
        activity: Callable[[], str | None],
        store: BrainJobStore | None = None,
        runner: Callable[..., dict[str, Any]] = run_brain_job,
        executor_factory: Callable[[], Executor] | None = None,
        poll_seconds: float = 0.5,
        tick_seconds: float = 15.0,
        timeout: float = DIGEST_TIMEOUT_SECONDS,
        started_at: datetime | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        if tick_seconds <= MIN_TICK_SECONDS:
            raise ValueError(
                f"tick_seconds={tick_seconds} must exceed {MIN_TICK_SECONDS}s, the bound on a "
                "yielded child's exit, or two analysis children could overlap"
            )
        self.config = config
        self.store = store or BrainJobStore.for_config(config)
        self._activity = activity
        self._runner = runner
        self._executor_factory = executor_factory
        self.poll_seconds = poll_seconds
        self.tick_seconds = tick_seconds
        self.timeout = timeout
        self.started_at = started_at or _now()
        self.running: str | None = None
        #: The daemon's stop signal; set, a dead child is a shutdown, not a failure.
        self._stop_event = stop_event

    def blocked(self) -> str | None:
        """Why no brain child may run now. In-memory only: safe on the loop."""
        reason = self._activity()
        if reason is not None:
            return reason
        if any_digest_in_flight():
            return "digest_running"
        if any_workbench_job_in_flight():
            return "workbench_running"
        if self.running is None and child_pool.child_alive(child_pool.BRAIN_CHILD):
            return "brain_child_exiting"
        return None

    def _yield_reason(self) -> str | None:
        """While a job runs: why it must stop (in-memory only)."""
        reason = self._activity()
        if reason is not None:
            return reason
        if any_digest_in_flight():
            return "digest_running"
        if any_workbench_job_in_flight():
            return "workbench_running"
        if child_pool.yield_requested(child_pool.BRAIN_CHILD):
            return "analysis_requested"
        return None

    def recover(self) -> list[BrainJob]:
        """Reopen: defer interrupted jobs and record the identity now in force.
        Blocking; :meth:`run_forever` calls it off the loop."""
        recovered = self.store.recover()
        self._reconcile()
        return recovered

    def _reconcile(self) -> None:
        try:
            reconcile_identity(self.config, write=True)
        except Exception:  # noqa: BLE001 - identity is reported, never fatal
            log.exception("brain identity reconcile failed")

    def _set(self, job: BrainJob, state: JobState, **changes: Any) -> BrainJob:
        """Record a transition, skipping one that changes nothing visible."""
        same = job.state == state and all(getattr(job, k) == v for k, v in changes.items())
        return job if same else self.store.transition(job, state, **changes)

    async def _io(self, call: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        return await asyncio.to_thread(call, *args, **kwargs)

    def _runnable(self) -> list[BrainJob]:
        return [j for j in self.store.ordered() if j.state in _RUNNABLE]

    def _defer_all(self, candidates: list[BrainJob], reason: str) -> None:
        for job in candidates:
            if job.state in ("queued", "deferred"):
                self._set(job, "deferred", reason=reason, detail="waiting for an idle window")

    def _pick(self, candidates: list[BrainJob]) -> BrainJob | None:
        """The first job whose precheck says run; records pending/failed ones."""
        for job in candidates:
            verdict, why, detail = precheck(self.config, job)
            if verdict == "pending":
                self._set(job, "pending", reason=why, detail=detail)
                continue
            if verdict == "failed":
                self._set(job, "failed", reason=why, detail=detail, finished_at=_now().isoformat())
                continue
            return job
        return None

    async def step(self) -> BrainJob | None:
        """Run the oldest runnable job if collection is inactive; return it as
        it ended (completed, failed, deferred), or ``None`` when nothing ran."""
        candidates: list[BrainJob] = await self._io(self._runnable)
        if not candidates:
            return None
        reason = self.blocked()
        if reason is not None:
            await self._io(self._defer_all, candidates, reason)
            return None
        job = await self._io(self._pick, candidates)
        if job is None or self.blocked() is not None:
            return None
        return await self._run(job)

    def _start(self, job: BrainJob) -> BrainJob:
        self._reconcile()
        return self.store.transition(
            job,
            "running",
            attempts=job.attempts + 1,
            reason=None,
            detail=None,
            started_at=_now().isoformat(),
            progress={
                "stage": "child_running",
                "attempt": job.attempts + 1,
                "breaks": _breaks(job),
            },
        )

    def shutting_down(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    def _broken(self, job: BrainJob, exc: BaseException) -> BrainJob:
        """The child died abruptly. During shutdown that is the shutdown; the
        first time otherwise it is an outside signal and the job reruns; only a
        second break in a row is the job's own failure."""
        detail = f"{type(exc).__name__}: {exc}"
        if self.shutting_down():
            return self.store.transition(
                job,
                "deferred",
                reason="daemon_shutdown",
                detail=f"the child died as the daemon stopped ({detail}); it reruns after restart",
                progress={**job.progress, "stage": "interrupted"},
            )
        breaks = _breaks(job) + 1
        if breaks < MAX_BREAKS:
            return self.store.transition(
                job,
                "deferred",
                reason="child_interrupted",
                detail=f"the child was killed abruptly ({detail}); it reruns once in the next "
                "idle window",
                progress={**job.progress, "stage": "interrupted", "breaks": breaks},
            )
        return self.store.transition(
            job,
            "failed",
            reason="child_failed",
            detail=f"the child died abruptly twice in a row; last: {detail}",
            finished_at=_now().isoformat(),
            progress={**job.progress, "stage": "failed", "breaks": breaks},
        )

    async def _run(self, job: BrainJob) -> BrainJob:
        child_pool.claim_child(child_pool.BRAIN_CHILD)
        self.running = job.job_id
        owned = self._executor_factory is None
        pool: Executor | None = None
        finished = False
        try:
            running: BrainJob = await self._io(self._start, job)
            call = functools.partial(
                self._runner,
                running.kind,
                running.params,
                self.config,
                job_id=running.job_id,
                results_dir=self.store.results,
                address_space_bytes=digest_address_space_bytes(self.config),
            )
            pool = (
                child_pool.spawn_pool()
                if self._executor_factory is None
                else self._executor_factory()
            )
            loop = asyncio.get_running_loop()
            started = loop.time()
            future = loop.run_in_executor(pool, call)
            child_pool.attach_child(child_pool.BRAIN_CHILD, pool)
            try:
                while True:
                    done, _ = await asyncio.wait({future}, timeout=self.poll_seconds)
                    if done:
                        break
                    reason = self._yield_reason()
                    if reason is not None:
                        future.cancel()
                        return await self._io(
                            self.store.transition,
                            running,
                            "deferred",
                            reason=f"yielded:{reason}",
                            detail="the child was stopped so collection or a higher-priority "
                            "analysis has the Pi; the job reruns in the next idle window",
                            progress={"stage": "yielded", "attempt": running.attempts},
                        )
                    if loop.time() - started > self.timeout:
                        future.cancel()
                        return await self._io(
                            self._failed,
                            running,
                            "child_timed_out",
                            f"did not finish within {self.timeout:.0f}s",
                        )
                try:
                    result = future.result()
                except BrokenExecutor as exc:
                    return await self._io(self._broken, running, exc)
                except MemoryError:
                    ceiling = digest_address_space_bytes(self.config) // (1024 * 1024)
                    return await self._io(
                        self._failed,
                        running,
                        "child_out_of_memory",
                        f"exceeded its {ceiling} MiB address-space ceiling",
                    )
                except Exception as exc:  # noqa: BLE001 - a job's failure is its state
                    return await self._io(
                        self._failed, running, "error", f"{type(exc).__name__}: {exc}"
                    )
                finished = True
                if isinstance(result, dict) and "pending" in result:
                    return await self._io(
                        self.store.transition,
                        running,
                        "pending",
                        reason=str(result["pending"].get("reason", "pending")),
                        detail=str(result["pending"].get("detail", "")),
                        result=result,
                        progress={
                            "stage": "pending",
                            "attempt": running.attempts,
                            "sessions_seen": result.get("sessions_seen"),
                        },
                    )
                return await self._io(
                    self.store.transition,
                    running,
                    "completed",
                    result=result,
                    finished_at=_now().isoformat(),
                    progress={"stage": "completed", "attempt": running.attempts},
                )
            except asyncio.CancelledError:
                # Shutdown: record it off the loop, shielded so a second
                # cancel cannot drop the record.
                await asyncio.shield(
                    asyncio.to_thread(
                        self.store.transition,
                        running,
                        "deferred",
                        reason="daemon_shutdown",
                        detail="the daemon stopped while the job ran; it reruns after restart",
                        progress={"stage": "interrupted", "attempt": running.attempts},
                    )
                )
                raise
        finally:
            self.running = None
            if pool is not None:
                if owned:
                    child_pool.discard_pool(pool, terminate=not finished)
                elif not finished:
                    pool.shutdown(wait=False, cancel_futures=True)
            child_pool.release_child(child_pool.BRAIN_CHILD)

    def _failed(self, job: BrainJob, reason: str, detail: str) -> BrainJob:
        return self.store.transition(
            job, "failed", reason=reason, detail=detail, finished_at=_now().isoformat()
        )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Recover, then run work in each idle window until ``stop_event``."""
        self._stop_event = stop_event
        await self._io(self.recover)
        while not stop_event.is_set():
            ran: BrainJob | None = None
            try:
                ran = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop outlives one bad job
                log.exception("brain scheduler step failed")
            if ran is not None and ran.state == "completed":
                continue  # back-to-back while the window stays idle
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                pass
