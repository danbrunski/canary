"""E08.5: the ``tools/rom_sim.py`` command line, on a synthetic image pair.

The world is built once: E07.6's synthetic base image
(``tests.test_sim_montecarlo.build_kit_and_image``), written as a plain file
beside a *copy* of the Xterra profile in ``tmp_path`` whose application model
is re-pinned to that image's program spans and whose ``tune_revisions.yaml``
gains a ``SYNTH-base`` revision carrying the image's sha256. That is what lets
the CLI, which loads everything from ``--profile``, run on bytes that are not
the real program. Nothing real is committed or read, and the real profile is
never written.

Coupling created here: ``tests.test_sim_montecarlo.build_kit_and_image`` (the
image), ``tests.test_paired_scenarios`` (through it), and the shape of two
profile files (``ecu_application_model.yaml``'s ``program.identity.sha256`` and
``tune_revisions.yaml``'s ``revisions`` list), which this file edits as data.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from src.analysis.ecu_application import program_sha256
from src.analysis.fuel_plant import plant_qualification
from src.analysis.rom_effects import CellEdit, RomEffectRefusalError, compare_roms, make_candidate
from src.analysis.rom_levers import classify_edit
from src.analysis.sim_cells import CellTable
from src.analysis.sim_montecarlo import load_sim_kit
from src.analysis.sim_report import render_report
from src.session import is_synthetic_session
from tests.rom_fixtures import PROFILE, encrypted_backup_lookalike
from tests.test_sim_montecarlo import build_kit_and_image
from tools import rom_sim

REPO = Path(__file__).resolve().parents[1]
FIXTURE_SLICE = REPO / "tests" / "fixtures" / "fuel_plant" / "_capture-tuning-2026-09-23T142926"
BASE = "SYNTH-base"
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
LEARNED = ["--stft-pct", "0", "--ltft-pct", "2.34", "--candidate-events", "none"]


#: A stated timing state (E09.7): HI DET clear, knock control active, warm.
TIMING_ARGS = ("--high-det", "clear", "--knock-control", "active", "--timing-coolant-c", "90")


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


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
    registry = _yaml(registry_path)
    registry["revisions"].append(
        {
            "revision": BASE,
            "calibration_id": "1ZS44A00000",
            "flashed_at": "2026-10-10T00:00:00Z",
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


def _cli(capsys: pytest.CaptureFixture[str], *argv: str | Path) -> tuple[int, dict[str, Any], str]:
    """Run ``main`` and return its exit code, stdout parsed as JSON, and stderr."""
    code = rom_sim.main([str(a) for a in argv])
    out, err = capsys.readouterr()
    return code, json.loads(out), err


def _profile_args(world: dict[str, Any]) -> list[str]:
    return ["--profile", str(world["profile"])]


@pytest.fixture(scope="module")
def grid_design(world: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("designs")
    with contextlib.redirect_stdout(io.StringIO()):
        code = rom_sim.main(
            [
                "design",
                "grid",
                *_profile_args(world),
                "--base-revision",
                BASE,
                *GRID_ARGS,
                "--out",
                str(out),
            ]
        )
    assert code == 0
    (directory,) = out.iterdir()
    return directory


def _run_args(world: dict[str, Any], design: Path, out: Path, *extra: str) -> list[str]:
    return [
        "run",
        *_profile_args(world),
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
        *LEARNED,
        "--out",
        str(out),
        *extra,
    ]


@pytest.fixture(scope="module")
def spec_run(
    world: dict[str, Any], grid_design: Path, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("run_spec")
    with contextlib.redirect_stdout(io.StringIO()):
        code = rom_sim.main(_run_args(world, grid_design, out, "--edits", str(world["spec"])))
    assert code == 0
    return {"out": out, "result": json.loads((out / "result.json").read_text())}


# -- the end-to-end run ---------------------------------------------------------------------


def test_a_synthetic_pair_and_a_grid_design_write_all_three_files(
    world: dict[str, Any], grid_design: Path, spec_run: dict[str, Any]
) -> None:
    out, result = spec_run["out"], spec_run["result"]
    assert sorted(p.name for p in out.iterdir()) == ["report.html", "result.json", "traces.parquet"]
    # result.json: provenance, caveats and a cell table that validates.
    assert result["tool"] == "rom_sim" and result["synthetic"] is True
    table = CellTable.model_validate(result["cell_table"])
    assert table.design_sha256 == grid_design.name == result["inputs"]["design_sha256"]
    assert table.base_image_sha256 == world["sha"]
    assert table.candidate_image_sha256 == world["candidate_sha"]
    assert "structural_error_not_sampled" in result["caveats"]
    assert "parameter_uncertainty_only" in result["caveats"]
    assert result["monte_carlo"]["seed"] == 7 and result["monte_carlo"]["draws"] == 6
    assert (
        result["inputs"]["candidate_events"] == []
        and result["inputs"]["learned"]["ltft_pct"] == 2.34
    )
    assert result["candidate"]["route"] == "edit_spec"
    assert (
        result["candidate"]["cells"][0]["old"] == 128
        and result["candidate"]["cells"][0]["new"] == 148
    )
    # The edited cell is on the grid, and the change reaches BFS in a visited cell.
    assert [(c["row"], c["col"]) for c in table.overlay.model_dump()["edited_cells"]] == [(4, 4)]
    # The grid's first centre is rpm 2000 at fill byte 63..64, the edited cell: it
    # alone moves BFS (a worked +20 on 128), the eight others read other cells.
    bfs = {(c.row, c.col): c.outputs["base_fuel_schedule"] for c in table.cells if c.n_points}
    assert len(bfs) == 9 and table.n_points == 9
    assert {k for k, v in bfs.items() if v.status == "changed"} == {(4, 4)}
    assert {v.status for k, v in bfs.items() if k != (4, 4)} == {"unchanged"}
    assert bfs[(4, 4)].quantiles.relative_delta.p05 > 0.05
    # traces.parquet: a long frame under one synthetic session.
    traces = pd.read_parquet(out / "traces.parquet")
    assert set(traces["session"]) == {result["traces_session"]}
    assert is_synthetic_session(result["traces_session"])
    assert len(traces) == table.n_points * table.draws * len(table.outputs)
    # report.html: the caveat block precedes the first chart, and the page
    # is the report of exactly this result.
    html = (out / "report.html").read_text()
    assert html.index("structural_error_not_sampled") < html.index("<svg")
    assert html.index("parameter_uncertainty_only") < html.index("<svg")
    assert html == render_report(
        json.dumps(result["cell_table"]), run=result["run"], timing=result["ignition_timing"]
    )
    for needle in (world["sha"], world["candidate_sha"], "cli test spec.yaml"):
        assert needle in html
    assert "http://" not in html and "https://" not in html


def test_the_run_facts_the_report_needs_come_from_the_provenance(spec_run: dict[str, Any]) -> None:
    run = spec_run["result"]["run"]
    assert run["seed"] == 7 and run["edit_route"] == "edit_spec"
    # The simulator's declared plant (sim_kit.yaml, E08.11), and its own
    # uncertainty declaration, not fuel-mv-1's.
    assert run["plant_version"] == "fuel-mv-2"
    mv2 = hashlib.sha256((PROFILE / "sim_uncertainty_mv2.yaml").read_bytes()).hexdigest()
    assert run["uncertainty_sha256"] == mv2
    assert run["base"]["label"] == "base.bin" and run["candidate"]["label"] == "cli test spec.yaml"
    assert run["spec_sha256"] == spec_run["result"]["candidate"]["spec_sha256"]
    assert run["edits"] == [
        {
            "table": "fuel_compensation",
            "slot": 1,
            "selector": "cells [[4, 4]]",
            "operation": "set_raw: 148",
            "changed": 1,
            "unchanged": 0,
        }
    ]
    assert run["design"]["kind"] == "grid" and run["design"]["points"] > 0
    assert run["uncertainty_sha256"] == spec_run["result"]["monte_carlo"]["uncertainty_sha256"]


def test_the_rom_file_route_makes_the_same_cell_table_as_the_spec_route(
    world: dict[str, Any],
    grid_design: Path,
    spec_run: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload, _ = _cli(
        capsys,
        *_run_args(
            world, grid_design, tmp_path, "--candidate-rom", str(world["root"] / "candidate.bin")
        ),
    )
    assert code == 0 and payload["candidate_sha256"] == world["candidate_sha"]
    via_file = json.loads((tmp_path / "result.json").read_text())
    assert via_file["candidate"]["route"] == "candidate_file"
    assert via_file["cell_table"] == spec_run["result"]["cell_table"]
    # The file route has no selector: one row per table the file changed.
    assert via_file["run"]["edits"] == [
        {
            "table": "fuel_compensation",
            "slot": 1,
            "selector": None,
            "operation": None,
            "changed": 1,
            "unchanged": 0,
        }
    ]
    assert (tmp_path / "report.html").read_text().count("<svg") > 0


def test_the_same_seed_writes_byte_identical_results_and_another_seed_does_not(
    world: dict[str, Any],
    grid_design: Path,
    spec_run: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    again = tmp_path / "again"
    assert (
        _cli(capsys, *_run_args(world, grid_design, again, "--edits", str(world["spec"])))[0] == 0
    )
    assert (again / "result.json").read_bytes() == (spec_run["out"] / "result.json").read_bytes()
    assert (again / "report.html").read_bytes() == (spec_run["out"] / "report.html").read_bytes()
    other = tmp_path / "other"
    args = _run_args(world, grid_design, other, "--edits", str(world["spec"]))
    args[args.index("--seed") + 1] = "8"
    assert _cli(capsys, *args)[0] == 0
    assert (other / "result.json").read_bytes() != (spec_run["out"] / "result.json").read_bytes()


def test_a_spec_that_names_its_own_base_needs_no_base_flags(
    world: dict[str, Any], grid_design: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _run_args(world, grid_design, tmp_path, "--edits", str(world["spec"]))
    i = args.index("--base")
    del args[i : i + 5]  # --base --rom X --sha256 H
    assert "--rom" not in args
    code, payload, _ = _cli(capsys, *args)
    assert code == 0 and payload["base_sha256"] == world["sha"]


# -- inspect --------------------------------------------------------------------------------


def test_inspect_lists_the_resolved_cells_and_what_they_reach_and_runs_nothing(
    world: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sorted(p.name for p in tmp_path.iterdir())
    code, payload, _ = _cli(
        capsys, "inspect", *_profile_args(world), "--edits", world["spec"], "--base",
        "--rom", world["root"] / "base.bin", "--sha256", world["sha"],
    )  # fmt: skip
    assert code == 0 and payload["command"] == "inspect"
    assert payload["ran"].startswith("nothing")
    assert payload["cells_changed"] == 1
    (cell,) = payload["cells"]
    assert (cell["table"], cell["slot"], cell["row"], cell["col"]) == ("fuel_compensation", 1, 4, 4)
    assert (cell["old"], cell["new"], cell["delta_raw"]) == (128, 148, 20)
    assert cell["pct"] == 15.625  # 20 / 128, worked by hand
    assert payload["candidate_sha256"] == world["candidate_sha"]
    assert payload["edits"][0]["selected"] == 1
    # What the edit reaches: Fuel Comp feeds BFS (a reached output is changed,
    # or unevaluable where the model cannot compute it, never unchanged).
    reached = {o["output"]: o["status"] for o in payload["reached"]["outputs"]}
    assert reached["base_fuel_schedule_word"] != "unchanged"
    assert "fuel_comp_lookup" in payload["reached"]["changed_nodes"]
    assert payload["reached"]["mode"] is None
    # The same trace under the run's contract mode, as the run's rom_report has it.
    assert payload["in_run_mode"]["mode"] == "conditional_rom_comparison"
    assert {o["output"] for o in payload["in_run_mode"]["outputs"]} == set(reached)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (
            {"table": "k_constant", "slot": 1, "all": True, "scale": 1.1, "rounding": "nearest"},
            "timing_input_changed:",
        ),
        (
            {"table": "fuel_compensation", "slot": 1, "cells": [[4, 4]], "add_raw": 5},
            "timing_input_changed:",
        ),
        ({"table": "base_timing_rate_map", "all": True, "add_raw": 5}, "unknown_bytes_changed"),
    ],
    ids=["k", "fuel_comp", "base_timing_rate_map"],
)
def test_an_edit_that_reaches_timing_outside_the_trims_is_not_zero_on_the_trim_grid(
    world: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    edit: dict[str, Any],
    reason: str,
) -> None:
    """E09.7: the trims' grid holds every other term equal, so a K, Fuel Comp
    or base-timing-map edit (each reaches ``ignition_timing`` per
    ``compare_roms``) is unevaluable there by name, never "0 changed"."""
    spec = world["root"] / "reach.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "base": {"file": "base.bin", "sha256": world["sha"]},
                "label": "reach",
                "edits": [edit],
            }
        )
    )
    code, payload, _ = _cli(capsys, "inspect", *_profile_args(world), "--edits", spec, *TIMING_ARGS)
    assert code == 0, payload
    timing = payload["ignition_timing"]
    assert timing["trims_changed"] is False
    assert timing["status"] == "unevaluable"
    assert "summary" not in timing and "cells" not in timing
    assert timing["reasons"] and all(r.startswith(reason) for r in timing["reasons"])
    assert timing["other_inputs_changed"] == timing["reasons"]
    # Without a stated state the block still names the reach after the state.
    code, payload, _ = _cli(capsys, "inspect", *_profile_args(world), "--edits", spec)
    assert code == 0, payload
    assert payload["ignition_timing"]["reasons"] == ["timing_state_unstated", *timing["reasons"]]


def test_inspect_judges_a_masked_trim_on_the_bits_the_ecm_reads(
    world: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = world["root"] / "trim.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "base": {"file": "base.bin", "sha256": world["sha"]},
                "label": "trim",
                "edits": [{"table": "timing_trim_a", "slot": 1, "cells": [[0, 0]], "add_raw": -1}],
            }
        )
    )
    code, payload, _ = _cli(capsys, "inspect", *_profile_args(world), "--edits", spec)
    # A trim edit names the HI DET latch and knock control: there is no default.
    assert (code, payload["refused"]) == (2, "timing_state_unstated")
    code, payload, _ = _cli(capsys, "inspect", *_profile_args(world), "--edits", spec, *TIMING_ARGS)
    assert code == 0
    timing = payload["ignition_timing"]
    assert timing["trims_changed"] and timing["summary"]["cells"] == 256
    # The synthetic image's trim axes are seeded noise, so which node reads the
    # edited cell is not pinned here (the private-ROM tests pin it); every row
    # shown is a change or names why it is not one.
    assert all(c["delta"] or c["reasons"] for c in timing["cells"])
    assert timing["torque"]["status"] == "unsupported"
    (cell,) = payload["cells"]
    assert cell["mask"] == 0x7F and cell["delta_raw"] == -1
    # A timing trim reaches the ignition outputs and leaves the fuel schedule alone.
    reached = {o["output"]: o["status"] for o in payload["reached"]["outputs"]}
    assert reached["base_fuel_schedule_word"] == "unchanged"
    assert reached["ignition_trim_selected"] != "unchanged"
    old = cell["old"] & 0x7F
    assert cell["pct"] == pytest.approx(-1 / old * 100.0, abs=1e-5) if old else cell["pct"] is None


# -- inspect names the lever and what blocks a number (E09.14a) ------------------------------


def _expected_messages(world: dict[str, Any], candidate: bytes, **kwargs: Any) -> list[str]:
    """What ``rom_levers.classify_edit`` says for the same edit: the strings
    the CLI prints and the ROM Editor shows (E09.14b calls the same function)."""
    kit = world["kit"]
    # The plant the CLI runs: sim_kit.yaml's declaration, not the contract's.
    report = compare_roms(
        world["image"],
        candidate,
        kit.rom_map,
        kit.model,
        kit.model_sha256,
        base_slot=1,
        candidate_slot=1,
        slot_source="default_slot",
        contract=kit.contract,
        mode="conditional_rom_comparison",
    )
    return list(
        classify_edit(
            report,
            rom_map=kit.rom_map,
            model=kit.model,
            contract=kit.contract,
            qualification=plant_qualification(load_sim_kit(world["profile"], None).kit.plant),
            **kwargs,
        ).messages
    )


def _trim_spec(world: dict[str, Any]) -> Path:
    spec = world["root"] / "trim_lever.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "base": {"file": "base.bin", "sha256": world["sha"]},
                "label": "trim lever",
                "edits": [{"table": "timing_trim_a", "slot": 1, "cells": [[0, 0]], "add_raw": -1}],
            }
        )
    )
    return spec


def test_inspect_names_the_lever_and_the_strings_equal_the_librarys(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    base = ["--rom", world["root"] / "base.bin", "--sha256", world["sha"]]
    # A Fuel Comp edit: closed loop, trim authority undecoded.
    code, payload, _ = _cli(
        capsys, "inspect", *_profile_args(world), "--edits", world["spec"], *base
    )
    assert code == 0
    levers = payload["levers"]
    assert [e["lever"] for e in levers["levers"]] == ["fuel_compensation"]
    assert levers["levers"][0]["changed_cells"] == 1
    assert "closed loop: trim authority undecoded" in "\n".join(levers["messages"])
    candidate = (world["root"] / "candidate.bin").read_bytes()
    assert levers["messages"] == _expected_messages(world, candidate)
    # Additive: the keys that were there still are.
    assert {"cells", "reached", "in_run_mode", "ranges"} <= set(payload)
    # A timing trim: the torque stage, built and not validated (E07.5 stage 4).
    code, payload, _ = _cli(
        capsys, "inspect", *_profile_args(world), "--edits", _trim_spec(world), *base, *TIMING_ARGS
    )
    assert code == 0
    said = "\n".join(payload["levers"]["messages"])
    assert "torque production built, not validated" in said
    assert "spark response unmeasured" in said
    trimmed = make_candidate(
        world["image"],
        world["kit"].rom_map,
        [CellEdit("timing_trim_a", 0, 0, payload["cells"][0]["new"], slot=1)],
        base_label="x",
    ).image
    assert payload["levers"]["messages"] == _expected_messages(world, trimmed)


def test_inspect_says_a_changed_byte_outside_the_map_is_not_mapped(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The MAF curve at 0xE27E is in no table of ``rom_map.yaml`` (E09.8 maps
    it), so only an edited image can express it."""
    image = bytearray(world["image"])
    image[0xE27E] ^= 0x01
    saved = world["root"] / "maf.bin"
    saved.write_bytes(bytes(image))
    code, payload, _ = _cli(
        capsys, "inspect", *_profile_args(world), "--candidate-rom", saved,
        "--rom", world["root"] / "base.bin", "--sha256", world["sha"],
    )  # fmt: skip
    assert code == 0
    messages = payload["levers"]["messages"]
    assert any("0xE27E" in m and "not mapped" in m for m in messages)
    assert payload["levers"]["messages"] == _expected_messages(world, bytes(image))
    assert {o["status"] for o in payload["levers"]["outputs"]} == {"unknown"}


