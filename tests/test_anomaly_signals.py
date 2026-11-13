"""Tests for :func:`compute_anomaly_signals`.

The derived signals power the new fuel/air behavior recipes (Phase 1: no
DIDs needed; Phase 2: requires DID polling). This test set pins both
the "inputs present" emit and the "inputs absent" no-op contract — each
new derived column must not appear at all when its inputs are missing,
so the recipe layer skips cleanly rather than fitting to NaN.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd
import pytest

from src.config import DISPLACEMENT_NOT_SET, VehicleParameters
from src.signals import derived
from src.signals.derived import (
    LEGACY_VE_CHANNEL,
    VE_AMBIENT_CHANNEL,
    VE_MANIFOLD_CHANNEL,
    compute_anomaly_signals,
    resolved_dependencies,
)


def _base(rows: int = 60) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "session": ["s"] * rows,
            "timestamp": pd.date_range("2026-05-20T12:00:00Z", periods=rows, freq="1s"),
        }
    )


# --------------------------------------------------------------------------- #
# AIR_MASS_PER_CYCLE — MAF + RPM
# --------------------------------------------------------------------------- #


def test_air_mass_per_cycle_emitted_with_maf_and_rpm() -> None:
    """4-stroke: mass/cycle = MAF * 120 / RPM. Pin the formula."""
    frame = _base(5).assign(
        **{
            "obd:MAF": [5.0, 5.0, 5.0, 5.0, 5.0],  # g/s
            "obd:RPM": [600.0, 1200.0, 2400.0, 4800.0, 600.0],
        }
    )
    out = compute_anomaly_signals(frame)
    assert "derived:AIR_MASS_PER_CYCLE" in out.columns
    values = out["derived:AIR_MASS_PER_CYCLE"].to_list()
    # MAF=5, RPM=600 → 5 * 120 / 600 = 1.0 g/cycle
    assert math.isclose(values[0], 1.0, rel_tol=1e-9)
    # MAF=5, RPM=2400 → 5 * 120 / 2400 = 0.25 g/cycle
    assert math.isclose(values[2], 0.25, rel_tol=1e-9)


def test_air_mass_per_cycle_skipped_when_inputs_missing() -> None:
    frame = _base(5).assign(**{"obd:MAF": [5.0] * 5})  # no RPM
    out = compute_anomaly_signals(frame)
    assert "derived:AIR_MASS_PER_CYCLE" not in out.columns


def test_air_mass_per_cycle_handles_zero_rpm_safely() -> None:
    """RPM=0 (engine off / KOEO) → NaN, no ZeroDivisionError."""
    frame = _base(3).assign(
        **{
            "obd:MAF": [0.0, 5.0, 5.0],
            "obd:RPM": [0.0, 600.0, 600.0],
        }
    )
    out = compute_anomaly_signals(frame)
    values = out["derived:AIR_MASS_PER_CYCLE"]
    assert pd.isna(values.iloc[0])
    assert math.isclose(values.iloc[1], 1.0, rel_tol=1e-9)


def test_air_mass_per_cycle_propagates_source_freshness() -> None:
    columns: dict[str, Any] = {
        "obd:MAF": [5.0, 5.0],
        "obd:RPM": [600.0, 600.0],
        "obd:MAF__gap": [False, True],
        "obd:RPM__gap": [False, False],
    }
    frame = _base(2).assign(**columns)
    out = compute_anomaly_signals(frame)
    assert out["derived:AIR_MASS_PER_CYCLE__gap"].tolist() == [False, True]


# --------------------------------------------------------------------------- #
# VE_PERCENT (Task 4.1) — measured air mass per cycle over the theoretical
# 100%-VE fill from MAP-or-baro/IAT/displacement.
# --------------------------------------------------------------------------- #


def test_ve_percent_computed_from_measured_over_theoretical() -> None:
    vehicle = VehicleParameters(displacement_l=4.0)
    frame = _base(3).assign(
        **{
            "obd:MAF": [5.0] * 3,
            "obd:RPM": [1200.0] * 3,
            "obd:INTAKE_PRESSURE": [101.325] * 3,
            "obd:INTAKE_TEMP": [25.0] * 3,
        }
    )
    out = compute_anomaly_signals(frame, vehicle)
    assert "derived:VE_PERCENT" in out.columns
    measured_g = 5.0 * 120.0 / 1200.0
    density_kg_m3 = (101.325 * 1000.0) / (287.05 * (25.0 + 273.15))
    theoretical_g = density_kg_m3 * (4.0 / 1000.0) * 1000.0
    expected = measured_g / theoretical_g * 100.0
    assert math.isclose(out["derived:VE_PERCENT"].iloc[0], expected, rel_tol=1e-6)


def test_ve_percent_falls_back_to_baro_without_map() -> None:
    vehicle = VehicleParameters(displacement_l=4.0)
    frame = _base(2).assign(
        **{
            "obd:MAF": [5.0] * 2,
            "obd:RPM": [1200.0] * 2,
            "obd:BAROMETRIC_PRESSURE": [101.325] * 2,
        }
    )
    out = compute_anomaly_signals(frame, vehicle)
    assert "derived:VE_PERCENT" in out.columns


def test_ve_percent_skipped_without_vehicle_param() -> None:
    """`vehicle` defaults to None so bare-frame callers/tests keep working —
    VE_PERCENT is gated on it same as any other absent input."""
    frame = _base(2).assign(
        **{"obd:MAF": [5.0] * 2, "obd:RPM": [1200.0] * 2, "obd:INTAKE_PRESSURE": [101.325] * 2}
    )
    out = compute_anomaly_signals(frame)
    assert "derived:AIR_MASS_PER_CYCLE" in out.columns
    assert "derived:VE_PERCENT" not in out.columns


def test_ve_percent_skipped_without_pressure() -> None:
    vehicle = VehicleParameters(displacement_l=4.0)
    frame = _base(2).assign(**{"obd:MAF": [5.0] * 2, "obd:RPM": [1200.0] * 2})
    out = compute_anomaly_signals(frame, vehicle)
    assert "derived:VE_PERCENT" not in out.columns


def test_ve_channels_refuse_by_name_without_displacement(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M01.20: displacement has no default. A profile that does not state it
    gets no VE channel, not a 4.0 L engine's, and the log names the missing
    fact once per channel; the channels that do not need it are unaffected."""
    monkeypatch.setattr(derived, "_REFUSALS_LOGGED", set())
    assert VehicleParameters().displacement_l is None
    frame = _base(3).assign(
        **{
            "obd:MAF": [5.0] * 3,
            "obd:RPM": [1200.0] * 3,
            "obd:INTAKE_PRESSURE": [35.0] * 3,
            "obd:BAROMETRIC_PRESSURE": [80.0] * 3,
            "obd:INTAKE_TEMP": [20.0] * 3,
        }
    )
    with caplog.at_level(logging.WARNING, logger=derived.log.name):
        out = compute_anomaly_signals(frame, VehicleParameters())
        compute_anomaly_signals(frame, VehicleParameters())  # a second tick
    for channel in (LEGACY_VE_CHANNEL, VE_MANIFOLD_CHANNEL, VE_AMBIENT_CHANNEL):
        assert channel not in out.columns
        named = [r for r in caplog.records if r.getMessage().startswith(f"{channel} refused")]
        assert len(named) == 1 and DISPLACEMENT_NOT_SET in named[0].getMessage()
    assert "derived:AIR_MASS_PER_CYCLE" in out.columns
    stated = compute_anomaly_signals(frame, VehicleParameters(displacement_l=3.954))
    assert {LEGACY_VE_CHANNEL, VE_MANIFOLD_CHANNEL, VE_AMBIENT_CHANNEL} <= set(stated.columns)


