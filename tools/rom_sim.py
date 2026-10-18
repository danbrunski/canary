#!/usr/bin/env python3
"""E08.5: edit a ROM, simulate it, see the effect with its uncertainty.

Offline only: reads ROM images, stored sessions and the vehicle profile,
prints JSON, and writes artifacts under a caller-given ``--out`` that is never
inside the checkout (``inside_checkout``: images, designs and traces stay out
of Git). Nothing here talks to the ECU, and no image is copied anywhere.

Subcommands::

    design drives  pre-change drives -> a frozen, hashed point design (E08.2)
    design grid    a stated rpm x load grid -> the same kind of design
    inspect        the resolved cell edits (old -> new raw, +/- %), what
                   compare_roms says they reach, and the lever and what
                   blocks a number (rom_levers); runs nothing
    run            a paired Monte Carlo over a design -> result.json,
                   traces.parquet (a ``_sim-*`` session) and report.html

Usage::

    .venv/bin/python -m tools.rom_sim design drives --base-revision ZS44A-20260920 \\
        --sessions _capture-tuning-2026-09-23T142926 --fuel-type gasoline \\
        --battery-v-when-unmeasured 14.0 --rom-dir "<ROM dir>" --out <scratch>/designs
    .venv/bin/python -m tools.rom_sim inspect --edits spec.yaml --rom-dir "<ROM dir>"
    .venv/bin/python -m tools.rom_sim run --base --revision ZS44A-20260920 \\
        --candidate-rom <edited.OsirisROM> --design <scratch>/designs/<sha> \\
        --draws 200 --seed 1 --stft-pct 0 --ltft-pct 0 --candidate-events ecu_reflash \\
        --rom-dir "<ROM dir>" --out <scratch>/run

ROM directories come from ``--rom-dir`` (repeatable) or ``$CANARY_ROM_DIRS``;
the base is a ``tune_revisions.yaml`` revision found there (``--revision``)
or a file with its plain-image sha256 (``--rom`` and ``--sha256``). ``--base``
is an optional marker that reads well beside ``--candidate-rom``; it changes
nothing. The candidate is an edit spec (``--edits``, E08.1) or an already
edited image (``--candidate-rom``, e.g. saved from UpRev).

What it makes impossible rather than documented:

- **Nothing the result depends on has a default.** ``--draws``, ``--seed``,
  ``--fuel-type`` (drive designs), ``--battery-v-when-unmeasured`` (when a
  drive needs it), the whole grid environment, and the learned trims and
  candidate events a run starts from (``--stft-pct``, ``--ltft-pct``,
  ``--candidate-events``; ``none`` is a stated answer) are refused when
  missing: ``argument_missing``, or the library's own ``environment_unstated``
  / ``battery_v_unstated`` naming what is absent. The ROM-file route to a
  candidate may state ``--candidate-events none`` but never leaves it out.
- **A refusal is data.** Every refusal prints ``{"refused": <reason>,
  "message": ...}`` on stdout and exits 2, whichever module raised it.
- **A run cannot be written into the checkout** (``inside_checkout``), and a
  run refuses a design built on another image (``design_revision_mismatch``).
- **The report and the result agree.** ``report.html`` is rendered from the
  cell table embedded in ``result.json`` plus the run facts and the ignition
  timing block it records, so ``render_report(result['cell_table'],
  run=result['run'], timing=result['ignition_timing'])`` rebuilds it.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, get_args

import pandas as pd
from pydantic import ValidationError

from src.analysis.ecu_application import ApplicationModelRefusalError, OverrideRegime
from src.analysis.fuel_comp_inputs import load_fuel_comp_declaration
from src.analysis.fuel_comp_rom import (
    ROM_DIRS_ENV,
    FuelCompRomRefusalError,
    RevisionImageResolver,
    rom_dirs_from_env,
)
from src.analysis.fuel_plant import FuelPlantRefusalError, plant_qualification
from src.analysis.paired_scenarios import (
    LearnedState,
    PairedScenario,
    SimKit,
    TimingState,
)
from src.analysis.rom_edit_spec import (
    BaseImage,
    BaseRef,
    Edit,
    EditSpec,
    build_candidate,
    load_candidate_file,
    load_edit_spec,
    resolve_base,
)
from src.analysis.rom_effects import (
    Candidate,
    EffectReport,
    RomEffectRefusalError,
    compare_roms,
)
from src.analysis.rom_levers import classify_edit
from src.analysis.sim_cells import (
    TOOL_VERSION as CELLS_TOOL_VERSION,
)
from src.analysis.sim_cells import (
    aggregate_cells,
    fuel_comp_axes,
    ignition_timing_block,
    ignition_timing_points,
    target_afr_axes,
)
from src.analysis.sim_contract import (
    EcuIdentity,
    PlantIdentity,
    SimulationRefusalError,
)
from src.analysis.sim_design import (
    SimDesign,
    SimDesignRefusalError,
    build_drive_design,
    build_grid_design,
    load_design,
    require_base,
    summary,
    write_design,
)
from src.analysis.sim_montecarlo import (
    MODE,
    LoadedUncertainty,
    MonteCarloDesign,
    load_sim_kit,
    run_monte_carlo,
)
from src.analysis.sim_montecarlo import TOOL_VERSION as MONTECARLO_TOOL_VERSION
from src.analysis.sim_report import TOOL_VERSION as REPORT_TOOL_VERSION
from src.analysis.sim_report import render_report
from src.paths import REPO_ROOT
from src.signals.derived import FUEL_PROPERTIES
from src.vehicle.tune_revisions import load_tune_revisions

TOOL = "rom_sim"
#: Bump when the result file's shape or meaning changes. 2: runs the
#: simulator's declared plant (sim_kit.yaml, fuel-mv-2) by default, with that
#: plant's own uncertainty declaration (E08.11).
#: 3: an ``ignition_timing`` block (E09.7): ECU-side timing per trim cell and
#: per design point, given a stated HI DET latch and knock-control state.
TOOL_VERSION = "3"

DEFAULT_PROFILE = REPO_ROOT / "data" / "nissan-xterra-2008"
DEFAULT_SESSIONS = Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008"

RESULT_JSON = "result.json"
TRACES_PARQUET = "traces.parquet"
REPORT_HTML = "report.html"

#: The learned state a run starts from, besides the trims the caller states:
#: held equal under both ROMs (the contract's own wording for the closed
#: throttle position and idle air) and purge off (the contract's: purge
#: scheduling is not modelled). Recorded in result.json.
LEARNED_HELD = {"throttle_closed_learned": 0.0, "idle_air_learned": 0.0, "evap_purge_area": 0.0}
#: The source string of the learned state; it names no recording.
LEARNED_SOURCE = "cli: trims stated by --stft-pct and --ltft-pct"
#: The slot the profile declares as the one the ECM runs (fuel_comp_fit.yaml).
SLOT_SOURCE = "default_slot"

GRIDS = ("fuel_comp", "target_afr")
#: The source a command-line timing state carries.
TIMING_SOURCE = "cli: stated by --high-det and --knock-control"


class CliRefusalError(ValueError):
    """An argument the tool needs and was not given; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


