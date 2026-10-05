"""The ECM lookup kernel (E02.4) against hand-computed vectors.

Every expected value below was worked by hand from the firmware's arithmetic
(``docs/ecu-firmware-analysis.md``, "Target AFR lookup, decoded") on the
ZS44A-20260920 Map 1 table as committed in ``afr_calibration.yaml``, not by
running the kernel. Truncation toward the first operand is the property a
float re-implementation would lose, so several vectors sit where rounding to
nearest gives a different count.
"""

from __future__ import annotations

import math

import pytest

from src.analysis import rom_trace
from src.decode import ecm_lookup
from src.decode.ecm_lookup import (
    AxisHit,
    interpolate_q16,
    lookup_2d,
    search_axis,
)

#: ZS44A-20260920 Map 1 (afr_calibration.yaml raw_values, == image slot 1).
TABLE = [
    [0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x92],
    [0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x95],
    [0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x95],
    [0x80, 0x80, 0x80, 0x80, 0x8E, 0x90, 0x90, 0x95],
    [0x80, 0x80, 0x80, 0x93, 0x96, 0x95, 0x95, 0x95],
    [0x80, 0x80, 0x90, 0x93, 0x96, 0x99, 0x99, 0x99],
    [0x80, 0x8B, 0x97, 0x99, 0x9D, 0x9B, 0x9B, 0x9B],
    [0x93, 0x98, 0x9D, 0xA0, 0xA3, 0x9F, 0x9F, 0x9F],
]
#: The rpm axis bytes 18 20 28 38 48 50 58 68 (hex), compared as byte x 4.
RPM = [b * 4 for b in (0x18, 0x20, 0x28, 0x38, 0x48, 0x50, 0x58, 0x68)]
#: The raw BFS axis, compared with the load byte directly.
LOAD = [0x34, 0x44, 0x5D, 0x64, 0x6C, 0x6E, 0x7F, 0x80]


# -- axis search ------------------------------------------------------------------


def test_a_value_on_a_breakpoint_is_that_index_with_no_fraction() -> None:
    hit = search_axis(LOAD, 0x5D)
    assert hit == AxisHit(index=2, numerator=0, span=1, edge=None)
    assert hit.fraction_q16 == 0
    # The top breakpoint itself is a hit, not a clamp.
    assert search_axis(LOAD, 0x80) == AxisHit(index=7, numerator=0, span=1, edge=None)


def test_a_value_between_breakpoints_is_the_lower_index_and_a_q16_fraction() -> None:
    # 0x64..0x6C spans 8 counts; 0x68 is half way: (4 << 16) // 8.
    hit = search_axis(LOAD, 0x68)
    assert (hit.index, hit.fraction_q16, hit.edge) == (3, 32768, None)
    # 0x6C..0x6E spans 2: 0x6D is (1 << 16) // 2.
    assert search_axis(LOAD, 0x6D).fraction_q16 == 32768
    # 0x34..0x44 spans 16: 0x35 is 65536 // 16 = 4096; 0x43 is 15 x 4096.
    assert search_axis(LOAD, 0x35).fraction_q16 == 4096
    assert search_axis(LOAD, 0x43).fraction_q16 == 61440
    # rpm word 300 (3,750 rpm) sits 12 of 32 counts past 288: (12 << 16) // 32.
    assert search_axis(RPM, 300).fraction_q16 == 24576


def test_the_fraction_truncates_like_the_firmware_divide() -> None:
    # 0x44..0x5D spans 25: (1 << 16) / 25 = 2621.44 -> 2621, never 2622.
    assert search_axis(LOAD, 0x45).fraction_q16 == 2621
    # rpm word 129 is 1 of 32 counts past 128: 2048 exactly.
    assert search_axis(RPM, 129).fraction_q16 == 2048


def test_past_either_end_the_search_clamps_and_names_the_edge() -> None:
    below = search_axis(LOAD, 0x10)
    assert (below.index, below.fraction_q16, below.edge) == (0, 0, "below")
    above = search_axis(LOAD, 0xFF)
    assert (above.index, above.fraction_q16, above.edge) == (7, 0, "above")
    assert search_axis(RPM, 2000).edge == "above"


def test_an_empty_axis_or_a_non_finite_value_has_no_position() -> None:
    assert search_axis([], 3) is None
    assert search_axis(LOAD, math.nan) is None


def test_float_positions_come_from_the_same_search() -> None:
    hit = search_axis([1200.0, 1600.0, 2000.0], 1500.0)
    assert hit is not None and hit.position == pytest.approx(0.75)
    with pytest.raises(ValueError, match="integer"):
        _ = search_axis([1.5, 2.5], 2.0).fraction_q16  # type: ignore[union-attr]


