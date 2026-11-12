"""The ECU's injector pulse chain, in its own integer arithmetic (E07.3).

This is the controller side of the fuel path: what pulse the ECM *commands*,
reproduced from the decoded firmware (``docs/ecu-firmware-analysis.md``,
"Injector pulse chain, dead time and the BFS ceiling"; Ghidra notes
``~/ecu-re/NOTES.md``). It is kept apart from :mod:`src.analysis.fuel_plant`,
the engine side, on purpose (E07 "Keep ECU integer arithmetic distinct from
the plant's continuous equations"): the dead time here is the ECM's
*compensation* for injector latency, a number in its calibration, not a
measurement of the injector. A plant that borrows it says so.

Order and formula, u16 words with u32 intermediates, 10 us per pulse count::

    base   = (BFS * phi_q14) >> 17
    G      = max(246, ((base * alpha / 0x6400 + wallfilm) * w1800) >> 14)
    k      = STFT + LTFT + w2E8C - 0xC800           # trims ADD; 0x6400 = 1.0
    Ti     = ((G * k) >> 16) + Ts
    Ts     = max(0, 47 + (((175 - Vb) * 92) >> 7)), Vb in 0.08 V counts
    w1800  = (b1772 * 10601) >> 8                   # 0x9254, injector-flow scaler
    b1772  = min(255, (b1A15 * 5259 + 0x128500) // 10000)   # linearised sqrt
    b1A15  = min(255, (175 << 18) // ((175 << 10) + vac512))  # 256*350/(350+vac)

``vac512`` is the ECM's *estimated* manifold vacuum (1/512 kPa) from baro, a
BFS-derived load byte and rpm through two 16-point curves (``D627``/``D637``);
it is never below about 0.2 x baro. Wall film and purge are taken neutral at
steady state (purge is unlogged on the tuning captures). The DIDs 0x1206/0x1207
read the per-cylinder word *including* Ts and *before* the short-pulse
correction and min-pulse floor, so the known answer compares ``(DID - Ts)``
with ``Ti - Ts``.

Calibration bytes come from the image, at addresses the profile declares
(``fuel_plant.yaml`` ``pulse_chain``); no ROM bytes are committed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

from src.analysis.ecu_state_models import byte_curve16
from src.analysis.rom_map import DTYPE_BYTES, RomMap

#: 1.0 in the trim words (0x6400).
TRIM_UNITY = 0x6400
#: Trims add: k = STFT + LTFT + w2E8C - 0xC800, with the diagnostic word
#: w2E8C neutral (0x6400) in normal driving.
TRIM_BIAS = 0xC800
#: One pulse count in ms (10 us; inferred from the timer set-up, UpRev agrees).
PULSE_COUNT_MS = 0.01
#: Battery-voltage count (PID 0x42 / 0.08 V is the word the dead time reads).
VB_COUNT_V = 0.08
#: phi in Q14.
PHI_Q14 = 16384


Int = NDArray[np.int64]


@dataclass(frozen=True)
class PulseChainCalibration:
    """The calibration words the pulse chain reads, as integers."""

    #: ``0x8C41``: dead time at the reference voltage, counts.
    dead_time_base: int
    #: ``0x84F2``: dead-time slope, counts x 128 per Vb count.
    dead_time_slope: int
    #: Code immediate: the reference voltage in Vb counts (175 = 14.0 V).
    dead_time_vref: int
    #: ``0x8A89``: rail reference (175, read as 350 kPa at 2 kPa/count).
    rail_reference: int
    #: ``0x9254``: injector-flow scaler multiplying every pulse after BFS.
    flow_scaler: int
    #: ``0xD627``: 16-point vacuum-estimate curve on the load byte.
    vacuum_load_curve: tuple[int, ...]
    #: ``0xD637``: 16-point vacuum-estimate curve on rpm / 25.
    vacuum_rpm_curve: tuple[int, ...]
    #: Minimum G (code immediate).
    minimum_g: int = 246

    def __post_init__(self) -> None:
        for name in ("vacuum_load_curve", "vacuum_rpm_curve"):
            curve = getattr(self, name)
            if len(curve) != 16 or any(not 0 <= v <= 255 for v in curve):
                raise ValueError(f"{name} must be 16 bytes, got {curve!r}")


def _ints(values: ArrayLike) -> Int:
    return np.asarray(np.rint(np.asarray(values, dtype=float)), dtype=np.int64)


#: The rom_map ids of the short-pulse words, in ShortPulseCalibration order.
SHORT_PULSE_TABLES: tuple[str, ...] = (
    "short_pulse_gain_a",
    "short_pulse_gain_b",
    "short_pulse_offset_a",
    "short_pulse_offset_b",
)


@dataclass(frozen=True)
class ShortPulseCalibration:
    """The short-pulse linearisation words (cal ``0x9120``-``0x9126``)."""

    gain_a: int
    gain_b: int
    offset_a: int
    offset_b: int

    def __post_init__(self) -> None:
        for name in ("gain_a", "gain_b", "offset_a", "offset_b"):
            if not 0 <= getattr(self, name) <= 0xFFFF:
                raise ValueError(f"{name} must be a u16 word, got {getattr(self, name)!r}")


def short_pulse_from_image(image: bytes, rom_map: RomMap) -> ShortPulseCalibration:
    """The four words from ``image``, each by its rom_map id (never a copied offset).

    Refuses a mapping that is not a single absolute word, so a map that moved
    or reshaped one cannot be read as a different number."""
    words = []
    for table_id in SHORT_PULSE_TABLES:
        table = rom_map.table(table_id)
        if table.offset is None or (table.rows, table.cols) != (1, 1) or table.dtype != "be16":
            raise ValueError(f"{table_id} must be one absolute be16 word in the rom map")
        width = DTYPE_BYTES[table.dtype]
        words.append(int.from_bytes(image[table.offset : table.offset + width], "big"))
    return ShortPulseCalibration(*words)


def short_pulse_linearize(pulse_counts: ArrayLike, cal: ShortPulseCalibration) -> Int:
    """The ECM's final per-cylinder pulse from its pulse word ``x`` (10 us counts).

    ``INJ_short_pulse_linearize`` (``0x05B8B0``), every step from the listing::

        a = min(0xFFFF, (x * gain_a + 100 * offset_a) >> 14)
        b = min(0xFFFF, (x * gain_b + 100 * offset_b) >> 14)
        y = max(x, a, b)

    Each line has a gain just under 1.0 (Q14) and a positive intercept, so it
    lengthens short pulses only: on ZS44A, term B below 1.78 ms (up to 5
    counts at zero) and term A below 2.84 ms (under half a count, so it never
    survives the truncation). ``x`` is the word after the per-cylinder terms,
    the one DID ``0x1206``/``0x1207`` reports; the min-pulse floor ``0x2770``
    comes after this. The result is what the ECM *commands*: it compensates
    the injector's short-pulse nonlinearity, and whether delivered fuel
    follows ``x`` or ``y`` is the plant's question, not answered here."""
    x = _ints(pulse_counts)
    if np.any((x < 0) | (x > 0xFFFF)):
        raise ValueError("pulse words are u16")
    a = np.minimum(0xFFFF, (x * cal.gain_a + 100 * cal.offset_a) >> 14)
    b = np.minimum(0xFFFF, (x * cal.gain_b + 100 * cal.offset_b) >> 14)
    out: Int = np.maximum(x, np.maximum(a, b))
    return out


