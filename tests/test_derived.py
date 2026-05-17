"""Tests for trip/fuel/performance derived channels (Task 6.6)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.derived import (
    FUEL_PROPERTIES,
    compute_derived_channels,
    fuel_properties,
    summarize_trip,
)
from src.config import VehicleParameters

GASOLINE = VehicleParameters()


def _frame(seconds: int, **channels: list[float]) -> pd.DataFrame:
    ts = pd.date_range("2026-05-16T00:00:00Z", periods=seconds, freq="1s")
    data: dict[str, object] = {"session": "s", "timestamp": ts}
    data.update(channels)
    return pd.DataFrame(data)


def test_fuel_rate_from_maf_and_economy() -> None:
    # MAF 7.35 g/s ÷ 14.7 AFR = 0.5 g/s fuel; density 0.745 kg/L →
    # 0.5 / 745 * 3600 ≈ 2.4161 L/h. At 100 kph that's 2.416 L/100km.
    df = _frame(3, **{"obd:MAF": [7.35] * 3, "obd:SPEED": [100.0] * 3})
    out = compute_derived_channels(df, GASOLINE)
    assert "derived:FUEL_RATE_LPH" in out.columns
    assert out["derived:FUEL_RATE_LPH"].iloc[0] == pytest.approx(2.4161, rel=1e-3)
    assert out["derived:INSTANT_ECONOMY_L_PER_100KM"].iloc[0] == pytest.approx(2.4161, rel=1e-3)
    assert out["derived:INSTANT_ECONOMY_MPG"].iloc[0] > 0


def test_engine_fuel_rate_pid_preferred_over_maf() -> None:
    df = _frame(2, **{"obd:ENGINE_FUEL_RATE": [6.0, 6.0], "obd:MAF": [50.0, 50.0]})
    out = compute_derived_channels(df, GASOLINE)
    # Uses the L/h PID directly, not the MAF estimate.
    assert out["derived:FUEL_RATE_LPH"].iloc[0] == pytest.approx(6.0, rel=1e-6)


def test_speed_density_fallback() -> None:
    df = _frame(
        2,
        **{
            "obd:RPM": [2000.0, 2000.0],
            "obd:INTAKE_PRESSURE": [100.0, 100.0],
            "obd:INTAKE_TEMP": [25.0, 25.0],
        },
    )
    out = compute_derived_channels(df, GASOLINE)
    assert "derived:FUEL_RATE_LPH" in out.columns
    assert out["derived:FUEL_RATE_LPH"].iloc[0] > 0


def test_boost_vacuum_uses_baro_when_present() -> None:
    df = _frame(
        2,
        **{"obd:INTAKE_PRESSURE": [40.0, 110.0], "obd:BAROMETRIC_PRESSURE": [100.0, 100.0]},
    )
    out = compute_derived_channels(df, GASOLINE)
    assert out["derived:BOOST_VACUUM_KPA"].tolist() == [-60.0, 10.0]


def test_estimated_power_from_torque_pids() -> None:
    # 50% of 300 Nm = 150 Nm at 3000 rpm → P = 150 * 3000 * 2π/60 / 1000
    # = 47.12 kW crank; ×(1-0.15) wheel; ×1.341 hp.
    df = _frame(
        2,
        **{
            "obd:RPM": [3000.0, 3000.0],
            "obd:ACTUAL_ENGINE_TORQUE": [50.0, 50.0],
            "obd:ENGINE_REFERENCE_TORQUE": [300.0, 300.0],
        },
    )
    out = compute_derived_channels(df, GASOLINE)
    expected_hp = 150.0 * 3000.0 * (2 * np.pi / 60.0) / 1000.0 * 0.85 * 1.34102209
    assert out["derived:ESTIMATED_POWER_HP"].iloc[0] == pytest.approx(expected_hp, rel=1e-3)
    assert out["derived:ESTIMATED_TORQUE_NM"].iloc[0] > 0


def test_cumulative_distance_fuel_idle() -> None:
    # 10 s at 36 kph = 0.01 km/s; first row dt=0 ⇒ 9 increments ⇒ 0.09 km.
    df = _frame(
        10,
        **{"obd:SPEED": [36.0] * 10, "obd:RPM": [800.0] * 10, "obd:MAF": [5.0] * 10},
    )
    out = compute_derived_channels(df, GASOLINE)
    assert out["derived:TRIP_DISTANCE_KM"].iloc[-1] == pytest.approx(0.09, rel=1e-6)
    assert out["derived:FUEL_USED_L"].iloc[-1] > 0
    # Moving, so no idle accrues.
    assert out["derived:IDLE_TIME_S"].iloc[-1] == 0.0


def test_idle_time_accumulates_when_stopped() -> None:
    df = _frame(10, **{"obd:SPEED": [0.0] * 10, "obd:RPM": [800.0] * 10})
    out = compute_derived_channels(df, GASOLINE)
    assert out["derived:IDLE_TIME_S"].iloc[-1] == pytest.approx(9.0, rel=1e-6)


def test_summarize_trip_accel_metrics() -> None:
    # Constant 8 kph/s ramp from a standstill for 30 s → crosses both
    # 60 mph (~96.6 kph) and a 1/4 mile.
    speed = [min(8.0 * t, 250.0) for t in range(31)]
    df = _frame(31, **{"obd:SPEED": speed, "obd:RPM": [3000.0] * 31, "obd:MAF": [20.0] * 31})
    summary = summarize_trip(df, GASOLINE)
    assert summary.duration_s == pytest.approx(30.0, rel=1e-6)
    assert summary.max_speed_kph == pytest.approx(240.0, rel=1e-6)
    assert summary.zero_to_60_mph_s is not None
    assert 11.0 <= summary.zero_to_60_mph_s <= 14.0
    assert summary.quarter_mile_s is not None
    assert summary.quarter_mile_trap_kph is not None
    assert summary.fuel_used_l is not None and summary.fuel_used_l > 0
    assert summary.co2_kg is not None and summary.co2_kg > 0


def test_summarize_trip_no_accel_event() -> None:
    df = _frame(5, **{"obd:SPEED": [10.0] * 5, "obd:RPM": [1500.0] * 5})
    summary = summarize_trip(df, GASOLINE)
    # Never started from a stop, so no 0-60 / quarter-mile.
    assert summary.zero_to_60_mph_s is None
    assert summary.quarter_mile_s is None
    assert summary.distance_km is not None


def test_empty_and_missing_inputs_are_safe() -> None:
    empty = compute_derived_channels(pd.DataFrame(), GASOLINE)
    assert empty.empty
    summary = summarize_trip(pd.DataFrame(), GASOLINE)
    assert summary.duration_s == 0.0
    assert summary.distance_km is None
    # Frame with only an unrelated channel: no derived columns invented.
    df = _frame(3, **{"obd:COOLANT_TEMP": [80.0] * 3})
    out = compute_derived_channels(df, GASOLINE)
    assert not [c for c in out.columns if c.startswith("derived:")]


def test_fuel_properties_by_type() -> None:
    assert fuel_properties(VehicleParameters(fuel_type="diesel")) is FUEL_PROPERTIES["diesel"]
    assert fuel_properties(VehicleParameters()) is FUEL_PROPERTIES["gasoline"]