#: Every refusal the library layers raise. Each carries a stable ``reason``.
_REFUSALS: tuple[type[Exception], ...] = (
    CliRefusalError,
    RomEffectRefusalError,
    SimulationRefusalError,
    SimDesignRefusalError,
    FuelCompRomRefusalError,
    FuelPlantRefusalError,
    ApplicationModelRefusalError,
)


# -- arguments -----------------------------------------------------------------------------


def _need(args: argparse.Namespace, *names: str) -> None:
    """Refuse (``argument_missing``) an argument the result depends on and the
    caller did not state. argparse is left permissive so the refusal is data."""
    missing = [f"--{n.replace('_', '-')}" for n in names if getattr(args, n) is None]
    if missing:
        raise CliRefusalError(
            "argument_missing",
            f"{' '.join(missing)} must be stated: the result depends on it and it has no default",
        )


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _out_dir(out: Path | None, rom_dirs: Sequence[Path] = ()) -> Path:
    """``--out``, resolved, and refused inside (or around) the checkout or any
    ROM directory (``--rom-dir`` and ``$CANARY_ROM_DIRS``): artifacts never sit
    where real images are kept, as ``write_candidate`` refuses."""
    if out is None:
        raise CliRefusalError("argument_missing", "--out must be stated")
    resolved = Path(out).expanduser().resolve()
    if _inside(resolved, REPO_ROOT) or _inside(REPO_ROOT, resolved):
        raise RomEffectRefusalError(
            "inside_checkout", f"{resolved} is in the checkout; artifacts stay out of Git"
        )
    for directory in rom_dirs:
        library = Path(directory).expanduser().resolve()
        if _inside(resolved, library) or _inside(library, resolved):
            raise RomEffectRefusalError(
                "inside_rom_library",
                f"{resolved} overlaps the ROM directory {library}; simulation artifacts stay "
                "apart from real images",
            )
    return resolved


def _profile(args: argparse.Namespace) -> Path:
    profile = Path(args.profile).expanduser()
    if not profile.is_dir():
        raise CliRefusalError("profile_not_found", f"{profile} is not a vehicle profile directory")
    return profile


def _rom_dirs(args: argparse.Namespace) -> list[Path]:
    return [Path(d).expanduser() for d in args.rom_dir] if args.rom_dir else rom_dirs_from_env()


def _all_rom_dirs(args: argparse.Namespace) -> list[Path]:
    """Every ROM directory the caller names, by flag and by environment: the
    places ``--out`` must stay out of."""
    flagged = [Path(d).expanduser() for d in args.rom_dir or []]
    return [*flagged, *rom_dirs_from_env()]


