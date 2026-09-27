"""Which columns a sequence model may train on (no torch training here).

The training entry is ``src.models.training_entry.train_sequence_detector``
(R06.4); these tests used to patch ``scripts/train_sequence.py``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from src.models import training_entry
from src.models.sequence_training import ValidationChoice
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import example_config

REPO_ROOT = Path(__file__).resolve().parents[1]


class _StopError(Exception):
    """Raised by the stand-in trainer once it has seen its arguments."""


#: Seen only on the validation drive. It must be a PID the serving ``default``
#: polls: one it does not poll is refused earlier, by the serving gate (D03.34).
VALIDATION_ONLY = "CATALYST_TEMP_B2S1"


def _session(root: Path, name: str, *, start: float, extra: bool) -> Path:
    records: list[TelemetryRecord] = []
    for i in range(90):
        t = start + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 780.0 + (i % 4), "rpm"),
                TelemetryRecord.obd(t, "SPEED", float(i % 5), "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 6.0 + (i % 3), "%"),
            ]
        )
        if extra:
            records.append(TelemetryRecord.obd(t, VALIDATION_ONLY, 400.0 + (i % 7), "degC"))
    write_records_to_parquet(records, root / name, max_rows=10_000)
    return root / name


def test_a_column_seen_only_in_validation_is_not_a_feature_and_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-23: a channel polled only on the validation drive left the
    training rows with no value for it, and the scaler fit raised, killing the
    retrain. Such a column is dropped and recorded in the schema."""
    data = yaml.safe_load((REPO_ROOT / "canary.example.yaml").read_text())
    data["models"]["lstm_autoencoder"].update({"sequence_length": 6, "validation_fraction": 0.3})
    config_path = tmp_path / "canary.yaml"
    config_path.write_text(yaml.safe_dump(data))
    config = example_config(config_path)
    root = tmp_path / "sessions"
    sessions = [
        _session(root, f"s{i}", start=1_778_500_000.0 + 3600 * i, extra=i == 2) for i in range(3)
    ]
    monkeypatch.setattr(
        training_entry,
        "choose_validation_sessions",
        lambda *_a, **_k: ValidationChoice(sessions=("s2",), record={"sessions": ["s2"]}),
    )
    seen: dict[str, Any] = {}

    def fake_trainer(_backend: object, dataset: Any, *_a: object, **kwargs: Any) -> None:
        seen["columns"] = list(dataset.feature_columns)
        seen["spec"] = kwargs["feature_spec"]
        seen["scaler"] = kwargs["scaler"]
        raise _StopError

    monkeypatch.setattr(training_entry, "train_sequence_autoencoder", fake_trainer)

    with pytest.raises(_StopError):
        training_entry.train_sequence_detector(
            training_entry.SEQUENCE_BACKBONES["lstm"],
            sessions,
            config,
            tmp_path / "out",
            bindings_path=tmp_path / "no-bindings.yaml",
        )

    dropped = list(seen["spec"]["unobserved_in_training"])
    assert dropped and all(c.startswith(f"obd:{VALIDATION_ONLY}") for c in dropped)
    assert not any(c.startswith(f"obd:{VALIDATION_ONLY}") for c in seen["columns"])
    assert "obd:RPM" in seen["columns"]


def _parked_session(root: Path, name: str, *, start: float) -> Path:
    """5 min polling OBD, 20 min parked (GPS only, every 30 s), 5 min polling."""
    records: list[TelemetryRecord] = []
    for i in range(30 * 60):
        t = start + i
        if 5 * 60 < i < 25 * 60:
            if i % 30 == 0:
                records.append(TelemetryRecord.gps(t, "speed", 3.0, "km/h"))
            continue
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 780.0 + (i % 4), "rpm"),
                TelemetryRecord.obd(t, "SPEED", float(i % 5), "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 6.0 + (i % 3), "%"),
            ]
        )
    write_records_to_parquet(records, root / name, max_rows=100_000)
    return root / name


