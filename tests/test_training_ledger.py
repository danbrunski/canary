"""Tests for the training-run ledger (Task 6.21)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import scripts.ledger as ledger_cli
from scripts import _cli, retrain
from src.models import training_entry
from src.models.feedback import FeedbackStore
from src.models.manifest import ManifestValidationError
from src.models.training_ledger import (
    TrainingLedger,
    TrainingRunRecord,
    build_run_record_from_manifest,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.builders import evaluation_report
from tests.configs import retrain_argv


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


def test_build_from_if_manifest_records_detector_benchmark_report_path(tmp_path: Path) -> None:
    report = tmp_path / "analyses" / "benchmark-20260719-120000.json"
    rec = build_run_record_from_manifest(
        _if_manifest(tmp_path / "if"), detector_benchmark_report_path=report
    )
    assert rec.detector_benchmark_report_path == str(report)


def test_build_from_if_manifest_without_benchmark_path_is_none(tmp_path: Path) -> None:
    rec = build_run_record_from_manifest(_if_manifest(tmp_path / "if2"))
    assert rec.detector_benchmark_report_path is None


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


def test_record_dispositions_defaults_to_none() -> None:
    rec = TrainingRunRecord(
        run_id="x",
        model_type="isolation_forest",
        created_at="2026-05-16T00:00:00Z",
        status="succeeded",
    )
    assert rec.dispositions is None


def test_record_dispositions_round_trip() -> None:
    """Task 1.4: a run's disposition summary (from
    ``src.analysis.training_disposition.classify_dispositions``) survives a
    JSONL round trip on the ledger record."""
    summary = {
        "groups": [
            {
                "channel": "obd:SHORT_FUEL_TRIM_1",
                "operating_mode": "idle",
                "accept_recenter": 0,
                "quarantine": 3,
                "accept": 97,
                "total": 100,
                "baseline_low": -5.0,
                "baseline_high": 5.0,
            }
        ],
        "reject_counts": {"obd:RPM": 2},
        "totals": {"reject": 2, "accept_recenter": 0, "quarantine": 3, "accept": 97},
    }
    rec = TrainingRunRecord(
        run_id="x",
        model_type="isolation_forest",
        created_at="2026-05-16T00:00:00Z",
        status="succeeded",
        dispositions=summary,
    )
    assert TrainingRunRecord.from_dict(rec.to_dict()) == rec
    assert rec.dispositions == summary


# ---------------------------------------------------------------------------
# Golden baseline (Task 3.4)
# ---------------------------------------------------------------------------


def test_set_golden_demotes_previous_golden(tmp_path: Path) -> None:
    led = TrainingLedger(tmp_path / "runs.jsonl")
    old = build_run_record_from_manifest(_lstm_manifest(tmp_path / "old", "lstm_v1"))
    new = build_run_record_from_manifest(_lstm_manifest(tmp_path / "new", "lstm_v2"))
    led.append(old)
    led.append(new)
    led.set_golden("lstm_v1")
    led.set_golden("lstm_v2")
    by_id = {r.run_id: r for r in led.records()}
    assert by_id["lstm_v2"].golden is True
    assert by_id["lstm_v1"].golden is False  # demoted when v2 declared golden
    current_golden = led.golden_run("lstm_autoencoder")
    assert current_golden is not None
    assert current_golden.run_id == "lstm_v2"


def test_set_golden_unknown_run_returns_none(tmp_path: Path) -> None:
    led = TrainingLedger(tmp_path / "runs.jsonl")
    assert led.set_golden("does-not-exist") is None


def test_golden_run_none_when_undeclared(tmp_path: Path) -> None:
    led = TrainingLedger(tmp_path / "runs.jsonl")
    led.append(build_run_record_from_manifest(_lstm_manifest(tmp_path / "lstm", "lstm_v1")))
    assert led.golden_run("lstm_autoencoder") is None


def test_build_run_record_records_compute_target(tmp_path: Path) -> None:
    """Task 6.36 — `scripts/retrain.py --compute-target` threads through."""
    record = build_run_record_from_manifest(_if_manifest(tmp_path / "if"), compute_target="laptop")
    assert record.compute_target == "laptop"


def test_build_run_record_compute_target_defaults_to_none(tmp_path: Path) -> None:
    record = build_run_record_from_manifest(_if_manifest(tmp_path / "if"))
    assert record.compute_target is None


def test_set_approved_records_operator_decision(tmp_path: Path) -> None:
    """Task 6.36 — approval is orthogonal to publish_status/golden: an
    approved run need not be published or declared golden."""
    led = TrainingLedger(tmp_path / "runs.jsonl")
    led.append(build_run_record_from_manifest(_if_manifest(tmp_path / "if", "if_v1")))

    approved = led.set_approved("if_v1", True, notes="looks good")
    assert approved is not None
    assert approved.approved is True
    assert approved.approved_at is not None
    assert approved.notes == "looks good"
    assert approved.publish_status == "unpublished"

    unapproved = led.set_approved("if_v1", False)
    assert unapproved is not None
    assert unapproved.approved is False
    assert unapproved.approved_at is None


def test_set_approved_unknown_run_returns_none(tmp_path: Path) -> None:
    led = TrainingLedger(tmp_path / "runs.jsonl")
    assert led.set_approved("does-not-exist", True) is None


def test_golden_is_independent_of_publish_status(tmp_path: Path) -> None:
    """Golden and active are orthogonal — a golden run need not be the one
    currently serving live traffic (the whole point is comparing them)."""
    led = TrainingLedger(tmp_path / "runs.jsonl")
    golden_run = build_run_record_from_manifest(_lstm_manifest(tmp_path / "g", "lstm_golden"))
    active_run = build_run_record_from_manifest(_lstm_manifest(tmp_path / "a", "lstm_active"))
    led.append(golden_run)
    led.append(active_run)
    led.set_golden("lstm_golden")
    led.set_publish_status("lstm_active", "active")
    by_id = {r.run_id: r for r in led.records()}
    assert by_id["lstm_golden"].golden is True
    assert by_id["lstm_golden"].publish_status == "unpublished"
    assert by_id["lstm_active"].publish_status == "active"
    assert by_id["lstm_active"].golden is False


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
# CLI: promote / golden / golden-status (Task 3.3/3.4)
# ---------------------------------------------------------------------------


def _benchmark_report(*, recall: float, per_hour: float) -> dict:
    """An evaluate_detectors report (R06.5: the protocol the gates read)."""
    return evaluation_report({"isolation_forest": recall}, {"isolation_forest": per_hour})


_CLEAN_DISPOSITIONS = {
    "groups": [],
    "reject_counts": {},
    "totals": {"reject": 0, "accept_recenter": 0, "quarantine": 0, "accept": 100},
}


def test_ledger_cli_promote_gate_pass(tmp_path: Path) -> None:
    ledger_path = tmp_path / "runs.jsonl"
    led = TrainingLedger(ledger_path)

    golden_report_path = tmp_path / "golden-bench.json"
    golden_report_path.write_text(json.dumps(_benchmark_report(recall=1.0, per_hour=1.0)))
    candidate_report_path = tmp_path / "candidate-bench.json"
    candidate_report_path.write_text(json.dumps(_benchmark_report(recall=1.0, per_hour=1.1)))

    golden = build_run_record_from_manifest(
        _if_manifest(tmp_path / "golden", "if_golden"),
        detector_benchmark_report_path=golden_report_path,
    ).model_copy(update={"dispositions": _CLEAN_DISPOSITIONS})
    candidate = build_run_record_from_manifest(
        _if_manifest(tmp_path / "candidate", "if_candidate"),
        detector_benchmark_report_path=candidate_report_path,
    ).model_copy(update={"dispositions": _CLEAN_DISPOSITIONS})
    led.append(golden)
    led.append(candidate)
    led.set_golden("if_golden")

    rc = ledger_cli.main(["--ledger", str(ledger_path), "promote", "if_candidate"])
    assert rc == 0
    updated = TrainingLedger(ledger_path).get("if_candidate")
    assert updated is not None
    assert updated.publish_status == "active"


def test_ledger_cli_promote_gate_rejects_in_sample_calibration(tmp_path: Path) -> None:
    ledger_path = tmp_path / "runs.jsonl"
    led = TrainingLedger(ledger_path)

    golden_report_path = tmp_path / "golden-bench.json"
    golden_report_path.write_text(json.dumps(_benchmark_report(recall=1.0, per_hour=1.0)))
    candidate_manifest_dir = tmp_path / "candidate"
    manifest_path = _if_manifest(candidate_manifest_dir, "if_candidate")
    manifest = json.loads(manifest_path.read_text())
    manifest["calibration"] = "in_sample"
    manifest_path.write_text(json.dumps(manifest))

    golden = build_run_record_from_manifest(
        _if_manifest(tmp_path / "golden", "if_golden"),
        detector_benchmark_report_path=golden_report_path,
    ).model_copy(update={"dispositions": _CLEAN_DISPOSITIONS})
    candidate = build_run_record_from_manifest(manifest_path).model_copy(
        update={"dispositions": _CLEAN_DISPOSITIONS}
    )
    led.append(golden)
    led.append(candidate)
    led.set_golden("if_golden")

    rc = ledger_cli.main(["--ledger", str(ledger_path), "promote", "if_candidate"])
    assert rc == 1
    updated = TrainingLedger(ledger_path).get("if_candidate")
    assert updated is not None
    assert updated.publish_status == "unpublished"  # never promoted


def test_ledger_cli_promote_rollback_via_publish(tmp_path: Path) -> None:
    """A promoted run can still be rolled back through the pre-existing
    ungated `publish --status rolled_back` escape hatch."""
    ledger_path = tmp_path / "runs.jsonl"
    led = TrainingLedger(ledger_path)
    golden_report_path = tmp_path / "golden-bench.json"
    golden_report_path.write_text(json.dumps(_benchmark_report(recall=1.0, per_hour=1.0)))
    candidate_report_path = tmp_path / "candidate-bench.json"
    candidate_report_path.write_text(json.dumps(_benchmark_report(recall=1.0, per_hour=1.0)))
    golden = build_run_record_from_manifest(
        _if_manifest(tmp_path / "golden", "if_golden"),
        detector_benchmark_report_path=golden_report_path,
    ).model_copy(update={"dispositions": _CLEAN_DISPOSITIONS})
    candidate = build_run_record_from_manifest(
        _if_manifest(tmp_path / "candidate", "if_candidate"),
        detector_benchmark_report_path=candidate_report_path,
    ).model_copy(update={"dispositions": _CLEAN_DISPOSITIONS})
    led.append(golden)
    led.append(candidate)
    led.set_golden("if_golden")

    assert ledger_cli.main(["--ledger", str(ledger_path), "promote", "if_candidate"]) == 0
    assert (
        ledger_cli.main(
            ["--ledger", str(ledger_path), "publish", "if_candidate", "--status", "rolled_back"]
        )
        == 0
    )
    updated = TrainingLedger(ledger_path).get("if_candidate")
    assert updated is not None
    assert updated.publish_status == "rolled_back"


def test_ledger_cli_golden_and_golden_status(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "runs.jsonl"
    led = TrainingLedger(ledger_path)
    golden_report_path = tmp_path / "golden-bench.json"
    golden_report_path.write_text(json.dumps(_benchmark_report(recall=1.0, per_hour=1.0)))
    current_report_path = tmp_path / "current-bench.json"
    current_report_path.write_text(json.dumps(_benchmark_report(recall=0.8, per_hour=3.0)))
    golden = build_run_record_from_manifest(
        _if_manifest(tmp_path / "golden", "if_golden"),
        detector_benchmark_report_path=golden_report_path,
    )
    current = build_run_record_from_manifest(
        _if_manifest(tmp_path / "current", "if_current"),
        detector_benchmark_report_path=current_report_path,
    )
    led.append(golden)
    led.append(current)

    assert ledger_cli.main(["--ledger", str(ledger_path), "golden", "if_golden"]) == 0
    assert "if_golden declared golden" in capsys.readouterr().out
    led.set_publish_status("if_current", "active")

    rc = ledger_cli.main(["--ledger", str(ledger_path), "golden-status", "isolation_forest"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["golden_run_id"] == "if_golden"
    assert payload["current_run_id"] == "if_current"
    assert payload["per_detector"]["isolation_forest"]["golden_per_hour"] == 1.0
    assert payload["per_detector"]["isolation_forest"]["current_per_hour"] == 3.0


def test_ledger_cli_golden_freezes_disposition_baselines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Task 3.7: `ledger.py golden` re-freezes the model_type's disposition
    baselines from the run's recorded input sessions -- a real, decodable
    session on disk (not the bare session-name placeholders the other
    golden tests use) so the freeze actually has something to read."""
    session = tmp_path / "sessions" / "2026-05-13T120000"
    write_records_to_parquet(
        [
            TelemetryRecord.obd(1_780_000_000.0 + i, "RPM", 900.0 + (i % 20), "rpm")
            for i in range(60)
        ],
        session,
        max_rows=100_000,
    )

    manifest_dir = tmp_path / "if"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "model.joblib").write_bytes(b"fake-forest")
    manifest = {
        "artifact_format_version": 1,
        "model_type": "isolation_forest",
        "model_version": "if_golden",
        "created_at": "2026-05-16T11:00:00+00:00",
        "training_data_hash": "def456",
        "training_sessions": [str(session)],
        "input_rows": 60,
        "hyperparams": {"n_estimators": 200, "contamination": 0.01},
        "schema": {"feature_columns": ["obd:RPM"], "mode_column": "operating_mode"},
        "modes": [{"mode": "all", "samples": 60}],
    }
    (manifest_dir / "model.manifest.json").write_text(json.dumps(manifest))

    ledger_path = tmp_path / "runs.jsonl"
    led = TrainingLedger(ledger_path)
    led.append(build_run_record_from_manifest(manifest_dir / "model.manifest.json"))

    rc = ledger_cli.main(
        ["--ledger", str(ledger_path), "golden", "if_golden", "--config", "canary.example.yaml"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "if_golden declared golden" in out
    assert "froze disposition baselines" in out

    from src.models.golden_baselines import golden_baselines_path, read_golden_baselines

    sidecar = golden_baselines_path(ledger_path, "isolation_forest")
    assert sidecar.exists()
    baselines = read_golden_baselines(sidecar)
    assert baselines is not None
    assert baselines.get("obd:RPM", "all") is not None


def test_ledger_cli_golden_status_no_golden_declared_errors(tmp_path: Path) -> None:
    ledger_path = tmp_path / "runs.jsonl"
    TrainingLedger(ledger_path).append(
        build_run_record_from_manifest(_if_manifest(tmp_path / "if", "if_run"))
    )
    rc = ledger_cli.main(["--ledger", str(ledger_path), "golden-status", "isolation_forest"])
    assert rc == 1


# ---------------------------------------------------------------------------
# retrain.py best-effort ledger hook
# ---------------------------------------------------------------------------


def test_retrain_ingests_ledger_when_manifest_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    if_out = tmp_path / "if"
    lstm_out = tmp_path / "lstm"

    # Stub trainers: write a manifest as the real trainers would.
    monkeypatch.setattr(
        training_entry, "train_isolation_forest", lambda *a, **k: _if_manifest(if_out, "if_run")
    )
    monkeypatch.setattr(
        training_entry,
        "train_sequence_detector",
        lambda *a, **k: _lstm_manifest(lstm_out, "lstm_run"),
    )
    ledger_path = tmp_path / "runs.jsonl"
    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--if-out",
            str(if_out),
            "--sequence-out",
            str(lstm_out),
            # This test stubs build_feature_frame to a sentinel and only
            # exercises the ledger; opt out of the feedback policy (covered
            # by test_retrain_ledger_records_feedback_policy with a real frame).
            "--no-feedback-policy",
            "--ledger",
            str(ledger_path),
            "--compute-target",
            "laptop",
        )
    )
    assert rc == 0
    records = {r.run_id: r for r in TrainingLedger(ledger_path).records()}
    assert set(records) == {"if_run", "lstm_run"}
    # Task 6.36 — `--compute-target` reaches every run ingested this call.
    assert records["if_run"].compute_target == "laptop"
    assert records["lstm_run"].compute_target == "laptop"


