"""Tests for trip/fuel/performance derived channels (Task 6.6)."""

from __future__ import annotations

import dataclasses
import logging

import numpy as np
import pandas as pd
import pytest

from src.config import DISPLACEMENT_NOT_SET, VehicleParameters
from src.signals import derived
from src.signals.afr import AfrCalibration
from src.signals.derived import (
    FUEL_PROPERTIES,
    SPEED_DENSITY_AIR,
    FuelProperties,
    TripSummary,
    _accel_metrics,
    compute_derived_channels,
    compute_instantaneous_channels,
    fuel_basis,
    fuel_properties,
    summarize_trip,
)
from src.signals.speed_correction import SpeedCorrection

GASOLINE = VehicleParameters(displacement_l=4.0)
#: Most tests here are about the integrals, not the speed scale: integrate the
#: speed column as given.
UNCORRECTED = SpeedCorrection.unmeasured("test frame: no measured ratio")


def _frame(seconds: int, **channels: list[float]) -> pd.DataFrame:
    ts = pd.date_range("2026-05-16T00:00:00Z", periods=seconds, freq="1s")
    data: dict[str, object] = {"session": "s", "timestamp": ts}
    data.update(channels)
    return pd.DataFrame(data)


def test_fuel_rate_from_maf_and_economy() -> None:
    # MAF 7.35 g/s ÷ 14.7 AFR = 0.5 g/s fuel; density 0.745 kg/L →
    # 0.5 / 745 * 3600 ≈ 2.4161 L/h. At 100 kph that's 2.416 L/100km.
    df = _frame(3, **{"obd:MAF": [7.35] * 3, "obd:SPEED": [100.0] * 3})
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert "derived:FUEL_RATE_LPH" in out.columns
    assert out["derived:FUEL_RATE_LPH"].iloc[0] == pytest.approx(2.4161, rel=1e-3)
    assert out["derived:INSTANT_ECONOMY_L_PER_100KM"].iloc[0] == pytest.approx(2.4161, rel=1e-3)
    assert out["derived:INSTANT_ECONOMY_MPG"].iloc[0] > 0


def test_a_commanded_ratio_of_zero_is_fuel_cut_not_infinite() -> None:
    """PID 0x44 reads exactly 0 in decel fuel cut (P02.15).

    Dividing by it produced `inf` fuel rate on the 2026-08-07 hybrid drive.
    That was first read as "not available" and sent to NaN, which the trip
    integrator then bridged with the fuelled samples either side. It is the
    ECM commanding no fuel: on 2026-09-20T152758 and 2026-10-10T153117 its 160
    phi-0 rows sit at a median 6 % throttle, 60 km/h and the wideband's lean
    limit (lambda 2.0). So the row is 0 g/s (M01.4): finite, and not stoich.
    """
    df = _frame(3, **{"obd:MAF": [7.35] * 3, "obd:COMMANDED_EQUIV_RATIO": [1.0, 0.0, 1.0]})
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    rate = out["derived:FUEL_RATE_LPH"]
    assert np.isfinite(rate.iloc[0]) and rate.iloc[0] == pytest.approx(2.4161, rel=1e-3)
    assert rate.iloc[1] == 0.0
    assert np.isfinite(rate.iloc[2])


def test_one_infinite_sample_cannot_poison_a_cumulative_channel() -> None:
    """`inf` passes `notna`, so it used to latch the running total forever.

    80 % of a 26-minute drive's FUEL_USED_L was `inf` from a single bad
    sample. The guard belongs in the integrator, not in every caller.
    """
    df = _frame(4, **{"obd:MAF": [7.35] * 4, "obd:SPEED": [100.0] * 4})
    df.loc[1, "obd:MAF"] = np.inf
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    used = out["derived:FUEL_USED_L"]
    assert np.isfinite(used).all(), "an infinite sample escaped into the integral"
    assert used.iloc[-1] > 0


def test_engine_fuel_rate_pid_preferred_over_maf() -> None:
    df = _frame(2, **{"obd:ENGINE_FUEL_RATE": [6.0, 6.0], "obd:MAF": [50.0, 50.0]})
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    # Uses the L/h PID directly, not the MAF estimate.
    assert out["derived:FUEL_RATE_LPH"].iloc[0] == pytest.approx(6.0, rel=1e-6)


def test_fuel_rate_prefers_measured_wideband_lambda_over_commanded() -> None:
    """Task 2.1: once wideband λ (0x24/0x25) is polled, method-2 fuel calc
    should use the *measured* λ instead of the *commanded* one (0x44) —
    confirmed here with mismatched values so the result proves which won."""
    # Commanded λ says stoich (1.0); measured wideband λ says lean (1.25).
    # MAF 7.35 g/s ÷ (14.7 * 1.25) ≈ 0.4 g/s fuel → smaller than the
    # commanded-λ (stoich) 0.5 g/s case in test_fuel_rate_from_maf_and_economy.
    df = _frame(
        3,
        **{
            "obd:MAF": [7.35] * 3,
            "obd:O2_WR_LAMBDA_S1": [1.25] * 3,
            "obd:COMMANDED_EQUIV_RATIO": [1.0] * 3,
        },
    )
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    expected_fuel_g_s = 7.35 / (14.7 * 1.25)
    fuel = fuel_properties(GASOLINE)
    expected_lph = expected_fuel_g_s * 3600.0 / (fuel.density_kg_per_l * 1000.0)
    assert out["derived:FUEL_RATE_LPH"].iloc[0] == pytest.approx(expected_lph, rel=1e-6)


def test_speed_density_fallback() -> None:
    df = _frame(
        2,
        **{
            "obd:RPM": [2000.0, 2000.0],
            "obd:INTAKE_PRESSURE": [100.0, 100.0],
            "obd:INTAKE_TEMP": [25.0, 25.0],
        },
    )
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert "derived:FUEL_RATE_LPH" in out.columns
    assert out["derived:FUEL_RATE_LPH"].iloc[0] > 0


