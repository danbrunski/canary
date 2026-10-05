"""SAE J1979 Mode 01 decoders, and the Mode 03/07/0A DTC parser (roadmap R02.13).

Pure functions of a response payload: no python-can, no transport. They lived
inside ``can_obd_client`` beside ``CanObdClient``, which made ``obd_registry``
import the transport client (including the private ``need_bytes``) and forced the
client to take ``byte_length_for`` by injection to dodge the import cycle.
``can_obd_client`` re-exports every name here.

This is a relocation, not a merge: the ELM327 path's decoders
(``obd_collector`` / ``pids``) stay separate by the July decision
(complementary PID coverage), as do ``decode_commanded_lambda`` and
``decode_wide_lambda`` (identical bodies, but the names carry phi vs lambda).
"""

from __future__ import annotations

from collections.abc import Callable

# ---------------------------------------------------------------------------
# PID decoders — SAE J1979 standardized formulas.
# Each takes the response payload (bytes after `41 PID`) and returns
# (value, unit). Decoders match python-obd's output for the same PIDs.
# Where the PID returns a single byte and the formula is "A−40" or
# "A * 100 / 255", we keep it explicit for readability over micro-DRY.
# ---------------------------------------------------------------------------


def need_bytes(data: bytes, n: int) -> bool:
    """Whether ``data`` carries at least ``n`` payload bytes."""
    return len(data) >= n


def decode_monitor_status(data: bytes) -> tuple[float, str] | None:
    """PID 0x01 / 0x41: bit 7 of A = MIL on; bits 0-6 of A = DTC count.

    Returns just the count — numeric and model-friendly for the continuous
    slow-tier telemetry channel (Task 1.2). The MIL bit is decoded
    separately by :func:`decode_mil_on` for the periodic DTC poll (Task
    1.4), not folded into this channel's value.
    """
    if not need_bytes(data, 1):
        return None
    return (float(data[0] & 0x7F), "count")


def decode_mil_on(data: bytes) -> bool | None:
    """PID 0x01 / 0x41, bit 7 of byte A: MIL (check-engine light) on/off.

    Reads the same byte as :func:`decode_monitor_status` but returns the bit
    ``decode_monitor_status`` deliberately drops — used when assembling a
    ``DtcReport`` (Task 1.4), not as a telemetry channel.
    """
    if not need_bytes(data, 1):
        return None
    return bool(data[0] & 0x80)


def decode_rpm(data: bytes) -> tuple[float, str] | None:
    if not need_bytes(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 4.0, "rpm")


def decode_speed(data: bytes) -> tuple[float, str] | None:
    if not need_bytes(data, 1):
        return None
    return (float(data[0]), "kph")


def decode_temp_minus_40(data: bytes) -> tuple[float, str] | None:
    """COOLANT_TEMP / INTAKE_TEMP / AMBIANT_AIR_TEMP / OIL_TEMP."""
    if not need_bytes(data, 1):
        return None
    return (float(data[0] - 40), "degC")


def decode_percent_a_over_255(data: bytes) -> tuple[float, str] | None:
    """THROTTLE_POS / ENGINE_LOAD / RELATIVE_THROTTLE_POS / FUEL_LEVEL / etc."""
    if not need_bytes(data, 1):
        return None
    return (data[0] * 100.0 / 255.0, "%")


def decode_percent_b_minus_128(data: bytes) -> tuple[float, str] | None:
    """SHORT_FUEL_TRIM / LONG_FUEL_TRIM, both banks. Range −100% to +99.2%."""
    if not need_bytes(data, 1):
        return None
    return ((data[0] - 128) * 100.0 / 128.0, "%")


def decode_maf(data: bytes) -> tuple[float, str] | None:
    if not need_bytes(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 100.0, "g/s")


def decode_intake_pressure(data: bytes) -> tuple[float, str] | None:
    if not need_bytes(data, 1):
        return None
    return (float(data[0]), "kPa")


def decode_baro(data: bytes) -> tuple[float, str] | None:
    if not need_bytes(data, 1):
        return None
    return (float(data[0]), "kPa")


#: Raw 0xFF on PID 0x0E is the ECU's "not available" sentinel, not a reading.
#: It decodes to exactly 63.5 degBTDC — the top of the A/2-64 scale — which is
#: not a physically achievable spark advance on a spark-ignition engine (this
#: one idles at ~17 and peaks around 40).
TIMING_ADVANCE_UNAVAILABLE = 0xFF


