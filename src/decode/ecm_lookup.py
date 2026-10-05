"""The ECM's 2-D table lookup arithmetic, once (E02.4, W05.1).

One pure kernel for everything that asks "where does this coordinate land on
a ROM table's axis, and what does the ECM read there": the ROM Maps page's
ECM cell (:mod:`src.analysis.rom_trace`) and the target-equivalence-ratio twin
(:mod:`src.analysis.target_phi`), stored-session and streaming alike. A second
copy of the axis search is how the two would come to disagree about a
breakpoint, so neither keeps one (``tests/test_ecm_lookup.py`` pins that the
trace calls :func:`search_axis`).

The arithmetic is the 2008 Xterra ECM program's (ZS44A; decoded from the
2026-10-04 flash read, ``docs/ecu-firmware-analysis.md``, "Target AFR lookup,
decoded"). Nothing vehicle-specific lives here: which table, which axes, what
an axis byte is multiplied by before the comparison, and which flag forces
which column are profile data the caller passes in.

**Axis search** (``0x027114`` byte axis, ``0x027194`` byte axis x 4): scan from
the top breakpoint down for the largest index ``i`` with ``axis[i] <= value``.
A value equal to a breakpoint is that index with no fraction. Above the top
breakpoint it is the top index, below the first it is index 0, both with no
fraction (the clamp). Otherwise the fraction is
``((value - axis[i]) << 16) // (axis[i + 1] - axis[i])``, unsigned (``0x027448``).

**Interpolation** (``0x026F10``): ``a + ((b - a) * frac >> 16)`` when
``b >= a``, else ``a - ((a - b) * frac >> 16)``: truncated toward ``a`` in
both directions, never rounded.

**Bilinear** (``0x026F40``, row-major): with a column fraction, interpolate
each of the two rows along the columns, then between the rows by the row
fraction; with none, read the cell (and interpolate down the column when there
is a row fraction). A cell past the last row or column is never read, because
a fraction is zero at the top index.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

#: Bumped whenever the search, interpolation or rounding changes, so every
#: prediction names the arithmetic it came from.
KERNEL_VERSION = 1

#: Which end of an axis a coordinate fell past.
AxisEdge = Literal["below", "above"]

#: Fixed-point weight resolution of the firmware's interpolation: a fraction
#: is ``0..FRACTION_ONE - 1`` of the way to the next breakpoint.
FRACTION_BITS = 16
FRACTION_ONE = 1 << FRACTION_BITS


@dataclass(frozen=True)
class AxisHit:
    """Where one coordinate landed on one axis.

    ``index`` is the lower breakpoint the lookup reads from. ``numerator`` /
    ``span`` is how far past it the coordinate sits (``0`` / ``1`` on a
    breakpoint and at a clamp), kept as the two exact differences so an
    integer caller takes the firmware's Q16 fraction and a float caller its
    true position from the same search.
    """

    index: int
    numerator: float
    span: float
    #: The edge a coordinate fell past (and was clamped at), or ``None``.
    edge: AxisEdge | None

    @property
    def position(self) -> float:
        """Fractional index: ``index`` plus the fraction toward ``index + 1``."""
        return self.index + (self.numerator / self.span if self.numerator else 0.0)

    @property
    def fraction_q16(self) -> int:
        """The firmware's unsigned Q16 fraction (``0x027448``). Integer axes only."""
        if not self.numerator:
            return 0
        numerator, span = int(self.numerator), int(self.span)
        if numerator != self.numerator or span != self.span:
            raise ValueError("a Q16 fraction needs integer breakpoints and coordinates")
        return (numerator << FRACTION_BITS) // span


