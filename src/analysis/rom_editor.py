"""The ROM Editor's preview and simulate, as pure functions (W09.1).

The dashboard's ROM Editor page edits the fuel chain of a ROM Library image
and runs the edit through the E08 simulator. Everything it does is here, on
plain data (image bytes, the library's ``revision_match`` dict, JSON-shaped
edits), so the route (W09.2), the workbench child and the tests run the same
code:

* :func:`preview_edits` -- the E08.1 edit spec the page's edits make, the
  candidate's provenance (cells old -> new, changed ranges, reach), the
  edited tables' after values, and ``levers`` (E09.14b): which lever the edit
  pulls and what blocks a number for each output, from
  :func:`src.analysis.rom_levers.classify_edit` (the strings
  ``tools/rom_sim.py inspect`` prints; this module words none of them). Cheap:
  the order of ``GET .../tables``, plus the simulator kit's load.
* :func:`simulate_edits` -- a stated rpm x load grid (E08.2), paired Monte
  Carlo draws (E08.3) and per-cell aggregation (E08.4), base vs candidate.
* :func:`run_job` -- the workbench child's entry point.

What it makes impossible rather than documented:

* **A GUI edit is an E08.1 edit.** The request's ``edits`` are
  :class:`~src.analysis.rom_edit_spec.Edit` dicts, wrapped in an
  :class:`~src.analysis.rom_edit_spec.EditSpec` by :func:`editor_spec`;
  every E08.1 refusal keeps its reason. Only Map 1's fuel chain and timing
  trims are editable (:data:`EDITABLE_TABLES`, :data:`SIM_SLOT`): the simulator runs
  slot 1 as the ECM does after a cold boot, so an edit elsewhere is refused
  (``table_not_editable``, ``slot_not_simulated``) instead of showing
  ``not_reached`` everywhere.
* **The candidate never leaves the simulator.** Nothing here writes a file;
  the candidate's bytes are hashed and simulated, never returned. What a
  caller can keep is the edit spec YAML (``spec_yaml``).
* **The simulator needs a recorded base** (``base_not_a_recorded_revision``):
  a grid design names its ``tune_revisions.yaml`` revision.
* **Nothing is defaulted.** :class:`SimRequest` has no field with a default:
  environment, learned state, candidate events, load basis, draws and seed
  are all stated (E08.2's ``environment_unstated``, E08.3's draws and seed).
  The timing output (E09.7) reads the HI DET latch and knock-control state,
  which nothing logs, so the request carries both (``high_det``,
  ``knock_control``) and a missing one is a 422 naming it. They are never
  typical values either (:data:`NEVER_TYPICAL_FIELDS`): a latch state is a
  condition of this run, and one filled in as "typical" would outlive the
  fault that set it.
* **Bounded before any work:** a grid over :data:`MAX_GRID_POINTS` cells
  (``grid_too_large``) or more than :data:`MAX_DRAWS` draws
  (``draws_too_many``) is refused before a design is built.
* **Refusals cross the process boundary as data.** The simulator's refusal
  classes take ``(reason, message)`` and do not survive pickling, so
  :func:`run_job` returns ``{"refused": {"kind", "reason", "message"}}``
  instead of raising them out of the child.

Nothing here, or anything it imports, can reach the ECU: see
``tests/test_rom_editor.py``'s clean-process import test.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from src.analysis.ecu_application import ApplicationModel, ApplicationModelRefusalError
from src.analysis.fuel_plant import FuelPlantRefusalError, plant_qualification
from src.analysis.paired_scenarios import (
    KnockControl,
    LearnedState,
    PairedScenario,
    SimKit,
    TimingState,
)
from src.analysis.rom_edit_spec import (
    BaseImage,
    EditSpec,
    EditSpecRefusalError,
    build_candidate,
    parse_edit_spec,
)
from src.analysis.rom_effects import EffectReport, RomEffectRefusalError, compare_roms
from src.analysis.rom_image import ImageFormatError, sha256_hex, table_values
from src.analysis.rom_levers import classify_edit
from src.analysis.rom_map import RomMap
from src.analysis.sim_cells import (
    Grid,
    aggregate_cells,
    fuel_comp_axes,
    ignition_timing_block,
    ignition_timing_points,
    target_afr_axes,
)
from src.analysis.sim_contract import EcuIdentity, PlantIdentity, SimulationRefusalError
from src.analysis.sim_design import (
    LoadBasis,
    SimDesign,
    SimDesignRefusalError,
    build_grid_design,
    require_base,
    summary,
)
from src.analysis.sim_montecarlo import (
    MODE,
    LoadedUncertainty,
    MonteCarloDesign,
    load_sim_kit,
    run_monte_carlo,
)
from src.vehicle.tune_revisions import TuneRevisionRegistry
from src.yaml_load import load_yaml

#: Bump when a payload's meaning changes.
TOOL_VERSION = "1"
TOOL = "rom_editor"

#: The fuel chain and the timing trims, Map 1 (W09 "Decisions"; trims E09.7):
#: every other table is refused.
EDITABLE_TABLES: tuple[str, ...] = (
    "fuel_targets",
    "fuel_compensation",
    "k_constant",
    "timing_trim_a",
    "timing_trim_b",
)
#: The slot the simulator runs: the one the ECM runs after a cold boot.
SIM_SLOT = 1
SLOT_SOURCE: Literal["default_slot"] = "default_slot"
#: Grid cells and draws a simulate request may ask for (W09.6 measures them).
MAX_GRID_POINTS = 1024
MAX_DRAWS = 2000

#: Every :class:`LearnedState` the editor builds says where it came from.
LEARNED_SOURCE = "stated: ROM Editor"
#: So does the :class:`TimingState` (``tools/rom_sim.py`` says ``cli: ...``).
TIMING_SOURCE = "stated: ROM Editor"
#: Why a base that is not a flashed revision cannot be simulated.
BASE_NOT_RECORDED = "base_not_a_recorded_revision"

#: The child's job kinds (W09.2 dispatches ``rom_sim``).
JOB_KINDS: tuple[str, ...] = ("rom_sim", "rom_preview")

#: The refusal classes :func:`run_job` returns as data, most specific first
#: (an :class:`EditSpecRefusalError` is a :class:`RomEffectRefusalError`).
REFUSALS: tuple[tuple[type[ValueError], str], ...] = (
    (EditSpecRefusalError, "edit_spec"),
    (RomEffectRefusalError, "rom_effect"),
    (SimDesignRefusalError, "sim_design"),
    (SimulationRefusalError, "simulation"),
    # Same (reason, message) signature, same pickling trap, raised from the plant
    # and the application model beneath the paired run.
    (FuelPlantRefusalError, "fuel_plant"),
    (ApplicationModelRefusalError, "application_model"),
    # A plain ValueError carrying only its message (no reason of its own).
    (ImageFormatError, "image_format"),
)

_REFUSAL_TYPES = tuple(cls for cls, _ in REFUSALS)

_SPEC_HEADER = (
    "# CANary ROM Editor edit spec (E08.1). A simulation input only: the candidate it\n"
    "# makes is never a flashable image. Runs through the simulator CLI (E08.5).\n"
)


# -- the base -------------------------------------------------------------------------------


def library_base(
    image: bytes, revision_match: Mapping[str, Any], *, sha256: str | None = None
) -> BaseImage:
    """A ROM Library image as an E08.1 base.

    A ``matched`` entry is its revision (``source: revision``), so the spec
    names ``{revision: R}`` and resolves through ``tune_revisions.yaml`` on
    any machine. Anything else (``reference``, ``unrecognised``, ``unknown``)
    is the library file by its plain sha256. ``sha256``, when given (the
    library key), must be the bytes' own (``image_hash_mismatch``)."""
    digest = sha256_hex(image)
    if sha256 is not None and sha256 != digest:
        raise EditSpecRefusalError(
            "image_hash_mismatch", f"the image bytes are {digest}, not the library's {sha256}"
        )
    file_name = f"{digest}.bin"
    if revision_match.get("status") == "matched":
        revision = revision_match.get("revision")
        if not isinstance(revision, str) or not revision:
            raise EditSpecRefusalError(
                "revision_match_invalid", f"a matched entry names no revision: {revision_match}"
            )
        return BaseImage(
            image=image,
            identity={
                "source": "revision",
                "revision": revision,
                "file": file_name,
                "image_sha256": digest,
                "image_format": "plain",
                "container_sha256": None,
            },
        )
    return BaseImage(
        image=image,
        identity={
            "source": "file",
            "revision": None,
            "file": file_name,
            "image_sha256": digest,
            "image_format": "plain",
            "container_sha256": None,
        },
    )


