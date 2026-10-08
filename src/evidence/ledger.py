"""The per-vehicle memory ledger: append, mint ids, resolve, supersede, query.

Append-only JSONL on the shared :class:`~src.jsonl_store.JsonlStore`
primitive, at ``daemon.memory_ledger`` (``control/<slug>/memory.jsonl``
under ``state_dir`` on the Pi; ``tests/test_state_dir.py``). The file is the
whole history: no line is ever rewritten, so re-reading it (a restart, a
replay of the log) reconstructs both times and every supersession exactly.

What the ledger enforces at append, so no caller has to remember it:

* the record's vehicle is the ledger's vehicle;
* ``id`` and ``recorded_at`` are minted here — a record arriving with either
  set is refused, so a producer cannot invent an id or backdate a belief;
* a record whose content matches a *current* record returns that record (a
  retry is not a second finding); the same content after it was superseded or
  retracted is a new record (new id, record time and supersession links are
  part of the id);
* every referenced id exists on this ledger, belongs to this vehicle and has
  the kind the reference requires;
* re-minting :class:`~src.evidence.records.Evidence` on a subject over the
  same data (a source session in common, overlapping windows) supersedes the
  current record there (a recompute; the old one stays, with its
  ``superseded_at``). The same subject over other data is a recurrence: a new
  occurrence, superseding nothing (E01.3);
* the live ledger refuses evidence read from a ``_replay-*`` session: replay
  output is re-emitted rows, never this vehicle's memory. A replay writes to
  its own ledger (:func:`replay_memory_path`);
* an :class:`~src.evidence.records.Outcome` is evaluated under its
  prediction's regime, and a null or a change is only claimed against the
  prediction's condition-matched noise floor (E01.4).

Derived, never stored (so never forgeable): supersession and retraction state,
retirement of references by an intervention, and whether a prediction preceded
its intervention (:meth:`MemoryLedger.prediction_order`).

**Bounded appends (E01.3).** The parsed index is kept in memory per file and
shared by every ledger instance on that file in the process; each read or
append stats the file and parses only the bytes appended since (another
instance's or another process's appends included). An append is a fixed number
of dict lookups plus one line written, not a re-read of the file. A file that
shrank or was replaced (a new inode) is re-read whole. Within the process
appends are serialized per file; across processes they are not.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from src.evidence.floors import verify_floor
from src.evidence.records import (
    ORDERABLE_PRECISIONS,
    RECORD_KINDS,
    AnyRecord,
    Evidence,
    Intervention,
    Outcome,
    Prediction,
    Reference,
    SourceRef,
    content_key,
    mint_id,
    record_from_dict,
    record_to_dict,
    recurrence_key,
)
from src.jsonl_store import JsonlStore
from src.session import SessionKind, is_synthetic_session, session_kind

if TYPE_CHECKING:
    from src.config import CanaryConfig

__all__ = [
    "ANY",
    "CorruptRecordError",
    "CrossVehicleReferenceError",
    "ForeignSupersessionError",
    "LedgerEntry",
    "MemoryLedger",
    "MemoryQuery",
    "MemoryReferenceError",
    "OutcomeAssessment",
    "OutcomeRuleError",
    "PredictionOrder",
    "ReplayIsolationError",
    "SyntheticIsolationError",
    "UnknownRecordError",
    "UnscopedMemoryError",
    "memory_ledger_for",
    "replay_memory_path",
]


_R = TypeVar("_R", bound=AnyRecord)


class MemoryReferenceError(ValueError):
    """A record id that cannot be used here (base class)."""


class UnknownRecordError(MemoryReferenceError):
    """No record with this id was ever minted on this ledger."""


class CorruptRecordError(MemoryReferenceError):
    """The id is on the ledger, but its stored line no longer validates."""


class CrossVehicleReferenceError(MemoryReferenceError):
    """The id (or record) belongs to another vehicle."""


class ForeignSupersessionError(MemoryReferenceError):
    """A tool superseding or retracting evidence another producer minted.

    Evidence is recomputed only by the tool that produced it: a learned
    correction re-minting the twin's subject would otherwise supersede the
    original discrepancy it explains (B08.5). The operator may still retract
    anything.
    """


class UnscopedMemoryError(ValueError):
    """Memory is per vehicle; an unscoped config has no vehicle to file under."""


class ReplayIsolationError(ValueError):
    """Replay output offered to live memory."""


class SyntheticIsolationError(ValueError):
    """Simulator output (a ``_sim-*`` trace) offered to memory as evidence (E07.6)."""


class OutcomeRuleError(ValueError):
    """An outcome that claims more than its prediction's floor can support."""


class _Any:
    """Filter value meaning "do not filter" (``None`` means "unknown")."""

    def __repr__(self) -> str:
        return "ANY"


