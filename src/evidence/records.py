"""Record contracts for investigation memory (E01.1, docs/roadmap/tasks/E01).

Seven record kinds share one envelope (:class:`MemoryRecord`):

* :class:`Evidence` — what an analysis tool computed, minted by that tool
  (E01.2): tool and code version, the session/window/sample references it
  read, hashes of its inputs and artifacts, its evaluation policy and the
  values it produced, each with a unit.
* :class:`Investigation` — an observation, the hypotheses about it and the
  evidence for and against each, by evidence id.
* :class:`Prediction` — an expected direction (and magnitude, when the
  evidence supports one) beside the noise floor it must clear.
* :class:`Intervention` — a calibration, hardware, fuel or instrumentation
  change. It points at the record that already holds the fact
  (``tune_revisions.yaml`` for a flash, the vehicle ledger for a physical
  change) rather than restating it.
* :class:`Outcome` — what was measured after an intervention, against a
  prediction.
* :class:`Reference` — a validated reference (an accepted baseline, a
  behavior model) and the investigation that justified it (E01.4). An
  intervention whose ``affects`` names its channels retires it as historical
  without deleting it.
* :class:`Retraction` — withdraws records without replacing them.

What the envelope makes impossible rather than documented:

* **Vehicle and program identity are required.** ``program_id`` and
  ``calibration`` have no default: a producer states them, or states ``None``
  (unknown), and an unknown is never filled in by guess. Program and
  calibration are separate because a reflash changes the calibration, not the
  memory map (AGENTS.md, "RAM channels").
* **Conditions and provenance are required.** A number without its regime
  is how the WOT noise floor got quoted against idle; ``conditions.regime``
  follows ``noise_floor.yaml``'s rule (matched character for character).
* **Two times.** ``valid_from``/``valid_to`` say when the thing was true;
  ``recorded_at`` says when CANary recorded it. ``recorded_at`` and ``id`` are
  minted by :class:`~src.evidence.ledger.MemoryLedger` on append, so a
  producer can neither backdate a belief nor invent an id.
* **Supersession keeps history.** A record names what it ``supersedes``; the
  old record stays on disk and the ledger derives its ``superseded_at`` from
  the superseding record's ``recorded_at``. Nothing is overwritten.
* **Predictions bound themselves.** A prediction's noise floor must have
  been measured under the prediction's own regime (character for character,
  ``noise_floor.yaml``'s rule); a floor from another regime is refused, so
  "no effect" can always be told from "too small to see".
* **Unevaluable is not normal.** :class:`Result` separates a named
  unevaluable reason from an evaluated result, and an unevaluable result
  cannot carry a residual (E00 shared contract).

Pydantic + stdlib only, so the live path (``plausibility``, ``behavior``),
the workbench (``analysis``) and the interfaces can all use one contract
(layer 1 in ``tests/test_layering.py``).
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, time
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from src.evidence.text import no_numerals

__all__ = [
    "AFFECTS_ALL",
    "DETECTABLE_SIGMA",
    "ORDERABLE_PRECISIONS",
    "EffectivePrecision",
    "MEMORY_SCHEMA",
    "RECORD_KINDS",
    "UNEVALUABLE_REASONS",
    "Conditions",
    "Evidence",
    "Hypothesis",
    "Intervention",
    "Investigation",
    "MemoryRecord",
    "Outcome",
    "Prediction",
    "Provenance",
    "Quality",
    "NoiseFloorRef",
    "Quantity",
    "RecordKind",
    "Reference",
    "Result",
    "Retraction",
    "SourceRef",
    "UnevaluableReason",
    "VehicleIdentity",
    "content_key",
    "mint_id",
    "recurrence_key",
    "record_from_dict",
    "record_to_dict",
]

MEMORY_SCHEMA: Literal["canary-memory/1"] = "canary-memory/1"

RecordKind = Literal[
    "evidence",
    "investigation",
    "prediction",
    "intervention",
    "outcome",
    "retraction",
    "reference",
]
RECORD_KINDS: tuple[str, ...] = (
    "evidence",
    "investigation",
    "prediction",
    "intervention",
    "outcome",
    "retraction",
    "reference",
)

#: Id prefix per kind, so an id names what it points at.
ID_PREFIX: dict[str, str] = {
    "evidence": "ev",
    "investigation": "inv",
    "prediction": "pred",
    "intervention": "intv",
    "outcome": "out",
    "retraction": "ret",
    "reference": "ref",
}

#: Why a result could not be evaluated. Each is a different answer and
#: demands a different response (AGENTS.md, "An empty result must say which
#: kind of empty it is"):
#:
#: - ``missing_input`` — an input was never captured (a channel absent by
#:   construction, e.g. Mode 01 disabled by the capture profile);
#: - ``stale_input`` — it was captured, but not recently enough to stand for
#:   the instant being evaluated;
#: - ``sentinel`` — the input holds a state, not a number (fuel-cut zero);
#: - ``unknown_context`` — the operating context (vehicle state, mode,
#:   calibration, program) needed to apply the model is unknown;
#: - ``uncovered`` — the operating point is outside what the model or table
#:   covers;
#: - ``unvalidated`` — the model has not passed its own validation here;
#: - ``refused`` — the tool's own refusal (too few pairs, a constant signal),
#:   named in ``Result.detail``.
UnevaluableReason = Literal[
    "missing_input",
    "stale_input",
    "sentinel",
    "unknown_context",
    "uncovered",
    "unvalidated",
    "refused",
]
UNEVALUABLE_REASONS: tuple[str, ...] = (
    "missing_input",
    "stale_input",
    "sentinel",
    "unknown_context",
    "uncovered",
    "unvalidated",
    "refused",
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RecordId = Annotated[str, Field(min_length=1)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_aware(value: datetime | None, name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{name} must be timezone-aware (UTC); a naive time has no instant")


class VehicleIdentity(_Strict):
    """Whose record this is, and which ECU program and calibration applied.

    ``profile`` is the vehicle-profile slug that partitions all learned state
    (``src.vehicle.profiles``). ``vin``, ``program_id`` and ``calibration``
    are required keys that may be ``None``: unknown is a statement, and a
    missing historical fact stays unknown rather than being backfilled.
    ``calibration`` names a ``tune_revisions.yaml`` revision; ``program_id``
    the ECU software (memory map), which a calibration flash does not change.
    """

    profile: str = Field(min_length=1)
    vin: str | None
    program_id: str | None
    calibration: str | None


class Conditions(_Strict):
    """The regime a record applies to.

    ``regime`` follows ``noise_floor.yaml``'s rule: a free-form but
    *required* description, matched character for character (``"all
    conditions"`` must be said, not implied). ``vehicle_state`` is the
    ``/status.vehicle_state`` verdict the record was made under, or ``None``
    for stored data where none was classified. ``bounds`` optionally pins
    numeric limits, each with its unit.
    """

    regime: str = Field(min_length=1)
    vehicle_state: str | None
    operating_mode: str | None = None
    bounds: dict[str, Quantity] = Field(default_factory=dict)

    @field_validator("regime")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("conditions.regime cannot be blank")
        return value


class Quantity(_Strict):
    """A number with its unit. Dimensionless quantities say ``"1"``; a unit
    the source did not declare says ``"unspecified"`` — never empty."""

    value: float
    unit: str = Field(min_length=1)
    #: One-sigma uncertainty in ``unit``, when the producer can state one.
    uncertainty: float | None = Field(default=None, ge=0.0)

    @field_validator("value")
    @classmethod
    def _finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("a quantity is a finite number; NaN/inf is unevaluable, not a value")
        return value


class Quality(_Strict):
    """How much measurement a result rests on."""

    #: Observations (pairs, samples, cells) the values were computed from.
    n_observations: int = Field(ge=0)
    #: Weakest clock provenance among the inputs (``src.signals.pairing``'s
    #: timestamp basis), or ``None`` when the tool has no clock to report.
    timestamp_basis: str | None
    flags: tuple[str, ...] = ()


class Result(_Strict):
    """The shared result status: evaluated, or unevaluable for a named reason.

    ``residual`` is the qualified residual (measured minus expected, in its
    unit). An unevaluable result has none: it is not a zero, and it is not
    "normal" (E00: unevaluable is not normal). An evaluated result may still
    carry no residual (a tool whose output is not a residual).
    """

    status: Literal["evaluated", "unevaluable"]
    reason: UnevaluableReason | None = None
    detail: str = ""
    residual: Quantity | None = None

    @model_validator(mode="after")
    def _status_rules(self) -> Result:
        if self.status == "unevaluable":
            if self.reason is None:
                raise ValueError("an unevaluable result names its reason")
            if not self.detail.strip():
                raise ValueError("an unevaluable result says why in detail")
            if self.residual is not None:
                raise ValueError(
                    "an unevaluable result cannot carry a qualified residual; "
                    "a residual from inputs that could not be evaluated is not a measurement"
                )
        elif self.reason is not None:
            raise ValueError("an evaluated result has no unevaluable reason")
        return self


class Provenance(_Strict):
    """Who produced a record, and from which other records.

    A ``tool`` producer states its version and a ``code_version`` (a hash of
    the producing source); an ``operator`` record is the operator's own
    statement. ``derived_from`` names the records it was computed or reasoned
    from (W3C PROV ``wasDerivedFrom``); the ledger refuses an id that is not
    on it or belongs to another vehicle.
    """

    producer: str = Field(min_length=1)
    producer_kind: Literal["tool", "operator"]
    version: str = Field(min_length=1)
    code_version: str | None = None
    derived_from: tuple[RecordId, ...] = ()

    @model_validator(mode="after")
    def _tool_states_its_code(self) -> Provenance:
        if self.producer_kind == "tool" and not self.code_version:
            raise ValueError("a tool-produced record states its code_version")
        return self


class SourceRef(_Strict):
    """The stored data a record rests on: session, window, channels, samples."""

    session: str = Field(min_length=1)
    window_start: datetime
    window_end: datetime
    channels: tuple[str, ...] = Field(min_length=1)
    #: Samples counted in the window (rows or pairs the tool consumed).
    n_samples: int = Field(ge=0)
    #: Specific samples a claim rests on (acquisition times), when it rests on
    #: particular ones rather than the window as a whole.
    sample_times: tuple[datetime, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> SourceRef:
        _require_aware(self.window_start, "window_start")
        _require_aware(self.window_end, "window_end")
        for stamp in self.sample_times:
            _require_aware(stamp, "sample_times")
        if self.window_end < self.window_start:
            raise ValueError("window_end precedes window_start")
        return self


class _RecordBase(_Strict):
    """Fields every record carries: identity, provenance, both times, links."""

    schema_: Literal["canary-memory/1"] = Field(default=MEMORY_SCHEMA, alias="schema")
    #: Minted by the ledger (content hash); empty until appended.
    id: str = ""
    #: When CANary recorded this (ledger clock); ``None`` until appended.
    recorded_at: datetime | None = None
    vehicle: VehicleIdentity
    provenance: Provenance
    valid_from: datetime
    valid_to: datetime | None = None
    #: Records this one replaces. The ledger fills it for re-minted evidence
    #: on the same subject; the old records remain, superseded.
    supersedes: tuple[RecordId, ...] = ()
    supersession_reason: str | None = None

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _times_and_links(self) -> _RecordBase:
        _require_aware(self.valid_from, "valid_from")
        _require_aware(self.valid_to, "valid_to")
        _require_aware(self.recorded_at, "recorded_at")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to precedes valid_from")
        if bool(self.supersedes) != bool(self.supersession_reason):
            raise ValueError("supersedes and supersession_reason come together")
        if self.id and not self.id.startswith(ID_PREFIX[self.kind_name] + "-"):
            raise ValueError(f"{self.id!r} is not a {self.kind_name} id")
        if self.id and self.id in self.references():
            raise ValueError("a record cannot reference itself")
        return self

    @property
    def kind_name(self) -> str:
        """The concrete record's ``kind`` (every subclass declares one)."""
        return str(type(self).model_fields["kind"].default)

    def references(self) -> tuple[str, ...]:
        """Every record id this record points at (checked by the ledger)."""
        return tuple(self.provenance.derived_from) + tuple(self.supersedes)

    def typed_references(self) -> tuple[tuple[str, str | None], ...]:
        """References with the kind each must have (``None``: any kind)."""
        return tuple((ref, None) for ref in self.references())


class _ConditionedRecord(_RecordBase):
    conditions: Conditions


class Evidence(_ConditionedRecord):
    """What a tool computed — minted by the tool, never typed by a model.

    ``subject`` is the tool's key for *what* was evaluated (e.g.
    ``compare_channels:<session>:<a>:<b>``); re-minting the same subject over
    the same data (a source session in common, overlapping windows) with
    different inputs, code or values supersedes the earlier record (a
    recompute). The same subject over *other* data is another occurrence — a
    recurrence — and supersedes nothing (E01.3).

    ``incident`` optionally names the recurring phenomenon (e.g.
    ``plausibility:MAF_vs_RPM``) when it spans subjects; occurrences group by
    :func:`recurrence_key`.
    """

    kind: Literal["evidence"] = "evidence"
    subject: str = Field(min_length=1)
    incident: str | None = None
    sources: tuple[SourceRef, ...] = Field(min_length=1)
    input_hashes: dict[str, Sha256] = Field(min_length=1)
    artifact_hashes: dict[str, Sha256] = Field(default_factory=dict)
    #: The evaluation policy the values depend on (tolerances, gates, minimum
    #: counts). A number without its tolerance is not a finding.
    policy: dict[str, Any] = Field(min_length=1)
    values: dict[str, Quantity] = Field(default_factory=dict)
    #: Categorical outputs (a verdict such as ``scale_verdict``).
    labels: dict[str, str] = Field(default_factory=dict)
    quality: Quality
    result: Result

    @model_validator(mode="after")
    def _evidence_rules(self) -> Evidence:
        if self.provenance.producer_kind != "tool":
            raise ValueError("evidence is minted by tools; an operator statement is not evidence")
        if self.result.status == "evaluated" and not self.values:
            raise ValueError("evaluated evidence carries the values it computed")
        return self


class Hypothesis(_Strict):
    label: str = Field(min_length=1)
    status: Literal["open", "supported", "refuted", "unresolvable"] = "open"
    supporting: tuple[RecordId, ...] = ()
    refuting: tuple[RecordId, ...] = ()

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return no_numerals(value, what="hypothesis label")


class Investigation(_ConditionedRecord):
    """An observation, its hypotheses and the evidence for and against each.

    The structured prose (title, question, hypothesis labels, unknowns, next
    measurements) is rendered on cards as claims, so it cannot state numbers
    (:func:`src.evidence.text.numeral_in`): a number reaches the operator
    only rendered from an evidence record. ``narrative`` is free prose and is
    shown labelled as narrative, never as a finding.
    """

    kind: Literal["investigation"] = "investigation"
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)
    status: Literal["open", "resolved", "abandoned"] = "open"
    #: The recurring phenomenon this investigation is about (E01.3); its
    #: evidence occurrences group under the same key.
    incident: str | None = None
    #: Evidence ids of the observation that opened it.
    observations: tuple[RecordId, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    unknowns: tuple[str, ...] = ()
    #: Measurements proposed next, in words (E03 selects them; E01.5 shows them).
    next_measurements: tuple[str, ...] = ()
    #: Free prose. Narrative, not evidence: numbers belong in evidence records.
    narrative: str = ""

    @field_validator("title", "question")
    @classmethod
    def _prose(cls, value: str) -> str:
        return no_numerals(value, what="investigation text")

    @field_validator("unknowns", "next_measurements")
    @classmethod
    def _prose_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            no_numerals(item, what="investigation text")
        return value

    def typed_references(self) -> tuple[tuple[str, str | None], ...]:
        evidence = [*self.observations]
        for hypothesis in self.hypotheses:
            evidence.extend(hypothesis.supporting)
            evidence.extend(hypothesis.refuting)
        return super().typed_references() + tuple((ref, "evidence") for ref in evidence)


#: How many standard deviations of the null-run floor a change must reach to
#: be distinguishable. ``src.analysis.noise_floor.SIGMA_THRESHOLD`` is this
#: value; a prediction may demand more, never less.
DETECTABLE_SIGMA = 2.0


class NoiseFloorRef(_Strict):
    """The detectable-effect bound a prediction states (E01.4).

    Copied from a measured floor (``noise_floor.yaml`` through
    ``src.analysis.noise_floor.floor_reference``) with the regime it was
    measured under. :class:`Prediction` refuses one whose ``conditions`` are
    not its own regime, character for character: the WOT spark floor quoted
    against an idle comparison understates noise ~16x. Because a copy can be
    relabelled or invented, the ledger also re-reads ``source`` at append
    time and refuses the reference unless the file hashes to
    ``source_sha256`` and states this sd, unit and regime
    (:func:`src.evidence.floors.verify_floor`).
    """

    #: The floor's own key (``timing_advance_deg``, ``delivery_error_pct``).
    quantity: str = Field(min_length=1)
    #: Per-cell standard deviation of the null-run delta, in its unit.
    sd: Quantity
    #: The regime the floor was measured under.
    conditions: str = Field(min_length=1)
    #: How many ``sd`` a change must reach to be distinguishable.
    sigma_threshold: float = Field(ge=DETECTABLE_SIGMA)
    #: The floor file (repo-relative inside the checkout) and its bytes' hash.
    source: str = Field(min_length=1)
    source_sha256: Sha256

    @property
    def detectable(self) -> float:
        """The smallest absolute change this floor can distinguish."""
        return self.sigma_threshold * self.sd.value


class Prediction(_ConditionedRecord):
    """An expectation stated before the result (E01.4 enforces the order).

    ``conditions`` is the affected regime; ``magnitude`` and ``noise_floor``
    are required keys that may be ``None``: a prediction without a supported
    magnitude says so, and one without a condition-matched floor cannot later
    distinguish "no effect" from "too small to see" (its null outcome can
    only be ``unresolvable``).

    Whether it was made *before* the intervention is not the producer's to
    say: the ledger derives it (``MemoryLedger.prediction_order``) from this
    record's minted ``recorded_at`` against the intervention's ``valid_from``.
    A post-hoc prediction is stored, never counted as confirmed.
    """

    kind: Literal["prediction"] = "prediction"
    investigation: RecordId | None
    intervention: RecordId | None
    quantity: str = Field(min_length=1)
    direction: Literal["increase", "decrease", "no_change"]
    magnitude: Quantity | None
    noise_floor: NoiseFloorRef | None

    @model_validator(mode="after")
    def _floor_matches(self) -> Prediction:
        floor = self.noise_floor
        if floor is None:
            return self
        if floor.conditions != self.conditions.regime:
            raise ValueError(
                f"the noise floor for {floor.quantity} was measured under "
                f"{floor.conditions!r}; this prediction is for {self.conditions.regime!r}. "
                "A floor is only valid for its own regime."
            )
        if self.magnitude is not None and self.magnitude.unit != floor.sd.unit:
            raise ValueError(
                f"magnitude is in {self.magnitude.unit!r} but the noise floor in "
                f"{floor.sd.unit!r}; compare like with like"
            )
        return self

    def typed_references(self) -> tuple[tuple[str, str | None], ...]:
        refs: list[tuple[str, str | None]] = []
        if self.investigation is not None:
            refs.append((self.investigation, "investigation"))
        if self.intervention is not None:
            refs.append((self.intervention, "intervention"))
        return super().typed_references() + tuple(refs)


#: ``Intervention.affects`` entry meaning "every reference" (a supercharger).
AFFECTS_ALL = "*"

#: How well an intervention's ``valid_from`` is known. ``instant``: pinned
#: (a flash capture). ``day``: a vehicle-ledger date, ``valid_from`` at that
#: day's UTC midnight. ``approximate``: a recollection or a file mtime.
#: ``floor``: a placeholder earlier than anything it must cover, not a time.
#: ``unknown``: nobody said. Only ``instant`` and ``day`` can order a
#: prediction before the change (``ORDERABLE_PRECISIONS``).
EffectivePrecision = Literal["instant", "day", "approximate", "floor", "unknown"]
ORDERABLE_PRECISIONS: frozenset[str] = frozenset({"instant", "day"})


class Intervention(_ConditionedRecord):
    """A change to the vehicle or its instruments.

    ``source``/``source_ref`` point at the record that already holds the fact
    — a ``tune_revisions.yaml`` revision name, or a vehicle-ledger event id —
    so memory does not keep a second, divergent copy of it;
    ``source_digest`` is the hash of that entry as it read when recorded, so a
    later edit of the source is detectable (``src.evidence.interventions``).

    ``valid_from`` is when the change took effect. ``effective_precision``
    (required: see :data:`EffectivePrecision`) says how well that is known:
    ``day`` (a vehicle-ledger date) puts ``valid_from`` at the start of that
    UTC day — the earliest it can have been, so a prediction must precede the
    whole day to count as prior; ``approximate``/``floor``/``unknown`` times
    cannot order a prediction at all.

    ``affects`` names the channels (or behavior-model targets) the change is
    expected to alter, or :data:`AFFECTS_ALL`. Every current
    :class:`Reference` on an affected channel established before the change
    is retired by it (derived by the ledger; nothing is deleted).

    A calibration intervention states ``image_sha256`` explicitly: the exact
    plain-image hash, or ``None`` when the flash history never recorded one
    (unknown stays unknown; a container hash is not an image hash).
    """

    kind: Literal["intervention"] = "intervention"
    intervention_kind: Literal["calibration", "hardware", "fuel", "instrumentation"]
    title: str = Field(min_length=1)
    source: Literal["tune_revision", "vehicle_ledger", "none"]
    source_ref: str | None
    #: Required whenever ``source`` names a record: without it a later edit of
    #: the source cannot be detected.
    source_digest: Sha256 | None = None
    affects: tuple[str, ...] = Field(min_length=1)
    effective_precision: EffectivePrecision
    image_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _source_rules(self) -> Intervention:
        if (self.source == "none") != (self.source_ref is None):
            raise ValueError("source_ref names the source record exactly when source is not 'none'")
        if self.source != "none" and self.source_digest is None:
            raise ValueError(
                "an intervention pointing at a source record carries that record's "
                "source_digest, so a later edit of it is detectable"
            )
        if self.source == "tune_revision" and self.intervention_kind != "calibration":
            raise ValueError("a tune revision is a calibration intervention")
        if self.intervention_kind == "calibration":
            if "image_sha256" not in self.model_fields_set:
                raise ValueError(
                    "a calibration intervention states its exact image_sha256 "
                    "(None only when the flash history never recorded one)"
                )
        elif self.image_sha256 is not None:
            raise ValueError("only a calibration intervention carries an image hash")
        if self.effective_precision == "day" and (
            self.valid_from.astimezone(UTC).timetz() != time(0, tzinfo=UTC)
        ):
            raise ValueError(
                "a day-precision intervention starts at that day's UTC midnight, the "
                "earliest it can have taken effect"
            )
        if any(not item.strip() for item in self.affects):
            raise ValueError("affects names channels; a blank entry names nothing")
        return self

    def retires(self, channels: tuple[str, ...]) -> bool:
        """Whether this change invalidates a reference on ``channels``."""
        return AFFECTS_ALL in self.affects or bool(set(self.affects) & set(channels))


class Reference(_ConditionedRecord):
    """A validated reference: established behavior under stated conditions.

    ``source``/``source_ref`` name where it lives (an accepted-baseline
    override, a behavior model); ``investigation`` the investigation that
    justified it, or ``None`` when no such record exists (a historical
    acceptance stays unlinked rather than being linked by guess).
    ``channels`` are what it is a reference *for*; an intervention affecting
    any of them retires it (``LedgerEntry.retired_by``).
    """

    kind: Literal["reference"] = "reference"
    reference_kind: Literal["accepted_baseline", "behavior_model", "evidence"]
    name: str = Field(min_length=1)
    channels: tuple[str, ...] = Field(min_length=1)
    source: Literal["accepted_baselines", "behavior_models", "memory"]
    source_ref: str = Field(min_length=1)
    investigation: RecordId | None
    evidence: tuple[RecordId, ...] = ()

    def typed_references(self) -> tuple[tuple[str, str | None], ...]:
        refs: list[tuple[str, str | None]] = []
        if self.investigation is not None:
            refs.append((self.investigation, "investigation"))
        refs.extend((ref, "evidence") for ref in self.evidence)
        return super().typed_references() + tuple(refs)


class Outcome(_ConditionedRecord):
    """What was measured afterwards, and which kind of null it is."""

    kind: Literal["outcome"] = "outcome"
    prediction: RecordId
    intervention: RecordId | None
    disposition: Literal["no_effect", "unresolvable", "measured_change"]
    evidence: tuple[RecordId, ...] = Field(min_length=1)
    result: Result

    @model_validator(mode="after")
    def _disposition_rules(self) -> Outcome:
        if self.result.status == "unevaluable" and self.disposition != "unresolvable":
            raise ValueError("an unevaluable result can only support an 'unresolvable' outcome")
        if self.disposition == "measured_change" and self.result.residual is None:
            raise ValueError("a measured change carries its residual")
        return self

    def typed_references(self) -> tuple[tuple[str, str | None], ...]:
        refs: list[tuple[str, str | None]] = [(self.prediction, "prediction")]
        if self.intervention is not None:
            refs.append((self.intervention, "intervention"))
        refs.extend((ref, "evidence") for ref in self.evidence)
        return super().typed_references() + tuple(refs)


class Retraction(_RecordBase):
    """Withdraws records with no replacement (W3C PROV ``wasInvalidatedBy``).

    The retracted records stay on the ledger; their ``superseded_at`` becomes
    this record's ``recorded_at``.
    """

    kind: Literal["retraction"] = "retraction"
    retracts: tuple[RecordId, ...] = Field(min_length=1)
    reason: str = Field(min_length=1)

    def typed_references(self) -> tuple[tuple[str, str | None], ...]:
        return super().typed_references() + tuple((ref, None) for ref in self.retracts)


AnyRecord = Evidence | Investigation | Prediction | Intervention | Outcome | Retraction | Reference
MemoryRecord = Annotated[AnyRecord, Field(discriminator="kind")]
_ADAPTER: TypeAdapter[AnyRecord] = TypeAdapter(MemoryRecord)


def record_to_dict(record: AnyRecord) -> dict[str, Any]:
    return record.model_dump(mode="json", by_alias=True)


def record_from_dict(data: dict[str, Any]) -> AnyRecord:
    """Validate one stored record (dispatching on ``kind``)."""
    if data.get("schema") != MEMORY_SCHEMA:
        raise ValueError(f"not a {MEMORY_SCHEMA} record")
    return _ADAPTER.validate_python(data)


#: Fields excluded from a record's content key: the ledger's own stamps and
#: the supersession links it may fill in. Two records with one content key
#: say the same thing; whether the second is a duplicate depends on whether
#: the first is still current (see ``MemoryLedger.append``).
_UNHASHED = frozenset({"id", "recorded_at", "supersedes", "supersession_reason"})


def _digest(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def content_key(record: AnyRecord) -> str:
    """sha256 of what a record says, without its stamps or supersession links."""
    return _digest({k: v for k, v in record_to_dict(record).items() if k not in _UNHASHED})


def mint_id(record: AnyRecord, salt: int = 0) -> str:
    """The id of one stored record: ``<kind prefix>-<sha256[:24]>`` over its
    whole stored form (record time and supersession links included), so the
    same content re-stated after it was superseded or retracted is a new
    record with its own id, never the old one brought back."""
    payload = {k: v for k, v in record_to_dict(record).items() if k != "id"}
    if salt:
        payload["_salt"] = salt
    return f"{ID_PREFIX[record.kind]}-{_digest(payload)[:24]}"


def recurrence_key(record: AnyRecord) -> str | None:
    """What makes records occurrences of one recurring thing (E01.3).

    An investigation's or evidence record's ``incident`` when it names one;
    otherwise an evidence record's ``subject`` (the same evaluation over other
    data is another occurrence). ``None`` for the other kinds.
    """
    if isinstance(record, Evidence | Investigation) and record.incident:
        return f"incident:{record.incident}"
    if isinstance(record, Evidence):
        return f"subject:{record.subject}"
    return None
