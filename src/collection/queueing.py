"""Telemetry queue pressure helpers."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Protocol, runtime_checkable

from src.telemetry import TelemetryRecord, channel_key

log = logging.getLogger(__name__)

DropCallback = Callable[[TelemetryRecord], None]
#: Injectable monotonic clock — the rate limiters below are tested with a fake
#: one, never with ``sleep``.
Clock = Callable[[], float]

#: Wall-clock seconds between two consecutive warning lines from one
#: :class:`DropLogThrottle`.
#:
#: Drop logging used to be count-based (one line per 100 drops), which made log
#: volume *proportional to loss volume*: the 2026-09-17 degradation window
#: (19:24:50-19:28:10Z) dropped 150,114 records and emitted 1,502 warning
#: lines, every one a synchronous journald write from inside the producer path,
#: competing for CPU and I/O with the consumer that was already failing to keep
#: up. A time-based limit puts a ceiling on that feedback: one line per handoff
#: per interval no matter how fast the loss arrives.
DROP_LOG_INTERVAL_SECONDS = 1.0

#: Distinct keys (``source:channel`` pairs, exception types) named in one
#: summary line before the remainder collapses into an ``others`` count.
#: Without a cap the breakdown reintroduces unbounded log volume one line at a
#: time — a flooded bus has hundreds of distinct channels.
DROP_LOG_BREAKDOWN_LIMIT = 5


@runtime_checkable
class SupportsDropFlush(Protocol):
    """A drop callback whose warning is rate-limited and can hold a tail."""

    def flush_drop_log(self, *, force: bool = False) -> None: ...


class DropLogThrottle:
    """One warning line per interval for a loss that recurs per record.

    Every drop is *counted* here; only lines are rate-limited. The first drop
    of a burst logs immediately (loss starting is the signal), then at most one
    line per ``interval``, each carrying what happened since the previous line:
    ``dropped_since_last``, a cumulative ``dropped_total`` that is always
    exactly the sum of this owner's ``dropped_since_last`` values (so it keeps
    accumulating across a collector restart, where the owner's own counter goes
    back to zero), and a breakdown over at most ``breakdown_limit`` keys plus
    an ``others`` count.
    Nothing here may become the source of truth for how much was lost — the
    owner's counter stays exact and is what ``/status`` reports.

    The tail is never lost: :meth:`flush` emits a finished burst's final
    summary at the next drop-free observation (an accepted record, a drained
    buffer, a good frame), and ``force=True`` emits it regardless of the
    interval at a boundary that is itself bounded (a collector closing).

    Thread-safe. Pass ``lock`` to share the owner's existing mutex rather than
    add a second one; the log call itself always happens *outside* the lock, so
    a journald write never serializes the producers.
    """

    def __init__(
        self,
        message: str,
        *,
        interval: float = DROP_LOG_INTERVAL_SECONDS,
        breakdown_limit: int = DROP_LOG_BREAKDOWN_LIMIT,
        clock: Clock = time.monotonic,
        lock: threading.Lock | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        if breakdown_limit <= 0:
            raise ValueError("breakdown_limit must be positive")
        self._message = message
        self._interval = interval
        self._breakdown_limit = breakdown_limit
        self._clock = clock
        self._lock = threading.Lock() if lock is None else lock
        self._pending = 0
        self._pending_by_key: Counter[str] = Counter()
        self._total = 0
        self._last_emit: float | None = None

    @property
    def pending(self) -> int:
        """Drops counted but not yet named in a line (unsynchronized read).

        Only for the caller's hot-path guard: a stale zero costs one deferred
        flush, and the next observation makes it again.
        """
        return self._pending

    def record(
        self,
        key: str,
        *,
        extra: Mapping[str, object] | None = None,
    ) -> bool:
        """Count one drop; return True when it was summarized in a line now."""
        with self._lock:
            self._pending += 1
            self._pending_by_key[key] += 1
            payload = self._due_locked(extra, force=False)
        return self._emit(payload)

    def flush(
        self,
        *,
        extra: Mapping[str, object] | None = None,
        force: bool = False,
    ) -> bool:
        """Emit the pending summary, if any; ``force`` ignores the interval."""
        if not self._pending:
            return False
        with self._lock:
            payload = self._due_locked(extra, force=force)
        return self._emit(payload)

    def _due_locked(
        self,
        extra: Mapping[str, object] | None,
        *,
        force: bool,
    ) -> dict[str, object] | None:
        if not self._pending:
            return None
        now = self._clock()
        if not force and self._last_emit is not None and now - self._last_emit < self._interval:
            return None
        self._last_emit = now
        # Accumulate what this throttle has actually summarized, rather than
        # echoing the owner's counter. The owners are long-lived strings whose
        # counters are not: a CanReader is rebuilt per reconnect attempt with
        # parse_errors back at 0, and run_can_obd_loop gets a fresh
        # CanObdLoopStats per attempt, so echoing that counter made
        # dropped_total undercount after a reconnect (found by verification
        # 2026-09-17: 5,000 + 3,000 drops reported a total of 5,000). This way
        # dropped_total is always exactly sum(dropped_since_last) for the owner,
        # across restarts, which is also what CanReceiveStats.parse_errors
        # accumulates.
        self._total += self._pending
        payload: dict[str, object] = dict(extra or {})
        payload["dropped_since_last"] = self._pending
        payload["dropped_total"] = self._total
        payload["log_interval_seconds"] = self._interval
        ranked = self._pending_by_key.most_common()
        top = ranked[: self._breakdown_limit]
        payload["dropped_by"] = dict(top)
        payload["dropped_by_others"] = sum(count for _, count in ranked[self._breakdown_limit :])
        payload["dropped_by_others_keys"] = len(ranked) - len(top)
        self._pending = 0
        self._pending_by_key.clear()
        return payload

    def _emit(self, payload: dict[str, object] | None) -> bool:
        if payload is None:
            return False
        log.warning(self._message, extra=payload)
        return True


@dataclass
class QueueDropCounter:
    """Count dropped telemetry records exactly; log at a bounded rate.

    ``dropped`` is the exact cumulative count every reader of loss (``/status``,
    ``CanReceiveStats``, the evidence reconciliation) uses. The warning it emits
    is rate-limited by :class:`DropLogThrottle`; call :meth:`flush_drop_log`
    when the queue is observed draining so a burst's final summary is not lost.
    """

    name: str
    dropped: int = 0
    interval_seconds: float = DROP_LOG_INTERVAL_SECONDS
    clock: Clock = time.monotonic

    @cached_property
    def _throttle(self) -> DropLogThrottle:
        # Deliberately not a dataclass field: it owns a lock, and a field
        # holding one makes ``dataclasses.asdict(counter)`` raise on the
        # deepcopy. The counters stay plain, reportable numbers.
        return DropLogThrottle(
            "telemetry queue full; dropping record",
            interval=self.interval_seconds,
            clock=self.clock,
        )

    def __call__(self, record: TelemetryRecord) -> None:
        self.dropped += 1
        self._throttle.record(
            channel_key(record.source, record.channel),
            extra={"queue": self.name},
        )

    def flush_drop_log(self, *, force: bool = False) -> None:
        self._throttle.flush(extra={"queue": self.name}, force=force)


#: Every production call site passes its own ``on_drop``; this is the
#: last-resort counter for one that does not, so a queue-full drop is still
#: counted exactly and logged at a bounded rate instead of one synchronous
#: line per dropped record.
_UNATTRIBUTED_QUEUE_DROPS = QueueDropCounter("unattributed")


def _drop_callback(on_drop: DropCallback | None) -> DropCallback:
    """Resolve a drop callback; ``None`` falls back to the module's counter."""
    return _UNATTRIBUTED_QUEUE_DROPS if on_drop is None else on_drop


