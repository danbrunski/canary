#!/usr/bin/env python3
"""Backfill session fingerprints + freeze the reference epoch (Task 4.2/4.3,
eval-longitudinal-and-dtc.md).

Fingerprints are normally computed automatically wherever classification
already runs (``scripts/label_sessions.py``, the daemon's on-demand
``classify_session`` control route) — see ``src/analysis/fingerprint.py``.
This CLI covers the two things that hook can't:

* ``backfill`` — compute ``fingerprint.json`` for existing sessions that
  were classified before the fingerprint hook landed (or whose
  ``fingerprint_version`` is stale). Mirrors ``label_sessions.py``'s
  ``--rebuild`` flag.
* ``freeze`` — build ``data/control/reference_epoch.json`` from the
  fingerprints of the currently "ready" sessions. **Never automatic** — run
  this by hand when a ledger ``corpus_boundary`` event says what "normal"
  looks like has changed (a mod, a repair, a big retrain).
* ``trends`` — Task 4.4/4.5's read side as a table: per-stat latest value,
  reference band, Mann-Kendall trend verdict, CUSUM alarm state, evaluated
  over the full fingerprint history against the frozen reference epoch.
  Same data as the daemon's ``GET /control/trends`` / MCP ``get_trends``,
  computed straight from disk (no running daemon required).

    .venv/bin/python scripts/fingerprint.py backfill
    .venv/bin/python scripts/fingerprint.py backfill --all --rebuild
    .venv/bin/python scripts/fingerprint.py freeze --notes "post cold-air-intake baseline"
    .venv/bin/python scripts/fingerprint.py trends
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

try:
    from _cli import (
        add_common_args,
        add_log_level_arg,
        discover_live_session_dirs,
        load_config,
    )
except ImportError:  # loaded as `scripts.fingerprint` — repo root on path, not scripts/
    from scripts._cli import (  # type: ignore[no-redef]
        add_common_args,
        add_log_level_arg,
        discover_live_session_dirs,
        load_config,
    )

from src.analysis.fingerprint import compute_fingerprint, is_stale, write_fingerprint
from src.analysis.reference_epoch import (  # noqa: E402
    build_reference_epoch,
    write_reference_epoch,
)
from src.config import CanaryConfig  # noqa: E402
from src.control.trends import corpus_trend_summary  # noqa: E402
from src.log import configure_logging  # noqa: E402
from src.preprocessing.derived_store import DEFAULT_DERIVED_DIR, use_store  # noqa: E402
from src.profiles import resolve_profile_for_tooling, scope_config  # noqa: E402
from src.storage.session_products import (
    read_classification,  # noqa: E402
    read_fingerprint,
)

log = logging.getLogger(__name__)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fingerprint",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
        help="Storage root to scan. Defaults to (profile-scoped) collection.storage_path.",
    )
    parser.add_argument(
        "--derived-dir",
        type=Path,
        default=DEFAULT_DERIVED_DIR,
        help="Derived session frames, read back or built here (P01.2; default "
        "data/cache/derived_sessions; never used on the Pi).",
    )
    parser.add_argument(
        "--no-derived-store",
        dest="derived_dir",
        action="store_const",
        const=None,
        help="Read every session from raw parquet and keep nothing.",
    )
    add_log_level_arg(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    p_backfill = sub.add_parser(
        "backfill", help="Compute fingerprint.json for sessions missing one."
    )
    p_backfill.add_argument(
        "--session", action="append", default=[], help="One session id. Repeatable."
    )
    p_backfill.add_argument(
        "--all", action="store_true", help="Scan every session under --sessions-root."
    )
    p_backfill.add_argument(
        "--rebuild", action="store_true", help="Force recompute even when already current."
    )
    p_backfill.add_argument("--dry-run", action="store_true")

    p_freeze = sub.add_parser(
        "freeze", help="Build reference_epoch.json from currently-ready sessions."
    )
    p_freeze.add_argument("--notes", default="", help="Free-text note recorded in the epoch.")
    p_freeze.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Override output path (default: daemon.reference_epoch).",
    )

    sub.add_parser(
        "trends",
        help="Print a per-stat trend table (latest value, band, MK verdict, CUSUM state).",
    )

    return parser.parse_args(argv)


def _scoped_config(args: argparse.Namespace) -> CanaryConfig:
    config = load_config(args)
    if not config.profiles.enabled:
        return config
    return scope_config(config, resolve_profile_for_tooling(config, args.profile))


def _run_backfill(args: argparse.Namespace, config: CanaryConfig, sessions_root: Path) -> int:
    if args.session:
        targets = [sessions_root / name for name in args.session]
        missing = [p.name for p in targets if not p.is_dir()]
        if missing:
            log.error("session(s) not found under %s: %s", sessions_root, ", ".join(missing))
            return 1
    else:
        if not args.all:
            log.info(
                "no --session or --all given; backfilling only sessions missing/stale "
                "fingerprints (use --all to force scan)"
            )
        targets = discover_live_session_dirs(sessions_root)

    if not targets:
        log.warning("no sessions found at %s", sessions_root)
        return 0

    computed = 0
    skipped = 0
    errors = 0
    for session_dir in targets:
        existing = read_fingerprint(session_dir)
        if not args.rebuild and not is_stale(existing):
            skipped += 1
            continue
        try:
            stats = compute_fingerprint(session_dir, config)
        except FileNotFoundError as exc:
            log.warning("skip %s: %s", session_dir.name, exc)
            errors += 1
            continue
        except Exception:  # noqa: BLE001 — one bad session must not kill the batch
            log.exception("fingerprint failed for %s", session_dir.name)
            errors += 1
            continue
        if not args.dry_run:
            write_fingerprint(session_dir, stats)
        computed += 1
        log.info("%s: %d stat(s)", session_dir.name, len(stats))

    log.info("done computed=%d skipped=%d error=%d", computed, skipped, errors)
    return 0 if errors == 0 else 2


def _run_freeze(args: argparse.Namespace, config: CanaryConfig, sessions_root: Path) -> int:
    fingerprints: dict[str, dict[str, float]] = {}
    skipped_not_ready = 0
    skipped_no_fingerprint = 0
    for session_dir in discover_live_session_dirs(sessions_root):
        classification = read_classification(session_dir)
        if classification is None or classification.disposition != "ready":
            skipped_not_ready += 1
            continue
        fingerprint = read_fingerprint(session_dir)
        if fingerprint is None:
            skipped_no_fingerprint += 1
            continue
        fingerprints[session_dir.name] = fingerprint.stats

    if not fingerprints:
        log.error(
            "no ready sessions with a fingerprint under %s "
            "(not_ready=%d, missing_fingerprint=%d) — run backfill first",
            sessions_root,
            skipped_not_ready,
            skipped_no_fingerprint,
        )
        return 1

    epoch = build_reference_epoch(fingerprints, notes=args.notes)
    out_path = args.out or config.daemon.reference_epoch
    write_reference_epoch(out_path, epoch)
    log.info(
        "froze reference epoch: sessions=%d stats=%d -> %s",
        len(epoch.sessions),
        len(epoch.stats),
        out_path,
    )
    return 0


def _run_trends(config: CanaryConfig, sessions_root: Path) -> int:
    summary = corpus_trend_summary(config, sessions_root=sessions_root)
    if not summary["available"]:
        log.error(summary["reason"])
        return 1
    stats = summary["stats"]
    if not stats:
        log.warning("reference epoch frozen but no stat has any fingerprint observations yet")
        return 0
    log.info(
        "frozen_at=%s reference_sessions=%d stats=%d",
        summary["frozen_at"],
        summary["reference_sessions"],
        len(stats),
    )
    header = f"{'stat':<40} {'n':>4} {'latest':>12} {'ref_band':>24} {'mk_trend':<12} {'cusum':<8}"
    log.info(header)
    for name, trend in stats.items():
        band = f"[{trend['reference_band'][0]:.3g}, {trend['reference_band'][1]:.3g}]"
        mk = trend["mann_kendall"]
        mk_trend = mk["trend"] if mk else "n/a"
        cusum = trend["cusum"]
        cusum_state = "ALARM" if cusum and cusum["alarm"] else "ok" if cusum else "n/a"
        log.info(
            "%-40s %4d %12.4g %24s %-12s %-8s",
            name,
            trend["n"],
            trend["latest_value"],
            band,
            mk_trend,
            cusum_state,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    # compute_fingerprint reads each session through resample_and_segment,
    # which uses the derived store while one is selected (off on the Pi).
    with use_store(args.derived_dir):
        return _main(args)


def _main(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)

    config = _scoped_config(args)
    sessions_root = args.sessions_root or config.collection.storage_path

    if args.command == "backfill":
        return _run_backfill(args, config, sessions_root)
    if args.command == "freeze":
        return _run_freeze(args, config, sessions_root)
    if args.command == "trends":
        return _run_trends(config, sessions_root)
    return 1


if __name__ == "__main__":
    sys.exit(main())
