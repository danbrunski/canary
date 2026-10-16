"""Read access to the second brain: one function per fact, for HTTP and MCP (E04.5).

Each ``/control/brain/*`` route returns exactly what one function here
computes, and every MCP brain tool is a pass-through to that route
(``src/mcp_server/tools/brain.py``), so the two surfaces cannot disagree: there
is no second implementation to drift. The dashboard reads the same routes.

Two kinds of explanation are kept apart wherever evidence is shown
(:func:`split_parts`):

* **ECU computation** — what the ECM computed, predicted by the twin from the
  decoded logic (evidence labelled ``computation: ecu_command``);
* **measured engine response** — what the engine did, measured by other tools
  (delivery error, channel comparisons).

A twin residual says the ECM computed what its code says; it says nothing about
whether the engine delivered it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from src.config import CanaryConfig
from src.control.brain_jobs import BrainJobStore, job_id_for
from src.control.brain_state import current_identity, recovery_report
from src.evidence.cards import evidence_payload, investigation_card
from src.evidence.ledger import (
    ANY,
    MemoryLedger,
    MemoryQuery,
    MemoryReferenceError,
    UnknownRecordError,
    UnscopedMemoryError,
    memory_ledger_for,
)
from src.evidence.records import RECORD_KINDS, record_to_dict

__all__ = [
    "BrainReadError",
    "brain_job",
    "brain_jobs",
    "brain_status",
    "evidence_record",
    "investigation_card_payload",
    "memory_query",
    "split_parts",
    "twin_session_evaluation",
]

ECU_COMPUTATION = "ecu_command"


class BrainReadError(Exception):
    """A read refused by name. ``status`` is the HTTP code; ``detail`` the body."""

    def __init__(self, status: int, detail: dict[str, Any]) -> None:
        super().__init__(str(detail.get("message", detail)))
        self.status = status
        self.detail = detail

    def __reduce__(self) -> tuple[object, ...]:
        # Rebuilt from its constructor's arguments, so it survives a process
        # boundary (pickle); tests/test_exceptions_pickle.py holds every class to it.
        return (type(self), (self.status, self.detail), self.__dict__)


def _ledger(config: CanaryConfig) -> MemoryLedger:
    try:
        return memory_ledger_for(config)
    except UnscopedMemoryError as exc:
        raise BrainReadError(503, {"error": "unscoped", "message": str(exc)}) from exc


def _store(config: CanaryConfig) -> BrainJobStore:
    try:
        return BrainJobStore.for_config(config)
    except UnscopedMemoryError as exc:
        raise BrainReadError(503, {"error": "unscoped", "message": str(exc)}) from exc


def _instant(name: str, value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrainReadError(
            400, {"error": "bad_time", "message": f"{name}: {value!r} is not an ISO time"}
        ) from exc
    if parsed.tzinfo is None:
        raise BrainReadError(
            400, {"error": "bad_time", "message": f"{name}: give a UTC offset (e.g. ...Z)"}
        )
    return parsed


def _nullable(value: Any) -> object:
    if value is None or value == "":
        return ANY
    return None if value == "unknown" else str(value)


def _kinds(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    items = value if isinstance(value, list | tuple) else str(value).split(",")
    return tuple(str(item).strip() for item in items if str(item).strip())


def _flag(value: Any) -> bool:
    return value is True or str(value).lower() in ("1", "true", "yes")


def _entry_row(entry: Any) -> dict[str, Any]:
    return {
        "record": record_to_dict(entry.record),
        "superseded_at": None if entry.superseded_at is None else entry.superseded_at.isoformat(),
        "superseded_by": list(entry.superseded_by),
        "retracted_by": entry.retracted_by,
        "retired_by": entry.retired_by,
    }


def memory_query(config: CanaryConfig, params: Mapping[str, Any]) -> dict[str, Any]:
    """:class:`~src.evidence.ledger.MemoryQuery` over the vehicle's ledger.

    ``params`` are the query string: ``kind`` (comma list), ``incident``,
    ``subject``, ``session``, ``regime``, ``calibration`` / ``program_id``
    (``unknown`` = recorded as unknown), ``valid_at`` / ``valid_before`` /
    ``valid_after`` / ``as_of`` (ISO with offset), ``text``, ``history``,
    ``limit``."""
    limit = params.get("limit")
    try:
        query = MemoryQuery(
            kinds=_kinds(params.get("kind")),
            incident=params.get("incident") or None,
            subject=params.get("subject") or None,
            session=params.get("session") or None,
            regime=params.get("regime") or None,
            calibration=_nullable(params.get("calibration")),  # type: ignore[arg-type]
            program_id=_nullable(params.get("program_id")),  # type: ignore[arg-type]
            valid_at=_instant("valid_at", params.get("valid_at")),
            valid_before=_instant("valid_before", params.get("valid_before")),
            valid_after=_instant("valid_after", params.get("valid_after")),
            as_of=_instant("as_of", params.get("as_of")),
            text=params.get("text") or None,
            include_superseded=_flag(params.get("history")),
            limit=None if limit in (None, "") else int(str(limit)),
        )
    except ValueError as exc:
        raise BrainReadError(400, {"error": "bad_query", "message": str(exc)}) from exc
    ledger = _ledger(config)
    found = ledger.query(query) if ledger.path.exists() else []
    return {
        "vehicle": ledger.vehicle,
        "kinds": list(RECORD_KINDS),
        "count": len(found),
        "records": [_entry_row(entry) for entry in found],
    }


def _evidence_part(view: Mapping[str, Any]) -> str:
    labels = view.get("labels") or {}
    return "ecu_computation" if labels.get("computation") == ECU_COMPUTATION else "engine_response"


def split_parts(views: list[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Evidence ids by explanation part: ECU computation vs measured response."""
    parts: dict[str, list[str]] = {"ecu_computation": [], "engine_response": []}
    for view in views:
        ids = parts[_evidence_part(view)]
        if view["id"] not in ids:
            ids.append(view["id"])
    return parts


