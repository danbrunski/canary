"""The ROM simulator's fixtures: paired scenarios, Monte Carlo designs, cell runs, editor
requests, effect reports and the timing tables (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import shutil
import struct
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.analysis.ecu_application import ApplicationModel, program_sha256
from src.analysis.ecu_state_models import BaseTimingCals, BaseTimingTables, Term279aTables
from src.analysis.fuel_plant import PLANT_FILE, FuelPlantProfile
from src.analysis.paired_scenarios import (
    LearnedState,
    OperatingPoint,
    PairedScenario,
    SimKit,
    load_kit,
)
from src.analysis.plant_validation import InterventionVerdict
from src.analysis.rom_effects import CellEdit, EffectReport, compare_roms, make_candidate
from src.analysis.rom_image import revision_match, sha256_hex
from src.analysis.rom_map import RomMap, load_rom_map
from src.analysis.sim_contract import EcuIdentity, PlantIdentity
from src.analysis.sim_design import DesignPoint, SessionCheck, SimDesign, build_grid_design
from src.analysis.sim_montecarlo import (
    UNCERTAINTY_FILE,
    LoadedUncertainty,
    MonteCarloDesign,
    MonteCarloResult,
    load_uncertainty,
    run_monte_carlo,
)
from src.analysis.sim_montecarlo import (
    DesignPoint as MonteCarloDesignPoint,
)
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry
from src.yaml_load import load_yaml
from tests.rom_fixtures import PROFILE, synthetic_image
from tools import rom_sim

DESIGN_SHA = "d" * 64


SOURCE = f"design:{DESIGN_SHA}"


def build_kit_and_image() -> tuple[SimKit, bytes]:
    kit = load_kit(PROFILE)
    image = paired_image(kit.model, kit.plant)
    identity = kit.model.program.identity.model_copy(
        update={"sha256": program_sha256(image, kit.model)}
    )
    model = kit.model.model_copy(
        update={"program": kit.model.program.model_copy(update={"identity": identity})}
    )
    return dataclasses.replace(kit, model=model), image


def mc_point(base: OperatingPoint, **over: Any) -> OperatingPoint:
    return dataclasses.replace(base, conditioned={}, source=SOURCE, **over)


def mc_design(image: bytes, *specs: tuple[OperatingPoint, int, int]) -> MonteCarloDesign:
    return MonteCarloDesign(
        sha256=DESIGN_SHA,
        base_image_sha256=sha256_hex(image),
        points=tuple(
            MonteCarloDesignPoint(p, lo, hi, 1.0 + k) for k, (p, lo, hi) in enumerate(specs)
        ),
    )


def mc_template(kit: SimKit, base: bytes, candidate: bytes, **over: Any) -> PairedScenario:
    from src.analysis.sim_contract import PlantIdentity

    fields: dict[str, Any] = dict(
        mode="conditional_rom_comparison",
        base=paired_identity(base),
        candidate=paired_identity(candidate),
        plant=PlantIdentity(
            version=kit.plant.plant_version,
            hardware_configuration=kit.plant.hardware_configuration,
        ),
        points=(),
        learned=LEARNED,
    )
    fields.update(over)
    return PairedScenario(**fields)


def mc_run(
    kit: SimKit,
    image: bytes,
    candidate: bytes,
    design: MonteCarloDesign,
    uncertainty: LoadedUncertainty,
    **kw: Any,
) -> MonteCarloResult:
    kw.setdefault("draws", 12)
    kw.setdefault("seed", 20261015)
    return run_monte_carlo(
        kit, mc_template(kit, image, candidate), design, image, candidate, uncertainty, **kw
    )


def paired_image(model: ApplicationModel, kit_plant: Any) -> bytes:
    """A synthetic image every decode the slice uses describes: the twin's final-
    command constants and Load Point Scaler, the model's constants and
    conditions, UpRev's patch, and the pulse chain's published words."""
    image = bytearray(synthetic_image())
    image[0x8B39] = 130
    image[0x8B3D] = 129
    image[0xD247 : 0xD247 + 8] = bytes([128] * 8)
    struct.pack_into(">H", image, 0x9416, 44019)
    for marker in model.program.patch.markers:
        raw = bytes.fromhex(marker.bytes)
        image[marker.offset : marker.offset + len(raw)] = raw
    for node in model.nodes:
        for ref in node.rom:
            if ref.offset is not None:
                assert ref.width is not None and ref.value is not None
                width = ref.width // 8
                image[ref.offset : ref.offset + width] = ref.value.to_bytes(width, "big")
        for c in node.calibration_conditions:
            for k in range(c.count):
                image[c.offset + k] = (image[c.offset + k] & ~c.mask & 0xFF) | c.equals
    chain = kit_plant.pulse_chain
    for word in (
        chain.dead_time_base,
        chain.dead_time_slope,
        chain.rail_reference,
        chain.flow_scaler,
    ):
        image[word.address : word.address + word.width] = word.value.to_bytes(word.width, "big")
    _air_charge_calibration(image)
    return bytes(image)


def _air_charge_calibration(image: bytearray) -> None:
    """The steady-BFS calibration at the 20260920 tune's values (K 28715, flow
    and speed multipliers 226/3, MAF report scale 149, BFS ceiling 255:
    docs/ecu-firmware-analysis.md, tests/test_air_charge.py), flat Fuel Comp
    128 on rising axes, so WOT BFS lands near the truck's ~38-40k."""
    rom_map = load_rom_map(PROFILE)

    def put(table_id: str, slot: int | None, values: list[int], width: int = 1) -> None:
        at = rom_map.table_address(rom_map.table(table_id), slot)
        for k, v in enumerate(values):
            image[at + k * width : at + (k + 1) * width] = v.to_bytes(width, "big")

    put("fuel_comp_engine_speed_axis", None, [8 * (i + 1) for i in range(16)])
    put("fuel_comp_flow_axis", None, [16 * i for i in range(16)])
    put("fuel_compensation", 1, [128] * 256)
    put("k_constant", 1, [28715], width=2)
    put("bfs_ceiling", None, [255] * 16)
    image[0x89D2], image[0x88AD], image[0x83E9] = 226, 3, 149


