"""Pulsation and gauge-step analysis (src.analysis.analog_pulsation) against
synthetic captures with known answers: engine-order lines are reported as the
same order in every band; a gauge output held between updates at 100 Hz is
reported as steps at 100 Hz; a smooth output is not; and the two empty cases
say which kind of empty they are."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from src.analysis.analog_pulsation import (
    NOMINAL_RATE_HZ,
    PulsationRefusedError,
    analyse,
    full_rate_series,
)

FS = NOMINAL_RATE_HZ
T0 = pd.Timestamp("2026-10-04T20:00:00Z")


def _capture(
    rpms: list[float], seconds_each: float = 20.0, update_hz: float | None = None, seed: int = 1
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pressure with lines at orders 1.5 and 3, sampled at FS; optionally held
    between gauge updates at ``update_hz`` (a staircase)."""
    rng = np.random.default_rng(seed)
    n_each = int(seconds_each * FS)
    t = np.arange(n_each * len(rpms)) / FS
    rpm = np.repeat(rpms, n_each).astype(float)
    phase = 2 * np.pi * np.cumsum(rpm / 60.0) / FS
    smooth = 350.0 + 1.0 * np.sin(1.5 * phase) + 0.5 * np.sin(3.0 * phase)
    if update_hz is not None:
        held_at = np.floor(t * update_hz) / update_hz
        smooth = np.interp(held_at, t, smooth)
    values = smooth + rng.normal(0, 0.05, len(t))
    stamps = T0 + pd.to_timedelta(t, unit="s")
    pressure = pd.DataFrame(
        {
            "source": "sensor",
            "channel": "FUEL_PRESSURE",
            "value": values,
            "timestamp": stamps,
            "metadata_json": [json.dumps({"adc_seq": int(n)}) for n in range(len(t))],
        }
    )
    obd_t = np.arange(0, t[-1], 0.1)
    rpm_rows = pd.DataFrame(
        {
            "timestamp": T0 + pd.to_timedelta(obd_t, unit="s"),
            "value": np.interp(obd_t, t, rpm),
        }
    )
    return pressure, rpm_rows


def test_engine_orders_are_reported_as_the_same_order_in_every_band() -> None:
    pressure, rpm = _capture([1200, 2000, 2800])
    report = analyse(full_rate_series(pressure), rpm)
    assert [round(b.rpm) for b in report.bands] == [1200, 2000, 2800]
    for band in report.bands:
        orders = sorted(round(p.order * 2) / 2 for p in band.pressure_peaks[:2])
        assert orders == [1.5, 3.0]
        strongest = band.pressure_peaks[0]
        assert strongest.rms == pytest.approx(1.0 / np.sqrt(2), rel=0.15)
    assert report.step_lines == []
    assert report.step_verdict.startswith("no steps resolved across 3 bands")


def test_a_held_output_is_reported_as_steps_at_its_update_rate() -> None:
    pressure, rpm = _capture([1200, 2000, 2800], update_hz=100.0)
    report = analyse(full_rate_series(pressure), rpm)
    assert report.step_verdict.startswith("steps:")
    assert any(abs(hz - 100.0) <= 1.5 for hz, _ in report.step_lines)


@pytest.mark.parametrize("update_hz", [60.0, 150.0])
def test_steps_survive_the_chains_low_pass_and_lead_the_verdict(update_hz: float) -> None:
    from scipy import signal  # type: ignore[import-untyped]

    pressure, rpm = _capture([1150, 1950, 2850], update_hz=update_hz)
    b, a = signal.butter(6, 180, fs=FS)  # stands in for the RC + FIR passband edge
    pressure["value"] = signal.lfilter(b, a, pressure["value"] - 350) + 350
    report = analyse(full_rate_series(pressure), rpm)
    assert report.step_lines[0][0] == pytest.approx(update_hz, abs=1.5)
    assert report.step_verdict.startswith(f"steps: the change energy peaks at {update_hz:g}")


def test_one_band_cannot_separate_fixed_from_order_lines() -> None:
    pressure, rpm = _capture([2000], update_hz=100.0)
    report = analyse(full_rate_series(pressure), rpm)
    assert report.step_verdict.startswith("no answer: steady rpm in one band only")


def test_no_steady_rpm_is_its_own_answer() -> None:
    pressure, rpm = _capture([2000])
    rpm["value"] = np.linspace(1000, 4000, len(rpm))  # a sweep, never steady
    report = analyse(full_rate_series(pressure), rpm)
    assert report.bands == []
    assert report.step_verdict.startswith("no answer: no window of steady rpm")


def test_means_and_v1_sessions_are_refused_with_the_reason() -> None:
    pressure, _ = _capture([2000], seconds_each=1.0)
    means = pressure.assign(metadata_json=json.dumps({"adc_seq": 0, "adc_samples": 5}))
    with pytest.raises(PulsationRefusedError, match="fuel-pulsation profile"):
        full_rate_series(means)
    v1 = pressure.assign(metadata_json=json.dumps({"adc_counts": 2114}))
    with pytest.raises(PulsationRefusedError, match="v1 firmware"):
        full_rate_series(v1)