def test_speed_density_refuses_without_displacement(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M01.20: without the vehicle's displacement there is no speed-density
    air, so no fuel flow and no power from it, never a 4.0 L engine's; the
    refusal is named in the log. A fuel-rate PID's fuel still stands."""
    monkeypatch.setattr(derived, "_REFUSALS_LOGGED", set())
    df = _frame(
        2,
        **{
            "obd:RPM": [2000.0, 2000.0],
            "obd:INTAKE_PRESSURE": [100.0, 100.0],
            "obd:INTAKE_TEMP": [25.0, 25.0],
        },
    )
    unset = VehicleParameters()
    with caplog.at_level(logging.WARNING, logger=derived.log.name):
        out = compute_instantaneous_channels(df, unset, speed_correction=UNCORRECTED)
        with_pid = compute_instantaneous_channels(
            df.assign(**{"obd:ENGINE_FUEL_RATE": [10.0, 10.0]}),
            unset,
            speed_correction=UNCORRECTED,
        )
    assert "derived:FUEL_RATE_LPH" not in out.columns
    assert "derived:ESTIMATED_POWER_HP" not in out.columns
    assert with_pid["derived:FUEL_RATE_LPH"].tolist() == pytest.approx([10.0, 10.0])
    # The power's air cap is speed-density here, so it refuses too (M01.6).
    assert "derived:ESTIMATED_POWER_HP" not in with_pid.columns
    named = [r.getMessage() for r in caplog.records if DISPLACEMENT_NOT_SET in r.getMessage()]
    assert named == [
        f"{SPEED_DENSITY_AIR} refused: {DISPLACEMENT_NOT_SET} (vehicle parameters.displacement_l)"
    ]
    stated = compute_instantaneous_channels(
        df, VehicleParameters(displacement_l=3.954), speed_correction=UNCORRECTED
    )
    four = compute_instantaneous_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    ratio = stated["derived:FUEL_RATE_LPH"] / four["derived:FUEL_RATE_LPH"]
    assert ratio.tolist() == pytest.approx([3.954 / 4.0] * 2, rel=1e-12)


def test_boost_vacuum_uses_baro_when_present() -> None:
    df = _frame(
        2,
        **{"obd:INTAKE_PRESSURE": [40.0, 110.0], "obd:BAROMETRIC_PRESSURE": [100.0, 100.0]},
    )
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert out["derived:BOOST_VACUUM_KPA"].tolist() == [-60.0, 10.0]


def test_boost_vacuum_skipped_when_baro_missing() -> None:
    """No baro means no ambient reference, so the channel is uncomputable.

    It used to fall back to sea-level 101.325 kPa, which is ~21 kPa wrong at
    the ~2000 m this vehicle runs at — reporting an NA engine as permanently
    deep in vacuum. Skipping matches the rest of compute_derived_channels.
    """
    df = _frame(2, **{"obd:INTAKE_PRESSURE": [40.0, 110.0]})
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert "derived:BOOST_VACUUM_KPA" not in out.columns


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
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    expected_hp = 150.0 * 3000.0 * (2 * np.pi / 60.0) / 1000.0 * 0.85 * 1.34102209
    assert out["derived:ESTIMATED_POWER_HP"].iloc[0] == pytest.approx(expected_hp, rel=1e-3)
    assert out["derived:ESTIMATED_TORQUE_NM"].iloc[0] > 0


E10 = VehicleParameters(fuel_type="e10")


def _wideband(maf: list[float], lam: list[float]) -> pd.DataFrame:
    return _frame(
        len(maf), **{"obd:MAF": maf, "obd:O2_WR_LAMBDA_S1": lam, "obd:RPM": [4000.0] * len(maf)}
    )


def test_fuel_energy_power_burns_only_the_stoichiometric_fuel_for_the_air() -> None:
    """M01.6 known answer: rich of stoich the extra fuel makes no power.

    100 g/s of air on E10 is 100 / 14.08 = 7.102 g/s of fuel at stoich, 41.3
    MJ/kg, 30 % brake efficiency, 15 % driveline loss: 74.80 kW = 100.31 hp.
    At lambda 0.85 the same air carries 1/0.85 the fuel, and the uncapped
    estimate read 1/0.85 = 1.176 of that (118.01 hp) though the engine made the
    same power. The fuel rate still reports every gram injected.
    """
    out = compute_instantaneous_channels(
        _wideband([100.0, 100.0, 100.0], [1.0, 0.85, 1.2]), E10, speed_correction=UNCORRECTED
    )
    stoich_hp = 100.0 / 14.08 / 1000.0 * 41.3e3 * 0.30 * 0.85 * 1.34102209
    assert stoich_hp == pytest.approx(100.31, abs=0.01)
    power = out["derived:ESTIMATED_POWER_HP"]
    fuel = out["derived:FUEL_RATE_LPH"]
    assert power.iloc[0] == pytest.approx(stoich_hp, rel=1e-9)
    # The old reading scaled with the fuel; the fuel did go up by 1/0.85.
    assert fuel.iloc[1] / fuel.iloc[0] == pytest.approx(1 / 0.85, rel=1e-9)
    assert stoich_hp / 0.85 == pytest.approx(118.01, abs=0.01)
    assert power.iloc[1] == pytest.approx(stoich_hp, rel=1e-9)
    # Lean of stoich every gram burns, so the fuel flow stands: 1/1.2 of stoich.
    assert power.iloc[2] == pytest.approx(stoich_hp / 1.2, rel=1e-9)


def test_the_air_cap_uses_speed_density_air_without_maf() -> None:
    """A fuel-rate PID with only MAP-style air is capped by the speed-density air
    (the same air the fuel route falls back to), so the cap never needs MAF."""
    df = _frame(
        2,
        **{
            "obd:ENGINE_FUEL_RATE": [30.0, 1.0],
            "obd:RPM": [2000.0, 2000.0],
            "obd:INTAKE_PRESSURE": [100.0, 100.0],
            "obd:INTAKE_TEMP": [25.0, 25.0],
        },
    )
    with_air = compute_instantaneous_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    density_only = compute_instantaneous_channels(
        df.drop(columns=["obd:ENGINE_FUEL_RATE"]), GASOLINE, speed_correction=UNCORRECTED
    )
    power = with_air["derived:ESTIMATED_POWER_HP"]
    # 30 L/h is far richer than the air allows: capped at the stoich fuel.
    assert power.iloc[0] == pytest.approx(density_only["derived:ESTIMATED_POWER_HP"].iloc[0])
    # 1 L/h is lean of it: the fuel flow stands.
    assert power.iloc[1] < density_only["derived:ESTIMATED_POWER_HP"].iloc[1]


def test_fuel_energy_power_refuses_without_air() -> None:
    """The decision (M01.6): no air, no bounded burn, no power — never the
    uncapped fuel figure. No air source at all: the channel is absent; a row
    whose air is missing: NaN on that row."""
    fuel_only = _frame(2, **{"obd:ENGINE_FUEL_RATE": [6.0, 6.0]})
    out = compute_instantaneous_channels(fuel_only, GASOLINE, speed_correction=UNCORRECTED)
    assert "derived:FUEL_RATE_LPH" in out.columns
    assert "derived:ESTIMATED_POWER_HP" not in out.columns
    assert "derived:ESTIMATED_TORQUE_NM" not in out.columns

    gap = _frame(2, **{"obd:ENGINE_FUEL_RATE": [6.0, 6.0], "obd:MAF": [50.0, np.nan]})
    power = compute_instantaneous_channels(gap, GASOLINE, speed_correction=UNCORRECTED)[
        "derived:ESTIMATED_POWER_HP"
    ]
    assert power.iloc[0] > 0
    assert np.isnan(power.iloc[1])


def test_fuel_cut_rows_make_no_power() -> None:
    """A fuel-cut row is 0 g/s of fuel (M01.4), so the cap keeps it 0 power,
    with or without a MAF sample on the row: no fuel burns nothing whatever the
    air (the third row; NaN there on 2 rows of 2026-09-20T152758 before the fix)."""
    df = _frame(
        3,
        **{
            "obd:MAF": [7.35, 7.35, np.nan],
            "obd:COMMANDED_EQUIV_RATIO": [1.0, 0.0, 0.0],
            "obd:RPM": [2000.0] * 3,
        },
    )
    out = compute_instantaneous_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert out["derived:ESTIMATED_POWER_HP"].iloc[0] > 0
    assert out["derived:ESTIMATED_POWER_HP"].iloc[1] == 0.0
    assert out["derived:ESTIMATED_POWER_HP"].iloc[2] == 0.0


def test_idle_fraction_divides_by_engine_running_time() -> None:
    """M01.6 known answer: key-on engine-off time is not in the denominator.

    10 s KOEO (rpm 0), 10 s idling, 10 s driving, 1 Hz. Running time is the
    trapezoid of the running indicator over 29 s: 0.5 s for the start edge,
    then 19 s, = 19.5 s. Idle is 0.5 + 9 + 0.5 = 10 s. The fraction is 10 /
    19.5 = 0.513; over the 29 s duration it read 0.345.
    """
    df = _frame(
        30,
        **{"obd:RPM": [0.0] * 10 + [800.0] * 20, "obd:SPEED": [0.0] * 20 + [36.0] * 10},
    )
    trip = summarize_trip(df, GASOLINE, speed_correction=UNCORRECTED)
    assert trip.duration_s == pytest.approx(29.0)
    assert trip.idle_time_s == pytest.approx(10.0)
    assert trip.engine_running_s == pytest.approx(19.5)
    assert trip.idle_fraction == pytest.approx(10.0 / 19.5)
    assert 10.0 / trip.duration_s == pytest.approx(0.345, abs=1e-3)


def test_idle_uses_the_shared_engine_running_definition() -> None:
    """Idle and running both use ``ENGINE_RUNNING_MIN_RPM`` (rpm >= 400, the
    ``vehicle_state`` threshold): 300 rpm cranking is neither, so it used to
    count as idle (floor 200) without counting as running. 400 exactly is both."""
    df = _frame(3, **{"obd:RPM": [300.0, 300.0, 300.0], "obd:SPEED": [0.0, 0.0, 0.0]})
    cranking = summarize_trip(df, GASOLINE, speed_correction=UNCORRECTED)
    assert cranking.idle_time_s == 0.0
    assert cranking.engine_running_s == 0.0
    assert cranking.idle_fraction is None, "the engine never ran: no fraction, not 0"
    df["obd:RPM"] = 400.0
    at_floor = summarize_trip(df, GASOLINE, speed_correction=UNCORRECTED)
    assert at_floor.idle_time_s == pytest.approx(2.0)
    assert at_floor.idle_fraction == pytest.approx(1.0)


def test_idle_fraction_is_none_without_rpm_or_speed() -> None:
    trip = summarize_trip(
        _frame(3, **{"obd:SPEED": [0.0, 0.0, 0.0]}), GASOLINE, speed_correction=UNCORRECTED
    )
    assert trip.engine_running_s is None
    assert trip.idle_fraction is None


def test_cumulative_distance_fuel_idle() -> None:
    # 10 s at 36 kph = 0.01 km/s; first row dt=0 ⇒ 9 increments ⇒ 0.09 km.
    df = _frame(
        10,
        **{"obd:SPEED": [36.0] * 10, "obd:RPM": [800.0] * 10, "obd:MAF": [5.0] * 10},
    )
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert out["derived:TRIP_DISTANCE_KM"].iloc[-1] == pytest.approx(0.09, rel=1e-6)
    assert out["derived:FUEL_USED_L"].iloc[-1] > 0
    # Moving, so no idle accrues.
    assert out["derived:IDLE_TIME_S"].iloc[-1] == 0.0


def test_idle_time_accumulates_when_stopped() -> None:
    df = _frame(10, **{"obd:SPEED": [0.0] * 10, "obd:RPM": [800.0] * 10})
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert out["derived:IDLE_TIME_S"].iloc[-1] == pytest.approx(9.0, rel=1e-6)


def test_sparse_channel_integrates_over_its_own_sample_spacing() -> None:
    """A gap-masked (sparse) channel on a dense grid must not lose the time
    between its samples — the 2026-07-26 trip summary halved distance and
    quartered fuel exactly this way (contended bus → poll spacing wider
    than the resampler's ffill ceiling → NaN rows → cumsum accrues 0)."""
    # SPEED valid on every 3rd row only: 10 valid samples spanning 27 s at a
    # steady 36 kph ⇒ 0.27 km, NOT 9 × 1 s ⇒ 0.09 km.
    speed = [36.0 if i % 3 == 0 else np.nan for i in range(30)]
    df = _frame(30, **{"obd:SPEED": speed})
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert out["derived:TRIP_DISTANCE_KM"].iloc[-1] == pytest.approx(0.27, rel=1e-6)
    # NaN rows carry the running total, they don't hold NaN.
    assert not out["derived:TRIP_DISTANCE_KM"].isna().any()


def test_integration_dt_capped_over_true_outage() -> None:
    """A genuine capture outage (minutes) is still bounded to one capped
    increment, not credited at cruise speed for its whole span."""
    ts = pd.to_datetime(["2026-05-16T00:00:00Z", "2026-05-16T00:10:00Z"], utc=True)
    df = pd.DataFrame({"session": "s", "timestamp": ts, "obd:SPEED": [36.0, 36.0]})
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    # 600 s elapsed, capped at 60 s ⇒ 36 kph × 60 s = 0.6 km.
    assert out["derived:TRIP_DISTANCE_KM"].iloc[-1] == pytest.approx(0.6, rel=1e-6)


def test_integration_never_accrues_across_session_seam() -> None:
    """With the generous outage cap, a session seam must contribute exactly
    zero — the seam row restarts the trapezoid, not a capped increment."""
    ts = pd.to_datetime(
        ["2026-05-16T00:00:00Z", "2026-05-16T00:00:01Z"]
        + ["2026-05-16T00:00:20Z", "2026-05-16T00:00:21Z"],
        utc=True,
    )
    df = pd.DataFrame({"session": ["a", "a", "b", "b"], "timestamp": ts, "obd:SPEED": [36.0] * 4})
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    # 1 s in session a + 1 s in session b; the 19 s seam accrues nothing.
    assert out["derived:TRIP_DISTANCE_KM"].iloc[-1] == pytest.approx(0.02, rel=1e-6)


def test_idle_time_bridges_unsampled_rows() -> None:
    """Idle accrual is an indicator integral over the rows where RPM and
    SPEED are both sampled — unsampled rows in a steady idle bridge instead
    of dropping the elapsed time."""
    rpm = [800.0 if i % 2 == 0 else np.nan for i in range(11)]
    speed = [0.0 if i % 2 == 0 else np.nan for i in range(11)]
    df = _frame(11, **{"obd:SPEED": speed, "obd:RPM": rpm})
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    # Valid pairs every 2 s spanning 10 s, idling throughout.
    assert out["derived:IDLE_TIME_S"].iloc[-1] == pytest.approx(10.0, rel=1e-6)


def test_summarize_trip_accel_metrics() -> None:
    # Constant 8 kph/s ramp from a standstill for 30 s → crosses both
    # 60 mph (~96.6 kph) and a 1/4 mile.
    speed = [min(8.0 * t, 250.0) for t in range(31)]
    df = _frame(31, **{"obd:SPEED": speed, "obd:RPM": [3000.0] * 31, "obd:MAF": [20.0] * 31})
    summary = summarize_trip(df, GASOLINE, speed_correction=UNCORRECTED)
    assert summary.duration_s == pytest.approx(30.0, rel=1e-6)
    assert summary.max_speed_kph == pytest.approx(240.0, rel=1e-6)
    assert summary.zero_to_60_mph_s is not None
    assert 11.0 <= summary.zero_to_60_mph_s <= 14.0
    assert summary.quarter_mile_s is not None
    assert summary.quarter_mile_trap_kph is not None
    assert summary.fuel_used_l is not None and summary.fuel_used_l > 0
    assert summary.co2_kg is not None and summary.co2_kg > 0


def _wait_then_constant_accel(wait_s: int, accel_mps2: float, run_s: int) -> list[float]:
    """1 Hz speed trace (kph): ``wait_s`` at exactly 0, then constant accel."""
    return [0.0] * wait_s + [accel_mps2 * k * 3.6 for k in range(run_s + 1)]


def test_accel_metrics_clock_starts_at_last_stopped_sample() -> None:
    """M01.1: 30 s at 0 km/h then 3 m/s² must time the run, not the wait.

    Before the fix the clock started at the first stopped sample and this
    returned 0-60 = 39.0 s and ¼ mile = 47.0 s. The analytic run is
    26.8224 / 3 = 8.941 s to 60 mph and sqrt(2 · 402.336 / 3) = 16.377 s to
    the quarter mile, at 3 · 16.377 m/s = 176.9 kph.
    """
    speed = _wait_then_constant_accel(30, 3.0, 30)
    t = pd.Series(np.arange(len(speed), dtype=float))
    zero_60, qm, trap = _accel_metrics(pd.Series(speed, dtype=float), t)
    assert zero_60 == pytest.approx(26.8224 / 3.0, abs=1e-6)
    # Linear interpolation of the trapezoid distance between 1 Hz samples:
    # within 0.01 s of the analytic 16.377 s, not quantized to 17 s.
    assert qm == pytest.approx(np.sqrt(2 * 402.336 / 3.0), abs=0.01)
    assert trap == pytest.approx(3.0 * np.sqrt(2 * 402.336 / 3.0) * 3.6, abs=0.1)


def test_accel_metrics_wait_is_excluded_through_summarize_trip() -> None:
    speed = _wait_then_constant_accel(30, 3.0, 30)
    df = _frame(len(speed), **{"obd:SPEED": speed})
    summary = summarize_trip(df, GASOLINE, speed_correction=UNCORRECTED)
    assert summary.zero_to_60_mph_s == pytest.approx(8.941, abs=0.01)
    assert summary.quarter_mile_s == pytest.approx(16.377, abs=0.01)


def test_accel_metrics_run_on_road_speed() -> None:
    """M01.1 / M01.2: the thresholds are road speed and road distance. True
    speed ramps at 3 m/s^2 after a wait; the ECM reports 0.9203 of it, so
    "60 mph" is reached at an indicated 55.2 mph and the quarter mile ends at
    402.336 m of road, not of indicated distance. Uncorrected, the indicated
    trace reaches 60 mph at a true 65.2 mph and the quarter mile at 437 m."""
    true_kph = _wait_then_constant_accel(30, 3.0, 30)
    df = _frame(len(true_kph), **{"obd:SPEED": [v * 0.9203 for v in true_kph]})
    road = summarize_trip(df, GASOLINE, speed_correction=XTERRA_SPEED)
    raw = summarize_trip(df, GASOLINE, speed_correction=UNCORRECTED)
    sixty_mps = 60.0 * 1.609344 / 3.6
    assert road.zero_to_60_mph_s == pytest.approx(sixty_mps / 3.0, abs=0.01)  # 8.941 s
    assert road.quarter_mile_s == pytest.approx((2 * 402.336 / 3.0) ** 0.5, abs=0.01)
    assert raw.zero_to_60_mph_s == pytest.approx(sixty_mps / 0.9203 / 3.0, abs=0.01)  # 9.715 s
    assert raw.quarter_mile_s == pytest.approx((2 * 402.336 / 0.9203 / 3.0) ** 0.5, abs=0.01)


def test_accel_metrics_creep_below_launch_speed_restarts_clock() -> None:
    """Rolling forward at under 2 mph before the pull is still 'at the line':
    the clock starts at the last sample at or below launch speed, whatever
    distance the creep covered."""
    speed = [0.0, 2.0, 3.0, 2.0, 0.0, 3.0] + [3.0 * k * 3.6 for k in range(1, 31)]
    t = pd.Series(np.arange(len(speed), dtype=float))
    zero_60, qm, _ = _accel_metrics(pd.Series(speed, dtype=float), t)
    # Last stopped sample is index 5 (3 kph ≤ 2 mph = 3.22 kph), so the clock
    # starts there, not at index 0: 0-60 is the ramp's 8.94 s (the old code gave 10.0 s).
    assert zero_60 is not None and 8.6 < zero_60 < 8.95
    assert qm is not None and 16.0 < qm < 16.4


def test_accel_metrics_bogged_run_does_not_count() -> None:
    """A run that drops back to a stop before 60 mph is abandoned; the clean
    pull after it is timed from its own start."""
    bog = [0.0, 10.8, 21.6, 10.8, 0.0, 0.0, 0.0]
    pull = [3.0 * k * 3.6 for k in range(1, 31)]
    speed = bog + pull
    t = pd.Series(np.arange(len(speed), dtype=float))
    zero_60, _, _ = _accel_metrics(pd.Series(speed, dtype=float), t)
    assert zero_60 == pytest.approx(26.8224 / 3.0, abs=1e-6)


def test_accel_metrics_crossing_is_interpolated_not_quantized() -> None:
    """At 1 Hz the crossing lies between samples; the reported time must be
    the interpolated instant, not the next whole second."""
    # 4 m/s²: 60 mph at 6.7056 s, between the 6 s and 7 s samples.
    speed = [4.0 * k * 3.6 for k in range(0, 21)]
    t = pd.Series(np.arange(len(speed), dtype=float))
    zero_60, _, _ = _accel_metrics(pd.Series(speed, dtype=float), t)
    assert zero_60 == pytest.approx(26.8224 / 4.0, abs=1e-6)


def test_summarize_trip_no_accel_event() -> None:
    df = _frame(5, **{"obd:SPEED": [10.0] * 5, "obd:RPM": [1500.0] * 5})
    summary = summarize_trip(df, GASOLINE, speed_correction=UNCORRECTED)
    # Never started from a stop, so no 0-60 / quarter-mile.
    assert summary.zero_to_60_mph_s is None
    assert summary.quarter_mile_s is None
    assert summary.distance_km is not None


def test_summarize_trip_avg_speed_is_distance_over_duration() -> None:
    # Constant 36 kph for 10 s (9 s of elapsed dt) -> distance = 36 * 9/3600 km.
    df = _frame(10, **{"obd:SPEED": [36.0] * 10})
    summary = summarize_trip(df, GASOLINE, speed_correction=UNCORRECTED)
    assert summary.avg_speed_kph is not None
    assert summary.distance_km is not None
    assert summary.avg_speed_kph == pytest.approx(
        summary.distance_km / (summary.duration_s / 3600.0), rel=1e-6
    )


def test_empty_and_missing_inputs_are_safe() -> None:
    empty = compute_derived_channels(pd.DataFrame(), GASOLINE, speed_correction=UNCORRECTED)
    assert empty.empty
    summary = summarize_trip(pd.DataFrame(), GASOLINE, speed_correction=UNCORRECTED)
    assert summary.duration_s == 0.0
    assert summary.distance_km is None
    assert summary.avg_speed_kph is None
    # Frame with only an unrelated channel: no derived columns invented.
    df = _frame(3, **{"obd:COOLANT_TEMP": [80.0] * 3})
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert not [c for c in out.columns if c.startswith("derived:")]


def test_fuel_properties_by_type() -> None:
    assert fuel_properties(VehicleParameters(fuel_type="diesel")) is FUEL_PROPERTIES["diesel"]
    assert fuel_properties(VehicleParameters()) is FUEL_PROPERTIES["gasoline"]


# --------------------------------------------------------------------------- #
# GEAR_ESTIMATE (Task 4.6) — rpm/speed ratio clustered to the vehicle
# profile's gear-ratio table. No TCM on this bus; this is the only gear
# source, so the table lives on VehicleParameters (the vehicle profile).
# --------------------------------------------------------------------------- #


XTERRA_FD = 4.08
XTERRA_TIRE_M = 2.609  # back-solved loaded circumference, data/bindings.yaml 0x6F7 notes
RPM_PER_KPH_PER_UNIT_RATIO = XTERRA_FD * 1000.0 / 60.0 / XTERRA_TIRE_M


def _driveline(*rpm_per_kph: float) -> VehicleParameters:
    """A vehicle whose gears give exactly these rpm per km/h."""
    return VehicleParameters(
        final_drive=XTERRA_FD,
        tire_loaded_circumference_m=XTERRA_TIRE_M,
        gearbox_ratios=tuple(r / RPM_PER_KPH_PER_UNIT_RATIO for r in rpm_per_kph),
        driveline_evidence="test",
    )


def test_rpm_per_kph_is_computed_from_the_driveline() -> None:
    """Direct drive at the Xterra's 4.08 and 2.609 m: 26.06 rpm per km/h,
    i.e. about 2,606 rpm at 100 km/h."""
    vehicle = VehicleParameters(
        final_drive=4.08,
        gearbox_ratios=(1.0,),
        tire_loaded_circumference_m=2.609,
        driveline_evidence="data/bindings.yaml 0x6F7 notes",
    )
    assert vehicle.rpm_per_kph() == pytest.approx((26.063,), abs=1e-3)
    assert VehicleParameters(final_drive=4.08, driveline_evidence="x").rpm_per_kph() == ()


def test_the_retired_gear_ratios_field_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="final_drive, gearbox_ratios"):
        VehicleParameters(gear_ratios=(40.0, 25.0))  # type: ignore[call-arg]
    # A stored profile that still carries the old empty default keeps loading.
    assert VehicleParameters.model_validate({"gear_ratios": []}).gearbox_ratios == ()


