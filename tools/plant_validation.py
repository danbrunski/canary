#!/usr/bin/env python3
"""E07.4: the plant's channel inventory and the intervention test, offline.

    .venv/bin/python -m tools.plant_validation inventory [--sessions-root DIR]
    .venv/bin/python -m tools.plant_validation case [--case fuel-comp-restore-2026-10-04]

``inventory`` lists every recording under the sessions root with each plant
need's read count and acquisition clock (median/p90 interval of the per-read
arrival times) and the revision in force. ``case`` runs the frozen plant across
the case's flash on the recordings its split declares and prints the report
(``pending`` until the plant is qualified and post-change data exist). Reads
stored sessions only; writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from src.analysis.fuel_plant import load_b06, load_fuel_plant
from src.analysis.plant_validation import (
    build_case_samples,
    evaluate_intervention,
    inventory_session,
    load_interventions,
    recorded_sessions,
    resolve_boundary,
    verify_prediction_of_record,
)
from src.jsonable import jsonable
from src.vehicle.tune_revisions import load_tune_revisions

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO / "data" / "nissan-xterra-2008"
DEFAULT_SESSIONS = Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008"
DEFAULT_CASE = "fuel-comp-restore-2026-10-04"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inventory", "case"])
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS)
    parser.add_argument("--case", default=DEFAULT_CASE)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    profile, _ = load_fuel_plant(args.profile)
    registry = load_tune_revisions(args.profile / "tune_revisions.yaml")
    if registry is None:
        print("no tune_revisions.yaml for this profile", file=sys.stderr)
        return 2
    case = load_interventions(args.profile).case(args.case)
    result: dict[str, Any]
    if args.command == "inventory":
        rows = [
            inventory_session(
                path, profile, extra={"load_input": case.load_input}, registry=registry
            )
            for path in recorded_sessions(args.sessions_root)
        ]
        boundary = resolve_boundary(case, registry)
        after = [r for r in rows if r["first_read"] and r["first_read"] >= boundary.at.isoformat()]
        result = {
            "sessions": len(rows),
            "plant_replay_ready": [r["session"] for r in rows if r["plant_replay_ready"]],
            "with_load_input": [r["session"] for r in rows if r["groups"]["load_input"]["present"]],
            "with_fuel_pressure": [
                r["session"] for r in rows if r["groups"]["fuel_pressure"]["present"]
            ],
            "after_boundary": [r["session"] for r in after],
            "per_session": rows,
        }
    else:
        delay = load_b06(args.profile, profile.delay)
        boundary = resolve_boundary(case, registry)
        names = [*case.split.pre, *case.split.post]
        frames = [build_case_samples(args.sessions_root / n, profile, delay, case) for n in names]
        result, _samples = evaluate_intervention(
            case, boundary, profile, delay, frames, sessions_root=args.sessions_root
        )
        result["prediction_of_record_check"] = verify_prediction_of_record(
            case.prediction_of_record
        )
    print(json.dumps(jsonable(result, ndigits=6), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
