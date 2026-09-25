"""Entry point for the CANary daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Sequence
from pathlib import Path

from src.collection import tx_gate
from src.config import CanaryConfig
from src.log import configure_logging
from src.profiles import resolve_active_profile, scope_config
from src.startup import StartupTimeline

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="canary",
        description="CANary on-vehicle anomaly-detection daemon.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("canary.yaml"),
        help="Path to canary.yaml configuration file (default: ./canary.yaml).",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help=(
            "Force a vehicle profile slug (skips the startup VIN probe). Use for "
            "sim/replay or to target a specific vehicle's models. Default: resolve "
            "the profile by reading the VIN at startup."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args(argv)


async def run(
    config: CanaryConfig,
    stop_event: asyncio.Event | None = None,
    *,
    start_collectors: bool = True,
    startup: StartupTimeline | None = None,
    passive_state: Path | None = None,
) -> None:
    """Start the daemon and block until the stop event fires.

    If ``stop_event`` is None, one is created and wired to SIGTERM/SIGINT. Tests
    pass an explicit event so they can trigger shutdown without signals.

    Runs the assembled daemon — collectors + writer + telemetry broadcaster +
    model manager polling + dashboard server. The ``--collection-only``
    fallback (a second, synchronous drain with no decode, arming or GPS sink)
    was retired by operator decision 2026-09-25 (R03.11).

    ``startup`` (S01.1) is the timeline :func:`main` began at process start;
    the daemon serves it as ``/status``'s ``startup`` block.
    ``passive_state`` is the file :func:`main` restored an armed passive from
    before the VIN probe (D03.30); ``run_daemon`` keeps writing it.
    """
    if stop_event is None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

    log.info(
        "canary starting",
        extra={
            "vehicle": f"{config.vehicle.year} {config.vehicle.make} {config.vehicle.model}",
            "obd_poll_rate_hz": config.collection.obd_poll_rate_hz,
            "storage_path": str(config.collection.storage_path),
        },
    )
    if startup is not None:
        startup.mark("canary_starting")

    if not start_collectors:
        await stop_event.wait()
    else:
        # Lazy import: ``src.daemon`` pulls in the dashboard deps (uvicorn).
        from src.daemon import run_daemon

        if startup is not None:
            startup.mark("daemon_imported")
        await run_daemon(config, stop_event, startup=startup, passive_state=passive_state)
    log.info("canary stopping")


def main(argv: Sequence[str] | None = None) -> None:
    # S01.1: measured from the process start (/proc/self/stat), so the
    # interpreter and this module's imports are inside the first stage.
    startup = StartupTimeline()
    startup.mark("main_entered")
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)
    # D03.30: an armed passive persisted by the previous process is restored
    # before the VIN probe below, which transmits Mode 09. Resolved on the
    # unscoped config: passive is a bus state, not a per-vehicle one.
    passive_state = tx_gate.passive_state_path(config)
    tx_gate.restore_persisted(passive_state)
    if config.profiles.enabled:
        # Identify the vehicle (by VIN) and scope every vehicle-specific path
        # under its profile slug, so two vehicles never share a corpus/models.
        profile = resolve_active_profile(config, override_slug=args.profile)
        config = scope_config(config, profile)
        log.info(
            "active vehicle profile",
            extra={
                "slug": profile.slug,
                "profile_name": profile.name or None,
                "vin": profile.vin or None,
            },
        )
    startup.mark("profile_resolved")
    asyncio.run(run(config, startup=startup, passive_state=passive_state))


if __name__ == "__main__":
    main()
