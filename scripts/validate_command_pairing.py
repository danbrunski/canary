#!/usr/bin/env python3
"""Acquisition-time validation of a command/measured (or redundant) pair (B05.5, B05.7).

Before a tracking surface is fitted, three questions have to be answered with
*same-instant* observations, never aggregates:

1. **Does the pairing work at all on this session?** A known-answer control
   pair (``--known-answer A,B``) whose right answer is already established —
   e.g. ``obd:RPM`` against ``did:ECM_RPM`` — is compared first. If the
   control does not land where it should, nothing else here is evidence.
2. **What is the relationship between the two channels?** Slope, offset,
   scale stability, the 1:1 residual, and the per-sample ratio, on the whole
   session and again on the *steady* subset only (the command held constant
   for ``--hold-seconds`` and RPM inside ``--max-rpm-range``), because a
   command/response lag turns into apparent disagreement during transitions.
3. **Can the relationship fail?** Negative controls (``--negative-control
   A,B``) compare a channel against one it should *not* track, so a passing
   result is evidence rather than a property of the method.

Everything goes through :func:`src.analysis.compare.compare_channels` with an
explicit :class:`PairingPolicy`; the steady subset is a row filter on the raw
long frame, so the comparison still does its own pairing on real samples.

Usage:

    .venv/bin/python scripts/validate_command_pairing.py \\
        --command did:ECM_INTAKE_CAM_ANGLE_COMMANDED \\
        --measured did:ECM_INTAKE_CAM_ANGLE_MEASURED \\
        --measured did:ECM_INTAKE_CAM_ANGLE_MEASURED_B \\
        --known-answer obd:RPM,did:ECM_RPM \\
        --rpm-spec did:ECM_RPM --tolerance 50ms \\
        --out /tmp/cam.json ~/canary-data/sessions-0917/_capture-*
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from _cli import (  # noqa: F401  (keeps the repo-root bootstrap)
    add_bindings_arg,
    add_log_level_arg,
    load_config,
)

from src.analysis.channels import read_channels
from src.analysis.compare import (
    ComparisonRefusedError,
    PairingPolicy,
    align_pairs,
    compare_channels,
)
from src.analysis.pairing import to_seconds
from src.log import configure_logging
from src.paths import DIDS_YAML
from src.preprocessing.loader import has_telemetry, read_session

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="validate_command_pairing", description=__doc__)
    parser.add_argument("sessions", nargs="+", type=Path)
    parser.add_argument("--command", required=True, help="Commanded/reference channel spec.")
    parser.add_argument(
        "--measured", action="append", required=True, help="Measured channel spec (repeatable)."
    )
    parser.add_argument(
        "--known-answer",
        action="append",
        default=[],
        help="A,B pair whose answer is already established (repeatable).",
    )
    parser.add_argument(
        "--negative-control",
        action="append",
        default=[],
        help="A,B pair that must NOT track (repeatable).",
    )
    parser.add_argument("--rpm-spec", default="did:ECM_RPM", help="RPM channel for steadiness.")
    parser.add_argument("--tolerance", default="50ms", help="Pairing tolerance.")
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=5.0,
        help="Command must be unchanged for this long (before and after) to count as steady.",
    )
    parser.add_argument("--max-rpm-range", type=float, default=200.0)
    add_bindings_arg(parser)
    parser.add_argument("--dids", type=Path, default=DIDS_YAML)
    parser.add_argument("--out", type=Path, required=True)
    add_log_level_arg(parser, default="WARNING")
    return parser.parse_args(argv)


def steady_intervals(
    frame: pd.DataFrame,
    *,
    command: str,
    rpm: str,
    hold_seconds: float,
    max_rpm_range: float,
    bindings: Path,
    dids: Path,
) -> list[tuple[float, float]]:
    """``[(start, end)]`` epoch-second spans where the command is held and RPM steady.

    A span is a maximal run of identical command values lasting at least
    ``hold_seconds``, trimmed by half a hold at each end so the settling
    transient is outside it, and kept only when every RPM sample inside it
    lies within ``max_rpm_range``. Response lag is a separate question from
    steady tracking — this is how the two are kept apart.
    """
    long = read_channels(frame, [command, rpm], bindings_path=bindings, dids_path=dids)
    cmd = long[long["spec"] == command].sort_values("timestamp")
    if cmd.empty:
        return []
    times = pd.to_datetime(cmd["timestamp"], utc=True).astype("datetime64[ns, UTC]")
    seconds = to_seconds(times)
    values = pd.to_numeric(cmd["value"], errors="coerce").to_numpy(dtype=float)
    changed = np.flatnonzero(np.diff(values) != 0.0) + 1
    starts = np.concatenate([[0], changed])
    ends = np.concatenate([changed, [len(values)]])
    rpm_rows = long[long["spec"] == rpm]
    rpm_time = (
        pd.to_datetime(rpm_rows["timestamp"], utc=True)
        .astype("datetime64[ns, UTC]")
        .astype("int64")
        .to_numpy(dtype=float)
        / 1e9
    )
    rpm_value = pd.to_numeric(rpm_rows["value"], errors="coerce").to_numpy(dtype=float)
    out: list[tuple[float, float]] = []
    margin = hold_seconds / 2.0
    for lo, hi in zip(starts, ends, strict=True):
        span = (seconds[lo], seconds[hi - 1])
        if span[1] - span[0] < hold_seconds:
            continue
        window = (span[0] + margin, span[1] - margin)
        if window[1] <= window[0]:
            continue
        inside = (rpm_time >= window[0]) & (rpm_time <= window[1]) & np.isfinite(rpm_value)
        if not inside.any():
            continue
        if rpm_value[inside].max() - rpm_value[inside].min() > max_rpm_range:
            continue
        out.append(window)
    return out


def _filter_to(frame: pd.DataFrame, spans: Sequence[tuple[float, float]]) -> pd.DataFrame:
    stamps = pd.to_datetime(frame["timestamp"], utc=True).astype("datetime64[ns, UTC]")
    seconds = to_seconds(stamps)
    keep = np.zeros(len(frame), dtype=bool)
    for lo, hi in spans:
        keep |= (seconds >= lo) & (seconds <= hi)
    return frame[keep]


def ratio_stats(
    frame: pd.DataFrame, a: str, b: str, *, policy: PairingPolicy, bindings: Path, dids: Path
) -> dict[str, Any]:
    """Per-sample ``b / a`` over the accepted pairs — read alongside the fit.

    The verdict from slope drift misleads in both directions (a fitted offset
    absorbs drift; a genuine mirror can read ``drifting``), so the ratio is
    reported whenever the denominator stays clear of zero, and explicitly
    refused when it does not.
    """
    pairs = align_pairs(frame, a, b, policy=policy, bindings_path=bindings, dids_path=dids).pairs
    if pairs.empty:
        return {"n_pairs": 0, "reason": "no pairs"}
    x = pairs["a"].to_numpy(dtype=float)
    y = pairs["b"].to_numpy(dtype=float)
    usable = np.isfinite(x) & np.isfinite(y) & (np.abs(x) > 1.0)
    if usable.sum() < 20:
        return {
            "n_pairs": int(len(pairs)),
            "usable": int(usable.sum()),
            "reason": "denominator too close to zero for a ratio",
        }
    ratio = y[usable] / x[usable]
    return {
        "n_pairs": int(len(pairs)),
        "usable": int(usable.sum()),
        "median": float(np.median(ratio)),
        "p05": float(np.quantile(ratio, 0.05)),
        "p95": float(np.quantile(ratio, 0.95)),
        "spread_pct": float((np.quantile(ratio, 0.95) - np.quantile(ratio, 0.05)) * 50.0),
    }


def _compare(
    frame: pd.DataFrame, a: str, b: str, *, policy: PairingPolicy, args: argparse.Namespace
) -> dict[str, Any]:
    try:
        result = compare_channels(
            frame, a, b, policy=policy, bindings_path=args.bindings, dids_path=args.dids
        )
    except ComparisonRefusedError as exc:
        return {"a": a, "b": b, "refused": str(exc)}
    out = result.as_dict()
    out["describe"] = result.describe()
    out["ratio"] = ratio_stats(frame, a, b, policy=policy, bindings=args.bindings, dids=args.dids)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    policy = PairingPolicy.within(args.tolerance)
    pairs = [tuple(p.split(",", 1)) for p in args.known_answer]
    negatives = [tuple(p.split(",", 1)) for p in args.negative_control]
    sessions: dict[str, Path] = {}
    for path in args.sessions:
        path = path.expanduser()
        if path.is_dir() and has_telemetry(path):
            sessions.setdefault(path.name, path)

    report: dict[str, Any] = {
        "report": "command-pairing-validation",
        "command": args.command,
        "measured": args.measured,
        "policy": policy.as_dict(),
        "steady_rule": {
            "hold_seconds": args.hold_seconds,
            "max_rpm_range": args.max_rpm_range,
            "rpm_spec": args.rpm_spec,
            "note": "command unchanged for hold_seconds, trimmed by half a hold each end",
        },
        "sessions": {},
    }
    for name, path in sessions.items():
        frame = read_session(path)
        entry: dict[str, Any] = {"path": str(path)}
        entry["known_answer"] = [_compare(frame, a, b, policy=policy, args=args) for a, b in pairs]
        entry["negative_controls"] = [
            _compare(frame, a, b, policy=policy, args=args) for a, b in negatives
        ]
        entry["all_rows"] = [
            _compare(frame, args.command, measured, policy=policy, args=args)
            for measured in args.measured
        ]
        if len(args.measured) > 1:
            entry["measured_vs_measured"] = _compare(
                frame, args.measured[0], args.measured[1], policy=policy, args=args
            )
        spans = steady_intervals(
            frame,
            command=args.command,
            rpm=args.rpm_spec,
            hold_seconds=args.hold_seconds,
            max_rpm_range=args.max_rpm_range,
            bindings=args.bindings,
            dids=args.dids,
        )
        entry["steady_spans"] = len(spans)
        entry["steady_seconds"] = float(sum(hi - lo for lo, hi in spans))
        if spans:
            steady = _filter_to(frame, spans)
            entry["steady_rows"] = [
                _compare(steady, args.command, measured, policy=policy, args=args)
                for measured in args.measured
            ]
        report["sessions"][name] = entry
        print(f"  {name}: {len(spans)} steady span(s)", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    for name, entry in report["sessions"].items():
        print(f"== {name}")
        for key in ("known_answer", "negative_controls", "all_rows", "steady_rows"):
            for item in entry.get(key) or []:
                label = item.get("describe") or f"{item['a']} vs {item['b']}: {item['refused']}"
                print(f"  [{key}] {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
