"""Tests for the retrain CLI (Task 5.17)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.alerts.feedback import FeedbackStore
from src.config import CanaryConfig

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _cli  # noqa: E402
import retrain  # noqa: E402

EXAMPLE_YAML = Path(__file__).resolve().parents[1] / "canary.example.yaml"


def test_retrain_calls_both_trainers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])

    feature_frame = object()
    calls: list[str] = []
    monkeypatch.setattr(
        retrain,
        "build_feature_frame",
        lambda *a, **k: calls.append("build") or feature_frame,
    )

    def _if_train(*_a: object, **kwargs: object) -> None:
        assert kwargs["feature_frame"] is feature_frame
        calls.append("if")

    def _lstm_train(*_a: object, **kwargs: object) -> None:
        assert kwargs["feature_frame"] is feature_frame
        calls.append("lstm")

    monkeypatch.setattr(retrain.if_trainer, "train", _if_train)
    monkeypatch.setattr(retrain.lstm_trainer, "train", _lstm_train)

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            # Not a feedback test; isolate from the machine's ambient
            # data/control/anomaly_feedback.jsonl (the feedback path is
            # covered by test_retrain_applies_feedback_policy_for_single_trainer).
            "--no-feedback-policy",
        ]
    )
    assert rc == 0
    assert calls == ["build", "if", "lstm"]
    out = capsys.readouterr().out
    assert "isolation_forest" in out
    assert "lstm_autoencoder" in out
    assert "✓" in out


def test_retrain_applies_feedback_policy_for_single_trainer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    feedback_path = tmp_path / "feedback.jsonl"
    FeedbackStore(feedback_path).record(
        event={
            "timestamp": "2026-05-17T12:00:01Z",
            "window_start_timestamp": "2026-05-17T12:00:01Z",
            "window_end_timestamp": "2026-05-17T12:00:01Z",
            "source": "isolation_forest",
            "model_version": "if_v1",
            "score": 1.0,
            "session": "drive-a",
        },
        label="true_positive",
    )
    feature_frame = pd.DataFrame(
        {
            "session": ["drive-a", "drive-a", "drive-a"],
            "timestamp": pd.to_datetime(
                [
                    "2026-05-17T12:00:00Z",
                    "2026-05-17T12:00:01Z",
                    "2026-05-17T12:00:02Z",
                ],
                utc=True,
            ),
            "obd:RPM": [700.0, 999.0, 710.0],
        }
    )
    calls: list[str] = []
    monkeypatch.setattr(
        retrain,
        "build_feature_frame",
        lambda *a, **k: calls.append("build") or feature_frame,
    )

    def _if_train(*_a: object, **kwargs: object) -> None:
        calls.append("if")
        filtered = kwargs["feature_frame"]
        assert isinstance(filtered, pd.DataFrame)
        assert filtered["obd:RPM"].tolist() == [700.0, 710.0]

    monkeypatch.setattr(retrain.if_trainer, "train", _if_train)
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: calls.append("lstm"))

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-sequence",
            "--feedback",
            str(feedback_path),
        ]
    )

    assert rc == 0
    assert calls == ["build", "if"]
    assert "feedback policy" in capsys.readouterr().out


def test_retrain_skip_if(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])

    calls: list[str] = []
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: calls.append("if"))
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: calls.append("lstm"))

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-if",
            "--no-feedback-policy",
        ]
    )
    assert rc == 0
    assert calls == ["lstm"]


def test_retrain_skip_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])

    calls: list[str] = []
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: calls.append("if"))
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: calls.append("lstm"))

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-sequence",
            "--no-feedback-policy",
        ]
    )
    assert rc == 0
    assert calls == ["if"]


def test_retrain_sequence_model_tcn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--sequence-model tcn dispatches to the TCN trainer, not the LSTM one."""
    import yaml

    data = yaml.safe_load(Path("canary.example.yaml").read_text())
    data["models"]["tcn_autoencoder"]["enabled"] = True
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(yaml.safe_dump(data))

    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])

    calls: list[str] = []
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: calls.append("if"))
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: calls.append("lstm"))
    monkeypatch.setattr(retrain.tcn_trainer, "train", lambda *a, **k: calls.append("tcn"))

    rc = retrain.main(
        [
            "--config",
            str(cfg_path),
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--sequence-model",
            "tcn",
            "--skip-if",
            "--no-feedback-policy",
        ]
    )
    assert rc == 0
    assert calls == ["tcn"]
    assert "tcn_autoencoder" in capsys.readouterr().out


