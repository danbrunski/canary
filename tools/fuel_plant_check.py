#!/usr/bin/env python3
"""E07.3 fuel plant: the pulse-chain known answer, identification, held-out evaluation.

Offline only: reads stored sessions, prints JSON.

    .venv/bin/python -m tools.fuel_plant_check chain --rom <plain image> [--sessions ...]
    .venv/bin/python -m tools.fuel_plant_check identify [--plant-version fuel-mv-2]
    .venv/bin/python -m tools.fuel_plant_check evaluate [--plant-version ...]
    .venv/bin/python -m tools.fuel_plant_check compare --plant-version fuel-mv-1 fuel-mv-2

``--plant-version`` picks a declaration among the profile's ``fuel_plant*.yaml``
(default: ``fuel_plant.yaml``, the version the contract runs). ``identify``
runs on ``split.identification`` only and prints the numbers to freeze into
that file's ``baseline`` (and the in-sample fit); for a plant that fits
VE(rpm) it fits that first, on the same drives, and identifies the scale with
it. ``evaluate`` refuses until that baseline is frozen, runs on
``split.held_out`` only, and appends every recording it scored to
``spent_held_out`` -- the one write this tool makes. ``compare`` scores frozen
versions on identical samples, per sample, on recordings that are in-sample
or already spent (never a fresh one); it writes nothing. ``chain`` also runs a
negative control (the ECM's pressure correction removed), which must leave the
known answer.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.analysis.fuel_plant import (
    PLANT_FILE,
    FuelPlantProfile,
    load_b06,
    load_fuel_plant,
    plant_files,
)
from src.analysis.fuel_plant_eval import (
    build_samples,
    calibration_from_image,
    compare_versions,
    evaluate,
    identify,
    identify_ve,
    pulse_chain_ratios,
    summarize_ratios,
    ve_samples,
    without_pressure_correction,
)
from src.jsonable import jsonable

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO / "data" / "nissan-xterra-2008"
DEFAULT_SESSIONS = Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["chain", "identify", "evaluate", "compare"])
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument(
        "--plant-version",
        nargs="*",
        default=None,
        help="plant version(s) (default fuel_plant.yaml's); compare takes two or more",
    )
    parser.add_argument("--sessions-root", type=Path, default=DEFAULT_SESSIONS)
    parser.add_argument("--sessions", nargs="*", default=None)
    parser.add_argument("--rom", type=Path, default=None, help="plain image (chain only)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    versions = args.plant_version or [None]
    if args.command == "compare":
        if len(versions) < 2:
            print("compare needs --plant-version A B [...]", file=sys.stderr)
            return 2
        return _compare(args, [load_fuel_plant(args.profile, v)[0] for v in versions])
    if len(versions) != 1:
        print(f"{args.command} takes one --plant-version", file=sys.stderr)
        return 2
    profile, _sha = load_fuel_plant(args.profile, versions[0])
    delay = load_b06(args.profile, profile.delay)
    if args.command == "identify":
        names = profile.split.identification
    elif args.command == "evaluate":
        names = profile.split.held_out
    else:
        names = args.sessions or [*profile.split.identification, *profile.split.held_out]
    frames = [build_samples(args.sessions_root / n, profile, delay) for n in names]
    result: dict[str, Any]
    if args.command == "identify":
        if profile.volumetric_efficiency is not None:
            ve = identify_ve(
                pd.concat([ve_samples(args.sessions_root / n, profile) for n in names]), profile
            )
            result = identify(
                frames, profile, delay, sessions_root=args.sessions_root, ve_curve=ve["ve_curve"]
            )
            result["volumetric_efficiency"] = ve
        else:
            result = identify(frames, profile, delay, sessions_root=args.sessions_root)
    elif args.command == "evaluate":
        result = evaluate(
            frames, profile, delay, sessions_root=args.sessions_root, profile_dir=args.profile
        )
        result["recorded"] = record_spent(args.profile, profile, result["spent_records"])
    else:
        if args.rom is None:
            print("chain needs --rom <plain image>", file=sys.stderr)
            return 2
        cal = calibration_from_image(args.rom.read_bytes(), profile.pulse_chain)
        control = without_pressure_correction(cal)
        per: dict[str, Any] = {}
        pooled: list[np.ndarray] = []
        pooled_control: list[np.ndarray] = []
        for name, frame in zip(names, frames, strict=True):
            checked = pulse_chain_ratios(frame, cal, profile.pulse_chain)
            ratios = checked["ratio"].to_numpy(dtype=float)
            per[name] = summarize_ratios(ratios)
            pooled.append(ratios)
            negative = pulse_chain_ratios(frame, control, profile.pulse_chain)
            pooled_control.append(negative["ratio"].to_numpy(dtype=float))
        result = {
            "pooled": summarize_ratios(np.concatenate(pooled)),
            "negative_control_no_pressure_correction": summarize_ratios(
                np.concatenate(pooled_control)
            ),
            "per_session": per,
        }
    print(json.dumps(jsonable(result, ndigits=6), indent=2))
    return 0


def _compare(args: argparse.Namespace, profiles: list[FuelPlantProfile]) -> int:
    """Per-sample comparison of frozen versions on in-sample and spent drives."""
    delay = load_b06(args.profile, profiles[0].delay)
    names = args.sessions or list(
        dict.fromkeys(n for p in profiles for n in [*p.split.identification, *p.spent()])
    )
    frames = [build_samples(args.sessions_root / n, profiles[0], delay) for n in names]
    print(json.dumps(jsonable(compare_versions(frames, profiles, delay), ndigits=6), indent=2))
    return 0


def record_spent(
    profile_dir: Path, profile: FuelPlantProfile, records: list[dict[str, str]]
) -> list[str]:
    """Append the scored recordings to ``spent_held_out`` (the file's last key).

    Already-recorded recordings (same baseline) are left alone; the profile is
    re-validated after writing, so a bad record cannot stay on disk unnoticed.
    """
    path = plant_files(profile_dir).get(profile.plant_version, profile_dir / PLANT_FILE)
    have = profile.spent()
    lines = []
    today = datetime.now(UTC).date().isoformat()
    for rec in records:
        if rec["recording"] in have:
            continue
        lines.append(
            f"  - {{recording: {rec['recording']}, baseline_id: {rec['baseline_id']}, "
            f"baseline_sha256: {rec['baseline_sha256']}, plant_version: "
            f"{rec['plant_version']}, scored_on: {today}, verdict: {rec['verdict']}}}\n"
        )
    if lines:
        text = path.read_text()
        last_key = [ln for ln in text.splitlines() if ln and not ln[0].isspace() and ln[0] != "#"]
        if not last_key or not last_key[-1].startswith("spent_held_out:"):
            raise SystemExit(f"{path}: spent_held_out must be the last key to append to")
        path.write_text(text.rstrip("\n") + "\n" + "".join(lines))
        load_fuel_plant(profile_dir, profile.plant_version)
    return [rec["recording"] for rec in records if rec["recording"] not in have]


if __name__ == "__main__":
    raise SystemExit(main())
