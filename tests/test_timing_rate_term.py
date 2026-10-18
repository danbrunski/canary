"""The base timing term 0xFFFF27C6 and the inputs of 0xFFFF279A (E09.7, E05.4).

Hand-worked vectors: every expected value below was computed by hand from the
firmware arithmetic (firmware notes 2026-10-17; manifest nodes
``base_timing_load_curve`` .. ``timing_term_279a_sum``), never by calling the
function under test. The tables are inlined: they are identical on ROM Store
ZS44A, k106, 20260920 and 20261004.
"""

from __future__ import annotations

import pytest

from src.analysis.ecu_state_models import (
    BaseTimingCals,
    BaseTimingTables,
    byte_curve8,
    coolant_rescaled,
    timing_26d0_curves,
    timing_charge_map_10eb,
    timing_charge_term_17c6,
    timing_charge_word_1b24,
    timing_load_curve_19cb,
    timing_rate_word_15de,
    timing_term_26d0,
    timing_term_26d9,
    timing_term_27c6,
    timing_term_27c6_chain,
    timing_term_27c6_steady,
    timing_term_279a,
    timing_term_1512,
)
from src.analysis.paired_scenarios import coolant_rescaled_byte

#: 0x9AF9: rows the rpm axis 0xC890 (compared with the rpm/50 byte), columns
#: the X axis 0xC88C.
RATE_9AF9 = (
    (0x11, 0x1A, 0x28, 0x3A),
    (0x19, 0x27, 0x3C, 0x55),
    (0x1B, 0x2A, 0x42, 0x5E),
    (0x1D, 0x2D, 0x46, 0x64),
)
#: 0x9B89: same axes, the rpm axis compared as byte x 4 with the rpm word.
CHARGE_9B89 = (
    (0x6C, 0x6C, 0x67, 0x64),
    (0x64, 0x64, 0x6E, 0x7F),
    (0x84, 0x84, 0x8D, 0xA0),
    (0x76, 0x76, 0x8E, 0x9C),
)
RPM_C890 = (0x08, 0x38, 0x58, 0x78)
LOAD_C88C = (0x10, 0x20, 0x40, 0x70)
CURVE_CD9B = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0)
FLAT_80 = (0x80,) * 8  # 0xCEAB and 0xD60F
COOLANT_CURVE = (0x67, 0x6C, 0x76, 0x78, 0x78, 0x7B, 0x80, 0x80)  # 0xCEBB and 0xD9AF

TABLES = BaseTimingTables(
    rate_map_9af9=RATE_9AF9,
    charge_map_9b89=CHARGE_9B89,
    rpm_axis_c890=RPM_C890,
    load_axis_c88c=LOAD_C88C,
    load_curve_cd9b=CURVE_CD9B,
    coolant_curve_cebb=COOLANT_CURVE,
    charge_curve_ceab=FLAT_80,
    coolant_curve_d9af=COOLANT_CURVE,
    charge_curve_d60f=FLAT_80,
)
#: 0x880D, 0x880E, 0x8440, 0x8B0C.
CALS = BaseTimingCals(cal_880d=179, cal_880e=25, cal_8440=32, cal_8b0c=14)

#: Hot (rescaled coolant byte 208 = 90 degC), phi 1 (0xFFFF1B24 = 0x4000),
#: 0xFFFF1512 = 0 (cal 0x8538 = 0). Common to the three operating points:
#:   15DC = 1CC9 = curve8(COOLANT_CURVE, 208): index 6, frac 16 << 11 = 32768,
#:     128 -> 128 = 128.
#:   r = (0x4000 - 0x2000) >> 6 = 128; 15D7 = 19B6 = 0x80 (flat) = 128.
#:   q = trunc(16384 / 29) = 564 (29 x 564 = 16356); 15F7 = 564 >> 5 = 17.
#:   15DA = 15D7 x 15DC x 15DB >> 14 = 15DB (128 x 128 = 1 << 14).
#:   15DE = S x 128 x 128 >> 14 = S.
HOT = {"coolant_1077": 208, "w1b24": 0x4000, "w1512": 0}

