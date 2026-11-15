"""Typed SAE J1979 PID / service registry (Task 6.1).

Every public observation PID/service exposed by the scheduled poller, with the
information the dashboard and coverage views (Task 6.3) need to explain *why* a
supported PID is or isn't a usable telemetry channel. ``PID_DECODERS``, the
minimal "PID number → (name, decoder)" table the streaming collector polls, is
derived from the ``polled`` rows of the Mode 01 spec here (R23.14): one fact,
one table, so the two cannot drift.

Design:

* The polled Mode 01 decoders stay single-sourced in ``pid_decoders`` — this
  module *imports* them, never re-implements them. PIDs whose J1979 formula is
  well-defined but not polled get a decoder defined here; PIDs that
  are real and supportable but whose value we deliberately don't decode get
  ``decoder=None`` so they classify as "supported but not decoded" rather than
  vanishing (Risk R7 in the plan).
* Mode 02 (freeze frame) reuses Mode 01 formulas — same ``A B`` bytes after the
  frame-number byte — so it's expressed as a view over the Mode 01 table.
* Mode 03 / 07 / 0A (DTC reads) and Mode 06 (on-board monitor results) and
  Mode 09 (vehicle info) are described as services with their own parsers.

This registry is deliberately narrower than the general diagnostic request
path: every entry is a J1979 data query. Mode 04 / 2E / 2F do not belong in a
telemetry metadata catalog, while `POST /control/obd_request` remains free to
send those or any other service byte.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from src.collection.pid_decoders import (
    PidDecoder,
    decode_absolute_load,
    decode_baro,
    decode_catalyst_temp,
    decode_commanded_lambda,
    decode_control_module_voltage,
    decode_distance_km,
    decode_egr_error,
    decode_egr_percent,
    decode_evap_purge,
    decode_evap_vapor_pressure_signed,
    decode_fuel_status,
    decode_intake_pressure,
    decode_maf,
    decode_monitor_status,
    decode_o2_voltage,
    decode_percent_a_over_255,
    decode_percent_b_minus_128,
    decode_rpm,
    decode_run_time,
    decode_speed,
    decode_temp_minus_40,
    decode_timing_advance,
    decode_warmups,
    decode_wide_lambda,
    linear,
    parse_dtc_payload,
)

__all__ = [
    "MISFIRE_COUNT_CHANNEL_PREFIX",
    "MODE01_REGISTRY",
    "MODE09_REGISTRY",
    "PID_DECODERS",
    "SERVICE_REGISTRY",
    "Category",
    "DashboardHint",
    "PidCoverage",
    "PidDefinition",
    "PollTier",
    "ServiceDefinition",
    "classify_pid",
    "decode_ascii",
    "decode_calibration_ids",
    "decode_cvn",
    "decode_vin",
    "lookup",
    "misfire_count_channel",
    "misfire_counts_from_results",
    "misfire_cylinder_number",
    "mode01_support_group",
    "parse_dtc_payload",
    "parse_mode06_response",
]

#: ``"dtc"`` is only used by ``obd_scheduler.PollRequest`` (the periodic
#: Mode 03/07/0A poll, Task 1.3) — no ``PidDefinition`` ever carries it since
#: it isn't a Mode 01 PID cadence.
PollTier = Literal["fast", "medium", "slow", "on_demand", "dtc"]
Category = Literal[
    "engine",
    "fuel",
    "air",
    "temperature",
    "pressure",
    "emissions",
    "o2",
    "speed",
    "electrical",
    "torque",
    "status",
    "info",
    "dtc",
    "monitor",
]
DashboardHint = Literal["gauge", "graph", "text", "indicator", "list"]

#: A decoder takes the service payload (bytes after the ``41 PID`` /
#: ``62 DID`` echo) and returns ``(value, unit)`` or ``None`` when the payload
#: is too short. Most return ``float``; Mode 09 text infotypes return ``str``.
DecodeResult = tuple[float | str, str] | None
Decoder = Callable[[bytes], DecodeResult]


@dataclass(frozen=True)
class PidDefinition:
    """One data-query PID, with everything the dashboard/coverage view needs.

    ``decoder is None`` means "this PID is real and CANary may request it, but
    we deliberately don't translate the bytes into an engineering value yet" —
    the coverage endpoint surfaces it as ``supported_not_decoded`` instead of
    dropping it (so "28 channels" never hides undecoded PIDs).
    """

    mode: int
    pid: int
    channel: str
    display_name: str
    category: Category
    unit: str
    #: Expected service-payload length in bytes. ``0`` = variable (DTC/VIN).
    byte_length: int
    poll_tier: PollTier
    dashboard_hint: DashboardHint
    decoder: Decoder | None
    #: Base PID of the Mode 01 supported-PID bitmask this PID lives in
    #: (``0x00`` covers 0x01–0x20, ``0x20`` covers 0x21–0x40, …). ``None``
    #: for non-Mode-01 entries.
    support_group: int | None


@dataclass(frozen=True)
class ServiceDefinition:
    """A whole OBD service (Mode 02/03/06/07/0A) rather than a single PID."""

    mode: int
    name: str
    display_name: str
    description: str
    #: ``True`` when the operator triggers it explicitly (DTC scan, freeze
    #: frame) rather than it being polled on a telemetry tier.
    on_demand: bool


# ---------------------------------------------------------------------------
# Decoders for well-defined J1979 PIDs the collector does not poll.
# Single-source rule: decoders already in pid_decoders are imported, never
# duplicated. These cover formula-stable PIDs it doesn't.
# ---------------------------------------------------------------------------

#: PID 0x0A: gauge fuel pressure, ``A * 3`` kPa.
decode_fuel_pressure = linear(1, 3, 0, "kPa")
#: Single-byte status bitmask (secondary-air status, O2-present, …).
decode_bitmask_a = linear(1, 1, 0, "bitmask")
#: Single-byte enum code: PID 0x1C (which OBD standard the ECU conforms to),
#: PID 0x51 (fuel type; 1 = gasoline, 4 = diesel, …).
decode_enum = linear(1, 1, 0, "enum")
#: PID 0x22: fuel rail pressure rel. to manifold vacuum, ``*0.079`` kPa.
decode_fuel_rail_pressure_vac = linear(2, 0.079, 0, "kPa")
#: PID 0x23 / 0x59: fuel rail gauge/absolute pressure, ``*10`` kPa.
decode_fuel_rail_gauge_pressure = linear(2, 10, 0, "kPa")
#: PID 0x4D / 0x4E: a time counter in minutes, ``A*256+B``.
decode_minutes = linear(2, 1, 0, "min")
#: PID 0x53: absolute evap system vapor pressure, ``(A*256+B)/200`` kPa.
decode_abs_evap_pressure = linear(2, "1/200", 0, "kPa")
#: PID 0x54: evap system vapor pressure, ``(A*256+B) - 32767`` Pa.
decode_evap_pressure_pa = linear(2, 1, -32767, "Pa")
#: PID 0x5D: fuel injection timing, ``((A*256+B) - 26880)/128`` deg.
decode_injection_timing = linear(2, "1/128", -210, "deg")
#: PID 0x5E: engine fuel rate, ``(A*256+B)/20`` L/h.
decode_fuel_rate = linear(2, "1/20", 0, "L/h")
#: PID 0x61 / 0x62: driver-demand / actual engine torque, ``A - 125`` %.
decode_torque_percent = linear(1, 1, -125, "%")
#: PID 0x63: engine reference torque, ``A*256+B`` Nm.
decode_reference_torque = linear(2, 1, 0, "Nm")


# --- Mode 09 vehicle-information decoders (text, not numeric) --------------


def _strip_padding(raw: bytes) -> bytes:
    """Drop ISO 15765 message-count prefixes / NUL+0xFF padding bytes."""
    return bytes(b for b in raw if 0x20 <= b < 0x7F)


def decode_ascii(data: bytes) -> tuple[str, str] | None:
    """Mode 09 ECU-name / generic ASCII infotype."""
    text = _strip_padding(data).decode("ascii", errors="ignore").strip()
    return (text, "text") if text else None


def decode_vin(data: bytes) -> tuple[str, str] | None:
    """Mode 09 InfoType 0x02 — 17-character VIN.

    The reassembled payload may carry a leading message-count byte; the VIN
    itself is the trailing 17 printable-ASCII characters.
    """
    text = _strip_padding(data).decode("ascii", errors="ignore").strip()
    if not text:
        return None
    return (text[-17:] if len(text) >= 17 else text, "text")


def decode_calibration_ids(data: bytes) -> tuple[str, str] | None:
    """Mode 09 InfoType 0x04 — one or more 16-byte calibration ID strings."""
    text = _strip_padding(data).decode("ascii", errors="ignore").strip()
    return (text, "text") if text else None


def decode_cvn(data: bytes) -> tuple[str, str] | None:
    """Mode 09 InfoType 0x06 — calibration verification numbers (4-byte hex).

    Like InfoType 0x02/0x04 the reassembled payload carries a leading
    message-count byte (how many 4-byte CVNs follow). The ASCII infotypes shed
    it incidentally when `_strip_padding` drops the non-printable byte; CVNs
    are binary, so nothing removes it and every group shifts one byte left.

    Observed on the Xterra 2026-07-26: the ECU returned ``01 0B4B7C60`` — one
    CVN of ``0B4B7C60`` — and this rendered it as ``010B4B7C``, inventing a
    leading ``01`` and silently dropping the trailing ``60``. A CVN is a
    calibration checksum whose whole purpose is exact comparison, so a
    plausible-looking wrong value is worse than none.

    Only stripped when the length actually implies a count byte
    (``len % 4 == 1``), so a bare 4-byte CVN still decodes unchanged.
    """
    if not data:
        return None
    body = data[1:] if len(data) % 4 == 1 else data
    # CVNs are not ASCII — render as space-separated 4-byte hex groups.
    groups = [body[i : i + 4].hex().upper() for i in range(0, len(body) - len(body) % 4, 4)]
    return (" ".join(groups), "hex") if groups else None


# ---------------------------------------------------------------------------
# Mode 06 — on-board monitor test results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorTestResult:
    """One standardized Mode 06 test record (ISO 15765-4 / SAE J1979 §6).

    Wire shape per record: ``MID TID UASID value(2) min(2) max(2)``. Scaling
    of value/min/max is governed by ``unit_and_scaling_id`` (UASID); we keep
    the raw 16-bit integers and the UASID so a UI can render or scale them
    without this parser hard-coding the full UASID table.
    """

    monitor_id: int
    test_id: int
    unit_and_scaling_id: int
    test_value: int
    min_limit: int
    max_limit: int

    @property
    def passed(self) -> bool:
        return self.min_limit <= self.test_value <= self.max_limit


def parse_mode06_response(payload: bytes) -> list[MonitorTestResult]:
    """Parse a Mode 06 response payload into monitor test records.

    ``payload`` is the bytes after the ``46`` positive-response service ID.
    Records are a fixed 9 bytes; a trailing short remainder is ignored rather
    than raising (some ECUs pad).
    """
    results: list[MonitorTestResult] = []
    for i in range(0, len(payload) - 8, 9):
        chunk = payload[i : i + 9]
        results.append(
            MonitorTestResult(
                monitor_id=chunk[0],
                test_id=chunk[1],
                unit_and_scaling_id=chunk[2],
                test_value=int.from_bytes(chunk[3:5], "big"),
                min_limit=int.from_bytes(chunk[5:7], "big"),
                max_limit=int.from_bytes(chunk[7:9], "big"),
            )
        )
    return results


#: SAE J1979 Mode 06 per-cylinder misfire MIDs: 0xA2 is cylinder 1 and the
#: range extends through 0xAC for engines with up to 11 cylinders. MID 0xA1
#: is composite/engine-wide data and is deliberately excluded.
MISFIRE_CYLINDER_MID_BASE = 0xA2
MISFIRE_CYLINDER_MID_COUNT = 11
#: TID 0x0C is the current-drive-cycle count. TID 0x0B is the ten-drive-cycle
#: EWMA and remains available in the raw monitor results, but must not be
#: conflated with the live per-session counter.
MISFIRE_CURRENT_CYCLE_TID = 0x0C


def misfire_cylinder_number(monitor_id: int) -> int | None:
    """1-based cylinder number for a per-cylinder misfire-counter MID.

    ``None`` for any MID outside the reserved per-cylinder misfire range
    (composite data, other monitors like catalyst/EVAP/O2, etc).
    """
    if (
        MISFIRE_CYLINDER_MID_BASE
        <= monitor_id
        < MISFIRE_CYLINDER_MID_BASE + MISFIRE_CYLINDER_MID_COUNT
    ):
        return monitor_id - MISFIRE_CYLINDER_MID_BASE + 1
    return None


#: Telemetry channel prefix for the per-cylinder numeric misfire-count
#: readings the collector derives from a Mode 06 sweep (Task 3.1/3.2) — one
#: reading per cylinder present, channel ``f"{MISFIRE_COUNT_CHANNEL_PREFIX}{n}"``
#: (1-based), landing in storage as ``obd:MISFIRE_COUNT_CYLn`` so the session
#: fingerprint (Task 3.2) can pick them up the same way it scans
#: ``deviation:*`` columns — no hardcoded cylinder count on either end.
MISFIRE_COUNT_CHANNEL_PREFIX = "MISFIRE_COUNT_CYL"


def misfire_count_channel(cylinder: int) -> str:
    """Telemetry channel name for one cylinder's misfire-count reading."""
    return f"{MISFIRE_COUNT_CHANNEL_PREFIX}{cylinder}"