WOT = OperatingPoint(
    rpm=4800.0,
    maf_gps=150.0,
    baro_kpa=81.0,
    iat_c=25.0,
    coolant_c=88.0,
    battery_v=14.0,
    rail_kpa=350.0,
    map_kpa=79.0,
    throttle_pct=90.0,
    pedal_flag=0,
    conditioned={"fuel_comp_load_byte": 200.0},
    source="design: WOT point, E07.6 test grid",
)


#: Open loop (enrichment) at WOT, and closed-loop cruise, from E07.6's grid.
P_WOT = mc_point(WOT)


#: The design's other route: MAP from the plant's own manifold model, no rail
#: reading (the working value applies), no module voltage read.
P_MODEL = mc_point(
    WOT, map_kpa_source="plant_model", rail_kpa_source="working_assumption", map_kpa=0.0
)


CRUISE = dataclasses.replace(
    WOT,
    rpm=2000.0,
    maf_gps=18.0,
    map_kpa=40.0,
    throttle_pct=10.0,
    conditioned={"fuel_comp_load_byte": 60.0},
    source="design: cruise point, E07.6 test grid",
)


P_CRUISE = mc_point(CRUISE)


LEARNED = LearnedState(
    stft_pct=0.0,
    ltft_pct=2.34,
    throttle_closed_learned=0.0,
    idle_air_learned=0.0,
    evap_purge_area=0.0,
    source="design: stated trims",
)


def paired_identity(image: bytes) -> EcuIdentity:
    return EcuIdentity(
        program_id="ZS44A", image_sha256=sha256_hex(image), slot=1, slot_source="default_slot"
    )


