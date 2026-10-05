"""N02.2: the 1-D lookup routines and the ``0xFFFF118A`` chain, on synthetic tables.

Every table here is invented (round numbers, rising), never a value read from
a ROM: the routines are firmware arithmetic and must be right for any table.
Each expected count was worked by hand from the routines as decoded
(``src/decode/fuel_comp_load.py`` names each address), and several sit where
rounding to nearest would give a different count than the firmware's
truncation. The real-image known answer is ``tests/test_fuel_comp_inputs.py``.
"""

from __future__ import annotations

import itertools

import pytest

from src.decode.ecm_lookup import (
    SHIFT12_LAST_ENTRY_FROM,
    interpolate_q16,
    lookup_curve_q16,
    lookup_shift12_q16,
)
from src.decode.fuel_comp_load import (
    X_SPLIT,
    Interval,
    LoadInputCurves,
    columns_read,
    counts_from_truncated_byte,
    effective_area,
    load_byte,
    load_x,
    pid_byte_from_percent,
    purge_area_from_command_byte,
    purge_area_unknown,
    reconstruct,
    reconstruct_interval,
    rpm_reciprocal,
    spans_one_cell,
    throttle_area,
)

#: Synthetic, strictly rising: 0, 100, ..., 3100.
AREA_AXIS = tuple(100 * i for i in range(32))
#: Synthetic, rising and convex: 200 + 30 i^2.
AREA_VALUES = tuple(200 + 30 * i * i for i in range(32))
#: Synthetic fill curves; the low curve ends where the high one starts.
FILL_LOW = (
    *(0, 8000, 16000, 22000, 27000, 31000, 34000, 36500),
    *(38500, 40000, 41200, 42200, 43000, 43600, 44000, 44000),
)
FILL_HIGH = (*range(44000, 57001, 1000), 57344, 57344)


def curves(**overrides: object) -> LoadInputCurves:
    fields: dict[str, object] = {
        "area_breakpoints": AREA_AXIS,
        "area_values": AREA_VALUES,
        "fill_low": FILL_LOW,
        "fill_high": FILL_HIGH,
        "rpm_multiplier": 150,
        "reciprocal_numerator": 10_000_000,
        "idle_air_cap": 1000,
        "limp_home_area": 700,
    }
    fields.update(overrides)
    return LoadInputCurves(**fields)  # type: ignore[arg-type]


# -- 0x026D58 / 0x027154: a word curve on its own word axis ----------------------


def test_a_curve_on_a_breakpoint_reads_that_entry() -> None:
    assert lookup_curve_q16(AREA_VALUES, AREA_AXIS, 0) == 200
    assert lookup_curve_q16(AREA_VALUES, AREA_AXIS, 500) == 200 + 30 * 25
    assert lookup_curve_q16(AREA_VALUES, AREA_AXIS, 3100) == AREA_VALUES[-1]


def test_a_curve_between_breakpoints_interpolates_and_truncates() -> None:
    # 150 is half way from 100 (230) to 200 (320): fraction (50 << 16) // 100
    # = 32768, 230 + (90 * 32768 >> 16) = 275.
    assert lookup_curve_q16(AREA_VALUES, AREA_AXIS, 150) == 275
    # 101: fraction (1 << 16) // 100 = 655; 90 * 655 >> 16 = 0, so 230, where
    # rounding the true 230.9 would give 231.
    assert lookup_curve_q16(AREA_VALUES, AREA_AXIS, 101) == 230
    # 199: fraction 64880; 90 * 64880 >> 16 = 89 (true 89.1).
    assert lookup_curve_q16(AREA_VALUES, AREA_AXIS, 199) == 319


def test_a_curve_clamps_past_its_last_breakpoint_and_never_extrapolates() -> None:
    assert lookup_curve_q16(AREA_VALUES, AREA_AXIS, 3101) == AREA_VALUES[-1]
    assert lookup_curve_q16(AREA_VALUES, AREA_AXIS, 0xFFFF) == AREA_VALUES[-1]
    shifted = tuple(b + 50 for b in AREA_AXIS)
    assert lookup_curve_q16(AREA_VALUES, shifted, 0) == AREA_VALUES[0]


def test_a_curve_falling_between_entries_truncates_toward_the_first() -> None:
    falling = tuple(reversed(AREA_VALUES))
    # 31 -> 30 entries: from 29,030 toward 27,200 by half: 29,030 - (1,830 * 32768 >> 16).
    assert lookup_curve_q16(falling, AREA_AXIS, 50) == AREA_VALUES[-1] - (1830 * 32768 >> 16)


@pytest.mark.parametrize("bad", [1.5, -1, 0x10000])
def test_a_curve_input_that_is_not_a_word_is_refused(bad: float) -> None:
    with pytest.raises(ValueError):
        lookup_curve_q16(AREA_VALUES, AREA_AXIS, bad)  # type: ignore[arg-type]


def test_a_curve_needs_one_entry_per_breakpoint() -> None:
    with pytest.raises(ValueError, match="one entry per breakpoint"):
        lookup_curve_q16(AREA_VALUES[:-1], AREA_AXIS, 10)