def _base_ref(base: BaseImage) -> dict[str, Any]:
    if base.identity.get("source") == "revision":
        return {"revision": base.identity["revision"]}
    return {"file": base.identity["file"], "sha256": base.image_sha256}


def sim_base(revision_match: Mapping[str, Any]) -> dict[str, Any]:
    """Whether the base can be simulated, and why not."""
    status = revision_match.get("status")
    ok = status == "matched" and bool(revision_match.get("revision"))
    return {
        "ok": ok,
        "status": status,
        "revision": revision_match.get("revision") if ok else None,
        "reason": None if ok else BASE_NOT_RECORDED,
    }


# -- the edit spec --------------------------------------------------------------------------


def _editable(spec: EditSpec) -> None:
    """Map 1 of the fuel chain only. The slot is checked first: a table's
    other-slot placement (``fuel_targets_maps_2_5``) and K in slot 3 are the
    same refusal, an edit the simulator would never read."""
    for index, edit in enumerate(spec.edits):
        if edit.slot is not None and edit.slot != SIM_SLOT:
            raise EditSpecRefusalError(
                "slot_not_simulated",
                f"edit {index} ({edit.table} slot {edit.slot}): the simulator runs slot "
                f"{SIM_SLOT} ({SLOT_SOURCE}, the slot the ECM runs after a cold boot), so an "
                "edit to another slot reaches nothing it computes",
            )
        if edit.table not in EDITABLE_TABLES:
            raise EditSpecRefusalError(
                "table_not_editable",
                f"edit {index}: {edit.table!r} is not editable here; the ROM Editor edits "
                f"{list(EDITABLE_TABLES)} in slot {SIM_SLOT}",
            )


