#!/usr/bin/env python3
"""Byte-vs-tag differential for broadcast CAN IDs.

Companion to `tools/inventory_broadcast.py`. The inventory tells you which
CAN IDs exist and which of their bytes change. This tool answers the next
question: which bytes change *between* labeled time windows?

For every (CAN ID, byte position) pair, compute mean/range/unique-count
inside each tag-defined window, then surface bytes whose per-window means
diverge the most. A byte whose mean shifts from 0x12 in `idle` to 0x67 in
`throttle_blip` is almost certainly a real signal; a byte that cycles
0..255 in both windows but with mean ~127 in each is a rolling counter,
not a signal.

Use this with a tagged session — e.g. the seated rev session — to
hand-map RPM/MAF/coolant/throttle bytes inside the broadcast frames
without any scan tool involved.

Usage:
  tools/broadcast_signals.py data/sessions/<timestamp>/
  tools/broadcast_signals.py data/sessions/<timestamp>/ --min-delta 20
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from src.analysis.diagnostics import DEFAULT_DIAG_IDS
from src.analysis.iso_tp import iter_frames
from src.session import TagWindow, build_windows, load_tags, session_paths


@dataclass(frozen=True)
class ByteStats:
    """Per-byte stats inside one (CAN ID, byte position, window)."""

    n: int
    mean: float
    min_val: int
    max_val: int
    n_unique: int


@dataclass(frozen=True)
class ByteDifferential:
    """One (CAN ID, byte position) summarized across tag windows.

    `delta` is the spread of per-window means — `max(means) - min(means)`.
    A high delta means the byte's average level shifts between windows;
    that's the signature of a real signal vs. a counter or padding.
    """

    can_id: int
    byte_index: int
    per_window: dict[str, ByteStats]
    delta: float


def _window_for(timestamp: float, windows: Sequence[TagWindow]) -> TagWindow | None:
    """Return the window covering `timestamp`, or None if it falls in a gap."""
    for w in windows:
        if w.start <= timestamp < w.end:
            return w
    return None


def collect_byte_stats(
    frames_path: Path,
    windows: Sequence[TagWindow],
    *,
    exclude_ids: frozenset[int] | set[int] = DEFAULT_DIAG_IDS,
) -> dict[tuple[int, int], dict[str, ByteStats]]:
    """Walk frames.log; aggregate per-byte stats keyed by (CAN ID, byte index).

    Inner dict is `window_label → ByteStats`. Frames outside any window are
    skipped. DLC is locked to the first sighting per CAN ID, like the
    inventory tool — real bus traffic for a given ID is fixed-length.
    """
    # Running accumulators: (can_id, byte_idx, label) → (n, sum, min, max, set_of_values)
    acc_n: dict[tuple[int, int, str], int] = defaultdict(int)
    acc_sum: dict[tuple[int, int, str], int] = defaultdict(int)
    acc_min: dict[tuple[int, int, str], int] = {}
    acc_max: dict[tuple[int, int, str], int] = {}
    acc_uniq: dict[tuple[int, int, str], set[int]] = defaultdict(set)
    dlc_seen: dict[int, int] = {}

    for frame in iter_frames(frames_path):
        if frame.can_id in exclude_ids:
            continue
        w = _window_for(frame.timestamp, windows)
        if w is None:
            continue
        if frame.can_id not in dlc_seen:
            dlc_seen[frame.can_id] = len(frame.data)
        dlc = dlc_seen[frame.can_id]
        label = w.label
        for i in range(min(len(frame.data), dlc)):
            v = frame.data[i]
            key = (frame.can_id, i, label)
            acc_n[key] += 1
            acc_sum[key] += v
            acc_min[key] = v if key not in acc_min else min(acc_min[key], v)
            acc_max[key] = v if key not in acc_max else max(acc_max[key], v)
            acc_uniq[key].add(v)

    # Reshape into (can_id, byte_idx) → {label → ByteStats}
    result: dict[tuple[int, int], dict[str, ByteStats]] = defaultdict(dict)
    for (can_id, idx, label), n in acc_n.items():
        result[(can_id, idx)][label] = ByteStats(
            n=n,
            mean=acc_sum[(can_id, idx, label)] / n,
            min_val=acc_min[(can_id, idx, label)],
            max_val=acc_max[(can_id, idx, label)],
            n_unique=len(acc_uniq[(can_id, idx, label)]),
        )
    return dict(result)


def rank_differentials(
    stats: dict[tuple[int, int], dict[str, ByteStats]],
    *,
    min_delta: float = 5.0,
    min_windows: int = 2,
) -> list[ByteDifferential]:
    """Score each (CAN ID, byte) by max-spread of per-window means.

    Filters: must appear in at least `min_windows` windows; delta must
    exceed `min_delta`. Sorted by delta descending.
    """
    diffs: list[ByteDifferential] = []
    for (can_id, idx), per_window in stats.items():
        if len(per_window) < min_windows:
            continue
        means = [s.mean for s in per_window.values()]
        delta = max(means) - min(means)
        if delta < min_delta:
            continue
        diffs.append(
            ByteDifferential(
                can_id=can_id,
                byte_index=idx,
                per_window=per_window,
                delta=delta,
            )
        )
    diffs.sort(key=lambda d: -d.delta)
    return diffs


def report(
    session_dir: Path,
    *,
    min_delta: float = 5.0,
    top_n: int = 25,
    show_full_per_id: int = 8,
) -> int:
    paths = session_paths(session_dir)
    if not paths.frames.exists():
        print(f"error: no frames.log in {session_dir}", file=sys.stderr)
        return 1

    tags = load_tags(paths.tags)
    if not tags:
        print(
            f"error: no tags in {paths.tags} — this tool needs tagged windows "
            "to differentiate against. Re-record with `record.py` and tag "
            "vehicle states (idle, revved, etc.) during the capture.",
            file=sys.stderr,
        )
        return 1

    # Bound the windows at the last frame's timestamp; build_windows needs
    # a session_end. Pre-tag prelude is included automatically.
    first_ts = last_ts = None
    for frame in iter_frames(paths.frames):
        if first_ts is None:
            first_ts = frame.timestamp
        last_ts = frame.timestamp
    if first_ts is None or last_ts is None:
        print("error: frames.log is empty", file=sys.stderr)
        return 1

    windows = build_windows(tags, last_ts + 0.001, session_start=first_ts)

    stats = collect_byte_stats(paths.frames, windows)
    diffs = rank_differentials(stats, min_delta=min_delta)

    print("=" * 78)
    print(f"BROADCAST SIGNAL DIFFERENTIAL  {session_dir}")
    print("=" * 78)

    print(f"\n  {len(windows)} tag windows:")
    for w in windows:
        print(f"    [{w.label[:30]:<30}]  t={w.start:.1f}..{w.end:.1f}  ({w.end - w.start:.1f}s)")

    if not diffs:
        print(
            f"\n  No bytes with cross-window mean delta ≥ {min_delta}. "
            "Either the bus is genuinely steady across your tagged states, "
            "or the windows are too short to average out counter noise. "
            "Try lowering --min-delta or recording longer per state."
        )
        return 0

    n_shown = min(top_n, len(diffs))
    print(f"\n  Top {n_shown} most-discriminating bytes (Δ = max-mean − min-mean):\n")
    print(f"  {'ID':<8}{'byte':>5}  {'delta':>8}  per-window means")
    print(f"  {'-' * 8}{'-' * 5}  {'-' * 8}  {'-' * 50}")
    for d in diffs[:top_n]:
        means = ", ".join(f"{label[:18]}={stats.mean:.1f}" for label, stats in d.per_window.items())
        print(f"  0x{d.can_id:03X}  {d.byte_index:>4}  {d.delta:>8.1f}  {means}")

    # Group by CAN ID and show full per-window stats for the few most-active IDs.
    by_id: dict[int, list[ByteDifferential]] = defaultdict(list)
    for d in diffs:
        by_id[d.can_id].append(d)
    top_ids = sorted(by_id.keys(), key=lambda i: -max(d.delta for d in by_id[i]))[:show_full_per_id]

    print(f"\n  Detail for top {len(top_ids)} signal-bearing IDs:")
    for can_id in top_ids:
        print(f"\n  0x{can_id:03X}")
        for d in by_id[can_id]:
            print(f"    byte {d.byte_index}  (Δ={d.delta:.1f})")
            for label, s in d.per_window.items():
                print(
                    f"      {label[:32]:<32}  "
                    f"mean={s.mean:6.1f}  range={s.min_val:3d}..{s.max_val:<3d}  "
                    f"unique={s.n_unique:<3d}  n={s.n}"
                )

    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="broadcast_signals",
        description=(
            "Find broadcast-CAN bytes whose mean shifts between tagged "
            "windows — those are real signals (RPM, MAF, etc.) vs. counters "
            "or padding. Companion to inventory_broadcast.py."
        ),
    )
    parser.add_argument("session_dir", type=Path)
    parser.add_argument(
        "--min-delta",
        type=float,
        default=5.0,
        help="Minimum cross-window mean spread to report (byte values 0..255).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="How many top-ranked (ID, byte) pairs to display.",
    )
    parser.add_argument(
        "--detail-ids",
        type=int,
        default=8,
        help="How many CAN IDs to show full per-window stats for.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return report(
        args.session_dir,
        min_delta=args.min_delta,
        top_n=args.top,
        show_full_per_id=args.detail_ids,
    )


if __name__ == "__main__":
    sys.exit(main())
