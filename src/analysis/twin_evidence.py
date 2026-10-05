"""Mint evidence from the target-phi twin's evaluation of a stored session (E04.4).

:func:`evaluate_and_mint` runs :func:`~src.analysis.target_phi.evaluate_session`
over a finished session for every map slot the session's own identity reads
resolve to, writes one :class:`~src.evidence.records.Evidence` record per slot
to the vehicle's memory ledger, and returns the ids beside a compact summary
(accounting, coverage, missing inputs, the observed/predicted trace) the
dashboard and MCP read back.

What the record can and cannot say is fixed here, not by the caller:

* **The twin predicts the ECM's command, never delivery.** Its evidence is the
  ECU computation; a measured engine response (B06 delivery error) is other
  evidence from another tool. The subject and the label say which.
* **A blocked qualification is ``unvalidated``, not a pass.** While the
  application model lists blockers between the lookup and the final word
  (E05.1), the result is unevaluable with the blockers named; the pass/miss
  counts stay as diagnostic values with no residual.
* **No eligible tuple is ``uncovered``** with the twin's own capture
  requirement as the detail ("which kind of empty", AGENTS.md).
* **Every record names what it read and what it omits** (E02.6):
  ``labels["evidence_class"]`` is ``ecu_command`` (the delivery loop refuses
  it), ``image_sha256``/``slot``/``table`` resolve the exact bytes, and
  ``blockers``/``omitted_logic``/``uncovered_logic`` are the application
  model's, so a residual's possible causes travel with it. A refusal, which
  resolved no table, labels all three ``unknown``, never ``none``.
  ``engine_health``
  is always ``not_assessed``: a zero residual says the reproduction agreed,
  not that the engine is healthy.
* **A qualified residual keeps its sign, beyond the tolerance.**
  ``residual_sign`` is ``positive`` (the ECM commanded richer than the
  reproduced logic by more than the twin's tolerance), ``negative``, ``zero``
  (every residual within the tolerance: a pass is agreement, never "richer")
  or ``mixed``. The per-sign counts, the tolerance and the raw extremes are
  values; the result's residual is the raw largest one unless signs are mixed.
* **A refusal is evidence too.** A table, program or image the twin refuses
  becomes an unevaluable record naming the refusal, so "could not evaluate" is
  not re-asked as if nobody had tried.

Identical inputs give byte-identical records, so a job that is re-run after a
restart returns the ids it minted before (the ledger's content dedup), and a
changed definition, table or code supersedes them as a recompute.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

import src.analysis.target_phi as target_phi_module
import src.decode.ecm_lookup as ecm_lookup_module
import src.signals.pairing as pairing_module
from src.analysis.target_phi import (
    EVALUATOR_VERSION,
    ResolvedTable,
    TwinDefinition,
    TwinRefusalError,
    evaluate_session,
)
from src.artifact_hash import sha256_file
from src.decode.ecm_lookup import KERNEL_VERSION
from src.evidence.ledger import MemoryLedger
from src.evidence.provenance import code_version, hash_json, hash_session_files
from src.evidence.records import (
    Conditions,
    Evidence,
    Provenance,
    Quality,
    Quantity,
    Result,
    SourceRef,
    UnevaluableReason,
    VehicleIdentity,
)
from src.paths import DIDS_YAML
from src.storage.session_files import session_files_fingerprint, session_footer_span
from src.storage.sessions import session_started_at

__all__ = [
    "ECU_COMMAND",
    "ENGINE_HEALTH_NOT_ASSESSED",
    "TRACE_LIMIT",
    "TWIN_INCIDENT",
    "TWIN_REGIME",
    "TWIN_TOOL",
    "TWIN_TOOL_VERSION",
    "TwinSessionIdentity",
    "evaluate_and_mint",
    "refusal_reason",
    "residual_sign",
    "summarise_evaluation",
    "tolerance_phi",
]

TWIN_TOOL = "target_phi_twin"
#: Bump when the mapping from an evaluation to an evidence record changes.
#: 2 (E02.6): evidence class, image/slot/table reference, omitted logic,
#: qualified-residual sign and counts.
TWIN_TOOL_VERSION = "2"
#: ``labels["evidence_class"]``: ECU computation agreement, never engine
#: response (B06 delivery error, ``src.analysis.delivery_evidence``).
ECU_COMMAND = "ecu_command"
#: ``labels["engine_health"]``: command agreement assesses the reproduction of
#: the ECM's logic, never the engine, so a zero residual is no health claim.
ENGINE_HEALTH_NOT_ASSESSED = "not_assessed"
#: What the twin's evidence is about across sessions (its recurrence key).
TWIN_INCIDENT = "twin:target_equiv_ratio"
#: The only regime the tool can claim: it accounts for every command sample
#: and applies the profile's declared gates itself.
TWIN_REGIME = (
    "every command sample in the session, gated by the profile's target_phi_twin.yaml "
    "(mode, warmth, steadiness, region, map identity)"
)
#: Most trace points a summary keeps; the rest are counted, never dropped silently.
TRACE_LIMIT = 5000

#: Refusal reasons from :mod:`src.analysis.target_phi` / the application model,
#: by the record contract's unevaluable reason.
_REFUSAL_REASONS: dict[str, UnevaluableReason] = {
    "wrong_program": "unknown_context",
    "program_unverified": "unknown_context",
    "image_hash_mismatch": "unknown_context",
    "application_model_mismatch": "unknown_context",
    "patch_missing": "unknown_context",
    "revision_unknown": "unknown_context",
    "image_unknown": "unknown_context",
    "table_missing": "uncovered",
    "slot_unresolved": "uncovered",
    "table_unverified": "unvalidated",
    "axis_unverified": "unvalidated",
    "mapping_unverified": "unvalidated",
    "mapping_mismatch": "unvalidated",
    "unmodelled_logic": "unvalidated",
}


def refusal_reason(reason: str) -> UnevaluableReason:
    """The record contract's reason for a named twin refusal."""
    return _REFUSAL_REASONS.get(reason, "refused")


