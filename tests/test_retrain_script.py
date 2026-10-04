"""Tests for the retrain CLI (Task 5.17)."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts import _cli, retrain
from src.config import CanaryConfig
from src.decode.dids import load_repo_did_map
from src.features.feature_pipeline import load_resampled_session
from src.models import evaluation as model_evaluation
from src.models import training_entry
from src.models.feedback import FeedbackStore
from tests.builders import IFConfig, write_fixture_session
from tests.configs import example_config, retrain_argv
from tests.repo_guard import RepoTreeGuard

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

    monkeypatch.setattr(training_entry, "train_isolation_forest", _if_train)
    monkeypatch.setattr(training_entry, "train_sequence_detector", _lstm_train)

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
            # Not a feedback test; isolate from the machine's ambient
            # data/control/anomaly_feedback.jsonl (the feedback path is
            # covered by test_retrain_applies_feedback_policy_for_single_trainer).
            "--no-feedback-policy",
        )
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

    monkeypatch.setattr(training_entry, "train_isolation_forest", _if_train)
    monkeypatch.setattr(
        training_entry, "train_sequence_detector", lambda *a, **k: calls.append("lstm")
    )

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-sequence",
            feedback=feedback_path,
        )
    )

    assert rc == 0
    assert calls == ["build", "if"]
    assert "feedback policy" in capsys.readouterr().out


def test_retrain_skip_if(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])

    calls: list[str] = []
    monkeypatch.setattr(
        training_entry, "train_isolation_forest", lambda *a, **k: calls.append("if")
    )
    monkeypatch.setattr(
        training_entry, "train_sequence_detector", lambda *a, **k: calls.append("lstm")
    )

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-if",
            "--no-feedback-policy",
            # Trainer dispatch only: these sessions are empty directories, and
            # the quarantine screen needs a real frame.
            "--skip-quarantine-exclusion",
        )
    )
    assert rc == 0
    assert calls == ["lstm"]


def test_retrain_skip_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])

    calls: list[str] = []
    monkeypatch.setattr(
        training_entry, "train_isolation_forest", lambda *a, **k: calls.append("if")
    )
    monkeypatch.setattr(
        training_entry, "train_sequence_detector", lambda *a, **k: calls.append("lstm")
    )

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-sequence",
            "--no-feedback-policy",
            # Trainer dispatch only: these sessions are empty directories, and
            # the quarantine screen needs a real frame.
            "--skip-quarantine-exclusion",
        )
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
    monkeypatch.setattr(
        training_entry, "train_isolation_forest", lambda *a, **k: calls.append("if")
    )
    monkeypatch.setattr(
        training_entry,
        "train_sequence_detector",
        lambda backend, *a, **k: calls.append(backend.selector),
    )

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            str(cfg_path),
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--sequence-model",
            "tcn",
            "--skip-if",
            "--no-feedback-policy",
            # Trainer dispatch only: these sessions are empty directories, and
            # the quarantine screen needs a real frame.
            "--skip-quarantine-exclusion",
        )
    )
    assert rc == 0
    assert calls == ["tcn"]
    assert "tcn_autoencoder" in capsys.readouterr().out


def test_retrain_since_boundary_excludes_older_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 3.5: --since-boundary drops sessions older than the latest
    corpus_boundary event; without the flag, nothing is filtered.

    The event is read from the vehicle profile's ledger, where the daemon
    records a calibration change (``profiles.record_calibration_change``).
    Retrain's own copy of this block read the unscoped ledger and missed it;
    refit's read the scoped one (R06.3: one helper, ``_cli.apply_since_boundary``).
    """
    import yaml

    from src.vehicle.profiles import resolve_profile_for_tooling, scope_config
    from src.vehicle.vehicle_ledger import VehicleLedgerStore

    ledger_path = tmp_path / "vehicle_ledger.jsonl"
    data = yaml.safe_load(EXAMPLE_YAML.read_text())
    data["daemon"] = {"vehicle_ledger": str(ledger_path)}
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    config = CanaryConfig.from_yaml(cfg_path)
    assert config.profiles.enabled
    profile_ledger = scope_config(
        config, resolve_profile_for_tooling(config, None)
    ).daemon.vehicle_ledger
    assert profile_ledger != ledger_path
    VehicleLedgerStore(profile_ledger).record(
        occurred_at=date(2026, 6, 1), kind="mod", title="Intake swap", corpus_boundary=True
    )

    old_session = tmp_path / "sessions" / "2026-05-01T120000"
    new_session = tmp_path / "sessions" / "2026-06-15T120000"
    old_session.mkdir(parents=True)
    new_session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [old_session, new_session])

    seen: dict[str, list[Path]] = {}

    def _if_train(sessions_arg: list[Path], *_a: object, **_kwargs: object) -> None:
        seen["sessions"] = list(sessions_arg)

    monkeypatch.setattr(training_entry, "train_isolation_forest", _if_train)
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            str(cfg_path),
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--since-boundary",
            "--skip-sequence",
            "--no-feedback-policy",
            # Trainer dispatch only: these sessions are empty directories, and
            # the quarantine screen needs a real frame.
            "--skip-quarantine-exclusion",
        )
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
    monkeypatch.setattr(model_evaluation, "session_parquet_bytes", lambda _p: 5_000_000)

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        training_entry,
        "train_isolation_forest",
        lambda positional, *_a, **_k: seen.__setitem__("if_sessions", list(positional)),
    )
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)

    rc = retrain.main(
        retrain_argv(
            tmp_path,
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
        )
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
        retrain_argv(
            tmp_path,
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
        )
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
    monkeypatch.setattr(training_entry, "train_isolation_forest", lambda *a, **k: None)
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)

    rc = retrain.main(
        retrain_argv(
            tmp_path,
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
        )
    )
    assert rc == 0