def _session_path(name: str, root: Path) -> Path:
    """A path (absolute, or with a separator) is itself; a bare name is under ``root``."""
    as_path = Path(name).expanduser()
    return as_path if as_path.is_absolute() or len(as_path.parts) > 1 else root / name


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str, allow_nan=False)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE,
        help="vehicle profile directory (default: the Xterra's, data/nissan-xterra-2008)",
    )
    parser.add_argument(
        "--rom-dir",
        type=Path,
        action="append",
        default=None,
        help=f"directory holding the registry's rom_file names, repeatable (else ${ROM_DIRS_ENV})",
    )
    parser.add_argument(
        "--plant-version",
        default=None,
        help="a frozen plant declaration among the profile's fuel_plant*.yaml that "
        "sim_kit.yaml lists an uncertainty declaration for (default: the simulator's "
        "declared plant, sim_kit.yaml's plant_version; designs default to it too)",
    )


def _add_timing(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(
        "ignition timing (E09.7)",
        "the ECU-side timing output needs both; an edit that changes a timing trim refuses "
        "without them (nothing reads the HI DET latch, so there is no default)",
    )
    group.add_argument(
        "--high-det",
        choices=("set", "clear"),
        help="0xFFFF283F bit 6 (UpRev's HI DET): set, warm timing reads trim B",
    )
    group.add_argument(
        "--knock-control",
        choices=("active", "fallback"),
        help="fallback: the knock-sensor fallback (0xFFFF479D bit 7); it does not imply HI DET",
    )


def _add_candidate(parser: argparse.ArgumentParser) -> None:
    base = parser.add_argument_group(
        "base image",
        "the image the edit starts from: --revision, or --rom with --sha256. With --edits and "
        "none of these, the spec's own base is used; a base that is not the spec's is "
        "base_mismatch.",
    )
    base.add_argument(
        "--base", action="store_true", help="optional marker: --revision/--rom name the base"
    )
    base.add_argument("--revision", help="a tune_revisions.yaml revision, found in the ROM dirs")
    base.add_argument("--rom", type=Path, help="a base image file (.OsirisROM or plain)")
    base.add_argument("--sha256", help="the plain-image sha256 of --rom (required with it)")
    cand = parser.add_argument_group("candidate (exactly one)")
    cand.add_argument("--edits", type=Path, help="an edit spec (E08.1 YAML)")
    cand.add_argument(
        "--candidate-rom", type=Path, help="an already edited image (e.g. saved from UpRev)"
    )


# -- resolution ----------------------------------------------------------------------------


class _World:
    """Everything the subcommands load once: the kit, the slot, the base and the candidate."""

    def __init__(self, args: argparse.Namespace, *, need_candidate: bool = True) -> None:
        self.profile = _profile(args)
        # The simulator's own declared plant (sim_kit.yaml, E08.11), never the
        # contract's default, with that plant's uncertainty declaration.
        loaded = load_sim_kit(self.profile, args.plant_version)
        self.kit: SimKit = loaded.kit
        self.uncertainty: LoadedUncertainty = loaded.uncertainty
        declaration = load_fuel_comp_declaration(self.profile)
        self.slot = declaration.program.map_slot
        registry = load_tune_revisions(self.profile / "tune_revisions.yaml")
        if registry is None:
            raise CliRefusalError("no_tune_registry", f"{self.profile} has no tune_revisions.yaml")
        self.resolver = RevisionImageResolver(
            registry,
            self.kit.rom_map,
            declaration.rom,
            rom_dirs=_rom_dirs(args),
            slot=self.slot,
        )
        if (args.edits is None) == (args.candidate_rom is None):
            raise CliRefusalError(
                "candidate_unstated",
                "give exactly one of --edits (an edit spec) and --candidate-rom (an edited image)",
            )
        self.spec: EditSpec | None = None
        spec_dir = Path.cwd()
        if args.edits is not None:
            if not Path(args.edits).is_file():
                raise RomEffectRefusalError("edit_spec_not_found", f"{args.edits}")
            self.spec = load_edit_spec(args.edits)
            spec_dir = Path(args.edits).resolve().parent
        self.base: BaseImage = resolve_base(
            self._base_ref(args), self.kit.rom_map, spec_dir=spec_dir, resolver=self.resolver
        )
        self.candidate: Candidate | None = None
        if need_candidate:
            self.candidate = self._candidate(args)

    def _base_ref(self, args: argparse.Namespace) -> BaseRef:
        if args.revision is not None and args.rom is not None:
            raise CliRefusalError("base_ambiguous", "give --revision or --rom, not both")
        if args.rom is not None:
            if args.sha256 is None:
                raise CliRefusalError(
                    "argument_missing", "--rom needs --sha256, the plain image's hash"
                )
            return BaseRef(file=str(Path(args.rom).expanduser().resolve()), sha256=args.sha256)
        if args.sha256 is not None:
            raise CliRefusalError("argument_missing", "--sha256 goes with --rom")
        if args.revision is not None:
            return BaseRef(revision=args.revision)
        if self.spec is not None:
            return self.spec.base
        raise CliRefusalError(
            "base_unstated", "give --revision R or --rom PATH --sha256 H (a spec names its own)"
        )

    def _candidate(self, args: argparse.Namespace) -> Candidate:
        common: dict[str, Any] = dict(
            model=self.kit.model,
            model_sha256=self.kit.model_sha256,
            slot=self.slot,
            slot_source=SLOT_SOURCE,
        )
        if self.spec is not None:
            return build_candidate(self.spec, self.kit.rom_map, self.base, **common)
        return load_candidate_file(
            Path(args.candidate_rom).expanduser(), self.kit.rom_map, base=self.base, **common
        )


# -- design ---------------------------------------------------------------------------------


def _design_payload(command: str, design: SimDesign, directory: Path) -> dict[str, Any]:
    return {
        "command": command,
        "directory": str(directory),
        "sha256": design.sha256(),
        "summary": summary(design),
        "assumptions": design.assumptions,
    }


def cmd_design_drives(args: argparse.Namespace) -> dict[str, Any]:
    _need(args, "base_revision", "sessions", "fuel_type", "out")
    out = _out_dir(args.out, _all_rom_dirs(args))
    if args.fuel_type not in FUEL_PROPERTIES:
        raise SimDesignRefusalError(
            "fuel_type_unknown", f"{args.fuel_type!r} is not one of {sorted(FUEL_PROPERTIES)}"
        )
    root = Path(args.sessions_root).expanduser()
    design = build_drive_design(
        [_session_path(name, root) for name in args.sessions],
        _profile(args),
        args.base_revision,
        fuel_type=args.fuel_type,
        rom_dirs=_rom_dirs(args),
        plant_version=args.plant_version,
        battery_v_when_unmeasured=args.battery_v_when_unmeasured,
    )
    return _design_payload("design drives", design, write_design(design, out))


def cmd_design_grid(args: argparse.Namespace) -> dict[str, Any]:
    _need(args, "base_revision", "rpm_edges", "load_edges", "out")
    out = _out_dir(args.out, _all_rom_dirs(args))
    rail: float | str | None = args.rail_kpa
    if isinstance(rail, str) and rail != "working_assumption":
        try:
            rail = float(rail)
        except ValueError as exc:
            raise CliRefusalError(
                "rail_kpa_invalid", f"--rail-kpa is a number or working_assumption, not {rail!r}"
            ) from exc
    design = build_grid_design(
        args.rpm_edges,
        args.load_edges,
        profile_dir=_profile(args),
        base_revision=args.base_revision,
        load_basis=args.load_basis,
        baro_kpa=args.baro_kpa,
        iat_c=args.iat_c,
        coolant_c=args.coolant_c,
        battery_v=args.battery_v,
        rail_kpa=rail,  # type: ignore[arg-type]
        throttle_pct=args.throttle_pct,
        pedal_flag=args.pedal_flag,
        plant_version=args.plant_version,
    )
    return _design_payload("design grid", design, write_design(design, out))


# -- inspect --------------------------------------------------------------------------------


def _cell_rows(world: _World) -> list[dict[str, Any]]:
    """The changed cells, old -> new raw with the change in percent of the old
    value. A masked cell (the timing trims' flag bit) is judged on the bits
    that carry the value, as the ECM reads it."""
    assert world.candidate is not None
    rows: list[dict[str, Any]] = []
    for cell in world.candidate.provenance["cells"]:
        decode = world.kit.rom_map.table(cell["table"]).decode
        mask = None if decode is None else decode.mask
        old, new = int(cell["old"]), int(cell["new"])
        old_v, new_v = (old, new) if mask is None else (old & mask, new & mask)
        rows.append(
            {
                "table": cell["table"],
                "slot": cell["slot"],
                "row": cell["row"],
                "col": cell["col"],
                "address": cell["address"],
                "old": old,
                "new": new,
                "delta_raw": new - old,
                "mask": mask,
                "pct": None if old_v == 0 else round((new_v - old_v) / old_v * 100.0, 6),
            }
        )
    return rows


def _compare(world: _World, candidate: Candidate, *, in_mode: bool) -> EffectReport:
    """``compare_roms`` on the two images: with ``in_mode`` under the run's
    contract mode (where a quantity the mode holds fixed makes an output
    ``unevaluable``), else the plain structural reach."""
    extra: dict[str, Any] = {"contract": world.kit.contract, "mode": MODE} if in_mode else {}
    return compare_roms(
        world.base.image,
        candidate.image,
        world.kit.rom_map,
        world.kit.model,
        world.kit.model_sha256,
        base_slot=world.slot,
        candidate_slot=world.slot,
        slot_source=SLOT_SOURCE,
        **extra,
    )


def _trace(world: _World, candidate: Candidate, *, in_mode: bool) -> dict[str, Any]:
    plain = _compare(world, candidate, in_mode=in_mode).to_dict()
    return {
        "mode": MODE if in_mode else None,
        "outcome": plain["outcome"],
        "reason": plain["reason"],
        "changed_nodes": plain["changed_nodes"],
        "moved_lookups": plain["moved_lookups"],
        "outputs": [
            {
                "output": o["output"],
                "status": o["status"],
                "reasons": o["reasons"],
                "model_status": o["model_status"],
                "path": o["path"],
            }
            for o in plain["outputs"]
        ],
    }


# -- ignition timing (E09.7) ----------------------------------------------------------------


def _timing_state(args: argparse.Namespace) -> TimingState | None:
    """The stated HI DET latch and knock-control state, both or neither."""
    given = (args.high_det, args.knock_control)
    if given == (None, None):
        return None
    if None in given:
        raise CliRefusalError(
            "timing_state_incomplete", "--high-det and --knock-control go together"
        )
    return TimingState(
        high_det=args.high_det == "set", knock_control=args.knock_control, source=TIMING_SOURCE
    )


def cmd_inspect(args: argparse.Namespace) -> dict[str, Any]:
    world = _World(args)
    candidate = world.candidate
    assert candidate is not None
    prov = candidate.provenance
    cells = _cell_rows(world)
    # The lever and what blocks a number: classified once, in
    # src.analysis.rom_levers, on the report a run would use. The ROM Editor
    # shows the same ``messages`` (E09.14).
    levers = classify_edit(
        _compare(world, candidate, in_mode=True),
        rom_map=world.kit.rom_map,
        model=world.kit.model,
        contract=world.kit.contract,
        qualification=plant_qualification(world.kit.plant),
        regime=args.regime,
        cells=prov["cells"],
    )
    return {
        "command": "inspect",
        "ran": "nothing: no design, no draws, no plant",
        "label": prov["label"],
        "route": prov["route"],
        "base": prov["base"],
        "candidate_sha256": prov["candidate_sha256"],
        "spec_sha256": prov["spec_sha256"],
        "edits": prov["spec_edits"],
        "candidate_file": prov["candidate_file"],
        "cells_changed": len(cells),
        "cells": cells,
        "ranges": prov["ranges"],
        "reached": _trace(world, candidate, in_mode=False),
        "in_run_mode": _trace(world, candidate, in_mode=True),
        "levers": levers.to_dict(),
        "ignition_timing": ignition_timing_block(
            world.kit,
            world.base.image,
            candidate.image,
            _timing_state(args),
            slot=world.slot,
            coolant_c=args.timing_coolant_c,
            all_cells=False,
        ),
    }


# -- run ------------------------------------------------------------------------------------


def _selector_text(edit: Edit) -> str:
    """The selector as the operator wrote it (a long cell list is counted)."""
    if edit.cells is not None:
        listed = [list(c) for c in edit.cells]
        return (
            f"cells {json.dumps(listed)}" if len(listed) <= 12 else f"cells ({len(listed)} listed)"
        )
    if edit.region is not None:
        bounds = edit.region.bounds()
        parts = [
            f"{axis} {lo:g}..{hi:g} {edit.region.units[axis]}" for axis, (lo, hi) in bounds.items()
        ]
        return "region " + ", ".join(parts)
    return "all"


def _operation_text(edit: Edit) -> str:
    if edit.scale is not None:
        return f"scale: {edit.scale:g} (rounding: {edit.rounding})"
    if edit.add_raw is not None:
        return f"add_raw: {edit.add_raw}"
    return f"set_raw: {edit.set_raw}"


def edit_rows(provenance: Mapping[str, Any], spec: EditSpec | None) -> list[dict[str, Any]]:
    """The edit list in the report's shape: one row per spec edit (selector and
    operation as written, cells changed and left alone, from E08.1's records),
    or, for an edited image (no selector exists), one row per table it changed."""
    if provenance["route"] == "edit_spec":
        assert spec is not None
        return [
            {
                "table": e["table"],
                "slot": e["slot"],
                "selector": _selector_text(spec.edits[e["index"]]),
                "operation": _operation_text(spec.edits[e["index"]]),
                "changed": len(e["changed"]),
                "unchanged": len(e["unchanged"]),
            }
            for e in provenance["spec_edits"]
        ]
    counts: dict[tuple[str, int | None], int] = {}
    for cell in provenance["cells"]:
        key = (cell["table"], cell["slot"])
        counts[key] = counts.get(key, 0) + 1
    rows = [
        {
            "table": table,
            "slot": slot,
            "selector": None,
            "operation": None,
            "changed": n,
            "unchanged": 0,
        }
        for (table, slot), n in sorted(counts.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0))
    ]
    # Changed bytes outside any table (the checksum words, unmapped bytes): the
    # image changed there too, whatever the cell list says.
    tables = {t for t, _ in counts}
    for r in provenance["ranges"]:
        named = {w.split(" (")[0] for w in r["what"]}
        if r["byte_class"] in ("checksum", "unknown") or not named & tables:
            rows.append(
                {
                    "table": f"{r['byte_class']} bytes 0x{r['start']:X}-0x{r['end']:X}",
                    "slot": None,
                    "selector": None,
                    "operation": None,
                    "changed": int(r["changed"]),
                    "unchanged": 0,
                }
            )
    return rows


