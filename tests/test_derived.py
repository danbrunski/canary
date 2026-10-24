"""Tests for trip/fuel/performance derived channels (Task 6.6)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.config import VehicleParameters
from src.signals.afr import AfrCalibration
from src.signals.derived import (
    FUEL_PROPERTIES,
    compute_derived_channels,
    compute_instantaneous_channels,
    fuel_properties,
    summarize_trip,
)

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


def test_a_reported_lambda_of_zero_is_absent_not_infinite() -> None:
    """PID 0x44 reads 0 through open loop on this ECM — "not available".

    Dividing by it produced `inf` fuel rate on the 2026-08-07 hybrid drive.
    The sample must go missing, not infinite, and not silently be recomputed
    at stoich: λ is unavailable exactly during open-loop enrichment, where
    assuming λ=1.0 is guaranteed wrong.
    """
    df = _frame(3, **{"obd:MAF": [7.35] * 3, "obd:COMMANDED_EQUIV_RATIO": [1.0, 0.0, 1.0]})
    out = compute_derived_channels(df, GASOLINE)
    rate = out["derived:FUEL_RATE_LPH"]
    assert np.isfinite(rate.iloc[0]) and rate.iloc[0] == pytest.approx(2.4161, rel=1e-3)
    assert pd.isna(rate.iloc[1])
    assert np.isfinite(rate.iloc[2])


def test_one_infinite_sample_cannot_poison_a_cumulative_channel() -> None:
    """`inf` passes `notna`, so it used to latch the running total forever.

    80 % of a 26-minute drive's FUEL_USED_L was `inf` from a single bad
    sample. The guard belongs in the integrator, not in every caller.
    """
    df = _frame(4, **{"obd:MAF": [7.35] * 4, "obd:SPEED": [100.0] * 4})
    df.loc[1, "obd:MAF"] = np.inf
    out = compute_derived_channels(df, GASOLINE)
    used = out["derived:FUEL_USED_L"]
    assert np.isfinite(used).all(), "an infinite sample escaped into the integral"
    assert used.iloc[-1] > 0


def test_engine_fuel_rate_pid_preferred_over_maf() -> None:
    df = _frame(2, **{"obd:ENGINE_FUEL_RATE": [6.0, 6.0], "obd:MAF": [50.0, 50.0]})
    out = compute_derived_channels(df, GASOLINE)
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
    out = compute_derived_channels(df, GASOLINE)
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


def test_boost_vacuum_skipped_when_baro_missing() -> None:
    """No baro means no ambient reference, so the channel is uncomputable.

    It used to fall back to sea-level 101.325 kPa, which is ~21 kPa wrong at
    the ~2000 m this vehicle runs at — reporting an NA engine as permanently
    deep in vacuum. Skipping matches the rest of compute_derived_channels.
    """
    df = _frame(2, **{"obd:INTAKE_PRESSURE": [40.0, 110.0]})
    out = compute_derived_channels(df, GASOLINE)
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


def test_sparse_channel_integrates_over_its_own_sample_spacing() -> None:
    """A gap-masked (sparse) channel on a dense grid must not lose the time
    between its samples — the 2026-07-26 trip summary halved distance and
    quartered fuel exactly this way (contended bus → poll spacing wider
    than the resampler's ffill ceiling → NaN rows → cumsum accrues 0)."""
    # SPEED valid on every 3rd row only: 10 valid samples spanning 27 s at a
    # steady 36 kph ⇒ 0.27 km, NOT 9 × 1 s ⇒ 0.09 km.
    speed = [36.0 if i % 3 == 0 else np.nan for i in range(30)]
    df = _frame(30, **{"obd:SPEED": speed})
    out = compute_derived_channels(df, GASOLINE)
    assert out["derived:TRIP_DISTANCE_KM"].iloc[-1] == pytest.approx(0.27, rel=1e-6)
    # NaN rows carry the running total, they don't hold NaN.
    assert not out["derived:TRIP_DISTANCE_KM"].isna().any()


def test_integration_dt_capped_over_true_outage() -> None:
    """A genuine capture outage (minutes) is still bounded to one capped
    increment, not credited at cruise speed for its whole span."""
    ts = pd.to_datetime(["2026-05-16T00:00:00Z", "2026-05-16T00:10:00Z"], utc=True)
    df = pd.DataFrame({"session": "s", "timestamp": ts, "obd:SPEED": [36.0, 36.0]})
    out = compute_derived_channels(df, GASOLINE)
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
    out = compute_derived_channels(df, GASOLINE)
    # 1 s in session a + 1 s in session b; the 19 s seam accrues nothing.
    assert out["derived:TRIP_DISTANCE_KM"].iloc[-1] == pytest.approx(0.02, rel=1e-6)


def test_idle_time_bridges_unsampled_rows() -> None:
    """Idle accrual is an indicator integral over the rows where RPM and
    SPEED are both sampled — unsampled rows in a steady idle bridge instead
    of dropping the elapsed time."""
    rpm = [800.0 if i % 2 == 0 else np.nan for i in range(11)]
    speed = [0.0 if i % 2 == 0 else np.nan for i in range(11)]
    df = _frame(11, **{"obd:SPEED": speed, "obd:RPM": rpm})
    out = compute_derived_channels(df, GASOLINE)
    # Valid pairs every 2 s spanning 10 s, idling throughout.
    assert out["derived:IDLE_TIME_S"].iloc[-1] == pytest.approx(10.0, rel=1e-6)


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


def test_summarize_trip_avg_speed_is_distance_over_duration() -> None:
    # Constant 36 kph for 10 s (9 s of elapsed dt) -> distance = 36 * 9/3600 km.
    df = _frame(10, **{"obd:SPEED": [36.0] * 10})
    summary = summarize_trip(df, GASOLINE)
    assert summary.avg_speed_kph is not None
    assert summary.distance_km is not None
    assert summary.avg_speed_kph == pytest.approx(
        summary.distance_km / (summary.duration_s / 3600.0), rel=1e-6
    )


def test_empty_and_missing_inputs_are_safe() -> None:
    empty = compute_derived_channels(pd.DataFrame(), GASOLINE)
    assert empty.empty
    summary = summarize_trip(pd.DataFrame(), GASOLINE)
    assert summary.duration_s == 0.0
    assert summary.distance_km is None
    assert summary.avg_speed_kph is None
    # Frame with only an unrelated channel: no derived columns invented.
    df = _frame(3, **{"obd:COOLANT_TEMP": [80.0] * 3})
    out = compute_derived_channels(df, GASOLINE)
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


def test_gear_estimate_clusters_to_nearest_ratio() -> None:
    vehicle = _driveline(40.0, 25.0, 15.0, 10.0)
    df = _frame(
        4,
        **{
            "obd:RPM": [2000.0, 2000.0, 2000.0, 2000.0],
            "obd:SPEED": [50.0, 80.0, 133.333, 200.0],
        },
    )
    out = compute_instantaneous_channels(df, vehicle)
    assert out["derived:GEAR_ESTIMATE"].to_list() == [1.0, 2.0, 3.0, 4.0]


def test_gear_estimate_skipped_without_ratio_table() -> None:
    """No driveline in the profile — the channel must be absent, not zero-filled."""
    df = _frame(2, **{"obd:RPM": [2000.0, 2000.0], "obd:SPEED": [50.0, 80.0]})
    out = compute_instantaneous_channels(df, GASOLINE)
    assert "derived:GEAR_ESTIMATE" not in out.columns


def test_gear_estimate_skipped_when_speed_or_rpm_missing() -> None:
    vehicle = _driveline(40.0, 25.0)
    df = _frame(2, **{"obd:RPM": [2000.0, 2000.0]})  # no SPEED
    out = compute_instantaneous_channels(df, vehicle)
    assert "derived:GEAR_ESTIMATE" not in out.columns


def test_gear_estimate_masks_low_speed() -> None:
    """Below the min-speed gate the ratio is unreliable (neutral/clutch-in
    ambiguity, speed-PID quantization) — NaN, not a fabricated gear."""
    vehicle = _driveline(40.0, 25.0)
    df = _frame(2, **{"obd:RPM": [2000.0, 2000.0], "obd:SPEED": [0.0, 50.0]})
    out = compute_instantaneous_channels(df, vehicle)
    values = out["derived:GEAR_ESTIMATE"]
    assert pd.isna(values.iloc[0])
    assert values.iloc[1] == 1.0


# --- PID 0x44 is phi on some ECMs and lambda on others ---


def _phi_cal() -> AfrCalibration:
    return AfrCalibration(commanded_equiv_ratio_convention="phi")


def _lambda_cal() -> AfrCalibration:
    return AfrCalibration(commanded_equiv_ratio_convention="lambda")


def test_commanded_ratio_is_read_through_the_vehicles_convention() -> None:
    """phi and lambda are reciprocals; the wide-frame path used to assume lambda.

    At an enrichment command of 1.242 the two readings are 11.83 vs 18.26 AFR
    — a 1.54x fuel error, silent at stoich and worst at WOT.
    """
    df = _frame(2, **{"obd:MAF": [14.7] * 2, "obd:COMMANDED_EQUIV_RATIO": [1.242] * 2})
    as_phi = compute_derived_channels(df, GASOLINE, calibration=_phi_cal())
    as_lambda = compute_derived_channels(df, GASOLINE, calibration=_lambda_cal())
    # fuel g/s = MAF / AFR; AFR = 14.7/1.242 = 11.83 (phi) vs 14.7*1.242 = 18.26.
    phi_lph = as_phi["derived:FUEL_RATE_LPH"].iloc[0]
    lambda_lph = as_lambda["derived:FUEL_RATE_LPH"].iloc[0]
    assert phi_lph / lambda_lph == pytest.approx(1.242**2, rel=1e-6)


def test_an_unverified_convention_refuses_to_guess_away_from_stoich() -> None:
    """Both conventions agree at 1.0, so only those rows are interpretable."""
    df = _frame(2, **{"obd:MAF": [14.7] * 2, "obd:COMMANDED_EQUIV_RATIO": [1.0, 1.242]})
    out = compute_derived_channels(df, GASOLINE)  # no calibration passed
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
    with_cal = compute_derived_channels(df, GASOLINE, calibration=_phi_cal())
    without = compute_derived_channels(df, GASOLINE)
    assert with_cal["derived:FUEL_RATE_LPH"].iloc[0] == pytest.approx(
        without["derived:FUEL_RATE_LPH"].iloc[0], rel=1e-9
    )
