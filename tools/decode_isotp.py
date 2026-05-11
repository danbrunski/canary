#!/usr/bin/env python3
"""Decode ISO-TP / UDS traffic in a recorded session and group by tag window.

Usage:
  tools/decode_isotp.py data/sessions/<timestamp>/
  tools/decode_isotp.py data/sessions/<timestamp>/ --diag-ids 7E0,7E8,7E1,7E9

Reads frames.log + tags.jsonl from the session directory, reassembles ISO-TP
messages on the diagnostic IDs, decodes UDS, and prints:
  1. Per-tag-window: ordered list of UDS exchanges
  2. Final summary: each Mode 22 DID, which tag windows it appeared in,
     response width, value range — i.e. candidate signal map
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from src.analysis.iso_tp import UdsMessage, decode_session, iter_frames


@dataclass(frozen=True)
class TagWindow:
    """A [start, end) interval defined by a start tag and the next tag."""

    label: str
    start: float
    end: float


def load_tags(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r") as f:
        return [json.loads(line) for line in f if line.strip()]


def build_windows(tags: list[dict], session_end: float) -> list[TagWindow]:
    """Each tag opens a window that closes when the next tag fires."""
    windows: list[TagWindow] = []
    for i, tag in enumerate(tags):
        start = float(tag["t"])
        end = float(tags[i + 1]["t"]) if i + 1 < len(tags) else session_end
        windows.append(TagWindow(label=tag["label"], start=start, end=end))
    return windows


def parse_diag_ids(raw: str) -> set[int]:
    return {int(s, 16) for s in raw.split(",") if s.strip()}


def _format_value_range(values: list[bytes]) -> str:
    """Show the observed range of response payloads, compactly."""
    if not values:
        return "—"
    widths = {len(v) for v in values}
    width_str = (
        f"{next(iter(widths))}B" if len(widths) == 1 else f"{min(widths)}-{max(widths)}B"
    )
    if len(widths) == 1 and next(iter(widths)) <= 4:
        # Show numeric range when payload is uniformly small.
        ints = sorted({int.from_bytes(v, "big") for v in values})
        if len(ints) == 1:
            return f"{width_str} const=0x{ints[0]:0{2 * len(values[0])}X}"
        return (
            f"{width_str} 0x{ints[0]:0{2 * len(values[0])}X}..0x{ints[-1]:0{2 * len(values[0])}X} "
            f"({len(ints)} distinct)"
        )
    return f"{width_str} ({len({bytes(v) for v in values})} distinct)"


def report(session_dir: Path, diag_ids: set[int]) -> int:
    frames_path = session_dir / "frames.log"
    tags_path = session_dir / "tags.jsonl"
    if not frames_path.exists():
        print(f"error: no frames.log in {session_dir}", file=sys.stderr)
        return 1

    tags = load_tags(tags_path)
    if not tags:
        print(f"warning: no tags.jsonl in {session_dir}; reporting whole session as one window",
              file=sys.stderr)

    # Two passes: one to learn the time bounds, one to decode. The capture is
    # large but linear; reading it twice is still cheap relative to writing it.
    first_ts: float | None = None
    last_ts: float = 0.0
    for f in iter_frames(frames_path):
        if first_ts is None:
            first_ts = f.timestamp
        last_ts = f.timestamp
    if first_ts is None:
        print(f"error: frames.log in {session_dir} has no parseable frames", file=sys.stderr)
        return 1

    windows = (
        build_windows(tags, session_end=last_ts + 0.001)
        if tags
        else [TagWindow(label="(whole session)", start=first_ts, end=last_ts + 0.001)]
    )

    # Bucket UDS messages into windows. Linear scan; windows are non-overlapping
    # and ordered, so we maintain a single window cursor.
    per_window: dict[int, list[UdsMessage]] = defaultdict(list)
    cursor = 0
    for msg in decode_session(iter_frames(frames_path), diag_ids=diag_ids):
        while cursor < len(windows) and msg.timestamp >= windows[cursor].end:
            cursor += 1
        if cursor >= len(windows):
            break
        if msg.timestamp >= windows[cursor].start:
            per_window[cursor].append(msg)

    _print_per_window(windows, per_window)
    _print_did_summary(windows, per_window)
    return 0


def _print_per_window(
    windows: list[TagWindow], per_window: dict[int, list[UdsMessage]]
) -> None:
    print("=" * 78)
    print("PER-WINDOW ISO-TP / UDS EXCHANGES")
    print("=" * 78)
    for i, w in enumerate(windows):
        msgs = per_window.get(i, [])
        duration = w.end - w.start
        print(f"\n--- {w.label!r}  ({duration:.1f}s, {len(msgs)} UDS msgs) ---")
        if not msgs:
            print("  (no diagnostic traffic)")
            continue
        # Cap per-window output so a long live-data stream doesn't drown the report.
        for msg in msgs[:30]:
            print(f"  {msg.timestamp - w.start:6.2f}s  0x{msg.can_id:03X}  {msg.short}")
        if len(msgs) > 30:
            print(f"  ... and {len(msgs) - 30} more")


def _print_did_summary(
    windows: list[TagWindow], per_window: dict[int, list[UdsMessage]]
) -> None:
    print()
    print("=" * 78)
    print("MODE 22 DID SUMMARY (response-side; DIDs read in this session)")
    print("=" * 78)

    # DID → window-label → list of response payload bytes
    by_did: dict[int, dict[str, list[bytes]]] = defaultdict(lambda: defaultdict(list))
    for win_idx, msgs in per_window.items():
        label = windows[win_idx].label
        for msg in msgs:
            if msg.is_response and msg.service_id == 0x22 and msg.did is not None:
                by_did[msg.did][label].append(msg.body)

    if not by_did:
        print("  (no Mode 22 responses captured)")
        return

    print(
        f"\n  {'DID':>6}  {'reads':>6}  {'response':<28}  windows"
    )
    print(f"  {'-' * 6}  {'-' * 6}  {'-' * 28}  {'-' * 30}")
    for did in sorted(by_did):
        per_label = by_did[did]
        all_values: list[bytes] = [v for vs in per_label.values() for v in vs]
        labels = list(per_label.keys())
        # Highlight "single-window" DIDs — those are strongest signal-name candidates.
        marker = "★" if len(labels) == 1 else " "
        labels_str = ", ".join(labels)
        if len(labels_str) > 60:
            labels_str = labels_str[:57] + "..."
        value_range = _format_value_range(all_values)
        print(
            f"{marker} 0x{did:04X}  {len(all_values):>6}  {value_range:<28}  {labels_str}"
        )
    print()
    print("  ★ = DID was only read in one tag window — strong signal-name candidate.")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="decode_isotp",
        description="Decode ISO-TP/UDS traffic in a recorded session, grouped by tag window.",
    )
    parser.add_argument("session_dir", type=Path, help="Path to data/sessions/<timestamp>/")
    parser.add_argument(
        "--diag-ids",
        default=None,
        help="Comma-separated hex IDs to treat as diagnostic (default: 7DF,7E0..7EF).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    diag_ids = (
        parse_diag_ids(args.diag_ids)
        if args.diag_ids
        else {0x7DF, *range(0x7E0, 0x7F0)}
    )
    return report(args.session_dir, diag_ids)


if __name__ == "__main__":
    sys.exit(main())