def _candidate_events(args: argparse.Namespace) -> tuple[str, ...]:
    _need(args, "candidate_events")
    events = list(args.candidate_events)
    if "none" in events:
        if events != ["none"]:
            raise CliRefusalError(
                "candidate_events_ambiguous", "none is the whole answer; do not list events with it"
            )
        return ()
    return tuple(events)


def _template(
    world: _World, args: argparse.Namespace, events: tuple[str, ...], candidate_sha: str
) -> PairedScenario:
    kit = world.kit
    program = kit.model.program.id

    def identity(sha: str) -> EcuIdentity:
        return EcuIdentity(
            program_id=program, image_sha256=sha, slot=world.slot, slot_source="default_slot"
        )

    return PairedScenario(
        mode=MODE,
        base=identity(world.base.image_sha256),
        candidate=identity(candidate_sha),
        plant=PlantIdentity(
            version=kit.plant.plant_version,
            hardware_configuration=kit.plant.hardware_configuration,
        ),
        points=(),
        learned=LearnedState(
            stft_pct=float(args.stft_pct),
            ltft_pct=float(args.ltft_pct),
            throttle_closed_learned=LEARNED_HELD["throttle_closed_learned"],
            idle_air_learned=LEARNED_HELD["idle_air_learned"],
            evap_purge_area=LEARNED_HELD["evap_purge_area"],
            source=LEARNED_SOURCE,
        ),
        candidate_events=events,
    )


