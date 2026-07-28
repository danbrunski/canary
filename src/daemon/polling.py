from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from src.collection import tx_gate
from src.config import CanaryConfig
from src.decode.behavior_models import load_behavior_models, unknown_operating_modes
from src.models.health import HealthMonitor
from src.models.manager import ModelManager
from src.preprocessing.live import LiveFeatureSource

log = logging.getLogger(__name__)

#: Seconds-scale is plenty for a minutes-scale TTL (matches the UPS
#: watchdog's own poll cadence) without adding meaningful drift to when an
#: expired window actually clears.
DEFAULT_PASSIVE_TTL_POLL_SECONDS = 2.0


async def poll_passive_ttl(
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = DEFAULT_PASSIVE_TTL_POLL_SECONDS,
) -> None:
    """Auto-disarm passive mode once its TTL (if any) expires.

    Unconditional, always-on background task — mirrors ``run_retention_sweeper``
    rather than the ``live_source``-gated hot-reload tasks below, since passive
    mode has nothing to do with live inference. ``tx_gate.check_expiry`` is a
    cheap no-op when nothing is armed or no TTL was set, so this costs nothing
    on the common path.
    """
    while not stop_event.is_set():
        tx_gate.check_expiry()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


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
                # Partial-deploy detector: a stale binary next to a fresh
                # manifest means /status's manifest-derived versions lie
                # about what's scoring. Checked every poll (not just on
                # swap) so it also clears once the binary lands.
                current = manager.current
                if current is not None:
                    health.observe_model_version_mismatch(
                        name,
                        serving=current.serving_version,
                        manifest=current.manifest.model_version,
                    )
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
                    mismatched = unknown_operating_modes(
                        new_models,
                        live_source.config.preprocessing.operating_modes,
                    )
                    if mismatched:
                        log.warning(
                            "behavior models gated on operating modes the live "
                            "segmenter can't emit — they will NEVER evaluate; "
                            "add the modes to preprocessing.operating_modes",
                            extra={"models": mismatched},
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
    "DEFAULT_PASSIVE_TTL_POLL_SECONDS",
    "poll_behavior_models",
    "poll_config",
    "poll_model_managers",
    "poll_passive_ttl",
]