# -- 0x026C28: a 16-entry table indexed by x >> 12 --------------------------------


def test_shift12_reads_entries_at_multiples_of_4096() -> None:
    for index in range(15):
        assert lookup_shift12_q16(FILL_HIGH, index << 12) == FILL_HIGH[index]


def test_shift12_fraction_is_the_low_12_bits_moved_up_to_q16() -> None:
    # 0x0800: fraction 0x800 << 4 = 32768, half of 0 -> 8000.
    assert lookup_shift12_q16(FILL_LOW, 0x0800) == 4000
    # 0x0001: fraction 16; 8000 * 16 >> 16 = 1 (true 1.95, truncated).
    assert lookup_shift12_q16(FILL_LOW, 0x0001) == 1
    # 0xEFFF: index 14, fraction 0xFFF0, between 44000 and 44000.
    assert lookup_shift12_q16(FILL_LOW, 0xEFFF) == 44000
    # 0x1FFF: index 1, fraction 0xFFF0: 8000 + (8000 * 65520 >> 16) = 15998.
    assert lookup_shift12_q16(FILL_LOW, 0x1FFF) == 15998


def test_shift12_from_0xf000_reads_the_last_entry_outright() -> None:
    table = (*FILL_HIGH[:15], 60000)
    assert lookup_shift12_q16(table, SHIFT12_LAST_ENTRY_FROM - 1) == interpolate_q16(
        table[14], table[15], 0xFFF0
    )
    assert lookup_shift12_q16(table, SHIFT12_LAST_ENTRY_FROM) == 60000
    assert lookup_shift12_q16(table, 0xFFFF) == 60000


def test_shift12_needs_sixteen_entries_and_a_word_input() -> None:
    with pytest.raises(ValueError, match="16-entry"):
        lookup_shift12_q16(FILL_LOW[:15], 0)
    with pytest.raises(ValueError):
        lookup_shift12_q16(FILL_LOW, 0x10000)


# -- the chain ---------------------------------------------------------------------


def test_rpm_reciprocal_saturates_the_product_and_clamps_the_quotient() -> None:
    c = curves()
    # 150 x 80 = 12,000; 10,000,000 // 12,000 = 833.
    assert rpm_reciprocal(80, c) == 833
    # 150 x 437 = 65,550 > 0xFFFF: the product pins at 65,535, 10e6 // 65,535 = 152.
    assert rpm_reciprocal(437, c) == 152
    assert rpm_reciprocal(1000, c) == 152  # above saturation X stops falling with rpm
    # A zero product gives 0, not a division by zero.
    assert rpm_reciprocal(0, c) == 0
    # A quotient past a word clamps: 10e6 // 150 = 66,666.
    assert rpm_reciprocal(1, c) == 0xFFFF


def test_throttle_area_reads_the_curve_at_four_times_the_counts() -> None:
    c = curves()
    assert throttle_area(25, c) == lookup_curve_q16(AREA_VALUES, AREA_AXIS, 100)
    assert throttle_area(0x4000, c) == AREA_VALUES[-1]  # 4 x 0x4000 clamps to 0xFFFF


def test_effective_area_clamps_at_zero_and_reads_idle_air_signed() -> None:
    c = curves()
    assert effective_area(500, 120, c) == 380
    assert effective_area(100, 120, c) == 0
    assert effective_area(500, -80, c) == 580
    with pytest.raises(ValueError, match="outside what the ECM can hold"):
        effective_area(500, 1001, c)  # above the cap the code applies


