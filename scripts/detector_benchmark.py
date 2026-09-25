#!/usr/bin/env python3
"""Corpus-wide detector benchmark (Phase 2B, Tasks 2.5/2.6).

Grids (held-out clean sessions x fault kinds x target channels) through the
synthetic-fault harness (`src.models.synthetic_fault_eval.evaluate_detectors`,
2.3/2.4) and rolls the per-cell reports up into one per-detector x
per-fault-class table: recall, an estimated precision, median time-to-detect,
and the clean-corpus alerts-per-hour rate. Prints the table and writes the
full report (aggregate + every per-cell detail) as JSON.

`--baseline <report.json>` (2.6) diffs this run's table against a previous
one, cell by cell, and flags any metric that regressed beyond `--tolerance`
(`diff_reports`); the pre-deploy gate `scripts/retrain.py` runs after itself.

Usage:
  scripts/detector_benchmark.py --sessions-root data/sessions
  scripts/detector_benchmark.py data/sessions/a data/sessions/b \
      --if-model models/isolation_forest/model.joblib
  scripts/detector_benchmark.py --channels obd:RPM obd:SPEED --fault-kinds spike dropout
  scripts/detector_benchmark.py --baseline data/analyses/benchmark-20260701-000000.json

Requires a trained Isolation Forest artifact (`--if-model`, default
`<models_dir>/<isolation_forest_artifact>` from config); the sequence
detector is optional and skipped gracefully when the configured backend
isn't enabled/available (mirrors `evaluate_detectors`'s own
`trained_sequence=None` handling).

Known limitation: `evaluate_detectors` (2.3/2.4) always calls
`featurize_wide_frame(resampled, config)` with no `behavior_models` — an IF
artifact trained with behavior-model deviation (`deviation:*`) feature
columns can't be schema-matched here (that's a gap in the already-completed
2.3/2.4 harness, not something this CLI re-opens). Bindings-decoded
(`decoded:*`) CAN channels work fine; only a deviation-column-trained
artifact needs a dedicated non-deviation IF artifact for this benchmark.

The grid, rollup and regression diff are ``src.models.detector_benchmark``,
and the per-session loader is ``feature_pipeline.load_resampled_session``
(R06.4): retrain's gate and ``evaluate_detectors`` used them through sibling
imports of this script. They are re-exported here under their old names.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pandas as pd

try:
    from _cli import (
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        load_config,
    )
except ImportError:  # loaded as `scripts.detector_benchmark` — repo root on path, not scripts/
    from scripts._cli import (  # type: ignore[no-redef]
        add_bindings_arg,
        add_common_args,
        add_log_level_arg,
        load_config,
    )

from src.log import configure_logging
from src.models.detector_benchmark import (
    DEFAULT_TARGET_CHANNELS,
    DEFAULT_TOLERANCE,
    LOADER_VERSION,
    GridCell,
    aggregate_cells,
    build_report,
    cell_seed,
    diff_reports,
    incomparable_reason,
    load_trained_sequence,
    run_grid,
    select_target_channels,
)
from src.models.isolation_forest import load_isolation_forest_model
from src.models.synthetic_fault_eval import DEFAULT_FAULT_KINDS
from src.preprocessing import AnomalyKind, discover_sessions
from src.preprocessing.feature_pipeline import load_resampled_session

log = logging.getLogger(__name__)


def print_report(report: dict[str, Any]) -> None:
    sessions = ", ".join(report["sessions"]) or "(none)"
    print("=" * 88)
    print(f"DETECTOR BENCHMARK  seed={report['seed']}  sessions: {sessions}")
    print("=" * 88)
    if not report["table"]:
        print("  (no grid cells — no sessions, or no requested channel usable in any of them)")
        return
    header = (
        f"  {'detector':<16} {'fault':<8} {'runs':>5} {'recall':>7} "
        f"{'precision':>10} {'med.t2d(s)':>11} {'alerts/hr':>10}"
    )
    print(header)
    for row in report["table"]:
        precision = "n/a" if row["precision"] is None else f"{row['precision']:.3f}"
        latency = (
            "n/a"
            if row["median_time_to_detect_seconds"] is None
            else f"{row['median_time_to_detect_seconds']:.1f}"
        )
        print(
            f"  {row['detector']:<16} {row['fault_kind']:<8} {row['runs']:>5} "
            f"{row['recall']:>7.3f} {precision:>10} {latency:>11} "
            f"{row['alerts_per_hour_clean']:>10.3f}"
        )
    for name, reason in report.get("withheld", {}).items():
        print(f"  {name:<16} withheld: {reason}")


def print_regression_diff(diff: dict[str, Any]) -> None:
    print("=" * 88)
    print(f"REGRESSION COMPARE  tolerance={diff['tolerance']:.0%}")
    print("=" * 88)
    if not diff.get("comparable", True):
        print(f"  incomparable: {diff['reason']}")
        return
    if not diff["cells"]:
        print("  (no baseline cells to compare)")
        return
    any_flagged = False
    for cell in diff["cells"]:
        label = f"{cell['detector']:<16} {cell['fault_kind']:<8}"
        if cell["status"] != "compared":
            only_in = "current" if cell["status"] == "added" else "baseline"
            print(f"  {label}  ({cell['status']}: only in {only_in} run)")
            continue
        flagged = [name for name, m in cell["metrics"].items() if m["regressed"]]
        if not flagged:
            continue
        any_flagged = True
        print(f"  REGRESSED  {label}")
        for metric_name in flagged:
            m = cell["metrics"][metric_name]
            print(f"    {metric_name}: {m['baseline']:.4g} -> {m['current']:.4g}")
    if not any_flagged:
        print("  no regressions beyond tolerance")


def _resolve_sessions(args: argparse.Namespace) -> list[Path]:
    if args.sessions:
        return [Path(p) for p in args.sessions]
    return discover_sessions(args.sessions_root)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="detector_benchmark",
        description=(
            "Grid a synthetic-fault benchmark (session x fault kind x target "
            "channel) over the recorded corpus and report per-detector x "
            "per-fault-class recall / precision / time-to-detect / clean "
            "alerts-per-hour (Phase 2B, 2.5)."
        ),
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        type=Path,
        help="Session directories to benchmark. Default: all discovered under --sessions-root.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path("data/sessions"),
        help="Where to discover sessions when none are given explicitly.",
    )
    add_common_args(parser)
    add_bindings_arg(parser)
    parser.add_argument(
        "--if-model",
        type=Path,
        default=None,
        help="Isolation Forest artifact path (default: <models_dir>/<isolation_forest_artifact>).",
    )
    parser.add_argument(
        "--sequence-model",
        type=Path,
        default=None,
        help="Sequence-detector artifact path override (default: the configured "
        "models.sequence_detector backend's own artifact path).",
    )
    parser.add_argument(
        "--no-sequence",
        action="store_true",
        help="Skip sequence-detector coverage even if configured/available.",
    )
    parser.add_argument(
        "--fault-kinds",
        nargs="+",
        choices=sorted(get_args(AnomalyKind)),
        default=list(DEFAULT_FAULT_KINDS),
        metavar="KIND",
    )
    parser.add_argument(
        "--channels",
        nargs="+",
        default=list(DEFAULT_TARGET_CHANNELS),
        metavar="CHANNEL",
        help="Target channels to inject faults into (skipped per-session if absent/all-NaN).",
    )
    parser.add_argument("--n-injections", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="A previous run's JSON report (Task 2.6). Diffs this run's per-detector "
        "x per-fault-class table against it and flags any metric that regressed "
        "beyond --tolerance; nonzero exit if any cell is flagged.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        # argparse runs `help % params` for every action (for %(default)s
        # etc.), so a literal "%" here must be doubled or it's parsed as a
        # (broken) format spec.
        help=f"Relative regression tolerance for --baseline compare (default "
        f"{DEFAULT_TOLERANCE * 100:.0f}%%; see DEFAULT_TOLERANCE for the rationale).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="JSON output path (default: data/analyses/benchmark-<timestamp>.json).",
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        help="Filesystem-safe timestamp for the default --out name and the report's "
        "generated_at field (default: now, UTC, YYYYMMDD-HHMMSS).",
    )
    add_log_level_arg(parser)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = load_config(args)

    session_dirs = _resolve_sessions(args)
    if not session_dirs:
        print(f"no sessions found under {args.sessions_root} (or none given); nothing to do")
        # Still an empty-but-valid report, per the "no corpus yet" case.
        session_dirs = []

    if_model_path = (
        args.if_model
        if args.if_model is not None
        else config.daemon.models_dir / config.daemon.isolation_forest_artifact
    )
    try:
        trained_if = load_isolation_forest_model(if_model_path)
    except FileNotFoundError:
        print(f"error: no Isolation Forest artifact at {if_model_path}", file=sys.stderr)
        return 1

    trained_sequence = load_trained_sequence(config, args.sequence_model, skip=args.no_sequence)

    sessions: list[tuple[str, pd.DataFrame]] = []
    for session_dir in session_dirs:
        try:
            resampled = load_resampled_session(session_dir, config, bindings_path=args.bindings)
        except FileNotFoundError as e:
            log.warning("skipping unreadable session %s: %s", session_dir, e)
            continue
        sessions.append((session_dir.name, resampled))

    cells = run_grid(
        sessions,
        config,
        trained_if,
        fault_kinds=args.fault_kinds,
        channels=args.channels,
        n_injections=args.n_injections,
        seed=args.seed,
        trained_sequence=trained_sequence,
    )

    timestamp = args.timestamp or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    report = build_report(
        cells,
        seed=args.seed,
        n_injections=args.n_injections,
        generated_at=timestamp,
    )
    print_report(report)

    regression_diff: dict[str, Any] | None = None
    if args.baseline is not None:
        try:
            baseline_report = json.loads(args.baseline.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: cannot read baseline report {args.baseline}: {e}", file=sys.stderr)
            return 2
        regression_diff = diff_reports(baseline_report, report, tolerance=args.tolerance)
        report["regressions"] = regression_diff
        print_regression_diff(regression_diff)

    out_path = args.out or Path("data/analyses") / f"benchmark-{timestamp}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"wrote {out_path}")

    if regression_diff is not None and regression_diff["regressed"]:
        print("error: regression(s) detected vs baseline", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_TARGET_CHANNELS",
    "DEFAULT_TOLERANCE",
    "LOADER_VERSION",
    "GridCell",
    "aggregate_cells",
    "build_report",
    "cell_seed",
    "diff_reports",
    "incomparable_reason",
    "load_resampled_session",
    "main",
    "parse_args",
    "print_regression_diff",
    "print_report",
    "run_grid",
    "select_target_channels",
]