def editor_spec(base: BaseImage, label: str, edits: Any) -> EditSpec:
    """The page's ``edits`` on ``base`` as an E08.1 spec, through
    :func:`parse_edit_spec` (its refusals keep their names), refused before
    resolution when an edit is outside :data:`EDITABLE_TABLES` slot 1."""
    spec = parse_edit_spec(
        {"schema_version": 1, "base": _base_ref(base), "label": label, "edits": edits}
    )
    _editable(spec)
    return spec


def spec_yaml(spec: EditSpec) -> str:
    """The spec as the YAML a caller downloads: :func:`load_edit_spec` gives
    back the same :meth:`EditSpec.sha256`."""
    body = yaml.safe_dump(
        spec.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True
    )
    return _SPEC_HEADER + body


# -- shared ---------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _EditContext:
    rom_map: RomMap
    model: ApplicationModel
    model_sha256: str

    @classmethod
    def of(cls, kit: SimKit) -> _EditContext:
        return cls(kit.rom_map, kit.model, kit.model_sha256)


def _sim_kit(profile_dir: Path, kit: SimKit | None) -> SimKit:
    """The kit an edit resolves, traces and is classified through: the
    caller's (tests on a synthetic image), else the simulator's own, the one
    ``sim_kit.yaml`` declares (:func:`load_sim_kit`, the loader
    ``tools/rom_sim.py`` and :func:`simulate_edits` use)."""
    return kit if kit is not None else load_sim_kit(profile_dir).kit


def _effects(base_image: bytes, candidate_image: bytes, kit: SimKit) -> EffectReport:
    """``compare_roms`` on the two images under the run's contract mode, as the
    simulator's ``rom_report`` has it. Where a quantity the mode holds fixed
    makes an output ``unevaluable``, this is the report that says so."""
    return compare_roms(
        base_image,
        candidate_image,
        kit.rom_map,
        kit.model,
        kit.model_sha256,
        base_slot=SIM_SLOT,
        candidate_slot=SIM_SLOT,
        slot_source=SLOT_SOURCE,
        contract=kit.contract,
        mode=MODE,
    )


def _candidate(base: BaseImage, spec: EditSpec, ctx: _EditContext) -> tuple[bytes, dict[str, Any]]:
    made = build_candidate(
        spec,
        ctx.rom_map,
        base,
        model=ctx.model,
        model_sha256=ctx.model_sha256,
        slot=SIM_SLOT,
        slot_source=SLOT_SOURCE,
    )
    return made.image, made.provenance


# -- preview --------------------------------------------------------------------------------


