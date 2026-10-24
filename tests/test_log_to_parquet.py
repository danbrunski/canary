"""Tests for the offline candump → Parquet batch converter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from src.storage.parquet_writer import (
    TELEMETRY_SCHEMA,
    write_records_to_parquet,
)
from src.telemetry import TelemetryRecord
from tools.log_to_parquet import convert


def _read_table(path: Path) -> Any:
    """``pq.read_table`` with the stub's missing annotation absorbed once."""
    return pq.read_table(path)  # type: ignore[no-untyped-call, unused-ignore]


# ---------------------------------------------------------------------------
# write_records_to_parquet (batch helper)
# ---------------------------------------------------------------------------


def test_write_records_emits_chunked_files(tmp_path: Path) -> None:
    records = [
        TelemetryRecord.can(1.0 + i * 0.001, 0x100 + i, bytes([i, i + 1])) for i in range(25)
    ]
    files = write_records_to_parquet(records, tmp_path, max_rows=10)
    # 25 records / 10 per chunk = 3 files (10, 10, 5)
    assert len(files) == 3
    assert files[0].name == "telemetry-00000.parquet"
    assert files[2].name == "telemetry-00002.parquet"
    for path in files:
        assert path.exists()


def test_write_records_empty_iterable_writes_nothing(tmp_path: Path) -> None:
    files = write_records_to_parquet([], tmp_path, max_rows=10)
    assert files == []
    assert not list(tmp_path.glob("telemetry-*.parquet"))


def test_write_records_creates_out_dir_if_missing(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "subdir"
    record = TelemetryRecord.can(1.0, 0x100, b"\x00\x01")
    files = write_records_to_parquet([record], out, max_rows=10)
    assert out.exists()
    assert len(files) == 1


def test_written_parquet_uses_canonical_schema(tmp_path: Path) -> None:
    record = TelemetryRecord.can(1.5, 0x551, b"\x42\x00")
    files = write_records_to_parquet([record], tmp_path)
    table = _read_table(files[0])
    # Schema field names + types must match the project's canonical shape.
    assert table.schema == TELEMETRY_SCHEMA
    assert table.num_rows == 1
    row = table.to_pylist()[0]
    assert row["source"] == "can"
    assert row["channel"] == "0x551"
    assert row["payload"] == b"\x42\x00"
    meta = json.loads(row["metadata_json"])
    assert meta["can_id"] == 0x551
    assert meta["dlc"] == 2


def test_written_parquet_preserves_timestamp_us_precision(tmp_path: Path) -> None:
    record = TelemetryRecord.from_epoch(
        1_778_513_500.123456, source="can", channel="0x100", payload=b"\x00"
    )
    files = write_records_to_parquet([record], tmp_path)
    table = _read_table(files[0])
    written = table.to_pylist()[0]["timestamp"]
    # PyArrow returns datetime in UTC microsecond precision
    assert written == datetime(2026, 5, 11, 15, 31, 40, 123456, tzinfo=UTC)


# ---------------------------------------------------------------------------
# convert() — end-to-end against a synthetic session
# ---------------------------------------------------------------------------


def _make_synthetic_session(root: Path) -> Path:
    """Build a minimal session dir with frames.log + pids.jsonl."""
    session = root / "synth"
    session.mkdir()
    (session / "frames.log").write_text(
        "\n".join(
            [
                "(1.0) vcan0 100#01020304",
                "(1.5) vcan0 200#AABB",
                "(2.0) vcan0 100#05060708",
            ]
        )
        + "\n"
    )
    (session / "pids.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"t": 1.2, "pid": "RPM", "value": 752.0, "unit": "rpm"}),
                json.dumps({"t": 1.8, "pid": "ENGINE_COOLANT_TEMP", "value": 87.0, "unit": "C"}),
            ]
        )
        + "\n"
    )
    return session


def test_convert_writes_parquet_with_merged_can_and_obd_records(tmp_path: Path) -> None:
    session = _make_synthetic_session(tmp_path)
    files = convert(session, max_rows=100)
    assert len(files) == 1

    table = _read_table(files[0])
    rows = table.to_pylist()
    # 3 CAN frames + 2 OBD readings = 5 records, sorted by timestamp
    assert len(rows) == 5
    timestamps = [r["timestamp"] for r in rows]
    assert timestamps == sorted(timestamps)

    sources = [r["source"] for r in rows]
    assert sources == ["can", "obd", "can", "obd", "can"]


def test_convert_skips_pids_when_no_pids_flag_set(tmp_path: Path) -> None:
    session = _make_synthetic_session(tmp_path)
    files = convert(session, include_pids=False, max_rows=100)
    rows = _read_table(files[0]).to_pylist()
    assert len(rows) == 3  # frames only
    assert all(r["source"] == "can" for r in rows)


def test_convert_handles_session_without_pids_file(tmp_path: Path) -> None:
    session = tmp_path / "no-pids"
    session.mkdir()
    (session / "frames.log").write_text("(1.0) vcan0 100#0102\n(2.0) vcan0 200#FF\n")
    files = convert(session, max_rows=100)
    rows = _read_table(files[0]).to_pylist()
    assert len(rows) == 2


def test_convert_chunks_when_records_exceed_max_rows(tmp_path: Path) -> None:
    session = tmp_path / "big"
    session.mkdir()
    lines = [f"({1.0 + i * 0.001:.3f}) vcan0 100#{i:04X}" for i in range(50)]
    (session / "frames.log").write_text("\n".join(lines) + "\n")

    files = convert(session, max_rows=20)
    assert len(files) == 3  # 20 + 20 + 10

    total_rows = sum(_read_table(f).num_rows for f in files)
    assert total_rows == 50


def test_convert_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    session = _make_synthetic_session(tmp_path)
    # Pre-existing telemetry file
    (session / "telemetry-00000.parquet").write_bytes(b"stale data")
    with pytest.raises(FileExistsError, match="--force"):
        convert(session)


def test_convert_overwrites_with_force(tmp_path: Path) -> None:
    session = _make_synthetic_session(tmp_path)
    (session / "telemetry-00000.parquet").write_bytes(b"stale data")
    files = convert(session, force=True)
    assert len(files) == 1
    # The new file should be a valid Parquet, not the placeholder bytes
    table = _read_table(files[0])
    assert table.num_rows == 5


def test_convert_errors_when_no_frames_log(tmp_path: Path) -> None:
    session = tmp_path / "empty"
    session.mkdir()
    with pytest.raises(FileNotFoundError, match="frames.log"):
        convert(session)


def test_convert_preserves_can_metadata(tmp_path: Path) -> None:
    """CAN frames should round-trip through Parquet with full metadata intact."""
    session = tmp_path / "meta"
    session.mkdir()
    (session / "frames.log").write_text("(1.0) vcan0 7E8#0562110305A30000\n")
    files = convert(session, max_rows=100)
    row = _read_table(files[0]).to_pylist()[0]
    assert row["source"] == "can"
    assert row["channel"] == "0x7E8"
    assert row["payload"] == bytes.fromhex("0562110305A30000")
    meta = json.loads(row["metadata_json"])
    assert meta["can_id"] == 0x7E8
    assert meta["dlc"] == 8
