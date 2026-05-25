"""Tests for the retrain CLI (Task 5.17)."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

from src.alerts.feedback import FeedbackStore

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import retrain  # noqa: E402


def test_retrain_calls_both_trainers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = tmp_path / "sessions" / "2026-05-13T120000"
    session.mkdir(parents=True)
    monkeypatch.setattr(retrain, "discover_sessions", lambda _root: [session])

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
    monkeypatch.setattr(retrain, "discover_sessions", lambda _root: [session])
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
    monkeypatch.setattr(retrain, "discover_sessions", lambda _root: [session])

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
    monkeypatch.setattr(retrain, "discover_sessions", lambda _root: [session])

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
    monkeypatch.setattr(retrain, "discover_sessions", lambda _root: [session])

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


def test_retrain_both_skip_returns_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retrain, "discover_sessions", lambda _root: [tmp_path])
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
    monkeypatch.setattr(retrain, "discover_sessions", lambda _root: [])
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
    monkeypatch.setattr(retrain, "discover_sessions", lambda _root: [session])
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
    monkeypatch.setattr(retrain, "discover_sessions", lambda _root: [session])
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
