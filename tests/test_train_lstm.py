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
from src.preprocessing.feature_pipeline import build_feature_frame
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.builders import write_obd_session

# Torch training: not a commit gate (see the pyproject test tiers).
pytestmark = [pytest.mark.slow, pytest.mark.ml_training]

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


def test_build_dataset_discovers_feature_columns(tmp_path: Path) -> None:
    config = _config(tmp_path)
    session = write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)

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
    session = write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
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


def test_train_records_the_suppress_list_it_ran_with(tmp_path: Path) -> None:
    """A01.5: the manifest carries the suppress list the schema was cut with,
    so the daemon can name a serve-time list that differs."""
    _torch()
    config = _config(tmp_path, suppress_channels=["obd:THROTTLE_POS"])
    session = write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    out_dir = tmp_path / "models" / "lstm"

    trained = train([session], config, out_dir, bindings_path=_no_bindings(tmp_path))

    payload = json.loads((out_dir / "model.manifest.json").read_text())
    record = payload["schema"]["suppress_channels"]
    assert record["configured"] == ["obd:THROTTLE_POS"]
    assert "obd:THROTTLE_POS" in record["excluded_columns"]
    assert not set(record["excluded_columns"]) & set(trained.feature_columns)

    from src.models.inference import SequenceInferenceEngine

    same = SequenceInferenceEngine(trained, suppress_channels=("obd:THROTTLE_POS",))
    none = SequenceInferenceEngine(trained, suppress_channels=())
    assert same.suppression.differs is False
    assert none.suppression.differs is True
    assert none.suppression.dropped_at_serve == ("obd:THROTTLE_POS",)


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


def test_evaluate_returns_none_when_held_out_too_short(tmp_path: Path) -> None:
    _torch()
    config = _config(tmp_path)
    train_session = write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    short = write_obd_session(tmp_path, "short", rows=4, start_epoch=1_778_600_000.0)
    trained = train([train_session], config, tmp_path / "m", bindings_path=_no_bindings(tmp_path))

    assert evaluate(trained, [short], config, bindings_path=_no_bindings(tmp_path)) is None


def test_main_trains_and_evaluates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _torch()
    config_path = _write_config(tmp_path)
    train_session = write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    held_out = write_obd_session(tmp_path, "holdout", rows=120, start_epoch=1_778_600_000.0)
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


def test_train_calibrates_on_the_pooled_multi_session_holdout_frame(tmp_path: Path) -> None:
    """The sequence detector calibrates on the SAME pooled holdout frame the IF
    gets (scripts/retrain.py builds it once) — every held-out session, not one."""
    _torch()
    config = _config(tmp_path)
    no_bindings = _no_bindings(tmp_path)
    training = write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    holdouts = [
        write_obd_session(tmp_path, f"held-{i}", rows=40, start_epoch=1_778_600_000.0 + 1000 * i)
        for i in range(3)
    ]
    pooled = build_feature_frame(holdouts, config, bindings_path=no_bindings)

    trained = train(
        [training],
        config,
        tmp_path / "models" / "lstm",
        bindings_path=no_bindings,
        calibration_sessions=holdouts,
        calibration_frame=pooled,
    )

    manifest = trained.manifest
    assert manifest.calibration == "held_out"
    assert set(manifest.calibration_sessions) == {"held-0", "held-1", "held-2"}
    assert manifest.calibration_holdout is not None
    assert manifest.calibration_holdout["below_recommended_minimum"] is False
    assert manifest.calibration_holdout["sessions_without_windows"] == []
    assert "train" not in manifest.calibration_sessions


def test_train_refuses_a_single_holdout_unless_the_override_is_stamped(tmp_path: Path) -> None:
    """A02.2: a one-drive calibration used to publish with a WARNING (the
    2026-09-20 artifact). It is refused before training now, and publishes only
    under an explicit operator override that the manifest records."""
    from src.models.sequence_autoencoder_training import CalibrationCoverageError

    _torch()
    config = _config(tmp_path)
    no_bindings = _no_bindings(tmp_path)
    training = write_obd_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    held = write_obd_session(tmp_path, "held", rows=40, start_epoch=1_778_600_000.0)

    with pytest.raises(CalibrationCoverageError, match="1 physical drive"):
        train(
            [training],
            config,
            tmp_path / "models" / "lstm",
            bindings_path=no_bindings,
            calibration_sessions=[held],
        )
    assert not (tmp_path / "models" / "lstm" / "model.pt").exists()

    trained = train(
        [training],
        config,
        tmp_path / "models" / "lstm",
        bindings_path=no_bindings,
        calibration_sessions=[held],
        calibration_override="test: the only held-out drive",
    )

    report = trained.manifest.calibration_holdout
    assert report is not None
    assert report["sessions_used"] == ["held"]
    assert report["below_recommended_minimum"] is True
    assert report["override"]["reason"] == "test: the only held-out drive"
    payload = json.loads((tmp_path / "models" / "lstm" / "model.manifest.json").read_text())
    assert payload["calibration_holdout"]["override"]["violations"]


