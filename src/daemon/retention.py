from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from src.storage.monitor import free_space_mb
from src.storage.parquet_writer import (
    delete_empty_sessions,
    delete_expired_replays,
    delete_expired_sessions,
    emergency_evict_for_free_space,
    enforce_storage_cap,
)

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

    while not stop_event.is_set():
        active = active_session_path()
        try:
            sizes_before = _session_sizes(storage_path)
            deleted = delete_expired_sessions(
                storage_path,
                retention_days,
                protect={active},
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
            husks = delete_empty_sessions(storage_path, protect={active})
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
                evicted = enforce_storage_cap(
                    storage_path,
                    storage_max_mb,
                    protect={active},
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
                active_replay = active_replay_dir() if active_replay_dir is not None else None
                protected_replays: set[Path] = (
                    {active_replay} if active_replay is not None else set()
                )
                evicted_replays = delete_expired_replays(
                    storage_path,
                    replay_retention_days,
                    protect=protected_replays,
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
                    )
                    if salvaged:
                        log.warning(
                            "emergency low-space eviction",
                            extra={
                                "count": len(salvaged),
                                "reclaimed_bytes": sum(
                                    sizes_before.get(path, 0) for path in salvaged
                                ),
                                "sessions": [str(path) for path in salvaged],
                                "storage_warning_mb": storage_warning_mb,
                                "free_mb_before": round(free_before, 1),
                                "free_mb_after": round(free_space_mb(storage_path), 1),
                                "active_session": str(active),
                            },
                        )
        except Exception:  # noqa: BLE001 — cleanup must not kill capture
            log.exception("retention sweep failed", extra={"storage_path": str(storage_path)})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


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


def _session_sizes(sessions_root: Path) -> dict[Path, int]:
    if not sessions_root.exists():
        return {}
    sizes: dict[Path, int] = {}
    for child in sessions_root.iterdir():
        if child.is_dir():
            sizes[child] = _tree_size(child)
    return sizes


def _tree_size(root: Path) -> int:
    total = 0
    with suppress(OSError):
        for child in root.rglob("*"):
            with suppress(OSError):
                if child.is_file():
                    total += child.stat().st_size
    return total


__all__ = [
    "run_retention_sweeper",
]