def decode_timing_advance(data: bytes) -> tuple[float, str] | None:
    """Mode 01 PID 0x0E — spark advance, A/2 - 64.

    Returns None for the 0xFF sentinel. Spark advance is undefined with no
    combustion, and this ECU reports 0xFF whenever the engine is not running:
    measured across four sessions on 2026-07-26, the value was 63.5 in 1012 of
    1012 samples taken at RPM == 0 and in 0 of 683 taken while running. No
    other polled PID behaves this way — ENGINE_LOAD reports a true 0.0,
    CONTROL_MODULE_VOLTAGE a true 11.9 V, INTAKE_TEMP a genuinely heat-soaked
    62 degC — so the sentinel handling belongs here rather than in a shared
    engine-state gate.

    Storing it as a number was actively harmful: `2026-07-22T154954` is 100%
    sentinel (engine never ran) and is in the deployed isolation forest's
    training set, so the model learned a constant. Emitting None drops the
    reading like any other unavailable PID instead of inventing a max-scale
    one, and downstream gap handling already copes with an absent sample.
    """
    if not need_bytes(data, 1):
        return None
    if data[0] == TIMING_ADVANCE_UNAVAILABLE:
        return None
    return (data[0] / 2.0 - 64.0, "degBTDC")


def decode_o2_voltage(data: bytes) -> tuple[float, str] | None:
    """Narrowband O2 voltage; byte B is short-term fuel trim if present."""
    if not need_bytes(data, 1):
        return None
    return (data[0] / 200.0, "V")


def decode_control_module_voltage(data: bytes) -> tuple[float, str] | None:
    if not need_bytes(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 1000.0, "V")


def decode_run_time(data: bytes) -> tuple[float, str] | None:
    if not need_bytes(data, 2):
        return None
    return (float(data[0] * 256 + data[1]), "s")


def decode_distance_km(data: bytes) -> tuple[float, str] | None:
    if not need_bytes(data, 2):
        return None
    return (float(data[0] * 256 + data[1]), "km")


def decode_warmups(data: bytes) -> tuple[float, str] | None:
    if not need_bytes(data, 1):
        return None
    return (float(data[0]), "count")


def decode_commanded_lambda(data: bytes) -> tuple[float, str] | None:
    """COMMANDED_EQUIV_RATIO: raw = (A*256+B) / 32768. 1.0 is stoichiometric.

    **This value is returned as J1979 sends it and is NOT necessarily λ.**
    Measured on the 2008 Nissan ECM 2026-08-05: at wide-open throttle this
    reads 1.109-1.242, and λ would make that a commanded 16.3-18.3 AFR, which
    no ECU commands under power. Read as φ (fuel/air), `14.7 / value` gives
    11.83-13.25 — which matches the ROM fuel-target curve to **-0.4 %** across
    four rpm bands. So this ECM reports **φ**, not λ.

    φ and λ are reciprocals and both equal 1.0 at stoich, so the two readings
    are indistinguishable in closed loop — the convention only reveals itself
    under enrichment. Any consumer converting this to AFR on a vehicle whose
    convention has not been checked at WOT is guessing.

    The convention is not the whole story either. That ECM's firmware
    (2026-10-05) computes the PID as φ × (A/F feedback target ÷ 14.7), so it
    is φ only while the feedback target is 14.7: idle reads of 0.871 had
    injector pulses matching φ = 1.0. Where a vehicle exposes the φ word
    itself, read that instead (on the 2008 Xterra,
    ``mem:ECM_TARGET_EQUIV_RATIO``; its notes in ``data/dids.yaml`` give the
    relation).

    Left as the raw ratio deliberately: flipping it here would silently change
    the meaning of every stored `COMMANDED_EQUIV_RATIO` row and break
    comparability with the existing corpus. Convert at the point of use, and
    verify the convention per vehicle.
    """
    if not need_bytes(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 32768.0, "ratio")


def decode_wide_lambda(data: bytes) -> tuple[float, str] | None:
    """PIDs 0x24-0x2B / 0x34-0x3B: wide-range O2 (Task 2.1).

    Four bytes: ``AB`` = equivalence ratio λ ``(A*256+B)/32768``, ``CD`` =
    sensor voltage or current. λ is the model-relevant channel; the raw
    voltage/current goes unused. Single-sourced here (not ``obd_registry.py``)
    because sensors 1/2 (0x24/0x25/0x34/0x35) and sensor 5 (0x28) are polled
    directly via ``PID_DECODERS``; the remaining wide-lambda PIDs still import this
    from here for their unpolled-but-decodable registry entries.
    """
    if not need_bytes(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 32768.0, "ratio")


def decode_fuel_status(data: bytes) -> tuple[float, str] | None:
    """PID 0x03 fuel-system status.

    SAE J1979 returns two bitmask bytes. The first byte carries the primary
    fuel-system state for vehicles with one bank; preserving it as a numeric
    bitmask keeps the channel model-friendly while avoiding a vehicle-specific
    enum interpretation in the collector.
    """
    if not need_bytes(data, 1):
        return None
    return (float(data[0]), "bitmask")


