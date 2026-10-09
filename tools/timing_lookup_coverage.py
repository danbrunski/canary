"""B08.4 on stored sessions: what the timing lookup context covers, and the
existing timing surface on identical samples.

For each session it reports which context inputs were captured, the timing
lookup coordinate (rpm word x BFS-X, ``timing_coordinate`` of
:mod:`src.analysis.operating_context`) at every rpm-word read, every read of
the existing surface's output paired with that surface's axes and the
coordinate under ``--pairing-ms`` (each unpaired read counted under its
reason), the existing surface graded on those identical samples through the
B02 evaluator (a capture-profile sample is refused by name, R03.12), the
barometric pressure the session spans, and where the coordinate falls on the
session's image's trim tables. Each memory-only target (B08.4 option c) is
counted with the coordinate from its own read, by reason. Across sessions it
reports HI DET branch evidence and whether it could validate both branches.

The images come from ``--image`` files matched by plain sha256 against the
tune registry's revision for each session's start; a session whose image is
not supplied gets no trim-cell placement (named). Analysis only: nothing is
fitted, published or written but ``--out``.

Usage::

    .venv/bin/python -m tools.timing_lookup_coverage \\
        --session _capture-rom-trace-2026-09-21T142926 \\
        --session _capture-ram-study-2026-10-04T204557 \\
        --image "/mnt/c/Users/thebo/Desktop/Uprev/Tuned ROMs/ZS44A-Tuned-20260920.OsirisROM" \\
        --pairing-ms 250 --out <scratch>/timing_lookup.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.behavior_validation import load_validation_document
from src.analysis.channels import read_channels
from src.analysis.ecu_application import load_application_model
from src.analysis.operating_context import (
    ContextProvider,
    ContextRefusalError,
    image_tables,
    load_context_definition,
)
from src.analysis.rom_image import ImageFormatError, load_image, sha256_hex
from src.analysis.rom_map import load_rom_map
from src.analysis.timing_lookup import (
    MIN_BRANCH_DRIVES,
    TimingLookupRefusalError,
    branch_evidence,
    coordinate_samples,
    identical_samples,
    memory_only_samples,
    require_branch_validation,
    score_identical_samples,
    summarize,
    trim_cell_coverage,
)
from src.behavior.behavior_models import BehaviorModel2D, load_behavior_models
from src.signals.pairing import PairingPolicy
from src.vehicle.tune_revisions import load_tune_revisions

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO_ROOT / "data" / "nissan-xterra-2008"
BARO = "obd:BAROMETRIC_PRESSURE"
COOLANT = "obd:COOLANT_TEMP"


def _args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008",
    )
    parser.add_argument("--session", action="append", required=True, help="session name(s)")
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--image", type=Path, action="append", default=[])
    parser.add_argument("--model", default="timing_advance_vs_rpm_load")
    parser.add_argument(
        "--pairing-ms",
        type=float,
        required=True,
        help="largest acquisition separation of an existing-surface axis from the target read",
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def _images(paths: Sequence[Path], rom_map: Any) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for path in paths:
        try:
            image = load_image(path.read_bytes(), rom_map).image
        except ImageFormatError as exc:
            raise SystemExit(f"{path}: {exc}") from exc
        out[sha256_hex(image)] = image
    return out


def _span(long: pd.DataFrame, spec: str) -> list[float] | None:
    values = pd.to_numeric(long.loc[long["spec"] == spec, "value"], errors="coerce").dropna()
    return None if values.empty else [float(values.min()), float(values.max())]


def main(argv: Sequence[str] | None = None) -> int:
    args = _args(argv)
    profile = args.profile_dir
    definition, _sha = load_context_definition(profile)
    limit_ms = definition.timing_trim.max_acquisition_skew_s * 1000.0
    if args.pairing_ms > limit_ms:
        raise SystemExit(
            f"--pairing-ms {args.pairing_ms:g} exceeds the timing lookup's skew limit "
            f"({limit_ms:g} ms, operating_context.yaml timing_trim.max_acquisition_skew_s)"
        )
    rom_map = load_rom_map(profile)
    application_model = load_application_model(profile)
    registry = load_tune_revisions(profile / "tune_revisions.yaml")
    images = _images(args.image, rom_map)
    models = load_behavior_models(profile / "behavior_models.yaml")
    model = next((m for m in models.models if m.name == args.model), None)
    if not isinstance(model, BehaviorModel2D):
        raise SystemExit(f"{args.model}: not a 2-D behavior model in behavior_models.yaml")
    document = load_validation_document(profile / "behavior_validation.yaml")
    criteria = None if document is None else document.recipes.get(args.model)
    min_drives = max(
        MIN_BRANCH_DRIVES, criteria.min_validation_drives if criteria is not None else 0
    )
    policy = PairingPolicy.within(pd.Timedelta(milliseconds=args.pairing_ms))
    sessions = [args.sessions_root / name for name in args.session]

    report: dict[str, Any] = {
        "model": {"name": model.name, "version": model.version, "output": model.output},
        "policy": policy.as_dict(),
        "lookup_skew_s": definition.timing_trim.max_acquisition_skew_s,
        "sessions": {},
    }
    all_coordinates = []
    for directory in sessions:
        revision = registry.for_session(directory) if registry is not None else None
        sha = None if revision is None else revision.image_sha256
        image = None if sha is None else images.get(sha)
        tables_image = image if image is not None else next(iter(images.values()), None)
        if tables_image is None:
            raise SystemExit("pass at least one --image (the provider needs an image's tables)")
        provider = ContextProvider.from_profile(
            profile,
            rom_map=rom_map,
            application_model=application_model,
            tables=image_tables(
                tables_image,
                rom_map,
                definition,
                application_model=application_model,
                expected_sha256=sha256_hex(tables_image),
            ),
        )
        specs = sorted(
            {
                *provider.channels(),
                *(axis.channel for axis in model.axes),
                model.output,
                *definition.timing_trim.final_timing_channels,
                BARO,
                COOLANT,
            }
        )
        long = read_channels(directory, specs, acquired_time=True)
        coordinates = coordinate_samples(provider, long, session=directory.name)
        all_coordinates.extend(coordinates)
        samples = identical_samples(provider, long, model, session=directory.name, policy=policy)
        scored = score_identical_samples(provider, model, samples, sessions=sessions)
        summary = summarize(samples, scored)
        identities = [r for r in provider.evaluate_frame(long, [c.t for c in coordinates])]
        slots = sorted({r["identity"]["slot"] for r in identities if "slot" in r["identity"]})
        entry: dict[str, Any] = {
            "revision": None if revision is None else revision.revision,
            "coverage": provider.coverage(long["spec"]),
            "reads": {k: int(v) for k, v in long["spec"].value_counts().items()},
            "baro_kpa": _span(long, BARO),
            "coolant_c": _span(long, COOLANT),
            "coordinate_samples": len(coordinates),
            "coordinate_observed": sum(1 for c in coordinates if c.observed),
            "coordinate_refused": dict(
                pd.Series([c.refused for c in coordinates if c.refused]).value_counts()
            ),
            "slots_observed": slots,
            "identical": summary.__dict__,
            # B08.4 option (c): each memory-only target with the coordinate
            # from its own read, no Mode 01 leg.
            "memory_only": {
                target.channel: summarize(
                    memory_only_samples(
                        provider, long, session=directory.name, target=target.channel
                    ),
                    [],
                ).__dict__
                for target in definition.timing_trim.memory_only_targets
            },
            "graded": [
                {
                    "t": s.sample.t,
                    "target": s.sample.target,
                    "expected": s.expected,
                    "residual": s.residual,
                    "rpm_word": s.sample.coordinate.rpm_word,
                    "bfs_x_raw": s.sample.coordinate.load_raw,
                    "high_det": s.sample.coordinate.high_det,
                }
                for s in scored
                if s.reason is None
            ],
        }
        if image is None:
            entry["trim_cells"] = {"refused": f"image_not_supplied:{sha}"}
        else:
            slot = slots[0] if len(slots) == 1 else 1
            assumptions = ["pedal column override not applied", "cold rule not applied"]
            if not slots:
                assumptions.append("slot unobserved on this session; slot 1 assumed")
            try:
                resolved = image_tables(
                    image,
                    rom_map,
                    definition,
                    application_model=application_model,
                    expected_sha256=sha256_hex(image),
                )(slot)
            except ContextRefusalError as exc:
                entry["trim_cells"] = {"refused": exc.reason}
            else:
                cov = trim_cell_coverage(coordinates, resolved, assumptions=assumptions)
                entry["trim_cells"] = {
                    "provenance": cov.provenance,
                    "assumptions": list(cov.assumptions),
                    "samples": cov.samples,
                    "cells": {f"{r},{c}": n for (r, c), n in cov.cells.items()},
                    "distinguishable_cells": [list(c) for c in cov.distinguishable_cells],
                    "samples_reading_distinguishable": cov.samples_reading_distinguishable,
                    "clamped": cov.clamped,
                }
        report["sessions"][directory.name] = entry

    evidence = branch_evidence(all_coordinates)
    try:
        require_branch_validation(evidence, min_drives=min_drives)
        verdict: str | None = None
    except TimingLookupRefusalError as exc:
        verdict = exc.reason
    report["branches"] = {
        "samples": evidence.samples,
        "drives": evidence.drives,
        "unobserved": evidence.unobserved,
        "min_drives": min_drives,
        "refused": verdict,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "sessions"}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