# --------------------------------------------------------------------------- #
# B04.5 — one VE definition per channel: manifold- vs ambient-referenced,
# measured IAT only, provenance of the legacy channel.
# --------------------------------------------------------------------------- #


def _fill_g(pressure_kpa: float, iat_c: float, displacement_l: float) -> float:
    return (pressure_kpa * 1000.0) / (287.05 * (iat_c + 273.15)) * displacement_l


def test_referenced_ve_channels_never_substitute_one_reference_for_the_other() -> None:
    vehicle = VehicleParameters(displacement_l=4.0)
    frame = _base(3).assign(
        **{
            "obd:MAF": [5.0] * 3,
            "obd:RPM": [1200.0] * 3,
            "obd:INTAKE_PRESSURE": [35.0] * 3,  # part-throttle manifold vacuum
            "obd:BAROMETRIC_PRESSURE": [80.0] * 3,  # ~2000 m ambient
            "obd:INTAKE_TEMP": [20.0] * 3,
        }
    )
    out = compute_anomaly_signals(frame, vehicle)
    measured = 5.0 * 120.0 / 1200.0
    manifold = out[VE_MANIFOLD_CHANNEL].iloc[0]
    ambient = out[VE_AMBIENT_CHANNEL].iloc[0]
    assert math.isclose(manifold, measured / _fill_g(35.0, 20.0, 4.0) * 100.0, rel_tol=1e-9)
    assert math.isclose(ambient, measured / _fill_g(80.0, 20.0, 4.0) * 100.0, rel_tol=1e-9)
    assert manifold > 2.0 * ambient  # two quantities, not one with noise
    # The legacy channel took MAP because the session carried it.
    assert math.isclose(out[LEGACY_VE_CHANNEL].iloc[0], manifold, rel_tol=1e-6)
    assert resolved_dependencies(out, LEGACY_VE_CHANNEL) == [
        "derived:AIR_MASS_PER_CYCLE",
        "obd:INTAKE_PRESSURE",
        "obd:INTAKE_TEMP",
    ]


