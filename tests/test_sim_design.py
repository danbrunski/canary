"""E08.2: operating-point designs, frozen and hashed, never a recording.

The drive design runs on the committed slice
``tests/fixtures/fuel_plant/_capture-tuning-2026-09-23T142926`` (ZS44A-20260920).
That slice, like every tuning-profile drive on that revision, logs neither
PID 0x11 nor the module voltage, so on it every eligible read is counted
``throttle_unpaired`` and nothing is placed. The placing tests copy the slice
into ``tmp_path`` and add a stated throttle and battery read at each rpm read
(labelled as such), and give N02 an invented Fuel Comp calibration under the
base revision's registry hash, so no private ROM is needed. The real images
are used only by the last test, which skips when they are absent.
"""

from __future__ import annotations

import dataclasses
import json
import math
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from src.analysis.fuel_comp_rom import FuelCompCalibration, FuelCompRomRefusalError
from src.analysis.fuel_plant import load_b06, load_fuel_plant, predict, resolve_parameters
from src.analysis.fuel_plant_eval import apply_manifold_refusals, build_samples, plant_inputs
from src.analysis.paired_scenarios import (
    LearnedState,
    PairedScenario,
    _refuse_recorded_sources,
)
from src.analysis.sim_contract import EcuIdentity, PlantIdentity, SimulationRefusalError
from src.analysis.sim_design import (
    SimDesign,
    SimDesignRefusalError,
    _dwell,
    build_drive_design,
    build_grid_design,
    load_design,
    require_base,
    summary,
    write_design,
)
from src.decode.fuel_comp_load import LoadInputCurves
from src.paths import REPO_ROOT
from src.vehicle.tune_revisions import TuneRevisionRegistry, load_tune_revisions
from tests.rom_fixtures import OPERATOR_ROMS, PROFILE

FIXTURE = Path("tests/fixtures/fuel_plant/_capture-tuning-2026-09-23T142926")
BASE = "ZS44A-20260920"
_LOADED = load_tune_revisions(PROFILE / "tune_revisions.yaml")
assert _LOADED is not None
REGISTRY: TuneRevisionRegistry = _LOADED


def _image_sha(revision: str) -> str:
    sha = next(r.image_sha256 for r in REGISTRY.revisions if r.revision == revision)
    assert sha is not None
    return sha


BASE_SHA = _image_sha(BASE)
GRID_ENV = dict(
    baro_kpa=80.0,
    iat_c=25.0,
    coolant_c=88.0,
    battery_v=14.0,
    rail_kpa="working_assumption",
    throttle_pct=20.0,
    pedal_flag=0,
)


def _calibration(name: str, image_sha256: str = BASE_SHA) -> FuelCompCalibration:
    """Invented tables (round numbers, never ROM values) claiming ``image_sha256``."""
    curves = LoadInputCurves(
        area_breakpoints=tuple(100 * i for i in range(32)),
        area_values=tuple(200 + 30 * i * i for i in range(32)),
        fill_low=(0, *(16000 + 2400 * i for i in range(15))),
        fill_high=tuple(49600 + 500 * i for i in range(16)),
        rpm_multiplier=150,
        reciprocal_numerator=10_000_000,
        idle_air_cap=1000,
        limp_home_area=700,
    )
    return FuelCompCalibration(
        name=name,
        image_sha256=image_sha256,
        source_path=Path("synthetic.bin"),
        image_format="plain",
        slot=1,
        fuel_comp=tuple((128,) * 16 for _ in range(16)),
        rpm_axis_raw=tuple(range(8, 136, 8)),
        load_axis_raw=(*range(16, 256, 16), 255),
        rpm_breakpoints=tuple(4 * b for b in range(8, 136, 8)),
        k=28000,
        purge_gain=128,
        curves=curves,
        table_ids={"fuel_comp": "synthetic"},
    )


def _resolver(name: str) -> FuelCompCalibration:
    return _calibration(name)


def _refusing_resolver(name: str) -> FuelCompCalibration:
    raise FuelCompRomRefusalError("image_not_found", f"{name}: no image for the test")


#: DID 0x120F raw count by rpm, for the DID throttle route: 130 at idle (so
#: N02's closed position is 130 - [23, 32] = [98, 107]) and 300 off idle.
def _did_raw(rpm: float) -> int:
    return 130 if rpm <= 900 else 300


def _augmented(
    tmp_path: Path,
    name: str = FIXTURE.name,
    *,
    pid: Literal["all", "first_half", "none"] = "all",
    did: Literal["none", "all", "off_idle"] = "none",
    battery: bool = True,
) -> Path:
    """The committed slice plus stated reads, labelled as test scaffolding (the
    slice's own drive logged none of them):

    * ``pid``: a 20 % PID 0x11 read at every rpm read's instant (or only in
      the first half of the drive);
    * ``did``: a DID 0x120F read on each pulse-DID read's Mode 22 cycle, raw
      :func:`_did_raw` of the nearest rpm read (``off_idle``: none at idle,
      so N02's closed-position method has nothing to estimate from);
    * ``battery``: a 14.1 V module-voltage read at every rpm read's instant.
    """
    out = tmp_path / "sessions" / name
    shutil.copytree(FIXTURE, out)
    frame = pd.read_parquet(FIXTURE / "telemetry-00000.parquet")
    rpm = frame[frame["channel"] == "RPM"]
    added = []

    def stated(rows: pd.DataFrame, channel: str, value: float, unit: str) -> pd.DataFrame:
        rows = rows.copy()
        rows["channel"] = channel
        rows["value_number"] = value
        rows["unit"] = unit
        rows["payload"] = None
        return rows

    if pid != "none":
        rows = rpm if pid == "all" else rpm[rpm["timestamp"] < rpm["timestamp"].median()]
        added.append(stated(rows, "THROTTLE_POS", 20.0, "%"))
    if battery:
        added.append(stated(rpm, "CONTROL_MODULE_VOLTAGE", 14.1, "V"))
    if did != "none":
        pulse = frame[frame["channel"] == "DID_1206"].sort_values("timestamp")
        near = pd.merge_asof(
            pulse[["timestamp"]],
            rpm[["timestamp", "value_number"]].sort_values("timestamp"),
            on="timestamp",
            direction="nearest",
        )["value_number"].to_numpy()
        rows = pulse.copy()
        rows["channel"] = "DID_120F"
        rows["value_text"] = [f"{_did_raw(r):04x}" for r in near]
        if did == "off_idle":
            rows = rows[near > 900]
        added.append(rows)
    if added:
        pd.concat(added, ignore_index=True).to_parquet(out / "telemetry-00001.parquet", index=False)
    return out


