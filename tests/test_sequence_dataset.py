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


def test_windows_form_on_multi_rate_frame_with_mask_and_delta() -> None:
    """A 1 Hz channel plus a channel observed every ~25 ticks must produce
    windows everywhere (no gap-rejection), with the slow channel's mask/Δt
    tracking its actual freshness."""
    n = 60
    ts = pd.date_range("2026-05-24T00:00:00Z", periods=n, freq="1s")
    # Fast channel: fresh every tick. Slow channel: fresh at t=0,25,50.
    slow_fresh = {0, 25, 50}
    frame = pd.DataFrame(
        {
            SESSION_COLUMN: ["s"] * n,
            TIMESTAMP_COLUMN: ts,
            "fast": [float(i) for i in range(n)],
            "slow": [100.0 + (i // 25) for i in range(n)],
            f"fast{GAP_SUFFIX}": [False] * n,
            f"slow{GAP_SUFFIX}": [i not in slow_fresh for i in range(n)],
        }
    )

    result = build_sequence_windows(frame, sequence_length=10, feature_columns=["fast", "slow"])

    # Every length-10 window inside the session forms (no rejection).
    assert len(result.windows) == n - 10 + 1
    fast_index = result.feature_columns.index("fast")
    slow_index = result.feature_columns.index("slow")
    # Fast channel: always observed, Δt always 0.
    assert bool((result.mask[:, fast_index] == 1.0).all())
    assert bool((result.delta[:, fast_index] == 0.0).all())
    # Slow channel: observed only on its fresh ticks, Δt ramps 0..24 between.
    for tick in range(n):
        expected_observed = 1.0 if tick in slow_fresh else 0.0
        assert result.mask[tick, slow_index] == expected_observed
    assert result.delta[10, slow_index] == 10.0  # 10 ticks since t=0
    assert result.delta[24, slow_index] == 24.0
    assert result.delta[25, slow_index] == 0.0  # fresh again


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


def test_build_sequence_windows_marks_gaps_in_mask_without_dropping() -> None:
    """GRU-D no longer rejects windows that touch a gap; the staleness is
    surfaced through the per-cell mask (¬gap) and accumulated Δt instead."""
    frame = _frame()
    gap_at_t2 = frame[SESSION_COLUMN].eq("A") & frame[TIMESTAMP_COLUMN].dt.second.eq(2)
    frame.loc[gap_at_t2, f"rpm{GAP_SUFFIX}"] = True

    result = build_sequence_windows(frame, sequence_length=3)

    # Every window survives — A keeps all three, B keeps both.
    assert [window.session for window in result.windows] == ["A", "A", "A", "B", "B"]
    # rpm is feature index 0; the t=2 row of session A (sorted row 2) is masked
    # stale, and its Δt is one tick past the prior observation at t=1.
    assert result.mask[2, 0] == pytest.approx(0.0)
    assert result.mask[1, 0] == pytest.approx(1.0)
    assert result.delta[2, 0] == pytest.approx(1.0)
    # speed has no gap column → always observed (mask 1, Δt 0).
    assert bool((result.mask[:, 1] == 1.0).all())
    assert bool((result.delta[:, 1] == 0.0).all())


def test_build_sequence_windows_imputes_leading_nan_and_masks_it() -> None:
    """A leading NaN (before a channel's first observation) is imputed to 0 in
    the value tensor and masked unobserved, instead of rejecting the window."""
    torch = pytest.importorskip("torch")
    frame = _frame()
    lead = frame[SESSION_COLUMN].eq("A") & frame[TIMESTAMP_COLUMN].dt.second.eq(0)
    frame.loc[lead, "rpm"] = np.nan

    dataset = SequenceWindowDataset(frame, sequence_length=3, feature_columns=["rpm", "speed"])
    assert len(dataset) == 5  # nothing dropped
    value, mask, delta = dataset[0]  # session A, rows 0..2 (the NaN is row 0)
    assert isinstance(value, torch.Tensor)
    assert float(value[0, 0]) == pytest.approx(0.0)  # NaN imputed to 0 (=mean)
    assert float(mask[0, 0]) == pytest.approx(0.0)  # masked unobserved


def test_build_sequence_windows_honors_explicit_gap_columns() -> None:
    frame = _frame()
    frame["manual_gap"] = False
    gap_mask = frame[SESSION_COLUMN].eq("B") & frame[TIMESTAMP_COLUMN].dt.second.eq(1)
    frame.loc[gap_mask, "manual_gap"] = True

    result = build_sequence_windows(
        frame,
        sequence_length=3,
        gap_columns=["manual_gap"],
    )

    # With an explicit gap column the mask is derived from value-presence only
    # (the explicit caller manages its own gap semantics); windows still all
    # form, and the feature set excludes the explicit gap column.
    assert result.feature_columns == ("rpm", "speed")
    assert [window.session for window in result.windows] == ["A", "A", "A", "B", "B"]
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


def test_sequence_window_dataset_returns_value_mask_delta_tuple() -> None:
    torch = pytest.importorskip("torch")
    dataset = SequenceWindowDataset(
        _frame(),
        sequence_length=3,
        feature_columns=["rpm", "speed"],
    )

    value, mask, delta = dataset[0]

    assert isinstance(value, torch.Tensor)
    assert isinstance(mask, torch.Tensor)
    assert isinstance(delta, torch.Tensor)
    assert tuple(value.shape) == (3, 2)
    assert tuple(mask.shape) == (3, 2)
    assert tuple(delta.shape) == (3, 2)


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
