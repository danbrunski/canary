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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import can

from src.config import CanBusConfig, VehicleConfig
from src.session import SessionPaths
from src.session import make_session_dir as _make_session_dir
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordingResult:
    started_at: datetime
    ended_at: datetime
    frames_recorded: int
    stopped_by: str  # "deadline" | "signal"


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
) -> None:
    """Read CAN frames and enqueue normalized telemetry records until stopped."""
    while not stop_event.is_set():
        msg = bus.recv(timeout=poll_interval)
        if msg is None:
            await asyncio.sleep(0)
            continue
        await queue.put(frame_to_record(msg))
        await asyncio.sleep(0)


async def collect_can_from_config(
    spec: CanBusConfig,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    poll_interval: float = 0.2,
) -> None:
    """Own a configured Bus for the daemon queue-producer path."""
    bus = bus_factory(spec)
    try:
        await collect_can(bus, queue, stop_event, poll_interval=poll_interval)
    finally:
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
) -> RecordingResult:
    """Record frames from `bus` into `paths.frames` until duration or stop_event.

    Either `duration` (seconds) or `stop_event` ends the loop; both may be set,
    whichever fires first wins. The bus is not closed here — the caller owns
    its lifetime so the same bus can be reused across sessions if needed.
    Manifest is written on exit, even if zero frames were captured.
    """
    started_at = datetime.now(UTC)
    deadline = time.monotonic() + duration if duration is not None else None
    stop_reason = "signal"
    count = 0

    log.info(
        "recording started",
        extra={
            "frames_path": str(paths.frames),
            "interface": spec.interface,
            "channel": spec.channel,
            "bitrate": spec.bitrate,
            "duration_seconds": duration,
        },
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
                msg = bus.recv(timeout=poll_interval)
                if msg is None:
                    continue
                writer.on_message_received(msg)
                count += 1
    except BaseException as exc:
        error = exc
        stop_reason = "error"
        raise
    finally:
        ended_at = datetime.now(UTC)
        result = RecordingResult(
            started_at=started_at,
            ended_at=ended_at,
            frames_recorded=count,
            stopped_by=stop_reason,
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
            },
        )
    return result


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
    }
    if error is not None:
        payload["error"] = f"{type(error).__name__}: {error}"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