def investigation_card_payload(
    config: CanaryConfig, investigation_id: str, *, as_of: str | None = None
) -> dict[str, Any]:
    """The deterministic E01.5 card, with its evidence split into the ECU's
    computation and the measured engine response."""
    ledger = _ledger(config)
    try:
        card = investigation_card(
            ledger, investigation_id, as_of=_instant("as_of", as_of)
        ).model_dump(mode="json")
    except UnknownRecordError as exc:
        raise BrainReadError(404, {"error": "unknown_record", "message": str(exc)}) from exc
    except MemoryReferenceError as exc:
        raise BrainReadError(400, {"error": "bad_reference", "message": str(exc)}) from exc
    views = list(card["what_happened"])
    for hypothesis in card["hypotheses"]:
        views.extend(hypothesis["supporting"])
        views.extend(hypothesis["refuting"])
    return {"card": card, "parts": split_parts(views)}


def evidence_record(config: CanaryConfig, evidence_id: str) -> dict[str, Any]:
    """One evidence id resolved (or refused by which kind of bad id it is)."""
    ledger = _ledger(config)
    try:
        entry = ledger.resolve_evidence(evidence_id)
    except UnknownRecordError as exc:
        raise BrainReadError(404, {"error": "unknown_record", "message": str(exc)}) from exc
    except MemoryReferenceError as exc:
        raise BrainReadError(400, {"error": "bad_reference", "message": str(exc)}) from exc
    payload = evidence_payload(entry)
    payload["part"] = _evidence_part(payload)
    payload["recurrences"] = len(ledger.recurrences(evidence_id))
    return payload


def brain_jobs(config: CanaryConfig) -> dict[str, Any]:
    """Every brain job and its state, oldest first, with counts per state."""
    jobs = _store(config).ordered()
    return {
        "jobs": [job.to_dict() for job in jobs],
        "counts": dict(sorted(Counter(job.state for job in jobs).items())),
    }


def brain_job(config: CanaryConfig, job_id: str) -> dict[str, Any]:
    store = _store(config)
    job = store.get(job_id)
    if job is None:
        raise BrainReadError(404, {"error": "unknown_job", "message": f"no brain job {job_id!r}"})
    return {"job": job.to_dict(), "result": store.read_result(job_id)}


def twin_session_evaluation(config: CanaryConfig, session_id: str) -> dict[str, Any]:
    """The twin's evaluation of one session: the job, its per-slot summaries
    (accounting, missing inputs, capture requirement, observed/predicted
    trace) and whether it is still current for the program and calibration
    the vehicle runs now. ``not_evaluated`` when no job exists."""
    store = _store(config)
    job_id = job_id_for("twin_session_evaluation", {"session": session_id})
    job = store.get(job_id)
    if job is None:
        return {"session": session_id, "job_id": job_id, "state": "not_evaluated", "job": None}
    result = store.read_result(job_id) if job.state == "completed" else None
    payload: dict[str, Any] = {
        "session": session_id,
        "job_id": job_id,
        "state": job.state,
        "job": job.to_dict(),
        "result": result,
    }
    if result is not None:
        live = current_identity(config, now=datetime.now(UTC))
        program = result.get("program_id")
        payload["currency"] = {
            "program_current": program is not None and program == live.program_id,
            "program_detail": (
                "computed under the program in force now"
                if program is not None and program == live.program_id
                else f"computed under program {program!r}; now {live.program_id!r} "
                f"({live.program_status}): program-specific results are not current"
            ),
            "session_calibration": result.get("calibration"),
            "current_calibration": live.calibration,
            "calibration_detail": (
                "the session's own calibration; its tables were the ones in force when it ran"
            ),
        }
    return payload


#: ``live_twin`` when no daemon tick publishes one (a bare app, a test).
LIVE_TWIN_ABSENT: dict[str, Any] = {
    "enabled": False,
    "result": "not_running",
    "reason": "no live inference pipeline publishes the twin in this process",
}


#: ``live_proposals`` when no daemon tick publishes them.
LIVE_PROPOSALS_ABSENT: dict[str, Any] = {
    "enabled": False,
    "result": "not_running",
    "detail": "no live inference pipeline publishes measurement proposals in this process",
    "proposals": [],
}


def brain_status(
    config: CanaryConfig,
    *,
    process_started_at: datetime | None = None,
    live_twin: Mapping[str, Any] | None = None,
    live_proposals: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Recovery standing (identity, invalidations, tables, transient state,
    memory) plus job counts, and the live twin's current predictions,
    residuals and applicability as the inference tick last published them
    (``/status`` ``detectors.inference.twin``, the same block). Read-only:
    computing it never records a change. ``live_proposals`` is the tick's
    next-measurement proposals (``/status`` ``detectors.inference.proposals``,
    E03.4), passed through the same way."""
    try:
        report = recovery_report(config, process_started_at=process_started_at, write=False)
    except UnscopedMemoryError as exc:
        raise BrainReadError(503, {"error": "unscoped", "message": str(exc)}) from exc
    report["jobs"] = brain_jobs(config)["counts"]
    report["live_twin"] = dict(live_twin) if live_twin is not None else dict(LIVE_TWIN_ABSENT)
    report["live_proposals"] = (
        dict(live_proposals) if live_proposals is not None else dict(LIVE_PROPOSALS_ABSENT)
    )
    return report
