"""Per-detector unit tests for the event-detector library.

Each detector is exercised against a small synthetic frame designed to
trigger the specific rule, plus a near-miss that should NOT trigger, plus
a degenerate input (empty / missing column) that should return [] cleanly.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.analysis.events import (
    detect_cold_starts,
    detect_cruise,
    detect_hard_accels,
    detect_hard_brakes,
    detect_idle_periods,
    detect_stops,
    detect_wot_runs,
)
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN


def _base_frame(rows: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["s"] * rows,
            TIMESTAMP_COLUMN: pd.date_range("2026-05-20T12:00:00Z", periods=rows, freq="1s"),
            MODE_COLUMN: ["unknown"] * rows,
        }
    )


# --------------------------------------------------------------------------- #
# cold_start
# --------------------------------------------------------------------------- #


def test_cold_start_fires_when_coolant_cold_and_run_time_low() -> None:
    frame = _base_frame(10)
    frame["obd:COOLANT_TEMP"] = [25.0, 28.0, 32.0, 36.0, 40.0, 45.0, 52.0, 60.0, 70.0, 80.0]
    frame["obd:RUN_TIME"] = list(range(1, 11))
    events = detect_cold_starts(frame, [])
    assert len(events) == 1
    assert events[0].kind == "cold_start"
    assert events[0].fields["coolant_temp_c"] == 25.0


def test_cold_start_reads_the_right_row_when_the_index_is_not_positional() -> None:
    """The detector took a LABEL from `idxmax()` and fed it to `.iloc`, which is
    positional. On a RangeIndex the two agree, so every fixture here hid it; on a
    filtered or concatenated frame it reads a different row (or raises).
    """
    frame = _base_frame(10)
    frame["obd:COOLANT_TEMP"] = [90.0, 90.0, 25.0, 28.0, 32.0, 36.0, 45.0, 60.0, 70.0, 80.0]
    frame["obd:RUN_TIME"] = [400.0, 401.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    shifted = frame.set_index(frame.index + 500)

    events = detect_cold_starts(shifted, [])

    assert len(events) == 1
    assert events[0].fields["coolant_temp_c"] == 25.0


def test_cold_start_does_not_fire_on_warm_restart() -> None:
    """RUN_TIME=2 but coolant already at thermostat — that's a warm restart."""
    frame = _base_frame(5)
    frame["obd:COOLANT_TEMP"] = [85.0, 86.0, 86.0, 87.0, 87.0]
    frame["obd:RUN_TIME"] = [2.0, 3.0, 4.0, 5.0, 6.0]
    assert detect_cold_starts(frame, []) == []


def test_cold_start_skips_when_columns_missing() -> None:
    assert detect_cold_starts(_base_frame(5), []) == []


# --------------------------------------------------------------------------- #
# idle_period
# --------------------------------------------------------------------------- #


def test_idle_period_emits_one_event_per_contiguous_run() -> None:
    """Two idle stretches separated by motion → two events."""
    frame = _base_frame(40)
    # 0-14 idle, 15-24 moving, 25-39 idle again.
    frame["obd:SPEED"] = [0.0] * 15 + [25.0] * 10 + [0.0] * 15
    events = detect_idle_periods(frame, [])
    assert [event.kind for event in events] == ["idle_period", "idle_period"]


def test_idle_period_drops_short_runs() -> None:
    """A 5-second blip below threshold doesn't register."""
    frame = _base_frame(20)
    frame["obd:SPEED"] = [25.0] * 10 + [0.0] * 5 + [25.0] * 5
    assert detect_idle_periods(frame, []) == []


# --------------------------------------------------------------------------- #
# stop
# --------------------------------------------------------------------------- #


def test_stop_fires_on_rolling_to_held_transition() -> None:
    frame = _base_frame(15)
    # Rolling 30 kph for 5s, then held at 0 for 10s — one stop event.
    frame["obd:SPEED"] = [30.0] * 5 + [0.0] * 10
    events = detect_stops(frame, [])
    assert len(events) == 1
    assert events[0].kind == "stop"
    assert events[0].fields["pre_stop_speed_kph"] == 30.0


def test_stop_skips_brief_zero_crossings() -> None:
    """One sample at zero in the middle of motion doesn't count as a stop."""
    frame = _base_frame(10)
    frame["obd:SPEED"] = [30.0, 30.0, 30.0, 30.0, 0.0, 30.0, 30.0, 30.0, 30.0, 30.0]
    assert detect_stops(frame, []) == []


def test_stop_ignores_session_that_opens_already_stopped() -> None:
    """No prior motion → no stop event (we can't infer pre-stop speed)."""
    frame = _base_frame(10)
    frame["obd:SPEED"] = [0.0] * 10
    assert detect_stops(frame, []) == []


# --------------------------------------------------------------------------- #
# hard_accel
# --------------------------------------------------------------------------- #


