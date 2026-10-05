"""The per-vehicle memory ledger: append, mint ids, resolve, supersede.

Append-only JSONL on the shared :class:`~src.jsonl_store.JsonlStore`
primitive, at ``daemon.memory_ledger`` (``control/<slug>/memory.jsonl``
under ``state_dir`` on the Pi; ``tests/test_state_dir.py``). The file is the
whole history: no line is ever rewritten, so re-reading it (a restart, a
replay of the log) reconstructs both times and every supersession exactly.

What the ledger enforces at append, so no caller has to remember it:

* the record's vehicle is the ledger's vehicle;
* ``id`` and ``recorded_at`` are minted here — a record arriving with either
  set is refused, so a producer cannot invent an id or backdate a belief;
* a record whose content matches a *current* record returns that record; the
  same content after it was superseded or retracted is a new record (new id,
  record time and supersession links are part of the id);
* every referenced id exists on this ledger, belongs to this vehicle and has
  the kind the reference requires;
* re-minting :class:`~src.evidence.records.Evidence` on a subject whose
  current record differs supersedes that record (the old one stays, with its
  ``superseded_at``).

Not thread-safe across processes; within the process appends are serialized
per file. Every append re-reads the file (hundreds of records; E01.3/E04
bound live appends).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from src.evidence.records import (
    AnyRecord,
    Evidence,
    content_key,
    mint_id,
    record_from_dict,
    record_to_dict,
)
from src.jsonl_store import JsonlStore

if TYPE_CHECKING:
    from src.config import CanaryConfig

__all__ = [
    "CorruptRecordError",
    "CrossVehicleReferenceError",
    "LedgerEntry",
    "MemoryLedger",
    "MemoryReferenceError",
    "UnknownRecordError",
    "UnscopedMemoryError",
    "memory_ledger_for",
]


class MemoryReferenceError(ValueError):
    """A record id that cannot be used here (base class)."""


class UnknownRecordError(MemoryReferenceError):
    """No record with this id was ever minted on this ledger."""


class CorruptRecordError(MemoryReferenceError):
    """The id is on the ledger, but its stored line no longer validates."""


class CrossVehicleReferenceError(MemoryReferenceError):
    """The id (or record) belongs to another vehicle."""


class UnscopedMemoryError(ValueError):
    """Memory is per vehicle; an unscoped config has no vehicle to file under."""


@dataclass(frozen=True)
class LedgerEntry:
    """A record plus what later records say about it (derived, never stored)."""

    record: AnyRecord
    #: ``recorded_at`` of the first record that superseded or retracted it.
    superseded_at: datetime | None = None
    #: Ids of the records that superseded it (empty if none).
    superseded_by: tuple[str, ...] = ()
    #: Id of the retraction that withdrew it, if one did.
    retracted_by: str | None = None

    @property
    def current(self) -> bool:
        return self.superseded_at is None


_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryLedger(JsonlStore[AnyRecord]):
    """One vehicle's investigation memory."""

    def __init__(self, path: Path, *, vehicle: str) -> None:
        if not vehicle:
            raise UnscopedMemoryError("a memory ledger belongs to one vehicle profile")
        super().__init__(path, from_dict=record_from_dict)
        self.vehicle = vehicle

    # -- writes -----------------------------------------------------------

    def append(self, record: AnyRecord, *, now: datetime | None = None) -> AnyRecord:
        """Validate, mint ``id``/``recorded_at``, persist; returns the stored record.

        ``now`` exists for tests that need a known record time; it must not
        precede the newest record on file (record time only moves forward).
        """
        if record.id or record.recorded_at is not None:
            raise ValueError(
                "id and recorded_at are minted by the ledger; a record arriving with "
                "either set was not produced by this ledger's caller"
            )
        if record.vehicle.profile != self.vehicle:
            raise CrossVehicleReferenceError(
                f"record is for vehicle {record.vehicle.profile!r}; this ledger is {self.vehicle!r}"
            )
        with _lock_for(self.path):
            entries = self._index()
            key = content_key(record)
            for entry in entries.values():
                # A duplicate only of what is believed now: content that was
                # superseded or retracted, stated again, is a new record.
                if entry.current and content_key(entry.record) == key:
                    return entry.record
            for ref, kind in record.typed_references():
                self._check_reference(entries, ref, kind)
            update: dict[str, object] = {}
            if isinstance(record, Evidence):
                stale = self._current_for_subject(entries, record)
                if stale:
                    update["supersedes"] = tuple(dict.fromkeys((*record.supersedes, *stale)))
                    update["supersession_reason"] = record.supersession_reason or (
                        f"recomputed {record.subject} with different inputs, code or values"
                    )
            stamp = now if now is not None else _now()
            latest = max(
                (e.record.recorded_at for e in entries.values() if e.record.recorded_at),
                default=None,
            )
            if latest is not None and stamp < latest:
                raise ValueError("record time cannot move backwards on an append-only ledger")
            update["recorded_at"] = stamp
            stamped = record.model_copy(update=update)
            salt = 0
            minted = mint_id(stamped)
            while minted in entries or minted in self._corrupt_ids():
                salt += 1
                minted = mint_id(stamped, salt)
            stored = record_from_dict(
                record_to_dict(stamped.model_copy(update={"id": minted}))  # re-validated whole
            )
            return self._append_record(stored, to_dict=record_to_dict)

    # -- reads ------------------------------------------------------------

    def get(self, record_id: str) -> AnyRecord:
        return self.entry(record_id).record

    def entry(self, record_id: str) -> LedgerEntry:
        """The record with its derived supersession state, or a refusal
        naming which kind of bad id it is."""
        entries = self._index()
        self._check_reference(entries, record_id, None)
        return entries[record_id]

    def resolve_evidence(self, evidence_id: str) -> LedgerEntry:
        """An evidence id a card or record cites, resolved — or refused."""
        entries = self._index()
        self._check_reference(entries, evidence_id, "evidence")
        return entries[evidence_id]

    def entries(self) -> list[LedgerEntry]:
        """Every record in write order, with its supersession state."""
        return list(self._index().values())

    def history(self, record_id: str) -> list[LedgerEntry]:
        """``record_id`` and every record it superseded, oldest first."""
        entries = self._index()
        self._check_reference(entries, record_id, None)
        chain: list[LedgerEntry] = []
        seen: set[str] = set()
        frontier = [record_id]
        while frontier:
            current = frontier.pop()
            if current in seen or current not in entries:
                continue
            seen.add(current)
            chain.append(entries[current])
            frontier.extend(entries[current].record.supersedes)
        return sorted(chain, key=lambda e: e.record.recorded_at or datetime.min.replace(tzinfo=UTC))

    # -- internals --------------------------------------------------------

    def _scan(self) -> tuple[list[AnyRecord], dict[str, str]]:
        """Valid stored records, and the ids of lines that fail the schema.

        A line that is JSON with an ``id`` but no longer validates is not
        "never minted": it is a corrupt record, and a citation of it says so.
        Lines that are not JSON at all (a torn final write) carry no id.
        """
        records: list[AnyRecord] = []
        corrupt: dict[str, str] = {}
        if not self.path.exists():
            return records, corrupt
        with self.path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                try:
                    record = record_from_dict(data)
                except ValueError as error:
                    record_id = data.get("id")
                    if isinstance(record_id, str) and record_id:
                        corrupt[record_id] = str(error).splitlines()[0]
                    continue
                if record.id and record.recorded_at is not None:
                    records.append(record)
                elif record.id:
                    corrupt[record.id] = "stored without a record time"
        return records, corrupt

    def _records(self) -> list[AnyRecord]:
        return self._scan()[0]

    def _corrupt_ids(self) -> dict[str, str]:
        return self._scan()[1]

    def _index(self) -> dict[str, LedgerEntry]:
        records = self._records()
        superseded_at: dict[str, datetime] = {}
        superseded_by: dict[str, list[str]] = {}
        retracted_by: dict[str, str] = {}
        for record in records:
            assert record.recorded_at is not None
            invalidated = list(record.supersedes)
            if record.kind == "retraction":
                invalidated.extend(record.retracts)
                for old in record.retracts:
                    retracted_by.setdefault(old, record.id)
            for old in invalidated:
                if old not in superseded_at or record.recorded_at < superseded_at[old]:
                    superseded_at[old] = record.recorded_at
                if old in record.supersedes:
                    superseded_by.setdefault(old, []).append(record.id)
        return {
            record.id: LedgerEntry(
                record=record,
                superseded_at=superseded_at.get(record.id),
                superseded_by=tuple(superseded_by.get(record.id, ())),
                retracted_by=retracted_by.get(record.id),
            )
            for record in records
        }

    def _check_reference(self, entries: dict[str, LedgerEntry], ref: str, kind: str | None) -> None:
        entry = entries.get(ref)
        if entry is None:
            corrupt = self._corrupt_ids()
            if ref in corrupt:
                raise CorruptRecordError(
                    f"{ref!r} is a corrupt record on {self.vehicle!r}'s memory ledger "
                    f"(the stored line fails the schema: {corrupt[ref]})"
                )
            raise UnknownRecordError(
                f"{ref!r} was not minted on {self.vehicle!r}'s memory ledger; "
                "cite only ids a tool returned"
            )
        if entry.record.vehicle.profile != self.vehicle:
            raise CrossVehicleReferenceError(
                f"{ref!r} belongs to vehicle {entry.record.vehicle.profile!r}, not {self.vehicle!r}"
            )
        if kind is not None and entry.record.kind != kind:
            raise MemoryReferenceError(f"{ref!r} is a {entry.record.kind} record, not {kind}")

    def _current_for_subject(self, entries: dict[str, LedgerEntry], record: Evidence) -> list[str]:
        return [
            entry.record.id
            for entry in entries.values()
            if isinstance(entry.record, Evidence)
            and entry.record.subject == record.subject
            and entry.current
        ]


def memory_ledger_for(config: CanaryConfig) -> MemoryLedger:
    """The ledger at ``config.daemon.memory_ledger`` for the scoped vehicle.

    Refuses an unscoped config: memory is per vehicle, and a ledger filed
    under no profile would mix vehicles' findings.
    """
    slug = config.scoped_profile
    if slug is None:
        raise UnscopedMemoryError(
            "investigation memory is per vehicle: scope the config to a profile "
            "(src.vehicle.profiles.scope_config) before opening the memory ledger"
        )
    return MemoryLedger(config.daemon.memory_ledger, vehicle=slug)
