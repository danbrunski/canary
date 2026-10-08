"""Mint a B08 behavior-model residual as E01 evidence (B08.5).

A B08 candidate (B08.3 throttle-fill/fuel context, B08.4 timing with lookup
context) evaluates a *historical relationship*: whether the engine still sits
where a condition-matched reference says it should. :func:`mint_behavior_residual`
writes one such result to the vehicle's memory ledger. It is the only producer
of :data:`BEHAVIOR_RELATIONSHIP` evidence, and the residual chain
(:mod:`src.analysis.residual_chain`) is the only thing that links it to an
investigation beside the twin's and B06's residuals.

What the record can and cannot say is fixed here, not by the caller:

* **Diagnostic-only until a qualification record exists.** No B08 candidate
  has passed B03/B08.7's held-out gates yet. A residual minted without a
  ``qualification`` is unevaluable ``unvalidated``: the number is kept as
  the ``diagnostic_residual`` value and the result carries no residual, so no
  consumer can read it as a finding. A ``qualification`` must resolve to a
  current, unretired behavior-model :class:`~src.evidence.records.Reference`
  for exactly this model and version on the same program and calibration,
  or the mint is refused (:class:`QualificationError`); an invalid
  qualification is never downgraded silently.
* **Exact ROM and model versions.** Every record names the model (name,
  version, artifact sha256) and the ROM the operating context read (image
  sha256, program, revision, slot, the B08.2 context-contract fingerprint).
  Without a resolved image the result is ``unknown_context``: a relationship
  placed on an unknown calibration is not evaluated.
* **Historical, never ECU computation or command/response.** The record's
  class is fixed (``residual_class: historical``). A learned offset around a
  decoded prediction may name the twin evidence it was computed beside
  (``beside``), but it is a separate record: it cannot supersede, rescale or
  stand in for the twin's discrepancy, which only the twin's own producer can
  recompute. Commanded-versus-measured mixture is B06's
  (:func:`src.analysis.delivery_evidence.mint_delivery_error`).
* **B06's fuel-state vocabulary.** ``fuel_state`` is one of B06's regimes
  (:data:`src.analysis.lambda_delivery.Regime`), ``fuel_cut``, or the explicit
  ``not_fuel_conditioned``; there is no default.
* **The inputs a residual is computed from are named** (``residual_inputs``),
  so the chain can resolve them to observation identities and never count an
  alias or a common input twice.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, get_args

from src.analysis.fault_model import observed_sign
from src.analysis.lambda_delivery import Regime
from src.analysis.operating_context import ResolvedContext
from src.evidence.ledger import MemoryLedger
from src.evidence.provenance import code_version, hash_json
from src.evidence.records import (
    DETECTABLE_SIGMA,
    Conditions,
    Evidence,
    Provenance,
    Quality,
    Quantity,
    Reference,
    Result,
    SourceRef,
    UnevaluableReason,
    VehicleIdentity,
)

__all__ = [
    "B08_TOOL",
    "B08_TOOL_VERSION",
    "BEHAVIOR_RELATIONSHIP",
    "DIAGNOSTIC_ONLY",
    "FUEL_STATES",
    "HISTORICAL",
    "QUALIFIED",
    "BehaviorModelIdentity",
    "QualificationError",
    "RomIdentity",
    "check_qualification",
    "mint_behavior_residual",
]

B08_TOOL = "b08_behavior_residual"
#: Bump when the mapping from a candidate result to evidence changes.
B08_TOOL_VERSION = "1"
#: ``labels["evidence_class"]``: a behavior relationship against its reference.
#: Neither the twin loop (``ecu_command``) nor the delivery loop
#: (``engine_response``) accepts it.
BEHAVIOR_RELATIONSHIP = "behavior_relationship"
#: ``labels["residual_class"]``: the residual chain's third question.
HISTORICAL = "historical"
QUALIFIED = "qualified"
DIAGNOSTIC_ONLY = "diagnostic_only"

#: B06's fuel-state vocabulary, plus fuel cut and the explicit "not conditioned".
FUEL_STATES: tuple[str, ...] = (*get_args(Regime), "fuel_cut", "not_fuel_conditioned")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class QualificationError(ValueError):
    """A qualification that does not qualify this model, version and ROM."""


@dataclass(frozen=True)
class RomIdentity:
    """The exact ROM a residual's operating context read (B08.2)."""

    image_sha256: str
    program_id: str
    revision: str | None
    slot: int | None
    #: The operating-context contract fingerprint (``contract()["fingerprint"]``).
    context_fingerprint: str | None

    def __post_init__(self) -> None:
        if not _SHA256.match(self.image_sha256):
            raise ValueError("a ROM identity names its exact plain-image sha256")
        if not self.program_id.strip():
            raise ValueError("a ROM identity names its program")

    @classmethod
    def from_context(cls, resolved: ResolvedContext, *, contract_fingerprint: str) -> RomIdentity:
        """The identity :func:`~src.analysis.operating_context.resolve_context` resolved."""
        return cls(
            image_sha256=resolved.image_sha256,
            program_id=resolved.program_id,
            revision=resolved.revision,
            slot=resolved.slot,
            context_fingerprint=contract_fingerprint,
        )