def test_hard_accel_fires_when_throttle_and_rpm_both_climb() -> None:
    frame = _base_frame(10)
    # 5s of 75% throttle, RPM climbs from 800 to 4000.
    frame["obd:THROTTLE_POS"] = [10.0, 10.0, 75.0, 80.0, 78.0, 76.0, 75.0, 10.0, 10.0, 10.0]
    frame["obd:RPM"] = [800.0, 820.0, 1500.0, 2400.0, 3200.0, 3700.0, 4000.0, 3000.0, 1500.0, 900.0]
    events = detect_hard_accels(frame, [])
    assert len(events) == 1
    assert events[0].kind == "hard_accel"
    assert events[0].fields["peak_throttle_pct"] >= 75.0


def test_hard_accel_skips_throttle_blip_without_rpm_rise() -> None:
    """Throttle goes 80% briefly but RPM stays flat (clutch in)."""
    frame = _base_frame(10)
    frame["obd:THROTTLE_POS"] = [10.0, 10.0, 80.0, 80.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    frame["obd:RPM"] = [800.0] * 10
    assert detect_hard_accels(frame, []) == []


# --------------------------------------------------------------------------- #
# hard_brake
# --------------------------------------------------------------------------- #


def test_hard_brake_fires_on_sharp_deceleration() -> None:
    frame = _base_frame(10)
    # 80 → 20 kph over 3 seconds = -20 kph/s; well past the -15 threshold.
    frame["obd:SPEED"] = [80.0, 80.0, 80.0, 80.0, 60.0, 40.0, 20.0, 20.0, 20.0, 20.0]
    events = detect_hard_brakes(frame, [])
    assert any(event.kind == "hard_brake" for event in events)


def test_hard_brake_skips_gentle_deceleration() -> None:
    """80 → 60 kph over 10s = -2 kph/s — that's regen / engine braking."""
    frame = _base_frame(11)
    frame["obd:SPEED"] = [80.0, 78.0, 76.0, 74.0, 72.0, 70.0, 68.0, 66.0, 64.0, 62.0, 60.0]
    assert detect_hard_brakes(frame, []) == []


# --------------------------------------------------------------------------- #
# cruise
# --------------------------------------------------------------------------- #


def test_cruise_fires_on_sustained_steady_speed() -> None:
    frame = _base_frame(60)
    frame["obd:SPEED"] = [85.0 + (i % 3) * 0.5 for i in range(60)]
    frame[MODE_COLUMN] = "highway"
    events = detect_cruise(frame, [])
    assert len(events) >= 1
    assert events[0].fields["regime"] == "highway"


def test_cruise_regime_is_the_segmenters_band_not_its_own() -> None:
    """R01.7: the detector said highway at >= 70 km/h while the segmenter's
    highway band starts at 80. A 72 km/h cruise the segmenter calls city is
    a surface cruise."""
    frame = _base_frame(60)
    frame["obd:SPEED"] = [72.0 + (i % 3) * 0.5 for i in range(60)]
    frame[MODE_COLUMN] = "city"
    events = detect_cruise(frame, [])
    assert len(events) >= 1
    assert events[0].fields["regime"] == "surface"


def test_cruise_does_not_fire_when_speed_swings() -> None:
    """Speed alternates 60/40 — outside the ±5 band, no cruise."""
    frame = _base_frame(60)
    frame["obd:SPEED"] = [60.0 if i % 2 == 0 else 40.0 for i in range(60)]
    assert detect_cruise(frame, []) == []


# --------------------------------------------------------------------------- #
# wot_run
# --------------------------------------------------------------------------- #


def test_wot_run_fires_on_sustained_full_throttle() -> None:
    frame = _base_frame(10)
    # 4 seconds at 95% throttle.
    frame["obd:THROTTLE_POS"] = [20.0, 20.0, 95.0, 96.0, 95.0, 95.0, 95.0, 20.0, 20.0, 20.0]
    events = detect_wot_runs(frame, [])
    assert len(events) == 1
    assert events[0].kind == "wot_run"


def test_wot_run_skips_brief_punch() -> None:
    """A single sample at 95% isn't sustained — under the 2s floor."""
    frame = _base_frame(10)
    frame["obd:THROTTLE_POS"] = [20.0, 20.0, 95.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0]
    assert detect_wot_runs(frame, []) == []


# --------------------------------------------------------------------------- #
# Degenerate inputs across the suite
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "detector",
    [
        detect_cold_starts,
        detect_idle_periods,
        detect_stops,
        detect_hard_accels,
        detect_hard_brakes,
        detect_cruise,
        detect_wot_runs,
    ],
)
def test_detector_handles_empty_frame_cleanly(detector) -> None:
    frame = pd.DataFrame(columns=[SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN, "obd:RPM"])
    assert detector(frame, []) == []
