"""Session retention and storage-pressure eviction helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.atomic_write import atomic_write_text
from src.session import (
    LIVE_SESSION_KINDS,
    SessionKind,
    classify_session_name,
    dir_size,
    iter_sessions,
    session_kind,
    session_sort_key,
)
from src.storage.monitor import free_space_mb as _default_free_space_mb

log = logging.getLogger(__name__)


def _delete_catalog_rows(catalog_db: Path | None, deleted: Iterable[Path]) -> None:
    """Best-effort catalog row deletion (Task 6.3) for every directory this
    sweep actually removed. Never blocks/undoes the real deletion — the
    catalog is a rebuildable index, not a second source of truth. ``None``
    (the default everywhere in this module) skips it entirely."""
    if catalog_db is None:
        return
    names = [path.name for path in deleted]
    if not names:
        return
    try:
        from src.storage.catalog import SessionCatalog

        with SessionCatalog(catalog_db) as catalog:
            for name in names:
                catalog.delete_session(name)
    except Exception:  # noqa: BLE001 — see docstring
        log.exception("catalog row deletion failed for %s", names)


def _is_truncated_parquet(path: Path) -> bool:
    """True if a non-empty file lacks the trailing ``PAR1`` magic.

    A Parquet file always ends with the 4-byte ``PAR1`` footer magic; a chunk
    killed mid-write (power loss) is renamed into place but lacks it. Reading
    the last four bytes is enough to spot it without parsing.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    return _footer_state(path, size) == "truncated"


#: ``PAR1`` header + ``PAR1`` footer: anything shorter cannot be a complete file.
_MIN_PARQUET_BYTES = 8


def _footer_state(path: Path, size: int) -> str:
    """``"ok"``, ``"truncated"`` or ``"unreadable"`` for a parquet of ``size`` bytes."""
    if size < _MIN_PARQUET_BYTES:
        return "truncated"
    try:
        with path.open("rb") as handle:
            handle.seek(-4, os.SEEK_END)
            return "ok" if handle.read(4) == b"PAR1" else "truncated"
    except OSError:
        return "unreadable"


def _is_session_name(name: str) -> bool:
    """Plain live session names only: retention never sweeps a capture, a
    ``_tuning-*`` or an ``_archive`` (replays have their own age sweep)."""
    return session_kind(name) is SessionKind.LIVE


def _live_started_at(name: str) -> datetime | None:
    parsed = classify_session_name(name)
    return parsed.started_at if parsed is not None and parsed.kind is SessionKind.LIVE else None


@dataclass(frozen=True)
class SessionScan:
    """One walk of one session directory: everything a retention tick needs.

    S01.5: a tick used to walk each tree three times (reclaimed-bytes sizes,
    the husk check, the byte cap) and read every parquet footer. The scan is
    taken once per tick and shared.
    """

    path: Path
    #: Every regular file under the tree, recursively.
    total_bytes: int
    #: The top-level ``*.parquet`` chunks, by path, with their sizes.
    parquet_sizes: Mapping[Path, int]
    #: :func:`chunk_fingerprint` of those chunks. A closed session's files
    #: never change, so an unchanged fingerprint means an unchanged set of
    #: footers.
    fingerprint: str
    #: How many files were stat'ed to build this scan.
    files_stat: int
    #: ``(size, mtime_ns, inode)`` of each top-level chunk, by path: what the
    #: fingerprint is computed from.
    parquet_stats: Mapping[Path, tuple[int, int, int]] = field(default_factory=dict)


def chunk_fingerprint(stats: Mapping[Path, tuple[int, int, int]]) -> str:
    """One digest over every chunk's name, size, mtime and inode.

    The footer check reads each top-level chunk's last four bytes, which can
    only change if the chunk is added, removed, renamed into place (a new
    inode), appended to or rewritten (size, mtime). Any of those changes the
    digest, so a session whose digest matches has the footers last verified.
    """
    digest = hashlib.sha256()
    for path in sorted(stats, key=lambda chunk: chunk.name):
        size, mtime_ns, inode = stats[path]
        digest.update(f"{path.name}\0{size}\0{mtime_ns}\0{inode}\n".encode())
    return digest.hexdigest()