def test_retrain_since_boundary_excludes_older_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 3.5: --since-boundary drops sessions older than the latest
    corpus_boundary event; without the flag, nothing is filtered."""
    import yaml

    from src.analysis.vehicle_ledger import VehicleLedgerStore

    ledger_path = tmp_path / "vehicle_ledger.jsonl"
    VehicleLedgerStore(ledger_path).record(
        occurred_at=date(2026, 6, 1), kind="mod", title="Intake swap", corpus_boundary=True
    )

    data = yaml.safe_load(EXAMPLE_YAML.read_text())
    data["daemon"] = {"vehicle_ledger": str(ledger_path)}
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(yaml.safe_dump(data))

    old_session = tmp_path / "sessions" / "2026-05-01T120000"
    new_session = tmp_path / "sessions" / "2026-06-15T120000"
    old_session.mkdir(parents=True)
    new_session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [old_session, new_session])

    seen: dict[str, list[Path]] = {}

    def _if_train(sessions_arg: list[Path], *_a: object, **_kwargs: object) -> None:
        seen["sessions"] = list(sessions_arg)

    monkeypatch.setattr(retrain.if_trainer, "train", _if_train)
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: None)

    rc = retrain.main(
        [
            "--config",
            str(cfg_path),
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--since-boundary",
            "--skip-sequence",
            "--no-feedback-policy",
        ]
    )
    assert rc == 0
    assert seen["sessions"] == [new_session]


# ---------------------------------------------------------------------------
# --since rolling-window selection (Task 3.1, online-training-strategy.md)
# ---------------------------------------------------------------------------


def test_retrain_since_selects_only_sessions_at_or_after_cutoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--since is a sliding window (an absolute cutoff), not a --days lookback."""
    root = tmp_path / "sessions"
    sessions = [
        root / "2026-05-01T120000",
        root / "2026-06-01T120000",
        root / "2026-06-15T120000",
        root / "2026-07-01T120000",
    ]
    for s in sessions:
        s.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    monkeypatch.setattr(retrain, "_session_parquet_bytes", lambda _p: 5_000_000)

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        retrain.if_trainer,
        "train",
        lambda positional, *_a, **_k: seen.__setitem__("if_sessions", list(positional)),
    )
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: None)

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(root),
            "--since",
            "2026-06-01",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            "--no-feedback-policy",
            "--skip-disposition-summary",
        ]
    )
    assert rc == 0
    # 2026-05-01 dropped by --since; of the remaining three, the auto
    # calibration-holdout carves the most recent one out for calibration
    # (3 sessions → 1 holdout, 2 left for training).
    assert seen["if_sessions"] == sessions[1:-1]


def test_retrain_since_window_too_small_for_holdout_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Task 3.1: a --since window that can't spare a calibration holdout
    refuses the run (a distinct exit code) rather than silently falling back
    to in-sample calibration — the posture a scheduled retrain needs (3.2
    treats this as a routine skip, not a hard failure)."""
    root = tmp_path / "sessions"
    sessions = [root / "2026-06-01T120000", root / "2026-06-02T120000"]
    for s in sessions:
        s.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(root),
            "--since",
            "2026-01-01",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
        ]
    )
    assert rc == retrain.WINDOW_TOO_SMALL_FOR_HOLDOUT
    assert "too small to reserve a calibration holdout" in capsys.readouterr().err


def test_retrain_since_with_no_calibration_holdout_does_not_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--no-calibration-holdout is still a valid, explicit manual opt-out even
    under --since — only a *scheduled* run (which never passes that flag,
    Task 3.2) is forced through the refusal path."""
    root = tmp_path / "sessions"
    sessions = [root / "2026-06-01T120000", root / "2026-06-02T120000"]
    for s in sessions:
        s.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: None)
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: None)

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(root),
            "--since",
            "2026-01-01",
            "--no-calibration-holdout",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            "--no-feedback-policy",
            "--skip-disposition-summary",
        ]
    )
    assert rc == 0


def test_retrain_both_skip_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [tmp_path])
    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-if",
            "--skip-sequence",
        ]
    )
    assert rc == 1