def test_calibration_and_evaluate_sessions_take_their_whole_drive(tmp_path: Path) -> None:
    """A02.1: ``--calibration-sessions``/``--evaluate`` expand to every session
    of the same physical drive; a drive on both sides is refused; holding
    everything out leaves nothing to train on rather than training on it all."""
    from scripts.train_sequence import _resolve_holdout

    root = tmp_path / "sessions"
    root.mkdir()
    train_session = write_obd_session(root, "a", rows=10, start_epoch=1_778_500_000.0)
    calib = write_obd_session(root, "b", rows=10, start_epoch=1_778_600_000.0)
    restart = write_obd_session(root, "b2", rows=10, start_epoch=1_778_600_060.0)
    held_out = write_obd_session(root, "c", rows=10, start_epoch=1_778_700_000.0)

    holdout = _resolve_holdout(
        SimpleNamespace(
            sessions=[], sessions_root=root, evaluate=[held_out], calibration_sessions=[calib]
        )
    )
    assert holdout is not None
    assert [p.name for p in holdout.training] == ["a"]
    assert [p.name for p in holdout.calibration] == ["b", "b2"]
    assert [p.name for p in holdout.evaluation] == ["c"]

    with pytest.raises(ValueError, match="both the calibration and the evaluation"):
        _resolve_holdout(
            SimpleNamespace(
                sessions=[], sessions_root=root, evaluate=[restart], calibration_sessions=[calib]
            )
        )

    assert (
        _resolve_training_sessions(
            SimpleNamespace(
                sessions=[train_session],
                sessions_root=root,
                evaluate=None,
                calibration_sessions=[train_session],
            )
        )
        is None
    )


