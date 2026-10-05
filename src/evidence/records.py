"""Record contracts for investigation memory (E01.1, docs/roadmap/tasks/E01).

Six record kinds share one envelope (:class:`MemoryRecord`):

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
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

__all__ = [
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
    "Quantity",
    "RecordKind",
    "Result",
    "Retraction",
    "SourceRef",
    "UnevaluableReason",
    "VehicleIdentity",
    "content_key",
    "mint_id",
    "record_from_dict",
    "record_to_dict",
]

MEMORY_SCHEMA: Literal["canary-memory/1"] = "canary-memory/1"

RecordKind = Literal[
    "evidence", "investigation", "prediction", "intervention", "outcome", "retraction"
]
RECORD_KINDS: tuple[str, ...] = (
    "evidence",
    "investigation",
    "prediction",
    "intervention",
    "outcome",
    "retraction",
)

#: Id prefix per kind, so an id names what it points at.
ID_PREFIX: dict[str, str] = {
    "evidence": "ev",
    "investigation": "inv",
    "prediction": "pred",
    "intervention": "intv",
    "outcome": "out",
    "retraction": "ret",
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
    ``compare_channels:<session>:<a>:<b>``); re-minting the same subject with
    different inputs, code or values supersedes the earlier record.
    """

    kind: Literal["evidence"] = "evidence"
    subject: str = Field(min_length=1)
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


class Investigation(_ConditionedRecord):
    kind: Literal["investigation"] = "investigation"
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)
    status: Literal["open", "resolved", "abandoned"] = "open"
    #: Evidence ids of the observation that opened it.
    observations: tuple[RecordId, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    unknowns: tuple[str, ...] = ()
    #: Free prose. Narrative, not evidence: numbers belong in evidence records.
    narrative: str = ""

    def typed_references(self) -> tuple[tuple[str, str | None], ...]:
        evidence = [*self.observations]
        for hypothesis in self.hypotheses:
            evidence.extend(hypothesis.supporting)
            evidence.extend(hypothesis.refuting)
        return super().typed_references() + tuple((ref, "evidence") for ref in evidence)


class Prediction(_ConditionedRecord):
    """An expectation stated before the result (E01.4 enforces the order).

    ``magnitude`` and ``noise_floor`` are required keys that may be ``None``:
    a prediction without a supported magnitude says so, and one without a
    condition-matched floor cannot later distinguish "no effect" from "too
    small to see".
    """

    kind: Literal["prediction"] = "prediction"
    investigation: RecordId | None
    intervention: RecordId | None
    quantity: str = Field(min_length=1)
    direction: Literal["increase", "decrease", "no_change"]
    magnitude: Quantity | None
    noise_floor: Quantity | None

    @model_validator(mode="after")
    def _units_agree(self) -> Prediction:
        if (
            self.magnitude is not None
            and self.noise_floor is not None
            and self.magnitude.unit != self.noise_floor.unit
        ):
            raise ValueError(
                f"magnitude is in {self.magnitude.unit!r} but the noise floor in "
                f"{self.noise_floor.unit!r}; compare like with like"
            )
        return self

    def typed_references(self) -> tuple[tuple[str, str | None], ...]:
        refs: list[tuple[str, str | None]] = []
        if self.investigation is not None:
            refs.append((self.investigation, "investigation"))
        if self.intervention is not None:
            refs.append((self.intervention, "intervention"))
        return super().typed_references() + tuple(refs)


class Intervention(_ConditionedRecord):
    """A change to the vehicle or its instruments.

    ``source``/``source_ref`` point at the record that already holds the fact
    — a ``tune_revisions.yaml`` revision name, or a vehicle-ledger event id —
    so memory does not keep a second, divergent copy of it.
    """

    kind: Literal["intervention"] = "intervention"
    intervention_kind: Literal["calibration", "hardware", "fuel", "instrumentation"]
    title: str = Field(min_length=1)
    source: Literal["tune_revision", "vehicle_ledger", "none"]
    source_ref: str | None
    image_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _source_rules(self) -> Intervention:
        if (self.source == "none") != (self.source_ref is None):
            raise ValueError("source_ref names the source record exactly when source is not 'none'")
        if self.source == "tune_revision" and self.intervention_kind != "calibration":
            raise ValueError("a tune revision is a calibration intervention")
        return self


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


MemoryRecord = Annotated[
    Evidence | Investigation | Prediction | Intervention | Outcome | Retraction,
    Field(discriminator="kind"),
]
_ADAPTER: TypeAdapter[
    Evidence | Investigation | Prediction | Intervention | Outcome | Retraction
] = TypeAdapter(MemoryRecord)

AnyRecord = Evidence | Investigation | Prediction | Intervention | Outcome | Retraction


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