@dataclass(frozen=True)
class BehaviorModelIdentity:
    """A versioned behavior model (a B08 candidate or a published one)."""

    name: str
    version: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ValueError("a behavior model names itself and its version")
        if not _SHA256.match(self.sha256):
            raise ValueError("a behavior model states its artifact sha256")

    @property
    def key(self) -> str:
        """What a qualification :class:`Reference` names as its ``source_ref``."""
        return f"{self.name}@{self.version}"


def check_qualification(
    ledger: MemoryLedger,
    qualification: str,
    *,
    model_key: str,
    program_id: str | None,
    calibration: str | None,
) -> str | None:
    """Why ``qualification`` does not qualify ``model_key`` here, or ``None``.

    A qualification is a current, unretired behavior-model reference whose
    ``source_ref`` is exactly ``<name>@<version>``, on the same program and
    calibration. Shared by the minter (refuses) and the residual chain
    (re-checks at read time: a later intervention retires the reference, and
    the residual is diagnostic-only from then on).
    """
    try:
        entry = ledger.entry(qualification)
    except ValueError as exc:
        return f"qualification {qualification!r} does not resolve: {exc}"
    record = entry.record
    if not isinstance(record, Reference) or record.reference_kind != "behavior_model":
        return f"{qualification} is not a behavior-model reference"
    if not entry.current:
        return f"{qualification} is superseded or retracted"
    if entry.retired:
        return f"{qualification} was retired by intervention {entry.retired_by}"
    if record.source_ref != model_key:
        return f"{qualification} qualifies {record.source_ref!r}, not {model_key!r}"
    if record.vehicle.program_id != program_id or record.vehicle.calibration != calibration:
        return (
            f"{qualification} qualifies program {record.vehicle.program_id!r} calibration "
            f"{record.vehicle.calibration!r}; this residual ran on program {program_id!r} "
            f"calibration {calibration!r}"
        )
    return None


def _provenance(derived_from: Sequence[str]) -> Provenance:
    return Provenance(
        producer=B08_TOOL,
        producer_kind="tool",
        version=B08_TOOL_VERSION,
        code_version=code_version(sys.modules[__name__]),
        derived_from=tuple(dict.fromkeys(derived_from)),
    )