def _write_shifted_session(
    root: Path, name: str, *, rows: int, start_epoch: float, rpm_offset: float = 0.0
) -> Path:
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(rows):
        t = start_epoch + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 780.0 + rpm_offset + (i % 4), "rpm"),
                TelemetryRecord.obd(t, "SPEED", float(i % 5), "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 6.0 + (i % 3), "%"),
                TelemetryRecord.obd(t, "BAROMETRIC_PRESSURE", 80.0 + (i % 2), "kPa"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_validation_rows_do_not_move_the_scaler(tmp_path: Path) -> None:
    """A03.4/A03.5: validation drives are fixed by the stated rule *before* the
    scaler is fit, and the scaler is fit on the training rows only — changing
    every value in the validation drive leaves it unchanged."""
    _torch()
    config = _config(tmp_path, validation_fraction=0.25)
    no_bindings = _no_bindings(tmp_path)

    def corpus(root: Path, offset: float) -> list[Path]:
        root.mkdir()
        return [
            _write_shifted_session(
                root,
                f"s{i}",
                rows=120,
                start_epoch=1_778_500_000.0 + 3600 * i,
                rpm_offset=offset if i == 0 else 0.0,
            )
            for i in range(4)
        ]

    manifests = []
    for label, offset in (("a", 0.0), ("b", 5_000.0)):
        sessions = corpus(tmp_path / label, offset)
        trained = train(sessions, config, tmp_path / label / "m", bindings_path=no_bindings)
        manifests.append(trained.manifest)

    first, second = manifests
    holdout = first.validation_holdout
    assert holdout is not None
    assert holdout["rule"] == "mode_coverage_by_drive_v2"
    assert holdout["scaler_fit_excludes_validation"] is True
    assert second.validation_holdout is not None
    assert holdout["sessions"] == second.validation_holdout["sessions"]
    assert first.scaler is not None and second.scaler is not None
    if "s0" in holdout["sessions"]:
        # The shifted session validated: the scaler never saw it.
        assert first.scaler == second.scaler
    else:
        # The shifted session trained: the scaler moved with it (the check can fail).
        assert first.scaler != second.scaler
    # Either way the manifest's scaler is the training rows' scaler.
    features = build_feature_frame(
        sorted((tmp_path / "a").glob("s*")), config, bindings_path=no_bindings
    )
    from src.preprocessing.normalization import ZScoreScaler

    columns = list(first.schema["feature_columns"])
    expected = ZScoreScaler.fit(
        features.loc[~features["session"].isin(holdout["sessions"])], columns=columns
    )
    assert first.scaler["means"] == pytest.approx(expected.to_dict()["means"])


def test_the_manifest_records_the_mask_rule_and_the_training_envelope(tmp_path: Path) -> None:
    """A03.1: new training records ``arrived_sample``; A03.8: per-mode hours and
    baro/speed ranges of what the model was fit on, beside the calibration's."""
    _torch()
    config = _config(tmp_path)
    no_bindings = _no_bindings(tmp_path)
    training = _write_shifted_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    holdouts = [
        _write_shifted_session(
            tmp_path, f"held-{i}", rows=60, start_epoch=1_778_600_000.0 + 1000 * i
        )
        for i in range(3)
    ]
    trained = train(
        [training],
        config,
        tmp_path / "m",
        bindings_path=no_bindings,
        calibration_sessions=holdouts,
    )
    manifest = trained.manifest
    assert manifest.schema["observation_mask"]["rule"] == "arrived_sample"
    envelope = manifest.training_envelope
    assert envelope is not None
    assert envelope["sessions"] == ["train"]
    (mode, record), *_ = envelope["modes"].items()
    assert record["hours"] > 0
    baro = record["quantities"]["baro_kpa"]
    assert (baro["min"], baro["max"]) == (80.0, 81.0)
    assert manifest.calibration_envelope is not None
    assert set(manifest.calibration_envelope["sessions"]) == {"held-0", "held-1", "held-2"}
    # The engine serves this artifact with the arrival mask.
    from src.models.inference import SequenceInferenceEngine

    assert SequenceInferenceEngine(trained).observation_mask == "arrived_sample"


def test_a_calibration_outside_the_training_envelope_is_refused(tmp_path: Path) -> None:
    """A02.2 wired to A03.8: three held-out drives at 63 kPa cannot calibrate a
    model fit at 80 kPa, and the refusal says so in those units."""
    from src.models.sequence_autoencoder_training import CalibrationCoverageError

    _torch()
    config = _config(tmp_path)
    no_bindings = _no_bindings(tmp_path)
    training = _write_shifted_session(tmp_path, "train", rows=120, start_epoch=1_778_500_000.0)
    holdouts = []
    for i in range(3):
        session = tmp_path / f"alt-{i}"
        records = []
        for k in range(60):
            t = 1_778_600_000.0 + 1000 * i + k
            records.extend(
                [
                    TelemetryRecord.obd(t, "RPM", 780.0 + (k % 4), "rpm"),
                    TelemetryRecord.obd(t, "SPEED", float(k % 5), "km/h"),
                    TelemetryRecord.obd(t, "THROTTLE_POS", 6.0 + (k % 3), "%"),
                    TelemetryRecord.obd(t, "BAROMETRIC_PRESSURE", 63.0, "kPa"),
                ]
            )
        write_records_to_parquet(records, session, max_rows=10_000)
        holdouts.append(session)

    with pytest.raises(CalibrationCoverageError, match="baro_kpa spans 63–63, covering 0%"):
        train(
            [training],
            config,
            tmp_path / "m",
            bindings_path=no_bindings,
            calibration_sessions=holdouts,
        )


def test_main_refuses_when_every_explicit_session_is_held_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A03.6: explicit positional sessions lose their held-out drives too, and
    an empty remainder is refused with the cause — not trained, and not
    reported as "none found"."""
    config_path = _write_config(tmp_path)
    only = write_obd_session(tmp_path, "only", rows=20, start_epoch=1_778_500_000.0)
    restart = write_obd_session(tmp_path, "restart", rows=20, start_epoch=1_778_500_060.0)

    exit_code = main(
        [
            str(only),
            str(restart),
            "--config",
            str(config_path),
            "--bindings",
            str(_no_bindings(tmp_path)),
            "--out",
            str(tmp_path / "m"),
            "--calibration-sessions",
            str(restart),
        ]
    )

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "nothing left to train on" in err
    assert "only, restart" in err
    assert not (tmp_path / "m" / "model.pt").exists()
