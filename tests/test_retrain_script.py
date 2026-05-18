"""Tests for the retrain CLI (Task 5.17)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import retrain  # noqa: E402


def test_retrain_calls_both_trainers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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
            "--if-out",
            str(tmp_path / "if"),
            "--lstm-out",
            str(tmp_path / "lstm"),
        ]
    )
    assert rc == 0
    assert calls == ["if", "lstm"]
    out = capsys.readouterr().out
    assert "isolation_forest" in out
    assert "lstm_autoencoder" in out
    assert "✓" in out


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
        ]
    )
    assert rc == 0
    assert calls == ["lstm"]


def test_retrain_skip_lstm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
            "--skip-lstm",
        ]
    )
    assert rc == 0
    assert calls == ["if"]


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
            "--skip-lstm",
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