def paired_scenario(
    kit: SimKit, base: bytes, candidate: bytes, *points: OperatingPoint, **over: Any
) -> PairedScenario:
    plant = PlantIdentity(
        version=kit.plant.plant_version,
        hardware_configuration=kit.plant.hardware_configuration,
    )
    fields: dict[str, Any] = dict(
        mode="conditional_rom_comparison",
        base=paired_identity(base),
        candidate=paired_identity(candidate),
        plant=plant,
        points=points or (WOT, CRUISE),
        learned=LEARNED,
    )
    fields.update(over)
    return PairedScenario(**fields)


def paired_edit_all(image: bytes, kit: SimKit, table_id: str, slot: int | None, step: int) -> bytes:
    table = kit.rom_map.table(table_id)
    start = kit.rom_map.table_address(table, slot)
    edits = []
    for r in range(table.rows):
        for c in range(table.cols):
            raw = image[start + r * table.cols + c]
            edits.append(CellEdit(table_id, r, c, min(255, max(0, raw + step)), slot=slot))
    edits = [e for e in edits if e.raw != image[start + e.row * table.cols + e.col]]
    return make_candidate(image, kit.rom_map, edits, base_label="synthetic").image


def paired_minted(result: Any, verdict: str, **over: Any) -> InterventionVerdict:
    """A verdict as evaluate_intervention mints it (the test stands in for a run)."""
    fields: dict[str, Any] = dict(
        case="test",
        verdict=verdict,
        synthetic=False,
        base_image_sha256=result.base_image_sha256,
        candidate_image_sha256=result.candidate_image_sha256,
        baseline_sha256=result.plant["baseline_sha256"],
        plant_version="fuel-mv-1",
        qualified_regimes=("closed_loop", "enrichment"),
    )
    fields.update(over)
    return InterventionVerdict._mint(**fields)


def paired_plant_with(verdict: str, *, synthetic: bool = False) -> FuelPlantProfile:
    raw: dict[str, Any] = load_yaml((PROFILE / PLANT_FILE).read_text())
    raw["synthetic"] = synthetic
    for rec in raw["spent_held_out"]:
        rec["verdict"] = verdict
    return FuelPlantProfile.model_validate(raw)


# A Target AFR edit's traced spark refusal (see the ranking test below).
SPARK_TRACED = "unsupported:ignition_timing:unevaluable"


#: E08.1's light-load Fuel Comp edit, as the page sends it.
FC_EDIT: dict[str, Any] = {
    "table": "fuel_compensation",
    "slot": 1,
    "region": {"rpm": [1500, 3000], "load": [20, 50], "units": {"rpm": "rpm", "load": "%"}},
    "scale": 1.02,
    "rounding": "nearest",
}


K_EDIT: dict[str, Any] = {
    "table": "k_constant",
    "slot": 1,
    "all": True,
    "scale": 1.05,
    "rounding": "nearest",
}


def request(**over: Any) -> dict[str, Any]:
    """A complete simulate request: every field stated."""
    body: dict[str, Any] = dict(
        label="K +5 %",
        edits=[K_EDIT],
        grid="fuel_comp",
        # Two rpm rows near the enrichment threshold on this image (4,400 and
        # 5,200 rpm centres) at three loads: five points open loop, one closed.
        rpm_edges=[4000.0, 4800.0, 5600.0],
        load_edges=[30.0, 60.0, 80.0, 100.0],
        load_basis="fill_fraction_as_manifold_ratio",
        baro_kpa=80.0,
        iat_c=25.0,
        coolant_c=88.0,
        battery_v=14.0,
        rail_kpa="working_assumption",
        throttle_pct=20.0,
        pedal_flag=0,
        stft_pct=0.0,
        ltft_pct=0.0,
        throttle_closed_learned=0.0,
        idle_air_learned=0.0,
        evap_purge_area=0.0,
        # The truck's state on 2026-10-08 (RAM read): HI DET set, knock control in
        # fallback. A scenario here, never a default: the request has none.
        high_det="set",
        knock_control="fallback",
        candidate_events="none",
        draws=6,
        seed=20261015,
    )
    body.update(over)
    return body