def test_without_map_only_the_ambient_reference_exists() -> None:
    vehicle = VehicleParameters(displacement_l=4.0)
    frame = _base(2).assign(
        **{
            "obd:MAF": [5.0] * 2,
            "obd:RPM": [1200.0] * 2,
            "obd:BAROMETRIC_PRESSURE": [80.0] * 2,
            "obd:INTAKE_TEMP": [20.0] * 2,
        }
    )
    out = compute_anomaly_signals(frame, vehicle)
    assert VE_MANIFOLD_CHANNEL not in out.columns
    assert VE_AMBIENT_CHANNEL in out.columns
    assert not out[f"{VE_AMBIENT_CHANNEL}__gap"].any()
    # Legacy provenance: this frame's VE_PERCENT is the ambient-referenced one.
    assert resolved_dependencies(out, LEGACY_VE_CHANNEL)[1] == "obd:BAROMETRIC_PRESSURE"
    assert math.isclose(
        out[LEGACY_VE_CHANNEL].iloc[0], out[VE_AMBIENT_CHANNEL].iloc[0], rel_tol=1e-6
    )


def test_missing_iat_is_an_unavailable_input_not_25_degc() -> None:
    vehicle = VehicleParameters(displacement_l=4.0)
    frame = _base(2).assign(
        **{
            "obd:MAF": [5.0] * 2,
            "obd:RPM": [1200.0] * 2,
            "obd:INTAKE_PRESSURE": [35.0] * 2,
            "obd:BAROMETRIC_PRESSURE": [80.0] * 2,
        }
    )
    out = compute_anomaly_signals(frame, vehicle)
    assert VE_MANIFOLD_CHANNEL not in out.columns
    assert VE_AMBIENT_CHANNEL not in out.columns
    # The legacy channel still computes at 25 degC (detector compatibility),
    # but every such row is stale and its provenance names the missing input.
    assert LEGACY_VE_CHANNEL in out.columns
    assert out[f"{LEGACY_VE_CHANNEL}__gap"].all()
    assert resolved_dependencies(out, LEGACY_VE_CHANNEL)[2] is None


