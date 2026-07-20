"""Typed SAE J1979 PID / service registry (Task 6.1).

``can_obd_client.PID_DECODERS`` is the minimal "PID number → (name, decoder)"
table the streaming collector polls. This module is the *metadata* layer on top
of it: every public, read-only PID/service CANary can safely request, with the
information the dashboard and coverage views (Task 6.3) need to explain *why* a
supported PID is or isn't a usable telemetry channel.

Design:

* The numeric Mode 01 decoders stay single-sourced in ``can_obd_client`` — this
  module *imports* them, never re-implements them. PIDs whose J1979 formula is
  well-defined but not in ``PID_DECODERS`` get a decoder defined here; PIDs that
  are real and supportable but whose value we deliberately don't decode get
  ``decoder=None`` so they classify as "supported but not decoded" rather than
  vanishing (Risk R7 in the plan).
* Mode 02 (freeze frame) reuses Mode 01 formulas — same ``A B`` bytes after the
  frame-number byte — so it's expressed as a view over the Mode 01 table.
* Mode 03 / 07 / 0A (DTC reads) and Mode 06 (on-board monitor results) and
  Mode 09 (vehicle info) are described as services with their own parsers.

Read-only by construction: every entry is a request the J1979 spec defines as a
read. No Mode 04 / 2E / 2F entry exists here, so a caller that drives the
registry can't synthesize a write.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from src.collection.can_obd_client import (
    PID_DECODERS,
    decode_o2_voltage,
    decode_percent_a_over_255,
    decode_percent_b_minus_128,
    parse_dtc_payload,
)

__all__ = [
    "MODE01_REGISTRY",
    "MODE09_REGISTRY",
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
    "mode01_support_group",
    "parse_dtc_payload",
    "parse_mode06_response",
]

#: ``"dtc"`` is only used by ``obd_scheduler._PollRequest`` (the periodic
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
    """One read-only PID, with everything the dashboard/coverage view needs.

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
# Decoders for well-defined J1979 PIDs not carried by PID_DECODERS.
# Single-source rule: numeric decoders already in can_obd_client are imported,
# never duplicated. These cover formula-stable PIDs it doesn't.
# ---------------------------------------------------------------------------


def _need(data: bytes, n: int) -> bool:
    return len(data) >= n


def decode_fuel_pressure(data: bytes) -> tuple[float, str] | None:
    """PID 0x0A: gauge fuel pressure, ``A * 3`` kPa."""
    if not _need(data, 1):
        return None
    return (float(data[0] * 3), "kPa")


def decode_bitmask_a(data: bytes) -> tuple[float, str] | None:
    """Single-byte status bitmask (secondary-air status, O2-present, …)."""
    if not _need(data, 1):
        return None
    return (float(data[0]), "bitmask")


def decode_obd_standard(data: bytes) -> tuple[float, str] | None:
    """PID 0x1C: which OBD standard the ECU conforms to (enum code)."""
    if not _need(data, 1):
        return None
    return (float(data[0]), "enum")


def decode_fuel_rail_pressure_vac(data: bytes) -> tuple[float, str] | None:
    """PID 0x22: fuel rail pressure rel. to manifold vacuum, ``*0.079`` kPa."""
    if not _need(data, 2):
        return None
    return ((data[0] * 256 + data[1]) * 0.079, "kPa")


def decode_fuel_rail_gauge_pressure(data: bytes) -> tuple[float, str] | None:
    """PID 0x23 / 0x59: fuel rail gauge/absolute pressure, ``*10`` kPa."""
    if not _need(data, 2):
        return None
    return (float((data[0] * 256 + data[1]) * 10), "kPa")


def decode_wide_lambda(data: bytes) -> tuple[float, str] | None:
    """PIDs 0x24-0x2B / 0x34-0x3B: wide-range O2.

    Four bytes: ``AB`` = equivalence ratio λ ``(A*256+B)/32768``, ``CD`` =
    sensor voltage or current. λ is the model-relevant channel; the raw
    voltage/current goes unused.
    """
    if not _need(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 32768.0, "ratio")


def decode_minutes(data: bytes) -> tuple[float, str] | None:
    """PID 0x4D / 0x4E: a time counter in minutes, ``A*256+B``."""
    if not _need(data, 2):
        return None
    return (float(data[0] * 256 + data[1]), "min")


