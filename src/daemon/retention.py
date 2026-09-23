from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from src.storage.monitor import free_space_mb
from src.storage.parquet_writer import (
    delete_empty_sessions,
    delete_expired_replays,
    delete_expired_sessions,
    emergency_evict_for_free_space,
    enforce_storage_cap,
)
from src.storage.retention import FooterLedger, scan_sessions

from .components import DaemonComponents

log = logging.getLogger(__name__)


async def run_retention_sweeper(
    storage_path: Path,
    retention_days: int,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
    active_session_path: Callable[[], Path],
    storage_max_mb: int | None = None,
    storage_warning_mb: int | None = None,
    replay_retention_days: int | None = None,
    active_replay_dir: Callable[[], Path | None] | None = None,
    catalog_db: Path | None = None,
) -> None:
    """Periodically prune session dirs from the production daemon.

    Each tick runs, in order: an age sweep (``delete_expired_sessions``); a
    byte-cap sweep (``enforce_storage_cap``) when ``storage_max_mb`` is set —
    the bounded ring buffer; and, when ``storage_warning_mb`` is set, an
    emergency low-space eviction (``emergency_evict_for_free_space``) if the
    filesystem free space has dropped below the threshold — the acting
    counterpart to ``warn_if_storage_low``'s log-only hot-path check.

    The active session is read through ``active_session_path`` on every tick,
    not captured once at startup, because ``ParquetTelemetryWriter`` rotates
    sessions when engine run time resets.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    # Across ticks: sessions whose footers were read once and have not changed
    # since are not read again (S01.5).
    footers = FooterLedger()
    while not stop_event.is_set():
        # Read on the loop: both callables reach loop-owned state (the
        # writer's current session, the replay controller).
        active = active_session_path()
        active_replay = active_replay_dir() if active_replay_dir is not None else None
        try:
            # Off the loop. A tick walks every session file and opens each
            # parquet to check its footer: 26 s on the event loop at a cold
            # boot with 62k files (py-spy, 2026-09-22), during which no sink
            # ran and the collectors had not even started. A session the
            # writer rotates to mid-tick is younger than delete_empty_sessions'
            # min_age_seconds, and the one it replaced is ``active``, so
            # neither can be taken for a husk.
            await asyncio.to_thread(
                _sweep_once,
                storage_path,
                retention_days,
                active=active,
                active_replay=active_replay,
                storage_max_mb=storage_max_mb,
                storage_warning_mb=storage_warning_mb,
                replay_retention_days=replay_retention_days,
                catalog_db=catalog_db,
                footers=footers,
            )
        except Exception:  # noqa: BLE001 — cleanup must not kill capture
            log.exception("retention sweep failed", extra={"storage_path": str(storage_path)})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


def _sweep_once(
    storage_path: Path,
    retention_days: int,
    *,
    active: Path,
    active_replay: Path | None,
    storage_max_mb: int | None,
    storage_warning_mb: int | None,
    replay_retention_days: int | None,
    catalog_db: Path | None,
    footers: FooterLedger | None = None,
) -> None:
    """One retention tick; blocking filesystem work, run in a thread.

    One walk per tick (S01.5): the scan taken first supplies the reclaimed
    bytes of whatever is deleted, the husk check's file list, and the byte
    cap's session sizes. ``footers`` carries the sessions already verified
    by earlier ticks, whose footers are not read again. Every tick logs its
    duration and how many files it stat'ed and opened.
    """
    started = time.monotonic()
    scans = scan_sessions(storage_path)
    sizes_before = {path: scan.total_bytes for path, scan in scans.items()}
    deleted = delete_expired_sessions(
        storage_path,
        retention_days,
        protect={active},
        catalog_db=catalog_db,
    )
    if deleted:
        log.info(
            "expired sessions deleted",
            extra={
                "count": len(deleted),
                "reclaimed_bytes": sum(sizes_before.get(path, 0) for path in deleted),
                "sessions": [str(path) for path in deleted],
                "active_session": str(active),
            },
        )
    husks = delete_empty_sessions(
        storage_path, protect={active}, catalog_db=catalog_db, scans=scans, footers=footers
    )
    if husks.removed_sessions or husks.removed_files:
        log.info(
            "empty session husks cleaned",
            extra={
                "removed_sessions": len(husks.removed_sessions),
                "removed_zero_byte_files": len(husks.removed_files),
                "active_session": str(active),
            },
        )
    if storage_max_mb is not None:
        removed_files = set(husks.removed_files)
        sizes_now = {
            path: scan.total_bytes
            - sum(size for chunk, size in scan.parquet_sizes.items() if chunk in removed_files)
            for path, scan in scans.items()
        }
        evicted = enforce_storage_cap(
            storage_path,
            storage_max_mb,
            protect={active},
            catalog_db=catalog_db,
            sizes=sizes_now,
        )
        if evicted:
            log.info(
                "storage cap evicted sessions",
                extra={
                    "count": len(evicted),
                    "reclaimed_bytes": sum(sizes_before.get(path, 0) for path in evicted),
                    "sessions": [str(path) for path in evicted],
                    "storage_max_mb": storage_max_mb,
                    "active_session": str(active),
                },
            )
    if replay_retention_days is not None:
        protected_replays: set[Path] = {active_replay} if active_replay is not None else set()
        evicted_replays = delete_expired_replays(
            storage_path,
            replay_retention_days,
            protect=protected_replays,
            catalog_db=catalog_db,
        )
        if evicted_replays:
            log.info(
                "expired replays deleted",
                extra={
                    "count": len(evicted_replays),
                    "replays": [str(path) for path in evicted_replays],
                    "replay_retention_days": replay_retention_days,
                },
            )
    if storage_warning_mb is not None:
        free_before = free_space_mb(storage_path)
        if free_before < storage_warning_mb:
            salvaged = emergency_evict_for_free_space(
                storage_path,
                storage_warning_mb,
                protect={active},
                catalog_db=catalog_db,
            )
            if salvaged:
                log.warning(
                    "emergency low-space eviction",
                    extra={
                        "count": len(salvaged),
                        "reclaimed_bytes": sum(sizes_before.get(path, 0) for path in salvaged),
                        "sessions": [str(path) for path in salvaged],
                        "storage_warning_mb": storage_warning_mb,
                        "free_mb_before": round(free_before, 1),
                        "free_mb_after": round(free_space_mb(storage_path), 1),
                        "active_session": str(active),
                    },
                )

    if footers is not None:
        footers.forget_missing(path for path in scans if path.exists())
    log.info(
        "retention tick",
        extra={
            "duration_ms": round((time.monotonic() - started) * 1000.0, 1),
            "sessions": len(scans),
            "files_stat": sum(scan.files_stat for scan in scans.values()),
            "footers_read": husks.footers_read,
        },
    )


def _active_replay_dir(components: DaemonComponents) -> Path | None:
    """Return the in-flight replay's session directory, or ``None`` if idle.

    Protects the active replay from the ``_replay-*`` retention sweep so a
    multi-day replay isn't yanked out from under itself by its own scheduler.
    """
    controller = components.replay_controller
    if controller is None:
        return None
    status = controller.status()
    if not status.running or status.output_session_dir is None:
        return None
    return Path(status.output_session_dir)


__all__ = [
    "run_retention_sweeper",
]