# fmt: off
STEADY = [
    # 700 rpm: rpm word 56, rpm/50 = 56 >> 2 = 14, X = 32.
    #   15DB: rpm/50 14 on 8,56,88,120 -> i 0, frac (6 << 16) // 48 = 8192;
    #     X 32 = column 1 exactly; 26 + (13 x 8192 >> 16 = 1) = 27. 15DA = 27.
    #   10EB: word 56 on 32,224,352,480 -> i 0, frac (24 << 16) // 192 = 8192;
    #     column 1; 108 - (8 x 8192 >> 16 = 1) = 107.
    #   R = 107 << 7 = 13696; 0x4000 - R = 2688; M = 32 x 2688 = 86016;
    #     M >> 8 = 336; F = 4096 - 336 = 3760.
    #   S = trunc(27 x 3760 / 1000 = 101.52) = 101 + trunc((25 x 56 << 6) / 625
    #     = 89600 / 625 = 143.36) = 143 -> 244. 15DE = 244.
    #   17C6 = trunc(32 x (0 + 17 + trunc(32768 / 107 = 306.2) = 306) / 25)
    #     = trunc(10336 / 25 = 413.44) = 413.  19CB = 32.
    #   t = floor(179 x 120 x 413 / (244 x 32)) = floor(8871240 / 7808) = 1136;
    #     u = 1136 + 35 = 1171.
    #   v = floor(((56 x 1171 = 65576) >> 10 = 64) x 3 / 5 = 38.4) = 38; 27C6 = 24.
    (56, 32, 27, 107, 3760, 244, 413, 32, 1136, 24),
    # 2,500 rpm: rpm word 200, rpm/50 50, X 64.
    #   15DB: 50 -> i 0, frac (42 << 16) // 48 = 57344; column 2;
    #     40 + (20 x 57344 >> 16 = 17) = 57.
    #   10EB: 200 -> i 0, frac (168 << 16) // 192 = 57344; column 2;
    #     103 + (7 x 57344 >> 16 = 6) = 109.
    #   R = 13952; 0x4000 - R = 2432; M = 77824; >> 8 = 304; F = 3792.
    #   S = trunc(57 x 3792 / 1000 = 216.14) = 216 + (25 x 200 << 6) / 625 = 512
    #     -> 728.
    #   17C6 = trunc(64 x (17 + trunc(32768 / 109 = 300.6) = 300) / 25)
    #     = trunc(20288 / 25 = 811.52) = 811.  19CB = 64.
    #   t = floor(21480 x 811 / (728 x 64)) = floor(17420280 / 46592) = 373; u = 408.
    #   v = floor(((200 x 408 = 81600) >> 10 = 79) x 3 / 5 = 47.4) = 47; 27C6 = 33.
    (200, 64, 57, 109, 3792, 728, 811, 64, 373, 33),
    # 4,400 rpm: rpm word 352, rpm/50 88, X 112.
    #   15DB: 88 = row 2, 112 = column 3, both exact: 0x5E = 94.
    #   10EB: 352 = 0x58 x 4 = row 2, column 3: 0xA0 = 160.
    #   R = 160 << 7 = 20480 > 0x4000: 0x4000 - R = -4096; M = -131072;
    #     M >>a 8 = -512; F = 4096 + 512 = 4608.
    #   S = trunc(94 x 4608 / 1000 = 433.15) = 433 + trunc(563200 / 625 = 901.12)
    #     = 901 -> 1334.
    #   17C6 = trunc(112 x (17 + trunc(32768 / 160 = 204.8) = 204) / 25)
    #     = trunc(24752 / 25 = 990.08) = 990.
    #   19CB: 112 -> index 3, frac 16 << 11 = 32768: 96 + (32 x 32768 >> 16) = 112.
    #   t = floor(21480 x 990 / (1334 x 112)) = floor(21265200 / 149408) = 142;
    #     u = 177.
    #   v = floor(((352 x 177 = 62304) >> 10 = 60) x 3 / 5) = 36; 27C6 = 22.
    (352, 112, 94, 160, 4608, 1334, 990, 112, 142, 22),
]
# fmt: on