def preview_edits(
    image: bytes,
    revision_match: Mapping[str, Any],
    label: str,
    edits: Any,
    profile_dir: Path,
    *,
    sha256: str | None = None,
    kit: SimKit | None = None,
) -> dict[str, Any]:
    """The spec, the candidate's provenance, the edited tables' after values,
    and ``levers``: which lever the edit pulls and what blocks a number for
    each output (E09.14b). ``levers`` is
    :meth:`~src.analysis.rom_levers.LeverClassification.to_dict` of
    :func:`~src.analysis.rom_levers.classify_edit` on the comparison the run
    would use, built the way ``tools/rom_sim.py inspect`` builds it, so
    ``levers["messages"]`` is the CLI's text for the same edit and nothing
    here words it. ``kit`` replaces the profile's simulator kit (tests on a
    synthetic image)."""
    base = library_base(image, revision_match, sha256=sha256)
    spec = editor_spec(base, label, edits)
    kit = _sim_kit(Path(profile_dir), kit)
    candidate_image, provenance = _candidate(base, spec, _EditContext.of(kit))
    edited = {(e.table, e.slot) for e in spec.edits}
    levers = classify_edit(
        _effects(base.image, candidate_image, kit),
        rom_map=kit.rom_map,
        model=kit.model,
        contract=kit.contract,
        qualification=plant_qualification(kit.plant),
        cells=provenance["cells"],
    )
    return {
        "tool": TOOL,
        "tool_version": TOOL_VERSION,
        "spec": spec.model_dump(mode="json"),
        "spec_yaml": spec_yaml(spec),
        "spec_sha256": spec.sha256(),
        "candidate_sha256": sha256_hex(candidate_image),
        "provenance": provenance,
        "tables": [
            t for t in table_values(candidate_image, kit.rom_map) if (t["id"], t["slot"]) in edited
        ],
        "levers": levers.to_dict(),
        "sim_base": sim_base(revision_match),
    }


# -- simulate -------------------------------------------------------------------------------

_Finite = Annotated[float, Field(allow_inf_nan=False)]


class SimRequest(BaseModel):
    """Everything a simulate states. No field has a default (W09 "Decisions")."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1)
    #: E08.1 ``Edit`` dicts (``rom_edit_spec``), validated there.
    edits: list[dict[str, Any]]
    #: The aggregation axes (E08.4): Fuel Comp's own, or Target AFR's.
    grid: Grid
    rpm_edges: list[_Finite]
    #: In Fuel Comp's load axis decode units (% of the fill model's ceiling).
    load_edges: list[_Finite]
    load_basis: LoadBasis
    baro_kpa: _Finite
    iat_c: _Finite
    coolant_c: _Finite
    battery_v: _Finite
    #: A stated rail pressure, or the plant's working assumption, said so.
    rail_kpa: _Finite | Literal["working_assumption"]
    throttle_pct: _Finite
    pedal_flag: StrictInt = Field(ge=0, le=255)
    stft_pct: _Finite
    ltft_pct: _Finite
    throttle_closed_learned: _Finite
    idle_air_learned: _Finite
    evap_purge_area: _Finite
    #: The ignition chain's two stated states (E09.7), which nothing logs:
    #: ``set`` is the HI DET latch 0xFFFF283F bit 6 (warm timing reads trim B,
    #: else A) and ``fallback`` is the knock controller's sensor-fault fallback
    #: 0xFFFF479D bit 7. ``fallback`` with ``clear`` is refused
    #: (``high_det_forced_by_ks_fault``: the KS fault sets the latch every pass
    #: in states 2-4 of 0x027CC4, [inferred] engine running); the other three
    #: pairings are states
    #: (:class:`~src.analysis.paired_scenarios.TimingState`).
    high_det: Literal["set", "clear"]
    knock_control: KnockControl
    #: ``none``: both ROMs start from the same learned state; ``ecu_reflash``:
    #: the flash resets what the contract says it resets on the candidate.
    candidate_events: Literal["none", "ecu_reflash"]
    draws: StrictInt = Field(ge=1)
    seed: StrictInt = Field(ge=0)

    @property
    def grid_points(self) -> int:
        return max(0, len(self.rpm_edges) - 1) * max(0, len(self.load_edges) - 1)

    @property
    def events(self) -> tuple[str, ...]:
        return () if self.candidate_events == "none" else (self.candidate_events,)

    @property
    def timing_state(self) -> TimingState:
        return TimingState(
            high_det=self.high_det == "set",
            knock_control=self.knock_control,
            source=TIMING_SOURCE,
        )


def _bounded(request: SimRequest) -> None:
    if request.grid_points > MAX_GRID_POINTS:
        raise SimulationRefusalError(
            "grid_too_large",
            f"{len(request.rpm_edges) - 1} x {len(request.load_edges) - 1} = "
            f"{request.grid_points} cells; at most {MAX_GRID_POINTS}",
        )
    if request.draws > MAX_DRAWS:
        raise SimulationRefusalError(
            "draws_too_many", f"{request.draws} draws; at most {MAX_DRAWS}"
        )


def _recorded(base: BaseImage) -> str:
    """The base's revision, or the refusal: a design names a recorded revision."""
    if base.identity.get("source") != "revision" or not base.identity.get("revision"):
        raise SimulationRefusalError(
            BASE_NOT_RECORDED,
            f"{base.label} is not a flashed revision in tune_revisions.yaml (a reference or "
            "unrecognised image): a grid design names the revision its base is, so this base "
            "can be previewed but not simulated",
        )
    return str(base.identity["revision"])


