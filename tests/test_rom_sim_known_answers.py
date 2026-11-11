"""E08.7: the ROM-edit workbench reproduces answers obtained independently.

Each known answer goes in through the command line (``tools/rom_sim.py``),
an E08.1 edit spec or an edited image, an E08.2 design written to disk and the
E08.3 Monte Carlo, and is read back from what the run wrote: ``traces.parquet``
per point x draw x output, and ``result.json``'s cell table. Every assertion is
per (point, draw), never on an aggregate of either side.

1. **Negative control.** An identical candidate file is ``unchanged`` in every
   visited cell, at every draw, with every computed delta exactly 0. The spec
   route cannot express an identical candidate (E08.1 refuses ``no_op_edit``),
   so its control is an edit no design point reads, which must leave every
   ECU-side output exactly unmoved.
2. **K +5 % moves BFS +5 %** (E07.6, ``test_a_k_edit_moves_bfs_one_for_one``)
   at every point until BFS meets its ceiling (``bfs_ceiling``, ``0xD8B3``),
   where it is the ceiling exactly.
3. The Fuel Comp restore on the real images lives in
   ``tests/test_rom_sim_private_rom.py``.
4. **A Target AFR edit of +4 counts** reproduces E07.6's WOT figures
   (``test_a_target_afr_edit_is_computed_through_the_pulse_and_the_plant``;
   E07 doc: phi +0.03125, pulse +0.41 ms, lambda -2.5 %, -2.52...-2.49 % over
   the assumptions).

The world is E07.6's synthetic image (``tests.test_sim_montecarlo.
build_kit_and_image``) written beside a copy of the Xterra profile in
``tmp_path``, its application model re-pinned to the image's program and its
``tune_revisions.yaml`` given the test revisions, as ``tests/test_rom_sim_cli.py``
does. A second base, ``SYNTH-ceiling``, is the same image with the BFS ceiling
curve set flat at :data:`CEILING_BYTE`: the synthetic image's 255 is never
reached by a steady point, and a flat curve makes the clamp a constant
(``ceiling << 8``) this file states without the twin's interpolation.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

from src.analysis.ecu_application import program_sha256
from src.analysis.paired_scenarios import SimKit, run_paired
from src.analysis.sim_design import load_design, write_design
from src.decode.air_charge import TP_SCALE, TP_SCALE_SHIFT
from src.yaml_load import load_yaml
from tests.rom_fixtures import PROFILE
from tests.test_paired_scenarios import LEARNED, WOT, _edit_all, _scenario
from tests.test_sim_montecarlo import build_kit_and_image
from tools import rom_sim

BASE = "SYNTH-base"
CEILING_BASE = "SYNTH-ceiling"
#: The flat BFS ceiling of the ``SYNTH-ceiling`` image: BFS clamps at
#: ``90 << 8 = 23040``, inside the grid's BFS range below (6,632-26,560).
CEILING_BYTE = 90
CEILING_WORD = CEILING_BYTE << 8
#: K on the synthetic image and the spec's +5 % (``scale 1.05``, ``floor``):
#: E07.6's own ``28715 * 105 // 100``.
K_OLD = 28715
K_NEW = 30150

#: Grid environment stated whole (the library refuses any part unstated).
ENVIRONMENT = [
    "--load-basis", "fill_fraction_as_manifold_ratio",
    "--baro-kpa", "81",
    "--iat-c", "25",
    "--coolant-c", "88",
    "--battery-v", "14",
    "--rail-kpa", "350",
    "--throttle-pct", "20",
    "--pedal-flag", "0",
]  # fmt: skip
#: Synthetic Fuel Comp axes: rpm 400..6400 x 400, fill bytes 0..240 x 16
#: (x 100/224 %). Centres 2000 / 3000 / 4000 rpm x loads 24 / 40 / 56 / 72 /
#: 88 %: the low loads run closed loop, the high ones enrichment.
GRID_RPM = ["1900", "2100", "2900", "3100", "4700", "4900"]
GRID_LOAD = ["20", "28", "36", "44", "52", "60", "68", "76", "84", "92", "100"]
#: E07.6's learned state (``tests.test_paired_scenarios.LEARNED``).
E076_LEARNED = ["--stft-pct", "0", "--ltft-pct", "2.34", "--candidate-events", "none"]


def _yaml(path: Path) -> Any:
    return load_yaml(path.read_text())


def _with_ceiling(image: bytes, kit: SimKit, byte: int) -> bytes:
    table = kit.rom_map.table("bfs_ceiling")
    at = kit.rom_map.table_address(table, None)
    out = bytearray(image)
    out[at : at + table.rows * table.cols] = bytes([byte] * (table.rows * table.cols))
    return bytes(out)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    kit, image = build_kit_and_image()
    low = _with_ceiling(image, kit, CEILING_BYTE)
    # The ceiling curve is calibration, not program: one model describes both.
    assert program_sha256(low, kit.model) == program_sha256(image, kit.model)
    root = tmp_path_factory.mktemp("known_answers")
    profile = root / "profile"
    shutil.copytree(PROFILE, profile, ignore=shutil.ignore_patterns("_candidates", "_prior"))
    model_path = profile / "ecu_application_model.yaml"
    model = _yaml(model_path)
    model["program"]["identity"]["sha256"] = program_sha256(image, kit.model)
    model_path.write_text(yaml.safe_dump(model, sort_keys=False))
    registry_path = profile / "tune_revisions.yaml"
    registry = _yaml(registry_path)
    images = {BASE: image, CEILING_BASE: low}
    # Dated in 2099 so a synthetic revision sorts after every real flash: the
    # registry refuses an out-of-order list, and a date just past the newest real
    # flash broke these fixtures when ZS44A-20261110 was recorded.
    for k, (revision, data) in enumerate(images.items()):
        registry["revisions"].append(
            {
                "revision": revision,
                "calibration_id": "1ZS44A00000",
                "flashed_at": f"2099-10-1{k}T00:00:00Z",
                "flashed_at_precision": "exact",
                "image_sha256": hashlib.sha256(data).hexdigest(),
                "notes": "synthetic: E08.7's known-answer base",
            }
        )
    registry_path.write_text(yaml.safe_dump(registry, sort_keys=False))
    files = {BASE: root / "base.bin", CEILING_BASE: root / "ceiling.bin"}
    for revision, path in files.items():
        path.write_bytes(images[revision])
    return {"root": root, "profile": profile, "kit": kit, "images": images, "files": files}


def _cli(*argv: str | Path) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = rom_sim.main([str(a) for a in argv])
    return code, json.loads(out.getvalue())


def _sha(world: dict[str, Any], revision: str) -> str:
    return hashlib.sha256(world["images"][revision]).hexdigest()


def _spec(world: dict[str, Any], name: str, revision: str, edits: list[dict[str, Any]]) -> Path:
    path: Path = world["root"] / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "base": {"file": world["files"][revision].name, "sha256": _sha(world, revision)},
                "label": f"E08.7 {name}",
                "edits": edits,
            }
        )
    )
    return path


def _grid(world: dict[str, Any], revision: str, out: Path) -> Path:
    code, payload = _cli(
        "design", "grid", "--profile", world["profile"], "--base-revision", revision,
        "--rpm-edges", *GRID_RPM, "--load-edges", *GRID_LOAD, *ENVIRONMENT, "--out", out,
    )  # fmt: skip
    assert code == 0, payload
    (directory,) = out.iterdir()
    return directory


def _run(
    world: dict[str, Any],
    revision: str,
    design: Path,
    out: Path,
    *candidate: str | Path,
    draws: int,
    seed: int = 7,
) -> tuple[dict[str, Any], pd.DataFrame]:
    code, payload = _cli(
        "run", "--profile", world["profile"], "--base", "--rom", world["files"][revision],
        "--sha256", _sha(world, revision), *candidate, "--design", design,
        "--draws", str(draws), "--seed", str(seed), *E076_LEARNED, "--out", out,
    )  # fmt: skip
    assert code == 0, payload
    result = json.loads((out / "result.json").read_text())
    traces = pd.read_parquet(out / "traces.parquet")
    assert len(traces) == result["inputs"]["draws"] * len(load_design(design).points) * len(
        set(traces["output"])
    )
    return result, traces


def _output(traces: pd.DataFrame, name: str) -> pd.DataFrame:
    frame = traces[traces["output"] == name].sort_values(["point", "draw"])
    assert len(frame), name
    return frame.reset_index(drop=True)


@pytest.fixture(scope="module")
def grid_design(world: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _grid(world, BASE, tmp_path_factory.mktemp("grid"))


@pytest.fixture(scope="module")
def ceiling_design(world: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _grid(world, CEILING_BASE, tmp_path_factory.mktemp("grid_ceiling"))


# -- 1. negative control ----------------------------------------------------------------------


def test_an_identical_candidate_file_is_unchanged_in_every_visited_cell_at_every_draw(
    world: dict[str, Any], grid_design: Path, tmp_path: Path
) -> None:
    same = tmp_path / "identical.bin"
    same.write_bytes(world["images"][BASE])
    for seed in (1, 2):
        out = tmp_path / f"run{seed}"
        result, traces = _run(
            world, BASE, grid_design, out, "--candidate-rom", same, draws=4, seed=seed
        )
        assert result["candidate"]["cells"] == [] and result["candidate"]["ranges"] == []
        assert result["inputs"]["candidate_sha256"] == _sha(world, BASE)
        # Per point x draw x output: whatever was computed is equal on both sides.
        computed = traces["base"].notna()
        assert computed.any()
        assert traces["candidate"].notna().equals(computed)
        assert (traces.loc[computed, "delta"] == 0.0).all()
        assert (traces.loc[computed, "base"] == traces.loc[computed, "candidate"]).all()
        assert set(traces["draw"]) == set(range(4))
        table = result["cell_table"]
        assert {r["status"] for r in table["reach"].values()} == {"no_change"}
        visited = [c for c in table["cells"] if c["n_points"] > 0]
        assert sum(c["n_points"] for c in visited) == len(load_design(grid_design).points)
        for cell in visited:
            for name, out_cell in cell["outputs"].items():
                supported = out_cell["supported_share"] > 0
                # Unsupported (the closed-loop trim, an unmeasured pulse) is a
                # separate kind of empty; every supported output is unchanged.
                expected = "unchanged" if supported else "unsupported"
                assert out_cell["status"] == expected, (cell["row"], cell["col"], name)
                block = out_cell["quantiles"]
                if block is not None:
                    for q in block["delta"].values():
                        assert q == 0.0, (cell["row"], cell["col"], name)
        assert any(c["outputs"]["delivered_lambda"]["status"] == "unchanged" for c in visited), (
            "no open-loop cell: the control would not cover lambda"
        )


def test_the_spec_route_cannot_express_an_identical_candidate(
    world: dict[str, Any], grid_design: Path, tmp_path: Path
) -> None:
    """An edit spec that changes nothing is refused by name, so the spec route's
    negative control is an edit no design point reads."""
    spec = _spec(
        world,
        "identity",
        BASE,
        [{"table": "fuel_compensation", "slot": 1, "cells": [[4, 4]], "set_raw": 128}],
    )
    code, payload = _cli(
        "run", "--profile", world["profile"], "--edits", spec, "--design", grid_design,
        "--draws", "2", "--seed", "1", *E076_LEARNED, "--out", tmp_path / "noop",
    )  # fmt: skip
    assert (code, payload["refused"]) == (2, "no_op_edit")


def test_a_spec_edit_no_point_reads_moves_no_ecu_output_at_any_draw(
    world: dict[str, Any], grid_design: Path, tmp_path: Path
) -> None:
    # Fuel Comp row 13 (5,600 rpm) x col 8 (byte 128): the grid's points sit at
    # 2,000-4,800 rpm, whose bilinear corners are rows 4..11, so none reads it.
    # Off the diagonal on purpose: its transpose (8, 13), 3,600 rpm x byte 208,
    # IS read by the 3,900 rpm points at bytes ~197, so a row/column swap fails.
    spec = _spec(
        world,
        "unread",
        BASE,
        [{"table": "fuel_compensation", "slot": 1, "cells": [[13, 8]], "add_raw": 20}],
    )
    result, traces = _run(world, BASE, grid_design, tmp_path / "run", "--edits", spec, draws=4)
    assert [(c["row"], c["col"], c["old"], c["new"]) for c in result["candidate"]["cells"]] == [
        (13, 8, 128, 148)
    ]
    # BFS is computed at every (point, draw) and never moves; nothing downstream
    # of it moves either, at any draw.
    bfs = _output(traces, "base_fuel_schedule")
    assert bfs["base"].notna().all()
    assert (bfs["delta"] == 0.0).all()
    computed = traces["base"].notna()
    assert traces["candidate"].notna().equals(computed)
    assert (traces.loc[computed, "delta"] == 0.0).all()
    # The edit reaches BFS structurally, so the cells are decided by the data.
    table = result["cell_table"]
    assert table["reach"]["base_fuel_schedule"]["status"] == "reached"
    for cell in table["cells"]:
        if cell["n_points"]:
            assert cell["outputs"]["base_fuel_schedule"]["status"] == "unchanged"


# -- 2. K +5 % ----------------------------------------------------------------------------------


#: Tp's scale c = ``TP_SCALE / 2**TP_SCALE_SHIFT`` = 9163/65536 (``air_charge_tp``).
TP_C = TP_SCALE / (1 << TP_SCALE_SHIFT)
K_RATIO = K_NEW / K_OLD
#: The word quantisation, derived. Tp's quotient is q = floor(K x A) with A the
#: same on both sides, so q_new - r q_old lies in (-1, r) for r = K_new / K_old;
#: Tp = floor(q x c), so Tp_new - r Tp_old lies in (-1 - c, r (1 + c)). Fuel
#: Comp is flat 128 here and BFS = 4 x floor(Tp x FC / 128) (``fuel_comp_product``
#: ``>> 7``, then ``steady_state_bfs`` ``<< 2``) = 4 Tp, so the candidate lies in
#: (r x base - 4 (1 + c), r x base + 4 r (1 + c)) = (-4.56, +4.80) counts.
K_BOUND_COUNTS = 4.0 * K_RATIO * (1.0 + TP_C)
#: Stated slack over the bound for q's own sub-count truncation (the low
#: byte's ``x m >> 8`` floor, divided by d x N >= 3 x 64): 0.2 counts. The
#: verifier's sweep of 32,861 kernel inputs found +4.54 / -4.25 at worst; this
#: grid's worst is 3.48. A 1 % ratio error is 66 counts at the lowest base.
K_QUANTUM_COUNTS = K_BOUND_COUNTS + 0.2


def k_expectation(base: np.ndarray, ratio: float) -> dict[str, np.ndarray]:
    """What each (point, draw)'s candidate BFS must be, from its base alone.

    ``expected``: base x ``ratio``, capped at the ceiling. ``free``: below the
    ceiling by more than the quantisation, where the ratio holds to the word.
    ``clamped``: base x ``ratio`` at or past the ceiling by more than the
    quantisation, where the candidate is the ceiling exactly. Points within
    one quantum of the ceiling are checked only against ``expected``."""
    scaled = base * ratio
    return {
        "expected": np.minimum(scaled, CEILING_WORD),
        "free": scaled < CEILING_WORD - K_QUANTUM_COUNTS,
        "clamped": scaled >= CEILING_WORD + K_QUANTUM_COUNTS,
        "base_clamped": base >= CEILING_WORD,
    }


def test_a_k_edit_of_five_percent_moves_bfs_five_percent_at_every_point_until_the_ceiling(
    world: dict[str, Any], ceiling_design: Path, tmp_path: Path
) -> None:
    edit = {"table": "k_constant", "slot": 1, "cells": [[0, 0]], "scale": 1.05, "rounding": "floor"}
    spec = _spec(world, "k_plus_5", CEILING_BASE, [edit])
    result, traces = _run(
        world, CEILING_BASE, ceiling_design, tmp_path / "run", "--edits", spec, draws=3
    )
    assert [(c["old"], c["new"]) for c in result["candidate"]["cells"]] == [(K_OLD, K_NEW)]
    bfs = _output(traces, "base_fuel_schedule")
    assert (bfs["status"] == "supported").all()
    base = bfs["base"].to_numpy()
    cand = bfs["candidate"].to_numpy()
    want = k_expectation(base, K_RATIO)
    free, clamped = want["free"], want["clamped"]
    # Every (point, draw): the candidate is base x K ratio, capped, to the word.
    error = np.abs(cand - want["expected"])
    assert (error <= K_QUANTUM_COUNTS).all(), error.max()
    # The grid spans the ceiling: free points, points only the edit clamps,
    # and points clamped on both sides.
    only_edit = clamped & ~want["base_clamped"]
    assert free.sum() >= 10 and only_edit.sum() >= 1 and want["base_clamped"].sum() >= 1, (
        free.sum(),
        only_edit.sum(),
        want["base_clamped"].sum(),
    )
    # Free of the ceiling: the per-point ratio is K's, to one quantum of that point.
    ratio = cand[free] / base[free]
    assert (np.abs(ratio - K_RATIO) <= K_QUANTUM_COUNTS / base[free]).all(), ratio
    # At the ceiling: the ceiling exactly, never above it on either side.
    assert (cand[clamped] == CEILING_WORD).all()
    assert (base[want["base_clamped"]] == CEILING_WORD).all()
    assert (cand <= CEILING_WORD).all() and (base <= CEILING_WORD).all()
    # BFS is ECU side: no plant draw moves it.
    assert (bfs.groupby("point")["candidate"].nunique() == 1).all()


# -- 4. Target AFR +4 counts --------------------------------------------------------------------

#: E07.6's WOT figures (E07 doc, ``test_a_target_afr_edit_is_computed_through_
#: the_pulse_and_the_plant``'s evidence, commit 84061a65).
E076_PHI_DELTA = 0.03125
E076_PULSE_DELTA_MS = 0.41
E076_LAMBDA_REL = -0.025
E076_LAMBDA_RANGE = (-0.0252, -0.0249)
#: Those figures were measured with BFS *stated* at 42,000 at the WOT point
#: (84061a65's ``WOT.conditioned``). E07.6 has since computed BFS from the
#: airflow (``paired_steady``) and refuses a stated one, so the same point now
#: runs at BFS 39,604. The airflow at which the steady BFS is 42,000 on this
#: image, everything else E07.6's WOT, is 159.06 g/s (PID word 15906; 15905
#: gives 41,996 and a 0.40 ms pulse step). Both points are in the design.
E076_BFS = 42000
E076_BFS_MAF_GPS = 159.06


def _stated_wot_design(grid_design: Path, root: Path) -> Path:
    """E07.6's WOT point, twice, as design points with every value stated.

    A grid places MAP at fill x baro and solves MAF through the manifold
    model, so it cannot hold E07.6's MAF with MAP 79 kPa. The points are
    E07.6's own ``WOT`` (point 0) and the same at :data:`E076_BFS_MAF_GPS`
    (point 1), written into the CLI-built grid design with every source
    ``stated`` and the fill byte the one-count interval [200, 200]. The hash
    is recomputed by ``write_design``, never carried over."""
    design = load_design(grid_design)
    template = design.points[0]
    points = [
        template.model_copy(
            update={
                "index": index,
                "cell_row": 0,
                "cell_col": index,
                "rpm": WOT.rpm,
                "maf_gps": maf,
                "maf_source": "stated",
                "baro_kpa": WOT.baro_kpa,
                "iat_c": WOT.iat_c,
                "coolant_c": WOT.coolant_c,
                "battery_v": WOT.battery_v,
                "battery_v_source": "stated",
                "rail_kpa": WOT.rail_kpa,
                "rail_kpa_source": "stated",
                "map_kpa": WOT.map_kpa,
                "map_kpa_source": "stated",
                "throttle_pct": WOT.throttle_pct,
                "throttle_pct_lo": WOT.throttle_pct,
                "throttle_pct_hi": WOT.throttle_pct,
                "throttle_pct_source": "stated",
                "pedal_flag": WOT.pedal_flag,
                "pedal_flag_source": "stated",
                "fill_byte_lo": 200,
                "fill_byte_hi": 200,
                "fill_byte_source": "stated",
                "weight": 1.0,
            }
        )
        for index, maf in enumerate((WOT.maf_gps, E076_BFS_MAF_GPS))
    ]
    stated = design.model_copy(
        update={
            "points": points,
            "reads": len(points),
            "plant_states": {"eligible": len(points)},
            "placement": {"placed": len(points)},
        }
    )
    return write_design(type(design).model_validate(stated.model_dump()), root)


def _e076_reference(kit: SimKit, image: bytes, candidate: bytes, maf: float) -> dict[str, Any]:
    """E07.6's own path (``run_paired``, its ``_scenario`` and ``LEARNED``) at the point."""
    point = dataclasses.replace(WOT, maf_gps=maf, conditioned={"fuel_comp_load_byte": 200.0})
    outputs: dict[str, Any] = run_paired(
        kit, _scenario(kit, image, candidate, point), image, candidate
    ).points[0]["outputs"]
    return outputs