def test_retrain_no_sessions_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [])
    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
        ]
    )
    assert rc == 1


def test_retrain_continues_after_first_trainer_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A broken IF trainer must not stop the LSTM trainer (and vice versa)."""
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())

    lstm_called: list[bool] = []

    def _broken_if(*_a: object, **_k: object) -> None:
        raise RuntimeError("simulated IF failure")

    def _ok_lstm(*_a: object, **_k: object) -> None:
        lstm_called.append(True)

    monkeypatch.setattr(retrain.if_trainer, "train", _broken_if)
    monkeypatch.setattr(retrain.lstm_trainer, "train", _ok_lstm)

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
        ]
    )
    # One trainer failed, but the other still ran; partial success exits zero so
    # sync-train can continue with independent behavior-model refits.
    assert rc == 0
    assert lstm_called == [True]
    out = capsys.readouterr().out
    assert "✗ isolation_forest" in out
    assert "✓ lstm_autoencoder" in out


def test_retrain_returns_error_when_all_requested_trainers_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())

    def _broken(*_a: object, **_k: object) -> None:
        raise RuntimeError("simulated trainer failure")

    monkeypatch.setattr(retrain.if_trainer, "train", _broken)
    monkeypatch.setattr(retrain.lstm_trainer, "train", _broken)

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
        ]
    )
    assert rc == 1


def test_retrain_skip_benchmark_flag_prevents_gate_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: None)
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: None)

    def _boom(*_a: object, **_k: object) -> tuple[None, None]:
        raise AssertionError("benchmark gate must not run with --skip-benchmark")

    monkeypatch.setattr(retrain, "_run_benchmark_gate", _boom)

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
            "--skip-benchmark",
        ]
    )
    assert rc == 0


def test_retrain_calls_benchmark_gate_with_calibration_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate is called (unless --skip-benchmark) once both trainers have
    been attempted, with whatever calibration holdouts retrain picked."""
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: None)
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: None)

    calls: list[object] = []

    def _fake_gate(config: object, calibration_sessions: object, args: object) -> tuple[None, None]:
        calls.append(calibration_sessions)
        return None, None

    monkeypatch.setattr(retrain, "_run_benchmark_gate", _fake_gate)

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
        ]
    )
    assert rc == 0
    assert len(calls) == 1
    assert calls[0] == []  # a single session can't be held out for calibration