def editor_timing_points_per_pair(
    kit: SimKit,
    result: Any,
    design: Any,
    base_image: bytes,
    candidate_image: bytes,
    state: Any,
    *,
    slot: int,
) -> list[dict[str, Any]]:
    """The reference: ``sim_cells.ignition_timing_points`` as it was before W09.13, one
    ``ignition_timing_at`` call per (point, draw) pair and nothing shared between pairs. The
    memoized function must return exactly these rows."""
    import math as _math

    from src.analysis import sim_cells
    from src.analysis.paired_scenarios import ignition_timing_at, rpm_word, timing_tables
    from src.analysis.target_phi import x_from_bfs

    reach = [
        r
        for r in sim_cells.timing_reach_reasons(kit, base_image, candidate_image, slot=slot)
        if not (
            r.startswith(f"{sim_cells.TIMING_INPUT_CHANGED}:")
            and sim_cells._carried_by_points(kit, r.split(":", 1)[1])
        )
    ]
    tb = timing_tables(base_image, kit.rom_map, slot=slot)
    tc = timing_tables(candidate_image, kit.rom_map, slot=slot)
    lps = (
        sim_cells._load_point_scaler(kit, base_image, slot=slot),
        sim_cells._load_point_scaler(kit, candidate_image, slot=slot),
    )
    bit = kit.twin.inputs.forced_column_flag.bit
    bfs = (result.base["base_fuel_schedule"], result.candidate["base_fuel_schedule"])
    phi = (result.base["target_phi"], result.candidate["target_phi"])
    rows: list[dict[str, Any]] = []
    for i, p in enumerate(design.points):
        word = rpm_word(p.rpm, kit.contract)
        pedal = bool((p.pedal_flag >> bit) & 1)
        outcomes: Counter[str] = Counter()
        detail: dict[str, dict[str, Any]] = {}
        for k in range(result.draws):
            words = (bfs[0][i, k], bfs[1][i, k])
            if not all(_math.isfinite(w) for w in words):
                at: dict[str, Any] = {
                    "status": "unevaluable",
                    "delta": None,
                    "delta_min": None,
                    "delta_max": None,
                    "trim_term_delta": None,
                    "term_27c6_delta": None,
                    "term_279a": None,
                    "charge_word_routes": None,
                    "reasons": [sim_cells.X_NOT_COMPUTED],
                    "x_base": None,
                    "x_candidate": None,
                }
            else:
                xb = x_from_bfs(int(words[0]), lps[0], kit.twin)
                xc = x_from_bfs(int(words[1]), lps[1], kit.twin)
                pb, pc = float(phi[0][i, k]), float(phi[1][i, k])
                moved = _math.isfinite(pb) != _math.isfinite(pc) or (
                    _math.isfinite(pb) and pb != pc
                )
                full = ignition_timing_at(
                    tb,
                    tc,
                    state,
                    rpm_word=word,
                    x_base=xb,
                    x_candidate=xc,
                    coolant_c=p.coolant_c,
                    pedal_flag=pedal,
                    phi_moved=moved,
                    bfs_moved=int(words[0]) != int(words[1]),
                    lookup_bytes=(
                        sim_cells._lookup_byte(kit, pb),
                        sim_cells._lookup_byte(kit, pc),
                    ),
                )
                at = {key: full[key] for key in sim_cells._POINT_KEYS}
                at.update(x_base=xb, x_candidate=xc)
                if reach:
                    at.update(
                        status="unevaluable",
                        delta=None,
                        delta_min=None,
                        delta_max=None,
                        reasons=list(dict.fromkeys([*at["reasons"], *reach])),
                    )
            key = json.dumps(at, sort_keys=True)
            outcomes[key] += 1
            detail[key] = at
        rows.append(
            {
                "point": i,
                "rpm": p.rpm,
                "rpm_word": word,
                "coolant_c": p.coolant_c,
                "pedal_flag": pedal,
                "outcomes": [{**detail[key], "draws": n} for key, n in sorted(outcomes.items())],
            }
        )
    return rows


