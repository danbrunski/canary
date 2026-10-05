"""Interventions in memory, pointing at their home records (E01.4).

:func:`record_intervention` turns one item of the intervention history
(:func:`src.vehicle.interventions.intervention_history`: a flash from
``tune_revisions.yaml`` or a classified vehicle-ledger event) into an
:class:`~src.evidence.records.Intervention` on the memory ledger. The memory
record carries the pointer (``source``/``source_ref``), the source entry's
digest, the effect time and its precision, and — for a flash — the exact
plain-image hash the revision records (``None`` when it never recorded one).
Nothing is copied that the home record could later contradict unnoticed:
:func:`source_drift` compares the stored digest with the source as it reads
now.

An unclassified physical change is refused, not guessed into a kind.
"""

from __future__ import annotations

from datetime import datetime

from src.evidence.ledger import MemoryLedger
from src.evidence.records import (
    Conditions,
    EffectivePrecision,
    Intervention,
    Provenance,
    VehicleIdentity,
)
from src.vehicle.interventions import InterventionSource

__all__ = ["UnclassifiedInterventionError", "record_intervention", "source_drift"]


class UnclassifiedInterventionError(ValueError):
    """A vehicle-ledger change with no intervention kind: classify it first."""


def record_intervention(
    ledger: MemoryLedger,
    source: InterventionSource,
    *,
    vehicle: VehicleIdentity,
    conditions: Conditions,
    provenance: Provenance,
    affects: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> Intervention:
    """Append ``source`` as an intervention; returns the stored record.

    ``affects`` defaults to the source's affected channels (a vehicle-ledger
    event's ``affected_channels``); a flash names none, so the caller states
    them. ``vehicle.calibration`` for a flash is the revision it installs.
    Idempotent: recording the same source twice returns the first record.
    ``now`` is passed to :meth:`MemoryLedger.append` (tests).
    """
    kind = source.intervention_kind
    if kind is None:
        raise UnclassifiedInterventionError(
            f"vehicle-ledger event {source.source_ref} ({source.title!r}) is not classified as "
            "a hardware, fuel or instrumentation change; set its `intervention` in the "
            "vehicle ledger rather than having memory guess"
        )
    stated = affects if affects is not None else source.affected_channels
    if not stated:
        raise ValueError(
            f"{source.title!r}: name the channels this change affects (or '*' for all); "
            "an intervention that affects nothing retires nothing and predicts nothing"
        )
    identity = vehicle
    if kind == "calibration":
        identity = vehicle.model_copy(update={"calibration": source.source_ref})
    precision: EffectivePrecision = source.precision
    record = Intervention(
        vehicle=identity,
        provenance=provenance,
        conditions=conditions,
        valid_from=source.effective_at,
        intervention_kind=kind,
        title=source.title,
        source=source.source,
        source_ref=source.source_ref,
        source_digest=source.digest,
        affects=tuple(stated),
        effective_precision=precision,
        image_sha256=source.image_sha256,
    )
    stored = ledger.append(record, now=now)
    assert isinstance(stored, Intervention)
    return stored


def source_drift(intervention: Intervention, history: list[InterventionSource]) -> str | None:
    """Why ``intervention`` no longer matches its home record, or ``None``.

    ``"removed"`` when the revision or event is gone; ``"no_digest"`` when the
    record carries no source digest (nothing can show it still matches);
    ``"edited"`` when the source's identity changed after the memory record
    was made; ``"mismatch:<fields>"`` when the memory record states an effect
    time, precision, kind or image hash its source does not. ``None`` for a
    record with no source (``source="none"``) or one that still matches.
    """
    if intervention.source == "none":
        return None
    for item in history:
        if item.source != intervention.source or item.source_ref != intervention.source_ref:
            continue
        if intervention.source_digest is None:
            return "no_digest"
        if item.digest != intervention.source_digest:
            return "edited"
        differ = [
            name
            for name, stated, actual in (
                ("valid_from", intervention.valid_from, item.effective_at),
                ("effective_precision", intervention.effective_precision, item.precision),
                ("intervention_kind", intervention.intervention_kind, item.intervention_kind),
                ("image_sha256", intervention.image_sha256, item.image_sha256),
            )
            if stated != actual
        ]
        return f"mismatch:{','.join(differ)}" if differ else None
    return "removed"