def decode_fuel_type(data: bytes) -> tuple[float, str] | None:
    """PID 0x51: fuel type (enum code; 1 = gasoline, 4 = diesel, …)."""
    if not _need(data, 1):
        return None
    return (float(data[0]), "enum")


def decode_abs_evap_pressure(data: bytes) -> tuple[float, str] | None:
    """PID 0x53: absolute evap system vapor pressure, ``(A*256+B)/200`` kPa."""
    if not _need(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 200.0, "kPa")


def decode_evap_pressure_pa(data: bytes) -> tuple[float, str] | None:
    """PID 0x54: evap system vapor pressure, ``(A*256+B) - 32767`` Pa."""
    if not _need(data, 2):
        return None
    return (float((data[0] * 256 + data[1]) - 32767), "Pa")


def decode_injection_timing(data: bytes) -> tuple[float, str] | None:
    """PID 0x5D: fuel injection timing, ``((A*256+B) - 26880)/128`` deg."""
    if not _need(data, 2):
        return None
    return (((data[0] * 256 + data[1]) - 26880) / 128.0, "deg")


def decode_fuel_rate(data: bytes) -> tuple[float, str] | None:
    """PID 0x5E: engine fuel rate, ``(A*256+B)/20`` L/h."""
    if not _need(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 20.0, "L/h")


def decode_torque_percent(data: bytes) -> tuple[float, str] | None:
    """PID 0x61 / 0x62: driver-demand / actual engine torque, ``A - 125`` %."""
    if not _need(data, 1):
        return None
    return (float(data[0] - 125), "%")


def decode_reference_torque(data: bytes) -> tuple[float, str] | None:
    """PID 0x63: engine reference torque, ``A*256+B`` Nm."""
    if not _need(data, 2):
        return None
    return (float(data[0] * 256 + data[1]), "Nm")


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
    """Mode 09 InfoType 0x06 — calibration verification numbers (4-byte hex)."""
    if not data:
        return None
    # CVNs are not ASCII — render as space-separated 4-byte hex groups.
    groups = [data[i : i + 4].hex().upper() for i in range(0, len(data) - len(data) % 4, 4)]
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


# (pid, channel, display, category, unit, byte_length, poll_tier, hint, decoder)
# decoder None ⇒ supported-but-not-decoded. Decoders already in PID_DECODERS
# are pulled from there so the table can't drift from the collector.
def _pd(pid: int) -> Decoder | None:
    entry = PID_DECODERS.get(pid)
    return entry[1] if entry is not None else None


