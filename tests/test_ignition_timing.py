"""E09.7: ECU-side ignition timing in the paired simulator.

Two kinds of evidence, labelled per test:

- **synthetic (code)**: hand-worked from the decoded chain
  (``src.analysis.ecu_state_models``; ``ecu_application_model.yaml`` nodes
  ``timing_trim_lookup`` .. ``timing_final_byte``). They pin our reading of
  the code, not the engine.
- **private ROM**: the operator's images (``tests.rom_fixtures.OPERATOR_ROMS``),
  skipping by name without them. k106's ten trim-A edits are the known answer
  for where a trim change lands; a -3 count trim-B edit on ZS44A-20261004 is
  the code's prediction for N01.2's "degrees per count", not a measurement.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.analysis.ecu_state_models import Term279aTables, timing_term_27c6_steady
from src.analysis.paired_scenarios import (
    CHARGE_WORD_INPUTS_MOVED,
    CHARGE_WORD_UNRESOLVED,
    ENRICHMENT_RETARD_UNMODELLED,
    HIGH_DET_FORCED_BY_KS_FAULT,
    TERM_279A_UNMODELLED,
    TERM_1512_UNMODELLED,
    TIMING_STATE_UNSTATED,
    BaseTimingImage,
    EnrichmentRetardCals,
    TimingState,
    TimingTables,
    charge_word_sweep,
    coolant_byte,
    ignition_timing_at,
    ignition_timing_cells,
    timing_cell_summary,
    timing_tables,
)
from src.analysis.rom_map import load_rom_map
from src.analysis.sim_cells import ignition_timing_block
from src.analysis.sim_contract import SimulationRefusalError
from src.analysis.sim_montecarlo import load_sim_kit
from tests.rom_fixtures import OPERATOR_ROMS, PROFILE, operator_image
from tests.test_timing_rate_term import CALS, CURVE_CC1B, CURVE_CC2B, CURVE_CC33, TABLES
from tools import rom_sim

RPM_AXIS = tuple(8 * (i + 1) for i in range(16))
LOAD_AXIS = tuple(8 * (i + 1) for i in range(16))
WARM_C = 90.0  # coolant byte 140, above cal 0x8CCC (110); rescaled byte 208

#: The 0xFFFF279A input tables as on every listed image (rom_map.yaml): map
#: 0x9B09 all zero, 0xCC33 zero but entries 6..9.
RETARD = Term279aTables(
    map_9b09=((0,) * 8,) * 8,
    rpm_axis_c8fc=(0x30, 0x38, 0x40, 0x48, 0x60, 0x80, 0x90, 0xA4),
    coolant_axis_c904=(0x40, 0x48, 0x50, 0x58, 0x60, 0x68, 0x70, 0x80),
    curve_cc1b=CURVE_CC1B,
    curve_cc2b=CURVE_CC2B,
    curve_cc33=CURVE_CC33,
    curve_d335=(0x00, 0x20, 0x20, 0x20, 0x20, 0x20, 0x00, 0x00),
    curve_d33d=(0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x00, 0x00),
)
#: The listed images' 0xFFFF27C6 chain (tests/test_timing_rate_term.py).
#: Cals 0x8792 / 0x8793 = 0x82 and 0x8797 / 0x8798 = 0 on every listed image.
ENRICH = EnrichmentRetardCals(
    threshold_clear=0x82, threshold_set=0x82, below_gain_clear=0, below_gain_set=0
)
BASE_TERM = BaseTimingImage(tables=TABLES, cals=CALS, cal_8538=0, retard=RETARD, enrichment=ENRICH)


def _tables(
    a: int = 50,
    b: int = 45,
    *,
    edit: tuple[str, int, int, int] | None = None,
    base_term: BaseTimingImage = BASE_TERM,
) -> TimingTables:
    cells = {"a": [[0x80 | a] * 16 for _ in range(16)], "b": [[b] * 16 for _ in range(16)]}
    if edit is not None:
        name, r, c, by = edit
        row = cells[name][r]
        row[c] = (row[c] & 0x80) | ((row[c] & 0x7F) + by)
    return TimingTables(
        a=tuple(tuple(r) for r in cells["a"]),
        b=tuple(tuple(r) for r in cells["b"]),
        rpm_axis=RPM_AXIS,
        load_axis=LOAD_AXIS,
        base_term=base_term,
    )


def _state(high_det: bool, knock: str = "active") -> TimingState:
    return TimingState(high_det=high_det, knock_control=knock, source="test")  # type: ignore[arg-type]


def _at(base: TimingTables, cand: TimingTables, state: TimingState | None, **kw: Any) -> Any:
    args: dict[str, Any] = dict(
        rpm_word=RPM_AXIS[3] * 4,
        x_base=LOAD_AXIS[5],
        x_candidate=LOAD_AXIS[5],
        coolant_c=WARM_C,
        pedal_flag=False,
        phi_moved=False,
        bfs_moved=False,
        # The enrichment gate's floor on both ROMs: not enriched, so an X move
        # leaves 0xFFFF174A at 0 (the enriched cases state their bytes).
        lookup_bytes=(0x80, 0x80),
    )
    return ignition_timing_at(base, cand, state, **{**args, **kw})


# -- synthetic (code) ------------------------------------------------------------------


def test_the_timing_state_is_stated_and_consistent() -> None:
    # The KS fault (0xFFFF479D bit 7) sets HI DET on every pass inside the
    # latch's gate (0x04A928-0x04A990), and the engine state machine 0x027CC4
    # keeps the gate open while the engine runs: fallback with HI DET clear
    # is not a running steady state, so it is refused by name.
    with pytest.raises(SimulationRefusalError) as refused:
        _state(False, "fallback")
    assert refused.value.reason == HIGH_DET_FORCED_BY_KS_FAULT
    assert "0x04A928" in refused.value.message and "0x027CC4" in refused.value.message
    # The other three pairings are states (the latch holds after the fault clears).
    for high_det, knock in ((True, "fallback"), (True, "active"), (False, "active")):
        assert _state(high_det, knock).high_det is high_det
    with pytest.raises(SimulationRefusalError):
        _state(True, "off")
    with pytest.raises(SimulationRefusalError):
        TimingState(high_det=True, knock_control="active", source=" ")
    assert coolant_byte(90.0) == 140 and coolant_byte(-60.0) == 0


def test_a_trim_b_edit_moves_timing_only_where_trim_b_is_read() -> None:
    base, cand = _tables(), _tables(edit=("b", 3, 5, -3))
    hot = _at(base, cand, _state(True, "fallback"))
    assert (hot["delta"], hot["status"], hot["table_read"]) == (-3, "delta_if_no_clamp", "b")
    assert hot["reasons"] == ["clamp_headroom_unknown"] and hot["base"] is None
    # HI DET clear: A is read, so the edit gives 0 and says why.
    clear = _at(base, cand, _state(False))
    assert (clear["delta"], clear["reasons"]) == (0, ["trim_b_not_read:high_det_latch_clear"])
    cold = _at(base, cand, _state(True), coolant_c=50.0)
    assert (cold["delta"], cold["reasons"]) == (0, ["trim_b_not_read:coolant_at_or_below_56c"])
    blend = _at(base, cand, _state(True), coolant_c=58.0)
    assert (blend["status"], blend["reasons"]) == ("unevaluable", ["coolant_blend_band"])
    assert _at(base, cand, None)["reasons"] == [TIMING_STATE_UNSTATED]
    # The logged base timing gives both absolutes (30 -> 27, no clamp binds).
    logged = _at(base, cand, _state(True), base_timing_deg=30)
    assert (logged["base"], logged["candidate"], logged["status"]) == (30, 27, "computed")


def test_a_trim_a_edit_with_hi_det_set_is_zero_with_a_reason() -> None:
    base, cand = _tables(), _tables(edit=("a", 3, 5, 2))
    hot = _at(base, cand, _state(True, "fallback"))
    assert (hot["delta"], hot["reasons"]) == (0, ["trim_a_not_read:high_det_latch_set"])
    assert _at(base, cand, _state(False))["delta"] == 2
    # The cold side reads A too.
    assert _at(base, cand, _state(True), coolant_c=50.0)["delta"] == 2


def test_an_x_change_adds_the_base_term_change_to_the_trim_term() -> None:
    """K or Fuel Comp moves X: the trims' column moves, and so does
    0xFFFF27C6, now computed per ROM at its own X.

    Trim A here is 60 - column (flag set), read with HI DET clear. At X 104
    (column 12 exactly) it is 48; at X 98 (column 11, fraction (2 << 16) / 8
    = 16384) it is 49 - (1 x 16384 >> 16 = 0) = 49: the trim term +1. 27C6 at
    rpm word 200, hot, takes {24, 25} across the 1B24 band at X 104 and
    {25, 26} at X 98 (the table below). The X move moves 1B24's input
    0xFFFF1736, so 1B24 is swept on each ROM independently: 27C6 changes by
    25 - 25 = 0 .. 26 - 24 = +2, and the change is the trim's +1 plus that,
    +1..+3."""
    base = cand = TimingTables(
        a=tuple(tuple(0x80 | (60 - c) for c in range(16)) for _ in range(16)),
        b=_tables().b,
        rpm_axis=RPM_AXIS,
        load_axis=LOAD_AXIS,
        base_term=BASE_TERM,
    )
    moved = _at(base, cand, _state(False), rpm_word=200, x_base=104, x_candidate=98)
    assert (moved["delta_min"], moved["delta_max"], moved["trim_term_delta"]) == (1, 3, 1)
    assert moved["term_27c6_delta"] == [0, 2]
    assert (moved["status"], moved["reasons"]) == (
        "range",
        ["clamp_headroom_unknown", CHARGE_WORD_UNRESOLVED, CHARGE_WORD_INPUTS_MOVED],
    )
    assert moved["charge_word_routes"] == ["0xFFFF1736 via X"]
    phi = _at(base, cand, _state(False), phi_moved=True)
    assert (phi["delta"], phi["reasons"]) == (
        None,
        [ENRICHMENT_RETARD_UNMODELLED, CHARGE_WORD_INPUTS_MOVED],
    )


# 0xFFFF27C6 at rpm word 200 (rpm/50 50), hot (rescaled coolant 208: 15DC =
# 1CC9 = 128; r inside 51..204 reads the flat 0x80 curves: 15D7 = 19B6 = 128,
# so 15DA = 15DB and 15DE = S). The 1B24 band 11469..21299 (0x4000 x 0.7 /
# 1.3) gives b = 1B24 // 928 = 12 .. 22. Only 17C6 depends on b. Rows are
# (b: 17C6, t, u, (200 u) >> 10, v, 27C6).
#
# X 104: 10EB = 9B89 at word 200 (row 0, frac 57344) and X 104 (column 2,
#   frac (40 << 16) // 48 = 54613): upper 103 - (3 x 54613 >> 16 = 2) = 101,
#   lower 110 + (17 x 54613 >> 16 = 14) = 124, 101 + (23 x 57344 >> 16 = 20)
#   = 121. 15DB = 9AF9 at rpm/50 50 (row 0, frac 57344), X 104: upper 40 +
#   (18 x 54613 >> 16 = 15) = 55, lower 60 + (25 x 54613 >> 16 = 20) = 80,
#   55 + (25 x 57344 >> 16 = 21) = 76. R = 121 << 7 = 15488, F = 4096 -
#   (32 x 896 >> 8 = 112) = 3984; S = trunc(76 x 3984 / 1000 = 302.8) = 302 +
#   512 = 814. 19CB = 104. 17C6 = trunc(104 (b + trunc(32768 / 121) = 270) / 25).
#   b 12: 1173, t = floor(21480 x 1173 / (814 x 104)) = 297, u 332, 64, v 38, 24
#   b 13: 1177, 298, 333, 65, 39, 25 ... b 22: 1214, 308, 343, 66, 39, 25.
# X 98: 10EB: column 2 frac (34 << 16) // 48 = 46421: upper 103 - (3 x 46421
#   >> 16 = 2) = 101, lower 110 + (17 x 46421 >> 16 = 12) = 122, 101 + (21 x
#   57344 >> 16 = 18) = 119. 15DB: upper 40 + (18 x 46421 >> 16 = 12) = 52,
#   lower 60 + (25 x 46421 >> 16 = 17) = 77, 52 + (25 x 57344 >> 16 = 21) =
#   73. R = 15232, F = 4096 - (32 x 1152 >> 8 = 144) = 3952; S = trunc(73 x
#   3952 / 1000 = 288.5) = 288 + 512 = 800. 17C6 = trunc(98 (b + 275) / 25).
#   b 12: 1125, t = floor(21480 x 1125 / (800 x 98)) = 308, u 343, 66, v 39, 25
#   b 13: 1128, 309, 344, 67, 40, 26 ... b 22: 1164, 318, 353, 68, 40, 26.
# So 104 -> 98 is +1 at b 12 (24 -> 25) and at b 13..22 (25 -> 26): exact.
#
# X 64 (17C6 = trunc(64 (b + 300) / 25), 15DE 728, 19CB 64; the 0x4000 row
# is test_timing_rate_term's 33):
#   b 12: 798, 367, 402, 78, 46, 32;  b 14: 803, 370, 405, 79, 47, 33;
#   b 18: 814, 375, 410, 80, 48, 34 (through b 22: 824, 379, 414, 80, 48, 34).
# X 61: 10EB 108, 15DB 55 (column 1, frac (29 << 16) // 32 = 59392), F 3776,
#   S = trunc(55 x 3776 / 1000 = 207.7) = 207 + 512 = 719; 17C6 = trunc(61 (b
#   + trunc(32768 / 108) = 303) / 25):
#   b 12: 768, 376, 411, 80, 48, 34;  b 19: 785, 384, 419, 81, 48, 34;
#   b 20: 788, 385, 420, 82, 49, 35 (through b 22: 793, 388, 423, 82, 49, 35).
# So 64 -> 61 is +2 at b 12-13, +1 at b 14-17, 0 at b 18-19, +1 at b 20-22.
#
# Held equal (the same 1B24 on both ROMs) the changes are 104 -> 98: +1 at
# every b, [1, 1]; 64 -> 61: [0, 2]. An X move moves 1B24's input 0xFFFF1736
# (map 0x9724 reads X), so each ROM takes its own 1B24 in the band: the change
# runs from min(candidate) - max(base) to max(candidate) - min(base): 104 -> 98
# is 25 - 25 = 0 .. 26 - 24 = 2; 64 -> 61 is 34 - 34 = 0 .. 35 - 32 = 3.
#
# Rpm word 40, X 91 and 85 (11CE 40; 17C6 = trunc(X (b + 320) / 25), 10EB
# 102, R 13056, F 3680; 15DE 286 at X 91, 274 at X 85), (b: 17C6, t, u,
# (40 u) >> 10, v, 27C6):
#   X 91 b 12: 1212, 1000, 1035, 40, 24, 10 ... b 22: 1248, 1030, 1065, 41, 24, 10
#   X 85 b 12: 1132, 1044, 1079, 42, 25, 11 ... b 22: 1166, 1075, 1110, 43, 25, 11
# 27C6 is 10 at every b at X 91 and 11 at every b at X 85: +1 whichever 1B24
# each ROM has, so the change is exact even swept independently.


def _held_equal_27c6(rpm_word: int, x_base: int, x_candidate: int) -> list[int]:
    """27C6's change with 1B24 held EQUAL on both ROMs (what the sweep gives
    when nothing moves 1B24), at hot coolant on the listed tables."""

    def term(x: int, a: int) -> int:
        return timing_term_27c6_steady(
            rpm_word=rpm_word, x=x, coolant_1077=208, w1b24=a, w1512=0, tables=TABLES, cals=CALS
        ).term

    deltas = [term(x_candidate, a) - term(x_base, a) for a in charge_word_sweep(TABLES, TABLES)]
    return [min(deltas), max(deltas)]


def test_a_k_style_x_move_that_holds_across_the_charge_band_is_one_value() -> None:
    """Rpm word 40, X 91 -> 85: 27C6 is 10 at every 1B24 on the base and 11 at
    every 1B24 on the candidate (the table above), so even with 1B24 swept on
    each ROM independently the change is exactly +1."""
    args = dict(rpm_word=40, x_base=91, x_candidate=85)
    hot = _at(_tables(), _tables(), _state(True, "fallback"), **args)
    assert (hot["status"], hot["delta"], hot["delta_min"], hot["delta_max"]) == (
        "delta_if_no_clamp",
        1,
        1,
        1,
    )
    assert (hot["trim_term_delta"], hot["term_27c6_delta"], hot["term_279a"]) == (0, [1, 1], "zero")
    assert hot["reasons"] == ["clamp_headroom_unknown", CHARGE_WORD_INPUTS_MOVED]
    # With a logged base the absolute is given, nothing clamps: 30 -> 31.
    logged = _at(_tables(), _tables(), _state(True, "fallback"), base_timing_deg=30, **args)
    assert (logged["status"], logged["base"], logged["candidate"]) == ("computed", 30, 31)
    assert logged["reasons"] == [CHARGE_WORD_INPUTS_MOVED]


def test_a_k_style_x_move_across_the_charge_band_is_a_range() -> None:
    """A K -5 % move, X 64 -> 61 at rpm word 200: the X move moves 1B24's
    input 0xFFFF1736, so each ROM's 27C6 takes any value its own 1B24 gives
    ({32, 33, 34} at X 64, {34, 35} at X 61): 0..+3, a range, never a number."""
    args = dict(rpm_word=200, x_base=64, x_candidate=61)
    hot = _at(_tables(), _tables(), _state(True, "fallback"), **args)
    assert (hot["status"], hot["delta"], hot["delta_min"], hot["delta_max"]) == (
        "range",
        None,
        0,
        3,
    )
    assert hot["reasons"] == [
        "clamp_headroom_unknown",
        CHARGE_WORD_UNRESOLVED,
        CHARGE_WORD_INPUTS_MOVED,
    ]
    assert (hot["trim_term_delta"], hot["term_27c6_delta"]) == (0, [0, 3])
    # Both ends go through the clamps: from 30, 30..33; from 47 the +3 end is
    # exactly 50 and nothing clamps; from 48 and 49 the top end caps at 50.
    ends = {
        base: _at(_tables(), _tables(), _state(True, "fallback"), base_timing_deg=base, **args)
        for base in (30, 47, 48, 49)
    }
    moved = [CHARGE_WORD_UNRESOLVED, CHARGE_WORD_INPUTS_MOVED]
    assert [(e["candidate_min"], e["candidate_max"], e["reasons"]) for e in ends.values()] == [
        (30, 33, moved),
        (47, 50, moved),
        (48, 50, ["timing_cap_binds", *moved]),
        (49, 50, ["timing_cap_binds", *moved]),
    ]
    assert (ends[49]["delta_min"], ends[49]["delta_max"], ends[49]["status"]) == (0, 1, "range")


def test_an_x_move_is_wider_than_the_held_equal_sweep_at_the_same_point() -> None:
    """Held equal, 1B24 would cancel: 104 -> 98 is [1, 1] and 64 -> 61 is
    [0, 2] (the table above). An X move moves 1B24 itself, so the reported
    change is the independent sweep, [0, 2] and [0, 3]: wider, and the
    held-equal answer always inside it."""
    for (xb, xc), held, independent in (((104, 98), [1, 1], [0, 2]), ((64, 61), [0, 2], [0, 3])):
        assert _held_equal_27c6(200, xb, xc) == held
        got = _at(
            _tables(), _tables(), _state(True, "fallback"), rpm_word=200, x_base=xb, x_candidate=xc
        )
        assert got["term_27c6_delta"] == independent
        assert independent[0] <= held[0] <= held[1] <= independent[1]
        assert independent[1] - independent[0] > held[1] - held[0]
    # BFS or phi moving 1B24 with X equal is swept independently too, and named.
    bfs = _at(
        _tables(), _tables(), _state(True, "fallback"), rpm_word=200, x_base=64, x_candidate=64,
        bfs_moved=True,
    )  # fmt: skip
    assert bfs["term_27c6_delta"] == [-2, 2]  # {32, 33, 34} against itself
    assert bfs["charge_word_routes"] == ["0xFFFF175C via BFS"]
    assert CHARGE_WORD_INPUTS_MOVED in bfs["reasons"]
    phi = _at(_tables(), _tables(), _state(True, "fallback"), rpm_word=200, phi_moved=True)
    assert phi["status"] == "unevaluable" and ENRICHMENT_RETARD_UNMODELLED in phi["reasons"]
    assert phi["charge_word_routes"] == ["0xFFFF26C0 via the Target AFR lookup"]


def test_an_x_move_at_an_enriched_point_holds_the_enrichment_retard() -> None:
    """With the pedal flag set the Target AFR lookup reads its last column,
    so a K edit moves X and not phi. The enrichment retard 0xFFFF174A =
    max(0, min(0xFFFF174C, g)) still moves: its cap 174C reads X, and g is not
    0 once the gated byte 0xFFFF174B reaches the threshold (0x04CAF8). Rpm
    word 40, X 91 -> 85, otherwise exact +1 (test above)."""
    args: dict[str, Any] = dict(rpm_word=40, x_base=91, x_candidate=85, pedal_flag=True)
    state = _state(True, "fallback")
    enriched = _at(_tables(), _tables(), state, lookup_bytes=(0x9A, 0x9A), **args)
    assert (enriched["status"], enriched["delta"]) == ("unevaluable", None)
    assert enriched["reasons"] == [ENRICHMENT_RETARD_UNMODELLED, CHARGE_WORD_INPUTS_MOVED]
    # Either ROM at the threshold is enough; 0x80 (the gate's floor) on both is not.
    assert (
        ENRICHMENT_RETARD_UNMODELLED
        in _at(_tables(), _tables(), state, lookup_bytes=(0x80, 0x82), **args)["reasons"]
    )
    lean = _at(_tables(), _tables(), state, lookup_bytes=(0x80, 0x80), **args)
    assert (lean["status"], lean["delta"]) == ("delta_if_no_clamp", 1)
    # A byte not known at the point may retard; so may any byte when the
    # below-threshold gain is not 0.
    for unknown in (None, (0x80, None)):
        got = _at(_tables(), _tables(), state, lookup_bytes=unknown, **args)
        assert ENRICHMENT_RETARD_UNMODELLED in got["reasons"]
    gain = dataclasses.replace(ENRICH, below_gain_set=1)
    with_gain = _tables(base_term=dataclasses.replace(BASE_TERM, enrichment=gain))
    assert (
        ENRICHMENT_RETARD_UNMODELLED
        in _at(with_gain, with_gain, state, lookup_bytes=(0x80, 0x80), **args)["reasons"]
    )
    # The HI DET latch picks the threshold: 0x8793 set, 0x8792 clear.
    split = dataclasses.replace(ENRICH, threshold_clear=0x90)
    tables = _tables(base_term=dataclasses.replace(BASE_TERM, enrichment=split))
    on_set = _at(tables, tables, state, lookup_bytes=(0x88, 0x88), **args)
    on_clear = _at(tables, tables, _state(False), lookup_bytes=(0x88, 0x88), **args)
    assert ENRICHMENT_RETARD_UNMODELLED in on_set["reasons"]
    assert ENRICHMENT_RETARD_UNMODELLED not in on_clear["reasons"]
    # X equal (the trim grid): 174C and g are equal on both ROMs, nothing held.
    still = _at(_tables(), _tables(edit=("b", 0, 15, 1)), state, lookup_bytes=(0x9A, 0x9A),
                rpm_word=40, x_base=91, x_candidate=91, pedal_flag=True)  # fmt: skip
    assert ENRICHMENT_RETARD_UNMODELLED not in still["reasons"]


def test_the_charge_sweep_takes_every_b_step_and_both_ends() -> None:
    # 0x4000 x 0.7 = 11468.8 -> 11469; x 1.3 = 21299.2 -> 21299; b steps at
    # multiples of 928 = 29 x 32 (12064 = 13 x 928 .. 20416 = 22 x 928).
    steps = charge_word_sweep(TABLES, TABLES)
    assert steps == (11469, *range(12064, 20417, 928), 21299)
    assert sorted({a // 29 >> 5 for a in steps}) == list(range(12, 23))
    # A curve indexed by r that is not flat across the band adds every r step (64).
    bent = dataclasses.replace(TABLES, charge_curve_ceab=(0x80,) * 4 + (0x90,) * 4)
    assert set(range(11520, 21299, 64)) <= set(charge_word_sweep(TABLES, bent))


def test_279a_is_zero_hot_and_unmodelled_cold_when_bfs_moves() -> None:
    """Hot (rescaled 208): 0xCC33 at index 13 is 0 and 0x9B09 is all zero, so
    279A = 0 on both ROMs and an X/BFS move is evaluable. At 40 degC (degC + 50
    = 90, rescaled (90 - 10) x 240 / 150 = 128) 0xCC33 reads entry 8 = 0xFF:
    26D0 is a product with the 0xFFFF1300 ramp, not provably 0, so a BFS move
    is term_279a_unmodelled; with nothing moved it is held equal as before."""
    args = dict(rpm_word=200, x_base=104, x_candidate=98, bfs_moved=True)
    hot = _at(_tables(), _tables(), _state(True, "fallback"), **args)
    assert (hot["status"], hot["term_279a"]) == ("range", "zero")
    cold = _at(_tables(), _tables(), _state(False), coolant_c=40.0, **args)
    assert (cold["status"], cold["delta"], cold["reasons"]) == (
        "unevaluable",
        None,
        [TERM_279A_UNMODELLED, CHARGE_WORD_INPUTS_MOVED],
    )
    assert (cold["trim_term_delta"], cold["term_279a"]) == (0, "unmodelled")
    still = _at(_tables(), _tables(edit=("a", 3, 5, 2)), _state(False), coolant_c=40.0)
    assert (still["delta"], still["term_279a"]) == (2, "held_equal")
    # BFS moved with X equal (a scaler and K change together) is still a move.
    bfs = _at(_tables(), _tables(), _state(False), coolant_c=40.0, bfs_moved=True)
    assert bfs["reasons"] == [TERM_279A_UNMODELLED, CHARGE_WORD_INPUTS_MOVED]


def test_a_9b09_edit_counts_only_where_it_is_read() -> None:
    """At rpm word 200 (0xFFFF11CE = 200, past the 0xC8FC axis's 0xA4) and
    rescaled coolant 208 (past 0xC904's 0x80) map 0x9B09 reads cell [7][7].
    An edit to [0][0] still reads 0 there on both ROMs; one to [7][7] does not."""

    def with_cell(r: int, c: int) -> TimingTables:
        cells = [list(row) for row in RETARD.map_9b09]
        cells[r][c] = 0x40
        retard = dataclasses.replace(RETARD, map_9b09=tuple(tuple(row) for row in cells))
        return _tables(base_term=dataclasses.replace(BASE_TERM, retard=retard))

    elsewhere = _at(_tables(), with_cell(0, 0), _state(True, "fallback"), rpm_word=200)
    assert (elsewhere["delta"], elsewhere["term_279a"]) == (0, "zero")
    read = _at(_tables(), with_cell(7, 7), _state(True, "fallback"), rpm_word=200)
    assert (read["status"], read["reasons"]) == ("unevaluable", [TERM_279A_UNMODELLED])


def test_a_nonzero_cal_8538_makes_1512_unmodelled() -> None:
    """0xFFFF1512 = 0xFFFF26F2 x cal 0x8538, and 26F2 is unidentified: with
    the cal not 0 on either image, a 27C6 change cannot be computed."""
    cand = _tables(base_term=dataclasses.replace(BASE_TERM, cal_8538=3))
    moved = _at(_tables(), cand, _state(True, "fallback"), x_base=64, x_candidate=61)
    assert (moved["status"], moved["reasons"]) == (
        "unevaluable",
        [TERM_1512_UNMODELLED, CHARGE_WORD_INPUTS_MOVED],
    )
    assert moved["term_27c6_delta"] is None and moved["trim_term_delta"] == 0


def test_the_pedal_flag_reads_the_last_column() -> None:
    base, cand = _tables(), _tables(edit=("a", 3, 15, -1))
    assert _at(base, cand, _state(False))["delta"] == 0  # column 5
    assert _at(base, cand, _state(False), pedal_flag=True)["delta"] == -1


def test_the_cells_show_an_edit_in_exactly_its_cells() -> None:
    base, cand = _tables(), _tables(edit=("b", 9, 14, -3))
    cells = ignition_timing_cells(base, cand, _state(True, "fallback"), coolant_c=WARM_C)
    changed = [(c["row"], c["col"], c["delta"]) for c in cells if c["delta"]]
    assert changed == [(9, 14, -3)]
    summary = timing_cell_summary(cells)
    assert (summary["cells"], summary["changed"], summary["unevaluable"]) == (256, 1, 0)


def test_a_one_cell_rate_map_edit_moves_the_cells_that_read_it() -> None:
    """0x9AF9 [1][2] (rpm/50 56, X 64) 0x3C -> 0x50 on the trims' grid (rpm
    byte = rpm/50 here, X the load byte; HI DET set, trims equal). Its
    bilinear support is rpm byte 16..80 x X 40..104 (strictly between the
    neighbouring axis points 8 / 88 and 32 / 112); every other cell reads
    none of it and stays 0.

    The node (rpm word 224, X 64), rescaled coolant 208: 10EB = 0x9B89 at
    word 224 (= 0x38 x 4, row 1 exactly) and X 64 (column 2): 0x6E = 110.
    R = 110 << 7 = 14080, F = 4096 - (32 x 2304 >> 8 = 288) = 3808. 15DB base
    0x3C = 60, edited 80. S = trunc(60 x 3808 / 1000 = 228.5) = 228 +
    trunc((25 x 224 << 6) / 625 = 573.4) = 573 -> 801; edited trunc(80 x 3808
    / 1000 = 304.6) = 304 + 573 = 877. 19CB 64; 17C6 = trunc(64 (b +
    trunc(32768 / 110) = 297) / 25); t = floor(21480 x 17C6 / (S x 64)), v =
    floor(((224 (t + 35)) >> 10) x 3 / 5). (b: 17C6, t base, v base, 27C6
    base, t edited, v edited, 27C6 edited):
      b 12: 791, 331, 48, 34, 302, 43, 29   (-5)
      b 14: 796, 333, 48, 34, 304, 44, 30   (-4)
      b 18: 806, 337, 48, 34, 308, 45, 31   (-3)
      b 21: 814, 341, 49, 35, 311, 45, 31   (-4)
    a range, -5 .. -3.
    (rpm byte 56, X 56): column 1, frac (24 << 16) // 32 = 49152. 10EB = 100
    + (10 x 49152 >> 16 = 7) = 107, R = 13696, F = 3760. 15DB base 39 + (21 x
    49152 >> 16 = 15) = 54, edited 39 + (41 x 49152 >> 16 = 30) = 69. S =
    trunc(54 x 3760 / 1000 = 203.0) = 203 + 573 = 776, edited trunc(69 x 3760
    / 1000 = 259.4) = 259 + 573 = 832. 17C6 = trunc(56 (b + 306) / 25):
    b 12: 712, t 351 / 328, v 50 / 47, 27C6 36 / 33; b 15: 719, t 355 / 331,
    v 51 / 48, 37 / 34 (the same through b 22):
    -3 at every b, exact."""
    edited = [list(row) for row in TABLES.rate_map_9af9]
    edited[1][2] = 0x3C + 20
    cand = _tables(
        base_term=dataclasses.replace(
            BASE_TERM,
            tables=dataclasses.replace(TABLES, rate_map_9af9=tuple(tuple(r) for r in edited)),
        )
    )
    cells = ignition_timing_cells(_tables(), cand, _state(True, "fallback"), coolant_c=WARM_C)
    by = {(RPM_AXIS[c["row"]], c["x"]): c for c in cells}
    moved = {k for k, c in by.items() if c["status"] == "range" or c["delta"]}
    assert len(moved) == 80  # 9 x 9 support less (16, 104), where every b gives 0
    assert all(16 <= r <= 80 and 40 <= x <= 104 for r, x in moved)
    node = by[(56, 64)]
    assert (node["status"], node["delta_min"], node["delta_max"]) == ("range", -5, -3)
    assert node["reasons"] == ["clamp_headroom_unknown", CHARGE_WORD_UNRESOLVED]
    exact = by[(56, 56)]
    assert (exact["status"], exact["delta"], exact["term_27c6_delta"]) == (
        "delta_if_no_clamp",
        -3,
        [-3, -3],
    )
    for away in ((96, 64), (56, 32), (8, 64), (56, 112)):
        assert (by[away]["delta"], by[away]["reasons"]) == (0, []), away
    summary = timing_cell_summary(cells)
    assert (summary["changed"], summary["unevaluable"]) == (80, 0)
    assert summary["ranged"] == sum(1 for c in cells if c["status"] == "range")


def _both(by: int) -> TimingTables:
    """Trim A and trim B both edited by ``by`` counts at the same cell."""
    one = _tables(edit=("a", 3, 5, by))
    two = _tables(edit=("b", 3, 5, by))
    return TimingTables(
        a=one.a, b=two.b, rpm_axis=RPM_AXIS, load_axis=LOAD_AXIS, base_term=BASE_TERM
    )


def test_the_unread_count_counts_only_a_trim_the_state_does_not_read() -> None:
    """``changed_trim_not_read`` is "a changed trim the ECM does not read". It
    once matched every reason that began ``trim_``, so a trim the ECM DOES read,
    set below the term's floor (``trim_term_floor_may_bind``), counted as an
    unread edit. Each case is one cell of the 256 at the latch's state."""

    def summary(base: TimingTables, cand: TimingTables, high_det: bool) -> dict[str, Any]:
        cells = ignition_timing_cells(base, cand, _state(high_det), coolant_c=WARM_C)
        return timing_cell_summary(cells)

    def counts(s: dict[str, Any]) -> tuple[int, int, int]:
        return s["changed"], s["unevaluable"], s["changed_trim_not_read"]

    # Trim B set to 3 (below the floor of 7) with the latch set: trim B IS read, the
    # answer is unevaluable, and nothing is "edited but not read".
    floor = summary(_tables(), _tables(edit=("b", 3, 5, 3 - 45)), True)
    assert counts(floor) == (0, 1, 0)
    assert "trim_term_floor_may_bind" in floor["reasons"]
    # The same edit with the latch clear is the unread case, and is counted.
    assert counts(summary(_tables(), _tables(edit=("b", 3, 5, -3)), False)) == (0, 0, 1)
    assert counts(summary(_tables(), _tables(edit=("a", 3, 5, 2)), True)) == (0, 0, 1)
    # Both trims edited with the latch set: B is read, so the cell changes, and the A
    # edit beside it is still named as unread.
    assert counts(summary(_tables(), _both(-2), True)) == (1, 0, 1)
    cell = next(
        c
        for c in ignition_timing_cells(_tables(), _both(-2), _state(True), coolant_c=WARM_C)
        if c["delta"]
    )
    assert cell["delta"] == -2
    assert cell["reasons"] == ["clamp_headroom_unknown", "trim_a_not_read:high_det_latch_set"]


# -- private ROM ----------------------------------------------------------------------


def _images(*names: str) -> list[bytes]:
    found = [operator_image(n) for n in names]
    if any(i is None for i in found):
        pytest.skip(f"operator ROMs {names} not on this machine")
    return [i for i in found if i is not None]


@pytest.mark.slow
def test_k106s_ten_trim_a_edits_move_timing_in_those_cells_and_only_with_hi_det_clear() -> None:
    """The ROM Store base against k106 ("added spark at 3000-4500 high load"):
    trim A rows 7-9 x columns 12-15, +1/+2. With HI DET clear the timing change
    is the trim change in exactly those ten cells; set (as on the truck since
    the knock sensor fallback, 0xFFFF13FE = 0xFB00) the ECM reads trim B and
    the change is 0 in every cell, each of the ten naming why."""
    base_image, k106_image = _images("base", "k106")
    rom_map = load_rom_map(PROFILE)
    base, k106 = (
        timing_tables(base_image, rom_map, slot=1),
        timing_tables(k106_image, rom_map, slot=1),
    )
    assert base.b == k106.b and base.a != k106.a
    clear = ignition_timing_cells(base, k106, _state(False), coolant_c=WARM_C)
    changed = {(c["row"], c["col"]): c["delta"] for c in clear if c["delta"]}
    assert len(changed) == 10
    assert all(7 <= r <= 9 and 12 <= c <= 15 for r, c in changed)
    assert set(changed.values()) <= {1, 2}
    for (r, c), delta in changed.items():  # the masked cell difference itself
        assert delta == (k106.a[r][c] & 0x7F) - (base.a[r][c] & 0x7F)
    held = ignition_timing_cells(base, k106, _state(True, "fallback"), coolant_c=WARM_C)
    assert not [c for c in held if c["delta"]]
    named = {(c["row"], c["col"]) for c in held if c["reasons"]}
    assert named == set(changed)
    assert {r for c in held for r in c["reasons"]} == {"trim_a_not_read:high_det_latch_set"}
    # k106 also raised K 6 %, which moves BFS and so X and 0xFFFF27C6: the
    # trim grid's block (every other term held) gives no timing for the pair.
    kit = load_sim_kit(PROFILE).kit
    block = ignition_timing_block(
        kit, base_image, k106_image, _state(False), slot=1, coolant_c=WARM_C, all_cells=False
    )
    assert block["status"] == "unevaluable" and block["trims_changed"]
    assert any(r.startswith("timing_input_changed:") for r in block["reasons"]), block["reasons"]


def _cli(*argv: str | Path) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = rom_sim.main([str(a) for a in argv])
    return code, json.loads(out.getvalue())


def _trim_b_spec(tmp_path: Path) -> Path:
    spec = tmp_path / "trim_b_minus_3.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "base": {"revision": "ZS44A-20261004"},
                "label": "trim B -3 counts",
                "edits": [{"table": "timing_trim_b", "slot": 1, "all": True, "add_raw": -3}],
            }
        )
    )
    return spec


