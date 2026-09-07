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

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import obd

from src.collection.queueing import DropCallback, QueueDropCounter, threadsafe_enqueue
from src.collection.reconnect import ReconnectPolicy, run_with_reconnect
from src.telemetry import MetadataValue, TelemetryRecord

log = logging.getLogger(__name__)

# OBD-II service modes that mutate ECU state. This legacy ELM327 path does
# not send them. (The active Canable path has no such restriction — see
# ``can_obd_client.query``; this guard is local to this module.) The only one
# python-obd exposes today is Mode 04 (`CLEAR_DTC`); Mode 0x2E
# `WriteDataByIdentifier` and 0x2F `InputOutputControlByIdentifier` aren't in
# python-obd's stock command table, but anyone hand-crafting an `OBDCommand`
# (or `Async.query()` on a raw mode-byte command) could still send one — guard
# both paths. ELM327 ``AT*`` configuration commands keep mode=None and stay
# allowed.
WRITE_COMMAND_MODES: frozenset[int] = frozenset({0x04, 0x2E, 0x2F})


class WriteCommandRejectedError(ValueError):
    """Raised when a caller tries to schedule a non-read OBD command.

    Subclasses ``ValueError`` so existing collector retry paths (which already
    re-raise ``ValueError`` instead of reconnecting) still surface bad config
    immediately rather than spinning on a hopeless reconnect loop.
    """


def assert_read_only_command(command: Any, *, requester: str = "") -> None:
    """Reject any OBDCommand whose service mode mutates ECU state.

    Mode is fetched defensively — a duck-typed fake without ``.mode`` is
    treated as unknown and allowed (the tests' fakes don't set it). Real
    ``OBDCommand`` objects always have it.
    """
    mode = getattr(command, "mode", None)
    if mode is None:
        return
    if mode in WRITE_COMMAND_MODES:
        name = getattr(command, "name", repr(command))
        prefix = f"{requester}: " if requester else ""
        raise WriteCommandRejectedError(
            f"{prefix}OBD command {name!r} is service mode 0x{mode:02X} "
            f"(write/control); the ELM327 path does not send these. Refusing."
        )


#: Sentinel for "auto-discover what the ECU supports". When ``ObdSpec.pids``
#: is exactly ``(AUTO_PIDS,)``, ``obd_factory`` opens the connection, reads
#: ``conn.supported_commands`` (populated by python-obd's PIDS_A/B/C/D
#: bitmask probe), and watches every Mode-01 PID the ECU claims minus the
#: PIDS_* metadata commands themselves. Portable across vehicles — the same
#: code path picks up wideband-O2 PIDs on cars that have them, EGR PIDs on
#: cars that do, etc.
AUTO_PIDS = "auto"

#: Commands we exclude from auto-discovery: PIDS_A/B/C/D are bitmasks of
#: which other PIDs are supported (we used them to discover the set in the
#: first place — no value in polling them continuously).
_AUTO_EXCLUDED_PIDS: frozenset[str] = frozenset({"PIDS_A", "PIDS_B", "PIDS_C", "PIDS_D", "PIDS_9A"})


@dataclass(frozen=True)
class ObdSpec:
    """How to construct the python-obd Async client.

    ``pids`` accepts either an explicit tuple of python-obd command names
    (`("RPM", "SPEED", ...)`) or the sentinel ``(AUTO_PIDS,)`` meaning
    "watch every Mode-01 PID this ECU claims to support". Auto mode is the
    portable default — works on a 2008 Xterra, a 2018 F-150, or anything
    OBD-II-compliant without per-vehicle tuning. Trade-off: more PIDs in
    the watch loop = lower per-PID refresh rate, since the ELM327
    round-robins them.

    ``dids`` is the Mode 22 ReadDataByIdentifier set (16-bit hex DIDs as
    ``"1101"``-style strings; the ``22`` mode byte is prepended for you).
    These are polled alongside ``pids`` through one ELM327. For exploratory
    DID sweeps use ``tools/scan_constant_dids.py`` instead; this side is
    for *known* DIDs you want logged through a drive (e.g. the injector-PW
    DID, after it's been identified via 4.E.4).
    """

    port: str
    baudrate: int = 38400
    # None lets python-obd auto-detect the OBD-II protocol; pin to "6"
    # (ISO 15765-4 CAN 11/500) to skip the probing handshake on a known bus.
    protocol: str | None = None
    pids: tuple[str, ...] = ()
    dids: tuple[str, ...] = ()
    did_header: str = "7E0"  # request header for Mode 22 — engine ECU by default