def scan_session(session: Path) -> SessionScan:
    """Walk ``session`` once, stat'ing each file once."""
    total = 0
    files = 0
    parquet: dict[Path, tuple[int, int, int]] = {}
    pending = [(session, True)]
    while pending:
        directory, top_level = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append((Path(entry.path), False))
                    continue
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            files += 1
            total += stat.st_size
            if top_level and entry.name.endswith(".parquet"):
                parquet[Path(entry.path)] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    return SessionScan(
        path=session,
        total_bytes=total,
        parquet_sizes={path: stats[0] for path, stats in parquet.items()},
        fingerprint=chunk_fingerprint(parquet),
        files_stat=files,
        parquet_stats=parquet,
    )


def scan_sessions(sessions_root: Path) -> dict[Path, SessionScan]:
    """:func:`scan_session` for every timestamp-named session directory.

    ``_archive``, ``_replay-*`` and anything else not named like a session is
    not walked: nothing in a tick sizes or footer-checks it.
    """
    if not sessions_root.exists():
        return {}
    return {
        child: scan_session(child)
        for child in sorted(sessions_root.iterdir())
        if child.is_dir() and _is_session_name(child.name)
    }


#: Bumped when the ledger's shape or :func:`chunk_fingerprint` changes: a file
#: of another version is ignored, so every footer is read again.
FOOTER_LEDGER_VERSION = 2


@dataclass
class FooterLedger:
    """Sessions whose parquet footers were all read and found intact.

    Kept by the retention sweeper across ticks (S01.5). Keyed to the
    session's :attr:`SessionScan.fingerprint` when it was verified: a session
    that gains, loses or rewrites a chunk no longer matches and is read again,
    so a truncated chunk that appears later is still caught.

    With a ``path`` (:meth:`load`), the ledger also survives a restart: the
    first tick after a start used to read every footer in the corpus (70.5 s
    after the 2026-10-04 cold boot, during a drive). The file is a cache.
    Missing, unreadable, of another version or written for another sessions
    root, it is ignored and every footer is read again; it never fails a tick.
    """

    _verified: dict[Path, str] = field(default_factory=dict)
    path: Path | None = None
    _dirty: bool = False

    @classmethod
    def load(cls, path: Path, sessions_root: Path) -> FooterLedger:
        """The ledger persisted at ``path`` for ``sessions_root``, or an empty one."""
        ledger = cls(path=path)
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return ledger
        except (OSError, ValueError) as exc:
            log.warning(
                "footer ledger unreadable; every footer will be read",
                extra={"path": str(path), "error": str(exc)},
            )
            return ledger
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or raw.get("version") != FOOTER_LEDGER_VERSION
            or raw.get("sessions_root") != str(sessions_root)
            or not isinstance(sessions, dict)
        ):
            log.warning(
                "footer ledger is for another version or sessions root; every footer will be read",
                extra={"path": str(path), "sessions_root": str(sessions_root)},
            )
            return ledger
        for name, fingerprint in sessions.items():
            # Only plain live session names: a key such as "../x" or "_archive"
            # could otherwise name a path the sweep never verified.
            if isinstance(name, str) and isinstance(fingerprint, str) and _is_session_name(name):
                ledger._verified[sessions_root / name] = fingerprint
        return ledger

    def is_verified(self, scan: SessionScan) -> bool:
        return self._verified.get(scan.path) == scan.fingerprint

    def mark_verified(self, path: Path, fingerprint: str) -> None:
        if self._verified.get(path) != fingerprint:
            self._verified[path] = fingerprint
            self._dirty = True

    def forget(self, path: Path) -> None:
        if self._verified.pop(path, None) is not None:
            self._dirty = True

    def forget_missing(self, present: Iterable[Path]) -> None:
        """Drop sessions that no longer exist (deleted by this or another sweep)."""
        keep = set(present)
        for path in [path for path in self._verified if path not in keep]:
            self.forget(path)

    def save(self, sessions_root: Path, *, exclude: Iterable[Path] = ()) -> bool:
        """Persist to :attr:`path` if anything changed; ``True`` if written.

        ``exclude`` (the session being recorded) is never written: an open
        session is never trusted from a previous process's word. A failed
        write is logged and leaves the next start to read every footer.
        """
        if self.path is None or not self._dirty:
            return False
        skip = {path.resolve() for path in exclude}
        sessions = {
            path.name: fingerprint
            for path, fingerprint in sorted(self._verified.items())
            if path.parent == sessions_root and path.resolve() not in skip
        }
        document = {
            "version": FOOTER_LEDGER_VERSION,
            "sessions_root": str(sessions_root),
            "sessions": sessions,
        }
        try:
            atomic_write_text(self.path, json.dumps(document, indent=0) + "\n", mkdir=True)
        except OSError as exc:
            log.warning(
                "footer ledger not saved; the next start reads every footer",
                extra={"path": str(self.path), "error": str(exc)},
            )
            return False
        self._dirty = False
        return True