def _shift(session: Path, to: str) -> None:
    """Move every stored and acquisition time of a session so it starts at ``to``."""
    files = sorted(session.glob("telemetry-*.parquet"))
    first = min(pd.read_parquet(f)["timestamp"].min() for f in files)
    delta = pd.Timestamp(to) - first
    for f in files:
        frame = pd.read_parquet(f)
        frame["timestamp"] = frame["timestamp"] + delta
        meta = frame["metadata_json"].map(json.loads)
        for m in meta:
            if "acquired_at" in m:
                m["acquired_at"] += delta.total_seconds()
        frame["metadata_json"] = meta.map(json.dumps)
        frame.to_parquet(f, index=False)


def _drive(sessions: list[Path], **kw: object) -> SimDesign:
    kw.setdefault("resolver", _resolver)
    # E08.2's evidence on the committed slice is fuel-mv-1's; the builders'
    # default is the simulator's declared plant (E08.11, tested below).
    kw.setdefault("plant_version", "fuel-mv-1")
    return build_drive_design(sessions, PROFILE, BASE, fuel_type="gasoline", **kw)  # type: ignore[arg-type]


def _grid(**over: object) -> SimDesign:
    kw: dict[str, object] = dict(
        profile_dir=PROFILE,
        base_revision=BASE,
        load_basis="fill_fraction_as_manifold_ratio",
        plant_version="fuel-mv-1",
        **GRID_ENV,
    )
    kw.update(over)
    return build_grid_design([800, 1600, 2400, 3200], [0, 25, 50, 75, 100], **kw)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def placed(tmp_path_factory: pytest.TempPathFactory) -> tuple[SimDesign, Path]:
    session = _augmented(tmp_path_factory.mktemp("placed"))
    return _drive([session]), session


# -- the committed slice: every read counted ---------------------------------------------------


def test_every_read_of_the_committed_slice_is_placed_or_counted_by_name() -> None:
    design = _drive([FIXTURE], resolver=_refusing_resolver)
    plant, _ = load_fuel_plant(PROFILE)
    delay = load_b06(PROFILE, plant.delay)
    frame = apply_manifold_refusals(
        build_samples(FIXTURE, plant, delay), plant, resolve_parameters(plant, delay)
    )
    assert design.reads == len(frame) == sum(design.plant_states.values()) > 0
    assert design.plant_states == frame["state"].value_counts().to_dict()
    eligible = design.plant_states["eligible"]
    assert eligible > 0
    # Every eligible read is placed or named; on this slice PID 0x11 was not
    # logged, so none is placed and the empty says which kind it is.
    assert sum(design.placement.values()) == eligible
    assert design.placement == {"throttle_unpaired": eligible}
    assert design.points == []
    assert design.unplaced_dwell_s["throttle_unpaired"] == pytest.approx(design.eligible_dwell_s)
    (check,) = design.sessions
    assert check.revision_at_start == BASE and check.revisions_in_reads == [BASE]
    assert sum(check.plant_states.values()) == check.reads == design.reads


def test_a_placed_design_accounts_for_every_eligible_read_and_its_dwell(
    placed: tuple[SimDesign, Path],
) -> None:
    design, _ = placed
    assert design.points, "the augmented slice places points"
    assert sum(design.placement.values()) == design.plant_states["eligible"]
    assert design.placement["placed"] == len(design.points)
    weight = math.fsum(p.weight for p in design.points)
    assert weight + math.fsum(design.unplaced_dwell_s.values()) == pytest.approx(
        design.eligible_dwell_s, abs=1e-9
    )
    for p in design.points:
        assert p.session == FIXTURE.name and p.throttle_pct == 20.0 and p.battery_v == 14.1
        assert p.throttle_pct_source == "pid:0x11"
        assert p.throttle_pct_lo == p.throttle_pct == p.throttle_pct_hi
        assert p.battery_v_source == "measured"
        assert p.pedal_flag == 0 and p.pedal_flag_source == "stated_zero:not_logged"
        assert p.fill_byte_source == "reconstructed:closed_loop"
        assert p.fill_byte_lo <= p.fill_byte_hi
        assert p.map_kpa_source == design.manifold_model == "speed_density_capped"
        assert p.rail_kpa_source == "working_assumption" and p.rail_kpa == 350.0
        assert p.regime in ("closed_loop", "enrichment", "lean_target", "unknown")
        assert 0 < p.weight <= design.dwell_cap_s  # type: ignore[operator]
    # Carried whole, never collapsed: this invented chain gives real intervals.
    assert any(p.fill_byte_hi > p.fill_byte_lo for p in design.points)


