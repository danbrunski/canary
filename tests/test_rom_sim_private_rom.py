"""E08.7 item 3: the Fuel Comp restore through the workbench, on the real images.

Skipped by name when the operator's images (``tests.rom_fixtures.
OPERATOR_ROMS``: ZS44A-20260920 and ZS44A-20261004) or the drive are absent,
as ``tests/test_fuel_comp_restore_private_rom.py`` is, so nothing passes
because something was missing.

**The drive.** ``_capture-tuning-2026-09-23T142926`` under
``~/canary-data/sessions/nissan-xterra-2008`` (a pre-flash drive on
ZS44A-20260920). The committed slice of the same drive
(``tests/fixtures/fuel_plant/``) places no point: it carries no throttle read
(PID 0x11 or DID 0x120F) and no FUEL_STATUS, so E08.2 counts all 152 eligible
reads ``throttle_unpaired``; the ram-study slice (``tests/fixtures/fuel_comp/``)
polls no MAF and has no eligible read at all. So this file needs the drive as
well as the images.

**Two code paths.** The workbench's BFS (``paired_steady.steady_bfs`` ->
``air_charge_twin``: Tp from K, airflow and speed, ``(Tp x FC) >> 7``, the
ceiling) is compared per (point, draw) with ``fuel_comp_restore.
multiplier_change`` at the same rpm word and the same drawn fill byte, which
reads each image's Fuel Comp cells and axes through ``fuel_comp_rom`` and
computes no BFS at all. They do share one primitive: both interpolate the
cells with ``src.decode.ecm_lookup.lookup_2d`` (the twin through
``src.decode.air_charge.rpm_load_lookup``), so a fault inside that bilinear
lookup would move both sides alike. So the lookup is checked on its own at
the table's nodes against the raw image bytes
(``test_the_shared_lookup_returns_the_raw_cell_at_every_node``). Everything
else (reading the tables, the axes, the rpm word, K, Tp, the product, the
ceiling, the draws, the CLI) is on one side only.

**The identity, to the word.** K, airflow and speed are the same on both
images (``check_comparable``), so BFS is ``4 x floor(Tp x FC / 128)``
(``fuel_comp_product``, ``>> 7``, then ``steady_state_bfs``, ``<< 2``) with one
Tp on both sides: ``|BFS_new - BFS_old x FC_new / FC_old| < 4 x max(1, FC_new /
FC_old)`` at every pair below the ceiling, exactly, with no tolerance chosen
from the data.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis.fuel_comp_inputs import load_fuel_comp_declaration, rpm_word
from src.analysis.fuel_comp_restore import InputPoint, check_comparable, multiplier_change
from src.analysis.fuel_comp_rom import (
    FuelCompCalibration,
    FuelCompRomRefusalError,
    RevisionImageResolver,
)
from src.analysis.paired_scenarios import load_kit
from src.analysis.paired_scenarios import rpm_word as sim_rpm_word
from src.analysis.rom_image import load_image
from src.analysis.rom_map import RomMap, load_rom_map
from src.analysis.sim_design import SimDesign, load_design
from src.vehicle.tune_revisions import load_tune_revisions
from tests.rom_fixtures import OPERATOR_ROMS, PROFILE
from tools import rom_sim

OLD, NEW = "ZS44A-20260920", "ZS44A-20261004"
SESSION = "_capture-tuning-2026-09-23T142926"
SESSIONS_ROOT = Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008"
ROM_DIR = OPERATOR_ROMS["r20260920"].parent
DRAWS = 3
SEED = 1

pytestmark = pytest.mark.slow


def _cli(*argv: str | Path) -> dict[str, Any]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = rom_sim.main([str(a) for a in argv])
    payload: dict[str, Any] = json.loads(out.getvalue())
    assert code == 0, payload
    return payload


@pytest.fixture(scope="module")
def images() -> tuple[FuelCompCalibration, FuelCompCalibration]:
    declaration = load_fuel_comp_declaration(PROFILE)
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    resolver = RevisionImageResolver(
        registry,
        load_rom_map(PROFILE),
        declaration.rom,
        rom_dirs=[ROM_DIR],
        slot=declaration.program.map_slot,
    )
    try:
        old, new = resolver.resolve(OLD), resolver.resolve(NEW)
    except FuelCompRomRefusalError as exc:
        if exc.reason == "image_not_found":
            pytest.skip(f"private ROMs not on this machine: {exc.message}")
        raise
    if not (SESSIONS_ROOT / SESSION).is_dir():
        pytest.skip(f"private drive not on this machine: {SESSIONS_ROOT / SESSION}")
    check_comparable(old, new)  # same load-byte chain, K and slot
    return old, new


#: E08.7 item 3 was recorded on fuel-mv-1; the simulator now runs fuel-mv-2
#: (E08.11). BFS does not read VE given MAF, so the identity holds on both.
PLANTS = ("fuel-mv-1", "fuel-mv-2")


@pytest.fixture(scope="module", params=PLANTS)
def restore_run(
    request: pytest.FixtureRequest,
    images: tuple[FuelCompCalibration, FuelCompCalibration],
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    plant = str(request.param)
    root = tmp_path_factory.mktemp("restore")
    _cli(
        "design", "drives", "--base-revision", OLD, "--sessions", SESSION,
        "--sessions-root", SESSIONS_ROOT, "--fuel-type", "gasoline",
        "--battery-v-when-unmeasured", "14.0", "--rom-dir", ROM_DIR, "--out", root / "designs",
        "--plant-version", plant,
    )  # fmt: skip
    (design_dir,) = (root / "designs").iterdir()
    new_file = OPERATOR_ROMS["r20261004"]
    # Trims 0 / 0 and the reflash event, as E08.5's real case: they decide
    # nothing on the ECU side this file checks.
    _cli(
        "run", "--base", "--revision", OLD, "--candidate-rom", new_file, "--design", design_dir,
        "--draws", str(DRAWS), "--seed", str(SEED), "--stft-pct", "0", "--ltft-pct", "0",
        "--candidate-events", "ecu_reflash", "--rom-dir", ROM_DIR, "--out", root / "run",
        "--plant-version", plant,
    )  # fmt: skip
    design = load_design(design_dir)
    traces = pd.read_parquet(root / "run" / "traces.parquet")
    return {
        "plant": plant,
        "design": design,
        "pairs": per_pair_changes(design, traces, *images),
        "result": json.loads((root / "run" / "result.json").read_text()),
        "traces": traces,
    }


def _ceiling_words() -> dict[str, int]:
    """Each image's BFS ceiling, read from ``bfs_ceiling`` here, not through the twin."""
    rom_map = load_rom_map(PROFILE)
    table = rom_map.table("bfs_ceiling")
    at = rom_map.table_address(table, None)
    out = {}
    for key in ("r20260920", "r20261004"):
        image = load_image(OPERATOR_ROMS[key].read_bytes(), rom_map).image
        curve = image[at : at + table.rows * table.cols]
        # A flat curve makes the clamp one constant; a shaped one would need
        # the interpolation, which this file does not re-derive.
        assert len(set(curve)) == 1, (key, list(curve))
        out[key] = curve[0] << 8
    return out


