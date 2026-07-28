#!/usr/bin/env python3
"""Unattended Mode 22 DID sweep for the ``did_sweep`` background job.

One sweep pass over ``[--start, --end]``, tagged ``--label``, over the
Canable-native transport only (no ELM327 — this runs inside
``JobController``'s subprocess against the daemon's own configured CAN
interface, not an operator's separate serial adapter). For the interactive
multi-pass "sweep, change engine state, sweep again" workflow at the
vehicle, use ``tools/scan_constant_dids.py`` instead; this script does
exactly one sweep and writes one JSON snapshot, so that two job runs (one
per engine/load state, each with its own ``--label``) can be compared after
the fact — see the ``diff_did_snapshots`` MCP tool.

READ-ONLY: only UDS service 0x22 (ReadDataByIdentifier) and, when
``--session`` resolves to something, 0x10 (DiagnosticSessionControl — no
memory write, no actuation) are ever sent.

Usage (normally invoked by ``start_job(kind="did_sweep", params={...})``,
not run by hand):
  tools/did_sweep.py --start 0x1100 --end 0x12FF --label control --out snap.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from src.analysis.did_scan import CanableMode22Transport, sweep_dids
from src.collection.can_collector import bus_factory
from src.collection.can_obd_client import CanObdClient
from src.collection.obd_scheduler import did_access
from src.config import CanaryConfig


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="did_sweep",
        description="One Mode 22 DID sweep pass; writes a labeled JSON snapshot.",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("canary.yaml"), help="canary.yaml path."
    )
    parser.add_argument(
        "--start", type=lambda s: int(s, 0), required=True, help="First DID (hex ok, e.g. 0x1100)."
    )
    parser.add_argument(
        "--end", type=lambda s: int(s, 0), required=True, help="Last DID inclusive."
    )
    parser.add_argument(
        "--label", required=True, help="Tag for this snapshot (e.g. 'control', 'wot')."
    )
    parser.add_argument(
        "--header", default="7E0", help="ECU request CAN ID, hex (default 7E0 = engine)."
    )
    parser.add_argument(
        "--session",
        default="auto",
        help=(
            "UDS diagnostic-session sub-function (hex) to enter + keep alive before "
            "0x22 reads. 'auto' (default) resolves from data/dids.yaml's access "
            "block; 'none' forces bare polling (ECUs that answer 0x22 in the "
            "default session)."
        ),
    )
    parser.add_argument(
        "--keepalive", type=float, default=2.0, help="Re-enter --session every N s (default 2.0)."
    )
    parser.add_argument(
        "--delay-seconds", type=float, default=0.0, help="Pause between DID reads, seconds."
    )
    parser.add_argument(
        "--read-timeout", type=float, default=1.0, help="Per-request read timeout (default 1.0)."
    )
    parser.add_argument("--out", type=Path, required=True, help="Write the snapshot JSON here.")
    return parser.parse_args(argv)


def _resolve_session(raw: str) -> int | None:
    normalized = raw.strip().lower()
    if normalized == "none":
        return None
    if normalized == "auto":
        access = did_access()
        return access.session if access is not None else None
    return int(raw, 16)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not 0 <= args.start <= args.end <= 0xFFFF:
        print("error: DID range must satisfy 0 <= start <= end <= 0xFFFF", file=sys.stderr)
        return 1
    try:
        request_id = int(args.header, 16)
    except ValueError:
        print(f"error: --header must be hex (e.g. 7E0), got {args.header!r}", file=sys.stderr)
        return 1
    if not 0 <= request_id <= 0x7FF:
        print(f"error: --header out of 11-bit CAN range: 0x{request_id:X}", file=sys.stderr)
        return 1
    try:
        session = _resolve_session(args.session)
    except ValueError:
        print(
            f"error: --session must be hex, 'auto', or 'none', got {args.session!r}",
            file=sys.stderr,
        )
        return 1

    config = CanaryConfig.from_yaml(args.config)
    dids = range(args.start, args.end + 1)
    bus = bus_factory(config.hardware.can)
    hits = 0
    t0 = time.time()
    try:
        response_id = request_id + 8
        client = CanObdClient(
            bus,
            request_id=request_id,
            response_id_min=response_id,
            response_id_max=response_id,
            timeout_seconds=args.read_timeout,
        )
        transport = CanableMode22Transport(
            client, request_id=request_id, session=session, keepalive_seconds=args.keepalive
        )
        session_desc = f"0x{session:X}" if session is not None else "none"
        print(
            f"did_sweep: {config.hardware.can.channel} req=0x{request_id:X} "
            f"session={session_desc} "
            f"range=0x{args.start:04X}-0x{args.end:04X} label={args.label!r}",
            file=sys.stderr,
        )

        def _progress(did: int, data: bytes | None) -> None:
            nonlocal hits
            if data is not None:
                hits += 1
                print(f"  0x{did:04X}: {data.hex()}")

        snapshot = sweep_dids(
            transport,
            dids,
            delay_seconds=args.delay_seconds,
            label=args.label,
            on_progress=_progress,
        )
    finally:
        bus.shutdown()

    elapsed = time.time() - t0
    print(f"did_sweep: {hits}/{len(dids)} DIDs answered in {elapsed:.1f}s", file=sys.stderr)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