@pytest.mark.parametrize(
    ("rpm_word", "x", "b15db", "b10eb", "factor", "w15de", "w17c6", "b19cb", "t", "term"),
    STEADY,
)
def test_the_base_timing_term_at_three_hot_operating_points(
    rpm_word: int,
    x: int,
    b15db: int,
    b10eb: int,
    factor: int,
    w15de: int,
    w17c6: int,
    b19cb: int,
    t: int,
    term: int,
) -> None:
    result = timing_term_27c6_steady(rpm_word=rpm_word, x=x, tables=TABLES, cals=CALS, **HOT)
    assert result.rpm50 == rpm_word >> 2
    assert result.b10eb == b10eb
    assert result.b19cb == b19cb
    assert (result.charge.w17c6, result.charge.b15f7) == (w17c6, 17)
    rate = result.rate
    assert (rate.b15dc, rate.b1cc9, rate.charge_index, rate.b15d7, rate.b19b6) == (
        128,
        128,
        128,
        128,
        128,
    )
    assert (rate.b15db, rate.b15da, rate.factor, rate.rate, rate.w15de) == (
        b15db,
        b15db,
        factor,
        w15de,
        w15de,
    )
    assert result.term == term
    # The stage by itself, on the same words, and t recovered from the term's
    # own inputs (u = t + 35 is inside the first branch here).
    assert (
        timing_term_27c6(
            w17c6=w17c6, w15de=w15de, b19cb=b19cb, rpm_word=rpm_word, cal_880d=179, cal_8b0c=14
        )
        == term
    )
    assert (179 * 120 * w17c6) // (w15de * b19cb) == t


def test_the_chain_on_snapshot_bytes_is_the_steady_composition_when_they_agree() -> None:
    steady = timing_term_27c6_steady(rpm_word=200, x=64, tables=TABLES, cals=CALS, **HOT)
    chain = timing_term_27c6_chain(
        rpm_word_11d8=200, rpm50_11ca=50, x_103e=64, b10eb=109, tables=TABLES, cals=CALS, **HOT
    )
    assert chain == steady
    # A stale 0xFFFF10EB (last cycle's lookup) moves the term: 10EB = 100 gives
    # 17C6 = trunc(64 x (17 + 327) / 25) = trunc(880.64) = 880; R = 12800,
    # F = 4096 - (32 x 3584 >> 8 = 448) = 3648; S = trunc(57 x 3648 / 1000 =
    # 207.9) + 512 = 719; t = floor(21480 x 880 / (719 x 64)) = floor(18902400
    # / 46016 = 410.78) = 410; u = 445; v = ((200 x 445 = 89000) >> 10 = 86)
    # x 3 / 5 = 51.6 -> 51; 27C6 = 37.
    stale = timing_term_27c6_chain(
        rpm_word_11d8=200, rpm50_11ca=50, x_103e=64, b10eb=100, tables=TABLES, cals=CALS, **HOT
    )
    assert (stale.charge.w17c6, stale.rate.factor, stale.rate.w15de, stale.term) == (
        880,
        3648,
        719,
        37,
    )


def test_a_zero_charge_map_byte_saturates_17c6() -> None:
    # 0xFFFF10EB = 0: no reciprocal, 17C6 = 0xFFFF; 15F7 is still q >> 5.
    term = timing_charge_term_17c6(x_103e=64, w1b24=0x4000, b10eb=0, w1512=0)
    assert (term.w17c6, term.b15f7) == (0xFFFF, 17)
    # And 0x04BA6C's ratio is 0, so F = 0x1000 - (32 x 0x4000 >> 8) = 2048.
    rate = timing_rate_word_15de(
        coolant_1077=208,
        w1b24=0x4000,
        rpm50_11ca=50,
        x_103e=64,
        rpm_word_11d8=200,
        b10eb=0,
        w1512=0,
        rate_map_9af9=RATE_9AF9,
        rpm_axis_c890=RPM_C890,
        load_axis_c88c=LOAD_C88C,
        coolant_curve_cebb=COOLANT_CURVE,
        charge_curve_ceab=FLAT_80,
        coolant_curve_d9af=COOLANT_CURVE,
        charge_curve_d60f=FLAT_80,
        cal_8440=32,
        cal_880e=25,
    )
    assert (rate.ratio, rate.factor) == (0, 2048)