def test_retrain_returns_error_when_benchmark_gate_regressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A benchmark regression fails the retrain run even though both
    trainers themselves succeeded — the gate is a distinct pass/fail check,
    not folded into trainer partial-success."""
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: None)
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: None)

    report_path = tmp_path / "analyses" / "benchmark-20260719-120000.json"
    monkeypatch.setattr(
        retrain,
        "_run_benchmark_gate",
        lambda *a, **k: (report_path, {"regressed": True, "tolerance": 0.15, "cells": []}),
    )

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "regression detected vs baseline" in out


def test_retrain_ok_benchmark_does_not_fail_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: None)
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: None)

    report_path = tmp_path / "analyses" / "benchmark-20260719-120000.json"
    monkeypatch.setattr(
        retrain,
        "_run_benchmark_gate",
        lambda *a, **k: (report_path, {"regressed": False, "tolerance": 0.15, "cells": []}),
    )

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "✓ benchmark" in out


# ---------------------------------------------------------------------------
# _run_benchmark_gate itself — every test above monkeypatches this function
# out, so these call the real thing to cover its actual internals (empty-
# holdouts skip, missing-artifact skip, and a real grid + baseline
# auto-discovery run).
# ---------------------------------------------------------------------------


def test_run_benchmark_gate_skips_cleanly_with_no_calibration_sessions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    args = retrain.parse_args(
        [
            "--config",
            "canary.example.yaml",
            "--if-out",
            str(tmp_path / "if"),
            "--analyses-dir",
            str(tmp_path / "analyses"),
        ]
    )

    caplog.set_level(logging.WARNING)
    result = retrain._run_benchmark_gate(config, [], args)

    assert result == (None, None)
    assert "no held-out sessions" in caplog.text


def test_run_benchmark_gate_skips_cleanly_with_missing_if_artifact(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.test_detector_benchmark import _write_fixture_session

    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    session = _write_fixture_session(tmp_path / "sessions", "2026-05-12T000000")
    args = retrain.parse_args(
        [
            "--config",
            "canary.example.yaml",
            "--if-out",
            str(tmp_path / "if"),  # no model.joblib written under here
            "--analyses-dir",
            str(tmp_path / "analyses"),
        ]
    )

    caplog.set_level(logging.WARNING)
    result = retrain._run_benchmark_gate(config, [session], args)

    assert result == (None, None)
    assert "no isolation forest artifact" in caplog.text


def test_run_benchmark_gate_runs_real_grid_and_discovers_prior_baseline(
    tmp_path: Path,
) -> None:
    """End-to-end: a tiny fixture session + a real trained IF artifact + a
    prior benchmark-*.json already on disk. Confirms the gate runs the real
    grid, auto-discovers the prior report as --benchmark-baseline (not
    given explicitly here), and returns a real report/diff pair."""
    from src.models.isolation_forest import default_manifest_path, fit_isolation_forest
    from src.preprocessing.feature_pipeline import featurize_wide_frame
    from tests.test_detector_benchmark import _write_fixture_session

    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    # The gate always attempts to load the configured sequence backend (Task
    # 2.6 has no --no-sequence knob); disable it here so this test doesn't
    # pick up the real Torch LSTM artifact that lives under
    # models/lstm_autoencoder in this working tree.
    config.models.lstm_autoencoder.enabled = False
    # This fixture session is a tiny 120-row single-mode frame (96 fit rows
    # after the 20 % calibration holdout) — well under the production
    # min_samples_per_mode floor (Task 2.2). Lower it so the gate still
    # trains a real model instead of skipping the only mode present.
    config.models.isolation_forest.min_samples_per_mode = 5

    session = _write_fixture_session(tmp_path / "sessions", "2026-05-12T000000", seed=1, n=120)
    long = retrain.detector_benchmark.load_resampled_session(session, config)
    features = featurize_wide_frame(long, config).features
    trained = fit_isolation_forest(
        features,
        config.models.isolation_forest,
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T00:00:00Z",
    )
    if_out = tmp_path / "if"
    artifact_path = if_out / "model.joblib"
    trained.save(artifact_path, default_manifest_path(artifact_path))

    analyses_dir = tmp_path / "analyses"
    analyses_dir.mkdir(parents=True)
    prior_report = analyses_dir / "benchmark-20260101-000000.json"
    prior_report.write_text(
        json.dumps({"generated_at": "20260101-000000", "table": [], "cells": []})
    )

    args = retrain.parse_args(
        [
            "--config",
            "canary.example.yaml",
            "--if-out",
            str(if_out),
            "--analyses-dir",
            str(analyses_dir),
        ]
    )

    report_path, diff = retrain._run_benchmark_gate(config, [session], args)

    assert report_path is not None
    assert report_path.exists()
    assert report_path.parent == analyses_dir
    report = json.loads(report_path.read_text())
    assert report["cells"]  # the real grid produced per-(session, channel) cells
    assert diff is not None  # baseline auto-discovered from prior_report on disk
    assert "regressed" in diff


def test_retrain_auto_holds_out_most_recent_session_for_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default: out-of-sample calibration. ``retrain`` auto-holds-out the
    most-recent *substantial* discovered session and passes it as
    ``calibration_sessions`` to the sequence trainer — in-sample calibration
    is the deprecated path."""
    root = tmp_path / "sessions"
    sessions = [root / "2026-05-13T120000", root / "2026-05-14T120000", root / "2026-05-15T120000"]
    for s in sessions:
        s.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    # Stub session sizes so each looks "substantial" without writing parquet —
    # the auto-holdout filter is gated on parquet byte totals.
    monkeypatch.setattr(retrain, "_session_parquet_bytes", lambda _p: 5_000_000)

    seen: dict[str, object] = {}

    def _seq_train(positional_sessions: list[Path], *_a: object, **kwargs: object) -> None:
        seen["sessions"] = list(positional_sessions)
        seen["calibration_sessions"] = kwargs.get("calibration_sessions")

    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: None)
    monkeypatch.setattr(retrain.lstm_trainer, "train", _seq_train)

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(root),
            "--all",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            "--no-feedback-policy",
        ]
    )
    assert rc == 0
    # Most-recent session held out for calibration; trainer sees the other two.
    assert seen["calibration_sessions"] == [sessions[-1]]
    assert seen["sessions"] == sessions[:-1]