def test_referenced_ve_row_with_stale_iat_is_stale() -> None:
    vehicle = VehicleParameters(displacement_l=4.0)
    frame = _base(2).assign(
        **{
            "obd:MAF": [5.0] * 2,
            "obd:RPM": [1200.0] * 2,
            "obd:BAROMETRIC_PRESSURE": [80.0] * 2,
            "obd:INTAKE_TEMP": [20.0, float("nan")],
        }
    )
    out = compute_anomaly_signals(frame, vehicle)
    assert out[f"{VE_AMBIENT_CHANNEL}__gap"].tolist() == [False, True]
    assert math.isnan(out[VE_AMBIENT_CHANNEL].iloc[1])


# --------------------------------------------------------------------------- #
# STFT / LTFT bank balance
# --------------------------------------------------------------------------- #


def test_stft_bank_balance_is_time_smoothed() -> None:
    """STFT bank balance is a trailing time-mean of the raw delta, not the raw
    delta itself — fast-channel sampling-skew transients are averaged out."""
    frame = _base(3).assign(
        **{
            "obd:SHORT_FUEL_TRIM_1": [0.0, 2.0, -3.0],
            "obd:SHORT_FUEL_TRIM_2": [0.0, 0.0, 1.0],
        }
    )
    # raw delta = [0, 2, -4]; 30 s window @ 1 Hz, min_periods=1 → running mean.
    out = compute_anomaly_signals(frame)
    smoothed = out["derived:STFT_BANK_BALANCE"].to_list()
    assert math.isclose(smoothed[0], 0.0, abs_tol=1e-9)
    assert math.isclose(smoothed[1], 1.0, abs_tol=1e-9)  # mean(0, 2)
    assert math.isclose(smoothed[2], -2.0 / 3.0, abs_tol=1e-9)  # mean(0, 2, -4)


def test_stft_bank_balance_attenuates_transient_keeps_sustained() -> None:
    """A one-sample spike is damped below the recipe's 6% band; a sustained
    offset converges to its true value — the whole point of smoothing."""
    rows = 60
    # Bank 2 flat at 0; bank 1 spikes to +8 for a single sample then a
    # sustained +5 offset for the rest.
    b1 = [0.0] + [8.0] + [5.0] * (rows - 2)
    frame = _base(rows).assign(
        **{
            "obd:SHORT_FUEL_TRIM_1": b1,
            "obd:SHORT_FUEL_TRIM_2": [0.0] * rows,
        }
    )
    out = compute_anomaly_signals(frame)["derived:STFT_BANK_BALANCE"]
    assert out.iloc[1] < 6.0  # the lone +8 spike is averaged down, doesn't trip
    assert math.isclose(out.iloc[-1], 5.0, abs_tol=0.5)  # sustained offset survives


def test_stft_bank_balance_does_not_bleed_across_sessions() -> None:
    """Each session's smoothing window is independent — session B's first row
    sees only itself, not session A's tail."""
    frame = pd.DataFrame(
        {
            "session": ["a", "a", "b", "b"],
            "timestamp": pd.to_datetime(
                [
                    "2026-05-20T12:00:00Z",
                    "2026-05-20T12:00:01Z",
                    "2026-05-20T12:00:02Z",
                    "2026-05-20T12:00:03Z",
                ]
            ),
            "obd:SHORT_FUEL_TRIM_1": [10.0, 10.0, 0.0, 0.0],
            "obd:SHORT_FUEL_TRIM_2": [0.0, 0.0, 0.0, 0.0],
        }
    )
    out = compute_anomaly_signals(frame)["derived:STFT_BANK_BALANCE"].to_list()
    assert math.isclose(out[2], 0.0, abs_tol=1e-9)  # session b row 0 ≠ session a's 10


