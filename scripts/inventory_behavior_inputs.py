#!/usr/bin/env python3
"""Inventory viable inputs per behavior recipe across recorded sessions (B05.1).

For every catalog recipe (or ``--recipe``), every named session is run
through the evaluation preprocessing chain (``build_evaluation_frame``: the
training decode → resample → derive → segment path, bounded to the ids it
needs) one session at a time, and the recipe's own serving contract is
applied. The report separates channels never recorded, stale rows,
contract refusals (named) and eligible rows, groups sessions into physical
drives, and counts qualifying independent drives per recipe. For the trim
recipes it adds per-bank pairing, fuel-status membership, warmup-mode
closed-loop rows, and the per-sample agreement of the two fuel-system states
in the stored PID 0x03 payloads.

Nothing is fitted; no artifact is read or written.

Usage:

    .venv/bin/python scripts/inventory_behavior_inputs.py \\
        --config canary.pi.example.yaml --profile nissan-xterra-2008 \\
        --out /tmp/inventory.json ~/canary-data/sessions-0918/*
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from _cli import load_config

from src.analysis.behavior_input_inventory import (
    fuel_system_state_agreement,
    recipe_input_inventory,
    summarize,
    trim_bank_facts,
    trim_unit_checks,
    with_contract_context,
)
from src.analysis.behavior_validation import (
    build_evaluation_frame,
    group_drives,
    load_validation_document,
    piece_time_ranges,
    validation_document_path,
)
from src.analysis.refit import default_recipes
from src.analysis.tune_revisions import load_tune_revisions
from src.decode.behavior_models import CURRENT_SCHEMA_VERSION, BehaviorModels
from src.log import configure_logging
from src.preprocessing.feature_pipeline import DEFAULT_DIDS_PATH
from src.profiles import resolve_profile_for_tooling, scope_config

log = logging.getLogger(__name__)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="inventory_behavior_inputs", description=__doc__)
    parser.add_argument("sessions", nargs="+", type=Path, help="Session directories.")
    parser.add_argument("--config", type=Path, required=True, help="Preprocessing config.")
    parser.add_argument("--profile", required=True, help="Vehicle profile slug.")
    parser.add_argument("--recipe", action="append", default=None, help="Limit to recipe(s).")
    parser.add_argument("--bindings", type=Path, default=Path("data/bindings.yaml"))
    parser.add_argument("--dids", type=Path, default=DEFAULT_DIDS_PATH)
    parser.add_argument("--out", type=Path, required=True, help="Report JSON path.")
    parser.add_argument(
        "--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser.parse_args(argv)


def _unique_sessions(paths: Sequence[Path]) -> tuple[list[Path], list[str]]:
    """First path per session name (the same drive is often pulled twice)."""
    seen: dict[str, Path] = {}
    duplicates: list[str] = []
    for path in paths:
        path = path.expanduser()
        if not path.is_dir() or not any(path.glob("telemetry-*.parquet")):
            continue
        if path.name in seen:
            duplicates.append(str(path))
            continue
        seen[path.name] = path
    return list(seen.values()), duplicates


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.log_level)
    config = load_config(args)
    scoped = scope_config(config, resolve_profile_for_tooling(config, args.profile))
    recipes = default_recipes()
    if args.recipe:
        unknown = sorted(set(args.recipe) - {r.name for r in recipes})
        if unknown:
            print(f"error: unknown recipe(s) {unknown}", file=sys.stderr)
            return 2
        recipes = [r for r in recipes if r.name in set(args.recipe)]
    document = load_validation_document(validation_document_path(scoped.daemon.behavior_models))
    join_gap = document.drive_join_gap_seconds if document is not None else 600.0
    registry = load_tune_revisions(scoped.daemon.behavior_models.parent / "tune_revisions.yaml")
    sessions, duplicates = _unique_sessions(args.sessions)
    empty = BehaviorModels(schema_version=CURRENT_SCHEMA_VERSION, models=[])

    per_session: list[dict[str, Any]] = []
    ranges: dict[str, Any] = {}
    for session in sessions:
        try:
            frame = build_evaluation_frame(
                [session],
                scoped,
                empty,
                bindings_path=args.bindings,
                dids_path=args.dids,
                decode_can="mode-inputs",
            )
        except Exception as exc:  # one unreadable session must not end the inventory
            log.warning("skipping %s: %s", session, exc)
            per_session.append({"session": session.name, "error": str(exc)})
            continue
        ranges.update(piece_time_ranges(frame))
        frame = with_contract_context(frame, recipes)
        revision = registry.for_session(session.name) if registry is not None else None
        per_session.append(
            {
                "session": session.name,
                "path": str(session),
                "tune_revision": None if revision is None else revision.revision,
                "rows": len(frame),
                "recipes": {r.name: recipe_input_inventory(frame, r).as_dict() for r in recipes},
                "trim_banks": trim_bank_facts(frame),
                "trim_units": trim_unit_checks(frame),
                "fuel_system_states": fuel_system_state_agreement(session),
            }
        )
        print(f"  {session.name}: {len(frame)} rows", file=sys.stderr)

    drives = group_drives(ranges, join_gap_seconds=join_gap)
    drive_of_parent = {
        piece.split("#", 1)[0]: drive.drive_id for drive in drives for piece in drive.pieces
    }
    ok = [entry for entry in per_session if "error" not in entry]
    for entry in ok:
        entry["drive_id"] = drive_of_parent.get(entry["session"], entry["session"])
    report = {
        "report": "behavior-input-inventory",
        "config": str(args.config),
        "profile": args.profile,
        "sessions": len(sessions),
        "duplicate_paths_skipped": duplicates,
        "drives": [d.record().model_dump(mode="json") for d in drives],
        "summary": summarize(
            ok, min_rows_per_drive={r.name: r.min_session_samples for r in recipes}
        ),
        "per_session": per_session,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    for name, row in report["summary"].items():
        print(
            f"{name:36s} eligible={row['eligible']:6d} "
            f"drives={row['drives_with_eligible_rows']:3d} "
            f"qualifying={len(row['qualifying_drives']):3d}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