def test_a_target_afr_edit_of_four_counts_reproduces_e076s_wot_figures(
    world: dict[str, Any], grid_design: Path, tmp_path: Path
) -> None:
    kit, image = world["kit"], world["images"][BASE]
    design = _stated_wot_design(grid_design, tmp_path / "designs")
    edit = {"table": "fuel_targets", "slot": 1, "all": True, "add_raw": 4}
    spec = _spec(world, "target_afr_plus_4", BASE, [edit])
    draws = 200
    result, traces = _run(world, BASE, design, tmp_path / "run", "--edits", spec, draws=draws)
    # The spec makes E07.6's candidate byte for byte, under E07.6's learned state.
    e076 = _edit_all(image, kit, "fuel_targets", 1, 4)
    assert result["inputs"]["candidate_sha256"] == hashlib.sha256(e076).hexdigest()
    assert (LEARNED.stft_pct, LEARNED.ltft_pct) == (0.0, 2.34)
    assert result["inputs"]["learned"]["ltft_pct"] == 2.34
    outputs = ("base_fuel_schedule", "target_phi", "injector_pulse_ms", "delivered_lambda")
    frames = {name: _output(traces, name) for name in outputs}
    for frame in frames.values():
        assert (frame["status"] == "supported").all()
        assert len(frame) == 2 * draws
    for index, maf in enumerate((WOT.maf_gps, E076_BFS_MAF_GPS)):
        ref = _e076_reference(kit, image, e076, maf)
        at = {name: frame[frame["point"] == index] for name, frame in frames.items()}
        # ECU side, every draw: BFS, phi and the pulse are E07.6's values exactly.
        for name in ("base_fuel_schedule", "target_phi", "injector_pulse_ms"):
            assert (at[name]["base"] == ref[name]["base"]).all(), (index, name)
            assert (at[name]["candidate"] == ref[name]["candidate"]).all(), (index, name)
        assert (at["target_phi"]["delta"] == E076_PHI_DELTA).all()
        # Lambda: a distribution over the plant's declared uncertainty, centred
        # on E07.6's value and inside its range over the assumptions.
        rel = at["delivered_lambda"]["relative_delta"].to_numpy()
        p05, p50, p95 = np.quantile(rel, [0.05, 0.5, 0.95])
        central = ref["delivered_lambda"]["relative_delta"]
        lo, hi = ref["delivered_lambda"]["relative_delta_range_over_assumptions"]
        assert abs(p50 - central) < 2e-4, (index, p50, central)
        # p05-p95, not every draw: the draws vary every parameter at once,
        # the range moves one at a time, so a joint draw may lie outside it.
        assert lo <= p05 <= p95 <= hi, (index, lo, p05, p95, hi)
    # Point 0 is E07.6's WOT as E07.6 runs it today: BFS 39,604, so +0.39 ms.
    bfs0 = frames["base_fuel_schedule"]
    assert set(bfs0.loc[bfs0["point"] == 0, "base"]) == {39604.0}
    pulse = frames["injector_pulse_ms"]
    assert set(pulse.loc[pulse["point"] == 0, "delta"].round(2)) == {0.39}
    # Point 1 is the BFS E07.6's figures were written at: its figures exactly.
    assert set(bfs0.loc[bfs0["point"] == 1, "base"]) == {float(E076_BFS)}
    assert set(pulse.loc[pulse["point"] == 1, "delta"].round(2)) == {E076_PULSE_DELTA_MS}
    lam = frames["delivered_lambda"]
    rel1 = lam.loc[lam["point"] == 1, "relative_delta"].to_numpy()
    ref1 = _e076_reference(kit, image, e076, E076_BFS_MAF_GPS)["delivered_lambda"]
    assert round(ref1["relative_delta"], 3) == E076_LAMBDA_REL
    lo1, hi1 = ref1["relative_delta_range_over_assumptions"]
    assert (round(lo1, 4), round(hi1, 4)) == E076_LAMBDA_RANGE
    assert round(float(np.quantile(rel1, 0.5)), 3) == E076_LAMBDA_REL
