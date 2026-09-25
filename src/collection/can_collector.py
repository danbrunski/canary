"""CAN bus collection: bus construction + frame recording.

Owns the read-only path from a `python-can` Bus to a candump-format `.log`
file plus a session manifest. Format choice is candump because every tool in
the ecosystem (SavvyCAN, Cabana, Wireshark, `canplayer`) reads it natively
and it's append-safe across crashes — Parquet conversion happens later as a
separate offline step.

The collector NEVER calls `bus.send()` — this path is a sniffer by FR-02.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

import can

# Bus construction and the shared frame primitives moved to can_bus
# (R02.12); these names stay importable from here.
from src.collection.can_bus import bus_factory as bus_factory
from src.collection.can_bus import frame_to_record as frame_to_record
from src.collection.queueing import (
    DEFAULT_HANDOFF_CAPACITY,
    DEFAULT_HANDOFF_MAX_BATCH,
    HandoffStats,
    QueueDropCounter,
    ThreadHandoff,
    flush_dropped_can_frame_log,
    log_dropped_can_frame,
)
from src.collection.reconnect import DEFAULT_RECONNECT_POLICY as DEFAULT_RECONNECT_POLICY
from src.collection.reconnect import (
    ReconnectPolicy,
    run_with_reconnect,
    wait_for_stop_or_timeout,
)
from src.collection.recording import (
    DiskWriteError,
    RecorderRun,
    RecordingResult,
)
from src.config import CanBusConfig, VehicleConfig
from src.session import SessionPaths
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CanReceiveStats:
    """Counters for one passive receive path (C07.5), reconcilable end to end.

    ``frames`` is every frame the reader took off the bus; each one is either
    ``handoff.accepted`` or ``handoff.dropped`` (buffer full / after close),
    and every accepted one either reached the queue or is in
    ``queue_dropped`` (asyncio queue full). ``parse_errors`` never became a
    record at all. Cumulative across reconnect attempts when reported through
    :func:`collect_can_from_config`.
    """

    frames: int
    parse_errors: int
    queue_dropped: int
    handoff: HandoffStats
    reconnects: int = 0

    def __add__(self, other: CanReceiveStats) -> CanReceiveStats:
        return CanReceiveStats(
            frames=self.frames + other.frames,
            parse_errors=self.parse_errors + other.parse_errors,
            queue_dropped=self.queue_dropped + other.queue_dropped,
            handoff=self.handoff + other.handoff,
            reconnects=self.reconnects + other.reconnects,
        )


ReceiveStatsCallback = Callable[[CanReceiveStats], None]


class CanReader(threading.Thread):
    """The one owned receiver thread for a passive bus (C07.2).

    Loops ``bus.recv(timeout)`` off the event loop and pushes each frame —
    converted here, so the loop never pays for it — into a
    :class:`ThreadHandoff`. It is a sniffer: it never calls ``bus.send``.

    Frame-level parser errors (``ValueError``/``IndexError`` from python-can's
    slcan parser) are counted and skipped exactly as in :func:`record`. Any
    other exception is a bus-level failure: it ends the thread and is stored
    in ``error`` for the owner to re-raise into the reconnect policy.
    ``on_exit`` always fires last, from this thread, so the owner can wake
    without polling.
    """

    def __init__(
        self,
        bus: can.BusABC,
        handoff: ThreadHandoff,
        *,
        poll_interval: float,
        stop: threading.Event,
        on_exit: Callable[[], None],
        name: str = "can-recv",
    ) -> None:
        super().__init__(name=name, daemon=True)
        self._bus = bus
        self._handoff = handoff
        self._poll_interval = poll_interval
        self._stop_event = stop
        self._on_exit = on_exit
        self.frames = 0
        self.parse_errors = 0
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    msg = self._bus.recv(timeout=self._poll_interval)
                except (ValueError, IndexError) as exc:
                    self.parse_errors += 1
                    log_dropped_can_frame(exc, owner=self.name)
                    time.sleep(0.001)
                    continue
                if msg is None:
                    continue
                flush_dropped_can_frame_log(owner=self.name)
                self.frames += 1
                self._handoff.push(frame_to_record(msg))
        except Exception as exc:  # noqa: BLE001 — bus-level failure, owner decides
            self.error = exc
        finally:
            # The thread is the only producer of these errors; its last burst
            # is summarized here or nowhere.
            flush_dropped_can_frame_log(owner=self.name, force=True)
            self._on_exit()


async def collect_can(
    bus: can.BusABC,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    poll_interval: float = 0.2,
    drop_counter: QueueDropCounter | None = None,
    on_receive_stats: ReceiveStatsCallback | None = None,
    handoff_capacity: int = DEFAULT_HANDOFF_CAPACITY,
    handoff_max_batch: int = DEFAULT_HANDOFF_MAX_BATCH,
    join_timeout: float | None = None,
    name: str = "can",
) -> CanReceiveStats:
    """Read CAN frames and enqueue normalized telemetry records until stopped.

    Ownership (C07.3): this coroutine owns exactly one :class:`CanReader`
    thread for ``bus``. Returning means the thread has been asked to stop and
    joined (bounded by ``join_timeout``, default ``2 * poll_interval + 1``),
    then the handoff was flushed and closed — so the caller may shut the bus
    down or rebuild it without a reader still using it, and any late callback
    is a no-op. A bus-level failure inside the reader is re-raised here so the
    reconnect policy in :func:`collect_can_from_config` sees it.

    ``on_receive_stats`` is called on the loop every ``poll_interval`` and once
    on exit with cumulative :class:`CanReceiveStats` for this attempt.
    """
    loop = asyncio.get_running_loop()
    on_drop = QueueDropCounter("can") if drop_counter is None else drop_counter
    handoff = ThreadHandoff(
        loop,
        queue,
        on_drop=on_drop,
        capacity=handoff_capacity,
        max_batch=handoff_max_batch,
        name=name,
    )
    thread_stop = threading.Event()
    finished = asyncio.Event()
    #: Set by either "stop requested" or "reader exited" so the owner wakes
    #: for both — waiting on ``finished`` alone would let a wedged ``recv``
    #: outlast the bounded join below (it did, in the first cut).
    wake = asyncio.Event()

    def _thread_exited() -> None:
        finished.set()
        wake.set()

    def _signal_finished() -> None:
        try:
            loop.call_soon_threadsafe(_thread_exited)
        except RuntimeError:
            pass  # loop already closed; the owner is gone and will not wait

    reader = CanReader(
        bus,
        handoff,
        poll_interval=poll_interval,
        stop=thread_stop,
        on_exit=_signal_finished,
        name=f"can-recv:{name}",
    )

    def _stats() -> CanReceiveStats:
        return CanReceiveStats(
            frames=reader.frames,
            parse_errors=reader.parse_errors,
            queue_dropped=on_drop.dropped,
            handoff=handoff.stats(),
        )

    def _report() -> None:
        if on_receive_stats is not None:
            on_receive_stats(_stats())

    async def _mirror_stop() -> None:
        await stop_event.wait()
        thread_stop.set()
        wake.set()

    mirror = asyncio.create_task(_mirror_stop())
    reader.start()
    try:
        while not stop_event.is_set() and not finished.is_set():
            await wait_for_stop_or_timeout(wake, poll_interval)
            _report()
    finally:
        mirror.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await mirror
        thread_stop.set()
        limit = 2 * poll_interval + 1.0 if join_timeout is None else join_timeout
        await asyncio.to_thread(reader.join, limit)
        if reader.is_alive():
            log.warning(
                "can reader thread did not stop within join timeout",
                extra={"reader_thread": reader.name, "join_timeout_seconds": limit},
            )
        flushed = handoff.close()
        if flushed:
            log.debug("flushed pending can records at stop", extra={"records": flushed})
        _report()
    if reader.error is not None:
        raise reader.error
    return _stats()


async def collect_can_from_config(
    spec: CanBusConfig,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    poll_interval: float = 0.2,
    factory: Callable[[CanBusConfig], can.BusABC] = bus_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
    on_receive_stats: ReceiveStatsCallback | None = None,
) -> None:
    """Own a configured Bus for the daemon queue-producer path.

    Reconnect ordering (C07.3): each attempt's :func:`collect_can` joins its
    reader before returning, and ``run_with_reconnect`` only then tears the
    bus down — so a rebuilt bus never has a reader still on the old one.
    ``on_receive_stats`` sees counters cumulative across attempts.
    """
    drop_counter = QueueDropCounter("can")
    completed = CanReceiveStats(
        frames=0,
        parse_errors=0,
        queue_dropped=0,
        handoff=HandoffStats(0, 0, 0, 0, 0, 0),
    )
    attempts = 0

    async def _attempt(capture: Callable[[can.BusABC], None]) -> None:
        nonlocal completed, attempts
        attempts += 1
        bus = factory(spec)
        capture(bus)
        log.info(
            "can collection started",
            extra={"interface": spec.interface, "channel": spec.channel},
        )
        # The queue-full counter is shared across attempts, so each attempt
        # contributes only the drops it added on top of the completed total.
        base_queue_dropped = drop_counter.dropped
        latest = CanReceiveStats(0, 0, 0, HandoffStats(0, 0, 0, 0, 0, 0))

        def _on_stats(stats: CanReceiveStats) -> None:
            nonlocal latest
            latest = CanReceiveStats(
                frames=stats.frames,
                parse_errors=stats.parse_errors,
                queue_dropped=stats.queue_dropped - base_queue_dropped,
                handoff=stats.handoff,
            )
            if on_receive_stats is not None:
                on_receive_stats(replace(completed + latest, reconnects=attempts - 1))

        try:
            await collect_can(
                bus,
                queue,
                stop_event,
                poll_interval=poll_interval,
                drop_counter=drop_counter,
                on_receive_stats=_on_stats,
                name=spec.channel,
            )
        finally:
            # collect_can reports once more in its own finally, so ``latest``
            # is the attempt's final tally whether it stopped or failed.
            completed = completed + latest

    await run_with_reconnect(
        _attempt,
        stop_event=stop_event,
        policy=reconnect_policy,
        teardown=lambda bus: bus.shutdown(),
        log=log,
        reconnect_message="can collector failed; reconnecting",
        log_extra=lambda: {"interface": spec.interface, "channel": spec.channel},
    )


def record(
    bus: can.BusABC,
    paths: SessionPaths,
    *,
    spec: CanBusConfig,
    vehicle: VehicleConfig | None,
    duration: float | None,
    vehicle_state: str | None = None,
    stop_event: threading.Event | None = None,
    poll_interval: float = 0.2,
    factory: Callable[[CanBusConfig], can.BusABC] | None = None,
    reconnect_policy: ReconnectPolicy | None = None,
    max_reconnects: int | None = 10,
) -> RecordingResult:
    """Record frames from `bus` into `paths.frames` until duration or stop_event.

    Either `duration` (seconds) or `stop_event` ends the loop; both may be set,
    whichever fires first wins. The bus is not closed here — the caller owns
    its lifetime so the same bus can be reused across sessions if needed.
    Manifest is written on exit, even if zero frames were captured.

    When `factory` AND `reconnect_policy` are provided, bus-level exceptions
    from `bus.recv()` (USB disconnect, adapter reset, generic OSError /
    RuntimeError) trigger an auto-reconnect: the failed bus is shut down,
    we wait per the policy, and `factory(spec)` is called to rebuild it.
    The SAME `CanutilsLogWriter` stays open across reconnects so frames.log
    remains a single continuous file. Without both params, bus-level
    exceptions propagate as before (backward-compatible).

    `max_reconnects` caps the number of CONSECUTIVE failed rebuild attempts
    (default 10). A successful rebuild resets the counter. Once exhausted,
    the recording stops with `stop_reason = "reconnect_exhausted"`. This
    prevents the recorder from retrying forever against a persistently
    broken adapter — important because in WSL2 a wedged USB device can
    leave the rebuild call in an uninterruptible kernel I/O state where
    even `kill -9` won't terminate the process. Set to None to allow
    unlimited retries (not recommended outside controlled environments).

    Frame-level parse errors (ValueError / IndexError from python-can's
    slcan parser, or `can.CanError` subclasses) are ALWAYS caught and
    counted into `parse_errors` — they're single bad lines out of millions,
    not a bus-level failure.

    Disk-write failures (OSError from `writer.on_message_received`) end the
    recording cleanly with `stop_reason = "disk_error"` and a count of
    `dropped_writes`. The manifest still gets written.
    """
    #: Identity of this loop's parse-error log throttle — per recorder, so two
    #: live buses never share (and suppress) each other's warning lines.
    parse_error_owner = f"record:{spec.channel}"
    run = RecorderRun(
        bus,
        paths,
        spec=spec,
        vehicle=vehicle,
        duration=duration,
        vehicle_state=vehicle_state,
        stop_event=stop_event,
        factory=factory,
        reconnect_policy=reconnect_policy,
        max_reconnects=max_reconnects,
        log=log,
        parse_error_owner=parse_error_owner,
    )
    log.info(
        "recording started",
        extra={
            "frames_path": str(paths.frames),
            "interface": spec.interface,
            "channel": spec.channel,
            "bitrate": spec.bitrate,
            "duration_seconds": duration,
            "auto_reconnect": run.reconnect_enabled,
        },
    )

    with run, can.io.CanutilsLogWriter(str(paths.frames)) as writer:
        write = run.frame_writer(writer)
        while not run.should_stop():
            current_bus = run.ensure_bus()
            if current_bus is None:
                if run.finished:
                    break
                continue
            try:
                msg = current_bus.recv(timeout=poll_interval)
            except (ValueError, IndexError) as exc:
                # Frame-level parser error from python-can's slcan parser:
                # slcan.py:282 (ValueError) and slcan.py:304 (IndexError).
                # Drop the bad line, keep recording. We DO NOT trap
                # can.CanError here — its subclasses (CanOperationError
                # from a dead serial pipe, CanInitializationError, etc.)
                # are bus-level failures that need the reconnect branch
                # below. Catching them as "parse errors" caused a 27-min
                # tight loop in session 2026-05-15T020322 — 63M errors,
                # zero reconnects, frames stopped 5 min in. A small sleep
                # keeps even legit storms of parse errors from burning CPU.
                run.parse_errors += 1
                log_dropped_can_frame(exc, owner=parse_error_owner)
                time.sleep(0.001)
                continue
            except Exception as exc:
                # Bus-level failure (OSError from a USB disconnect,
                # RuntimeError from adapter reset, etc.): rebuild on the next
                # pass, or propagate without a factory/policy.
                run.on_bus_failure(exc, "bus recv failed; will attempt reconnect")
                continue

            if msg is None:
                continue

            flush_dropped_can_frame_log(owner=parse_error_owner)
            try:
                write(msg)
            except DiskWriteError as exc:
                # Full disk, read-only filesystem, SD card hiccup: recording
                # can't continue meaningfully. End cleanly so the manifest
                # still writes with an accurate stopped_by + dropped_writes.
                run.on_disk_error(exc)
                break
    assert run.result is not None
    return run.result
