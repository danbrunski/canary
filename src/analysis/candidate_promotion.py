"""Evaluate an adaptive candidate on independent drives and promote it (E03.6).

An E03.5 candidate (:mod:`src.models.adaptive_candidate`) learns beside a
frozen reference (:mod:`src.models.change_detection`); this module decides,
after collection stops, whether it may *replace* that reference. The rules are
B03's champion/challenger rules (``src.models.promotion_gate``,
``docs/roadmap/online-training-strategy.md``) applied to a change-detection
reference, each a gate that can only fail closed:

1. **Calibration stamp (in-sample is categorically ineligible).** Evaluation
   uses only *independent* drives: none that fitted or calibrated the
   reference, none starting before the candidate's learned data ends, none
   across an invalidating intervention, none on another calibration, and none
   that began before the candidate's prediction was recorded. A better fit on
   the drives it learned from is never an input, so it cannot promote.
2. **Contamination sanity.** A checkpoint frozen by an alarm, that learned
   nothing, or whose parameters are not identified is rejected; an unresolved
   change alarm on the residual keeps the decision pending (promotion would
   absorb the change being investigated).
3. **Pre-registered prediction (E01.4).** :func:`register_prediction` records,
   before the evaluation drives, that the candidate predicts the residual
   better than the reference, with the vehicle's condition-matched noise floor
   for one drive's mean. Each test drive's paired comparison (the same blocks,
   the same cells, mean absolute residual of each relation) is minted as
   evidence and judged as an E01 outcome; the ledger derives whether the
   prediction came first. Only ``confirmed`` outcomes count, on
   ``min_independent_drives`` distinct drives. Without a floor every outcome is
   ``unresolvable`` and the decision stays pending: a change that cannot be
   told from noise is not evidence.
4. **Known answers (recall non-regression on injected faults).** Every
   declared non-ramp shift is injected into each test drive; the promoted
   reference must detect it on at least as many drives as the current one, and
   raise no more alarms on the drives as recorded.

The promoted reference is :func:`~src.models.change_detection.calibrate_promoted`
over the independent drives after the test drives, under the same policy and
every calibration gate. Too few drives anywhere is ``pending``, never forced.

Promotion (:func:`promote`) is a separate, explicit step, as B03's is: the
evaluation never promotes. It swaps the store's active reference atomically
(:class:`~src.models.candidate_store.CandidateStore`) and supersedes the
reference's memory record; the old reference stays on disk and on the ledger,
recoverable. A decision is content-keyed, the evidence and outcomes are
content-deduplicated by the ledger and the swap is compare-and-swap, so the
same evidence replayed creates no second outcome, decision or promotion.
Decisions on synthetic drives promote only on an isolated (replay) ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np

import src.models.candidate_store as store_module
import src.models.change_detection as change_module
from src.analysis.change_evaluation import (
    ResidualDefinition,
    assemble_drives,
    read_session_samples,
)
from src.analysis.noise_floor import FloorRefusedError, floor_reference
from src.evidence.ledger import MemoryLedger, MemoryQuery
from src.evidence.provenance import code_version
from src.evidence.records import (
    Conditions,
    Evidence,
    NoiseFloorRef,
    Outcome,
    Prediction,
    Provenance,
    Quality,
    Quantity,
    Reference,
    Result,
    SourceRef,
    VehicleIdentity,
)
from src.models.adaptive_candidate import invalidating_boundaries, unresolved_alarms
from src.models.candidate_store import CandidateStore
from src.models.change_detection import (
    Block,
    ChangeMonitor,
    ChangePolicy,
    FrozenReference,
    ReferenceRefusedError,
    blocks_of,
    calibrate_promoted,
)
from src.storage.sessions import recorded_sessions, session_started_at
from src.vehicle.tune_revisions import TuneRevisionRegistry

__all__ = [
    "TOOL",
    "TOOL_VERSION",
    "CandidateDecision",
    "CandidateDrive",
    "PromotionError",
    "PromotionResult",
    "PromotionRules",
    "candidate_status",
    "evaluate_candidate",
    "floor_quantity",
    "load_independent_drives",
    "prediction_for",
    "promote",
    "register_prediction",
]

TOOL = "candidate_promotion"
TOOL_VERSION = "1"

DecisionStatus = Literal["pending", "rejected", "qualified"]
DataOrigin = Literal["observed", "synthetic"]


class PromotionError(ValueError):
    """A promotion refused (not qualified, synthetic on live memory, stale)."""


@dataclass(frozen=True)
class PromotionRules:
    """How much independent evidence a promotion needs (B03's shape)."""

    #: Confirmed, pre-registered outcomes on this many distinct drives.
    min_independent_drives: int = 3
    #: Contradicted outcomes tolerated among the test drives.
    max_contradicted: int = 0

    def __post_init__(self) -> None:
        if self.min_independent_drives < 2:
            raise ValueError("one drive is not independent evidence; need at least two")


@dataclass(frozen=True)
class CandidateDrive:
    """One physical drive's blocks under the residual's policy."""

    drive: str
    sessions: tuple[str, ...]
    calibration: str | None
    blocks: tuple[Block, ...]
    data_origin: DataOrigin = "observed"

    @property
    def start(self) -> float:
        return min(b.start for b in self.blocks)

    @property
    def end(self) -> float:
        return max(b.end for b in self.blocks)


@dataclass
class CandidateDecision:
    residual: str
    checkpoint: str
    #: The active reference the decision was made against.
    reference: str | None
    status: DecisionStatus
    reasons: list[str]
    checks: dict[str, Any] = field(default_factory=dict)
    prediction: str | None = None
    data_origin: DataOrigin = "observed"
    test_drives: list[str] = field(default_factory=list)
    calibration_drives: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)
    #: Per test drive: evidence id, outcome id and the ledger's verdict.
    outcomes: dict[str, dict[str, str]] = field(default_factory=dict)
    promoted_reference: str | None = None
    key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "residual": self.residual,
            "checkpoint": self.checkpoint,
            "reference": self.reference,
            "status": self.status,
            "reasons": list(self.reasons),
            "checks": self.checks,
            "prediction": self.prediction,
            "data_origin": self.data_origin,
            "test_drives": list(self.test_drives),
            "calibration_drives": list(self.calibration_drives),
            "excluded": dict(self.excluded),
            "outcomes": self.outcomes,
            "promoted_reference": self.promoted_reference,
        }