def test_stft_bank_balance_falls_back_to_raw_without_session_columns() -> None:
    """A bare frame (no session/timestamp) can't be windowed — pass the raw
    delta through rather than erroring, for trivial unit-test callers."""
    frame = pd.DataFrame(
        {
            "obd:SHORT_FUEL_TRIM_1": [0.0, 2.0, -3.0],
            "obd:SHORT_FUEL_TRIM_2": [0.0, 0.0, 1.0],
        }
    )
    out = compute_anomaly_signals(frame)
    assert out["derived:STFT_BANK_BALANCE"].to_list() == [0.0, 2.0, -4.0]


def test_ltft_bank_balance_is_not_smoothed() -> None:
    """LTFT is the slow / drift channel by design — left raw, no smoothing."""
    frame = _base(3).assign(
        **{
            "obd:LONG_FUEL_TRIM_1": [1.0, 2.0, 5.0],
            "obd:LONG_FUEL_TRIM_2": [1.0, 0.0, 0.0],
        }
    )
    out = compute_anomaly_signals(frame)
    assert out["derived:LTFT_BANK_BALANCE"].to_list() == [0.0, 2.0, 5.0]


def test_bank_balance_skipped_when_only_one_bank_present() -> None:
    frame = _base(3).assign(**{"obd:SHORT_FUEL_TRIM_1": [0.0, 1.0, 2.0]})
    out = compute_anomaly_signals(frame)
    assert "derived:STFT_BANK_BALANCE" not in out.columns


# --------------------------------------------------------------------------- #
# TOTAL_TRIM_B1/B2 (Task 4.3) — LTFT + STFT per bank.
# --------------------------------------------------------------------------- #


def test_total_trim_sums_stft_and_ltft_per_bank() -> None:
    frame = _base(3).assign(
        **{
            "obd:SHORT_FUEL_TRIM_1": [2.0] * 3,
            "obd:LONG_FUEL_TRIM_1": [5.0] * 3,
            "obd:SHORT_FUEL_TRIM_2": [-1.0] * 3,
            "obd:LONG_FUEL_TRIM_2": [3.0] * 3,
        }
    )
    out = compute_anomaly_signals(frame)
    assert out["derived:TOTAL_TRIM_B1"].to_list() == [7.0, 7.0, 7.0]
    assert out["derived:TOTAL_TRIM_B2"].to_list() == [2.0, 2.0, 2.0]


def test_total_trim_skipped_when_one_channel_missing() -> None:
    frame = _base(3).assign(**{"obd:SHORT_FUEL_TRIM_1": [2.0] * 3})  # no LTFT_1
    out = compute_anomaly_signals(frame)
    assert "derived:TOTAL_TRIM_B1" not in out.columns
    assert "derived:TOTAL_TRIM_B2" not in out.columns


# --------------------------------------------------------------------------- #
# CAT_EFFICIENCY_B1/B2 (Task 4.4) — upstream vs downstream O2 switch-count
# ratio, a bounded 0-1 heuristic index (NOT a literal efficiency).
# --------------------------------------------------------------------------- #


def test_cat_efficiency_separates_damped_from_undamped_downstream() -> None:
    """Bank 1's downstream dwells slowly (20-high/5-low, a damping/healthy
    cat); bank 2's downstream mirrors the fast upstream toggle exactly (no
    damping at all, a degraded cat) — the index must rank them accordingly
    and stay within [0, 1]."""
    rows = 90
    upstream = [0.8 if i % 2 == 0 else 0.1 for i in range(rows)]
    downstream_healthy = [0.8 if (i % 25) < 20 else 0.1 for i in range(rows)]
    frame = _base(rows).assign(
        **{
            "obd:O2_B1S1": upstream,
            "obd:O2_B2S1": upstream,
            "obd:O2_B1S2": downstream_healthy,
            "obd:O2_B2S2": upstream,  # mirrors upstream exactly
        }
    )
    out = compute_anomaly_signals(frame)
    b1 = out["derived:CAT_EFFICIENCY_B1"].iloc[-1]
    b2 = out["derived:CAT_EFFICIENCY_B2"].iloc[-1]
    assert 0.0 <= b1 <= 1.0
    assert 0.0 <= b2 <= 1.0
    assert b1 > 0.5
    assert b2 < 0.3
    assert b1 > b2


