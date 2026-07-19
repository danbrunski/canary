"""Tests for the LSTM-vs-TCN sequence-detector bake-off (Phase 9.7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.sequence_bakeoff import (
    BackboneResult,
    channel_rate_class,
    main,
    run_bakeoff,
)
from src.config import CanaryConfig
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

pytestmark = pytest.mark.slow  # heavy ML training/bake-off; see pyproject test tiers

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"

_TINY_LSTM = {
    "enabled": True,
    "sequence_length": 6,
    "hidden_size": 6,
    "num_layers": 1,
    "latent_dim": 2,
    "batch_size": 8,
    "learning_rate": 0.01,
    "max_epochs": 2,
    "validation_fraction": 0.0,
    "early_stopping_patience": 2,
    "threshold_k": 8.0,
}
_TINY_TCN = {
    "enabled": True,
    "sequence_length": 6,
    "channels": 8,
    "kernel_size": 2,
    "num_levels": 2,
    "latent_dim": 2,
    "use_delta_decay": True,
    "batch_size": 8,
    "learning_rate": 0.01,
    "max_epochs": 2,
    "validation_fraction": 0.0,
    "early_stopping_patience": 2,
    "threshold_k": 8.0,
}


def _torch():
    return pytest.importorskip("torch")


def test_channel_rate_class() -> None:
    assert channel_rate_class("obd:RPM") == "slow"
    assert channel_rate_class("obd:RPM__mean") == "slow"  # suffix keeps the source
    assert channel_rate_class("did:ECM_INJECTOR_PW_B1") == "slow"
    assert channel_rate_class("gps:speed") == "slow"
    assert channel_rate_class("decoded:WHEEL_SPEED_FL") == "dense"
    assert channel_rate_class("dbc:STEERING_ANGLE") == "dense"
    assert channel_rate_class("derived:STFT_BANK_BALANCE") == "dense"


def _write_config(tmp_path: Path) -> Path:
    data = yaml.safe_load(EXAMPLE_YAML.read_text())
    data["models"]["lstm_autoencoder"].update(_TINY_LSTM)
    data["models"]["tcn_autoencoder"].update(_TINY_TCN)
    path = tmp_path / "canary.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


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


def test_run_bakeoff_compares_both_backbones(tmp_path: Path) -> None:
    _torch()
    config = CanaryConfig.from_yaml(_write_config(tmp_path))
    train_session = _write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    held_out = _write_obd_session(tmp_path, "holdout", rows=120, start_epoch=1_778_600_000.0)

    results = run_bakeoff(
        [train_session],
        [held_out],
        config,
        tmp_path / "bakeoff",
        bindings_path=_no_bindings(tmp_path),
    )

    assert [r.key for r in results] == ["lstm", "tcn"]
    by_type = {r.model_type: r for r in results}
    assert set(by_type) == {"lstm_autoencoder", "tcn_autoencoder"}
    for result in results:
        assert isinstance(result, BackboneResult)
        assert result.artifact_bytes > 0
        assert result.window_count > 0
        # OBD-only sessions → every channel classifies as slow; both rate-class
        # buckets are still reported (dense with zero channels).
        classes = {item.rate_class for item in result.rate_class_fpr}
        assert classes == {"dense", "slow"}
        slow = next(item for item in result.rate_class_fpr if item.rate_class == "slow")
        assert slow.channel_count > 0
        # Per-backbone artifact landed in its own subdir.
        assert (tmp_path / "bakeoff" / result.model_type / "model.pt").exists()


def test_main_writes_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _torch()
    config_path = _write_config(tmp_path)
    train_session = _write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    held_out = _write_obd_session(tmp_path, "holdout", rows=120, start_epoch=1_778_600_000.0)
    out_json = tmp_path / "bakeoff.json"

    exit_code = main(
        [
            str(train_session),
            "--config",
            str(config_path),
            "--bindings",
            str(_no_bindings(tmp_path)),
            "--out",
            str(tmp_path / "bakeoff"),
            "--evaluate",
            str(held_out),
            "--json",
            str(out_json),
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "SEQUENCE-DETECTOR BAKE-OFF" in out
    payload = json.loads(out_json.read_text())
    assert {r["model_type"] for r in payload["results"]} == {
        "lstm_autoencoder",
        "tcn_autoencoder",
    }