def floor_quantity(residual: str) -> str:
    """The noise-floor key a candidate's gain is judged against: the null-run
    spread of one drive's mean residual (``noise_floor.yaml``)."""
    return f"{residual}_drive_mean"


def gain_quantity(residual: str, checkpoint: str) -> str:
    return f"candidate_gain:{residual}:{checkpoint[:16]}"


def _provenance(derived_from: Sequence[str] = ()) -> Provenance:
    return Provenance(
        producer=TOOL,
        producer_kind="tool",
        version=TOOL_VERSION,
        code_version=code_version(sys.modules[__name__], change_module, store_module),
        derived_from=tuple(dict.fromkeys(derived_from)),
    )


def _aware(t: float) -> datetime:
    return datetime.fromtimestamp(t, UTC)


# ---------------------------------------------------------------------------
# 1. Predict before the evaluation drives


def prediction_for(ledger: MemoryLedger, residual: str, checkpoint: str) -> Prediction | None:
    """The current pre-registered prediction for this checkpoint, if any."""
    quantity = gain_quantity(residual, checkpoint)
    found = [
        entry.record
        for entry in ledger.query(MemoryQuery(kinds=("prediction",)))
        if isinstance(entry.record, Prediction) and entry.record.quantity == quantity
    ]
    return found[0] if found else None


def register_prediction(
    ledger: MemoryLedger,
    store: CandidateStore,
    residual: str,
    checkpoint_fp: str,
    *,
    vehicle: VehicleIdentity,
    floor_path: Path | None,
    floor_conditions: str,
    now: datetime | None = None,
) -> str:
    """Record that the candidate predicts the residual better than the reference.

    Before any evaluation drive: a drive that began before this record is
    ``post_hoc`` for it and never counted. The floor is the vehicle's measured
    spread of one drive's mean residual under ``floor_conditions``
    (:func:`floor_quantity`); without one the prediction is recorded with no
    floor, and every outcome on it is ``unresolvable``. Recording the same
    prediction again returns the same record.
    """
    checkpoint, _spec = store.checkpoint(residual, checkpoint_fp)
    if checkpoint.valid_to is None:
        raise ValueError("a checkpoint that learned nothing predicts nothing")
    floor: NoiseFloorRef | None = None
    if floor_path is not None:
        try:
            floor = floor_reference(
                floor_path, floor_quantity(residual), conditions=floor_conditions
            )
        except FloorRefusedError:
            floor = None
    prediction = Prediction(
        vehicle=vehicle,
        provenance=_provenance(),
        conditions=Conditions(regime=floor_conditions, vehicle_state="engine_running"),
        valid_from=checkpoint.valid_to,
        investigation=None,
        intervention=None,
        quantity=gain_quantity(residual, checkpoint_fp),
        direction="increase",
        magnitude=None,
        noise_floor=floor,
    )
    return ledger.append(prediction, now=now).id


