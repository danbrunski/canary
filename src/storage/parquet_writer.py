"""Buffered Parquet storage for normalized telemetry records.

Durability note: the Pi has no UPS — it loses power the instant the ignition
key turns off, mid-write, every drive. So each flushed chunk is fsync'd before
it is published (and the directory entry fsync'd after the rename): once
``flush()`` returns, that chunk has survived a power cut, and only the rows
still buffered since the last flush (bounded by ``flush_interval_seconds`` /
``max_rows``) can be lost. A chunk caught mid-write still leaves a truncated
tail, which the loader skips and the husk sweep removes.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from src.atomic_write import atomic_write_text
from src.collection.capture_profiles import CaptureProfile
from src.collection.record_arming import RecordArmingGate
from src.session import SessionPaths, format_session_timestamp, make_session_dir, session_paths
from src.storage.retention import (
    EmptySessionSweep as EmptySessionSweep,
)
from src.storage.retention import (
    delete_empty_sessions as delete_empty_sessions,
)
from src.storage.retention import (
    delete_expired_replays as delete_expired_replays,
)
from src.storage.retention import (
    delete_expired_sessions as delete_expired_sessions,
)
from src.storage.retention import (
    emergency_evict_for_free_space as emergency_evict_for_free_space,
)
from src.storage.retention import (
    enforce_storage_cap as enforce_storage_cap,
)
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

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
        session_gap_seconds: float = 300.0,
        clock: Callable[[], datetime] | None = None,
        arming_gate: RecordArmingGate | None = None,
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
        if session_gap_seconds <= 0:
            raise ValueError("session_gap_seconds must be positive")
        self.session_gap_seconds = session_gap_seconds
        #: Optional operator-intent gate (Task 8.6). ``None`` (the default)
        #: preserves the always-on behavior byte-for-byte.
        self.arming_gate = arming_gate
        self._buffer: list[TelemetryRecord] = []
        self._chunk_index = 0
        self._last_flush = self._clock()
        self._last_engine_run_time: float | None = None
        self._last_record_timestamp: datetime | None = None
        self.files_written: list[Path] = []
        #: Rows accepted into the *current* session, buffered or flushed. The
        #: capture UI reports it so an operator can see a log growing without
        #: waiting for a flush.
        self.rows_written = 0
        self.session_paths: list[SessionPaths] = [self.paths]

    def write(self, record: TelemetryRecord) -> Path | None:
        """Append one record, flushing when row count or time threshold is reached."""
        self._rotate_if_record_gap(record)
        self._rotate_if_engine_run_time_reset(record)
        if self.arming_gate is not None and not self.arming_gate.observe(record):
            return None
        self._buffer.append(record)
        self.rows_written += 1
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
        self.rows_written = 0
        self._chunk_index = 0
        self._last_flush = self._clock()
        self._last_engine_run_time = None
        self._last_record_timestamp = None
        if self.arming_gate is not None:
            # New key cycle: gate again until this session sees its own
            # vehicle-active signal (Task 8.6).
            self.arming_gate.reset_for_new_session()
        return flushed

    def _rotate_if_record_gap(self, record: TelemetryRecord) -> None:
        timestamp = record.timestamp
        previous = self._last_record_timestamp
        if (
            previous is not None
            and (timestamp - previous).total_seconds() > self.session_gap_seconds
        ):
            self.rotate_session(started_at=timestamp)
        # Small cross-source reordering must not move the watermark backwards.
        if previous is None or timestamp > previous:
            self._last_record_timestamp = timestamp

    def _enter_isolated_dir(self, dir_name: str) -> Path | None:
        """Flush + redirect writes to ``<out_root>/<dir_name>/``.

        ``dir_name`` must carry the leading ``_`` that excludes the directory
        from training discovery (:mod:`src.preprocessing.loader`), from the
        ``GET /control/sessions`` listing (timestamp-only regex), and from the
        byte-bounded retention sweep (timestamp parse fails) — see the
        ``_replay-*`` / ``_tuning-*`` isolation note in ``docs/mcp-server.md``.
        """
        flushed = self.flush()
        isolated_root = self._out_root / dir_name
        isolated_root.mkdir(parents=True, exist_ok=True)
        self.paths = session_paths(isolated_root)
        self.session_paths.append(self.paths)
        self.rows_written = 0
        self._chunk_index = 0
        self._last_flush = self._clock()
        self._last_record_timestamp = None
        return flushed

    def _exit_isolated_dir(self, started_at: datetime | None = None) -> Path | None:
        """Flush the isolated buffer and roll back to a fresh live session dir.

        :func:`make_session_dir` collides if called twice in one wall-clock
        second (it uses ``exist_ok=False`` on purpose to make collisions
        loud). After a short replay or tuning window, the resuming timestamp
        can match the one from before it — bump forward a second until the
        new dir name is available.
        """
        flushed = self.flush()
        clock_now = self._clock() if started_at is None else started_at
        existing_names = {p.root.name for p in self.session_paths}
        attempt = clock_now
        for _ in range(60):
            name = format_session_timestamp(attempt.astimezone(UTC))
            if name not in existing_names and not (self._out_root / name).exists():
                break
            attempt = attempt + timedelta(seconds=1)
        self.paths = make_session_dir(self._out_root, attempt)
        self.session_paths.append(self.paths)
        self.rows_written = 0
        self._chunk_index = 0
        self._last_flush = self._clock()
        self._last_record_timestamp = None
        return flushed

    def enter_replay_mode(self, replay_id: str) -> Path | None:
        """Flush + redirect writes to ``<out_root>/_replay-<replay_id>/``."""
        return self._enter_isolated_dir(f"_replay-{replay_id}")

    def exit_replay_mode(self, *, started_at: datetime | None = None) -> Path | None:
        """Flush the replay buffer and roll back to a fresh live session dir."""
        return self._exit_isolated_dir(started_at)

    def enter_tuning_mode(self, tuning_id: str) -> Path | None:
        """Flush + redirect writes to ``<out_root>/_tuning-<tuning_id>/``.

        ``engine-tuning-support.md`` 1.2. Tuning captures are deliberately
        abnormal — flat-AFR-target test maps force open loop and produce
        fuelling the behavior models have never seen — so they must never be
        discoverable by training, "even transiently". That rules out
        post-hoc archiving and requires the quarantine to start the moment
        tuning mode is armed, which is what this does.
        """
        return self._enter_isolated_dir(f"_tuning-{tuning_id}")

    def enter_capture_mode(
        self,
        profile: CaptureProfile,
        capture_id: str,
        tune_revision: str | None = None,
        resolved_polling_plan: dict[str, Any] | None = None,
    ) -> Path | None:
        """Rotate atomically into a profile-owned capture session."""
        if profile.name == "default":
            raise ValueError("default is normal collection, not a bounded capture")
        if profile.corpus_eligible:
            flushed = self.rotate_session()
        else:
            prefix = profile.storage_prefix or f"_capture-{profile.name}"
            flushed = self._enter_isolated_dir(f"{prefix}-{capture_id}")
        sidecar = {
            "schema_version": 2,
            "profile": profile.name,
            "started_at": self._clock().isoformat(),
            "tune_revision": tune_revision,
            "profile_snapshot": profile.model_dump(mode="json"),
            "resolved_polling_plan": resolved_polling_plan,
        }
        atomic_write_text(
            self.paths.root / "capture_profile.json",
            json.dumps(sidecar, indent=2, sort_keys=True) + "\n",
        )
        return flushed

    def exit_capture_mode(self, *, started_at: datetime | None = None) -> Path | None:
        return self._exit_isolated_dir(started_at)

    def exit_tuning_mode(self, *, started_at: datetime | None = None) -> Path | None:
        """Flush the tuning buffer and roll back to a fresh live session dir."""
        return self._exit_isolated_dir(started_at)

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
    """Write Parquet to a same-directory temp file, then publish atomically.

    fsync the temp file before the rename and the directory after it, so the
    chunk is durable on disk the moment this returns — without it a hard power
    cut (key-off, no UPS) can leave the rename committed but the data still in
    the page cache, i.e. a renamed-but-empty truncated chunk.
    """
    tmp_path = path.with_name(f"{path.name}.tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    _PQ.write_table(table, tmp_path, compression=compression)
    _fsync_path(tmp_path)
    tmp_path.replace(path)
    _fsync_path(path.parent)


def _fsync_path(path: Path) -> None:
    """Best-effort fsync of a file or directory; logs and continues on failure.

    A failed fsync means that write isn't guaranteed durable, but crashing the
    collector would lose more — so it's best-effort. Used to make each Parquet
    chunk + its directory entry survive a power loss.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        log.warning("fsync open failed for %s: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        log.warning("fsync failed for %s: %s", path, exc)
    finally:
        os.close(fd)
