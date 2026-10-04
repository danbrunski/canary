"""Vehicle maintenance / intervention ledger (Task 3.1, eval-longitudinal-and-dtc.md).

An append-only, profile-scoped record of what physically happened to the
truck — maintenance, mods, repairs, observations, and auto-appended DTC
sightings (Task 3.4) — so anomalies can later be scored against outcomes
("did the misfire alerts stop after the plug change?") and training
corpora can be segmented at intervention boundaries (Task 3.5): a mod or
repair that changes what "normal" looks like shouldn't have pre- and
post-change sessions blended into one corpus without the operator opting
in.

Storage mirrors :class:`~src.models.feedback.FeedbackStore`: JSONL under the
control state directory (profile-scoped via
``src.vehicle.profiles.scope_config``), one line per event, new events
appended.

**Edits and deletes rewrite the file** (operator, 2026-10-04). A correction
used to be another append under the same ``id``, with reads collapsing to the
latest line, so a mis-measured tread depth (0.592 in for 0.475 in) stayed on
disk and had to be cut out by hand. An entry here is the operator's own
statement of what happened to the truck, not provenance to preserve: a typo or
a wrong reading should be gone, not hidden. :meth:`VehicleLedgerStore.replace`
and :meth:`VehicleLedgerStore.remove` rewrite the file atomically, under the
same lock as :meth:`VehicleLedgerStore.append` so a concurrent append (a DTC
sighting) cannot be lost. :meth:`VehicleLedgerStore.records` still collapses
to the latest line per id, for files written before this.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.atomic_write import atomic_write_text
from src.jsonl_store import JsonlStore, latest_by_key
from src.paths import CONTROL_DIR

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_VEHICLE_LEDGER_PATH",
    "VEHICLE_EVENT_KINDS",
    "TyreAction",
    "TyreRecord",
    "VehicleEvent",
    "VehicleEventKind",
    "VehicleLedgerStore",
]

DEFAULT_VEHICLE_LEDGER_PATH = CONTROL_DIR / "vehicle_ledger.jsonl"

VehicleEventKind = Literal["maintenance", "mod", "repair", "observation", "dtc"]
#: The valid kinds, as a set, for request validation at the API edge.
VEHICLE_EVENT_KINDS: frozenset[str] = frozenset(
    {"maintenance", "mod", "repair", "observation", "dtc"}
)


TyreAction = Literal["tread_measured", "rotated", "replaced", "pressure_changed"]


class TyreRecord(BaseModel):
    """A tyre fact on a ledger event, readable by code (tyre wear).

    ``tread_measured`` is a gauge reading: the baseline that turns the trip
    digests' rolling circumference into tread depth (:mod:`src.control.tyres`).
    ``rotated`` and ``replaced`` change which tyres the driven axle carries,
    so a circumference trend restarts there; ``pressure_changed`` is named
    because ~6 psi moves the circumference as much as ~0.8 mm of tread.
    """

    model_config = ConfigDict(extra="forbid")

    action: TyreAction
    #: Gauge reading, driven-axle tyres, mm (required for ``tread_measured``).
    tread_depth_mm: float | None = Field(default=None, gt=0.0, lt=40.0)
    pressure_psi: float | None = Field(default=None, gt=0.0, lt=120.0)

    @model_validator(mode="after")
    def _a_measurement_has_a_depth(self) -> TyreRecord:
        if self.action == "tread_measured" and self.tread_depth_mm is None:
            raise ValueError("a tread_measured record needs tread_depth_mm")
        return self


class VehicleEvent(BaseModel):
    """One thing that physically happened to the vehicle."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    recorded_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    #: Date the event actually happened (may predate ``recorded_at`` for a
    #: late-entered record).
    occurred_at: date
    odometer_km: float | None = None
    kind: VehicleEventKind
    title: str
    details: str = ""
    #: Channels this event is expected to change the behavior of (e.g. a
    #: coolant-thermostat swap → ``["obd:COOLANT_TEMP"]``). Free-form, not
    #: validated against ``bindings.yaml`` — narrows what a later "did the
    #: anomalies stop" review needs to look at.
    affected_channels: list[str] = Field(default_factory=list)
    #: Does "normal" change at this event? A mod or repair that shifts
    #: expected sensor behavior should bisect the training corpus (Task 3.5);
    #: routine maintenance and observations usually don't.
    corpus_boundary: bool = False
    #: A tyre fact the tyre-wear trend reads, or ``None``.
    tyres: TyreRecord | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VehicleEvent:
        return cls.model_validate(data)


