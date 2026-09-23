from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.collection import bus_budget, tx_gate
from src.config import CanaryConfig
from src.decode.behavior_models import (
    behavior_policy_path,
    diagnostic_only_models,
    load_behavior_models,
    unknown_operating_modes,
)
from src.models.health import HealthMonitor
from src.models.manager import ModelManager

if TYPE_CHECKING:
    # Annotation only: it loads the feature pipeline, and this module is on
    # the daemon's path to recording (S01.3).
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


async def poll_bus_budget_ttl(
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = DEFAULT_PASSIVE_TTL_POLL_SECONDS,
) -> None:
    """Revert an OBD bus-budget override once its TTL expires.

    Deliberately a separate task from :func:`poll_passive_ttl` rather than a
    second line in its loop: the two gates are independent, and folding this
    in would have meant renaming that function plus its references across
    another session's freshly-landed code for no behavioural gain. The cost
    is one extra task that sleeps.

    Matters more than the passive TTL does, because a forgotten override is
    *quiet* — it just keeps polling at up to 25x the intended rate. See
    ``src.collection.bus_budget``.
    """
    while not stop_event.is_set():
        bus_budget.check_expiry()
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

    With ``health``, the first load of each model and the end of the first
    pass over all of them are startup stages (S01.1).
    """
    while not stop_event.is_set():
        # Snapshot: a live sequence-detector swap (Phase 9.6) mutates this dict
        # from the control thread, which would otherwise raise "dict changed
        # size during iteration" here.
        for name, manager in list(managers.items()):
            try:
                # A swap is a full artifact load (seconds warm, tens of
                # seconds cold off the SD card); run on the loop it stalled
                # collection, HTTP and every sink for that long.
                attempt = await asyncio.to_thread(manager.poll)
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
                    health.startup.mark_keyed("model_loaded", name)
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
        if health is not None:
            health.startup.mark("models_polled")
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


def _behavior_stamp(path: Path, policy_path: Path) -> tuple[float, float | None] | None:
    """``(artifact mtime, policy mtime)``; ``None`` while the artifact is absent."""
    if not path.exists():
        return None
    policy_mtime = policy_path.stat().st_mtime if policy_path.exists() else None
    return (path.stat().st_mtime, policy_mtime)


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

    B03.2/B03.10 — the sibling ``behavior_policy.yaml`` is part of the stamp:
    editing a demotion reapplies it without a restart. Any stamp *change*
    reloads (not only a newer one), so restoring the prior artifact with its
    original mtime (``cp -p``, ``git checkout`` of an old tree) is picked up
    as a rollback — and the reloaded artifact is governed by the same policy.
    """
    policy_path = behavior_policy_path(path)
    last_stamp = _behavior_stamp(path, policy_path)
    while not stop_event.is_set():
        try:
            if path.exists():
                stamp = _behavior_stamp(path, policy_path)
                if last_stamp is None or stamp != last_stamp:
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
                    diagnostic = diagnostic_only_models(new_models)
                    if diagnostic:
                        log.warning(
                            "hot-reloaded behavior models are diagnostic-only — "
                            "shown but not alerting until refit with a serving "
                            "contract and held-out validation",
                            extra={"models": diagnostic},
                        )
                    last_stamp = stamp
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
    "poll_bus_budget_ttl",
    "poll_passive_ttl",
]
