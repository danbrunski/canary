"""What a brain job computes, and whether it may run now (E04.4, E03.6, R29.13).

Split out of :mod:`src.control.brain_jobs`, which still exports every name here.
Submission validation (:func:`canonical_params`), the kind runners the child
executes (:func:`run_brain_job` and the ``_run_*`` it dispatches to) and the
cheap in-process :func:`precheck` the scheduler asks before it spends a child.
Nothing here owns a lock or a registry: the job log is
:mod:`src.control.brain_jobs_store`.
"""

from __future__ import annotations

import functools
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from src.analysis.candidate_promotion import (
    CandidateDrive,
    PromotionRules,
    evaluate_candidate,
    load_independent_drives,
    promote,
)
from src.analysis.change_evaluation import CHANGE_RESIDUALS_FILE, load_change_residuals
from src.analysis.digest import digest_row_budget
from src.analysis.target_phi import (
    TWIN_FILE,
    load_twin_definition,
)
from src.analysis.twin_evidence import TwinSessionIdentity, evaluate_and_mint
from src.analysis.twin_investigation import investigate_command_residual
from src.atomic_write import atomic_write_text
from src.config import CanaryConfig
from src.control.brain_jobs_store import BrainJob, BrainJobRefusedError, BrainJobStore, _now
from src.control.brain_state import brain_dir
from src.control.trip_end import limit_address_space
from src.control.twin_identity import TwinResolver, session_last_read
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
                image_sha256=identity.image_sha256,
            ),
            program_id=identity.program_id,
            table_for_slot=functools.partial(resolver.table, identity),
            window=segment.window(),
            segment=name,
        )
        for key in out:
            out[key].extend(minted[key])
    # E02.6: each minted record joins the investigation of its command
    # direction (or of "could not compare"), as ECU-command evidence. Re-running
    # the job re-states the same investigation, which the ledger deduplicates.
    out["investigations"] = list(
        dict.fromkeys(investigate_command_residual(ledger, ev) for ev in out["evidence"])
    )
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
