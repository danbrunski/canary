"""Buffered Parquet storage for normalized telemetry records."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from src.session import SessionPaths, make_session_dir
from src.telemetry import TelemetryRecord

_PA: Any = pa
_PQ: Any = pq

TELEMETRY_SCHEMA: Any = _PA.schema(
    [
        _PA.field("timestamp", _PA.timestamp("us", tz="UTC")),
        _PA.field("source", _PA.string()),
        _PA.field("channel", _PA.string()),
        _PA.field("value_number", _PA.float64()),
        _PA.field("value_text", _PA.string()),
        _PA.field("value_bool", _PA.bool_()),
        _PA.field("unit", _PA.string()),
        _PA.field("payload", _PA.binary()),
        _PA.field("metadata_json", _PA.string()),
    ]
)

ENGINE_RUN_TIME_CHANNELS = frozenset({"RUN_TIME", "run_time", "runtime", "engine_run_time"})


class ParquetTelemetryWriter:
    """Buffer telemetry records and flush them into per-session Parquet chunks."""

    def __init__(
        self,
        out_root: Path,
        *,
        session_started_at: datetime | None = None,
        max_rows: int = 10_000,
        flush_interval_seconds: float = 5.0,
        engine_run_time_reset_threshold_seconds: float = 5.0,
        clock: Callable[[], datetime] | None = None,
    ):
        self._clock: Callable[[], datetime] = (
            (lambda: datetime.now(UTC)) if clock is None else clock
        )
        self._out_root = out_root
        started_at = self._clock() if session_started_at is None else session_started_at
        self.paths: SessionPaths = make_session_dir(self._out_root, started_at)
        self.max_rows = max_rows
        self.flush_interval_seconds = flush_interval_seconds
        self.engine_run_time_reset_threshold_seconds = engine_run_time_reset_threshold_seconds
        self._buffer: list[TelemetryRecord] = []
        self._chunk_index = 0
        self._last_flush = self._clock()
        self._last_engine_run_time: float | None = None
        self.files_written: list[Path] = []
        self.session_paths: list[SessionPaths] = [self.paths]

    def write(self, record: TelemetryRecord) -> Path | None:
        """Append one record, flushing when row count or time threshold is reached."""
        self._rotate_if_engine_run_time_reset(record)
        self._buffer.append(record)
        if len(self._buffer) >= self.max_rows or self.flush_due():
            return self.flush()
        return None

    def write_many(self, records: Iterable[TelemetryRecord]) -> list[Path]:
        files: list[Path] = []
        for record in records:
            flushed = self.write(record)
            if flushed is not None:
                files.append(flushed)
        return files

    def flush_due(self) -> bool:
        elapsed = (self._clock() - self._last_flush).total_seconds()
        return elapsed >= self.flush_interval_seconds

    def flush(self) -> Path | None:
        """Write the current buffer to one Parquet file and clear it."""
        if not self._buffer:
            self._last_flush = self._clock()
            return None

        rows = [record.parquet_row() for record in self._buffer]
        table = _PA.Table.from_pylist(rows, schema=TELEMETRY_SCHEMA)
        path = self.paths.root / f"telemetry-{self._chunk_index:05d}.parquet"
        _write_table_atomic(table, path, compression="snappy")

        self._chunk_index += 1
        self._buffer.clear()
        self._last_flush = self._clock()
        self.files_written.append(path)
        return path

    def close(self) -> Path | None:
        return self.flush()

    def rotate_session(self, *, started_at: datetime | None = None) -> Path | None:
        """Flush the current buffer and start a new timestamped session directory."""
        flushed = self.flush()
        self.paths = make_session_dir(
            self._out_root,
            self._clock() if started_at is None else started_at,
        )
        self.session_paths.append(self.paths)
        self._chunk_index = 0
        self._last_flush = self._clock()
        return flushed

    def _rotate_if_engine_run_time_reset(self, record: TelemetryRecord) -> None:
        run_time = _engine_run_time_seconds(record)
        if run_time is None:
            return

        previous = self._last_engine_run_time
        if previous is not None and (
            run_time < previous
            or (
                previous > self.engine_run_time_reset_threshold_seconds
                and run_time <= self.engine_run_time_reset_threshold_seconds
            )
        ):
            self.rotate_session(started_at=record.timestamp)
        self._last_engine_run_time = run_time


def write_records_to_parquet(
    records: Iterable[TelemetryRecord],
    out_dir: Path,
    *,
    max_rows: int = 10_000,
    compression: str = "snappy",
    chunk_name: str = "telemetry",
) -> list[Path]:
    """Write telemetry records to chunked Parquet files in `out_dir`.

    Batch counterpart to `ParquetTelemetryWriter` — same schema and chunk
    naming, but driven by a finite iterable instead of a real-time stream.
    Used by `tools/log_to_parquet.py` to convert recorded `frames.log` /
    `pids.jsonl` into the canonical Parquet shape.

    The output is byte-equivalent (modulo compression headers) to what the
    daemon would have produced if the same records had streamed through it,
    so downstream tooling can ignore where the data came from.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    buffer: list[TelemetryRecord] = []
    chunk_index = 0

    def flush() -> None:
        nonlocal chunk_index
        if not buffer:
            return
        rows = [r.parquet_row() for r in buffer]
        table = _PA.Table.from_pylist(rows, schema=TELEMETRY_SCHEMA)
        path = out_dir / f"{chunk_name}-{chunk_index:05d}.parquet"
        _write_table_atomic(table, path, compression=compression)
        files.append(path)
        buffer.clear()
        chunk_index += 1

    for record in records:
        buffer.append(record)
        if len(buffer) >= max_rows:
            flush()
    flush()  # final partial chunk
    return files


def _engine_run_time_seconds(record: TelemetryRecord) -> float | None:
    if record.source != "obd" or record.channel not in ENGINE_RUN_TIME_CHANNELS:
        return None
    if isinstance(record.value, bool) or record.value is None:
        return None
    try:
        return float(record.value)
    except (TypeError, ValueError):
        return None


def _write_table_atomic(table: Any, path: Path, *, compression: str) -> None:
    """Write Parquet to a same-directory temp file, then publish atomically."""
    tmp_path = path.with_name(f"{path.name}.tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    _PQ.write_table(table, tmp_path, compression=compression)
    tmp_path.replace(path)


def delete_expired_sessions(
    sessions_root: Path,
    retention_days: int,
    *,
    now: datetime | None = None,
) -> list[Path]:
    """Delete timestamp-named session directories older than retention_days."""
    if retention_days < 0:
        raise ValueError("retention_days must be non-negative")
    if not sessions_root.exists():
        return []

    cutoff = (datetime.now(UTC) if now is None else now.astimezone(UTC)) - timedelta(
        days=retention_days
    )
    deleted: list[Path] = []
    for child in sessions_root.iterdir():
        if not child.is_dir():
            continue
        try:
            started_at = datetime.strptime(child.name, "%Y-%m-%dT%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        if (child / ".canary-pin").exists():
            continue
        if started_at < cutoff:
            shutil.rmtree(child)
            deleted.append(child)
    return deleted
