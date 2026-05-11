#!/usr/bin/env python3
"""Record CAN frames from the configured bus into a session directory.

Usage:
  .venv/bin/python scripts/record.py                  # record until Ctrl+C
  .venv/bin/python scripts/record.py --seconds 30     # record for 30s
  .venv/bin/python scripts/record.py --out-dir mysess

Output (per session):
  <out-dir>/<UTC-timestamp>/
    frames.log     — candump format, append-only
    manifest.json  — vehicle, bus settings, counts, timestamps

Listen-only: never transmits on the bus.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from src.collection.can_collector import bus_factory, make_session_dir, record
from src.config import CanaryConfig
from src.log import configure_logging

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="record",
        description="Record CAN frames to a timestamped session directory.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("canary.yaml"),
        help="Path to canary.yaml (default: ./canary.yaml).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data/sessions"),
        help="Directory to create the session subfolder under (default: ./data/sessions).",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="Stop after this many seconds. Default: run until SIGINT/SIGTERM.",
    )
    parser.add_argument(
        "--vehicle-state",
        default=None,
        help=(
            "Vehicle operating state during this recording. Free-form, but "
            "prefer one of: engine_off_key_on, engine_running_idle, "
            "engine_running_warmup, driving, cold_start. Stored verbatim in "
            "manifest.json so the analyzer can reason about value ranges."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)

    config = CanaryConfig.from_yaml(args.config)
    spec = config.hardware.can

    from datetime import UTC, datetime

    paths = make_session_dir(args.out_dir, datetime.now(UTC))
    log.info("session created", extra={"session_dir": str(paths.root)})

    # SIGINT/SIGTERM → stop_event so the recorder exits cleanly with a flushed
    # writer and a finalized manifest, instead of dying mid-write.
    stop_event = threading.Event()

    def _request_stop(signum: int, _frame: object) -> None:
        log.info("stop requested", extra={"signal": signum})
        stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    bus = bus_factory(spec)
    try:
        record(
            bus,
            paths,
            spec=spec,
            vehicle=config.vehicle,
            vehicle_state=args.vehicle_state,
            duration=args.seconds,
            stop_event=stop_event,
        )
    finally:
        bus.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