def test_17c6_caps_at_a_word() -> None:
    # 1512 = 0xFFFF: (0xFFFF >> 7 = 511) + 17 + 32768 = 33296; x 255 / 25 =
    # 339619 -> 0xFFFF.
    assert timing_charge_term_17c6(x_103e=255, w1b24=0x4000, b10eb=1, w1512=0xFFFF).w17c6 == 0xFFFF


def _rate(**overrides: int) -> int:
    args = {
        "coolant_1077": 208,
        "w1b24": 0x4000,
        "rpm50_11ca": 50,
        "x_103e": 64,
        "rpm_word_11d8": 200,
        "b10eb": 109,
        "w1512": 0,
        "cal_8440": 32,
        "cal_880e": 25,
    } | overrides
    return timing_rate_word_15de(
        rate_map_9af9=RATE_9AF9,
        rpm_axis_c890=RPM_C890,
        load_axis_c88c=LOAD_C88C,
        coolant_curve_cebb=COOLANT_CURVE,
        # A non-flat charge curve so the index shows: entry k = 16 x k.
        charge_curve_ceab=(0, 16, 32, 48, 64, 80, 96, 112),
        coolant_curve_d9af=COOLANT_CURVE,
        charge_curve_d60f=FLAT_80,
        **args,
    ).charge_index


@pytest.mark.parametrize(
    ("w1b24", "index"),
    [
        (0x0000, 0),
        (0x1FFF, 0),  # below 0x2000: 0
        (0x2000, 0),  # (0) >> 6
        (0x2040, 1),
        (0x5FFF, 255),  # 0x3FFF >> 6 = 255
        (0x6000, 255),  # from 0x6000: 0xFF
        (0xFFFF, 255),
    ],
)
def test_the_charge_index_band(w1b24: int, index: int) -> None:
    assert _rate(w1b24=w1b24) == index


def test_the_charge_index_reaches_the_curve() -> None:
    # 1B24 = 0x3000: r = 0x1000 >> 6 = 64 -> index 2, frac 0: 15D7 = 32.
    # 15DA = 32 x 128 x 57 >> 14 = 14 (233472 / 16384 = 14.25).
    rate = timing_rate_word_15de(
        coolant_1077=208,
        w1b24=0x3000,
        rpm50_11ca=50,
        x_103e=64,
        rpm_word_11d8=200,
        b10eb=109,
        w1512=0,
        rate_map_9af9=RATE_9AF9,
        rpm_axis_c890=RPM_C890,
        load_axis_c88c=LOAD_C88C,
        coolant_curve_cebb=COOLANT_CURVE,
        charge_curve_ceab=(0, 16, 32, 48, 64, 80, 96, 112),
        coolant_curve_d9af=COOLANT_CURVE,
        charge_curve_d60f=FLAT_80,
        cal_8440=32,
        cal_880e=25,
    )
    assert (rate.charge_index, rate.b15d7, rate.b15da) == (64, 32, 14)


def test_the_factor_uses_an_arithmetic_shift() -> None:
    # Cal 0x8440 = 1 and 10EB = 0x81 (1512 = 0): R = 0x81 << 7 = 16512,
    # 0x4000 - R = -128, M = -128; M >>a 8 = -1 (a truncating divide would give
    # 0), F = 0x1000 + 1 = 4097.
    rate = timing_rate_word_15de(
        coolant_1077=208,
        w1b24=0x4000,
        rpm50_11ca=50,
        x_103e=64,
        rpm_word_11d8=200,
        b10eb=0x81,
        w1512=0,
        rate_map_9af9=RATE_9AF9,
        rpm_axis_c890=RPM_C890,
        load_axis_c88c=LOAD_C88C,
        coolant_curve_cebb=COOLANT_CURVE,
        charge_curve_ceab=FLAT_80,
        coolant_curve_d9af=COOLANT_CURVE,
        charge_curve_d60f=FLAT_80,
        cal_8440=1,
        cal_880e=25,
    )
    assert (rate.ratio, rate.factor) == (16512, 4097)