def test_retrain_skips_tiny_sessions_when_picking_calibration_holdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-progress single-chunk capture at the tail of the corpus (a few KB
    on disk) doesn't have the rows to anchor a useful calibration band, so
    auto-holdout walks back to the most-recent *substantial* session instead.
    Without this, a glitch capture would silently push every mode to the
    global threshold."""
    root = tmp_path / "sessions"
    real_a = root / "2026-05-13T120000"
    real_b = root / "2026-05-14T120000"
    tiny = root / "2026-05-15T172257"  # truncated in-progress capture
    sessions = [real_a, real_b, tiny]
    for s in sessions:
        s.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())

    def _fake_bytes(p: Path) -> int:
        return 22_000 if p == tiny else 5_000_000

    monkeypatch.setattr(retrain, "_session_parquet_bytes", _fake_bytes)

    seen: dict[str, object] = {}
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: None)
    monkeypatch.setattr(
        retrain.lstm_trainer,
        "train",
        lambda positional, *a, **k: seen.update(
            sessions=list(positional), calibration_sessions=k.get("calibration_sessions")
        ),
    )

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(root),
            "--all",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            "--no-feedback-policy",
        ]
    )
    assert rc == 0
    # The tiny tail session is skipped; the previous substantial one is held
    # out, and the training set keeps the tiny session (it still contributes
    # whatever rows the dataset can extract — that's fine for training, just
    # not as a calibration anchor).
    assert seen["calibration_sessions"] == [real_b]
    assert seen["sessions"] == [real_a, tiny]


def test_calibration_picker_rejects_quarantine_and_narrow_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "ready"
    narrow = tmp_path / "narrow"
    quarantined = tmp_path / "quarantined"
    sessions = [ready, narrow, quarantined]
    monkeypatch.setattr(retrain, "_session_parquet_bytes", lambda _p: 5_000_000)
    monkeypatch.setattr(retrain, "_session_mode_coverage", lambda _p: {"city"})
    health = {
        ready: ("ready", 100),
        narrow: ("ready", 50),
        quarantined: ("quarantine", 100),
    }
    monkeypatch.setattr(retrain, "_session_holdout_health", health.__getitem__)

    assert retrain._pick_calibration_holdouts(sessions) == [ready]


def test_retrain_no_calibration_holdout_keeps_full_training_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--no-calibration-holdout opts back into in-sample calibration: every
    discovered session goes to training, no calibration_sessions passed."""
    root = tmp_path / "sessions"
    sessions = [root / "2026-05-13T120000", root / "2026-05-14T120000", root / "2026-05-15T120000"]
    for s in sessions:
        s.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())

    seen: dict[str, object] = {}
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: None)
    monkeypatch.setattr(
        retrain.lstm_trainer,
        "train",
        lambda positional, *a, **k: seen.update(
            sessions=list(positional), calibration_sessions=k.get("calibration_sessions")
        ),
    )

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(root),
            "--all",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            "--no-feedback-policy",
            "--no-calibration-holdout",
        ]
    )
    assert rc == 0
    assert seen["calibration_sessions"] is None
    assert seen["sessions"] == sessions  # nothing held out


# ---------------------------------------------------------------------------
# 2026-09-20: one pooled multi-session holdout for BOTH detectors
# ---------------------------------------------------------------------------


def _healthy_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: list[str]
) -> list[Path]:
    sessions = [tmp_path / "sessions" / name for name in names]
    for session in sessions:
        session.mkdir(parents=True)
    monkeypatch.setattr(retrain, "_session_parquet_bytes", lambda _p: 5_000_000)
    return sessions