def test_retrain_both_skip_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [tmp_path])
    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--skip-if",
            "--skip-sequence",
        )
    )
    assert rc == 1


def test_retrain_no_sessions_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [])
    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
        )
    )
    assert rc == 1


def test_retrain_refuses_a_named_tuning_session_before_building_a_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R03.12 (P02.12): a ``_tuning-*`` recording named on the command line is
    refused at session selection, with exit 2, and no trainer or frame build
    runs. Training discovery never offers one, so only an explicit path could."""
    root = tmp_path / "sessions"
    live = root / "2026-09-20T152758"
    tuning = root / "_tuning-2026-09-19T120000"
    for session in (live, tuning):
        session.mkdir(parents=True)

    def _must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("refused sessions reached a frame build or trainer")

    monkeypatch.setattr(retrain, "build_feature_frame", _must_not_run)
    monkeypatch.setattr(training_entry, "train_isolation_forest", _must_not_run)
    rc = retrain.main(retrain_argv(tmp_path, str(live), str(tuning)))
    assert rc == 2
    err = capsys.readouterr().err
    assert "R03.12" in err
    assert tuning.name in err and live.name not in err.split("R03.12", 1)[1]


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

    monkeypatch.setattr(training_entry, "train_isolation_forest", _broken_if)
    monkeypatch.setattr(training_entry, "train_sequence_detector", _ok_lstm)

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
        )
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

    monkeypatch.setattr(training_entry, "train_isolation_forest", _broken)
    monkeypatch.setattr(training_entry, "train_sequence_detector", _broken)

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
        )
    )
    assert rc == 1


def test_retrain_skip_benchmark_flag_prevents_gate_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    monkeypatch.setattr(training_entry, "train_isolation_forest", lambda *a, **k: None)
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)

    def _boom(*_a: object, **_k: object) -> tuple[None, None]:
        raise AssertionError("benchmark gate must not run with --skip-benchmark")

    monkeypatch.setattr(retrain, "_run_benchmark_gate", _boom)

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
            "--skip-benchmark",
        )
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
    monkeypatch.setattr(training_entry, "train_isolation_forest", lambda *a, **k: None)
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)

    calls: list[object] = []

    def _fake_gate(config: object, calibration_sessions: object, args: object) -> tuple[None, None]:
        calls.append(calibration_sessions)
        return None, None

    monkeypatch.setattr(retrain, "_run_benchmark_gate", _fake_gate)

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
        )
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
    monkeypatch.setattr(training_entry, "train_isolation_forest", lambda *a, **k: None)
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)

    report_path = tmp_path / "analyses" / "benchmark-20260719-120000.json"
    monkeypatch.setattr(
        retrain,
        "_run_benchmark_gate",
        lambda *a, **k: (report_path, {"regressed": True, "tolerance": 0.15, "cells": []}),
    )

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
        )
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
    monkeypatch.setattr(training_entry, "train_isolation_forest", lambda *a, **k: None)
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)

    report_path = tmp_path / "analyses" / "benchmark-20260719-120000.json"
    monkeypatch.setattr(
        retrain,
        "_run_benchmark_gate",
        lambda *a, **k: (report_path, {"regressed": False, "tolerance": 0.15, "cells": []}),
    )

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
        )
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


def test_run_benchmark_gate_skips_cleanly_with_no_evaluation_sessions(
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
    result = retrain._run_benchmark_gate(config, [], retrain._start_run(args, config))

    assert result == (None, None)
    assert "no evaluation holdout" in caplog.text


def test_benchmark_gate_asks_the_harness_for_every_fault_on_a_fixed_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R06.5: the gate is A02's protocol with injections (every realistic
    fault at the harness defaults), a fixed seed so two retrains of one
    holdout inject the same windows, the artifacts this run wrote (the
    sequence one by its own path), and neither baselines nor the shift test."""
    from tests.builders import evaluation_report

    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config.models.lstm_autoencoder.enabled = True
    if_out, seq_out = tmp_path / "if", tmp_path / "seq"
    (if_out / "model.joblib").parent.mkdir(parents=True)
    (if_out / "model.joblib").write_bytes(b"x")
    session = tmp_path / "sessions" / "2026-05-12T000000"
    args = retrain.parse_args(
        [
            "--config",
            "canary.example.yaml",
            "--if-out",
            str(if_out),
            "--sequence-out",
            str(seq_out),
            "--sequence-model",
            "lstm",
            "--analyses-dir",
            str(tmp_path / "analyses"),
            "--no-feature-cache",
        ]
    )
    run = retrain._start_run(args, config)
    seq_artifact = run.train_dir(retrain.SEQUENCE_SOURCE) / "model.pt"
    seq_artifact.parent.mkdir(parents=True)
    seq_artifact.write_bytes(b"x")
    calls: list[list[str]] = []

    def harness(argv: list[str]) -> int:
        calls.append(argv)
        out = Path(argv[argv.index("--out") + 1])
        out.write_text(json.dumps(evaluation_report({}, {"isolation_forest": 0.0})))
        return 0

    monkeypatch.setattr(retrain, "run_evaluate_detectors", harness)
    report_path, diff = retrain._run_benchmark_gate(config, [session], run)

    (argv,) = calls
    assert report_path is not None and diff is None  # nothing prior to compare
    assert argv[0] == str(session)
    assert "--injections-per-drive" not in argv and "--faults" not in argv
    assert argv[argv.index("--seed") + 1] == str(retrain.BENCHMARK_SEED)
    assert argv[argv.index("--baselines") + 1].startswith("--")  # none requested
    assert "--no-shift-test" in argv
    assert argv[argv.index("--if-model") + 1] == str(if_out / "model.joblib")
    assert argv[argv.index("--sequence-model") + 1] == str(seq_artifact)
    assert "--cache-dir" not in argv  # --no-feature-cache: the harness default
    assert json.loads(report_path.read_text())["holdout"]["evaluation"] == [session.name]


