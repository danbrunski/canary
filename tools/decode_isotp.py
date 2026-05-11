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
from src.analysis.signal_lookup import (
    SignalCatalog,
    fuzzy_match_tag,
    load_catalog,
    module_for_can_id,
)


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


def report(
    session_dir: Path, diag_ids: set[int], catalog: SignalCatalog | None = None
) -> int:
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
    _print_did_summary(windows, per_window, catalog)
    _print_window_dominant_dids(windows, per_window, catalog)
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
    windows: list[TagWindow],
    per_window: dict[int, list[UdsMessage]],
    catalog: SignalCatalog | None,
) -> None:
    print()
    print("=" * 78)
    print("MODE 22 DID SUMMARY (response-side; DIDs read in this session)")
    print("=" * 78)

    # DID → window-label → list of response payload bytes
    by_did: dict[int, dict[str, list[bytes]]] = defaultdict(lambda: defaultdict(list))
    # DID → set of CAN IDs the response came from (for module attribution)
    can_ids_per_did: dict[int, set[int]] = defaultdict(set)
    for win_idx, msgs in per_window.items():
        label = windows[win_idx].label
        for msg in msgs:
            if msg.is_response and msg.service_id == 0x22 and msg.did is not None:
                by_did[msg.did][label].append(msg.body)
                can_ids_per_did[msg.did].add(msg.can_id)

    if not by_did:
        print("  (no Mode 22 responses captured)")
        return

    if catalog is None:
        print("  (no FSM signal catalog loaded; pass --signals data/fsm/signals.json"
              " for name suggestions.)")

    print(
        f"\n  {'DID':>6}  {'mod':>5}  {'reads':>6}  {'response':<28}  windows"
    )
    print(f"  {'-' * 6}  {'-' * 5}  {'-' * 6}  {'-' * 28}  {'-' * 30}")
    for did in sorted(by_did):
        per_label = by_did[did]
        all_values: list[bytes] = [v for vs in per_label.values() for v in vs]
        labels = list(per_label.keys())
        marker = "★" if len(labels) == 1 else " "
        labels_str = ", ".join(labels)
        if len(labels_str) > 50:
            labels_str = labels_str[:47] + "..."
        value_range = _format_value_range(all_values)
        # Module attribution from response CAN IDs. Almost always one ID per DID.
        modules = {module_for_can_id(cid) for cid in can_ids_per_did[did]} - {None}
        module_str = next(iter(modules)) if len(modules) == 1 else "?"
        print(
            f"{marker} 0x{did:04X}  {module_str:>5}  {len(all_values):>6}  "
            f"{value_range:<28}  {labels_str}"
        )
        # Name suggestions for ★ DIDs only — multi-window DIDs are housekeeping
        # and naming them based on any one window is misleading.
        if catalog is not None and marker == "★" and module_str != "?":
            tag_label = labels[0]
            matches = fuzzy_match_tag(tag_label, catalog.for_module(module_str), top_k=3)
            if matches:
                top = matches[0]
                others = (
                    f" | also: {', '.join(m.name for m in matches[1:])}"
                    if len(matches) > 1
                    else ""
                )
                print(
                    f"          → candidate: {top.name}  "
                    f"(score={top.score:.2f}, {top.why}, FSM p.{top.page}){others}"
                )
    print()
    print("  ★ = DID read in only one tag window — strongest signal-name candidate.")
    if catalog is not None:
        print("  → candidate names come from `data/fsm/signals.json` "
              "(fuzzy-matched against the tag label).")