def _uncertainty(profile_dir: Path, kit: SimKit) -> LoadedUncertainty:
    """The uncertainty declaration of the kit's plant (E08.11: sim_kit.yaml
    names one per plant version; load_sim_kit checks it against the plant)."""
    return load_sim_kit(profile_dir, kit.plant.plant_version).uncertainty


def _template(
    kit: SimKit, base_sha: str, candidate_sha: str, request: SimRequest
) -> PairedScenario:
    def identity(sha: str) -> EcuIdentity:
        return EcuIdentity(
            program_id=kit.model.program.id,
            image_sha256=sha,
            slot=SIM_SLOT,
            slot_source=SLOT_SOURCE,
        )

    return PairedScenario(
        mode=MODE,
        base=identity(base_sha),
        candidate=identity(candidate_sha),
        plant=PlantIdentity(
            version=kit.plant.plant_version,
            hardware_configuration=kit.plant.hardware_configuration,
        ),
        points=(),
        learned=LearnedState(
            stft_pct=request.stft_pct,
            ltft_pct=request.ltft_pct,
            throttle_closed_learned=request.throttle_closed_learned,
            idle_air_learned=request.idle_air_learned,
            evap_purge_area=request.evap_purge_area,
            source=LEARNED_SOURCE,
        ),
        candidate_events=request.events,
    )


def _grid_design(
    request: SimRequest,
    revision: str,
    profile_dir: Path,
    registry: TuneRevisionRegistry | None,
    plant_version: str | None = None,
) -> SimDesign:
    return build_grid_design(
        request.rpm_edges,
        request.load_edges,
        profile_dir=profile_dir,
        base_revision=revision,
        load_basis=request.load_basis,
        baro_kpa=request.baro_kpa,
        iat_c=request.iat_c,
        coolant_c=request.coolant_c,
        battery_v=request.battery_v,
        rail_kpa=request.rail_kpa,
        throttle_pct=request.throttle_pct,
        pedal_flag=request.pedal_flag,
        plant_version=plant_version,
        registry=registry,
    )


def _simulate(
    base: BaseImage,
    candidate_image: bytes,
    candidate_provenance: Mapping[str, Any],
    request: SimRequest,
    *,
    kit: SimKit,
    profile_dir: Path,
    registry: TuneRevisionRegistry | None = None,
) -> dict[str, Any]:
    """Base vs ``candidate_image`` over the request's grid. Private: the
    candidate is whatever bytes it is given, so the negative control (the
    base's own bytes, which E08.1 refuses as a no-op spec) can run through
    the same path :func:`simulate_edits` uses."""
    revision = _recorded(base)
    _bounded(request)
    design = _grid_design(request, revision, profile_dir, registry, kit.plant.plant_version)
    require_base(design, base.image_sha256)
    # The request states the HI DET latch and knock control (module docstring),
    # so a trim edit simulates; the cells are read at the request's coolant, the
    # one every design point carries.
    state = request.timing_state
    timing = ignition_timing_block(
        kit,
        base.image,
        candidate_image,
        state,
        slot=SIM_SLOT,
        coolant_c=request.coolant_c,
        all_cells=False,
    )
    candidate_sha = sha256_hex(candidate_image)
    result = run_monte_carlo(
        kit,
        _template(kit, base.image_sha256, candidate_sha, request),
        MonteCarloDesign.from_sim_design(design),
        base.image,
        candidate_image,
        _uncertainty(profile_dir, kit),
        draws=request.draws,
        seed=request.seed,
    )
    effects = _effects(base.image, candidate_image, kit)
    axes = (
        fuel_comp_axes(kit, base.image, slot=SIM_SLOT)
        if request.grid == "fuel_comp"
        else target_afr_axes(kit, base.image, slot=SIM_SLOT)
    )
    cells = aggregate_cells(result, design, axes, effects=effects)
    timing["points"] = ignition_timing_points(
        kit, result, design, base.image, candidate_image, state, slot=SIM_SLOT
    )
    assert design.grid is not None
    return {
        "tool": TOOL,
        "tool_version": TOOL_VERSION,
        "label": result.label,
        "synthetic": result.synthetic,
        # The result's caveats, unchanged and first.
        "caveats": list(result.caveats),
        # The design's own assumptions are in here, "design: "-prefixed (E08.10);
        # `design` carries no second copy of them.
        "assumptions": list(result.assumptions),
        "unresolved": list(result.unresolved),
        "inert_parameters": list(result.inert_parameters),
        "coverage": dict(result.coverage),
        "outcome": result.outcome,
        "learned_state": result.learned_state,
        "provenance": result.provenance,
        "design": {
            **summary(design),
            "grid": design.grid.model_dump(mode="json"),
        },
        "candidate": dict(candidate_provenance),
        "cells": cells.to_dict(),
        "ignition_timing": timing,
        "spec_yaml": None,
    }


