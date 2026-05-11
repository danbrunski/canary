"""Tests for sequence-window dataset preparation."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import CanaryConfig
from src.models.sequence_dataset import (
    MissingSequenceDependencyError,
    SequenceWindowDataset,
    build_sequence_windows,
    resolve_gap_columns,
    resolve_sequence_feature_columns,
    sequence_frame_from_sessions,
)
from src.preprocessing.resampler import GAP_SUFFIX, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["B"] * 4 + ["A"] * 5,
            TIMESTAMP_COLUMN: pd.to_datetime(
                [
                    "2026-05-11T00:00:01Z",
                    "2026-05-11T00:00:00Z",
                    "2026-05-11T00:00:03Z",
                    "2026-05-11T00:00:02Z",
                    "2026-05-11T00:00:00Z",
                    "2026-05-11T00:00:01Z",
                    "2026-05-11T00:00:02Z",
                    "2026-05-11T00:00:03Z",
                    "2026-05-11T00:00:04Z",
                ],
                utc=True,
            ),
            "rpm": [11.0, 10.0, 13.0, 12.0, 0.0, 1.0, 2.0, 3.0, 4.0],
            "speed": [21.0, 20.0, 23.0, 22.0, 30.0, 31.0, 32.0, 33.0, 34.0],
            f"rpm{GAP_SUFFIX}": [False] * 9,
            MODE_COLUMN: ["city"] * 4 + ["idle"] * 5,
        }
    )


def _config() -> CanaryConfig:
    return CanaryConfig.from_yaml(EXAMPLE_YAML)


def test_resolve_sequence_feature_columns_picks_numeric_non_gap_columns() -> None:
    frame = _frame()
    frame["notes"] = ["x"] * len(frame)

    columns = resolve_sequence_feature_columns(frame)

    assert columns == ("rpm", "speed")


def test_build_sequence_windows_sorts_and_does_not_cross_sessions() -> None:
    result = build_sequence_windows(_frame(), sequence_length=3)

    assert result.feature_columns == ("rpm", "speed")
    assert len(result.windows) == 5  # A has 3 windows; B has 2 windows.
    assert [window.session for window in result.windows] == ["A", "A", "A", "B", "B"]
    assert [window.start_row for window in result.windows] == [0, 1, 2, 5, 6]
    assert [window.end_row for window in result.windows] == [3, 4, 5, 8, 9]
    assert result.windows[0].start_timestamp == "2026-05-11T00:00:00Z"
    assert result.windows[0].end_timestamp == "2026-05-11T00:00:02Z"


def test_build_sequence_windows_applies_stride() -> None:
    result = build_sequence_windows(_frame(), sequence_length=3, stride=2)

    assert [window.start_row for window in result.windows] == [0, 2, 5]


def test_build_sequence_windows_filters_gap_flag_windows() -> None:
    frame = _frame()
    mask = frame[SESSION_COLUMN].eq("A") & frame[TIMESTAMP_COLUMN].dt.second.eq(2)
    frame.loc[mask, f"rpm{GAP_SUFFIX}"] = True

    result = build_sequence_windows(frame, sequence_length=3)

    # All three A windows contain row t=2, so only B's two clean windows remain.
    assert [window.session for window in result.windows] == ["B", "B"]


def test_build_sequence_windows_filters_missing_feature_windows() -> None:
    frame = _frame()
    mask = frame[SESSION_COLUMN].eq("A") & frame[TIMESTAMP_COLUMN].dt.second.eq(2)
    frame.loc[mask, "rpm"] = np.nan

    result = build_sequence_windows(frame, sequence_length=3)

    assert [window.session for window in result.windows] == ["B", "B"]


def test_build_sequence_windows_honors_explicit_gap_columns() -> None:
    frame = _frame()
    frame["manual_gap"] = False
    mask = frame[SESSION_COLUMN].eq("B") & frame[TIMESTAMP_COLUMN].dt.second.eq(1)
    frame.loc[mask, "manual_gap"] = True

    result = build_sequence_windows(
        frame,
        sequence_length=3,
        gap_columns=["manual_gap"],
    )

    assert result.feature_columns == ("rpm", "speed")
    assert [window.session for window in result.windows] == ["A", "A", "A"]
    assert resolve_gap_columns(frame, ["manual_gap"]) == ("manual_gap",)


def test_build_sequence_windows_rejects_invalid_parameters() -> None:
    with pytest.raises(ValueError, match="sequence_length"):
        build_sequence_windows(_frame(), sequence_length=0)
    with pytest.raises(ValueError, match="stride"):
        build_sequence_windows(_frame(), sequence_length=3, stride=0)


def test_sequence_window_dataset_exposes_metadata_without_torch() -> None:
    dataset = SequenceWindowDataset(
        _frame(),
        sequence_length=3,
        feature_columns=["rpm", "speed"],
    )

    assert len(dataset) == 5
    assert dataset.feature_columns == ("rpm", "speed")
    assert dataset.window_metadata(0).to_dict()["session"] == "A"


def test_sequence_window_dataset_getitem_dependency_error_without_torch() -> None:
    if _torch_installed():
        pytest.skip("torch installed")
    dataset = SequenceWindowDataset(_frame(), sequence_length=3, feature_columns=["rpm"])

    with pytest.raises(MissingSequenceDependencyError, match="torch is required"):
        dataset[0]


def test_sequence_window_dataset_returns_tensor_when_torch_is_installed() -> None:
    torch = pytest.importorskip("torch")
    dataset = SequenceWindowDataset(
        _frame(),
        sequence_length=3,
        feature_columns=["rpm", "speed"],
    )

    sample = dataset[0]

    assert isinstance(sample, torch.Tensor)
    assert tuple(sample.shape) == (3, 2)


def test_sequence_frame_from_sessions_runs_preprocessing_pipeline(tmp_path: Path) -> None:
    session = tmp_path / "session-a"
    records: list[TelemetryRecord] = []
    for i in range(8):
        t = 1_778_500_000.0 + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 800.0 + i, "rpm"),
                TelemetryRecord.obd(t, "SPEED", 0.0, "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 5.0, "%"),
            ]
        )
    write_records_to_parquet(records, session)

    frame = sequence_frame_from_sessions([session], _config())

    assert len(frame) == 8
    assert MODE_COLUMN in frame.columns
    assert "obd:RPM__mean" in frame.columns


def test_sequence_window_dataset_from_sessions_uses_lstm_sequence_length(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session-a"
    records: list[TelemetryRecord] = []
    for i in range(35):
        t = 1_778_500_000.0 + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 800.0 + i, "rpm"),
                TelemetryRecord.obd(t, "SPEED", 0.0, "km/h"),
                TelemetryRecord.obd(t, "THROTTLE_POS", 5.0, "%"),
            ]
        )
    write_records_to_parquet(records, session)

    dataset = SequenceWindowDataset.from_sessions(
        [session],
        _config(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:THROTTLE_POS"],
    )

    assert dataset.sequence_length == 30
    assert len(dataset) == 6


def _torch_installed() -> bool:
    try:
        import torch  # noqa: F401
    except ImportError:
        return False
    return True