def misfire_counts_from_results(results: Sequence[MonitorTestResult]) -> dict[int, int]:
    """Per-cylinder misfire counts (cylinder number -> raw test value).

    Non-misfire MIDs (catalyst, EVAP, O2 sensor, …) are silently dropped —
    callers that need the full monitor list still have ``results`` itself.
    """
    counts: dict[int, int] = {}
    for result in results:
        cylinder = misfire_cylinder_number(result.monitor_id)
        if cylinder is not None and result.test_id == MISFIRE_CURRENT_CYCLE_TID:
            counts[cylinder] = result.test_value
    return counts


# ---------------------------------------------------------------------------
# Mode 01 registry
# ---------------------------------------------------------------------------

#: Mode 01 supported-PID bitmask probe PIDs. Each is a 32-bit mask of the next
#: 32 PIDs; not telemetry themselves.
SUPPORT_GROUP_PIDS: tuple[int, ...] = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0)


def mode01_support_group(pid: int) -> int:
    """Which supported-PID bitmask group ``pid`` is reported in.

    PID 0x05 → 0x00 (the "PIDs 01–20 supported" mask), 0x42 → 0x40, etc.
    """
    for base in reversed(SUPPORT_GROUP_PIDS):
        if pid > base:
            return base
    return 0x00