def test_driveline_facts_need_their_evidence() -> None:
    with pytest.raises(ValueError, match="driveline_evidence"):
        VehicleParameters(final_drive=4.08)
    with pytest.raises(ValueError, match="driveline_evidence"):
        VehicleParameters(gearbox_ratios=(3.0, 1.0))


def test_tire_size_needs_no_evidence() -> None:
    """A tire swap saves on its own (operator, 2026-11-07)."""
    params = VehicleParameters(tire_loaded_circumference_m=2.6, tire_diameter_m=0.834)
    assert params.driveline_evidence is None


def test_gear_estimate_clusters_to_nearest_ratio() -> None:
    vehicle = _driveline(40.0, 25.0, 15.0, 10.0)
    df = _frame(
        4,
        **{
            "obd:RPM": [2000.0, 2000.0, 2000.0, 2000.0],
            "obd:SPEED": [50.0, 80.0, 133.333, 200.0],
        },
    )
    out = compute_instantaneous_channels(df, vehicle, speed_correction=UNCORRECTED)
    assert out["derived:GEAR_ESTIMATE"].to_list() == [1.0, 2.0, 3.0, 4.0]


def test_gear_estimate_skipped_without_ratio_table() -> None:
    """No driveline in the profile — the channel must be absent, not zero-filled."""
    df = _frame(2, **{"obd:RPM": [2000.0, 2000.0], "obd:SPEED": [50.0, 80.0]})
    out = compute_instantaneous_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert "derived:GEAR_ESTIMATE" not in out.columns