MODE = "conditional_rom_comparison"


#: A warm part-load point (E07.6's cruise), every source measured.
_POINT: dict[str, Any] = dict(
    rpm=2000.0,
    maf_gps=18.0,
    maf_source="pid:0x10",
    baro_kpa=81.0,
    iat_c=25.0,
    coolant_c=88.0,
    battery_v=14.0,
    battery_v_source="measured",
    rail_kpa=350.0,
    rail_kpa_source="measured",
    map_kpa=40.0,
    map_kpa_source="measured",
    throttle_pct=10.0,
    throttle_pct_lo=10.0,
    throttle_pct_hi=10.0,
    throttle_pct_source="pid:0x11",
    pedal_flag=0,
    pedal_flag_source="stated_zero:not_logged",
    fill_byte_lo=60,
    fill_byte_hi=60,
    fill_byte_source="test",
    weight=1.0,
    regime="closed_loop",
)


def cells_design(image: bytes, kit: SimKit, *points: dict[str, Any]) -> SimDesign:
    pts = []
    for i, over in enumerate(points):
        fields = {**_POINT, **over}
        fields.setdefault("throttle_pct_lo", fields["throttle_pct"])
        fields["throttle_pct_lo"] = fields["throttle_pct_hi"] = fields["throttle_pct"]
        pts.append(DesignPoint(index=i, session="_sim-test", t=float(i), **fields))
    n = len(pts)
    dwell = float(sum(p.weight for p in pts))
    return SimDesign(
        schema_version=1,
        kind="drive",
        vehicle_profile="test",
        base_revision="synthetic",
        base_image_sha256=sha256_hex(image),
        plant_version=kit.plant.plant_version,
        plant_file_sha256="0" * 64,
        plant_baseline_sha256="test",
        manifold_model="test",
        weight_basis="dwell_s",
        dwell_cap_s=2.0,
        sessions=[
            SessionCheck(
                session="_sim-test",
                dated_at=datetime(2026, 10, 15, tzinfo=UTC),
                revision_at_start="synthetic",
                revisions_in_reads=["synthetic"],
                reads=n,
                plant_states={"eligible": n},
                placement={"placed": n},
                eligible_dwell_s=dwell,
                placed_weight=dwell,
            )
        ],
        reads=n,
        plant_states={"eligible": n},
        placement={"placed": n},
        eligible_dwell_s=dwell,
        unplaced_dwell_s={},
        assumptions=[],
        points=pts,
    )


def cells_run(
    kit: SimKit,
    image: bytes,
    candidate: bytes,
    design: SimDesign,
    uncertainty: LoadedUncertainty,
    *,
    draws: int = 6,
) -> tuple[MonteCarloResult, EffectReport]:
    result = run_monte_carlo(
        kit,
        mc_template(kit, image, candidate),
        MonteCarloDesign.from_sim_design(design),
        image,
        candidate,
        uncertainty,
        draws=draws,
        seed=7,
    )
    effects = compare_roms(
        image,
        candidate,
        kit.rom_map,
        kit.model,
        kit.model_sha256,
        base_slot=1,
        candidate_slot=1,
        slot_source="default_slot",
        contract=kit.contract,
        mode=MODE,
    )
    return result, effects


BASE = "ZS44A-20260920"


GRID_ENV = dict(
    baro_kpa=80.0,
    iat_c=25.0,
    coolant_c=88.0,
    battery_v=14.0,
    rail_kpa="working_assumption",
    throttle_pct=20.0,
    pedal_flag=0,
)