def simulate_edits(
    image: bytes,
    revision_match: Mapping[str, Any],
    request: SimRequest,
    profile_dir: Path,
    *,
    sha256: str | None = None,
    kit: SimKit | None = None,
    registry: TuneRevisionRegistry | None = None,
) -> dict[str, Any]:
    """The request's edits on a recorded base, simulated over its grid.

    Refuses a base that is not a matched revision, then an oversized grid or
    draw count, before any work. ``kit`` and ``registry`` replace the
    profile's (tests on a synthetic image state its registry entry)."""
    profile_dir = Path(profile_dir)
    base = library_base(image, revision_match, sha256=sha256)
    if revision_match.get("status") != "matched":
        raise SimulationRefusalError(
            BASE_NOT_RECORDED,
            f"revision_match is {revision_match.get('status')!r}: only a flashed revision in "
            "tune_revisions.yaml can be simulated (the page still previews the edits)",
        )
    _recorded(base)
    _bounded(request)
    spec = editor_spec(base, request.label, request.edits)
    kit = _sim_kit(profile_dir, kit)
    candidate_image, provenance = _candidate(base, spec, _EditContext.of(kit))
    out = _simulate(
        base,
        candidate_image,
        provenance,
        request,
        kit=kit,
        profile_dir=profile_dir,
        registry=registry,
    )
    out["spec_yaml"] = spec_yaml(spec)
    out["spec_sha256"] = spec.sha256()
    return out


# -- typical values -------------------------------------------------------------------------

#: ``<profile>/sim_typical_values.yaml``: the vehicle's typical stated values (W09.11).
TYPICAL_VALUES_FILE = "sim_typical_values.yaml"
#: The SimRequest fields the form's "Fill typical values" button may fill: the stated
#: environment and the learned state, vehicle facts. Twelve of the twenty-three.
TYPICAL_FIELDS: tuple[str, ...] = (
    "baro_kpa",
    "iat_c",
    "coolant_c",
    "battery_v",
    "rail_kpa",
    "throttle_pct",
    "pedal_flag",
    "stft_pct",
    "ltft_pct",
    "throttle_closed_learned",
    "idle_air_learned",
    "evap_purge_area",
)
#: The rest describe this experiment, this base or this run, never the vehicle: the
#: button never fills them, and a typical-values file naming one is refused.
#:
#: ``high_det`` and ``knock_control`` (E09.7) are in this group on purpose, and the
#: evidence is the reason. They are live ECU states, not constants of the vehicle: on
#: 2026-10-08 a RAM read found HI DET set (0xFFFF283F = 0x40) and knock control in
#: fallback (0xFFFF13FE = 0xFB00, the KS1 fault, P0328), and both clear when KS1 is
#: repaired. A typical value would keep answering "set / fallback" after that, and a
#: ``set`` default makes every trim A edit read as "no change" (the ECM reads trim B
#: while the latch is set). They are also the experiment's own condition: trim A is
#: only read with the latch clear and trim B only with it set, so "what would this
#: edit do after the KS1 repair?" is a run with ``clear`` / ``active``. The operator
#: states both every run, and the page remembers neither.
NEVER_TYPICAL_FIELDS: tuple[str, ...] = (
    "label",
    "edits",
    "grid",
    "rpm_edges",
    "load_edges",
    "load_basis",
    "high_det",
    "knock_control",
    "candidate_events",
    "draws",
    "seed",
)


def _field_check(name: str) -> TypeAdapter[Any]:
    """The SimRequest field's own type and bounds, as a validator for one value."""
    info = SimRequest.model_fields[name]
    annotation: Any = info.annotation
    if info.metadata:
        annotation = Annotated[(annotation, *info.metadata)]
    return TypeAdapter(annotation)


