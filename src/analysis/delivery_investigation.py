"""The fuel-delivery evidence/memory loop (E03.1).

observe → investigate → predict → verify → remember, entirely through the E01
ledger and cards, on B06's existing delivery analysis
(:mod:`src.analysis.delivery_evidence`) and the vehicle's fault model
(:mod:`src.analysis.fault_model`, E03.2):

1. :func:`detect_discrepancy` reads one delivery-error evidence record and
   says whether it is a *qualified* discrepancy — an evaluated residual whose
   sign cleared the regime's condition-matched floor, on a drive that did not
   inform B06's enrichment constants — or which kind of not: ``unevaluable``
   (missing commanded AFR, never co-observed, no enrichment reached — each
   its own reason), ``within_floor`` or ``unqualified`` (in-sample drive, no
   floor, delay-unstable).
2. :func:`investigate` opens the investigation for a qualified discrepancy —
   or, when one is already open on the incident, *retrieves* it and folds the
   new occurrence in (a later drive does not open a second investigation).
   Hypotheses are the fault model's; each carries the evidence ids that
   support or refute it under its signature (single-fault: any conflict
   refutes). The structural analysis runs per drive, on that drive's own
   channels (``labels["fault_model_channels"]``) and coverage. Pairs no
   observation has separated are unknowns; :func:`select_measurement` names
   what could separate them.
3. :func:`predict_recurrence` / :func:`predict_pressure_flow_loss` record
   predictions before the verifying data, each with the delivery error's
   condition-matched floor.
4. :func:`evaluate_outcome` writes the outcome on the *next independent
   drive* only (another, later session); an unevaluable verifying record
   keeps its reason (missing input and uncovered stay distinct), an
   unqualified or in-sample one is ``refused``, a record from another region
   is ``uncovered``; none of them is a zero or a resolution.
5. :func:`retrieve` answers "have we seen this before?" for a new occurrence.

ECU-command disagreement (the target-phi twin, E02/E05) is not engine
response: every entry point refuses evidence from any producer other than the
delivery and fuel-pressure tools, so a twin residual cannot open or feed a
delivery investigation.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import src.analysis.fault_model as fault_model_module
from src.analysis.delivery_evidence import (
    DELIVERY_INCIDENT,
    DELIVERY_QUANTITY,
    DELIVERY_REGION,
    DELIVERY_TOOL,
    DROOP_TOOL,
    ENGINE_RESPONSE,
    FLOW_LOSS_QUANTITY,
    INFORMED_REFUSAL,
)
from src.analysis.fault_model import (
    FaultModel,
    Feature,
    Observation,
    StructuralReport,
    evaluate_observations,
    score,
    structural_analysis,
)
from src.analysis.noise_floor import floor_reference
from src.evidence.ledger import MemoryLedger, MemoryQuery
from src.evidence.provenance import code_version
from src.evidence.records import (
    Conditions,
    Evidence,
    Hypothesis,
    Investigation,
    Outcome,
    Prediction,
    Provenance,
    Quantity,
    Result,
)

__all__ = [
    "INVESTIGATION_TOOL",
    "INVESTIGATION_TOOL_VERSION",
    "Discrepancy",
    "InvestigationStep",
    "MeasurementChoice",
    "NotEngineResponseError",
    "NotNextDriveError",
    "Retrieval",
    "UnresolvedPair",
    "detect_discrepancy",
    "drive_reports",
    "evaluate_outcome",
    "investigate",
    "observation_of",
    "predict_pressure_flow_loss",
    "predict_recurrence",
    "retrieve",
    "select_measurement",
    "unresolved_pairs",
]

INVESTIGATION_TOOL = "delivery_investigation"
INVESTIGATION_TOOL_VERSION = "2"

#: Producers whose records are observations of a fault-model residual.
_OBSERVING_TOOLS = frozenset({DELIVERY_TOOL, DROOP_TOOL})


class NotEngineResponseError(ValueError):
    """Evidence that is not an engine-response residual offered to this loop."""


class NotNextDriveError(ValueError):
    """An outcome offered on data that is not a later, independent drive."""


def _provenance(derived_from: Sequence[str], model: FaultModel | None = None) -> Provenance:
    version = INVESTIGATION_TOOL_VERSION
    if model is not None:
        version += f"; fault model v{model.version}"
    return Provenance(
        producer=INVESTIGATION_TOOL,
        producer_kind="tool",
        version=version,
        code_version=code_version(sys.modules[__name__], fault_model_module),
        derived_from=tuple(dict.fromkeys(derived_from)),
    )


def _evidence(ledger: MemoryLedger, evidence_id: str) -> Evidence:
    record = ledger.resolve_evidence(evidence_id).record
    assert isinstance(record, Evidence)
    return record


def _require_engine_response(record: Evidence, *, tools: frozenset[str]) -> None:
    if record.provenance.producer not in tools or (
        record.labels.get("evidence_class") != ENGINE_RESPONSE
    ):
        raise NotEngineResponseError(
            f"{record.id} was produced by {record.provenance.producer!r} "
            f"({record.labels.get('evidence_class', 'no evidence class')}); this loop takes "
            f"engine-response residuals from {sorted(tools)}. A twin residual is ECU-"
            "computation agreement (E02/E05): it predicts the command, not what the engine "
            "did with it, and stays separate evidence."
        )


def _independent(record: Evidence) -> bool | None:
    """``True`` only when the record states it is out of sample."""
    stated = record.labels.get("session_informed_constants")
    return None if stated is None else stated == "false"


# ---------------------------------------------------------------------------
# 1. Detect


DiscrepancyStatus = Literal["qualified", "within_floor", "unqualified", "unevaluable"]


@dataclass(frozen=True)
class Discrepancy:
    evidence: str
    status: DiscrepancyStatus
    #: ``lean`` / ``rich`` for a qualified discrepancy, else ``None``.
    direction: Literal["lean", "rich"] | None
    #: The E01 unevaluable reason, or why the sign was not qualified.
    reason: str | None
    detail: str

    @property
    def qualified(self) -> bool:
        return self.status == "qualified"


def detect_discrepancy(ledger: MemoryLedger, evidence_id: str) -> Discrepancy:
    """Is this delivery-error record a qualified discrepancy, and if not, why not.

    Qualified needs all of: evaluated, a sign the tool qualified, and an
    explicit statement that the drive did not inform B06's enrichment
    constants. An in-sample drive is ``unqualified`` whatever its residual.
    """
    record = _evidence(ledger, evidence_id)
    _require_engine_response(record, tools=frozenset({DELIVERY_TOOL}))
    if record.result.status == "unevaluable":
        return Discrepancy(
            evidence_id, "unevaluable", None, record.result.reason, record.result.detail
        )
    independent = _independent(record)
    if independent is not True:
        detail = (
            INFORMED_REFUSAL
            if independent is False
            else "the record does not state whether its drive informed the constants"
        )
        return Discrepancy(evidence_id, "unqualified", None, "in_sample", detail)
    sign = record.labels.get("sign")
    if sign is None:
        refused = record.labels.get("sign_refused", "no qualified sign")
        return Discrepancy(evidence_id, "unqualified", None, "unqualified", refused)
    if sign == "0":
        return Discrepancy(
            evidence_id,
            "within_floor",
            None,
            None,
            "the residual is within the regime's noise floor",
        )
    return Discrepancy(
        evidence_id,
        "qualified",
        "lean" if sign == "+" else "rich",
        None,
        f"delivery error beyond the floor measured under {record.labels.get('floor_conditions')}",
    )


# ---------------------------------------------------------------------------
# 2. Investigate


def observation_of(ledger: MemoryLedger, evidence_id: str) -> Observation:
    """A fault-model observation from an engine-response evidence record."""
    record = _evidence(ledger, evidence_id)
    _require_engine_response(record, tools=_OBSERVING_TOOLS)
    feature = Feature(record.labels["residual"], record.labels["region"])
    if record.result.status == "unevaluable":
        return Observation(
            feature, None, evidence=evidence_id, unevaluable=str(record.result.reason)
        )
    sign = record.labels.get("sign")
    if _independent(record) is False:
        return Observation(
            feature, None, evidence=evidence_id, unevaluable="refused: " + INFORMED_REFUSAL
        )
    if sign not in ("+", "-", "0"):
        return Observation(
            feature,
            None,
            evidence=evidence_id,
            unevaluable="unqualified: " + record.labels.get("sign_refused", "no sign"),
        )
    return Observation(feature, sign, evidence=evidence_id)  # type: ignore[arg-type]


def drive_reports(
    ledger: MemoryLedger, model: FaultModel, evidence_ids: Iterable[str]
) -> dict[str, StructuralReport]:
    """The structural analysis per drive, on that drive's own channels.

    Channels come from each record's ``labels["fault_model_channels"]`` (what
    the minting tool found in the session); coverage is the regions that
    drive produced a qualified sign in.
    """
    channels: dict[str, set[str]] = {}
    covered: dict[str, set[str]] = {}
    for evidence_id in evidence_ids:
        record = _evidence(ledger, evidence_id)
        observation = observation_of(ledger, evidence_id)
        for source in record.sources:
            stated = record.labels.get("fault_model_channels", "")
            channels.setdefault(source.session, set()).update(c for c in stated.split(",") if c)
            regions = covered.setdefault(source.session, set())
            if observation.sign is not None:
                regions.add(observation.feature.region)
    return {
        session: structural_analysis(model, channels[session], covered_regions=covered[session])
        for session in channels
    }


@dataclass(frozen=True)
class UnresolvedPair:
    """Two open hypotheses no qualified observation has yet separated."""

    a: str
    b: str
    #: What could separate them, actionable first: the measurement of each
    #: differing residual not yet observed with a sign — why it is unavailable
    #: on the newest drive, or its own measurement when it was usable but
    #: unobserved.
    measurements: tuple[str, ...]
    #: Whether each measurement is actionable (an evaluator exists; data alone).
    actionable: tuple[bool, ...]
    #: The fault-model features each measurement would observe (E03.4 scores
    #: a measurement by what those features can tell apart).
    features: tuple[tuple[Feature, ...], ...] = ()


def unresolved_pairs(
    model: FaultModel,
    reports: Sequence[StructuralReport],
    observations: Iterable[Observation],
    open_hypotheses: Iterable[str],
) -> list[UnresolvedPair]:
    """Open pairs no qualified observation tells apart, and what could.

    A residual being *available* is not the pair being separated: a residual
    whose sign sets differ for the pair has to be observed with a sign only
    one of the two allows. ``reports`` are per drive, oldest first; the
    newest drive's exclusion explains why a residual is unavailable.
    """
    signed = [o for o in observations if o.sign is not None]
    seen = {o.feature for o in signed}
    newest = reports[-1] if reports else None
    excluded = {e.feature: e for e in newest.excluded} if newest is not None else {}
    names = [h.name for h in model.modelled if h.name in set(open_hypotheses)]
    pairs: list[UnresolvedPair] = []
    for index, a_name in enumerate(names):
        for b_name in names[index + 1 :]:
            a, b = model.hypothesis(a_name), model.hypothesis(b_name)
            if any(
                (o.sign in a.allowed(o.feature.residual, o.feature.region))
                != (o.sign in b.allowed(o.feature.residual, o.feature.region))
                for o in signed
            ):
                continue
            found: dict[str, bool] = {}
            observes: dict[str, list[Feature]] = {}
            for feature in model.features:
                if feature in seen:
                    continue
                if a.allowed(feature.residual, feature.region) == b.allowed(
                    feature.residual, feature.region
                ):
                    continue
                exclusion = excluded.get(feature)
                if exclusion is None:
                    text, actionable = model.residual(feature.residual).measurement, True
                else:
                    text, actionable = exclusion.measurement, exclusion.actionable
                found[text] = found.get(text, False) or actionable
                observes.setdefault(text, []).append(feature)
            ordered = sorted(found.items(), key=lambda item: not item[1])
            pairs.append(
                UnresolvedPair(
                    a_name,
                    b_name,
                    tuple(text for text, _ in ordered),
                    tuple(flag for _, flag in ordered),
                    tuple(tuple(observes[text]) for text, _ in ordered),
                )
            )
    return pairs


@dataclass(frozen=True)
class MeasurementChoice:
    measurement: str
    #: An evaluator exists and needs only the data (vs qualification work or a
    #: new instrument).
    actionable: bool
    #: Unresolved pairs (by hypothesis name) this measurement could separate.
    separates: tuple[tuple[str, str], ...]
    #: The fault-model features it would observe.
    features: tuple[Feature, ...] = ()


def select_measurement(pairs: Iterable[UnresolvedPair]) -> list[MeasurementChoice]:
    """Measurements that could separate unresolved pairs.

    Actionable first (data alone, an evaluator exists), then by how many
    pairs each could separate — a structural count, not a probability (E03.4
    adds information gain where likelihoods exist). Ties keep the fault
    model's order.
    """
    tally: dict[str, list[tuple[str, str]]] = {}
    actionable: dict[str, bool] = {}
    observes: dict[str, dict[Feature, None]] = {}
    for pair in pairs:
        features = pair.features or tuple(() for _ in pair.measurements)
        for text, flag, seen in zip(pair.measurements, pair.actionable, features, strict=True):
            tally.setdefault(text, []).append((pair.a, pair.b))
            actionable[text] = actionable.get(text, False) or flag
            observes.setdefault(text, {}).update(dict.fromkeys(seen))
    order = list(tally)
    ranked = sorted(
        tally.items(),
        key=lambda item: (not actionable[item[0]], -len(item[1]), order.index(item[0])),
    )
    return [
        MeasurementChoice(text, actionable[text], tuple(found), tuple(observes[text]))
        for text, found in ranked
    ]


@dataclass(frozen=True)
class InvestigationStep:
    discrepancy: Discrepancy
    #: The current investigation record id, or ``None`` when nothing was
    #: opened (no qualified discrepancy and none open).
    investigation: str | None
    #: True when an open investigation on the incident was found, not opened.
    retrieved: bool
    #: Per drive (session name): the structural analysis on its own channels.
    structural: dict[str, StructuralReport]
    consistent: tuple[str, ...]
    unresolved: tuple[UnresolvedPair, ...]
    measurements: tuple[MeasurementChoice, ...]


def _open_investigation(ledger: MemoryLedger) -> Investigation | None:
    found = [
        entry.record
        for entry in ledger.query(MemoryQuery(kinds=("investigation",), incident=DELIVERY_INCIDENT))
        if isinstance(entry.record, Investigation) and entry.record.status == "open"
    ]
    return found[-1] if found else None


def investigate(
    ledger: MemoryLedger,
    model: FaultModel,
    *,
    delivery_evidence: str,
    other_evidence: Sequence[str] = (),
    now: datetime | None = None,
) -> InvestigationStep:
    """Open, or retrieve and update, the delivery investigation.

    ``other_evidence`` are further engine-response observations (fuel-pressure
    residuals). Nothing is opened unless the delivery record is a qualified
    discrepancy; an open investigation is updated by any new occurrence.
    """
    discrepancy = detect_discrepancy(ledger, delivery_evidence)
    for evidence_id in other_evidence:
        _require_engine_response(_evidence(ledger, evidence_id), tools=_OBSERVING_TOOLS)
    existing = _open_investigation(ledger)
    ids = list(
        dict.fromkeys(
            [*(existing.observations if existing else ()), delivery_evidence, *other_evidence]
        )
    )
    ordered = sorted(ids, key=lambda ev: _evidence(ledger, ev).valid_from)
    observations = [observation_of(ledger, ev) for ev in ids]
    reports = drive_reports(ledger, model, ordered)
    consistency = evaluate_observations(model, observations)

    if existing is None and not discrepancy.qualified:
        return InvestigationStep(discrepancy, None, False, reports, consistency.consistent, (), ())

    hypotheses: list[Hypothesis] = []
    refuted: set[str] = set()
    for item in consistency.hypotheses:
        if item.conflicts:
            refuted.add(item.name)
        hypotheses.append(
            Hypothesis(
                label=model.hypothesis(item.name).label,
                status="refuted" if item.conflicts else "open",
                supporting=tuple(o.evidence for o in item.supporting if o.evidence),
                refuting=tuple(c.evidence for c in item.conflicts if c.evidence),
            )
        )
    for name in consistency.unscored:
        hypotheses.append(Hypothesis(label=model.hypothesis(name).label, status="open"))
    open_names = [h.name for h in model.modelled if h.name not in refuted]
    unresolved = unresolved_pairs(model, list(reports.values()), observations, open_names)
    choices = select_measurement(unresolved)
    unknowns = [
        f"{pair.a} and {pair.b} are not yet separated by any qualified observation"
        for pair in unresolved
    ]
    if score(model, ledger).refused:
        unknowns.append(
            "no hypothesis carries an evidence-backed likelihood, so none is ranked by probability"
        )
    unknowns.extend(
        f"{name} is language-generated and unscored until modelled" for name in consistency.unscored
    )
    for observation in consistency.unevaluable:
        unknowns.append(f"{observation.feature} unevaluable: {observation.unevaluable}")
    resolved = len(open_names) == 1
    opening = _evidence(ledger, ids[0])
    record = Investigation(
        vehicle=opening.vehicle,
        provenance=_provenance(ids, model),
        conditions=Conditions(
            regime=opening.conditions.regime,
            vehicle_state=opening.conditions.vehicle_state,
            operating_mode=DELIVERY_REGION,
        ),
        valid_from=opening.valid_from,
        title="Measured mixture differs from the ECM command at power enrichment",
        question=(
            "Which explanation of the commanded versus measured lambda disagreement at "
            "enrichment holds, and which measurement separates the rest?"
        ),
        status="resolved" if resolved else "open",
        incident=DELIVERY_INCIDENT,
        observations=tuple(ids),
        hypotheses=tuple(hypotheses),
        unknowns=tuple(unknowns),
        next_measurements=tuple(choice.measurement for choice in choices),
        narrative=(
            "Opened by the delivery investigation from B06 delivery error (engine response "
            "to the observed command, not ECU computation). Hypotheses and signatures are "
            f"the vehicle's fault model version {model.version}, single-fault reasoning: "
            + " ".join(model.assumptions)
        ),
        supersedes=() if existing is None else (existing.id,),
        supersession_reason=(
            None if existing is None else "re-evaluated with a later occurrence or observation"
        ),
    )
    stored = ledger.append(record, now=now)
    return InvestigationStep(
        discrepancy,
        stored.id,
        existing is not None,
        reports,
        consistency.consistent,
        tuple(unresolved),
        tuple(choices),
    )


# ---------------------------------------------------------------------------
# 3. Predict


def _opening(ledger: MemoryLedger, investigation_id: str) -> tuple[Investigation, Evidence]:
    investigation = ledger.get(investigation_id)
    if not isinstance(investigation, Investigation):
        raise ValueError(f"{investigation_id} is not an investigation")
    for evidence_id in investigation.observations:
        if detect_discrepancy(ledger, evidence_id).qualified:
            return investigation, _evidence(ledger, evidence_id)
    raise ValueError(f"{investigation_id} holds no qualified discrepancy to predict from")


def predict_recurrence(
    ledger: MemoryLedger,
    investigation_id: str,
    *,
    floor_path: Path,
    floor_conditions: str,
    now: datetime | None = None,
) -> str:
    """The discrepancy recurs on the next independent drive that reaches enrichment.

    Stated under the floor's own regime with the floor (so its outcome can be
    ``no_effect`` or ``measured_change``); no magnitude, because one drive
    does not support one.
    """
    _, opening = _opening(ledger, investigation_id)
    direction: Literal["increase", "decrease"] = (
        "increase" if opening.labels.get("sign") == "+" else "decrease"
    )
    floor = floor_reference(floor_path, DELIVERY_QUANTITY, conditions=floor_conditions)
    prediction = Prediction(
        vehicle=opening.vehicle,
        provenance=_provenance([opening.id]),
        conditions=Conditions(
            regime=floor_conditions, vehicle_state="engine_running", operating_mode=DELIVERY_REGION
        ),
        valid_from=opening.valid_to or opening.valid_from,
        investigation=investigation_id,
        intervention=None,
        quantity=DELIVERY_QUANTITY,
        direction=direction,
        magnitude=None,
        noise_floor=floor,
    )
    return ledger.append(prediction, now=now).id


def predict_pressure_flow_loss(
    ledger: MemoryLedger,
    investigation_id: str,
    *,
    floor_path: Path,
    floor_conditions: str,
    now: datetime | None = None,
) -> str:
    """If delivery sag explains the lean, droop at enrichment loses that much flow.

    Magnitude: the observed lean itself (injector flow lost to droop, square-
    root law, is what sag claims). Judged against the delivery error's
    condition-matched floor: the question is whether pressure explains a lean
    the delivery residual can detect, and the pressure evidence's own sign is
    qualified on the same threshold with its measurement margin.
    """
    _, opening = _opening(ledger, investigation_id)
    residual = opening.result.residual
    if residual is None or opening.labels.get("sign") != "+":
        raise ValueError("a flow-loss magnitude follows only from a qualified lean delivery error")
    floor = floor_reference(floor_path, DELIVERY_QUANTITY, conditions=floor_conditions)
    prediction = Prediction(
        vehicle=opening.vehicle,
        provenance=_provenance([opening.id]),
        conditions=Conditions(
            regime=floor_conditions, vehicle_state="engine_running", operating_mode=DELIVERY_REGION
        ),
        valid_from=opening.valid_to or opening.valid_from,
        investigation=investigation_id,
        intervention=None,
        quantity=FLOW_LOSS_QUANTITY,
        direction="increase",
        magnitude=Quantity(value=residual.value, unit="percent"),
        noise_floor=floor,
    )
    return ledger.append(prediction, now=now).id


# ---------------------------------------------------------------------------
# 4. Verify


def _require_next_drive(ledger: MemoryLedger, prediction: Prediction, record: Evidence) -> None:
    """The verifying record is from another session, later than the data the
    prediction was made from."""
    basis = [_evidence(ledger, ev) for ev in prediction.provenance.derived_from]
    sessions = {s.session for ev in basis for s in ev.sources}
    reused = sessions & {s.session for s in record.sources}
    if reused:
        raise NotNextDriveError(
            f"{record.id} reads {sorted(reused)}, the drive the prediction was made from; "
            "an outcome is measured on the next independent drive"
        )
    ends = [ev.valid_to or ev.valid_from for ev in basis]
    if ends and record.valid_from <= max(ends):
        raise NotNextDriveError(
            f"{record.id} starts {record.valid_from.isoformat()}, not after the prediction's "
            f"data ends {max(ends).isoformat()}: it is not the next drive"
        )


def evaluate_outcome(
    ledger: MemoryLedger,
    prediction_id: str,
    evidence_id: str,
    *,
    now: datetime | None = None,
) -> str:
    """The outcome of ``prediction_id`` on ``evidence_id``; returns the outcome id.

    Refuses (:class:`NotNextDriveError`) a record from the prediction's own
    drive or an earlier one. Otherwise every non-qualified record is
    ``unresolvable`` with its reason, never a zero:

    * unevaluable — its own reason (``missing_input``, ``stale_input``,
      ``uncovered``);
    * a drive that informed the enrichment constants, or a residual whose sign
      the tool refused — ``refused`` with that text;
    * another region than the prediction's — ``uncovered``.
    """
    prediction = ledger.get(prediction_id)
    if not isinstance(prediction, Prediction):
        raise ValueError(f"{prediction_id} is a {prediction.kind} record, not a prediction")
    record = _evidence(ledger, evidence_id)
    _require_engine_response(record, tools=_OBSERVING_TOOLS)
    if record.labels.get("quantity") != prediction.quantity:
        raise ValueError(
            f"{evidence_id} measures {record.labels.get('quantity')}; the prediction is "
            f"about {prediction.quantity}"
        )
    _require_next_drive(ledger, prediction, record)
    region = record.labels.get("region")
    result: Result
    disposition: Literal["no_effect", "unresolvable", "measured_change"] = "unresolvable"
    if record.result.status == "unevaluable":
        result = Result(
            status="unevaluable", reason=record.result.reason, detail=record.result.detail
        )
    elif _independent(record) is not True:
        result = Result(status="unevaluable", reason="refused", detail=INFORMED_REFUSAL)
    elif region != prediction.conditions.operating_mode:
        result = Result(
            status="unevaluable",
            reason="uncovered",
            detail=(
                f"{evidence_id} measured region {region}; the prediction is for "
                f"{prediction.conditions.operating_mode}, which this data did not cover"
            ),
        )
    elif record.labels.get("sign") not in ("+", "-", "0"):
        result = Result(
            status="unevaluable",
            reason="refused",
            detail="the residual's sign was not qualified: "
            + record.labels.get("sign_refused", "no reason recorded"),
        )
    else:
        residual = record.result.residual
        floor = prediction.noise_floor
        if floor is None or residual is None:
            result = Result(
                status="evaluated",
                residual=residual,
                detail="no condition-matched noise floor: a change cannot be told from noise",
            )
        else:
            disposition = (
                "measured_change" if abs(residual.value) >= floor.detectable else "no_effect"
            )
            result = Result(status="evaluated", residual=residual)
    outcome = Outcome(
        vehicle=prediction.vehicle,
        provenance=_provenance([evidence_id]),
        conditions=prediction.conditions,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
        prediction=prediction_id,
        intervention=None,
        disposition=disposition,
        evidence=(evidence_id,),
        result=result,
    )
    return ledger.append(outcome, now=now).id


# ---------------------------------------------------------------------------
# 5. Remember


@dataclass(frozen=True)
class Retrieval:
    """What memory holds for a new delivery occurrence."""

    evidence: str
    #: Current investigations on the incident (newest last).
    investigations: tuple[str, ...]
    #: Every current occurrence of the incident, in valid-time order.
    occurrences: tuple[str, ...]


def retrieve(ledger: MemoryLedger, evidence_id: str) -> Retrieval:
    """Earlier investigations and occurrences for a new delivery evidence record."""
    record = _evidence(ledger, evidence_id)
    _require_engine_response(record, tools=frozenset({DELIVERY_TOOL}))
    investigations = tuple(
        entry.record.id
        for entry in ledger.query(MemoryQuery(kinds=("investigation",), incident=record.incident))
    )
    occurrences = tuple(entry.record.id for entry in ledger.recurrences(evidence_id))
    return Retrieval(evidence_id, investigations, occurrences)
