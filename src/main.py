"""Entry point for the CANary daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Sequence
from pathlib import Path

from src.config import CanaryConfig
from src.log import configure_logging

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
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO).",
    )
    return parser.parse_args(argv)


async def run(config: CanaryConfig, stop_event: asyncio.Event | None = None) -> None:
    """Start the daemon and block until the stop event fires.

    If `stop_event` is None, one is created and wired to SIGTERM/SIGINT. Tests
    pass an explicit event so they can trigger shutdown without signals.
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

    # TODO(Phase 1+): start collectors → writer → inference → dashboard here.

    await stop_event.wait()
    log.info("canary stopping")


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
