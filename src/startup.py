"""When did the daemon start recording? (S01.1)

A cold boot on the Pi takes about a minute before anything is stored, and the
only ways to see where that minute went were to query the session's Parquet or
attach ``py-spy``. The log could not be trusted for it: after ``04b6cd0`` it
said "collectors started" 25 s before any collector ran, because the retention
sweep held the event loop in between.

:class:`StartupTimeline` records each startup stage once, as seconds from the
process start, and is served as the ``startup`` block of ``GET /status``. The
stage that matters is ``first_record_stored``: it is marked by the storage sink
after the writer accepted the record, not by a collector, so it cannot run
ahead of what was actually recorded.

The unit's own start (``systemctl show -p ExecMainStartTimestampMonotonic``) is
not readable as the ``canary`` user, so the origin is this process's start
time from ``/proc/self/stat``. The unit's ``ExecStart`` is this process, so the
two differ only by the ``exec`` itself.

Stdlib only: ``src.main`` imports this before anything heavy.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: Every stage, in the order a normal start reaches them. Keyed stages
#: (:data:`KEYED_STAGES`) also carry one time per source / model.
STAGES: tuple[str, ...] = (
    # ``src.main`` imported and ``main()`` entered: interpreter start-up and
    # the entry point's imports.
    "main_entered",
    # The VIN probe (S01.4) and profile scoping done.
    "profile_resolved",
    # The "canary starting" log line.
    "canary_starting",
    # ``src.daemon`` imported (S01.2/S01.3 keep this short).
    "daemon_imported",
    "components_built",
    # The collector tasks exist; the first ``await`` runs them.
    "collector_tasks_created",
    # The storage sink's writer accepted the first record (any source).
    "first_record_stored",
    "live_source_ready",
    # The model poller's first pass over every manager finished, loaded or not.
    "models_polled",
    "app_built",
    "http_bound",
)

#: Stages recorded once per key as well: ``first_record_stored`` per source,
#: ``model_loaded`` per model manager.
KEYED_STAGES: tuple[str, ...] = ("first_record_stored", "model_loaded")

ORIGIN_NOTE = (
    "seconds from the process start in /proc/self/stat; the unit's own start "
    "timestamp is not readable as the canary user, and its ExecStart is this process"
)


class _StoredRecord(Protocol):
    @property
    def source(self) -> str: ...

    @property
    def timestamp(self) -> datetime: ...


def process_age_seconds() -> float | None:
    """How long ago this process started, from ``/proc/self/stat``.

    ``starttime`` (field 22) is in clock ticks since boot, on the boot-time
    clock. ``None`` where there is no procfs.
    """
    try:
        with open("/proc/self/stat", encoding="ascii") as handle:
            stat = handle.read()
        # comm (field 2) may contain spaces and parentheses; fields resume
        # after the last ')', at field 3.
        fields = stat.rsplit(")", 1)[1].split()
        start_ticks = int(fields[22 - 3])
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        return max(0.0, time.clock_gettime(time.CLOCK_BOOTTIME) - start_ticks / ticks_per_second)
    except (OSError, IndexError, ValueError, AttributeError):
        return None


class StartupTimeline:
    """Startup stages as seconds from the process start. Thread-safe.

    Each stage is recorded once; the first mark wins, so a stage reached
    again later (a second model load, a reconnect) never moves it. When every
    stage declared with :meth:`expect` is marked or skipped, one summary line
    is logged.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        process_age: Callable[[], float | None] = process_age_seconds,
    ) -> None:
        age = process_age()
        self._origin_source = "proc_self_stat" if age is not None else "timeline_created"
        age = 0.0 if age is None else age
        self._clock = clock
        self._origin = clock() - age
        self._origin_wall = wall_clock() - age
        self._lock = threading.Lock()
        self._stages: dict[str, float] = {}
        self._keyed: dict[str, dict[str, float]] = {stage: {} for stage in KEYED_STAGES}
        self._first_record_at: dict[str, str] = {}
        self._skipped: dict[str, str] = {}
        self._expected: set[str] = set()
        self._summary_logged = False

    # -- recording ---------------------------------------------------------

    def _elapsed(self) -> float:
        return round(self._clock() - self._origin, 3)

    def mark(self, stage: str) -> None:
        """Record ``stage`` now, unless it was already recorded."""
        _check_stage(stage)
        with self._lock:
            if stage in self._stages:
                return
            self._stages[stage] = self._elapsed()
        self._maybe_log_summary()

    def mark_keyed(self, stage: str, key: str) -> None:
        """Record ``stage`` for ``key`` now, unless already recorded for it."""
        if stage not in KEYED_STAGES:
            raise ValueError(f"{stage!r} is not a keyed startup stage ({KEYED_STAGES})")
        with self._lock:
            if key in self._keyed[stage]:
                return
            self._keyed[stage][key] = self._elapsed()

    def note_stored(self, records: Iterable[_StoredRecord]) -> None:
        """The storage writer accepted ``records``: mark each new source.

        Called by the storage sink after a successful write, never by a
        collector, so ``first_record_stored`` is what was recorded rather than
        what was read off the bus.
        """
        firsts: dict[str, datetime] = {}
        for record in records:
            source = str(record.source)
            if source in self._keyed["first_record_stored"]:
                continue
            seen = firsts.get(source)
            if seen is None or record.timestamp < seen:
                firsts[source] = record.timestamp
        if not firsts:
            return
        with self._lock:
            now = self._elapsed()
            for source, first_at in firsts.items():
                if source not in self._keyed["first_record_stored"]:
                    self._keyed["first_record_stored"][source] = now
                    self._first_record_at[source] = first_at.isoformat()
            self._stages.setdefault("first_record_stored", now)
        self._maybe_log_summary()

    def expect(self, *stages: str) -> None:
        """Declare stages this start will reach; the summary waits for them."""
        for stage in stages:
            _check_stage(stage)
        with self._lock:
            self._expected.update(stages)
        self._maybe_log_summary()

    def skip(self, stage: str, reason: str) -> None:
        """This start will not reach ``stage`` (disabled, or its build failed)."""
        _check_stage(stage)
        with self._lock:
            if stage not in self._stages:
                self._skipped[stage] = reason
        self._maybe_log_summary()

    # -- reading -----------------------------------------------------------

    def pending(self) -> list[str]:
        with self._lock:
            return self._pending_locked()

    def _pending_locked(self) -> list[str]:
        return [
            stage
            for stage in STAGES
            if stage in self._expected and stage not in self._stages and stage not in self._skipped
        ]

    def snapshot(self) -> dict[str, Any]:
        """The ``startup`` block of ``GET /status``."""
        with self._lock:
            pending = self._pending_locked()
            return {
                "clock": ORIGIN_NOTE,
                "process_start": {
                    "at": datetime.fromtimestamp(self._origin_wall, UTC).isoformat(),
                    "source": self._origin_source,
                },
                "stages": {stage: self._stages.get(stage) for stage in STAGES},
                "first_record_stored_by_source": dict(self._keyed["first_record_stored"]),
                "first_record_timestamp_by_source": dict(self._first_record_at),
                "model_loaded": dict(self._keyed["model_loaded"]),
                "skipped": dict(self._skipped),
                "pending": pending,
                "complete": bool(self._expected) and not pending,
            }

    def _maybe_log_summary(self) -> None:
        with self._lock:
            if self._summary_logged or not self._expected or self._pending_locked():
                return
            self._summary_logged = True
            stages = {stage: self._stages[stage] for stage in STAGES if stage in self._stages}
            extra = {
                "stages": stages,
                "first_record_stored_by_source": dict(self._keyed["first_record_stored"]),
                "model_loaded": dict(self._keyed["model_loaded"]),
                "skipped": dict(self._skipped),
                "origin": self._origin_source,
            }
        log.info(
            "startup complete: first record stored %.1f s after process start",
            stages.get("first_record_stored", float("nan")),
            extra=extra,
        )


def _check_stage(stage: str) -> None:
    if stage not in STAGES:
        raise ValueError(f"unknown startup stage {stage!r}; stages are {STAGES}")


__all__ = [
    "KEYED_STAGES",
    "ORIGIN_NOTE",
    "STAGES",
    "StartupTimeline",
    "process_age_seconds",
]