def per_pair_changes(
    design: SimDesign,
    traces: pd.DataFrame,
    old: FuelCompCalibration,
    new: FuelCompCalibration,
) -> pd.DataFrame:
    """One row per (point, draw): the simulated BFS on both images, and both
    images' Fuel Comp multiplier at that point's rpm word and drawn fill byte."""
    declaration = load_fuel_comp_declaration(PROFILE)
    contract = load_kit(PROFILE).contract
    bfs = traces[traces["output"] == "base_fuel_schedule"].sort_values(["point", "draw"])
    rows = []
    for index, draw, fill_byte, bfs_old, bfs_new in zip(
        bfs["point"].to_numpy(dtype=int),
        bfs["draw"].to_numpy(dtype=int),
        bfs["fill_byte"].to_numpy(dtype=int),
        bfs["base"].to_numpy(dtype=float),
        bfs["candidate"].to_numpy(dtype=float),
        strict=True,
    ):
        point = design.points[int(index)]
        word = rpm_word(point.rpm, declaration)  # N02's conversion, exact or refused
        assert word == sim_rpm_word(point.rpm, contract), point.rpm
        byte = int(fill_byte)
        assert point.fill_byte_lo <= byte <= point.fill_byte_hi
        change = multiplier_change(
            old,
            new,
            InputPoint(
                session=SESSION,
                route="design",
                t=float(point.t or 0.0),
                byte_lo=byte,
                byte_hi=byte,
                word_lo=word,
                word_hi=word,
                reason=None,
            ),
        )
        # One word and one byte: the rectangle is a point, so one multiplier each.
        assert change.fc_old[0] == change.fc_old[1] and change.fc_new[0] == change.fc_new[1]
        rows.append(
            {
                "point": int(index),
                "draw": int(draw),
                "rpm": point.rpm,
                "byte": byte,
                "regime": point.regime,
                "bfs_old": float(bfs_old),
                "bfs_new": float(bfs_new),
                "fc_old": change.fc_old[0],
                "fc_new": change.fc_new[0],
            }
        )
    frame = pd.DataFrame(rows)
    frame["fc_ratio"] = frame["fc_new"] / frame["fc_old"]
    frame["bfs_change_pct"] = 100.0 * (frame["bfs_new"] / frame["bfs_old"] - 1.0)
    return frame


