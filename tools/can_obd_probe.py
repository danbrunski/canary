#!/usr/bin/env python3
"""Canable-native OBD-II probe — no ELM327 required.

Connects to the configured CAN bus, walks the ECU's Mode 01 supported-PID
bitmask, optionally polls each supported PID once for a value snapshot, and
reads stored DTCs (Mode 03). The same Canable hardware that's running the
broadcast sniffer in production can also do this — see
``docs/data-architecture.md`` for why.

Usage:
  tools/can_obd_probe.py --config canary.yaml
  tools/can_obd_probe.py --config canary.yaml --read-pids
  tools/can_obd_probe.py --config canary.yaml --no-supported-walk --dtcs

The bus comes from ``canary.yaml``'s ``hardware.can`` block (slcan for
the Canable in WSL dev, socketcan for the Pi target). No write commands
are ever sent.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from src.collection.can_collector import bus_factory
from src.collection.can_obd_client import (
    PID_DECODERS,
    CanObdClient,
    parse_dtc_payload,
    read_pid,
)
from src.config import CanaryConfig
from src.log import configure_logging


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="can_obd_probe",
        description=(
            "Probe what Mode-01 PIDs the ECU supports + read DTCs over the "
            "Canable. No ELM327 needed — uses the same CAN bus the broadcast "
            "sniffer uses."
        ),
    )
    parser.add_argument(
        "--config", type=Path, default=Path("canary.yaml"), help="canary.yaml path."
    )
    parser.add_argument(
        "--read-pids",
        action="store_true",
        help=(
            "After walking the supported-PID bitmask, query each supported PID "
            "once and print the decoded value. Slower (one CAN round-trip per "
            "PID) but produces a snapshot. Skipped by default — most users want "
            "just the capability list."
        ),
    )
    parser.add_argument(
        "--dtcs",
        action="store_true",
        help="Also read stored DTCs (Mode 03).",
    )
    parser.add_argument(
        "--no-supported-walk",
        dest="supported_walk",
        action="store_false",
        help="Skip the PID bitmask walk (only useful with --dtcs).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        help="Per-request timeout in seconds (default 1.0).",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)
    bus = bus_factory(config.hardware.can)
    try:
        client = CanObdClient(bus, timeout_seconds=args.timeout)

        if args.supported_walk:
            print("walking Mode-01 supported-PID bitmask…", file=sys.stderr)
            supported = client.query_supported_mode_01_pids()
            print(f"\nSUPPORTED MODE-01 PIDS ({len(supported)}):")
            for pid in supported:
                name = PID_DECODERS.get(pid, (None,))[0] or "—"
                print(f"  0x{pid:02X}  {name}")
        else:
            supported = ()

        if args.read_pids and supported:
            print("\nSAMPLE READINGS:")
            for pid in supported:
                decoded = read_pid(client, pid)
                if decoded is None:
                    print(f"  0x{pid:02X}  (no decoder or no response)")
                else:
                    print(
                        f"  0x{pid:02X}  {decoded.name:<28}  {decoded.value:>10.3f} {decoded.unit}"
                    )

        if args.dtcs:
            print("\nMODE 03 STORED DTCS:")
            resp = client.query_mode_03()
            if resp is None:
                print("  (no response from ECU)")
            else:
                dtcs = parse_dtc_payload(resp.payload)
                if dtcs:
                    for code in dtcs:
                        print(f"  {code}")
                else:
                    print("  (none)")
    finally:
        bus.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