# ---------------------------------------------------------------------------
# 2. Evaluate on independent drives


def _blocks_hash(blocks: Iterable[Block]) -> str:
    digest = hashlib.sha256()
    for b in blocks:
        digest.update(
            json.dumps(
                [b.drive, b.start, b.end, b.cell, b.n, b.y, list(b.x)], separators=(",", ":")
            ).encode()
        )
    return digest.hexdigest()


def _detected(
    reference: FrozenReference, drive: str, blocks: Sequence[Block], onset: float, want: str
) -> bool:
    monitor = ChangeMonitor(reference)
    monitor.start_drive(drive)
    for block in blocks:
        monitor.observe(block)
    monitor.close_drive()
    return any(
        e.direction == want and e.first_alarm >= onset and e.detector in ("cusum", "glr")
        for e in monitor.events
    )


def _observed_events(reference: FrozenReference, drives: Sequence[CandidateDrive]) -> int:
    monitor = ChangeMonitor(reference)
    for drive in drives:
        monitor.start_drive(drive.drive)
        for block in drive.blocks:
            monitor.observe(block)
        monitor.close_drive()
    return len(monitor.events)


def _gain_evidence(
    ledger: MemoryLedger,
    drive: CandidateDrive,
    blocks: Sequence[Block],
    *,
    residual: str,
    unit: str,
    channels: tuple[str, ...],
    reference: FrozenReference,
    theta: np.ndarray,
    checkpoint: str,
    vehicle: VehicleIdentity,
    regime: str,
    now: datetime | None,
) -> str:
    design = np.asarray([b.x for b in blocks], dtype=np.float64)
    y = np.asarray([b.y for b in blocks], dtype=np.float64)
    ref_abs = float(np.mean(np.abs(y - design @ np.asarray(reference.theta))))
    cand_abs = float(np.mean(np.abs(y - design @ theta)))
    gain = ref_abs - cand_abs
    per_cell: dict[str, float] = {}
    for cell in sorted({b.cell for b in blocks}):
        mask = np.asarray([b.cell == cell for b in blocks])
        per_cell[cell] = float(
            np.mean(np.abs(y[mask] - design[mask] @ np.asarray(reference.theta)))
            - np.mean(np.abs(y[mask] - design[mask] @ theta))
        )
    values = {
        "reference_mean_abs_residual": Quantity(value=ref_abs, unit=unit),
        "candidate_mean_abs_residual": Quantity(value=cand_abs, unit=unit),
        "gain": Quantity(value=gain, unit=unit),
        "blocks": Quantity(value=float(len(blocks)), unit="1"),
    }
    for cell, value in per_cell.items():
        values[f"gain_{cell}"] = Quantity(value=value, unit=unit)
    evidence = Evidence(
        vehicle=vehicle,
        provenance=_provenance(),
        conditions=Conditions(regime=regime, vehicle_state="engine_running"),
        valid_from=_aware(min(b.start for b in blocks)),
        valid_to=_aware(max(b.end for b in blocks)),
        subject=f"{TOOL}:{residual}:{checkpoint[:16]}:{drive.drive}",
        sources=(
            SourceRef(
                session=",".join(drive.sessions),
                window_start=_aware(min(b.start for b in blocks)),
                window_end=_aware(max(b.end for b in blocks)),
                channels=channels,
                n_samples=sum(b.n for b in blocks),
            ),
        ),
        input_hashes={
            "blocks": _blocks_hash(blocks),
            "checkpoint": checkpoint,
            "reference": reference.fingerprint,
        },
        policy={
            "comparison": (
                "paired: the same blocks in the reference's calibrated cells, mean absolute "
                "residual of the reference's relation minus the candidate's"
            ),
            "reference": reference.fingerprint,
            "checkpoint": checkpoint,
            "cells": sorted({b.cell for b in blocks}),
        },
        values=values,
        labels={
            "kind": "candidate_gain",
            "residual": residual,
            "data_origin": drive.data_origin,
            "evidence_class": "engine_response",
        },
        quality=Quality(
            n_observations=len(blocks),
            timestamp_basis="acquired",
            flags=("synthetic",) if drive.data_origin == "synthetic" else (),
        ),
        result=Result(status="evaluated", residual=Quantity(value=gain, unit=unit)),
    )
    return ledger.append(evidence, now=now).id