def test_weights_are_dwell_to_the_next_eligible_read_capped_at_the_window(
    placed: tuple[SimDesign, Path],
) -> None:
    """Brute force from the plant's own samples: the eligible dwell is the
    capped gap to the next eligible read, and the last read's cap is the one
    window by which it can differ from the uncapped stretch time."""
    design, session = placed
    plant, _ = load_fuel_plant(PROFILE)
    delay = load_b06(PROFILE, plant.delay)
    params = resolve_parameters(plant, delay)
    frame = apply_manifold_refusals(build_samples(session, plant, delay), plant, params)
    t = np.sort(frame.loc[frame["state"] == "eligible", "t"].to_numpy(dtype=float))
    window = plant.steady.window_s
    brute = 0.0
    for a, b in zip(t, [*t[1:], math.inf], strict=True):
        brute += min(b - a, window)
    assert design.eligible_dwell_s == pytest.approx(brute, abs=1e-9)
    without_last = brute - window
    assert without_last <= design.eligible_dwell_s <= without_last + window
    # Per point: the weight is that read's capped gap, not 1 per read.
    by_t = {float(a): min(b - a, window) for a, b in zip(t, [*t[1:], math.inf], strict=True)}
    for p in design.points:
        assert p.weight == pytest.approx(by_t[p.t])  # type: ignore[index]
    assert len({round(p.weight, 6) for p in design.points}) > 1
    # map_kpa is the plant's own manifold model at the read.
    eligible = frame[frame["state"] == "eligible"].set_index("t")
    rows = eligible.loc[[p.t for p in design.points]]
    maps = predict(plant_inputs(rows.reset_index()), params, plant.geometry).map_kpa
    assert [p.map_kpa for p in design.points] == pytest.approx(list(maps), rel=0, abs=0)


def test_a_slow_drive_and_a_fast_drive_weigh_the_same_driving_equally() -> None:
    cap = 2.0
    slow = np.arange(0.0, 60.0, 0.5)  # 2 Hz
    fast = np.arange(0.0, 60.0, 1 / 15)  # 15 Hz
    assert _dwell(slow, cap).sum() == pytest.approx(_dwell(fast, cap).sum(), abs=cap)
    assert len(fast) > 7 * len(slow)
    # A gap is capped, and the order of the reads given does not matter.
    gaps = _dwell(np.array([10.0, 0.0, 1.0, 30.0]), cap)
    assert list(gaps) == [2.0, 1.0, 2.0, 2.0]


def test_the_hash_is_computed_stable_and_moves_with_one_point(
    tmp_path: Path, placed: tuple[SimDesign, Path]
) -> None:
    design, session = placed
    again = _drive([session])
    assert again.sha256() == design.sha256()
    assert again.canonical_json() == design.canonical_json()
    assert "sha256" not in SimDesign.model_fields
    moved = list(design.points)
    moved[0] = moved[0].model_copy(update={"rpm": moved[0].rpm + 12.5})
    assert design.model_copy(update={"points": moved}).sha256() != design.sha256()
    reweighted = list(design.points)
    reweighted[-1] = reweighted[-1].model_copy(update={"weight": reweighted[-1].weight / 2})
    assert design.model_copy(update={"points": reweighted}).sha256() != design.sha256()
    g = _grid()
    assert _grid().sha256() == g.sha256()
    assert _grid(iat_c=26.0).sha256() != g.sha256()


def test_a_drive_after_the_base_revisions_successor_flash_is_refused_by_name() -> None:
    # The slice is 2026-09-23; ZS44A-k106's successor, ZS44A-20260920, was
    # flashed 2026-09-21T03:08:45Z (tune_revisions.yaml).
    with pytest.raises(SimDesignRefusalError) as caught:
        build_drive_design(
            [FIXTURE], PROFILE, "ZS44A-k106", fuel_type="gasoline", resolver=_resolver
        )
    assert caught.value.reason == "drive_not_on_base_revision"
    assert FIXTURE.name in str(caught.value) and BASE in str(caught.value)
    # A drive before the base revision's flash is refused the same way.
    with pytest.raises(SimDesignRefusalError) as caught:
        build_drive_design(
            [FIXTURE], PROFILE, "ZS44A-20261004", fuel_type="gasoline", resolver=_resolver
        )
    assert caught.value.reason == "drive_not_on_base_revision"


def test_a_drive_straddling_the_successor_flash_is_refused(tmp_path: Path) -> None:
    """Dated by its first sample, a session can still run past the next flash."""
    session = _augmented(tmp_path, pid="none", battery=False)
    _shift(session, "2026-10-04T23:00:00Z")
    with pytest.raises(SimDesignRefusalError) as caught:
        _drive([session])
    assert caught.value.reason == "drive_not_on_base_revision"
    assert "ZS44A-20261004" in str(caught.value)


def test_drive_inputs_are_refused_by_name(tmp_path: Path) -> None:
    def reason(**kw: object) -> str:
        sessions = kw.pop("sessions", [FIXTURE])
        base = kw.pop("base", BASE)
        kw.setdefault("fuel_type", "gasoline")
        kw.setdefault("resolver", _resolver)
        with pytest.raises(SimDesignRefusalError) as caught:
            build_drive_design(sessions, PROFILE, base, **kw)  # type: ignore[arg-type]
        return caught.value.reason

    assert reason(sessions=[]) == "no_sessions"
    assert reason(sessions=[FIXTURE, FIXTURE]) == "duplicate_session"
    assert reason(sessions=[tmp_path / "2026-09-23T150000"]) == "session_missing"
    assert reason(base="ZS44A-nope") == "base_revision_unknown"
    assert reason(base="ZS44A-k105") == "base_image_unknown"
    assert reason(fuel_type="kerosene") == "fuel_type_unknown"
    synthetic = tmp_path / "_sim-0123456789ab"
    shutil.copytree(FIXTURE, synthetic)
    assert reason(sessions=[synthetic]) == "synthetic_as_observed"