#: One parse-error throttle per read loop, created on first use. Keys are the
#: caller's own loop identity ("can-recv:can", "canable-obd", ...): a shared
#: throttle would let one live bus's storm suppress the other's lines and make
#: ``dropped_total`` flip between two unrelated counters.
_CAN_FRAME_DROP_LOGS: dict[str, DropLogThrottle] = {}
_CAN_FRAME_DROP_LOGS_LOCK = threading.Lock()


def _can_frame_drop_log(owner: str) -> DropLogThrottle:
    with _CAN_FRAME_DROP_LOGS_LOCK:
        throttle = _CAN_FRAME_DROP_LOGS.get(owner)
        if throttle is None:
            throttle = DropLogThrottle("dropped malformed CAN frame")
            _CAN_FRAME_DROP_LOGS[owner] = throttle
        return throttle


def log_dropped_can_frame(exc: Exception, *, owner: str) -> None:
    """Rate-limited warning for a malformed CAN frame parse error (Task 4.7).

    Shared by the three read loops that hand-rolled this warning identically
    (``can_collector.py``'s slcan reader thread and socketcan recorder loop,
    ``can_obd_collector.py``'s frame-read helper). Each site keeps its own
    exact counter (a thread attribute, a local ``int``, a ``CanObdLoopStats``
    field) and that is what ``/status`` reports; this owns only the logging
    policy: one line per ``owner`` per
    :data:`DROP_LOG_INTERVAL_SECONDS`, summarizing the interval by exception
    type. Parse-error storms are proportional to a failure rate (63M errors in
    27 minutes, session 2026-05-15T020322), which is exactly the shape that
    must not become log volume. Call :func:`flush_dropped_can_frame_log` after
    a good frame so a burst's final summary lands.
    """
    _can_frame_drop_log(owner).record(
        type(exc).__name__,
        extra={"owner": owner, "error": f"{type(exc).__name__}: {exc}"},
    )


