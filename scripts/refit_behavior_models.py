#!/usr/bin/env python3
"""Refit the behavior-model surfaces from recorded sessions (Task 4.E.8).

Manual trigger in v1; cron later (the daemon doesn't refit live yet).
Discovers recent Parquet sessions, runs the same preprocessing pipeline the
IF / LSTM trainers use (decode bindings.yaml → wide → resample → segment →
deviation columns from any previously-published behavior models), and runs
every recipe in ``default_recipes()`` (or a user-supplied subset). Skipped
recipes (missing columns, too few samples) are logged but don't fail the
run; surfaces that *did* refit get their ``version`` bumped one above the
previous ``behavior_models.yaml`` entry of the same name.

Usage:

    .venv/bin/python scripts/refit_behavior_models.py
    .venv/bin/python scripts/refit_behavior_models.py --days 14
    .venv/bin/python scripts/refit_behavior_models.py --dry-run
    .venv/bin/python scripts/refit_behavior_models.py \\
        --recipe coolant_warmup --recipe idle_rpm_vs_coolant

Output: ``data/behavior_models.yaml`` (or wherever ``--out`` points).
Atomic write — temp file + rename so a crash mid-write doesn't truncate
the previous version.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.analysis.refit import Recipe, default_recipes, refit_all
from src.analysis.session_classifier import read_classification
from src.config import CanaryConfig
from src.decode.behavior_models import (
    dump_behavior_models,
    load_behavior_models,
    unknown_operating_modes,
)
from src.log import configure_logging
from src.preprocessing import discover_sessions
from src.preprocessing.feature_pipeline import build_feature_frame
from src.profiles import resolve_profile_for_tooling, scope_config

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="refit_behavior_models",
        description=(
            "Refit every behavior-model recipe from recently-captured sessions. "
            "Existing models in --out have their `version` bumped on successful refit; "
            "skipped recipes inherit the prior model unchanged."
        ),
    )
    parser.add_argument(
        "sessions",
        nargs="*",
        type=Path,
        help="Session directories. Default: all under --sessions-root within --days.",
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=None,
        help=(
            "Where to discover sessions when none are given explicitly. "
            "Default: the (profile-scoped) collection.storage_path from --config."
        ),
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help=(
            "Vehicle profile slug to scope sessions/output to (see profiles.yaml). "
            "Default: the vehicle: block's profile. No VIN probe is performed."
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help=(
            "Only refit using sessions captured in the last N days. Default 30. "
            "Use --all to override (or 0)."
        ),
    )
    parser.add_argument(
        "--all",
        dest="use_all_sessions",
        action="store_true",
        help="Override --days and use every session under --sessions-root.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("canary.yaml"),
        help="Path to canary.yaml (preprocessing config).",
    )
    parser.add_argument(
        "--bindings",
        type=Path,
        default=Path("data/bindings.yaml"),
        help="Path to bindings.yaml for CAN-signal decode (default data/bindings.yaml).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output YAML. Default: the (profile-scoped) daemon.behavior_models from --config.",
    )
    parser.add_argument(
        "--recipe",
        action="append",
        default=None,
        help=(
            "Recipe name(s) to refit; repeatable. Default: all recipes from "
            "default_recipes(). Useful for iterating on one surface."
        ),
    )
    parser.add_argument(
        "--regrid",
        action="append",
        default=None,
        help=(
            "2D recipe name(s) to explicitly re-grid this run (recompute auto bin "
            "edges + bump grid_version) instead of reusing the prior model's stored "
            "edges (4.3). Repeatable. Default: none — every 2D recipe reuses its "
            "prior edges when a prior model exists."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would refit (recipe → fitted / skipped) but don't write --out.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def _filter_sessions_by_age(sessions: list[Path], days: int, now: datetime) -> list[Path]:
    """Keep session dirs whose timestamped name falls within ``days`` of ``now``.

    The convention is ``YYYY-MM-DDTHHMMSS`` (UTC, matches ``make_session_dir``).
    Non-conforming names are kept — better to over-include than to silently
    drop something the user explicitly placed under sessions/.
    """
    if days <= 0:
        return sessions
    cutoff = now - timedelta(days=days)
    kept: list[Path] = []
    for session in sessions:
        try:
            started = datetime.strptime(session.name, "%Y-%m-%dT%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            kept.append(session)  # unknown format → keep
            continue
        if started >= cutoff:
            kept.append(session)
    return kept


def _select_recipes(catalog: list[Recipe], names: list[str] | None) -> list[Recipe]:
    if names is None:
        return catalog
    by_name = {r.name: r for r in catalog}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise SystemExit(f"unknown recipe(s): {unknown}; available: {sorted(by_name)}")
    return [by_name[n] for n in names]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = CanaryConfig.from_yaml(args.config)

    # Scope sessions/output to a vehicle profile so a refit reads and writes the
    # right vehicle's tree. Offline tool: no VIN probe — --profile or default.
    scoped = (
        scope_config(config, resolve_profile_for_tooling(config, args.profile))
        if config.profiles.enabled
        else config
    )
    sessions_root = args.sessions_root or scoped.collection.storage_path
    out: Path = args.out or scoped.daemon.behavior_models

    sessions: list[Path] = list(args.sessions)
    if not sessions:
        sessions = discover_sessions(sessions_root)
        if not args.use_all_sessions:
            sessions = _filter_sessions_by_age(sessions, args.days, datetime.now(UTC))
    if not sessions:
        print(
            f"error: no sessions found under {sessions_root} within --days {args.days}. "
            "Pass session paths directly or use --all.",
            file=sys.stderr,
        )
        return 1

    catalog = default_recipes()
    recipes = _select_recipes(catalog, args.recipe)
    log.info(
        "starting refit",
        extra={
            "sessions": len(sessions),
            "recipes": [r.name for r in recipes],
            "out": str(out),
            "dry_run": args.dry_run,
        },
    )

    existing = load_behavior_models(out) if out.exists() else None
    if existing is not None:
        log.info("loaded prior models", extra={"count": len(existing.models)})

    # The feature pipeline gives the wide, resampled, segmented frame the
    # fitters expect — same path the IF / LSTM trainers consume.
    frame = build_feature_frame(
        sessions, config, bindings_path=args.bindings, behavior_models_path=out
    )
    log.info("built feature frame", extra={"rows": len(frame), "columns": len(frame.columns)})

    # Load each session's classifier sidecar so recipes that require
    # specific events (e.g. ``coolant_warmup`` requires ``cold_start``)
    # filter the input frame down to eligible sessions. Sessions
    # without a sidecar yet are excluded from event-gated recipes; they
    # still feed un-gated ones. Run ``scripts/label_sessions.py`` to
    # close the gap before relying on event-gated training.
    session_events: dict[str, set[str]] = {}
    for session in sessions:
        classification = read_classification(session)
        if classification is None:
            continue
        session_events[session.name] = set(classification.event_counts.keys())
    log.info(
        "loaded session classifications for event-gated recipes",
        extra={"classified": len(session_events), "total_sessions": len(sessions)},
    )

    models, results = refit_all(
        recipes,
        frame,
        sessions=[s.name for s in sessions],
        existing=existing,
        session_events=session_events,
        catalog=catalog,
        regrid_recipes=set(args.regrid or ()),
    )

    _print_report(results, frame_rows=len(frame))

    # A model gated on a mode this config's segmenter never emits can't be
    # fit from this frame (and won't evaluate on a daemon running the same
    # mode set) — usually a sign the refit ran with canary.yaml where
    # canary.train.yaml was intended.
    mismatched = unknown_operating_modes(models, scoped.preprocessing.operating_modes)
    if mismatched:
        log.warning(
            "catalog models gated on operating modes missing from %s — "
            "they were carried over un-refit and won't evaluate under this "
            "mode set: %s",
            args.config,
            mismatched,
        )

    if args.dry_run:
        log.info("dry-run; not writing")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    dump_behavior_models(models, tmp)
    tmp.replace(out)
    log.info("wrote behavior models", extra={"path": str(out), "count": len(models.models)})
    return 0


def _print_report(results: Sequence, frame_rows: int) -> None:
    """Stdout summary so an interactive run shows which recipes won / lost."""
    fitted = [r for r in results if r.fitted]
    skipped = [r for r in results if r.skipped]
    print("=" * 72)
    print(f"BEHAVIOR MODEL REFIT  ({frame_rows} feature rows)")
    print("=" * 72)
    if fitted:
        print(f"\n  fitted ({len(fitted)}):")
        for res in fitted:
            assert res.model is not None
            fit = res.model.fit
            r2 = f"r²={fit.r_squared:.3f}" if fit.r_squared is not None else "r²=N/A"
            print(f"    ✓ {res.name:35s}  n={fit.sample_count:>6d}  {r2}  v{res.model.version}")
    if skipped:
        print(f"\n  skipped ({len(skipped)}):")
        for res in skipped:
            print(f"    ✗ {res.name:35s}  {res.reason}")
    print()


if __name__ == "__main__":
    sys.exit(main())