def test_a_fill_byte_reconstructed_on_another_image_is_refused(tmp_path: Path) -> None:
    session = _augmented(tmp_path)
    with pytest.raises(SimDesignRefusalError) as caught:
        _drive([session], resolver=lambda name: _calibration(name, image_sha256="0" * 64))
    assert caught.value.reason == "fill_image_mismatch"


def test_without_the_image_every_point_is_unplaced_under_n02s_reason(tmp_path: Path) -> None:
    """N02 names a row by its first failing state (the slice never logged
    FUEL_STATUS, so that comes first); without an image there is no byte, so
    the row's own N02 state and reason is the point's. With the image the
    same rows place (``placed`` above): the byte is reconstructed whatever
    the error route's gates say."""
    design = _drive([_augmented(tmp_path)], resolver=_refusing_resolver)
    assert design.points == []
    assert design.placement
    assert all(
        k == "fill_byte_unpaired" or k.startswith("fill_byte:input_missing:")
        for k in design.placement
    )
    assert design.placement.get("fill_byte:input_missing:obd:FUEL_STATUS", 0) > 0


# -- grid designs ------------------------------------------------------------------------------


def test_the_grid_refuses_an_unstated_environment_by_name() -> None:
    for name in GRID_ENV:
        with pytest.raises(SimDesignRefusalError) as caught:
            _grid(**{name: None})
        assert caught.value.reason == "environment_unstated"
        assert name in str(caught.value)
    with pytest.raises(SimDesignRefusalError) as caught:
        build_grid_design([800, 1600], [0, 50], profile_dir=PROFILE, base_revision=BASE)
    assert caught.value.reason == "environment_unstated"
    for name in ("baro_kpa", "iat_c"):
        assert name in str(caught.value)
    with pytest.raises(SimDesignRefusalError) as caught:
        _grid(load_basis=None)
    assert caught.value.reason == "load_basis_unstated"


@pytest.mark.parametrize(
    ("rpm", "load", "reason"),
    [
        ([800], [0, 50], "grid_edges_invalid"),
        ([800, 800], [0, 50], "grid_edges_invalid"),
        ([800, 1600], [50, 25], "grid_edges_invalid"),
        ([800, 1600], [0, 120], "load_outside_model"),
        ([800, 1600], [-5, 50], "load_outside_model"),
    ],
)
def test_a_malformed_grid_is_refused_by_name(
    rpm: list[float], load: list[float], reason: str
) -> None:
    kw: dict[str, object] = dict(
        profile_dir=PROFILE, base_revision=BASE, load_basis="fill_fraction_as_manifold_ratio"
    )
    with pytest.raises(SimDesignRefusalError) as caught:
        build_grid_design(rpm, load, **kw, **GRID_ENV)  # type: ignore[arg-type]
    assert caught.value.reason == reason


@pytest.mark.parametrize("plant_version", ["fuel-mv-1", "fuel-mv-3"])
def test_grid_maf_is_solved_through_the_plants_own_manifold_model(plant_version: str) -> None:
    design = _grid(plant_version=plant_version)
    assert design.plant_version == plant_version
    assert len(design.points) == 3 * 4 and design.placement == {"placed": 12}
    plant, _ = load_fuel_plant(PROFILE, plant_version)
    params = resolve_parameters(plant, load_b06(PROFILE, plant.delay))
    for p in design.points:
        assert design.grid is not None
        load_c = (design.grid.load_edges[p.cell_col] + design.grid.load_edges[p.cell_col + 1]) / 2  # type: ignore[index, operator]
        assert p.map_kpa == pytest.approx(load_c / 100 * 80.0, rel=1e-9)
        # Recomputed independently: the plant at this MAF gives this MAP.
        from src.analysis.fuel_plant import PlantInputs

        out = predict(
            PlantInputs(
                pulse_ms=np.array([3.0]),
                ecu_dead_time_ms=np.array([0.5]),
                rpm=np.array([p.rpm]),
                maf_gps=np.array([p.maf_gps]),
                baro_kpa=np.array([p.baro_kpa]),
                iat_c=np.array([p.iat_c]),
                rail_kpa=np.array([p.rail_kpa]),
            ),
            params,
            plant.geometry,
        )
        assert float(out.map_kpa[0]) == p.map_kpa
        assert (
            p.maf_source
            == f"manifold_model_inverse:{params.manifold_model}:fill_fraction_as_manifold_ratio"
        )
        # The cell centre's byte: the whole counts either side, never rounded.
        raw = load_c / (100 / 224)
        assert p.fill_byte_lo == math.floor(raw + 1e-9) and p.fill_byte_hi == math.ceil(raw - 1e-9)
        assert p.weight == 1.0 and p.regime == "unknown"
    # More load or more rpm needs more air.
    by_cell = {(p.cell_row, p.cell_col): p.maf_gps for p in design.points}
    assert by_cell[(0, 0)] < by_cell[(0, 3)] < by_cell[(2, 3)]


