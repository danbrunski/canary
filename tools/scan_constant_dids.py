#!/usr/bin/env python3
"""Sweep Mode 22 DID space over an ELM327; split runtime-varying DIDs from
fixed calibration constants (Task 4.E.5).

Run at the vehicle with the engine in a known state. By default it does two
passes and pauses between them so you can change the engine state (e.g. pass 1
at idle, pass 2 while revving / moving): DIDs whose value differs between passes
are "varying" (candidate live signals — injector PW, timing, etc.), DIDs that
answer in every pass but never change are "constant" (calibration / config).

READ-ONLY — only UDS service 0x22 (ReadDataByIdentifier) is sent. Make sure the
Canable, if attached, is in its sniffing role only.

Usage:
  tools/scan_constant_dids.py --port /dev/ttyUSB0
  tools/scan_constant_dids.py --port /dev/ttyUSB0 --full --out data/sessions/<ts>/dids.json
  tools/scan_constant_dids.py --port /dev/ttyUSB0 --start 0x1100 --end 0x12FF --passes 3
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from src.analysis.did_scan import (
    DidSnapshot,
    Elm327Mode22Transport,
    classify_dids,
    sweep_dids,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scan_constant_dids",
        description=(
            "Sweep Mode 22 DIDs over an ELM327 and classify varying vs. constant "
            "across passes taken at different engine states. Read-only (service 0x22)."
        ),
    )
    parser.add_argument("--port", required=True, help="ELM327 serial device, e.g. /dev/ttyUSB0.")
    parser.add_argument("--baud", type=int, default=38400, help="ELM327 baud rate (default 38400).")
    parser.add_argument(
        "--header", default="7E0", help="ECU request CAN ID (default 7E0 = engine)."
    )
    parser.add_argument(
        "--start",
        type=lambda s: int(s, 0),
        default=0x0000,
        help="First DID (hex ok, default 0x0000).",
    )
    parser.add_argument(
        "--end",
        type=lambda s: int(s, 0),
        default=0x2000,
        help="Last DID inclusive (default 0x2000).",
    )
    parser.add_argument("--full", action="store_true", help="Sweep the whole 0x0000–0xFFFF range.")
    parser.add_argument("--passes", type=int, default=2, help="Number of sweeps (default 2).")
    parser.add_argument(
        "--per-did-delay", type=float, default=0.0, help="Pause between DID reads, seconds."
    )
    parser.add_argument(
        "--pause-prompt",
        action="store_true",
        help="Wait for Enter between passes (so you can change engine state).",
    )
    parser.add_argument("--read-timeout", type=float, default=1.0, help="Per-command read timeout.")
    parser.add_argument(
        "--out", type=Path, default=None, help="Write snapshots + classification JSON."
    )
    return parser.parse_args(argv)


def _open_serial(port: str, baud: int):  # type: ignore[no-untyped-def]
    try:
        import serial  # pyserial — pulled in by python-obd / python-can
    except ImportError as exc:  # pragma: no cover - serial is a hard dep in practice
        raise RuntimeError("pyserial is required for the DID scanner") from exc
    return serial.Serial(port, baud, timeout=0.2)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.passes < 1:
        print("error: --passes must be ≥ 1", file=sys.stderr)
        return 1
    start, end = (0x0000, 0xFFFF) if args.full else (args.start, args.end)
    if not 0 <= start <= end <= 0xFFFF:
        print("error: DID range must satisfy 0 ≤ start ≤ end ≤ 0xFFFF", file=sys.stderr)
        return 1
    dids = range(start, end + 1)

    try:
        ser = _open_serial(args.port, args.baud)
    except Exception as exc:  # noqa: BLE001 - surface any serial open failure cleanly
        print(f"error: could not open {args.port}: {exc}", file=sys.stderr)
        return 1
    try:
        print(f"Initializing ELM327 on {args.port} (header {args.header})…", file=sys.stderr)
        transport = Elm327Mode22Transport(
            ser, header=args.header, read_timeout_seconds=args.read_timeout
        )
        snapshots: list[DidSnapshot] = []
        for i in range(args.passes):
            if i > 0 and args.pause_prompt:
                input(
                    f"\n--- ready for pass {i + 1}/{args.passes}? change engine state, then Enter "
                )
            print(f"Pass {i + 1}/{args.passes}: sweeping {len(dids)} DIDs…", file=sys.stderr)
            t0 = time.time()
            hits = [0]

            def _progress(_did: int, data: bytes | None, _hits: list[int] = hits) -> None:
                if data is not None:
                    _hits[0] += 1

            snap = sweep_dids(
                transport,
                dids,
                delay_seconds=args.per_did_delay,
                label=f"pass{i + 1}",
                on_progress=_progress,
            )
            snapshots.append(snap)
            print(
                f"  pass {i + 1}: {hits[0]} DIDs answered ({time.time() - t0:.0f}s)",
                file=sys.stderr,
            )
    finally:
        try:
            ser.close()
        except Exception:  # noqa: BLE001
            pass

    _report(snapshots)
    if args.out is not None:
        payload = {
            "header": args.header,
            "range": [start, end],
            "snapshots": [s.to_dict() for s in snapshots],
        }
        if len(snapshots) >= 2:
            cls = classify_dids(snapshots)
            payload["classification"] = {
                "varying": [f"0x{d:04X}" for d in sorted(cls.varying)],
                "constant": [f"0x{d:04X}" for d in sorted(cls.constant)],
                "intermittent": [f"0x{d:04X}" for d in sorted(cls.intermittent)],
            }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nWrote {args.out}", file=sys.stderr)
    return 0


def _report(snapshots: list[DidSnapshot]) -> None:
    print("=" * 78)
    print(f"MODE 22 DID SCAN  ({len(snapshots)} pass{'es' if len(snapshots) != 1 else ''})")
    print("=" * 78)
    if len(snapshots) < 2:
        snap = snapshots[0]
        print(f"\n  {len(snap.values)} DIDs answered:")
        for did, data in sorted(snap.values.items()):
            print(f"    0x{did:04X}  {data.hex(' ')}")
        print("\n  (only one pass — run with --passes 2 at different engine states to classify)")
        return
    cls = classify_dids(snapshots)
    print(f"\n  {len(cls.varying)} VARYING (changed across passes — candidate live signals):")
    for did in sorted(cls.varying):
        vals = " → ".join(s.values[did].hex() for s in snapshots)
        print(f"    0x{did:04X}  {vals}")
    print(f"\n  {len(cls.constant)} CONSTANT (answered every pass, never changed):")
    for did in sorted(cls.constant):
        print(f"    0x{did:04X}  {snapshots[0].values[did].hex(' ')}")
    if cls.intermittent:
        print(f"\n  {len(cls.intermittent)} INTERMITTENT (answered some passes, not all):")
        print("    " + ", ".join(f"0x{d:04X}" for d in sorted(cls.intermittent)))


if __name__ == "__main__":
    sys.exit(main())