@pytest.mark.slow
def test_a_minus_three_count_trim_b_edit_is_minus_three_degrees_in_every_cell(
    tmp_path: Path,
) -> None:
    """ZS44A-20261004, trim B -3 counts everywhere, through the edit spec and
    ``rom_sim inspect``: with HI DET set every trim cell's timing changes -3
    degrees (the chain adds the trim term without scaling; the absolute is
    unknown, so ``clamp_headroom_unknown``), torque refused with the spark
    reason. With HI DET clear it is 0 in every cell, each naming why."""
    _images("r20261004")
    rom_dir = OPERATOR_ROMS["r20261004"].parent
    spec = _trim_b_spec(tmp_path)
    common: list[str | Path] = [
        "inspect",
        "--edits",
        spec,
        "--rom-dir",
        rom_dir,
        "--timing-coolant-c",
        "90",
    ]
    code, payload = _cli(*common, "--high-det", "set", "--knock-control", "fallback")
    assert code == 0, payload
    timing = payload["ignition_timing"]
    assert Counter(c["delta"] for c in timing["cells"]) == Counter({-3: 256})
    assert timing["summary"]["reasons"] == ["clamp_headroom_unknown"]
    assert timing["torque"]["reason"].startswith("spark_response_unmeasured")
    assert timing["knock"]["reasons"][0].startswith("knock_unmodelled")
    code, payload = _cli(*common, "--high-det", "clear", "--knock-control", "active")
    assert code == 0, payload
    cells = payload["ignition_timing"]["cells"]
    assert len(cells) == 256 and {c["delta"] for c in cells} == {0}
    assert {r for c in cells for r in c["reasons"]} == {"trim_b_not_read:high_det_latch_clear"}
    code, payload = _cli("inspect", "--edits", spec, "--rom-dir", rom_dir)
    assert (code, payload["refused"]) == (2, "timing_state_unstated")


