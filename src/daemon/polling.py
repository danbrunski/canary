from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from src.config import CanaryConfig
from src.decode.behavior_models import load_behavior_models
from src.models.health import HealthMonitor
from src.models.manager import ModelManager
from src.preprocessing.live import LiveFeatureSource

log = logging.getLogger(__name__)


async def poll_model_managers(
    managers: dict[str, ModelManager[Any]],
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
    health: HealthMonitor | None = None,
) -> None:
    """Periodically call ``manager.poll()`` on each ModelManager.

    Light task: the manager's mtime check makes each tick cheap when nothing
    changed. Cadence comes from ``daemon.model_poll_seconds`` — refits are
    minute-to-hour scale so 30s default is fine.
    """
    while not stop_event.is_set():
        # Snapshot: a live sequence-detector swap (Phase 9.6) mutates this dict
        # from the control thread, which would otherwise raise "dict changed
        # size during iteration" here.
        for name, manager in list(managers.items()):
            try:
                attempt = manager.poll()
            except Exception:  # noqa: BLE001 — manager.poll already swallows + logs internally
                log.exception("model manager poll raised", extra={"manager": name})
                if health is not None:
                    health.note_swap("error")
                continue
            if health is not None:
                health.note_swap(attempt.kind, version=attempt.new_version)
            if attempt.kind == "swapped":
                log.info(
                    "model swapped",
                    extra={"manager": name, "version": attempt.new_version},
                )
            elif attempt.kind == "rejected":
                log.warning(
                    "swap rejected",
                    extra={"manager": name, "reason": attempt.detail},
                )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def poll_config(
    path: Path,
    live_source: LiveFeatureSource,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
) -> None:
    """Hot-reload operating-mode config from ``canary.yaml`` when its mtime changes.

    Swaps ``live_source.config`` so ``operating_modes`` (and the segmenter's
    recent-motion window) take effect without a restart — the common config
    edit. STRUCTURAL fields baked into the live source at construction —
    ``resample_hz``, ``rolling_window_seconds``, ``idle_transient_lookback_seconds``
    (ring-buffer size / feature window / gap horizon) — can't be hot-applied; if
    those change we log + skip the whole swap, so a half-applied structural
    change can't skew segmentation. Those (and model/fusion/emitter/hardware
    config, owned elsewhere) still need a restart. Errors log + keep the
    previous config.
    """

    def _structural(cfg: CanaryConfig) -> tuple[float, int, int]:
        p = cfg.preprocessing
        return (p.resample_hz, p.rolling_window_seconds, p.idle_transient_lookback_seconds)

    last_mtime: float | None = path.stat().st_mtime if path.exists() else None
    while not stop_event.is_set():
        try:
            if path.exists():
                mtime = path.stat().st_mtime
                if last_mtime is None or mtime > last_mtime:
                    new_config = CanaryConfig.from_yaml(path)
                    if _structural(new_config) != _structural(live_source.config):
                        log.warning(
                            "config change touches structural fields (resample_hz / "
                            "rolling_window_seconds / idle_transient_lookback_seconds); "
                            "restart required — not hot-reloaded",
                            extra={"path": str(path)},
                        )
                    else:
                        live_source.config = new_config
                        log.info(
                            "config hot-reloaded (operating modes)",
                            extra={
                                "path": str(path),
                                "modes": sorted(new_config.preprocessing.operating_modes),
                            },
                        )
                    last_mtime = mtime
        except Exception:  # noqa: BLE001 — keep the previous config; log + retry
            log.exception("config hot-reload failed; keeping previous", extra={"path": str(path)})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def poll_behavior_models(
    path: Path,
    live_source: LiveFeatureSource,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
) -> None:
    """Hot-reload ``behavior_models.yaml`` when its mtime changes.

    Mirrors :class:`ModelManager`'s mtime-poll pattern: stat the file each
    tick, reload + swap when the mtime advances. Without this, deploying
    a refitted behavior model requires a daemon restart — the May 2026
    storm-quieting refit ran into exactly that wall, and operator
    coordination on a no-restart Pi is annoying. Reload errors log + skip;
    the previous in-memory set keeps running.
    """
    last_mtime: float | None = None
    if path.exists():
        last_mtime = path.stat().st_mtime
    while not stop_event.is_set():
        try:
            if path.exists():
                mtime = path.stat().st_mtime
                if last_mtime is None or mtime > last_mtime:
                    new_models = load_behavior_models(path)
                    live_source.behavior_models = new_models
                    log.info(
                        "behavior models hot-reloaded",
                        extra={
                            "path": str(path),
                            "count": len(new_models.models),
                            "names": [m.name for m in new_models.models],
                        },
                    )
                    last_mtime = mtime
        except Exception:  # noqa: BLE001 — keep the previous models; log + retry
            log.exception(
                "behavior model hot-reload failed; keeping previous",
                extra={"path": str(path)},
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


__all__ = [
    "poll_behavior_models",
    "poll_config",
    "poll_model_managers",
]
