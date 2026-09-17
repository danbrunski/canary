"""gpsd-backed GPS collection.

The alternative to :mod:`src.collection.gps_collector`'s serial NMEA path: here
``gpsd`` owns the receiver's serial port and canary reads fixes from gpsd's TCP
socket (``localhost:2947``) as line-delimited JSON.

Why bother, when the serial path already works? Because gpsd also feeds the
clock. gpsd publishes GPS time into a SHM segment that ``chrony`` consumes as a
refclock, so the Pi disciplines its clock from satellites with no network — the
whole point of the exercise (no RTC coin cell, no NTP in the truck). That only
works if gpsd owns the port, and a serial device has exactly one owner. So the
choice of GPS backend and the availability of offline time are the same switch.

Channel parity with the NMEA path is the hard requirement: both backends build
the same :class:`~src.collection.gps_nmea.GpsReading` and hand it to
``reading_to_records``, so the emitted ``gps:*`` channels, units, and the
speed-trust gating are shared code rather than two implementations that drift.
The only backend-specific logic is the field mapping in ``update_reading_gpsd``
-- see the notes there, two of the mappings are not the obvious ones.

Protocol: connect, send ``?WATCH={"enable":true,"json":true}``, then read one
JSON object per line. ``TPV`` (time-position-velocity) is the per-epoch fix
report and drives the flush, mirroring RMC in the NMEA path. ``SKY`` carries
fix health and arrives on its own cadence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from src.collection.gps_collector import reading_to_records
from src.collection.gps_nmea import GpsReading
from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import ReconnectPolicy, run_with_reconnect
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

DEFAULT_RECONNECT_POLICY = ReconnectPolicy()

#: Sent on connect to start the JSON stream. gpsd stays silent until watched.
WATCH_COMMAND = b'?WATCH={"enable":true,"json":true}\n'

#: gpsd ``TPV.mode``: 0 unknown, 1 no fix, 2 2D fix, 3 3D fix.
_MODE_HAS_FIX = 2


@dataclass(frozen=True)
class GpsdSpec:
    """How to reach the local gpsd instance."""

    host: str = "127.0.0.1"
    port: int = 2947
    #: Bounds how quickly the collector notices a stop request while idle
    #: between fixes, and how long a silent gpsd goes unnoticed.
    read_timeout_seconds: float = 2.0


class LineReader(Protocol):
    """The slice of the gpsd connection the collector uses (faked in tests)."""

    def readline(self) -> bytes: ...

    def close(self) -> None: ...


class GpsdReader:
    """Line-delimited reader over a gpsd socket.

    Rolls its own buffering rather than using ``socket.makefile`` because the
    read timeout is load-bearing: it's what lets the collector notice a stop
    request between fixes. A timeout on a buffered file object can strand a
    partially-read line, so instead we keep the partial buffer across timeouts
    and simply return ``b""`` -- which the read loop treats as "nothing yet".
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = b""

    def readline(self) -> bytes:
        while b"\n" not in self._buffer:
            try:
                chunk = self._sock.recv(4096)
            except TimeoutError:
                return b""
            if not chunk:  # gpsd closed the connection — surface it for reconnect
                raise ConnectionError("gpsd closed the connection")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line + b"\n"

    def close(self) -> None:
        self._sock.close()


def gpsd_factory(spec: GpsdSpec) -> LineReader:
    """Connect to gpsd and start the JSON watch. Read-only; we never steer it."""
    sock = socket.create_connection((spec.host, spec.port), timeout=spec.read_timeout_seconds)
    sock.settimeout(spec.read_timeout_seconds)
    sock.sendall(WATCH_COMMAND)
    return GpsdReader(sock)


def _as_float(value: Any) -> float | None:
    """Coerce a gpsd JSON field to float, or None if absent/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _used_satellites(obj: dict[str, Any]) -> int | None:
    """Satellites *used in the fix* from a SKY report.

    Deliberately not ``nSat`` (satellites *seen*). The NMEA path takes this
    channel from GGA, which reports satellites used, and the sensor-committee
    gate (``gps:num_satellites >= 7``) plus ``_speed_fix_trustworthy`` are tuned
    against that meaning. Seen-count runs well above used-count on a marginal
    fix, so sourcing it from ``nSat`` would quietly re-open the exact parked-
    truck speed-jitter leak those gates exist to close.
    """
    used = obj.get("uSat")
    if isinstance(used, int) and not isinstance(used, bool):
        return used
    satellites = obj.get("satellites")
    if isinstance(satellites, list):
        return sum(1 for sat in satellites if isinstance(sat, dict) and sat.get("used"))
    return None


def parse_gpsd_line(line: str) -> dict[str, Any] | None:
    """Decode one line of gpsd's JSON stream, or None if it isn't a report.

    gpsd is a local trusted source, but a half-line at connection setup or a
    future message shape shouldn't take the collector down.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("class"), str):
        return None
    return obj