def decode_evap_vapor_pressure_signed(data: bytes) -> tuple[float, str] | None:
    """PID 0x32: signed 16-bit / 4 Pa. Negative = vacuum."""
    if not need_bytes(data, 2):
        return None
    raw = int.from_bytes(data[:2], byteorder="big", signed=True)
    return (raw / 4.0, "Pa")


def decode_egr_percent(data: bytes) -> tuple[float, str] | None:
    """COMMANDED_EGR: A * 100/255."""
    return decode_percent_a_over_255(data)


def decode_egr_error(data: bytes) -> tuple[float, str] | None:
    """EGR_ERROR: (A − 128) * 100/128. Range −100%..+99.2%."""
    return decode_percent_b_minus_128(data)


def decode_evap_purge(data: bytes) -> tuple[float, str] | None:
    return decode_percent_a_over_255(data)


def decode_catalyst_temp(data: bytes) -> tuple[float, str] | None:
    """CATALYST_TEMP_*: ((A*256+B) / 10) − 40. Often unsupported on US-spec
    light vehicles even when claimed."""
    if not need_bytes(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 10.0 - 40.0, "degC")


def decode_absolute_load(data: bytes) -> tuple[float, str] | None:
    """ABSOLUTE_LOAD (PID 0x43): 2-byte percent, ``(A*256+B) * 100/255``.

    Task 2.3: SAE J1979 defines this PID as 2-byte (unlike the 1-byte
    ``decode_percent_a_over_255`` family) so it can read past 100% — real
    under boost/load on many engines. The old 1-byte decode silently
    truncated/wrapped above that.
    """
    if not need_bytes(data, 2):
        return None
    return ((data[0] * 256 + data[1]) * 100.0 / 255.0, "%")


#: Registry: PID number → (name, decoder). Drives both the
#: ``read_supported`` flow and any caller that wants to translate a numeric
#: PID into a named telemetry channel.
PID_DECODERS: dict[int, tuple[str, Callable[[bytes], tuple[float, str] | None]]] = {
    0x01: ("MONITOR_STATUS", decode_monitor_status),
    0x03: ("FUEL_STATUS", decode_fuel_status),
    0x04: ("ENGINE_LOAD", decode_percent_a_over_255),
    0x05: ("COOLANT_TEMP", decode_temp_minus_40),
    0x06: ("SHORT_FUEL_TRIM_1", decode_percent_b_minus_128),
    0x07: ("LONG_FUEL_TRIM_1", decode_percent_b_minus_128),
    0x08: ("SHORT_FUEL_TRIM_2", decode_percent_b_minus_128),
    0x09: ("LONG_FUEL_TRIM_2", decode_percent_b_minus_128),
    0x0B: ("INTAKE_PRESSURE", decode_intake_pressure),
    0x0C: ("RPM", decode_rpm),
    0x0D: ("SPEED", decode_speed),
    0x0E: ("TIMING_ADVANCE", decode_timing_advance),
    0x0F: ("INTAKE_TEMP", decode_temp_minus_40),
    0x10: ("MAF", decode_maf),
    0x11: ("THROTTLE_POS", decode_percent_a_over_255),
    0x14: ("O2_B1S1", decode_o2_voltage),
    0x15: ("O2_B1S2", decode_o2_voltage),
    0x18: ("O2_B2S1", decode_o2_voltage),
    0x19: ("O2_B2S2", decode_o2_voltage),
    0x1F: ("RUN_TIME", decode_run_time),
    0x21: ("DISTANCE_W_MIL", decode_distance_km),
    # Wide-range O2 λ, bank-1 sensor 1/2 (Task 2.1): 0x24/0x25 are the
    # "equivalence ratio" variant, 0x34/0x35 the "current" variant — probe
    # which the ECM answers; the ECM claiming support for one over the
    # other is normal, so both are decoded identically (decode_wide_lambda
    # ignores the raw voltage/current bytes either way).
    0x24: ("O2_WR_LAMBDA_S1", decode_wide_lambda),
    0x25: ("O2_WR_LAMBDA_S2", decode_wide_lambda),
    # Sensor 5: bank 2 sensor 1 under the PID 0x13 O2 layout, bank 3 sensor 1
    # under 0x1D. Which bank it is belongs to the vehicle
    # (``WIDEBAND_LAMBDA`` in src.signals.vehicle_signals). Polled since
    # D03.34 (2026-09-26).
    0x28: ("O2_WR_LAMBDA_S5", decode_wide_lambda),
    0x2C: ("COMMANDED_EGR", decode_egr_percent),
    0x2D: ("EGR_ERROR", decode_egr_error),
    0x2E: ("EVAPORATIVE_PURGE", decode_evap_purge),
    0x2F: ("FUEL_LEVEL", decode_percent_a_over_255),
    0x30: ("WARMUPS_SINCE_DTC_CLEAR", decode_warmups),
    0x31: ("DISTANCE_SINCE_DTC_CLEAR", decode_distance_km),
    0x32: ("EVAP_VAPOR_PRESSURE", decode_evap_vapor_pressure_signed),
    0x33: ("BAROMETRIC_PRESSURE", decode_baro),
    0x34: ("O2_WR_CURRENT_S1", decode_wide_lambda),
    0x35: ("O2_WR_CURRENT_S2", decode_wide_lambda),
    0x3C: ("CATALYST_TEMP_B1S1", decode_catalyst_temp),
    0x3D: ("CATALYST_TEMP_B2S1", decode_catalyst_temp),
    0x3E: ("CATALYST_TEMP_B1S2", decode_catalyst_temp),
    0x3F: ("CATALYST_TEMP_B2S2", decode_catalyst_temp),
    0x42: ("CONTROL_MODULE_VOLTAGE", decode_control_module_voltage),
    0x43: ("ABSOLUTE_LOAD", decode_absolute_load),
    0x44: ("COMMANDED_EQUIV_RATIO", decode_commanded_lambda),
    0x45: ("RELATIVE_THROTTLE_POS", decode_percent_a_over_255),
    0x46: ("AMBIANT_AIR_TEMP", decode_temp_minus_40),
    # Throttle/pedal family (Task 2.2): one-time probe on the Xterra's real
    # ECM (both KOEO and engine-running) confirmed it answers these four but
    # not 0x48/0x4B — see data/nissan-xterra-2008/pid-probe-2026-07-22.md.
    0x47: ("ABS_THROTTLE_POS_B", decode_percent_a_over_255),
    0x49: ("ACCEL_PEDAL_POS_D", decode_percent_a_over_255),
    0x4A: ("ACCEL_PEDAL_POS_E", decode_percent_a_over_255),
    0x4C: ("COMMANDED_THROTTLE_ACTUATOR", decode_percent_a_over_255),
    0x5C: ("OIL_TEMP", decode_temp_minus_40),
}