def _print_window_dominant_dids(
    windows: list[TagWindow],
    per_window: dict[int, list[UdsMessage]],
    catalog: SignalCatalog | None,
) -> None:
    """For each tag window, surface DIDs polled MUCH more here than elsewhere.

    The "★ single-window" heuristic in the summary table catches DIDs read
    in only one window (mostly ECU-ID block during module-select). The
    *interesting* signal bindings are usually DIDs polled across many
    windows but at much higher rate during the window whose tag names them
    — e.g. NT510 narrows polling to MAF when the MAF screen is open.

    Threshold: a DID is "dominant" in window W if its reads-per-second in
    W is at least 2x its average rate across all other windows AND it had
    at least 5 reads in W. Avoids false positives from one-shot probes.
    """
    dominance_ratio = 2.0
    min_reads_in_window = 5

    # Build per-DID per-window read counts (responses only; matches summary).
    by_did_window: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    can_ids_per_did: dict[int, set[int]] = defaultdict(set)
    for win_idx, msgs in per_window.items():
        for msg in msgs:
            if msg.is_response and msg.service_id == 0x22 and msg.did is not None:
                by_did_window[msg.did][win_idx] += 1
                can_ids_per_did[msg.did].add(msg.can_id)

    print()
    print("=" * 78)
    print("DOMINANT DIDS PER TAG WINDOW (rate-spike candidates for binding)")
    print("=" * 78)

    any_output = False
    for win_idx, w in enumerate(windows):
        duration = max(w.end - w.start, 0.001)
        # Compute (DID, rate-here, ratio-vs-elsewhere) for DIDs read in this window.
        rows: list[tuple[int, float, float, int]] = []
        for did, win_counts in by_did_window.items():
            n_here = win_counts.get(win_idx, 0)
            if n_here < min_reads_in_window:
                continue
            rate_here = n_here / duration
            # Average rate across all OTHER windows the DID appeared in.
            other_total_reads = sum(c for wi, c in win_counts.items() if wi != win_idx)
            other_total_time = sum(
                max(windows[wi].end - windows[wi].start, 0.001)
                for wi, c in win_counts.items()
                if wi != win_idx and c > 0
            )
            rate_elsewhere = other_total_reads / other_total_time if other_total_time > 0 else 0.0
            # Ratio: ∞ if elsewhere=0 (only-here is just like a ★), else ratio.
            ratio = rate_here / rate_elsewhere if rate_elsewhere > 0 else float("inf")
            if ratio >= dominance_ratio:
                rows.append((did, rate_here, ratio, n_here))

        if not rows:
            continue
        any_output = True
        rows.sort(key=lambda r: -r[1])  # by reads-per-second here

        # Try to bind the tag label to FSM names for the dominant module.
        suggestions: list[str] = []
        if catalog is not None:
            modules_seen = {
                m
                for did, _, _, _ in rows
                for m in (module_for_can_id(cid) for cid in can_ids_per_did[did])
                if m is not None
            }
            if len(modules_seen) == 1:
                module = next(iter(modules_seen))
                matches = fuzzy_match_tag(w.label, catalog.for_module(module), top_k=3)
                suggestions = [f"{m.name} (FSM p.{m.page})" for m in matches]

        print(f"\n--- {w.label!r}  ({duration:.1f}s) ---")
        if suggestions:
            print(f"  FSM name candidates from tag label: {', '.join(suggestions)}")
        for did, rate, ratio, n in rows[:5]:
            ratio_str = "only here" if ratio == float("inf") else f"{ratio:.1f}x elsewhere"
            print(f"  0x{did:04X}  {rate:5.1f}/s  ({n} reads, {ratio_str})")

    if not any_output:
        print("\n  (no DIDs met the dominance threshold; either polling was uniform")
        print("   across windows or windows are too short to compute meaningful rates)")


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
    parser.add_argument(
        "--signals",
        type=Path,
        default=Path("data/fsm/signals.json"),
        help=(
            "Path to FSM-extracted signal catalog (default: data/fsm/signals.json). "
            "Pass --no-signals to skip name suggestions entirely."
        ),
    )
    parser.add_argument(
        "--no-signals",
        action="store_true",
        help="Skip loading the FSM signal catalog (no name suggestions).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    diag_ids = (
        parse_diag_ids(args.diag_ids)
        if args.diag_ids
        else {0x7DF, *range(0x7E0, 0x7F0)}
    )
    catalog: SignalCatalog | None = None
    if not args.no_signals and args.signals.exists():
        catalog = load_catalog(args.signals)
    return report(args.session_dir, diag_ids, catalog)


if __name__ == "__main__":
    sys.exit(main())
