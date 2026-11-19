#!/usr/bin/env python3
"""E07.5 stage 1 (manifold mass/energy balance and breathing): numerics, the
corpus breathing bound, and every plant stage's derived standing.

Offline only: reads the profile and stored sessions, prints JSON, writes nothing.

    .venv/bin/python -m tools.manifold_check --profile <slug> study [--method rk4]
    .venv/bin/python -m tools.manifold_check --profile <slug> breathing-bound \\
        [--sessions-root DIR] [--sessions NAME ...]
    .venv/bin/python -m tools.manifold_check --profile <slug> standing \\
        [--fuel-plant-version fuel-mv-2]

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
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.engine_dynamics import (
    breathing_bound,
    breathing_samples,
    convergence_study,
    load_kit,
    rollout_blockers,
    standings,
)
from src.jsonable import jsonable

try:
    from _cli import add_profile_dir_args, profile_data_or_exit  # type: ignore[import-not-found]
except ImportError:  # loaded as `tools.<name>`: repo root on path
    from tools._cli import add_profile_dir_args, profile_data_or_exit


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    add_profile_dir_args(ap, sessions_flag=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    study = sub.add_parser("study")
    study.add_argument("--method", choices=("fixed_step_euler", "rk4"), default=None)
    bound = sub.add_parser("breathing-bound")
    bound.add_argument(
        "--sessions-root",
        type=Path,
        default=None,
        help="Directory holding this vehicle's session folders (default: --config's storage).",
    )
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
    data = profile_data_or_exit(args, need_sessions=args.cmd == "breathing-bound")
    contract, manifold, fuel = load_kit(data.directory)
    out: dict[str, Any]
    if args.cmd == "study":
        out = convergence_study(contract, manifold, fuel, method=args.method).to_dict()
    elif args.cmd == "breathing-bound":
        names = args.sessions or [*fuel.split.identification, *fuel.split.held_out]
        frames = []
        missing = []
        for name in names:
            path = data.require_sessions_root() / name
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
        st = standings(contract, data.directory, versions=versions)
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
    print(json.dumps(jsonable(out), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