def _outcome(
    ledger: MemoryLedger, prediction: Prediction, evidence_id: str, now: datetime | None
) -> str:
    record = ledger.get(evidence_id)
    assert isinstance(record, Evidence) and record.result.residual is not None
    gain = record.result.residual
    floor = prediction.noise_floor
    result: Result
    disposition: Literal["no_effect", "unresolvable", "measured_change"]
    if floor is None:
        disposition = "unresolvable"
        result = Result(
            status="evaluated",
            residual=gain,
            detail=(
                "no condition-matched noise floor for one drive's mean residual: an "
                "improvement cannot be told from noise"
            ),
        )
    else:
        disposition = "measured_change" if abs(gain.value) >= floor.detectable else "no_effect"
        result = Result(status="evaluated", residual=gain)
    outcome = Outcome(
        vehicle=prediction.vehicle,
        provenance=_provenance([evidence_id]),
        conditions=prediction.conditions,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
        prediction=prediction.id,
        intervention=None,
        disposition=disposition,
        evidence=(evidence_id,),
        result=result,
    )
    return ledger.append(outcome, now=now).id


def _identified(checkpoint: Any, spec: Any) -> list[str]:
    weak = []
    for i, name in enumerate(spec.parameters):
        sd = math.sqrt(max(float(checkpoint.covariance[i][i]), 0.0))
        if sd > spec.identified_sd[i]:
            weak.append(f"{name} (posterior sd {sd:.3g} > {spec.identified_sd[i]:g})")
    return weak