@pytest.mark.parametrize("version", ["fuel-mv-2", "fuel-mv-3"])
def test_a_grid_outside_the_fitted_ve_range_is_refused_by_name_or_counted(version: str) -> None:
    """E08.11. A grid states one baro and one IAT: outside the frozen VE
    fit's range (baro 79-82 kPa, IAT 23-50 C) every cell would be outside it,
    so the grid is refused by name rather than built empty (63 kPa is a real
    trip's altitude, which the fit has not seen). An rpm outside the fit's
    bands is a per-cell count, never placed."""
    for env, named in (
        ({"baro_kpa": 63.0}, "baro_kpa 63 (fit 79-82)"),
        ({"iat_c": 15.0}, "iat_c 15 (fit 23-50)"),
    ):
        with pytest.raises(SimDesignRefusalError) as refused:
            _grid(plant_version=version, **env)
        assert refused.value.reason == "outside_ve_fit"
        assert named in str(refused.value)
    edges = {"rpm": [6000, 6400, 7000], "load": [0, 50, 100]}
    kw = {k: v for k, v in GRID_ENV.items()}
    design = build_grid_design(
        edges["rpm"],
        edges["load"],
        profile_dir=PROFILE,
        base_revision=BASE,
        load_basis="fill_fraction_as_manifold_ratio",
        plant_version=version,
        **kw,  # type: ignore[arg-type]
    )
    assert design.placement == {"outside_ve_fit": 2, "placed": 2}
    assert {p.cell_row for p in design.points} == {0}
    # fuel-mv-1 runs one working VE and has no fit range to be outside.
    assert _grid(baro_kpa=63.0).placement == {"placed": 12}


def test_designs_default_to_the_simulators_declared_plant() -> None:
    """E08.11: with no plant named, both builders take the plant
    ``sim_kit.yaml`` declares (fuel-mv-2), never the contract's fuel-mv-1, so a
    design and the simulator's run agree by default. On fuel-mv-2 the
    committed slice's 20-22 C reads are counted ``outside_ve_fit`` by name,
    and every read is still placed or counted."""
    from src.analysis.sim_kit import sim_plant_version

    assert sim_plant_version(PROFILE) == "fuel-mv-2"
    grid = _grid(plant_version=None)
    assert (grid.plant_version, grid.manifold_model) == ("fuel-mv-2", "speed_density_refusing")
    assert any("VE(rpm)" in a and "na-stock-2026-10-08.mv2" in a for a in grid.assumptions)
    drive = _drive([FIXTURE], plant_version=None)
    assert drive.plant_version == "fuel-mv-2"
    assert drive.plant_states.get("outside_ve_fit", 0) > 0
    assert sum(drive.plant_states.values()) == drive.reads
    assert sum(drive.placement.values()) == drive.plant_states["eligible"]
    mv1 = _drive([FIXTURE])
    assert mv1.plant_version == "fuel-mv-1" and "outside_ve_fit" not in mv1.plant_states


# -- storage -----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["drive", "grid"])
def test_a_design_round_trips_byte_identical(
    tmp_path: Path, kind: str, placed: tuple[SimDesign, Path]
) -> None:
    design = placed[0] if kind == "drive" else _grid()
    directory = write_design(design, tmp_path / "a")
    assert directory.name == design.sha256()
    assert sorted(p.name for p in directory.iterdir()) == ["design.json", "design.parquet"]
    loaded = load_design(directory)
    assert loaded == design
    assert loaded.canonical_json() == design.canonical_json()
    assert loaded.sha256() == design.sha256()
    rewritten = write_design(loaded, tmp_path / "b")
    for name in ("design.json", "design.parquet"):
        assert (rewritten / name).read_bytes() == (directory / name).read_bytes()


def test_writing_is_refused_inside_the_checkout_and_a_tampered_design_on_load(
    tmp_path: Path,
) -> None:
    design = _grid()
    for root in (REPO_ROOT / "data" / "_sim_designs", REPO_ROOT, REPO_ROOT.parent):
        with pytest.raises(SimDesignRefusalError) as caught:
            write_design(design, root)
        assert caught.value.reason == "inside_checkout"
    directory = write_design(design, tmp_path)
    meta = json.loads((directory / "design.json").read_text())
    meta["base_revision"] = "ZS44A-20261004"
    (directory / "design.json").write_text(json.dumps(meta))
    with pytest.raises(SimDesignRefusalError) as caught:
        load_design(directory)
    assert caught.value.reason == "design_hash_mismatch"
    with pytest.raises(SimDesignRefusalError) as caught:
        load_design(tmp_path / "nothing")
    assert caught.value.reason == "design_missing"


# -- what a run may do with it -----------------------------------------------------------------


def _scenario(points: tuple, learned_source: str = "design: stated trims") -> PairedScenario:  # type: ignore[type-arg]
    identity = EcuIdentity(
        program_id="ZS44A", image_sha256=BASE_SHA, slot=1, slot_source="default_slot"
    )
    return PairedScenario(
        mode="conditional_rom_comparison",
        base=identity,
        candidate=identity,
        plant=PlantIdentity(
            version="fuel-mv-1", hardware_configuration="na_stock_injectors_2026_10"
        ),
        points=points,
        learned=LearnedState(
            stft_pct=0.0,
            ltft_pct=0.0,
            throttle_closed_learned=0.0,
            idle_air_learned=0.0,
            evap_purge_area=0.0,
            source=learned_source,
        ),
    )


def test_design_sources_pass_the_recording_guard_and_a_session_source_is_still_refused(
    placed: tuple[SimDesign, Path],
) -> None:
    design, _ = placed
    points = design.operating_points([p.fill_byte_lo for p in design.points])
    assert {p.source for p in points} == {f"design:{design.sha256()}"}
    assert points[0].conditioned == {"fuel_comp_load_byte": float(design.points[0].fill_byte_lo)}
    _refuse_recorded_sources(_scenario(tuple(points)))  # the E07.6 guard, unchanged
    grid = _grid()
    _refuse_recorded_sources(
        _scenario(tuple(grid.operating_points([p.fill_byte_hi for p in grid.points])))
    )
    named = dataclasses.replace(points[0], source=f"design: from {FIXTURE.name}")
    with pytest.raises(SimulationRefusalError) as caught:
        _refuse_recorded_sources(_scenario((named,)))
    assert caught.value.reason == "recorded_value_refused"