def flush_dropped_can_frame_log(*, owner: str, force: bool = False) -> None:
    """Emit ``owner``'s final parse-error summary once frames read cleanly."""
    throttle = _CAN_FRAME_DROP_LOGS.get(owner)
    if throttle is None or not throttle.pending:
        return
    throttle.flush(extra={"owner": owner}, force=force)


@dataclass
class QueueDepthMonitor:
    """Warn when an asyncio queue remains near capacity."""

    warning_fraction: float = 0.8
    clear_fraction: float = 0.5
    warned: bool = False

    def observe(self, queue: asyncio.Queue[TelemetryRecord]) -> None:
        maxsize = queue.maxsize
        if maxsize <= 0:
            return
        depth = queue.qsize()
        fraction = depth / maxsize
        if fraction >= self.warning_fraction and not self.warned:
            self.warned = True
            log.warning(
                "telemetry queue near capacity",
                extra={
                    "queue_depth": depth,
                    "queue_maxsize": maxsize,
                    "queue_fraction": round(fraction, 3),
                },
            )
        elif self.warned and fraction <= self.clear_fraction:
            self.warned = False


#: Producer-side buffer bound (records) between a collector thread and the
#: event loop. Sized for a few seconds of a flooded CAN bus (~1 kHz) while the
#: loop is busy; overflow is counted, never silent.
DEFAULT_HANDOFF_CAPACITY = 4096
#: Records one scheduled drain callback moves per loop turn. Bounds loop
#: occupancy per callback; leftovers reschedule with ``call_soon`` so the loop
#: interleaves other work between chunks.
DEFAULT_HANDOFF_MAX_BATCH = 256


@dataclass(frozen=True)
class HandoffStats:
    """Snapshot of one :class:`ThreadHandoff`'s counters (C07.5).

    ``accepted + dropped`` equals every record ever pushed, so C03-style loss
    attribution can reconcile against the producer's own count. ``dropped``
    is loss *before* the asyncio queue (buffer full, or pushed after
    :meth:`ThreadHandoff.close`); queue-full drops are the ``on_drop``
    callback's to count, they are a different cause.
    """

    accepted: int
    dropped: int
    high_water: int
    capacity: int
    drains: int
    max_batch: int

    def __add__(self, other: HandoffStats) -> HandoffStats:
        """Cumulate across reconnect attempts: counters sum, marks take the max."""
        return HandoffStats(
            accepted=self.accepted + other.accepted,
            dropped=self.dropped + other.dropped,
            high_water=max(self.high_water, other.high_water),
            capacity=max(self.capacity, other.capacity),
            drains=self.drains + other.drains,
            max_batch=max(self.max_batch, other.max_batch),
        )


