"""Session-level ISO-TP / UDS diagnostic reporting."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from src.analysis.diagnostics import body_as_big_endian_int, body_as_scalar_int
from src.analysis.iso_tp import UdsMessage, decode_session, iter_frames
from src.analysis.range_match import match_did_range
from src.analysis.signal_lookup import (
    SignalCatalog,
    fuzzy_match_tag,
    module_for_can_id,
)
from src.session import FRAMES_FILENAME, TagWindow, build_windows, load_tags, session_paths


@dataclass(frozen=True)
class DecodedDiagnosticSession:
    """A recorded session decoded into UDS messages grouped by tag window."""

    windows: list[TagWindow]
    per_window: dict[int, list[UdsMessage]]
    had_tags: bool


def load_decoded_diagnostic_session(
    session_dir: Path, diag_ids: set[int]
) -> DecodedDiagnosticSession:
    """Load frames/tags from a session and bucket decoded UDS messages by window."""
    paths = session_paths(session_dir)
    if not paths.frames.exists():
        raise FileNotFoundError(f"no {FRAMES_FILENAME} in {session_dir}")

    tags = load_tags(paths.tags)

    first_ts: float | None = None
    last_ts = 0.0
    for frame in iter_frames(paths.frames):
        if first_ts is None:
            first_ts = frame.timestamp
        last_ts = frame.timestamp
    if first_ts is None:
        raise ValueError(f"{FRAMES_FILENAME} in {session_dir} has no parseable frames")

    windows = (
        build_windows(tags, session_start=first_ts, session_end=last_ts + 0.001)
        if tags
        else [TagWindow(label="(whole session)", start=first_ts, end=last_ts + 0.001)]
    )

    per_window: dict[int, list[UdsMessage]] = {}
    cursor = 0
    for msg in decode_session(iter_frames(paths.frames), diag_ids=diag_ids):
        while cursor < len(windows) and msg.timestamp >= windows[cursor].end:
            cursor += 1
        if cursor >= len(windows):
            break
        if msg.timestamp >= windows[cursor].start:
            per_window.setdefault(cursor, []).append(msg)

    return DecodedDiagnosticSession(windows=windows, per_window=per_window, had_tags=bool(tags))


def report(
    session_dir: Path,
    diag_ids: set[int],
    catalog: SignalCatalog | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Print the full diagnostic report for a session."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    try:
        decoded = load_decoded_diagnostic_session(session_dir, diag_ids)
    except FileNotFoundError as e:
        print(f"error: {e}", file=err)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=err)
        return 1

    if not decoded.had_tags:
        print(
            f"warning: no tags.jsonl in {session_dir}; reporting whole session as one window",
            file=err,
        )

    _print_per_window(decoded.windows, decoded.per_window, out)
    _print_did_summary(decoded.windows, decoded.per_window, catalog, out)
    _print_window_bindings(decoded.windows, decoded.per_window, catalog, out)
    _print_fsm_range_matches(decoded.per_window, catalog, out)
    return 0


def _format_value_range(values: list[bytes]) -> str:
    """Show the observed range of response payloads, compactly."""
    if not values:
        return "-"
    widths = {len(v) for v in values}
    width_str = f"{next(iter(widths))}B" if len(widths) == 1 else f"{min(widths)}-{max(widths)}B"
    if len(widths) == 1 and next(iter(widths)) <= 2:
        ints = sorted({body_as_big_endian_int(v) for v in values})
        if len(ints) == 1:
            return f"{width_str} const=0x{ints[0]:0{2 * len(values[0])}X}"
        return (
            f"{width_str} 0x{ints[0]:0{2 * len(values[0])}X}.."
            f"0x{ints[-1]:0{2 * len(values[0])}X} ({len(ints)} distinct)"
        )
    return f"{width_str} ({len({bytes(v) for v in values})} distinct)"


def _print_per_window(
    windows: list[TagWindow], per_window: dict[int, list[UdsMessage]], out: TextIO
) -> None:
    print("=" * 78, file=out)
    print("PER-WINDOW ISO-TP / UDS EXCHANGES", file=out)
    print("=" * 78, file=out)
    for i, window in enumerate(windows):
        msgs = per_window.get(i, [])
        duration = window.end - window.start
        print(f"\n--- {window.label!r}  ({duration:.1f}s, {len(msgs)} UDS msgs) ---", file=out)
        if not msgs:
            print("  (no diagnostic traffic)", file=out)
            continue
        for msg in msgs[:30]:
            print(
                f"  {msg.timestamp - window.start:6.2f}s  0x{msg.can_id:03X}  {msg.short}",
                file=out,
            )
        if len(msgs) > 30:
            print(f"  ... and {len(msgs) - 30} more", file=out)


def _print_did_summary(
    windows: list[TagWindow],
    per_window: dict[int, list[UdsMessage]],
    catalog: SignalCatalog | None,
    out: TextIO,
) -> None:
    print(file=out)
    print("=" * 78, file=out)
    print("MODE 22 DID SUMMARY (response-side; DIDs read in this session)", file=out)
    print("=" * 78, file=out)

    by_did: dict[int, dict[str, list[bytes]]] = {}
    can_ids_per_did: dict[int, set[int]] = {}
    for win_idx, msgs in per_window.items():
        label = windows[win_idx].label
        for msg in msgs:
            if msg.is_response and msg.service_id == 0x22 and msg.did is not None:
                by_did.setdefault(msg.did, {}).setdefault(label, []).append(msg.body)
                can_ids_per_did.setdefault(msg.did, set()).add(msg.can_id)

    if not by_did:
        print("  (no Mode 22 responses captured)", file=out)
        return

    if catalog is None:
        print(
            "  (no FSM signal catalog loaded; pass --signals data/fsm/signals.json"
            " for name suggestions.)",
            file=out,
        )

    print(f"\n  {'DID':>6}  {'mod':>5}  {'reads':>6}  {'response':<28}  windows", file=out)
    print(f"  {'-' * 6}  {'-' * 5}  {'-' * 6}  {'-' * 28}  {'-' * 30}", file=out)
    for did in sorted(by_did):
        per_label = by_did[did]
        all_values: list[bytes] = [value for values in per_label.values() for value in values]
        labels = list(per_label.keys())
        marker = "*" if len(labels) == 1 else " "
        labels_str = ", ".join(labels)
        if len(labels_str) > 50:
            labels_str = labels_str[:47] + "..."
        value_range = _format_value_range(all_values)

        modules: set[str] = set()
        for can_id in can_ids_per_did[did]:
            module = module_for_can_id(can_id)
            if module is not None:
                modules.add(module)
        module_str = next(iter(modules)) if len(modules) == 1 else "?"

        print(
            f"{marker} 0x{did:04X}  {module_str:>5}  {len(all_values):>6}  "
            f"{value_range:<28}  {labels_str}",
            file=out,
        )
        if catalog is not None and marker == "*" and module_str != "?":
            tag_label = labels[0]
            matches = fuzzy_match_tag(tag_label, catalog.for_module(module_str), top_k=3)
            if matches:
                top = matches[0]
                others = (
                    f" | also: {', '.join(match.name for match in matches[1:])}"
                    if len(matches) > 1
                    else ""
                )
                print(
                    f"          -> candidate: {top.name}  "
                    f"(score={top.score:.2f}, {top.why}, FSM p.{top.page}){others}",
                    file=out,
                )
    print(file=out)
    print("  * = DID read in only one tag window - strongest signal-name candidate.", file=out)
    if catalog is not None:
        print(
            "  -> candidate names come from `data/fsm/signals.json` "
            "(fuzzy-matched against the tag label).",
            file=out,
        )


def _print_window_bindings(
    windows: list[TagWindow],
    per_window: dict[int, list[UdsMessage]],
    catalog: SignalCatalog | None,
    out: TextIO,
) -> None:
    expansion_ratio_threshold = 5.0
    min_spread_for_expansion = 30
    max_absence_shown = 5

    by_did_win_vals: dict[int, dict[int, list[int]]] = {}
    can_ids_per_did: dict[int, set[int]] = {}
    for win_idx, msgs in per_window.items():
        for msg in msgs:
            if msg.is_response and msg.service_id == 0x22 and msg.did is not None and msg.body:
                value = body_as_scalar_int(msg.body)
                if value is None:
                    continue
                by_did_win_vals.setdefault(msg.did, {}).setdefault(win_idx, []).append(value)
                can_ids_per_did.setdefault(msg.did, set()).add(msg.can_id)

    windows_for_did: dict[int, set[int]] = {
        did: set(per_win.keys()) for did, per_win in by_did_win_vals.items()
    }

    print(file=out)
    print("=" * 78, file=out)
    print("PER-WINDOW BINDING ANALYSIS (presence/absence + range expansion)", file=out)
    print("=" * 78, file=out)

    n_windows = len(windows)
    any_output = False
    min_other_windows_for_absence = max(3, n_windows - 3)
    windows_with_traffic: set[int] = {
        win_idx
        for win_idx, msgs in per_window.items()
        if any(msg.is_response and msg.service_id == 0x22 for msg in msgs)
    }

    for win_idx, window in enumerate(windows):
        duration = max(window.end - window.start, 0.001)

        expanded: list[tuple[int, int, int, float]] = []
        for did, by_win in by_did_win_vals.items():
            vals_here = by_win.get(win_idx, [])
            if len(vals_here) < 2:
                continue
            spread_here = max(vals_here) - min(vals_here)
            if spread_here < min_spread_for_expansion:
                continue
            other_spreads = [
                max(values) - min(values)
                for other_win_idx, values in by_win.items()
                if other_win_idx != win_idx and len(values) >= 2
            ]
            baseline = max(other_spreads) if other_spreads else 0
            ratio = float("inf") if baseline == 0 else spread_here / baseline
            if ratio >= expansion_ratio_threshold:
                expanded.append((did, spread_here, baseline, ratio))

        unique = [did for did, win_set in windows_for_did.items() if win_set == {win_idx}]

        absent = (
            [
                did
                for did, win_set in windows_for_did.items()
                if win_idx not in win_set and len(win_set) >= min_other_windows_for_absence
            ]
            if win_idx in windows_with_traffic
            else []
        )

        if not expanded and not unique and not absent:
            continue
        any_output = True

        suggestions: list[str] = []
        if catalog is not None:
            modules_seen: set[str] = set()
            for did in [did for did, _, _, _ in expanded] + unique + absent:
                for can_id in can_ids_per_did.get(did, set()):
                    module = module_for_can_id(can_id)
                    if module is not None:
                        modules_seen.add(module)
            if len(modules_seen) == 1:
                module = next(iter(modules_seen))
                matches = fuzzy_match_tag(window.label, catalog.for_module(module), top_k=3)
                suggestions = [f"{match.name} (FSM p.{match.page})" for match in matches]

        print(f"\n--- {window.label!r}  ({duration:.1f}s) ---", file=out)
        if suggestions:
            print(f"  Tag-fuzzy FSM candidates: {', '.join(suggestions)}", file=out)

        if expanded:
            print("  Range-expanded DIDs (responded physically in this window):", file=out)
            expanded.sort(key=lambda row: -row[3])
            for did, spread_h, baseline, ratio in expanded[:5]:
                ratio_str = "no baseline" if ratio == float("inf") else f"{ratio:.0f}x baseline"
                print(
                    f"    0x{did:04X}  spread {spread_h} here  vs <={baseline} elsewhere "
                    f"({ratio_str})",
                    file=out,
                )

        if unique:
            print(
                f"  DIDs polled only in this window ({len(unique)} total, * in summary):",
                file=out,
            )
            for did in sorted(unique)[:5]:
                print(f"    0x{did:04X}", file=out)
            if len(unique) > 5:
                print(f"    ... +{len(unique) - 5} more", file=out)

        if absent and n_windows > 2:
            absence_header = (
                f"  DIDs polled in >={min_other_windows_for_absence} other windows but absent here:"
            )
            print(
                absence_header,
                file=out,
            )
            for did in sorted(absent)[:max_absence_shown]:
                print(f"    0x{did:04X}  (in {len(windows_for_did[did])} other windows)", file=out)
            if len(absent) > max_absence_shown:
                print(f"    ... +{len(absent) - max_absence_shown} more", file=out)

    if not any_output:
        print("\n  (no per-window binding signals found)", file=out)


def _print_fsm_range_matches(
    per_window: dict[int, list[UdsMessage]],
    catalog: SignalCatalog | None,
    out: TextIO,
) -> None:
    if catalog is None:
        return

    by_did_can: dict[int, set[int]] = {}
    by_did_range: dict[int, tuple[int, int]] = {}
    for msgs in per_window.values():
        for msg in msgs:
            if msg.is_response and msg.service_id == 0x22 and msg.did is not None and msg.body:
                val = body_as_scalar_int(msg.body)
                if val is None:
                    continue
                by_did_can.setdefault(msg.did, set()).add(msg.can_id)
                lo, hi = by_did_range.get(msg.did, (val, val))
                by_did_range[msg.did] = (min(lo, val), max(hi, val))

    print(file=out)
    print("=" * 78, file=out)
    print("FSM RANGE-MATCH BINDINGS (DID raw range vs FSM expected range)", file=out)
    print("=" * 78, file=out)

    any_output = False
    for did in sorted(by_did_range):
        raw_lo, raw_hi = by_did_range[did]
        if raw_hi == raw_lo:
            continue

        modules: set[str] = set()
        for can_id in by_did_can[did]:
            module = module_for_can_id(can_id)
            if module is not None:
                modules.add(module)
        if not modules:
            continue

        candidates = []
        for module in modules:
            candidates.extend(catalog.for_module(module))
        if not candidates:
            continue

        matches = match_did_range(raw_lo, raw_hi, candidates, top_k=8)
        if not matches:
            continue

        any_output = True
        module_str = ", ".join(sorted(modules))
        print(f"\n  0x{did:04X}  [{module_str}]  raw range {raw_lo}..{raw_hi}", file=out)
        for match in matches:
            scale_str = f"x{match.scale:g}"
            if match.offset != 0:
                scale_str = f"{scale_str}{match.offset:+g}"
            decoded_str = f"[{match.decoded_min:.2f}, {match.decoded_max:.2f}]"
            unit = match.expected.unit or "(no unit)"
            exp_lo = "-inf" if match.expected.min == float("-inf") else f"{match.expected.min:g}"
            exp_hi = "inf" if match.expected.max == float("inf") else f"{match.expected.max:g}"
            print(
                f"    score={match.score:.2f}  {match.param.name:25s}  "
                f"scale={scale_str:>14s}  decoded {decoded_str} vs FSM "
                f"[{exp_lo}, {exp_hi}] {unit}  (p.{match.param.page})",
                file=out,
            )

    if not any_output:
        print("\n  (no DID raw ranges fit any FSM expected range under tested scalings)", file=out)
        return
    print(file=out)
    print("  Caveat: FSM range extraction has known row-attribution imperfections", file=out)
    print("  (some ranges are mis-attributed to neighboring params). Treat matches", file=out)
    print("  as candidate bindings to disambiguate via ELM327 cross-reference, not", file=out)
    print("  as confirmations on their own. Multi-window and physically-correlated", file=out)
    print("  signals may produce multiple high-scoring candidates.", file=out)
