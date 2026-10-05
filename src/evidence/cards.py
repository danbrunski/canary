"""Deterministic investigation cards: evidence rendered without a model (E01.5).

:func:`investigation_card` reads one investigation and everything it cites
from the vehicle's memory ledger and builds an :class:`InvestigationCard`:
what happened, the evidence for and against each hypothesis, the
alternatives still open, what is unknown, and the next measurement. No
language model, no network: a fixed template over resolved records, so the
same ledger always renders the same card. This is the one representation the
HTTP/MCP/dashboard surfaces expose (E04 owns that integration); a JSON client
reads :meth:`InvestigationCard.model_dump`, a terminal :func:`render_text`.

What the template makes impossible rather than documented:

* **Every number comes from a record.** A card holds numbers only as
  :class:`CardValue` objects copied from a resolved record, each naming the
  record id it came from. Investigation prose cannot state numbers
  (``src.evidence.records.Investigation``); free ``narrative`` is shown under
  :data:`NARRATIVE_NOTICE`, never as a finding.
* **Unavailable is never normal.** Unevaluable evidence renders its named
  reason and detail and no value; a card whose evidence is all unevaluable, or
  absent, says so in :attr:`InvestigationCard.evidence_state`, which has no
  "normal" or "ok" value at all.
* **A what-if is labelled where it is shown.** A computed command-change
  prediction renders as a :class:`WhatIfView` under :data:`WHAT_IF_NOTICE`,
  in the predictions section, never among the evidence (E02.6).
* **Recurrence and retraction are visible.** Each evidence view carries its
  standing (current / superseded / retracted, with the retraction's reason)
  and how many occurrences of the same thing the ledger holds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from src.evidence.ledger import LedgerEntry, MemoryLedger, MemoryReferenceError
from src.evidence.records import (
    Evidence,
    Investigation,
    Outcome,
    Prediction,
    Quantity,
    Retraction,
    WhatIf,
    record_to_dict,
)

__all__ = [
    "CARD_TEMPLATE",
    "NARRATIVE_NOTICE",
    "CardValue",
    "EvidenceView",
    "HypothesisView",
    "InvestigationCard",
    "OutcomeView",
    "PredictionView",
    "WHAT_IF_NOTICE",
    "WhatIfPointView",
    "WhatIfView",
    "evidence_payload",
    "investigation_card",
    "render_text",
]

CARD_TEMPLATE: Literal["canary-investigation-card/1"] = "canary-investigation-card/1"

NARRATIVE_NOTICE = (
    "Narrative: free prose recorded with the investigation. Not evidence; any "
    "number in it is not a finding until a tool mints a record for it."
)

WHAT_IF_NOTICE = (
    "What-if: computed by reproducing the ECU's logic on two calibrations. Not a "
    "measurement and not an outcome; nothing was read from the vehicle and no ECU "
    "write was requested."
)

Standing = Literal["current", "superseded", "retracted"]


class _Card(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CardValue(_Card):
    """One number on a card, with the record it was read from."""

    name: str
    value: float
    unit: str
    uncertainty: float | None
    record_id: str

    @classmethod
    def of(cls, name: str, quantity: Quantity, record_id: str) -> CardValue:
        return cls(
            name=name,
            value=quantity.value,
            unit=quantity.unit,
            uncertainty=quantity.uncertainty,
            record_id=record_id,
        )


class SourceView(_Card):
    session: str
    window_start: datetime
    window_end: datetime
    channels: tuple[str, ...]
    n_samples: int


class EvidenceView(_Card):
    """One cited evidence record as the card shows it."""

    id: str
    tool: str
    tool_version: str
    subject: str
    regime: str
    status: Literal["evaluated", "unevaluable"]
    #: Unevaluable only: the named reason and the tool's own words.
    reason: str | None
    detail: str
    values: tuple[CardValue, ...]
    residual: CardValue | None
    labels: dict[str, str]
    n_observations: int
    flags: tuple[str, ...]
    sources: tuple[SourceView, ...]
    valid_from: datetime
    valid_to: datetime | None
    recorded_at: datetime
    standing: Standing
    superseded_by: tuple[str, ...]
    retracted_by: str | None
    retraction_reason: str | None
    #: Current occurrences of the same thing on the ledger (this one included).
    occurrences: int
    recurrence: str | None


class HypothesisView(_Card):
    label: str
    status: str
    supporting: tuple[EvidenceView, ...]
    refuting: tuple[EvidenceView, ...]
    #: Current evaluated evidence on both sides.
    conflicting: bool


class OutcomeView(_Card):
    id: str
    disposition: str
    verdict: str
    order: str
    reason: str
    residual: CardValue | None
    evidence: tuple[str, ...]
    standing: Standing
    superseded_by: tuple[str, ...]
    retracted_by: str | None
    retraction_reason: str | None


class WhatIfPointView(_Card):
    key: str
    inputs: tuple[CardValue, ...]
    before: CardValue | None
    after: CardValue | None
    change: CardValue | None
    reason: str | None


class WhatIfView(_Card):
    """A computed command-change prediction as the card shows it: labelled,
    with every value naming the prediction record it was read from."""

    label: Literal["what_if"] = "what_if"
    notice: str = WHAT_IF_NOTICE
    output: str
    base_image_sha256: str
    target_image_sha256: str | None
    change_set_sha256: str | None
    assumptions: tuple[str, ...]
    blockers: tuple[str, ...]
    not_reproduced: tuple[str, ...]
    points: tuple[WhatIfPointView, ...]
    #: The flash it is ordered against (``MemoryLedger.what_if_intervention``)
    #: and the derived order, or ``None`` with ``order_note`` saying why.
    ordered_against: str | None = None
    order: str | None = None
    order_note: str = ""

    @classmethod
    def of(
        cls,
        what_if: WhatIf,
        record_id: str,
        *,
        ordered_against: str | None = None,
        order: str | None = None,
    ) -> WhatIfView:
        points = []
        for point in what_if.points:
            change = point.change
            unit = point.before.unit if point.before is not None else "1"
            points.append(
                WhatIfPointView(
                    key=point.key,
                    inputs=tuple(
                        CardValue.of(name, quantity, record_id)
                        for name, quantity in point.inputs.items()
                    ),
                    before=None
                    if point.before is None
                    else CardValue.of("before", point.before, record_id),
                    after=None
                    if point.after is None
                    else CardValue.of("after", point.after, record_id),
                    change=None
                    if change is None
                    else CardValue(
                        name="after - before",
                        value=change,
                        unit=unit,
                        uncertainty=None,
                        record_id=record_id,
                    ),
                    reason=point.reason,
                )
            )
        return cls(
            output=what_if.output,
            base_image_sha256=what_if.base_image_sha256,
            target_image_sha256=what_if.target_image_sha256,
            change_set_sha256=what_if.change_set_sha256,
            assumptions=what_if.assumptions,
            blockers=what_if.blockers,
            not_reproduced=what_if.not_reproduced,
            points=tuple(points),
            ordered_against=ordered_against,
            order=order,
            order_note=_order_note(what_if, ordered_against, order),
        )


def _order_note(what_if: WhatIf, against: str | None, order: str | None) -> str:
    if against is not None:
        return f"ordered against flash {against}: {order}"
    if what_if.target_image_sha256 is None:
        return (
            "not orderable: a cell edit set names no image, so it is ordered only against "
            "a flash named when it was recorded"
        )
    return "no flash of the target image after this prediction is on record yet"


class PredictionView(_Card):
    id: str
    quantity: str
    direction: str
    regime: str
    magnitude: CardValue | None
    #: The condition-matched floor and the smallest change it can see.
    noise_floor: CardValue | None
    detectable: CardValue | None
    intervention: str | None
    #: ``prior`` / ``post_hoc`` against the intervention; ``None`` without one.
    order: str | None
    recorded_at: datetime
    outcomes: tuple[OutcomeView, ...]
    #: A computed command-change prediction's detail (E02.6), else ``None``.
    what_if: WhatIfView | None
    #: A withdrawn prediction stays on the card, labelled, never dropped.
    standing: Standing
    superseded_by: tuple[str, ...]
    retracted_by: str | None
    retraction_reason: str | None


EvidenceState = Literal[
    "no_evidence", "unevaluable", "retracted", "conflicting", "partially_evaluable", "evaluated"
]


class InvestigationCard(_Card):
    """The deterministic card for one investigation (see module docstring)."""

    template: Literal["canary-investigation-card/1"] = CARD_TEMPLATE
    investigation: str
    vehicle: str
    vin: str | None
    program_id: str | None
    calibration: str | None
    title: str
    question: str
    status: str
    regime: str
    incident: str | None
    valid_from: datetime
    valid_to: datetime | None
    recorded_at: datetime
    #: The record time this card was rendered as of (``None``: now).
    as_of: datetime | None
    standing: Standing
    superseded_by: tuple[str, ...]
    #: No "normal" value exists: absence and unevaluability are stated.
    evidence_state: EvidenceState
    what_happened: tuple[EvidenceView, ...]
    hypotheses: tuple[HypothesisView, ...]
    alternatives: tuple[str, ...]
    conflicts: tuple[str, ...]
    retractions: tuple[str, ...]
    predictions: tuple[PredictionView, ...]
    unknowns: tuple[str, ...]
    next_measurements: tuple[str, ...]
    narrative: str | None
    narrative_notice: str | None


# -- building -------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def evidence_payload(entry: LedgerEntry) -> dict[str, Any]:
    """What a renderer needs from one resolved evidence record (LLM-assist
    cards render the same payload; E01.2)."""
    record = entry.record
    assert isinstance(record, Evidence)
    full = record_to_dict(record)
    return {
        "id": record.id,
        "tool": record.provenance.producer,
        "tool_version": record.provenance.version,
        "subject": record.subject,
        "conditions": full["conditions"],
        "valid_from": full["valid_from"],
        "valid_to": full["valid_to"],
        "recorded_at": full["recorded_at"],
        "sources": full["sources"],
        "values": full["values"],
        "labels": full["labels"],
        "quality": full["quality"],
        "result": full["result"],
        "superseded_at": _iso(entry.superseded_at),
        "superseded_by": list(entry.superseded_by),
        "retracted_by": entry.retracted_by,
    }


def _standing(entry: LedgerEntry) -> Standing:
    if entry.retracted_by is not None:
        return "retracted"
    return "current" if entry.current else "superseded"


class _Builder:
    def __init__(self, ledger: MemoryLedger, as_of: datetime | None) -> None:
        self.ledger = ledger
        self.as_of = as_of
        self._views: dict[str, EvidenceView] = {}

    def entry(self, record_id: str) -> LedgerEntry:
        return self.ledger.entry(record_id, as_of=self.as_of)

    def evidence(self, evidence_id: str) -> EvidenceView:
        cached = self._views.get(evidence_id)
        if cached is not None:
            return cached
        entry = self.entry(evidence_id)
        record = entry.record
        if not isinstance(record, Evidence):
            raise MemoryReferenceError(f"{evidence_id!r} is a {record.kind} record, not evidence")
        assert record.recorded_at is not None
        retraction_reason = self.retraction_reason(entry)
        occurrences = self.ledger.recurrences(evidence_id, as_of=self.as_of)
        view = EvidenceView(
            id=record.id,
            tool=record.provenance.producer,
            tool_version=record.provenance.version,
            subject=record.subject,
            regime=record.conditions.regime,
            status=record.result.status,
            reason=record.result.reason,
            detail=record.result.detail,
            values=tuple(
                CardValue.of(name, quantity, record.id) for name, quantity in record.values.items()
            ),
            residual=None
            if record.result.residual is None
            else CardValue.of("residual", record.result.residual, record.id),
            labels=dict(record.labels),
            n_observations=record.quality.n_observations,
            flags=record.quality.flags,
            sources=tuple(
                SourceView(
                    session=source.session,
                    window_start=source.window_start,
                    window_end=source.window_end,
                    channels=source.channels,
                    n_samples=source.n_samples,
                )
                for source in record.sources
            ),
            valid_from=record.valid_from,
            valid_to=record.valid_to,
            recorded_at=record.recorded_at,
            standing=_standing(entry),
            superseded_by=entry.superseded_by,
            retracted_by=entry.retracted_by,
            retraction_reason=retraction_reason,
            occurrences=max(len(occurrences), 1),
            recurrence=record.incident or None,
        )
        self._views[evidence_id] = view
        return view

    def retraction_reason(self, entry: LedgerEntry) -> str | None:
        if entry.retracted_by is None:
            return None
        retraction = self.entry(entry.retracted_by).record
        assert isinstance(retraction, Retraction)
        return retraction.reason

    def predictions(self, investigation_id: str) -> tuple[PredictionView, ...]:
        """Every prediction made for the investigation, withdrawn ones included."""
        predictions = [
            entry
            for entry in self.ledger.query(
                kinds=("prediction",), as_of=self.as_of, include_superseded=True
            )
            if isinstance(entry.record, Prediction)
            and entry.record.investigation == investigation_id
        ]
        outcomes = [
            entry
            for entry in self.ledger.query(
                kinds=("outcome",), as_of=self.as_of, include_superseded=True
            )
            if isinstance(entry.record, Outcome)
        ]
        views: list[PredictionView] = []
        for entry in predictions:
            prediction = entry.record
            assert isinstance(prediction, Prediction) and prediction.recorded_at is not None
            floor = prediction.noise_floor
            against = prediction.intervention
            order = self.ledger.prediction_order(prediction.id)
            if prediction.what_if is not None:
                against = self.ledger.what_if_intervention(prediction.id)
                order = (
                    None
                    if against is None
                    else self.ledger.prediction_order(prediction.id, intervention=against)
                )
            outcome_views = []
            for outcome_entry in outcomes:
                outcome = outcome_entry.record
                assert isinstance(outcome, Outcome)
                if outcome.prediction != prediction.id:
                    continue
                assessment = self.ledger.assess(outcome.id)
                outcome_views.append(
                    OutcomeView(
                        id=outcome.id,
                        disposition=outcome.disposition,
                        verdict=assessment.verdict,
                        order=assessment.order,
                        reason=assessment.reason,
                        residual=None
                        if outcome.result.residual is None
                        else CardValue.of("residual", outcome.result.residual, outcome.id),
                        evidence=outcome.evidence,
                        standing=_standing(outcome_entry),
                        superseded_by=outcome_entry.superseded_by,
                        retracted_by=outcome_entry.retracted_by,
                        retraction_reason=self.retraction_reason(outcome_entry),
                    )
                )
            views.append(
                PredictionView(
                    id=prediction.id,
                    quantity=prediction.quantity,
                    direction=prediction.direction,
                    regime=prediction.conditions.regime,
                    magnitude=None
                    if prediction.magnitude is None
                    else CardValue.of("magnitude", prediction.magnitude, prediction.id),
                    noise_floor=None
                    if floor is None
                    else CardValue.of(
                        f"noise floor sd ({floor.quantity})", floor.sd, prediction.id
                    ),
                    detectable=None
                    if floor is None
                    else CardValue(
                        name="smallest distinguishable change",
                        value=floor.detectable,
                        unit=floor.sd.unit,
                        uncertainty=None,
                        record_id=prediction.id,
                    ),
                    intervention=against,
                    order=order,
                    recorded_at=prediction.recorded_at,
                    outcomes=tuple(outcome_views),
                    what_if=None
                    if prediction.what_if is None
                    else WhatIfView.of(
                        prediction.what_if, prediction.id, ordered_against=against, order=order
                    ),
                    standing=_standing(entry),
                    superseded_by=entry.superseded_by,
                    retracted_by=entry.retracted_by,
                    retraction_reason=self.retraction_reason(entry),
                )
            )
        return tuple(views)


_NEXT_FOR_REASON = {
    "missing_input": "capture {channels}: never captured in {sessions} ({detail})",
    "stale_input": "re-capture {channels} fresh: stale in {sessions} ({detail})",
    "sentinel": "collect {channels} outside the sentinel state ({detail})",
    "unknown_context": "establish the operating context for {subject} ({detail})",
    "uncovered": "collect data in the regime {subject} does not yet cover ({detail})",
    "unvalidated": "validate the model behind {subject} before relying on it ({detail})",
    "refused": "re-measure so {subject} can be evaluated ({detail})",
}


def _evidence_state(views: list[EvidenceView], conflicts: list[str]) -> EvidenceState:
    if not views:
        return "no_evidence"
    standing = [view for view in views if view.standing == "current"]
    if not standing:
        return "retracted"
    if conflicts:
        return "conflicting"
    evaluated = [view for view in standing if view.status == "evaluated"]
    if not evaluated:
        return "unevaluable"
    if len(evaluated) < len(views):
        return "partially_evaluable"
    return "evaluated"


def investigation_card(
    ledger: MemoryLedger, investigation_id: str, *, as_of: datetime | None = None
) -> InvestigationCard:
    """The card for ``investigation_id``, as the ledger stands (or stood at
    ``as_of``). Raises :class:`~src.evidence.ledger.MemoryReferenceError` for
    an id that is not an investigation on this vehicle's ledger."""
    builder = _Builder(ledger, as_of)
    entry = builder.entry(investigation_id)
    record = entry.record
    if not isinstance(record, Investigation):
        raise MemoryReferenceError(
            f"{investigation_id!r} is a {record.kind} record, not an investigation"
        )
    assert record.recorded_at is not None
    observations = [builder.evidence(ev) for ev in record.observations]
    hypotheses: list[HypothesisView] = []
    conflicts: list[str] = []
    for hypothesis in record.hypotheses:
        supporting = tuple(builder.evidence(ev) for ev in hypothesis.supporting)
        refuting = tuple(builder.evidence(ev) for ev in hypothesis.refuting)

        def live(views: tuple[EvidenceView, ...]) -> list[EvidenceView]:
            return [v for v in views if v.standing == "current" and v.status == "evaluated"]

        conflicting = bool(live(supporting)) and bool(live(refuting))
        if conflicting:
            conflicts.append(
                f"'{hypothesis.label}': supported by "
                f"{', '.join(v.id for v in live(supporting))} and refuted by "
                f"{', '.join(v.id for v in live(refuting))}"
            )
        if hypothesis.status == "supported" and not live(supporting):
            conflicts.append(
                f"'{hypothesis.label}' is marked supported, but none of its supporting "
                "evidence is current and evaluated"
            )
        hypotheses.append(
            HypothesisView(
                label=hypothesis.label,
                status=hypothesis.status,
                supporting=supporting,
                refuting=refuting,
                conflicting=conflicting,
            )
        )
    every = {view.id: view for view in observations}
    for hypothesis_view in hypotheses:
        for view in (*hypothesis_view.supporting, *hypothesis_view.refuting):
            every.setdefault(view.id, view)
    views = list(every.values())

    unknowns: list[str] = list(record.unknowns)
    if record.vehicle.calibration is None:
        unknowns.append("calibration in force: unknown")
    if record.vehicle.program_id is None:
        unknowns.append("ECU program: unknown")
    next_measurements: list[str] = list(record.next_measurements)
    retractions: list[str] = []
    for view in views:
        if view.standing == "retracted":
            retractions.append(
                f"{view.id} ({view.subject}) retracted by {view.retracted_by}: "
                f"{view.retraction_reason}"
            )
        elif view.standing == "superseded":
            retractions.append(
                f"{view.id} ({view.subject}) superseded by {', '.join(view.superseded_by)}"
            )
        if view.status == "unevaluable" and view.standing == "current":
            unknowns.append(f"{view.subject}: unevaluable ({view.reason}): {view.detail}")
            template = _NEXT_FOR_REASON.get(view.reason or "", "")
            if template:
                channels = sorted({c for s in view.sources for c in s.channels})
                next_measurements.append(
                    template.format(
                        channels=", ".join(channels),
                        sessions=", ".join(s.session for s in view.sources),
                        subject=view.subject,
                        detail=view.detail,
                    )
                )
    for hypothesis_view in hypotheses:
        if not hypothesis_view.supporting and not hypothesis_view.refuting:
            unknowns.append(f"no evidence yet for or against '{hypothesis_view.label}'")
            next_measurements.append(f"measure to test '{hypothesis_view.label}'")
    if not views:
        next_measurements.append(
            "record the observation as evidence: no tool has minted any for this investigation"
        )
    leading = {h.label for h in hypotheses if h.status == "supported"}
    alternatives = tuple(
        f"{h.label} ({h.status})"
        for h in hypotheses
        if h.label not in leading and h.status in ("open", "unresolvable")
    )
    predictions = builder.predictions(record.id)
    withdrawn: list[tuple[str, PredictionView | OutcomeView]] = [
        ("prediction", view) for view in predictions
    ]
    withdrawn.extend(("outcome", o) for view in predictions for o in view.outcomes)
    for kind, item in withdrawn:
        if item.standing == "retracted":
            retractions.append(
                f"{kind} {item.id} retracted by {item.retracted_by}: {item.retraction_reason}"
            )
        elif item.standing == "superseded":
            retractions.append(f"{kind} {item.id} superseded by {', '.join(item.superseded_by)}")
    return InvestigationCard(
        investigation=record.id,
        vehicle=record.vehicle.profile,
        vin=record.vehicle.vin,
        program_id=record.vehicle.program_id,
        calibration=record.vehicle.calibration,
        title=record.title,
        question=record.question,
        status=record.status,
        regime=record.conditions.regime,
        incident=record.incident,
        valid_from=record.valid_from,
        valid_to=record.valid_to,
        recorded_at=record.recorded_at,
        as_of=as_of,
        standing=_standing(entry),
        superseded_by=entry.superseded_by,
        evidence_state=_evidence_state(views, conflicts),
        what_happened=tuple(observations),
        hypotheses=tuple(hypotheses),
        alternatives=alternatives,
        conflicts=tuple(conflicts),
        retractions=tuple(retractions),
        predictions=predictions,
        unknowns=tuple(dict.fromkeys(unknowns)),
        next_measurements=tuple(dict.fromkeys(next_measurements)),
        narrative=record.narrative or None,
        narrative_notice=NARRATIVE_NOTICE if record.narrative else None,
    )