def _publish(out: Path, texts: Mapping[str, str], traces: pd.DataFrame) -> None:
    """Write every output to a temporary name, then move them all into place.

    A failure at any step leaves nothing of this run in ``out``: the staged
    files are removed, and so is anything already moved."""
    out.mkdir(parents=True, exist_ok=True)
    staged: dict[str, Path] = {}
    moved: list[Path] = []
    try:
        for name, text in texts.items():
            staged[name] = out / f"{name}.tmp"
            staged[name].write_text(text)
        staged[TRACES_PARQUET] = out / f"{TRACES_PARQUET}.tmp"
        traces.to_parquet(staged[TRACES_PARQUET], index=False)
        for name, tmp in staged.items():
            tmp.replace(out / name)
            moved.append(out / name)
    except BaseException:
        for leftover in [*staged.values(), *moved]:
            leftover.unlink(missing_ok=True)
        raise


def cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    _need(args, "draws", "seed", "stft_pct", "ltft_pct", "design", "out")
    events = _candidate_events(args)
    out = _out_dir(args.out, _all_rom_dirs(args))
    started = time.monotonic()
    world = _World(args)
    candidate = world.candidate
    assert candidate is not None
    kit, base = world.kit, world.base
    design = load_design(args.design)
    require_base(design, base.image_sha256)
    uncertainty = world.uncertainty
    template = _template(world, args, events, candidate.image_sha256)
    result = run_monte_carlo(
        kit,
        template,
        MonteCarloDesign.from_sim_design(design),
        base.image,
        candidate.image,
        uncertainty,
        draws=args.draws,
        seed=args.seed,
    )
    effects = compare_roms(
        base.image,
        candidate.image,
        kit.rom_map,
        kit.model,
        kit.model_sha256,
        base_slot=world.slot,
        candidate_slot=world.slot,
        slot_source=SLOT_SOURCE,
        contract=kit.contract,
        mode=MODE,
    )
    axes = (
        fuel_comp_axes(kit, base.image, slot=world.slot)
        if args.grid == "fuel_comp"
        else target_afr_axes(kit, base.image, slot=world.slot)
    )
    table = aggregate_cells(result, design, axes, effects=effects)
    state = _timing_state(args)
    # The cells are read at the design's coldest point: every point the
    # plant accepts is warm, and a design with a point in the trims' blend
    # band shows it per cell.
    timing = ignition_timing_block(
        kit,
        base.image,
        candidate.image,
        state,
        slot=world.slot,
        coolant_c=min(p.coolant_c for p in design.points),
        all_cells=True,
    )
    timing["points"] = ignition_timing_points(
        kit, result, design, base.image, candidate.image, state, slot=world.slot
    )
    prov = candidate.provenance
    run_facts: dict[str, Any] = {
        "label": prov["label"],
        "seed": args.seed,
        "base": {"label": base.label, "sha256": base.image_sha256},
        "candidate": {"label": prov["label"], "sha256": candidate.image_sha256},
        "edit_route": prov["route"],
        "spec_sha256": prov["spec_sha256"],
        "edits": edit_rows(prov, world.spec),
        "design": summary(design),
        "plant_version": result.provenance["plant_version"],
        "uncertainty_sha256": result.provenance["uncertainty_sha256"],
    }
    traces = result.traces()
    document = {
        "tool": TOOL,
        "tool_version": TOOL_VERSION,
        "kind": "simulation_result",
        "label": "simulation",
        "synthetic": True,
        "versions": {
            "sim_montecarlo": MONTECARLO_TOOL_VERSION,
            "sim_cells": CELLS_TOOL_VERSION,
            "sim_report": REPORT_TOOL_VERSION,
        },
        "result_digest": result.digest(),
        "traces_session": result.session,
        "files": {"result": RESULT_JSON, "traces": TRACES_PARQUET, "report": REPORT_HTML},
        "inputs": {
            "profile": world.profile.name,
            "base": prov["base"],
            "candidate_sha256": candidate.image_sha256,
            "design_sha256": design.sha256(),
            "design_directory_name": Path(args.design).name,
            "plant_version": result.provenance["plant_version"],
            "uncertainty_sha256": result.provenance["uncertainty_sha256"],
            "seed": args.seed,
            "draws": args.draws,
            "grid": args.grid,
            "slot": world.slot,
            "slot_source": SLOT_SOURCE,
            "candidate_events": list(events),
            "learned": {
                "stft_pct": args.stft_pct,
                "ltft_pct": args.ltft_pct,
                "source": LEARNED_SOURCE,
                "held": LEARNED_HELD,
            },
        },
        "caveats": result.caveats,
        "unresolved": result.unresolved,
        "assumptions": result.assumptions,
        "inert_parameters": result.inert_parameters,
        "coverage": result.coverage,
        "monte_carlo": result.provenance,
        "candidate": prov,
        "run": run_facts,
        "cell_table": table.to_dict(),
        "ignition_timing": timing,
    }
    html = render_report(table.to_json(), run=run_facts, timing=timing)
    try:
        text = _json(document) + "\n"
    except ValueError as exc:
        raise CliRefusalError(
            "result_not_finite", f"the result holds a value JSON cannot carry: {exc}"
        ) from exc
    _publish(out, {RESULT_JSON: text, REPORT_HTML: html}, traces)
    return {
        "command": "run",
        "directory": str(out),
        "files": {name: str(out / name) for name in (RESULT_JSON, TRACES_PARQUET, REPORT_HTML)},
        "result_digest": document["result_digest"],
        "traces_session": document["traces_session"],
        "design_sha256": design.sha256(),
        "base_sha256": base.image_sha256,
        "candidate_sha256": candidate.image_sha256,
        "n_points": result.n_points,
        "draws": result.draws,
        "status_counts": table.status_counts,
        "weight_total": table.weight_total,
        "caveats": result.caveats,
        "ignition_timing": {
            k: timing[k]
            for k in (
                "output",
                "trims_changed",
                "other_inputs_changed",
                "summary",
                "status",
                "reasons",
            )
            if k in timing
        },
        "wall_seconds": round(time.monotonic() - started, 2),
    }


