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
from src.analysis.range_match import match_did_range
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
    _print_window_bindings(windows, per_window, catalog)
    _print_fsm_range_matches(per_window, catalog)
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


def _print_window_bindings(
    windows: list[TagWindow],
    per_window: dict[int, list[UdsMessage]],
    catalog: SignalCatalog | None,
) -> None:
    """Per-window binding signals: range expansion + presence/absence.

    Two findings drove this design over the prior rate-dominance metric:

      1. NT510 *substitutes* DIDs across screens (not adds-on-top). A DID
         being polled in some windows but absent in others is itself a
         binding signal — likely the NT510-deems-relevant-for-this-screen
         set. Surfaced as "polled-here-but-absent-elsewhere" / its inverse.

      2. The strongest binding signal is value-range expansion correlated
         with a known physical action in the window. e.g. blipping the
         throttle made one DID go from spread ≤8 (at idle) to spread 596
         (during the blip) — a 70x expansion. That DID is unambiguously a
         throttle/MAF/ignition family signal regardless of NT510's polling.

    Each window gets:
      * Tag-fuzzy FSM candidates for the named signal
      * Range-expanded DIDs (spread here ≥5x baseline elsewhere; ≥30 raw)
      * DIDs uniquely polled in this window (already in summary as ★)
      * DIDs polled in many other windows but absent here (suggests the
        named signal is unrelated to those DIDs)
    """
    expansion_ratio_threshold = 5.0
    min_spread_for_expansion = 30
    # Absence is interesting only when a DID is polled in *almost every other*
    # window but missing here — that suggests this window's named signal
    # isn't related to it. A DID polled in only 3 of 10 other windows being
    # absent from an 11th window is meaningless noise.
    max_absence_shown = 5

    # Per-DID per-window: list of decoded integer values (big-endian, all body bytes).
    by_did_win_vals: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    can_ids_per_did: dict[int, set[int]] = defaultdict(set)
    for win_idx, msgs in per_window.items():
        for msg in msgs:
            if (
                msg.is_response
                and msg.service_id == 0x22
                and msg.did is not None
                and msg.body
            ):
                by_did_win_vals[msg.did][win_idx].append(int.from_bytes(msg.body, "big"))
                can_ids_per_did[msg.did].add(msg.can_id)

    # Per-DID set of windows it appeared in (for presence/absence).
    windows_for_did: dict[int, set[int]] = {
        did: set(per_win.keys()) for did, per_win in by_did_win_vals.items()
    }

    print()
    print("=" * 78)
    print("PER-WINDOW BINDING ANALYSIS (presence/absence + range expansion)")
    print("=" * 78)

    n_windows = len(windows)
    any_output = False
    # Tighten as window count grows: only "missing from a near-universal DID"
    # is informative. For 11 windows, requires a DID polled in ≥8 others.
    min_other_windows_for_absence = max(3, n_windows - 3)
    # Windows with no UDS traffic at all (idle baselines) trivially show
    # everything as absent — filter those out so the report stays signal.
    windows_with_traffic: set[int] = {
        wi for wi, msgs in per_window.items()
        if any(m.is_response and m.service_id == 0x22 for m in msgs)
    }

    for win_idx, w in enumerate(windows):
        duration = max(w.end - w.start, 0.001)

        # Range expansion: DID's spread in this window vs max spread elsewhere.
        expanded: list[tuple[int, int, int, float]] = []  # (did, spread_here, baseline, ratio)
        for did, by_win in by_did_win_vals.items():
            vals_here = by_win.get(win_idx, [])
            if len(vals_here) < 2:
                continue
            spread_here = max(vals_here) - min(vals_here)
            if spread_here < min_spread_for_expansion:
                continue
            other_spreads = [
                max(v) - min(v) for wi, v in by_win.items() if wi != win_idx and len(v) >= 2
            ]
            baseline = max(other_spreads) if other_spreads else 0
            if baseline == 0:
                # No comparable elsewhere; treat as infinite ratio.
                ratio = float("inf")
            else:
                ratio = spread_here / baseline
            if ratio >= expansion_ratio_threshold:
                expanded.append((did, spread_here, baseline, ratio))

        # Window-unique (only-polled-here): same as ★ in summary, listed for context.
        unique = [
            did
            for did, win_set in windows_for_did.items()
            if win_set == {win_idx}
        ]

        # Absence: polled in many other windows but missing here. Suggests the
        # named signal is unrelated to these DIDs. Skip absence entirely for
        # windows with no UDS traffic (everything's trivially absent).
        absent: list[int] = (
            [
                did
                for did, win_set in windows_for_did.items()
                if win_idx not in win_set
                and len(win_set) >= min_other_windows_for_absence
            ]
            if win_idx in windows_with_traffic
            else []
        )

        if not expanded and not unique and not absent:
            continue
        any_output = True

        # Tag-fuzzy FSM candidates (best guess for what this window is testing).
        suggestions: list[str] = []
        if catalog is not None:
            modules_seen = {
                m
                for did in (
                    [d for d, _, _, _ in expanded] + unique + absent
                )
                for m in (module_for_can_id(cid) for cid in can_ids_per_did.get(did, set()))
                if m is not None
            }
            if len(modules_seen) == 1:
                module = next(iter(modules_seen))
                matches = fuzzy_match_tag(w.label, catalog.for_module(module), top_k=3)
                suggestions = [f"{m.name} (FSM p.{m.page})" for m in matches]

        print(f"\n--- {w.label!r}  ({duration:.1f}s) ---")
        if suggestions:
            print(f"  Tag-fuzzy FSM candidates: {', '.join(suggestions)}")

        if expanded:
            print("  Range-expanded DIDs (responded physically in this window):")
            expanded.sort(key=lambda r: -r[3])
            for did, spread_h, baseline, ratio in expanded[:5]:
                ratio_str = "no baseline" if ratio == float("inf") else f"{ratio:.0f}x baseline"
                print(
                    f"    0x{did:04X}  spread {spread_h} here  vs ≤{baseline} elsewhere "
                    f"({ratio_str})"
                )

        if unique:
            print(f"  DIDs polled only in this window ({len(unique)} total, ★ in summary):")
            for did in sorted(unique)[:5]:
                print(f"    0x{did:04X}")
            if len(unique) > 5:
                print(f"    ... +{len(unique) - 5} more")

        if absent and n_windows > 2:
            print(
                f"  DIDs polled in ≥{min_other_windows_for_absence} other windows but absent here:"
            )
            for did in sorted(absent)[:max_absence_shown]:
                print(f"    0x{did:04X}  (in {len(windows_for_did[did])} other windows)")
            if len(absent) > max_absence_shown:
                print(f"    ... +{len(absent) - max_absence_shown} more")

    if not any_output:
        print("\n  (no per-window binding signals found)")