def delete_expired_sessions(
    sessions_root: Path,
    retention_days: int,
    *,
    now: datetime | None = None,
    protect: Iterable[Path] | None = None,
    catalog_db: Path | None = None,
) -> list[Path]:
    """Delete timestamp-named session directories older than retention_days.

    ``protect`` is an explicit safety rail for live sessions. A protected
    directory is never removed even if its timestamp is older than the
    retention cutoff. Pin files keep their existing behavior. ``catalog_db``
    (Task 6.3), when given, drops the matching catalog row for every
    directory actually deleted; ``None`` (the default) skips it.
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
    for child in iter_sessions(sessions_root, kinds=LIVE_SESSION_KINDS):
        started_at = session_sort_key(child)[0]
        if child.resolve() in protected:
            continue
        if (child / ".canary-pin").exists():
            continue
        if started_at < cutoff:
            shutil.rmtree(child)
            deleted.append(child)
    _delete_catalog_rows(catalog_db, deleted)
    return deleted


def delete_expired_replays(
    sessions_root: Path,
    replay_retention_days: int,
    *,
    now: datetime | None = None,
    protect: Iterable[Path] | None = None,
    catalog_db: Path | None = None,
) -> list[Path]:
    """Evict ``_replay-<replay_id>`` directories older than the configured age.

    Mirrors :func:`delete_expired_sessions` but for the isolated replay
    output tree. The replay_id is the same sortable
    ``YYYY-MM-DDTHHMMSS`` form used by live sessions, just with a
    ``_replay-`` prefix that excludes the dir from training discovery
    and the live-session listing. Anything under ``sessions_root`` that
    doesn't match the prefix + timestamp shape is ignored. ``catalog_db``
    (Task 6.3) — see :func:`delete_expired_sessions`.
    """
    if replay_retention_days < 0:
        raise ValueError("replay_retention_days must be non-negative")
    if not sessions_root.exists():
        return []

    protected = {path.resolve() for path in protect or ()}
    cutoff = (datetime.now(UTC) if now is None else now.astimezone(UTC)) - timedelta(
        days=replay_retention_days
    )
    deleted: list[Path] = []
    for child in iter_sessions(sessions_root, kinds={SessionKind.REPLAY}):
        started_at = session_sort_key(child)[0]
        if child.resolve() in protected:
            continue
        if started_at < cutoff:
            shutil.rmtree(child)
            deleted.append(child)
    _delete_catalog_rows(catalog_db, deleted)
    return deleted


@dataclass(frozen=True)
class EmptySessionSweep:
    """What :func:`delete_empty_sessions` removed in one pass."""

    removed_sessions: list[Path]  # husk dirs with no surviving data
    removed_files: list[Path]  # 0-byte parquet dropped from kept sessions
    #: Parquet files opened to read their footer (S01.5: zero on a tick over
    #: sessions already verified).
    footers_read: int = 0


def delete_empty_sessions(
    sessions_root: Path,
    *,
    now: datetime | None = None,
    protect: Iterable[Path] | None = None,
    min_age_seconds: float = 120.0,
    catalog_db: Path | None = None,
    scans: Mapping[Path, SessionScan] | None = None,
    footers: FooterLedger | None = None,
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
    the whole directory if no data survives anywhere in its tree. ``catalog_db``
    (Task 6.3) — see :func:`delete_expired_sessions`.

    ``scans`` (this tick's :func:`scan_sessions`) replaces the per-session
    glob and tree walk; ``footers`` skips sessions whose footers were all
    read on an earlier tick and have not changed since (S01.5). Without them
    every aged session is walked and every footer read, as before.
    """
    if not sessions_root.exists():
        return EmptySessionSweep([], [])

    protected = {path.resolve() for path in protect or ()}
    now_dt = datetime.now(UTC) if now is None else now.astimezone(UTC)
    removed_sessions: list[Path] = []
    removed_files: list[Path] = []
    footers_read = 0
    children = list(scans) if scans is not None else sorted(sessions_root.iterdir())
    for child in children:
        if not child.is_dir():
            continue
        started_at = _live_started_at(child.name)
        if started_at is None:
            continue
        if child.resolve() in protected or (child / ".canary-pin").exists():
            continue
        if (now_dt - started_at).total_seconds() < min_age_seconds:
            continue
        scan = scans[child] if scans is not None else scan_session(child)
        if footers is not None and footers.is_verified(scan):
            continue
        removed_here: dict[Path, int] = {}
        all_read = True
        for parquet, size in scan.parquet_sizes.items():
            state = _footer_state(parquet, size)
            if size >= _MIN_PARQUET_BYTES:  # below it the footer is not read
                footers_read += 1
            if state == "unreadable":
                all_read = False
                continue
            if state == "truncated":
                try:
                    parquet.unlink()
                except OSError:
                    all_read = False
                    continue
                removed_files.append(parquet)
                removed_here[parquet] = size
        if scan.total_bytes - sum(removed_here.values()) == 0:
            shutil.rmtree(child)
            removed_sessions.append(child)
            continue
        if footers is not None:
            if all_read:
                # Verified as the next tick will scan it: without the chunks
                # removed (removing a file changes no sibling's stat).
                kept = {
                    path: stats
                    for path, stats in scan.parquet_stats.items()
                    if path not in removed_here
                }
                footers.mark_verified(child, chunk_fingerprint(kept))
            else:
                footers.forget(child)
    _delete_catalog_rows(catalog_db, removed_sessions)
    return EmptySessionSweep(removed_sessions, removed_files, footers_read)


