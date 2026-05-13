"""Tests for streaming LSTM inference helpers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from src.config import CanaryConfig
from src.models.inference import LSTMInferenceEngine, LSTMSequenceBuffer
from src.models.lstm_training import train_lstm_autoencoder
from src.models.sequence_dataset import SequenceWindowDataset
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _torch():
    return pytest.importorskip("torch")


def _config(**updates):
    base = CanaryConfig.from_yaml(EXAMPLE_YAML).models.lstm_autoencoder
    defaults = {
        "sequence_length": 4,
        "hidden_size": 6,
        "num_layers": 1,
        "latent_dim": 2,
        "batch_size": 4,
        "learning_rate": 0.01,
        "max_epochs": 1,
        "validation_fraction": 0.0,
        "early_stopping_patience": 2,
        "random_state": 9,
        "threshold_k": 100.0,
    }
    defaults.update(updates)
    return base.model_copy(update=defaults)


def _frame(rows: int = 12) -> pd.DataFrame:
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["s1"] * rows,
            TIMESTAMP_COLUMN: pd.date_range("2026-05-12T00:00:00Z", periods=rows, freq="s"),
            "rpm": [700.0 + (idx % 3) for idx in range(rows)],
            "speed": [float(idx % 4) for idx in range(rows)],
        }
    )


def _dataset(sequence_length: int = 4) -> SequenceWindowDataset:
    return SequenceWindowDataset(
        _frame(),
        sequence_length=sequence_length,
        feature_columns=["rpm", "speed"],
    )


def _trained(**config_updates):
    _torch()
    dataset = _dataset()
    return train_lstm_autoencoder(
        dataset,
        _config(**config_updates),
        trained_at="2026-05-12T12:00:00Z",
        model_version="inference-test",
    )


def test_sequence_buffer_scores_only_after_full_window() -> None:
    _torch()
    engine = LSTMInferenceEngine(_trained(), time_fn=_fake_clock())
    frame = _frame()

    assert engine.observe(frame.iloc[0]) is None
    assert engine.observe(frame.iloc[1]) is None
    assert engine.observe(frame.iloc[2]) is None
    result = engine.observe(frame.iloc[3])

    assert result is not None
    assert result.model_version == "inference-test"
    assert result.session == "s1"
    assert result.window_start_timestamp == "2026-05-12T00:00:00Z"
    assert result.window_end_timestamp == "2026-05-12T00:00:03Z"
    assert set(result.channel_scores) == {"rpm", "speed"}
    assert set(result.channel_thresholds) == {"rpm", "speed"}
    assert result.latency_ms == pytest.approx(100.0)


def test_inference_result_to_dict_is_json_safe() -> None:
    engine = LSTMInferenceEngine(_trained(), time_fn=_fake_clock())
    result = None
    for _, row in _frame().iloc[:4].iterrows():
        result = engine.observe(row)

    assert result is not None
    payload = result.to_dict()
    assert payload["model_version"] == "inference-test"
    assert isinstance(payload["channel_scores"], dict)
    assert isinstance(payload["anomalous_channels"], list)


def test_inference_flags_anomaly_against_low_thresholds() -> None:
    trained = _trained(threshold_k=100.0)
    thresholds = dict(trained.manifest.thresholds or {})
    thresholds["global_error"] = dict(thresholds["global_error"])
    thresholds["global_error"]["threshold"] = 0.0
    thresholds["channel_errors"] = {
        channel: {**distribution, "threshold": 0.0}
        for channel, distribution in dict(thresholds["channel_errors"]).items()
    }
    trained = replace(trained, manifest=replace(trained.manifest, thresholds=thresholds))
    engine = LSTMInferenceEngine(trained, time_fn=_fake_clock())

    result = None
    for _, row in _frame().iloc[:4].iterrows():
        result = engine.observe(row)

    assert result is not None
    assert result.is_anomaly is True
    assert result.global_score > result.global_threshold
    assert result.anomalous_channels


def test_inference_engine_requires_calibrated_thresholds() -> None:
    _torch()  # skip cleanly when torch is unavailable (CI without the `ml` extra)
    trained = train_lstm_autoencoder(
        _dataset(),
        _config(),
        trained_at="2026-05-12T12:00:00Z",
        calibrate_thresholds=False,
    )

    with pytest.raises(ValueError, match="calibrated thresholds"):
        LSTMInferenceEngine(trained)


def test_sequence_buffer_resets_on_missing_or_non_finite_features() -> None:
    buffer = LSTMSequenceBuffer(feature_columns=("rpm", "speed"), sequence_length=2)

    incomplete = {
        SESSION_COLUMN: "s1",
        TIMESTAMP_COLUMN: "2026-05-12T00:00:00Z",
        "rpm": 1.0,
    }
    assert buffer.append(incomplete) is False
    assert buffer.ready is False
    assert (
        buffer.append(
            {
                SESSION_COLUMN: "s1",
                TIMESTAMP_COLUMN: "2026-05-12T00:00:00Z",
                "rpm": 1.0,
                "speed": float("nan"),
            }
        )
        is False
    )
    assert buffer.ready is False


def test_sequence_buffer_resets_on_session_change() -> None:
    buffer = LSTMSequenceBuffer(feature_columns=("rpm", "speed"), sequence_length=2)
    row = {
        TIMESTAMP_COLUMN: "2026-05-12T00:00:00Z",
        "rpm": 1.0,
        "speed": 2.0,
    }

    assert buffer.append({**row, SESSION_COLUMN: "s1"}) is False
    assert buffer.append({**row, SESSION_COLUMN: "s2"}) is False
    assert buffer.append({**row, SESSION_COLUMN: "s2"}) is True
    start, end, session = buffer.metadata()
    assert start == "2026-05-12T00:00:00Z"
    assert end == "2026-05-12T00:00:00Z"
    assert session == "s2"


def test_score_window_validates_shape() -> None:
    import numpy as np

    engine = LSTMInferenceEngine(_trained())

    with pytest.raises(ValueError, match="window shape"):
        engine.score_window(
            np.zeros((3, 2), dtype="float32"),
            timestamp="t",
            session="s",
            window_start_timestamp="t0",
            window_end_timestamp="t1",
        )


def _fake_clock():
    values = iter([1.0, 1.1, 2.0, 2.1, 3.0, 3.1])
    return lambda: next(values)