def test_the_ratio_with_a_nonzero_1512() -> None:
    # 10EB = 100, 1512 = 0x4000: divisor ((0x4000 x 100) >> 9 = 3200) + 8192 =
    # 11392; R = floor((100 << 20 = 104857600) / 11392) = 9204 (11392 x 9204 =
    # 104851968, remainder 5632); F = 4096 - (32 x (16384 - 9204) = 229760
    # >> 8 = 897) = 3199.
    rate = timing_rate_word_15de(
        coolant_1077=208,
        w1b24=0x4000,
        rpm50_11ca=50,
        x_103e=64,
        rpm_word_11d8=200,
        b10eb=100,
        w1512=0x4000,
        rate_map_9af9=RATE_9AF9,
        rpm_axis_c890=RPM_C890,
        load_axis_c88c=LOAD_C88C,
        coolant_curve_cebb=COOLANT_CURVE,
        charge_curve_ceab=FLAT_80,
        coolant_curve_d9af=COOLANT_CURVE,
        charge_curve_d60f=FLAT_80,
        cal_8440=32,
        cal_880e=25,
    )
    assert (rate.ratio, rate.factor) == (9204, 3199)


def test_a_zero_divisor_takes_the_ffff_time_and_the_second_branch() -> None:
    # 15DE x 19CB = 0: t = 0xFFFF, u = 0xFFFF + 35 >= 0x10000, so
    # v = trunc(((11D8 x 0xFFFF) >>a 10) x 3 / 5).
    # rpm word 3: 196605 >> 10 = 191; 573 / 5 = 114.6 -> 114; 27C6 = 100.
    # (u itself in the first branch would give (3 x 65570 = 196710) >> 10 = 192,
    # 576 / 5 = 115 -> 101: the branch is visible.)
    assert (
        timing_term_27c6(w17c6=500, w15de=0, b19cb=64, rpm_word=3, cal_880d=179, cal_8b0c=14) == 100
    )
    assert (
        timing_term_27c6(w17c6=500, w15de=728, b19cb=0, rpm_word=3, cal_880d=179, cal_8b0c=14)
        == 100
    )
    # rpm word 56: 3669960 >> 10 = 3583; x 3 / 5 = 2149 > 141 -> 127.
    assert (
        timing_term_27c6(w17c6=500, w15de=0, b19cb=0, rpm_word=56, cal_880d=179, cal_8b0c=14) == 127
    )


def test_x_zero_saturates_the_term_through_19cb() -> None:
    # X = 0: 19CB = 0 and 17C6 = 0, so t = 0xFFFF, and at 700 rpm v = 2149: 127.
    result = timing_term_27c6_steady(rpm_word=56, x=0, tables=TABLES, cals=CALS, **HOT)
    assert (result.b19cb, result.charge.w17c6, result.term) == (0, 0, 127)


def test_the_first_branch_caps_above_141_and_goes_negative_near_zero() -> None:
    # t = floor(179 x 120 x 1000 / (100 x 107)) = floor(21480000 / 10700 =
    # 2007.5) = 2007, u = 2042; rpm word 80: (80 x 2042 = 163360)
    # >> 10 = 159; x 3 / 5 = 95.4 -> 95; 27C6 = 81.
    assert (
        timing_term_27c6(w17c6=1000, w15de=100, b19cb=107, rpm_word=80, cal_880d=179, cal_8b0c=14)
        == 81
    )
    # rpm word 200: (200 x 2042 = 408400) >> 10 = 398; x 3 / 5 = 238 > 141: 127.
    assert (
        timing_term_27c6(w17c6=1000, w15de=100, b19cb=107, rpm_word=200, cal_880d=179, cal_8b0c=14)
        == 127
    )
    # v = 141 exactly is the last uncapped value: 141 - 14 = 127 either way;
    # v = 142 caps. rpm word 0: v = 0, 27C6 = -14.
    assert (
        timing_term_27c6(w17c6=1000, w15de=100, b19cb=107, rpm_word=0, cal_880d=179, cal_8b0c=14)
        == -14
    )
    # Small: t = 0 (17C6 = 0), u = 35; rpm word 56: (1960 >> 10 = 1) x 3 / 5 = 0;
    # 27C6 = -14. rpm word 300: (10500 >> 10 = 10) x 3 / 5 = 6; 27C6 = -8.
    assert (
        timing_term_27c6(w17c6=0, w15de=728, b19cb=64, rpm_word=56, cal_880d=179, cal_8b0c=14)
        == -14
    )
    assert (
        timing_term_27c6(w17c6=0, w15de=728, b19cb=64, rpm_word=300, cal_880d=179, cal_8b0c=14)
        == -8
    )