def test_inspect_regime_selects_whether_the_closed_loop_blocker_applies(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    candidate = (world["root"] / "candidate.bin").read_bytes()
    base = ["--rom", world["root"] / "base.bin", "--sha256", world["sha"]]
    code, payload, _ = _cli(
        capsys, "inspect", *_profile_args(world), "--edits", world["spec"], *base,
        "--regime", "fuel_cut",
    )  # fmt: skip
    assert code == 0 and payload["levers"]["regime"] == "fuel_cut"
    assert "closed loop" not in "\n".join(payload["levers"]["messages"])
    assert payload["levers"]["messages"] == _expected_messages(world, candidate, regime="fuel_cut")


# -- refusals -------------------------------------------------------------------------------


def _refused(capsys: pytest.CaptureFixture[str], reason: str, *argv: str | Path) -> dict[str, Any]:
    code, payload, _ = _cli(capsys, *argv)
    assert code == 2, payload
    assert payload["refused"] == reason, payload
    assert payload["message"]
    return payload


def test_a_run_refuses_every_argument_the_result_depends_on(
    world: dict[str, Any], grid_design: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    full = _run_args(world, grid_design, tmp_path, "--edits", str(world["spec"]))

    def without(*flags: str) -> list[str]:
        out = list(full)
        for flag in flags:
            i = out.index(flag)
            n = 2
            if flag == "--candidate-events":
                n = 2  # ``none`` is the one value of this run's list
            del out[i : i + n]
        return out

    for flags in (
        ("--draws",),
        ("--seed",),
        ("--stft-pct",),
        ("--ltft-pct",),
        ("--candidate-events",),
        ("--design",),
    ):
        payload = _refused(capsys, "argument_missing", *without(*flags))
        assert flags[0] in payload["message"]
    assert not any(tmp_path.iterdir())


def test_a_design_refuses_what_it_was_not_told(
    world: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prof = _profile_args(world)
    payload = _refused(
        capsys, "argument_missing", "design", "drives", *prof, "--base-revision", BASE,
        "--sessions", FIXTURE_SLICE, "--out", tmp_path,
    )  # fmt: skip
    assert "--fuel-type" in payload["message"]
    # The grid's own refusals come through by their library names.
    keep = {"--rpm-edges", "--load-edges"}
    for flag, reason in (
        ("--baro-kpa", "environment_unstated"),
        ("--iat-c", "environment_unstated"),
        ("--coolant-c", "environment_unstated"),
        ("--battery-v", "environment_unstated"),
        ("--rail-kpa", "environment_unstated"),
        ("--throttle-pct", "environment_unstated"),
        ("--pedal-flag", "environment_unstated"),
        ("--load-basis", "load_basis_unstated"),
    ):
        args = list(GRID_ARGS)
        i = args.index(flag)
        assert flag not in keep
        del args[i : i + 2]
        payload = _refused(
            capsys, reason, "design", "grid", *prof, "--base-revision", BASE, *args,
            "--out", tmp_path,
        )  # fmt: skip
        if reason == "environment_unstated":
            assert flag.removeprefix("--").replace("-", "_") in payload["message"]
    assert not any(tmp_path.iterdir())


def test_a_drive_design_refuses_by_the_librarys_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The committed slice ran on ZS44A-20260920, not on its predecessor.
    _refused(
        capsys, "drive_not_on_base_revision", "design", "drives",
        "--base-revision", "ZS44A-k106", "--sessions", FIXTURE_SLICE,
        "--fuel-type", "gasoline", "--out", tmp_path,
    )  # fmt: skip
    _refused(
        capsys, "session_missing", "design", "drives",
        "--base-revision", "ZS44A-20260920", "--sessions", tmp_path / "nope",
        "--fuel-type", "gasoline", "--out", tmp_path,
    )  # fmt: skip
    _refused(
        capsys, "base_revision_unknown", "design", "drives",
        "--base-revision", "no-such-revision", "--sessions", FIXTURE_SLICE,
        "--fuel-type", "gasoline", "--out", tmp_path,
    )  # fmt: skip
    assert not any(tmp_path.iterdir())


def test_the_out_directory_is_refused_inside_or_around_the_checkout() -> None:
    """The real checkout: nothing is written by this test, only the check runs."""
    for bad in (REPO / "data" / "x", REPO, REPO.parent, REPO / "tools" / ".." / "data"):
        with pytest.raises(RomEffectRefusalError) as refused:
            rom_sim._out_dir(bad)
        assert refused.value.reason == "inside_checkout"
    assert rom_sim._out_dir(Path("/tmp") / "elsewhere").name == "elsewhere"
    with pytest.raises(rom_sim.CliRefusalError) as missing:
        rom_sim._out_dir(None)
    assert missing.value.reason == "argument_missing"


def test_every_writing_subcommand_checks_out_before_it_does_any_work(
    world: dict[str, Any],
    grid_design: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checkout is moved to a scratch directory, so a regression writes
    there and not into the repository."""
    fake_repo = tmp_path / "checkout"
    fake_repo.mkdir()
    monkeypatch.setattr(rom_sim, "REPO_ROOT", fake_repo)
    out = fake_repo / "scratch"
    _refused(
        capsys, "inside_checkout", *_run_args(world, grid_design, out, "--edits", world["spec"])
    )
    _refused(
        capsys, "inside_checkout", "design", "grid", *_profile_args(world),
        "--base-revision", BASE, *GRID_ARGS, "--out", out,
    )  # fmt: skip
    _refused(
        capsys, "inside_checkout", "design", "drives", "--base-revision", "ZS44A-20260920",
        "--sessions", FIXTURE_SLICE, "--fuel-type", "gasoline", "--out", out,
    )  # fmt: skip
    # A parent of the checkout is refused too: the run would write around it.
    _refused(
        capsys,
        "inside_checkout",
        *_run_args(world, grid_design, tmp_path, "--edits", world["spec"]),
    )
    assert not out.exists() and sorted(p.name for p in fake_repo.iterdir()) == []


def test_a_run_refuses_a_design_built_on_another_image(
    world: dict[str, Any], grid_design: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other = tmp_path / "designs"
    code = rom_sim.main(
        [
            "design", "grid", "--base-revision", "ZS44A-20260920", *GRID_ARGS, "--out", str(other)
        ]
    )  # fmt: skip
    assert code == 0
    capsys.readouterr()  # the design command's own JSON
    (real_base_design,) = other.iterdir()
    payload = _refused(
        capsys, "design_revision_mismatch",
        *_run_args(world, real_base_design, tmp_path / "out", "--edits", world["spec"]),
    )  # fmt: skip
    assert not (tmp_path / "out").exists()
    assert "design" in payload["message"]


def test_a_run_refuses_a_missing_or_tampered_design(
    world: dict[str, Any], grid_design: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _refused(
        capsys, "design_missing",
        *_run_args(world, tmp_path / "nowhere", tmp_path / "o1", "--edits", world["spec"]),
    )  # fmt: skip
    copy = tmp_path / "copy" / grid_design.name
    shutil.copytree(grid_design, copy)
    record = json.loads((copy / "design.json").read_text())
    record["plant_baseline_sha256"] = "tampered"
    (copy / "design.json").write_text(json.dumps(record))
    _refused(
        capsys, "design_hash_mismatch",
        *_run_args(world, copy, tmp_path / "o2", "--edits", world["spec"]),
    )  # fmt: skip


def test_a_candidate_and_a_base_are_each_stated_exactly(
    world: dict[str, Any], grid_design: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "o"
    both = _run_args(world, grid_design, out, "--edits", world["spec"])
    both += ["--candidate-rom", str(world["root"] / "candidate.bin")]
    _refused(capsys, "candidate_unstated", *both)
    neither = _run_args(world, grid_design, out)
    _refused(capsys, "candidate_unstated", *neither)
    cand = ["--candidate-rom", str(world["root"] / "candidate.bin")]
    prof = _profile_args(world)
    _refused(capsys, "base_unstated", "inspect", *prof, *cand)
    _refused(capsys, "base_ambiguous", "inspect", *prof, *cand, "--revision", BASE,
             "--rom", world["root"] / "base.bin", "--sha256", world["sha"])  # fmt: skip
    _refused(
        capsys, "argument_missing", "inspect", *prof, *cand, "--rom", world["root"] / "base.bin"
    )
    _refused(
        capsys, "argument_missing", "inspect", *prof, *cand, "--revision", BASE, "--sha256", "a"
    )
    _refused(
        capsys, "candidate_events_ambiguous",
        *_run_args(world, grid_design, out, "--edits", world["spec"]), "--candidate-events",
        "none", "ecu_reflash",
    )  # fmt: skip
    assert not out.exists()


def test_the_librarys_refusals_surface_by_their_own_reasons(
    world: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prof = _profile_args(world)
    base = ["--rom", world["root"] / "base.bin", "--sha256", world["sha"]]
    # A base that is not the file it names.
    _refused(capsys, "image_hash_mismatch", "inspect", *prof, "--edits", world["spec"],
             "--rom", world["root"] / "base.bin", "--sha256", "1" * 64)  # fmt: skip
    # A spec on another base than the one given.
    _refused(capsys, "base_mismatch", "inspect", *prof, "--edits", world["spec_other_base"], *base)
    # An edit that changes nothing.
    _refused(capsys, "no_op_edit", "inspect", *prof, "--edits", world["spec_no_op"], *base)
    _refused(capsys, "edit_spec_not_found", "inspect", *prof, "--edits", tmp_path / "x.yaml", *base)
    # A file that is not an image the simulator can use: UpRev's encrypted backup.
    backup = tmp_path / "backup.bin"
    backup.write_bytes(encrypted_backup_lookalike())
    payload = _refused(
        capsys, "image_unreadable", "inspect", *prof, "--candidate-rom", backup, *base
    )
    assert "backup" in payload["message"].lower()
    _refused(
        capsys, "image_not_found", "inspect", *prof, "--candidate-rom", tmp_path / "gone", *base
    )
    # A revision without a ROM directory to find its image in.
    _refused(
        capsys, "image_not_found", "inspect", *prof, "--revision", BASE, "--edits", world["spec"],
        "--rom-dir", tmp_path,
    )  # fmt: skip
    # Not a profile.
    _refused(
        capsys,
        "profile_not_found",
        "inspect",
        "--profile",
        tmp_path / "none",
        "--edits",
        world["spec"],
    )


def test_the_revision_route_finds_its_image_in_the_rom_dirs_or_the_environment(
    world: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prof = _profile_args(world)
    # The registry names a rom_file for the real revisions only; a synthetic
    # revision with an image_sha256 is found by hash among --image files, which
    # the CLI does not take, so its refusal is the resolver's own.
    monkeypatch.delenv("CANARY_ROM_DIRS", raising=False)
    _refused(
        capsys, "image_not_found", "inspect", *prof, "--revision", BASE, "--edits", world["spec"]
    )
    monkeypatch.setenv("CANARY_ROM_DIRS", str(tmp_path))
    _refused(
        capsys, "image_not_found", "inspect", *prof, "--revision", BASE, "--edits", world["spec"]
    )


# -- the clean process ----------------------------------------------------------------------

_WRITE_PATHS = ("src.control", "src.daemon", "src.dashboard", "src.mcp_server", "can", "serial")
_ALLOWED_COLLECTION = {"src.collection", "src.collection.kline", "src.collection.kline.names"}


def test_the_command_line_imports_nothing_that_talks_to_the_ecu(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """Every subcommand's code path, in a fresh interpreter."""
    designs = tmp_path / "designs"
    run = tmp_path / "run"
    prof = _profile_args(world)
    base = ["--rom", str(world["root"] / "base.bin"), "--sha256", world["sha"]]
    steps = [
        ["design", "grid", *prof, "--base-revision", BASE, *GRID_ARGS, "--out", str(designs)],
        ["inspect", *prof, "--edits", str(world["spec"]), *base],
        ["inspect", *prof, "--candidate-rom", str(world["root"] / "candidate.bin"), *base],
        # design drives, on the committed slice and the real profile.
        [
            "design", "drives", "--base-revision", "ZS44A-20260920",
            "--sessions", str(FIXTURE_SLICE), "--fuel-type", "gasoline",
            "--battery-v-when-unmeasured", "14.0", "--out", str(tmp_path / "drive_designs"),
        ],
    ]  # fmt: skip
    # The design directory's name is its sha256, known only after step one.
    runner = textwrap.dedent(
        """
        import json, sys
        from pathlib import Path
        from tools import rom_sim
        first, rest = json.loads(sys.argv[1]), json.loads(sys.argv[2])
        for argv in first:
            assert rom_sim.main(argv) == 0, argv
        (design,) = Path(sys.argv[3]).iterdir()
        for argv in rest:
            assert rom_sim.main([a.replace("@DESIGN@", str(design)) for a in argv]) == 0, argv
        print(json.dumps(sorted(sys.modules)))
        """
    )
    rest = [
        [
            "run",
            *prof,
            *base,
            "--edits",
            str(world["spec"]),
            "--design",
            "@DESIGN@",
            "--draws",
            "2",
            "--seed",
            "1",
            *LEARNED,
            "--out",
            str(run),
        ]  # fmt: skip
    ]
    done = subprocess.run(
        [sys.executable, "-c", runner, json.dumps(steps), json.dumps(rest), str(designs)],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=600,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    assert "tools.rom_sim" in loaded and "src.analysis.sim_report" in loaded
    reached = [
        m
        for m in loaded
        if any(m == p or m.startswith(p + ".") for p in _WRITE_PATHS)
        or (m.startswith("src.collection") and m not in _ALLOWED_COLLECTION)
    ]
    assert reached == []
    assert (run / "result.json").is_file()


# -- --help ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "mentions"),
    [
        ([], ["design drives", "design grid", "inspect", "run", "inside the checkout", "exit 2"]),
        (["design"], ["drives", "grid"]),
        (
            ["design", "drives"],
            [
                "--base-revision",
                "--sessions",
                "--sessions-root",
                "--fuel-type",
                "--battery-v-when-unmeasured",
                "--rom-dir",
                "--out",
                "--profile",
                "--plant-version",
            ],
        ),  # fmt: skip
        (
            ["design", "grid"],
            [
                "--rpm-edges",
                "--load-edges",
                "--load-basis",
                "--baro-kpa",
                "--iat-c",
                "--coolant-c",
                "--battery-v",
                "--rail-kpa",
                "--throttle-pct",
                "--pedal-flag",
                "--out",
            ],
        ),  # fmt: skip
        (
            ["inspect"],
            [
                "--edits",
                "--candidate-rom",
                "--revision",
                "--rom",
                "--sha256",
                "--base",
                "--rom-dir",
            ],
        ),
        (
            ["run"],
            [
                "--edits",
                "--candidate-rom",
                "--revision",
                "--design",
                "--draws",
                "--seed",
                "--stft-pct",
                "--ltft-pct",
                "--candidate-events",
                "--grid",
                "--plant-version",
                "--out",
                "result.json",
                "traces.parquet",
                "report.html",
            ],
        ),  # fmt: skip
    ],
)
def test_help_documents_every_subcommand_and_argument(
    argv: list[str], mentions: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exited:
        rom_sim.main([*argv, "--help"])
    assert exited.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    missing = [m for m in mentions if m not in text]
    assert missing == []


def test_every_option_of_every_subcommand_is_in_its_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Introspect the parser: an argument added without help text is a failure."""
    parser = rom_sim._parser()
    stack = [((), parser)]
    seen = 0
    while stack:
        path, p = stack.pop()
        for action in p._actions:
            if action.option_strings and action.dest != "help":
                assert action.help, (path, action.option_strings)
                seen += 1
            sub = getattr(action, "choices", None)
            if isinstance(sub, dict):
                stack.extend(((*path, name), q) for name, q in sub.items())
    assert seen > 30


# -- review fixes: where outputs may go, non-finite numbers, partial outputs ------------------


def test_the_out_directory_is_refused_inside_or_around_a_rom_directory(
    world: dict[str, Any],
    grid_design: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = tmp_path / "roms"
    library.mkdir()
    link = tmp_path / "link"
    link.symlink_to(library)
    monkeypatch.delenv("CANARY_ROM_DIRS", raising=False)
    # By flag: inside, the directory itself, around it, and through a symlink.
    for out in (library / "zzsim", library, tmp_path, link / "zzsim"):
        _refused(
            capsys, "inside_rom_library",
            *_run_args(world, grid_design, out, "--edits", world["spec"], "--rom-dir", library),
        )  # fmt: skip
        _refused(
            capsys, "inside_rom_library", "design", "grid", *_profile_args(world),
            "--base-revision", BASE, *GRID_ARGS, "--rom-dir", library, "--out", out,
        )  # fmt: skip
        _refused(
            capsys, "inside_rom_library", "design", "drives", "--base-revision", "ZS44A-20260920",
            "--sessions", FIXTURE_SLICE, "--fuel-type", "gasoline", "--rom-dir", library,
            "--out", out,
        )  # fmt: skip
    # By environment, with no flag at all.
    monkeypatch.setenv("CANARY_ROM_DIRS", str(library))
    _refused(
        capsys, "inside_rom_library",
        *_run_args(world, grid_design, library / "zzsim", "--edits", world["spec"]),
    )  # fmt: skip
    assert sorted(p.name for p in library.iterdir()) == []
    # A directory beside it is fine.
    monkeypatch.delenv("CANARY_ROM_DIRS")
    other = tmp_path / "elsewhere"
    code = rom_sim.main(_run_args(world, grid_design, other, "--edits", str(world["spec"])))
    capsys.readouterr()
    assert code == 0 and (other / "result.json").is_file()


@pytest.mark.parametrize("value", ["nan", "inf"])
@pytest.mark.parametrize("flag", ["--stft-pct", "--ltft-pct"])
def test_a_non_finite_number_is_refused_and_nothing_is_written(
    world: dict[str, Any],
    grid_design: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
) -> None:
    args = _run_args(world, grid_design, tmp_path / "o", "--edits", world["spec"])
    args[args.index(flag) + 1] = value
    payload = _refused(capsys, "argument_invalid", *args)
    assert flag in payload["message"]
    assert not (tmp_path / "o").exists()
    # The grid's numbers and edges are checked the same way.
    grid = list(GRID_ARGS)
    grid[grid.index("--baro-kpa") + 1] = value
    _refused(
        capsys, "argument_invalid", "design", "grid", *_profile_args(world),
        "--base-revision", BASE, *grid, "--out", tmp_path / "d",
    )  # fmt: skip
    edges = list(GRID_ARGS)
    edges[edges.index("--rpm-edges") + 1] = value
    _refused(
        capsys, "argument_invalid", "design", "grid", *_profile_args(world),
        "--base-revision", BASE, *edges, "--out", tmp_path / "d",
    )  # fmt: skip
    assert not (tmp_path / "d").exists()


def test_a_failure_while_publishing_leaves_no_partial_outputs(
    world: dict[str, Any],
    grid_design: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _run_args(world, grid_design, tmp_path / "o", "--edits", world["spec"])
    # The result cannot be serialised: refused, and not even the traces appear.
    real_json = rom_sim._json

    def unserialisable(payload: Any) -> str:
        if "cell_table" in payload:  # the result file, not a refusal being printed
            raise ValueError("Out of range float values are not JSON compliant")
        return real_json(payload)

    monkeypatch.setattr(rom_sim, "_json", unserialisable)
    _refused(capsys, "result_not_finite", *args)
    assert not (tmp_path / "o").exists() or not any((tmp_path / "o").iterdir())
    monkeypatch.setattr(rom_sim, "_json", real_json)
    # A move that fails after others succeeded takes the earlier ones back.
    calls: list[Path] = []
    real_replace = Path.replace

    def failing_replace(self: Path, target: Any) -> Any:
        calls.append(self)
        if len(calls) == 3:
            raise OSError("disk went away")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    with pytest.raises(OSError, match="disk went away"):
        rom_sim.main([str(a) for a in args])
    capsys.readouterr()
    assert len(calls) == 3
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == []


def test_a_missing_required_argument_is_json_not_usage_text(
    world: dict[str, Any], grid_design: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prof = _profile_args(world)
    drop = {"--out", "--base-revision", "--rpm-edges", "--load-edges"}
    for flag in sorted(drop):
        args = list(GRID_ARGS) + ["--base-revision", BASE, "--out", str(tmp_path / "d")]
        i = args.index(flag)
        j = i + 1
        while j < len(args) and not args[j].startswith("--"):
            j += 1
        del args[i:j]
        payload = _refused(capsys, "argument_missing", "design", "grid", *prof, *args)
        assert flag in payload["message"]
    payload = _refused(
        capsys, "argument_missing", "design", "drives", *prof, "--fuel-type", "gasoline"
    )
    assert "--base-revision" in payload["message"] and "--sessions" in payload["message"]
    assert "--out" in payload["message"]
    run = _run_args(world, grid_design, tmp_path / "o", "--edits", world["spec"])
    del run[run.index("--out") : run.index("--out") + 2]
    assert "--out" in _refused(capsys, "argument_missing", *run)["message"]
    assert not (tmp_path / "d").exists()


def test_a_file_route_lists_the_changed_bytes_outside_any_table(
    world: dict[str, Any],
    grid_design: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An image saved elsewhere also has its checksum words changed; the edit
    list says so beside the table cells."""
    image = bytearray((world["root"] / "candidate.bin").read_bytes())
    word = world["kit"].rom_map.checksums.sum_offset
    for k in range(4):
        image[word + k] ^= 0xFF
    saved = world["root"] / "candidate_checksum.bin"
    saved.write_bytes(bytes(image))
    out = tmp_path / "o"
    code, _, _ = _cli(
        capsys,
        *_run_args(world, grid_design, out, "--candidate-rom", str(saved)),
    )
    assert code == 0
    rows = json.loads((out / "result.json").read_text())["run"]["edits"]
    assert rows[0]["table"] == "fuel_compensation" and rows[0]["changed"] == 1
    (extra,) = rows[1:]
    assert extra["table"] == f"checksum bytes 0x{word:X}-0x{word + 3:X}"
    assert (extra["changed"], extra["slot"], extra["selector"]) == (4, None, None)
    assert extra["table"] in (out / "report.html").read_text()
