#!/usr/bin/env python3
"""N02.4-N02.6 on stored sessions: fuel-supply droop, a per-cell Fuel Comp
proposal on the target revision, held-out validation with the MAF-shaped
diagnostic, and the knock-on prediction, as ``fit.json`` and ``report.md``.

**A proposal, not a measured outcome. Nothing here writes or exports a ROM.**
Every output is JSON, Markdown or Parquet in ``--out``
(:func:`src.analysis.fuel_comp_report.write_outputs` refuses anything else):

- ``fit.json``: every input's revision and image sha256, the gates and
  pairing, sample accounting, both known answers, droop evidence, every
  cell's outcome both ways (DID route excluded / included), validation and
  diagnostic, knock-on per cell;
- ``report.md``: the same for a person, headed as a proposal;
- ``fit_samples.parquet``: each eligible sample's required factor, design
  row and refusal; ``knock_on.parquet``: each predicted sample;
  ``cells.parquet``: the declared configuration's 256 cells;
- ``samples.parquet``: N02.3's frame, when computed here (not with
  ``--samples``).

Reads the private ROM images from directories you name (``--rom-dir``, or
``CANARY_ROM_DIRS``) and refuses any whose plain sha256 is not the registry's.

Usage::

    CANARY_ROM_DIRS="/mnt/c/Users/thebo/Desktop/Uprev/Tuned ROMs:..." \\
    .venv/bin/python -m tools.fuel_comp_fit --since 2026-08-03 --out <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from src.analysis.ecu_application import load_application_model
from src.analysis.fuel_comp_droop import pressure_sessions
from src.analysis.fuel_comp_inputs import (
    assign_drives,
    known_answer,
    load_fuel_comp_declaration,
)
from src.analysis.fuel_comp_knock_on import KnockOnRefusalError, KnockOnTables, knock_on_tables
from src.analysis.fuel_comp_report import (
    build_result,
    fit_table_frame,
    render_markdown,
    write_outputs,
)
from src.analysis.fuel_comp_rom import (
    ROM_DIRS_ENV,
    FuelCompCalibration,
    FuelCompRomRefusalError,
    RevisionImageResolver,
    rom_dirs_from_env,
)
from src.analysis.fuel_comp_samples import COLUMNS, SAMPLES_VERSION, session_samples
from src.analysis.lambda_delivery import load_lambda_delivery
from src.analysis.rom_map import load_rom_map
from src.analysis.rom_trace import load_viewer_definition
from src.analysis.target_phi import TwinRefusalError, load_twin_definition, resolve_table
from src.paths import DIDS_YAML
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
    parser.add_argument("--image", type=Path, action="append", default=[])
    parser.add_argument("--since", help="first session start date (UTC, YYYY-MM-DD)")
    parser.add_argument("--session", action="append", default=[], help="session name(s) only")
    parser.add_argument(
        "--samples",
        type=Path,
        help=f"reuse a samples.parquet from tools.fuel_comp_samples (v{SAMPLES_VERSION})",
    )
    parser.add_argument(
        "--fuel-type", choices=sorted(FUEL_PROPERTIES), default="gasoline", help="for stoich AFR"
    )
    parser.add_argument(
        "--dids",
        type=Path,
        default=DIDS_YAML,
        help="DID catalog to decode with (default the repo's; a pinned copy keeps a long "
        "corpus run independent of edits made while it runs)",
    )
    parser.add_argument(
        "--lambda-delivery",
        type=Path,
        default=None,
        help="lambda_delivery.yaml to use (default the profile's); its sha256 is recorded",
    )
    parser.add_argument(
        "--afr-calibration",
        type=Path,
        default=None,
        help="afr_calibration.yaml to use (default the profile's); its sha256 is recorded",
    )
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


def _sessions(args: argparse.Namespace) -> list[Path]:
    sessions = recorded_sessions([args.sessions_root])
    if args.session:
        wanted = set(args.session)
        sessions = [s for s in sessions if s.name in wanted]
    if args.since:
        floor = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
        sessions = [s for s in sessions if (session_started_at(s) or floor) >= floor]
    return sessions


#: Flag columns a samples.parquet stores as text ("True"/"False").
_FLAGS = (
    "throttle_validated",
    "cell_ambiguous",
    "idle_air_moves_cell",
    "purge_moves_cell",
    "throttle_moves_cell",
    "rpm_moves_cell",
)


def _read_samples(path: Path) -> pd.DataFrame:
    """A stored samples frame with its flags and numbers restored: the writer
    stores object columns as text, and ``"False"`` is truthy."""
    frame = pd.read_parquet(path)
    missing = [c for c in COLUMNS if c not in frame.columns]
    if missing:
        raise SystemExit(
            f"{path} is not samples v{SAMPLES_VERSION} (missing {missing[:6]}...): re-run "
            "tools.fuel_comp_samples or drop --samples"
        )
    for column in frame.columns:
        if column in _FLAGS:
            text = frame[column].astype(object)
            frame[column] = text.map({"True": True, "False": False, True: True, False: False})
        elif isinstance(frame[column].dtype, pd.StringDtype):
            numbers = pd.to_numeric(frame[column], errors="coerce")
            if numbers.notna().sum() == frame[column].notna().sum():
                frame[column] = numbers.astype("float64")
            else:
                frame[column] = frame[column].astype(object)
    return frame


def _session_span(path: Path) -> tuple[float, float] | None:
    """First and last stored timestamp (epoch s) from the parquet footers'
    statistics: no row is read."""
    lo = hi = None
    for part in sorted(path.glob("telemetry-*.parquet")):
        meta = pq.ParquetFile(part).metadata  # type: ignore[no-untyped-call, unused-ignore]
        names = meta.schema.names
        if "timestamp" not in names:
            continue
        column = names.index("timestamp")
        for group in range(meta.num_row_groups):
            stats = meta.row_group(group).column(column).statistics
            if stats is None or not stats.has_min_max:
                continue
            a, b = pd.Timestamp(stats.min).timestamp(), pd.Timestamp(stats.max).timestamp()
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
    return None if lo is None or hi is None else (lo, hi)


def _file_record(path: Path, tracked: Path) -> dict[str, Any]:
    """sha256 of a profile file as read, and whether it differs from HEAD's
    copy of ``tracked`` (the repo file it stands for; itself unless pinned)."""
    data = path.read_bytes()
    record: dict[str, Any] = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}
    try:
        relative = tracked.resolve().relative_to(REPO)
        head = subprocess.run(
            ["git", "-C", str(REPO), "show", f"HEAD:{relative}"],
            capture_output=True,
            check=True,
        ).stdout
        record["dirty"] = head != data
    except (ValueError, subprocess.CalledProcessError, OSError):
        record["dirty"] = None  # outside the checkout or not tracked
    return record


def _known_answer(
    args: argparse.Namespace, declaration: Any, registry: Any, resolver: RevisionImageResolver
) -> dict[str, Any]:
    session = args.sessions_root / declaration.known_answer.session
    if not session.is_dir():
        return {"summary": f"not run: never_captured ({session} is not on this machine)"}
    first = pd.read_parquet(sorted(session.glob("telemetry-*.parquet"))[0], columns=["timestamp"])
    revision = str(registry.revisions_at(first["timestamp"].head(1)).iloc[0])
    try:
        calibration = resolver.resolve(revision)
    except FuelCompRomRefusalError as exc:
        return {"summary": f"not run: unresolvable ({exc})"}
    result = known_answer(session, calibration, declaration, dids_path=args.dids).as_dict()
    subsets = result["subsets"]
    result["summary"] = {
        "revision": revision,
        "passed": result["passed"],
        "idle": {k: subsets["idle"].get(k) for k in ("n", "median_ratio", "inside")},
        "off_idle": {k: subsets["off_idle"].get(k) for k in ("n", "median_ratio", "inside")},
    }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    profile = args.profile
    declaration = load_fuel_comp_declaration(profile)
    registry = load_tune_revisions(profile / "tune_revisions.yaml")
    if registry is None:
        print("this vehicle has no tune_revisions.yaml", file=sys.stderr)
        return 2
    rom_map = load_rom_map(profile)
    resolver = RevisionImageResolver(
        registry,
        rom_map,
        declaration.rom,
        rom_dirs=args.rom_dir if args.rom_dir else rom_dirs_from_env(),
        image_files=args.image,
        slot=declaration.program.map_slot,
    )
    try:
        target = resolver.resolve(declaration.fit.target_revision)
    except FuelCompRomRefusalError as exc:
        print(f"the target revision cannot be read, so nothing is proposed: {exc}", file=sys.stderr)
        return 2
    sessions = _sessions(args)
    lambda_path = args.lambda_delivery or profile / "lambda_delivery.yaml"
    afr_path = args.afr_calibration or profile / "afr_calibration.yaml"
    read_files = {
        "fuel_comp_fit.yaml": profile / "fuel_comp_fit.yaml",
        "tune_revisions.yaml": profile / "tune_revisions.yaml",
        "rom_map.yaml": profile / "rom_map.yaml",
        "lambda_delivery.yaml": lambda_path,
        "afr_calibration.yaml": afr_path,
        "target_phi_twin.yaml": profile / "target_phi_twin.yaml",
        "rom_viewer.yaml": profile / "rom_viewer.yaml",
        "ecu_application_model.yaml": profile / "ecu_application_model.yaml",
        "dids.yaml": args.dids,
    }
    tracked = {"dids.yaml": REPO / "data" / "dids.yaml"}
    profile_files = {
        k: _file_record(v, tracked.get(k, profile / k)) for k, v in read_files.items() if v.exists()
    }
    spans = {s.name: span for s in sessions if (span := _session_span(s)) is not None}
    drives = assign_drives(spans, declaration.fit.drive_unit)
    outputs: dict[str, Any] = {}
    if args.samples:
        samples = _read_samples(args.samples)
    else:
        delivery = load_lambda_delivery(lambda_path)
        afr = load_afr_calibration(afr_path)
        frames = []
        for session in sessions:
            try:
                found = session_samples(
                    session,
                    declaration=declaration,
                    delivery=delivery,
                    afr_calibration=afr,
                    afr_stoich=FUEL_PROPERTIES[args.fuel_type].afr_stoich,
                    registry=registry,
                    resolver=resolver,
                    dids_path=args.dids,
                )
            except (FuelCompRomRefusalError, ValueError) as exc:
                print(f"{session.name}: ERROR {exc}", file=sys.stderr)
                continue
            frames.append(found.frame)
            print(
                f"{session.name}: {found.counts['closed_loop'].get('eligible', 0)} / "
                f"{found.counts['open_loop'].get('eligible', 0)} eligible"
            )
        samples = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
        outputs["samples.parquet"] = samples
    eligible = samples[samples["state"].astype(str) == "eligible"]
    calibrations: dict[str, FuelCompCalibration] = {}
    for revision in sorted({str(r) for r in eligible["revision"].dropna()}):
        try:
            calibrations[revision] = resolver.resolve(revision)
        except FuelCompRomRefusalError as exc:
            print(f"{revision}: {exc}", file=sys.stderr)
    with_samples = {str(s) for s in eligible["session"]}
    wanted_drives = {drives.get(n, n) for n in with_samples} | {
        drives.get(n, n) for n in declaration.droop.known_answer.sessions
    }
    pressure = pressure_sessions(
        [s for s in sessions if drives.get(s.name, s.name) in wanted_drives],
        declaration.droop,
        dids_path=args.dids,
    )
    tables: KnockOnTables | None = None
    refusal: str | None = None
    try:
        twin, _ = load_twin_definition(profile)
        image = resolver.image(target.name)
        target_afr = resolve_table(
            image,
            rom_map,
            twin,
            viewer=load_viewer_definition(profile),
            application_model=load_application_model(profile),
            expected_sha256=target.image_sha256,
            slot=declaration.program.map_slot,
        )
        tables = knock_on_tables(image, rom_map, target, target_afr, twin, declaration)
    except (TwinRefusalError, KnockOnRefusalError, FuelCompRomRefusalError) as exc:
        refusal = str(exc)
    result = build_result(
        samples,
        declaration=declaration,
        pressure=pressure,
        calibrations=calibrations,
        target=target,
        tables=tables,
        tables_refusal=refusal,
        load_byte_known_answer=_known_answer(args, declaration, registry, resolver),
        sessions=[s.name for s in sessions],
        drives=drives,
        profile_files=profile_files,
        notes=[
            f"samples from {args.samples}" if args.samples else "samples computed here",
            f"DID catalog {args.dids}",
        ],
    )
    primary = result["fit_json"]["primary_configuration"]
    outputs.update(
        {
            "fit.json": result["fit_json"],
            "report.md": render_markdown(result["fit_json"]),
            "fit_samples.parquet": result["prepared"],
            "knock_on.parquet": result["predictions"],
            "cells.parquet": fit_table_frame(result["fits"][primary]),
        }
    )
    for path in write_outputs(args.out, outputs):
        print(f"wrote {path}")
    fits = result["fit_json"]["fits"]
    for name, fit in fits.items():
        print(name, fit["status_counts"], result["fit_json"]["validation"][name]["verdict"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
