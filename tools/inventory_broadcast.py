#!/usr/bin/env python3
"""Inventory non-diagnostic (broadcast) CAN IDs in a captured session.

Skips the diagnostic ID range (0x7DF, 0x7E0..0x7EF) which only carries
scan-tool request/response traffic. Looks at the natural inter-module
broadcast — what the production daemon sees with no scan tool attached.

For each CAN ID, reports:
  * Frame count and median period (period names the owner: ~10ms = ECM
    hot-path, ~20ms = drivetrain, ~100ms = body, ~1s = housekeeping).
  * DLC (frame data length).
  * Per-byte variability — unique-value count per byte position. Signals
    live in varying bytes; constant bytes are padding/IDs; rolling-counter
    bytes cycle through a small set (often 4 or 16).
  * Sample frames to eyeball patterns by hand.

Usage:
  tools/inventory_broadcast.py data/sessions/<timestamp>/
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from src.analysis.diagnostics import DEFAULT_DIAG_IDS
from src.analysis.iso_tp import iter_frames


@dataclass(frozen=True)
class IdInventory:
    """Per-CAN-ID summary of broadcast traffic in a session."""

    can_id: int
    n_frames: int
    period_ms: float  # median inter-arrival; 0 when only one frame
    dlc: int  # canonical frame data length
    byte_variability: tuple[int, ...]  # unique-value count per byte position
    sample_frames: tuple[bytes, ...]  # representative captures
    first_seen: float
    last_seen: float


def inventory(
    frames_path: Path,
    *,
    exclude_ids: frozenset[int] | set[int] = DEFAULT_DIAG_IDS,
    n_samples: int = 5,
) -> list[IdInventory]:
    """Walk frames.log; build per-ID inventory for non-excluded IDs.

    Sorted by frame count descending — busiest IDs first.
    """
    timestamps: dict[int, list[float]] = defaultdict(list)
    samples: dict[int, list[bytes]] = defaultdict(list)
    by_byte: dict[int, list[set[int]]] = defaultdict(list)
    dlc_seen: dict[int, int] = {}

    for frame in iter_frames(frames_path):
        if frame.can_id in exclude_ids:
            continue
        can_id = frame.can_id
        data = frame.data
        timestamps[can_id].append(frame.timestamp)
        # Lock the canonical DLC to the first sighting; track per-byte
        # uniques only across that many positions. Real bus traffic for
        # a given ID is fixed-length, so this is robust.
        if can_id not in dlc_seen:
            dlc_seen[can_id] = len(data)
            by_byte[can_id] = [set() for _ in range(len(data))]
        for i in range(min(len(data), dlc_seen[can_id])):
            by_byte[can_id][i].add(data[i])
        if len(samples[can_id]) < n_samples:
            samples[can_id].append(data)

    results: list[IdInventory] = []
    for can_id, ts_list in timestamps.items():
        deltas = [ts_list[i + 1] - ts_list[i] for i in range(len(ts_list) - 1)]
        period_ms = (statistics.median(deltas) * 1000.0) if deltas else 0.0
        results.append(
            IdInventory(
                can_id=can_id,
                n_frames=len(ts_list),
                period_ms=period_ms,
                dlc=dlc_seen[can_id],
                byte_variability=tuple(len(s) for s in by_byte[can_id]),
                sample_frames=tuple(samples[can_id]),
                first_seen=ts_list[0],
                last_seen=ts_list[-1],
            )
        )

    results.sort(key=lambda r: -r.n_frames)
    return results


def _format_period(ms: float) -> str:
    if ms == 0:
        return "—"
    if ms < 1:
        return f"{ms * 1000:.0f}µs"
    if ms < 100:
        return f"{ms:.1f}ms"
    return f"{ms / 1000:.2f}s"


def _format_bytes(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def report(session_dir: Path, *, n_samples: int = 5, show_samples_top: int = 15) -> int:
    frames_path = session_dir / "frames.log"
    if not frames_path.exists():
        print(f"error: no frames.log in {session_dir}", file=sys.stderr)
        return 1

    inv = inventory(frames_path, n_samples=n_samples)

    print("=" * 78)
    print(f"BROADCAST INVENTORY  {session_dir}")
    print("=" * 78)

    if not inv:
        print("\n  No broadcast frames found (everything was diagnostic).")
        return 0

    total_frames = sum(r.n_frames for r in inv)
    duration = max(r.last_seen for r in inv) - min(r.first_seen for r in inv)
    print(
        f"\n  {len(inv)} distinct CAN IDs (excl. diag range), "
        f"{total_frames} frames over {duration:.1f}s\n"
    )

    # Per-ID table. Widest byte-variability column is at most DLC=8 entries
    # of 3 chars each plus brackets/commas — comfortably fits in 80 cols.
    print(f"  {'ID':<8}{'count':>8}  {'period':>8}  {'DLC':>3}  byte uniques")
    print(f"  {'-' * 8}{'-' * 8}  {'-' * 8}  {'-' * 3}  {'-' * 40}")
    for r in inv:
        bv = "[" + ",".join(f"{v:>3}" for v in r.byte_variability) + "]"
        print(
            f"  0x{r.can_id:03X}   {r.n_frames:>7}  "
            f"{_format_period(r.period_ms):>8}  {r.dlc:>3}  {bv}"
        )

    # Sample frames for IDs with variable bytes — those are the
    # signal-bearing ones worth eyeballing.
    interesting = [r for r in inv if any(v > 1 for v in r.byte_variability)]
    if interesting:
        print(f"\n  Sample frames (top {show_samples_top} signal-bearing IDs):")
        for r in interesting[:show_samples_top]:
            varying = [i for i, v in enumerate(r.byte_variability) if v > 1]
            print(f"\n  0x{r.can_id:03X}  (varies on bytes {varying})")
            for sf in r.sample_frames:
                print(f"    {_format_bytes(sf)}")

    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="inventory_broadcast",
        description=(
            "Inventory non-diagnostic CAN IDs in a captured session. Surfaces "
            "broadcast frames (fixed-position DBC-style signals) that the "
            "daemon sees with no scan tool attached."
        ),
    )
    parser.add_argument("session_dir", type=Path)
    parser.add_argument(
        "--samples",
        type=int,
        default=5,
        help="How many sample frames to display per ID.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="How many signal-bearing IDs to show samples for.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return report(args.session_dir, n_samples=args.samples, show_samples_top=args.top)


if __name__ == "__main__":
    sys.exit(main())