_FIELD_CHECKS: dict[str, TypeAdapter[Any]] = {name: _field_check(name) for name in TYPICAL_FIELDS}


#: What a declared value is. ``typical``: a sourced figure for the vehicle.
#: ``placeholder``: a number the simulator reads nowhere today, filled only so the
#: form can run; the page tags it ``placeholder``, never ``typical``, so it cannot
#: pass for a measurement, and a test fails the moment any code reads it.
TypicalKind = Literal["typical", "placeholder"]
TYPICAL_KINDS: tuple[str, ...] = ("typical", "placeholder")


class TypicalValue(BaseModel):
    """One typical value, what kind it is, and where it comes from. No source, no
    value; no declared kind, no value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Strict: a quoted "81" or a bare `true` is not a number a form can be filled with.
    value: StrictFloat | StrictInt | Literal["working_assumption"]
    kind: TypicalKind
    source: str = Field(min_length=1)

    @field_validator("source")
    @classmethod
    def _source_is_text(cls, source: str) -> str:
        if not source.strip():
            raise ValueError("is blank")
        return source

    @model_validator(mode="after")
    def _placeholder_says_so(self) -> TypicalValue:
        if self.kind == "placeholder" and "placeholder" not in self.source.lower():
            raise ValueError("a placeholder's source must say it is a placeholder")
        return self


class TypicalLeftEmpty(BaseModel):
    """A fillable field the vehicle declares no typical value for, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def _reason_is_text(cls, reason: str) -> str:
        if not reason.strip():
            raise ValueError("is blank")
        return reason


class TypicalValuesDeclaration(BaseModel):
    """``sim_typical_values.yaml``: every fillable field is a sourced value or an
    explicit, reasoned omission, so the page can always say what it left empty."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    values: dict[str, TypicalValue]
    left_empty: dict[str, TypicalLeftEmpty]

    @model_validator(mode="after")
    def _declared(self) -> TypicalValuesDeclaration:
        for name in (*self.values, *self.left_empty):
            if name in NEVER_TYPICAL_FIELDS:
                raise ValueError(
                    f"{name} is never filled by the typical-values button (it describes this "
                    "experiment, not the vehicle)"
                )
            if name not in TYPICAL_FIELDS:
                raise ValueError(f"{name!r} is not a SimRequest field a typical value may fill")
        for name in self.values.keys() & self.left_empty.keys():
            raise ValueError(f"{name} is declared both as a value and as left empty")
        for name in TYPICAL_FIELDS:
            if name not in self.values and name not in self.left_empty:
                raise ValueError(
                    f"{name} is neither a sourced value nor listed under left_empty with a reason"
                )
        for name, declared in self.values.items():
            value = declared.value
            if isinstance(value, bool):
                raise ValueError(f"{name}: {value!r} is not a number")
            try:
                _FIELD_CHECKS[name].validate_python(value, strict=True)
            except ValidationError as exc:
                raise ValueError(
                    f"{name}: {value!r} is not a valid {name} ({exc.errors()[0]['msg']})"
                ) from exc
        return self


def typical_values(profile_dir: Path) -> dict[str, Any]:
    """The vehicle's typical stated values, for the Simulate form's "Fill typical
    values" button: ``{profile, file, file_sha256, values: {field: {value, kind,
    source}}, left_empty: {field: {reason}}}``, fields in :data:`TYPICAL_FIELDS`
    order. ``kind`` is ``typical`` or ``placeholder`` (:data:`TYPICAL_KINDS`).

    Refused by name as :class:`SimulationRefusalError` (data in the child, a 409 on
    the route): ``typical_values_undeclared`` when the profile has no file, and
    ``typical_values_invalid`` when it fails the schema (an unsourced value, an
    unknown or never-typical field, a value the SimRequest would refuse, a fillable
    field neither declared nor reasoned away). Nothing is assumed in their place."""
    profile_dir = Path(profile_dir)
    path = profile_dir / TYPICAL_VALUES_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise SimulationRefusalError(
            "typical_values_undeclared",
            f"{profile_dir.name} has no {TYPICAL_VALUES_FILE}: this vehicle declares no typical "
            "values, and none are assumed",
        ) from exc
    try:
        declared = TypicalValuesDeclaration.model_validate(load_yaml(raw))
    except (yaml.YAMLError, ValidationError) as exc:
        problems = (
            [
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" if e["loc"] else e["msg"]
                for e in exc.errors()
            ]
            if isinstance(exc, ValidationError)
            else [str(exc)]
        )
        raise SimulationRefusalError(
            "typical_values_invalid", f"{TYPICAL_VALUES_FILE}: " + "; ".join(problems)
        ) from exc
    return {
        "profile": profile_dir.name,
        "file": TYPICAL_VALUES_FILE,
        "file_sha256": sha256_hex(raw),
        "values": {
            name: {
                "value": declared.values[name].value,
                "kind": declared.values[name].kind,
                "source": declared.values[name].source,
            }
            for name in TYPICAL_FIELDS
            if name in declared.values
        },
        "left_empty": {
            name: {"reason": declared.left_empty[name].reason}
            for name in TYPICAL_FIELDS
            if name in declared.left_empty
        },
    }


# -- the workbench child --------------------------------------------------------------------


def refusal_kind(exc: BaseException) -> str | None:
    """The refusal class's kind name, or ``None`` when ``exc`` is not one."""
    for cls, kind in REFUSALS:
        if isinstance(exc, cls):
            return kind
    return None