def evaluate_candidate(
    ledger: MemoryLedger,
    store: CandidateStore,
    residual: str,
    checkpoint_fp: str,
    drives: Sequence[CandidateDrive],
    *,
    channels: tuple[str, ...],
    vehicle: VehicleIdentity,
    rules: PromotionRules | None = None,
    now: datetime | None = None,
) -> CandidateDecision:
    """Decide ``pending`` / ``rejected`` / ``qualified`` for one checkpoint.

    Mints the paired comparison and its outcome for each test drive (the
    first ``min_independent_drives`` independent drives) and records the
    decision in the store. Never promotes.
    """
    rules = rules or PromotionRules()
    moment = now or datetime.now(UTC)
    decision = CandidateDecision(residual, checkpoint_fp, None, "pending", [])
    origins = {d.data_origin for d in drives}
    decision.data_origin = "synthetic" if "synthetic" in origins else "observed"
    reference = store.active(residual)
    if reference is None:
        decision.reasons.append(
            "no_frozen_reference: no reference has been frozen for this residual (E03.3), "
            "so there is nothing for a candidate to replace"
        )
        return _record(store, decision)
    decision.reference = reference.fingerprint
    policy = store.policy(residual, reference.fingerprint)
    checkpoint, spec = store.checkpoint(residual, checkpoint_fp)
    prediction = prediction_for(ledger, residual, checkpoint_fp)
    rejected: list[str] = []

    # Gate 2: contamination and lineage.
    if checkpoint.reference_fingerprint != reference.fingerprint:
        rejected.append(
            f"lineage: the candidate adapted from {checkpoint.reference_fingerprint[:16]}, not "
            f"the active reference {reference.fingerprint[:16]}"
        )
    if checkpoint.calibration != reference.calibration:
        rejected.append("calibration: the candidate belongs to another calibration")
    if checkpoint.frozen_by:
        rejected.append(
            f"frozen_by_alarm: the candidate was frozen by {list(checkpoint.frozen_by)}; a "
            "lineage frozen by a change cannot promote (start a new one once it resolves)"
        )
    if checkpoint.n_learned == 0 or checkpoint.valid_to is None:
        rejected.append("nothing_learned: the checkpoint learned no block")
    weak = _identified(checkpoint, spec)
    if weak:
        rejected.append(f"unidentified: {', '.join(weak)}")
    boundaries = [b for b in invalidating_boundaries(ledger, channels) if b > reference.valid_from]
    boundary = min(boundaries, default=None)
    if boundary is not None:
        rejected.append(
            f"retired: an intervention at {boundary.isoformat()} retires the reference; its "
            "candidates retire with it"
        )
    decision.checks["contamination"] = {"ok": not rejected, "learned": checkpoint.n_learned}
    if rejected:
        decision.status = "rejected"
        decision.reasons = rejected
        return _record(store, decision)
    alarms = unresolved_alarms(ledger, residual, before=moment)
    if alarms:
        decision.reasons.append(
            f"unresolved_change_alarm: {list(alarms)}; promotion would absorb the change "
            "under investigation"
        )
        return _record(store, decision)
    if prediction is None:
        decision.reasons.append(
            "no_prediction: register the candidate's prediction before evaluation drives "
            "are recorded (register_prediction)"
        )
        return _record(store, decision)
    decision.prediction = prediction.id
    assert checkpoint.valid_to is not None and prediction.recorded_at is not None

    # Gate 1: independent drives only.
    independent: list[CandidateDrive] = []
    folds = {*reference.train_drives, *reference.calibration_drives}
    for drive in sorted(drives, key=lambda d: (d.start if d.blocks else math.inf, d.drive)):
        calibrated = [b for b in drive.blocks if reference.limits(b.cell) is not None]
        why: str | None = None
        if not drive.blocks:
            why = "no_blocks"
        elif drive.calibration != reference.calibration:
            why = (
                f"calibration {drive.calibration!r}, not the reference's {reference.calibration!r}"
            )
        elif drive.drive in folds or set(drive.sessions) & folds:
            why = "in_sample: it fitted or calibrated the reference"
        elif drive.start <= checkpoint.valid_to.timestamp():
            why = "in_sample: it begins within the candidate's learned data"
        elif boundary is not None and drive.end >= boundary.timestamp():
            why = "across_intervention"
        elif drive.start <= prediction.recorded_at.timestamp():
            why = "post_hoc: it began before the prediction was recorded"
        elif not calibrated:
            why = "uncovered: no block in a calibrated cell"
        if why is not None:
            decision.excluded[drive.drive] = why
            continue
        independent.append(replace(drive, blocks=tuple(drive.blocks)))
    test = independent[: rules.min_independent_drives]
    calibration_fold = independent[rules.min_independent_drives :]
    decision.test_drives = [d.drive for d in test]
    decision.calibration_drives = [d.drive for d in calibration_fold]

    # Gate 3: the pre-registered prediction, judged per independent test drive.
    theta = np.asarray(checkpoint.theta, dtype=np.float64)
    verdicts: dict[str, str] = {}
    for drive in test:
        calibrated = [b for b in drive.blocks if reference.limits(b.cell) is not None]
        evidence_id = _gain_evidence(
            ledger,
            drive,
            calibrated,
            residual=residual,
            unit=reference.unit,
            channels=channels,
            reference=reference,
            theta=theta,
            checkpoint=checkpoint_fp,
            vehicle=vehicle,
            regime=prediction.conditions.regime,
            now=now,
        )
        outcome_id = _outcome(ledger, prediction, evidence_id, now)
        verdict = ledger.assess(outcome_id).verdict
        verdicts[drive.drive] = verdict
        decision.outcomes[drive.drive] = {
            "evidence": evidence_id,
            "outcome": outcome_id,
            "verdict": verdict,
        }
    confirmed = sum(1 for v in verdicts.values() if v == "confirmed")
    contradicted = [d for d, v in verdicts.items() if v == "contradicted"]
    decision.checks["prediction"] = {
        "floor": None
        if prediction.noise_floor is None
        else {
            "quantity": prediction.noise_floor.quantity,
            "detectable": prediction.noise_floor.detectable,
            "conditions": prediction.noise_floor.conditions,
        },
        "confirmed": confirmed,
        "needed": rules.min_independent_drives,
        "contradicted": contradicted,
    }
    if len(contradicted) > rules.max_contradicted:
        decision.status = "rejected"
        decision.reasons = [
            f"contradicted: on independent drive(s) {contradicted} the candidate did not "
            "predict the residual better than the reference beyond the noise floor"
        ]
        return _record(store, decision)
    if len(test) < rules.min_independent_drives:
        decision.reasons.append(
            f"insufficient_independent_drives: {len(test)} of {rules.min_independent_drives} "
            "test drives (independent of the reference, the candidate's learning and its "
            "prediction)"
        )
    elif confirmed < rules.min_independent_drives:
        unresolved = sorted(d for d, v in verdicts.items() if v != "confirmed")
        decision.reasons.append(
            f"outcomes_unresolved: {confirmed} of {rules.min_independent_drives} confirmed; "
            f"{unresolved} are {sorted({verdicts[d] for d in unresolved})}"
            + (
                " (no condition-matched floor: measure "
                f"{floor_quantity(residual)} under {prediction.conditions.regime!r})"
                if prediction.noise_floor is None
                else ""
            )
        )

    # The promoted reference, calibrated on the independent drives after the tests.
    promoted: FrozenReference | None = None
    if len(test) == rules.min_independent_drives:
        try:
            promoted = calibrate_promoted(
                policy,
                reference,
                checkpoint.theta,
                [b for d in calibration_fold for b in d.blocks],
                promoted_from=checkpoint_fp,
                learned_from=checkpoint.valid_from or checkpoint.valid_to,
            )
        except ReferenceRefusedError as exc:
            decision.reasons.append(
                f"recalibration_pending: {exc} (independent drives after the test drives "
                f"{decision.calibration_drives})"
            )
    if promoted is not None:
        # Gate 4: known answers and false alarms on the test drives.
        recall = {"current": 0, "promoted": 0, "trials": 0}
        for shift in policy.shifts:
            if shift.kind == "drive_ramp":
                continue
            want = "+" if shift.magnitude > 0 else "-"
            for drive in test:
                onset = drive.start + shift.start_after_seconds
                shifted = [replace(b, y=b.y + shift.offset(b.start - onset)) for b in drive.blocks]
                recall["trials"] += 1
                recall["current"] += _detected(reference, drive.drive, shifted, onset, want)
                recall["promoted"] += _detected(promoted, drive.drive, shifted, onset, want)
        alarms_now = {
            "current": _observed_events(reference, test),
            "promoted": _observed_events(promoted, test),
        }
        decision.checks["known_answers"] = recall
        decision.checks["observed_events"] = alarms_now
        if recall["promoted"] < recall["current"]:
            decision.status = "rejected"
            decision.reasons = [
                f"recall_regression: the promoted reference detects {recall['promoted']} of "
                f"{recall['trials']} injected shifts, the current one {recall['current']}"
            ]
            return _record(store, decision)
        if alarms_now["promoted"] > alarms_now["current"]:
            decision.status = "rejected"
            decision.reasons = [
                f"more_alarms: {alarms_now['promoted']} events on the recorded test drives, "
                f"against {alarms_now['current']} under the current reference"
            ]
            return _record(store, decision)
        store.stage_reference(residual, promoted, policy)
        decision.promoted_reference = promoted.fingerprint
    if not decision.reasons and promoted is not None:
        decision.status = "qualified"
    return _record(store, decision)


