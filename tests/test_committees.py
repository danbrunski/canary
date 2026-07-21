"""Tests for sensor-committee divergence residuals."""

from __future__ import annotations

import pandas as pd
import pytest

from src.analysis.committees import (
    ALTITUDE_COMMITTEE,
    BARO_PRESSURE_COMMITTEE,
    COMMITTEES,
    GRADE_COMMITTEE,
    HEADING_YAW_COMMITTEE,
    LONGITUDINAL_ACCEL_COMMITTEE,
    SPEED_COMMITTEE,
    STATIONARY_CONFIRM_COMMITTEE,
    MemberGate,
    SensorCommittee,
    append_committee_residuals,
    non_trainable_committee_outputs,
)

_GOOD_FIX = {"gps:num_satellites": 12.0, "gps:hdop": 0.8}
_MARGINAL_FIX = {"gps:num_satellites": 5.0, "gps:hdop": 2.5}


def _speed_frame(obd: float, gps: float, **fix: float) -> pd.DataFrame:
    return pd.DataFrame([{"obd:SPEED": obd, "gps:speed": gps, **fix}])


def test_speed_committee_zero_when_sensors_agree() -> None:
    out = append_committee_residuals(_speed_frame(60.0, 60.0, **_GOOD_FIX))
    assert out[SPEED_COMMITTEE.output].iloc[0] == 0.0


def test_speed_committee_reports_divergence_on_good_fix() -> None:
    out = append_committee_residuals(_speed_frame(0.0, 60.0, **_GOOD_FIX))
    assert out[SPEED_COMMITTEE.output].iloc[0] == 60.0


def test_speed_committee_suppressed_on_marginal_fix() -> None:
    # Sensors disagree, but a marginal fix means GPS can't be trusted: no residual.
    out = append_committee_residuals(_speed_frame(0.0, 9.0, **_MARGINAL_FIX))
    assert pd.isna(out[SPEED_COMMITTEE.output].iloc[0])


def test_speed_committee_suppressed_on_high_hdop() -> None:
    out = append_committee_residuals(
        _speed_frame(0.0, 9.0, **{"gps:num_satellites": 12.0, "gps:hdop": 3.0})
    )
    assert pd.isna(out[SPEED_COMMITTEE.output].iloc[0])


def test_committee_gate_fails_closed_without_health_channel() -> None:
    # gps:speed present but no satellite/HDOP columns → can't confirm fix → NaN.
    out = append_committee_residuals(pd.DataFrame([{"obd:SPEED": 0.0, "gps:speed": 9.0}]))
    assert pd.isna(out[SPEED_COMMITTEE.output].iloc[0])


def test_committee_skipped_when_member_absent() -> None:
    # No gps:speed column at all → not enough members → no output column.
    out = append_committee_residuals(pd.DataFrame([{"obd:SPEED": 40.0, **_GOOD_FIX}]))
    assert SPEED_COMMITTEE.output not in out.columns


def test_three_member_spread() -> None:
    committee = SensorCommittee(
        name="ALT_DIVERGENCE",
        quantity="altitude (m)",
        members=("gps:altitude", "baro:altitude", "obd:altitude"),
        min_members=2,
    )
    frame = pd.DataFrame([{"gps:altitude": 100.0, "baro:altitude": 104.0, "obd:altitude": 99.0}])
    out = append_committee_residuals(frame, committees=[committee])
    assert out[committee.output].iloc[0] == 5.0  # max(104) - min(99)


def test_min_members_requires_enough_present() -> None:
    committee = SensorCommittee(
        name="ALT_DIVERGENCE",
        quantity="altitude (m)",
        members=("gps:altitude", "baro:altitude", "obd:altitude"),
        min_members=2,
    )
    # Only one member non-NaN this row → not scorable.
    frame = pd.DataFrame([{"gps:altitude": 100.0, "baro:altitude": float("nan")}])
    out = append_committee_residuals(frame, committees=[committee])
    assert pd.isna(out[committee.output].iloc[0])


def test_speed_committee_is_non_trainable() -> None:
    assert SPEED_COMMITTEE.output in non_trainable_committee_outputs()


def test_member_gate_mask_bounds() -> None:
    gate = MemberGate("gps:hdop", max_value=2.0)
    frame = pd.DataFrame({"gps:hdop": [0.8, 2.0, 3.0, float("nan")]})
    assert list(gate.mask(frame)) == [True, True, False, False]


# ---------------------------------------------------------------------------
# Phase 2a.8 — future committees (docs/sensor-committees.md §Roadmap)
# ---------------------------------------------------------------------------


_FUTURE_COMMITTEES = (
    LONGITUDINAL_ACCEL_COMMITTEE,
    HEADING_YAW_COMMITTEE,
    STATIONARY_CONFIRM_COMMITTEE,
    ALTITUDE_COMMITTEE,
    BARO_PRESSURE_COMMITTEE,
    GRADE_COMMITTEE,
)


def test_future_committees_are_registered_in_committees_tuple() -> None:
    for committee in _FUTURE_COMMITTEES:
        assert committee in COMMITTEES


def test_future_committees_are_non_trainable() -> None:
    non_trainable = non_trainable_committee_outputs()
    for committee in _FUTURE_COMMITTEES:
        assert committee.output in non_trainable


def test_future_committees_are_no_ops_on_todays_imu_less_frame() -> None:
    """No hardware exists yet — a real stored session's wide frame has none
    of these member columns, so every future committee must be silently
    skipped (no output column at all), same as SPEED_COMMITTEE would be
    without a gps:speed column."""
    frame = pd.DataFrame([{"obd:SPEED": 40.0, "obd:RPM": 1500.0, "gps:speed": 41.0}])
    out = append_committee_residuals(frame, committees=COMMITTEES)
    for committee in _FUTURE_COMMITTEES:
        assert committee.output not in out.columns


def test_longitudinal_accel_committee_activates_once_members_appear() -> None:
    """Synthetic-data proof that these declarations are real, working
    committees, not just inert placeholders — once the named columns exist
    (however they get there — Phase 2b hardware, later code), the same
    generic append_committee_residuals path scores them with zero further
    changes."""
    frame = pd.DataFrame(
        [
            {
                "imu:ACCEL_LON": 0.30,
                "derived:OBD_SPEED_ACCEL_G": 0.28,
                "derived:GPS_SPEED_ACCEL_G": 0.05,
            }
        ]
    )
    out = append_committee_residuals(frame, committees=COMMITTEES)
    assert out[LONGITUDINAL_ACCEL_COMMITTEE.output].iloc[0] == pytest.approx(0.25, abs=1e-9)


def test_grade_committee_three_member_spread() -> None:
    frame = pd.DataFrame(
        [
            {
                "derived:BARO_ALT_RATE": 1.0,
                "derived:GPS_VERTICAL_SPEED": 1.2,
                "derived:IMU_VERTICAL_ACCEL_INTEGRAL": 0.9,
            }
        ]
    )
    out = append_committee_residuals(frame, committees=COMMITTEES)
    assert out[GRADE_COMMITTEE.output].iloc[0] == pytest.approx(0.3, abs=1e-9)