# -- text -----------------------------------------------------------------------

_STATE_TEXT: dict[str, str] = {
    "no_evidence": "NO EVIDENCE: nothing has been measured for this investigation",
    "unevaluable": "UNEVALUABLE: every cited measurement could not be evaluated",
    "retracted": "WITHDRAWN: every cited measurement has been retracted or superseded",
    "conflicting": "CONFLICTING: current evidence points both ways",
    "partially_evaluable": "PARTIAL: some cited evidence is unevaluable or withdrawn",
    "evaluated": "EVALUATED: every cited measurement was evaluated",
}


def _value(value: CardValue) -> str:
    spread = "" if value.uncertainty is None else f" ± {value.uncertainty:.6g}"
    return f"{value.name} = {value.value:.6g}{spread} {value.unit}  [{value.record_id}]"


def _withdrawn(
    standing: Standing,
    superseded_by: tuple[str, ...],
    retracted_by: str | None,
    reason: str | None,
) -> str:
    if standing == "retracted":
        return f"  RETRACTED by {retracted_by}: {reason}"
    if standing == "superseded":
        return f"  SUPERSEDED by {', '.join(superseded_by)}"
    return ""


def _evidence_lines(view: EvidenceView, indent: str) -> list[str]:
    standing = {
        "current": "",
        "superseded": f"  SUPERSEDED by {', '.join(view.superseded_by)}",
        "retracted": f"  RETRACTED by {view.retracted_by}: {view.retraction_reason}",
    }[view.standing]
    lines = [f"{indent}{view.id}  {view.tool} v{view.tool_version}  {view.subject}{standing}"]
    for source in view.sources:
        lines.append(
            f"{indent}  read {source.session} {source.window_start.isoformat()} .. "
            f"{source.window_end.isoformat()} ({', '.join(source.channels)}; "
            f"{source.n_samples} samples)"
        )
    lines.append(f"{indent}  conditions: {view.regime}")
    if view.status == "unevaluable":
        lines.append(f"{indent}  UNEVALUABLE ({view.reason}): {view.detail}  — not a normal result")
    else:
        lines.extend(f"{indent}  {_value(value)}" for value in view.values)
        if view.residual is not None:
            lines.append(f"{indent}  {_value(view.residual)}")
        lines.extend(f"{indent}  {name}: {label}" for name, label in view.labels.items())
    if view.occurrences > 1:
        lines.append(f"{indent}  RECURRENCE: {view.occurrences} occurrences on record")
    return lines


