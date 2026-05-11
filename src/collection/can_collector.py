"""CAN bus collection: bus construction + frame recording.

Owns the read-only path from a `python-can` Bus to a candump-format `.log`
file plus a session manifest. Format choice is candump because every tool in
the ecosystem (SavvyCAN, Cabana, Wireshark, `canplayer`) reads it natively
and it's append-safe across crashes — Parquet conversion happens later as a
separate offline step.

The collector NEVER calls `bus.send()`. CANary is listen-only by FR-02 / NFR-06.
"""

from __future__ import annotations

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

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionPaths:
    """File layout for a single recording session."""

    root: Path
    frames: Path
    manifest: Path


@dataclass(frozen=True)
class RecordingResult:
    started_at: datetime
    ended_at: datetime
    frames_recorded: int
    stopped_by: str  # "deadline" | "signal"


def bus_factory(spec: CanBusConfig) -> can.BusABC:
    """Construct a python-can Bus from config.

    `bitrate` is omitted for `virtual` (the backend rejects it as unknown).
    """
    kwargs: dict[str, Any] = {"interface": spec.interface, "channel": spec.channel}
    if spec.interface != "virtual":
        kwargs["bitrate"] = spec.bitrate
    return can.Bus(**kwargs)


def make_session_dir(out_root: Path, started_at: datetime) -> SessionPaths:
    """Create `out_root/<UTC-timestamp>/` and return the file layout.

    Timestamp uses `T` separator and no colons so the path is safe on every
    filesystem we care about (tmpfs, ext4, exFAT, NTFS via WSL).
    """
    ts = started_at.strftime("%Y-%m-%dT%H%M%S")
    root = out_root / ts
    root.mkdir(parents=True, exist_ok=False)
    return SessionPaths(root=root, frames=root / "frames.log", manifest=root / "manifest.json")


def record(
    bus: can.BusABC,
    paths: SessionPaths,
    *,
    spec: CanBusConfig,
    vehicle: VehicleConfig | None,
    duration: float | None,
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

    ended_at = datetime.now(UTC)
    result = RecordingResult(
        started_at=started_at,
        ended_at=ended_at,
        frames_recorded=count,
        stopped_by=stop_reason,
    )
    _write_manifest(paths.manifest, spec=spec, vehicle=vehicle, result=result)
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
) -> None:
    payload: dict[str, Any] = {
        "started_at": result.started_at.isoformat(),
        "ended_at": result.ended_at.isoformat(),
        "duration_seconds": (result.ended_at - result.started_at).total_seconds(),
        "host": socket.gethostname(),
        "vehicle": vehicle.model_dump() if vehicle is not None else None,
        "bus": {
            "interface": spec.interface,
            "channel": spec.channel,
            "bitrate": spec.bitrate,
        },
        "frames_recorded": result.frames_recorded,
        "stopped_by": result.stopped_by,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