def design_grid(**over: object) -> SimDesign:
    kw: dict[str, object] = dict(
        profile_dir=PROFILE,
        base_revision=BASE,
        load_basis="fill_fraction_as_manifold_ratio",
        plant_version="fuel-mv-1",
        **GRID_ENV,
    )
    kw.update(over)
    return build_grid_design([800, 1600, 2400, 3200], [0, 25, 50, 75, 100], **kw)  # type: ignore[arg-type]


def modelled_image(model: ApplicationModel) -> bytes:
    """A synthetic image that every node's decode describes."""
    image = bytearray(synthetic_image())
    for marker in model.program.patch.markers:
        raw = bytes.fromhex(marker.bytes)
        image[marker.offset : marker.offset + len(raw)] = raw
    for node in model.nodes:
        for ref in node.rom:
            if ref.offset is not None:
                assert ref.width is not None and ref.value is not None
                width = ref.width // 8
                image[ref.offset : ref.offset + width] = ref.value.to_bytes(width, "big")
        for c in node.calibration_conditions:
            for k in range(c.count):
                image[c.offset + k] = (image[c.offset + k] & ~c.mask & 0xFF) | c.equals
    return bytes(image)


def effects_bump(
    image: bytes, rom_map: RomMap, table_id: str, slot: int | None, row: int, col: int
) -> CellEdit:
    table = rom_map.table(table_id)
    address = rom_map.table_address(table, slot) + (row * table.cols + col)
    return CellEdit(table_id, row, col, image[address] ^ 0x01, slot=slot)


#: 0x9AF9: rows the rpm axis 0xC890 (compared with the rpm/50 byte), columns
#: the X axis 0xC88C.
RATE_9AF9 = (
    (0x11, 0x1A, 0x28, 0x3A),
    (0x19, 0x27, 0x3C, 0x55),
    (0x1B, 0x2A, 0x42, 0x5E),
    (0x1D, 0x2D, 0x46, 0x64),
)


#: 0x9B89: same axes, the rpm axis compared as byte x 4 with the rpm word.
CHARGE_9B89 = (
    (0x6C, 0x6C, 0x67, 0x64),
    (0x64, 0x64, 0x6E, 0x7F),
    (0x84, 0x84, 0x8D, 0xA0),
    (0x76, 0x76, 0x8E, 0x9C),
)


RPM_C890 = (0x08, 0x38, 0x58, 0x78)


LOAD_C88C = (0x10, 0x20, 0x40, 0x70)


CURVE_CD9B = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0)


FLAT_80 = (0x80,) * 8  # 0xCEAB and 0xD60F


COOLANT_CURVE = (0x67, 0x6C, 0x76, 0x78, 0x78, 0x7B, 0x80, 0x80)  # 0xCEBB and 0xD9AF


TABLES = BaseTimingTables(
    rate_map_9af9=RATE_9AF9,
    charge_map_9b89=CHARGE_9B89,
    rpm_axis_c890=RPM_C890,
    load_axis_c88c=LOAD_C88C,
    load_curve_cd9b=CURVE_CD9B,
    coolant_curve_cebb=COOLANT_CURVE,
    charge_curve_ceab=FLAT_80,
    coolant_curve_d9af=COOLANT_CURVE,
    charge_curve_d60f=FLAT_80,
)


#: 0x880D, 0x880E, 0x8440, 0x8B0C.
CALS = BaseTimingCals(cal_880d=179, cal_880e=25, cal_8440=32, cal_8b0c=14)


#: The 2026-10-17 image values: 0xCC33 is 0 except entries 6..9.
CURVE_CC33 = (0, 0, 0, 0, 0, 0, 0xFF, 0xFF, 0xFF, 0xFF, 0, 0, 0, 0, 0, 0)