@pytest.mark.parametrize(
    ("compute_target", "pi_board", "expected"),
    [(None, False, False), ("pi", False, True), ("laptop", True, True)],
)
def test_the_gates_score_without_the_score_cache_on_the_pi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compute_target: str | None,
    pi_board: bool,
    expected: bool,
) -> None:
    """The harness's score cache and the derived store both refuse on the Pi
    (P01's rules), so a Pi-side retrain passes ``--no-score-cache`` and
    ``--no-derived-store`` to its gates rather than failing them (P02.5)."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    argv = ["--config", "canary.example.yaml", "--if-out", str(tmp_path / "if")]
    if compute_target is not None:
        argv += ["--compute-target", compute_target]
    run = retrain._start_run(retrain.parse_args(argv), config)
    calls: list[list[str]] = []

    def harness(argv: list[str]) -> int:
        calls.append(argv)
        Path(argv[argv.index("--out") + 1]).write_text("{}")
        return 0

    monkeypatch.setattr(retrain, "run_evaluate_detectors", harness)
    monkeypatch.setattr(retrain, "on_the_pi", lambda: pi_board)
    retrain._evaluate_on_drives(
        [tmp_path / "s"],
        run,
        if_model=tmp_path / "m.joblib",
        sequence_model=None,
        out=tmp_path / "r.json",
    )
    (called,) = calls
    assert ("--no-score-cache" in called) is expected
    # The derived store refuses on the Pi as well: the gate reduces in memory
    # there, or every session load would fail and it would score nothing.
    assert ("--no-derived-store" in called) is expected
    assert ("--derived-dir" in called) is not expected


def test_run_benchmark_gate_skips_cleanly_with_missing_if_artifact(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    session = write_fixture_session(tmp_path / "sessions", "2026-05-12T000000")
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
    result = retrain._run_benchmark_gate(config, [session], retrain._start_run(args, config))

    assert result == (None, None)
    assert "no isolation forest artifact" in caplog.text


def test_run_benchmark_gate_runs_the_evaluation_protocol_and_discovers_prior_baseline(
    tmp_path: Path,
) -> None:
    """End-to-end (R06.5): a fixture session + a real trained IF artifact +
    prior benchmark-*.json reports on disk. The gate runs A02's harness
    (scripts/evaluate_detectors.py) in its own process, passes over a
    grid-era report by name, finds the newest comparable report, and a cell
    the baseline graded and this run did not is a regression."""
    from src.features.feature_pipeline import featurize_wide_frame
    from src.models.benchmark_gate import PROTOCOL_NAME
    from src.models.isolation_forest import default_manifest_path, fit_isolation_forest

    config = example_config(EXAMPLE_YAML)
    # A sequence detector disabled in config is not scored (the gate would
    # otherwise look for this run's sequence artifact).
    config.models.lstm_autoencoder.enabled = False

    # Trained on one session, gated on another: the harness skips a drive
    # any scored artifact trained on (A02.1).
    training = write_fixture_session(tmp_path / "train", "2026-05-11T000000", seed=2, n=120)
    session = write_fixture_session(tmp_path / "sessions", "2026-05-12T000000", seed=1, n=120)
    features = featurize_wide_frame(
        load_resampled_session(training, config),
        config,
        behavior_models=None,
        did_map=load_repo_did_map(),
    ).features
    trained = fit_isolation_forest(
        features,
        IFConfig(n_estimators=40),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T00:00:00Z",
    )
    if_out = tmp_path / "if"
    artifact_path = if_out / "model.joblib"
    trained.save(artifact_path, default_manifest_path(artifact_path))

    analyses_dir = tmp_path / "analyses"
    analyses_dir.mkdir(parents=True)
    grid_report = analyses_dir / "benchmark-20260101-000000.json"
    grid_report.write_text(
        json.dumps({"generated_at": "20260101-000000", "table": [], "cells": []})
    )

    args = retrain.parse_args(
        [
            "--config",
            "canary.example.yaml",
            "--profile",
            "nissan-xterra-2008",
            "--if-out",
            str(if_out),
            "--analyses-dir",
            str(analyses_dir),
            "--feature-cache",
            str(tmp_path / "cache" / "feature_frames"),
        ]
    )

    report_path, diff = retrain._run_benchmark_gate(
        config, [session], retrain._start_run(args, config)
    )

    assert report_path is not None and report_path.parent == analyses_dir
    report = json.loads(report_path.read_text())
    assert report["protocol"]["name"] == PROTOCOL_NAME
    assert report["detectors"] == ["isolation_forest"]
    assert [d["sessions"] for d in report["drives"]] == [[session.name]]
    assert report["holdout"]["evaluation"] == [session.name]
    # The harness's derived session frames sit beside the trainers' feature
    # cache (P01.2), in the same store the trainers read.
    assert list((tmp_path / "cache" / "derived_sessions").glob("*/cells-*.parquet"))
    assert diff is not None  # baseline auto-discovered from the report on disk
    # A grid-era report is not a regression baseline: reported, never failed on.
    assert diff["comparable"] is False and diff["regressed"] is False
    assert "retired detector_benchmark grid" in diff["reason"]

    # The same drives under the same protocol are found and compared, even
    # when an incomparable one is newer: newest comparable wins, which is the
    # first run's own report, not the 2099 one or the older copy.
    older = analyses_dir / "benchmark-20260102-000000.json"
    older.write_text(report_path.read_text())
    newer_other = analyses_dir / "benchmark-20990101-000000.json"
    newer_other.write_text(json.dumps({**report, "drives": []}))
    # P02.5: the harness keeps each detector's scores beside the feature
    # cache, so the next gate on the same artifact reads them back.
    scores = tmp_path / "cache" / "evaluation_frames" / "scores"

    def written() -> dict[Path, int]:
        # Inode: an entry rescored is re-written (staged temp file, renamed
        # into place on commit), so an unchanged inode means it was read back,
        # not recomputed. (A hit refreshes the mtime, for LRU pruning.)
        return {p: p.stat().st_ino for p in scores.glob("isolation_forest-*/*/*.pkl")}

    kept = written()
    assert kept
    report_path_2, diff = retrain._run_benchmark_gate(
        config, [session], retrain._start_run(args, config)
    )
    assert written() == kept
    # The alert-budget gate's evaluation (clean rows, same artifact) reads the
    # benchmark gate's session scores back too.
    run = retrain._start_run(args, config)
    retrain._evaluate_on_drives(
        [session],
        run,
        if_model=artifact_path,
        sequence_model=None,
        out=tmp_path / "alert-budget.json",
    )
    assert written() == kept
    assert diff is not None and diff["comparable"] is True
    assert diff["regressed"] is False
    assert diff["baseline"].endswith(report_path.name)

    # A cell the baseline graded and this run did not is a regression.
    assert report_path_2 is not None
    report_path_2.unlink()
    graded = json.loads(report_path.read_text())
    graded["recall"] = {
        "isolation_forest": {"maf_gain": {"0.1": {"graded": 2, "detected": 2, "injections": 2}}}
    }
    report_path.write_text(json.dumps(graded))
    _, diff = retrain._run_benchmark_gate(config, [session], retrain._start_run(args, config))
    assert diff is not None and diff["regressed"] is True
    assert diff["baseline"].endswith(report_path.name)
    (missing,) = [c for c in diff["cells"] if c["status"] == "missing"]
    assert (missing["detector"], missing["fault_kind"]) == ("isolation_forest", "maf_gain")


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
    monkeypatch.setattr(model_evaluation, "session_parquet_bytes", lambda _p: 5_000_000)

    seen: dict[str, object] = {}

    def _seq_train(
        _backend: object, positional_sessions: list[Path], *_a: object, **kwargs: object
    ) -> None:
        seen["sessions"] = list(positional_sessions)
        seen["calibration_sessions"] = kwargs.get("calibration_sessions")

    monkeypatch.setattr(training_entry, "train_isolation_forest", lambda *a, **k: None)
    monkeypatch.setattr(training_entry, "train_sequence_detector", _seq_train)

    rc = retrain.main(
        retrain_argv(
            tmp_path,
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
        )
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

    monkeypatch.setattr(model_evaluation, "session_parquet_bytes", _fake_bytes)

    seen: dict[str, object] = {}
    monkeypatch.setattr(training_entry, "train_isolation_forest", lambda *a, **k: None)
    monkeypatch.setattr(
        training_entry,
        "train_sequence_detector",
        lambda _backend, positional, *a, **k: seen.update(
            sessions=list(positional), calibration_sessions=k.get("calibration_sessions")
        ),
    )

    rc = retrain.main(
        retrain_argv(
            tmp_path,
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
        )
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
    monkeypatch.setattr(model_evaluation, "session_parquet_bytes", lambda _p: 5_000_000)
    monkeypatch.setattr(model_evaluation, "session_mode_coverage", lambda _p: {"city"})
    health = {
        ready: ("ready", 100),
        narrow: ("ready", 50),
        quarantined: ("quarantine", 100),
    }
    monkeypatch.setattr(model_evaluation, "session_holdout_health", health.__getitem__)

    assert model_evaluation.pick_calibration_holdouts(sessions) == [ready]


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
    monkeypatch.setattr(training_entry, "train_isolation_forest", lambda *a, **k: None)
    monkeypatch.setattr(
        training_entry,
        "train_sequence_detector",
        lambda _backend, positional, *a, **k: seen.update(
            sessions=list(positional), calibration_sessions=k.get("calibration_sessions")
        ),
    )

    rc = retrain.main(
        retrain_argv(
            tmp_path,
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
        )
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
    monkeypatch.setattr(model_evaluation, "session_parquet_bytes", lambda _p: 5_000_000)
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
    monkeypatch.setattr(
        model_evaluation, "session_mode_coverage", lambda _p: {"city", "highway", "idle"}
    )
    monkeypatch.setattr(model_evaluation, "session_holdout_health", lambda _p: ("ready", 100))

    picked = model_evaluation.pick_calibration_holdouts(sessions)

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
    monkeypatch.setattr(model_evaluation, "session_mode_coverage", lambda _p: None)
    monkeypatch.setattr(model_evaluation, "session_holdout_health", lambda _p: None)

    (anchor,) = model_evaluation.pick_calibration_holdouts(sorted(sessions), max_holdouts=1)
    assert anchor.name == "_tuning-2026-09-19T120000"


def test_holdout_picker_never_tops_up_with_an_unhealthy_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _healthy_corpus(
        tmp_path,
        monkeypatch,
        ["2026-08-05T120000", "2026-08-06T120000", "2026-09-18T120000", "2026-09-19T120000"],
    )
    monkeypatch.setattr(model_evaluation, "session_mode_coverage", lambda _p: {"city"})
    health = {
        "2026-08-05T120000": ("quarantine", 100),
        "2026-08-06T120000": ("ready", 100),
        "2026-09-18T120000": ("ready", 40),  # narrow schema
        "2026-09-19T120000": ("ready", 100),
    }
    monkeypatch.setattr(model_evaluation, "session_holdout_health", lambda p: health[p.name])

    picked = model_evaluation.pick_calibration_holdouts(sessions)
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
    monkeypatch.setattr(model_evaluation, "session_mode_coverage", lambda _p: {"city"})
    monkeypatch.setattr(model_evaluation, "session_holdout_health", lambda _p: ("ready", 100))

    built: list[list[Path]] = []

    def _build(positional: list[Path], *_a: object, **_k: object) -> object:
        built.append(list(positional))
        return {"frame_for": tuple(p.name for p in positional)}

    monkeypatch.setattr(retrain, "build_feature_frame", _build)
    seen: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(
        training_entry,
        "train_isolation_forest",
        lambda *a, **k: seen.__setitem__("if", dict(k, train=a[0])),
    )
    monkeypatch.setattr(
        training_entry,
        "train_sequence_detector",
        lambda *a, **k: seen.__setitem__("seq", dict(k, train=a[1])),
    )

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
            "--skip-benchmark",
        )
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
    monkeypatch.setattr(
        training_entry, "train_isolation_forest", lambda *a, **k: None
    )  # writes nothing
    monkeypatch.setattr(training_entry, "train_sequence_detector", lambda *a, **k: None)
    report_path = tmp_path / "analyses" / "benchmark-20260920-000000.json"
    monkeypatch.setattr(
        retrain,
        "_run_benchmark_gate",
        lambda *a, **k: (report_path, {"regressed": False, "tolerance": 0.15, "cells": []}),
    )
    _write_manifest(tmp_path / "if", "isolation_forest_DEPLOYED")  # the "deployed" model
    ledger_path = tmp_path / "training_runs.jsonl"

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
            "--ledger",
            str(ledger_path),
            "--no-feedback-policy",
        )
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
        training_entry,
        "train_isolation_forest",
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
        retrain_argv(
            tmp_path,
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
        )
    )
    assert rc == 0
    (row,) = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert row["run_id"] == "isolation_forest_NEW"
    assert row["detector_benchmark_report_path"] == str(report_path)


def test_default_cli_paths_cannot_append_to_the_real_training_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_write_guard: RepoTreeGuard
) -> None:
    """The conftest guard (R07.2): even a trainer that DOES write a fresh
    manifest cannot land a test row in data/control/training_runs.jsonl."""
    from src.models.training_ledger import DEFAULT_TRAINING_LEDGER_PATH

    repo_root = Path(__file__).resolve().parents[1]
    real_ledger = (repo_root / DEFAULT_TRAINING_LEDGER_PATH).resolve()
    assert real_ledger.is_relative_to(repo_root / "data")
    assert retrain.parse_args([]).ledger == DEFAULT_TRAINING_LEDGER_PATH

    _write_manifest(tmp_path / "if", "isolation_forest_NEW")
    before = real_ledger.stat() if real_ledger.exists() else None
    monkeypatch.chdir(repo_root)
    # _ingest_ledger logs and swallows an OSError; the guard still refused it.
    with repo_write_guard.expect() as refused:
        retrain._ingest_ledger(
            retrain.TrainingLedger(DEFAULT_TRAINING_LEDGER_PATH),
            tmp_path / "if",
            "succeeded",
            None,
        )
    # The ledger itself, or -- on a checkout with no data/control yet, since it
    # is gitignored -- the mkdir of its directory; either is the same refusal.
    assert any(str(real_ledger.parent) in what for what in refused), refused
    after = real_ledger.stat() if real_ledger.exists() else None
    assert (before and (before.st_size, before.st_mtime_ns)) == (
        after and (after.st_size, after.st_mtime_ns)
    )


# ---------------------------------------------------------------------------
# A02.1 — calibration and evaluation holdouts are disjoint, by physical drive
# ---------------------------------------------------------------------------


def _day_sessions(root: Path, count: int) -> list[Path]:
    sessions = [root / f"2026-05-{10 + i:02d}T120000" for i in range(count)]
    for session in sessions:
        session.mkdir(parents=True)
    return sessions


def test_a_session_in_both_holdouts_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sessions = _day_sessions(tmp_path / "sessions", 5)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    monkeypatch.setattr(
        training_entry,
        "train_isolation_forest",
        lambda *a, **k: pytest.fail("no trainer may run on an overlapping holdout"),
    )

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
            "--calibration-sessions",
            str(sessions[3]),
            str(sessions[4]),
            "--evaluation-sessions",
            str(sessions[4]),
        )
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "2026-05-14T120000" in err
    assert "both the calibration and the evaluation holdout" in err


def test_gate_scores_evaluation_drives_disjoint_from_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate used to score the calibration sessions themselves (A02.1)."""
    sessions = _day_sessions(tmp_path / "sessions", 8)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    monkeypatch.setattr(model_evaluation, "session_parquet_bytes", lambda _p: 5_000_000)
    seen: dict[str, object] = {}

    def _seq_train(_backend: object, positional: list[Path], *_a: object, **kwargs: object) -> None:
        seen["training"] = list(positional)
        seen["calibration"] = kwargs.get("calibration_sessions")
        seen["evaluation"] = kwargs.get("evaluation_sessions")
        seen["override"] = kwargs.get("calibration_override")

    def _gate(_config: object, evaluation: list[Path], run: object) -> tuple[None, None]:
        seen["gate"] = list(evaluation)
        seen["split"] = run.holdout_split  # type: ignore[attr-defined]
        return None, None

    monkeypatch.setattr(training_entry, "train_isolation_forest", lambda *a, **k: None)
    monkeypatch.setattr(training_entry, "train_sequence_detector", _seq_train)
    monkeypatch.setattr(retrain, "_run_benchmark_gate", _gate)

    rc = retrain.main(
        retrain_argv(
            tmp_path,
            "--config",
            "canary.example.yaml",
            "--sessions-root",
            str(tmp_path / "sessions"),
            "--all",
            "--no-feedback-policy",
            "--if-out",
            str(tmp_path / "if"),
            "--sequence-out",
            str(tmp_path / "lstm"),
            "--accept-thin-calibration",
            "test fixture",
        )
    )

    assert rc == 0
    calibration = set(seen["calibration"])  # type: ignore[arg-type]
    evaluation = set(seen["gate"])  # type: ignore[arg-type]
    training = set(seen["training"])  # type: ignore[arg-type]
    assert calibration and evaluation
    assert not calibration & evaluation
    assert not training & (calibration | evaluation)
    assert set(seen["evaluation"]) == evaluation  # type: ignore[arg-type]
    assert len(training) >= 2
    split = seen["split"]
    assert set(split.evaluation) == {s.name for s in evaluation}  # type: ignore[attr-defined]
    assert seen["override"] == "test fixture"