def enforce_storage_cap(
    sessions_root: Path,
    max_mb: int,
    *,
    protect: Iterable[Path] | None = None,
    catalog_db: Path | None = None,
    sizes: Mapping[Path, int] | None = None,
) -> list[Path]:
    """Evict oldest session directories until total session bytes are under cap.

    This is the byte-bounded ring buffer: ``delete_expired_sessions`` bounds
    sessions by *age*; this bounds them by *total size*. Only timestamp-named
    session directories are measured and evicted — ``scratch`` / ``_archive``
    and other non-session entries are neither counted nor removed.

    Eviction is oldest-first by parsed start time
    (:func:`src.session.iter_sessions`, R01.1).
    A directory containing ``.canary-pin`` or listed in ``protect`` (the live
    session dir) is never deleted, even if that means staying over the cap.
    Returns the deleted directories, oldest first. ``catalog_db`` (Task 6.3) —
    see :func:`delete_expired_sessions`. ``sizes`` (bytes by session directory,
    from this tick's :func:`scan_sessions`) saves walking every tree again;
    a session missing from it is walked.
    """
    if max_mb <= 0:
        raise ValueError("max_mb must be positive")
    if not sessions_root.exists():
        return []

    protected = {path.resolve() for path in protect or ()}
    cap_bytes = max_mb * 1024 * 1024

    sessions: list[tuple[Path, int]] = []
    for child in iter_sessions(sessions_root, kinds=LIVE_SESSION_KINDS):
        size = sizes.get(child) if sizes is not None else None
        sessions.append((child, dir_size(child) if size is None else size))

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
    _delete_catalog_rows(catalog_db, deleted)
    return deleted


def emergency_evict_for_free_space(
    sessions_root: Path,
    warning_mb: float,
    *,
    protect: Iterable[Path] | None = None,
    free_space_mb: Callable[[Path], float] = _default_free_space_mb,
    catalog_db: Path | None = None,
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
    ``catalog_db`` (Task 6.3) — see :func:`delete_expired_sessions`.
    """
    if warning_mb < 0:
        raise ValueError("warning_mb must be non-negative")
    if not sessions_root.exists():
        return []
    if free_space_mb(sessions_root) >= warning_mb:
        return []

    protected = {path.resolve() for path in protect or ()}
    deleted: list[Path] = []
    for child in iter_sessions(sessions_root, kinds=LIVE_SESSION_KINDS):
        if free_space_mb(sessions_root) >= warning_mb:
            break
        if child.resolve() in protected:
            continue
        if (child / ".canary-pin").exists():
            continue
        shutil.rmtree(child)
        deleted.append(child)
    _delete_catalog_rows(catalog_db, deleted)
    return deleted


__all__ = [
    "EmptySessionSweep",
    "FooterLedger",
    "FOOTER_LEDGER_VERSION",
    "SessionScan",
    "chunk_fingerprint",
    "delete_empty_sessions",
    "delete_expired_replays",
    "delete_expired_sessions",
    "emergency_evict_for_free_space",
    "enforce_storage_cap",
    "scan_session",
    "scan_sessions",
]