@dataclass(frozen=True, kw_only=True)
class _Row:
    """One Mode 01 PID as written in :data:`_MODE01_SPEC`.

    ``decoder=None`` means supported-but-not-decoded. ``polled`` puts the PID
    in :data:`PID_DECODERS`, the table the streaming collector polls: that
    table is derived from these rows, so a polled PID's channel name and
    decoder cannot differ between the two (R23.14 replaced the import-time
    ``assert_pid_tables_consistent`` with this construction).
    """

    pid: int
    channel: str
    display_name: str
    category: Category
    unit: str
    #: Expected service-payload length in bytes.
    byte_length: int
    poll_tier: PollTier
    dashboard_hint: DashboardHint
    decoder: PidDecoder | None
    polled: bool = False


_MODE01_SPEC: tuple[_Row, ...] = (
    _Row(
        pid=0x01,
        channel="MONITOR_STATUS",
        display_name="Monitor Status / DTC Count",
        category="status",
        unit="count",
        byte_length=4,
        poll_tier="slow",
        dashboard_hint="indicator",
        decoder=decode_monitor_status,
        polled=True,
    ),
    _Row(
        pid=0x03,
        channel="FUEL_STATUS",
        display_name="Fuel System Status",
        category="fuel",
        unit="bitmask",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="indicator",
        decoder=decode_fuel_status,
        polled=True,
    ),
    _Row(
        pid=0x04,
        channel="ENGINE_LOAD",
        display_name="Calculated Engine Load",
        category="engine",
        unit="%",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="gauge",
        decoder=decode_percent_a_over_255,
        polled=True,
    ),
    _Row(
        pid=0x05,
        channel="COOLANT_TEMP",
        display_name="Engine Coolant Temp",
        category="temperature",
        unit="degC",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="gauge",
        decoder=decode_temp_minus_40,
        polled=True,
    ),
    _Row(
        pid=0x06,
        channel="SHORT_FUEL_TRIM_1",
        display_name="Short Fuel Trim B1",
        category="fuel",
        unit="%",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_percent_b_minus_128,
        polled=True,
    ),
    _Row(
        pid=0x07,
        channel="LONG_FUEL_TRIM_1",
        display_name="Long Fuel Trim B1",
        category="fuel",
        unit="%",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_percent_b_minus_128,
        polled=True,
    ),
    _Row(
        pid=0x08,
        channel="SHORT_FUEL_TRIM_2",
        display_name="Short Fuel Trim B2",
        category="fuel",
        unit="%",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_percent_b_minus_128,
        polled=True,
    ),
    _Row(
        pid=0x09,
        channel="LONG_FUEL_TRIM_2",
        display_name="Long Fuel Trim B2",
        category="fuel",
        unit="%",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_percent_b_minus_128,
        polled=True,
    ),
    _Row(
        pid=0x0A,
        channel="FUEL_PRESSURE",
        display_name="Fuel Pressure",
        category="pressure",
        unit="kPa",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="gauge",
        decoder=decode_fuel_pressure,
    ),
    _Row(
        pid=0x0B,
        channel="INTAKE_PRESSURE",
        display_name="Intake Manifold Pressure",
        category="pressure",
        unit="kPa",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="gauge",
        decoder=decode_intake_pressure,
        polled=True,
    ),
    _Row(
        pid=0x0C,
        channel="RPM",
        display_name="Engine RPM",
        category="engine",
        unit="rpm",
        byte_length=2,
        poll_tier="fast",
        dashboard_hint="gauge",
        decoder=decode_rpm,
        polled=True,
    ),
    _Row(
        pid=0x0D,
        channel="SPEED",
        display_name="Vehicle Speed",
        category="speed",
        unit="kph",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="gauge",
        decoder=decode_speed,
        polled=True,
    ),
    _Row(
        pid=0x0E,
        channel="TIMING_ADVANCE",
        display_name="Timing Advance",
        category="engine",
        unit="degBTDC",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_timing_advance,
        polled=True,
    ),
    _Row(
        pid=0x0F,
        channel="INTAKE_TEMP",
        display_name="Intake Air Temp",
        category="temperature",
        unit="degC",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="gauge",
        decoder=decode_temp_minus_40,
        polled=True,
    ),
    _Row(
        pid=0x10,
        channel="MAF",
        display_name="Mass Air Flow",
        category="air",
        unit="g/s",
        byte_length=2,
        poll_tier="fast",
        dashboard_hint="gauge",
        decoder=decode_maf,
        polled=True,
    ),
    _Row(
        pid=0x11,
        channel="THROTTLE_POS",
        display_name="Throttle Position",
        category="engine",
        unit="%",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="gauge",
        decoder=decode_percent_a_over_255,
        polled=True,
    ),
    _Row(
        pid=0x12,
        channel="SECONDARY_AIR_STATUS",
        display_name="Commanded Secondary Air",
        category="emissions",
        unit="bitmask",
        byte_length=1,
        poll_tier="slow",
        dashboard_hint="indicator",
        decoder=decode_bitmask_a,
    ),
    _Row(
        pid=0x13,
        channel="O2_SENSORS_PRESENT_2B",
        display_name="O2 Sensors Present (2 banks)",
        category="o2",
        unit="bitmask",
        byte_length=1,
        poll_tier="slow",
        dashboard_hint="indicator",
        decoder=decode_bitmask_a,
    ),
    _Row(
        pid=0x14,
        channel="O2_B1S1",
        display_name="O2 Sensor B1S1",
        category="o2",
        unit="V",
        byte_length=2,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_o2_voltage,
        polled=True,
    ),
    _Row(
        pid=0x15,
        channel="O2_B1S2",
        display_name="O2 Sensor B1S2",
        category="o2",
        unit="V",
        byte_length=2,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_o2_voltage,
        polled=True,
    ),
    _Row(
        pid=0x16,
        channel="O2_B1S3",
        display_name="O2 Sensor B1S3",
        category="o2",
        unit="V",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_o2_voltage,
    ),
    _Row(
        pid=0x17,
        channel="O2_B1S4",
        display_name="O2 Sensor B1S4",
        category="o2",
        unit="V",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_o2_voltage,
    ),
    _Row(
        pid=0x18,
        channel="O2_B2S1",
        display_name="O2 Sensor B2S1",
        category="o2",
        unit="V",
        byte_length=2,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_o2_voltage,
        polled=True,
    ),
    _Row(
        pid=0x19,
        channel="O2_B2S2",
        display_name="O2 Sensor B2S2",
        category="o2",
        unit="V",
        byte_length=2,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_o2_voltage,
        polled=True,
    ),
    _Row(
        pid=0x1A,
        channel="O2_B2S3",
        display_name="O2 Sensor B2S3",
        category="o2",
        unit="V",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_o2_voltage,
    ),
    _Row(
        pid=0x1B,
        channel="O2_B2S4",
        display_name="O2 Sensor B2S4",
        category="o2",
        unit="V",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_o2_voltage,
    ),
    _Row(
        pid=0x1C,
        channel="OBD_STANDARD",
        display_name="OBD Standard Conformance",
        category="info",
        unit="enum",
        byte_length=1,
        poll_tier="slow",
        dashboard_hint="text",
        decoder=decode_enum,
    ),
    _Row(
        pid=0x1D,
        channel="O2_SENSORS_PRESENT_4B",
        display_name="O2 Sensors Present (4 banks)",
        category="o2",
        unit="bitmask",
        byte_length=1,
        poll_tier="slow",
        dashboard_hint="indicator",
        decoder=decode_bitmask_a,
    ),
    _Row(
        pid=0x1E,
        channel="AUX_INPUT_STATUS",
        display_name="Auxiliary Input Status",
        category="status",
        unit="bitmask",
        byte_length=1,
        poll_tier="slow",
        dashboard_hint="indicator",
        decoder=decode_bitmask_a,
    ),
    _Row(
        pid=0x1F,
        channel="RUN_TIME",
        display_name="Engine Run Time",
        category="engine",
        unit="s",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="text",
        decoder=decode_run_time,
        polled=True,
    ),
    _Row(
        pid=0x21,
        channel="DISTANCE_W_MIL",
        display_name="Distance with MIL On",
        category="status",
        unit="km",
        byte_length=2,
        poll_tier="slow",
        dashboard_hint="text",
        decoder=decode_distance_km,
        polled=True,
    ),
    _Row(
        pid=0x22,
        channel="FUEL_RAIL_PRESSURE_VAC",
        display_name="Fuel Rail Pressure (vac ref)",
        category="pressure",
        unit="kPa",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="gauge",
        decoder=decode_fuel_rail_pressure_vac,
    ),
    _Row(
        pid=0x23,
        channel="FUEL_RAIL_GAUGE_PRESSURE",
        display_name="Fuel Rail Gauge Pressure",
        category="pressure",
        unit="kPa",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="gauge",
        decoder=decode_fuel_rail_gauge_pressure,
    ),
    # Wide-range O2 λ, bank-1 sensor 1/2 (Task 2.1): 0x24/0x25 are the
    # "equivalence ratio" variant, 0x34/0x35 the "current" variant — probe
    # which the ECM answers; the ECM claiming support for one over the
    # other is normal, so both are decoded identically (decode_wide_lambda
    # ignores the raw voltage/current bytes either way).
    _Row(
        pid=0x24,
        channel="O2_WR_LAMBDA_S1",
        display_name="Wide-Range O2 λ S1",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
        polled=True,
    ),
    _Row(
        pid=0x25,
        channel="O2_WR_LAMBDA_S2",
        display_name="Wide-Range O2 λ S2",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
        polled=True,
    ),
    _Row(
        pid=0x26,
        channel="O2_WR_LAMBDA_S3",
        display_name="Wide-Range O2 λ S3",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
    ),
    _Row(
        pid=0x27,
        channel="O2_WR_LAMBDA_S4",
        display_name="Wide-Range O2 λ S4",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
    ),
    # Sensor 5: bank 2 sensor 1 under the PID 0x13 O2 layout, bank 3 sensor 1
    # under 0x1D. Which bank it is belongs to the vehicle
    # (``WIDEBAND_LAMBDA`` in src.signals.vehicle_signals). Polled since
    # D03.34 (2026-09-26); fast, like S1 (0x24): an upstream wide-band sensor
    # under either O2 layout.
    _Row(
        pid=0x28,
        channel="O2_WR_LAMBDA_S5",
        display_name="Wide-Range O2 λ S5",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
        polled=True,
    ),
    _Row(
        pid=0x29,
        channel="O2_WR_LAMBDA_S6",
        display_name="Wide-Range O2 λ S6",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
    ),
    _Row(
        pid=0x2A,
        channel="O2_WR_LAMBDA_S7",
        display_name="Wide-Range O2 λ S7",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
    ),
    _Row(
        pid=0x2B,
        channel="O2_WR_LAMBDA_S8",
        display_name="Wide-Range O2 λ S8",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
    ),
    _Row(
        pid=0x2C,
        channel="COMMANDED_EGR",
        display_name="Commanded EGR",
        category="emissions",
        unit="%",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_egr_percent,
        polled=True,
    ),
    _Row(
        pid=0x2D,
        channel="EGR_ERROR",
        display_name="EGR Error",
        category="emissions",
        unit="%",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_egr_error,
        polled=True,
    ),
    _Row(
        pid=0x2E,
        channel="EVAPORATIVE_PURGE",
        display_name="Commanded Evap Purge",
        category="emissions",
        unit="%",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_evap_purge,
        polled=True,
    ),
    _Row(
        pid=0x2F,
        channel="FUEL_LEVEL",
        display_name="Fuel Tank Level",
        category="fuel",
        unit="%",
        byte_length=1,
        poll_tier="slow",
        dashboard_hint="gauge",
        decoder=decode_percent_a_over_255,
        polled=True,
    ),
    _Row(
        pid=0x30,
        channel="WARMUPS_SINCE_DTC_CLEAR",
        display_name="Warm-ups Since Codes Cleared",
        category="status",
        unit="count",
        byte_length=1,
        poll_tier="slow",
        dashboard_hint="text",
        decoder=decode_warmups,
        polled=True,
    ),
    _Row(
        pid=0x31,
        channel="DISTANCE_SINCE_DTC_CLEAR",
        display_name="Distance Since Codes Cleared",
        category="status",
        unit="km",
        byte_length=2,
        poll_tier="slow",
        dashboard_hint="text",
        decoder=decode_distance_km,
        polled=True,
    ),
    _Row(
        pid=0x32,
        channel="EVAP_VAPOR_PRESSURE",
        display_name="Evap System Vapor Pressure",
        category="pressure",
        unit="Pa",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_evap_vapor_pressure_signed,
        polled=True,
    ),
    _Row(
        pid=0x33,
        channel="BAROMETRIC_PRESSURE",
        display_name="Barometric Pressure",
        category="pressure",
        unit="kPa",
        byte_length=1,  # J1979: A = kPa. Was 2 — desynced every batch containing it.
        poll_tier="slow",
        dashboard_hint="gauge",
        decoder=decode_baro,
        polled=True,
    ),
    _Row(
        pid=0x34,
        channel="O2_WR_CURRENT_S1",
        display_name="Wide-Range O2 λ (current) S1",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
        polled=True,
    ),
    _Row(
        pid=0x35,
        channel="O2_WR_CURRENT_S2",
        display_name="Wide-Range O2 λ (current) S2",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
        polled=True,
    ),
    _Row(
        pid=0x36,
        channel="O2_WR_CURRENT_S3",
        display_name="Wide-Range O2 λ (current) S3",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
    ),
    _Row(
        pid=0x37,
        channel="O2_WR_CURRENT_S4",
        display_name="Wide-Range O2 λ (current) S4",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
    ),
    _Row(
        pid=0x38,
        channel="O2_WR_CURRENT_S5",
        display_name="Wide-Range O2 λ (current) S5",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
    ),
    _Row(
        pid=0x39,
        channel="O2_WR_CURRENT_S6",
        display_name="Wide-Range O2 λ (current) S6",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
    ),
    _Row(
        pid=0x3A,
        channel="O2_WR_CURRENT_S7",
        display_name="Wide-Range O2 λ (current) S7",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
    ),
    _Row(
        pid=0x3B,
        channel="O2_WR_CURRENT_S8",
        display_name="Wide-Range O2 λ (current) S8",
        category="o2",
        unit="ratio",
        byte_length=4,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_wide_lambda,
    ),
    _Row(
        pid=0x3C,
        channel="CATALYST_TEMP_B1S1",
        display_name="Catalyst Temp B1S1",
        category="temperature",
        unit="degC",
        byte_length=2,
        poll_tier="slow",
        dashboard_hint="graph",
        decoder=decode_catalyst_temp,
        polled=True,
    ),
    _Row(
        pid=0x3D,
        channel="CATALYST_TEMP_B2S1",
        display_name="Catalyst Temp B2S1",
        category="temperature",
        unit="degC",
        byte_length=2,
        poll_tier="slow",
        dashboard_hint="graph",
        decoder=decode_catalyst_temp,
        polled=True,
    ),
    _Row(
        pid=0x3E,
        channel="CATALYST_TEMP_B1S2",
        display_name="Catalyst Temp B1S2",
        category="temperature",
        unit="degC",
        byte_length=2,
        poll_tier="slow",
        dashboard_hint="graph",
        decoder=decode_catalyst_temp,
        polled=True,
    ),
    _Row(
        pid=0x3F,
        channel="CATALYST_TEMP_B2S2",
        display_name="Catalyst Temp B2S2",
        category="temperature",
        unit="degC",
        byte_length=2,
        poll_tier="slow",
        dashboard_hint="graph",
        decoder=decode_catalyst_temp,
        polled=True,
    ),
    _Row(
        pid=0x41,
        channel="MONITOR_STATUS_DRIVE_CYCLE",
        display_name="Monitor Status This Drive Cycle",
        category="status",
        unit="bitmask",
        byte_length=4,
        poll_tier="slow",
        dashboard_hint="indicator",
        decoder=None,
    ),
    _Row(
        pid=0x42,
        channel="CONTROL_MODULE_VOLTAGE",
        display_name="Control Module Voltage",
        category="electrical",
        unit="V",
        byte_length=2,
        poll_tier="fast",
        dashboard_hint="gauge",
        decoder=decode_control_module_voltage,
        polled=True,
    ),
    _Row(
        pid=0x43,
        channel="ABSOLUTE_LOAD",
        display_name="Absolute Load Value",
        category="engine",
        unit="%",
        byte_length=2,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_absolute_load,
        polled=True,
    ),
    _Row(
        pid=0x44,
        channel="COMMANDED_EQUIV_RATIO",
        display_name="Commanded Equivalence Ratio",
        category="fuel",
        unit="ratio",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_commanded_lambda,
        polled=True,
    ),
    _Row(
        pid=0x45,
        channel="RELATIVE_THROTTLE_POS",
        display_name="Relative Throttle Position",
        category="engine",
        unit="%",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_percent_a_over_255,
        polled=True,
    ),
    _Row(
        pid=0x46,
        channel="AMBIANT_AIR_TEMP",
        display_name="Ambient Air Temp",
        category="temperature",
        unit="degC",
        byte_length=1,
        poll_tier="slow",
        dashboard_hint="gauge",
        decoder=decode_temp_minus_40,
        polled=True,
    ),
    # Throttle/pedal family (Task 2.2): one-time probe on the Xterra's real
    # ECM (both KOEO and engine-running) confirmed it answers 0x47/0x49/0x4A/
    # 0x4C but not 0x48/0x4B — see data/nissan-xterra-2008/pid-probe-2026-07-22.md.
    _Row(
        pid=0x47,
        channel="ABS_THROTTLE_POS_B",
        display_name="Absolute Throttle Position B",
        category="engine",
        unit="%",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_percent_a_over_255,
        polled=True,
    ),
    _Row(
        pid=0x48,
        channel="ABS_THROTTLE_POS_C",
        display_name="Absolute Throttle Position C",
        category="engine",
        unit="%",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_percent_a_over_255,
    ),
    _Row(
        pid=0x49,
        channel="ACCEL_PEDAL_POS_D",
        display_name="Accelerator Pedal Position D",
        category="engine",
        unit="%",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_percent_a_over_255,
        polled=True,
    ),
    _Row(
        pid=0x4A,
        channel="ACCEL_PEDAL_POS_E",
        display_name="Accelerator Pedal Position E",
        category="engine",
        unit="%",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_percent_a_over_255,
        polled=True,
    ),
    _Row(
        pid=0x4B,
        channel="ACCEL_PEDAL_POS_F",
        display_name="Accelerator Pedal Position F",
        category="engine",
        unit="%",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_percent_a_over_255,
    ),
    _Row(
        pid=0x4C,
        channel="COMMANDED_THROTTLE_ACTUATOR",
        display_name="Commanded Throttle Actuator",
        category="engine",
        unit="%",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_percent_a_over_255,
        polled=True,
    ),
    _Row(
        pid=0x4D,
        channel="TIME_RUN_WITH_MIL",
        display_name="Time Run with MIL On",
        category="status",
        unit="min",
        byte_length=2,
        poll_tier="slow",
        dashboard_hint="text",
        decoder=decode_minutes,
    ),
    _Row(
        pid=0x4E,
        channel="TIME_SINCE_DTC_CLEAR",
        display_name="Time Since Codes Cleared",
        category="status",
        unit="min",
        byte_length=2,
        poll_tier="slow",
        dashboard_hint="text",
        decoder=decode_minutes,
    ),
    _Row(
        pid=0x51,
        channel="FUEL_TYPE",
        display_name="Fuel Type",
        category="info",
        unit="enum",
        byte_length=1,
        poll_tier="slow",
        dashboard_hint="text",
        decoder=decode_enum,
    ),
    _Row(
        pid=0x52,
        channel="ETHANOL_PERCENT",
        display_name="Ethanol Fuel %",
        category="fuel",
        unit="%",
        byte_length=1,
        poll_tier="slow",
        dashboard_hint="gauge",
        decoder=decode_percent_a_over_255,
    ),
    _Row(
        pid=0x53,
        channel="ABS_EVAP_VAPOR_PRESSURE",
        display_name="Absolute Evap Vapor Pressure",
        category="pressure",
        unit="kPa",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_abs_evap_pressure,
    ),
    _Row(
        pid=0x54,
        channel="EVAP_VAPOR_PRESSURE_ABS",
        display_name="Evap System Vapor Pressure",
        category="pressure",
        unit="Pa",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_evap_pressure_pa,
    ),
    _Row(
        pid=0x55,
        channel="SHORT_O2_TRIM_B1",
        display_name="Short-Term Sec O2 Trim B1",
        category="o2",
        unit="%",
        byte_length=2,  # J1979: A = this bank, B = bank 3 (unused; decoder reads A)
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_percent_b_minus_128,
    ),
    _Row(
        pid=0x56,
        channel="LONG_O2_TRIM_B1",
        display_name="Long-Term Sec O2 Trim B1",
        category="o2",
        unit="%",
        byte_length=2,  # J1979: A = this bank, B = bank 3 (unused; decoder reads A)
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_percent_b_minus_128,
    ),
    _Row(
        pid=0x57,
        channel="SHORT_O2_TRIM_B2",
        display_name="Short-Term Sec O2 Trim B2",
        category="o2",
        unit="%",
        byte_length=2,  # J1979: A = this bank, B = bank 4 (unused; decoder reads A)
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_percent_b_minus_128,
    ),
    _Row(
        pid=0x58,
        channel="LONG_O2_TRIM_B2",
        display_name="Long-Term Sec O2 Trim B2",
        category="o2",
        unit="%",
        byte_length=2,  # J1979: A = this bank, B = bank 4 (unused; decoder reads A)
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_percent_b_minus_128,
    ),
    _Row(
        pid=0x59,
        channel="FUEL_RAIL_ABS_PRESSURE",
        display_name="Fuel Rail Absolute Pressure",
        category="pressure",
        unit="kPa",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="gauge",
        decoder=decode_fuel_rail_gauge_pressure,
    ),
    _Row(
        pid=0x5A,
        channel="RELATIVE_ACCEL_PEDAL_POS",
        display_name="Relative Accelerator Pedal",
        category="engine",
        unit="%",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_percent_a_over_255,
    ),
    _Row(
        pid=0x5B,
        channel="HYBRID_BATTERY_LIFE",
        display_name="Hybrid Battery Pack Life",
        category="electrical",
        unit="%",
        byte_length=1,
        poll_tier="slow",
        dashboard_hint="gauge",
        decoder=decode_percent_a_over_255,
    ),
    _Row(
        pid=0x5C,
        channel="OIL_TEMP",
        display_name="Engine Oil Temp",
        category="temperature",
        unit="degC",
        byte_length=1,
        poll_tier="medium",
        dashboard_hint="gauge",
        decoder=decode_temp_minus_40,
        polled=True,
    ),
    _Row(
        pid=0x5D,
        channel="FUEL_INJECTION_TIMING",
        display_name="Fuel Injection Timing",
        category="engine",
        unit="deg",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="graph",
        decoder=decode_injection_timing,
    ),
    _Row(
        pid=0x5E,
        channel="ENGINE_FUEL_RATE",
        display_name="Engine Fuel Rate",
        category="fuel",
        unit="L/h",
        byte_length=2,
        poll_tier="medium",
        dashboard_hint="gauge",
        decoder=decode_fuel_rate,
    ),
    _Row(
        pid=0x61,
        channel="DRIVER_DEMAND_TORQUE",
        display_name="Driver's Demand Torque",
        category="torque",
        unit="%",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_torque_percent,
    ),
    _Row(
        pid=0x62,
        channel="ACTUAL_ENGINE_TORQUE",
        display_name="Actual Engine Torque",
        category="torque",
        unit="%",
        byte_length=1,
        poll_tier="fast",
        dashboard_hint="graph",
        decoder=decode_torque_percent,
    ),
    _Row(
        pid=0x63,
        channel="ENGINE_REFERENCE_TORQUE",
        display_name="Engine Reference Torque",
        category="torque",
        unit="Nm",
        byte_length=2,
        poll_tier="slow",
        dashboard_hint="text",
        decoder=decode_reference_torque,
    ),
)