def test_v_141_and_142() -> None:
    # 17C6 = 0 makes t = 0, so cal 0x8B0C sets u: cal 94, u = 470 >> 1 = 235.
    # rpm word 1022: (240170 >> 10 = 234) x 3 / 5 = 140.4 -> v 140 -> 126.
    # rpm word 1029: (241815 >> 10 = 236) x 3 / 5 = 141.6 -> v 141 -> 127.
    # rpm word 1034: (242990 >> 10 = 237) x 3 / 5 = 142.2 -> v 142 -> capped 127.
    common = {"w17c6": 0, "w15de": 728, "b19cb": 64, "cal_880d": 179, "cal_8b0c": 94}
    assert timing_term_27c6(rpm_word=1022, **common) == 126
    assert timing_term_27c6(rpm_word=1029, **common) == 127
    assert timing_term_27c6(rpm_word=1034, **common) == 127


@pytest.mark.parametrize("rpm_word", [0x8001, 0xFFFF])
def test_a_negative_v_caps_through_the_unsigned_compare(rpm_word: int) -> None:
    # Second branch (15DE = 0): 0x8001 x 0xFFFF = 0x80007FFF, s32
    # -2147450881; >>a 10 = -2097121; x 3 = -6291363; sdiv 5 = -1258272.
    # 0xFFFF x 0xFFFF = 0xFFFE0001, s32 -131071; >>a 10 = -128; x 3 = -384;
    # sdiv 5 = -76. The compare with 141 is cmp/hi (0x04BD0A): a negative v is
    # a large unsigned value, so 127, never v - 14.
    assert (
        timing_term_27c6(w17c6=0, w15de=0, b19cb=0, rpm_word=rpm_word, cal_880d=179, cal_8b0c=14)
        == 127
    )


def test_the_rate_sum_divides_toward_zero_with_a_negative_factor() -> None:
    # Cal 0x8440 = 255, 10EB = 0: R = 0, M = 255 x 0x4000 = 4177920, M >> 8 =
    # 16320, F = s16(4096 - 16320) = -12224 (a plain 16-bit mask would give
    # 53312). Hot, rpm/50 50, X 64: 15DB = 15DA = 57 (as the 2,500 rpm vector).
    # 15DA x F = -696768: sdiv by 1000 truncates to -696 (floor: -697).
    # rpm word 300: (25 x 300 << 6) / 625 = 480000 / 625 = 768.
    # S = -696 + 768 = 72 (floor: 71; mask: 57 x 53312 / 1000 = 3038, + 768 =
    # 3806); 15DE = 72 x 128 x 128 >> 14 = 72.
    rate = timing_rate_word_15de(
        coolant_1077=208,
        w1b24=0x4000,
        rpm50_11ca=50,
        x_103e=64,
        rpm_word_11d8=300,
        b10eb=0,
        w1512=0,
        rate_map_9af9=RATE_9AF9,
        rpm_axis_c890=RPM_C890,
        load_axis_c88c=LOAD_C88C,
        coolant_curve_cebb=COOLANT_CURVE,
        charge_curve_ceab=FLAT_80,
        coolant_curve_d9af=COOLANT_CURVE,
        charge_curve_d60f=FLAT_80,
        cal_8440=255,
        cal_880e=25,
    )
    assert (rate.ratio, rate.factor, rate.b15da, rate.rate, rate.w15de) == (0, -12224, 57, 72, 72)


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        # Curve 0, 32, 64, 96, 128, 160, 192, 255 (the last step 63).
        (0, 0),
        (31, 31),  # index 0, frac 31 << 11 = 63488: 32 x 63488 >> 16 = 31
        (32, 32),
        (223, 253),  # index 6, frac 63488: 192 + (63 x 63488 >> 16 = 61) = 253
        (224, 255),  # from 224: the last entry outright
        (255, 255),
    ],
)
def test_byte_curve8_edges(x: int, expected: int) -> None:
    assert byte_curve8((0, 32, 64, 96, 128, 160, 192, 255), x) == expected


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        # Falling curve 200, 100, ...: truncation toward the first entry.
        (0, 200),
        (31, 104),  # 200 - (100 x 63488 >> 16 = 96) = 104
        (1, 197),  # 200 - (100 x 2048 >> 16 = 3) = 197
    ],
)
def test_byte_curve8_truncates_toward_the_first_entry(x: int, expected: int) -> None:
    assert byte_curve8((200, 100, 0, 0, 0, 0, 0, 0), x) == expected