CURVE_CC1B = (0, 0, 0, 0x60, 0xC4, 0xD0, 0xFF, 0x88, 0x50, 0, 0, 0, 0, 0, 0, 0)


CURVE_CC2B = (0x00, 0x02, 0x0B, 0x0C, 0x0A, 0x08, 0x03, 0x00)


#: The 0xFFFF279A input tables as on every listed image (rom_map.yaml): map
#: 0x9B09 all zero, 0xCC33 zero but entries 6..9.
RETARD = Term279aTables(
    map_9b09=((0,) * 8,) * 8,
    rpm_axis_c8fc=(0x30, 0x38, 0x40, 0x48, 0x60, 0x80, 0x90, 0xA4),
    coolant_axis_c904=(0x40, 0x48, 0x50, 0x58, 0x60, 0x68, 0x70, 0x80),
    curve_cc1b=CURVE_CC1B,
    curve_cc2b=CURVE_CC2B,
    curve_cc33=CURVE_CC33,
    curve_d335=(0x00, 0x20, 0x20, 0x20, 0x20, 0x20, 0x00, 0x00),
    curve_d33d=(0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x00, 0x00),
)


@pytest.fixture(scope="module")
def kit_and_image() -> tuple[SimKit, bytes]:
    return build_kit_and_image()


@pytest.fixture(scope="module")
def uncertainty() -> LoadedUncertainty:
    return load_uncertainty(PROFILE / UNCERTAINTY_FILE)


#: The synthetic image's registry entry, stated here: a flashed revision whose
#: plain-image hash is the synthetic image's.
REVISION = "synthetic-w09"


@pytest.fixture(scope="module")
def registry(kit_and_image: tuple[SimKit, bytes]) -> TuneRevisionRegistry:
    _, image = kit_and_image
    return TuneRevisionRegistry(
        revisions=[
            TuneRevision(
                revision=REVISION,
                flashed_at=datetime(2026, 10, 1, tzinfo=UTC),
                image_sha256=sha256_hex(image),
            )
        ]
    )


@pytest.fixture(scope="module")
def matched(kit_and_image: tuple[SimKit, bytes], registry: TuneRevisionRegistry) -> dict[str, Any]:
    _, image = kit_and_image
    found = revision_match(sha256_hex(image), registry)
    assert found["status"] == "matched" and found["revision"] == REVISION
    return found


def editor_params(
    image: bytes, tmp_path: Path, status: dict[str, Any], **over: Any
) -> dict[str, Any]:
    sha = sha256_hex(image)
    path = tmp_path / f"{sha}.bin"
    path.write_bytes(image)
    params: dict[str, Any] = {
        "image_path": str(path),
        "sha256": sha,
        "revision_match": status,
        "profile_dir": str(PROFILE),
        "request": request(),
    }
    params.update(over)
    return params


CLI_BASE = "SYNTH-base"


#: Cell centres: rpm 2000, 2500, 3000 x load 28.5, 46, 65 %. The synthetic Fuel
#: Comp axes are rpm 400..6400 x 400 and fill 0..240 x 16 (x 100/224 %), so the
#: edit at (4, 4) is rpm 2000 at fill byte 64 = 28.57 %: the first cell centre
#: reads it (bytes 63..64), the other eight read other cells.
GRID_ARGS = [
    "--rpm-edges", "1900", "2100", "2900", "3100",
    "--load-edges", "25", "32", "60", "70",
    "--load-basis", "fill_fraction_as_manifold_ratio",
    "--baro-kpa", "80",
    "--iat-c", "25",
    "--coolant-c", "88",
    "--battery-v", "14",
    "--rail-kpa", "working_assumption",
    "--throttle-pct", "20",
    "--pedal-flag", "0",
]  # fmt: skip


CLI_LEARNED = ["--stft-pct", "0", "--ltft-pct", "2.34", "--candidate-events", "none"]


