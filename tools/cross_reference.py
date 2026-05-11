#!/usr/bin/env python3
"""CLI for Mode 22 DID to Mode 01 PID cross-reference analysis.

Usage:
  tools/cross_reference.py data/sessions/<timestamp>/

The reusable loading, timestamp pairing, and regression logic lives in
`src.analysis.cross_reference`; this file handles argparse and presentation.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from src.analysis.cross_reference import (
    CrossRefBinding,
    cross_reference,
    linear_regression,
    load_did_timeseries,
    load_pid_units,
    load_pids_jsonl,
    pair_by_timestamp,
)

__all__ = [
    "CrossRefBinding",
    "cross_reference",
    "linear_regression",
    "load_did_timeseries",
    "load_pid_units",
    "load_pids_jsonl",
    "pair_by_timestamp",
    "main",
    "parse_args",
    "report",
]


def report(
    session_dir: Path,
    *,
    min_pairs: int = 20,
    min_r_squared: float = 0.5,
    tolerance_seconds: float = 0.5,
) -> int:
    try:
        bindings = cross_reference(
            session_dir,
            min_pairs=min_pairs,
            min_r_squared=min_r_squared,
            tolerance_seconds=tolerance_seconds,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print("=" * 78)
    print("CROSS-REFERENCE BINDINGS (Mode 22 DID <-> Mode 01 PID)")
    print("=" * 78)

    if not bindings:
        print("\n  No bindings met the threshold.")
        print("  Check that pids.jsonl and frames.log have overlapping timestamps,")
        print(
            "  and that the engine state varied enough to produce signal "
            "(idle alone gives constant values that can't be regressed)."
        )
        return 0

    by_did: defaultdict[int, list[CrossRefBinding]] = defaultdict(list)
    for binding in bindings:
        by_did[binding.did].append(binding)

    print(f"\n  {len(by_did)} DIDs bound across {len(bindings)} (DID, PID) pairs.\n")
    for did in sorted(by_did):
        rows = by_did[did]
        best_r2 = rows[0].r_squared
        for binding in rows:
            if binding.r_squared < best_r2 - 0.05:
                break
            scale_str = f"value = raw x {binding.slope:g}" + (
                f" {binding.offset:+g}" if binding.offset != 0 else ""
            )
            unit = f" {binding.pid_unit}" if binding.pid_unit else ""
            print(
                f"  0x{binding.did:04X} <-> {binding.pid:<24}  "
                f"r2={binding.r_squared:.3f}  n={binding.n_pairs:<4}  "
                f"{scale_str}{unit}"
            )
            print(
                f"          raw {binding.raw_range[0]:>6}..{binding.raw_range[1]:<6}  "
                f"-> decoded {binding.decoded_range[0]:.2f}.."
                f"{binding.decoded_range[1]:.2f}"
            )

    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cross_reference",
        description=(
            "Bind captured Mode 22 DIDs to Mode 01 PIDs by linear regression "
            "of paired timestamps. Requires a session recorded with "
            "`record.py --obd-port`."
        ),
    )
    parser.add_argument("session_dir", type=Path, help="Path to data/sessions/<timestamp>/")
    parser.add_argument(
        "--min-pairs",
        type=int,
        default=20,
        help="Minimum (DID-read, PID-reading) pairs required to attempt regression.",
    )
    parser.add_argument(
        "--min-r2",
        type=float,
        default=0.5,
        help="Minimum r2 to consider a binding worth reporting.",
    )
    parser.add_argument(
        "--tolerance-seconds",
        type=float,
        default=0.5,
        help="Max time delta when pairing a DID read to a PID reading.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return report(
        args.session_dir,
        min_pairs=args.min_pairs,
        min_r_squared=args.min_r2,
        tolerance_seconds=args.tolerance_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
