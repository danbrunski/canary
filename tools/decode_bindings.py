#!/usr/bin/env python3
"""Decode a session's frames against the canonical bindings.yaml.

Two purposes:

1. **Sanity check** — every entry in bindings.yaml claims a byte position and
   (sometimes) a scaling. This tool walks a captured session, applies each
   binding to every matching frame, and reports the raw/decoded ranges
   observed. If COOLANT_TEMP comes back at 250 °C, the binding is wrong.

2. **Coverage report** — which signals were actually present in a session,
   which weren't, which had no scaling and therefore couldn't be decoded.

Usage:
  tools/decode_bindings.py data/sessions/<timestamp>/
  tools/decode_bindings.py data/sessions/<timestamp>/ --bindings data/bindings.yaml
  tools/decode_bindings.py data/sessions/<timestamp>/ --confidence high,medium
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from src.analysis.iso_tp import iter_frames
from src.decode.bindings import (
    Signal,
    apply_scaling,
    decode_raw,
    is_plausible,
    load_bindings,
)
from src.session import session_paths

_CONFIDENCE_ORDER = ("confirmed", "high", "medium", "low", "hypothesized")


@dataclass
class SignalStats:
    """Aggregated min/max/n for one signal across a session."""

    signal: Signal
    n_samples: int = 0
    raw_min: int | None = None
    raw_max: int | None = None
    decoded_min: float | None = None
    decoded_max: float | None = None
    implausible_samples: int = 0  # decoded values outside plausible_min/max
    implausible_min: float | None = None  # most-extreme low offender
    implausible_max: float | None = None  # most-extreme high offender

    def update(self, raw: int, decoded: float | None) -> None:
        self.n_samples += 1
        if self.raw_min is None or raw < self.raw_min:
            self.raw_min = raw
        if self.raw_max is None or raw > self.raw_max:
            self.raw_max = raw
        if decoded is not None:
            if self.decoded_min is None or decoded < self.decoded_min:
                self.decoded_min = decoded
            if self.decoded_max is None or decoded > self.decoded_max:
                self.decoded_max = decoded
            # Plausibility tally: counts and tracks the worst offenders so the
            # report can flag "N decoded samples outside [min, max]".
            ok = is_plausible(self.signal, decoded)
            if ok is False:
                self.implausible_samples += 1
                if self.implausible_min is None or decoded < self.implausible_min:
                    self.implausible_min = decoded
                if self.implausible_max is None or decoded > self.implausible_max:
                    self.implausible_max = decoded


def collect_stats(frames_path: Path, signals: list[Signal]) -> dict[str, SignalStats]:
    """Walk frames.log; aggregate raw/decoded stats per signal name.

    Pre-indexes signals by CAN ID so the per-frame inner loop is just a
    short list of matching bindings — keeps the walk linear in frame count.
    """
    by_can_id: dict[int, list[Signal]] = defaultdict(list)
    for sig in signals:
        by_can_id[sig.can_id].append(sig)

    stats: dict[str, SignalStats] = {sig.name: SignalStats(signal=sig) for sig in signals}

    for frame in iter_frames(frames_path):
        for sig in by_can_id.get(frame.can_id, ()):
            raw = decode_raw(sig, frame.data)
            if raw is None:
                continue
            decoded = apply_scaling(sig, raw)
            stats[sig.name].update(raw, decoded)

    return stats


def _format_byte_span(sig: Signal) -> str:
    """`0x551:0` for 1-byte, `0x23D:1-2` for multi-byte."""
    if sig.byte_length == 1:
        return f"0x{sig.can_id:03X}:{sig.byte_start}"
    end = sig.byte_start + sig.byte_length - 1
    return f"0x{sig.can_id:03X}:{sig.byte_start}-{end}"


def _format_scaling(sig: Signal) -> str:
    if sig.scale is None:
        return "(no scaling)"
    offset_str = ""
    if sig.offset is not None and sig.offset != 0:
        offset_str = f" {sig.offset:+g}"
    return f"× {sig.scale:g}{offset_str}"


def report(
    session_dir: Path,
    bindings_path: Path,
    *,
    confidence_filter: set[str] | None = None,
) -> int:
    paths = session_paths(session_dir)
    if not paths.frames.exists():
        print(f"error: no frames.log in {session_dir}", file=sys.stderr)
        return 1
    if not bindings_path.exists():
        print(f"error: bindings file not found: {bindings_path}", file=sys.stderr)
        return 1

    bindings = load_bindings(bindings_path)
    signals = bindings.signals
    if confidence_filter:
        signals = [s for s in signals if s.confidence in confidence_filter]

    stats = collect_stats(paths.frames, signals)

    print("=" * 78)
    print(f"BINDING DECODE  {session_dir}")
    print(
        f"  bindings: {bindings_path}  ({len(bindings.signals)} signals total,"
        f" {len(signals)} after filter)"
    )
    print("=" * 78)

    # Group by confidence so the most-trusted bindings come first.
    by_conf: dict[str, list[SignalStats]] = defaultdict(list)
    for s in stats.values():
        by_conf[s.signal.confidence].append(s)

    any_shown = False
    for conf in _CONFIDENCE_ORDER:
        rows = by_conf.get(conf, [])
        if not rows:
            continue
        any_shown = True
        present = [r for r in rows if r.n_samples > 0]
        absent = [r for r in rows if r.n_samples == 0]
        print(
            f"\n  {conf.upper()} CONFIDENCE  ({len(rows)} bindings, "
            f"{len(present)} seen / {len(absent)} missing)"
        )
        for r in sorted(present, key=lambda x: x.signal.name):
            sig = r.signal
            span = _format_byte_span(sig)
            scaling = _format_scaling(sig)
            print(f"\n    {sig.name}")
            print(f"      {span:<13}  {scaling}")
            print(f"      raw     : {r.raw_min:>6} .. {r.raw_max:<6}   n={r.n_samples}")
            if r.decoded_min is not None and r.decoded_max is not None:
                unit = f" {sig.unit}" if sig.unit and sig.unit != "unknown" else ""
                # Plausibility status:
                #   no bounds        → nothing extra
                #   bounds, all OK   → "✓ plausible"
                #   bounds, some bad → "✗ N samples outside [a, b]; worst x..y"
                plaus_str = ""
                if sig.plausible_min is not None or sig.plausible_max is not None:
                    bounds_lo = f"{sig.plausible_min:g}" if sig.plausible_min is not None else "-∞"
                    bounds_hi = f"{sig.plausible_max:g}" if sig.plausible_max is not None else "+∞"
                    if r.implausible_samples == 0:
                        plaus_str = f"  ✓ in [{bounds_lo}, {bounds_hi}]"
                    else:
                        pct = 100.0 * r.implausible_samples / r.n_samples
                        plaus_str = (
                            f"  ✗ {r.implausible_samples} samples ({pct:.1f}%)"
                            f" outside [{bounds_lo}, {bounds_hi}]"
                        )
                line = (
                    f"      decoded : {r.decoded_min:>6.2f} .. "
                    f"{r.decoded_max:<6.2f}{unit}{plaus_str}"
                )
                print(line)
                if r.implausible_samples > 0:
                    worst_lo = f"{r.implausible_min:.2f}" if r.implausible_min is not None else "—"
                    worst_hi = f"{r.implausible_max:.2f}" if r.implausible_max is not None else "—"
                    print(f"                worst offenders: {worst_lo} .. {worst_hi}{unit}")
            else:
                print("      decoded : —  (scale not yet pinned)")
        if absent:
            print(
                "\n    Not seen in this session: "
                + ", ".join(sorted(r.signal.name for r in absent))
            )

    if not any_shown:
        print("\n  No bindings matched the filter.")

    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="decode_bindings",
        description=(
            "Decode a captured session against the canonical bindings.yaml. "
            "Reports per-signal raw/decoded ranges to validate binding "
            "hypotheses and surface missing or implausible signals."
        ),
    )
    parser.add_argument("session_dir", type=Path)
    parser.add_argument(
        "--bindings",
        type=Path,
        default=Path("data/bindings.yaml"),
        help="Path to bindings.yaml (default: data/bindings.yaml).",
    )
    parser.add_argument(
        "--confidence",
        default=None,
        help="Comma-separated confidence levels to include (e.g. 'high,medium'). Default: all.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    confidence_filter: set[str] | None = None
    if args.confidence:
        confidence_filter = {c.strip() for c in args.confidence.split(",") if c.strip()}
    return report(
        args.session_dir,
        args.bindings,
        confidence_filter=confidence_filter,
    )


if __name__ == "__main__":
    sys.exit(main())