def test_byte_curve8_refuses_a_16_entry_curve_and_a_word_input() -> None:
    with pytest.raises(ValueError, match="8-entry"):
        byte_curve8(tuple(range(16)), 10)
    with pytest.raises(ValueError, match="not a byte"):
        byte_curve8(CURVE_CD9B, 256)


def test_19cb_is_x_capped_at_224_on_these_images() -> None:
    assert [timing_load_curve_19cb(CURVE_CD9B, x) for x in (0, 17, 112, 223, 224, 255)] == [
        0,
        17,
        112,
        223,
        224,
        224,
    ]


def test_the_charge_map_compares_its_rpm_axis_as_byte_x_4() -> None:
    # rpm word 128 on 32, 224, 352, 480: i 0, frac (96 << 16) // 192 = 32768;
    # X 16 = column 0: 108 - (8 x 32768 >> 16 = 4) = 104. Compared with the
    # raw bytes (8, 56, ...) the word would sit past the top row.
    assert timing_charge_map_10eb(CHARGE_9B89, RPM_C890, LOAD_C88C, 128, 16) == 104
    # Above both axes: the corner, 0x9C.
    assert timing_charge_map_10eb(CHARGE_9B89, RPM_C890, LOAD_C88C, 600, 200) == 156


def test_1b24_and_1512() -> None:
    # (0x4800 - 0x0800) x 0x4000 >> 14 = 0x4000; below 26E8: 0.
    assert timing_charge_word_1b24(0x4800, 0x0800, 0x4000) == 0x4000
    assert timing_charge_word_1b24(0x0800, 0x4800, 0x4000) == 0
    # 0xFFFF x 0x8000 >> 14 = 0x1FFFE: capped.
    assert timing_charge_word_1b24(0xFFFF, 0, 0x8000) == 0xFFFF
    # (0x4000 x 0x4CCD) >> 14 = 0x4CCD (phi 1.2 enrichment).
    assert timing_charge_word_1b24(0x4000, 0, 0x4CCD) == 0x4CCD
    assert timing_term_1512(200, 0) == 0
    # mulu.w keeps the low 16 bits: 255 x 255 = 65025 fits; 0x1512 is a word.
    assert timing_term_1512(255, 255) == 65025


def test_279a_inputs_and_clamp() -> None:
    # 26D9 = 3630 x 15E5 >> 8: 255 x 128 = 32640 >> 8 = 127; map 0x9B09 = 0: 0.
    assert timing_term_26d9(255, 128) == 127
    assert timing_term_26d9(0, 255) == 0
    # 26D0: 255^4 = 4228250625 >> 24 = 252; 128 x 255^3 = 2122416000 >> 24 = 126.
    assert timing_term_26d0(255, 255, 255, 255) == 252
    assert timing_term_26d0(128, 255, 255, 255) == 126
    assert timing_term_26d0(255, 255, 255, 0) == 0
    # 279A = clamp(26D9 + 26D0 - (17FA >> 4), 0, 255).
    assert timing_term_279a(0, 0, 0) == 0
    assert timing_term_279a(0, 0, 255) == 0  # 0 - 15 clamps at 0
    assert timing_term_279a(10, 20, 0x50) == 25  # 30 - 5
    assert timing_term_279a(200, 100, 0) == 255
    assert timing_term_279a(127, 252, 0xFF) == 255  # 379 - 15 = 364