def _what_if_lines(view: WhatIfView) -> list[str]:
    change = (
        f"target image {view.target_image_sha256}"
        if view.target_image_sha256 is not None
        else f"cell edit set {view.change_set_sha256}"
    )
    lines = [
        f"    WHAT-IF ({view.notice})",
        f"      {view.output}: base image {view.base_image_sha256} -> {change}",
    ]
    lines.extend(f"      assumes: {item}" for item in view.assumptions)
    lines.append(f"      blockers: {', '.join(view.blockers) or 'none'}")
    lines.append(f"      not reproduced: {'; '.join(view.not_reproduced) or 'none'}")
    lines.append(f"      order: {view.order_note}")
    for point in view.points:
        inputs = ", ".join(f"{v.name} {v.value:.6g} {v.unit}" for v in point.inputs)
        lines.append(f"      {point.key} at {inputs}:")
        if point.before is None or point.after is None or point.change is None:
            lines.append(f"        no value ({point.reason})")
        else:
            lines.extend(f"        {_value(v)}" for v in (point.before, point.after, point.change))
    return lines


def render_text(card: InvestigationCard) -> str:
    """The card as fixed-format plain text (deterministic)."""
    unknown = "unknown"
    lines = [
        f"INVESTIGATION {card.investigation}: {card.title}",
        f"  question: {card.question}",
        f"  status: {card.status}; vehicle {card.vehicle}; calibration "
        f"{card.calibration or unknown}; program {card.program_id or unknown}",
        f"  conditions: {card.regime}",
        f"  valid from {card.valid_from.isoformat()}"
        + (f" to {card.valid_to.isoformat()}" if card.valid_to else "")
        + f"; recorded {card.recorded_at.isoformat()}",
    ]
    if card.as_of is not None:
        lines.append(f"  as believed at {card.as_of.isoformat()}")
    if card.standing != "current":
        lines.append(f"  THIS INVESTIGATION IS {card.standing.upper()}")
    if card.incident:
        lines.append(f"  incident: {card.incident}")
    lines.append(f"  {_STATE_TEXT[card.evidence_state]}")
    lines.append("WHAT HAPPENED")
    if not card.what_happened:
        lines.append("  no observation evidence recorded")
    for view in card.what_happened:
        lines.extend(_evidence_lines(view, "  "))
    lines.append("HYPOTHESES")
    for hypothesis in card.hypotheses:
        flag = "  CONFLICTING" if hypothesis.conflicting else ""
        lines.append(f"  {hypothesis.label} [{hypothesis.status}]{flag}")
        for title, views in (
            ("supporting", hypothesis.supporting),
            ("refuting", hypothesis.refuting),
        ):
            if views:
                lines.append(f"    {title}:")
                for view in views:
                    lines.extend(_evidence_lines(view, "      "))
            else:
                lines.append(f"    {title}: none")
    for heading, items in (
        ("ALTERNATIVES", card.alternatives),
        ("CONFLICTS", card.conflicts),
        ("RETRACTED OR SUPERSEDED EVIDENCE", card.retractions),
    ):
        if items:
            lines.append(heading)
            lines.extend(f"  - {item}" for item in items)
    if card.predictions:
        lines.append("PREDICTIONS")
    for prediction in card.predictions:
        order = f"; {prediction.order.upper()}" if prediction.order else ""
        lines.append(
            f"  {prediction.id}: {prediction.quantity} {prediction.direction} under "
            f"{prediction.regime} (recorded {prediction.recorded_at.isoformat()}{order})"
            + _withdrawn(
                prediction.standing,
                prediction.superseded_by,
                prediction.retracted_by,
                prediction.retraction_reason,
            )
        )
        for value in (prediction.magnitude, prediction.noise_floor, prediction.detectable):
            if value is not None:
                lines.append(f"    {_value(value)}")
        if prediction.noise_floor is None:
            lines.append("    no condition-matched noise floor: a null cannot be told from noise")
        if prediction.what_if is not None:
            lines.extend(_what_if_lines(prediction.what_if))
        for outcome in prediction.outcomes:
            lines.append(
                f"    outcome {outcome.id}: {outcome.disposition} -> {outcome.verdict.upper()} "
                f"({outcome.reason})"
                + _withdrawn(
                    outcome.standing,
                    outcome.superseded_by,
                    outcome.retracted_by,
                    outcome.retraction_reason,
                )
            )
            if outcome.residual is not None:
                lines.append(f"      {_value(outcome.residual)}")
    lines.append("UNKNOWNS")
    lines.extend(f"  - {item}" for item in card.unknowns or ("none stated",))
    lines.append("NEXT MEASUREMENT")
    lines.extend(f"  - {item}" for item in card.next_measurements or ("none proposed",))
    if card.narrative:
        lines.append("NARRATIVE")
        lines.append(f"  ({card.narrative_notice})")
        lines.append(f"  {card.narrative}")
    return "\n".join(lines) + "\n"