def test_holdout_picker_tops_up_to_three_even_when_the_newest_covers_every_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mode coverage alone stopped at ONE session whenever the freshest drive
    touched every mode — one day's weather, altitude and traffic."""
    sessions = _healthy_corpus(
        tmp_path,
        monkeypatch,
        [
            "2026-08-05T120000",
            "2026-08-06T120000",
            "2026-08-20T120000",
            "2026-09-18T120000",
            "2026-09-18T180000",
            "2026-09-19T120000",
        ],
    )
    monkeypatch.setattr(retrain, "_session_mode_coverage", lambda _p: {"city", "highway", "idle"})
    monkeypatch.setattr(retrain, "_session_holdout_health", lambda _p: ("ready", 100))

    picked = retrain._pick_calibration_holdouts(sessions)

    assert len(picked) == retrain.MIN_CALIBRATION_HOLDOUT_SESSIONS == 3
    assert picked[0].name == "2026-09-19T120000"  # newest still anchors
    # The top-ups are spread across the corpus, not the next two newest.
    assert {p.name for p in picked} == {
        "2026-09-19T120000",
        "2026-08-05T120000",
        "2026-08-20T120000",
    }


def test_holdout_picker_orders_by_parsed_time_not_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prefixed directory sorts first by name however late it was recorded."""
    sessions = _healthy_corpus(
        tmp_path,
        monkeypatch,
        ["_tuning-2026-09-19T120000", "2026-08-05T120000", "2026-08-06T120000"],
    )
    monkeypatch.setattr(retrain, "_session_mode_coverage", lambda _p: None)
    monkeypatch.setattr(retrain, "_session_holdout_health", lambda _p: None)

    (anchor,) = retrain._pick_calibration_holdouts(sorted(sessions), max_holdouts=1)
    assert anchor.name == "_tuning-2026-09-19T120000"


def test_holdout_picker_never_tops_up_with_an_unhealthy_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _healthy_corpus(
        tmp_path,
        monkeypatch,
        ["2026-08-05T120000", "2026-08-06T120000", "2026-09-18T120000", "2026-09-19T120000"],
    )
    monkeypatch.setattr(retrain, "_session_mode_coverage", lambda _p: {"city"})
    health = {
        "2026-08-05T120000": ("quarantine", 100),
        "2026-08-06T120000": ("ready", 100),
        "2026-09-18T120000": ("ready", 40),  # narrow schema
        "2026-09-19T120000": ("ready", 100),
    }
    monkeypatch.setattr(retrain, "_session_holdout_health", lambda p: health[p.name])

    picked = retrain._pick_calibration_holdouts(sessions)
    assert [p.name for p in picked] == ["2026-09-19T120000", "2026-08-06T120000"]


def test_retrain_gives_both_detectors_the_same_pooled_holdout_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The IF got three held-out sessions and the LSTM one, because each
    trainer rebuilt the holdout frame privately and the second build died.
    One frame, built once, handed to both."""
    names = [f"2026-09-{day:02d}T120000" for day in range(1, 8)]
    sessions = _healthy_corpus(tmp_path, monkeypatch, names)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))
    monkeypatch.setattr(retrain, "_session_mode_coverage", lambda _p: {"city"})
    monkeypatch.setattr(retrain, "_session_holdout_health", lambda _p: ("ready", 100))

    built: list[list[Path]] = []

    def _build(positional: list[Path], *_a: object, **_k: object) -> object:
        built.append(list(positional))
        return {"frame_for": tuple(p.name for p in positional)}

    monkeypatch.setattr(retrain, "build_feature_frame", _build)
    seen: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(
        retrain.if_trainer, "train", lambda *a, **k: seen.__setitem__("if", dict(k, train=a[0]))
    )
    monkeypatch.setattr(
        retrain.lstm_trainer, "train", lambda *a, **k: seen.__setitem__("seq", dict(k, train=a[0]))
    )

    rc = retrain.main(
        [
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
            "--skip-benchmark",
        ]
    )
    assert rc == 0
    held = seen["if"]["calibration_sessions"]
    assert isinstance(held, list) and len(held) == 3
    assert seen["seq"]["calibration_sessions"] == held
    # The SAME object: built once (training frame + holdout frame = 2 builds).
    assert seen["seq"]["calibration_frame"] is seen["if"]["calibration_frame"]
    assert seen["if"]["calibration_frame"] == {"frame_for": tuple(p.name for p in held)}
    assert len(built) == 2 and built[1] == held
    # Held-out sessions train neither detector.
    assert not set(held) & set(seen["if"]["train"])  # type: ignore[call-overload]
    assert seen["seq"]["train"] == seen["if"]["train"]
    # Both select features under one policy resolved for this vehicle profile.
    assert seen["seq"]["feature_policy"] is seen["if"]["feature_policy"]