def _build_mode01() -> dict[int, PidDefinition]:
    table: dict[int, PidDefinition] = {}
    for row in _MODE01_SPEC:
        table[row.pid] = PidDefinition(
            mode=0x01,
            pid=row.pid,
            channel=row.channel,
            display_name=row.display_name,
            category=row.category,
            unit=row.unit,
            byte_length=row.byte_length,
            poll_tier=row.poll_tier,
            dashboard_hint=row.dashboard_hint,
            decoder=row.decoder,
            support_group=mode01_support_group(row.pid),
        )
    return table


MODE01_REGISTRY: dict[int, PidDefinition] = _build_mode01()

#: PID number → (channel name, decoder) for every PID the collector polls.
#: Drives the ``read_supported`` flow and any caller that translates a
#: numeric PID into a named telemetry channel. Derived from the ``polled``
#: rows of :data:`_MODE01_SPEC`, so it is a view of the registry, not a
#: second copy of it.
PID_DECODERS: dict[int, tuple[str, PidDecoder]] = {
    row.pid: (row.channel, row.decoder)
    for row in _MODE01_SPEC
    if row.polled and row.decoder is not None
}


# ---------------------------------------------------------------------------
# Mode 09 registry
# ---------------------------------------------------------------------------

_MODE09_SPEC: tuple[tuple[int, str, str, str, Decoder | None], ...] = (
    (0x02, "VIN", "Vehicle Identification Number", "text", decode_vin),
    (0x04, "CALIBRATION_ID", "Calibration ID", "text", decode_calibration_ids),
    (0x06, "CVN", "Calibration Verification Numbers", "hex", decode_cvn),
    (0x08, "IN_USE_PERFORMANCE", "In-Use Performance Tracking", "list", None),
    (0x0A, "ECU_NAME", "ECU Name", "text", decode_ascii),
    (0x0B, "IN_USE_PERFORMANCE_COMP", "In-Use Performance (compression)", "list", None),
)