#: Version of the timing metadata ``reading_to_record`` persists (C02.6).
TS_SCHEMA_VERSION = 2
#: Clock source behind ``ObdReading.acquired_at``.
AcquiredClock = Literal["kernel", "userspace"]


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
    #: Raw response data bytes the decoder consumed (None on paths that
    #: never see the wire bytes, e.g. python-obd's pint-quantity readings).
    #: Persisted to Parquet so a wrong decoder can be re-run over history.
    payload: bytes | None = None
    #: Wire-arrival instant of the (first) response frame — the measurement
    #: clock. The Canable collector always sets it (``ts_schema`` 2, C02.6);
    #: ``timestamp`` is then the *presentation* stamp: the capture round's
    #: scheduled start when the active profile asks for ``round_timestamps``,
    #: otherwise this same instant. ``None`` only on paths that never see the
    #: wire (the legacy ELM327 collector), whose rows stay ``stored``-basis.
    acquired_at: float | None = None
    #: Where ``acquired_at`` came from: ``"kernel"`` when the bus driver
    #: stamped the frame (SocketCAN ``SO_TIMESTAMP``; python-can
    #: ``Message.timestamp``) and it agreed with the wall clock, else
    #: ``"userspace"`` — a ``time.time()`` read after receipt.
    acquired_clock: AcquiredClock | None = None
    #: Multi-frame ISO-TP responses use the FIRST frame's arrival as
    #: ``acquired_at``; this is completion minus first frame when > 0, so the
    #: reassembly span is kept rather than folded into the stamp (C02.7).
    response_interval_s: float | None = None
    #: Capture generation and round this reading was acquired in (C02.2/C02.8).
    #: Round identity says which window scheduled the request; it is NOT
    #: evidence that members were sampled together — compare ``acquired_at``.
    generation: int | None = None
    round_id: int | None = None

    def to_json_line(self) -> str:
        # payload intentionally omitted: the JSONL sidecar is a human-facing
        # debug log; the bytes ride the Parquet ``payload`` column instead.
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


