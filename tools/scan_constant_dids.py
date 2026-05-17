#!/usr/bin/env python3
"""Sweep Mode 22 DID space; split runtime-varying DIDs from fixed
calibration constants (Task 4.E.5).

Two interchangeable transports for UDS service 0x22 — pick whichever
adapter is on the OBD-II port:

* ``--canable`` — over the CAN bus from ``--config``, via
  ``CanObdClient.query_mode_22``. Uses the same CAN interface as the
  broadcast sniffer; no separate adapter needed.
* ``--port /dev/ttyUSBx`` — over an ELM327-class serial adapter.

Run at the vehicle with the engine in a known state. By default it does two
passes and pauses between them so you can change the engine state (e.g. pass 1
at idle, pass 2 while revving / moving): DIDs whose value differs between passes
are "varying" (candidate live signals — injector PW, timing, etc.), DIDs that
answer in every pass but never change are "constant" (calibration / config).

READ-ONLY — only UDS service 0x22 (ReadDataByIdentifier) is sent. Whatever
adapter is used here is request-only; don't run this alongside a broadcast
recording on the same single-consumer device (e.g. one slcan node).

Usage:
  tools/scan_constant_dids.py --canable
  tools/scan_constant_dids.py --canable --header 7E0 --full --out data/sessions/<ts>/dids.json
  tools/scan_constant_dids.py --canable --start 0x1100 --end 0x12FF --passes 3
  tools/scan_constant_dids.py --port /dev/ttyUSB0
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from src.analysis.did_scan import (
    CanableMode22Transport,
    DidSnapshot,
    Elm327Mode22Transport,
    Mode22Transport,
    classify_dids,
    sweep_dids,
)
from src.collection.can_collector import bus_factory
from src.collection.can_obd_client import CanObdClient
from src.config import CanaryConfig


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scan_constant_dids",
        description=(
            "Sweep Mode 22 DIDs and classify varying vs. constant across passes "
            "taken at different engine states. Read-only (service 0x22)."
        ),
    )
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument(
        "--canable",
        action="store_true",
        help="UDS 0x22 over the CAN bus from --config (no separate adapter).",
    )
    transport.add_argument(
        "--port",
        help="UDS 0x22 over an ELM327-class serial adapter, e.g. /dev/ttyUSB0.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("canary.yaml"),
        help="canary.yaml path (--canable bus config). Default: ./canary.yaml.",
    )
    parser.add_argument("--baud", type=int, default=38400, help="ELM327 baud rate (default 38400).")
    parser.add_argument(
        "--header",
        default="7E0",
        help="ECU request CAN ID, hex (default 7E0 = engine; trans 7E1, ABS 7E2, …).",
    )
    parser.add_argument(
        "--session",
        default=None,
        help=(
            "UDS diagnostic-session sub-function (hex) to enter + keep alive "
            "before 0x22 reads. The 2008 Nissan ECM gates Mode 22 behind "
            "'C0' — pass --session C0. Omit for ECUs that answer 0x22 in the "
            "default session. (--canable only.) NOTE: while a non-default "
            "session is held this ECM stops answering standard OBD Mode 01."
        ),
    )
    parser.add_argument(
        "--keepalive",
        type=float,
        default=2.0,
        help="Re-enter --session every N s (session lapses; default 2.0).",
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


def _build_transport(
    args: argparse.Namespace,
) -> tuple[Mode22Transport, Callable[[], None], str]:
    """Return ``(transport, close_fn, banner)`` for the selected link.

    Raises on a bad ``--header`` or a link-open failure; the caller turns
    that into a clean exit-1.
    """
    try:
        request_id = int(args.header, 16)
    except ValueError as exc:
        raise RuntimeError(f"--header must be hex (e.g. 7E0), got {args.header!r}") from exc
    if not 0 <= request_id <= 0x7FF:
        raise RuntimeError(f"--header out of 11-bit CAN range: 0x{request_id:X}")

    if args.canable:
        config = CanaryConfig.from_yaml(args.config)
        bus = bus_factory(config.hardware.can)
        response_id = request_id + 8  # 0x7E0→0x7E8, 0x7E1→0x7E9, …
        client = CanObdClient(
            bus,
            request_id=request_id,
            response_id_min=response_id,
            response_id_max=response_id,
            timeout_seconds=args.read_timeout,
        )
        session = int(args.session, 16) if args.session is not None else None
        transport = CanableMode22Transport(
            client,
            request_id=request_id,
            session=session,
            keepalive_seconds=args.keepalive,
        )
        banner = (
            f"Canable {config.hardware.can.channel} (req 0x{request_id:X} → resp 0x{response_id:X}"
        )
        if session is not None:
            banner += f", session 0x{session:X} keepalive {args.keepalive}s)"
        else:
            banner += ")"
        return transport, bus.shutdown, banner

    ser = _open_serial(args.port, args.baud)
    elm = Elm327Mode22Transport(ser, header=args.header, read_timeout_seconds=args.read_timeout)
    return elm, ser.close, f"ELM327 on {args.port} (header {args.header})"


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
        transport, close_fn, banner = _build_transport(args)
    except Exception as exc:  # noqa: BLE001 - surface any link-open failure cleanly
        print(f"error: could not open transport: {exc}", file=sys.stderr)
        return 1
    try:
        print(f"Mode 22 DID scan via {banner}…", file=sys.stderr)
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
            close_fn()
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