@dataclass(frozen=True)
class TwinSessionIdentity:
    """Who and which calibration a session ran, resolved by the caller from
    the flash history (never typed): ``calibration`` is ``None`` when unknown."""

    profile: str
    vin: str | None
    calibration: str | None
    calibration_source: str
    calibration_conflict: bool = False
    #: The plain image the flash history records for ``calibration`` (named on
    #: a refusal, which has no resolved table to name it).
    image_sha256: str | None = None


def _aware(text: str) -> datetime:
    stamp = pd.Timestamp(text)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return stamp.floor("us").to_pydatetime()


def _session_window(session: Path) -> tuple[datetime, datetime]:
    span = session_footer_span(session)
    if span.first is not None and span.last is not None:
        return _aware(str(span.first)), _aware(str(span.last))
    started = session_started_at(session.name)
    if started is None:
        raise ValueError(f"{session.name}: no footer timestamps and no parseable start")
    return started, started


def _specs(definition: TwinDefinition) -> tuple[str, ...]:
    gates = definition.gates
    return tuple(
        dict.fromkeys(
            [
                definition.reference.channel,
                definition.reference.sentinel_channel,
                *definition.inputs.channels().values(),
                *definition.map_identity.channels(),
                *gates.coolant.sources,
                gates.mode.channel,
                *(c.channel for c in gates.steadiness.channels),
            ]
        )
    )


def _table_hash(table: ResolvedTable) -> str:
    return hash_json(
        {
            "cells": table.cells,
            "rows": table.row_axis_raw,
            "columns": table.column_axis_raw,
            "image_sha256": table.image_sha256,
            "slot": table.slot,
        }
    )