def dead_time_counts(vb_counts: ArrayLike, *, base: int, slope: int, vref: int) -> Int:
    """The ECM's dead time ``Ts`` (10 us counts) at battery-voltage counts ``Vb``.

    ``(vref - Vb) * slope >> 7`` is an arithmetic shift (floors a negative
    product, as SH-2 ``SHAR`` does), so above 14.0 V Ts falls below ``base``.
    """
    vb = _ints(vb_counts)
    out: Int = np.maximum(base + (((vref - vb) * slope) >> 7), 0)
    return out


def vb_counts_from_volts(volts: ArrayLike) -> Int:
    """PID 0x42 volts -> the 0.08 V word the dead time reads."""
    return _ints(np.asarray(volts, dtype=float) / VB_COUNT_V)


def _curve16(table: tuple[int, ...], x: Int) -> Int:
    """The ECM's 16-point byte curve (``0x026BBC``), element-wise.

    One implementation: each element goes through
    :func:`src.analysis.ecu_state_models.byte_curve16`, the twin's reference
    kernel (E06.1 inventory); this only clamps the input to a byte first, as
    the callers' ``min(255, ...)`` already do in the firmware.
    """
    xs = np.clip(np.asarray(x, dtype=np.int64), 0, 255)
    flat = [byte_curve16(table, int(v)) for v in xs.ravel()]
    result: Int = np.asarray(flat, dtype=np.int64).reshape(xs.shape)
    return result


