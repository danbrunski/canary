"""Session retention and storage-pressure eviction helpers."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.storage.monitor import free_space_mb as _default_free_space_mb


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


__all__ = [
    "EmptySessionSweep",
    "delete_empty_sessions",
    "delete_expired_replays",
    "delete_expired_sessions",
    "emergency_evict_for_free_space",
    "enforce_storage_cap",
]