# ---------------------------------------------------------------------------
# DTC parsing (Mode 03 / 07 share the same on-wire shape)
# ---------------------------------------------------------------------------


def parse_dtc_payload(data: bytes) -> list[str]:
    """Parse a Mode 03 (or Mode 07) payload into ``"P0123"``-style codes.

    Encoding: count byte, then N × 2 bytes per DTC. Each DTC's high nibble
    of byte 0 encodes the system letter (P/C/B/U). The rest is hex. Trailing
    null DTCs (0x0000) are filler — dropped.
    """
    if not data:
        return []
    count = data[0]
    dtcs: list[str] = []
    # The count byte is the *number* of DTCs reported by the ECU; the payload
    # carries ``count * 2`` more bytes. Some ECUs only emit the DTC bytes
    # without the count prefix in single-frame replies (their `length` is 1+
    # already accounted for), so be forgiving: scan in 2-byte pairs.
    body = data[1 : 1 + count * 2] if 1 + count * 2 <= len(data) else data
    for i in range(0, len(body) - 1, 2):
        b0, b1 = body[i], body[i + 1]
        if b0 == 0 and b1 == 0:
            continue
        system = "PCBU"[(b0 >> 6) & 0x03]
        first_digit = (b0 >> 4) & 0x03
        second_digit = b0 & 0x0F
        third_fourth = b1
        dtcs.append(f"{system}{first_digit}{second_digit:X}{third_fourth:02X}")
    return dtcs


__all__ = [
    "need_bytes",
    "PID_DECODERS",
    "TIMING_ADVANCE_UNAVAILABLE",
    "decode_absolute_load",
    "decode_baro",
    "decode_catalyst_temp",
    "decode_commanded_lambda",
    "decode_control_module_voltage",
    "decode_distance_km",
    "decode_egr_error",
    "decode_egr_percent",
    "decode_evap_purge",
    "decode_evap_vapor_pressure_signed",
    "decode_fuel_status",
    "decode_intake_pressure",
    "decode_maf",
    "decode_mil_on",
    "decode_monitor_status",
    "decode_o2_voltage",
    "decode_percent_a_over_255",
    "decode_percent_b_minus_128",
    "decode_rpm",
    "decode_run_time",
    "decode_speed",
    "decode_temp_minus_40",
    "decode_timing_advance",
    "decode_warmups",
    "decode_wide_lambda",
    "parse_dtc_payload",
]