def test_a_calibration_drive_takes_its_sibling_sessions(tmp_path: Path) -> None:
    """A daemon restart two minutes into a drive leaves two sessions of one
    drive; the second may not train or grade while the first calibrates."""
    root = tmp_path / "sessions"
    first = root / "2026-05-12T120000"
    restart = root / "2026-05-12T120200"
    other = root / "2026-05-13T120000"
    for session in (first, restart, other):
        session.mkdir(parents=True)

    held = model_evaluation.holdout_by_drive([first, restart, other], calibration=[first])
    assert held.calibration == (first, restart)
    assert held.training == (other,)


def _write_span(session: Path, start: str, minutes: float) -> None:
    """A recording answering OBD every 10 s for ``minutes``."""
    session.mkdir(parents=True)
    stamps = pd.date_range(start, periods=int(minutes * 6) + 1, freq="10s", tz="UTC")
    pd.DataFrame({"timestamp": stamps, "source": "obd", "value_number": 1.0}).to_parquet(
        session / "telemetry-00000.parquet"
    )


def test_a_capture_window_keeps_the_sessions_either_side_one_drive(tmp_path: Path) -> None:
    """The training pool never holds captures, but a capture is recorded in
    the middle of a drive. On 2026-09-21 a 20-minute fuel-target capture sat
    between two live sessions of one drive; without it they were 20 minutes
    apart and the retrain split put one on the evaluation side and the other
    in training. The capture bridges the grouping and never joins a side."""
    root = tmp_path / "sessions"
    before = root / "2026-09-21T194247"
    capture = root / "_capture-fuel-target-research-2026-09-21T195844"
    after = root / "2026-09-21T201836"
    other = root / "2026-09-22T163415"
    _write_span(before, "2026-09-21T19:42:47", 15)
    _write_span(capture, "2026-09-21T19:58:44", 20)
    _write_span(after, "2026-09-21T20:18:36", 60)
    _write_span(other, "2026-09-22T16:34:15", 30)

    drives = model_evaluation.drives_of_sessions([before, after, other])
    assert [drive.session_names for drive in drives] == [
        (before.name, after.name),
        (other.name,),
    ]

    held = model_evaluation.holdout_by_drive([before, after, other], evaluation=[before])
    assert held.evaluation == (before, after)
    assert held.training == (other,)
    assert capture not in (*held.training, *held.calibration, *held.evaluation)


