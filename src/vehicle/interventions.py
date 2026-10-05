"""One intervention history: flashes and physical changes, in effect order (E01.4).

The two facts already have homes, and this does not start a third:
calibration flashes are ``tune_revisions.yaml`` revisions
(:mod:`src.vehicle.tune_revisions`, operational copy merged over the tracked
file), and hardware, fuel and instrumentation changes are vehicle-ledger
events (:mod:`src.vehicle.vehicle_ledger`, the runtime JSONL under
``state_dir``) whose ``intervention`` says which. :func:`intervention_history`
reads both and returns them as :class:`InterventionSource` items, oldest
first by when the change took effect, so "what changed between these two
drives?" is one call.

What is unknown stays unknown:

* a revision without a plain-image hash has ``image_sha256=None`` — never the
  container's ``rom_sha256``, which no ECU read reproduces;
* a flash time is ``instant`` only when the revision says
  ``flashed_at_precision: exact``; otherwise ``approximate``, ``floor`` or
  ``unknown``, and no prediction can be shown to precede it;
* a vehicle-ledger date has day precision: ``effective_at`` is that day's
  UTC midnight (the earliest it can have been) with ``precision="day"``;
* a ``mod``/``repair`` event recorded without an ``intervention`` kind is
  listed with ``intervention_kind=None`` (unclassified), not guessed into
  ``hardware``. It cannot become a memory record until the operator
  classifies it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any, Literal

from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry
from src.vehicle.vehicle_ledger import VehicleEvent

__all__ = [
    "InterventionSource",
    "intervention_from_revision",
    "intervention_from_vehicle_event",
    "intervention_history",
    "source_digest",
]

#: Vehicle-ledger kinds that change the vehicle. An event of these kinds
#: without an ``intervention`` classification is listed as unclassified;
#: observations, maintenance and DTC sightings are listed only when classified.
_PHYSICAL_CHANGE_KINDS = frozenset({"mod", "repair"})


def source_digest(payload: dict[str, Any]) -> str:
    """sha256 of a source entry's identity, canonical JSON."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class InterventionSource:
    """One change, as its home record states it."""

    #: ``calibration`` | ``hardware`` | ``fuel`` | ``instrumentation``, or
    #: ``None`` for a physical change the operator has not classified.
    intervention_kind: Literal["calibration", "hardware", "fuel", "instrumentation"] | None
    source: Literal["tune_revision", "vehicle_ledger"]
    #: The revision name, or the vehicle-ledger event id.
    source_ref: str
    title: str
    effective_at: datetime
    #: ``instant`` (a revision whose ``flashed_at_precision`` is ``exact``),
    #: ``day`` (a vehicle-ledger date), ``approximate``, ``floor``, or
    #: ``unknown`` (a revision that never said) — never silently ``instant``.
    precision: Literal["instant", "day", "approximate", "floor", "unknown"]
    #: Calibration only: the exact plain-image hash, ``None`` if never recorded.
    image_sha256: str | None
    #: Channels the source says the change affects (vehicle ledger only).
    affected_channels: tuple[str, ...]
    #: Hash of the source entry's identity, to detect a later edit of it.
    digest: str


_REVISION_PRECISION: dict[
    str | None, Literal["instant", "day", "approximate", "floor", "unknown"]
] = {"exact": "instant", "approximate": "approximate", "floor": "floor", None: "unknown"}


def intervention_from_revision(revision: TuneRevision) -> InterventionSource:
    identity = revision.model_dump(mode="json", exclude={"notes"})
    return InterventionSource(
        intervention_kind="calibration",
        source="tune_revision",
        source_ref=revision.revision,
        title=f"flash {revision.revision}",
        effective_at=_aware(revision.flashed_at),
        precision=_REVISION_PRECISION[revision.flashed_at_precision],
        image_sha256=revision.image_sha256,
        affected_channels=(),
        digest=source_digest(identity),
    )


def intervention_from_vehicle_event(event: VehicleEvent) -> InterventionSource:
    identity = event.model_dump(mode="json", exclude={"recorded_at", "details"})
    return InterventionSource(
        intervention_kind=event.intervention,
        source="vehicle_ledger",
        source_ref=event.id,
        title=event.title,
        effective_at=datetime.combine(event.occurred_at, time(0), tzinfo=UTC),
        precision="day",
        image_sha256=None,
        affected_channels=tuple(event.affected_channels),
        digest=source_digest(identity),
    )


def intervention_history(
    registry: TuneRevisionRegistry | None, events: list[VehicleEvent]
) -> list[InterventionSource]:
    """Every flash and every physical change, oldest first by effect time.

    A day-precision event sorts at its day's start, a lower bound: a flash
    later that same day sorts after it whether or not it really came after.
    Exact ties keep flashes first.
    """
    items = [intervention_from_revision(r) for r in (registry.revisions if registry else [])]
    items.extend(
        intervention_from_vehicle_event(event)
        for event in events
        if event.intervention is not None or event.kind in _PHYSICAL_CHANGE_KINDS
    )
    return sorted(items, key=lambda item: (item.effective_at, item.source != "tune_revision"))
