"""Tests for the LSTM-vs-TCN sequence-detector bake-off (Phase 9.7)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from scripts.sequence_bakeoff import (
    BackboneResult,
    _auto_carve_calibration_sessions,
    channel_rate_class,
    main,
    run_bakeoff,
)
from src.config import CanaryConfig
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

# Same module object scripts/sequence_bakeoff.py's bare `import retrain` binds —
# needed to monkeypatch its byte-size gate (matches test_retrain_script.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import retrain  # noqa: E402

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
    assert channel_rate_class("did:ECM_AIR_CHARGE") == "slow"
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
    calib_session = _write_obd_session(tmp_path, "calib", rows=120, start_epoch=1_778_550_000.0)
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
            "--calibration-sessions",
            str(calib_session),
            # One fixture calibration session: refused without this (A02.2).
            "--accept-thin-calibration",
            "test fixture: one calibration session",
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


def test_main_errors_without_calibration_sessions_and_too_few_to_auto_carve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Task 3.3: a default invocation (no --calibration-sessions) with too
    small a corpus to auto-carve a holdout must fail loudly rather than
    silently calibrate in-sample."""
    _torch()
    config_path = _write_config(tmp_path)
    train_session = _write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    held_out = _write_obd_session(tmp_path, "holdout", rows=120, start_epoch=1_778_600_000.0)

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
        ]
    )

    assert exit_code == 1
    assert "calibration" in capsys.readouterr().err


def test_main_auto_carves_calibration_sessions_when_corpus_is_large_enough(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 3.3: with enough sessions, a default invocation auto-carves a
    held-out calibration split instead of erroring or calibrating in-sample."""
    _torch()
    # Stub session sizes so each looks "substantial" without writing 1MB+ of
    # parquet — mirrors test_retrain_script.py's auto-holdout tests.
    monkeypatch.setattr(retrain, "_session_parquet_bytes", lambda _p: 5_000_000)
    config_path = _write_config(tmp_path)
    sessions = [
        _write_obd_session(tmp_path, f"train{i}", rows=40, start_epoch=1_778_500_000.0 + i * 1000)
        for i in range(4)
    ]
    held_out = _write_obd_session(tmp_path, "holdout", rows=120, start_epoch=1_778_600_000.0)

    exit_code = main(
        [
            *[str(s) for s in sessions],
            "--config",
            str(config_path),
            "--bindings",
            str(_no_bindings(tmp_path)),
            "--out",
            str(tmp_path / "bakeoff"),
            "--evaluate",
            str(held_out),
            # Two fixture sessions carved for calibration: refused without this.
            "--accept-thin-calibration",
            "test fixture: small corpus",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "SEQUENCE-DETECTOR BAKE-OFF" in out


def test_auto_carve_calibration_sessions_returns_empty_below_minimum(tmp_path: Path) -> None:
    sessions = [tmp_path / "a", tmp_path / "b"]
    assert _auto_carve_calibration_sessions(sessions) == []


def test_bakeoff_holdouts_take_their_whole_drive(tmp_path: Path) -> None:
    """A02.1: a calibration session's restart sibling may not train, and a
    drive may not sit on the calibration and evaluation sides at once."""
    from argparse import Namespace

    from scripts.sequence_bakeoff import _resolve_training_sessions

    root = tmp_path / "sessions"
    root.mkdir()
    calib = _write_obd_session(root, "calib", rows=40, start_epoch=1_778_600_000.0)
    restart = _write_obd_session(root, "calib-restart", rows=40, start_epoch=1_778_600_060.0)
    train = _write_obd_session(root, "train", rows=40, start_epoch=1_778_500_000.0)
    held_out = _write_obd_session(root, "holdout", rows=40, start_epoch=1_778_700_000.0)

    args = Namespace(
        sessions=[train, calib, restart],
        sessions_root=root,
        evaluate=[held_out],
        calibration_sessions=[calib],
    )
    assert _resolve_training_sessions(args) == [train]
    assert args.calibration_sessions == [calib, restart]

    overlapping = Namespace(
        sessions=[train, calib, restart],
        sessions_root=root,
        evaluate=[restart],
        calibration_sessions=[calib],
    )
    with pytest.raises(ValueError, match="both the calibration and the evaluation"):
        _resolve_training_sessions(overlapping)

    # A03.6: holding out every given session is refused with the cause.
    everything_held = Namespace(
        sessions=[calib, restart],
        sessions_root=root,
        evaluate=[held_out],
        calibration_sessions=[calib],
    )
    with pytest.raises(ValueError, match="nothing left to train on"):
        _resolve_training_sessions(everything_held)
