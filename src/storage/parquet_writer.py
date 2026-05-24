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

import logging
import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from src.session import SessionPaths, make_session_dir
from src.storage.monitor import free_space_mb as _default_free_space_mb
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

    def enter_replay_mode(self, replay_id: str) -> Path | None:
        """Flush + redirect writes to ``<out_root>/_replay-<replay_id>/``.

        The leading ``_`` excludes the directory from training discovery
        (:mod:`src.preprocessing.loader`), from the ``GET /control/sessions``
        listing (timestamp-only regex), and from the byte-bounded retention
        sweep (timestamp parse fails) — see ``_replay-*`` isolation in
        ``docs/mcp-server.md``.
        """
        flushed = self.flush()
        replay_root = self._out_root / f"_replay-{replay_id}"
        replay_root.mkdir(parents=True, exist_ok=True)
        from src.session import session_paths

        self.paths = session_paths(replay_root)
        self.session_paths.append(self.paths)
        self._chunk_index = 0
        self._last_flush = self._clock()
        return flushed

    def exit_replay_mode(self, *, started_at: datetime | None = None) -> Path | None:
        """Flush the replay buffer and roll back to a fresh live session dir.

        :func:`make_session_dir` collides if called twice in one wall-clock
        second (it uses ``exist_ok=False`` on purpose to make collisions
        loud). After a short replay, the post-replay timestamp can match
        the pre-replay one — bump forward a second until the new dir name
        is available.
        """
        flushed = self.flush()
        clock_now = self._clock() if started_at is None else started_at
        existing_names = {p.root.name for p in self.session_paths}
        attempt = clock_now
        for _ in range(60):
            name = attempt.astimezone(UTC).strftime("%Y-%m-%dT%H%M%S")
            if name not in existing_names and not (self._out_root / name).exists():
                break
            attempt = attempt + timedelta(seconds=1)
        self.paths = make_session_dir(self._out_root, attempt)
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


def _is_truncated_parquet(path: Path) -> bool:
    """True if a non-empty file lacks the trailing ``PAR1`` magic.

    A Parquet file always ends with the 4-byte ``PAR1`` footer magic; a chunk
    killed mid-write (power loss) is renamed into place but lacks it. Reading
    the last four bytes is enough to spot it without parsing.
    """
    try:
        size = path.stat().st_size
        if size < 8:
            return True
        with path.open("rb") as handle:
            handle.seek(-4, os.SEEK_END)
            return handle.read(4) != b"PAR1"
    except OSError:
        return False


