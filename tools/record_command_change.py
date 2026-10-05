#!/usr/bin/env python3
"""Record a command-change what-if before a flash (E02.6).

Given the calibration the ECM runs now (``--base``) and a proposed change --
another exact image (``--target``) or a Fuel Targets editor patch
(``--patch``, the editor's ``rom-targets-known-edit.json`` shape) -- computes
the commanded equivalence ratio the reproduced target logic gives before and
after at stated raw lookup inputs, per changed cell (or per ``--region``), and
records one E01 prediction per region on the vehicle's memory ledger. Prints
each prediction with its what-if points.

``--base``/``--target`` take a tune revision name (``ZS44A-20261004``), a
plain-image sha256 held in the ROM library, or the path of a plain image
file (a candidate not yet in the library). A held image is read through W08
and every changed range outside the reproduced table is named; a revision
whose image is not held falls back to its transcribed table, and what else
changed is then reported unknown.

Run it **before** flashing: the ledger orders the prediction against the flash
when that is recorded (``--intervention`` names one already on the ledger).
This computes and records; it sends nothing to the vehicle, and it creates no
ECU write request.

Usage:
  tools/record_command_change.py --config canary.dev.example.yaml \\
      --profile nissan-xterra-2008 --base ZS44A-20261004 --target ./candidate.bin
  tools/record_command_change.py --config canary.dev.example.yaml \\
      --profile nissan-xterra-2008 --base ZS44A-k106 \\
      --patch data/nissan-xterra-2008/rom-targets-known-edit.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

REPO_ROOT = Path(__file__).resolve().parents[1]

if TYPE_CHECKING:
    from src.analysis.target_phi import ResolvedTable
    from src.analysis.twin_command_change import Region
    from src.config import CanaryConfig

_SHA = re.compile(r"^[0-9a-f]{64}$")
_REGION = re.compile(r"^(ordinary|override):(\d+)-(\d+)(?::(\d+)-(\d+))?$")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="record_command_change",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, required=True, help="canary.yaml to scope by.")
    parser.add_argument("--profile", default=None, help="Vehicle profile slug.")
    parser.add_argument("--base", required=True, help="Revision, library sha256 or image path.")
    change = parser.add_mutually_exclusive_group(required=True)
    change.add_argument("--target", help="Revision, library sha256 or image path.")
    change.add_argument("--patch", type=Path, help="Fuel Targets editor patch (JSON).")
    parser.add_argument("--slot", type=int, default=None, help="Map slot (default: profile's).")
    parser.add_argument(
        "--region",
        action="append",
        default=[],
        metavar="BRANCH:R0-R1[:C0-C1]",
        help="Region by table index, e.g. ordinary:6-7:3-5 or override:0-7 (repeatable). "
        "Default: each changed cell.",
    )
    parser.add_argument("--investigation", default=None, help="Investigation id it serves.")
    parser.add_argument("--intervention", default=None, help="Flash it predicts, if recorded.")
    parser.add_argument("--json", action="store_true", help="Print the predictions as JSON.")
    return parser.parse_args(argv)


def parse_region(text: str, columns: int) -> Region:
    from src.analysis.twin_command_change import Region

    match = _REGION.match(text)
    if match is None:
        raise ValueError(f"region {text!r} is not BRANCH:R0-R1[:C0-C1]")
    branch, r0, r1, c0, c1 = match.groups()
    if branch == "override":
        return Region((int(r0), int(r1)), (columns - 1, columns - 1), "override")
    if c0 is None:
        raise ValueError(f"an ordinary region names its columns: {text!r}")
    return Region((int(r0), int(r1)), (int(c0), int(c1)), "ordinary")


def _resolve(config: CanaryConfig, which: str, slot: int) -> tuple[ResolvedTable, bytes | None]:
    """A calibration by revision name, library sha256 or image file path."""
    from src.analysis.ecu_application import load_application_model
    from src.analysis.rom_trace import load_viewer_definition
    from src.analysis.target_phi import load_twin_definition, resolve_table, table_from_calibration
    from src.control.rom_library import RomLibrary, RomNotFoundError
    from src.signals.afr import load_afr_calibration
    from src.vehicle.profiles import active_registry, profile_dir

    pdir = profile_dir(config)
    definition, _sha = load_twin_definition(pdir)
    viewer = load_viewer_definition(pdir)
    model = load_application_model(pdir)
    registry = active_registry(config)
    library = RomLibrary(
        config.daemon.rom_library_dir, profile_dir=pdir, registry=lambda: active_registry(config)
    )
    image: bytes | None = None
    revision: str | None = None
    if _SHA.match(which):
        sha = which
    elif Path(which).is_file():
        image = Path(which).read_bytes()
        sha = hashlib.sha256(image).hexdigest()
    else:
        entry = next(
            (r for r in (registry.revisions if registry else []) if r.revision == which), None
        )
        if entry is None or entry.image_sha256 is None:
            raise ValueError(f"{which!r} is no revision with a recorded image, sha256 or file")
        sha, revision = entry.image_sha256, entry.revision
    if image is None:
        try:
            image = library.image(sha)
        except RomNotFoundError:
            image = None
    if image is not None:
        table = resolve_table(
            image,
            library.rom_map(),
            definition,
            viewer=viewer,
            application_model=model,
            expected_sha256=sha,
            slot=slot,
        )
        return table, image
    if revision is None and registry is not None:
        revision, _reference = registry.match_image(sha)
    if revision is None:
        raise ValueError(f"image {sha} is neither held in the ROM library nor a known revision")
    table = table_from_calibration(
        load_afr_calibration(config.daemon.afr_calibration),
        definition,
        viewer=viewer,
        application_model=model,
        revision=revision,
        image_sha256=sha,
        slot=slot,
    )
    return table, None


def record(
    config: CanaryConfig,
    *,
    base: str,
    target: str | None = None,
    patch: Path | None = None,
    slot: int | None = None,
    regions: Sequence[str] = (),
    investigation: str | None = None,
    intervention: str | None = None,
) -> list[dict[str, Any]]:
    """Compute and record; returns each stored prediction as a dict."""
    from src.analysis.rom_targets import Patch
    from src.analysis.target_phi import load_twin_definition
    from src.analysis.twin_command_change import (
        change_from_cell_edits,
        change_from_images,
        record_command_change,
    )
    from src.control.rom_library import RomLibrary
    from src.evidence import VehicleIdentity, memory_ledger_for, record_to_dict
    from src.vehicle.profiles import active_registry, profile_dir

    if (target is None) == (patch is None):
        raise ValueError("name the change once: a target image or a patch")
    assert config.scoped_profile is not None, "scope the config to a vehicle profile"
    definition, _sha = load_twin_definition(profile_dir(config))
    chosen = slot if slot is not None else definition.map_identity.default_slot.slot
    base_table, base_image = _resolve(config, base, chosen)
    if target is not None:
        target_table, target_image = _resolve(config, target, chosen)
        library = RomLibrary(
            config.daemon.rom_library_dir,
            profile_dir=profile_dir(config),
            registry=lambda: active_registry(config),
        )
        change = change_from_images(
            base_table,
            target_table,
            base_image=base_image,
            target_image=target_image,
            rom_map=library.rom_map(),
        )
    else:
        assert patch is not None
        registry = active_registry(config)
        if registry is None:
            raise ValueError("no flash history: a patch's source container cannot be checked")
        change = change_from_cell_edits(
            base_table,
            Patch.model_validate_json(patch.read_text()),
            registry=registry,
            definition=definition,
        )
    columns = len(base_table.cells[0])
    ledger = memory_ledger_for(config)
    ids = record_command_change(
        ledger,
        change,
        definition,
        vehicle=VehicleIdentity(
            profile=config.scoped_profile,
            vin=config.vehicle.vin or None,
            program_id=base_table.application.program_id,
            calibration=base_table.revision,
        ),
        regions=[parse_region(r, columns) for r in regions] or None,
        investigation=investigation,
        intervention=intervention,
    )
    return [record_to_dict(ledger.get(pid)) for pid in ids]


def _print(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        what_if = row["what_if"]
        change = what_if["target_image_sha256"] or f"cell edit set {what_if['change_set_sha256']}"
        print(f"{row['id']}  WHAT-IF {row['direction']}  {row['conditions']['regime']}")
        print(f"  base {what_if['base_image_sha256']} -> {change}")
        print(f"  blockers: {', '.join(what_if['blockers']) or 'none'}")
        print(f"  not reproduced: {'; '.join(what_if['not_reproduced']) or 'none'}")
        for point in what_if["points"]:
            if point["before"] is None:
                print(f"    {point['key']}: no value ({point['reason']})")
            else:
                print(
                    f"    {point['key']}: phi {point['before']['value']:.8g} -> "
                    f"{point['after']['value']:.8g}"
                )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    sys.path.insert(0, str(REPO_ROOT))
    from src.analysis.target_phi import TwinRefusalError
    from src.analysis.twin_command_change import CommandChangeRefusalError
    from src.config import CanaryConfig
    from src.vehicle.profiles import resolve_profile_for_tooling, scope_config

    base = CanaryConfig.from_yaml(args.config)
    config = scope_config(base, resolve_profile_for_tooling(base, args.profile))
    try:
        rows = record(
            config,
            base=args.base,
            target=args.target,
            patch=args.patch,
            slot=args.slot,
            regions=args.region,
            investigation=args.investigation,
            intervention=args.intervention,
        )
    except (CommandChangeRefusalError, TwinRefusalError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        _print(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