# -- command line ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rom_sim",
        description=(__doc__ or "").split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="subcommands:\n"
        "  design drives  pre-change drives -> a frozen, hashed point design\n"
        "  design grid    a stated rpm x load grid -> the same kind of design\n"
        "  inspect        resolved cell edits (old -> new raw, +/- %), the outputs they reach,\n"
        "                 and what blocks a number per output; runs nothing\n"
        "  run            paired Monte Carlo -> result.json, traces.parquet, report.html\n\n"
        "Run `rom_sim <subcommand> --help` (`rom_sim design drives --help`) for arguments.\n"
        "Refusals print JSON with a stable `reason` and exit 2. Output goes under --out,\n"
        "never inside the checkout.",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="subcommand")

    design = commands.add_parser(
        "design",
        help="build a frozen operating-point design (drives or grid)",
        description="Build a frozen, hashed operating-point design and write it under --out.",
    )
    kinds = design.add_subparsers(dest="kind", required=True, metavar="kind")

    drives = kinds.add_parser(
        "drives",
        help="a design from pre-change drives, all on the base revision",
        description="Warm steady reads from drives recorded on --base-revision become design "
        "points, weighted by dwell time. Writes <out>/<sha256>/design.json + design.parquet.",
    )
    _add_common(drives)
    drives.add_argument("--base-revision", help="the tune_revisions.yaml revision (required)")
    drives.add_argument(
        "--sessions",
        nargs="+",
        help="session names under --sessions-root, or paths with a separator (required)",
    )
    drives.add_argument(
        "--sessions-root",
        type=Path,
        default=DEFAULT_SESSIONS,
        help="where bare session names are found (default: the laptop's ~/canary-data copy)",
    )
    drives.add_argument(
        "--fuel-type", choices=sorted(FUEL_PROPERTIES), help="the vehicle's fuel (required)"
    )
    drives.add_argument(
        "--battery-v-when-unmeasured",
        type=float,
        help="volts to place a read with no module-voltage read at (required when a drive has "
        "one; recorded in the design)",
    )
    drives.add_argument("--out", type=Path, help="scratch directory, not the repo (required)")
    drives.set_defaults(handler=cmd_design_drives)

    grid = kinds.add_parser(
        "grid",
        help="a design from a stated rpm x load grid (no drives)",
        description="One point per cell centre of an rpm x load grid in Fuel Comp's axis units. "
        "Every environment value is stated; none has a default.",
    )
    _add_common(grid)
    grid.add_argument("--base-revision", help="the tune_revisions.yaml revision (required)")
    grid.add_argument("--rpm-edges", nargs="+", type=float, help="rpm cell edges (required)")
    grid.add_argument(
        "--load-edges",
        nargs="+",
        type=float,
        help="load edges, in %% (0-100), Fuel Comp's column axis unit (required)",
    )
    grid.add_argument(
        "--load-basis",
        help="how a fill-%% load becomes airflow (required): fill_fraction_as_manifold_ratio",
    )
    grid.add_argument("--baro-kpa", type=float, help="barometric pressure (required)")
    grid.add_argument("--iat-c", type=float, help="intake air temperature (required)")
    grid.add_argument("--coolant-c", type=float, help="coolant temperature (required)")
    grid.add_argument("--battery-v", type=float, help="module voltage (required)")
    grid.add_argument(
        "--rail-kpa", help="fuel rail pressure, or the word working_assumption (required)"
    )
    grid.add_argument("--throttle-pct", type=float, help="throttle position (required)")
    grid.add_argument("--pedal-flag", type=int, help="the pedal-enrichment flag byte (required)")
    grid.add_argument("--out", type=Path, help="scratch directory, not the repo (required)")
    grid.set_defaults(handler=cmd_design_grid)

    inspect = commands.add_parser(
        "inspect",
        help="what an edit changes, without running anything",
        description="Resolve the candidate and print its changed cells (old -> new raw, change "
        "in %) and the outputs compare_roms says they reach. No design, no draws, no plant.",
    )
    _add_common(inspect)
    _add_candidate(inspect)
    inspect.add_argument(
        "--regime",
        choices=list(get_args(OverrideRegime)),
        default=None,
        help="the operating regime the edit is judged in (optional). Only the closed-loop "
        "blocker (trim authority undecoded) is regime-aware: it is listed for closed_loop, "
        "and when no regime is stated since most of a drive is closed loop. Other regimes' "
        "unqualified overrides and the plant's regime_not_qualified are not consulted.",
    )
    _add_timing(inspect)
    inspect.add_argument(
        "--timing-coolant-c",
        type=float,
        help="coolant temperature the timing cells are read at (required with --high-det)",
    )
    inspect.set_defaults(handler=cmd_inspect)

    run = commands.add_parser(
        "run",
        help="a paired Monte Carlo over a design: result.json, traces.parquet, report.html",
        description="Run both ROMs over every design point and draw (common random numbers), "
        "aggregate per Fuel Comp cell, and write result.json (provenance, caveats, the cell "
        "table), traces.parquet (a _sim-* session) and report.html under --out.",
    )
    _add_common(run)
    _add_candidate(run)
    run.add_argument("--design", type=Path, help="a design directory <root>/<sha256> (required)")
    run.add_argument("--draws", type=int, help="Monte Carlo draws (required)")
    run.add_argument("--seed", type=int, help="the generator's seed (required)")
    run.add_argument(
        "--stft-pct", type=float, help="the learned short-term trim the base starts from (required)"
    )
    run.add_argument(
        "--ltft-pct", type=float, help="the learned long-term trim the base starts from (required)"
    )
    run.add_argument(
        "--candidate-events",
        nargs="+",
        help="what the candidate implies for the run, e.g. ecu_reflash (resets the learned "
        "trim), or `none` (required)",
    )
    run.add_argument(
        "--grid",
        choices=GRIDS,
        default="fuel_comp",
        help="the cell grid: Fuel Comp's own axes (default) or Target AFR's",
    )
    _add_timing(run)
    run.add_argument("--out", type=Path, help="scratch directory, not the repo (required)")
    run.set_defaults(handler=cmd_run)
    return parser


def _finite(args: argparse.Namespace) -> None:
    """Refuse a non-finite number (``nan``, ``inf``) in any numeric argument:
    argparse's ``float`` accepts them and they would reach a result file."""
    for name, value in sorted(vars(args).items()):
        values = value if isinstance(value, list) else [value]
        bad = [v for v in values if isinstance(v, float) and not math.isfinite(v)]
        if bad:
            raise CliRefusalError(
                "argument_invalid",
                f"--{name.replace('_', '-')} must be finite numbers, not {bad}",
            )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _finite(args)
        payload = args.handler(args)
    except _REFUSALS as exc:
        reason = str(getattr(exc, "reason", type(exc).__name__))
        print(_json({"refused": reason, "message": str(getattr(exc, "message", exc))}))
        return 2
    except ValidationError as exc:
        print(_json({"refused": "invalid_input", "message": str(exc)}))
        return 2
    print(_json(payload))
    return 0


__all__ = ["edit_rows", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
