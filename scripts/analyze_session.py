#!/usr/bin/env python3
"""CLI for the offline "did my change move behavior?" session analysis.

Diffs one stored session's telemetry against the learned behavior surfaces
and prints a per-surface deviation table — the tool this repo's operator
built after answering that question by hand with DuckDB over SSH one time
too many. See ``src/analysis/session_deviations.py`` for the analysis
itself; this script is just argument parsing + a text table.

Usage:

    .venv/bin/python scripts/analyze_session.py --session 2026-07-19T000112
    .venv/bin/python scripts/analyze_session.py --session 2026-07-19T000112 \\
        --mode idle --profile nissan-xterra-2008
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from src.analysis.session_deviations import SessionDeviationReport, analyze_session
from src.config import CanaryConfig
from src.control.sessions import resolve_session
from src.decode.behavior_models import DEFAULT_BEHAVIOR_MODELS_PATH, load_behavior_models
from src.log import configure_logging
from src.profiles import resolve_profile_for_tooling, scope_config


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="analyze_session",
        description="Diff a stored session against the learned behavior surfaces.",
    )
    parser.add_argument("--config", type=Path, default=Path("canary.yaml"))
    parser.add_argument("--session", required=True, help="Session id, e.g. 2026-07-19T000112")
    parser.add_argument(
        "--mode",
        default=None,
        help="Only show surfaces gated on this operating_mode (default: all surfaces).",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Vehicle profile slug (default: resolved from the vehicle: block, no VIN probe).",
    )
    parser.add_argument(
        "--behavior-models",
        type=Path,
        default=None,
        help="Override behavior_models.yaml path (default: the profile-scoped daemon path).",
    )
    parser.add_argument(
        "--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser.parse_args(argv)


def _fmt(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.0f}%"


def print_report(report: SessionDeviationReport, *, mode_filter: str | None) -> None:
    surfaces = [
        s for s in report.surfaces if mode_filter is None or s.operating_mode == mode_filter
    ]
    print(f"session:    {report.session_dir}")
    print(f"rows:       {report.n_rows}")
    if mode_filter is not None:
        print(f"mode filter: {mode_filter} ({len(surfaces)}/{len(report.surfaces)} surfaces)")
    print()

    header = (
        f"{'surface':<32} {'kind':<11} {'mode':<10} {'n_obs':>6} {'n_dom':>6} "
        f"{'actual':>9} {'expected':>9} {'Δ':>9} {'warn':>6} {'crit':>6}"
    )
    print(header)
    print("-" * len(header))
    for s in sorted(surfaces, key=lambda s: s.name):
        print(
            f"{s.name:<32} {s.kind:<11} {s.operating_mode or '—':<10} "
            f"{s.n_observed:>6} {s.n_in_domain:>6} "
            f"{_fmt(s.mean_actual):>9} {_fmt(s.mean_expected):>9} {_fmt(s.mean_deviation):>9} "
            f"{_fmt_pct(s.warning_fraction):>6} {_fmt_pct(s.critical_fraction):>6}"
        )

    flagged = [
        s for s in surfaces if (s.warning_fraction or 0) > 0 or (s.critical_fraction or 0) > 0
    ]
    if flagged:
        print()
        print("flagged (nonzero warning/critical fraction):")
        for s in flagged:
            warn = _fmt_pct(s.warning_fraction)
            crit = _fmt_pct(s.critical_fraction)
            print(f"  {s.name}: warning={warn} critical={crit}")
            for cell in s.cells:
                if cell["mean_deviation"] is None:
                    continue
                print(
                    f"    x_bin={cell['x_bin']} y_bin={cell['y_bin']} "
                    f"n={cell['n']} mean_deviation={_fmt(cell['mean_deviation'])}"
                )

    if report.gated_on_missing_mode:
        print()
        print("gated on a mode this config's segmenter can't emit (never evaluates):")
        for name, mode in sorted(report.gated_on_missing_mode.items()):
            print(f"  {name}: operating_mode={mode!r}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)
    profile = resolve_profile_for_tooling(config, args.profile)
    config = scope_config(config, profile)

    try:
        session_dir = resolve_session(config.collection.storage_path, args.session)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    behavior_models_path = args.behavior_models or config.daemon.behavior_models
    if not behavior_models_path.exists():
        behavior_models_path = DEFAULT_BEHAVIOR_MODELS_PATH
    if not behavior_models_path.exists():
        print(f"error: no behavior_models.yaml found ({behavior_models_path})", file=sys.stderr)
        return 1
    models = load_behavior_models(behavior_models_path)

    report = analyze_session(session_dir, config, models)
    print_report(report, mode_filter=args.mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