def test_a_fill_byte_outside_the_points_interval_is_refused(placed: tuple[SimDesign, Path]) -> None:
    design, _ = placed
    wide = next(p for p in design.points if p.fill_byte_hi > p.fill_byte_lo)
    design.operating_point(wide.index, fill_byte=wide.fill_byte_hi)
    for bad in (wide.fill_byte_lo - 1, wide.fill_byte_hi + 1, wide.fill_byte_lo + 0.5):
        with pytest.raises(SimDesignRefusalError) as caught:
            design.operating_point(wide.index, fill_byte=bad)  # type: ignore[arg-type]
        assert caught.value.reason == "fill_byte_outside_interval"
    with pytest.raises(SimDesignRefusalError) as caught:
        design.operating_points([0])
    assert caught.value.reason == "fill_bytes_mismatch"


def test_a_run_on_another_base_image_is_refused() -> None:
    design = _grid()
    require_base(design, BASE_SHA)
    other = _image_sha("ZS44A-20261004")
    with pytest.raises(SimDesignRefusalError) as caught:
        require_base(design, other)
    assert caught.value.reason == "design_revision_mismatch"


def test_summary_reports_points_weight_and_states(placed: tuple[SimDesign, Path]) -> None:
    design, _ = placed
    s = summary(design)
    assert s["points"] == len(design.points) and s["sha256"] == design.sha256()
    assert s["weight"] == pytest.approx(math.fsum(p.weight for p in design.points))
    assert sum(s["weight_by_regime"].values()) == pytest.approx(s["weight"])


# -- nothing here talks to the ECU -------------------------------------------------------------

_WRITE_PATHS = ("src.control", "src.daemon", "src.dashboard", "src.mcp_server", "can", "serial")
_ALLOWED_COLLECTION = {"src.collection", "src.collection.kline", "src.collection.kline.names"}