#: Pass to a nullable :class:`MemoryQuery` filter to leave it open; ``None``
#: asks for records where that fact is unknown.
ANY = _Any()


@dataclass(frozen=True)
class LedgerEntry:
    """A record plus what later records say about it (derived, never stored).

    Derived as of a record time (``MemoryLedger.entry(..., as_of=...)``):
    supersessions, retractions and retirements recorded after it are not yet
    known.
    """

    record: AnyRecord
    #: ``recorded_at`` of the first record that superseded or retracted it.
    superseded_at: datetime | None = None
    #: Ids of the records that superseded it (empty if none).
    superseded_by: tuple[str, ...] = ()
    #: Id of the retraction that withdrew it, if one did.
    retracted_by: str | None = None
    #: References only: the intervention that retired it as historical, and
    #: when (the intervention's ``valid_from``: valid time, not record time).
    retired_by: str | None = None
    retired_at: datetime | None = None

    @property
    def current(self) -> bool:
        return self.superseded_at is None

    @property
    def retired(self) -> bool:
        return self.retired_by is not None


@dataclass(frozen=True)
class MemoryQuery:
    """Exact filters over the ledger (E01.3). Every set filter must hold.

    Times are aware datetimes. ``as_of`` answers "what did we believe then?":
    only records recorded at or before it, with supersessions, retractions and
    retirements as they stood then. ``valid_*`` filter on valid time (when the
    thing was true); ``recorded_*`` on record time. ``regime`` matches
    ``conditions.regime`` character for character (``noise_floor.yaml``'s
    rule). ``calibration``/``program_id``/``vehicle_state``/``operating_mode``
    take :data:`ANY` (no filter) or a value, ``None`` meaning "unknown".
    ``text`` matches every whitespace-separated term, case-insensitively, as a
    substring of the record's text (titles, subjects, labels, details,
    regimes, ids). Results are in record-time order.
    """

    kinds: tuple[str, ...] = ()
    #: An incident key (``Evidence.incident``/``Investigation.incident``).
    incident: str | None = None
    #: A :func:`~src.evidence.records.recurrence_key` (incident or subject).
    recurrence: str | None = None
    subject: str | None = None
    #: Evidence read from this session.
    session: str | None = None
    regime: str | None = None
    vehicle_state: str | None | _Any = ANY
    operating_mode: str | None | _Any = ANY
    calibration: str | None | _Any = ANY
    program_id: str | None | _Any = ANY
    #: Valid at this instant: ``valid_from <= t`` and not ended before it.
    valid_at: datetime | None = None
    #: ``valid_from`` strictly before / at-or-after this instant ("what
    #: preceded the flash?" is ``valid_before=<flash valid_from>``).
    valid_before: datetime | None = None
    valid_after: datetime | None = None
    recorded_after: datetime | None = None
    recorded_before: datetime | None = None
    as_of: datetime | None = None
    text: str | None = None
    #: Include superseded and retracted records (history), not just current.
    include_superseded: bool = False
    #: Include references retired by an intervention.
    include_retired: bool = True
    limit: int | None = None

    def __post_init__(self) -> None:
        unknown = sorted(set(self.kinds) - set(RECORD_KINDS))
        if unknown:
            raise ValueError(f"unknown record kind(s) {unknown}; known: {list(RECORD_KINDS)}")
        for name in (
            "valid_at",
            "valid_before",
            "valid_after",
            "recorded_after",
            "recorded_before",
            "as_of",
        ):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if self.limit is not None and self.limit < 1:
            raise ValueError("limit is at least one")


OutcomeVerdict = Literal["confirmed", "contradicted", "unresolvable", "post_hoc", "undetermined"]
#: ``undetermined``: the intervention's time is approximate, a floor or
#: unknown, so whether the prediction came first cannot be shown.
PredictionOrder = Literal["prior", "post_hoc", "undetermined"]


@dataclass(frozen=True)
class OutcomeAssessment:
    """Whether an outcome confirms its prediction — derived, never claimed.

    ``order`` compares the prediction's minted ``recorded_at`` with the
    instant it predicts across: the intervention's ``valid_from``, or, with
    no intervention, the earliest ``valid_from`` of the outcome's evidence. A
    post-hoc prediction's verdict is ``post_hoc`` whatever was measured; one
    against an intervention whose time is not pinned (``approximate``,
    ``floor``, ``unknown`` precision) is ``undetermined``. Neither is ever
    ``confirmed``.
    """

    outcome: str
    prediction: str
    intervention: str | None
    order: PredictionOrder
    verdict: OutcomeVerdict
    reason: str


