#!/usr/bin/env python3
"""Probe what Mode-01 PIDs an ECU actually claims to support.

python-obd issues the PIDS_A/B/C/D bitmask requests at connection time and
populates ``conn.supported_commands``. This tool just surfaces that in a
readable form so the operator can compare it against the
``record.py --obd-pids`` default and adjust.

Usage:
  tools/obd_coverage.py --port /dev/ttyUSB0
  tools/obd_coverage.py --port /dev/ttyUSB0 --compare record-default

This legacy ELM tool only queries PID support; its path-local
``enforce_read_only`` wrapper means Mode 04 is unavailable here. Exits with rc
1 if the adapter doesn't connect. The active Canable request path is not
restricted this way.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import obd

from src.collection.obd_collector import enforce_read_only

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="obd_coverage",
        description=(
            "Probe what Mode-01 PIDs the connected ECU claims to support. "
            "Used to tune `record.py --obd-pids`."
        ),
    )
    parser.add_argument("--port", required=True, help="ELM327 serial device, e.g. /dev/ttyUSB0.")
    parser.add_argument("--baud", type=int, default=38400)
    parser.add_argument(
        "--protocol",
        default=None,
        help="Pin OBD-II protocol number (e.g. '6' for ISO 15765-4 CAN 11/500).",
    )
    parser.add_argument(
        "--compare",
        choices=("record-default",),
        default=None,
        help=(
            "Compare the ECU's supported set against the named reference. "
            "`record-default` reads scripts/record.py's --obd-pids default."
        ),
    )
    return parser.parse_args(argv)


def _record_default_pids() -> set[str]:
    """Parse the default --obd-pids out of scripts/record.py.

    Reading the script directly (rather than importing) keeps this tool
    free of the heavy collection-module imports and works even if the
    script's argparse defaults shift.
    """
    text = (REPO_ROOT / "scripts" / "record.py").read_text()
    match = re.search(r'"--obd-pids",\s*default=\((.*?)\),\s*help=', text, re.DOTALL)
    if not match:
        return set()
    body = match.group(1)
    # Strip Python string concatenation + comments
    pids: list[str] = []
    for line in body.splitlines():
        line = re.sub(r"#.*$", "", line).strip()
        line = line.strip(",")
        if not line:
            continue
        # The default is a sequence of "PID,PID,..." string literals concatenated.
        # Strip the quotes, split on commas.
        for token in re.findall(r'"([^"]+)"', line):
            for pid in token.split(","):
                pid = pid.strip()
                if pid:
                    pids.append(pid)
    return set(pids)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    kwargs: dict = {"portstr": args.port, "baudrate": args.baud, "fast": False}
    if args.protocol is not None:
        kwargs["protocol"] = args.protocol

    print(f"connecting to ELM327 at {args.port}…", file=sys.stderr)
    conn = obd.OBD(**kwargs)
    if not conn.is_connected():
        print(f"error: did not connect (status={conn.status()})", file=sys.stderr)
        return 1
    enforce_read_only(conn)

    try:
        supported_names = sorted(c.name for c in conn.supported_commands if c.mode == 1)
        print()
        print(f"PROTOCOL: {conn.protocol_id()} ({conn.protocol_name()})")
        print(f"STATUS:   {conn.status()}")
        print(f"SUPPORTED MODE-01 PIDS ({len(supported_names)}):")
        for name in supported_names:
            print(f"  {name}")

        if args.compare == "record-default":
            ours = _record_default_pids()
            theirs = set(supported_names)
            polling_unsupported = sorted(ours - theirs)
            unpolled_supported = sorted(theirs - ours)

            print()
            print("=" * 60)
            print("COMPARISON vs scripts/record.py --obd-pids default")
            print("=" * 60)
            print(
                f"\nPolling ({len(ours)}) but unsupported by this ECU ({len(polling_unsupported)}):"
            )
            for p in polling_unsupported:
                print(f"  - {p}  (will warn+skip at factory time)")
            print(f"\nSupported but NOT in our default ({len(unpolled_supported)}):")
            for p in unpolled_supported:
                print(f"  + {p}")
            print(
                "\n(Consider adding the `+` lines to `record.py --obd-pids` if "
                "they have ML / diagnostic value. Skip wideband O2 / hybrid PIDs.)"
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
