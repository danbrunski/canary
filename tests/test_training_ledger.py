"""Tests for the training-run ledger (Task 6.21)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from src.models.manifest import ManifestValidationError
from src.models.training_ledger import (
    TrainingLedger,
    TrainingRunRecord,
    build_run_record_from_manifest,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ledger as ledger_cli  # noqa: E402
import retrain  # noqa: E402


def _lstm_manifest(out_dir: Path, version: str = "lstm_autoencoder_2026") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "model.pt").write_bytes(b"fake-weights")
    manifest = {
        "artifact_format_version": 1,
        "model_type": "lstm_autoencoder",
        "model_version": version,
        "created_at": "2026-05-16T10:00:00+00:00",
        "training_data_hash": "abc123",
        "training_start_timestamp": "2026-05-16T09:00:00+00:00",
        "training_end_timestamp": "2026-05-16T09:30:00+00:00",
        "training_sessions": ["2026-05-11T222447", "2026-05-13T004818"],
        "input_rows": 4096,
        "hyperparams": {"latent_dim": 8, "sequence_length": 30},
        "schema": {"feature_columns": ["obd:RPM", "obd:SPEED"], "sequence_length": 30},
        "thresholds": {"global": {"threshold": 0.42, "threshold_k": 3.0}},
        "architecture": {"hidden_size": 64, "num_layers": 1},
        "losses": [0.9, 0.5, 0.3],
    }
    path = out_dir / "model.manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def _if_manifest(out_dir: Path, version: str = "isolation_forest_2026") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "model.joblib").write_bytes(b"fake-forest")
    manifest = {
        "artifact_format_version": 1,
        "model_type": "isolation_forest",
        "model_version": version,
        "created_at": "2026-05-16T11:00:00+00:00",
        "training_data_hash": "def456",
        "training_sessions": ["2026-05-11T222447"],
        "input_rows": 2048,
        "hyperparams": {"n_estimators": 200, "contamination": 0.01},
        "schema": {"feature_columns": ["obd:RPM"], "mode_column": "operating_mode"},
        "modes": [{"mode": "all", "samples": 2048}],
    }
    path = out_dir / "model.manifest.json"
    path.write_text(json.dumps(manifest))
    return path


# ---------------------------------------------------------------------------
# build_run_record_from_manifest
# ---------------------------------------------------------------------------


def test_build_from_lstm_manifest_extracts_metrics(tmp_path: Path) -> None:
    fp = tmp_path / "fp.json"
    fp.write_text(json.dumps({"false_positive_rate": 0.0, "alerts_per_hour": 0.0}))
    rec = build_run_record_from_manifest(
        _lstm_manifest(tmp_path / "lstm"),
        job_id="job-1",
        false_positive_benchmark_path=fp,
    )
    assert rec.model_type == "lstm_autoencoder"
    assert rec.run_id == "lstm_autoencoder_2026"
    assert rec.input_sessions == ["2026-05-11T222447", "2026-05-13T004818"]
    assert rec.feature_schema == ["obd:RPM", "obd:SPEED"]
    assert rec.hyperparams["latent_dim"] == 8
    assert rec.metrics["loss_curve"] == [0.9, 0.5, 0.3]
    assert rec.metrics["thresholds"]["global"]["threshold"] == 0.42
    assert rec.metrics["architecture"]["hidden_size"] == 64
    assert rec.metrics["false_positive_benchmark"]["alerts_per_hour"] == 0.0
    assert rec.artifacts["model"].endswith("model.pt")
    assert rec.job_id == "job-1"
    assert rec.publish_status == "unpublished"


def test_build_from_if_manifest(tmp_path: Path) -> None:
    rec = build_run_record_from_manifest(_if_manifest(tmp_path / "if"))
    assert rec.model_type == "isolation_forest"
    assert rec.metrics["modes"] == [{"mode": "all", "samples": 2048}]
    assert rec.artifacts["model"].endswith("model.joblib")


def test_build_from_bad_manifest_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(ManifestValidationError):
        build_run_record_from_manifest(missing)
    bad = tmp_path / "bad.json"
    bad.write_text('{"model_type": "lstm_autoencoder"}')  # missing required fields
    with pytest.raises(ManifestValidationError):
        build_run_record_from_manifest(bad)


# ---------------------------------------------------------------------------
# TrainingLedger
# ---------------------------------------------------------------------------


def test_ledger_append_and_collapse_to_latest(tmp_path: Path) -> None:
    led = TrainingLedger(tmp_path / "runs.jsonl")
    assert led.records() == []
    r1 = build_run_record_from_manifest(_if_manifest(tmp_path / "if"))
    r2 = build_run_record_from_manifest(_lstm_manifest(tmp_path / "lstm"))
    led.append(r1)
    led.append(r2)
    # Update r1 (same run_id, new line) — records() shows the latest only.
    led.append(r1.model_copy(update={"publish_status": "published"}))
    runs = led.records()
    assert len(runs) == 2
    if_run = next(r for r in runs if r.model_type == "isolation_forest")
    assert if_run.publish_status == "published"
    # The full provenance chain is still on disk.
    assert len(led.raw_records()) == 3
    assert led.get(r2.run_id) is not None


def test_set_publish_status_demotes_previous_active(tmp_path: Path) -> None:
    led = TrainingLedger(tmp_path / "runs.jsonl")
    old = build_run_record_from_manifest(_lstm_manifest(tmp_path / "old", "lstm_v1"))
    new = build_run_record_from_manifest(_lstm_manifest(tmp_path / "new", "lstm_v2"))
    led.append(old)
    led.append(new)
    led.set_publish_status("lstm_v1", "active")
    led.set_publish_status("lstm_v2", "active")
    by_id = {r.run_id: r for r in led.records()}
    assert by_id["lstm_v2"].publish_status == "active"
    # v1 was demoted to published when v2 went active (only one active/type).
    assert by_id["lstm_v1"].publish_status == "published"
    assert led.set_publish_status("does-not-exist", "active") is None


def test_record_json_round_trip() -> None:
    rec = TrainingRunRecord(
        run_id="x",
        model_type="isolation_forest",
        created_at="2026-05-16T00:00:00Z",
        status="failed",
    )
    assert TrainingRunRecord.from_dict(rec.to_dict()) == rec


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_ledger_cli_ingest_list_publish_show(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "runs.jsonl"
    manifest = _lstm_manifest(tmp_path / "lstm", "lstm_cli")
    assert ledger_cli.main(["--ledger", str(ledger_path), "ingest", str(manifest)]) == 0
    assert "recorded lstm_cli" in capsys.readouterr().out

    assert ledger_cli.main(["--ledger", str(ledger_path), "list"]) == 0
    assert "lstm_cli" in capsys.readouterr().out

    assert (
        ledger_cli.main(["--ledger", str(ledger_path), "publish", "lstm_cli", "--status", "active"])
        == 0
    )
    assert "lstm_cli → active" in capsys.readouterr().out

    assert ledger_cli.main(["--ledger", str(ledger_path), "show", "lstm_cli"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["publish_status"] == "active"

    assert ledger_cli.main(["--ledger", str(ledger_path), "show", "ghost"]) == 1


# ---------------------------------------------------------------------------
# retrain.py best-effort ledger hook
# ---------------------------------------------------------------------------


def test_retrain_ingests_ledger_when_manifest_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(retrain, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    if_out = tmp_path / "if"
    lstm_out = tmp_path / "lstm"

    # Stub trainers: write a manifest as the real trainers would.
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: _if_manifest(if_out, "if_run"))
    monkeypatch.setattr(
        retrain.lstm_trainer, "train", lambda *a, **k: _lstm_manifest(lstm_out, "lstm_run")
    )
    ledger_path = tmp_path / "runs.jsonl"
    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--if-out",
            str(if_out),
            "--lstm-out",
            str(lstm_out),
            "--ledger",
            str(ledger_path),
        ]
    )
    assert rc == 0
    runs = {r.run_id for r in TrainingLedger(ledger_path).records()}
    assert runs == {"if_run", "lstm_run"}


def test_retrain_ledger_hook_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(retrain, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    # Stub trainers that write nothing — ledger ingest must not crash retrain.
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: None)
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: None)
    ledger_path = tmp_path / "runs.jsonl"
    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--if-out",
            str(tmp_path / "if"),
            "--lstm-out",
            str(tmp_path / "lstm"),
            "--ledger",
            str(ledger_path),
        ]
    )
    assert rc == 0
    assert not ledger_path.exists()  # nothing recorded, no stray file


# ---------------------------------------------------------------------------
# GET /control/training-runs (Task 6.22 feed)
# ---------------------------------------------------------------------------

_API_CONFIG_YAML = """
vehicle: {{make: Nissan, model: Xterra, year: 2008}}
hardware:
  elm327_port: /dev/ttyUSB0
  can: {{interface: virtual, channel: vcan0, bitrate: 500000}}
