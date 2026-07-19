"""Tests for Parquet telemetry session loading."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.config import CanaryConfig
from src.preprocessing.loader import (
    channel_key,
    discover_sessions,
    numeric_value_columns,
    read_session,
    read_sessions,
    resample_and_segment,
    to_wide,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _write_session(
    root: Path, name: str, records: list[TelemetryRecord], *, max_rows: int = 10
) -> Path:
    session = root / name
    write_records_to_parquet(records, session, max_rows=max_rows)
    return session


def test_discover_sessions_finds_only_dirs_with_telemetry(tmp_path: Path) -> None:
    session = _write_session(
        tmp_path,
        "2026-05-11T120000",
        [TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm")],
    )
    (tmp_path / "scratch").mkdir()
    (tmp_path / "telemetry-00000.parquet.tmp").write_text("partial")

    assert discover_sessions(tmp_path) == [session]


def test_read_session_loads_chunks_and_adds_channel_keys(tmp_path: Path) -> None:
    started = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    session = _write_session(
        tmp_path,
        "2026-05-11T120000",
        [
            TelemetryRecord.obd(started.timestamp(), "RPM", 700.0, "rpm"),
            TelemetryRecord.can((started + timedelta(seconds=1)).timestamp(), 0x123, b"\x01\x02"),
            TelemetryRecord.obd((started + timedelta(seconds=2)).timestamp(), "GEAR", "P", ""),
        ],
        max_rows=2,
    )

    frame = read_session(session)

    assert list(frame["session"].unique()) == ["2026-05-11T120000"]
    assert list(frame["source_channel"]) == ["obd:RPM", "can:0x123", "obd:GEAR"]
    assert frame.loc[0, "value"] == 700.0
    assert frame.loc[1, "payload"] == b"\x01\x02"
    assert frame.loc[2, "value"] == "P"


def test_read_session_raises_when_no_telemetry_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(FileNotFoundError, match="telemetry"):
        read_session(empty)


def test_read_sessions_concatenates_and_sorts_by_session(tmp_path: Path) -> None:
    first = _write_session(
        tmp_path,
        "2026-05-11T120000",
        [TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm")],
    )
    second = _write_session(
        tmp_path,
        "2026-05-11T130000",
        [TelemetryRecord.obd(1_778_003_600.0, "SPEED", 30.0, "km/h")],
    )

    frame = read_sessions([second, first])

    assert list(frame["session"]) == ["2026-05-11T120000", "2026-05-11T130000"]


def test_to_wide_pivots_one_column_per_source_channel(tmp_path: Path) -> None:
    session = _write_session(
        tmp_path,
        "2026-05-11T120000",
        [
            TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm"),
            TelemetryRecord.obd(1_778_000_000.0, "SPEED", 0.0, "km/h"),
            TelemetryRecord.obd(1_778_000_001.0, "RPM", 710.0, "rpm"),
        ],
    )

    wide = to_wide(read_session(session))

    assert channel_key("obd", "RPM") in wide.columns
    assert channel_key("obd", "SPEED") in wide.columns
    assert list(wide["obd:RPM"]) == [700.0, 710.0]
    assert list(wide["obd:SPEED"].dropna()) == [0.0]


def test_numeric_value_columns_excludes_gap_flags() -> None:
    """Regression: session_classifier.py and sequence_dataset.py each grew a
    byte-drifted copy of this selector; only one excluded ``__gap`` columns.
    A frozen/stuck channel's boolean gap flag must never be counted as a
    real feature column — it would silently inflate completeness metrics.
    """
    frame = pd.DataFrame(
        {
            "session": ["s1"],
            "timestamp": [pd.Timestamp.now(tz="UTC")],
            "operating_mode": ["idle"],
            "obd:COOLANT_TEMP": [85.0],
            "obd:COOLANT_TEMP__gap": [True],
        }
    )

    columns = numeric_value_columns(frame)

    assert columns == ["obd:COOLANT_TEMP"]


def test_resample_and_segment_runs_shared_chain(tmp_path: Path) -> None:
    session = _write_session(
        tmp_path,
        "2026-05-11T120000",
        [
            TelemetryRecord.obd(1_778_000_000.0, "RPM", 800.0, "rpm"),
            TelemetryRecord.obd(1_778_000_000.0, "SPEED", 0.0, "km/h"),
            TelemetryRecord.obd(1_778_000_001.0, "RPM", 810.0, "rpm"),
            TelemetryRecord.obd(1_778_000_001.0, "SPEED", 0.0, "km/h"),
        ],
    )

    segmented = resample_and_segment([session], CanaryConfig.from_yaml(EXAMPLE_YAML))

    assert not segmented.empty
    assert "operating_mode" in segmented.columns
    assert (segmented["session"] == "2026-05-11T120000").all()


def test_resample_and_segment_handles_no_numeric_channels(tmp_path: Path) -> None:
    session = _write_session(
        tmp_path,
        "2026-05-11T120000",
        [TelemetryRecord.obd(1_778_000_000.0, "VIN", "1N4AL11D75C109151", "")],
    )

    segmented = resample_and_segment([session], CanaryConfig.from_yaml(EXAMPLE_YAML))

    assert "session" in segmented.columns
    assert "timestamp" in segmented.columns