def test_gear_estimate_skipped_when_speed_or_rpm_missing() -> None:
    vehicle = _driveline(40.0, 25.0)
    df = _frame(2, **{"obd:RPM": [2000.0, 2000.0]})  # no SPEED
    out = compute_instantaneous_channels(df, vehicle, speed_correction=UNCORRECTED)
    assert "derived:GEAR_ESTIMATE" not in out.columns


def test_gear_estimate_masks_low_speed() -> None:
    """Below the min-speed gate the ratio is unreliable (neutral/clutch-in
    ambiguity, speed-PID quantization) — NaN, not a fabricated gear."""
    vehicle = _driveline(40.0, 25.0)
    df = _frame(2, **{"obd:RPM": [2000.0, 2000.0], "obd:SPEED": [0.0, 50.0]})
    out = compute_instantaneous_channels(df, vehicle, speed_correction=UNCORRECTED)
    values = out["derived:GEAR_ESTIMATE"]
    assert pd.isna(values.iloc[0])
    assert values.iloc[1] == 1.0


# --- PID 0x44 is phi on some ECMs and lambda on others ---


def _phi_cal() -> AfrCalibration:
    return AfrCalibration(afr_scale=14.7, commanded_equiv_ratio_convention="phi")


def _lambda_cal() -> AfrCalibration:
    return AfrCalibration(afr_scale=14.7, commanded_equiv_ratio_convention="lambda")