def test_cat_efficiency_skipped_when_downstream_missing() -> None:
    frame = _base(5).assign(**{"obd:O2_B1S1": [0.8, 0.1, 0.8, 0.1, 0.8]})
    out = compute_anomaly_signals(frame)
    assert "derived:CAT_EFFICIENCY_B1" not in out.columns
    assert "derived:CAT_EFFICIENCY_B2" not in out.columns


# --------------------------------------------------------------------------- #
# AFR bank balance (DID-dependent)
# --------------------------------------------------------------------------- #


def test_afr_bank_balance_emitted_when_did_pair_present() -> None:
    frame = _base(3).assign(
        **{
            "did:ECM_AF_SENSOR_V_B1": [2.15, 2.20, 1.80],
            "did:ECM_AF_SENSOR_V_B2": [2.15, 2.15, 2.15],
        }
    )
    out = compute_anomaly_signals(frame)
    assert "derived:AFR_BANK_BALANCE" in out.columns
    values = out["derived:AFR_BANK_BALANCE"].to_list()
    assert math.isclose(values[0], 0.0, abs_tol=1e-9)
    assert math.isclose(values[1], 0.05, abs_tol=1e-9)
    assert math.isclose(values[2], -0.35, abs_tol=1e-9)


def test_afr_bank_balance_skipped_when_dids_absent() -> None:
    """Until the DIDs are wired into ``obd_dids``, recipe must not see this column."""
    frame = _base(3)
    out = compute_anomaly_signals(frame)
    assert "derived:AFR_BANK_BALANCE" not in out.columns


# --------------------------------------------------------------------------- #
# TPS voltage / pct ratio (DID-dependent)
# --------------------------------------------------------------------------- #


def test_tps_voltage_per_pct_emitted_when_inputs_present() -> None:
    frame = _base(3).assign(
        **{
            "did:ECM_THROTTLE_POS_SENSOR_1": [0.65, 0.90, 2.20],
            "obd:THROTTLE_POS": [10.0, 25.0, 80.0],
        }
    )
    out = compute_anomaly_signals(frame)
    values = out["derived:TPS_VOLTAGE_PER_PCT"].to_list()
    assert math.isclose(values[0], 0.065, abs_tol=1e-9)
    assert math.isclose(values[1], 0.036, abs_tol=1e-9)
    assert math.isclose(values[2], 0.0275, abs_tol=1e-9)


def test_tps_voltage_per_pct_masks_closed_throttle() -> None:
    """At closed throttle (pct ≈ 0) the ratio is ill-defined; NaN it out."""
    frame = _base(2).assign(
        **{
            "did:ECM_THROTTLE_POS_SENSOR_1": [0.5, 0.65],
            "obd:THROTTLE_POS": [0.0, 10.0],
        }
    )
    out = compute_anomaly_signals(frame)
    values = out["derived:TPS_VOLTAGE_PER_PCT"]
    assert pd.isna(values.iloc[0])
    assert not pd.isna(values.iloc[1])


# --------------------------------------------------------------------------- #
# Composability / empty-frame contract
# --------------------------------------------------------------------------- #


def test_empty_frame_returns_empty() -> None:
    out = compute_anomaly_signals(pd.DataFrame())
    assert out.empty


def test_no_inputs_returns_passthrough_with_no_derived_columns() -> None:
    frame = _base(3)
    out = compute_anomaly_signals(frame)
    assert [c for c in out.columns if c.startswith("derived:")] == []