@dataclass
class _Index:
    """Everything parsed from one ledger file, maintained incrementally."""

    inode: int | None = None
    offset: int = 0
    #: Trailing bytes with no newline yet (a torn or in-progress write).
    partial: bool = False
    #: The file's first bytes and the bytes just before ``offset``, as last
    #: read. A file rewritten in place (same inode, any length) or deleted and
    #: recreated on a reused inode no longer matches, and is re-read whole.
    head: bytes = b""
    tail: bytes = b""
    records: dict[str, AnyRecord] = field(default_factory=dict)
    corrupt: dict[str, str] = field(default_factory=dict)
    #: old id -> [(recorded_at, by id, "supersede" | "retract")]
    invalidated: dict[str, list[tuple[datetime, str, str]]] = field(default_factory=dict)
    #: (kind, valid_from) -> ids: identical content shares both.
    by_slot: dict[tuple[str, datetime], list[str]] = field(default_factory=dict)
    by_subject: dict[str, list[str]] = field(default_factory=dict)
    by_recurrence: dict[str, list[str]] = field(default_factory=dict)
    interventions: list[str] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)
    text: dict[str, str] = field(default_factory=dict)
    latest: datetime | None = None

    def add(self, record: AnyRecord) -> None:
        assert record.recorded_at is not None
        rid = record.id
        self.records[rid] = record
        self.corrupt.pop(rid, None)
        stamp = record.recorded_at
        for old in record.supersedes:
            self.invalidated.setdefault(old, []).append((stamp, rid, "supersede"))
        if record.kind == "retraction":
            for old in record.retracts:
                self.invalidated.setdefault(old, []).append((stamp, rid, "retract"))
        self.by_slot.setdefault((record.kind, record.valid_from), []).append(rid)
        if isinstance(record, Evidence):
            self.by_subject.setdefault(record.subject, []).append(rid)
        key = recurrence_key(record)
        if key is not None:
            self.by_recurrence.setdefault(key, []).append(rid)
        if isinstance(record, Intervention):
            self.interventions.append(rid)
        if self.latest is None or stamp > self.latest:
            self.latest = stamp

    def ingest_line(self, raw: bytes) -> None:
        """One stored line. A line that is JSON with an ``id`` but no longer
        validates is a corrupt record, not "never minted"; a line that is not
        JSON at all (a torn write) carries no id."""
        line = raw.strip()
        if not line:
            return
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(data, dict):
            return
        try:
            record = record_from_dict(data)
        except ValueError as error:
            record_id = data.get("id")
            if isinstance(record_id, str) and record_id and record_id not in self.records:
                self.corrupt[record_id] = str(error).splitlines()[0]
            return
        if record.id and record.recorded_at is not None:
            self.add(record)
        elif record.id:
            self.corrupt[record.id] = "stored without a record time"

    def key(self, rid: str) -> str:
        found = self.keys.get(rid)
        if found is None:
            found = self.keys[rid] = content_key(self.records[rid])
        return found


_HEAD_BYTES = 4096
_TAIL_BYTES = 512

#: One lock and one parsed index per ledger file, shared by every
#: :class:`MemoryLedger` on it in this process (route handlers build their own).
_LOCKS: dict[Path, threading.RLock] = {}
_INDEXES: dict[Path, _Index] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _now() -> datetime:
    return datetime.now(UTC)


def _overlaps(a: tuple[SourceRef, ...], b: tuple[SourceRef, ...]) -> bool:
    """The same data: a session in common with overlapping windows."""
    return any(
        x.session == y.session and x.window_start <= y.window_end and y.window_start <= x.window_end
        for x in a
        for y in b
    )


def _is_replay_session(name: str) -> bool:
    return session_kind(name) is SessionKind.REPLAY or name.startswith("_replay")


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _matches(value: object, wanted: object) -> bool:
    return isinstance(wanted, _Any) or value == wanted


