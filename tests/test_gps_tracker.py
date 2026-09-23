"""Tests for the GPS live-state + trip-odometer tracker."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.analysis.gps_tracker import GpsTracker
from src.telemetry import TelemetryRecord


def _speed(epoch: float, kmh: float) -> TelemetryRecord:
    return TelemetryRecord.gps(epoch, "speed", kmh, "km/h")


def test_distance_and_drive_time_accumulate() -> None:
    tracker = GpsTracker()
    for epoch in (100.0, 101.0, 102.0, 103.0):
        tracker.observe(_speed(epoch, 36.0))  # 36 km/h over three 1-s steps
    trip = tracker.trip_status()
    assert trip["distance_km"] == pytest.approx(0.03, abs=1e-6)
    assert trip["moving_time_seconds"] == pytest.approx(3.0)
    # distance_miles is rounded to 3 decimals for display (0.018641 → 0.019).
    assert trip["distance_miles"] == pytest.approx(round(0.03 / 1.609344, 3))


def test_parked_jitter_below_threshold_does_not_accrue() -> None:
    tracker = GpsTracker()
    for epoch in (100.0, 101.0, 102.0):
        tracker.observe(_speed(epoch, 0.4))  # GPS noise while stopped
    trip = tracker.trip_status()
    assert trip["distance_km"] == 0.0
    assert trip["moving_time_seconds"] == 0.0


def test_large_time_gap_breaks_integration() -> None:
    tracker = GpsTracker()
    tracker.observe(_speed(100.0, 60.0))
    tracker.observe(_speed(250.0, 60.0))  # 150 s gap (restart / fix loss)
    assert tracker.trip_status()["distance_km"] == 0.0


def test_persistence_round_trip(tmp_path: Path) -> None:
    state = tmp_path / "trip.json"
    tracker = GpsTracker(state_path=state, persist_interval_seconds=0.0)
    for epoch in (100.0, 101.0, 102.0, 103.0):
        tracker.observe(_speed(epoch, 36.0))
    tracker.persist()

    reloaded = GpsTracker(state_path=state)
    assert reloaded.trip_status()["distance_km"] == pytest.approx(0.03, abs=1e-6)
    assert reloaded.trip_status()["moving_time_seconds"] == pytest.approx(3.0)


def test_gps_status_reports_latest_fix_and_age() -> None:
    tracker = GpsTracker()
    tracker.observe(TelemetryRecord.gps(1000.0, "fix_quality", 1, ""))
    tracker.observe(TelemetryRecord.gps(1000.0, "num_satellites", 7, ""))
    tracker.observe(TelemetryRecord.gps(1000.0, "latitude", 39.9, "deg"))
    tracker.observe(TelemetryRecord.gps(1000.0, "speed", 50.0, "km/h"))

    status = tracker.gps_status(now=datetime.fromtimestamp(1005.0, tz=UTC))
    assert status is not None
    assert status["has_fix"] is True
    assert status["fix_quality"] == 1
    assert status["num_satellites"] == 7
    assert status["latitude"] == pytest.approx(39.9)
    assert status["speed"] == pytest.approx(50.0)
    assert status["age_seconds"] == pytest.approx(5.0)


def test_gps_status_marks_stale_fix_as_no_fix() -> None:
    tracker = GpsTracker()
    tracker.observe(TelemetryRecord.gps(1000.0, "fix_quality", 1, ""))
    # 30 s later → past the staleness window even though quality was 1.
    status = tracker.gps_status(now=datetime.fromtimestamp(1030.0, tz=UTC))
    assert status is not None
    assert status["has_fix"] is False


def test_gps_status_none_before_any_observation() -> None:
    assert GpsTracker().gps_status() is None


def test_non_gps_records_are_ignored() -> None:
    tracker = GpsTracker()
    tracker.observe(TelemetryRecord.obd(100.0, "RPM", 800, "rpm"))
    assert tracker.gps_status() is None
    assert tracker.trip_status()["distance_km"] == 0.0


def test_trip_ended_edge_fires_after_sustained_stationary() -> None:
    """Task 5.4: moving -> stationary sustained for the configured window
    fires the edge exactly once."""
    tracker = GpsTracker(trip_end_stationary_seconds=60.0)
    assert tracker.observe(_speed(0.0, 40.0)) is None  # start moving
    assert tracker.observe(_speed(10.0, 0.0)) is None  # stop — sustain clock starts
    assert tracker.observe(_speed(30.0, 0.0)) is None  # only 20s stationary so far
    end = tracker.observe(_speed(71.0, 0.0))  # 61s stationary -> edge fires
    assert end is not None and end.observed and end.speed_source == "gps:speed"
    # The edge fires exactly once; further stationary ticks don't re-fire.
    assert tracker.observe(_speed(200.0, 0.0)) is None


def test_trip_ended_edge_requires_prior_motion() -> None:
    """Sitting stationary from the very first observation is not a trip end
    (there was no trip to end — e.g. daemon started while parked)."""
    tracker = GpsTracker(trip_end_stationary_seconds=60.0)
    for epoch in (0.0, 30.0, 70.0, 400.0):
        assert tracker.observe(_speed(epoch, 0.0)) is None


def test_trip_ended_edge_resets_on_renewed_motion() -> None:
    """A second trip after the first ends can fire its own edge."""
    tracker = GpsTracker(trip_end_stationary_seconds=60.0)
    tracker.observe(_speed(0.0, 40.0))
    tracker.observe(_speed(10.0, 0.0))
    assert tracker.observe(_speed(80.0, 0.0)) is not None  # first trip ends
    tracker.observe(_speed(200.0, 40.0))  # moving again — new trip starts
    tracker.observe(_speed(210.0, 0.0))
    assert tracker.observe(_speed(280.0, 0.0)) is not None  # second trip ends


def test_brief_stop_does_not_end_trip() -> None:
    """A stoplight-length stop must not fire the sustained edge."""
    tracker = GpsTracker(trip_end_stationary_seconds=60.0)
    tracker.observe(_speed(0.0, 40.0))
    tracker.observe(_speed(10.0, 0.0))
    assert tracker.observe(_speed(20.0, 0.0)) is None  # only 10s stopped
    assert tracker.observe(_speed(25.0, 40.0)) is None  # moving again