def test_commanded_ratio_is_read_through_the_vehicles_convention() -> None:
    """phi and lambda are reciprocals; the wide-frame path used to assume lambda.

    At an enrichment command of 1.242 the two readings are 11.83 vs 18.26 AFR
    — a 1.54x fuel error, silent at stoich and worst at WOT.
    """
    df = _frame(2, **{"obd:MAF": [14.7] * 2, "obd:COMMANDED_EQUIV_RATIO": [1.242] * 2})
    as_phi = compute_derived_channels(
        df, GASOLINE, speed_correction=UNCORRECTED, calibration=_phi_cal()
    )
    as_lambda = compute_derived_channels(
        df, GASOLINE, speed_correction=UNCORRECTED, calibration=_lambda_cal()
    )
    # fuel g/s = MAF / AFR; AFR = 14.7/1.242 = 11.83 (phi) vs 14.7*1.242 = 18.26.
    phi_lph = as_phi["derived:FUEL_RATE_LPH"].iloc[0]
    lambda_lph = as_lambda["derived:FUEL_RATE_LPH"].iloc[0]
    assert phi_lph / lambda_lph == pytest.approx(1.242**2, rel=1e-6)


def test_an_unverified_convention_refuses_to_guess_away_from_stoich() -> None:
    """Both conventions agree at 1.0, so only those rows are interpretable."""
    df = _frame(2, **{"obd:MAF": [14.7] * 2, "obd:COMMANDED_EQUIV_RATIO": [1.0, 1.242]})
    out = compute_derived_channels(
        df, GASOLINE, speed_correction=UNCORRECTED
    )  # no calibration passed
    rate = out["derived:FUEL_RATE_LPH"]
    assert np.isfinite(rate.iloc[0]), "a stoich command is unambiguous and must compute"
    assert pd.isna(rate.iloc[1]), "an enrichment command with no verified convention must not guess"