# -- interpolation ----------------------------------------------------------------


def test_interpolation_truncates_toward_the_first_operand_both_ways() -> None:
    # 18 x 0.875 = 15.75: up from 128 truncates to +15 (nearest would be 16).
    assert interpolate_q16(128, 146, 57344) == 143
    # Down from 146 truncates toward 146: 146 - 15 = 131 (nearest 130).
    assert interpolate_q16(146, 128, 57344) == 131
    # 3 x 0.333 = 0.99997: no step at all, in either direction.
    assert interpolate_q16(128, 131, 21845) == 128
    assert interpolate_q16(131, 128, 21845) == 131
    assert interpolate_q16(150, 150, 40000) == 150


# -- the 2-D lookup ---------------------------------------------------------------


def test_a_cell_on_both_breakpoints_reads_that_cell_alone() -> None:
    result = lookup_2d(TABLE, RPM, LOAD, 288, 0x6C)
    assert (result.value, result.cells) == (0x96, ((4, 4),))


def test_an_interior_point_reads_four_cells_rows_then_columns() -> None:
    # rpm 256 (3,200): row 3, half way to row 4. load 0x6D: column 4, half way.
    # Row 3: 0x8E -> 0x90 by half = 143. Row 4: 0x96 -> 0x95 by half = 150
    # (truncated toward 150). Then 143 -> 150 by half = 146.
    result = lookup_2d(TABLE, RPM, LOAD, 256, 0x6D)
    assert result.value == 146
    assert result.cells == ((3, 4), (3, 5), (4, 4), (4, 5))
    assert (result.row_fraction, result.column_fraction) == (32768, 32768)


def test_a_point_on_a_column_breakpoint_interpolates_down_the_column_only() -> None:
    # rpm word 408 is 56 of 64 counts past 352 (frac 57344). Column 0:
    # 0x80 -> 0x93 = 19 x 0.875 = 16.625, truncated: 144 (nearest: 145).
    result = lookup_2d(TABLE, RPM, LOAD, 408, 0x34)
    assert (result.value, result.cells) == (144, ((6, 0), (7, 0)))


def test_a_point_on_a_row_breakpoint_interpolates_along_the_row_only() -> None:
    # Row 6 (rpm 352), load 0x45: 0x8B -> 0x97 by 2621/65536 = 12 x 0.04 = 0.48 -> 0.
    result = lookup_2d(TABLE, RPM, LOAD, 352, 0x45)
    assert (result.value, result.cells) == (0x8B, ((6, 1), (6, 2)))


def test_edges_clamp_to_the_last_row_and_column() -> None:
    # Past the top of both axes: the corner, with no interpolation.
    corner = lookup_2d(TABLE, RPM, LOAD, 600, 0xFF)
    assert (corner.value, corner.cells) == (0x9F, ((7, 7),))
    assert (corner.row.edge, corner.column.edge) == ("above", "above")
    # Below both: the first cell.
    assert lookup_2d(TABLE, RPM, LOAD, 10, 0).value == 0x80
    # The pedal-enrichment branch: load 0xFF reads the last column at any rpm
    # between rows 1 and 4 as 0x95 (phi 1.1640625).
    for rpm_word in (128, 236, 276, 288):
        assert lookup_2d(TABLE, RPM, LOAD, rpm_word, 0xFF).value == 0x95


def test_a_fractional_coordinate_is_refused_not_truncated() -> None:
    with pytest.raises(ValueError, match="whole raw count"):
        lookup_2d(TABLE, RPM, LOAD, 236, 25.0859)  # type: ignore[arg-type]


def test_a_table_that_does_not_fit_its_axes_is_refused() -> None:
    with pytest.raises(ValueError, match="shape"):
        lookup_2d(TABLE[:7], RPM, LOAD, 236, 0x40)


# -- one kernel ---------------------------------------------------------------------


def test_the_rom_maps_ecm_cell_uses_this_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """W05.1's ECM cell and the twin share one axis search (E02.4)."""
    calls: list[float] = []
    real = ecm_lookup.search_axis

    def spy(breakpoints, value):  # type: ignore[no-untyped-def]
        calls.append(value)
        return real(breakpoints, value)

    monkeypatch.setattr(rom_trace, "search_axis", spy)
    axes = rom_trace.TraceAxes(
        x=[float(v) for v in LOAD],
        y=[1200.0, 1600.0],
        x_display=[0.0] * 8,
        y_display=[1200.0, 1600.0],
    )
    cell = rom_trace.ecm_cell(axes, 0x68, 1400.0, clamp=True)
    assert cell is not None and (cell.column, cell.row) == (3, 0)
    assert calls == [0x68, 1400.0]
