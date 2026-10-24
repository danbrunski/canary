"""E05.3 state models and the E05.4 timing-trim lookup.

Three kinds of evidence, labelled per test:

- **synthetic (code)**: vectors derived by hand from the decoded integer
  semantics (``docs/roadmap/tasks/E05-ecu-application-twin.md``; the
  manifest's node text). They pin our reading of the code, not the vehicle.
- **fixture**: the committed 2026-10-04 ram-study slice, where its inputs
  exist (the pedal flag byte, the UpRev window). Missing inputs are counted
  unevaluable, never filled.
- **private ROM**: the operator's images, skipping by name without them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.analysis.ecu_state_models import (
    MODEL_VERSION,
    TIMING_CAP,
    TIMING_FLOOR,
    UPREV_MAGIC,
    Checkpoint,
    EnrichmentInputs,
    EnrichmentLatches,
    EnrichmentState,
    FlagRead,
    PedalFlagModel,
    Step,
    UpRevState,
    account_pedal_flag,
    account_uprev_reads,
    base_timing_select,
    base_timing_sum,
    byte_curve16,
    enrichment_step,
    loaded_slot,
    pedal_comparand,
    pedal_curve_input,
    pedal_flag_next,
    selected_timing_trim,
    timing_cycle_copy,
    timing_degrees_btdc,
    timing_delta,
    timing_final_byte,
    timing_rate_limit,
    timing_rate_limit_steady,
    timing_retard_combine,
    timing_trim_terms,
    trim_lookup,
    uprev_power_on,
    uprev_step,
)
from src.yaml_load import load_yaml

REPO = Path(__file__).resolve().parents[1]
EXPECT = load_yaml((REPO / "tests/fixtures/ground_truth/target_phi.yaml").read_text())
#: Curve 0xCEDB on every listed image (and Map1Change): 200 at every rpm.
CEDB = (200,) * 16


def _slice() -> pd.DataFrame:
    (part,) = [p for p in EXPECT["telemetry"] if "target-phi-slice" in p["path"]]
    path = REPO / part["path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == part["sha256"]
    frame = pd.read_parquet(path)
    frame["acquired_at"] = frame["metadata_json"].map(lambda m: json.loads(m)["acquired_at"])
    return frame


# -- 0x026BBC and the pedal flag (synthetic, code) ------------------------------------


def test_the_byte_curve_steps_sixteen_counts_per_entry_and_truncates_toward_the_entry() -> None:
    rising = [10 * i for i in range(16)]
    assert byte_curve16(rising, 0) == 0
    assert byte_curve16(rising, 8) == 5  # half way, (10 x 0x8000) >> 16
    assert byte_curve16(rising, 17) == 10  # 10 + (10 x 0x1000 >> 16) = 10 + 0
    assert byte_curve16(rising, 239) == 149  # 140 + (10 x 0xF000 >> 16) = 140 + 9
    assert byte_curve16(rising, 240) == byte_curve16(rising, 255) == 150
    falling = [100, 90] + [0] * 14
    assert byte_curve16(falling, 1) == 100  # 100 - (10 x 0x1000 >> 16): toward the entry
    assert byte_curve16(falling, 15) == 91
    with pytest.raises(ValueError, match="16-entry"):
        byte_curve16([0] * 8, 0)


def test_the_curve_input_is_the_rpm_word_halved_exactly() -> None:
    """0x03907A writes w1210 = w1204 >> 3 and 0x039048 writes b1207 =
    min(255, w1204 >> 4); both reach the lookup through one cycle-start copy.
    So the byte follows from the observed rpm word with no rounding, for every
    w1204."""
    for w1204 in range(0x10000):
        assert pedal_curve_input(w1204 >> 3) == min(255, w1204 >> 4)


def test_the_comparand_is_the_requested_position_scaled_and_saturated() -> None:
    assert pedal_comparand(0) == 0
    assert pedal_comparand(637) == 199  # 637 x 20571 >> 16 = 199.95
    assert pedal_comparand(638) == 200  # the first position that sets the flag
    assert pedal_comparand(612) == 192
    assert pedal_comparand(611) == 191  # below 192 it clears whatever it was
    assert pedal_comparand(819) == 255 and pedal_comparand(0xFFFF) == 255


def test_the_flag_needs_its_history_only_inside_the_eight_count_band() -> None:
    assert pedal_flag_next(None, 200, 200).value is True
    assert pedal_flag_next(None, 191, 200).value is False
    for comparand in range(192, 200):
        assert pedal_flag_next(True, comparand, 200).value is True
        assert pedal_flag_next(False, comparand, 200).value is False
        unknown = pedal_flag_next(None, comparand, 200)
        assert (unknown.value, unknown.reason) == (None, "hysteresis_band_state_unknown")
    # 255 + 8 does not wrap: the firmware compares in 32 bits.
    assert pedal_flag_next(True, 255, 255).value is True


def test_the_flag_model_starts_unknown_attaches_to_a_read_and_forgets_on_a_gap() -> None:
    model = PedalFlagModel(CEDB)
    assert model.step(195, rpm_word=236).reason == "hysteresis_band_state_unknown"
    assert model.step(210, rpm_word=236).value is True  # out of band: resynchronised
    assert model.step(195, rpm_word=236).value is True  # held by the hysteresis
    model.gap()
    assert model.step(195, rpm_word=236).value is None
    model.attach(False, Checkpoint("mem:ECM_PEDAL_ENRICH_FLAGS", 1791146759.6))
    assert model.checkpoint is not None and model.step(195, rpm_word=236).value is False


# -- the pedal flag on the 2026-10-04 capture (fixture) -----------------------------------


def test_every_flag_read_and_transition_of_october_4_is_counted_unevaluable() -> None:
    """191 reads, 2 of them set, 4 observed transitions. 0xFFFF3CDC was never
    read, so none is scored: the count is the capture requirement, not a pass."""
    frame = _slice()
    flags = frame[frame["channel"] == "mem:ECM_PEDAL_ENRICH_FLAGS"]
    reads = [
        FlagRead(float(t), bool(int(v) & 1))
        for t, v in zip(flags["acquired_at"], flags["value_number"], strict=True)
    ]
    result = account_pedal_flag(reads, CEDB)
    assert result == {
        "model_version": MODEL_VERSION,
        "reads": 191,
        "scored": {"pass": 0, "miss": 0},
        "unevaluable": {"input_not_captured:0xFFFF3CDC": 191},
        "transitions": {"observed": 4, "pass": 0, "miss": 0, "unevaluable": 4},
    }
    # The same window carries 0xFFFF2842: 0x04 on every read, so the knock
    # control's trim-difference branch (0x04A512) was bypassed all drive.
    assert set(flags["payload"].map(lambda p: p[1])) == {0x04}


def test_a_captured_comparand_is_scored_and_a_band_read_is_not() -> None:
    reads = [
        FlagRead(0.0, False, comparand=150),
        FlagRead(1.0, True, comparand=230),
        FlagRead(2.0, True, comparand=195),  # band: history unknown at a read
        FlagRead(3.0, True, comparand=100),  # a miss stays a miss
    ]
    result = account_pedal_flag(reads, CEDB)
    assert result["scored"] == {"pass": 2, "miss": 1}
    assert result["unevaluable"] == {"hysteresis_band_state_unknown": 1}
    assert result["transitions"] == {"observed": 1, "pass": 1, "miss": 0, "unevaluable": 0}
    sloped = account_pedal_flag([FlagRead(0.0, True, comparand=230)], list(range(16)))
    assert sloped["unevaluable"] == {"input_not_captured:0xFFFF11D8": 1}


# -- UpRev map selection, init and copy -------------------------------------------------


def test_power_on_initialises_only_without_the_magic_and_never_from_unknown_ram() -> None:
    """Synthetic (code): 0x0DE1A4."""
    cold, copied = uprev_power_on(UpRevState(5, 8, 3, 0x12345678, 4, 0x42))
    assert (cold, copied) == (UpRevState(0, 0, 0, UPREV_MAGIC, 0, 0), 1)
    retained = UpRevState(0, 0, 0, UPREV_MAGIC, 3, 0x123)
    assert uprev_power_on(retained) == (retained, None)  # warm: map 3 survives
    with pytest.raises(ValueError, match="not a cold boot"):
        uprev_power_on(UpRevState(None, None, None, None, None))


def _run(state: UpRevState, inputs: list[int]) -> tuple[UpRevState, list[int], list[bool | None]]:
    copies, indicator = [], []
    for value in inputs:
        cycle = uprev_step(state, value).value
        assert cycle is not None
        state = cycle.state
        if cycle.copied is not None:
            copies.append(cycle.copied)
        indicator.append(cycle.indicator)
    return state, copies, indicator


def test_two_presses_while_holding_commit_map_two_and_blink_it_twice() -> None:
    """Synthetic (code): bit 3 held, bit 1 counted on rising edges, commit on
    release from bit 3 alone; the countdown blinks 0xFFFF4858 bit 0."""
    start = UpRevState(0, 0, 0, UPREV_MAGIC, 0, 0)
    presses = [0x08, 0x0A, 0x0A, 0x08, 0x0A, 0x08, 0x00]
    state, copies, _ = _run(start, presses)
    assert copies == [2]
    assert (state.active_map, state.selector, state.countdown, state.history) == (2, 2, 128, 2)
    state, _, indicator = _run(state, [0x00] * 130)
    rises = sum(1 for a, b in zip([False, *indicator], indicator, strict=False) if b and not a)
    assert rises == 2 and state.countdown == 0 and indicator[-1] is None
    assert state.selector == 0  # cleared by the idle cycles


def test_a_release_from_both_bits_or_an_invalid_selector_commits_nothing() -> None:
    start = UpRevState(0, 0, 0, UPREV_MAGIC, 3, 0)
    state, copies, _ = _run(start, [0x08, 0x0A, 0x00])
    # Released from 0x0A: no commit; the selector is cleared one cycle later.
    assert copies == [] and state.active_map == 3 and state.selector == 1
    state, copies, _ = _run(state, [0x00])
    assert copies == [] and state.selector == 0
    six = [0x08] + [0x0A, 0x08] * 6 + [0x00]
    state, copies, _ = _run(start, six)
    assert copies == [] and state.active_map == 3 and state.history == 6  # attempted, refused
    unknown = uprev_step(UpRevState(0, None, 0, UPREV_MAGIC, 3), 0x08)
    assert (unknown.value, unknown.reason) == (None, "uprev_debounce_unknown")


def test_the_loaded_slot_follows_the_committed_byte_or_the_init_default() -> None:
    assert loaded_slot(UPREV_MAGIC, 0).value == (1, "init_default")
    assert loaded_slot(UPREV_MAGIC, 4).value == (4, "committed")
    assert loaded_slot(0, 0).reason == "uprev_not_initialised"
    assert loaded_slot(None, 0).reason == "uprev_state_unobserved"
    assert loaded_slot(UPREV_MAGIC, 7).reason == "uprev_active_map_invalid"


def test_october_4_ran_slot_one_by_init_on_every_read_with_no_switch() -> None:
    """Fixture: the 6-byte window from 0xFFFFB003 (selector, magic, map)."""
    frame = _slice()
    window = frame[frame["channel"] == "mem:ECM_ACTIVE_MAP"]
    reads = []
    for t, payload in zip(window["acquired_at"], window["payload"], strict=True):
        reads.append((float(t), int.from_bytes(payload[1:5], "big"), payload[5], payload[0]))
    assert account_uprev_reads(reads) == {
        "model_version": MODEL_VERSION,
        "reads": 196,
        "slots": {"1:init_default": 196},
        "unevaluable": {},
        "map_changes": {"observed": 0, "unevaluable": 0},
    }


# -- the enrichment request and committed state (synthetic, code) -------------------------


def _inputs(**overrides: Any) -> EnrichmentInputs:
    base: dict[str, Any] = {
        "coolant": 140,
        "rpm_coarse": 50,
        "gated_lookup": 0x95,
        "pedal_flag": False,
        "pressure_10c5": 0,
        "tick": True,
    }
    return EnrichmentInputs(**{**base, **overrides})


KNOWN = EnrichmentState(
    latches=EnrichmentLatches(True, False, False, False, True),
    commit_flag=False,
    timer_1742=0,
    timer_1744=0,
    timer_1746=0,
    timer_1748=0,
    committed=1,
)


def test_the_request_follows_coolant_gate_band_and_pedal() -> None:
    cycle = enrichment_step(KNOWN, _inputs(coolant=100))
    assert (cycle.request, cycle.committed) == (8, 5)
    cycle = enrichment_step(KNOWN, _inputs(gated_lookup=0x80))
    assert (cycle.request, cycle.committed) == (1, 1)
    assert enrichment_step(KNOWN, _inputs()).request == 2  # below 3,100 rpm
    assert enrichment_step(KNOWN, _inputs(rpm_coarse=64)).request == 4  # 3,200 rpm
    assert enrichment_step(KNOWN, _inputs(rpm_coarse=70)).request == 6  # 3,500 rpm
    assert enrichment_step(KNOWN, _inputs(rpm_coarse=70, pedal_flag=True)).request == 7
    # 0xFFFF2866 bit 0: set by 0xFFFF10C5 > 0xFD with the pedal flag, else kept.
    cycle = enrichment_step(KNOWN, _inputs(pedal_flag=True, pressure_10c5=0xFE))
    assert (cycle.request, cycle.committed, cycle.state.commit_flag) == (3, 4, True)
    cycle = enrichment_step(cycle.state, _inputs(pedal_flag=True, pressure_10c5=0))
    assert (cycle.committed, cycle.state.commit_flag) == (4, True)
    assert enrichment_step(cycle.state, _inputs()).state.commit_flag is False


def test_an_unknown_latch_inside_its_band_is_unevaluable_not_assumed_cold_or_warm() -> None:
    blank = EnrichmentState()
    cycle = enrichment_step(blank, _inputs(coolant=113))  # 113 + 3 >= 115: band
    assert (cycle.request, cycle.committed, cycle.reason) == (None, None, "warm_latch_unknown")
    cycle = enrichment_step(blank, _inputs(coolant=140, rpm_coarse=59))  # 59 + 4 >= 62
    assert cycle.reason == "rpm_band_latch_unknown"
    cycle = enrichment_step(blank, _inputs(coolant=140, rpm_coarse=40))
    assert (cycle.request, cycle.reason) == (2, "timer_unknown")


def _hold(state: EnrichmentState, inputs: EnrichmentInputs, cycles: int) -> list[int | None]:
    committed = []
    for _ in range(cycles):
        cycle = enrichment_step(state, inputs)
        state = cycle.state
        committed.append(cycle.committed)
    return committed


def test_transition_timing_is_counted_in_ticks_because_the_tick_rate_is_not_decoded() -> None:
    # Request 2 holds 2 for 30 ticks (0xFFFF1748 reaches 15 << 8 at +0x80), then 3.
    assert _hold(KNOWN, _inputs(), 32) == [2] * 30 + [3] * 2
    # Request 4: 2 while 0xFFFF1742 >> 8 < 9 after its step (17 ticks), 6 until
    # it reaches 25 << 8 (50 ticks), then 3.
    assert _hold(KNOWN, _inputs(rpm_coarse=64), 52) == [2] * 17 + [6] * 33 + [3] * 2
    # An unknown tick leaves the timers unknown: the hold stops being evaluable.
    unknown_tick = _hold(KNOWN, _inputs(tick=None), 2)
    assert unknown_tick == [2, None]
    # Request 1 with a known tick zeroes 0xFFFF1748 again; a timer it only
    # decrements (0xFFFF1746) stays unknown.
    lost = enrichment_step(KNOWN, _inputs(tick=None)).state
    assert (lost.timer_1746, lost.timer_1748, lost.timer_1742) == (None, None, 0)
    reset = enrichment_step(lost, _inputs(gated_lookup=0x80)).state
    assert (reset.timer_1748, reset.timer_1746) == (0, None)


# -- the timing-trim lookup (E05.4: an intermediate, never final timing) ------------------

RPM_AXIS = [8, 12, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120]
LOAD_AXIS = [8 * (i + 1) for i in range(16)]


def _table(base: int, flag_from_column: int = 16) -> list[list[int]]:
    return [
        [(base + r + c) | (0x80 if c >= flag_from_column else 0) for c in range(16)]
        for r in range(16)
    ]


def test_the_trim_lookup_masks_cells_and_carries_the_lower_left_flag() -> None:
    table = _table(40, flag_from_column=8)
    exact = trim_lookup(table, RPM_AXIS, LOAD_AXIS, 24 * 4, 64)  # row 3, column 7
    assert (exact.value, exact.trim, exact.flag) == (50, 50, False)
    # Half way from column 7 (unflagged, 50) to column 8 (flagged, 51 masked).
    between = trim_lookup(table, RPM_AXIS, LOAD_AXIS, 24 * 4, 68)
    assert (between.trim, between.flag) == (50, False)
    # From the flagged column the flag comes along; the masked value interpolates.
    flagged = trim_lookup(table, RPM_AXIS, LOAD_AXIS, 24 * 4, 76)
    assert (flagged.trim, flagged.flag) == (51, True)
    # Half way down a row: 0x027194 compares the rpm word with axis byte x 4.
    rows = trim_lookup(table, RPM_AXIS, LOAD_AXIS, 28 * 4, 64)
    assert rows.trim == 50 and rows.lookup.row_fraction == 0x8000
    # Past both ends: the corner cell, no fraction.
    assert trim_lookup(table, RPM_AXIS, LOAD_AXIS, 0xFFFF, 0xFF).value == (40 + 30) | 0x80


def _selected(**overrides: Any) -> Any:
    args: dict[str, Any] = {
        "table_a": _table(40),
        "table_b": _table(30),
        "rpm_axis": RPM_AXIS,
        "load_axis": LOAD_AXIS,
        "rpm_word": 24 * 4,
        "load_byte": 64,
        "coolant": 140,
        "pedal_flag": False,
        "high_det": True,
    }
    return selected_timing_trim(**{**args, **overrides})


def test_the_selected_trim_follows_coolant_the_high_det_latch_and_the_pedal_column() -> None:
    warm = _selected()
    assert (warm.selected.value.trim, warm.main.value.trim) == (40, 50)  # B, A
    assert _selected(high_det=False).selected.value.trim == 50
    cold = _selected(coolant=106)
    assert (cold.selected.value.trim, cold.main.value.trim) == (50, 50)  # A only
    blend = _selected(coolant=108)
    assert blend.selected.reason == blend.main.reason == "coolant_blend_band"
    assert _selected(coolant=111).selected.value is not None  # first warm byte
    unknown = _selected(high_det=None)
    assert unknown.selected.reason == "high_det_latch_unknown" and unknown.main.value is not None
    assert _selected(high_det=None, coolant=100).selected.value is not None
    pedal = _selected(pedal_flag=True)
    assert pedal.main.value.lookup.column.index == 15  # load input 0xFF
    assert _selected(pedal_flag=None).selected.reason == "pedal_flag_unknown"


def test_the_lookup_reads_k106s_edit_where_the_axis_roles_put_it() -> None:
    """Private ROM: an axis-role and table-identity check, not an
    independent validation of the timing. k106's ten edited cells (one or two
    counts) are trim A rows 7-9 x columns 12-15: rpm axis bytes 56-72, about
    2,800-3,600 rpm, at the four highest load columns, which fits its "added
    spark at high load" note only with rows read as rpm. On the grid the
    lookup sees exactly those cells change and trim B none; off the grid it
    interpolates them as hand-worked from the cells below."""
    from tests.rom_fixtures import operator_image

    base, k106 = operator_image("base"), operator_image("k106")
    if base is None or k106 is None:
        pytest.skip("operator ROMs not on this machine")

    def table(image: bytes, offset: int) -> list[list[int]]:
        start = 0x0E0000 + offset
        return [list(image[start + 16 * r : start + 16 * r + 16]) for r in range(16)]

    rpm_axis, load_axis = list(base[0xCC93 : 0xCC93 + 16]), list(base[0xCC83 : 0xCC83 + 16])
    assert rpm_axis == RPM_AXIS and load_axis == LOAD_AXIS
    for offset, expected in ((0x020, 10), (0x120, 0)):
        changed = {}
        for r in range(16):
            for c in range(16):
                at = (rpm_axis[r] * 4, load_axis[c])
                before = trim_lookup(table(base, offset), rpm_axis, load_axis, *at).trim
                after = trim_lookup(table(k106, offset), rpm_axis, load_axis, *at).trim
                if before != after:
                    changed[(r, c)] = after - before
        assert len(changed) == expected
        assert all(7 <= r <= 9 and 12 <= c <= 15 for r, c in changed)
        assert set(changed.values()) <= {1, 2}
    # Off-grid, by hand from the masked cells (base / k106):
    #   row 7 cols 12-13: 49 50 / 50 51; row 8 cols 12-15: 51 51 51 51 / 52 52 52 51;
    #   row 9 cols 14-15: 50 50 / 51 52. Every cell carries the bit-7 flag.
    # (a) rpm word 240 (half way 224 -> 256), load 108 (half way 104 -> 112):
    #     k106 upper 50 + (1 x 0x8000 >> 16) = 50, lower 52, 50 + (2 x 0x8000 >> 16) = 51;
    #     base upper 49, lower 51, 50.
    # (b) rpm word 232 (0x4000), load 110 (0xC000):
    #     k106 upper 50 + (0xC000 >> 16) = 50, lower 52, 50 + (2 x 0x4000 >> 16) = 50;
    #     base 49 + 0 = 49, lower 51, 49 + 0 = 49.
    # (c) rpm word 272 (half way 256 -> 288), load 124 (half way 120 -> 128):
    #     k106 upper 52 - (1 x 0x8000 >> 16) = 52, lower 51 + 0 = 51, 52 - 0 = 52
    #     (truncated toward the first operand); base 51, 50, 51.
    a_base, a_k106 = table(base, 0x020), table(k106, 0x020)
    for (word, load), (want_base, want_k106) in {
        (240, 108): (50, 51),
        (232, 110): (49, 50),
        (272, 124): (51, 52),
    }.items():
        got_base = trim_lookup(a_base, rpm_axis, load_axis, word, load)
        got_k106 = trim_lookup(a_k106, rpm_axis, load_axis, word, load)
        assert (got_base.trim, got_k106.trim) == (want_base, want_k106), (word, load)
        assert got_base.flag and got_k106.flag


# -- E05.4 / E09.7: the ignition chain, node by node (synthetic, code) ------------------


def test_the_trim_term_masks_the_flag_and_floors_at_zero() -> None:
    # 0x04BD24: 1350 = 1777 & 0x7F, 1352 = 177A & 0x7F, 2793 = max(0, 1350 - 11CB).
    terms = timing_trim_terms(
        selected=0x80 | 50, main=52, subtrahend_11cb=3, pair_term_enabled=False
    )
    assert (terms.selected, terms.pair, terms.main, terms.term) == (50, 0, 52, 47)
    assert (
        timing_trim_terms(selected=50, main=52, subtrahend_11cb=3, pair_term_enabled=True).pair == 2
    )  # cal 0x8ADE bit 1: max(0, 52 - 50)
    assert (
        timing_trim_terms(selected=5, main=5, subtrahend_11cb=7, pair_term_enabled=False).term == 0
    )


def test_the_base_sum_adds_the_trim_term_and_clamps_to_a_signed_byte() -> None:
    plain = dict(w1d3e=0x40, w27c6=30, w2793=47, w279a=2, w134a=0, w174a=0, w1344=0, w27c3=15)
    # 64 + 30 + 47 - 2 - 0 - 0 - 128 + 0 = 11
    assert base_timing_sum(alternate_path=False, **plain) == 11
    # The 0xFFFF2833 bit 4 sum: 64 + 15 - 2 - 64 = 13, no trim term.
    assert base_timing_sum(alternate_path=True, **plain) == 13
    assert base_timing_sum(alternate_path=True, **{**plain, "w2793": 0}) == 13
    high = {**plain, "w1d3e": 255, "w27c6": 127, "w2793": 68}
    assert base_timing_sum(alternate_path=False, **high) == 127  # 322 clamped
    low = {**plain, "w1d3e": 0, "w27c6": -128, "w279a": 255}
    assert base_timing_sum(alternate_path=False, **low) == -128
    with pytest.raises(ValueError):
        base_timing_sum(alternate_path=False, **{**plain, "w27c6": 128})


def test_the_select_takes_the_coolant_value_on_its_flags_unless_the_mode_byte_holds() -> None:
    args = dict(w1a58=11, w1a59=30, mode_127d=0)
    assert base_timing_select(flag_4785_bit6=False, flag_2833_bit4=False, **args) == 11
    assert base_timing_select(flag_4785_bit6=True, flag_2833_bit4=False, **args) == 30
    assert base_timing_select(flag_4785_bit6=False, flag_2833_bit4=True, **args) == 30
    for mode in (1, 2):
        held = {**args, "mode_127d": mode}
        assert base_timing_select(flag_4785_bit6=True, flag_2833_bit4=True, **held) == 11
    assert timing_cycle_copy(11) == 11


def test_the_retard_combine_takes_the_larger_retard_and_its_two_paths() -> None:
    args: dict[str, Any] = dict(
        w274e=25, w1215=0, w26d2=-5, w272a=0, w2334=0, w46e6=0,
        flag_28ab_bit4=False, bypass_28b4_bit1=False,
    )  # fmt: skip
    active = timing_retard_combine(knock_active=True, **args)
    assert (active.retard, active.w2338, active.combined) == (-5, 20, 20)
    assert timing_retard_combine(knock_active=False, **args).combined == 25
    # min(26D2, -1215): the larger retard wins.
    assert (
        timing_retard_combine(knock_active=True, **{**args, "w1215": 3, "w26d2": -1}).retard == -3
    )
    # 46E6 path: 274E + min(2332 + 272A, -46E6) - 2334 = 25 + min(-3, -4) - 1 = 20.
    capped = timing_retard_combine(
        knock_active=True, **{**args, "w46e6": 4, "w272a": 2, "w2334": 1}
    )
    assert (capped.w2338, capped.combined) == (None, 20)
    # 28AB bit 4 takes the plain path even with 46E6 set: 25 - 5 + 2 - 1 = 21.
    plain = {**args, "w46e6": 4, "w272a": 2, "w2334": 1, "flag_28ab_bit4": True}
    assert timing_retard_combine(knock_active=True, **plain).combined == 21
    bypass = timing_retard_combine(knock_active=True, **{**args, "bypass_28b4_bit1": True})
    assert (bypass.w2338, bypass.combined) == (None, 25)
    assert (
        timing_retard_combine(knock_active=False, **{**args, "w274e": 127, "w272a": 127}).combined
        == 127
    )
    # 0xFFFF1215 is a signed byte (0x04DF4C mov.b, neg, no extu.b): raw 0xFE
    # is -2, so -1215 is +2 of advance, and the knock min still applies.
    raw_fe = 0xFE - 0x100
    assert timing_retard_combine(knock_active=False, **{**args, "w1215": raw_fe}).retard == 2
    assert timing_retard_combine(knock_active=False, **{**args, "w1215": -128}).combined == 127
    assert timing_retard_combine(knock_active=True, **{**args, "w1215": raw_fe}).retard == -5
    with pytest.raises(ValueError):
        timing_retard_combine(knock_active=False, **{**args, "w1215": 0xFE})
    # 0xFFFF46E6 is a byte (0x04E05E mov.b, extu.b): 0x80 is 128, not -128.
    late = timing_retard_combine(knock_active=False, **{**args, "w46e6": 0x80})
    assert (late.w2338, late.combined) == (None, 25 - 128)
    with pytest.raises(ValueError):
        timing_retard_combine(knock_active=False, **{**args, "w46e6": 0x100})


def test_the_rate_limit_steps_then_caps_and_its_fixed_point_ignores_the_step() -> None:
    def seg(v: int, prev: int, **kw: Any) -> int:
        args: dict[str, Any] = dict(trim_62b4=0, retard_9932=None, step_up=4, first_run=False)
        return timing_rate_limit(w1346=v, previous_232e=prev, **{**args, **kw})

    assert seg(30, 10) == 14  # rise at most +4
    assert seg(30, 10, step_up=15) == 25
    assert seg(10, 40) == 25  # fall at most -15
    assert seg(60, 50) == 50  # the 50 cap
    assert seg(-30, -7) == -7  # -22 after the step, then the -7 floor
    assert seg(30, 20, retard_9932=-20) == 20  # retard clamped to -10
    assert seg(30, 30, retard_9932=5) == 30  # and never an advance
    assert seg(30, 30, trim_62b4=2) == 32
    assert seg(30, 30, first_run=True) == 0
    for value, want in ((30, 30), (60, TIMING_CAP), (-30, TIMING_FLOOR), (-7, -7)):
        assert timing_rate_limit_steady(value, step_up=4).value == want
        prev = TIMING_FLOOR
        for _ in range(40):
            prev = seg(value, prev)
        assert prev == want, value  # iterating the segment reaches the same point
    assert timing_rate_limit_steady(30, step_up=0).reason == "rate_limit_rise_disabled"


def test_the_final_byte_branches() -> None:
    assert timing_final_byte(w232e=30, branch="normal", forced_4858_bit5=False) == 80
    assert timing_degrees_btdc(80) == 30
    assert timing_final_byte(w232e=30, branch="flag_28b1_bit5", forced_4858_bit5=False) == 50
    assert timing_final_byte(w232e=-7, branch="flag_28b1_bit5", forced_4858_bit5=False) == 80
    assert timing_final_byte(w232e=30, branch="fault_plain", forced_4858_bit5=False) == 80
    assert timing_final_byte(w232e=30, branch="fault_offset", forced_4858_bit5=False) == 90
    assert timing_final_byte(w232e=30, branch="normal", forced_4858_bit5=True) == 0x5A


def _chain_degrees(selected_trim: int) -> int:
    """All seven nodes at one stated state (every unidentified word a stated
    number), steady, to degrees BTDC."""
    terms = timing_trim_terms(
        selected=0x80 | selected_trim, main=52, subtrahend_11cb=2, pair_term_enabled=False
    )
    s = base_timing_sum(
        alternate_path=False, w1d3e=0x40, w27c6=24, w2793=terms.term, w279a=1,
        w134a=0, w174a=0, w1344=0, w27c3=0,
    )  # fmt: skip
    e274 = timing_cycle_copy(
        base_timing_select(
            w1a58=s, w1a59=30, flag_4785_bit6=False, flag_2833_bit4=False, mode_127d=0
        )
    )
    combined = timing_retard_combine(
        w274e=e274, w1215=0, w26d2=-5, knock_active=True, w272a=0, w2334=0, w46e6=0,
        flag_28ab_bit4=False, bypass_28b4_bit1=False,
    ).combined  # fmt: skip
    w232e = timing_rate_limit_steady(combined + 1, step_up=4).value
    assert w232e is not None
    return timing_degrees_btdc(
        timing_final_byte(w232e=w232e, branch="normal", forced_4858_bit5=False)
    )


def test_one_trim_count_is_one_degree_through_all_seven_nodes() -> None:
    """The code's prediction for N01.2's "degrees per count": every node from
    0xFFFF2793 to 0xFFFF232E adds without scaling, so with no clamp binding a
    trim change of n counts is n degrees BTDC."""
    # 64 + 24 + (50 - 2) - 1 - 128 = 7; - 5 knock retard = 2; + 1 = 3 deg.
    assert _chain_degrees(50) == 3
    for n in (-3, -1, 1, 2):
        assert _chain_degrees(50 + n) - _chain_degrees(50) == n


def _sel(trim: int) -> Any:
    table = [[0x80 | trim] * 16 for _ in range(16)]
    return Step(trim_lookup(table, RPM_AXIS, LOAD_AXIS, 200, 64))


def test_the_timing_delta_says_which_kind_of_answer_it_is() -> None:
    d = timing_delta(_sel(50), _sel(47))
    assert (d.status, d.delta_deg, d.reasons) == (
        "delta_if_no_clamp",
        -3,
        ("clamp_headroom_unknown",),
    )
    assert timing_delta(_sel(50), _sel(50)).reasons == ()  # no change, no clamp question
    at = timing_delta(_sel(50), _sel(53), base_timing_deg=30)
    assert (at.status, at.base_deg, at.candidate_deg, at.delta_deg) == ("computed", 30, 33, 3)
    cap = timing_delta(_sel(50), _sel(53), base_timing_deg=48)
    assert (cap.delta_deg, cap.candidate_deg, cap.reasons) == (2, 50, ("timing_cap_binds",))
    assert timing_delta(_sel(50), _sel(53), base_timing_deg=50).reasons == ("base_timing_at_clamp",)
    held = timing_delta(_sel(50), _sel(53), held_reasons=["term_279a_unmodelled"])
    assert (held.status, held.delta_deg, held.trim_term_delta) == ("unevaluable", None, 3)
    blend = timing_delta(Step(None, "coolant_blend_band"), _sel(50))
    assert (blend.status, blend.reasons) == ("unevaluable", ("coolant_blend_band",))
    assert timing_delta(_sel(5), _sel(6)).reasons == ("trim_term_floor_may_bind",)