@pytest.mark.slow
def test_the_paired_run_carries_the_timing_change_at_every_design_point(tmp_path: Path) -> None:
    """``rom_sim run`` over a grid design on ZS44A-20261004 with the -3 trim B
    edit: every (point, draw) pair is -3 degrees, and the report shows it."""
    _images("r20261004")
    rom_dir = OPERATOR_ROMS["r20261004"].parent
    designs = tmp_path / "designs"
    grid: list[str | Path] = [
        "design", "grid", "--base-revision", "ZS44A-20261004",
        "--rpm-edges", "1900", "2100", "2900", "3100", "--load-edges", "25", "32", "60", "70",
        "--load-basis", "fill_fraction_as_manifold_ratio", "--baro-kpa", "80", "--iat-c", "25",
        "--coolant-c", "88", "--battery-v", "14", "--rail-kpa", "working_assumption",
        "--throttle-pct", "20", "--pedal-flag", "0", "--out", designs,
    ]  # fmt: skip
    assert _cli(*grid)[0] == 0
    (design,) = designs.iterdir()
    run = tmp_path / "run"
    code, payload = _cli(
        "run", "--edits", _trim_b_spec(tmp_path), "--rom-dir", rom_dir, "--design", design,
        "--draws", "3", "--seed", "1", "--stft-pct", "0", "--ltft-pct", "0",
        "--candidate-events", "none", "--high-det", "set", "--knock-control", "fallback",
        "--out", run,
    )  # fmt: skip
    assert code == 0, payload
    result = json.loads((run / "result.json").read_text())
    points = result["ignition_timing"]["points"]
    assert len(points) == 9
    outcomes = [o for p in points for o in p["outcomes"]]
    assert {o["delta"] for o in outcomes} == {-3}
    assert sum(o["draws"] for o in outcomes) == 9 * 3
    assert all(o["x_base"] == o["x_candidate"] for o in outcomes)
    assert "Ignition timing (ECU side" in (run / "report.html").read_text()


def test_the_report_shows_a_range_as_the_range() -> None:
    """A change known only across the 1B24 band renders as ``lo..hi``, in the
    cells table and at the design points, never as a blank or a number."""
    from src.analysis.sim_report import _timing

    cell = {
        "row": 6, "col": 7, "rpm_word": 224, "x": 64, "delta": None, "delta_min": -5,
        "delta_max": -3, "status": "range",
        "reasons": ["clamp_headroom_unknown", CHARGE_WORD_UNRESOLVED],
    }  # fmt: skip
    outcome = {**cell, "x_base": 64, "x_candidate": 61, "trim_term_delta": 0, "draws": 2}
    page = _timing(
        {
            "status": "evaluated",
            "state": {"high_det": True, "knock_control": "fallback", "source": "test"},
            "cells_at": {"coolant_c": 90.0},
            "summary": {"cells": 256, "changed": 1, "ranged": 1, "unevaluable": 0},
            "cells": [cell],
            "points": [{"point": 0, "rpm": 2500.0, "coolant_c": 90.0, "outcomes": [outcome]}],
            "conditions": [],
        }
    )
    assert page.count("<td>-5..-3</td>") == 2
    assert "(1 known only as a range)" in page
