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

With --obd-port, OBD-II PID polling runs in a supervised daemon thread that
reconnects when the adapter drops and reappears (e.g. a flaky CH340 over
WSL-USB). An OBD failure never aborts the CAN capture — the CAN side is the
priority; pids.jsonl is best-effort.
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
    resolve_pids,
    supervise_obd_capture,
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
        default=(
            "RPM,SPEED,COOLANT_TEMP,INTAKE_TEMP,INTAKE_PRESSURE,MAF,THROTTLE_POS,"
            "ENGINE_LOAD,TIMING_ADVANCE,SHORT_FUEL_TRIM_1,LONG_FUEL_TRIM_1,"
            "SHORT_FUEL_TRIM_2,LONG_FUEL_TRIM_2,O2_B1S1,O2_B2S1,BAROMETRIC_PRESSURE,"
            "CONTROL_MODULE_VOLTAGE,RUN_TIME,FUEL_STATUS"
        ),
        help=(
            "Comma-separated python-obd command names to watch. Default is the "
            "fuller set for ML + behavior-model work (RPM/speed/MAP/MAF/trims/O2/"
            "timing/baro/...); ~19 PIDs polls at roughly 1 Hz each through one "
            "ELM327. Unsupported PIDs are warned-and-skipped. Pass a leaner list "
            "for transient-heavy segments. See docs/data-collection-plan.md."
        ),
    )
    parser.add_argument(
        "--obd-dids",
        default="",
        help=(
            "Comma-separated 4-hex Mode 22 DIDs to poll alongside --obd-pids "
            "(e.g. '1101,F190'). Each DID is appended to the python-obd "
            "Async watch loop with a passthrough decoder; the response bytes "
            "land in pids.jsonl as a hex string. Use this for known DIDs you "
            "want logged through a drive (injector PW, ignition timing) — for "
            "exploratory sweeps use tools/scan_constant_dids.py instead. Empty "
            "by default; ISO-TP multi-frame DIDs may need parsing downstream."
        ),
    )
    parser.add_argument(
        "--obd-did-header",
        default="7E0",
        help=(
            "Request header for Mode 22 DID polling (default 7E0 = engine ECU). "
            "Other modules: 7E1 = trans, 7E2 = ABS, etc."
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
        "--max-reconnects",
        type=int,
        default=10,
        help=(
            "Cap consecutive failed bus-rebuild attempts. Once exceeded, "
            "the recording stops with stopped_by='reconnect_exhausted' so "
            "we don't spin forever against a persistently broken adapter "
            "(which on WSL2 can lead to uninterruptible kernel I/O state). "
            "Default 10."
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
    # Two-press behavior: first Ctrl-C requests a clean stop, second restores
    # the default handler so a third press raises KeyboardInterrupt and a
    # fourth would terminate immediately. This escape hatch matters because
    # in WSL2 with a wedged USB adapter the recorder's reconnect logic can
    # land in uninterruptible kernel I/O (D state) where SIGKILL won't even
    # land. The user shouldn't have to `wsl --shutdown` to recover.
    stop_event = threading.Event()
    interrupt_count = 0

    def _request_stop(signum: int, _frame: object) -> None:
        nonlocal interrupt_count
        interrupt_count += 1
        log.info(
            "stop requested",
            extra={"signal": signum, "press_count": interrupt_count},
        )
        stop_event.set()
        if interrupt_count >= 2:
            # Restore default handler — next Ctrl-C raises KeyboardInterrupt
            # and propagates out of any Python-level loop.
            print(
                "\nSecond stop requested — restoring default signal handler. "
                "Press Ctrl-C again to force exit.",
                file=sys.stderr,
            )
            signal.signal(signal.SIGINT, signal.SIG_DFL)

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    bus = None
    obd_thread = None
    obd_writer = None
    try:
        bus = bus_factory(spec)
        if args.obd_port:
            pids = tuple(p.strip() for p in args.obd_pids.split(",") if p.strip())
            dids = tuple(d.strip() for d in args.obd_dids.split(",") if d.strip())
            resolve_pids(pids)  # fail fast on a typo, before the CAN capture starts
            for did in dids:
                if len(did) != 4 or any(c not in "0123456789abcdefABCDEF" for c in did):
                    raise ValueError(f"--obd-dids: {did!r} is not a 4-character hex DID")
            obd_writer = JsonlReadingWriter(paths.pids)
            obd_spec = ObdSpec(
                port=args.obd_port,
                baudrate=args.obd_baud,
                pids=pids,
                dids=dids,
                did_header=args.obd_did_header,
            )
            log.info(
                "starting parallel OBD capture (supervised)",
                extra={
                    "port": obd_spec.port,
                    "pids": list(pids),
                    "dids": list(dids),
                    "reconnect": not args.no_reconnect,
                },
            )
            # Run OBD in a daemon thread that survives the adapter dropping out
            # and reattaching (e.g. usbipd losing a flaky CH340 over WSL-USB):
            # it watches the device file + python-obd's connection state, and
            # on a drop tears down + rebuilds into the same pids.jsonl writer.
            # OBD failure never aborts the CAN capture — the CAN side is the
            # priority; the PID sidecar is best-effort.
            obd_thread = threading.Thread(
                target=supervise_obd_capture,
                kwargs={
                    "spec": obd_spec,
                    "on_reading": obd_writer.write,
                    "stop_event": stop_event,
                    "factory": obd_factory,
                    "reconnect_policy": None if args.no_reconnect else DEFAULT_RECONNECT_POLICY,
                },
                name="obd-supervisor",
                daemon=True,
            )
            obd_thread.start()

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
            max_reconnects=args.max_reconnects,
        )
    finally:
        stop_event.set()  # signal the OBD supervisor to wind down
        if obd_thread is not None:
            obd_thread.join(timeout=15.0)
            if obd_thread.is_alive():
                log.warning("obd supervisor thread did not exit within 15s; abandoning it")
        if obd_writer is not None:
            obd_writer.close()
        if bus is not None:
            bus.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