def _build_mode09() -> dict[int, PidDefinition]:
    table: dict[int, PidDefinition] = {}
    for info_type, channel, display, unit, decoder in _MODE09_SPEC:
        table[info_type] = PidDefinition(
            mode=0x09,
            pid=info_type,
            channel=channel,
            display_name=display,
            category="info",
            unit=unit,
            byte_length=0,  # variable, multi-frame
            poll_tier="on_demand",
            dashboard_hint="text",
            decoder=decoder,
            support_group=None,
        )
    return table


MODE09_REGISTRY: dict[int, PidDefinition] = _build_mode09()


# ---------------------------------------------------------------------------
# Service registry (Mode 02 / 03 / 06 / 07 / 0A)
# ---------------------------------------------------------------------------

SERVICE_REGISTRY: dict[int, ServiceDefinition] = {
    0x02: ServiceDefinition(
        mode=0x02,
        name="FREEZE_FRAME",
        display_name="Freeze Frame Data",
        description="Mode 01 PID values captured at the moment a DTC set. Same "
        "formulas as Mode 01; the response carries a frame number byte first.",
        on_demand=True,
    ),
    0x03: ServiceDefinition(
        mode=0x03,
        name="STORED_DTC",
        display_name="Stored Diagnostic Trouble Codes",
        description="Confirmed DTCs that turned on the MIL.",
        on_demand=True,
    ),
    0x06: ServiceDefinition(
        mode=0x06,
        name="MONITOR_RESULTS",
        display_name="On-Board Monitor Test Results",
        description="Non-continuously-monitored system test results "
        "(catalyst, EVAP, O2 sensor, …) with value/min/max limits.",
        on_demand=True,
    ),
    0x07: ServiceDefinition(
        mode=0x07,
        name="PENDING_DTC",
        display_name="Pending Diagnostic Trouble Codes",
        description="DTCs detected this/last drive cycle, not yet confirmed.",
        on_demand=True,
    ),
    0x0A: ServiceDefinition(
        mode=0x0A,
        name="PERMANENT_DTC",
        display_name="Permanent Diagnostic Trouble Codes",
        description="DTCs that cannot be cleared by a scan tool; clear only "
        "after the ECU re-verifies the repair.",
        on_demand=True,
    ),
}


