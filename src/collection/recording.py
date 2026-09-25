"""The recorder skeleton both foreground recorders share (roadmap R02.8).

``can_collector.record`` (CAN only) and ``can_obd_collector.record_can_obd``
(one Canable, CAN plus OBD) duplicated the stop checks, the four
``RebuildOutcome`` → ``stopped_by`` branches, best-effort shutdown, the
``finally`` that closes a rebuilt bus, ``RecordingResult`` and the manifest
try/except. The copies had already drifted: ``record`` ended a full disk as
``disk_error``, while in ``record_can_obd`` the writer runs inside the loop's
frame tap, so its ``OSError`` was taken for a bus fault — the bus was rebuilt
and the run retried until its deadline, never reporting the disk (R02.6).

:class:`RecorderRun` owns all of it. Each recorder keeps only its receive
step. Frames and readings writers wrapped by :meth:`RecorderRun.frame_writer`
and :meth:`RecorderRun.reading_writer` raise :class:`DiskWriteError`, which
no receive loop mistakes for a bus fault.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol, TypeVar

import can

from src.collection.queueing import flush_dropped_can_frame_log
from src.collection.reconnect import BusReconnector, RebuildOutcome, ReconnectPolicy
from src.config import CanBusConfig, VehicleConfig
from src.session import SessionPaths


@dataclass(frozen=True)
class RecordingResult:
    started_at: datetime
    ended_at: datetime
    frames_recorded: int
    stopped_by: str  # "deadline" | "signal" | "error" | "disk_error" | "reconnect_exhausted"
    parse_errors: int = 0  # malformed frames dropped without aborting the run
    reconnects: int = 0  # successful bus rebuilds during the recording
    dropped_writes: int = 0  # frames the disk writer failed to persist


class DiskWriteError(Exception):
    """The frames writer could not persist a frame (full disk, read-only FS).

    Deliberately not an ``OSError``: a receive loop treats ``OSError`` as a
    bus fault and rebuilds the bus, which is the wrong answer to a full disk.
    """

    def __init__(self, cause: OSError) -> None:
        super().__init__(str(cause))
        self.cause = cause


ReadingT = TypeVar("ReadingT")


class FrameSink(Protocol):
    def on_message_received(self, msg: can.Message) -> None: ...


def write_manifest(
    path: Path,
    *,
    spec: CanBusConfig,
    vehicle: VehicleConfig | None,
    result: RecordingResult,
    vehicle_state: str | None = None,
    error: BaseException | None = None,
) -> None:
    """The session ``manifest.json`` a recording leaves behind."""
    payload: dict[str, Any] = {
        "started_at": result.started_at.isoformat(),
        "ended_at": result.ended_at.isoformat(),
        "duration_seconds": (result.ended_at - result.started_at).total_seconds(),
        "host": socket.gethostname(),
        "vehicle": vehicle.model_dump() if vehicle is not None else None,
        "vehicle_state": vehicle_state,
        "bus": {
            "interface": spec.interface,
            "channel": spec.channel,
            "bitrate": spec.bitrate,
        },
        "frames_recorded": result.frames_recorded,
        "stopped_by": result.stopped_by,
        "parse_errors": result.parse_errors,
        "reconnects": result.reconnects,
        "dropped_writes": result.dropped_writes,
    }
    if error is not None:
        payload["error"] = f"{type(error).__name__}: {error}"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


class RecorderRun:
    """One foreground recording: stop reasons, bus rebuilds, finalization.

    Used as a context manager around the recorder's loop::

        with RecorderRun(...) as run:
            while not run.should_stop():
                bus = run.ensure_bus()
                if bus is None:
                    if run.finished:
                        break
                    continue
                ...  # receive; run.on_bus_failure(exc) on a bus fault

    On exit — normal or not — it closes a rebuilt bus, builds
    :attr:`result`, writes the manifest and logs one "recording stopped"
    line. An exception escaping the block is recorded as ``stopped_by =
    "error"`` and propagates.
    """

    def __init__(
        self,
        bus: can.BusABC,
        paths: SessionPaths,
        *,
        spec: CanBusConfig,
        vehicle: VehicleConfig | None,
        duration: float | None,
        vehicle_state: str | None,
        stop_event: threading.Event | None,
        factory: Callable[[CanBusConfig], can.BusABC] | None,
        reconnect_policy: ReconnectPolicy | None,
        max_reconnects: int | None,
        log: logging.Logger,
        parse_error_owner: str | None = None,
    ) -> None:
        self.paths = paths
        self.spec = spec
        self.vehicle = vehicle
        self.vehicle_state = vehicle_state
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.factory = factory
        self.log = log
        self.parse_error_owner = parse_error_owner
        self.started_at = datetime.now(UTC)
        self.deadline = time.monotonic() + duration if duration is not None else None
        self.reconnect_enabled = factory is not None and reconnect_policy is not None
        self.reconnector = (
            BusReconnector(reconnect_policy, max_reconnects, log)
            if self.reconnect_enabled and reconnect_policy is not None
            else None
        )
        self._original_bus = bus
        self.bus: can.BusABC | None = bus
        self.stop_reason = "signal"
        self.error: BaseException | None = None
        self.frames_recorded = 0
        self.parse_errors = 0
        self.dropped_writes = 0
        #: Set when a stop outcome (not a bus fault) ended the loop.
        self.finished = False
        self.result: RecordingResult | None = None

    # ------------------------------------------------------------------
    # The loop's questions
    # ------------------------------------------------------------------

    def should_stop(self) -> bool:
        """A stop signal or the deadline ends the run (and names which)."""
        if self.finished:
            return True
        if self.stop_event.is_set():
            return self._finish("signal")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return self._finish("deadline")
        return False

    def settle_stop_reason(self) -> None:
        """After a receive step returned normally: which stop ended it."""
        if self.stop_event.is_set():
            self.stop_reason = "signal"
        elif self.deadline is not None and time.monotonic() >= self.deadline:
            self.stop_reason = "deadline"
        self.finished = True

    def ensure_bus(self) -> can.BusABC | None:
        """The bus to receive on, rebuilding a lost one; ``None`` = loop again.

        A rebuild's stop outcomes set :attr:`finished` and the reason; a
        failed attempt (``RETRY``) returns ``None`` with :attr:`finished`
        unset, so the loop waits out the next attempt.
        """
        if self.bus is not None:
            return self.bus
        assert self.reconnector is not None and self.factory is not None
        outcome, rebuilt = self.reconnector.rebuild(
            self.factory,
            self.spec,
            stop_event=self.stop_event,
            deadline_monotonic=self.deadline,
            frames_recorded=self.frames_recorded,
        )
        if outcome is RebuildOutcome.STOP_SIGNAL:
            self._finish("signal")
            return None
        if outcome is RebuildOutcome.STOP_DEADLINE:
            self._finish("deadline")
            return None
        if outcome is RebuildOutcome.EXHAUSTED:
            self.error = self.reconnector.last_error
            self._finish("reconnect_exhausted")
            return None
        if outcome is RebuildOutcome.RETRY:
            return None
        # Only REBUILT falls through — it always returns a bus.
        assert rebuilt is not None
        self.bus = rebuilt
        return rebuilt

    def on_bus_failure(self, exc: BaseException, message: str) -> None:
        """A bus-level fault: rebuild on the next pass, or propagate.

        Without both ``factory`` and ``reconnect_policy`` the exception is
        re-raised (the backward-compatible behaviour).
        """
        if not self.reconnect_enabled:
            raise exc
        assert self.reconnector is not None
        self.log.warning(
            message,
            extra={
                "error": f"{type(exc).__name__}: {exc}",
                "delay_seconds": self.reconnector.delay,
            },
        )
        self._shutdown(self.bus)
        self.bus = None

    def on_disk_error(self, exc: DiskWriteError | OSError) -> None:
        """A frame the disk could not take: stop now, cleanly, as ``disk_error``."""
        cause = exc.cause if isinstance(exc, DiskWriteError) else exc
        self.dropped_writes += 1
        self.error = cause
        self.log.error(
            "disk write failed; stopping recording",
            extra={
                "error": f"{type(cause).__name__}: {cause}",
                "frames_recorded_before_failure": self.frames_recorded,
            },
        )
        self._finish("disk_error")

    def frame_writer(self, sink: FrameSink) -> Callable[[can.Message], None]:
        """``sink.on_message_received`` that counts frames and raises
        :class:`DiskWriteError` (never a bare ``OSError``) on a write failure."""

        def write(msg: can.Message) -> None:
            try:
                sink.on_message_received(msg)
            except OSError as exc:
                raise DiskWriteError(exc) from exc
            self.frames_recorded += 1

        return write

    def reading_writer(self, write: Callable[[ReadingT], None]) -> Callable[[ReadingT], None]:
        """A readings sink (``JsonlReadingWriter.write``) that raises
        :class:`DiskWriteError` on a write failure, like :meth:`frame_writer`.

        The readings file flushes every line, so on a full disk it is usually
        the first write to fail — inside the poll loop, where a bare
        ``OSError`` reads as a bus fault and the bus gets rebuilt.
        """

        def wrapped(reading: ReadingT) -> None:
            try:
                write(reading)
            except OSError as exc:
                raise DiskWriteError(exc) from exc

        return wrapped

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def __enter__(self) -> RecorderRun:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        if exc is not None:
            self.error = exc
            self.stop_reason = "error"
        ended_at = datetime.now(UTC)
        if self.parse_error_owner is not None:
            # A parse-error burst that ran to the end is summarized here or never.
            flush_dropped_can_frame_log(owner=self.parse_error_owner, force=True)
        # The bus may have been rebuilt; the caller's reference is stale.
        # Close whatever is held so no USB handle leaks. The original bus is
        # the caller's to close.
        if self.reconnect_enabled and self.bus is not None and self.bus is not self._original_bus:
            self._shutdown(self.bus)
        reconnects = self.reconnector.reconnects if self.reconnector is not None else 0
        self.result = RecordingResult(
            started_at=self.started_at,
            ended_at=ended_at,
            frames_recorded=self.frames_recorded,
            stopped_by=self.stop_reason,
            parse_errors=self.parse_errors,
            reconnects=reconnects,
            dropped_writes=self.dropped_writes,
        )
        try:
            write_manifest(
                self.paths.manifest,
                spec=self.spec,
                vehicle=self.vehicle,
                result=self.result,
                vehicle_state=self.vehicle_state,
                error=self.error,
            )
        except Exception:
            if self.error is None:
                raise
            self.log.exception("failed to write manifest after recording error")
        self.log.info(
            "recording stopped",
            extra={
                "frames_recorded": self.frames_recorded,
                "stopped_by": self.stop_reason,
                "elapsed_seconds": (ended_at - self.started_at).total_seconds(),
                "parse_errors": self.parse_errors,
                "reconnects": reconnects,
                "dropped_writes": self.dropped_writes,
            },
        )
        return False

    def _finish(self, reason: str) -> bool:
        self.stop_reason = reason
        self.finished = True
        return True

    @staticmethod
    def _shutdown(bus: can.BusABC | None) -> None:
        if bus is None:
            return
        try:
            bus.shutdown()
        except Exception:  # noqa: BLE001 — the bus may already be dead; best-effort
            pass


__all__ = [
    "DiskWriteError",
    "RecorderRun",
    "RecordingResult",
    "write_manifest",
]