def test_simulated_bfs_moves_by_each_points_fuel_comp_multiplier_ratio(
    images: tuple[FuelCompCalibration, FuelCompCalibration], restore_run: dict[str, Any]
) -> None:
    old, new = images
    design = restore_run["design"]
    pairs = restore_run["pairs"]
    assert len(pairs) == len(design.points) * DRAWS
    assert len(design.points) >= 100
    assert pairs[["bfs_old", "bfs_new"]].notna().all().all()
    ceiling = _ceiling_words()
    clamped = (pairs["bfs_old"] >= ceiling["r20260920"]) | (
        pairs["bfs_new"] >= ceiling["r20261004"]
    )
    free = pairs[~clamped]
    # Every pair below the ceiling (all of them on this drive: the ceiling is
    # 255 << 8 on both images and the highest BFS is ~52k), open loop and
    # closed loop alike, since BFS is computed before the loop splits.
    assert len(free) == len(pairs)
    error = (free["bfs_new"] - free["bfs_old"] * free["fc_ratio"]).abs()
    bound = 4.0 * np.maximum(1.0, free["fc_ratio"])
    assert (error < bound).all(), free[error >= bound].head().to_string()
    # The check bites: the multiplier moves at most pairs, by whole counts.
    moved = free[free["fc_new"] != free["fc_old"]]
    assert len(moved) > len(free) / 2
    assert (moved["bfs_new"] != moved["bfs_old"]).all()
    unmoved = free[free["fc_new"] == free["fc_old"]]
    assert (unmoved["bfs_new"] == unmoved["bfs_old"]).all()
    result = restore_run["result"]
    assert result["monte_carlo"]["plant_version"] == restore_run["plant"] == design.plant_version
    if restore_run["plant"] == "fuel-mv-1":
        # Enrichment points (the design's open-loop label) are among them.
        assert (free["regime"] == "enrichment").any()
    else:
        # fuel-mv-2's VE fit has not seen this morning drive's 20-22 C intake
        # (fit 23-50 C): those reads are counted outside_ve_fit by name, never
        # placed, and the WOT pulls are among them; the identity above holds
        # at every pair that is placed.
        assert design.plant_states.get("outside_ve_fit", 0) > 0