# ---------------------------------------------------------------------------
# Lookup + coverage classification
# ---------------------------------------------------------------------------


def lookup(mode: int, pid: int) -> PidDefinition | None:
    """Find a PID definition for ``(mode, pid)``.

    Mode 02 (freeze frame) reuses the Mode 01 table — same formulas, so a
    Mode 02 lookup returns the Mode 01 definition with ``mode`` rewritten.
    """
    if mode in (0x01, 0x02):
        base = MODE01_REGISTRY.get(pid)
        if base is None:
            return None
        if mode == 0x02:
            return PidDefinition(**{**base.__dict__, "mode": 0x02})
        return base
    if mode == 0x09:
        return MODE09_REGISTRY.get(pid)
    return None


def mode01_registry_width(pid: int) -> int | None:
    """The registry's Mode 01 payload width for ``pid``, or ``None`` when the
    PID is unknown or has no fixed width.

    The one copy: the batched-response walk (``CanObdClient``) and the
    width ledger (``obd_scheduler.requests.Mode01WidthLedger``) both slice by
    it, so they cannot disagree about a PID's width.
    """
    definition = lookup(0x01, pid)
    if definition is None or definition.byte_length <= 0:
        return None
    return definition.byte_length


PidCoverageStatus = Literal[
    "decoded",
    "supported_not_decoded",
    "unsupported",
    "unknown_pid",
]


