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

from src.collection.can_collector import (
    DEFAULT_RECONNECT_POLICY,
    bus_factory,
    make_session_dir,
    record,
)
from src.collection.obd_collector import (
    JsonlReadingWriter,
    ObdSpec,
    obd_factory,
)
from src.config import CanaryConfig
from src.log import configure_logging
from src.storage.monitor import free_space_mb

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
        "--obd-port",
        default=None,
        help=(
            "Serial device for an ELM327-class adapter (e.g. /dev/ttyUSB0). "
            "When given, runs python-obd in parallel with the CAN recorder "
            "and writes Mode 01 readings to <session>/pids.jsonl. Enables "
            "Mode 22 ↔ Mode 01 cross-reference for binding derivation."
        ),
    )
    parser.add_argument(
        "--obd-pids",
        default="RPM,COOLANT_TEMP,INTAKE_TEMP,MAF,THROTTLE_POS,ENGINE_LOAD,"
        "CONTROL_MODULE_VOLTAGE,TIMING_ADVANCE",
        help=(
            "Comma-separated python-obd command names to watch. Default covers "
            "the PIDs most useful for cross-referencing Nissan Mode 22 DIDs."
        ),
    )
    parser.add_argument(
        "--obd-baud",
        type=int,
        default=38400,
        help="ELM327 baud rate (default 38400, sometimes 9600 for cheap clones).",
    )
    parser.add_argument(
        "--min-free-mb",
        type=int,
        default=200,
        help=(
            "Refuse to start if less than this much disk space is free under "
            "--out-dir. Set to 0 to skip the pre-flight check. Default 200MB."
        ),
    )
    parser.add_argument(
        "--no-reconnect",
        action="store_true",
        help=(
            "Disable auto-reconnect on adapter failure. By default the "
            "recorder rebuilds the bus via bus_factory on USB disconnect / "
            "adapter reset / OSError. Useful when debugging — without this "
            "flag, those errors are silently retried."
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

    # Pre-flight: refuse to start if disk space is tight. A 1-hour drive at
    # ~1600 fps × ~75 bytes per candump line is ~440 MB raw; even after the
    # parse-tolerance fixes, running out of disk mid-recording is the kind
    # of failure that loses the LAST data, which is exactly the most
    # interesting part. Better to fail fast at startup with a clear message.
    if args.min_free_mb > 0:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        free_mb = free_space_mb(args.out_dir)
        if free_mb < args.min_free_mb:
            log.error(
                "insufficient disk space; aborting",
                extra={
                    "out_dir": str(args.out_dir),
                    "free_mb": free_mb,
                    "required_mb": args.min_free_mb,
                },
            )
            print(
                f"error: only {free_mb:.0f} MB free in {args.out_dir} "
                f"(need ≥{args.min_free_mb} MB). Free some space or pass "
                f"--min-free-mb to lower the threshold.",
                file=sys.stderr,
            )
            return 1

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

    bus = None
    obd_conn = None
    obd_writer = None
    try:
        bus = bus_factory(spec)
        if args.obd_port:
            pids = tuple(p.strip() for p in args.obd_pids.split(",") if p.strip())
            obd_writer = JsonlReadingWriter(paths.pids)
            obd_spec = ObdSpec(port=args.obd_port, baudrate=args.obd_baud, pids=pids)
            log.info(
                "starting parallel OBD capture",
                extra={"port": obd_spec.port, "pids": list(pids)},
            )
            obd_conn = obd_factory(obd_spec, on_reading=obd_writer.write)
            obd_conn.start()

        # Pass factory + reconnect_policy so a USB hiccup / adapter reset /
        # OSError doesn't kill the recording mid-drive. The factory lets
        # record() rebuild the bus into the same writer, so frames.log stays
        # one continuous file across reconnects.
        record(
            bus,
            paths,
            spec=spec,
            vehicle=config.vehicle,
            vehicle_state=args.vehicle_state,
            duration=args.seconds,
            stop_event=stop_event,
            factory=None if args.no_reconnect else bus_factory,
            reconnect_policy=None if args.no_reconnect else DEFAULT_RECONNECT_POLICY,
        )
    finally:
        if obd_conn is not None:
            obd_conn.stop()
        if obd_writer is not None:
            obd_writer.close()
        if bus is not None:
            bus.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