def _trace(result: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    points = []
    for record in result["records"]:
        sample = record["sample"]
        points.append(
            {
                "t": sample["acquired_at"],
                "observed_phi": sample["observed_phi"],
                "predicted_phi": record.get("predicted_phi"),
                "state": record["state"],
                "reason": record.get("reason"),
                "branch": record.get("branch"),
                "qualified_residual_phi": record.get("qualified_residual_phi"),
            }
        )
    omitted = max(0, len(points) - TRACE_LIMIT)
    return points[:TRACE_LIMIT], omitted


def _missing_inputs(result: dict[str, Any]) -> list[str]:
    """Inputs the session never held (``input_not_captured:<name>``), by name."""
    reasons = result["accounting"]["reasons"].get("unevaluable", {})
    return sorted(
        reason.split(":", 1)[1] for reason in reasons if reason.startswith("input_not_captured:")
    )


def summarise_evaluation(result: dict[str, Any], *, slot: int) -> dict[str, Any]:
    """The evaluation without its per-record detail: what the dashboard and MCP show."""
    trace, omitted = _trace(result)
    return {
        "slot": slot,
        "provenance": result["provenance"],
        "pairing": result["pairing"],
        "tolerance_words": result["tolerance_words"],
        "qualification_blockers": list(result["qualification_blockers"]),
        "accounting": result["accounting"],
        "coverage": result["coverage"],
        "capture_requirement": result["capture_requirement"],
        "missing_inputs": _missing_inputs(result),
        "trace": trace,
        "trace_omitted": omitted,
    }


def tolerance_phi(result: dict[str, Any], definition: TwinDefinition) -> float:
    """The evaluation's agreement tolerance in phi (``tolerance_words`` Q-words)."""
    return float(result["tolerance_words"]) / (1 << definition.reference.q_bits)


def _counts(result: dict[str, Any], tolerance: float) -> dict[str, Quantity]:
    accounting = result["accounting"]
    eligible = accounting["eligible"]
    values = {
        "candidates": Quantity(value=accounting["candidates"], unit="count"),
        **{state: Quantity(value=n, unit="count") for state, n in accounting["states"].items()},
    }
    for branch in ("ordinary", "override"):
        for verdict in ("pass", "miss"):
            values[f"{branch}_{verdict}"] = Quantity(value=eligible[branch][verdict], unit="count")
    differences = [
        abs(r["difference_phi"])
        for r in result["records"]
        if r["state"] == "eligible" and r.get("difference_phi") is not None
    ]
    if differences:
        values["max_abs_difference_phi"] = Quantity(value=max(differences), unit="1")
    residuals = _qualified(result)
    if residuals:
        values["qualified_residuals"] = Quantity(value=len(residuals), unit="count")
        values["tolerance_phi"] = Quantity(value=tolerance, unit="1")
        for sign, count in _signs(residuals, tolerance).items():
            values[f"{sign}_qualified_residuals"] = Quantity(value=count, unit="count")
        values["max_qualified_residual_phi"] = Quantity(value=max(residuals), unit="1")
        values["min_qualified_residual_phi"] = Quantity(value=min(residuals), unit="1")
    return values


def _qualified(result: dict[str, Any]) -> list[float]:
    """Every per-sample qualified residual (observed - predicted phi)."""
    return [
        float(r["qualified_residual_phi"])
        for r in result["records"]
        if r.get("qualified_residual_phi") is not None
    ]


def _signs(residuals: list[float], tolerance: float) -> dict[str, int]:
    """Counts by sign, where ``zero`` is *within the twin's tolerance*: a
    difference the evaluation itself scores a pass is agreement, not a
    direction."""
    return {
        "positive": sum(1 for r in residuals if r > tolerance),
        "negative": sum(1 for r in residuals if r < -tolerance),
        "zero": sum(1 for r in residuals if abs(r) <= tolerance),
    }


def residual_sign(residuals: list[float], tolerance: float) -> str:
    """``positive`` (the ECM commanded richer than the reproduced logic beyond
    ``tolerance``), ``negative`` (leaner), ``zero`` (every residual within the
    tolerance: agreement) or ``mixed``; ``none`` without any. Residuals within
    the tolerance never set a direction, so a passing sample cannot read as
    "richer"."""
    if not residuals:
        return "none"
    counts = _signs(residuals, tolerance)
    directed = [sign for sign in ("positive", "negative") if counts[sign]]
    if len(directed) == 2:
        return "mixed"
    if directed:
        return directed[0]
    return "zero"


def _result(result: dict[str, Any], tolerance: float) -> Result:
    accounting = result["accounting"]
    blockers = result["qualification_blockers"]
    eligible = accounting["states"]["eligible"]
    if accounting["candidates"] == 0:
        return Result(
            status="unevaluable",
            reason="missing_input",
            detail="the session holds no command sample of the twin's reference channel",
        )
    if blockers:
        return Result(
            status="unevaluable",
            reason="unvalidated",
            detail=(
                "qualification is blocked by "
                + ", ".join(blockers)
                + f"; {eligible} eligible tuple(s) kept as diagnostic counts, no residual"
            ),
        )
    if not eligible:
        requirement = result["capture_requirement"] or {}
        return Result(
            status="unevaluable",
            reason="uncovered",
            detail=requirement.get("message") or "no eligible tuple in the session",
        )
    residuals = _qualified(result)
    sign = residual_sign(residuals, tolerance)
    if sign in ("positive", "negative", "zero"):
        # The raw residual of largest magnitude. For a direction it has that
        # direction's sign; for agreement it is within the tolerance.
        extreme = max(residuals, key=abs)
        return Result(status="evaluated", residual=Quantity(value=extreme, unit="1"))
    # Mixed signs: no single residual stands for the session; the counts and
    # extremes are in the values.
    return Result(status="evaluated")


#: A refusal resolved no table, so the logic it would have omitted is unknown.
UNKNOWN_LOGIC = "unknown"


def _joined(items: tuple[str, ...] | list[str] | None) -> str:
    if items is None:
        return UNKNOWN_LOGIC
    return ",".join(items) or "none"


def _command_labels(
    *,
    image_sha256: str | None,
    slot: int,
    blockers: tuple[str, ...] | list[str] | None,
    omitted: tuple[str, ...] | None,
    uncovered: tuple[str, ...] | None,
) -> dict[str, str]:
    """Labels every twin record carries: what it is evidence *of* (the ECU's
    computation, never engine response or a fault), the exact image and slot it
    read, and the logic it omits (E02.6)."""
    return {
        "computation": "ecu_command",
        "evidence_class": ECU_COMMAND,
        "engine_health": ENGINE_HEALTH_NOT_ASSESSED,
        "image_sha256": image_sha256 or "unknown",
        "slot": str(slot),
        "blockers": _joined(blockers),
        "omitted_logic": _joined(omitted),
        "uncovered_logic": _joined(uncovered),
    }


def _provenance() -> Provenance:
    return Provenance(
        producer=TWIN_TOOL,
        producer_kind="tool",
        version=f"{TWIN_TOOL_VERSION}+evaluator{EVALUATOR_VERSION}+kernel{KERNEL_VERSION}",
        code_version=code_version(
            sys.modules[__name__], target_phi_module, ecm_lookup_module, pairing_module
        ),
    )


def _subject(session: Path, slot: int, tolerance_words: int, segment: str | None = None) -> str:
    """One subject per session, slot and tolerance; a session that spans a
    flash adds its identity segment (its calibration), so each segment's
    evaluation is its own record."""
    suffix = "" if segment is None else f":seg-{segment}"
    return f"{TWIN_TOOL}:{session.name}:slot{slot}:tol{tolerance_words}{suffix}"


def _mint_refusal(
    ledger: MemoryLedger,
    session: Path,
    *,
    vehicle: VehicleIdentity,
    definition: TwinDefinition,
    definition_sha256: str,
    slot: int,
    tolerance_words: int,
    refusal: TwinRefusalError,
    inputs: dict[str, str],
    flags: tuple[str, ...],
    image_sha256: str | None,
    segment: str | None = None,
) -> str:
    start, end = _session_window(session)
    record = Evidence(
        vehicle=vehicle,
        provenance=_provenance(),
        conditions=Conditions(regime=TWIN_REGIME, vehicle_state=None),
        valid_from=start,
        valid_to=end,
        subject=_subject(session, slot, tolerance_words, segment),
        incident=TWIN_INCIDENT,
        sources=(
            SourceRef(
                session=session.name,
                window_start=start,
                window_end=end,
                channels=_specs(definition),
                n_samples=0,
            ),
        ),
        input_hashes={**inputs, "twin_definition": definition_sha256},
        policy={"tolerance_words": tolerance_words, "slot": slot},
        labels={
            **_command_labels(
                image_sha256=image_sha256,
                slot=slot,
                blockers=None,
                omitted=None,
                uncovered=None,
            ),
            "refusal": refusal.reason,
        },
        quality=Quality(n_observations=0, timestamp_basis=None, flags=flags),
        result=Result(
            status="unevaluable", reason=refusal_reason(refusal.reason), detail=str(refusal)
        ),
    )
    return ledger.append(record).id


def _mint(
    ledger: MemoryLedger,
    session: Path,
    result: dict[str, Any],
    *,
    vehicle: VehicleIdentity,
    definition: TwinDefinition,
    definition_sha256: str,
    table: ResolvedTable,
    tolerance_words: int,
    inputs: dict[str, str],
    flags: tuple[str, ...],
    segment: str | None = None,
) -> str:
    stamps = [_aware(r["sample"]["acquired_at"]) for r in result["records"]]
    start, end = (min(stamps), max(stamps)) if stamps else _session_window(session)
    provenance = result["provenance"]
    record = Evidence(
        vehicle=vehicle,
        provenance=_provenance(),
        conditions=Conditions(regime=TWIN_REGIME, vehicle_state=None),
        valid_from=start,
        valid_to=end,
        subject=_subject(session, table.slot, tolerance_words, segment),
        incident=TWIN_INCIDENT,
        sources=(
            SourceRef(
                session=session.name,
                window_start=start,
                window_end=end,
                channels=_specs(definition),
                n_samples=len(stamps),
            ),
        ),
        input_hashes={
            **inputs,
            "twin_definition": definition_sha256,
            "application_model": provenance["application_model"]["sha256"],
            "table": _table_hash(table),
            "records": hash_json(result["records"]),
        },
        policy={
            "tolerance_words": tolerance_words,
            "slot": table.slot,
            "pairing": result["pairing"],
        },
        values=_counts(result, tolerance_phi(result, definition)),
        labels={
            **_command_labels(
                image_sha256=table.image_sha256,
                slot=table.slot,
                blockers=result["qualification_blockers"],
                omitted=table.application.blocked_nodes,
                uncovered=table.application.uncovered,
            ),
            "table": table.table_id,
            "table_source": table.source,
            "revision": table.revision or "unknown",
            "qualification": "blocked" if result["qualification_blockers"] else "qualified",
            "capture_requirement": (result["capture_requirement"] or {}).get("reason", "none"),
            "residual_sign": residual_sign(_qualified(result), tolerance_phi(result, definition)),
        },
        quality=Quality(
            n_observations=len(stamps),
            timestamp_basis=definition.gates.clock,
            flags=flags + tuple(f"blocker:{b}" for b in result["qualification_blockers"]),
        ),
        result=_result(result, tolerance_phi(result, definition)),
    )
    return ledger.append(record).id


def _slots_seen(result: dict[str, Any]) -> set[int]:
    seen: set[int] = set()
    for record in result["records"]:
        for side in (record.get("map_identity") or {}).values():
            if side and side.get("slot") is not None:
                seen.add(int(side["slot"]))
    return seen


def evaluate_and_mint(
    ledger: MemoryLedger,
    session: Path,
    *,
    definition: TwinDefinition,
    definition_sha256: str,
    identity: TwinSessionIdentity,
    program_id: str | None,
    table_for_slot: Callable[[int], ResolvedTable],
    dids_path: Path = DIDS_YAML,
    tolerance_words: int = 1,
    window: tuple[float | None, float | None] | None = None,
    segment: str | None = None,
) -> dict[str, Any]:
    """Evaluate a finished ``session`` per resolved slot and mint one record each.

    ``window``/``segment``: one identity segment of a session that spans a
    flash (``src.control.twin_identity``), evaluated on its own reads and
    minted under its own subject.

    ``table_for_slot`` resolves the table the ECM reads in a slot (from the W08
    library image or the transcribed calibration) and raises
    :class:`TwinRefusalError` by name. The profile's default slot is evaluated
    first; every other slot the session's identity reads resolve to is then
    evaluated against its own table, so a sample is only ever predicted from
    the slot it was read under. Returns ``{"evidence": [...], "evaluations":
    [...], "refusals": [...]}``.
    """
    session = Path(session)
    vehicle = VehicleIdentity(
        profile=identity.profile,
        vin=identity.vin or None,
        program_id=program_id,
        calibration=identity.calibration,
    )
    flags = ("calibration_conflict",) if identity.calibration_conflict else ()
    inputs = {
        "session_files": hash_session_files(session_files_fingerprint(session)),
        "dids": sha256_file(dids_path),
    }
    evidence: list[str] = []
    evaluations: list[dict[str, Any]] = []
    refusals: list[dict[str, Any]] = []
    pending = [definition.map_identity.default_slot.slot]
    done: set[int] = set()
    while pending:
        slot = pending.pop(0)
        if slot in done:
            continue
        done.add(slot)
        try:
            table = table_for_slot(slot)
            result = evaluate_session(
                session,
                definition,
                table,
                definition_sha256=definition_sha256,
                dids_path=dids_path,
                session_complete=True,
                tolerance_words=tolerance_words,
                window=window,
            )
        except TwinRefusalError as refusal:
            evidence.append(
                _mint_refusal(
                    ledger,
                    session,
                    vehicle=vehicle,
                    definition=definition,
                    definition_sha256=definition_sha256,
                    slot=slot,
                    tolerance_words=tolerance_words,
                    refusal=refusal,
                    inputs=inputs,
                    flags=flags,
                    image_sha256=identity.image_sha256,
                    segment=segment,
                )
            )
            refusals.append(
                {"slot": slot, "reason": refusal.reason, "detail": str(refusal), "id": evidence[-1]}
            )
            continue
        evidence.append(
            _mint(
                ledger,
                session,
                result,
                vehicle=vehicle,
                definition=definition,
                definition_sha256=definition_sha256,
                table=table,
                tolerance_words=tolerance_words,
                inputs=inputs,
                flags=flags,
                segment=segment,
            )
        )
        summary = summarise_evaluation(result, slot=slot)
        if segment is not None:
            summary["segment"] = segment
        summary["evidence_id"] = evidence[-1]
        evaluations.append(summary)
        pending.extend(sorted(_slots_seen(result) - done))
    return {"evidence": evidence, "evaluations": evaluations, "refusals": refusals}
