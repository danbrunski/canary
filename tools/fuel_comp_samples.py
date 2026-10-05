#!/usr/bin/env python3
"""N02.2/N02.3 on stored sessions: the load byte's known answer, and every
fuelling-error candidate placed on its Fuel Comp cell, accounted for.

Reads the private ROM images from directories you name (``--rom-dir``, or
``CANARY_ROM_DIRS``, ``os.pathsep``-separated) and refuses any whose plain
sha256 is not the tune registry's. Writes nothing but ``--out``:

- ``samples.parquet``: one row per candidate (:data:`src.analysis.
  fuel_comp_samples.COLUMNS`), every one in exactly one state;
- ``summary.json``: per session the counts by route, state and reason, what
  an empty route means, the revisions seen (and whether the session straddles
  a flash), and per revision and route the eligible and unambiguous counts,
  split by throttle route with its validation, and the top reasons;
- ``known_answer.json`` with ``--known-answer``: the reconstruction against
  the logged byte, and each negative control.

Analysis only: nothing here writes or exports a ROM.

Usage::

    .venv/bin/python -m tools.fuel_comp_samples \\
        --rom-dir "/mnt/c/Users/thebo/Desktop/Uprev/Tuned ROMs" \\
        --rom-dir "/mnt/c/Users/thebo/Desktop/Uprev" \\
        --since 2026-08-03 --known-answer --out <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.analysis.fuel_comp_inputs import (
    known_answer,
    load_fuel_comp_declaration,
    negative_controls,
)
from src.analysis.fuel_comp_rom import (
    ROM_DIRS_ENV,
    FuelCompRomRefusalError,
    RevisionImageResolver,
    rom_dirs_from_env,
)
from src.analysis.fuel_comp_samples import eligible_summary, session_samples
from src.analysis.lambda_delivery import load_lambda_delivery
from src.analysis.rom_map import load_rom_map
from src.signals.afr import load_afr_calibration
from src.signals.derived import FUEL_PROPERTIES
from src.storage.sessions import recorded_sessions, session_started_at
from src.vehicle.tune_revisions import load_tune_revisions

REPO = Path(__file__).resolve().parents[1]


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", type=Path, default=REPO / "data" / "nissan-xterra-2008")
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008",
    )
    parser.add_argument(
        "--rom-dir",
        type=Path,
        action="append",
        default=None,
        help=f"directory holding the registry's rom_file names (repeatable; else ${ROM_DIRS_ENV})",
    )
    parser.add_argument(
        "--image",
        type=Path,
        action="append",
        default=[],
        help="an image file matched by plain sha256 whatever its name (repeatable)",
    )
    parser.add_argument("--since", help="first session start date (UTC, YYYY-MM-DD)")
    parser.add_argument("--session", action="append", default=[], help="session name(s) only")
    parser.add_argument(
        "--fuel-type", choices=sorted(FUEL_PROPERTIES), default="gasoline", help="for stoich AFR"
    )
    parser.add_argument("--known-answer", action="store_true")
    parser.add_argument(
        "--control-closed-position",
        type=int,
        default=96,
        help="closed position (counts) the uncorrected-0x120F control adds",
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def _per_revision(frame: pd.DataFrame) -> dict[str, Any]:
    """:func:`eligible_summary` plus each revision and route's top reasons."""
    out = eligible_summary(frame)
    if frame.empty:
        return out
    revision = frame["revision"].astype(object).where(frame["revision"].notna(), "none")
    for (rev, route), rows in frame.groupby([revision, frame["route"]], sort=True):
        reasons = Counter(
            f"{s}:{r}"
            for s, r in zip(rows["state"], rows["reason"], strict=True)
            if s != "eligible"
        )
        out[str(rev)][str(route)]["top_reasons"] = reasons.most_common(8)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    profile = args.profile
    declaration = load_fuel_comp_declaration(profile)
    registry = load_tune_revisions(profile / "tune_revisions.yaml")
    if registry is None:
        print("this vehicle has no tune_revisions.yaml", file=sys.stderr)
        return 2
    delivery = load_lambda_delivery(profile / "lambda_delivery.yaml")
    afr = load_afr_calibration(profile / "afr_calibration.yaml")
    afr_stoich = FUEL_PROPERTIES[args.fuel_type].afr_stoich
    rom_dirs = args.rom_dir if args.rom_dir else rom_dirs_from_env()
    resolver = RevisionImageResolver(
        registry,
        load_rom_map(profile),
        declaration.rom,
        rom_dirs=rom_dirs,
        image_files=args.image,
        slot=declaration.program.map_slot,
    )
    args.out.mkdir(parents=True, exist_ok=True)

    if args.known_answer:
        session = args.sessions_root / declaration.known_answer.session
        first = pd.read_parquet(
            sorted(session.glob("telemetry-*.parquet"))[0], columns=["timestamp"]
        )
        revision = registry.revisions_at(first["timestamp"].head(1)).iloc[0]
        calibration = resolver.resolve(str(revision))
        decoded = known_answer(session, calibration, declaration)
        # The uncorrected-DID control adds a closed position: the warm-idle
        # estimate's midpoint for this capture's key cycle (94-98 by steady
        # brackets), so the control models feeding 0x120F raw counts.
        controls = [
            known_answer(session, calibration, declaration, variant=variant)
            for variant in negative_controls(args.control_closed_position)
        ]
        payload = {
            "revision": str(revision),
            "decoded": decoded.as_dict(),
            "controls": [c.as_dict() for c in controls],
        }
        (args.out / "known_answer.json").write_text(json.dumps(payload, indent=2, default=str))
        print(json.dumps(payload["decoded"], indent=1, default=str))
        for control in controls:
            print(control.variant, "passed" if control.passed else "FAILED", control.reasons)

    sessions = recorded_sessions([args.sessions_root])
    if args.session:
        wanted = set(args.session)
        sessions = [s for s in sessions if s.name in wanted]
    if args.since:
        floor = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
        sessions = [s for s in sessions if (session_started_at(s) or floor) >= floor]
    frames: list[pd.DataFrame] = []
    summaries: list[dict[str, Any]] = []
    for session in sessions:
        try:
            result = session_samples(
                session,
                declaration=declaration,
                delivery=delivery,
                afr_calibration=afr,
                afr_stoich=afr_stoich,
                registry=registry,
                resolver=resolver,
            )
        except (FuelCompRomRefusalError, ValueError) as exc:
            summaries.append({"session": session.name, "error": f"{type(exc).__name__}: {exc}"})
            print(f"{session.name}: ERROR {exc}", file=sys.stderr)
            continue
        frames.append(result.frame)
        summaries.append(result.as_dict())
        eligible = {r: c.get("eligible", 0) for r, c in result.counts.items()}
        print(f"{session.name}: {result.revisions} eligible {eligible} empty {result.empty}")
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not frame.empty:
        frame.astype({c: "string" for c in frame.columns if frame[c].dtype == object}).to_parquet(
            args.out / "samples.parquet", index=False
        )
    gates: dict[str, Counter[str]] = defaultdict(Counter)
    for route in ("closed_loop", "open_loop"):
        rows = frame[frame["route"] == route] if not frame.empty else frame
        if not rows.empty:
            gates[route].update(
                f"{s}:{r}"
                for s, r in zip(rows["state"], rows["reason"], strict=True)
                if s != "eligible"
            )
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "sessions": summaries,
        "per_revision": _per_revision(frame),
        "top_reasons": {route: counter.most_common(12) for route, counter in gates.items()},
        "straddling_sessions": [s["session"] for s in summaries if s.get("straddles_flash")],
        "rom_refusals": {k: v for s in summaries for k, v in (s.get("refusals") or {}).items()},
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary["per_revision"], indent=1, default=str))
    print(json.dumps(summary["top_reasons"], indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
