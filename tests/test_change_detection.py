"""E03.3 kernel: change detection against a frozen reference (synthetic data).

Every stream here is synthetic and says so; the stored-drive evaluation is
``tests/test_change_evaluation.py``.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest
from pydantic import ValidationError

from src.models.change_detection import (
    Block,
    CellLimits,
    ChangeMonitor,
    ChangePolicy,
    FrozenReference,
    NotAFrozenReferenceError,
    ReferenceRefusedError,
    Sample,
    blocks_of,
    fit_reference,
)

DAY = 86_400.0


def policy_dict() -> dict[str, object]:
    return {
        "schema": "canary-change-detection/2",
        "residual": "synthetic_balance",
        "unit": "percent",
        "features": ["1"],
        "excitation": {
            "condition": "obd:MAF",
            "unit": "g/s",
            "cells": [
                {"name": "idle", "lower": 0.0, "upper": 6.0},
                {"name": "cruise", "lower": 6.0, "upper": 60.0},
            ],
            "min_block_samples": 3,
        },
        "dependence": {
            "block_seconds": 30.0,
            "max_gap_seconds": 10.0,
            "correction": "ar1_variance_inflation",
            "max_within_drive_lag1": 0.5,
        },
        "calibration": {
            "miscoverage": 0.1,
            "n_calibration_drives": 3,
            "n_test_drives": 3,
            "min_train_drives": 2,
            "min_cell_blocks": 8,
            "min_cell_drives": 2,
            "max_bias_fraction": 0.5,
        },
        "detectors": {
            "shift_of_interest": 3.0,
            "cusum_h": 5.0,
            "glr_window_blocks": 30,
            "glr_h": 8.0,
        },
        "drive_level": {
            "cusum_h": 4.0,
            "glr_h": 5.0,
            "window_drives": 10,
            "between_drive_sd_floor": 0.3,
            "min_drive_blocks": 3,
        },
        "persistence": {"min_consecutive_blocks": 2, "clip_sigma": 3.0},
        "deduplication": {"merge_seconds": 600.0, "reset_at_drive_boundary": True},
        "shifts": [
            {
                "name": "step",
                "kind": "step",
                "magnitude": 3.0,
                "start_after_seconds": 300.0,
                "ramp_seconds": 0.0,
                "synthetic": True,
            }
        ],
    }


def _policy(**overrides: object) -> ChangePolicy:
    raw = policy_dict()
    raw.update(overrides)
    return ChangePolicy.model_validate(raw)


def _blocks(
    drive: str,
    n: int,
    *,
    seed: int,
    offset: float = 0.0,
    t0: float = 1.8e9,
    cell: str = "idle",
    noise: float = 1.0,
) -> list[Block]:
    rng = np.random.default_rng(seed)
    return [
        Block(
            drive=drive,
            start=t0 + 30.0 * i,
            end=t0 + 30.0 * i + 27.0,
            cell=cell,
            n=10,
            y=float(rng.normal(offset, noise)),
            x=(1.0,),
            condition=4.0 if cell == "idle" else 30.0,
        )
        for i in range(n)
    ]


def _reference(policy: ChangePolicy | None = None) -> FrozenReference:
    policy = policy or _policy()
    train = _blocks("train-a", 40, seed=1) + _blocks("train-b", 40, seed=2, t0=1.8e9 + DAY)
    cal = (
        _blocks("cal-a", 30, seed=3, t0=1.8e9 + 2 * DAY)
        + _blocks("cal-b", 30, seed=4, t0=1.8e9 + 3 * DAY)
        + _blocks("cal-c", 30, seed=5, t0=1.8e9 + 4 * DAY)
    )
    return fit_reference(policy, train, cal, name="synthetic", calibration_revision="REV")


def test_every_declaration_is_required():
    raw = _policy().model_dump(mode="json", by_alias=True)
    for section in (
        "excitation",
        "dependence",
        "calibration",
        "persistence",
        "deduplication",
        "drive_level",
    ):
        broken = {k: v for k, v in raw.items() if k != section}
        with pytest.raises(ValidationError, match=section):
            ChangePolicy.model_validate(broken)


def test_statistics_cannot_be_carried_across_drives():
    raw = _policy().model_dump(mode="json", by_alias=True)
    raw["deduplication"]["reset_at_drive_boundary"] = False
    with pytest.raises(ValidationError):
        ChangePolicy.model_validate(raw)


def test_holdout_is_fixed_and_small_never_leave_one_out():
    raw = _policy().model_dump(mode="json", by_alias=True)
    raw["calibration"]["n_test_drives"] = 10
    with pytest.raises(ValidationError):
        ChangePolicy.model_validate(raw)


def test_a_synthetic_shift_must_say_it_is_synthetic():
    raw = _policy().model_dump(mode="json", by_alias=True)
    raw["shifts"][0]["synthetic"] = False
    with pytest.raises(ValidationError):
        ChangePolicy.model_validate(raw)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("persistence", "clip_sigma", 4.5),  # 4.5**2/2 > detectors.glr_h 8
        ("drive_level", "glr_h", 4.0),  # 3**2/2 = 4.5 >= 4: one drive could alarm
        ("drive_level", "cusum_h", 2.5),  # clip 3 >= 2.5
    ],
)
def test_a_limit_one_block_or_drive_can_reach_is_refused(section, key, value):
    raw = _policy().model_dump(mode="json", by_alias=True)
    raw[section][key] = value
    with pytest.raises(ValidationError, match="single"):
        ChangePolicy.model_validate(raw)


def test_blocks_stay_inside_one_cell_and_short_blocks_are_counted():
    policy = _policy()
    samples = [Sample(t=float(i * 3), y=1.0, x=(1.0,), condition=4.0) for i in range(12)]
    samples += [Sample(t=36.0 + i * 3, y=2.0, x=(1.0,), condition=30.0) for i in range(2)]
    samples += [Sample(t=42.0, y=5.0, x=(1.0,), condition=500.0)]
    blocks, dropped = blocks_of(policy, "d", samples)
    assert [b.cell for b in blocks] == ["idle"]
    assert blocks[0].n == 10
    assert dropped == {"uncovered": 1, "short_block": 4}


def test_calibration_must_be_out_of_sample():
    policy = _policy()
    shared = _blocks("same", 40, seed=1)
    with pytest.raises(ReferenceRefusedError, match="out of sample"):
        fit_reference(
            policy,
            shared + _blocks("t2", 40, seed=2, t0=1.9e9),
            shared + _blocks("c2", 40, seed=3, t0=1.95e9),
            name="x",
            calibration_revision=None,
        )


def _ar_drive(
    drive: str, n: int, t0: float, seed: int, phi: float, offset: float = 0.0
) -> list[Block]:
    rng = np.random.default_rng(seed)
    level = 0.0
    out: list[Block] = []
    for i in range(n):
        level = phi * level + rng.normal(0.0, 0.5)
        out.append(
            Block(drive, t0 + 30 * i, t0 + 30 * i + 27, "idle", 10, offset + level, (1.0,), 4.0)
        )
    return out


def test_within_drive_dependence_is_refused_past_the_declared_limit():
    cal = [b for k in range(3) for b in _ar_drive(f"c{k}", 60, 1.9e9 + k * DAY, 7 + k, 0.9)]
    train = _blocks("t1", 40, seed=1) + _blocks("t2", 40, seed=2, t0=1.81e9)
    with pytest.raises(ReferenceRefusedError, match="within-drive lag-1"):
        fit_reference(_policy(), train, cal, name="x", calibration_revision=None)


def test_a_per_drive_offset_is_between_drive_variance_not_within_drive_dependence():
    """Drive offsets of +-1 with white noise inside: the within-drive lag-1
    stays small, and the offset shows up as the between-drive component."""
    cal = [
        b
        for k, offset in enumerate((-1.0, 0.0, 1.0))
        for b in _blocks(f"c{k}", 40, seed=20 + k, offset=offset, t0=1.9e9 + k * DAY, noise=0.3)
    ]
    train = _blocks("t1", 40, seed=1, noise=0.3) + _blocks("t2", 40, seed=2, t0=1.81e9, noise=0.3)
    ref = fit_reference(_policy(), train, cal, name="x", calibration_revision=None)
    limits = ref.limits("idle")
    assert limits is not None
    assert abs(ref.within_drive_lag1) < 0.3
    assert limits.between_sd > 0.8
    assert limits.within_sd < 0.4


def test_a_reference_biased_on_its_own_calibration_fold_is_refused():
    train = _blocks("t1", 40, seed=1) + _blocks("t2", 40, seed=2, t0=1.81e9)
    cal = [
        b
        for k in range(3)
        for b in _blocks(f"c{k}", 30, seed=9 + k, offset=2.0, t0=1.9e9 + k * DAY)
    ]
    with pytest.raises(ReferenceRefusedError, match="biased on its own calibration fold"):
        fit_reference(_policy(), train, cal, name="x", calibration_revision=None)


def test_effective_count_and_drive_count_gate_a_cell():
    policy = _policy()
    train = _blocks("t1", 40, seed=1) + _blocks("t2", 40, seed=2, t0=1.81e9)
    one_drive = _blocks("c1", 40, seed=3, t0=1.9e9) + _blocks("c2", 40, seed=4, t0=1.95e9)
    one_drive += _blocks("c1", 20, seed=5, t0=1.9e9 + 5000, cell="cruise")
    ref = fit_reference(policy, train, one_drive, name="x", calibration_revision=None)
    assert "drive" in ref.uncalibrated_cells["cruise"]
    # The conformal rank is checked again on the effective count, and the
    # interval says whether it held.
    raw = policy_dict()
    raw["calibration"]["min_cell_blocks"] = 5  # type: ignore[index]
    small = ChangePolicy.model_validate(raw)
    cal = [b for k in range(3) for b in _ar_drive(f"s{k}", 8, 1.9e9 + k * DAY, 30 + k, 0.2)]
    ref = fit_reference(small, train, cal, name="x", calibration_revision=None)
    limits = ref.limits("idle")
    assert limits is not None and limits.n_blocks == 24
    assert limits.n_effective <= 24
    rank = math.ceil((limits.n_effective + 1) * 0.9)
    assert limits.coverage_guaranteed is (rank <= math.floor(limits.n_effective))


def test_too_few_blocks_per_drive_cannot_estimate_dependence():
    train = _blocks("t1", 40, seed=1) + _blocks("t2", 40, seed=2, t0=1.81e9)
    cal = [b for k in range(3) for b in _blocks(f"c{k}", 3, seed=40 + k, t0=1.9e9 + k * DAY)]
    with pytest.raises(ReferenceRefusedError, match="too few to estimate"):
        fit_reference(_policy(), train, cal, name="x", calibration_revision=None)


def test_conformal_interval_covers_held_out_blocks_from_the_same_process():
    reference = _reference()
    limits = reference.limits("idle")
    assert limits is not None and limits.n_drives == 3 and limits.coverage_guaranteed
    monitor = ChangeMonitor(reference)
    monitor.start_drive("test")
    inside = [monitor.observe(b).inside for b in _blocks("test", 400, seed=11, t0=1.9e9)]
    assert 0.84 <= sum(bool(v) for v in inside) / len(inside) <= 0.96  # nominal 0.90


def test_an_uncalibrated_cell_is_unevaluable_not_normal():
    reference = _reference()
    assert "cruise" in reference.uncalibrated_cells
    monitor = ChangeMonitor(reference)
    monitor.start_drive("t")
    verdict = monitor.observe(_blocks("t", 1, seed=1, cell="cruise")[0])
    assert verdict.status == "unevaluable"
    assert verdict.residual is None and verdict.inside is None
    assert verdict.reason is not None and verdict.reason.startswith("uncovered")


def test_an_exceedance_is_not_an_event():
    monitor = ChangeMonitor(_reference())
    monitor.start_drive("t")
    blocks = _blocks("t", 20, seed=2, t0=1.9e9)
    spike = dataclasses.replace(blocks[10], y=8.0)
    verdicts = [monitor.observe(b) for b in [*blocks[:10], spike, *blocks[11:]]]
    assert verdicts[10].inside is False
    assert monitor.events == []


def test_a_spike_on_an_accumulated_statistic_is_not_an_alarm():
    """CUSUM built up by blocks just past k, then one wild block takes it over
    h: persistence counts blocks beyond k, so the spike alone cannot alarm."""
    reference = _reference()
    limits = reference.limits("idle")
    assert limits is not None
    k_points = reference.alarm.shift_of_interest / 2.0
    monitor = ChangeMonitor(reference)
    monitor.start_drive("t")
    base = _blocks("t", 30, seed=0, t0=1.9e9, noise=0.0)
    ys = [k_points * 1.6] * 5 + [20.0] + [k_points * 0.9] * 3 + [0.0] * 21
    verdicts = [monitor.observe(dataclasses.replace(b, y=y)) for b, y in zip(base, ys, strict=True)]
    assert verdicts[5].cusum_pos >= reference.alarm.cusum_h
    # Still over h on the next block: a rule counting only "statistic over h"
    # would have alarmed here.
    assert verdicts[6].cusum_pos >= reference.alarm.cusum_h
    # (GLR may rightly see the five shifted blocks; CUSUM must not alarm on
    # the spike that lifted it over h.)
    assert not [e for e in monitor.events if e.detector == "cusum"]


def test_a_declared_step_is_detected_once_with_a_bounded_latency():
    monitor = ChangeMonitor(_reference())
    monitor.start_drive("t")
    blocks = _blocks("t", 60, seed=5, t0=1.9e9)
    onset = blocks[20].start
    for b in [dataclasses.replace(b, y=b.y + 3.0) if b.start >= onset else b for b in blocks]:
        monitor.observe(b)
    cusum = [e for e in monitor.events if e.detector == "cusum"]
    assert len(cusum) == 1, "repeated alarms on one change merge into one event"
    assert cusum[0].direction == "+"
    assert 0 <= cusum[0].first_alarm - onset <= 12 * 30
    assert abs(cusum[0].shift_estimate - 3.0) < 1.5


def test_statistics_restart_at_each_drive():
    monitor = ChangeMonitor(_reference())
    monitor.start_drive("a")
    for b in _blocks("a", 3, seed=1, offset=2.5, t0=1.9e9):
        monitor.observe(b)
    monitor.close_drive()
    monitor.start_drive("b")
    first = monitor.observe(_blocks("b", 1, seed=1, t0=1.91e9)[0])
    assert first.cusum_pos <= 1.0 and first.cusum_neg <= 1.0
    with pytest.raises(ValueError, match="start_drive"):
        monitor.observe(_blocks("c", 1, seed=1)[0])


def test_a_drift_between_drives_is_seen_at_drive_level_and_missed_per_block():
    """+0.4 points per drive (synthetic), 3 blocks a drive: no drive alone
    accumulates a per-block alarm; the drive-level detector alarms."""
    monitor = ChangeMonitor(_reference())
    first_drive_alarm = None
    for i in range(1, 11):
        drive = f"d{i}"
        monitor.start_drive(drive)
        for b in _blocks(drive, 3, seed=100 + i, offset=0.4 * i, t0=2e9 + i * DAY, noise=0.5):
            monitor.observe(b)
        verdict = monitor.close_drive()
        if verdict.alarms and first_drive_alarm is None:
            first_drive_alarm = i
            break
    assert first_drive_alarm is not None
    assert not [e for e in monitor.events if e.detector in ("cusum", "glr")]
    assert all(e.direction == "+" for e in monitor.events)
    assert first_drive_alarm <= 6


def test_a_short_drive_is_unevaluable_at_drive_level():
    monitor = ChangeMonitor(_reference())
    monitor.start_drive("short")
    for b in _blocks("short", 2, seed=1, t0=2e9):
        monitor.observe(b)
    verdict = monitor.close_drive()
    assert verdict.status == "unevaluable" and verdict.u is None


def test_the_reference_is_frozen_and_self_hashed():
    reference = _reference()
    with pytest.raises(ValidationError):
        reference.theta = (5.0,)
    widened = reference.model_dump(mode="json")
    widened["alarm"]["cusum_h"] = 50.0
    with pytest.raises(ValidationError, match="fingerprint"):
        FrozenReference.model_validate(widened)
    moved = reference.model_dump(mode="json")
    moved["theta"] = [2.0]
    with pytest.raises(ValidationError, match="fingerprint"):
        FrozenReference.model_validate(moved)


def test_change_detection_refuses_anything_but_a_frozen_reference():
    reference = _reference()
    limits = reference.cells[0]
    for target in (limits, reference.model_dump(mode="json")):
        with pytest.raises(NotAFrozenReferenceError):
            ChangeMonitor(target)  # type: ignore[arg-type]
    assert isinstance(limits, CellLimits)


def test_verdicts_carry_the_reference_and_every_open_alarm():
    monitor = ChangeMonitor(_reference(), unresolved=("ev-earlier",))
    monitor.start_drive("t")
    verdict = monitor.observe(_blocks("t", 1, seed=1, t0=1.9e9)[0])
    assert verdict.open_alarms == ("ev-earlier",)
    assert verdict.reference_fingerprint == monitor.reference.fingerprint
    assert monitor.issued(verdict)
    monitor.resolve(["ev-earlier"])
    assert monitor.observe(_blocks("t", 2, seed=2, t0=1.9e9)[1]).open_alarms == ()
    assert not monitor.issued(verdict)


@pytest.mark.parametrize("wild", [4.0, 8.0, 20.0])
def test_one_wild_drive_among_in_control_drives_raises_no_drive_level_alarm(wild):
    """Offsets [0, 0, W, 0, 0] (synthetic): a level that came and went is not
    a drift; drive-level alarms need two consecutive drives beyond k."""
    monitor = ChangeMonitor(_reference())
    for i, offset in enumerate([0.0, 0.0, wild, 0.0, 0.0]):
        drive = f"d{i}"
        monitor.start_drive(drive)
        for b in _blocks(drive, 10, seed=50 + i, offset=offset, t0=2e9 + i * DAY, noise=0.5):
            monitor.observe(b)
        monitor.close_drive()
    assert not [e for e in monitor.events if e.detector in ("drive_glr", "drive_cusum")]


def test_lag1_is_corrected_for_the_demeaning_bias():
    reference = _reference()
    assert reference.within_drive_lag1 > reference.within_drive_lag1_raw