def test_measured_wideband_lambda_is_unaffected_by_the_convention() -> None:
    """The calibration describes PID 0x44 only; 0x24/0x25 stay lambda."""
    df = _frame(
        2,
        **{
            "obd:MAF": [14.7] * 2,
            "obd:O2_WR_LAMBDA_S1": [1.25] * 2,
            "obd:COMMANDED_EQUIV_RATIO": [1.242] * 2,
        },
    )
    with_cal = compute_derived_channels(
        df, GASOLINE, speed_correction=UNCORRECTED, calibration=_phi_cal()
    )
    without = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert with_cal["derived:FUEL_RATE_LPH"].iloc[0] == pytest.approx(
        without["derived:FUEL_RATE_LPH"].iloc[0], rel=1e-9
    )


# --------------------------------------------------------------------------- #
# M01.2 — road speed: the profile's measured ratio, applied once
# --------------------------------------------------------------------------- #

XTERRA_SPEED = SpeedCorrection.measured(0.9203, "test: the Xterra's obd:SPEED ratio")


def test_distance_and_average_speed_integrate_road_speed() -> None:
    """One hour at an indicated 100 km/h is 108.66 km on a 0.9203 ECM."""
    df = _frame(3601, **{"obd:SPEED": [100.0] * 3601, "obd:RPM": [2000.0] * 3601})
    raw = summarize_trip(df, GASOLINE, speed_correction=UNCORRECTED)
    road = summarize_trip(df, GASOLINE, speed_correction=XTERRA_SPEED)
    assert raw.distance_km == pytest.approx(100.0)
    assert road.distance_km == pytest.approx(100.0 / 0.9203)
    assert road.avg_speed_kph == pytest.approx(100.0 / 0.9203)
    assert road.max_speed_kph == pytest.approx(100.0 / 0.9203)