def test_x_skips_the_purge_term_when_the_area_term_saturates() -> None:
    # (1000 x 500) >> 9 = 976; (500 x 80) >> 11 = 19.
    assert load_x(1000, 80, 500) == 976 + 19
    # (40000 x 900) >> 9 = 70,312 >= 0xFFFF: X is 0xFFFF and purge is not added.
    assert load_x(40000, 80, 900) == 0xFFFF
    # Below saturation the sum still clamps: 65,000 + 1,000 > 0xFFFF.
    first = (65000 * 512) >> 9
    assert first == 65000
    assert load_x(65000, 2048 * 1000 // 512, 512) == 0xFFFF


def test_the_byte_switches_curves_at_0x3c00() -> None:
    c = curves()
    below = X_SPLIT - 1
    assert load_byte(below, c) == lookup_shift12_q16(FILL_LOW, 4 * below) >> 8
    assert load_byte(X_SPLIT, c) == FILL_HIGH[0] >> 8
    assert load_byte(X_SPLIT + 0x1800, c) == lookup_shift12_q16(FILL_HIGH, 0x1800) >> 8
    # X - 0x3C00 is at most 0xC3FF, so the high curve is read no further than
    # between entries 12 and 13: the entries past 13 are unreachable.
    assert load_byte(0xFFFF, c) == interpolate_q16(FILL_HIGH[12], FILL_HIGH[13], 0x3FF0) >> 8
    assert load_byte(0xFC00, c) == FILL_HIGH[12] >> 8


def test_reconstruct_end_to_end_by_hand() -> None:
    c = curves()
    trace = reconstruct(throttle_counts=50, rpm_word=64, idle_air=100, purge_area=40, curves=c)
    # w116A = 10e6 // (150 x 64) = 1041.
    assert trace.w116a == 1041
    # Area at 200: 200 + 30 x 4 = 320; minus 100 idle air = 220.
    assert (trace.area, trace.w1124) == (320, 220)
    # X = (1041 x 220) >> 9 + (1041 x 40) >> 11 = 447 + 20 = 467.
    assert trace.x == 467
    # 4X = 1868: index 0, fraction 1868 << 4 = 29888; 8000 x 29888 >> 16 = 3648.
    assert trace.byte == 3648 >> 8 == 14
    limp = reconstruct(
        throttle_counts=50, rpm_word=64, idle_air=100, purge_area=40, curves=c, limp_home=True
    )
    assert limp.w1124 == 700  # the limp-home area replaces the throttle term


def test_the_interval_is_exactly_the_extremes_over_every_combination() -> None:
    """Monotone by construction, so the two ends bound every combination."""
    c = curves()
    throttle, idle, purge = Interval(30, 34), Interval(60, 200), Interval(0, 120)
    for word in (56, 64, 160, 300):
        bounds = reconstruct_interval(
            throttle_counts=throttle, rpm_word=word, idle_air=idle, purge_area=purge, curves=c
        )
        seen = [
            reconstruct(throttle_counts=t, rpm_word=word, idle_air=i, purge_area=p, curves=c).byte
            for t, i, p in itertools.product(
                range(throttle.lo, throttle.hi + 1),
                [*range(idle.lo, idle.hi, 7), idle.hi],
                [*range(purge.lo, purge.hi, 11), purge.hi],
            )
        ]
        assert bounds.low.byte == min(seen)
        assert bounds.high.byte == max(seen)


def test_curves_that_would_break_monotonicity_are_refused() -> None:
    with pytest.raises(ValueError, match="rise strictly"):
        curves(area_breakpoints=(0, 0, *AREA_AXIS[2:]))
    with pytest.raises(ValueError, match="area_values falls"):
        curves(area_values=(300, 200, *AREA_VALUES[2:]))
    with pytest.raises(ValueError, match="not monotone in X"):
        curves(fill_high=(40000, *FILL_HIGH[1:]))
    with pytest.raises(ValueError, match="16-bit word"):
        curves(fill_low=(*FILL_LOW[:15], 70000))
    with pytest.raises(ValueError, match="the ECM reads 16"):
        curves(fill_low=FILL_LOW[:15])


# -- inputs from what was logged ---------------------------------------------------


def test_pid_bytes_round_trip_from_their_stored_percent() -> None:
    for byte in range(256):
        assert pid_byte_from_percent(byte * 100 / 255) == byte
    with pytest.raises(ValueError, match="not a whole Mode 01 byte"):
        pid_byte_from_percent(50.1)


def test_truncated_byte_inverse_holds_every_count_and_no_other() -> None:
    """``A = clamp_u8(floor(max(0, w) x 100 / 321))``, exhaustively over 10 bits."""
    for byte in range(256):
        interval = counts_from_truncated_byte(
            byte, numerator=100, denominator=321, full_scale=255, counts_max=1023
        )
        expected = [w for w in range(1024) if min(255, w * 100 // 321) == byte]
        assert expected, byte
        assert (interval.lo, interval.hi) == (min(expected), max(expected)), byte
    # 3 or 4 counts per byte below the clamp, never one.
    widths = {
        counts_from_truncated_byte(
            b, numerator=100, denominator=321, full_scale=255, counts_max=1023
        ).hi
        - counts_from_truncated_byte(
            b, numerator=100, denominator=321, full_scale=255, counts_max=1023
        ).lo
        + 1
        for b in range(255)
    }
    assert widths == {3, 4}


def test_purge_area_from_the_command_byte() -> None:
    assert purge_area_from_command_byte(0, gain=128, gain_shift=7) == Interval(0, 0)  # off
    assert purge_area_from_command_byte(1, gain=128, gain_shift=7) == Interval(0, 0)  # raw 0
    # raw 9 at gain 150: 9 x 150 >> 7 = 10 (true 10.5, truncated).
    assert purge_area_from_command_byte(10, gain=150, gain_shift=7) == Interval(10, 10)
    # 255 is raw 254, or 255 and up with the low byte wrapping: every value.
    assert purge_area_from_command_byte(255, gain=150, gain_shift=7) == Interval(0, 298)
    assert purge_area_unknown(gain=150, gain_shift=7) == Interval(0, 298)


def test_columns_read_and_cell_uniqueness() -> None:
    axis = (16, 32, 48, 64)
    assert columns_read(axis, 40) == (1, 2)
    assert columns_read(axis, 32) == (1, 1)
    assert columns_read(axis, 10) == (0, 0)  # below the first: the first column
    assert columns_read(axis, 99) == (3, 3)  # past the last: the last column
    assert spans_one_cell(axis, Interval(33, 47))
    assert spans_one_cell(axis, Interval(32, 48))  # reads columns 1..2 only
    assert not spans_one_cell(axis, Interval(30, 50))