def search_axis(breakpoints: Sequence[float], value: float) -> AxisHit | None:
    """The firmware's axis search (see the module docstring). ``None`` only
    for an empty axis or a non-finite value; past either end it clamps and
    names the edge, and the caller decides whether a clamp is allowed."""
    if not breakpoints or not math.isfinite(value):
        return None
    top = len(breakpoints) - 1
    for index in range(top, -1, -1):
        point = breakpoints[index]
        if value == point:
            return AxisHit(index=index, numerator=0, span=1, edge=None)
        if value > point:
            if index == top:
                return AxisHit(index=top, numerator=0, span=1, edge="above")
            return AxisHit(
                index=index,
                numerator=value - point,
                span=breakpoints[index + 1] - point,
                edge=None,
            )
    return AxisHit(index=0, numerator=0, span=1, edge="below")


def interpolate_q16(a: int, b: int, fraction: int) -> int:
    """``0x026F10``: from ``a`` toward ``b`` by ``fraction``/65536, truncated
    toward ``a`` whichever way it moves."""
    if b >= a:
        return a + (((b - a) * fraction) >> FRACTION_BITS)
    return a - (((a - b) * fraction) >> FRACTION_BITS)


@dataclass(frozen=True)
class LookupResult:
    """One 2-D lookup: the value read, and every input that decided it."""

    value: int
    row: AxisHit
    column: AxisHit
    row_fraction: int
    column_fraction: int
    #: ``(row, column)`` of each cell the interpolation read, in read order.
    cells: tuple[tuple[int, int], ...]


def bilinear_q16(table: Sequence[Sequence[int]], row: AxisHit, column: AxisHit) -> LookupResult:
    """``0x026F40``: the table value at two axis hits (row-major cells)."""
    r, c = row.index, column.index
    row_fraction, column_fraction = row.fraction_q16, column.fraction_q16
    if column_fraction:
        upper = interpolate_q16(table[r][c], table[r][c + 1], column_fraction)
        if not row_fraction:
            return LookupResult(
                upper, row, column, row_fraction, column_fraction, ((r, c), (r, c + 1))
            )
        lower = interpolate_q16(table[r + 1][c], table[r + 1][c + 1], column_fraction)
        return LookupResult(
            interpolate_q16(upper, lower, row_fraction),
            row,
            column,
            row_fraction,
            column_fraction,
            ((r, c), (r, c + 1), (r + 1, c), (r + 1, c + 1)),
        )
    if not row_fraction:
        return LookupResult(table[r][c], row, column, 0, 0, ((r, c),))
    return LookupResult(
        interpolate_q16(table[r][c], table[r + 1][c], row_fraction),
        row,
        column,
        row_fraction,
        0,
        ((r, c), (r + 1, c)),
    )


def lookup_2d(
    table: Sequence[Sequence[int]],
    row_breakpoints: Sequence[int],
    column_breakpoints: Sequence[int],
    row_value: int,
    column_value: int,
) -> LookupResult:
    """Search both axes and interpolate, exactly as the ECM does.

    Breakpoints are in the comparison domain of their input (an rpm axis the
    firmware compares as ``byte x 4`` is passed multiplied by 4). Every value
    must be a whole count: a fractional coordinate is not something the ECM
    ever sees, so it is refused rather than truncated.
    """
    for name, value in (("row", row_value), ("column", column_value)):
        if int(value) != value:
            raise ValueError(f"{name} coordinate {value!r} is not a whole raw count")
    row = search_axis(row_breakpoints, row_value)
    column = search_axis(column_breakpoints, column_value)
    if row is None or column is None:
        raise ValueError("a lookup needs non-empty axes and finite coordinates")
    if len(table) != len(row_breakpoints) or any(
        len(cells) != len(column_breakpoints) for cells in table
    ):
        raise ValueError("table shape does not match its axes")
    return bilinear_q16(table, row, column)


__all__ = [
    "FRACTION_BITS",
    "FRACTION_ONE",
    "KERNEL_VERSION",
    "AxisEdge",
    "AxisHit",
    "LookupResult",
    "bilinear_q16",
    "interpolate_q16",
    "lookup_2d",
    "search_axis",
]
