"""E05.2: BFS formation, integer for integer, and its steady-state twin.

Code-derived (synthetic) known answers for :mod:`src.decode.air_charge` --
each one hand-worked from the listing of ``0x03C62C`` / ``0x03CC3C`` -- and the
adapter's contracts in :mod:`src.analysis.air_charge_twin`: the frozen gates,
the pairing it does itself, and that observed BFS is never an input. No ROM
and no session here; the image-backed and drive-backed checks are
``tests/test_air_charge_private_rom.py``.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src.analysis import air_charge_twin as twin
from src.analysis.ecu_application import load_application_model
from src.decode import air_charge as ac

REPO = Path(__file__).resolve().parents[1]
PROFILE = REPO / "data" / "nissan-xterra-2008"


# -- Tp (0x03C646-0x03C7AA) -----------------------------------------------------------


def _floor_formula(qa: int, k: int, speed: int, m: int = 226, d: int = 3) -> int:
    n = max(ac.SPEED_FLOOR, speed)
    quotient = min(ac.TP_QUOTIENT_CAP, (k * qa * m // 256) // (d * n))
    return (quotient * ac.TP_SCALE) >> 16


def test_tp_hand_worked_at_2000_rpm() -> None:
    """K 28,715 (20260920), airflow 5,000, 2,000 rpm (w1204 = 2000 / 1.5625 =
    1,280): floor(28715 x 5000 x 226 / 256) = 126,749,804; / (3 x 1280) =
    33,007; x 0x23CB >> 16 = 302,443,141 >> 16 = 4,614."""
    result = ac.air_charge_tp(5000, 28715, 1280, flow_multiplier=226, speed_multiplier=3)
    assert (result.quotient, result.tp) == (33007, 4614)


def test_the_two_step_24_8_divide_is_the_floor_formula_below_the_wrap() -> None:
    rng = random.Random(52)
    for _ in range(5000):
        qa = rng.randrange(1, 1 << 24)
        k = rng.randrange(0, 0x10000)
        speed = rng.randrange(0, 0x10000)
        got = ac.air_charge_tp(qa, k, speed, flow_multiplier=226, speed_multiplier=3).tp
        assert got == _floor_formula(qa, k, speed), (qa, k, speed)


def test_the_quotient_cap_keeps_tp_a_word() -> None:
    """K 256, speed 64 (d x N = 192): total = 226 x airflow. Around the cap
    0x726F9 the two-step divide and the 0x727 short cut agree with the floor
    formula, and the cap is exactly 0xFFFF after the scale."""
    for qa in range(398_200, 398_230):
        got = ac.air_charge_tp(qa, 256, 64, flow_multiplier=226, speed_multiplier=3)
        assert got.tp == _floor_formula(qa, 256, 64)
        assert got.quotient <= ac.TP_QUOTIENT_CAP
    capped = ac.air_charge_tp(10**6, 28715, 64, flow_multiplier=226, speed_multiplier=3)
    assert (capped.quotient, capped.tp) == (ac.TP_QUOTIENT_CAP, 0xFFFF)


def test_tp_floors_speed_zero_airflow_and_a_zero_divisor() -> None:
    low = ac.air_charge_tp(5000, 28715, 10, flow_multiplier=226, speed_multiplier=3)
    assert low == ac.air_charge_tp(5000, 28715, 0x40, flow_multiplier=226, speed_multiplier=3)
    for qa in (0, -1, -0x80000000):  # cmp/pl: only a positive long is airflow
        assert ac.air_charge_tp(qa, 28715, 1280, flow_multiplier=226, speed_multiplier=3).tp == 0
    zero = ac.air_charge_tp(5000, 28715, 1280, flow_multiplier=226, speed_multiplier=0)
    assert zero.quotient == ac.TP_QUOTIENT_CAP


def test_tp_refuses_what_the_ecm_cannot_hold() -> None:
    with pytest.raises(ValueError, match="not a whole raw count"):
        ac.air_charge_tp(5000.5, 28715, 1280, flow_multiplier=226, speed_multiplier=3)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="outside"):
        ac.air_charge_tp(5000, 0x10000, 1280, flow_multiplier=226, speed_multiplier=3)
    with pytest.raises(ValueError, match="outside"):
        ac.air_charge_tp(1 << 31, 28715, 1280, flow_multiplier=226, speed_multiplier=3)


def test_the_speed_source_and_the_running_branch_follow_the_flags() -> None:
    common = {"speed_1204": 1280, "speed_233c": 1270, "count_2c91": 0x30}
    assert ac.select_tp_speed(flags_4786=0, flags_283b=0, **common) == 1280
    assert ac.select_tp_speed(flags_4786=0, flags_283b=0x40, **common) == 1270
    assert ac.select_tp_speed(flags_4786=0x40, flags_283b=0x40, **common) == 0x180
    assert ac.running_branch(flags_4786=0, start_hold_4a2d=3, count_2c91=1)
    assert not ac.running_branch(flags_4786=0, start_hold_4a2d=0, count_2c91=0)
    assert not ac.running_branch(flags_4786=0x80, start_hold_4a2d=1, count_2c91=9)
    assert not ac.running_branch(flags_4786=0x40, start_hold_4a2d=0, count_2c91=0x1F)
    assert ac.running_branch(flags_4786=0x40, start_hold_4a2d=0, count_2c91=0x20)


# -- Fuel Comp and its product ----------------------------------------------------------


def test_the_fuel_comp_lookup_compares_rpm_axis_bytes_times_four() -> None:
    """Row axis bytes are 50 rpm (the word is 12.5 rpm, compared with byte <<
    2); column bytes compare with the load byte as they are."""
    cells = [[128 + r * 4 + c for c in range(2)] for r in range(2)]
    # rpm word 200 (2,500 rpm) is half way between bytes 40 and 60 (x4 = 160, 240).
    assert ac.rpm_load_lookup(cells, [40, 60], [10, 30], 200, 10) == 130
    # load byte 20 half way: from 130 toward 131 by 0x8000 -> 130.
    assert ac.rpm_load_lookup(cells, [40, 60], [10, 30], 200, 20) == 130
    assert ac.rpm_load_lookup(cells, [40, 60], [10, 30], 240, 30) == 133
    assert ac.fuel_comp_product(4614, 128) == 4614
    assert ac.fuel_comp_product(4614, 131) == 4614 * 131 >> 7
    assert ac.fuel_comp_product(0xFFFF, 255) == 0xFFFF


# -- the fill coefficient, filter, transient, blend and BFS ---------------------------------


def test_the_fill_coefficient_is_three_wrapping_products() -> None:
    # T 100, gain 58, rpm word 160 (2,000 rpm): 100 x 58 x 160 x 55 >> 16 = 778.
    got = ac.fill_coefficient(160, 100, gain=58, transient_model=True, default=819)
    assert got == ac.FillCoefficient(w1164=778, a=778, b=0x8000 - 778)
    off = ac.fill_coefficient(160, 100, gain=58, transient_model=False, default=819)
    assert (off.w1164, off.a, off.b) == (778, 819, 0x8000 - 819)
    big = ac.fill_coefficient(0xFFFF, 255, gain=255, transient_model=True, default=819)
    assert big.w1164 == min(0x7FFF, ((((255 * 255) * 0xFFFF) & 0xFFFFFFFF) * 55 & 0xFFFFFFFF) >> 16)


def test_the_filter_closes_a_stall_with_its_plus_one_rule() -> None:
    """w105C 1,000 (target 4,000), a = 819: from 3,990, (4000 x 819 + 3990 x
    31949) >> 15 = 3,990 -- a stall -- so the firmware adds one."""
    assert ac.fill_filter_step(1000, 819, 0x8000 - 819, 3990, 3990) == 3991
    # The rule needs the stored word to equal the computed one.
    assert ac.fill_filter_step(1000, 819, 0x8000 - 819, 3990, 3989) == 3990
    assert ac.fill_filter_step(1000, 819, 0x8000 - 819, 3900, 3900) == 3902


@pytest.mark.parametrize("c", [0, 1, 37, 1000, 9000, 16_383, 16_384, 20_000, 0xFFFF])
@pytest.mark.parametrize("a", [64, 819, 0x3000, 0x7FFF, 0x8000])
def test_the_filter_settles_on_one_fixed_point_from_either_side(c: int, a: int) -> None:
    target = ac.steady_state_bfs(c, 255)
    b = max(0, 0x8000 - a)
    for start in (0, target // 2, target, min(0xFFFF, target + 5000), 0xFFFF):
        y = start
        for _ in range(5_000):
            nxt = ac.fill_filter_step(c, a, b, y, y)
            if nxt == y:
                break
            y = nxt
        assert min(y, 0xFF00) == target, (c, a, start, y)
        assert ac.fill_filter_step(c, a, b, y, y) == y


def test_the_filter_saturates_and_has_a_subtracting_form() -> None:
    assert ac.fill_filter_step(0x8000, 0x7FFF, 1, 0, 0) == 0xFFFF  # w105C x a >= 2^30
    assert ac.fill_filter_step(100, 100, 0x8000 - 100, 5000, 5000, reverse=True) == (
        (5000 * (0x8000 - 100) - 4 * 100 * 100) >> 15
    )
    assert ac.fill_filter_step(1000, 0x7FFF, 1, 1, 1, reverse=True) == 0


def test_the_transient_is_an_arithmetic_shift_clamped_to_a_signed_word() -> None:
    assert ac.fill_transient(1001, 1000, gain=160, active=True) == 2  # 160 >> 6
    assert ac.fill_transient(1000, 1001, gain=160, active=True) == -3  # -160 >> 6, floor
    assert ac.fill_transient(0, 0xFFFF, gain=160, active=True) == -0x8000
    assert ac.fill_transient(0xFFFF, 0, gain=160, active=True) == 0x7FFF
    assert ac.fill_transient(2000, 1000, gain=160, active=False) == 0
    active = {"flags_283c": 0x0C, "near_wot": False, "maf_fault_hold": 0, "throttle_fault_hold": 0}
    assert ac.transient_active(**active)
    for change in (
        {"flags_283c": 0x08},
        {"flags_283c": 0x04},
        {"near_wot": True},
        {"maf_fault_hold": 1},
        {"throttle_fault_hold": 4},
    ):
        assert not ac.transient_active(**{**active, **change})  # type: ignore[arg-type]


def test_the_near_wot_latch_blend_and_bfs_word() -> None:
    assert ac.near_wot_next(None, 202, 4000, 1000, set_at=202, clear_below=200) is True
    assert ac.near_wot_next(None, 202, 3999, 1000, set_at=202, clear_below=200) is None
    assert ac.near_wot_next(True, 201, 0, 1000, set_at=202, clear_below=200) is True
    assert ac.near_wot_next(True, 199, 9000, 1000, set_at=202, clear_below=200) is False
    assert ac.blend_weight(0, gain=255) == 0x1A
    assert ac.blend_weight(100, gain=255) == 255  # 25,500 >> 5 = 796, capped
    assert ac.blend_weight(10, gain=255) == 79
    assert ac.fill_blend(4000, 3000, 64, active=True) == (4000 * 64 + 3000 * 192) >> 8
    assert ac.fill_blend(4000, 3000, 64, active=False) == 4000
    assert ac.bfs_word(4000, -5000, 255) == 0
    assert ac.bfs_word(0xFFFF, 0x7FFF, 255) == 0xFF00
    assert ac.bfs_word(4000, 120, 0x10) == 0x1000
    assert ac.steady_state_bfs(20_000, 255) == 0xFF00
    assert ac.steady_state_bfs(9000, 255) == 36_000


# -- what was reported --------------------------------------------------------------------


def test_every_reported_maf_word_inverts_to_exactly_its_airflow_words() -> None:
    top = ac.maf_report_word(0xFFFF, scale=149)
    assert top == 38_143  # 381 g/s: the 0xFFFF cap never binds at this scale
    previous_hi = -1
    for word in range(top + 1):
        interval = ac.maf_word_interval(word, scale=149)
        assert interval.lo == previous_hi + 1
        assert ac.maf_report_word(interval.lo, scale=149) == word
        assert ac.maf_report_word(interval.hi, scale=149) == word
        previous_hi = interval.hi
    assert previous_hi == 0xFFFF
    for unreachable in (top + 1, 0xFFFF):
        with pytest.raises(ValueError, match="largest report"):
            ac.maf_word_interval(unreachable, scale=149)
    with pytest.raises(ValueError, match="scale of 0"):
        ac.maf_word_interval(100, scale=0)


def test_the_speed_word_spans_eight_w1204_counts() -> None:
    assert ac.speed_1204_interval(160) == ac.WordInterval(1280, 1287)
    assert ac.speed_1204_interval(0x2000).hi == 0xFFFF


# -- the adapter ----------------------------------------------------------------------------


def _calibration(**changes: object) -> twin.AirChargeCalibration:
    cells = tuple(tuple(128 + ((r + c) % 5) - 2 for c in range(16)) for r in range(16))
    base = twin.AirChargeCalibration(
        image_sha256="0" * 64,
        slot=1,
        k=28715,
        flow_multiplier=226,
        speed_multiplier=3,
        maf_scale=149,
        fuel_comp=cells,
        fuel_comp_rpm_axis=tuple(range(8, 136, 8)),
        fuel_comp_load_axis=(16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 144),
        ceiling=(255,) * 16,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_the_steady_prediction_is_an_exact_interval_over_its_inputs() -> None:
    cal = _calibration()
    p = twin.predict_steady_bfs(cal, maf_word=1500, rpm_word=160)
    assert p.lo <= p.mid <= p.hi
    assert (p.speed_lo, p.speed_hi) == (1280, 1287)
    assert ac.maf_report_word(p.airflow_lo + ac.AIRFLOW_FILTER_STALL, scale=149) == 1500
    assert (p.fuel_comp_lo, p.fuel_comp_hi) == (126, 130)
    exact = twin.predict_steady_bfs(cal, maf_word=1500, rpm_word=160, load_byte=40)
    assert exact.fuel_comp_exact and p.lo <= exact.lo <= exact.hi <= p.hi
    # Monotone: more air or a higher Fuel Comp never lowers it, more speed never raises it.
    assert twin.predict_steady_bfs(cal, maf_word=1501, rpm_word=160).lo >= p.lo
    assert twin.predict_steady_bfs(cal, maf_word=1500, rpm_word=161).hi <= p.hi
    with pytest.raises(twin.AirChargeRefusalError) as refused:
        twin.predict_steady_bfs(cal, maf_word=0xFFFF, rpm_word=160)
    assert refused.value.reason == "maf_word_unreachable"


def test_the_gates_are_the_ones_declared_before_scoring() -> None:
    """Declared 2026-10-05 before any drive was scored; a change is a new
    evaluation, not an adjustment."""
    assert asdict(twin.FROZEN_GATES) == {
        "max_maf_skew_s": 0.1,
        "max_rpm_skew_s": 0.25,
        "max_load_skew_s": 0.75,
        "window_s": 1.0,
        "max_maf_spread": 0.02,
        "max_rpm_spread": 0.01,
        "min_maf_reads_in_window": 3,
        "min_rpm": 600.0,
        "median_ratio_band": (0.98, 1.02),
        "p10_min": 0.95,
        "p90_max": 1.05,
        "min_scored": 200,
    }


def _rows(samples: list[tuple[float, int, int, int]]) -> pd.DataFrame:
    """Raw telemetry rows as the parquet writer stores them: DIDs as hex text,
    Mode 01 as payload bytes, acquisition time in metadata."""
    rows = []
    for at, bfs, maf, rpm_word in samples:
        meta = json.dumps({"acquired_at": at})
        rows.append(("DID_1208", None, f"{bfs:04x}", None, meta))
        rows.append(("DID_1209", None, f"{maf:04x}", None, json.dumps({"acquired_at": at + 0.002})))
        word = rpm_word * 50
        rows.append(("RPM", rpm_word * 12.5, None, bytes([word >> 8, word & 0xFF]), meta))
    frame = pd.DataFrame(
        rows, columns=["channel", "value_number", "value_text", "payload", "metadata_json"]
    )
    frame["timestamp"] = pd.Timestamp("2026-10-05", tz="UTC")
    return frame


def test_the_evaluator_pairs_raw_reads_and_counts_every_exclusion() -> None:
    cal = _calibration()
    steady_bfs = twin.predict_steady_bfs(cal, maf_word=1500, rpm_word=160).mid
    samples = [(100.0 + 0.2 * i, steady_bfs, 1500, 160) for i in range(20)]
    samples += [(200.0 + 0.2 * i, 9000, 1500 + 60 * i, 160) for i in range(10)]  # MAF moving
    samples += [(300.0, 9000, 1500, 40)]  # 500 rpm
    report = twin.evaluate_steady_bfs(_rows(samples), cal)
    assert report.total == len(samples)
    assert len(report.records) == 20
    assert {r.ratio for r in report.records} == {1.0}
    assert report.excluded["maf_not_steady"] == 10
    assert report.excluded["below_min_rpm"] == 1
    assert not report.passes()  # 20 < min_scored, whatever the ratios
    # The cranking / MAF-fault / subtracting-form flags are never read: every
    # scored sample is counted as assuming it is outside those regions.
    assert report.summary()["regions_assumed"] == {region: 20 for region in twin.REGIONS_NOT_READ}
    assert all(r.regions_assumed == twin.REGIONS_NOT_READ for r in report.records)


def test_observed_bfs_is_scored_never_used() -> None:
    cal = _calibration()
    samples = [(100.0 + 0.2 * i, 7000, 1500, 160) for i in range(20)]
    first = twin.evaluate_steady_bfs(_rows(samples), cal)
    second = twin.evaluate_steady_bfs(_rows([(t, b * 3, m, r) for t, b, m, r in samples]), cal)
    assert [r.prediction for r in first.records] == [r.prediction for r in second.records]


def test_a_channel_without_a_raw_word_rule_is_refused() -> None:
    with pytest.raises(twin.AirChargeRefusalError) as refused:
        twin.evaluate_steady_bfs(_rows([]), _calibration(), maf_channel="MAF_GPS_SMOOTHED")
    assert refused.value.reason == "channel_unsupported"
    with pytest.raises(twin.AirChargeRefusalError):
        twin.evaluate_steady_bfs(_rows([]), _calibration(), bfs_channel="DID_1209")


def test_the_calibration_offsets_read_are_the_manifests() -> None:
    """Every image read of the adapter is cited on the node that consumes it,
    so rom_effects classes an edit to it and check_image covers it."""
    model, _ = load_application_model(PROFILE)
    tp = model.node("air_charge_tp")
    constants = {r.offset for r in tp.rom if r.offset is not None}
    assert {twin.FLOW_MULTIPLIER_OFFSET, twin.SPEED_MULTIPLIER_OFFSET} <= constants
    assert "k_constant" in {r.rom_map for r in tp.rom}
    qa = model.node("airflow_qa")
    assert twin.MAF_REPORT_SCALE_OFFSET in {r.offset for r in qa.rom}
    lookup = {r.rom_map for r in model.node("fuel_comp_lookup").rom}
    assert {"fuel_compensation", "fuel_comp_engine_speed_axis", "fuel_comp_flow_axis"} <= lookup
    assert "bfs_ceiling" in {r.rom_map for r in model.node("base_fuel_schedule").rom}


def test_bfs_is_a_conditional_output_and_target_phi_did_not_move() -> None:
    model, _ = load_application_model(PROFILE)
    bfs = model.output("base_fuel_schedule_word")
    assert bfs.status == "conditional"
    assert [b.name for b in bfs.blockers] == [
        "air_charge_ram_copy_unverified",
        "airflow_unmodelled",
        "fill_byte_not_computed",
        "fill_dynamics_reference_only",
    ]
    # K and Fuel Comp are read from their active-slot RAM copies, which the
    # twin takes from the ROM slot: an assumption, visibly.
    copy = model.node("air_charge_ram_copy")
    assert copy.twin == "assumed"
    assert {0xFFFFB6A0, 0xFFFFB7A2} <= set(copy.writes)
    assert "air_charge_ram_copy" in model.node("air_charge_tp").depends_on
    assert "air_charge_ram_copy" in model.node("fuel_comp_lookup").depends_on
    assert bfs.twin is not None and bfs.twin.predicts == "base_fuel_schedule"
    path = {n.id for n in model.path(bfs.written_by)}
    assert {"air_charge_tp", "fuel_comp_lookup", "fuel_comp_product", "airflow_qa"} <= path
    # X stays observed, so nothing upstream of it reaches target phi's path.
    target = {n.id for n in model.path(model.output("target_equiv_ratio").written_by)}
    shared = {"load_input_x", "engine_speed_word", "uprev_slot_state"}  # all observed
    assert target.isdisjoint(path - shared)
    assert [b.name for b in model.output("target_equiv_ratio").blockers] == [
        "final_command_path_unverified",
        "ram_copy_unverified",
    ]


def test_every_inventoried_air_charge_kernel_exists_and_is_python_only() -> None:
    """The contract lists each E05.2 kernel as Python-only (no operation, no
    Rust symbol, no vectors) and names that gap as a blocker."""
    contract = yaml.safe_load((REPO / "data" / "ecu_twin_contract.yaml").read_text())
    listed = {
        e["python"].split(":")[1]
        for e in contract["inventory"]
        if e["python"].startswith("src.decode.air_charge:")
    }
    assert listed >= {"air_charge_tp", "fuel_comp_product", "steady_state_bfs", "maf_word_interval"}
    for name in listed:
        assert callable(getattr(ac, name)), name
    operations = [op["python"] for op in contract["operations"]]
    assert not any(p.startswith("src.decode.air_charge:") for p in operations)
    assert "air_charge_not_in_contract" in {b["id"] for b in contract["blockers"]}


def test_the_steady_airflow_interval_allows_the_averages_one_count_stall() -> None:
    """0x03C18E-0x03C1C0 with g = 128: rising toward 1,000 the average stops
    at 999, so the airflow long can sit one count below w1056 at a fixed point."""
    level, target = 900, 1000
    for _ in range(200):
        level = ((level * (256 - 128)) >> 4) + ((target * 128) >> 4) >> 4
    assert level == target - ac.AIRFLOW_FILTER_STALL
    words = ac.maf_word_interval(1500, scale=149)
    assert ac.steady_airflow_interval(1500, scale=149) == ac.WordInterval(words.lo - 1, words.hi)
    assert ac.steady_airflow_interval(0, scale=149).lo == 0


@pytest.mark.parametrize(
    ("offset", "flip", "node"),
    [
        (0x874D, 0x01, "airflow_qa"),  # 0xFFFF20E2 would no longer be 0
        (0x86C8, 0x20, "airflow_qa"),  # the write-skip pair becomes reachable
        (0x8A68, 0x01, "airflow_qa"),  # the stall bound
        (0x9066, 0x01, "fill_coefficient"),  # the 0x0333 weight, high byte
        (0x9067, 0x01, "fill_coefficient"),  # and low byte
        (0x8AE1, 0x20, "fuel_comp_lookup"),  # the Fuel Comp table switched off
    ],
)
def test_a_calibration_that_breaks_a_zero_or_a_constant_refuses_by_name(
    offset: int, flip: int, node: str
) -> None:
    from src.analysis.ecu_application import ApplicationModelRefusalError, check_image
    from tests.test_ecu_application_model import _synthetic

    model, _ = load_application_model(PROFILE)
    image, same = _synthetic(model)
    check_image(image, same)
    edited = bytearray(image)
    edited[offset] ^= flip
    with pytest.raises(ApplicationModelRefusalError) as refused:
        check_image(bytes(edited), same)
    assert refused.value.reason == "unmodelled_logic"
    assert refused.value.message.startswith(f"{node}:"), refused.value.message