def vacuum_estimate_512(
    bfs: ArrayLike,
    rpm: ArrayLike,
    baro_kpa: ArrayLike,
    iat_c: ArrayLike,
    cal: PulseChainCalibration,
) -> Int:
    """The ECM's manifold-vacuum estimate, 1/512 kPa (word ``0xFFFF3954``).

    Baro and IAT words are rebuilt from the Mode 01 values (integer kPa, degC)
    the way ``~/ecu-re/scripts/inj_pulse_check.py`` does: baro to 1/512 kPa
    with half a count added back, IAT as degC + 50.
    """
    b512 = np.minimum(0xFFFF, _ints((np.asarray(baro_kpa, dtype=float) + 0.5) * 512))
    br = np.clip((b512 << 8) // 0xCA99, 128, 256)
    t = _ints(np.asarray(iat_c, dtype=float) + 50)
    dr = np.clip(0x12A00 // (t + 0xDF), 179, 343)
    load = np.minimum(255, (((_ints(bfs) * br) >> 8) * dr) >> 15)
    rpm25 = np.minimum(255, _ints(np.floor(np.asarray(rpm, dtype=float) / 25)))
    s = np.minimum(
        255, _curve16(cal.vacuum_load_curve, load) + _curve16(cal.vacuum_rpm_curve, rpm25)
    )
    vac: Int = np.minimum(0xFFFF, (b512 * (256 - s)) >> 8)
    return vac


def pressure_correction_word(vac512: ArrayLike, cal: PulseChainCalibration) -> Int:
    """``w1800``: the ECM's rail-to-manifold flow correction times ``0x9254``."""
    vac = _ints(vac512)
    ref = cal.rail_reference
    a15 = np.minimum(255, (ref << 18) // ((ref << 10) + vac))
    a72 = np.minimum(255, (a15 * 5259 + 0x128500) // 10000)
    word: Int = (a72 * cal.flow_scaler) >> 8
    return word


def trim_word(percent: ArrayLike) -> Int:
    """A Mode 01 fuel-trim percent -> the ECM's trim word (0x6400 = 1.0).

    PID 0x06..0x09 byte A is ``(percent / 100) * 128 + 128``; the word is that
    byte times 200, so 0 % is exactly 0x6400.
    """
    byte = _ints(np.asarray(percent, dtype=float) / 100.0 * 128 + 128)
    return byte * 200


@dataclass(frozen=True)
class PulsePrediction:
    """The chain's output and the intermediates a check reports."""

    #: Predicted per-cylinder word, counts (Ts included).
    pulse: Int
    #: Its dead time, counts.
    dead_time: Int
    #: ``w1800``.
    pressure_word: Int
    #: The ECM's vacuum estimate, kPa.
    vacuum_kpa: NDArray[np.float64]
    #: ``G``: the untrimmed metering word the trims multiply (``Ti - Ts =
    #: (G x k) >> 16``). A closed-loop fixed point holds ``G x k`` (E07.6).
    g: Int | None = None

    @property
    def effective(self) -> Int:
        """``Ti - Ts``: the part of the pulse that meters fuel."""
        return self.pulse - self.dead_time


def predict_pulse(
    *,
    bfs: ArrayLike,
    phi_q14: ArrayLike,
    stft_word: ArrayLike,
    ltft_word: ArrayLike,
    vb_counts: ArrayLike,
    rpm: ArrayLike,
    baro_kpa: ArrayLike,
    iat_c: ArrayLike,
    cal: PulseChainCalibration,
    purge_word: ArrayLike = TRIM_UNITY,
    wallfilm: ArrayLike = 0,
) -> PulsePrediction:
    """The steady-state per-cylinder pulse word, before the short-pulse correction."""
    base = (_ints(bfs) * _ints(phi_q14)) >> 17
    a = (base * _ints(purge_word)) // TRIM_UNITY + _ints(wallfilm)
    vac = vacuum_estimate_512(bfs, rpm, baro_kpa, iat_c, cal)
    w1800 = pressure_correction_word(vac, cal)
    g = np.maximum(cal.minimum_g, np.minimum(0xFFFF, (a * w1800) >> 14))
    k = np.maximum(0, _ints(stft_word) + _ints(ltft_word) + TRIM_UNITY - TRIM_BIAS)
    ts = dead_time_counts(
        vb_counts, base=cal.dead_time_base, slope=cal.dead_time_slope, vref=cal.dead_time_vref
    )
    pulse = ((g * k) >> 16) + ts
    ts = np.broadcast_to(ts, pulse.shape).copy()
    return PulsePrediction(
        pulse=pulse,
        dead_time=ts,
        pressure_word=w1800,
        vacuum_kpa=vac.astype(float) / 512.0,
        g=np.broadcast_to(g, pulse.shape).copy(),
    )