#: The 2026-10-17 image values: 0xCC33 is 0 except entries 6..9.
CURVE_CC33 = (0, 0, 0, 0, 0, 0, 0xFF, 0xFF, 0xFF, 0xFF, 0, 0, 0, 0, 0, 0)
CURVE_CC1B = (0, 0, 0, 0x60, 0xC4, 0xD0, 0xFF, 0x88, 0x50, 0, 0, 0, 0, 0, 0, 0)
CURVE_CC2B = (0x00, 0x02, 0x0B, 0x0C, 0x0A, 0x08, 0x03, 0x00)


def test_26d0_is_zero_on_a_warm_engine() -> None:
    # Rescaled coolant 160 (60 degC): curve16 0xCC33 index 10, frac 0: 0, so
    # 26D0 = 0 whatever the ramp and the other curves; and 279A = 0.
    warm = timing_26d0_curves(
        curve_cc1b=CURVE_CC1B,
        curve_cc2b=CURVE_CC2B,
        curve_cc33=CURVE_CC33,
        rpm25_11cf=0x50,
        w1038=0x1800,
        coolant_1077=160,
    )
    # 1301: 0x50 = index 5 exactly: 0xD0. 1302: 0x1800 >> 7 = 48 -> index 1,
    # frac 16 << 11 = 32768: 2 + (9 x 32768 >> 16 = 4) = 6.
    assert (warm.b1301, warm.b1302, warm.b1303) == (0xD0, 6, 0)
    assert timing_term_26d0(255, warm.b1301, warm.b1302, warm.b1303) == 0
    assert timing_term_279a(0, 0, 0x40) == 0
    # Rescaled coolant 159: index 9, frac 15 << 12 = 61440:
    # 255 - (255 x 61440 >> 16 = 239) = 16. 1038 = 0x3000: 12288 >> 7 = 96,
    # index 3 exactly: 1302 = 0x0C.
    cool = timing_26d0_curves(
        curve_cc1b=CURVE_CC1B,
        curve_cc2b=CURVE_CC2B,
        curve_cc33=CURVE_CC33,
        rpm25_11cf=0x50,
        w1038=0x3000,
        coolant_1077=159,
    )
    assert (cool.b1301, cool.b1302, cool.b1303) == (0xD0, 0x0C, 16)
    # 255 x 16 x 208 x 12 = 10183680 >> 24 = 0: the product needs all four large.
    assert timing_term_26d0(255, cool.b1301, cool.b1302, cool.b1303) == 0
    # BFS saturates the curve input at 255: 0xFFFF >> 7 = 511 -> 255 -> last entry 0.
    hot_bfs = timing_26d0_curves(
        curve_cc1b=CURVE_CC1B,
        curve_cc2b=CURVE_CC2B,
        curve_cc33=CURVE_CC33,
        rpm25_11cf=0,
        w1038=0xFFFF,
        coolant_1077=0x70,
    )
    assert (hot_bfs.b1301, hot_bfs.b1302, hot_bfs.b1303) == (0, 0, 0xFF)


@pytest.mark.parametrize(
    ("raw", "rescaled"),
    [
        (0, 0),
        (9, 0),  # below 10
        (10, 0),
        (11, 1),  # 240 / 150 = 1.6 -> 1
        (140, 208),  # 90 degC: 130 x 240 / 150 = 208
        (110, 160),  # 60 degC: 100 x 1.6 = 160
        (169, 254),  # 159 x 1.6 = 254.4 -> 254
        (170, 255),  # from 170: 255
        (255, 255),
    ],
)
def test_the_coolant_rescale(raw: int, rescaled: int) -> None:
    assert coolant_rescaled(raw) == rescaled


def test_the_rescaled_coolant_byte_from_a_temperature() -> None:
    # 90 degC -> 140 (degC + 50) -> 208; 60 degC -> 160; -40 degC -> 10 -> 0;
    # 120 degC -> 170 -> 255.
    assert [coolant_rescaled_byte(c) for c in (90.0, 60.0, -40.0, 120.0)] == [208, 160, 0, 255]
    # degC = byte x 0.625 - 40 recovers the temperature at whole steps.
    assert coolant_rescaled_byte(90.0) * 0.625 - 40 == 90.0