def test_retrain_ledger_records_detector_benchmark_report_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 2.6: the benchmark gate's report path links into the ledger
    record of the run it was benchmarked against."""
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    if_out = tmp_path / "if"
    monkeypatch.setattr(
        training_entry, "train_isolation_forest", lambda *a, **k: _if_manifest(if_out, "if_run")
    )
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)

    report_path = tmp_path / "analyses" / "benchmark-20260719-120000.json"
    monkeypatch.setattr(
        retrain,
        "_run_benchmark_gate",
        lambda *a, **k: (report_path, {"regressed": False, "tolerance": 0.15, "cells": []}),
    )
    ledger_path = tmp_path / "runs.jsonl"

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-sequence",
            "--if-out",
            str(if_out),
            "--no-feedback-policy",
            "--ledger",
            str(ledger_path),
        )
    )

    assert rc == 0
    [record] = TrainingLedger(ledger_path).records()
    assert record.detector_benchmark_report_path == str(report_path)


def test_retrain_ledger_records_feedback_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    feedback_path = tmp_path / "feedback.jsonl"
    FeedbackStore(feedback_path).record(
        event={
            "timestamp": "2026-05-17T12:00:00Z",
            "source": "isolation_forest",
            "model_version": "if_v1",
            "score": 0.5,
            "session": "drive-a",
        },
        label="false_positive",
    )
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(
        retrain,
        "build_feature_frame",
        lambda *a, **k: pd.DataFrame(
            {
                "session": ["drive-a"],
                "timestamp": pd.to_datetime(["2026-05-17T12:00:00Z"], utc=True),
                "obd:RPM": [700.0],
            }
        ),
    )
    if_out = tmp_path / "if"
    monkeypatch.setattr(
        training_entry, "train_isolation_forest", lambda *a, **k: _if_manifest(if_out, "if_run")
    )
    ledger_path = tmp_path / "runs.jsonl"

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-sequence",
            "--if-out",
            str(if_out),
            "--ledger",
            str(ledger_path),
            feedback=feedback_path,
        )
    )

    assert rc == 0
    record = TrainingLedger(ledger_path).records()[0]
    assert record.metrics["feedback_policy"]["clean_baseline_labels"] == 1
    assert record.metrics["feedback_policy"]["excluded_rows"] == 0


def test_retrain_ledger_records_disposition_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 1.4/3.3: a real retrain run over decodable sessions finally
    populates `TrainingRunRecord.dispositions` — the champion/challenger
    promotion gate's contamination-sanity check depends on this."""
    session = tmp_path / "sessions" / "2026-05-13T120000"
    records = []
    for i in range(200):
        t = 1_780_000_000.0 + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 900.0 + (i % 20), "rpm"),
                TelemetryRecord.obd(t, "COOLANT_TEMP", 85.0, "C"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=100_000)

    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    if_out = tmp_path / "if"
    monkeypatch.setattr(
        training_entry, "train_isolation_forest", lambda *a, **k: _if_manifest(if_out, "if_run")
    )
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)
    ledger_path = tmp_path / "runs.jsonl"

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-sequence",
            "--if-out",
            str(if_out),
            "--no-feedback-policy",
            "--ledger",
            str(ledger_path),
        )
    )
    assert rc == 0
    [record] = TrainingLedger(ledger_path).records()
    assert record.dispositions is not None
    assert "totals" in record.dispositions


