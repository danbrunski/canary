"""Generic append-only JSONL store base (Task 3.4, refactoring.md).

``FeedbackStore`` (``src.models.feedback``), ``TrainingLedger``
(``src.models.training_ledger``), and ``VehicleLedgerStore``
(``src.vehicle.vehicle_ledger``) were three byte-for-byte identical
implementations of the same shape: append one JSON line (mkdir + open
``"a"`` + write + flush), tolerantly re-read the whole file skipping
blank/malformed lines, and — for two of the three — collapse repeated
appends of the same key down to the latest one. This module is that shape,
pulled out once.

Not every JSONL writer in the codebase fits this shape, and forcing them to
would trade a real divergence for a false one:

* ``AnomalyEventLogger`` (``src.alerts.event_logger``) keeps one file handle
  open across writes and rotates it by day/replay-boundary instead of
  opening fresh per append — a genuinely different write path, not just a
  different record type.
* ``ControlAuditLogger`` (``src.control.audit``) is write-only (no read-side
  at all), has no JSON-safe record *model* (a plain dict payload), and is
  thread-locked because control-plane requests can call it concurrently.
  It still reuses :func:`append_jsonl_line` for its own append body.
* The job ledger (``src.control.jobs.JobController``) keeps an in-memory
  ``dict[str, JobRecord]`` loaded once at startup and mutated incrementally
  under a lock (concurrent job execution needs that cache); it does not
  re-scan the file on every read the way the three stores below do. It
  still reuses :func:`append_jsonl_line` for its own append body.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Callable, Hashable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Generic, NamedTuple, TypeVar

R = TypeVar("R")
K = TypeVar("K", bound=Hashable)


def append_jsonl_line(path: Path, payload: dict[str, Any], *, sort_keys: bool = True) -> None:
    """Append one JSON line to ``path`` (mkdir + open + write + flush).

    The innermost primitive shared by every append-only JSONL writer in the
    codebase, including the ones that don't otherwise fit :class:`JsonlStore`
    (``ControlAuditLogger``, the job ledger) — callers that need locking
    take it themselves (:func:`path_lock`, or :func:`append_jsonl_line_locked`
    for a writer that is also read by another process).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(payload, sort_keys=sort_keys, default=str) + "\n")
        handle.flush()


# -- locking ------------------------------------------------------------------

#: One in-process lock per resolved file, shared by every store on it.
_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def path_lock(path: Path) -> threading.RLock:
    """The one in-process lock for the file at ``path`` (by resolved path).

    Every store, route handler and writer that touches a file takes this same
    lock, however many instances it builds. It is **reentrant**: a method that
    holds it can call another that takes it (``BrainJobStore.submit`` holds it
    through ``get`` and ``transition``; the evidence ledger's reads and its
    append nest the same way), and a plain lock deadlocks the first such call.
    A caller that never nests loses nothing to reentrancy.
    """
    key = Path(path).resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """:func:`path_lock` plus an exclusive ``flock`` on the file itself.

    The in-process lock serializes threads and the ``flock`` serializes
    processes, so a read, a decision and a write made inside are one step.
    Creates the file (and its directory) if missing. ``flock`` locks belong to
    the open file description, so code inside must not take this (or
    :func:`append_jsonl_line_locked`) again on a *different* handle: a second
    ``flock`` in the same process blocks forever. Use
    :func:`append_jsonl_line_repairing` inside.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path_lock(path), path.open("ab") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


class Appended(NamedTuple):
    """Where an append landed: the file's size after it, and the bytes written
    (the line, plus the newline that terminated a torn tail, if there was one)."""

    end: int
    written: int


def _write_line_repairing(handle: IO[bytes], payload: dict[str, Any], sort_keys: bool) -> Appended:
    line = (json.dumps(payload, sort_keys=sort_keys, default=str) + "\n").encode()
    handle.seek(0, 2)
    if handle.tell():
        handle.seek(-1, 2)
        if handle.read(1) != b"\n":
            # A torn tail (power cut mid-line) must not swallow this line.
            line = b"\n" + line
    handle.write(line)
    handle.flush()
    return Appended(end=os.fstat(handle.fileno()).st_size, written=len(line))


def append_jsonl_line_repairing(
    path: Path, payload: dict[str, Any], *, sort_keys: bool = True
) -> Appended:
    """Append one JSON line, first terminating a torn final line.

    No locking: the caller holds :func:`file_lock` (or accepts a racing
    writer). Flushed, not fsynced.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        return _write_line_repairing(handle, payload, sort_keys)