def _record(store: CandidateStore, decision: CandidateDecision) -> CandidateDecision:
    decision.key = store.record_decision(decision.residual, decision.as_dict())
    return decision


# ---------------------------------------------------------------------------
# 3. Promote (explicit, separate)


@dataclass(frozen=True)
class PromotionResult:
    #: ``False`` when the candidate was already active (nothing changed).
    changed: bool
    reference: str
    previous: str
    #: The memory record of the promoted reference.
    record: str


def promote(
    ledger: MemoryLedger,
    store: CandidateStore,
    residual: str,
    decision_key: str,
    *,
    vehicle: VehicleIdentity,
    channels: tuple[str, ...],
    now: datetime | None = None,
) -> PromotionResult:
    """Make a qualified decision's reference active; the old one stays recoverable."""
    found = [d for d in store.decisions(residual) if d["key"] == decision_key]
    if not found:
        raise PromotionError(f"no decision {decision_key[:16]} for {residual}")
    decision = found[0]
    if decision["status"] != "qualified" or decision.get("promoted_reference") is None:
        raise PromotionError(
            f"decision {decision_key[:16]} is {decision['status']}: "
            f"{'; '.join(decision['reasons']) or 'not qualified'}"
        )
    if decision["data_origin"] == "synthetic" and ledger.replay_id is None:
        raise PromotionError(
            "a decision on synthetic drives cannot promote a reference in this vehicle's "
            "live memory; demonstrate it on an isolated (replay) ledger"
        )
    fingerprint = str(decision["promoted_reference"])
    previous = str(decision["reference"])
    # The decision was right when it was made; memory may have moved since.
    # Re-check the gates that can change after it: an alarm raised, or an
    # intervention that retires the reference being replaced.
    moment = now or datetime.now(UTC)
    alarms = unresolved_alarms(ledger, residual, before=moment)
    if alarms:
        raise PromotionError(
            f"unresolved_change_alarm: {list(alarms)} stands since decision "
            f"{decision_key[:16]}; promotion would absorb the change under investigation"
        )
    replaced = store.reference(residual, previous)
    boundaries = [b for b in invalidating_boundaries(ledger, channels) if b > replaced.valid_from]
    if boundaries:
        raise PromotionError(
            f"retired: an intervention at {min(boundaries).isoformat()} retires reference "
            f"{previous[:16]}; its candidates retire with it"
        )
    reference = store.reference(residual, fingerprint)
    policy = store.policy(residual, fingerprint)
    changed = store.activate(
        residual,
        reference,
        policy,
        expected_active=previous,
        decision=decision_key,
        now=now,
    )
    earlier = [
        entry.record.id
        for entry in ledger.query(MemoryQuery(kinds=("reference",)))
        if isinstance(entry.record, Reference)
        and entry.record.source_ref == f"change_reference:{previous}"
    ]
    evidence = tuple(v["evidence"] for v in decision["outcomes"].values())
    record = Reference(
        vehicle=vehicle,
        provenance=_provenance(
            [
                *([decision["prediction"]] if decision.get("prediction") else []),
                *(v["outcome"] for v in decision["outcomes"].values()),
            ]
        ),
        conditions=Conditions(
            regime=f"{residual}: promoted candidate {str(decision['checkpoint'])[:16]}",
            vehicle_state="engine_running",
        ),
        valid_from=reference.valid_from,
        reference_kind="evidence",
        name=reference.name,
        channels=channels,
        source="memory",
        source_ref=f"change_reference:{fingerprint}",
        investigation=None,
        evidence=evidence,
        supersedes=tuple(earlier),
        supersession_reason=(
            f"promoted candidate {str(decision['checkpoint'])[:16]} (decision "
            f"{decision_key[:16]}); the previous reference is kept and recoverable"
            if earlier
            else None
        ),
    )
    stored = ledger.append(record, now=now)
    return PromotionResult(changed, fingerprint, previous, stored.id)


