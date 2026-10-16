"""The ROM Editor's preview and simulate, as pure functions (W09.1).

The dashboard's ROM Editor page edits the fuel chain of a ROM Library image
and runs the edit through the E08 simulator. Everything it does is here, on
plain data (image bytes, the library's ``revision_match`` dict, JSON-shaped
edits), so the route (W09.2), the workbench child and the tests run the same
code:

* :func:`preview_edits` -- the E08.1 edit spec the page's edits make, the
  candidate's provenance (cells old -> new, changed ranges, reach) and the
  edited tables' after values. Cheap: the order of ``GET .../tables``.
* :func:`simulate_edits` -- a stated rpm x load grid (E08.2), paired Monte
  Carlo draws (E08.3) and per-cell aggregation (E08.4), base vs candidate.
* :func:`run_job` -- the workbench child's entry point.

What it makes impossible rather than documented:

* **A GUI edit is an E08.1 edit.** The request's ``edits`` are
  :class:`~src.analysis.rom_edit_spec.Edit` dicts, wrapped in an
  :class:`~src.analysis.rom_edit_spec.EditSpec` by :func:`editor_spec`;
  every E08.1 refusal keeps its reason. Only the fuel chain's Map 1 is
  editable (:data:`EDITABLE_TABLES`, :data:`SIM_SLOT`): the simulator runs
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
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from src.analysis.ecu_application import (
    ApplicationModel,
    ApplicationModelRefusalError,
    load_application_model,
)
from src.analysis.fuel_plant import FuelPlantRefusalError
from src.analysis.paired_scenarios import LearnedState, PairedScenario, SimKit, load_kit
from src.analysis.rom_edit_spec import (
    BaseImage,
    EditSpec,
    EditSpecRefusalError,
    build_candidate,
    parse_edit_spec,
)
from src.analysis.rom_effects import RomEffectRefusalError, compare_roms
from src.analysis.rom_image import ImageFormatError, sha256_hex, table_values
from src.analysis.rom_map import RomMap, load_rom_map
from src.analysis.sim_cells import Grid, aggregate_cells, fuel_comp_axes, target_afr_axes
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
    UNCERTAINTY_FILE,
    LoadedUncertainty,
    MonteCarloDesign,
    load_uncertainty,
    run_monte_carlo,
)
from src.vehicle.tune_revisions import TuneRevisionRegistry

#: Bump when a payload's meaning changes.
TOOL_VERSION = "1"
TOOL = "rom_editor"

#: The fuel chain, Map 1 (W09 "Decisions"): every other table is refused.
EDITABLE_TABLES: tuple[str, ...] = ("fuel_targets", "fuel_compensation", "k_constant")
#: The slot the simulator runs: the one the ECM runs after a cold boot.
SIM_SLOT = 1
SLOT_SOURCE: Literal["default_slot"] = "default_slot"
#: Grid cells and draws a simulate request may ask for (W09.6 measures them).
MAX_GRID_POINTS = 1024
MAX_DRAWS = 2000

#: Every :class:`LearnedState` the editor builds says where it came from.
LEARNED_SOURCE = "stated: ROM Editor"
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


def _edit_context(profile_dir: Path, kit: SimKit | None) -> _EditContext:
    """The map and application model an edit resolves and traces through:
    the kit's, else the profile's (without loading the rest of a kit)."""
    if kit is not None:
        return _EditContext(kit.rom_map, kit.model, kit.model_sha256)
    model, model_sha = load_application_model(profile_dir)
    return _EditContext(load_rom_map(profile_dir), model, model_sha)


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
    """The spec, the candidate's provenance and the edited tables' after
    values. ``kit`` replaces the profile's map and application model (tests
    on a synthetic image)."""
    base = library_base(image, revision_match, sha256=sha256)
    spec = editor_spec(base, label, edits)
    ctx = _edit_context(Path(profile_dir), kit)
    candidate_image, provenance = _candidate(base, spec, ctx)
    edited = {(e.table, e.slot) for e in spec.edits}
    return {
        "tool": TOOL,
        "tool_version": TOOL_VERSION,
        "spec": spec.model_dump(mode="json"),
        "spec_yaml": spec_yaml(spec),
        "spec_sha256": spec.sha256(),
        "candidate_sha256": sha256_hex(candidate_image),
        "provenance": provenance,
        "tables": [
            t for t in table_values(candidate_image, ctx.rom_map) if (t["id"], t["slot"]) in edited
        ],
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


def _uncertainty(profile_dir: Path) -> LoadedUncertainty:
    try:
        return load_uncertainty(profile_dir / UNCERTAINTY_FILE)
    except FileNotFoundError as exc:
        raise SimulationRefusalError(
            "uncertainty_missing", f"{profile_dir.name} has no {UNCERTAINTY_FILE}"
        ) from exc
    except ValidationError as exc:
        raise SimulationRefusalError(
            "uncertainty_invalid", f"{UNCERTAINTY_FILE}: {exc.error_count()} error(s)"
        ) from exc


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
    design = _grid_design(request, revision, profile_dir, registry)
    require_base(design, base.image_sha256)
    candidate_sha = sha256_hex(candidate_image)
    result = run_monte_carlo(
        kit,
        _template(kit, base.image_sha256, candidate_sha, request),
        MonteCarloDesign.from_sim_design(design),
        base.image,
        candidate_image,
        _uncertainty(profile_dir),
        draws=request.draws,
        seed=request.seed,
    )
    effects = compare_roms(
        base.image,
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
    axes = (
        fuel_comp_axes(kit, base.image, slot=SIM_SLOT)
        if request.grid == "fuel_comp"
        else target_afr_axes(kit, base.image, slot=SIM_SLOT)
    )
    cells = aggregate_cells(result, design, axes, effects=effects)
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
    kit = kit if kit is not None else load_kit(profile_dir)
    candidate_image, provenance = _candidate(
        base, spec, _EditContext(kit.rom_map, kit.model, kit.model_sha256)
    )
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
    "REFUSALS",
    "SIM_SLOT",
    "SLOT_SOURCE",
    "TOOL_VERSION",
    "SimRequest",
    "editor_spec",
    "library_base",
    "preview_edits",
    "refusal_kind",
    "run_job",
    "sim_base",
    "simulate_edits",
    "spec_yaml",
]
