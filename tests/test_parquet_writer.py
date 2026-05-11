"""Tests for telemetry Parquet storage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from src.storage import parquet_writer
from src.storage.parquet_writer import ParquetTelemetryWriter, delete_expired_sessions
from src.telemetry import TelemetryRecord


def test_writer_flushes_records_to_readable_parquet(tmp_path: Path) -> None:
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
        max_rows=2,
    )

    first = writer.write(TelemetryRecord.obd(1_778_000_000.0, "RPM", 720.0, "rpm"))
    second = writer.write(TelemetryRecord.can(1_778_000_000.1, 0x123, b"\x01\x02"))

    assert first is None
    assert second is not None
    assert second == tmp_path / "2026-05-11T120000" / "telemetry-00000.parquet"

    table = pq.read_table(second)
    rows = table.to_pylist()
    assert len(rows) == 2
    assert rows[0]["source"] == "obd"
    assert rows[0]["channel"] == "RPM"
    assert rows[0]["value_number"] == 720.0
    assert rows[1]["source"] == "can"
    assert rows[1]["payload"] == b"\x01\x02"
    assert not list(tmp_path.glob("*/*.tmp"))


def test_close_flushes_partial_buffer(tmp_path: Path) -> None:
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 5, 11, 12, 1, tzinfo=UTC),
        max_rows=10,
    )
    writer.write(TelemetryRecord.obd(1_778_000_000.0, "SPEED", 30.0, "km/h"))
    path = writer.close()
    assert path is not None
    assert path.exists()


def test_time_based_flush(tmp_path: Path) -> None:
    now = datetime(2026, 5, 11, 12, 2, tzinfo=UTC)

    def clock() -> datetime:
        return now

    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=now,
        max_rows=10,
        flush_interval_seconds=1.0,
        clock=clock,
    )
    writer.write(TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm"))

    now = now + timedelta(seconds=2)
    flushed = writer.write(TelemetryRecord.obd(1_778_000_001.0, "RPM", 701.0, "rpm"))
    assert flushed is not None


def test_writer_rotates_session_when_engine_run_time_resets(tmp_path: Path) -> None:
    first_started = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    second_started = datetime(2026, 5, 11, 12, 10, tzinfo=UTC)
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=first_started,
        max_rows=10,
    )

    writer.write(TelemetryRecord.obd(first_started.timestamp(), "RUN_TIME", 120.0, "s"))
    writer.write(
        TelemetryRecord.obd((first_started + timedelta(seconds=1)).timestamp(), "RPM", 700.0, "rpm")
    )
    writer.write(TelemetryRecord.obd(second_started.timestamp(), "RUN_TIME", 1.0, "s"))
    writer.close()

    files = sorted(tmp_path.glob("*/telemetry-*.parquet"))
    assert files == [
        tmp_path / "2026-05-11T120000" / "telemetry-00000.parquet",
        tmp_path / "2026-05-11T121000" / "telemetry-00000.parquet",
    ]
    first_rows = pq.read_table(files[0]).to_pylist()
    second_rows = pq.read_table(files[1]).to_pylist()
    assert [row["channel"] for row in first_rows] == ["RUN_TIME", "RPM"]
    assert [row["channel"] for row in second_rows] == ["RUN_TIME"]


def test_failed_atomic_write_does_not_publish_final_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 5, 11, 12, 3, tzinfo=UTC),
    )
    writer.write(TelemetryRecord.obd(1_778_000_000.0, "RPM", 720.0, "rpm"))

    def fail_write_table(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("disk disappeared")

    with monkeypatch.context() as patch:
        patch.setattr(parquet_writer._PQ, "write_table", fail_write_table)
        with pytest.raises(RuntimeError, match="disk disappeared"):
            writer.flush()

    assert not list(tmp_path.glob("*/telemetry-*.parquet"))
    path = writer.flush()
    assert path is not None
    assert pq.read_table(path).to_pylist()[0]["channel"] == "RPM"


def test_delete_expired_sessions_removes_only_old_timestamped_dirs(tmp_path: Path) -> None:
    old = tmp_path / "2026-05-01T000000"
    keep = tmp_path / "2026-05-10T000000"
    scratch = tmp_path / "scratch"
    old.mkdir()
    keep.mkdir()
    scratch.mkdir()

    deleted = delete_expired_sessions(
        tmp_path,
        retention_days=5,
        now=datetime(2026, 5, 11, tzinfo=UTC),
    )
    assert deleted == [old]
    assert not old.exists()
    assert keep.exists()
    assert scratch.exists()


def test_delete_expired_sessions_rejects_negative_retention(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="retention_days"):
        delete_expired_sessions(tmp_path, retention_days=-1)
