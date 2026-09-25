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

Default CAN-only recording never transmits on the bus.

With --obd-source canable, the same Canable records broadcast frames and issues
active OBD ISO-TP requests, writing decoded readings to pids.jsonl. With
--obd-port / --obd-source elm327, the legacy ELM327 path runs in a supervised
daemon thread.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from _cli import add_common_args, load_config

from src.collection.can_collector import (
    DEFAULT_RECONNECT_POLICY,
    bus_factory,
    make_session_dir,
    record,
)
from src.collection.can_obd_collector import (
    CanObdSpec,
    parse_can_id,
    record_can_obd,
    resolve_did_session_spec,
)
from src.collection.obd_collector import (
    JsonlReadingWriter,
    ObdSpec,
    obd_factory,
    resolve_pids,
    supervise_obd_capture,
)
from src.log import configure_logging
from src.paths import DIDS_YAML
from src.session import PID_FILENAME
from src.storage.monitor import free_space_mb

log = logging.getLogger(__name__)


def _resolve_did_names_to_hex(dids: Sequence[str]) -> tuple[str, ...]:
    """Translate symbolic DID names (``ECM_AIR_CHARGE``) to 4-char hex.

    Names not found in ``data/dids.yaml`` and inputs that already look like
    hex pass through unchanged. The downstream validator catches any
    leftover garbage (typos that match neither a name nor a valid hex).
    Returns a tuple to match the ``ObdSpec.dids`` field type.
    """

    from src.decode.dids import load_did_map

    dids_path = DIDS_YAML
    if not dids_path.exists():
        return tuple(dids)
    try:
        did_map = load_did_map(dids_path)
    except Exception:  # noqa: BLE001 — let the validator catch the bad input
        return tuple(dids)
    by_name = {entry.name: f"{entry.did:04X}" for entry in did_map.dids}
    return tuple(by_name.get(d, d) for d in dids)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="record",
        description="Record CAN frames to a timestamped session directory.",
    )
    add_common_args(parser, help="Path to canary.yaml (default: ./canary.yaml).")
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
        "--obd-source",
        choices=["off", "canable", "elm327"],
        default=None,
        help=(
            "OBD sidecar source. `canable` uses the same CAN adapter for "
            "ISO-TP Mode 01/22 requests. `elm327` uses the legacy "
            "python-obd path. Default: elm327 when --obd-port is set, "
            "otherwise collection.obd_source from the config."
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
        default=None,
        help=(
            "What Mode-01 PIDs to watch. Default: collection.obd_pids from "
            "the config (itself `auto` out of the box), which "
            "queries the ECU's supported-PID bitmask at connect time and "
            "watches everything it claims — portable across vehicles, no "
            "per-vehicle tuning. Pass a comma-separated list "
            "(`RPM,SPEED,COOLANT_TEMP,...`) for an explicit subset (faster "
            "per-PID refresh at the cost of coverage); pass `auto,EXTRA_PID` "
            "to add an explicit PID on top of auto. Unsupported PIDs are "
            "warned-and-skipped."
        ),
    )
    parser.add_argument(
        "--obd-dids",
        default=None,
        help=(
            "Comma-separated 4-hex Mode 22 DIDs to poll alongside --obd-pids "
            "(e.g. '1101,F190'). Responses land in pids.jsonl as hex strings "
            "under DID_<hex> channels. Default: collection.obd_dids from the "
            "config (empty out of the box)."
        ),
    )
    parser.add_argument(
        "--obd-did-header",
        default="7E0",
        help=(
            "Request header / CAN ID for Mode 22 DID polling "
            "(default 7E0 = engine ECU). Other modules: 7E1 = trans, 7E2 = ABS, etc."
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

    config = load_config(args)
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

    # Stable handle for out-of-band stop. Written now, removed in `finally`
    # on any clean exit (signal, deadline, error). A controller reads this
    # instead of `pgrep`-ing for the process, which is unreliable when the
    # recorder runs under a shell/eval wrapper or when several sessions
    # exist on disk. A stale file (process SIGKILLed before `finally`) is
    # detected by stop_recording.py via a liveness check.
    pid_file = paths.root / PID_FILENAME
    pid_file.write_text(f"{os.getpid()}\n")
    log.info("recorder pidfile written", extra={"pid_file": str(pid_file), "pid": os.getpid()})
    print(
        f"recording {paths.root} (pid {os.getpid()}). Stop cleanly with Ctrl-C, "
        f"or from another shell:\n"
        f"  .venv/bin/python scripts/stop_recording.py --session {paths.root}",
        file=sys.stderr,
    )

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

    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    bus = None
    obd_thread = None
    obd_writer = None
    obd_source = "off"
    try:
        bus = bus_factory(spec)
        # PID/DID selection: explicit CLI wins, else fall back to the config
        # so a configured PID subset actually reaches the recorder (the
        # canable branch previously ignored config.collection.obd_pids).
        if args.obd_pids is None:
            pids = tuple(config.collection.obd_pids)
        else:
            pids = tuple(p.strip() for p in args.obd_pids.split(",") if p.strip())
        if args.obd_dids is None:
            dids = tuple(config.collection.obd_dids)
        else:
            dids = tuple(d.strip() for d in args.obd_dids.split(",") if d.strip())
        # OBD source precedence: explicit --obd-source wins; else --obd-port
        # implies elm327; else honor collection.obd_source from the config
        # ("disabled" -> off). This previously hard-defaulted to "off" and
        # never consulted the config, so scripts/record.py silently produced
        # CAN-only sessions even with `obd_source: canable` configured — the
        # heterogeneous-schema bug that starved behavior models and collapsed
        # the LSTM windows.
        obd_source = args.obd_source
        if obd_source is None:
            if args.obd_port:
                obd_source = "elm327"
            else:
                obd_source = config.collection.obd_source
                if obd_source == "disabled":
                    obd_source = "off"
        if args.obd_port and obd_source == "off":
            obd_source = "elm327"
        if obd_source == "off":
            log.warning(
                "OBD capture is OFF — this session will be CAN-only. The "
                "behavior models and LSTM windower need source=obd rows; a "
                "CAN-only session yields a heterogeneous training set and a "
                "model the Pi may reject. Set collection.obd_source in the "
                "config or pass --obd-source.",
            )

        if obd_source == "canable":
            obd_writer = JsonlReadingWriter(paths.pids)
            can_obd_spec = CanObdSpec(
                can=spec,
                poll_rate_hz=config.collection.obd_poll_rate_hz,
                pids=pids,
                dids=dids,
                did_request_id=parse_can_id(args.obd_did_header),
                request_timeout_seconds=config.collection.obd_request_timeout_seconds,
                did_session=resolve_did_session_spec(config.collection),
            )
            log.info(
                "starting Canable-native OBD capture",
                extra={
                    "pids": list(pids),
                    "dids": list(dids),
                    "did_request_id": f"0x{can_obd_spec.did_request_id:X}",
                },
            )
            # Pass factory + reconnect_policy so an in-vehicle USB hiccup /
            # adapter re-enumeration / OSError doesn't end the drive mid-
            # capture. The factory rebuilds the bus into the same writer, so
            # frames.log stays one continuous file across reconnects. This is
            # the same resilience the CAN-only record() path already has.
            record_can_obd(
                bus,
                paths,
                spec=can_obd_spec,
                vehicle=config.vehicle,
                vehicle_state=args.vehicle_state,
                duration=args.seconds,
                stop_event=stop_event,
                obd_writer=obd_writer,
                factory=None if args.no_reconnect else bus_factory,
                reconnect_policy=None if args.no_reconnect else DEFAULT_RECONNECT_POLICY,
                max_reconnects=args.max_reconnects,
            )
        else:
            if obd_source == "elm327":
                if args.obd_port is None:
                    args.obd_port = config.hardware.elm327_port
                resolve_pids(pids)  # fail fast on a typo, before the CAN capture starts
                # DIDs may be given as 4-char hex (legacy) or as symbolic
                # names from data/dids.yaml (the canary.example.yaml default,
                # e.g. ECM_AIR_CHARGE). Resolve names to hex so the
                # ELM327 path accepts the same config the Canable path uses.
                dids = _resolve_did_names_to_hex(dids)
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
                    "starting parallel ELM327 OBD capture (supervised)",
                    extra={
                        "port": obd_spec.port,
                        "pids": list(pids),
                        "dids": list(dids),
                        "reconnect": not args.no_reconnect,
                    },
                )
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
        # Remove the pidfile first: once we're in `finally` the recorder is
        # definitively stopping, and a later shutdown step (bus.shutdown on a
        # wedged adapter) could hang — we don't want stop_recording.py to
        # keep signalling a process that's already on its way out.
        pid_file.unlink(missing_ok=True)
        stop_event.set()  # signal the OBD supervisor to wind down
        if obd_thread is not None:
            obd_thread.join(timeout=15.0)
            if obd_thread.is_alive():
                log.warning("obd supervisor thread did not exit within 15s; abandoning it")
        if obd_writer is not None:
            obd_writer.close()
        if bus is not None:
            bus.shutdown()
        signal.signal(signal.SIGINT, previous_sigint_handler)
        signal.signal(signal.SIGTERM, previous_sigterm_handler)

    # Schema-enforcement boundary (Layer 1): if OBD was configured on but the
    # session captured zero readings, it is CAN-only / schema-incomplete.
    # Fail loudly with a non-zero exit so it cannot silently enter the
    # training set, where it collapses LSTM windows and starves behavior
    # models. An engine-running session with a working adapter must produce
    # OBD rows; zero means the adapter, ignition state, or ECU support is the
    # problem — surface it now, not at train time.
    if obd_source != "off":
        readings = (
            sum(1 for line in paths.pids.open() if line.strip()) if paths.pids.exists() else 0
        )
        if readings == 0:
            log.error(
                "OBD was enabled (source=%s) but this session captured ZERO "
                "OBD readings (%s) — it is CAN-only / schema-incomplete and "
                "must not enter the training set. Check the adapter, "
                "ignition/engine state, and ECU OBD support.",
                obd_source,
                paths.pids,
            )
            return 1
        log.info(
            "OBD readings captured",
            extra={"readings": readings, "obd_source": obd_source},
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