@dataclass(frozen=True)
class CollectorHandoffStats:
    """A polling collector's cumulative handoff accounting (C03.6).

    ``handoff`` is the :class:`ThreadHandoff` view (loss before the ingest
    queue), ``queue_dropped`` the ingest-queue-full drops counted by the
    collector's :class:`QueueDropCounter`. ``derived_records`` is what the
    loop emitted as telemetry (one batched Mode 01 response -> N readings ->
    N records); ``raw_responses`` is the ECU responses behind them, when the
    loop reports them, so a derived expansion is never mistaken for a raw
    count. All cumulative across reconnect attempts.
    """

    handoff: HandoffStats
    queue_dropped: int
    derived_records: int
    raw_responses: int | None
    reconnects: int = 0

    def to_dict(self) -> dict[str, int | None]:
        return {
            "accepted": self.handoff.accepted,
            "buffer_dropped": self.handoff.dropped,
            "buffer_high_water": self.handoff.high_water,
            "buffer_capacity": self.handoff.capacity,
            "batches": self.handoff.drains,
            "queue_dropped": self.queue_dropped,
            "derived_records": self.derived_records,
            "raw_responses": self.raw_responses,
            "reconnects": self.reconnects,
        }


class ThreadHandoff:
    """Bounded producer buffer that hops records onto the event loop in chunks.

    C07.4/C07.5 — replaces one ``loop.call_soon_threadsafe`` per record. A
    push from any thread appends to a bounded FIFO and schedules **one**
    drain callback only when none is already pending; every record pushed
    before that drain runs rides the same wake-up. On an idle loop the drain
    runs on the very next turn, so sparse telemetry pays exactly the old
    per-record latency (no timer, no waiting for a full batch); under load the
    loop's own backlog is what coalesces pushes. ``max_latency`` is therefore
    structurally one loop turn rather than a tunable wait.

    Ordering: single producer -> FIFO buffer -> single drain owner (the loop),
    so source order is preserved end to end. The drain moves at most
    ``max_batch`` records per callback and reschedules itself for the rest.

    Ownership: :meth:`close` (loop thread only) flushes what is buffered into
    the queue in order and then refuses further pushes — a late push or a late
    drain callback after close is a counted no-op, never a queue write.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[TelemetryRecord],
        *,
        on_drop: DropCallback | None = None,
        capacity: int = DEFAULT_HANDOFF_CAPACITY,
        max_batch: int = DEFAULT_HANDOFF_MAX_BATCH,
        name: str = "handoff",
        log_interval_seconds: float = DROP_LOG_INTERVAL_SECONDS,
        clock: Clock = time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if max_batch <= 0:
            raise ValueError("max_batch must be positive")
        self._loop = loop
        self._queue = queue
        self._on_drop = on_drop
        self._capacity = capacity
        self._max_batch = max_batch
        self._name = name
        self._lock = threading.Lock()
        #: Shares the buffer's mutex — the counters it summarizes are already
        #: mutated under it, and a second lock on this path buys nothing.
        self._drop_log = DropLogThrottle(
            "telemetry handoff buffer full; dropping record",
            interval=log_interval_seconds,
            clock=clock,
            lock=self._lock,
        )
        self._buffer: deque[TelemetryRecord] = deque()
        self._scheduled = False
        self._closed = False
        self._accepted = 0
        self._dropped = 0
        self._high_water = 0
        self._drains = 0

    @property
    def closed(self) -> bool:
        return self._closed

    def push(self, record: TelemetryRecord) -> bool:
        """Buffer ``record`` (any thread); False when it was dropped instead."""
        with self._lock:
            if self._closed or len(self._buffer) >= self._capacity:
                self._dropped += 1
                dropped_total = self._dropped
            else:
                self._buffer.append(record)
                self._accepted += 1
                depth = len(self._buffer)
                if depth > self._high_water:
                    self._high_water = depth
                dropped_total = 0
                schedule = not self._scheduled
                self._scheduled = True
        if dropped_total:
            self._log_drop(record)
            return False
        # An accepted record is the drop-free observation that closes a burst:
        # emit its final summary rather than let the tail of the story sit in
        # the throttle (the guard keeps the common path free of the lock).
        if self._drop_log.pending:
            self._flush_drop_logs()
        if schedule:
            self._schedule_drain()
        return True

    __call__ = push

    def close(self) -> int:
        """Flush buffered records in order, then refuse further pushes.

        Loop-thread only. Returns how many records were flushed. Safe to
        call more than once.
        """
        with self._lock:
            self._closed = True
            pending = list(self._buffer)
            self._buffer.clear()
            self._scheduled = False
        for record in pending:
            put_nowait_or_drop(self._queue, record, on_drop=self._on_drop)
        # Last chance for this handoff: a burst that ended with the producer is
        # summarized here or never. close() is once per lifetime, so forcing
        # past the interval cannot amplify anything.
        self._flush_drop_logs(force=True)
        return len(pending)

    def stats(self) -> HandoffStats:
        with self._lock:
            return HandoffStats(
                accepted=self._accepted,
                dropped=self._dropped,
                high_water=self._high_water,
                capacity=self._capacity,
                drains=self._drains,
                max_batch=self._max_batch,
            )

    def _schedule_drain(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._drain)
        except RuntimeError:
            # Loop already closed: nothing will ever drain. Leave the flag set
            # so the producer stops paying for wake-ups; close() reconciles.
            log.warning("telemetry handoff loop closed; buffered records stranded")

    def _drain(self) -> None:
        """Move one bounded chunk onto the queue (loop thread only)."""
        with self._lock:
            if self._closed:
                self._scheduled = False
                return
            take = min(self._max_batch, len(self._buffer))
            batch = [self._buffer.popleft() for _ in range(take)]
            remaining = len(self._buffer)
            self._scheduled = remaining > 0
            self._drains += 1
        for record in batch:
            put_nowait_or_drop(self._queue, record, on_drop=self._on_drop)
        if remaining:
            self._loop.call_soon(self._drain)
        else:
            # The buffer drained: whatever pressure caused the loss is over.
            # One drain callback per loop turn, so this needs no hot-path guard
            # and covers the ingest-queue counter's tail too.
            self._flush_drop_logs()

    def _log_drop(self, record: TelemetryRecord) -> None:
        self._drop_log.record(
            channel_key(record.source, record.channel),
            extra=self._drop_log_extra(),
        )

    def _flush_drop_logs(self, *, force: bool = False) -> None:
        """Emit the finished burst's last line for this handoff and its queue.

        Both loss causes on this path hold a pending tail — buffer-full here,
        ingest-queue-full in the ``on_drop`` counter — and a summary that is
        never emitted is how "it dropped 150k records" becomes invisible.
        """
        self._drop_log.flush(extra=self._drop_log_extra(), force=force)
        on_drop = _drop_callback(self._on_drop)
        if isinstance(on_drop, SupportsDropFlush):
            on_drop.flush_drop_log(force=force)

    def _drop_log_extra(self) -> dict[str, object]:
        return {"handoff": self._name, "capacity": self._capacity, "closed": self._closed}


def threadsafe_enqueue(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[TelemetryRecord],
    *,
    on_drop: DropCallback | None = None,
    capacity: int = DEFAULT_HANDOFF_CAPACITY,
    max_batch: int = DEFAULT_HANDOFF_MAX_BATCH,
    name: str = "handoff",
) -> ThreadHandoff:
    """Thread-safe "hop onto the event loop and enqueue" callback (Task 4.7).

    Returns a :class:`ThreadHandoff` — callable exactly like the plain closure
    it replaced (``enqueue(record)``), so a collector's background reader
    thread can push a record without touching the asyncio queue directly —
    but backed by one bounded, accounted buffer and one coalesced drain
    callback instead of one ``call_soon_threadsafe`` per record (C07.4).
    Callers still do their own reading->``TelemetryRecord`` conversion before
    calling it — that conversion genuinely differs per collector
    (``reading_to_record`` vs ``frame_to_record``). Owners that know when
    their producer thread has exited should call :meth:`ThreadHandoff.close`
    so late callbacks become no-ops.
    """
    return ThreadHandoff(
        loop, queue, on_drop=on_drop, capacity=capacity, max_batch=max_batch, name=name
    )


def put_nowait_or_drop(
    queue: asyncio.Queue[TelemetryRecord],
    record: TelemetryRecord,
    *,
    on_drop: DropCallback | None = None,
) -> bool:
    """Put a record without blocking; return False and count when the queue is full."""
    try:
        queue.put_nowait(record)
    except asyncio.QueueFull:
        _drop_callback(on_drop)(record)
        return False
    return True