class ObdConnection(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


DEFAULT_RECONNECT_POLICY = ReconnectPolicy()


def resolve_pids(names: tuple[str, ...]) -> list[Any]:
    """Map python-obd command names to OBDCommand instances; raise on unknown.

    Validation runs before connecting to the adapter so an `--obd-pids RMP`
    typo fails loudly at startup rather than silently dropping a signal.

    The ``AUTO_PIDS`` sentinel is permitted but contributes no commands —
    auto-discovery happens later, after the connection is open and the
    ECU has reported its supported-PID bitmask.
    """
    out: list[Any] = []
    for name in names:
        if name == AUTO_PIDS:
            continue
        cmd = getattr(obd.commands, name, None)
        if cmd is None:
            raise ValueError(
                f"unknown OBD command {name!r}; "
                f"see https://python-obd.readthedocs.io/en/latest/Command%20Tables/"
            )
        assert_read_only_command(cmd, requester="resolve_pids")
        out.append(cmd)
    return out


def discover_supported_pids(conn: Any) -> list[Any]:
    """Read the ECU's supported Mode-01 PIDs and return the OBDCommand objects.

    Uses python-obd's ``conn.supported_commands``, populated at connection
    time from the PIDS_A/B/C/D bitmask responses. We filter to Mode 01
    (the only mode that's universally bitmask-queryable) and drop the
    PIDS_* metadata commands themselves. Every returned command also gets
    a ``assert_read_only_command`` check — defense-in-depth against a
    future python-obd that adds a write command to supported_commands.

    Returns a list, deterministically sorted by command name so a session's
    manifest is stable across runs.
    """
    discovered: list[Any] = []
    for cmd in conn.supported_commands:
        mode = getattr(cmd, "mode", None)
        name = getattr(cmd, "name", "")
        if mode != 1 or name in _AUTO_EXCLUDED_PIDS:
            continue
        assert_read_only_command(cmd, requester="discover_supported_pids")
        discovered.append(cmd)
    discovered.sort(key=lambda c: c.name)
    return discovered


def did_command(did_hex: str, *, header: str = "7E0") -> Any:
    """Build a python-obd OBDCommand that polls one Mode 22 DID.

    ``did_hex`` is a 4-character hex DID (``"1101"``, ``"F190"``, …). The
    ``22`` service byte is prepended. The decoder is a passthrough — the
    raw response bytes are stringified to hex and surfaced as the
    ``ObdReading.value``. Multi-frame ISO-TP responses arrive concatenated;
    consumers parse them downstream. ``header`` defaults to engine ECU
    (``7E0``); pass another to point at a different module.

    Service 0x22 is a *read* in the UDS sense, but python-obd's mode field
    is OBD-II service (1–9). We set mode=None so the read-only enforcement
    doesn't reject it.
    """
    if len(did_hex) != 4 or any(c not in "0123456789abcdefABCDEF" for c in did_hex):
        raise ValueError(f"DID must be a 4-character hex string, got {did_hex!r}")
    name = f"DID_{did_hex.upper()}"
    request = f"22{did_hex.upper()}".encode("ascii")

    def _decoder(messages: Any) -> str | None:
        """Concatenate response bytes across (potentially multi-frame) messages."""
        out = bytearray()
        for msg in messages or ():
            data = getattr(msg, "data", None)
            if not isinstance(data, (bytes, bytearray)):
                continue
            out.extend(data)
        return out.hex() if out else None

    return obd.OBDCommand(
        name,
        f"Mode 22 ReadDataByIdentifier DID 0x{did_hex.upper()}",
        request,
        0,
        _decoder,
        ecu=obd.protocols.ECU.ENGINE,
        fast=False,
        header=header.encode("ascii"),
    )


def obd_factory(
    spec: ObdSpec,
    on_reading: Callable[[ObdReading], None],
) -> obd.Async:
    """Build an `obd.Async` client and prime it with watch commands.

    Two PID-resolution paths:

    * **Explicit list** (``spec.pids = ("RPM", "SPEED", ...)``) — each PID
      is checked against the ECU's supported set; unsupported PIDs warn +
      skip per FR-01.
    * **Auto-discovery** (``spec.pids = (AUTO_PIDS,)``) — after the
      connection opens, query the ECU's reported supported-PID bitmask
      and watch every Mode-01 PID it claims (minus the PIDS_* metadata
      commands themselves). Portable across vehicles; this is the
      ``record.py`` default.

    Mode 22 DIDs from ``spec.dids`` are appended to the watch list with
    custom OBDCommands (``did_command``) regardless of the PID path.
    Returns the live connection — caller must call `.start()` and `.stop()`.
    """
    explicit_commands = resolve_pids(spec.pids)
    did_commands = [did_command(d, header=spec.did_header) for d in spec.dids]
    auto_mode = AUTO_PIDS in spec.pids

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
    enforce_read_only(conn)

    # Build the actual watch list. In auto mode the supported set lands here;
    # in explicit mode it's just what `resolve_pids` returned.
    if auto_mode:
        auto_commands = discover_supported_pids(conn)
        explicit_names = tuple(n for n in spec.pids if n != AUTO_PIDS)
        auto_names = tuple(c.name for c in auto_commands)
        # Union explicit + auto; explicit wins on dedupe so the operator can
        # ADD a PID the ECU's bitmask didn't claim (rare but useful for ECUs
        # that under-report).
        seen: set[str] = set()
        watch_commands: list[Any] = []
        explicit_paired = [c for c in explicit_commands if c.name in explicit_names]
        for name, cmd in [
            *zip(explicit_names, explicit_paired, strict=False),
            *zip(auto_names, auto_commands, strict=True),
        ]:
            if name in seen:
                continue
            seen.add(name)
            watch_commands.append(cmd)
        watch_names = tuple(c.name for c in watch_commands)
    else:
        watch_commands = explicit_commands
        watch_names = spec.pids

    log.info(
        "obd connected",
        extra={
            "port": spec.port,
            "auto_pid_discovery": auto_mode,
            "watching_pids": list(watch_names),
            "watching_dids": list(spec.dids),
        },
    )

    for pid_name, cmd in zip(watch_names, watch_commands, strict=True):
        if not conn.supports(cmd):
            log.warning("PID not supported by this ECU; skipping", extra={"pid": pid_name})
            continue
        conn.watch(cmd, callback=_make_callback(pid_name, on_reading))

    # DIDs aren't in the ECU's `supports()` set (python-obd only tracks
    # standard Mode 01 PIDs), so we watch them unconditionally and let
    # negative responses be logged as warnings by the decoder.
    for did, cmd in zip(spec.dids, did_commands, strict=True):
        conn.watch(cmd, callback=_make_callback(f"DID_{did.upper()}", on_reading))

    return conn


def enforce_read_only(conn: Any) -> Any:
    """Monkey-patch ``conn`` so ``watch``/``query`` reject write-mode commands.

    Defence-in-depth on top of ``resolve_pids``: any later caller — including
    a future Mode 22 DID extension or hand-written `Async.query()` — is
    guarded. The original method bindings are kept; we wrap them in a check
    that calls ``assert_read_only_command`` first.
    """
    for method_name in ("watch", "query", "unwatch"):
        original = getattr(conn, method_name, None)
        if original is None or getattr(original, "_canary_read_only_wrapped", False):
            continue

        def _wrap(orig: Any, label: str) -> Any:
            def _guarded(command: Any, *args: Any, **kwargs: Any) -> Any:
                assert_read_only_command(command, requester=f"obd.{label}")
                return orig(command, *args, **kwargs)

            _guarded._canary_read_only_wrapped = True  # type: ignore[attr-defined]
            return _guarded

        setattr(conn, method_name, _wrap(original, method_name))
    return conn


def reading_to_record(reading: ObdReading) -> TelemetryRecord:
    """Convert an OBD reading into the shared telemetry schema.

    Timing provenance travels in ``metadata`` (Parquet ``metadata_json``)
    under :data:`TS_SCHEMA_VERSION`; see :func:`acquisition_metadata`.
    """
    value = reading.value
    if not isinstance(value, (bool, int, float, str, type(None))):
        value = str(value)
    return TelemetryRecord.obd(
        reading.timestamp,
        reading.pid,
        value,
        reading.unit,
        payload=reading.payload,
        metadata=acquisition_metadata(reading),
    )


def acquisition_metadata(reading: ObdReading) -> dict[str, MetadataValue] | None:
    """The persisted timing fields for one reading, or ``None`` for legacy paths.

    Schema versions, as readers must interpret them (``docs/capture-rounds.md``):

    * absent ``acquired_at`` — legacy: the stored ``timestamp`` is a
      decode-time clock read, or a round stamp if the session manifest says
      ``round_timestamps`` (``timestamp_basis`` ``stored`` / ``round``);
    * ``acquired_at`` without ``ts_schema`` — v1: written only when the row
      was round-stamped;
    * ``ts_schema: 2`` — ``acquired_at`` is always present with its clock
      source, plus ``generation`` / ``round_id`` when a round scheduled it.
    """
    if reading.acquired_at is None:
        return None
    metadata: dict[str, MetadataValue] = {
        "ts_schema": TS_SCHEMA_VERSION,
        "acquired_at": reading.acquired_at,
        "acquired_clock": reading.acquired_clock or "userspace",
    }
    if reading.response_interval_s is not None:
        metadata["response_interval_s"] = reading.response_interval_s
    if reading.generation is not None:
        metadata["generation"] = reading.generation
    if reading.round_id is not None:
        metadata["round_id"] = reading.round_id
    return metadata


def queue_reading_callback(
    queue: asyncio.Queue[TelemetryRecord],
    loop: asyncio.AbstractEventLoop,
    *,
    on_drop: DropCallback | None = None,
) -> Callable[[ObdReading], None]:
    """Thread-safe callback that pushes OBD readings onto an asyncio queue."""
    enqueue = threadsafe_enqueue(loop, queue, on_drop=on_drop)

    def on_reading(reading: ObdReading) -> None:
        enqueue(reading_to_record(reading))

    return on_reading


async def collect_obd(
    spec: ObdSpec,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    factory: Callable[[ObdSpec, Callable[[ObdReading], None]], ObdConnection] = obd_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
    drop_counter: QueueDropCounter | None = None,
) -> None:
    """Run python-obd Async as a daemon queue producer until stopped."""
    loop = asyncio.get_running_loop()
    on_drop = QueueDropCounter("obd") if drop_counter is None else drop_counter

    async def _attempt(capture: Callable[[ObdConnection], None]) -> None:
        conn = factory(spec, queue_reading_callback(queue, loop, on_drop=on_drop))
        capture(conn)
        conn.start()
        log.info("obd collection started", extra={"port": spec.port})
        await stop_event.wait()

    def _teardown(conn: ObdConnection) -> None:
        try:
            conn.stop()
        except Exception:
            log.exception("failed to stop obd connection")

    await run_with_reconnect(
        _attempt,
        stop_event=stop_event,
        policy=reconnect_policy,
        teardown=_teardown,
        log=log,
        reconnect_message="obd collector failed; reconnecting",
        log_extra=lambda: {"port": spec.port},
    )


def supervise_obd_capture(
    spec: ObdSpec,
    on_reading: Callable[[ObdReading], None],
    stop_event: threading.Event,
    *,
    factory: Callable[[ObdSpec, Callable[[ObdReading], None]], ObdConnection] = obd_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
    port_exists: Callable[[str], bool] = os.path.exists,
    health_poll_seconds: float = 1.0,
    stale_after_seconds: float = 10.0,
) -> None:
    """Run OBD capture, surviving USB dropout / reattachment of the adapter.

    Built to run in a daemon thread next to the CAN recorder. It watches the
    adapter's device file and python-obd's connection status; when the adapter
    disappears (e.g. ``usbipd`` dropping a flaky CH340 over WSL-USB) or the
    serial link goes dead — including the case where python-obd's worker thread
    died silently and just stopped producing readings — it tears the connection
    down, waits for the device to reappear, and rebuilds, writing into the same
    ``on_reading`` sink so ``pids.jsonl`` stays continuous.

    With ``reconnect_policy=None`` it's a single best-effort attempt that still
    won't crash the caller. ``ValueError`` (a bad PID name) is re-raised since
    retrying can't fix it.
    """
    last_reading_at = [time.monotonic()]

    def _track(reading: ObdReading) -> None:
        last_reading_at[0] = time.monotonic()
        on_reading(reading)

    delay = reconnect_policy.initial_delay_seconds if reconnect_policy is not None else 0.0

    while not stop_event.is_set():
        if not _wait_for_obd_device(spec.port, stop_event, port_exists, health_poll_seconds):
            return  # stop requested while the adapter was missing

        conn: ObdConnection | None = None
        try:
            conn = factory(spec, _track)
            conn.start()
            connected_at = last_reading_at[0] = time.monotonic()
            log.info("obd capture connected", extra={"port": spec.port})
            if reconnect_policy is not None:
                delay = reconnect_policy.initial_delay_seconds  # reset backoff on success
            reason = _run_obd_health_loop(
                conn,
                spec.port,
                stop_event,
                port_exists=port_exists,
                health_poll_seconds=health_poll_seconds,
                stale_after_seconds=stale_after_seconds,
                last_reading_at=last_reading_at,
                connected_at=connected_at,
            )
            if stop_event.is_set():
                return
            log.warning(
                "obd capture interrupted; will reconnect",
                extra={"port": spec.port, "reason": reason},
            )
        except ValueError:
            raise
        except Exception as exc:
            if stop_event.is_set():
                return
            log.warning(
                "obd capture attempt failed",
                extra={"port": spec.port, "error": f"{type(exc).__name__}: {exc}"},
            )
        finally:
            if conn is not None:
                try:
                    conn.stop()
                except Exception:
                    log.exception("failed to stop obd connection", extra={"port": spec.port})

        if stop_event.is_set():
            return
        if reconnect_policy is None:
            log.info("obd reconnect disabled; giving up on OBD capture", extra={"port": spec.port})
            return
        if stop_event.wait(delay):
            return
        delay = reconnect_policy.next_delay(delay)


def _wait_for_obd_device(
    port: str,
    stop_event: threading.Event,
    port_exists: Callable[[str], bool],
    poll_seconds: float,
) -> bool:
    """Block until ``port`` exists or stop is requested. Return False if stopped."""
    announced = False
    while not stop_event.is_set():
        if port_exists(port):
            return True
        if not announced:
            log.warning(
                "obd adapter device not present; waiting for reattach", extra={"port": port}
            )
            announced = True
        if stop_event.wait(poll_seconds):
            return False
    return False


def _run_obd_health_loop(
    conn: ObdConnection,
    port: str,
    stop_event: threading.Event,
    *,
    port_exists: Callable[[str], bool],
    health_poll_seconds: float,
    stale_after_seconds: float,
    last_reading_at: list[float],
    connected_at: float,
) -> str:
    """Watch a live OBD connection; return why it stopped being healthy.

    Returns "" when stop was requested (the caller checks ``stop_event``).
    """
    is_connected = getattr(conn, "is_connected", None)
    while not stop_event.is_set():
        if stop_event.wait(health_poll_seconds):
            return ""
        if not port_exists(port):
            return "device disappeared"
        if callable(is_connected) and not is_connected():
            return "serial link reported disconnected"
        now = time.monotonic()
        if (
            now - connected_at > stale_after_seconds
            and now - last_reading_at[0] > stale_after_seconds
        ):
            return f"no readings for {stale_after_seconds:g}s"
    return ""


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