def test_building_and_writing_designs_loads_no_ecu_facing_package(tmp_path: Path) -> None:
    script = textwrap.dedent(
        f"""
        import json, sys
        from pathlib import Path
        from src.analysis.sim_design import build_drive_design, build_grid_design, write_design
        from src.analysis.fuel_comp_rom import FuelCompRomRefusalError
        profile = Path("data/nissan-xterra-2008")

        def refuse(name):
            raise FuelCompRomRefusalError("image_not_found", name)

        drive = build_drive_design(
            [Path({str(FIXTURE)!r})], profile, {BASE!r}, fuel_type="gasoline", resolver=refuse
        )
        grid = build_grid_design(
            [800, 1600], [0, 50], profile_dir=profile, base_revision={BASE!r},
            load_basis="fill_fraction_as_manifold_ratio", baro_kpa=80.0, iat_c=25.0,
            coolant_c=88.0, battery_v=14.0, rail_kpa="working_assumption", throttle_pct=20.0,
            pedal_flag=0,
        )
        write_design(drive, Path({str(tmp_path)!r}))
        write_design(grid, Path({str(tmp_path)!r}))
        print(json.dumps(sorted(sys.modules)))
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        timeout=300,
    )
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    reached = [
        m
        for m in loaded
        if any(m == p or m.startswith(p + ".") for p in _WRITE_PATHS)
        or (m.startswith("src.collection") and m not in _ALLOWED_COLLECTION)
    ]
    assert reached == []


# -- the real images (private: skips when absent) ----------------------------------------------


@pytest.mark.skipif(
    not OPERATOR_ROMS["r20260920"].exists(), reason="operator ROM ZS44A-Tuned-20260920 not here"
)
def test_the_real_base_image_reconstructs_fill_bytes_on_the_augmented_slice(
    tmp_path: Path,
) -> None:
    design = build_drive_design(
        [_augmented(tmp_path)],
        PROFILE,
        BASE,
        fuel_type="gasoline",
        rom_dirs=[OPERATOR_ROMS["r20260920"].parent],
    )
    assert design.points
    assert sum(design.placement.values()) == design.plant_states["eligible"]
    assert all(0 <= p.fill_byte_lo <= p.fill_byte_hi <= 224 for p in design.points)


def test_design_points_run_through_the_paired_comparison_unchanged() -> None:
    """The contract and E07.6's guards accept a design's points as they are."""
    from src.analysis.ecu_application import program_sha256
    from src.analysis.paired_scenarios import load_kit, run_paired
    from tests.test_paired_scenarios import LEARNED, _identity, _image

    kit = load_kit(PROFILE)
    image = _image(kit.model, kit.plant)
    identity = kit.model.program.identity.model_copy(
        update={"sha256": program_sha256(image, kit.model)}
    )
    kit = dataclasses.replace(
        kit,
        model=kit.model.model_copy(
            update={"program": kit.model.program.model_copy(update={"identity": identity})}
        ),
    )
    grid = build_grid_design(
        [4000, 6000],
        [50, 75, 100],
        profile_dir=PROFILE,
        base_revision=BASE,
        load_basis="fill_fraction_as_manifold_ratio",
        **GRID_ENV,  # type: ignore[arg-type]
    )
    points = tuple(grid.operating_points([p.fill_byte_lo for p in grid.points]))
    scenario = PairedScenario(
        mode="conditional_rom_comparison",
        base=_identity(image),
        candidate=_identity(image),
        plant=PlantIdentity(
            version=kit.plant.plant_version,
            hardware_configuration=kit.plant.hardware_configuration,
        ),
        points=points,
        learned=LEARNED,
    )
    result = run_paired(kit, scenario, image, bytes(image))
    assert result.outcome == "identical" and len(result.points) == len(points) == 2
    assert all(p["outputs"]["base_fuel_schedule"]["status"] == "supported" for p in result.points)


# -- rework 2026-10-15: grid floor, session order, throttle and battery routes -----------------


def test_a_grid_cell_below_the_manifold_models_floor_is_counted_not_placed() -> None:
    """fuel-mv-3's residual gas keeps the manifold above a floor: a 2.5 %
    fill target (2 kPa at 80 kPa) is unreachable. Verifier finding: it was
    placed at 4.17 kPa with MAF 3.9e-31."""
    design = build_grid_design(
        [800, 1600, 2400],
        [0, 5, 25],
        profile_dir=PROFILE,
        base_revision=BASE,
        load_basis="fill_fraction_as_manifold_ratio",
        plant_version="fuel-mv-3",
        **GRID_ENV,  # type: ignore[arg-type]
    )
    assert design.placement == {"load_below_model_floor": 2, "placed": 2}
    assert {p.cell_col for p in design.points} == {1}
    for p in design.points:
        assert p.map_kpa == pytest.approx(15.0 / 100 * 80.0, rel=1e-9)
    # fuel-mv-1 has no floor: the same cells place at their targets.
    capped = build_grid_design(
        [800, 1600, 2400],
        [0, 5, 25],
        profile_dir=PROFILE,
        base_revision=BASE,
        load_basis="fill_fraction_as_manifold_ratio",
        **GRID_ENV,  # type: ignore[arg-type]
    )
    assert capped.placement == {"placed": 4}
    assert min(p.map_kpa for p in capped.points) == pytest.approx(2.0, rel=1e-9)


def test_sessions_are_ordered_by_their_data_not_their_names(tmp_path: Path) -> None:
    """Names sort opposite to start times: ``_capture-alpha`` sorts before
    ``_capture-tuning`` but its data starts two days later."""
    early = _augmented(tmp_path / "e", FIXTURE.name)
    late = _augmented(tmp_path / "l", "_capture-alpha-2026-09-23T142926")
    _shift(late, "2026-09-25T14:36:00Z")
    assert sorted([late.name, early.name]) == [late.name, early.name]
    design = _drive([late, early])
    assert [s.session for s in design.sessions] == [early.name, late.name]
    order = [p.session for p in design.points]
    rank = {early.name: 0, late.name: 1}
    assert order == sorted(order, key=lambda name: rank[str(name)])
    assert set(order) == {early.name, late.name}
    one = _drive([early])
    (single,) = one.sessions
    for s in design.sessions:
        assert s.plant_states == single.plant_states and s.placement == single.placement
        assert s.eligible_dwell_s == pytest.approx(single.eligible_dwell_s, abs=1e-9)
        assert s.placed_weight == pytest.approx(single.placed_weight, abs=1e-9)
    assert design.reads == 2 * one.reads
    assert design.plant_states == {k: 2 * v for k, v in one.plant_states.items()}
    assert design.placement == {k: 2 * v for k, v in one.placement.items()}
    assert design.eligible_dwell_s == pytest.approx(
        math.fsum(s.eligible_dwell_s for s in design.sessions), abs=1e-9
    )
    assert math.fsum(p.weight for p in design.points) == pytest.approx(
        math.fsum(s.placed_weight for s in design.sessions), abs=1e-9
    )


def _forward_pct(counts: int) -> float:
    """PID 0x11's handler by hand: A = min(255, w x 100 // 321), A x 100 / 255."""
    return min(255, max(0, counts) * 100 // 321) * 100 / 255


def test_without_pid_0x11_throttle_comes_from_did_0x120f_through_n02s_closed_position(
    tmp_path: Path,
) -> None:
    design = _drive([_augmented(tmp_path, pid="none", did="all")])
    assert design.points
    (check,) = design.sessions
    closed = check.throttle_closed_position
    assert closed is not None and closed["interval"] == [98, 107] and closed["mode_raw"] == 130
    for p in design.points:
        assert p.throttle_pct_source == "did:0x120F"
        raw = _did_raw(p.rpm)
        lo, hi = _forward_pct(raw - 107), _forward_pct(raw - 98)
        assert (p.throttle_pct_lo, p.throttle_pct_hi) == (lo, hi)
        assert p.throttle_pct == (lo + hi) / 2
    assert {round(p.throttle_pct_lo, 3) for p in design.points} >= {round(_forward_pct(23), 3)}


def test_pid_0x11_is_used_where_co_observed_and_the_did_elsewhere(tmp_path: Path) -> None:
    """PID 0x11 reads stop halfway; DID 0x120F runs throughout. The first half
    places on the PID. In the second half the DID supplies the throttle (no
    read is ``throttle_unpaired``), but N02 reconstructs the fill byte on the
    PID route whenever the session captured PID 0x11 at all, so those reads
    have no byte and are unplaced under N02's own (first) reason: on this
    slice ``input_missing:obd:FUEL_STATUS``."""
    session = _augmented(tmp_path, pid="first_half", did="all")
    design = _drive([session])
    assert {p.throttle_pct_source for p in design.points} == {"pid:0x11"}
    assert "throttle_unpaired" not in design.placement
    assert design.placement.get("fill_byte:input_missing:obd:FUEL_STATUS", 0) > 0
    pid_only = _drive([_augmented(tmp_path / "p", pid="first_half")])
    assert pid_only.placement.get("throttle_unpaired", 0) > 0
    assert summary(design)["points_by_throttle_source"] == {"pid:0x11": len(design.points)}


def test_a_did_with_no_closed_position_estimate_is_named_apart_from_no_throttle(
    tmp_path: Path,
) -> None:
    design = _drive([_augmented(tmp_path, pid="none", did="off_idle")])
    assert design.points == []
    assert set(design.placement) == {"throttle_closed_position_unestimated", "throttle_unpaired"}
    (check,) = design.sessions
    assert check.throttle_closed_position is not None
    assert check.throttle_closed_position["reason"].startswith("closed_position_unestimated")


def test_an_unmeasured_battery_is_placed_at_the_stated_voltage_or_refused(
    tmp_path: Path,
) -> None:
    session = _augmented(tmp_path, battery=False)
    with pytest.raises(SimDesignRefusalError) as caught:
        _drive([session])
    assert caught.value.reason == "battery_v_unstated"
    design = _drive([session], battery_v_when_unmeasured=13.8)
    assert design.points and design.battery_v_when_unmeasured == 13.8
    assert {(p.battery_v, p.battery_v_source) for p in design.points} == {(13.8, "unmeasured")}
    assert "battery_v_missing" not in design.placement
    s = summary(design)
    assert s["battery_unmeasured_points"] == len(design.points)
    assert s["points_by_battery_source"] == {"unmeasured": len(design.points)}
    assert _drive([session], battery_v_when_unmeasured=13.9).sha256() != design.sha256()
    # Measured everywhere: the stated value is not needed, and not recorded.
    measured = _drive([_augmented(tmp_path / "m")], battery_v_when_unmeasured=13.8)
    assert measured.battery_v_when_unmeasured is None
    assert {p.battery_v_source for p in measured.points} == {"measured"}


def test_per_point_sources_are_in_the_points_table_and_the_hash(
    tmp_path: Path, placed: tuple[SimDesign, Path]
) -> None:
    design, _ = placed
    schema = pq.read_schema(  # type: ignore[no-untyped-call, unused-ignore]
        write_design(design, tmp_path) / "design.parquet"
    )
    columns = set(schema.names)
    for name in ("map_kpa_source", "rail_kpa_source", "battery_v_source", "throttle_pct_source"):
        assert name in columns
        assert name in json.loads(design.canonical_json())["points"][0]
    for field, value in (
        ("map_kpa_source", "speed_density_load_dependent"),
        ("rail_kpa_source", "measured"),
        ("battery_v_source", "unmeasured"),
    ):
        changed = list(design.points)
        changed[0] = changed[0].model_copy(update={field: value})
        assert design.model_copy(update={"points": changed}).sha256() != design.sha256()


def test_a_point_above_ambient_anywhere_in_the_manifold_brackets_is_counted_not_placed() -> None:
    """E08.11 rework: a model-MAP point is placed only if MAP stays at or below
    ambient across the declared bracket of every manifold-model input (the VE
    floor [0.4, 1.0] in the floor bands, in practice). On the W09 form grid
    (fuel-mv-2, 81 kPa, 25 C) that counts 9 cells: 800 rpm from ~57 % fill
    (VE 0.4 under a 0.7 floor) and 1,200/1,600 rpm at the top cell (that
    band's envelope 0.677). fuel-mv-1 caps and counts none."""
    from src.analysis.fuel_plant import PlantInputs, load_b06, load_fuel_plant, predict
    from src.analysis.fuel_plant import resolve_parameters as resolve

    rpm = [200.0 + 400.0 * i for i in range(17)]
    load = [
        3.571429, 10.71429, 17.85714, 25.0, 32.14286, 39.28571, 46.42857, 53.57143,
        60.71429, 67.85714, 75.0, 82.14286, 89.28571, 96.42857, 100.0,
    ]  # fmt: skip
    env = {**GRID_ENV, "baro_kpa": 81.0, "rail_kpa": 350.0}
    kw: dict[str, object] = dict(
        profile_dir=PROFILE, base_revision=BASE, load_basis="fill_fraction_as_manifold_ratio"
    )
    mv2 = build_grid_design(rpm, load, plant_version="fuel-mv-2", **kw, **env)  # type: ignore[arg-type]
    assert mv2.placement == {
        "manifold_above_ambient_in_bracket": 9,
        "outside_ve_fit": 14,
        "placed": 201,
    }
    placed = {(p.cell_row, p.cell_col) for p in mv2.points}
    counted = {(i, j) for i in range(16) for j in range(14)} - placed - {(0, j) for j in range(14)}
    # 800 rpm from the 57.14 % cell (0.5714 x 0.7 / 0.4 = 1.0000 x baro, on the
    # line) up; 1,200 and 1,600 rpm at the 98.2 % cell (x 0.7 / 0.67674 > 1).
    assert counted == {(1, j) for j in range(7, 14)} | {(2, 13), (3, 13)}
    mv1 = build_grid_design(rpm, load, plant_version="fuel-mv-1", **kw, **env)  # type: ignore[arg-type]
    assert mv1.placement == {"placed": 224}
    # Every placed point stays below ambient at both ends of the VE bracket.
    plant, _ = load_fuel_plant(PROFILE, "fuel-mv-2")
    delay = load_b06(PROFILE, plant.delay)
    for ve in (0.4, 1.0):
        out = predict(
            PlantInputs(
                pulse_ms=np.full(len(mv2.points), 2.0),
                ecu_dead_time_ms=np.zeros(len(mv2.points)),
                rpm=np.array([p.rpm for p in mv2.points]),
                maf_gps=np.array([p.maf_gps for p in mv2.points]),
                baro_kpa=np.full(len(mv2.points), 81.0),
                iat_c=np.full(len(mv2.points), 25.0),
                rail_kpa=np.full(len(mv2.points), 350.0),
            ),
            resolve(plant, delay, overrides={"manifold_pressure": ve}),
            plant.geometry,
        )
        assert out.manifold_refusal is not None and (out.manifold_refusal == "").all(), ve