#: One lock per ledger file, shared by every store instance on it: the route
#: handlers, the DTC auto-append and the tyre trend each build their own store.
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


class VehicleLedgerStore(JsonlStore[VehicleEvent]):
    """JSONL store of vehicle events: append, replace and remove.

    Writes are serialized per file within the process (the daemon is the
    only writer). Reads tolerate a torn final line (Task 3.4: the shared
    :class:`~src.jsonl_store.JsonlStore` base).
    """

    def __init__(self, path: Path = DEFAULT_VEHICLE_LEDGER_PATH) -> None:
        super().__init__(path, from_dict=VehicleEvent.from_dict)

    def append(self, event: VehicleEvent) -> VehicleEvent:
        """Append one event (flushed; crash loses at most the last line)."""
        with _lock_for(self.path):
            return self._append_record(event, to_dict=VehicleEvent.to_dict)

    def replace(self, event: VehicleEvent) -> VehicleEvent:
        """Store ``event`` in place of every line with its ``id``.

        The earlier version is gone from disk, not superseded. An ``id`` not
        on the ledger is simply added.
        """
        with _lock_for(self.path):
            self._rewrite_without(event.id, then=event)
        return event

    def remove(self, event_id: str) -> VehicleEvent | None:
        """Delete every line for ``event_id``; the removed event, or ``None``."""
        with _lock_for(self.path):
            removed = self.get(event_id)
            if removed is not None:
                self._rewrite_without(event_id, then=None)
        return removed

    def _rewrite_without(self, event_id: str, *, then: VehicleEvent | None) -> None:
        """Rewrite the file without ``event_id``'s lines (caller holds the lock).

        Lines that do not parse are kept verbatim: this edits one entry and
        must not silently drop anything else.
        """
        kept: list[str] = []
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    if json.loads(line).get("id") == event_id:
                        continue
                except (json.JSONDecodeError, AttributeError):
                    pass
                kept.append(line)
        if then is not None:
            kept.append(json.dumps(then.to_dict(), sort_keys=True, default=str))
        atomic_write_text(self.path, "".join(f"{line}\n" for line in kept), mkdir=True)

    def record(
        self,
        *,
        occurred_at: date,
        kind: VehicleEventKind,
        title: str,
        details: str = "",
        odometer_km: float | None = None,
        affected_channels: list[str] | None = None,
        corpus_boundary: bool = False,
        event_id: str | None = None,
        tyres: TyreRecord | None = None,
    ) -> VehicleEvent:
        """Build and store a :class:`VehicleEvent`: appended when new, or in
        place of the existing entry when ``event_id`` names one."""
        kwargs: dict[str, Any] = {
            "occurred_at": occurred_at,
            "kind": kind,
            "title": title,
            "details": details,
            "odometer_km": odometer_km,
            "affected_channels": affected_channels or [],
            "corpus_boundary": corpus_boundary,
            "tyres": tyres,
        }
        if event_id is not None:
            kwargs["id"] = event_id
            return self.replace(VehicleEvent(**kwargs))
        return self.append(VehicleEvent(**kwargs))

    def records(self) -> list[VehicleEvent]:
        """Latest event per id, ordered by ``occurred_at`` (oldest first).

        Collapsing the append-only log: a later line for the same id wins
        (a correction), but the events themselves are ordered by when they
        physically happened, not when they were recorded.
        """
        by_id = latest_by_key(self._iter_raw(), key=lambda e: e.id)
        return sorted(by_id.values(), key=lambda e: (e.occurred_at, e.recorded_at))

    def get(self, event_id: str) -> VehicleEvent | None:
        """Latest event for ``event_id``, or ``None`` if unknown."""
        latest: VehicleEvent | None = None
        for event in self._iter_raw():
            if event.id == event_id:
                latest = event
        return latest

    def latest_boundary(self) -> VehicleEvent | None:
        """Most recent ``corpus_boundary`` event by ``occurred_at``, if any.

        Feeds Task 3.5's ``--since-boundary`` corpus filter and the
        dataset-health bisection note.
        """
        boundaries = [e for e in self.records() if e.corpus_boundary]
        return boundaries[-1] if boundaries else None