def test_parked_rows_neither_train_nor_validate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-23: the r2 sequence candidate validated on a parked tail the
    segmenter called ``city`` from GPS drift. Rows no vehicle activity lies
    near are dropped at the training entry, before the validation split."""
    from src.preprocessing.drives import DRIVE_COLUMN

    data = yaml.safe_load((REPO_ROOT / "canary.example.yaml").read_text())
    data["models"]["lstm_autoencoder"].update({"sequence_length": 6, "validation_fraction": 0.3})
    config_path = tmp_path / "canary.yaml"
    config_path.write_text(yaml.safe_dump(data))
    config = example_config(config_path)
    root = tmp_path / "sessions"
    sessions = [
        _parked_session(root, f"2026-08-0{i + 1}T120000", start=1_785_600_000.0 + 86_400 * i)
        for i in range(3)
    ]
    real_choice = training_entry.choose_validation_sessions
    seen: dict[str, Any] = {}

    def spy_choice(frame: Any, **kwargs: Any) -> Any:
        seen["validation_frame"] = frame
        return real_choice(frame, **kwargs)

    def fake_trainer(_backend: object, dataset: Any, *_a: object, **kwargs: Any) -> None:
        seen["dataset_frame"] = dataset.frame
        seen["spec"] = kwargs["feature_spec"]
        raise _StopError

    monkeypatch.setattr(training_entry, "choose_validation_sessions", spy_choice)
    monkeypatch.setattr(training_entry, "train_sequence_autoencoder", fake_trainer)

    with pytest.raises(_StopError):
        training_entry.train_sequence_detector(
            training_entry.SEQUENCE_BACKBONES["lstm"],
            sessions,
            config,
            tmp_path / "out",
            bindings_path=tmp_path / "no-bindings.yaml",
        )

    assert seen["spec"]["rows_without_physical_drive"] > 0
    for key in ("validation_frame", "dataset_frame"):
        frame = seen[key]
        assert DRIVE_COLUMN in frame.columns
        assert frame[DRIVE_COLUMN].notna().all(), key
        # Each recording kept both of its active stretches, two drives apiece.
        assert frame[DRIVE_COLUMN].nunique() == 6, key


def test_a_tail_block_validation_never_moves_the_scaler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no drive eligible for validation (A03.5), validation is the tail
    of the windows. The scaler used to be fit on every row then, and the A03.4
    leak check refused the r3 retrain (2026-09-24). The scaler is now fit on
    the rows before the tail, and says where it stopped per session."""
    from src.models.sequence_training import validation_split

    data = yaml.safe_load((REPO_ROOT / "canary.example.yaml").read_text())
    data["models"]["lstm_autoencoder"].update(
        {"sequence_length": 6, "validation_fraction": 0.25, "training_stride": 1}
    )
    config_path = tmp_path / "canary.yaml"
    config_path.write_text(yaml.safe_dump(data))
    config = example_config(config_path)
    root = tmp_path / "sessions"
    session = _session(root, "2026-08-01T120000", start=1_785_600_000.0, extra=False)
    seen: dict[str, Any] = {}

    def fake_trainer(_backend: object, dataset: Any, *_a: object, **kwargs: Any) -> None:
        split = validation_split(
            dataset,
            validation_fraction=0.25,
            random_state=0,
            choice=kwargs["validation_choice"],
        )
        seen.update(split=split, dataset=dataset, **kwargs)
        raise _StopError

    monkeypatch.setattr(training_entry, "train_sequence_autoencoder", fake_trainer)
    with pytest.raises(_StopError):
        training_entry.train_sequence_detector(
            training_entry.SEQUENCE_BACKBONES["lstm"],
            [session],
            config,
            tmp_path / "out",
            bindings_path=tmp_path / "no-bindings.yaml",
        )

    split, dataset = seen["split"], seen["dataset"]
    assert split.record["fallback"] == "tail_block"
    until = pd.Timestamp(seen["scaler_fit_until"][session.name])
    assert split.validation_indexes
    for i in split.validation_indexes:
        assert pd.Timestamp(dataset.windows[i].start_timestamp) > until