collection:
  obd_poll_rate_hz: 1.0
  storage_path: {storage}
  session_retention_days: 30
  storage_warning_mb: 500
preprocessing:
  resample_hz: 1.0
  rolling_window_seconds: 5
  operating_modes: {{}}
models:
  isolation_forest: {{enabled: true, n_estimators: 100, contamination: 0.01}}
  lstm_autoencoder: {{enabled: false, sequence_length: 30, latent_dim: 8, threshold_k: 3.0}}
dashboard: {{host: 127.0.0.1, port: 8000}}
daemon:
  training_ledger: {ledger}
"""


def test_training_runs_endpoint(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from src.config import CanaryConfig
    from src.dashboard.api import create_app

    storage = tmp_path / "storage"
    (storage / "2026-05-17T120000").mkdir(parents=True)
    ledger_path = tmp_path / "runs.jsonl"
    TrainingLedger(ledger_path).append(
        build_run_record_from_manifest(_lstm_manifest(tmp_path / "lstm", "lstm_api"))
    )
    cfg = tmp_path / "canary.yaml"
    cfg.write_text(_API_CONFIG_YAML.format(storage=storage, ledger=ledger_path))
    config = CanaryConfig.from_yaml(cfg)
    client = TestClient(create_app(config, anomalies_dir=tmp_path / "anom", control_token="secret"))
    assert client.get("/control/training-runs").status_code == 401
    body = client.get("/control/training-runs", headers={"Authorization": "Bearer secret"}).json()
    assert body["count"] == 1
    assert body["runs"][0]["run_id"] == "lstm_api"
