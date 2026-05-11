#!/usr/bin/env python3
"""Cross-reference captured Mode 22 DIDs against Mode 01 PID ground truth.

The decisive disambiguator on top of FSM range-matching: when a recording
session has both `frames.log` (CAN bus capture, includes Mode 22 reads
from NT510 + similar) AND `pids.jsonl` (decoded Mode 01 readings from
python-obd via an ELM327), we can pair the two streams by timestamp and
linear-regress decoded ground truth against raw Mode 22 values.

A clean fit (r² > 0.5) IS the binding, with the slope and offset fixed
as a byproduct. No more guessing scalings.

Usage:
  tools/cross_reference.py data/sessions/<timestamp>/

Requires the session was recorded with `record.py --obd-port ...` so a
`pids.jsonl` sidecar exists. Without it, this tool errors out with
guidance.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from math import sqrt
from pathlib import Path

from src.analysis.iso_tp import decode_session, iter_frames

# Default diagnostic-ID filter — same as the decoder.
_DEFAULT_DIAG_IDS = {0x7DF, *range(0x7E0, 0x7F0)}


@dataclass(frozen=True)
class CrossRefBinding:
    """One scored DID → PID binding from cross-reference regression."""

    did: int
    pid: str
    pid_unit: str
    n_pairs: int
    r_squared: float
    slope: float
    offset: float
    raw_range: tuple[int, int]
    decoded_range: tuple[float, float]


def load_pids_jsonl(path: Path) -> dict[str, list[tuple[float, float]]]:
    """PID → list of (timestamp, decoded_value), sorted by timestamp.

    Skips non-numeric values (VIN, DTCs, etc.) — those can't be regressed.
    """
    by_pid: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        value = rec.get("value")
        if not isinstance(value, (int, float)):
            continue
        by_pid[rec["pid"]].append((float(rec["t"]), float(value)))
    for pid in by_pid:
        by_pid[pid].sort()
    return dict(by_pid)


def load_pid_units(path: Path) -> dict[str, str]:
    """First-seen unit per PID (the value stays consistent across reads)."""
    units: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec["pid"] not in units:
            units[rec["pid"]] = rec.get("unit", "")
    return units


def load_did_timeseries(
    frames_path: Path, diag_ids: set[int]
) -> dict[int, list[tuple[float, int]]]:
    """DID → list of (timestamp, raw_value), sorted by timestamp."""
    by_did: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for msg in decode_session(iter_frames(frames_path), diag_ids=diag_ids):
        if (
            msg.is_response
            and msg.service_id == 0x22
            and msg.did is not None
            and msg.body
        ):
            by_did[msg.did].append(
                (msg.timestamp, int.from_bytes(msg.body, "big"))
            )
    for did in by_did:
        by_did[did].sort()
    return dict(by_did)


def pair_by_timestamp(
    a: list[tuple[float, float]],
    b: list[tuple[float, float]],
    tolerance_seconds: float = 0.5,
) -> list[tuple[float, float]]:
    """For each `(t, va)` in `a`, find nearest `(s, vb)` in `b` within
    `tolerance_seconds`. Returns the matched `(va, vb)` pairs.

    O(n+m) using a single forward sweep — both inputs must be sorted.
    """
    if not a or not b:
        return []
    pairs: list[tuple[float, float]] = []
    j = 0
    for ta, va in a:
        # Advance j while the next b is closer in time.
        while j + 1 < len(b) and abs(b[j + 1][0] - ta) <= abs(b[j][0] - ta):
            j += 1
        # Defensive walk-back in case caller passed b that's not strictly sorted.
        while j > 0 and abs(b[j - 1][0] - ta) < abs(b[j][0] - ta):
            j -= 1
        if abs(b[j][0] - ta) <= tolerance_seconds:
            pairs.append((va, b[j][1]))
    return pairs


def linear_regression(pairs: list[tuple[float, float]]) -> tuple[float, float, float]:
    """OLS fit `y = slope * x + offset`. Returns `(slope, offset, r²)`.

    `r²` is Pearson's correlation coefficient squared, in [0, 1].
    Returns `(0, 0, 0)` for degenerate inputs (constant x, constant y, n<3).
    """
    n = len(pairs)
    if n < 3:
        return 0.0, 0.0, 0.0
    sx = sy = sxx = syy = sxy = 0.0
    for x, y in pairs:
        sx += x
        sy += y
        sxx += x * x
        syy += y * y
        sxy += x * y
    denom = n * sxx - sx * sx
    if denom == 0:  # constant x → no signal to fit against
        return 0.0, 0.0, 0.0
    slope = (n * sxy - sx * sy) / denom
    offset = (sy - slope * sx) / n
    var_y = n * syy - sy * sy
    if var_y == 0:  # constant y → no variation to explain
        return slope, offset, 0.0
    r = (n * sxy - sx * sy) / sqrt(denom * var_y)
    return slope, offset, r * r


def cross_reference(
    session_dir: Path,
    *,
    min_pairs: int = 20,
    min_r_squared: float = 0.5,
    tolerance_seconds: float = 0.5,
) -> list[CrossRefBinding]:
    """End-to-end: load both streams, pair, regress, return bindings.

    Bindings sorted by r² descending. Multiple bindings per DID are
    possible (one per high-correlation PID); caller decides how to display.
    """
    frames_path = session_dir / "frames.log"
    pids_path = session_dir / "pids.jsonl"
    if not pids_path.exists():
        raise FileNotFoundError(
            f"no pids.jsonl in {session_dir} — re-record with "
            "`record.py --obd-port /dev/ttyUSB0 ...` to capture Mode 01 "
            "ground truth alongside the CAN stream."
        )
    if not frames_path.exists():
        raise FileNotFoundError(f"no frames.log in {session_dir}")

    did_series = load_did_timeseries(frames_path, _DEFAULT_DIAG_IDS)
    pid_series = load_pids_jsonl(pids_path)
    pid_units = load_pid_units(pids_path)

    bindings: list[CrossRefBinding] = []
    for did, dpoints in did_series.items():
        for pid, ppoints in pid_series.items():
            pairs = pair_by_timestamp(dpoints, ppoints, tolerance_seconds=tolerance_seconds)
            if len(pairs) < min_pairs:
                continue
            slope, offset, r2 = linear_regression(pairs)
            if r2 < min_r_squared:
                continue
            raw_vals = [int(x) for x, _ in pairs]
            dec_vals = [y for _, y in pairs]
            bindings.append(
                CrossRefBinding(
                    did=did,
                    pid=pid,
                    pid_unit=pid_units.get(pid, ""),
                    n_pairs=len(pairs),
                    r_squared=r2,
                    slope=slope,
                    offset=offset,
                    raw_range=(min(raw_vals), max(raw_vals)),
                    decoded_range=(min(dec_vals), max(dec_vals)),
                )
            )

    bindings.sort(key=lambda b: -b.r_squared)
    return bindings


def report(session_dir: Path, **kwargs: object) -> int:
    try:
        bindings = cross_reference(session_dir, **kwargs)  # type: ignore[arg-type]
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print("=" * 78)
    print("CROSS-REFERENCE BINDINGS (Mode 22 DID ↔ Mode 01 PID)")
    print("=" * 78)

    if not bindings:
        print("\n  No bindings met the threshold.")
        print("  Check that pids.jsonl and frames.log have overlapping timestamps,")
        print("  and that the engine state varied enough to produce signal "
              "(idle alone gives constant values that can't be regressed).")
        return 0

    # Group by DID, show best PID per DID + any other high-r² ties.
    by_did: dict[int, list[CrossRefBinding]] = defaultdict(list)
    for b in bindings:
        by_did[b.did].append(b)

    print(f"\n  {len(by_did)} DIDs bound across {len(bindings)} (DID, PID) pairs.\n")
    for did in sorted(by_did):
        rows = by_did[did]
        # Best first; show ties (within 0.05 r²) too in case it's ambiguous.
        best_r2 = rows[0].r_squared
        for b in rows:
            if b.r_squared < best_r2 - 0.05:
                break
            scale_str = (
                f"value = raw × {b.slope:g}"
                + (f" {b.offset:+g}" if b.offset != 0 else "")
            )
            unit = f" {b.pid_unit}" if b.pid_unit else ""
            print(
                f"  0x{b.did:04X} ↔ {b.pid:<24}  r²={b.r_squared:.3f}  "
                f"n={b.n_pairs:<4}  {scale_str}{unit}"
            )
            print(
                f"          raw {b.raw_range[0]:>6}..{b.raw_range[1]:<6}  "
                f"→ decoded {b.decoded_range[0]:.2f}..{b.decoded_range[1]:.2f}"
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
        help="Minimum r² to consider a binding worth reporting.",
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