_MODE01_SPEC: tuple[
    tuple[int, str, str, Category, str, int, PollTier, DashboardHint, Decoder | None], ...
] = (
    (
        0x01,
        "MONITOR_STATUS",
        "Monitor Status / DTC Count",
        "status",
        "count",
        4,
        "slow",
        "indicator",
        _pd(0x01),
    ),  # noqa: E501
    (
        0x03,
        "FUEL_STATUS",
        "Fuel System Status",
        "fuel",
        "bitmask",
        2,
        "medium",
        "indicator",
        _pd(0x03),
    ),  # noqa: E501
    (0x04, "ENGINE_LOAD", "Calculated Engine Load", "engine", "%", 1, "fast", "gauge", _pd(0x04)),
    (
        0x05,
        "COOLANT_TEMP",
        "Engine Coolant Temp",
        "temperature",
        "degC",
        1,
        "medium",
        "gauge",
        _pd(0x05),
    ),  # noqa: E501
    (0x06, "SHORT_FUEL_TRIM_1", "Short Fuel Trim B1", "fuel", "%", 1, "medium", "graph", _pd(0x06)),
    (0x07, "LONG_FUEL_TRIM_1", "Long Fuel Trim B1", "fuel", "%", 1, "medium", "graph", _pd(0x07)),
    (0x08, "SHORT_FUEL_TRIM_2", "Short Fuel Trim B2", "fuel", "%", 1, "medium", "graph", _pd(0x08)),
    (0x09, "LONG_FUEL_TRIM_2", "Long Fuel Trim B2", "fuel", "%", 1, "medium", "graph", _pd(0x09)),
    (
        0x0A,
        "FUEL_PRESSURE",
        "Fuel Pressure",
        "pressure",
        "kPa",
        1,
        "medium",
        "gauge",
        decode_fuel_pressure,
    ),  # noqa: E501
    (
        0x0B,
        "INTAKE_PRESSURE",
        "Intake Manifold Pressure",
        "pressure",
        "kPa",
        1,
        "fast",
        "gauge",
        _pd(0x0B),
    ),  # noqa: E501
    (0x0C, "RPM", "Engine RPM", "engine", "rpm", 2, "fast", "gauge", _pd(0x0C)),
    (0x0D, "SPEED", "Vehicle Speed", "speed", "kph", 1, "fast", "gauge", _pd(0x0D)),
    (0x0E, "TIMING_ADVANCE", "Timing Advance", "engine", "degBTDC", 1, "fast", "graph", _pd(0x0E)),
    (
        0x0F,
        "INTAKE_TEMP",
        "Intake Air Temp",
        "temperature",
        "degC",
        1,
        "medium",
        "gauge",
        _pd(0x0F),
    ),  # noqa: E501
    (0x10, "MAF", "Mass Air Flow", "air", "g/s", 2, "fast", "gauge", _pd(0x10)),
    (0x11, "THROTTLE_POS", "Throttle Position", "engine", "%", 1, "fast", "gauge", _pd(0x11)),
    (
        0x12,
        "SECONDARY_AIR_STATUS",
        "Commanded Secondary Air",
        "emissions",
        "bitmask",
        1,
        "slow",
        "indicator",
        decode_bitmask_a,
    ),  # noqa: E501
    (
        0x13,
        "O2_SENSORS_PRESENT_2B",
        "O2 Sensors Present (2 banks)",
        "o2",
        "bitmask",
        1,
        "slow",
        "indicator",
        decode_bitmask_a,
    ),  # noqa: E501
    (0x14, "O2_B1S1", "O2 Sensor B1S1", "o2", "V", 2, "medium", "graph", _pd(0x14)),
    (0x15, "O2_B1S2", "O2 Sensor B1S2", "o2", "V", 2, "medium", "graph", _pd(0x15)),
    (0x16, "O2_B1S3", "O2 Sensor B1S3", "o2", "V", 2, "medium", "graph", decode_o2_voltage),
    (0x17, "O2_B1S4", "O2 Sensor B1S4", "o2", "V", 2, "medium", "graph", decode_o2_voltage),
    (0x18, "O2_B2S1", "O2 Sensor B2S1", "o2", "V", 2, "medium", "graph", _pd(0x18)),
    (0x19, "O2_B2S2", "O2 Sensor B2S2", "o2", "V", 2, "medium", "graph", _pd(0x19)),
    (0x1A, "O2_B2S3", "O2 Sensor B2S3", "o2", "V", 2, "medium", "graph", decode_o2_voltage),
    (0x1B, "O2_B2S4", "O2 Sensor B2S4", "o2", "V", 2, "medium", "graph", decode_o2_voltage),
    (
        0x1C,
        "OBD_STANDARD",
        "OBD Standard Conformance",
        "info",
        "enum",
        1,
        "slow",
        "text",
        decode_obd_standard,
    ),  # noqa: E501
    (
        0x1D,
        "O2_SENSORS_PRESENT_4B",
        "O2 Sensors Present (4 banks)",
        "o2",
        "bitmask",
        1,
        "slow",
        "indicator",
        decode_bitmask_a,
    ),  # noqa: E501
    (
        0x1E,
        "AUX_INPUT_STATUS",
        "Auxiliary Input Status",
        "status",
        "bitmask",
        1,
        "slow",
        "indicator",
        decode_bitmask_a,
    ),  # noqa: E501
    (0x1F, "RUN_TIME", "Engine Run Time", "engine", "s", 2, "medium", "text", _pd(0x1F)),
    (0x21, "DISTANCE_W_MIL", "Distance with MIL On", "status", "km", 2, "slow", "text", _pd(0x21)),
    (
        0x22,
        "FUEL_RAIL_PRESSURE_VAC",
        "Fuel Rail Pressure (vac ref)",
        "pressure",
        "kPa",
        2,
        "medium",
        "gauge",
        decode_fuel_rail_pressure_vac,
    ),  # noqa: E501
    (
        0x23,
        "FUEL_RAIL_GAUGE_PRESSURE",
        "Fuel Rail Gauge Pressure",
        "pressure",
        "kPa",
        2,
        "medium",
        "gauge",
        decode_fuel_rail_gauge_pressure,
    ),  # noqa: E501
    (
        0x24,
        "O2_WR_LAMBDA_S1",
        "Wide-Range O2 λ S1",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x25,
        "O2_WR_LAMBDA_S2",
        "Wide-Range O2 λ S2",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x26,
        "O2_WR_LAMBDA_S3",
        "Wide-Range O2 λ S3",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x27,
        "O2_WR_LAMBDA_S4",
        "Wide-Range O2 λ S4",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x28,
        "O2_WR_LAMBDA_S5",
        "Wide-Range O2 λ S5",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x29,
        "O2_WR_LAMBDA_S6",
        "Wide-Range O2 λ S6",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x2A,
        "O2_WR_LAMBDA_S7",
        "Wide-Range O2 λ S7",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x2B,
        "O2_WR_LAMBDA_S8",
        "Wide-Range O2 λ S8",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (0x2C, "COMMANDED_EGR", "Commanded EGR", "emissions", "%", 1, "medium", "graph", _pd(0x2C)),
    (0x2D, "EGR_ERROR", "EGR Error", "emissions", "%", 1, "medium", "graph", _pd(0x2D)),
    (
        0x2E,
        "EVAPORATIVE_PURGE",
        "Commanded Evap Purge",
        "emissions",
        "%",
        1,
        "medium",
        "graph",
        _pd(0x2E),
    ),  # noqa: E501
    (0x2F, "FUEL_LEVEL", "Fuel Tank Level", "fuel", "%", 1, "slow", "gauge", _pd(0x2F)),
    (
        0x30,
        "WARMUPS_SINCE_DTC_CLEAR",
        "Warm-ups Since Codes Cleared",
        "status",
        "count",
        1,
        "slow",
        "text",
        _pd(0x30),
    ),  # noqa: E501
    (
        0x31,
        "DISTANCE_SINCE_DTC_CLEAR",
        "Distance Since Codes Cleared",
        "status",
        "km",
        2,
        "slow",
        "text",
        _pd(0x31),
    ),  # noqa: E501
    (
        0x32,
        "EVAP_VAPOR_PRESSURE",
        "Evap System Vapor Pressure",
        "pressure",
        "Pa",
        2,
        "medium",
        "graph",
        _pd(0x32),
    ),  # noqa: E501
    (
        0x33,
        "BAROMETRIC_PRESSURE",
        "Barometric Pressure",
        "pressure",
        "kPa",
        2,
        "slow",
        "gauge",
        _pd(0x33),
    ),  # noqa: E501
    (
        0x34,
        "O2_WR_CURRENT_S1",
        "Wide-Range O2 λ (current) S1",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x35,
        "O2_WR_CURRENT_S2",
        "Wide-Range O2 λ (current) S2",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x36,
        "O2_WR_CURRENT_S3",
        "Wide-Range O2 λ (current) S3",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x37,
        "O2_WR_CURRENT_S4",
        "Wide-Range O2 λ (current) S4",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x38,
        "O2_WR_CURRENT_S5",
        "Wide-Range O2 λ (current) S5",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x39,
        "O2_WR_CURRENT_S6",
        "Wide-Range O2 λ (current) S6",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x3A,
        "O2_WR_CURRENT_S7",
        "Wide-Range O2 λ (current) S7",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x3B,
        "O2_WR_CURRENT_S8",
        "Wide-Range O2 λ (current) S8",
        "o2",
        "ratio",
        4,
        "medium",
        "graph",
        decode_wide_lambda,
    ),  # noqa: E501
    (
        0x3C,
        "CATALYST_TEMP_B1S1",
        "Catalyst Temp B1S1",
        "temperature",
        "degC",
        2,
        "slow",
        "graph",
        _pd(0x3C),
    ),  # noqa: E501
    (
        0x3D,
        "CATALYST_TEMP_B2S1",
        "Catalyst Temp B2S1",
        "temperature",
        "degC",
        2,
        "slow",
        "graph",
        _pd(0x3D),
    ),  # noqa: E501
    (
        0x3E,
        "CATALYST_TEMP_B1S2",
        "Catalyst Temp B1S2",
        "temperature",
        "degC",
        2,
        "slow",
        "graph",
        _pd(0x3E),
    ),  # noqa: E501
    (
        0x3F,
        "CATALYST_TEMP_B2S2",
        "Catalyst Temp B2S2",
        "temperature",
        "degC",
        2,
        "slow",
        "graph",
        _pd(0x3F),
    ),  # noqa: E501
    (
        0x41,
        "MONITOR_STATUS_DRIVE_CYCLE",
        "Monitor Status This Drive Cycle",
        "status",
        "bitmask",
        4,
        "slow",
        "indicator",
        None,
    ),  # noqa: E501
    (
        0x42,
        "CONTROL_MODULE_VOLTAGE",
        "Control Module Voltage",
        "electrical",
        "V",
        2,
        "fast",
        "gauge",
        _pd(0x42),
    ),  # noqa: E501
    (0x43, "ABSOLUTE_LOAD", "Absolute Load Value", "engine", "%", 2, "fast", "graph", _pd(0x43)),
    (
        0x44,
        "COMMANDED_EQUIV_RATIO",
        "Commanded Equivalence Ratio",
        "fuel",
        "ratio",
        2,
        "medium",
        "graph",
        _pd(0x44),
    ),  # noqa: E501
    (
        0x45,
        "RELATIVE_THROTTLE_POS",
        "Relative Throttle Position",
        "engine",
        "%",
        1,
        "fast",
        "graph",
        _pd(0x45),
    ),  # noqa: E501
    (
        0x46,
        "AMBIANT_AIR_TEMP",
        "Ambient Air Temp",
        "temperature",
        "degC",
        1,
        "slow",
        "gauge",
        _pd(0x46),
    ),  # noqa: E501
    (
        0x47,
        "ABS_THROTTLE_POS_B",
        "Absolute Throttle Position B",
        "engine",
        "%",
        1,
        "medium",
        "graph",
        decode_percent_a_over_255,
    ),  # noqa: E501
    (
        0x48,
        "ABS_THROTTLE_POS_C",
        "Absolute Throttle Position C",
        "engine",
        "%",
        1,
        "medium",
        "graph",
        decode_percent_a_over_255,
    ),  # noqa: E501
    (
        0x49,
        "ACCEL_PEDAL_POS_D",
        "Accelerator Pedal Position D",
        "engine",
        "%",
        1,
        "fast",
        "graph",
        decode_percent_a_over_255,
    ),  # noqa: E501
    (
        0x4A,
        "ACCEL_PEDAL_POS_E",
        "Accelerator Pedal Position E",
        "engine",
        "%",
        1,
        "fast",
        "graph",
        decode_percent_a_over_255,
    ),  # noqa: E501
    (
        0x4B,
        "ACCEL_PEDAL_POS_F",
        "Accelerator Pedal Position F",
        "engine",
        "%",
        1,
        "fast",
        "graph",
        decode_percent_a_over_255,
    ),  # noqa: E501
    (
        0x4C,
        "COMMANDED_THROTTLE_ACTUATOR",
        "Commanded Throttle Actuator",
        "engine",
        "%",
        1,
        "fast",
        "graph",
        decode_percent_a_over_255,
    ),  # noqa: E501
    (
        0x4D,
        "TIME_RUN_WITH_MIL",
        "Time Run with MIL On",
        "status",
        "min",
        2,
        "slow",
        "text",
        decode_minutes,
    ),  # noqa: E501
    (
        0x4E,
        "TIME_SINCE_DTC_CLEAR",
        "Time Since Codes Cleared",
        "status",
        "min",
        2,
        "slow",
        "text",
        decode_minutes,
    ),  # noqa: E501
    (0x51, "FUEL_TYPE", "Fuel Type", "info", "enum", 1, "slow", "text", decode_fuel_type),
    (
        0x52,
        "ETHANOL_PERCENT",
        "Ethanol Fuel %",
        "fuel",
        "%",
        1,
        "slow",
        "gauge",
        decode_percent_a_over_255,
    ),  # noqa: E501
    (
        0x53,
        "ABS_EVAP_VAPOR_PRESSURE",
        "Absolute Evap Vapor Pressure",
        "pressure",
        "kPa",
        2,
        "medium",
        "graph",
        decode_abs_evap_pressure,
    ),  # noqa: E501
    (
        0x54,
        "EVAP_VAPOR_PRESSURE_ABS",
        "Evap System Vapor Pressure",
        "pressure",
        "Pa",
        2,
        "medium",
        "graph",
        decode_evap_pressure_pa,
    ),  # noqa: E501
    (
        0x55,
        "SHORT_O2_TRIM_B1",
        "Short-Term Sec O2 Trim B1",
        "o2",
        "%",
        1,
        "medium",
        "graph",
        decode_percent_b_minus_128,
    ),  # noqa: E501
    (
        0x56,
        "LONG_O2_TRIM_B1",
        "Long-Term Sec O2 Trim B1",
        "o2",
        "%",
        1,
        "medium",
        "graph",
        decode_percent_b_minus_128,
    ),  # noqa: E501
    (
        0x57,
        "SHORT_O2_TRIM_B2",
        "Short-Term Sec O2 Trim B2",
        "o2",
        "%",
        1,
        "medium",
        "graph",
        decode_percent_b_minus_128,
    ),  # noqa: E501
    (
        0x58,
        "LONG_O2_TRIM_B2",
        "Long-Term Sec O2 Trim B2",
        "o2",
        "%",
        1,
        "medium",
        "graph",
        decode_percent_b_minus_128,
    ),  # noqa: E501
    (
        0x59,
        "FUEL_RAIL_ABS_PRESSURE",
        "Fuel Rail Absolute Pressure",
        "pressure",
        "kPa",
        2,
        "medium",
        "gauge",
        decode_fuel_rail_gauge_pressure,
    ),  # noqa: E501
    (
        0x5A,
        "RELATIVE_ACCEL_PEDAL_POS",
        "Relative Accelerator Pedal",
        "engine",
        "%",
        1,
        "fast",
        "graph",
        decode_percent_a_over_255,
    ),  # noqa: E501
    (
        0x5B,
        "HYBRID_BATTERY_LIFE",
        "Hybrid Battery Pack Life",
        "electrical",
        "%",
        1,
        "slow",
        "gauge",
        decode_percent_a_over_255,
    ),  # noqa: E501
    (0x5C, "OIL_TEMP", "Engine Oil Temp", "temperature", "degC", 1, "medium", "gauge", _pd(0x5C)),
    (
        0x5D,
        "FUEL_INJECTION_TIMING",
        "Fuel Injection Timing",
        "engine",
        "deg",
        2,
        "medium",
        "graph",
        decode_injection_timing,
    ),  # noqa: E501
    (
        0x5E,
        "ENGINE_FUEL_RATE",
        "Engine Fuel Rate",
        "fuel",
        "L/h",
        2,
        "medium",
        "gauge",
        decode_fuel_rate,
    ),  # noqa: E501
    (
        0x61,
        "DRIVER_DEMAND_TORQUE",
        "Driver's Demand Torque",
        "torque",
        "%",
        1,
        "fast",
        "graph",
        decode_torque_percent,
    ),  # noqa: E501
    (
        0x62,
        "ACTUAL_ENGINE_TORQUE",
        "Actual Engine Torque",
        "torque",
        "%",
        1,
        "fast",
        "graph",
        decode_torque_percent,
    ),  # noqa: E501
    (
        0x63,
        "ENGINE_REFERENCE_TORQUE",
        "Engine Reference Torque",
        "torque",
        "Nm",
        2,
        "slow",
        "text",
        decode_reference_torque,
    ),  # noqa: E501
)


def _build_mode01() -> dict[int, PidDefinition]:
    table: dict[int, PidDefinition] = {}
    for pid, channel, display, category, unit, blen, tier, hint, decoder in _MODE01_SPEC:
        table[pid] = PidDefinition(
            mode=0x01,
            pid=pid,
            channel=channel,
            display_name=display,
            category=category,
            unit=unit,
            byte_length=blen,
            poll_tier=tier,
            dashboard_hint=hint,
            decoder=decoder,
            support_group=mode01_support_group(pid),
        )
    return table


MODE01_REGISTRY: dict[int, PidDefinition] = _build_mode01()


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