def _print_fsm_range_matches(
    per_window: dict[int, list[UdsMessage]],
    catalog: SignalCatalog | None,
) -> None:
    """Match each varying DID's captured range against FSM expected ranges.

    Independent evidence from tag-fuzzy + range-expansion: even DIDs without
    a meaningful tag context get bound here when their value range fits a
    known FSM signal under a plausible scaling. This is what turns the soft
    'I think 0x1226 is throttle-related' guess into 'MASS AIRFLOW at /32 —
    decoded 2.25-20.88 g·m/s matches FSM expected 2.0-6.0 idle / 7.0-20.0
    revved' with derived scaling and offset.
    """
    if catalog is None:
        return

    # Aggregate captured raw range per DID; track CAN IDs for module attribution.
    by_did_can: dict[int, set[int]] = defaultdict(set)
    by_did_range: dict[int, tuple[int, int]] = {}
    for msgs in per_window.values():
        for msg in msgs:
            if (
                msg.is_response
                and msg.service_id == 0x22
                and msg.did is not None
                and msg.body
            ):
                val = int.from_bytes(msg.body, "big")
                by_did_can[msg.did].add(msg.can_id)
                lo, hi = by_did_range.get(msg.did, (val, val))
                by_did_range[msg.did] = (min(lo, val), max(hi, val))

    print()
    print("=" * 78)
    print("FSM RANGE-MATCH BINDINGS (DID raw range vs FSM expected range)")
    print("=" * 78)

    any_output = False
    for did in sorted(by_did_range):
        raw_lo, raw_hi = by_did_range[did]
        if raw_hi == raw_lo:
            continue  # constants can't be range-matched
        modules = {module_for_can_id(cid) for cid in by_did_can[did]} - {None}
        if not modules:
            continue
        candidates = []
        for module in modules:
            candidates.extend(catalog.for_module(module))
        if not candidates:
            continue
        # Bigger top_k here than the matcher default — many ECM signals have
        # generic FSM ranges (0-65, 0-80%, etc.) that fit any captured data
        # by coincidence. Show more candidates so the actual binding (often
        # a more specific range) isn't truncated by coincidental matches.
        matches = match_did_range(raw_lo, raw_hi, candidates, top_k=8)
        if not matches:
            continue
        any_output = True
        module_str = ", ".join(sorted(modules))
        print(f"\n  0x{did:04X}  [{module_str}]  raw range {raw_lo}..{raw_hi}")
        for m in matches:
            scale_str = f"×{m.scale:g}"
            if m.offset != 0:
                scale_str = f"{scale_str}{m.offset:+g}"
            decoded_str = f"[{m.decoded_min:.2f}, {m.decoded_max:.2f}]"
            unit = m.expected.unit or "(no unit)"
            exp_lo = "−∞" if m.expected.min == float("-inf") else f"{m.expected.min:g}"
            exp_hi = "∞" if m.expected.max == float("inf") else f"{m.expected.max:g}"
            print(
                f"    score={m.score:.2f}  {m.param.name:25s}  scale={scale_str:>14s}  "
                f"decoded {decoded_str} vs FSM [{exp_lo}, {exp_hi}] {unit}  (p.{m.param.page})"
            )

    if not any_output:
        print("\n  (no DID raw ranges fit any FSM expected range under tested scalings)")
        return
    print()
    print("  Caveat: FSM range extraction has known row-attribution imperfections")
    print("  (some ranges are mis-attributed to neighboring params). Treat matches")
    print("  as candidate bindings to disambiguate via ELM327 cross-reference, not")
    print("  as confirmations on their own. Multi-window and physically-correlated")
    print("  signals (e.g. throttle-blip → MAF/IGN TIMING/INJ PULSE all spike) may")
    print("  produce multiple high-scoring candidates that are all defensible.")


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