def _yaml(path: Path) -> Any:
    return load_yaml(path.read_text())


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    kit, image = build_kit_and_image()
    root = tmp_path_factory.mktemp("rom_sim")
    profile = root / "profile"
    shutil.copytree(PROFILE, profile, ignore=shutil.ignore_patterns("_candidates", "_prior"))
    # The application model describes this image's program, not the real one.
    model_path = profile / "ecu_application_model.yaml"
    model = _yaml(model_path)
    model["program"]["identity"]["sha256"] = program_sha256(image, kit.model)
    model_path.write_text(yaml.safe_dump(model, sort_keys=False))
    sha = hashlib.sha256(image).hexdigest()
    registry_path = profile / "tune_revisions.yaml"
    # Dated in 2099 so a synthetic revision sorts after every real flash: the
    # registry refuses an out-of-order list, and a date just past the newest real
    # flash broke these fixtures when ZS44A-20261110 was recorded.
    registry = _yaml(registry_path)
    registry["revisions"].append(
        {
            "revision": CLI_BASE,
            "calibration_id": "1ZS44A00000",
            "flashed_at": "2099-10-10T00:00:00Z",
            "flashed_at_precision": "exact",
            "image_sha256": sha,
            "notes": "synthetic: the CLI test's base image",
        }
    )
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False))
    (root / "base.bin").write_bytes(image)
    # (4, 4) +20 counts: 128 -> 148, hand-worked +15.625 %.
    candidate = make_candidate(
        image, kit.rom_map, [CellEdit("fuel_compensation", 4, 4, 148, slot=1)], base_label="x"
    ).image
    (root / "candidate.bin").write_bytes(candidate)

    def spec(name: str, edits: list[dict[str, Any]], base_sha: str = sha) -> Path:
        path = root / name
        path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "base": {"file": "base.bin", "sha256": base_sha},
                    "label": f"cli test {name}",
                    "edits": edits,
                }
            )
        )
        return path

    cell = {"table": "fuel_compensation", "slot": 1, "cells": [[4, 4]], "set_raw": 148}
    return {
        "root": root,
        "profile": profile,
        "image": image,
        "sha": sha,
        "candidate_sha": hashlib.sha256(candidate).hexdigest(),
        "spec": spec("spec.yaml", [cell]),
        "spec_other_base": spec("other.yaml", [cell], base_sha="0" * 64),
        "spec_no_op": spec("noop.yaml", [{**cell, "set_raw": 128}]),
        "kit": kit,
    }


def cli_profile_args(world: dict[str, Any]) -> list[str]:
    return ["--profile", str(world["profile"])]


@pytest.fixture(scope="module")
def grid_design(world: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("designs")
    with contextlib.redirect_stdout(io.StringIO()):
        code = rom_sim.main(
            [
                "design",
                "grid",
                *cli_profile_args(world),
                "--base-revision",
                CLI_BASE,
                *GRID_ARGS,
                "--out",
                str(out),
            ]
        )
    assert code == 0
    (directory,) = out.iterdir()
    return directory


def cli_run_args(world: dict[str, Any], design: Path, out: Path, *extra: str | Path) -> list[str]:
    return [
        "run",
        *cli_profile_args(world),
        "--base",
        "--rom",
        str(world["root"] / "base.bin"),
        "--sha256",
        world["sha"],
        "--design",
        str(design),
        "--draws",
        "6",
        "--seed",
        "7",
        *CLI_LEARNED,
        "--out",
        str(out),
        *(str(e) for e in extra),
    ]


@pytest.fixture(scope="module")
def spec_run(
    world: dict[str, Any], grid_design: Path, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("run_spec")
    with contextlib.redirect_stdout(io.StringIO()):
        code = rom_sim.main(cli_run_args(world, grid_design, out, "--edits", str(world["spec"])))
    assert code == 0
    return {"out": out, "result": json.loads((out / "result.json").read_text())}
