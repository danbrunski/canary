#!/usr/bin/env python3
"""E07.5 stage 1 (manifold mass/energy balance and breathing): numerics, the
corpus breathing bound, and every plant stage's derived standing.

Offline only: reads the profile and stored sessions, prints JSON, writes nothing.

    .venv/bin/python -m tools.manifold_check study [--method fixed_step_euler|rk4]
    .venv/bin/python -m tools.manifold_check breathing-bound [--sessions NAME ...]
    .venv/bin/python -m tools.manifold_check standing [--fuel-plant-version fuel-mv-2]

``study`` runs the contract's step-size refinement ladder at every
applicability corner (``--method`` shows another method against the same
declared bounds). ``breathing-bound`` computes the per-sample charge fraction
(MAF over the ambient fill) on stored recordings -- by default every recording
in fuel_plant.yaml's split -- and counts where it contradicts the shared VE
working assumption. ``standing`` derives each subsystem's standing and the
coupled-rollout blockers.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.engine_dynamics import (
    breathing_bound,
    breathing_samples,
    convergence_study,
    load_kit,
    rollout_blockers,
    standings,
)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO / "data" / "nissan-xterra-2008"
DEFAULT_SESSIONS = Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008"


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return None if math.isnan(f) else f
    if isinstance(value, np.integer):
        return int(value)
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    study = sub.add_parser("study")
    study.add_argument("--method", choices=("fixed_step_euler", "rk4"), default=None)
    bound = sub.add_parser("breathing-bound")
    bound.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS)
    bound.add_argument("--sessions", nargs="*", default=None)
    standing = sub.add_parser("standing")
    standing.add_argument(
        "--fuel-plant-version",
        default=None,
        help="ask fuel_delivery's standing for another declared version (e.g. fuel-mv-2)",
    )
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    contract, manifold, fuel = load_kit(args.profile)
    out: dict[str, Any]
    if args.cmd == "study":
        out = convergence_study(contract, manifold, fuel, method=args.method).to_dict()
    elif args.cmd == "breathing-bound":
        names = args.sessions or [*fuel.split.identification, *fuel.split.held_out]
        frames = []
        missing = []
        for name in names:
            path = args.sessions_root / name
            if not path.is_dir():
                missing.append(name)
                continue
            frames.append(breathing_samples(path, fuel))
        if not frames:
            print(json.dumps({"error": "no_sessions", "missing": missing}))
            return 1
        out = breathing_bound(pd.concat(frames, ignore_index=True), fuel)
        out["missing_sessions"] = missing
    else:
        versions = {"fuel_delivery": args.fuel_plant_version} if args.fuel_plant_version else None
        st = standings(contract, args.profile, versions=versions)
        out = {
            "standings": {
                k: {
                    "built": v.built,
                    "numerically_verified": v.numerically_verified,
                    "validated": v.validated,
                    "reasons": list(v.reasons),
                }
                for k, v in st.items()
            },
            "rollout_blockers": rollout_blockers(contract, st),
        }
    print(json.dumps(_jsonable(out), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