def test_retrain_excludes_quarantine_cells_from_training_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 3.6: a fault-bearing synthetic window shows rows excluded — the
    trainer must never see the Quarantine cells, and the ledger records how
    many were masked out."""
    session = tmp_path / "sessions" / "2026-05-13T120000"
    # Real telemetry on disk too (same shape) so the informational disposition
    # summary (`_compute_disposition_summary`, a *separate* raw decode of the
    # sessions) also succeeds and gets merged with the excluded counts below —
    # an empty session dir would leave `dispositions` `None` and mask the
    # merge this test is asserting on.
    write_records_to_parquet(
        [
            TelemetryRecord.obd(1_780_000_000.0 + i, "SHORT_FUEL_TRIM_1", 0.0, "%")
            for i in range(30)
        ],
        session,
        max_rows=100_000,
    )
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])

    n = 30
    fault_rows = 5
    feature_frame = pd.DataFrame(
        {
            "session": ["s0"] * n,
            "timestamp": pd.to_datetime(
                [1_780_000_000.0 + i for i in range(n)], unit="s", utc=True
            ),
            "operating_mode": ["idle"] * n,
            # A clear, persistent fault band in the first `fault_rows` rows —
            # everything else wanders continuously near 0.
            "obd:SHORT_FUEL_TRIM_1": (
                [500.0] * fault_rows + [-3.0 + (i % 61) * 0.1 for i in range(n - fault_rows)]
            ),
        }
    )
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: feature_frame)

    captured: dict[str, object] = {}

    def _if_train(*_a: object, **kwargs: object) -> Path:
        captured["feature_frame"] = kwargs["feature_frame"]
        return _if_manifest(tmp_path / "if", "if_run")

    monkeypatch.setattr(training_entry, "train_isolation_forest", _if_train)
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)
    ledger_path = tmp_path / "runs.jsonl"

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            "--no-feedback-policy",
            "--ledger",
            str(ledger_path),
        )
    )
    assert rc == 0

    trained_frame = captured["feature_frame"]
    assert isinstance(trained_frame, pd.DataFrame)
    # The fault rows are masked to NaN in the actual training matrix —
    # cell-level, never a row drop (the frame is still 30 rows).
    assert len(trained_frame) == n
    assert trained_frame["obd:SHORT_FUEL_TRIM_1"].iloc[:fault_rows].isna().all()
    assert trained_frame["obd:SHORT_FUEL_TRIM_1"].iloc[fault_rows:].notna().all()

    [record] = TrainingLedger(ledger_path).records()
    assert record.dispositions is not None
    assert record.dispositions["excluded"]["obd:SHORT_FUEL_TRIM_1"] == fault_rows


def test_retrain_skip_quarantine_exclusion_flag_leaves_matrix_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])

    n = 30
    feature_frame = pd.DataFrame(
        {
            "session": ["s0"] * n,
            "timestamp": pd.to_datetime(
                [1_780_000_000.0 + i for i in range(n)], unit="s", utc=True
            ),
            "operating_mode": ["idle"] * n,
            "obd:SHORT_FUEL_TRIM_1": ([500.0] * 5 + [-3.0 + (i % 61) * 0.1 for i in range(n - 5)]),
        }
    )
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: feature_frame)

    captured: dict[str, object] = {}

    def _if_train(*_a: object, **kwargs: object) -> Path:
        captured["feature_frame"] = kwargs["feature_frame"]
        return _if_manifest(tmp_path / "if", "if_run")

    monkeypatch.setattr(training_entry, "train_isolation_forest", _if_train)
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            "--no-feedback-policy",
            "--no-ledger",
            "--skip-quarantine-exclusion",
        )
    )
    assert rc == 0
    trained_frame = captured["feature_frame"]
    assert isinstance(trained_frame, pd.DataFrame)
    assert trained_frame["obd:SHORT_FUEL_TRIM_1"].notna().all()  # untouched


def test_retrain_quarantine_exclusion_is_golden_anchored_when_declared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 3.7: when a frozen golden baseline sidecar exists for
    isolation_forest, `_apply_quarantine_exclusion` screens against it
    instead of this window's own (self-referential) statistics -- catching
    a drift that has come to dominate the candidate window, which
    window-local screening would wave through as the new "normal"."""
    from src.models.golden_baselines import (
        ChannelBaseline,
        GoldenBaselines,
        golden_baselines_path,
        write_golden_baselines,
    )

    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])

    n = 30
    # The whole window has drifted to 20.0 -- a window-local screen would
    # read that as the new median and quarantine nothing.
    feature_frame = pd.DataFrame(
        {
            "session": ["s0"] * n,
            "timestamp": pd.to_datetime(
                [1_780_000_000.0 + i for i in range(n)], unit="s", utc=True
            ),
            "operating_mode": ["idle"] * n,
            "obd:SHORT_FUEL_TRIM_1": [20.0] * n,
        }
    )
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: feature_frame)

    ledger_path = tmp_path / "runs.jsonl"
    sidecar = golden_baselines_path(ledger_path, "isolation_forest")
    write_golden_baselines(
        sidecar,
        GoldenBaselines(
            baselines=(
                ChannelBaseline(
                    channel="obd:SHORT_FUEL_TRIM_1",
                    operating_mode="idle",
                    is_discrete=False,
                    median=0.0,
                    scaled_mad=1.0,
                ),
            )
        ),
    )

    captured: dict[str, object] = {}

    def _if_train(*_a: object, **kwargs: object) -> Path:
        captured["feature_frame"] = kwargs["feature_frame"]
        return _if_manifest(tmp_path / "if", "if_run")

    monkeypatch.setattr(training_entry, "train_isolation_forest", _if_train)
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            "--no-feedback-policy",
            "--ledger",
            str(ledger_path),
        )
    )
    assert rc == 0
    trained_frame = captured["feature_frame"]
    assert isinstance(trained_frame, pd.DataFrame)
    # Golden-anchored: every one of the drifted 20.0s reads as outside
    # golden's frozen 0.0±sigma band, so the whole column is masked out --
    # the exact opposite of what window-local screening would do here.
    assert trained_frame["obd:SHORT_FUEL_TRIM_1"].isna().all()


def test_retrain_skip_disposition_summary_flag_leaves_it_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    records = [TelemetryRecord.obd(1_780_000_000.0, "RPM", 900.0, "rpm")]
    write_records_to_parquet(records, session, max_rows=100_000)

    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    if_out = tmp_path / "if"
    monkeypatch.setattr(
        training_entry, "train_isolation_forest", lambda *a, **k: _if_manifest(if_out, "if_run")
    )
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)
    ledger_path = tmp_path / "runs.jsonl"

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-sequence",
            "--if-out",
            str(if_out),
            "--no-feedback-policy",
            "--ledger",
            str(ledger_path),
            "--skip-disposition-summary",
        )
    )
    assert rc == 0
    [record] = TrainingLedger(ledger_path).records()
    assert record.dispositions is None


def test_retrain_ledger_hook_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    # Stub trainers that write nothing — ledger ingest must not crash retrain.
    monkeypatch.setattr(training_entry, "train_isolation_forest", lambda *a, **k: None)
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)
    ledger_path = tmp_path / "runs.jsonl"
    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            # Ledger-only test with a sentinel feature frame; the feedback
            # policy needs a real DataFrame and is covered elsewhere.
            "--no-feedback-policy",
            "--ledger",
            str(ledger_path),
        )
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
