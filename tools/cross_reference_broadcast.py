#!/usr/bin/env python3
"""Regress broadcast-CAN bytes against Mode 01 PIDs to pin signal scalings.

The byte↔PID counterpart to ``tools/cross_reference.py`` (Mode 22 DID ↔ Mode 01).
For a session recorded with ``record.py --obd-port``, for each probe (a CAN id +
byte range) it fits ``raw → PID value``; a high r² tells you which PID the byte
tracks and the slope/offset give the scaling.

By default the probes are the *unpinned* signals in ``data/bindings.yaml``
(``scale: null``) — i.e. "for each candidate, which PID is it and at what
scale?" Or pass explicit ``--probe ID:BYTE[:LEN][:le]`` specs.

Usage:
  tools/cross_reference_broadcast.py data/sessions/<timestamp>/
  tools/cross_reference_broadcast.py data/sessions/<ts>/ --probe 0x1F9:2:2 --probe 0x551:0
  tools/cross_reference_broadcast.py data/sessions/<ts>/ --all-bindings --min-r2 0.3
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from src.analysis.broadcast_correlate import (
    ByteProbe,
    correlate_broadcast,
    probes_from_bindings,
)

DEFAULT_BINDINGS_PATH = Path("data/bindings.yaml")


def parse_probe(spec: str) -> ByteProbe:
    """Parse ``ID:BYTE[:LEN][:le]`` (ID hex with or without 0x) into a ByteProbe."""
    parts = spec.split(":")
    if not 2 <= len(parts) <= 4:
        raise ValueError(f"bad --probe {spec!r}; expected ID:BYTE[:LEN][:le]")
    can_id = int(parts[0], 16)
    byte_start = int(parts[1])
    byte_length = 1
    order = "big"
    for extra in parts[2:]:
        if extra.lower() in ("le", "little"):
            order = "little"
        elif extra.lower() in ("be", "big"):
            order = "big"
        else:
            byte_length = int(extra)
    return ByteProbe(
        can_id=can_id,
        byte_start=byte_start,
        byte_length=byte_length,
        byte_order=order,  # type: ignore[arg-type]
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cross_reference_broadcast",
        description=(
            "Regress broadcast-CAN bytes against Mode 01 PIDs (byte↔PID). "
            "Probes default to the unpinned signals in bindings.yaml."
        ),
    )
    parser.add_argument("session_dir", type=Path, help="Path to data/sessions/<timestamp>/")
    parser.add_argument(
        "--probe",
        action="append",
        default=None,
        metavar="ID:BYTE[:LEN][:le]",
        help="Explicit probe (repeatable). Overrides the bindings.yaml defaults.",
    )
    parser.add_argument(
        "--bindings",
        type=Path,
        default=DEFAULT_BINDINGS_PATH,
        help="Path to bindings.yaml (used when no --probe is given).",
    )
    parser.add_argument(
        "--all-bindings",
        action="store_true",
        help="Probe every bindings.yaml signal, not just the unpinned ones.",
    )
    parser.add_argument("--min-pairs", type=int, default=30, help="Min paired samples to fit.")
    parser.add_argument("--min-r2", type=float, default=0.5, help="Min r² to report a PID fit.")
    parser.add_argument(
        "--tolerance-seconds",
        type=float,
        default=0.6,
        help="Max time delta when pairing a raw frame to a PID reading.",
    )
    parser.add_argument("--top", type=int, default=3, help="PID fits to show per probe.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.probe:
            probes = [parse_probe(spec) for spec in args.probe]
        else:
            probes = probes_from_bindings(args.bindings, unpinned_only=not args.all_bindings)
        if not probes:
            print(
                "error: no probes — all bindings already pinned? pass --probe or --all-bindings",
                file=sys.stderr,
            )
            return 1
        results = correlate_broadcast(
            args.session_dir,
            probes,
            min_pairs=args.min_pairs,
            min_r2=args.min_r2,
            tolerance_seconds=args.tolerance_seconds,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("=" * 78)
    print(f"BROADCAST ↔ PID CROSS-REFERENCE  {args.session_dir}")
    print("=" * 78)
    matched = 0
    for result in results:
        head = result.probe.display
        if result.raw_range is None:
            print(f"\n  {head}\n    (no frames matched this id/length in the session)")
            continue
        lo, hi = result.raw_range
        print(f"\n  {head}\n    raw range {lo}..{hi}  ({result.n_frames} frames)")
        if not result.fits:
            print(f"    no PID fit ≥ r²={args.min_r2:g}")
            continue
        matched += 1
        for fit in result.fits[: args.top]:
            unit = f" {fit.pid_unit}" if fit.pid_unit else ""
            inv = f"  (raw ≈ {fit.pid}×{1.0 / fit.slope:g})" if fit.slope else ""
            lo_d, hi_d = fit.decoded_range
            print(
                f"    r²={fit.r_squared:.3f}  {fit.pid} = raw×{fit.slope:.5g} + {fit.offset:.5g}"
                f"{unit}  [{lo_d:.3g}..{hi_d:.3g}, n={fit.n_pairs}]{inv}"
            )
    print(f"\n  {matched}/{len(results)} probes matched a PID at r² ≥ {args.min_r2:g}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
