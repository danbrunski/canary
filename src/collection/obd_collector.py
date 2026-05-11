"""OBD-II Mode 01 PID polling for parallel-capture sessions.

Pairs with the CAN recorder: when both run during one session, the CAN
side captures the proprietary Mode 22 traffic from the NT510 and the
OBD side captures the standardized Mode 01 PIDs python-obd queries
through the ELM327. Both streams share the same wall clock
(`time.time()`), enabling offline cross-reference to derive scalings
for the unknown Mode 22 DIDs (decoded_mode01 ↔ raw_mode22 → linear
regression → bound DID with scale + offset).

Library usage:

    spec = ObdSpec(port="/dev/ttyUSB0", pids=("RPM", "COOLANT_TEMP", "MAF"))
    writer = JsonlReadingWriter(Path("pids.jsonl"))
    conn = obd_factory(spec, on_reading=writer.write)
    conn.start()
    ...
    conn.stop()
    writer.close()

This module is also the seed of the FR-01 OBD-II daemon collector — same
poll loop, just a different sink (Parquet ring buffer instead of JSONL).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import obd

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObdSpec:
    """How to construct the python-obd Async client."""

    port: str
    baudrate: int = 38400
    # None lets python-obd auto-detect the OBD-II protocol; pin to "6"
    # (ISO 15765-4 CAN 11/500) to skip the probing handshake on a known bus.
    protocol: str | None = None
    pids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObdReading:
    """One Mode 01 PID reading at a wall-clock timestamp.

    `timestamp` uses `time.time()` so it aligns directly with the
    candump-format `frames.log` from the CAN recorder.
    `value` is decoded — float for numeric PIDs (after pint stripping),
    str for textual ones (VIN), list for DTCs.
    """

    timestamp: float
    pid: str
    value: Any
    unit: str

    def to_json_line(self) -> str:
        return json.dumps(
            {"t": self.timestamp, "pid": self.pid, "value": self.value, "unit": self.unit}
        )


class JsonlReadingWriter:
    """Append OBD readings to a JSONL file, flushed per write for crash safety.

    Mirrors how the tag sidecar works — durable per line so a crash mid-
    session loses at most one in-flight reading, and external tools can
    tail the file live during a recording.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = path.open("a")

    def write(self, reading: ObdReading) -> None:
        self._fp.write(reading.to_json_line() + "\n")
        self._fp.flush()

    def close(self) -> None:
        self._fp.close()


def resolve_pids(names: tuple[str, ...]) -> list[Any]:
    """Map python-obd command names to OBDCommand instances; raise on unknown.

    Validation runs before connecting to the adapter so an `--obd-pids RMP`
    typo fails loudly at startup rather than silently dropping a signal.
    """
    out: list[Any] = []
    for name in names:
        cmd = getattr(obd.commands, name, None)
        if cmd is None:
            raise ValueError(
                f"unknown OBD command {name!r}; "
                f"see https://python-obd.readthedocs.io/en/latest/Command%20Tables/"
            )
        out.append(cmd)
    return out


def obd_factory(
    spec: ObdSpec,
    on_reading: Callable[[ObdReading], None],
) -> obd.Async:
    """Build an `obd.Async` client and prime it with watch commands.

    Each PID is checked against the ECU's supported set; unsupported PIDs
    are warned and skipped (per FR-01: "log a warning and continue").
    Returns the live connection — caller must call `.start()` and `.stop()`.
    """
    commands = resolve_pids(spec.pids)

    kwargs: dict[str, Any] = {"portstr": spec.port, "baudrate": spec.baudrate}
    if spec.protocol is not None:
        kwargs["protocol"] = spec.protocol
    conn = obd.Async(**kwargs)
    if not conn.is_connected():
        raise RuntimeError(
            f"OBD-II adapter at {spec.port} did not connect. "
            "Check `usbipd attach`, that /dev/ttyUSB* exists in WSL, "
            "and that the ELM327 is plugged into the vehicle's OBD-II port."
        )

    log.info("obd connected", extra={"port": spec.port, "watching": list(spec.pids)})

    for pid_name, cmd in zip(spec.pids, commands, strict=True):
        if not conn.supports(cmd):
            log.warning("PID not supported by this ECU; skipping", extra={"pid": pid_name})
            continue
        conn.watch(cmd, callback=_make_callback(pid_name, on_reading))

    return conn


def _make_callback(
    pid_name: str,
    on_reading: Callable[[ObdReading], None],
) -> Callable[[Any], None]:
    """Wrap the user callback into python-obd's `OBDResponse → None` shape.

    Captures the timestamp at the moment the response arrives (not when
    the request was issued) — this is the time we want to align with the
    CAN recorder's view of the response frame.
    """

    def cb(response: Any) -> None:
        if response is None or (hasattr(response, "is_null") and response.is_null()):
            return
        value = response.value
        unit = ""
        # python-obd numeric responses are pint Quantities; strip the unit
        # into a separate field so JSON stays primitive-friendly.
        if hasattr(value, "magnitude") and hasattr(value, "units"):
            units = str(value.units)
            unit = "" if units == "dimensionless" else units
            value = float(value.magnitude)
        on_reading(ObdReading(timestamp=time.time(), pid=pid_name, value=value, unit=unit))

    return cb
