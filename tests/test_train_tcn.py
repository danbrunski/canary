"""Tests for the TCN autoencoder training CLI."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.train_tcn import (
    _resolve_training_sessions,
    build_dataset,
    evaluate,
    main,
    train,
)
from src.config import CanaryConfig
from src.models.false_positive_benchmark import FalsePositiveBenchmarkReport
from src.models.tcn_training import load_tcn_autoencoder_model
from src.yaml_load import load_yaml
from tests.builders import write_obd_session
from tests.configs import example_config

# Torch training: not a commit gate (see the pyproject test tiers).
pytestmark = [pytest.mark.slow, pytest.mark.ml_training]

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"

_TINY_TCN = {
    "enabled": True,
    "sequence_length": 6,
    "channels": 8,
    "kernel_size": 2,
    "num_levels": 2,
    "latent_dim": 2,
    "dropout": 0.0,
    "use_delta_decay": True,
    "batch_size": 8,
    "learning_rate": 0.01,
    "max_epochs": 2,
    "validation_fraction": 0.0,
    "early_stopping_patience": 2,
    "min_delta": 0.0,
    "random_state": 7,
    "threshold_k": 8.0,
}


def _torch():
    return pytest.importorskip("torch")


def _write_config(tmp_path: Path, **tcn_overrides: object) -> Path:
    data = load_yaml(EXAMPLE_YAML.read_text())
    data["models"]["tcn_autoencoder"].update(_TINY_TCN)
    data["models"]["tcn_autoencoder"].update(tcn_overrides)
    data["models"]["sequence_detector"] = "tcn"
    path = tmp_path / "canary.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _config(tmp_path: Path, **tcn_overrides: object) -> CanaryConfig:
    return example_config(_write_config(tmp_path, **tcn_overrides))


def _no_bindings(tmp_path: Path) -> Path:
    return tmp_path / "no-bindings.yaml"


def test_build_dataset_uses_tcn_sequence_length(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)

    dataset = build_dataset([session], config, bindings_path=_no_bindings(tmp_path))

    assert len(dataset) > 0
    assert dataset.sequence_length == _TINY_TCN["sequence_length"]
    assert "obd:RPM" in dataset.feature_columns


def test_train_writes_checkpoint_and_manifest(tmp_path: Path) -> None:
    _torch()
    config = _config(tmp_path)
    session = write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    out_dir = tmp_path / "models" / "tcn"

    trained = train([session], config, out_dir, bindings_path=_no_bindings(tmp_path))

    artifact = out_dir / "model.pt"
    manifest = out_dir / "model.manifest.json"
    assert artifact.exists()
    assert manifest.exists()
    assert trained.manifest.thresholds is not None
    assert trained.manifest.input_windows > 0

    reloaded = load_tcn_autoencoder_model(artifact)
    assert reloaded.feature_columns == trained.feature_columns
    payload = json.loads(manifest.read_text())
    assert payload["model_type"] == "tcn_autoencoder"
    assert payload["architecture"]["kernel_size"] == _TINY_TCN["kernel_size"]
    assert payload["thresholds"]["threshold_k"] == _TINY_TCN["threshold_k"]


def test_evaluate_runs_false_positive_benchmark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _torch()
    config = _config(tmp_path)
    train_session = write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    held_out = write_obd_session(tmp_path, "holdout", rows=120, start_epoch=1_778_600_000.0)
    trained = train([train_session], config, tmp_path / "m", bindings_path=_no_bindings(tmp_path))

    report = evaluate(trained, [held_out], config, bindings_path=_no_bindings(tmp_path))

    assert isinstance(report, FalsePositiveBenchmarkReport)
    assert report.window_count > 0
    assert report.evaluated_sessions == ("holdout",)
    out = capsys.readouterr().out
    assert "FALSE-POSITIVE BENCHMARK" in out
    assert "k sweep" in out


def test_main_trains_and_evaluates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _torch()
    config_path = _write_config(tmp_path)
    train_session = write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    held_out = write_obd_session(tmp_path, "holdout", rows=120, start_epoch=1_778_600_000.0)
    out_dir = tmp_path / "models" / "tcn"

    exit_code = main(
        [
            str(train_session),
            "--config",
            str(config_path),
            "--bindings",
            str(_no_bindings(tmp_path)),
            "--out",
            str(out_dir),
            "--evaluate",
            str(held_out),
        ]
    )

    assert exit_code == 0
    assert (out_dir / "model.pt").exists()
    out = capsys.readouterr().out
    assert "Trained TCN autoencoder saved to" in out
    assert "FALSE-POSITIVE BENCHMARK" in out


def test_main_errors_when_no_sessions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = _write_config(tmp_path)
    exit_code = main(["--sessions-root", str(tmp_path / "empty"), "--config", str(config_path)])
    assert exit_code == 1
    assert "no sessions" in capsys.readouterr().err


def test_resolve_training_sessions_excludes_evaluate_sessions(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    write_obd_session(root, "a", rows=10, start_epoch=1_778_500_000.0)
    held_out = write_obd_session(root, "b", rows=10, start_epoch=1_778_600_000.0)

    resolved = _resolve_training_sessions(
        SimpleNamespace(
            sessions=[], sessions_root=root, evaluate=[held_out], calibration_sessions=None
        )
    )

    assert resolved is not None
    assert [Path(p).name for p in resolved] == ["a"]


def test_resolve_training_sessions_excludes_calibration_sessions(tmp_path: Path) -> None:
    """Out-of-sample calibration: --calibration-sessions are held out from the
    training set too, alongside --evaluate, so the model never trains on data
    its thresholds will be calibrated against."""
    root = tmp_path / "sessions"
    root.mkdir()
    write_obd_session(root, "a", rows=10, start_epoch=1_778_500_000.0)
    calib = write_obd_session(root, "b", rows=10, start_epoch=1_778_600_000.0)
    held_out = write_obd_session(root, "c", rows=10, start_epoch=1_778_700_000.0)

    resolved = _resolve_training_sessions(
        SimpleNamespace(
            sessions=[],
            sessions_root=root,
            evaluate=[held_out],
            calibration_sessions=[calib],
        )
    )

    assert resolved is not None
    assert [Path(p).name for p in resolved] == ["a"]