class MemoryLedger(JsonlStore[AnyRecord]):
    """One vehicle's investigation memory (or one replay's isolated memory)."""

    def __init__(self, path: Path, *, vehicle: str, replay_id: str | None = None) -> None:
        if not vehicle:
            raise UnscopedMemoryError("a memory ledger belongs to one vehicle profile")
        super().__init__(path, from_dict=record_from_dict)
        self.vehicle = vehicle
        #: Set on a replay's isolated ledger; ``None`` on the live one.
        self.replay_id = replay_id
        self._key = self.path.resolve()

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
        if self.replay_id is None and isinstance(record, Evidence):
            replayed = [s.session for s in record.sources if _is_replay_session(s.session)]
            if replayed:
                raise ReplayIsolationError(
                    f"evidence read from replay output {replayed} cannot enter live memory: "
                    "a replay re-emits rows, it is not this vehicle's recording. Use the "
                    "replay's own ledger (memory_ledger_for(config, replay_id=...))."
                )
        if isinstance(record, Evidence):
            synthetic = [s.session for s in record.sources if is_synthetic_session(s.session)]
            if synthetic:
                raise SyntheticIsolationError(
                    f"evidence read from simulator output {synthetic} cannot enter memory on "
                    "any ledger: a simulated trace is a prediction, never an observation of "
                    "this vehicle (E07.6)"
                )
        if isinstance(record, Prediction) and record.noise_floor is not None:
            verify_floor(record.noise_floor)  # FloorVerificationError: relabelled/invented
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # In-process lock for threads, then an exclusive flock on the ledger
        # file itself for other processes: refresh, decide and write are one
        # step, so two writers cannot both supersede the same record.
        with _lock_for(self.path), self.path.open("ab") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            return self._append_locked(record, now)

    def _append_locked(self, record: AnyRecord, now: datetime | None) -> AnyRecord:
        index = self._refresh()
        key = content_key(record)
        for rid in index.by_slot.get((record.kind, record.valid_from), ()):
            # A duplicate only of what is believed now: content that was
            # superseded or retracted, stated again, is a new record.
            if rid not in index.invalidated and index.key(rid) == key:
                return index.records[rid]
        for ref, kind in record.typed_references():
            self._check_reference(index, ref, kind)
        invalidates = (*record.supersedes, *getattr(record, "retracts", ()))
        for ref in invalidates:
            if index.records[ref].kind == "retraction":
                raise MemoryReferenceError(
                    f"{ref!r} is a retraction; a retraction cannot be retracted or "
                    "superseded. To withdraw it, append the withdrawn finding again "
                    "(a new record, current from its own record time)"
                )
        if record.provenance.producer_kind == "tool":
            for ref in invalidates:
                self._check_same_producer(index, record, ref)
        if isinstance(record, Outcome):
            self._check_outcome(index, record)
        update: dict[str, object] = {}
        if isinstance(record, Evidence):
            stale = [
                rid
                for rid in index.by_subject.get(record.subject, ())
                if rid not in index.invalidated
                and index.records[rid].vehicle.profile == self.vehicle
                and _overlaps(record.sources, self._evidence(index, rid).sources)
            ]
            for rid in stale:
                self._check_same_producer(index, record, rid)
            if stale:
                update["supersedes"] = tuple(dict.fromkeys((*record.supersedes, *stale)))
                update["supersession_reason"] = record.supersession_reason or (
                    f"recomputed {record.subject} with different inputs, code or values"
                )
        stamp = now if now is not None else _now()
        if index.latest is not None and stamp < index.latest:
            raise ValueError("record time cannot move backwards on an append-only ledger")
        update["recorded_at"] = stamp
        stamped = record.model_copy(update=update)
        salt = 0
        minted = mint_id(stamped)
        while minted in index.records or minted in index.corrupt:
            salt += 1
            minted = mint_id(stamped, salt)
        stored = record_from_dict(
            record_to_dict(stamped.model_copy(update={"id": minted}))  # re-validated whole
        )
        self._write(index, stored)
        return stored

    # -- reads ------------------------------------------------------------

    def get(self, record_id: str) -> AnyRecord:
        return self.entry(record_id).record

    def entry(self, record_id: str, *, as_of: datetime | None = None) -> LedgerEntry:
        """The record with its derived state (as of a record time, if given),
        or a refusal naming which kind of bad id it is."""
        with _lock_for(self.path):
            index = self._refresh()
            self._check_reference(index, record_id, None)
            record = index.records[record_id]
            if as_of is not None and record.recorded_at is not None and record.recorded_at > as_of:
                raise UnknownRecordError(
                    f"{record_id!r} was recorded at {record.recorded_at.isoformat()}, after "
                    f"{as_of.isoformat()}: it was not yet believed then"
                )
            return self._entry(index, record_id, as_of)

    def resolve_evidence(self, evidence_id: str) -> LedgerEntry:
        """An evidence id a card or record cites, resolved — or refused."""
        with _lock_for(self.path):
            index = self._refresh()
            self._check_reference(index, evidence_id, "evidence")
            return self._entry(index, evidence_id, None)

    def entries(self) -> list[LedgerEntry]:
        """Every record in write order, with its supersession state."""
        with _lock_for(self.path):
            index = self._refresh()
            return [self._entry(index, rid, None) for rid in index.records]

    def history(self, record_id: str) -> list[LedgerEntry]:
        """``record_id`` and every record it superseded, oldest first."""
        with _lock_for(self.path):
            index = self._refresh()
            self._check_reference(index, record_id, None)
            chain: list[LedgerEntry] = []
            seen: set[str] = set()
            frontier = [record_id]
            while frontier:
                current = frontier.pop()
                if current in seen or current not in index.records:
                    continue
                seen.add(current)
                chain.append(self._entry(index, current, None))
                frontier.extend(index.records[current].supersedes)
        return sorted(chain, key=lambda e: e.record.recorded_at or datetime.min.replace(tzinfo=UTC))

    def query(self, query: MemoryQuery | None = None, /, **filters: Any) -> list[LedgerEntry]:
        """Records matching ``query`` (or :class:`MemoryQuery` keyword filters).

        This vehicle's records only. Linear in the records on file (no file
        read beyond new appends); hundreds to tens of thousands of records need
        no full-text engine (measured in E01.3).
        """
        if query is None:
            query = MemoryQuery(**filters)
        elif filters:
            raise TypeError("pass a MemoryQuery or keyword filters, not both")
        terms = query.text.lower().split() if query.text else []
        found: list[LedgerEntry] = []
        with _lock_for(self.path):
            index = self._refresh()
            if query.recurrence is not None:
                candidates: Iterable[str] = index.by_recurrence.get(query.recurrence, ())
            elif query.subject is not None:
                candidates = index.by_subject.get(query.subject, ())
            else:
                candidates = index.records
            for rid in candidates:
                record = index.records[rid]
                if not self._record_matches(record, query):
                    continue
                entry = self._entry(index, rid, query.as_of)
                if not query.include_superseded and not entry.current:
                    continue
                if not query.include_retired and entry.retired:
                    continue
                if query.valid_at is not None and not self._valid_at(entry, query.valid_at):
                    continue
                if terms and not self._text_matches(index, rid, terms):
                    continue
                found.append(entry)
                if query.limit is not None and len(found) >= query.limit:
                    break
        return found

    def recurrences(self, record_id: str, *, as_of: datetime | None = None) -> list[LedgerEntry]:
        """Every current occurrence of what ``record_id`` is an occurrence of,
        in valid-time order (the record itself included).

        Occurrences share a :func:`~src.evidence.records.recurrence_key`: an
        evidence record's ``incident`` (or its subject, the same evaluation
        over other data). A retried append is the same record, not a
        recurrence; a recompute supersedes, so it is one occurrence.
        """
        record = self.entry(record_id, as_of=as_of).record
        key = recurrence_key(record)
        if key is None:
            return [self.entry(record_id, as_of=as_of)]
        occurrences = self.query(MemoryQuery(recurrence=key, kinds=("evidence",), as_of=as_of))
        return sorted(occurrences, key=lambda e: (e.record.valid_from, e.record.recorded_at))

    def prediction_order(
        self, prediction_id: str, *, intervention: str | None = None
    ) -> PredictionOrder | None:
        """Whether a prediction was recorded before its intervention took effect.

        ``intervention`` defaults to the one the prediction names; ``None``
        when there is none to order against.
        """
        prediction = self._typed(prediction_id, Prediction)
        target = intervention or prediction.intervention
        if target is None:
            return None
        change = self._typed(target, Intervention)
        assert prediction.recorded_at is not None
        if change.effective_precision not in ORDERABLE_PRECISIONS:
            return "undetermined"
        return "prior" if prediction.recorded_at < change.valid_from else "post_hoc"

    def what_if_intervention(self, prediction_id: str) -> str | None:
        """The flash a what-if prediction (E02.6) is ordered against.

        The intervention it names; else the first calibration intervention
        installing its target image that took effect after the prediction was
        recorded. An earlier flash of the same image (re-flashing a previous
        calibration) is not its change. ``None`` while no such flash is on the
        ledger, and always for an unlinked cell edit set, which names no image
        to match a flash by. Refuses a prediction that is not a what-if.
        """
        prediction = self._typed(prediction_id, Prediction)
        what_if = prediction.what_if
        if what_if is None:
            raise MemoryReferenceError(f"{prediction_id!r} is not a what-if prediction")
        if prediction.intervention is not None:
            return prediction.intervention
        if what_if.target_image_sha256 is None:
            return None
        assert prediction.recorded_at is not None
        after = sorted(
            (
                entry.record
                for entry in self.query(MemoryQuery(kinds=("intervention",)))
                if isinstance(entry.record, Intervention)
                and entry.record.intervention_kind == "calibration"
                and entry.record.image_sha256 == what_if.target_image_sha256
                and entry.record.valid_from > prediction.recorded_at
            ),
            key=lambda record: record.valid_from,
        )
        return after[0].id if after else None

    def assess(self, outcome_id: str) -> OutcomeAssessment:
        """Does this outcome confirm its prediction? Derived; see :class:`OutcomeAssessment`."""
        outcome = self._typed(outcome_id, Outcome)
        prediction = self._typed(outcome.prediction, Prediction)
        intervention = outcome.intervention or prediction.intervention
        assert prediction.recorded_at is not None
        orderable = True
        if intervention is not None:
            change = self._typed(intervention, Intervention)
            instant = change.valid_from
            against = f"intervention {intervention} took effect"
            orderable = change.effective_precision in ORDERABLE_PRECISIONS
        else:
            instant = min(self._typed(ev, Evidence).valid_from for ev in outcome.evidence)
            against = "the evaluated data begins"
        order: PredictionOrder = (
            "undetermined"
            if not orderable
            else "prior"
            if prediction.recorded_at < instant
            else "post_hoc"
        )

        def judged(verdict: OutcomeVerdict, reason: str) -> OutcomeAssessment:
            return OutcomeAssessment(
                outcome=outcome_id,
                prediction=prediction.id,
                intervention=intervention,
                order=order,
                verdict=verdict,
                reason=reason,
            )

        if order == "undetermined":
            return judged(
                "undetermined",
                f"{against} at a time that is not pinned (approximate, floor or "
                "unknown precision): the prediction cannot be shown to precede "
                "it, so it cannot count as confirmed",
            )
        if order == "post_hoc":
            return judged(
                "post_hoc",
                (
                    f"prediction recorded {prediction.recorded_at.isoformat()}, not before "
                    f"{against} at {instant.isoformat()}: post-hoc, cannot count as confirmed"
                ),
            )
        if outcome.disposition == "unresolvable":
            return judged("unresolvable", "the outcome could not resolve an effect")
        if outcome.disposition == "no_effect":
            if prediction.direction == "no_change":
                return judged("confirmed", "no change predicted; none measured")
            return judged(
                "contradicted",
                f"{prediction.direction} predicted; no effect beyond the noise floor",
            )
        residual = outcome.result.residual
        assert residual is not None
        measured = "increase" if residual.value > 0 else "decrease"
        if prediction.direction == measured:
            return judged("confirmed", f"{measured} predicted and measured")
        return judged(
            "contradicted",
            f"{prediction.direction} predicted; {measured} measured",
        )

    def reload(self) -> None:
        """Forget this process's parsed index of the file (as a restart would)."""
        with _lock_for(self.path):
            _INDEXES.pop(self._key, None)

    # -- internals --------------------------------------------------------

    def _refresh(self) -> _Index:
        """The index, with any bytes appended since the last read parsed in.

        Re-reads the whole file when it is not the file the index was built
        from: another inode, shorter than what was read, or different bytes
        at the head or just before the read offset (an in-place rewrite, or
        a delete and recreate that reused the inode).
        """
        index = _INDEXES.get(self._key)
        try:
            handle = self.path.open("rb")
        except FileNotFoundError:
            index = _INDEXES[self._key] = _Index()
            return index
        with handle:
            stat = os.fstat(handle.fileno())
            if (
                index is None
                or index.inode != stat.st_ino
                or stat.st_size < index.offset
                or not self._same_bytes(handle, index)
            ):
                index = _INDEXES[self._key] = _Index(inode=stat.st_ino)
            if stat.st_size == index.offset:
                return index
            handle.seek(index.offset)
            data = handle.read(stat.st_size - index.offset)
            end = data.rfind(b"\n")
            index.partial = end + 1 < len(data)
            if end < 0:
                return index
            for line in data[: end + 1].split(b"\n"):
                index.ingest_line(line)
            index.offset += end + 1
            self._fingerprint(handle, index)
        return index

    @staticmethod
    def _fingerprint(handle: Any, index: _Index) -> None:
        handle.seek(0)
        index.head = handle.read(min(index.offset, _HEAD_BYTES))
        start = max(0, index.offset - _TAIL_BYTES)
        handle.seek(start)
        index.tail = handle.read(index.offset - start)

    @staticmethod
    def _same_bytes(handle: Any, index: _Index) -> bool:
        if index.offset == 0:
            return True
        handle.seek(0)
        if handle.read(len(index.head)) != index.head:
            return False
        handle.seek(index.offset - len(index.tail))
        return bool(handle.read(len(index.tail)) == index.tail)

    def _write(self, index: _Index, stored: AnyRecord) -> None:
        """Append one line (after terminating a torn tail) and index it."""
        line = json.dumps(record_to_dict(stored), sort_keys=True, default=str) + "\n"
        payload = ("\n" if index.partial else "") + line
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as handle:
            handle.write(payload.encode())
            handle.flush()
            size = handle.tell()
        stat = os.stat(self.path)
        if index.inode is None and index.offset == 0 and not index.partial:
            index.inode = stat.st_ino  # this append created the file
        if (
            stat.st_ino == index.inode
            and size == stat.st_size
            and size - len(line.encode()) - index.offset == (1 if index.partial else 0)
        ):
            # Nobody else wrote in between: index the record we hold.
            index.add(stored)
            index.offset = size
            index.partial = False
            with self.path.open("rb") as reread:
                self._fingerprint(reread, index)
        else:
            self._refresh()

    def _entry(self, index: _Index, rid: str, as_of: datetime | None) -> LedgerEntry:
        record = index.records[rid]
        events = [
            event
            for event in index.invalidated.get(rid, ())
            if (as_of is None or event[0] <= as_of) and event[1] in index.records
        ]
        superseded_at = min((event[0] for event in events), default=None)
        retired_by: str | None = None
        retired_at: datetime | None = None
        if isinstance(record, Reference):
            retired_by, retired_at = self._retirement(index, record, as_of)
        return LedgerEntry(
            record=record,
            superseded_at=superseded_at,
            superseded_by=tuple(event[1] for event in events if event[2] == "supersede"),
            retracted_by=next((event[1] for event in events if event[2] == "retract"), None),
            retired_by=retired_by,
            retired_at=retired_at,
        )

    def _retirement(
        self, index: _Index, reference: Reference, as_of: datetime | None
    ) -> tuple[str | None, datetime | None]:
        """The earliest standing intervention that retires ``reference``."""
        best: Intervention | None = None
        for rid in index.interventions:
            change = index.records[rid]
            assert isinstance(change, Intervention) and change.recorded_at is not None
            if as_of is not None and change.recorded_at > as_of:
                continue
            if change.vehicle.profile != reference.vehicle.profile:
                continue
            if any(
                (as_of is None or event[0] <= as_of) for event in index.invalidated.get(rid, ())
            ):
                continue  # corrected or retracted: it no longer stands
            if change.valid_from <= reference.valid_from or not change.retires(reference.channels):
                continue
            if best is None or change.valid_from < best.valid_from:
                best = change
        return (None, None) if best is None else (best.id, best.valid_from)

    @staticmethod
    def _valid_at(entry: LedgerEntry, instant: datetime) -> bool:
        record = entry.record
        if record.valid_from > instant:
            return False
        ends = [t for t in (record.valid_to, entry.retired_at) if t is not None]
        return not ends or instant <= min(ends)

    def _record_matches(self, record: AnyRecord, query: MemoryQuery) -> bool:
        if record.vehicle.profile != self.vehicle:
            return False
        if query.kinds and record.kind not in query.kinds:
            return False
        assert record.recorded_at is not None
        if query.as_of is not None and record.recorded_at > query.as_of:
            return False
        if query.recorded_after is not None and record.recorded_at < query.recorded_after:
            return False
        if query.recorded_before is not None and record.recorded_at > query.recorded_before:
            return False
        if query.valid_before is not None and not record.valid_from < query.valid_before:
            return False
        if query.valid_after is not None and record.valid_from < query.valid_after:
            return False
        if not _matches(record.vehicle.calibration, query.calibration):
            return False
        if not _matches(record.vehicle.program_id, query.program_id):
            return False
        if query.incident is not None and getattr(record, "incident", None) != query.incident:
            return False
        if query.subject is not None and getattr(record, "subject", None) != query.subject:
            return False
        if query.session is not None and not (
            isinstance(record, Evidence)
            and any(source.session == query.session for source in record.sources)
        ):
            return False
        conditions = getattr(record, "conditions", None)
        conditioned = (
            query.regime is not None
            or not isinstance(query.vehicle_state, _Any)
            or not isinstance(query.operating_mode, _Any)
        )
        if conditioned:
            if conditions is None:
                return False
            if query.regime is not None and conditions.regime != query.regime:
                return False
            if not _matches(conditions.vehicle_state, query.vehicle_state):
                return False
            if not _matches(conditions.operating_mode, query.operating_mode):
                return False
        return True

    @staticmethod
    def _text_matches(index: _Index, rid: str, terms: list[str]) -> bool:
        text = index.text.get(rid)
        if text is None:
            text = index.text[rid] = "\n".join(_strings(record_to_dict(index.records[rid]))).lower()
        return all(term in text for term in terms)

    @staticmethod
    def _evidence(index: _Index, rid: str) -> Evidence:
        record = index.records[rid]
        assert isinstance(record, Evidence)
        return record

    def _typed(self, record_id: str, kind: type[_R]) -> _R:
        record = self.get(record_id)
        if not isinstance(record, kind):
            raise MemoryReferenceError(f"{record_id!r} is a {record.kind} record, not {kind}")
        return record

    def _check_outcome(self, index: _Index, outcome: Outcome) -> None:
        prediction = index.records[outcome.prediction]
        assert isinstance(prediction, Prediction)
        if outcome.conditions.regime != prediction.conditions.regime:
            raise OutcomeRuleError(
                f"the outcome was evaluated under {outcome.conditions.regime!r}; its prediction "
                f"is for {prediction.conditions.regime!r}. Evaluate under matched conditions."
            )
        if (
            outcome.intervention is not None
            and prediction.intervention is not None
            and outcome.intervention != prediction.intervention
        ):
            raise OutcomeRuleError(
                f"the outcome names intervention {outcome.intervention!r}; its prediction "
                f"was made for {prediction.intervention!r}"
            )
        if outcome.disposition == "unresolvable":
            return
        target = outcome.intervention or prediction.intervention
        if target is not None:
            change = index.records[target]
            assert isinstance(change, Intervention)
            early = [
                ev for ev in outcome.evidence if index.records[ev].valid_from < change.valid_from
            ]
            if early:
                raise OutcomeRuleError(
                    f"evidence {early} starts before intervention {target} took effect "
                    f"({change.valid_from.isoformat()}): it cannot measure the change's "
                    "outcome; the outcome is 'unresolvable' on it"
                )
        floor = prediction.noise_floor
        if floor is None:
            raise OutcomeRuleError(
                f"{outcome.disposition!r} needs the prediction's condition-matched noise "
                "floor; without one a change cannot be told from noise, and the outcome "
                "is 'unresolvable'"
            )
        residual = outcome.result.residual
        if residual is None:
            return  # no_effect with no residual stated
        if residual.unit != floor.sd.unit:
            raise OutcomeRuleError(
                f"the residual is in {residual.unit!r} but the noise floor in "
                f"{floor.sd.unit!r}; compare like with like"
            )
        beyond = abs(residual.value) >= floor.detectable
        if outcome.disposition == "measured_change" and not beyond:
            raise OutcomeRuleError(
                f"the residual is within the noise floor ({floor.sigma_threshold:g} sd of "
                f"{floor.quantity} under {floor.conditions!r}): that is 'no_effect'"
            )
        if outcome.disposition == "no_effect" and beyond:
            raise OutcomeRuleError(
                "the residual clears the noise floor: that is a 'measured_change'"
            )

    @staticmethod
    def _check_same_producer(index: _Index, record: AnyRecord, ref: str) -> None:
        """Refuse a tool invalidating evidence another producer minted."""
        prior = index.records[ref]
        if not isinstance(prior, Evidence):
            return
        if prior.provenance.producer != record.provenance.producer:
            raise ForeignSupersessionError(
                f"{record.provenance.producer!r} cannot supersede or retract {ref} "
                f"({prior.kind} by {prior.provenance.producer!r}): evidence is recomputed "
                "only by its own producer, and only the operator retracts another's"
            )

    def _check_reference(self, index: _Index, ref: str, kind: str | None) -> None:
        record = index.records.get(ref)
        if record is None:
            if ref in index.corrupt:
                raise CorruptRecordError(
                    f"{ref!r} is a corrupt record on {self.vehicle!r}'s memory ledger "
                    f"(the stored line fails the schema: {index.corrupt[ref]})"
                )
            raise UnknownRecordError(
                f"{ref!r} was not minted on {self.vehicle!r}'s memory ledger; "
                "cite only ids a tool returned"
            )
        if record.vehicle.profile != self.vehicle:
            raise CrossVehicleReferenceError(
                f"{ref!r} belongs to vehicle {record.vehicle.profile!r}, not {self.vehicle!r}"
            )
        if kind is not None and record.kind != kind:
            raise MemoryReferenceError(f"{ref!r} is a {record.kind} record, not {kind}")