#: Read once a minute, like the Xterra ``default``'s Mode 22 DIDs (D03.34). A
#: served PID, so the serving gate does not refuse it first.
ONCE_A_MINUTE = "CATALYST_TEMP_B1S1"


def _once_a_minute_session(root: Path, name: str, *, start: float) -> Path:
    records: list[TelemetryRecord] = []
    for i in range(240):
        t = start + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 780.0 + (i % 4), "rpm"),
                TelemetryRecord.obd(t, "SPEED", float(i % 5), "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 6.0 + (i % 3), "%"),
            ]
        )
        if i % 60 == 0:
            records.append(TelemetryRecord.obd(t, ONCE_A_MINUTE, 400.0 + i / 10, "degC"))
    write_records_to_parquet(records, root / name, max_rows=10_000)
    return root / name


def test_both_trainers_exclude_a_once_a_minute_channel_and_name_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D03.34: a channel read once per ~60 s is too sparse for the 1 Hz grid.
    Neither detector trains on it, and each manifest names it under the same
    reason; the 1 Hz channels are kept by both."""
    from src.preprocessing.feature_pipeline import COVERAGE_FLOOR_SCHEMA_KEY, SPARSE_FOR_GRID

    data = yaml.safe_load((REPO_ROOT / "canary.example.yaml").read_text())
    data["models"]["lstm_autoencoder"].update({"sequence_length": 6, "validation_fraction": 0.3})
    config_path = tmp_path / "canary.yaml"
    config_path.write_text(yaml.safe_dump(data))
    config = example_config(config_path)
    root = tmp_path / "sessions"
    sessions = [
        _once_a_minute_session(root, f"2026-08-0{i + 1}T120000", start=1_785_600_000.0 + 86_400 * i)
        for i in range(3)
    ]
    seen: dict[str, dict[str, Any]] = {}

    def fake_sequence(_backend: object, dataset: Any, *_a: object, **kwargs: Any) -> None:
        seen["sequence"] = {
            "columns": list(dataset.feature_columns),
            "spec": kwargs["feature_spec"],
        }
        raise _StopError

    def fake_if(_features: object, _cfg: object, **kwargs: Any) -> None:
        seen["isolation_forest"] = {
            "columns": list(kwargs["feature_columns"]),
            "spec": kwargs["feature_spec"],
        }
        raise _StopError

    monkeypatch.setattr(training_entry, "train_sequence_autoencoder", fake_sequence)
    monkeypatch.setattr(training_entry, "fit_isolation_forest", fake_if)
    bindings = tmp_path / "no-bindings.yaml"
    with pytest.raises(_StopError):
        training_entry.train_sequence_detector(
            training_entry.SEQUENCE_BACKBONES["lstm"],
            sessions,
            config,
            tmp_path / "out",
            bindings_path=bindings,
        )
    with pytest.raises(_StopError):
        training_entry.train_isolation_forest(
            sessions, config, tmp_path / "out-if", bindings_path=bindings
        )

    sparse = f"obd:{ONCE_A_MINUTE}"
    for detector, got in seen.items():
        assert "obd:RPM" in got["columns"], detector
        # The read and its rolling siblings (the 30 s __mean holds it for
        # half the rows) are all out, each named against the channel.
        family = [c for c in got["spec"]["feature_health"]["columns"] if c.startswith(sparse)]
        assert not family, detector
        assert not any(c.startswith(sparse) for c in got["columns"]), detector
        record = got["spec"][COVERAGE_FLOOR_SCHEMA_KEY]
        assert record["min_coverage"] == 0.5
        excluded = record["columns"]
        assert {sparse, f"{sparse}__mean"} <= set(excluded), detector
        for column in (sparse, f"{sparse}__mean"):
            assert excluded[column]["reason"] == SPARSE_FOR_GRID, detector
            assert excluded[column]["coverage"] < 0.1, detector
            assert excluded[column]["measured_on"] == sparse, detector
        assert "obd:RPM" not in excluded, detector
    assert set(seen) == {"sequence", "isolation_forest"}
