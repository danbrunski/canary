"""Generic append-only JSONL store base (Task 3.4, refactoring.md).

``FeedbackStore`` (``src.alerts.feedback``), ``TrainingLedger``
(``src.models.training_ledger``), and ``VehicleLedgerStore``
(``src.analysis.vehicle_ledger``) were three byte-for-byte identical
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

import json
from collections.abc import Callable, Hashable, Iterable, Iterator
from pathlib import Path
from typing import Any, Generic, TypeVar

R = TypeVar("R")
K = TypeVar("K", bound=Hashable)


def append_jsonl_line(path: Path, payload: dict[str, Any], *, sort_keys: bool = True) -> None:
    """Append one JSON line to ``path`` (mkdir + open + write + flush).

    The innermost primitive shared by every append-only JSONL writer in the
    codebase, including the ones that don't otherwise fit :class:`JsonlStore`
    (``ControlAuditLogger``, the job ledger) — callers that need locking
    take it themselves around this call.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(payload, sort_keys=sort_keys, default=str) + "\n")
        handle.flush()


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


__all__ = ["JsonlStore", "append_jsonl_line", "latest_by_key"]
