"""Tests for :func:`compute_anomaly_signals`.

The derived signals power the new fuel/air behavior recipes (Phase 1: no
DIDs needed; Phase 2: requires DID polling). This test set pins both
the "inputs present" emit and the "inputs absent" no-op contract — each
new derived column must not appear at all when its inputs are missing,
so the recipe layer skips cleanly rather than fitting to NaN.
"""

from __future__ import annotations

import math

import pandas as pd

from src.analysis.derived import compute_anomaly_signals


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