def _refused(kind: str, reason: str, message: str) -> dict[str, Any]:
    return {"refused": {"kind": kind, "reason": reason, "message": message}}


def _as_refused(exc: ValueError) -> dict[str, Any]:
    kind = refusal_kind(exc)
    assert kind is not None
    reason = str(getattr(exc, "reason", kind))
    message = getattr(exc, "message", None)
    if message is None:
        # SimDesignRefusalError keeps only "reason: message" as its text.
        text = str(exc)
        message = text[len(reason) + 2 :] if text.startswith(f"{reason}: ") else text
    return _refused(kind, reason, str(message))


def _job_image(params: Mapping[str, Any]) -> bytes:
    path = Path(str(params["image_path"]))
    try:
        image = path.read_bytes()
    except OSError as exc:
        raise EditSpecRefusalError("image_not_found", f"{path.name}: {exc}") from exc
    expected = str(params["sha256"])
    if sha256_hex(image) != expected:
        raise EditSpecRefusalError(
            "image_hash_mismatch", f"{path.name} is not image {expected}: the library changed"
        )
    return image


def run_job(
    kind: str,
    params: Mapping[str, Any],
    *,
    kit: SimKit | None = None,
    registry: TuneRevisionRegistry | None = None,
) -> dict[str, Any]:
    """The workbench child's entry (W09.2's job kind ``rom_sim``).

    ``params``: ``image_path`` (the library's plain image), ``sha256`` (its
    key, checked against the bytes), ``revision_match``, ``profile_dir``,
    and ``request`` (a :class:`SimRequest` as JSON) for ``rom_sim`` or
    ``label`` and ``edits`` for ``rom_preview``. A refusal comes back as
    ``{"refused": {"kind", "reason", "message"}}``: the refusal classes do
    not survive pickling, so raised from the child they would reach the
    route as an opaque failure. Anything else raises."""
    if kind not in JOB_KINDS:
        raise ValueError(f"rom_editor job kind {kind!r} is not one of {list(JOB_KINDS)}")
    try:
        image = _job_image(params)
        profile_dir = Path(str(params["profile_dir"]))
        revision_match = dict(params["revision_match"])
        if kind == "rom_preview":
            return preview_edits(
                image,
                revision_match,
                str(params["label"]),
                params["edits"],
                profile_dir,
                sha256=str(params["sha256"]),
                kit=kit,
            )
        try:
            request = SimRequest.model_validate(params["request"])
        except ValidationError as exc:
            return _refused("request", "request_invalid", str(exc))
        return simulate_edits(
            image,
            revision_match,
            request,
            profile_dir,
            sha256=str(params["sha256"]),
            kit=kit,
            registry=registry,
        )
    except _REFUSAL_TYPES as exc:
        return _as_refused(exc)


__all__ = [
    "BASE_NOT_RECORDED",
    "EDITABLE_TABLES",
    "JOB_KINDS",
    "LEARNED_SOURCE",
    "MAX_DRAWS",
    "MAX_GRID_POINTS",
    "NEVER_TYPICAL_FIELDS",
    "REFUSALS",
    "SIM_SLOT",
    "SLOT_SOURCE",
    "TOOL_VERSION",
    "TYPICAL_FIELDS",
    "TYPICAL_KINDS",
    "TYPICAL_VALUES_FILE",
    "SimRequest",
    "editor_spec",
    "library_base",
    "preview_edits",
    "refusal_kind",
    "run_job",
    "sim_base",
    "simulate_edits",
    "spec_yaml",
    "typical_values",
]
