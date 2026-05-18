"""Tests for the LSTM autoencoder training CLI."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.train_lstm import (
    _resolve_training_sessions,
    build_dataset,
    evaluate,
    main,
    train,
)
from src.config import CanaryConfig
from src.models.false_positive_benchmark import FalsePositiveBenchmarkReport
from src.models.lstm_training import load_lstm_autoencoder_model
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"

_TINY_LSTM = {
    "sequence_length": 6,
    "hidden_size": 6,
    "num_layers": 1,
    "latent_dim": 2,
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


def _write_config(tmp_path: Path, **lstm_overrides: object) -> Path:
    data = yaml.safe_load(EXAMPLE_YAML.read_text())
    data["models"]["lstm_autoencoder"].update(_TINY_LSTM)
    data["models"]["lstm_autoencoder"].update(lstm_overrides)
    path = tmp_path / "canary.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _config(tmp_path: Path, **lstm_overrides: object) -> CanaryConfig:
    return CanaryConfig.from_yaml(_write_config(tmp_path, **lstm_overrides))


def _no_bindings(tmp_path: Path) -> Path:
    return tmp_path / "no-bindings.yaml"


def _write_obd_session(root: Path, name: str, *, rows: int, start_epoch: float) -> Path:
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(rows):
        t = start_epoch + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 780.0 + (i % 4), "rpm"),
                TelemetryRecord.obd(t, "SPEED", float(i % 5), "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 6.0 + (i % 3), "%"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_build_dataset_discovers_feature_columns(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = _write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)

    dataset = build_dataset([session], config, bindings_path=_no_bindings(tmp_path))

    assert len(dataset) > 0
    assert dataset.sequence_length == _TINY_LSTM["sequence_length"]
    assert "obd:RPM" in dataset.feature_columns
    assert any(col.startswith("obd:RPM__") for col in dataset.feature_columns)


def test_build_dataset_rejects_sessions_without_numeric_columns(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = tmp_path / "can_only"
    records = [
        TelemetryRecord.can(1_778_500_000.0 + i, 0x100, b"\x01\x02\x03\x04") for i in range(20)
    ]
    write_records_to_parquet(records, session, max_rows=10_000)

    with pytest.raises(ValueError, match="no trainable feature columns"):
        build_dataset([session], config, bindings_path=_no_bindings(tmp_path))


def test_train_writes_checkpoint_and_manifest(tmp_path: Path) -> None:
    _torch()
    config = _config(tmp_path)
    session = _write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    out_dir = tmp_path / "models" / "lstm"

    trained = train([session], config, out_dir, bindings_path=_no_bindings(tmp_path))

    artifact = out_dir / "model.pt"
    manifest = out_dir / "model.manifest.json"
    assert artifact.exists()
    assert manifest.exists()
    assert trained.manifest.thresholds is not None
    assert trained.feature_columns
    assert trained.manifest.input_windows > 0

    reloaded = load_lstm_autoencoder_model(artifact)
    assert reloaded.feature_columns == trained.feature_columns
    assert reloaded.manifest.thresholds is not None
    payload = json.loads(manifest.read_text())
    assert payload["model_type"] == "lstm_autoencoder"
    assert payload["schema"]["feature_spec_version"] == 1
    assert payload["schema"]["preprocessing"]["resample_hz"] == config.preprocessing.resample_hz
    assert payload["thresholds"]["threshold_k"] == _TINY_LSTM["threshold_k"]


def test_evaluate_runs_false_positive_benchmark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _torch()
    config = _config(tmp_path)
    train_session = _write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    held_out = _write_obd_session(tmp_path, "holdout", rows=120, start_epoch=1_778_600_000.0)
    trained = train([train_session], config, tmp_path / "m", bindings_path=_no_bindings(tmp_path))

    report = evaluate(trained, [held_out], config, bindings_path=_no_bindings(tmp_path))

    assert isinstance(report, FalsePositiveBenchmarkReport)
    assert report.window_count > 0
    assert report.evaluated_sessions == ("holdout",)
    out = capsys.readouterr().out
    assert "FALSE-POSITIVE BENCHMARK" in out
    assert "k sweep" in out


def test_evaluate_returns_none_when_held_out_too_short(tmp_path: Path) -> None:
    _torch()
    config = _config(tmp_path)
    train_session = _write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    short = _write_obd_session(tmp_path, "short", rows=4, start_epoch=1_778_600_000.0)
    trained = train([train_session], config, tmp_path / "m", bindings_path=_no_bindings(tmp_path))

    assert evaluate(trained, [short], config, bindings_path=_no_bindings(tmp_path)) is None


def test_main_trains_and_evaluates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _torch()
    config_path = _write_config(tmp_path)
    train_session = _write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    held_out = _write_obd_session(tmp_path, "holdout", rows=120, start_epoch=1_778_600_000.0)
    out_dir = tmp_path / "models" / "lstm"

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
    assert "Trained LSTM autoencoder saved to" in out
    assert "FALSE-POSITIVE BENCHMARK" in out


def test_main_errors_when_no_sessions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = _write_config(tmp_path)
    exit_code = main(["--sessions-root", str(tmp_path / "empty"), "--config", str(config_path)])
    assert exit_code == 1
    assert "no sessions" in capsys.readouterr().err


def test_resolve_training_sessions_excludes_evaluate_sessions(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    _write_obd_session(root, "a", rows=10, start_epoch=1_778_500_000.0)
    held_out = _write_obd_session(root, "b", rows=10, start_epoch=1_778_600_000.0)

    resolved = _resolve_training_sessions(
        SimpleNamespace(sessions=[], sessions_root=root, evaluate=[held_out])
    )

    assert resolved is not None
    assert [Path(p).name for p in resolved] == ["a"]
