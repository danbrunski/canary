"""R05.4 — the anomaly signals are declared once, as data.

``compute_anomaly_signals`` used to restate each signal's inputs inline, and
``DERIVED_CHANNEL_INPUTS`` declared only four of them, so
``capture_profiles._channel_satisfied`` treated the unmapped rest as satisfied
and ``model_input_warnings`` could never warn for the bank-balance and
total-trim models. The table is now the single declaration; the dependency
registry, the freshness gaps and the computation all read it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.analysis.derived import (
    ANOMALY_SIGNALS,
    DERIVED_CHANNEL_INPUTS,
    compute_anomaly_signals,
)
from src.collection.capture_profiles import CaptureProfile, model_input_warnings
from src.config import VehicleParameters
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column

BEHAVIOR_MODELS = Path("data/nissan-xterra-2008/behavior_models.yaml")


def _every_input_frame(n: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    frame = pd.DataFrame(
        {
            SESSION_COLUMN: ["2026-01-01T000000"] * n,
            TIMESTAMP_COLUMN: pd.date_range("2026-01-01", periods=n, freq="1s", tz="UTC"),
        }
    )
    columns = {
        column
        for signal in ANOMALY_SIGNALS
        for group in signal.input_groups
        for column in group
        if not column.startswith(("derived:", "decoded:"))
    }
    for column in sorted(columns):
        frame[column] = rng.uniform(1.0, 100.0, n)
    return frame


def test_the_registry_is_derived_from_the_table() -> None:
    for signal in ANOMALY_SIGNALS:
        assert DERIVED_CHANNEL_INPUTS[signal.name] == signal.input_groups
    assert len({signal.name for signal in ANOMALY_SIGNALS}) == len(ANOMALY_SIGNALS)


def test_every_declared_signal_is_produced_with_its_gap_in_table_order() -> None:
    frame = _every_input_frame()
    out = compute_anomaly_signals(frame, VehicleParameters())
    produced = [c for c in out.columns if c.startswith("derived:") and "__" not in c]
    assert produced == [signal.name for signal in ANOMALY_SIGNALS]
    for signal in ANOMALY_SIGNALS:
        assert gap_column(signal.name) in out.columns


def test_a_signal_missing_an_input_is_skipped_not_nan_filled() -> None:
    frame = _every_input_frame().drop(columns=["obd:SHORT_FUEL_TRIM_2"])
    out = compute_anomaly_signals(frame, VehicleParameters())
    assert "derived:STFT_BANK_BALANCE" not in out.columns
    assert "derived:TOTAL_TRIM_B2" not in out.columns
    assert "derived:TOTAL_TRIM_B1" in out.columns


@pytest.mark.skipif(not BEHAVIOR_MODELS.exists(), reason="vehicle behavior models absent")
def test_a_profile_missing_the_stft_inputs_warns_for_the_bank_balance_models() -> None:
    polls_everything_but_stft = CaptureProfile(
        name="default",
        mode01={
            "RPM": 1.0,
            "ENGINE_LOAD": 1.0,
            "MAF": 1.0,
            "SPEED": 1.0,
            "COOLANT_TEMP": 1.0,
            "RUN_TIME": 1.0,
            "FUEL_STATUS": 1.0,
            "LONG_FUEL_TRIM_1": 1.0,
            "LONG_FUEL_TRIM_2": 1.0,
        },
        corpus_eligible=True,
        broadcast_decode=True,
    )
    warnings = {
        w["model"]: w["missing_channels"]
        for w in model_input_warnings(polls_everything_but_stft, BEHAVIOR_MODELS)
    }
    stft_models = [name for name in warnings if name.startswith("stft_bank_balance")]
    assert stft_models, warnings
    for name in stft_models:
        assert "derived:STFT_BANK_BALANCE" in warnings[name]
    # LTFT is polled, so its balance models are not starved by it.
    for name, missing in warnings.items():
        assert "derived:LTFT_BANK_BALANCE" not in missing, name