def test_the_summary_states_its_speed_basis() -> None:
    df = _frame(3, **{"obd:SPEED": [10.0, 20.0, 30.0]})
    road = summarize_trip(df, GASOLINE, speed_correction=XTERRA_SPEED)
    raw = summarize_trip(df, GASOLINE, speed_correction=UNCORRECTED)
    assert road.speed_basis == XTERRA_SPEED.basis
    assert "/ 0.9203" in road.speed_basis
    assert "uncorrected" in raw.speed_basis
    empty = summarize_trip(pd.DataFrame(), GASOLINE, speed_correction=XTERRA_SPEED)
    assert empty.speed_basis == XTERRA_SPEED.basis
    assert empty.to_dict()["fuel_basis"].startswith("gasoline:")


def test_the_basis_fields_are_required() -> None:
    """A summary cannot be built without saying what it was computed on."""
    fields = TripSummary.__dataclass_fields__
    for name in ("speed_basis", "fuel_basis"):
        assert fields[name].default is dataclasses.MISSING
        assert fields[name].default_factory is dataclasses.MISSING


def test_gear_estimate_matches_on_road_speed() -> None:
    """A close-ratio box: gears 6 % apart at true speed. Indicated speed
    8 % low moves the ratio by more than half the gap, so the uncorrected
    estimate names the wrong gear and the corrected one the right one."""
    vehicle = _driveline(20.0, 18.8)
    true_kph = 100.0
    rpm = 18.8 * true_kph  # in the higher gear
    df = _frame(2, **{"obd:RPM": [rpm, rpm], "obd:SPEED": [true_kph * 0.9203] * 2})
    road = compute_instantaneous_channels(df, vehicle, speed_correction=XTERRA_SPEED)
    raw = compute_instantaneous_channels(df, vehicle, speed_correction=UNCORRECTED)
    assert road["derived:GEAR_ESTIMATE"].to_list() == [2.0, 2.0]
    assert raw["derived:GEAR_ESTIMATE"].to_list() == [1.0, 1.0]


def test_instant_economy_divides_by_road_speed() -> None:
    df = _frame(2, **{"obd:MAF": [7.35, 7.35], "obd:SPEED": [92.03, 92.03]})
    out = compute_instantaneous_channels(df, GASOLINE, speed_correction=XTERRA_SPEED)
    lph = out["derived:FUEL_RATE_LPH"].iloc[0]
    assert out["derived:INSTANT_ECONOMY_L_PER_100KM"].iloc[0] == pytest.approx(lph)


# --------------------------------------------------------------------------- #
# M01.4 — no fuel in decel fuel cut
# --------------------------------------------------------------------------- #


def test_fuel_cut_rows_count_no_fuel_on_the_wideband_route() -> None:
    """MAF reads a few g/s in fuel cut and the wideband its lean limit; the
    injectors are off, so the row is 0, not MAF / (stoich * lambda)."""
    df = _frame(
        4,
        **{
            "obd:MAF": [10.0, 5.0, 5.0, 10.0],
            "obd:O2_WR_LAMBDA_S1": [1.0, 2.0, 2.0, 1.0],
            "obd:COMMANDED_EQUIV_RATIO": [1.0, 0.0, 0.0, 1.0],
        },
    )
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    rate = out["derived:FUEL_RATE_LPH"].to_list()
    assert rate[1] == 0.0 and rate[2] == 0.0
    fuelled = 10.0 / 14.7 / 745.0 * 3600.0
    assert rate[0] == pytest.approx(fuelled)
    # Trapezoid: half a fuelled second at each edge of the cut.
    assert out["derived:FUEL_USED_L"].iloc[-1] == pytest.approx(fuelled / 3600.0)