def test_the_cell_pattern_is_b08_6s_restore_result(
    images: tuple[FuelCompCalibration, FuelCompCalibration], restore_run: dict[str, Any]
) -> None:
    """B08.6, 2026-10-15: idle leaner (-1.56 % to -2.33 %), light load off idle
    richer (+0.8 to +2.3 % at 2,000-3,000 rpm, up to +4.8 % near 1,200), full
    throttle +2.3 to +4.6 % rising with rpm. Checked per (point, draw) on the
    simulated BFS, then on the cell table's medians."""
    old, new = images
    pairs = restore_run["pairs"]
    change = pairs["bfs_change_pct"]
    if restore_run["plant"] != "fuel-mv-1":
        # On fuel-mv-2 the drive keeps too few light-load and WOT reads (the
        # 20-22 C ones are outside_ve_fit) for the pattern below; idle, where
        # it does place reads, shows B08.6's leaner step per pair.
        idle = (pairs["rpm"] <= 800) & pairs["byte"].between(48, 57)
        assert idle.sum() >= 30
        assert change[idle].between(-2.45, -1.5).all(), change[idle].describe()
        return
    # Idle, bytes 48-57 (B08.6's ~760 rpm reads): leaner at every pair, by one
    # Fuel Comp count (126/128, -1.5625 %) or three (125/128 ... -2.34 %), each
    # to within the BFS word.
    idle = (pairs["rpm"] <= 800) & pairs["byte"].between(48, 57)
    assert idle.sum() >= 30
    assert change[idle].between(-2.45, -1.5).all(), change[idle].describe()
    assert abs(change[idle].median() - 100 * (126 / 128 - 1)) < 0.1
    # Light load off idle: never leaner, richer at most pairs, under +4.8 %.
    light = pairs["rpm"].between(1000, 3200) & pairs["byte"].between(20, 60)
    assert light.sum() >= 10
    assert change[light].between(0.0, 4.8).all(), change[light].describe()
    assert (change[light] >= 0.75).mean() > 0.5
    # Full throttle (bytes >= ~197) at 4,000 rpm and above: +2.3 to +4.6 %,
    # each higher rpm band above the one below it.
    wot = (pairs["byte"] >= 190) & (pairs["rpm"] >= 4000)
    assert wot.sum() >= 3
    assert change[wot].between(2.2, 4.7).all(), change[wot].describe()
    by_band = change[wot].groupby(pd.cut(pairs.loc[wot, "rpm"], [4000, 4800, 6400]))
    low_band, high_band = by_band.max().iloc[0], by_band.min().iloc[1]
    assert low_band < high_band, (low_band, high_band)
    # The same pattern in the cell table the report draws (relative dBFS p50).
    cells = {
        (c["row_value"], c["col_breakpoint"]): c["outputs"]["base_fuel_schedule"]
        for c in restore_run["result"]["cell_table"]["cells"]
        if c["n_points"]
    }

    def p50(rpm: float, byte: int) -> float:
        out = cells[(rpm, byte)]
        assert out["status"] == "changed", (rpm, byte, out["status"])
        return 100.0 * float(out["quantiles"]["relative_delta"]["p50"])

    assert -2.45 < p50(800.0, 48) < -1.5
    assert -2.45 < p50(800.0, 56) < -1.5
    assert 0.75 < p50(1600.0, 48) < 4.8
    assert 0.75 < p50(2400.0, 32) < 2.4
    assert 2.2 < p50(4400.0, 144) < p50(5200.0, 144) < 4.7


def _raw_table(rom_map: RomMap, image: bytes, table_id: str, slot: int | None) -> list[int]:
    """A u8 table's bytes straight from the image, row-major, via ``rom_map`` only."""
    table = rom_map.table(table_id)
    assert table.dtype == "u8", table.dtype
    at = rom_map.table_address(table, slot)
    return list(image[at : at + table.rows * table.cols])


def test_the_shared_lookup_returns_the_raw_cell_at_every_node(
    images: tuple[FuelCompCalibration, FuelCompCalibration], restore_run: dict[str, Any]
) -> None:
    """Both paths interpolate with ``lookup_2d``, so it is checked apart from
    either: at an rpm word and fill byte that sit exactly on an image's
    breakpoints, the multiplier must be the raw byte at the
    ``fuel_compensation`` address + r x 16 + c, read here from the image bytes.
    Every node of each image is evaluated directly (16 x 16 each), then any
    drawn pair that lands on a node. This checks the lookup at its nodes; the
    interpolation between them stays shared and unchecked here."""
    old, new = images
    rom_map = load_rom_map(PROFILE)
    slot = load_fuel_comp_declaration(PROFILE).program.map_slot
    table = rom_map.table("fuel_compensation")
    sides = {"r20260920": (old, 0), "r20261004": (new, 1)}
    nodes: dict[int, dict[tuple[int, int], int]] = {}
    for key, (calibration, side) in sides.items():
        image = load_image(OPERATOR_ROMS[key].read_bytes(), rom_map).image
        cells = _raw_table(rom_map, image, "fuel_compensation", slot)
        # The axes too, from the bytes: rpm compared as byte << 2, load byte for byte.
        rpm_axis = [4 * b for b in _raw_table(rom_map, image, "fuel_comp_engine_speed_axis", None)]
        load_axis = _raw_table(rom_map, image, "fuel_comp_flow_axis", None)
        assert tuple(rpm_axis) == tuple(calibration.rpm_breakpoints), key
        assert tuple(load_axis) == tuple(calibration.load_axis_raw), key
        # A repeated breakpoint would make "the node" ambiguous.
        assert len(set(rpm_axis)) == table.rows and len(set(load_axis)) == table.cols
        nodes[side] = {}
        for r, word in enumerate(rpm_axis):
            for c, byte in enumerate(load_axis):
                change = multiplier_change(
                    old,
                    new,
                    InputPoint(
                        session="node",
                        route="design",
                        t=0.0,
                        byte_lo=byte,
                        byte_hi=byte,
                        word_lo=word,
                        word_hi=word,
                        reason=None,
                    ),
                )
                got = (change.fc_old, change.fc_new)[side]
                assert got == (cells[r * table.cols + c],) * 2, (key, r, c, got)
                nodes[side][(word, byte)] = cells[r * table.cols + c]
    # The drawn pairs that sit on a node of either image carry that node's byte.
    pairs = restore_run["pairs"]
    words = [rpm_word(rpm, load_fuel_comp_declaration(PROFILE)) for rpm in pairs["rpm"]]
    on_node = 0
    for word, byte, fc_old, fc_new in zip(
        words, pairs["byte"], pairs["fc_old"], pairs["fc_new"], strict=True
    ):
        for side, fc in ((0, fc_old), (1, fc_new)):
            if (word, int(byte)) in nodes[side]:
                on_node += 1
                assert fc == nodes[side][(word, int(byte))], (side, word, byte, fc)
    # On this drive and seed no drawn pair lands exactly on a node (0 of 894:
    # the words and bytes fall between breakpoints), which is why every node
    # is evaluated directly above. The loop stays so a design that does land
    # on one is checked too.
    assert on_node >= 0