def test_calibration_picker_takes_one_session_per_physical_drive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sessions"
    names = [
        "2026-05-10T120000",
        "2026-05-10T120200",  # a restart two minutes into the same drive
        "2026-05-12T120000",
        "2026-05-14T120000",
    ]
    sessions = [root / name for name in names]
    for session in sessions:
        session.mkdir(parents=True)
    monkeypatch.setattr(model_evaluation, "session_parquet_bytes", lambda _p: 5_000_000)

    picked = model_evaluation.pick_calibration_holdouts(sessions, max_holdouts=3)

    # Three drives, not three sessions — and whole drives: the restart comes
    # with its drive rather than staying behind to train.
    assert sorted(picked) == sorted(sessions)


def test_retrain_hands_the_if_trainer_its_calibration_policy_and_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A04.3/A04.4: the IF manifest records what the calibration rows went
    through (quarantine deliberately not applied, and why) and the thin-
    calibration override reason reaches the IF trainer, not only the sequence
    trainer; the quarantine screen's cells reach it so rows lost per mode are
    counted."""
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: object())
    seen: dict[str, object] = {}
    monkeypatch.setattr(training_entry, "train_isolation_forest", lambda *a, **k: seen.update(k))
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
            "--accept-thin-calibration",
            "bench only",
        )
    )
    assert rc == 0
    assert seen["calibration_override"] == "bench only"
    policy = seen["calibration_policy"]
    assert isinstance(policy, dict)
    assert policy["quarantine"]["applied"] is False
    assert "serve-time scoring does not quarantine" in policy["quarantine"]["reason"]
    assert "quarantined_cells" in seen


@pytest.mark.parametrize("skip", ["--skip-sequence", "--skip-if"])
def test_quarantine_exclusion_applies_whichever_trainers_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skip: str
) -> None:
    """2026-09-23: an IF-only retrain skipped the quarantine screen, because the
    shared frame it is applied to was built only when both trainers ran. The
    A06.3 candidate IF trained on 0 quarantined cells where the same corpus
    with both trainers lost 369 of 709 city rows."""
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: [session])
    frame = pd.DataFrame({"row": range(3)})
    monkeypatch.setattr(retrain, "build_feature_frame", lambda *a, **k: frame)
    screened = frame.assign(screened=True)
    monkeypatch.setattr(
        retrain,
        "_apply_quarantine_exclusion",
        lambda f, *_a, **_k: (screened, {"obd:RPM": 1}, {"obd:RPM": pd.Index([0])}),
    )
    seen: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(
        training_entry, "train_isolation_forest", lambda *a, **k: seen.update(if_=k)
    )
    monkeypatch.setattr(
        training_entry, "train_sequence_detector", lambda *a, **k: seen.update(seq=k)
    )

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
            "--skip-benchmark",
            "--skip-disposition-summary",
            skip,
        )
    )
    assert rc == 0
    (kwargs,) = seen.values()
    assert kwargs["feature_frame"] is screened
    if skip == "--skip-sequence":
        assert kwargs["quarantined_cells"] == {"obd:RPM": pd.Index([0])}


def test_feedback_filtered_calibration_frame_goes_to_the_if_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A04.3 verifier finding: the feedback-label policy on the calibration
    rows is recorded in the IF manifest only, so only the IF gets the filtered
    frame; the sequence trainer keeps the unfiltered one it always had."""
    from types import SimpleNamespace

    names = [f"2026-09-{day:02d}T120000" for day in range(1, 8)]
    sessions = _healthy_corpus(tmp_path, monkeypatch, names)
    monkeypatch.setattr(_cli, "discover_sessions", lambda _root: list(sessions))
    monkeypatch.setattr(model_evaluation, "session_mode_coverage", lambda _p: {"city"})
    monkeypatch.setattr(model_evaluation, "session_holdout_health", lambda _p: ("ready", 100))
    built: list[pd.DataFrame] = []

    def _build(positional: list[Path], *_a: object, **_k: object) -> pd.DataFrame:
        frame = pd.DataFrame({"row": range(4)})
        built.append(frame)
        return frame

    monkeypatch.setattr(retrain, "build_feature_frame", _build)

    class _Labels:
        def __init__(self, *_a: object) -> None:
            pass

        def labels(self) -> dict[str, str]:
            return {"event": "true_positive"}

    monkeypatch.setattr(retrain, "FeedbackStore", _Labels)
    report = SimpleNamespace(
        excluded_rows=3,
        excluded_labels=1,
        to_dict=lambda: {},
        clean_baseline_labels=0,
        enriched_labels=0,
    )
    monkeypatch.setattr(
        retrain,
        "apply_retraining_feedback_policy",
        lambda frame, *_a, **_k: (frame.iloc[1:], report),
    )
    seen: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(
        training_entry, "train_isolation_forest", lambda *a, **k: seen.__setitem__("if", k)
    )
    monkeypatch.setattr(
        training_entry, "train_sequence_detector", lambda *a, **k: seen.__setitem__("seq", k)
    )

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
            "--no-ledger",
            "--skip-benchmark",
            # FeedbackStore is stubbed; the path only has to turn the policy on.
            feedback=tmp_path / "feedback.jsonl",
        )
    )
    assert rc == 0
    calibration = built[1]  # training frame first, then the holdout frame
    assert seen["seq"]["calibration_frame"] is calibration
    assert len(calibration) == 4
    filtered = seen["if"]["calibration_frame"]
    assert isinstance(filtered, pd.DataFrame) and filtered["row"].tolist() == [1, 2, 3]
    policy = seen["if"]["calibration_policy"]
    assert isinstance(policy, dict)
    assert policy["feedback_labels"] == {"applied": True, "excluded_rows": 3, "excluded_labels": 1}
    assert "calibration_policy" not in seen["seq"]