# ---------------------------------------------------------------------------
# Real drives and status


def load_independent_drives(
    storage: Path,
    definition: ResidualDefinition,
    policy: ChangePolicy,
    *,
    registry: TuneRevisionRegistry | None,
    calibration: str | None,
    after: datetime,
) -> tuple[list[CandidateDrive], dict[str, str]]:
    """Recorded sessions starting after ``after``, as drives of blocks on
    ``calibration`` (``read_session_samples`` + ``assemble_drives``: the same
    gates, pairing and exclusions as E03.3); returns drives and the excluded
    sessions with reasons. Whole-session reads: run in the brain child."""
    sessions = [
        s
        for s in recorded_sessions([storage])
        if (started := session_started_at(s)) is not None and started > after
    ]
    samples = [read_session_samples(s, definition, registry=registry) for s in sessions]
    drives, excluded = assemble_drives(samples, calibration=calibration)
    out: list[CandidateDrive] = []
    for drive in drives:
        blocks, _dropped = blocks_of(policy, drive.drive_id, drive.samples(definition))
        out.append(CandidateDrive(drive.drive_id, drive.sessions, calibration, tuple(blocks)))
    return out, excluded


def candidate_status(store: CandidateStore, residual: str) -> dict[str, Any]:
    """What the store holds for ``residual``: plain data for a report or route."""
    active = store.active_fingerprint(residual)
    decisions = store.decisions(residual)
    return {
        "residual": residual,
        "active_reference": active,
        "references": store.references(residual),
        "history": store.history(residual),
        "checkpoints": store.checkpoints(residual),
        "latest_decision": decisions[-1] if decisions else None,
        "state": (
            "no_frozen_reference"
            if active is None
            else decisions[-1]["status"]
            if decisions
            else "no_decision"
        ),
    }