@dataclass(frozen=True)
class PidCoverage:
    """Coverage classification for one ``(mode, pid)`` against an ECU's claim.

    ``status``:
      * ``decoded`` — the ECU supports it and CANary has a decoder.
      * ``supported_not_decoded`` — the ECU supports it but CANary has no
        decoder (a real, honest gap — surfaced, never hidden).
      * ``unsupported`` — the ECU's supported-PID bitmask does not claim it.
      * ``unknown_pid`` — not in the registry at all (likely manufacturer or
        out of the public J1979 range).
    """

    mode: int
    pid: int
    status: PidCoverageStatus
    channel: str | None
    display_name: str | None
    category: Category | None
    unit: str | None


def classify_pid(mode: int, pid: int, *, supported: bool) -> PidCoverage:
    """Classify a PID for the coverage endpoint (Task 6.3)."""
    definition = lookup(mode, pid)
    if definition is None:
        return PidCoverage(
            mode=mode,
            pid=pid,
            status="unknown_pid" if supported else "unsupported",
            channel=None,
            display_name=None,
            category=None,
            unit=None,
        )
    if not supported:
        status: PidCoverageStatus = "unsupported"
    elif definition.decoder is None:
        status = "supported_not_decoded"
    else:
        status = "decoded"
    return PidCoverage(
        mode=mode,
        pid=pid,
        status=status,
        channel=definition.channel,
        display_name=definition.display_name,
        category=definition.category,
        unit=definition.unit,
    )