def delete_expired_sessions(
    sessions_root: Path,
    retention_days: int,
    *,
    now: datetime | None = None,
    protect: Iterable[Path] | None = None,
) -> list[Path]:
    """Delete timestamp-named session directories older than retention_days.

    ``protect`` is an explicit safety rail for live sessions. A protected
    directory is never removed even if its timestamp is older than the
    retention cutoff. Pin files keep their existing behavior.
    """
    if retention_days < 0:
        raise ValueError("retention_days must be non-negative")
    if not sessions_root.exists():
        return []

    protected = {path.resolve() for path in protect or ()}
    cutoff = (datetime.now(UTC) if now is None else now.astimezone(UTC)) - timedelta(
        days=retention_days
    )
    deleted: list[Path] = []
    for child in sorted(sessions_root.iterdir()):
        if not child.is_dir():
            continue
        try:
            started_at = datetime.strptime(child.name, "%Y-%m-%dT%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        if child.resolve() in protected:
            continue
        if (child / ".canary-pin").exists():
            continue
        if started_at < cutoff:
            shutil.rmtree(child)
            deleted.append(child)
    return deleted


def delete_expired_replays(
    sessions_root: Path,
    replay_retention_days: int,
    *,
    now: datetime | None = None,
    protect: Iterable[Path] | None = None,
) -> list[Path]:
    """Evict ``_replay-<replay_id>`` directories older than the configured age.

    Mirrors :func:`delete_expired_sessions` but for the isolated replay
    output tree. The replay_id is the same sortable
    ``YYYY-MM-DDTHHMMSS`` form used by live sessions, just with a
    ``_replay-`` prefix that excludes the dir from training discovery
    and the live-session listing. Anything under ``sessions_root`` that
    doesn't match the prefix + timestamp shape is ignored.
    """
    if replay_retention_days < 0:
        raise ValueError("replay_retention_days must be non-negative")
    if not sessions_root.exists():
        return []

    protected = {path.resolve() for path in protect or ()}
    cutoff = (datetime.now(UTC) if now is None else now.astimezone(UTC)) - timedelta(
        days=replay_retention_days
    )
    prefix = "_replay-"
    deleted: list[Path] = []
    for child in sorted(sessions_root.iterdir()):
        if not child.is_dir() or not child.name.startswith(prefix):
            continue
        replay_id = child.name[len(prefix) :]
        try:
            started_at = datetime.strptime(replay_id, "%Y-%m-%dT%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        if child.resolve() in protected:
            continue
        if started_at < cutoff:
            shutil.rmtree(child)
            deleted.append(child)
    return deleted


@dataclass(frozen=True)
class EmptySessionSweep:
    """What :func:`delete_empty_sessions` removed in one pass."""

    removed_sessions: list[Path]  # husk dirs with no surviving data
    removed_files: list[Path]  # 0-byte parquet dropped from kept sessions


def delete_empty_sessions(
    sessions_root: Path,
    *,
    now: datetime | None = None,
    protect: Iterable[Path] | None = None,
    min_age_seconds: float = 120.0,
) -> EmptySessionSweep:
    """Remove crash-loop / interrupted-write session husks.

    A daemon that crash-loops (or is killed mid-flush) leaves timestamp-named
    session dirs that are empty or hold only 0-byte ``*.parquet`` files. The age
    sweep won't catch them (they're recent) and they clutter training discovery
    and the live-session list. For each timestamp session dir — skipping
    ``protect`` (the live session), pinned dirs, and anything newer than
    ``min_age_seconds`` (so a just-created session that hasn't flushed its first
    file isn't raced) — this drops 0-byte *and* truncated parquet files (a chunk
    killed mid-write by a power loss, missing its ``PAR1`` footer), then removes
    the whole directory if no data survives anywhere in its tree.
    """
    if not sessions_root.exists():
        return EmptySessionSweep([], [])

    protected = {path.resolve() for path in protect or ()}
    now_dt = datetime.now(UTC) if now is None else now.astimezone(UTC)
    removed_sessions: list[Path] = []
    removed_files: list[Path] = []
    for child in sorted(sessions_root.iterdir()):
        if not child.is_dir():
            continue
        try:
            started_at = datetime.strptime(child.name, "%Y-%m-%dT%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        if child.resolve() in protected or (child / ".canary-pin").exists():
            continue
        if (now_dt - started_at).total_seconds() < min_age_seconds:
            continue
        for parquet in child.glob("*.parquet"):
            try:
                if parquet.stat().st_size == 0 or _is_truncated_parquet(parquet):
                    parquet.unlink()
                    removed_files.append(parquet)
            except OSError:
                continue
        if _dir_tree_size(child) == 0:
            shutil.rmtree(child)
            removed_sessions.append(child)
    return EmptySessionSweep(removed_sessions, removed_files)


def _dir_tree_size(root: Path) -> int:
    """Total size in bytes of every regular file under ``root`` (recursive)."""
    total = 0
    for child in root.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def enforce_storage_cap(
    sessions_root: Path,
    max_mb: int,
    *,
    protect: Iterable[Path] | None = None,
) -> list[Path]:
    """Evict oldest session directories until total session bytes are under cap.

    This is the byte-bounded ring buffer: ``delete_expired_sessions`` bounds
    sessions by *age*; this bounds them by *total size*. Only timestamp-named
    session directories are measured and evicted — ``scratch`` / ``_archive``
    and other non-session entries are neither counted nor removed.

    Eviction is oldest-first by directory name (ISO timestamp, so lexical
    order is chronological — the same convention as ``delete_expired_sessions``).
    A directory containing ``.canary-pin`` or listed in ``protect`` (the live
    session dir) is never deleted, even if that means staying over the cap.
    Returns the deleted directories, oldest first.
    """
    if max_mb <= 0:
        raise ValueError("max_mb must be positive")
    if not sessions_root.exists():
        return []

    protected = {path.resolve() for path in protect or ()}
    cap_bytes = max_mb * 1024 * 1024

    sessions: list[tuple[Path, int]] = []
    for child in sorted(sessions_root.iterdir()):
        if not child.is_dir():
            continue
        try:
            datetime.strptime(child.name, "%Y-%m-%dT%H%M%S")
        except ValueError:
            continue
        sessions.append((child, _dir_tree_size(child)))

    total = sum(size for _, size in sessions)
    deleted: list[Path] = []
    for child, size in sessions:  # sorted() above => oldest first
        if total <= cap_bytes:
            break
        if child.resolve() in protected:
            continue
        if (child / ".canary-pin").exists():
            continue
        shutil.rmtree(child)
        deleted.append(child)
        total -= size
    return deleted


def emergency_evict_for_free_space(
    sessions_root: Path,
    warning_mb: float,
    *,
    protect: Iterable[Path] | None = None,
    free_space_mb: Callable[[Path], float] = _default_free_space_mb,
) -> list[Path]:
    """Oldest-first eviction until filesystem free space recovers.

    ``enforce_storage_cap`` bounds the *session footprint*; this is the
    safety net for the *filesystem* — when free space on ``sessions_root``
    falls below ``warning_mb`` (a smaller card than expected, other data on
    the partition, or the byte cap disabled), delete oldest unpinned,
    non-protected timestamp session dirs until free space recovers to
    ``warning_mb`` or only pinned/active dirs remain. Free space is re-probed
    after every delete because it is a filesystem property, not a function of
    session bytes alone. Aggressive relative to the byte cap: it ignores
    ``storage_max_mb`` and will delete every evictable dir if that is what it
    takes to keep the disk writable. Returns the evicted dirs, oldest first.
    """
    if warning_mb < 0:
        raise ValueError("warning_mb must be non-negative")
    if not sessions_root.exists():
        return []
    if free_space_mb(sessions_root) >= warning_mb:
        return []

    protected = {path.resolve() for path in protect or ()}
    deleted: list[Path] = []
    for child in sorted(sessions_root.iterdir()):
        if free_space_mb(sessions_root) >= warning_mb:
            break
        if not child.is_dir():
            continue
        try:
            datetime.strptime(child.name, "%Y-%m-%dT%H%M%S")
        except ValueError:
            continue
        if child.resolve() in protected:
            continue
        if (child / ".canary-pin").exists():
            continue
        shutil.rmtree(child)
        deleted.append(child)
    return deleted