# ---------------------------------------------------------------------------
# 2026-09-20: a manifest this run did not write must never reach the ledger
# ---------------------------------------------------------------------------


def _write_manifest(out_dir: Path, version: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "model.manifest.json").write_text(
        json.dumps(
            {
                "artifact_format_version": 1,
                "model_type": "isolation_forest",
                "model_version": version,
                "created_at": "2026-09-20T05:47:03Z",
                "training_data_hash": "sha256:abc",
                "training_sessions": ["2026-09-18T134345"],
                "input_rows": 10,
                "hyperparams": {},
                "schema": {"feature_columns": ["obd:RPM"]},
            }
        )
    )


def test_stubbed_trainer_does_not_re_ingest_the_previous_runs_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """How pytest tmp paths reached real ledger rows: a trainer that returns
    without writing left the PREVIOUS run's manifest in out_dir, and it was
    ingested again as a fresh `succeeded` run (with this run's benchmark path).
    """
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    monkeypatch.setattr(retrain.if_trainer, "train", lambda *a, **k: None)  # writes nothing
    monkeypatch.setattr(retrain.lstm_trainer, "train", lambda *a, **k: None)
    report_path = tmp_path / "analyses" / "benchmark-20260920-000000.json"
    monkeypatch.setattr(
        retrain,
        "_run_benchmark_gate",
        lambda *a, **k: (report_path, {"regressed": False, "tolerance": 0.15, "cells": []}),
    )
    _write_manifest(tmp_path / "if", "isolation_forest_DEPLOYED")  # the "deployed" model
    ledger_path = tmp_path / "training_runs.jsonl"

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            "--ledger",
            str(ledger_path),
            "--no-feedback-policy",
        ]
    )
    assert rc == 0
    assert not ledger_path.exists() or ledger_path.read_text() == ""


def test_a_manifest_written_by_this_run_is_ingested_with_its_benchmark_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    _write_manifest(tmp_path / "if", "isolation_forest_PREVIOUS")
    monkeypatch.setattr(
        retrain.if_trainer,
        "train",
        lambda *a, **k: _write_manifest(tmp_path / "if", "isolation_forest_NEW"),
    )
    report_path = tmp_path / "analyses" / "benchmark-20260920-000000.json"
    monkeypatch.setattr(
        retrain,
        "_run_benchmark_gate",
        lambda *a, **k: (report_path, {"regressed": False, "tolerance": 0.15, "cells": []}),
    )
    ledger_path = tmp_path / "training_runs.jsonl"

    rc = retrain.main(
        [
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-sequence",
            "--if-out",
            str(tmp_path / "if"),
            "--ledger",
            str(ledger_path),
            "--no-feedback-policy",
        ]
    )
    assert rc == 0
    (row,) = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert row["run_id"] == "isolation_forest_NEW"
    assert row["detector_benchmark_report_path"] == str(report_path)


def test_default_cli_paths_cannot_append_to_the_real_training_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conftest guard: even a trainer that DOES write a fresh manifest
    cannot land a test row in data/control/training_runs.jsonl."""
    from src.models.training_ledger import DEFAULT_TRAINING_LEDGER_PATH
    from tests.conftest import _REAL_TRAINING_LEDGER

    # The guard spells the path out to keep torch out of conftest; pin it.
    repo_root = Path(__file__).resolve().parents[1]
    assert (repo_root / DEFAULT_TRAINING_LEDGER_PATH).resolve() == _REAL_TRAINING_LEDGER
    assert retrain.parse_args([]).ledger == DEFAULT_TRAINING_LEDGER_PATH

    _write_manifest(tmp_path / "if", "isolation_forest_NEW")
    before = _REAL_TRAINING_LEDGER.stat().st_size if _REAL_TRAINING_LEDGER.exists() else None
    monkeypatch.chdir(repo_root)
    with pytest.raises(AssertionError, match="real training ledger"):
        retrain._ingest_ledger(
            retrain.TrainingLedger(DEFAULT_TRAINING_LEDGER_PATH),
            tmp_path / "if",
            "succeeded",
            None,
        )
    after = _REAL_TRAINING_LEDGER.stat().st_size if _REAL_TRAINING_LEDGER.exists() else None
    assert before == after
