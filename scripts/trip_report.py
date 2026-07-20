#!/usr/bin/env python3
"""Print the trip-summary table for one stored session (Task 1.7,
engine-analysis-and-mapping.md) — the "drive report" CLI that doesn't exist
today.

Works off the same on-demand compute-and-cache path the public HTTP endpoint
(``GET /sessions/{id}/trip-summary``) and the ``get_trip_summary`` MCP tool
use (``src/analysis/trip_summary_sidecar.py``): computes ``trip_summary.json``
from the session's stored telemetry the first time it's asked for, then reads
the cached copy. No running daemon required.

    .venv/bin/python scripts/trip_report.py 2026-07-19T120000
    .venv/bin/python scripts/trip_report.py 2026-07-19T120000 --rebuild
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from _cli import (  # noqa: E402  (bootstraps repo root onto sys.path)
    add_common_args,
    load_config,
)

from src.analysis.trip_summary_sidecar import get_or_compute_trip_summary  # noqa: E402
from src.config import CanaryConfig  # noqa: E402
from src.log import configure_logging  # noqa: E402
from src.profiles import resolve_profile_for_tooling, scope_config  # noqa: E402

log = logging.getLogger(__name__)

#: (``TripSummary`` field, display label, unit) — printed in this order.
_ROWS: tuple[tuple[str, str, str], ...] = (
    ("duration_s", "duration", "s"),
    ("distance_km", "distance", "km"),
    ("fuel_used_l", "fuel used", "L"),
    ("avg_economy_l_per_100km", "avg economy", "L/100km"),
    ("avg_economy_mpg", "avg economy", "mpg"),
    ("co2_kg", "CO2", "kg"),
    ("idle_time_s", "idle time", "s"),
    ("idle_fraction", "idle fraction", "%"),
    ("max_speed_kph", "max speed", "kph"),
    ("avg_speed_kph", "avg speed", "kph"),
    ("zero_to_60_mph_s", "0-60 mph", "s"),
    ("quarter_mile_s", "1/4 mile ET", "s"),
    ("quarter_mile_trap_kph", "1/4 mile trap", "kph"),
)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="trip_report",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("session", help="Session id (directory name under storage_path).")
    add_common_args(parser)
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Vehicle profile slug to scope paths to. Default: the vehicle: block's profile.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=None,
        help=(
            "Storage root the session lives under. Defaults to "
            "(profile-scoped) collection.storage_path."
        ),
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Recompute even when a cached trip_summary.json already exists.",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR")
    )
    return parser.parse_args(argv)


def _scoped_config(args: argparse.Namespace) -> CanaryConfig:
    config = load_config(args)
    if not config.profiles.enabled:
        return config
    return scope_config(config, resolve_profile_for_tooling(config, args.profile))


def _format_row(key: str, label: str, unit: str, value: float | None) -> str:
    if value is None:
        shown = "—"
    elif key == "idle_fraction":
        shown = f"{value * 100:.1f}%"
    else:
        shown = f"{value:.2f} {unit}".rstrip()
    return f"{label:<16} {shown}"


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    configure_logging(args.log_level)

    config = _scoped_config(args)
    sessions_root = args.sessions_root or config.collection.storage_path
    session_dir = sessions_root / args.session
    if not session_dir.is_dir():
        log.error("session not found: %s", session_dir)
        return 1

    result = get_or_compute_trip_summary(session_dir, config, force=args.rebuild)
    if result is None:
        log.error("no telemetry recorded for session %s", args.session)
        return 1

    summary = result["summary"]
    log.info("trip summary for %s (computed_at=%s)", args.session, result["computed_at"])
    for key, label, unit in _ROWS:
        log.info(_format_row(key, label, unit, summary.get(key)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
