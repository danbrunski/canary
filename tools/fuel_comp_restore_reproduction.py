#!/usr/bin/env python3
"""B08.6 desk part: the October 4 Fuel Comp restore's written multiplier
changes, reproduced from both images at a pre-flash drive's input points.

The case (``plant_interventions.yaml``) supplies the written bands and both
image hashes; the flash time is the registry's (``resolve_boundary``). The
images are read from ``--rom-dir`` (or ``CANARY_ROM_DIRS``) or ``--image``
and refused unless their plain sha256 is the case's. Each session is read
under ``--sessions-root`` by name, or as a path when one exists (a committed
fixture slice). Points:

- ``observed``: every read of the case's load input with the rpm words of the
  PID 0x0C reads bracketing it, each within ``fuel_comp_fit.yaml``
  ``known_answer.max_gap_s``;
- ``reconstructed`` (``--reconstructed``): N02's per-sample byte interval
  (:func:`src.analysis.fuel_comp_samples.session_samples`), on sessions that
  never read the byte.

Writes only ``--out`` (JSON). Analysis only: nothing is fitted, flashed or
exported.

Usage::

    .venv/bin/python -m tools.fuel_comp_restore_reproduction \\
        --session tests/fixtures/fuel_comp/_capture-ram-study-2026-10-04T204557 \\
        --rom-dir "/mnt/c/Users/thebo/Desktop/Uprev/Tuned ROMs" \\
        --out <scratch>/fuel_comp_restore.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from src.analysis.channels import read_channels
from src.analysis.fuel_comp_inputs import load_fuel_comp_declaration
from src.analysis.fuel_comp_restore import (
    InputPoint,
    RestoreReproductionRefusalError,
    observed_points,
    points_from_samples,
    reproduce,
)
from src.analysis.fuel_comp_rom import (
    ROM_DIRS_ENV,
    FuelCompRomRefusalError,
    RevisionImageResolver,
    rom_dirs_from_env,
)
from src.analysis.fuel_comp_samples import session_samples
from src.analysis.fuel_plant import FuelPlantRefusalError
from src.analysis.lambda_delivery import load_lambda_delivery
from src.analysis.plant_validation import load_interventions, resolve_boundary
from src.analysis.rom_map import load_rom_map
from src.signals.afr import load_afr_calibration
from src.vehicle.tune_revisions import load_tune_revisions

REPO = Path(__file__).resolve().parents[1]


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", type=Path, default=REPO / "data" / "nissan-xterra-2008")
    parser.add_argument("--case", default="fuel-comp-restore-2026-10-04")
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008",
    )
    parser.add_argument("--session", action="append", required=True, help="name or path")
    parser.add_argument(
        "--rom-dir",
        type=Path,
        action="append",
        default=None,
        help=f"directory holding the registry's rom_file names (else ${ROM_DIRS_ENV})",
    )
    parser.add_argument("--image", type=Path, action="append", default=[])
    parser.add_argument(
        "--reconstructed",
        action="store_true",
        help="also place N02's reconstructed byte intervals (needs the images' curves)",
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def _session_path(name: str, root: Path) -> Path:
    as_path = Path(name)
    return as_path if as_path.is_dir() else root / name


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    profile = args.profile
    declaration = load_fuel_comp_declaration(profile)
    registry = load_tune_revisions(profile / "tune_revisions.yaml")
    if registry is None:
        print("this vehicle has no tune_revisions.yaml", file=sys.stderr)
        return 2
    case = load_interventions(profile).case(args.case)
    resolver = RevisionImageResolver(
        registry,
        load_rom_map(profile),
        declaration.rom,
        rom_dirs=args.rom_dir if args.rom_dir else rom_dirs_from_env(),
        image_files=args.image,
        slot=declaration.program.map_slot,
    )
    try:
        boundary = resolve_boundary(case, registry)
        old = resolver.resolve(case.base_revision)
        new = resolver.resolve(case.candidate_revision)
    except (FuelPlantRefusalError, FuelCompRomRefusalError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    max_gap_s = declaration.known_answer.max_gap_s
    points: list[InputPoint] = []
    sessions: dict[str, str] = {}
    for name in args.session:
        session = _session_path(name, args.sessions_root)
        long = read_channels(
            session, [case.load_input, declaration.inputs.rpm.channel], acquired_time=True
        )
        try:
            points += observed_points(
                long,
                session=session.name,
                byte_spec=case.load_input,
                declaration=declaration,
                max_gap_s=max_gap_s,
            )
            sessions[session.name] = "observed"
        except RestoreReproductionRefusalError as exc:
            sessions[session.name] = exc.reason
        if args.reconstructed:
            samples = session_samples(
                session,
                declaration=declaration,
                delivery=load_lambda_delivery(profile / "lambda_delivery.yaml"),
                afr_calibration=load_afr_calibration(profile / "afr_calibration.yaml"),
                registry=registry,
                resolver=resolver,
            )
            points += points_from_samples(samples.frame)
    try:
        result = reproduce(
            case, boundary, old, new, points, rpm_per_count=declaration.inputs.rpm.rpm_per_count
        )
    except RestoreReproductionRefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    payload = {
        **result.as_dict(),
        "sessions": sessions,
        "bracket_max_gap_s": max_gap_s,
        "bracket_max_gap_source": "fuel_comp_fit.yaml known_answer.max_gap_s",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str))
    for band in payload["bands"]:
        print(
            f"band {band['index']} {band['as_written']}: n={band['n_points']} "
            f"change {band['change_pct_range']} -> {band['verdict']}"
        )
    print(f"unplaced {payload['unplaced']}; no claim {payload['no_claim']['by_reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