def mint_behavior_residual(
    ledger: MemoryLedger,
    *,
    session: str,
    window: tuple[datetime, datetime],
    model: BehaviorModelIdentity,
    rom: RomIdentity | None,
    quantity: str,
    residual: Quantity | None,
    n_pairs: int,
    residual_inputs: Sequence[str],
    regime: str,
    fuel_state: str,
    input_hashes: Mapping[str, str],
    unevaluable: tuple[UnevaluableReason, str] | None = None,
    detectable: Quantity | None = None,
    qualification: str | None = None,
    beside: Sequence[str] = (),
    vin: str | None = None,
    now: datetime | None = None,
) -> str:
    """One B08 candidate residual on one drive as evidence; returns its id.

    ``residual`` is the candidate's paired residual (measured minus the
    model's expectation, in its unit) over ``n_pairs`` pairs, or ``None`` with
    ``unevaluable`` naming why. ``qualification`` names the behavior-model
    reference that qualifies ``model`` (see :func:`check_qualification`);
    without it the record is diagnostic-only. ``detectable`` (required when
    qualified) is the smallest residual the qualification can distinguish, in
    the residual's unit. ``beside`` names evidence the residual was computed
    beside (a twin record whose prediction it is a learned offset around);
    those records are cited, never replaced.
    """
    if (residual is None) == (unevaluable is None):
        raise ValueError("state the residual, or why it could not be evaluated, not both")
    if fuel_state not in FUEL_STATES:
        raise ValueError(f"fuel_state {fuel_state!r} is not one of {list(FUEL_STATES)}")
    inputs = tuple(dict.fromkeys(residual_inputs))
    if not inputs:
        raise ValueError("a residual names the inputs it is computed from")
    if n_pairs < 0:
        raise ValueError("n_pairs counts pairs")
    for ref in beside:
        ledger.resolve_evidence(ref)  # refuses an id that is not evidence on this vehicle
    vehicle = VehicleIdentity(
        profile=ledger.vehicle,
        vin=vin,
        program_id=None if rom is None else rom.program_id,
        calibration=None if rom is None else rom.revision,
    )
    labels: dict[str, str] = {
        "evidence_class": BEHAVIOR_RELATIONSHIP,
        "residual_class": HISTORICAL,
        "quantity": quantity,
        "model": model.name,
        "model_version": model.version,
        "model_sha256": model.sha256,
        "image_sha256": "unknown" if rom is None else rom.image_sha256,
        "program_id": "unknown" if rom is None else rom.program_id,
        "revision": "unknown" if rom is None or rom.revision is None else rom.revision,
        "slot": "unknown" if rom is None or rom.slot is None else str(rom.slot),
        "context_fingerprint": (
            "unknown" if rom is None or rom.context_fingerprint is None else rom.context_fingerprint
        ),
        "residual_inputs": ",".join(inputs),
        "fuel_state": fuel_state,
        "engine_health": "not_assessed",
    }
    if beside:
        labels["beside"] = ",".join(beside)
    values: dict[str, Quantity] = {"n_pairs": Quantity(value=float(n_pairs), unit="count")}
    hashes = {**input_hashes, "model": model.sha256}
    if rom is not None:
        hashes["rom_image"] = rom.image_sha256
    policy: dict[str, Any] = {
        "model": model.key,
        "qualification": qualification,
        "sign_sigma": DETECTABLE_SIGMA,
        "detectable": None if detectable is None else detectable.model_dump(),
    }
    derived_from: list[str] = list(beside)

    if qualification is not None:
        if rom is None:
            raise QualificationError(
                "a qualification applies to a program and calibration; this residual's "
                "operating context resolved no ROM"
            )
        why = check_qualification(
            ledger,
            qualification,
            model_key=model.key,
            program_id=rom.program_id,
            calibration=rom.revision,
        )
        if why is not None:
            raise QualificationError(why)
        derived_from.append(qualification)
        labels["qualification_reference"] = qualification
    labels["qualification"] = DIAGNOSTIC_ONLY if qualification is None else QUALIFIED

    result: Result
    if unevaluable is not None:
        reason, detail = unevaluable
        result = Result(status="unevaluable", reason=reason, detail=detail)
    elif rom is None:
        assert residual is not None
        values["diagnostic_residual"] = residual
        result = Result(
            status="unevaluable",
            reason="unknown_context",
            detail=(
                "the operating context resolved no ROM image, so the relationship cannot be "
                "placed on a calibration; the residual is kept as a diagnostic value only"
            ),
        )
    elif qualification is None:
        assert residual is not None
        values["diagnostic_residual"] = residual
        result = Result(
            status="unevaluable",
            reason="unvalidated",
            detail=(
                f"{model.key} has no qualification record (a behavior-model reference "
                "published through B03/B08.7 after held-out validation); the residual is "
                "diagnostic-only and supports no hypothesis"
            ),
        )
    else:
        assert residual is not None
        if detectable is None or detectable.unit != residual.unit or detectable.value <= 0:
            raise QualificationError(
                "a qualified residual states its detectable bound in the residual's unit"
            )
        labels["sign"] = observed_sign(residual.value, detectable.value)
        values["detectable"] = detectable
        result = Result(status="evaluated", residual=residual)

    start, end = window
    record = Evidence(
        vehicle=vehicle,
        provenance=_provenance(derived_from),
        conditions=Conditions(regime=regime, vehicle_state=None),
        valid_from=start,
        valid_to=end,
        subject=f"{B08_TOOL}:{model.key}:{session}:{quantity}:{hash_json(policy)[:16]}",
        incident=f"b08:{model.name}:{quantity}",
        sources=(
            SourceRef(
                session=session,
                window_start=start,
                window_end=end,
                channels=inputs,
                n_samples=n_pairs,
            ),
        ),
        input_hashes=hashes,
        policy=policy,
        values=values,
        labels=labels,
        quality=Quality(
            n_observations=n_pairs,
            timestamp_basis="acquired",
            flags=(labels["qualification"],),
        ),
        result=result,
    )
    return ledger.append(record, now=now).id
