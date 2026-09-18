"""Tests for Parquet telemetry session loading."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from src.config import CanaryConfig, OperatingModeSpec
from src.preprocessing.imu_decimation import decimate_imu_wide
from src.preprocessing.loader import (
    channel_key,
    discover_sessions,
    numeric_value_columns,
    read_session,
    read_sessions,
    resample_and_segment,
    to_wide,
)
from src.preprocessing.resampler import ResampleConfig, resample_wide
from src.preprocessing.segmenter import segment_operating_modes
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


def test_long_resampler_matches_legacy_wide_chain_with_imu(tmp_path: Path) -> None:
    """The memory fix must preserve the established IMU and gap semantics."""
    start = 1_778_000_000.0
    records = [
        TelemetryRecord.imu(start + i * 0.01, "ACCEL_LON", (i % 17 - 8) / 10, "g")
        for i in range(200)
    ]
    records += [
        TelemetryRecord.imu(start + i * 0.01, "ACCEL_LAT", (i % 11 - 5) / 20, "g")
        for i in range(200)
    ]
    records += [TelemetryRecord.obd(start + i * 0.5, "RPM", 800.0 + i, "rpm") for i in range(4)]
    session = _write_session(tmp_path, "2026-05-11T120000", records, max_rows=37)
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)

    long = read_sessions([session])
    wide = decimate_imu_wide(
        to_wide(long, value="value_number"), hz=config.preprocessing.resample_hz
    )
    legacy = resample_wide(
        wide,
        config=ResampleConfig(
            hz=config.preprocessing.resample_hz,
            max_gap_factor=config.preprocessing.max_gap_factor,
            slow_channel_max_gap_seconds=config.preprocessing.slow_channel_max_gap_seconds,
            session_gap_seconds=config.preprocessing.session_gap_seconds,
        ),
        value_columns=numeric_value_columns(wide),
    )
    legacy = segment_operating_modes(
        legacy,
        config.preprocessing.operating_modes,
        mode_dwell_ticks=config.preprocessing.mode_dwell_ticks,
    )
    reduced = resample_and_segment([session], config)

    pd.testing.assert_frame_equal(
        reduced.reindex(sorted(reduced.columns), axis=1),
        legacy.reindex(sorted(legacy.columns), axis=1),
        check_dtype=False,
        rtol=1e-5,
        atol=1e-6,
    )


# ---------------------------------------------------------------------------
# 5.2 — mode dwell/hysteresis (batch path)
# ---------------------------------------------------------------------------


def _dwell_config(dwell_ticks: int) -> CanaryConfig:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config.preprocessing.operating_modes = {
        "city": OperatingModeSpec(speed_max=50),
        "highway": OperatingModeSpec(speed_min=80),
    }
    config.preprocessing.mode_dwell_ticks = dwell_ticks
    return config


def _speed_session(tmp_path: Path, name: str, speeds: list[float]) -> Path:
    records = [
        TelemetryRecord.obd(1_778_000_000.0 + i, "SPEED", speed, "km/h")
        for i, speed in enumerate(speeds)
    ]
    return _write_session(tmp_path, name, records)


def test_mode_dwell_absorbs_single_row_spike(tmp_path: Path) -> None:
    """A one-row highway reading surrounded by city rows must not switch the
    label — the candidate never holds for ``mode_dwell_ticks`` in a row."""
    session = _speed_session(tmp_path, "2026-05-11T120000", [20, 20, 20, 90, 20, 20, 20])

    segmented = resample_and_segment([session], _dwell_config(dwell_ticks=3))

    assert list(segmented.sort_values("timestamp")["operating_mode"]) == ["city"] * 7


def test_mode_dwell_switches_after_sustained_transition(tmp_path: Path) -> None:
    """3+ consecutive highway rows DO switch the label, taking effect on the
    row that completes the dwell streak (not immediately on the first)."""
    session = _speed_session(tmp_path, "2026-05-11T120000", [20, 20, 20, 90, 90, 90, 90, 90])

    segmented = resample_and_segment([session], _dwell_config(dwell_ticks=3))

    assert list(segmented.sort_values("timestamp")["operating_mode"]) == [
        "city",
        "city",
        "city",
        "city",
        "city",
        "highway",
        "highway",
        "highway",
    ]