def update_reading_gpsd(reading: GpsReading, obj: dict[str, Any]) -> None:
    """Merge one gpsd report into the in-progress fix epoch.

    Field mapping is mostly direct; the two that aren't:

    ``mode`` -> ``fix_quality`` is *not* a passthrough. The NMEA path fills
    ``fix_quality`` from GGA, where 0 means no fix and any positive value means
    a fix -- and ``gps_tracker`` relies on exactly that, deriving ``has_fix``
    from ``fix_quality > 0``. gpsd's ``mode`` uses a different scale where 1
    means *no fix*, so copying it across would make an unfixed receiver report
    a fix on the dashboard. We collapse to the GGA convention instead.

    ``speed`` is metres/second in gpsd and km/h in ``gps:speed`` (chosen to
    match ``obd:SPEED`` so the operating-mode segmenter can use either).
    """
    kind = obj.get("class")
    if kind == "TPV":
        mode = obj.get("mode")
        mode_value = mode if isinstance(mode, int) and not isinstance(mode, bool) else 0
        reading.has_fix = mode_value >= _MODE_HAS_FIX
        reading.fix_quality = 1 if reading.has_fix else 0
        reading.latitude = _as_float(obj.get("lat"))
        reading.longitude = _as_float(obj.get("lon"))
        # gpsd >= 3.20 splits altitude into MSL/HAE; GGA (and therefore the
        # serial path) reports MSL, so prefer it and fall back to legacy ``alt``.
        altitude = _as_float(obj.get("altMSL"))
        if altitude is None:
            altitude = _as_float(obj.get("alt"))
        reading.altitude_m = altitude
        speed_ms = _as_float(obj.get("speed"))
        reading.speed_kmh = None if speed_ms is None else speed_ms * 3.6
        reading.heading_deg = _as_float(obj.get("track"))
    elif kind == "SKY":
        satellites = _used_satellites(obj)
        if satellites is not None:
            reading.num_satellites = satellites
        hdop = _as_float(obj.get("hdop"))
        if hdop is not None:
            reading.hdop = hdop


def _carry_fix_health(previous: GpsReading) -> GpsReading:
    """Start the next epoch, retaining fix health from the previous one.

    SKY and TPV arrive on independent cadences -- gpsd may send several TPVs
    between SKY reports. The NMEA path can reset wholesale because GGA (its
    health source) accompanies every RMC burst; here a wholesale reset would
    blank ``num_satellites``/``hdop`` on most epochs, and those two channels
    gate whether ``gps:speed`` is trusted at all. So they persist until gpsd
    revises them.
    """
    return GpsReading(num_satellites=previous.num_satellites, hdop=previous.hdop)


async def collect_gpsd(
    reader: LineReader,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    now_fn: Callable[[], float] = time.time,
    drop_counter: QueueDropCounter | None = None,
) -> None:
    """Read gpsd JSON reports and enqueue ``gps:*`` records until stopped.

    One record set is flushed per ``TPV`` (the per-epoch fix report), mirroring
    the NMEA path's flush-on-RMC. The blocking socket read runs in a worker
    thread so the daemon event loop keeps serving the dashboard.
    """
    on_drop = QueueDropCounter("gps") if drop_counter is None else drop_counter
    reading = GpsReading()
    while not stop_event.is_set():
        raw = await asyncio.to_thread(reader.readline)
        if not raw:
            await asyncio.sleep(0)
            continue
        obj = parse_gpsd_line(raw.decode("utf-8", "ignore"))
        if obj is None:
            await asyncio.sleep(0)
            continue
        update_reading_gpsd(reading, obj)
        if obj.get("class") == "TPV":
            timestamp = now_fn()
            for record in reading_to_records(reading, timestamp):
                put_nowait_or_drop(queue, record, on_drop=on_drop)
            # Once per fix report (~1 Hz): closes out a drop burst's
            # rate-limited log, mirroring the NMEA path.
            on_drop.flush_drop_log()
            reading = _carry_fix_health(reading)
        await asyncio.sleep(0)


async def collect_gpsd_from_config(
    spec: GpsdSpec,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    factory: Callable[[GpsdSpec], LineReader] = gpsd_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
    now_fn: Callable[[], float] = time.time,
) -> None:
    """Own a gpsd connection for the daemon queue-producer path.

    Reconnects on the same bounded-backoff policy as the serial path: gpsd may
    be restarting, or may not have come up yet when canary starts.
    """
    drop_counter = QueueDropCounter("gps")

    async def _attempt(capture: Callable[[LineReader], None]) -> None:
        reader = factory(spec)
        capture(reader)
        log.info(
            "gps collection started (gpsd)",
            extra={"host": spec.host, "port": spec.port},
        )
        await collect_gpsd(
            reader,
            queue,
            stop_event,
            now_fn=now_fn,
            drop_counter=drop_counter,
        )

    def _teardown(reader: LineReader) -> None:
        try:
            reader.close()
        except Exception:  # noqa: BLE001 — best-effort close on a possibly-dead handle
            pass

    await run_with_reconnect(
        _attempt,
        stop_event=stop_event,
        policy=reconnect_policy,
        teardown=_teardown,
        log=log,
        reconnect_message="gpsd collector failed; reconnecting",
        log_extra=lambda: {"host": spec.host, "port": spec.port},
    )