WOT_SESSION = "_capture-tuning-2026-09-22T235224"


def test_a_default_draw_run_over_a_real_wot_drive_on_fuel_mv_2_never_refuses(
    images: tuple[FuelCompCalibration, FuelCompCalibration],
    tmp_path: Path,
) -> None:
    """E08.11 rework: the verifier's real repro. A drive design of
    09-22T235224 (a WOT drive) on the simulator's fuel-mv-2, then a run over every
    declared parameter: it used to refuse ``manifold_refusal_varies_by_draw``
    at point 250 (a drawn bank split lifted one bank's MAP above ambient).
    With one plenum and bracket-safe placement it runs, and the one read that
    a VE-floor draw would lift above ambient is counted, not placed."""
    if not (SESSIONS_ROOT / WOT_SESSION).is_dir():
        pytest.skip(f"private drive not on this machine: {SESSIONS_ROOT / WOT_SESSION}")
    _cli(
        "design", "drives", "--base-revision", OLD, "--sessions", WOT_SESSION,
        "--sessions-root", SESSIONS_ROOT, "--fuel-type", "gasoline",
        "--battery-v-when-unmeasured", "14.0", "--rom-dir", ROM_DIR, "--out", tmp_path / "d",
    )  # fmt: skip
    (design_dir,) = (tmp_path / "d").iterdir()
    design = load_design(design_dir)
    assert design.plant_version == "fuel-mv-2"
    assert design.placement["manifold_above_ambient_in_bracket"] == 1
    assert design.unplaced_dwell_s["manifold_above_ambient_in_bracket"] > 0
    assert len(design.points) == design.placement["placed"] == 305
    # The steady pull the split used to lift: 2,562 rpm, enrichment, MAP
    # 0.977 x baro (x 1.04 at split 0.52 read 1.016 x baro and refused).
    near = [p for p in design.points if p.map_kpa / p.baro_kpa > 0.96]
    assert [p.regime for p in near] == ["enrichment"]
    for draws, seed in ((4, 20261015), (100, 1)):
        _cli(
            "run", "--base", "--revision", OLD, "--candidate-rom", OPERATOR_ROMS["r20261004"],
            "--design", design_dir, "--draws", str(draws), "--seed", str(seed),
            "--stft-pct", "0", "--ltft-pct", "0", "--candidate-events", "ecu_reflash",
            "--rom-dir", ROM_DIR, "--out", tmp_path / f"run{draws}",
        )  # fmt: skip
        result = json.loads((tmp_path / f"run{draws}" / "result.json").read_text())
        assert result["monte_carlo"]["plant_version"] == "fuel-mv-2"
        assert "bank_air_split" in result["monte_carlo"]["varied"]
