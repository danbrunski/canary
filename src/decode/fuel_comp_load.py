"""The ECM's Fuel Comp load input ``0xFFFF118A``, reconstructed (N02.2).

Fuel Comp (map slot ``+0x660``) is indexed by rpm and by this byte, which the
ECM writes in exactly one place, ``FUN_0003c3a4`` (``docs/ecu-firmware-
analysis.md``, "Fuel Comp load input ``0xFFFF118A``, and how BFS is formed").
The byte was logged only once (``_capture-ram-study-2026-10-04T204557``), so
every other sample's Fuel Comp cell has to be rebuilt from what *was* logged.
These are the routines that rebuild it, integer for integer:

* ``FUN_0003c3ba`` / ``FUN_0003c3e4``: ``w227C = min(0xFFFF, k x rpm word)``
  (``k`` the calibration byte at ``0x8D25``; the rpm word ``0xFFFF1210`` is
  12.5 rpm per count, the PID 0x0C word), then
  ``w116A = min(0xFFFF, numerator // w227C)``, 0 when ``w227C`` is 0
  (``numerator`` the program literal ``0x00DC1D00`` at ``0x03C5E8``).
* ``FUN_0003c4d0`` at ``0x03C578``-``0x03C5C8``: the throttle effective area
  ``w4136 = curve(min(0xFFFF, 4 x w2CC8))`` through ``0x026D58`` (curve
  ``0xDD94`` on axis ``0xEE6A``), then ``w2C32 = clamp(area - w63FC, 0,
  0xFFFF)`` with the idle-air word ``w63FC`` read signed.
* ``FUN_0003c416``: ``w1124 = w2C32``, or the limp-home area (``0x90BE``)
  while the throttle-sensor fault bit is set.
* ``FUN_0003c444``: ``X = (w116A x w1124) >> 9``; if that is already
  ``0xFFFF`` or more it is ``0xFFFF`` and the purge term is skipped, else
  ``X = min(0xFFFF, X + (w116A x w139E) >> 11)``. Below ``0x3C00`` the byte
  is ``curve(4 x X) >> 8`` on ``0xEAEC``; from ``0x3C00`` it is
  ``curve(X - 0x3C00) >> 8`` on ``0xEACC``, both through ``0x026C28``
  (``X - 0x3C00`` is at most ``0xC3FF``, so ``0xEACC``'s last two entries are
  never read).

The arithmetic is this ECM *program's* (calibration ID 1ZS44A00000): the
shifts, the split and the routines are code, so they are constants here, each
naming its address. Everything a calibration can change -- the curves, the
rpm multiplier, the caps -- comes in as :class:`LoadInputCurves`, read from
the image by the caller; this module reads no file.

**Intervals.** Two inputs were never logged in history: idle air ``w63FC`` and,
in the tuning captures, purge ``w139E``. Throttle reaches the chain only
through PID 0x11's truncation. So a historical byte is an interval, not a
number (:func:`reconstruct_interval`). The chain is monotone in every input --
rising in throttle counts and purge, falling in idle air and rpm -- *provided*
the curves are non-decreasing and the two fill curves meet at the split;
:class:`LoadInputCurves` refuses any curves for which that does not hold, so
an interval computed from its two ends cannot be wrong by construction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.decode.ecm_lookup import (
    SHIFT12_ENTRIES,
    WORD_MAX,
    lookup_curve_q16,
    lookup_shift12_q16,
    search_axis,
)

#: ``0x03C582`` ``shll2``: the area curve is read at four times the throttle
#: counts (the axis runs 0..3,172 for 0..793 counts above closed).
AREA_INPUT_MULTIPLIER = 4
#: ``0x03C466``/``0x03C468`` (``shlr8``, ``shlr``): the area term's shift.
AREA_TERM_SHIFT = 9
#: ``0x03C480``-``0x03C484`` (``shlr8``, ``shlr2``, ``shlr``): the purge term's.
PURGE_TERM_SHIFT = 11
#: ``0x03C48E``-``0x03C494``: the X at which the high fill curve takes over.
X_SPLIT = 0x3C00
#: ``0x03C4A2`` ``shll2``: below the split the low curve is read at ``4 x X``.
LOW_CURVE_MULTIPLIER = 4
#: ``0x03C4B8`` ``shlr8``: the byte is the curve's high byte.
BYTE_SHIFT = 8
#: Entries in the area curve and its axis (``0x026D58`` is called with the top
#: index offset 0x3E, i.e. 31).
AREA_ENTRIES = 32
#: ``w63FC`` is read with ``mov.w`` (sign-extended) and is a clamped signed
#: word upstream (``0x054E70``), so it may be negative.
S16_MIN = -0x8000


class NonMonotoneCurvesError(ValueError):
    """The curves would make the byte non-monotone in an input, so a byte
    interval could not be bounded by its ends. Raised by
    :class:`LoadInputCurves`; a caller reading an image refuses that image."""


@dataclass(frozen=True)
class Interval:
    """A closed integer interval ``[lo, hi]`` of one raw input."""

    lo: int
    hi: int

    def __post_init__(self) -> None:
        if int(self.lo) != self.lo or int(self.hi) != self.hi:
            raise ValueError(f"interval [{self.lo!r}, {self.hi!r}] is not whole counts")
        if self.lo > self.hi:
            raise ValueError(f"interval lo {self.lo} is above hi {self.hi}")

    @classmethod
    def exactly(cls, value: int) -> Interval:
        return cls(int(value), int(value))

    @property
    def exact(self) -> bool:
        return self.lo == self.hi

    @property
    def mid(self) -> int:
        """The lower middle count: a representative value, never a claim."""
        return (self.lo + self.hi) // 2


@dataclass(frozen=True)
class LoadInputCurves:
    """Every calibration value the ``0xFFFF118A`` chain reads, from one image.

    Raw ROM words and bytes, as the ECM reads them. Validated on
    construction: lengths, word ranges, a rising area axis, and
    monotonicity of the whole chain (non-decreasing curves, and the low fill
    curve's last reachable value not above the high curve's first), which is
    what lets :func:`reconstruct_interval` bound a byte by its two ends.
    """

    #: ``0xEE6A``: 32 rising words, the area curve's axis (4 x counts).
    area_breakpoints: tuple[int, ...]
    #: ``0xDD94``: 32 words, the throttle effective area.
    area_values: tuple[int, ...]
    #: ``0xEAEC``: 16 words, read at ``4 x X`` below the split.
    fill_low: tuple[int, ...]
    #: ``0xEACC``: 16 words, read at ``X - 0x3C00`` from the split.
    fill_high: tuple[int, ...]
    #: Calibration byte ``0x8D25``: multiplies the rpm word.
    rpm_multiplier: int
    #: Program literal at ``0x03C5E8`` (``0x00DC1D00``): divided by the product.
    reciprocal_numerator: int
    #: ``0x8ED2``: the idle-air word is ``min(w1CC0, cap)``.
    idle_air_cap: int
    #: ``0x90BE``: ``w1124`` while the throttle-sensor fault bit is set.
    limp_home_area: int

    def __post_init__(self) -> None:
        lengths = {
            "area_breakpoints": (self.area_breakpoints, AREA_ENTRIES),
            "area_values": (self.area_values, AREA_ENTRIES),
            "fill_low": (self.fill_low, SHIFT12_ENTRIES),
            "fill_high": (self.fill_high, SHIFT12_ENTRIES),
        }
        for name, (values, expected) in lengths.items():
            if len(values) != expected:
                raise ValueError(f"{name} has {len(values)} entries; the ECM reads {expected}")
            for value in values:
                if int(value) != value or not 0 <= value <= WORD_MAX:
                    raise ValueError(f"{name} entry {value!r} is not a 16-bit word")
        for name in ("idle_air_cap", "limp_home_area"):
            value = getattr(self, name)
            if int(value) != value or not 0 <= value <= WORD_MAX:
                raise ValueError(f"{name} {value!r} is not a 16-bit word")
        if not 0 <= self.rpm_multiplier <= 0xFF:
            raise ValueError(f"rpm_multiplier {self.rpm_multiplier!r} is not a byte")
        if not 0 < self.reciprocal_numerator <= 0xFFFFFFFF:
            raise ValueError("reciprocal_numerator must be a positive 32-bit literal")
        axis = self.area_breakpoints
        if any(b <= a for a, b in zip(axis, axis[1:], strict=False)):
            raise NonMonotoneCurvesError(
                "area_breakpoints must rise strictly: 0x027154 divides by the gap"
            )
        for name in ("area_values", "fill_low", "fill_high"):
            values = getattr(self, name)
            if any(b < a for a, b in zip(values, values[1:], strict=False)):
                raise NonMonotoneCurvesError(
                    f"{name} falls somewhere; the byte would not be monotone in its inputs, "
                    "so an interval could not be bounded by its ends"
                )
        below_split = lookup_shift12_q16(self.fill_low, LOW_CURVE_MULTIPLIER * (X_SPLIT - 1))
        if below_split > self.fill_high[0]:
            raise NonMonotoneCurvesError(
                f"the low fill curve reaches {below_split} just below the split, above the high "
                f"curve's first value {self.fill_high[0]}: the byte is not monotone in X"
            )


@dataclass(frozen=True)
class LoadTrace:
    """One evaluation of the chain, with every intermediate word."""

    #: ``0xFFFF118A``, the Fuel Comp load input.
    byte: int
    #: ``0xFFFF2136``.
    x: int
    #: ``w4136``: the throttle effective area before idle air.
    area: int
    #: ``w1124``: the area term X is formed from.
    w1124: int
    #: ``w116A``: the rpm reciprocal.
    w116a: int


def rpm_reciprocal(rpm_word: int, curves: LoadInputCurves) -> int:
    """``w116A`` (``FUN_0003c3ba`` then ``FUN_0003c3e4``)."""
    word = _word("rpm word", rpm_word)
    product = min(WORD_MAX, curves.rpm_multiplier * word)
    if product == 0:
        return 0
    return min(WORD_MAX, curves.reciprocal_numerator // product)


def throttle_area(throttle_counts: int, curves: LoadInputCurves) -> int:
    """``w4136``: the area curve at ``min(0xFFFF, 4 x w2CC8)``."""
    counts = _word("throttle counts", throttle_counts)
    x = min(WORD_MAX, AREA_INPUT_MULTIPLIER * counts)
    return lookup_curve_q16(curves.area_values, curves.area_breakpoints, x)


def effective_area(area: int, idle_air: int, curves: LoadInputCurves) -> int:
    """``w2C32 = clamp(area - w63FC, 0, 0xFFFF)``; ``idle_air`` is signed."""
    air = _idle_air(idle_air, curves)
    return max(0, min(WORD_MAX, int(area) - air))


def load_x(w1124: int, purge_area: int, w116a: int) -> int:
    """``FUN_0003c444`` up to ``0xFFFF2136``."""
    first = (_word("w1124", w1124) * _word("w116A", w116a)) >> AREA_TERM_SHIFT
    if first >= WORD_MAX:
        return WORD_MAX
    second = (_word("purge area", purge_area) * w116a) >> PURGE_TERM_SHIFT
    return min(WORD_MAX, first + second)


def load_byte(x: int, curves: LoadInputCurves) -> int:
    """``0xFFFF118A`` from X: the split, the fill curve, the high byte."""
    value = _word("X", x)
    if value < X_SPLIT:
        word = lookup_shift12_q16(curves.fill_low, LOW_CURVE_MULTIPLIER * value)
    else:
        word = lookup_shift12_q16(curves.fill_high, value - X_SPLIT)
    return word >> BYTE_SHIFT


def reconstruct(
    *,
    throttle_counts: int,
    rpm_word: int,
    idle_air: int,
    purge_area: int,
    curves: LoadInputCurves,
    limp_home: bool = False,
) -> LoadTrace:
    """The byte the ECM writes for one set of raw inputs."""
    w116a = rpm_reciprocal(rpm_word, curves)
    area = throttle_area(throttle_counts, curves)
    w1124 = curves.limp_home_area if limp_home else effective_area(area, idle_air, curves)
    x = load_x(w1124, purge_area, w116a)
    return LoadTrace(byte=load_byte(x, curves), x=x, area=area, w1124=w1124, w116a=w116a)


@dataclass(frozen=True)
class LoadInterval:
    """The byte's range over the inputs' intervals, with the trace at each end."""

    low: LoadTrace
    high: LoadTrace

    @property
    def byte(self) -> Interval:
        return Interval(self.low.byte, self.high.byte)


def reconstruct_interval(
    *,
    throttle_counts: Interval,
    rpm_word: int,
    idle_air: Interval,
    purge_area: Interval,
    curves: LoadInputCurves,
    limp_home: bool = False,
) -> LoadInterval:
    """The byte's exact range over every combination of the input intervals.

    The low end is fewest throttle counts, most idle air and least purge; the
    high end the reverse. That is exact (not an approximation) because the
    chain is monotone in each input, which :class:`LoadInputCurves` refuses
    to be constructed without. rpm is a single word: the PID 0x0C value is the
    word times 12.5 exactly, so it carries no interval.
    """
    low = reconstruct(
        throttle_counts=throttle_counts.lo,
        rpm_word=rpm_word,
        idle_air=idle_air.hi,
        purge_area=purge_area.lo,
        curves=curves,
        limp_home=limp_home,
    )
    high = reconstruct(
        throttle_counts=throttle_counts.hi,
        rpm_word=rpm_word,
        idle_air=idle_air.lo,
        purge_area=purge_area.hi,
        curves=curves,
        limp_home=limp_home,
    )
    return LoadInterval(low=low, high=high)


def columns_read(load_axis: Sequence[int], byte: int) -> tuple[int, int]:
    """``(first, last)`` Fuel Comp column the load search reads for ``byte``.

    The load axis is compared byte for byte (``0x027114``): on a breakpoint
    or past an end the lookup reads one column, between two it reads both.
    """
    hit = search_axis(load_axis, byte)
    if hit is None:
        raise ValueError("a load axis needs at least one breakpoint")
    return (hit.index, hit.index + 1 if hit.fraction_q16 else hit.index)


def spans_one_cell(load_axis: Sequence[int], byte: Interval) -> bool:
    """Whether every byte in the interval reads within one pair of columns.

    Monotone in the byte, so the columns read across the interval run from
    the low end's first to the high end's last; the cell assignment is
    unique when that is at most two adjacent columns.
    """
    first, _ = columns_read(load_axis, byte.lo)
    _, last = columns_read(load_axis, byte.hi)
    return last - first <= 1


# -- inputs from what was logged ------------------------------------------------


def pid_byte_from_percent(percent: float, *, full_scale: int = 255) -> int:
    """A Mode 01 percent PID's data byte ``A`` (``percent = A x 100 / 255``).

    Stored percentages are ``A x 100/255`` in floating point, so rounding
    ``percent x 255 / 100`` to the nearest integer recovers ``A`` exactly;
    anything further than 0.01 of a count from an integer was not such a
    byte and is refused.
    """
    raw = float(percent) * full_scale / 100.0
    byte = round(raw)
    if abs(raw - byte) > 0.01 or not 0 <= byte <= full_scale:
        raise ValueError(f"{percent!r} % is not a whole Mode 01 byte (A = {raw:.4f})")
    return int(byte)


def counts_from_truncated_byte(
    byte: int, *, numerator: int, denominator: int, full_scale: int, counts_max: int
) -> Interval:
    """Every count ``w`` with ``min(full_scale, floor(max(0, w) x numerator /
    denominator)) == byte``, clamped to ``0..counts_max``.

    PID 0x11's handler (``0xD41E0``) forms ``A = clamp_u8(max(0, w2CCE) x
    100 / 321)``, truncating; the inverse is the interval
    ``[ceil(A x 321/100), ceil((A + 1) x 321/100) - 1]``. ``A = 0`` also covers
    every negative count, which the chain's input ``w2CC8`` clamps to 0, and
    ``A = full_scale`` every count from its floor up to ``counts_max``.
    """
    if not 0 <= byte <= full_scale:
        raise ValueError(f"byte {byte} outside 0..{full_scale}")
    lo = -(-byte * denominator // numerator)
    hi = counts_max if byte == full_scale else -(-(byte + 1) * denominator // numerator) - 1
    return Interval(max(0, lo), min(counts_max, hi))


def purge_area_from_command_byte(command: int, *, gain: int, gain_shift: int) -> Interval:
    """``w139E`` from the purge byte ``0xFFFF272E`` (PID 0x2E's and DID 0x1110's).

    ``FUN_0006a52c`` writes the byte as ``purge_raw + 1`` (255 at most) and 0
    while purge is off, when ``w139E`` is 0 too (the zero-command branch at
    ``0x06E224``). Otherwise ``w139E = (purge_raw & 0xFF) x gain >> 7``
    (``gain`` the calibration byte ``0x82FE``, the shift the helper
    ``0x022584``). A byte of 255 is ambiguous -- ``purge_raw`` 254, or 255
    and above with the low byte wrapping -- so it returns the whole range the
    code can produce.
    """
    if not 0 <= command <= 0xFF:
        raise ValueError(f"purge command byte {command} is not a byte")
    if command == 0:
        return Interval.exactly(0)
    if command == 0xFF:
        return Interval(0, (0xFF * gain) >> gain_shift)
    return Interval.exactly(((command - 1) * gain) >> gain_shift)


def purge_area_unknown(*, gain: int, gain_shift: int) -> Interval:
    """Every ``w139E`` the code can produce: purge not logged."""
    return Interval(0, (0xFF * gain) >> gain_shift)


def _word(name: str, value: int) -> int:
    if int(value) != value or not 0 <= value <= WORD_MAX:
        raise ValueError(f"{name} {value!r} is not a whole 16-bit count")
    return int(value)


def _idle_air(value: int, curves: LoadInputCurves) -> int:
    if int(value) != value:
        raise ValueError(f"idle air {value!r} is not a whole count")
    if not S16_MIN <= value <= curves.idle_air_cap:
        raise ValueError(
            f"idle air {value} is outside what the ECM can hold: w63FC = min(w1CC0, "
            f"{curves.idle_air_cap}) with w1CC0 a signed word"
        )
    return int(value)


__all__ = [
    "AREA_ENTRIES",
    "AREA_INPUT_MULTIPLIER",
    "AREA_TERM_SHIFT",
    "BYTE_SHIFT",
    "LOW_CURVE_MULTIPLIER",
    "PURGE_TERM_SHIFT",
    "S16_MIN",
    "X_SPLIT",
    "Interval",
    "LoadInputCurves",
    "LoadInterval",
    "LoadTrace",
    "NonMonotoneCurvesError",
    "columns_read",
    "counts_from_truncated_byte",
    "effective_area",
    "load_byte",
    "load_x",
    "pid_byte_from_percent",
    "purge_area_from_command_byte",
    "purge_area_unknown",
    "reconstruct",
    "reconstruct_interval",
    "rpm_reciprocal",
    "spans_one_cell",
    "throttle_area",
]
