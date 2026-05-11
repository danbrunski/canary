"""CAN bus collection: bus construction + frame recording.

Owns the read-only path from a `python-can` Bus to a candump-format `.log`
file plus a session manifest. Format choice is candump because every tool in
the ecosystem (SavvyCAN, Cabana, Wireshark, `canplayer`) reads it natively
and it's append-safe across crashes — Parquet conversion happens later as a
separate offline step.

The collector NEVER calls `bus.send()`. CANary is listen-only by FR-02 / NFR-06.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import can

from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import ReconnectPolicy, wait_for_stop_or_timeout
from src.config import CanBusConfig, VehicleConfig
from src.session import SessionPaths
from src.session import make_session_dir as _make_session_dir
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

DEFAULT_RECONNECT_POLICY = ReconnectPolicy()


@dataclass(frozen=True)
class RecordingResult:
    started_at: datetime
    ended_at: datetime
    frames_recorded: int
    stopped_by: str  # "deadline" | "signal" | "error" | "disk_error"
    parse_errors: int = 0  # malformed frames dropped without aborting the run
    reconnects: int = 0  # successful bus rebuilds during the recording
    dropped_writes: int = 0  # frames the disk writer failed to persist


def make_session_dir(out_root: Path, started_at: datetime) -> SessionPaths:
    """Create a timestamped recording session directory."""
    return _make_session_dir(out_root, started_at)


def bus_factory(spec: CanBusConfig) -> can.BusABC:
    """Construct a python-can Bus from config.

    `bitrate` is omitted for `virtual` (the backend rejects it as unknown).
    """
    kwargs: dict[str, Any] = {"interface": spec.interface, "channel": spec.channel}
    if spec.interface != "virtual":
        kwargs["bitrate"] = spec.bitrate
    return can.Bus(**kwargs)


def frame_to_record(msg: can.Message) -> TelemetryRecord:
    """Convert a python-can message into the shared telemetry schema."""
    return TelemetryRecord.can(
        float(msg.timestamp),
        int(msg.arbitration_id),
        bytes(msg.data),
    )


async def collect_can(
    bus: can.BusABC,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    poll_interval: float = 0.2,
    drop_counter: QueueDropCounter | None = None,
) -> None:
    """Read CAN frames and enqueue normalized telemetry records until stopped."""
    on_drop = QueueDropCounter("can") if drop_counter is None else drop_counter
    parse_errors = 0
    while not stop_event.is_set():
        try:
            msg = bus.recv(timeout=poll_interval)
        except ValueError as exc:
            parse_errors += 1
            if parse_errors == 1 or parse_errors % 100 == 0:
                log.warning(
                    "dropped malformed CAN frame",
                    extra={
                        "error": f"{type(exc).__name__}: {exc}",
                        "parse_errors_total": parse_errors,
                    },
                )
            await asyncio.sleep(0)
            continue
        if msg is None:
            await asyncio.sleep(0)
            continue
        put_nowait_or_drop(queue, frame_to_record(msg), on_drop=on_drop)
        await asyncio.sleep(0)


async def collect_can_from_config(
    spec: CanBusConfig,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    poll_interval: float = 0.2,
    factory: Callable[[CanBusConfig], can.BusABC] = bus_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
) -> None:
    """Own a configured Bus for the daemon queue-producer path."""
    delay = reconnect_policy.initial_delay_seconds if reconnect_policy is not None else 0.0
    drop_counter = QueueDropCounter("can")

    while not stop_event.is_set():
        bus: can.BusABC | None = None
        try:
            bus = factory(spec)
            log.info(
                "can collection started",
                extra={"interface": spec.interface, "channel": spec.channel},
            )
            await collect_can(
                bus,
                queue,
                stop_event,
                poll_interval=poll_interval,
                drop_counter=drop_counter,
            )
            return
        except ValueError:
            raise
        except Exception as exc:
            if stop_event.is_set():
                return
            if reconnect_policy is None:
                raise
            log.warning(
                "can collector failed; reconnecting",
                extra={
                    "interface": spec.interface,
                    "channel": spec.channel,
                    "error": f"{type(exc).__name__}: {exc}",
                    "reconnect_delay_seconds": delay,
                },
            )
            if await wait_for_stop_or_timeout(stop_event, delay):
                return
            delay = reconnect_policy.next_delay(delay)
        finally:
            if bus is not None:
                bus.shutdown()


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

    Frame-level parse errors (ValueError / IndexError from python-can's
    slcan parser, or `can.CanError` subclasses) are ALWAYS caught and
    counted into `parse_errors` — they're single bad lines out of millions,
    not a bus-level failure.

    Disk-write failures (OSError from `writer.on_message_received`) end the
    recording cleanly with `stop_reason = "disk_error"` and a count of
    `dropped_writes`. The manifest still gets written.
    """
    started_at = datetime.now(UTC)
    deadline = time.monotonic() + duration if duration is not None else None
    stop_reason = "signal"
    count = 0
    parse_errors = 0  # python-can slcan parser blows up on corrupt frames; count them
    reconnects = 0  # successful bus rebuilds via factory()
    dropped_writes = 0  # frames the disk writer couldn't persist

    log.info(
        "recording started",
        extra={
            "frames_path": str(paths.frames),
            "interface": spec.interface,
            "channel": spec.channel,
            "bitrate": spec.bitrate,
            "duration_seconds": duration,
            "auto_reconnect": factory is not None and reconnect_policy is not None,
        },
    )

    reconnect_enabled = factory is not None and reconnect_policy is not None
    current_bus: can.BusABC | None = bus
    reconnect_delay = (
        reconnect_policy.initial_delay_seconds if reconnect_policy is not None else 0.0
    )

    error: BaseException | None = None
    result: RecordingResult
    try:
        with can.io.CanutilsLogWriter(str(paths.frames)) as writer:
            while True:
                if stop_event is not None and stop_event.is_set():
                    stop_reason = "signal"
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    stop_reason = "deadline"
                    break

                # If we lost the bus on a previous iteration and reconnect is
                # configured, try to rebuild it before attempting to recv.
                if current_bus is None:
                    assert reconnect_enabled  # otherwise we'd have raised already
                    assert factory is not None and reconnect_policy is not None
                    if _wait_interruptible(stop_event, reconnect_delay):
                        stop_reason = "signal"
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        stop_reason = "deadline"
                        break
                    try:
                        current_bus = factory(spec)
                        reconnects += 1
                        reconnect_delay = reconnect_policy.initial_delay_seconds
                        log.info(
                            "bus reconnected",
                            extra={"total_reconnects": reconnects},
                        )
                    except Exception as build_exc:
                        log.warning(
                            "bus rebuild failed; will retry",
                            extra={
                                "error": f"{type(build_exc).__name__}: {build_exc}",
                                "next_delay_seconds": reconnect_delay,
                            },
                        )
                        reconnect_delay = reconnect_policy.next_delay(reconnect_delay)
                        continue

                try:
                    msg = current_bus.recv(timeout=poll_interval)
                except (ValueError, IndexError, can.CanError) as exc:
                    # Frame-level parser error. See slcan.py:282 (ValueError)
                    # and slcan.py:304 (IndexError) for the two observed modes.
                    # Drop the bad line, keep recording.
                    parse_errors += 1
                    if parse_errors == 1 or parse_errors % 100 == 0:
                        log.warning(
                            "dropped malformed CAN frame",
                            extra={
                                "error": f"{type(exc).__name__}: {exc}",
                                "parse_errors_total": parse_errors,
                            },
                        )
                    continue
                except Exception as exc:
                    # Bus-level failure (OSError from a USB disconnect,
                    # RuntimeError from adapter reset, etc.). Either we can
                    # reconnect — close the dead bus and let the next loop
                    # iteration rebuild it — or we propagate (backward-compat
                    # behavior when factory / reconnect_policy aren't set).
                    if not reconnect_enabled:
                        raise
                    log.warning(
                        "bus recv failed; will attempt reconnect",
                        extra={
                            "error": f"{type(exc).__name__}: {exc}",
                            "delay_seconds": reconnect_delay,
                        },
                    )
                    try:
                        current_bus.shutdown()
                    except Exception:
                        # Bus may already be dead; shutting it down is best-effort.
                        pass
                    current_bus = None
                    continue

                if msg is None:
                    continue

                try:
                    writer.on_message_received(msg)
                    count += 1
                except OSError as exc:
                    # Disk failure mid-flush — full disk, read-only filesystem,
                    # SD card hiccup. Recording can't continue meaningfully.
                    # End the loop cleanly so the manifest still writes with
                    # an accurate stopped_by + dropped_writes.
                    dropped_writes += 1
                    log.error(
                        "disk write failed; stopping recording",
                        extra={
                            "error": f"{type(exc).__name__}: {exc}",
                            "frames_recorded_before_failure": count,
                        },
                    )
                    stop_reason = "disk_error"
                    error = exc
                    break
    except BaseException as exc:
        error = exc
        stop_reason = "error"
        raise
    finally:
        ended_at = datetime.now(UTC)
        # The bus may have been rebuilt during reconnect; the caller's
        # reference is stale. Close whatever we're currently holding so
        # we don't leak a USB handle even on the disk-error path. The
        # original bus (if it's the same object) gets closed twice, which
        # python-can tolerates.
        if reconnect_enabled and current_bus is not None and current_bus is not bus:
            try:
                current_bus.shutdown()
            except Exception:
                pass
        result = RecordingResult(
            started_at=started_at,
            ended_at=ended_at,
            frames_recorded=count,
            stopped_by=stop_reason,
            parse_errors=parse_errors,
            reconnects=reconnects,
            dropped_writes=dropped_writes,
        )
        try:
            _write_manifest(
                paths.manifest,
                spec=spec,
                vehicle=vehicle,
                result=result,
                vehicle_state=vehicle_state,
                error=error,
            )
        except Exception:
            if error is None:
                raise
            log.exception("failed to write manifest after recording error")
        log.info(
            "recording stopped",
            extra={
                "frames_recorded": count,
                "stopped_by": stop_reason,
                "elapsed_seconds": (ended_at - started_at).total_seconds(),
                "parse_errors": parse_errors,
                "reconnects": reconnects,
                "dropped_writes": dropped_writes,
            },
        )
    return result


def _wait_interruptible(stop_event: threading.Event | None, seconds: float) -> bool:
    """Sleep for `seconds`, but wake immediately if `stop_event` fires.

    Returns True if the wait was cut short by the stop event — caller
    should treat that as a clean stop signal.
    """
    if seconds <= 0:
        return False
    if stop_event is None:
        time.sleep(seconds)
        return False
    return stop_event.wait(seconds)


def _write_manifest(
    path: Path,
    *,
    spec: CanBusConfig,
    vehicle: VehicleConfig | None,
    result: RecordingResult,
    vehicle_state: str | None = None,
    error: BaseException | None = None,
) -> None:
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