def test_fuel_cut_is_not_bridged_on_the_commanded_ratio_route() -> None:
    """Without a wideband, PID 0x44 = 0 used to give NaN, and the integrator
    bridged the cut with the fuelled samples either side."""
    df = _frame(
        5,
        **{
            "obd:MAF": [14.7, 3.0, 3.0, 3.0, 14.7],
            "obd:COMMANDED_EQUIV_RATIO": [1.0, 0.0, 0.0, 0.0, 1.0],
        },
    )
    out = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    one_g_s_lph = 1.0 / 745.0 * 3600.0
    assert out["derived:FUEL_RATE_LPH"].to_list() == pytest.approx(
        [one_g_s_lph, 0.0, 0.0, 0.0, one_g_s_lph]
    )
    assert out["derived:FUEL_USED_L"].iloc[-1] == pytest.approx(one_g_s_lph / 3600.0)


def test_missing_lambda_outside_fuel_cut_stays_nan_not_stoich() -> None:
    """The open-loop-enrichment rule stands: no lambda and no cut is unknown."""
    df = _frame(
        2,
        **{
            "obd:MAF": [30.0, 30.0],
            "obd:O2_WR_LAMBDA_S1": [0.0, 0.0],
            "obd:COMMANDED_EQUIV_RATIO": [1.2, 1.2],
        },
    )
    out = compute_instantaneous_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    assert out["derived:FUEL_RATE_LPH"].isna().all()


def test_fuel_basis_says_whether_fuel_cut_was_detectable() -> None:
    with_phi = _frame(3, **{"obd:MAF": [5.0] * 3, "obd:COMMANDED_EQUIV_RATIO": [1.0, 0.0, 1.0]})
    without = _frame(3, **{"obd:MAF": [5.0] * 3})
    e10 = VehicleParameters(fuel_type="e10")
    basis = summarize_trip(with_phi, e10, speed_correction=UNCORRECTED).fuel_basis
    assert basis.startswith("e10: stoich 14.08, 0.749 kg/L, 2.28 kg CO2/L, LHV 41.3 MJ/kg")
    assert "zero in decel fuel cut" in basis and "on 1 rows" in basis
    assert "not detectable" in fuel_basis(e10, without)


# --------------------------------------------------------------------------- #
# M01.3(b) — E10
# --------------------------------------------------------------------------- #


def test_e10_constants() -> None:
    e10 = fuel_properties(VehicleParameters(fuel_type="e10"))
    assert e10 == FuelProperties(14.08, 0.749, 2.28, 41.3)


def test_e10_fuel_volume_moves_by_the_analytic_factor_per_unit_air() -> None:
    """Fuel volume = MAF / (stoich * lambda * density), so E0 -> E10 is
    (14.7 * 0.745) / (14.08 * 0.749) = 1.0385 per unit MAF, every sample."""
    df = _frame(
        4,
        **{"obd:MAF": [4.0, 25.0, 60.0, 150.0], "obd:O2_WR_LAMBDA_S1": [1.0, 1.0, 0.95, 0.85]},
    )
    e0 = compute_derived_channels(df, GASOLINE, speed_correction=UNCORRECTED)
    e10 = compute_derived_channels(
        df, VehicleParameters(fuel_type="e10"), speed_correction=UNCORRECTED
    )
    factor = (14.7 * 0.745) / (14.08 * 0.749)
    ratio = e10["derived:FUEL_RATE_LPH"] / e0["derived:FUEL_RATE_LPH"]
    assert ratio.to_list() == pytest.approx([factor] * 4)
    assert factor == pytest.approx(1.03845, abs=1e-5)


@pytest.mark.parametrize(("fuel", "ethanol_fraction"), [("e10", 0.10), ("e85", 0.85)])
def test_blend_co2_and_heating_value_follow_the_blend_arithmetic(
    fuel: str, ethanol_fraction: float
) -> None:
    """Ethanol (0.789 kg/L, 52.14 % C, 26.8 MJ/kg) + E0 (0.745 kg/L, 86.6 % C,
    43.0 MJ/kg) by volume: the comment's arithmetic and the table agree."""
    ethanol_kg, gasoline_kg = ethanol_fraction * 0.789, (1 - ethanol_fraction) * 0.745
    carbon = ethanol_kg * 0.5214 + gasoline_kg * 0.866
    lhv = (ethanol_kg * 26.8 + gasoline_kg * 43.0) / (ethanol_kg + gasoline_kg)
    props = FUEL_PROPERTIES[fuel]
    assert props.co2_kg_per_l == pytest.approx(carbon * 44.01 / 12.01, abs=0.005)
    assert props.lhv_mj_per_kg == pytest.approx(lhv, abs=0.05)


def test_fuel_cut_is_defined_once() -> None:
    """``src.signals.fuel_cut`` is the one definition (M01.4: ``derived``
    cannot import ``afr``, which imports ``derived``); ``afr`` re-exports it,
    and nothing under ``src/`` defines a second ``fuel_cut_rows``."""
    from src.paths import REPO_ROOT
    from src.signals import afr, fuel_cut

    assert afr.fuel_cut_rows is fuel_cut.fuel_cut_rows
    assert afr.COMMANDED_EQUIV_RATIO_COLUMN is fuel_cut.COMMANDED_EQUIV_RATIO_COLUMN
    definers = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "src").rglob("*.py")
        if "def fuel_cut_rows(" in path.read_text()
    )
    assert definers == ["src/signals/fuel_cut.py"]
