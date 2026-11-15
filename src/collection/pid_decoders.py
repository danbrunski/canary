"""SAE J1979 Mode 01 decoders, and the Mode 03/07/0A DTC parser (roadmap R02.13).

Pure functions of a response payload: no python-can, no transport. They lived
inside ``can_obd_client`` beside ``CanObdClient``, which made ``obd_registry``
import the transport client (including the private ``need_bytes``) and forced the
client to take ``byte_length_for`` by injection to dodge the import cycle.
``can_obd_client`` re-exports every name here. ``PID_DECODERS``, the table of
polled PIDs, is derived from the registry's rows in ``obd_registry`` (R23.14).

This is a relocation, not a merge: the ELM327 path's decoders
(``obd_collector`` / ``pids``) stay separate by the July decision
(complementary PID coverage), as do ``decode_commanded_lambda`` and
``decode_wide_lambda`` (identical bodies, but the names carry phi vs lambda).
"""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction

# ---------------------------------------------------------------------------
# PID decoders — SAE J1979 standardized formulas.
# Each takes the response payload (bytes after `41 PID`) and returns
# (value, unit), or None when the payload is too short. Decoders match
# python-obd's output for the same PIDs.
# ---------------------------------------------------------------------------

#: The decoder shape every Mode 01 PID shares.
PidDecoder = Callable[[bytes], tuple[float, str] | None]


def need_bytes(data: bytes, n: int) -> bool:
    """Whether ``data`` carries at least ``n`` payload bytes."""
    return len(data) >= n


def linear(
    width: int,
    scale: int | float | str | Fraction,
    offset: int,
    unit: str,
    *,
    signed: bool = False,
) -> PidDecoder:
    """A decoder for ``raw * scale + offset``, ``raw`` the first ``width``
    payload bytes as a big-endian integer (R23.14).

    ``scale`` is exact: ``"100/255"`` or ``Fraction(1, 4)`` is that rational,
    and a float is its own binary value. The value is ``raw * numerator /
    denominator`` in integer arithmetic with one correctly rounded division,
    so ``"1/100"`` gives what ``raw / 100.0`` gave and ``0.079`` what
    ``raw * 0.079`` gave, bit for bit (``raw * 0.01`` would not). ``offset``
    is an integer, added after the scale; the formulas written ``(A - 128) *
    100/128`` are ``linear(1, "100/128", -100, ...)``, exact because the
    scaled value is.
    """
    ratio = Fraction(scale)
    numerator, denominator = ratio.numerator, ratio.denominator

    def decode(data: bytes) -> tuple[float, str] | None:
        if len(data) < width:
            return None
        raw = int.from_bytes(data[:width], "big", signed=signed)
        value = raw * numerator / denominator
        return (value + offset if offset else value, unit)

    decode.__qualname__ = decode.__name__ = (
        f"linear({width}, {ratio}, {offset}, {unit!r}{', signed' if signed else ''})"
    )
    return decode


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


decode_rpm = linear(2, "1/4", 0, "rpm")
decode_speed = linear(1, 1, 0, "kph")
#: COOLANT_TEMP / INTAKE_TEMP / AMBIANT_AIR_TEMP / OIL_TEMP.
decode_temp_minus_40 = linear(1, 1, -40, "degC")
#: THROTTLE_POS / ENGINE_LOAD / RELATIVE_THROTTLE_POS / FUEL_LEVEL /
#: COMMANDED_EGR / EVAPORATIVE_PURGE / etc.
decode_percent_a_over_255 = linear(1, "100/255", 0, "%")
#: SHORT_FUEL_TRIM / LONG_FUEL_TRIM, both banks, and EGR_ERROR:
#: ``(A - 128) * 100/128``. Range -100% to +99.2%.
decode_percent_b_minus_128 = linear(1, "100/128", -100, "%")
decode_maf = linear(2, "1/100", 0, "g/s")
decode_intake_pressure = linear(1, 1, 0, "kPa")
decode_baro = linear(1, 1, 0, "kPa")

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


#: Narrowband O2 voltage; byte B is short-term fuel trim if present.
decode_o2_voltage = linear(1, "1/200", 0, "V")


decode_control_module_voltage = linear(2, "1/1000", 0, "V")


decode_run_time = linear(2, 1, 0, "s")


decode_distance_km = linear(2, 1, 0, "km")


decode_warmups = linear(1, 1, 0, "count")


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
    (``PID_DECODERS``); the registry rows for the rest decode with it too.
    """
    if not need_bytes(data, 2):
        return None
    return ((data[0] * 256 + data[1]) / 32768.0, "ratio")


#: PID 0x03 fuel-system status. SAE J1979 returns two bitmask bytes. The
#: first carries the primary fuel-system state for vehicles with one bank;
#: preserving it as a numeric bitmask keeps the channel model-friendly while
#: avoiding a vehicle-specific enum interpretation in the collector.
decode_fuel_status = linear(1, 1, 0, "bitmask")


#: PID 0x32: signed 16-bit / 4 Pa. Negative = vacuum.
decode_evap_vapor_pressure_signed = linear(2, "1/4", 0, "Pa", signed=True)


#: COMMANDED_EGR: A * 100/255.
decode_egr_percent = decode_percent_a_over_255


#: EGR_ERROR: (A - 128) * 100/128. Range -100%..+99.2%.
decode_egr_error = decode_percent_b_minus_128


decode_evap_purge = decode_percent_a_over_255


#: CATALYST_TEMP_*: ((A*256+B) / 10) - 40. Often unsupported on US-spec
#: light vehicles even when claimed.
decode_catalyst_temp = linear(2, "1/10", -40, "degC")


#: ABSOLUTE_LOAD (PID 0x43): 2-byte percent, ``(A*256+B) * 100/255``.
#: Task 2.3: SAE J1979 defines this PID as 2-byte (unlike the 1-byte
#: ``decode_percent_a_over_255`` family) so it can read past 100% — real
#: under boost/load on many engines. The old 1-byte decode silently
#: truncated/wrapped above that.
decode_absolute_load = linear(2, "100/255", 0, "%")


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
    "PidDecoder",
    "linear",
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