def replay_memory_path(live_path: Path, replay_id: str) -> Path:
    """A replay's isolated memory ledger, beside the live one under ``_replay/``.

    Mirrors the plausibility evidence's replay isolation
    (``<anomalies_dir>/_replay/<replay_id>.…``): the leading ``_`` keeps it out
    of every live reader, and nothing a replay mints can reach the live file.
    """
    if not replay_id or "/" in replay_id or replay_id.startswith("."):
        raise ValueError(f"not a replay id: {replay_id!r}")
    return live_path.parent / "_replay" / f"{replay_id}.memory.jsonl"


def memory_ledger_for(config: CanaryConfig, *, replay_id: str | None = None) -> MemoryLedger:
    """The ledger at ``config.daemon.memory_ledger`` for the scoped vehicle,
    or, with ``replay_id``, that replay's isolated ledger beside it.

    Refuses an unscoped config: memory is per vehicle, and a ledger filed
    under no profile would mix vehicles' findings.
    """
    slug = config.scoped_profile
    if slug is None:
        raise UnscopedMemoryError(
            "investigation memory is per vehicle: scope the config to a profile "
            "(src.vehicle.profiles.scope_config) before opening the memory ledger"
        )
    if replay_id is None:
        return MemoryLedger(config.daemon.memory_ledger, vehicle=slug)
    return MemoryLedger(
        replay_memory_path(config.daemon.memory_ledger, replay_id),
        vehicle=slug,
        replay_id=replay_id,
    )