def append_jsonl_line_locked(
    path: Path, payload: dict[str, Any], *, sort_keys: bool = True
) -> Appended:
    """Append one JSON line under :func:`file_lock`, repairing a torn tail.

    For a file several threads *and processes* append to. Flushed, not
    fsynced (durability is the caller's separate decision).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path_lock(path), path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return _write_line_repairing(handle, payload, sort_keys)


# -- incremental reading ------------------------------------------------------


@dataclass(frozen=True)
class TailRead:
    """What :meth:`TailIndex.read_new` found.

    ``reset`` means the file is not the one the index was built from (first
    read, replaced, truncated, rewritten in place, or missing): whatever the
    caller derived from earlier lines is void and ``lines`` are the whole file.
    ``lines`` are the complete lines appended since the last read, without
    their newline; a torn tail is left for the read that finds it finished.
    """

    reset: bool
    lines: list[bytes]


class TailIndex:
    """Where a reader of an append-only file has got to, so each read parses
    only the bytes appended since the last.

    The caller keeps what it derives from the lines; this keeps the position.
    A file that shrank or was replaced (another inode or device) is read whole.
    With ``verify_bytes`` it also notices a rewrite that kept the inode and did
    not shrink, by comparing the file's first and last bytes already read with
    what it saw (the evidence ledger's check; the job log does without).
    """

    HEAD_BYTES = 4096
    TAIL_BYTES = 512

    def __init__(self, *, verify_bytes: bool = False) -> None:
        self.verify_bytes = verify_bytes
        self.identity: tuple[int, int] | None = None
        self.offset = 0
        self._head = b""
        self._tail = b""

    def reset(self) -> None:
        self.identity = None
        self.offset = 0
        self._head = b""
        self._tail = b""

    def read_new(self, path: Path) -> TailRead:
        """The complete lines written to ``path`` since the last call."""
        try:
            handle = Path(path).open("rb")
        except FileNotFoundError:
            self.reset()
            return TailRead(reset=True, lines=[])
        with handle:
            stat = os.fstat(handle.fileno())
            identity = (stat.st_ino, stat.st_dev)
            reset = False
            if (
                identity != self.identity
                or stat.st_size < self.offset
                or (self.verify_bytes and not self._same_bytes(handle))
            ):
                self.reset()
                self.identity = identity
                reset = True
            if stat.st_size == self.offset:
                return TailRead(reset, [])
            handle.seek(self.offset)
            data = handle.read(stat.st_size - self.offset)
            end = data.rfind(b"\n")
            if end < 0:
                return TailRead(reset, [])
            self.offset += end + 1
            if self.verify_bytes:
                self._fingerprint(handle)
            return TailRead(reset, data[:end].split(b"\n"))

    def accept_own_append(self, path: Path, appended: Appended) -> bool:
        """Skip past a line this process just appended, if nothing else was
        written since the last read; the caller has indexed it itself.

        ``False`` (no change) when another writer got in between, or the file
        is not the one indexed: the next :meth:`read_new` picks it all up.
        """
        stat = os.stat(path)
        identity = (stat.st_ino, stat.st_dev)
        if self.identity is None and self.offset == 0 and appended.end == appended.written:
            self.identity = identity  # this append created the file
        if (
            identity != self.identity
            or appended.end != stat.st_size
            or appended.end - appended.written != self.offset
        ):
            return False
        self.offset = appended.end
        if self.verify_bytes:
            with Path(path).open("rb") as handle:
                self._fingerprint(handle)
        return True

    def _fingerprint(self, handle: IO[bytes]) -> None:
        handle.seek(0)
        self._head = handle.read(min(self.offset, self.HEAD_BYTES))
        start = max(0, self.offset - self.TAIL_BYTES)
        handle.seek(start)
        self._tail = handle.read(self.offset - start)

    def _same_bytes(self, handle: IO[bytes]) -> bool:
        if self.offset == 0:
            return True
        handle.seek(0)
        if handle.read(len(self._head)) != self._head:
            return False
        handle.seek(self.offset - len(self._tail))
        return bool(handle.read(len(self._tail)) == self._tail)


def latest_by_key(records: Iterable[R], key: Callable[[R], K]) -> dict[K, R]:
    """Collapse an append-only sequence to the latest record per ``key``.

    Walking in write order and overwriting on each match is exactly "a later
    line for the same key wins" — the shared inner loop of
    ``FeedbackStore.labels``, ``TrainingLedger.records``, and
    ``VehicleLedgerStore.records``, which otherwise differ in what they sort
    or wrap the result in.
    """
    latest: dict[K, R] = {}
    for record in records:
        latest[key(record)] = record
    return latest


class JsonlStore(Generic[R]):
    """Append-only JSONL store: append+flush, tolerant re-read, no schema.

    Callers that need a "latest per key" view build it themselves with
    :func:`latest_by_key` over :meth:`raw_records` — that step's ordering/
    return-shape genuinely diverges per store (a dict vs. a sorted list, and
    what field the sort key even is), so it isn't collapsed here too.

    Not thread-safe on its own, matching every subclass's prior docstring —
    callers serialize writes themselves (the daemon does, the same way for
    all of them).
    """

    def __init__(self, path: Path, *, from_dict: Callable[[dict[str, Any]], R]) -> None:
        self.path = Path(path)
        self._from_dict = from_dict

    def _append_record(self, record: R, *, to_dict: Callable[[R], dict[str, Any]]) -> R:
        """Append one record (flushed; crash loses at most the last line)."""
        append_jsonl_line(self.path, to_dict(record))
        return record

    def _iter_raw(self) -> Iterator[R]:
        if not self.path.exists():
            return
        with self.path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield self._from_dict(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue

    def raw_records(self) -> list[R]:
        """Every appended line in write order (the full provenance chain)."""
        return list(self._iter_raw())


__all__ = [
    "Appended",
    "JsonlStore",
    "TailIndex",
    "TailRead",
    "append_jsonl_line",
    "append_jsonl_line_locked",
    "append_jsonl_line_repairing",
    "file_lock",
    "latest_by_key",
    "path_lock",
]
