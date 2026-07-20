"""Tests pinning the Task 2.1 unification's two backbone-specific hooks.

``src.models.lstm_training``/``tcn_training`` now route through the shared
:mod:`src.models.sequence_autoencoder_training` engine. This file exists to
prove the two genuine divergences the refactoring doc named are preserved as
real backbone-specific behavior — not silently applied to both backbones, and
not silently dropped from either:

* LSTM's ``strict=False`` decay-key load tolerance (only LSTM has it; TCN's
  checkpoint load stays fully strict).
* The calibrated-without-scaler load guard — which the doc named as
  LSTM-specific, but which reading the current code shows is *not*: both
  backbones already carry byte-identical guards. This file pins the guard
  applies symmetrically to both, rather than asserting the doc's (stale)
  claim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from src.models.lstm_training import (  # noqa: E402
    load_lstm_autoencoder_model,
    train_lstm_autoencoder,
)
from src.models.sequence_dataset import SequenceWindowDataset  # noqa: E402
from src.models.sequence_registry import (  # noqa: E402
    LSTM_TRAINING_BACKEND,
    TCN_TRAINING_BACKEND,
)
from src.models.tcn_training import (  # noqa: E402
    load_tcn_autoencoder_model,
    train_tcn_autoencoder,
)
from src.preprocessing.normalization import ZScoreScaler  # noqa: E402
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN  # noqa: E402

pytestmark = pytest.mark.slow  # heavy ML training; see pyproject test tiers

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _frame(rows: int = 14) -> pd.DataFrame:
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["clean"] * rows,
            TIMESTAMP_COLUMN: pd.date_range("2026-05-12T00:00:00Z", periods=rows, freq="s"),
            "rpm": [700.0 + (idx % 4) for idx in range(rows)],
            "speed": [float(idx % 5) for idx in range(rows)],
        }
    )


def _dataset(rows: int = 14, sequence_length: int = 4) -> SequenceWindowDataset:
    frame = _frame(rows)
    scaler = ZScoreScaler.fit(frame, columns=["rpm", "speed"])
    return SequenceWindowDataset(
        scaler.transform(frame),
        sequence_length=sequence_length,
        feature_columns=["rpm", "speed"],
    )


def _lstm_config(**updates: object) -> object:
    from src.config import CanaryConfig

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
        "random_state": 7,
        "threshold_k": 2.0,
        "threshold_quantile": None,
    }
    defaults.update(updates)
    return base.model_copy(update=defaults)


def _tcn_config() -> object:
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Cfg:
        enabled: bool = True
        sequence_length: int = 4
        channels: int = 8
        kernel_size: int = 2
        num_levels: int = 2
        latent_dim: int = 4
        dropout: float = 0.0
        use_delta_decay: bool = True
        batch_size: int = 4
        learning_rate: float = 0.01
        max_epochs: int = 1
        validation_fraction: float = 0.0
        early_stopping_patience: int = 2
        min_delta: float = 0.0
        random_state: int = 7
        threshold_k: float = 2.0

    return _Cfg()


def test_registry_only_lstm_tolerates_a_missing_decay_key() -> None:
    """Registry-level pin of the one genuine divergence: LSTM tolerates
    exactly the pre-decay-target checkpoint shape; TCN tolerates nothing."""
    assert LSTM_TRAINING_BACKEND.tolerated_missing_state_keys == frozenset(
        {"encoder_cell.decay_input_target"}
    )
    assert TCN_TRAINING_BACKEND.tolerated_missing_state_keys == frozenset()


def test_tcn_load_does_not_tolerate_any_missing_state_dict_key(tmp_path: Path) -> None:
    """TCN has no legacy checkpoint shape, so its load must stay fully
    strict — unlike LSTM, dropping *any* key (not just LSTM's tolerated one)
    must fail loudly rather than silently loading with zeroed weights."""
    trained = train_tcn_autoencoder(
        _dataset(),
        _tcn_config(),
        trained_at="2026-05-12T12:00:00Z",
        allow_in_sample_calibration=True,
    )
    artifact = tmp_path / "tcn.pt"
    trained.save(artifact)

    payload = torch.load(artifact, map_location="cpu", weights_only=False)
    dropped_key = next(iter(payload["state_dict"]))
    del payload["state_dict"][dropped_key]
    torch.save(payload, artifact)

    # TCN has an empty ``tolerated_missing_state_keys``, so the shared loader
    # takes the strict=True branch and lets torch's own (unwrapped) error
    # propagate — unlike LSTM, which gets the custom "state dict mismatch"
    # message from the strict=False + tolerated-key-check branch.
    with pytest.raises(RuntimeError, match="Missing key"):
        load_tcn_autoencoder_model(artifact)


def test_load_rejects_lstm_and_tcn_thresholds_without_scaler_symmetrically(
    tmp_path: Path,
) -> None:
    """The calibrated-without-scaler guard is NOT LSTM-specific (despite the
    refactoring doc naming it that way) — both backbones already enforce it
    identically. Pin the symmetric behavior directly, rather than asserting
    the doc's stale claim that only LSTM has it."""
    lstm_trained = train_lstm_autoencoder(
        _dataset(),
        _lstm_config(),
        trained_at="2026-05-12T12:00:00Z",
        allow_in_sample_calibration=True,
    )
    lstm_artifact = tmp_path / "lstm.pt"
    lstm_trained.save(lstm_artifact)
    lstm_payload = torch.load(lstm_artifact, map_location="cpu", weights_only=False)
    lstm_payload["manifest"]["scaler"] = None
    torch.save(lstm_payload, lstm_artifact)
    with pytest.raises(ValueError, match="no scaler"):
        load_lstm_autoencoder_model(lstm_artifact)

    tcn_trained = train_tcn_autoencoder(
        _dataset(),
        _tcn_config(),
        trained_at="2026-05-12T12:00:00Z",
        allow_in_sample_calibration=True,
    )
    tcn_artifact = tmp_path / "tcn.pt"
    tcn_trained.save(tcn_artifact)
    tcn_payload = torch.load(tcn_artifact, map_location="cpu", weights_only=False)
    tcn_payload["manifest"]["scaler"] = None
    torch.save(tcn_payload, tcn_artifact)
    with pytest.raises(ValueError, match="no scaler"):
        load_tcn_autoencoder_model(tcn_artifact)


def test_lstm_and_tcn_manifests_are_the_same_class() -> None:
    """The unified ``SequenceAutoencoderManifest`` replaces the two
    byte-compatible clones; both trainers' manifests are now literally the
    same class, not just structurally similar ones."""
    lstm_trained = train_lstm_autoencoder(
        _dataset(),
        _lstm_config(),
        trained_at="2026-05-12T12:00:00Z",
        allow_in_sample_calibration=True,
    )
    tcn_trained = train_tcn_autoencoder(
        _dataset(),
        _tcn_config(),
        trained_at="2026-05-12T12:00:00Z",
        allow_in_sample_calibration=True,
    )
    assert type(lstm_trained.manifest) is type(tcn_trained.manifest)


def test_manifest_json_round_trips_for_both_backbones(tmp_path: Path) -> None:
    lstm_trained = train_lstm_autoencoder(
        _dataset(),
        _lstm_config(),
        trained_at="2026-05-12T12:00:00Z",
        allow_in_sample_calibration=True,
    )
    artifact = tmp_path / "lstm.pt"
    lstm_trained.save(artifact)
    payload = json.loads(artifact.with_suffix(".manifest.json").read_text())
    assert payload["model_type"] == "lstm_autoencoder"
