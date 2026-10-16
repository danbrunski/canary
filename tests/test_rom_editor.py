"""W09.1: the ROM Editor's preview and simulate as pure functions.

Everything runs on E07.6's synthetic image (``tests.test_sim_montecarlo``'s
kit: the Xterra's layout, flat Fuel Comp 128, K 28,715), with the registry
entry that makes it a recorded revision stated here, never read from
``tune_revisions.yaml``. No real ``.OsirisROM`` is needed (W09.6 runs those).
"""

from __future__ import annotations

import ast
import copy
import json
import os
import pickle
import re
import shutil
import subprocess
import sys
import textwrap
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from src.analysis import rom_editor, sim_montecarlo
from src.analysis.ecu_application import ApplicationModelRefusalError
from src.analysis.fuel_plant import FuelPlantRefusalError, plant_qualification
from src.analysis.paired_scenarios import OUTPUTS, SimKit
from src.analysis.paired_steady import TRIM_AUTHORITY_UNDECODED
from src.analysis.rom_edit_spec import build_candidate, load_edit_spec
from src.analysis.rom_effects import RomEffectRefusalError, compare_roms
from src.analysis.rom_image import ImageFormatError, revision_match, sha256_hex
from src.analysis.rom_levers import classify_edit
from src.analysis.sim_contract import SimulationRefusalError
from src.analysis.sim_montecarlo import DESIGN_ASSUMPTION_PREFIX, MonteCarloResult
from src.vehicle.tune_revisions import ReferenceImage, TuneRevision, TuneRevisionRegistry
from tests.rom_fixtures import PROFILE
from tests.test_sim_montecarlo import build_kit_and_image

#: The synthetic image's registry entry, stated here: a flashed revision whose
#: plain-image hash is the synthetic image's.
REVISION = "synthetic-w09"

#: E08.1's light-load Fuel Comp edit, as the page sends it.
FC_EDIT: dict[str, Any] = {
    "table": "fuel_compensation",
    "slot": 1,
    "region": {"rpm": [1500, 3000], "load": [20, 50], "units": {"rpm": "rpm", "load": "%"}},
    "scale": 1.02,
    "rounding": "nearest",
}
FC_YAML = """\
schema_version: 1
base: {{revision: {revision}}}
label: light load 2 % richer
edits:
  - table: fuel_compensation
    slot: 1
    region:
      rpm: [1500, 3000]
      load: [20, 50]
      units: {{rpm: rpm, load: '%'}}
    scale: 1.02
    rounding: nearest
"""
K_EDIT: dict[str, Any] = {
    "table": "k_constant",
    "slot": 1,
    "all": True,
    "scale": 1.05,
    "rounding": "nearest",
}


@pytest.fixture(scope="module")
def kit_and_image() -> tuple[SimKit, bytes]:
    return build_kit_and_image()


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
        candidate_events="none",
        draws=6,
        seed=20261015,
    )
    body.update(over)
    return body


def _reason(fn: Any, *args: Any, **kw: Any) -> str:
    with pytest.raises((RomEffectRefusalError, SimulationRefusalError, ValueError)) as err:
        fn(*args, **kw)
    return str(getattr(err.value, "reason", type(err.value).__name__))


# -- preview ---------------------------------------------------------------------------------


def test_a_preview_is_build_candidate_on_the_equivalent_yaml_spec(
    kit_and_image: tuple[SimKit, bytes], matched: dict[str, Any], tmp_path: Path
) -> None:
    """The route adds nothing of its own: same cells, candidate and spec hash."""
    kit, image = kit_and_image
    preview = rom_editor.preview_edits(
        image, matched, "light load 2 % richer", [FC_EDIT], PROFILE, kit=kit
    )
    path = tmp_path / "spec.yaml"
    path.write_text(FC_YAML.format(revision=REVISION))
    spec = load_edit_spec(path)
    direct = build_candidate(
        spec,
        kit.rom_map,
        rom_editor.library_base(image, matched),
        model=kit.model,
        model_sha256=kit.model_sha256,
        slot=1,
        slot_source="default_slot",
    )
    assert preview["spec_sha256"] == spec.sha256()
    assert preview["candidate_sha256"] == direct.image_sha256
    assert preview["provenance"]["cells"] == direct.provenance["cells"]
    assert preview["provenance"] == direct.provenance
    assert preview["spec"]["base"] == {"revision": REVISION, "file": None, "sha256": None}
    # The E08.1 provenance whole: cells, ranges and the reach.
    prov = preview["provenance"]
    assert prov["flashable"] is False and prov["route"] == "edit_spec"
    # rpm rows 1,600-2,800 (3..6) x fill 21.4-50.0 % (bytes 48..112, cols 3..7).
    assert {(c["row"], c["col"]) for c in prov["cells"]} == {
        (r, c) for r in range(3, 7) for c in range(3, 8)
    }
    assert all(c["new"] == 131 and c["old"] == 128 for c in prov["cells"])
    assert prov["effect"]["changed_nodes"] == ["fuel_comp_lookup"]
    assert prov["ranges"]
    # Only the edited (table, slot), with the candidate's values.
    (table,) = preview["tables"]
    assert (table["id"], table["slot"]) == ("fuel_compensation", 1)
    for cell in prov["cells"]:
        assert table["raw"][cell["row"]][cell["col"]] == cell["new"]
    assert preview["sim_base"] == {
        "ok": True,
        "status": "matched",
        "revision": REVISION,
        "reason": None,
    }
    json.dumps(preview)


def _classified(kit: SimKit, image: bytes, candidate: bytes, cells: Any) -> dict[str, Any]:
    """``classify_edit`` called the way ``tools/rom_sim.py inspect`` calls it,
    on the kit's own plant: the answer the preview must equal."""
    report = compare_roms(
        image,
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
    return classify_edit(
        report,
        rom_map=kit.rom_map,
        model=kit.model,
        contract=kit.contract,
        qualification=plant_qualification(kit.plant),
        cells=cells,
    ).to_dict()


@pytest.mark.parametrize(
    ("edit", "lever", "cells"),
    [
        (FC_EDIT, "fuel_compensation", 20),
        (K_EDIT, "k_constant", 1),
        (
            {"table": "fuel_targets", "slot": 1, "cells": [[7, 7], [7, 6]], "set_raw": 151},
            "fuel_targets",
            2,
        ),
    ],
)
def test_a_preview_names_the_lever_and_says_what_blocks_a_number(
    kit_and_image: tuple[SimKit, bytes],
    matched: dict[str, Any],
    edit: dict[str, Any],
    lever: str,
    cells: int,
) -> None:
    """E09.14b: ``levers`` is ``classify_edit`` on the run's comparison, whole
    and unreworded; the page lists ``levers["messages"]``."""
    kit, image = kit_and_image
    preview = rom_editor.preview_edits(image, matched, "levers", [edit], PROFILE, kit=kit)
    levers = preview["levers"]
    assert [e["lever"] for e in levers["levers"]] == [lever]
    assert levers["levers"][0]["changed_cells"] == cells
    assert levers["schema_version"] == 1 and levers["regime"] is None
    assert levers["messages"] and all(isinstance(m, str) for m in levers["messages"])
    candidate = build_candidate(
        rom_editor.editor_spec(rom_editor.library_base(image, matched), "levers", [edit]),
        kit.rom_map,
        rom_editor.library_base(image, matched),
        model=kit.model,
        model_sha256=kit.model_sha256,
        slot=1,
        slot_source="default_slot",
    )
    assert levers == _classified(kit, image, candidate.image, preview["provenance"]["cells"])
    # A preview is data: JSON-clean, and it never carries image bytes.
    assert json.loads(json.dumps(preview)) == preview
    assert not _bytes_in(preview)


def _bytes_in(value: Any) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if isinstance(value, dict):
        return any(_bytes_in(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_bytes_in(v) for v in value)
    return False


def test_an_edit_across_two_tables_names_both_levers_and_travels_through_the_job(
    kit_and_image: tuple[SimKit, bytes], matched: dict[str, Any], tmp_path: Path
) -> None:
    kit, image = kit_and_image
    params = _params(image, tmp_path, matched, label="two", edits=[FC_EDIT, K_EDIT])
    job = rom_editor.run_job("rom_preview", params, kit=kit)
    assert [e["lever"] for e in job["levers"]["levers"]] == ["fuel_compensation", "k_constant"]
    # The child's answer crosses the process boundary: it pickles, and the strings survive.
    assert pickle.loads(pickle.dumps(job))["levers"]["messages"] == job["levers"]["messages"]
    assert any(
        m.startswith("Fuel Comp + K -> base_fuel_schedule_word: a number can be given")
        for m in job["levers"]["messages"]
    ), job["levers"]["messages"]


def test_a_vehicle_with_no_declared_simulator_plant_is_refused_a_preview_by_name(
    kit_and_image: tuple[SimKit, bytes], matched: dict[str, Any], tmp_path: Path
) -> None:
    """The classification qualifies the plant ``sim_kit.yaml`` declares, never
    the contract's default, so a profile without one cannot be previewed: the
    refusal names it (a preview that said nothing about the plant would read as
    "nothing blocks")."""
    _, image = kit_and_image
    profile = tmp_path / "no_sim_kit"
    shutil.copytree(PROFILE, profile, ignore=shutil.ignore_patterns("_candidates", "_prior"))
    (profile / "sim_kit.yaml").unlink()
    params = _params(image, tmp_path, matched, profile_dir=str(profile), label="x", edits=[FC_EDIT])
    refused = rom_editor.run_job("rom_preview", params)["refused"]
    assert (refused["kind"], refused["reason"]) == ("simulation", "sim_kit_undeclared")


def test_an_unrecorded_base_previews_by_file_and_says_why_it_cannot_simulate(
    kit_and_image: tuple[SimKit, bytes], tmp_path: Path
) -> None:
    kit, image = kit_and_image
    sha = sha256_hex(image)
    for status in (
        revision_match(sha, None),
        revision_match(
            sha,
            TuneRevisionRegistry(
                reference_images=[ReferenceImage(name="tuner-base", image_sha256=sha)]
            ),
        ),
    ):
        preview = rom_editor.preview_edits(image, status, "x", [FC_EDIT], PROFILE, kit=kit)
        assert preview["spec"]["base"] == {"revision": None, "file": f"{sha}.bin", "sha256": sha}
        assert preview["sim_base"]["ok"] is False
        assert preview["sim_base"]["reason"] == "base_not_a_recorded_revision"
        # The file-based spec round-trips too, beside its image.
        (tmp_path / f"{sha}.bin").write_bytes(image)
        path = tmp_path / "spec.yaml"
        path.write_text(preview["spec_yaml"])
        assert load_edit_spec(path).sha256() == preview["spec_sha256"]


def test_the_library_key_must_be_the_bytes(kit_and_image: tuple[SimKit, bytes]) -> None:
    _, image = kit_and_image
    status = revision_match(sha256_hex(image), None)
    assert _reason(rom_editor.library_base, image, status, sha256="0" * 64) == "image_hash_mismatch"
    bad = {"status": "matched", "revision": None, "reference": None}
    assert _reason(rom_editor.library_base, image, bad) == "revision_match_invalid"


@pytest.mark.parametrize(
    ("edits", "reason"),
    [
        ([{"table": "timing_trim_a", "slot": 1, "all": True, "add_raw": 1}], "table_not_editable"),
        (
            [{"table": "fuel_targets_maps_2_5", "slot": 2, "all": True, "add_raw": 1}],
            "slot_not_simulated",
        ),
        ([{"table": "k_constant", "slot": 3, "all": True, "add_raw": 1}], "slot_not_simulated"),
        (
            [
                {"table": "fuel_compensation", "slot": 1, "cells": [[2, 3]], "add_raw": 1},
                {"table": "fuel_compensation", "slot": 1, "cells": [[2, 3]], "add_raw": 2},
            ],
            "duplicate_edit",
        ),
        (
            [{"table": "fuel_compensation", "slot": 1, "cells": [[2, 3]], "set_raw": 128}],
            "no_op_edit",
        ),
        (
            [{"table": "fuel_compensation", "slot": 1, "cells": [[2, 3]], "set_raw": 300}],
            "value_out_of_range",
        ),
        (
            [{"table": "fuel_compensation", "slot": 1, "all": True, "scale": 1.02}],
            "rounding_undeclared",
        ),
        ([], "no_edits"),
    ],
)
def test_edit_refusals_come_back_by_name(
    kit_and_image: tuple[SimKit, bytes],
    matched: dict[str, Any],
    edits: list[dict[str, Any]],
    reason: str,
) -> None:
    kit, image = kit_and_image
    assert _reason(rom_editor.preview_edits, image, matched, "x", edits, PROFILE, kit=kit) == reason


def test_the_downloaded_spec_yaml_round_trips(
    kit_and_image: tuple[SimKit, bytes], matched: dict[str, Any], tmp_path: Path
) -> None:
    kit, image = kit_and_image
    edits = [
        FC_EDIT,
        K_EDIT,
        {"table": "fuel_targets", "slot": 1, "cells": [[7, 7], [7, 6]], "set_raw": 151},
    ]
    preview = rom_editor.preview_edits(image, matched, "three tables", edits, PROFILE, kit=kit)
    path = tmp_path / "three.edit-spec.yaml"
    path.write_text(preview["spec_yaml"])
    loaded = load_edit_spec(path)
    assert loaded.sha256() == preview["spec_sha256"]
    assert loaded.model_dump(mode="json") == preview["spec"]
    assert {(t["id"], t["slot"]) for t in preview["tables"]} == {
        ("fuel_compensation", 1),
        ("k_constant", 1),
        ("fuel_targets", 1),
    }


# -- the request ------------------------------------------------------------------------------


def test_no_request_field_has_a_default_and_each_missing_one_is_named() -> None:
    fields = rom_editor.SimRequest.model_fields
    assert fields and all(f.is_required() for f in fields.values())
    full = request()
    assert set(full) == set(fields)
    rom_editor.SimRequest.model_validate(full)
    for name in fields:
        partial = {k: v for k, v in full.items() if k != name}
        with pytest.raises(ValidationError) as err:
            rom_editor.SimRequest.model_validate(partial)
        assert [(e["type"], e["loc"]) for e in err.value.errors()] == [("missing", (name,))]
    with pytest.raises(ValidationError) as err:
        rom_editor.SimRequest.model_validate({**full, "slot": 2})
    assert err.value.errors()[0]["type"] == "extra_forbidden"


def test_bounds_and_the_base_are_refused_before_any_work(
    kit_and_image: tuple[SimKit, bytes], matched: dict[str, Any], tmp_path: Path
) -> None:
    """A profile directory that does not exist would fail any work done: the
    refusals come first and name themselves."""
    _, image = kit_and_image
    nowhere = tmp_path / "no-profile"
    edges = [float(400 + 100 * i) for i in range(34)]  # 33 x 33 cells
    for over, reason in (
        (dict(rpm_edges=edges, load_edges=[float(i) for i in range(34)]), "grid_too_large"),
        (dict(draws=rom_editor.MAX_DRAWS + 1), "draws_too_many"),
    ):
        req = rom_editor.SimRequest.model_validate(request(**over))
        assert _reason(rom_editor.simulate_edits, image, matched, req, nowhere) == reason
    at_cap = rom_editor.SimRequest.model_validate(
        request(rpm_edges=edges[:33], load_edges=[float(i) for i in range(33)])
    )
    assert at_cap.grid_points == rom_editor.MAX_GRID_POINTS
    unrecorded = revision_match(sha256_hex(image), None)
    req = rom_editor.SimRequest.model_validate(request())
    assert (
        _reason(rom_editor.simulate_edits, image, unrecorded, req, nowhere)
        == "base_not_a_recorded_revision"
    )


# -- simulate ---------------------------------------------------------------------------------


def test_the_negative_control_is_unchanged_in_every_visited_cell(
    kit_and_image: tuple[SimKit, bytes],
    matched: dict[str, Any],
    registry: TuneRevisionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E08.1 refuses a no-op spec, so the base's own bytes go through the
    private seam simulate_edits itself uses."""
    kit, image = kit_and_image
    runs: list[MonteCarloResult] = []
    real = sim_montecarlo.run_monte_carlo

    def spy(*args: Any, **kw: Any) -> MonteCarloResult:
        runs.append(real(*args, **kw))
        return runs[-1]

    monkeypatch.setattr(rom_editor, "run_monte_carlo", spy)
    base = rom_editor.library_base(image, matched)
    # The default grid: five points open loop (the plant is supported there)
    # and one closed loop, so both sides of the chain are exercised.
    req = rom_editor.SimRequest.model_validate(request())
    out = rom_editor._simulate(
        base,
        bytes(image),
        {"route": "negative_control"},
        req,
        kit=kit,
        profile_dir=PROFILE,
        registry=registry,
    )
    cells = out["cells"]
    assert out["outcome"] == "identical"
    assert {r["status"] for r in cells["reach"].values()} == {"no_change"}
    visited = {(c["row"], c["col"]): c for c in cells["cells"] if c["pairs"]}
    assert len(visited) == out["design"]["points"] == 6
    #: The one closed-loop cell (4,400 rpm, 45 % fill): its plant outputs are
    #: unsupported by name; everywhere else every output is exactly unchanged.
    closed = (10, 6)
    assert visited[closed]["loop_shares"] == {"closed_loop": 1.0}
    open_cells = [c for k, c in visited.items() if k != closed]
    assert len(open_cells) == 5
    assert all(c["loop_shares"] == {"open_loop": 1.0} for c in open_cells)
    for cell in open_cells:
        assert set(cell["outputs"]) == set(OUTPUTS)
        for name, o in cell["outputs"].items():
            where = (cell["row"], cell["col"], name)
            assert o["status"] == "unchanged", where
            assert o["supported_share"] == 1.0 and o["changed_share"] == 0.0, where
            assert o["quantiles"]["delta"] == {"p05": 0.0, "p50": 0.0, "p95": 0.0}, where
    ecu = ("base_fuel_schedule", "target_phi")
    for name, o in visited[closed]["outputs"].items():
        if name in ecu:
            assert o["status"] == "unchanged", name
        else:
            assert o["status"] == "unsupported", name
            assert [e["reasons"] for e in o["support"]] == [[TRIM_AUTHORITY_UNDECODED]], name
            q = o["quantiles"]
            if q is not None:
                assert q["delta"] == {"p05": 0.0, "p50": 0.0, "p95": 0.0}, name
    assert cells["overlay"]["edited_cells"] == [] and cells["overlay"]["axis_edits"] == []
    # Every draw of every point, from the run the cells came from: whatever
    # was computed is identical on both sides.
    (result,) = runs
    for name in result.outputs:
        computed = np.isfinite(result.base[name]) & np.isfinite(result.candidate[name])
        assert np.array_equal(result.base[name][computed], result.candidate[name][computed])
    assert np.isfinite(result.base["base_fuel_schedule"]).all()


def test_a_k_edit_moves_bfs_by_the_realised_raw_ratio_in_every_open_loop_pair(
    kit_and_image: tuple[SimKit, bytes],
    matched: dict[str, Any],
    registry: TuneRevisionRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E07.6's known answer through the editor: BFS = K x Qa / Ne x FC, so K
    scaled by r moves every supported open-loop BFS by r (to the word's
    rounding), at every pair."""
    kit, image = kit_and_image
    runs: list[MonteCarloResult] = []
    real = sim_montecarlo.run_monte_carlo

    def spy(*args: Any, **kw: Any) -> MonteCarloResult:
        runs.append(real(*args, **kw))
        return runs[-1]

    monkeypatch.setattr(rom_editor, "run_monte_carlo", spy)
    req = rom_editor.SimRequest.model_validate(request())
    out = rom_editor.simulate_edits(image, matched, req, PROFILE, kit=kit, registry=registry)
    (cell_edit,) = out["candidate"]["cells"]
    assert (cell_edit["table"], cell_edit["old"]) == ("k_constant", 28715)
    ratio = cell_edit["new"] / cell_edit["old"]  # 30151 / 28715, not 1.05 exactly
    assert ratio != 1.05

    # Per pair, from the run the cells were aggregated from.
    (result,) = runs
    base = result.base["base_fuel_schedule"]
    cand = result.candidate["base_fuel_schedule"]
    open_pairs = (result.loop == "open_loop") & result.supported("base_fuel_schedule")
    # One point (4,400 rpm, light load) runs closed loop; the other five open.
    assert open_pairs.sum() == 5 * result.draws
    # BFS lands on a grid of whole steps on each side (4 counts here, the
    # gcd of every value the run gave): within one step of r x base, no more.
    step = int(np.gcd.reduce(np.concatenate([base[open_pairs], cand[open_pairs]]).astype(int)))
    assert 1 <= step <= 4
    assert np.max(np.abs(cand[open_pairs] - ratio * base[open_pairs])) <= step
    assert np.min(base[open_pairs]) > 10_000  # so one step is < 4e-4 of the ratio

    # Per cell: the median relative delta is the realised ratio - 1.
    checked = 0
    for cell in out["cells"]["cells"]:
        if cell["loop_shares"] != {"open_loop": 1.0}:
            continue
        bfs = cell["outputs"]["base_fuel_schedule"]
        assert bfs["status"] == "changed" and bfs["supported_share"] == 1.0
        rel = bfs["quantiles"]["relative_delta"]
        for q in ("p05", "p50", "p95"):
            assert rel[q] == pytest.approx(ratio - 1.0, abs=5e-4)
        checked += 1
    assert checked == 5 and out["design"]["points"] == 6
    # Caveats unchanged, and first: only the payload's identity precedes them,
    # and they lead every finding (assumptions, unresolved, ..., cells).
    assert out["caveats"] == result.caveats
    keys = list(out)
    lead = keys.index("caveats")
    assert keys[:lead] == ["tool", "tool_version", "label", "synthetic"]
    for later in ("assumptions", "unresolved", "inert_parameters", "coverage", "design", "cells"):
        assert keys.index(later) > lead, later
    # The design's own assumptions are stated once: in `assumptions`, "design: "-prefixed
    # (E08.10), with the plant's, and `caveats` repeats them. `design` carries no second copy.
    assert "assumptions" not in out["design"]
    designed = [a for a in out["assumptions"] if a.startswith(DESIGN_ASSUMPTION_PREFIX)]
    assert designed and len(set(out["assumptions"])) == len(out["assumptions"])
    assert all(a in out["caveats"] for a in out["assumptions"])
    assert out["provenance"]["seed"] == 20261015 and out["provenance"]["draws"] == 6
    assert out["design"]["grid"]["rpm_edges"] == [4000.0, 4800.0, 5600.0]
    assert load_spec_sha(out["spec_yaml"]) == out["spec_sha256"]
    json.dumps(out)
    assert pickle.loads(pickle.dumps(out)) == out


def load_spec_sha(text: str) -> str:
    import yaml

    from src.analysis.rom_edit_spec import parse_edit_spec

    return parse_edit_spec(yaml.safe_load(text)).sha256()


def test_the_target_afr_grid_and_a_reflash_event_are_carried(
    kit_and_image: tuple[SimKit, bytes],
    matched: dict[str, Any],
    registry: TuneRevisionRegistry,
) -> None:
    kit, image = kit_and_image
    req = rom_editor.SimRequest.model_validate(
        request(grid="target_afr", candidate_events="ecu_reflash", ltft_pct=3.0, draws=2)
    )
    out = rom_editor.simulate_edits(image, matched, req, PROFILE, kit=kit, registry=registry)
    assert out["cells"]["grid"] == "target_afr"
    with pytest.raises(ValidationError):
        rom_editor.SimRequest.model_validate(request(grid="fuel_targets"))
    assert out["provenance"]["candidate_events"] == ["ecu_reflash"]
    assert out["learned_state"]["candidate"]["ltft_pct"] == 0.0
    assert out["learned_state"]["base"]["ltft_pct"] == 3.0


# -- the child entry ----------------------------------------------------------------------------


def _params(image: bytes, tmp_path: Path, status: dict[str, Any], **over: Any) -> dict[str, Any]:
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


@pytest.mark.parametrize(
    ("over", "kind", "reason"),
    [
        (
            dict(edits=[{"table": "timing_trim_a", "slot": 1, "all": True, "add_raw": 1}]),
            "edit_spec",
            "table_not_editable",
        ),
        (
            dict(edits=[{"table": "fuel_targets", "slot": 1, "cells": [[0, 0]], "set_raw": 300}]),
            "rom_effect",
            "value_out_of_range",
        ),
        (dict(rpm_edges=[2000.0]), "sim_design", "grid_edges_invalid"),
        (dict(draws=rom_editor.MAX_DRAWS + 1), "simulation", "draws_too_many"),
        (dict(seed=None), "request", "request_invalid"),
    ],
)
def test_run_job_returns_each_refusal_class_as_data_that_pickles(
    kit_and_image: tuple[SimKit, bytes],
    matched: dict[str, Any],
    registry: TuneRevisionRegistry,
    tmp_path: Path,
    over: dict[str, Any],
    kind: str,
    reason: str,
) -> None:
    kit, image = kit_and_image
    params = _params(image, tmp_path, matched, request=request(**over))
    out = rom_editor.run_job("rom_sim", params, kit=kit, registry=registry)
    assert set(out) == {"refused"}
    assert out["refused"]["kind"] == kind and out["refused"]["reason"] == reason
    assert out["refused"]["message"] and not out["refused"]["message"].startswith(f"{reason}:")
    assert pickle.loads(pickle.dumps(out)) == out
    json.dumps(out)


@pytest.mark.parametrize(
    ("exc", "kind", "reason"),
    [
        (
            FuelPlantRefusalError("pressure_unmodelled", "no rail model"),
            "fuel_plant",
            "pressure_unmodelled",
        ),
        (
            ApplicationModelRefusalError("wrong_program", "not this ECU"),
            "application_model",
            "wrong_program",
        ),
        (ImageFormatError("not a 1 MiB image"), "image_format", "image_format"),
    ],
)
def test_the_refusals_beneath_the_paired_run_are_data_too(
    monkeypatch: pytest.MonkeyPatch,
    exc: ValueError,
    kind: str,
    reason: str,
) -> None:
    """FuelPlantRefusalError and ApplicationModelRefusalError take (reason, message)
    and do not pickle either: raised in the spawn child they would reach the route
    as an opaque 503 (W09 Decisions). ImageFormatError carries only a message."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(rom_editor, "_job_image", boom)
    out = rom_editor.run_job("rom_sim", {})
    assert out["refused"]["kind"] == kind and out["refused"]["reason"] == reason
    assert out["refused"]["message"] and not out["refused"]["message"].startswith(f"{reason}:")
    assert pickle.loads(pickle.dumps(out)) == out


def test_run_job_checks_the_image_and_runs_both_kinds(
    kit_and_image: tuple[SimKit, bytes],
    matched: dict[str, Any],
    registry: TuneRevisionRegistry,
    tmp_path: Path,
) -> None:
    kit, image = kit_and_image
    params = _params(image, tmp_path, matched, request=request(draws=2))
    wrong = rom_editor.run_job("rom_sim", {**params, "sha256": "0" * 64}, kit=kit)
    assert wrong["refused"]["reason"] == "image_hash_mismatch"
    missing = rom_editor.run_job("rom_sim", {**params, "image_path": str(tmp_path / "x.bin")})
    assert missing["refused"]["reason"] == "image_not_found"
    unrecorded = rom_editor.run_job(
        "rom_sim", {**params, "revision_match": revision_match(sha256_hex(image), None)}
    )
    assert unrecorded["refused"] == {
        "kind": "simulation",
        "reason": "base_not_a_recorded_revision",
        "message": unrecorded["refused"]["message"],
    }
    done = rom_editor.run_job("rom_sim", params, kit=kit, registry=registry)
    assert "refused" not in done and done["cells"]["schema_version"] == 1
    assert pickle.loads(pickle.dumps(done)) == done
    preview = rom_editor.run_job(
        "rom_preview", {**params, "label": "x", "edits": [FC_EDIT]}, kit=kit
    )
    assert preview["candidate_sha256"] != sha256_hex(image)
    with pytest.raises(ValueError, match="job kind"):
        rom_editor.run_job("rom_flash", params)


# -- no write path ---------------------------------------------------------------------------

_WRITE_PATHS = ("src.control", "src.daemon", "src.dashboard", "src.mcp_server", "can", "serial")
_ALLOWED_COLLECTION = {"src.collection", "src.collection.kline", "src.collection.kline.names"}


def test_the_rom_editor_imports_nothing_that_talks_to_the_ecu() -> None:
    script = textwrap.dedent(
        """
        import json, sys
        from datetime import UTC, datetime
        from src.analysis import rom_editor, sim_montecarlo
        from src.analysis.rom_image import revision_match, sha256_hex
        from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry
        from tests.rom_fixtures import PROFILE
        from tests.test_rom_editor import FC_EDIT, request
        from tests.test_sim_montecarlo import build_kit_and_image
        kit, image = build_kit_and_image()
        sha = sha256_hex(image)
        registry = TuneRevisionRegistry(revisions=[TuneRevision(
            revision="r", flashed_at=datetime(2026, 10, 1, tzinfo=UTC), image_sha256=sha)])
        status = revision_match(sha, registry)
        rom_editor.preview_edits(image, status, "x", [FC_EDIT], PROFILE, kit=kit)
        req = rom_editor.SimRequest.model_validate(request(edits=[FC_EDIT], draws=2))
        rom_editor.simulate_edits(image, status, req, PROFILE, kit=kit, registry=registry)
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
    assert "src.analysis.rom_editor" in loaded
    reached = [
        m
        for m in loaded
        if any(m == p or m.startswith(p + ".") for p in _WRITE_PATHS)
        or (m.startswith("src.collection") and m not in _ALLOWED_COLLECTION)
    ]
    assert reached == []


# -- typical values (W09.11) -----------------------------------------------------------------

TYPICAL_FILE = PROFILE / rom_editor.TYPICAL_VALUES_FILE
REPO_ROOT = Path(__file__).resolve().parents[1]


def _typical_declaration() -> dict[str, Any]:
    loaded = yaml.safe_load(TYPICAL_FILE.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _profile_with(tmp_path: Path, text: str | None) -> Path:
    """A profile directory holding exactly this typical-values file (or none)."""
    profile = tmp_path / "vehicle-x"
    profile.mkdir()
    if text is not None:
        (profile / rom_editor.TYPICAL_VALUES_FILE).write_text(text)
    return profile


def _refusal(profile: Path) -> SimulationRefusalError:
    with pytest.raises(SimulationRefusalError) as err:
        rom_editor.typical_values(profile)
    return err.value


def test_the_shipped_typical_values_load_with_a_source_for_every_value() -> None:
    body = rom_editor.typical_values(PROFILE)
    assert body["profile"] == PROFILE.name
    assert body["file"] == "sim_typical_values.yaml"
    assert body["file_sha256"] == sha256_hex(TYPICAL_FILE.read_bytes())
    assert body["values"], "a vehicle that ships the file declares at least one value"
    for name, declared in body["values"].items():
        assert set(declared) == {"value", "kind", "source"}, name
        assert declared["kind"] in rom_editor.TYPICAL_KINDS, name
        assert declared["source"].strip(), f"{name} has no source"
        if declared["kind"] == "placeholder":
            assert "placeholder" in declared["source"].lower(), name
        # A number a form can be filled with, or the rail's one named alternative.
        assert declared["value"] == "working_assumption" or isinstance(
            declared["value"], (int, float)
        )
    for name, why in body["left_empty"].items():
        assert set(why) == {"reason"} and why["reason"].strip(), name
    json.dumps(body)  # JSON-clean: it is a route body
    pickle.loads(pickle.dumps(body))


def test_every_fillable_field_is_a_value_or_a_reasoned_omission_and_a_simrequest_field() -> None:
    """A typo cannot ship, and no fillable field is silently absent."""
    body = rom_editor.typical_values(PROFILE)
    declared = list(body["values"]) + list(body["left_empty"])
    assert sorted(declared) == sorted(rom_editor.TYPICAL_FIELDS)
    assert set(declared) <= set(rom_editor.SimRequest.model_fields)
    assert not set(declared) & set(rom_editor.NEVER_TYPICAL_FIELDS)
    # The raw file says the same, so a key the model ignored would show here.
    raw = _typical_declaration()
    assert set(raw) == {"schema_version", "values", "left_empty"}
    assert sorted([*raw["values"], *raw["left_empty"]]) == sorted(rom_editor.TYPICAL_FIELDS)
    for name, entry in raw["values"].items():
        assert set(entry) == {"value", "kind", "source"}, name
        assert isinstance(entry["source"], str) and entry["source"].strip(), name


def test_a_new_simrequest_field_must_be_classified_as_typical_or_never() -> None:
    """The page's button fills only what the file may declare. A field added to
    the request is neither until someone decides, and this test says so."""
    fields = set(rom_editor.SimRequest.model_fields)
    typical, never = set(rom_editor.TYPICAL_FIELDS), set(rom_editor.NEVER_TYPICAL_FIELDS)
    assert not typical & never
    assert typical | never == fields, f"unclassified: {sorted(fields - typical - never)}"
    # The two experiment choices and the run's own size and identity are never filled.
    assert {
        "grid",
        "candidate_events",
        "rpm_edges",
        "load_edges",
        "draws",
        "seed",
        "label",
    } <= never


def test_the_shipped_values_and_the_remaining_fields_make_a_valid_request() -> None:
    body = rom_editor.typical_values(PROFILE)
    filled = {name: declared["value"] for name, declared in body["values"].items()}
    stated_by_the_operator = {name: 0.0 for name in body["left_empty"]}
    complete = request(**filled, **stated_by_the_operator)
    # Only the typical fields differ from the stated request; the others are untouched.
    assert set(complete) == set(rom_editor.SimRequest.model_fields)
    parsed = rom_editor.SimRequest.model_validate(complete)
    assert parsed.pedal_flag == body["values"]["pedal_flag"]["value"]


def _call_keywords(path: Path, name: str) -> dict[str, Any]:
    """The constant keyword arguments of ``NAME = dict(...)`` in a test module,
    read from its source (importing the private-ROM module would run its imports
    and mark nothing)."""
    for node in ast.parse(path.read_text()).body:
        target = node.target if isinstance(node, ast.AnnAssign) else None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        call = getattr(node, "value", None)
        assert isinstance(call, ast.Call), f"{name} is not a dict(...) call"
        return {k.arg: ast.literal_eval(k.value) for k in call.keywords if k.arg}
    raise AssertionError(f"{path.name} has no {name}")


def _find_parameter(node: Any, parameter_id: str) -> dict[str, Any] | None:
    if isinstance(node, dict):
        if node.get("id") == parameter_id:
            return node
        node = list(node.values())
    if isinstance(node, list):
        for item in node:
            found = _find_parameter(item, parameter_id)
            if found is not None:
                return found
    return None


def test_each_typical_value_equals_the_constant_its_source_names() -> None:
    """The sources are cited by file and symbol. A value that drifts from the
    constant it cites, or a citation to a file that is gone, fails here."""
    body = rom_editor.typical_values(PROFILE)
    values = {name: declared["value"] for name, declared in body["values"].items()}

    from tests.test_paired_scenarios import LEARNED, WOT

    for field in ("baro_kpa", "iat_c", "coolant_c", "battery_v", "rail_kpa"):
        assert values[field] == getattr(WOT, field), f"E07.6's WOT point states {field}"
    for field in (
        "stft_pct",
        "ltft_pct",
        "evap_purge_area",
        "throttle_closed_learned",
        "idle_air_learned",
    ):
        assert values[field] == getattr(LEARNED, field), f"E07.6's LEARNED states {field}"

    env = _call_keywords(REPO_ROOT / "tests" / "test_rom_editor_private_rom.py", "ENV")
    for field in ("baro_kpa", "iat_c", "coolant_c", "battery_v", "rail_kpa"):
        assert values[field] == env[field], f"W09.6's ENV states {field}"
    assert values["throttle_pct"] == env["throttle_pct"]
    assert values["pedal_flag"] == env["pedal_flag"]

    # The rail's value is the plant's own declared reference pressure.
    plant = yaml.safe_load((PROFILE / "fuel_plant_mv2.yaml").read_text())
    reference = _find_parameter(plant, "reference_delta_p")
    assert reference is not None and reference["value"] == values["rail_kpa"]

    # Every file a source cites exists, so a rename cannot leave a dead citation.
    cited = {
        match
        for declared in (*body["values"].values(), *body["left_empty"].values())
        for match in re.findall(
            r"(?:tests|data|docs|src)/[\w./-]+?\.(?:py|yaml|md)",
            " ".join(str(item) for item in declared.values()),
        )
    }
    assert cited, "the sources cite no repo file"
    assert [c for c in sorted(cited) if not (REPO_ROOT / c).is_file()] == []


def test_a_vehicle_with_no_typical_values_file_is_refused_by_name(tmp_path: Path) -> None:
    refused = _refusal(_profile_with(tmp_path, None))
    assert refused.reason == "typical_values_undeclared"
    assert "vehicle-x" in refused.message and "sim_typical_values.yaml" in refused.message
    # It survives the process boundary, as every refusal class here must.
    again = pickle.loads(pickle.dumps(refused))
    assert (again.reason, again.message) == (refused.reason, refused.message)


def _mutated(mutate: Any) -> str:
    declaration = copy.deepcopy(_typical_declaration())
    mutate(declaration)
    return yaml.safe_dump(declaration, sort_keys=False)


UNSOURCED = _mutated(lambda d: d["values"]["baro_kpa"].pop("source"))


INVALID_TYPICAL_FILES: list[tuple[str, str, str]] = [
    ("unsourced value", UNSOURCED, "values.baro_kpa.source"),
    (
        "empty source",
        _mutated(lambda d: d["values"]["baro_kpa"].update(source="")),
        "values.baro_kpa.source",
    ),
    (
        "blank source",
        _mutated(lambda d: d["values"]["baro_kpa"].update(source="   ")),
        "values.baro_kpa.source: Value error, is blank",
    ),
    (
        "typo in a field name",
        _mutated(lambda d: d["values"].update(bar_kpa=d["values"].pop("baro_kpa"))),
        "'bar_kpa' is not a SimRequest field",
    ),
    (
        "an experiment field",
        _mutated(
            lambda d: d["values"].update(draws={"value": 200, "kind": "typical", "source": "x"})
        ),
        "draws is never filled",
    ),
    (
        "the grid",
        _mutated(lambda d: d["left_empty"].update(grid={"reason": "x"})),
        "grid is never filled",
    ),
    (
        "pedal flag past a byte",
        _mutated(lambda d: d["values"]["pedal_flag"].update(value=256)),
        "pedal_flag: 256",
    ),
    (
        "pedal flag not whole",
        _mutated(lambda d: d["values"]["pedal_flag"].update(value=1.5)),
        "pedal_flag: 1.5",
    ),
    (
        "pedal flag as a float",
        _mutated(lambda d: d["values"]["pedal_flag"].update(value=0.0)),
        "pedal_flag: 0.0",
    ),
    (
        "a quoted number",
        _mutated(lambda d: d["values"]["baro_kpa"].update(value="81")),
        "values.baro_kpa.value",
    ),
    (
        "a boolean",
        _mutated(lambda d: d["values"]["baro_kpa"].update(value=True)),
        "values.baro_kpa.value",
    ),
    (
        "not a number",
        _mutated(lambda d: d["values"]["baro_kpa"].update(value=float("nan"))),
        "baro_kpa: nan",
    ),
    (
        "working assumption for a field that is not the rail",
        _mutated(lambda d: d["values"]["baro_kpa"].update(value="working_assumption")),
        "baro_kpa: 'working_assumption'",
    ),
    (
        "rail that is neither",
        _mutated(lambda d: d["values"]["rail_kpa"].update(value="stated")),
        "values.rail_kpa.value",
    ),
    (
        "a fillable field neither declared nor reasoned away",
        _mutated(lambda d: d["values"].pop("idle_air_learned")),
        "idle_air_learned is neither a sourced value nor listed under left_empty",
    ),
    (
        "declared and left empty",
        _mutated(lambda d: d["left_empty"].update(baro_kpa={"reason": "x"})),
        "baro_kpa is declared both",
    ),
    (
        "an omission with no reason",
        _mutated(
            lambda d: (
                d["values"].pop("idle_air_learned"),
                d["left_empty"].update(idle_air_learned={"reason": ""}),
            )
        ),
        "left_empty.idle_air_learned.reason",
    ),
    (
        "a value with no declared kind",
        _mutated(lambda d: d["values"]["baro_kpa"].pop("kind")),
        "values.baro_kpa.kind",
    ),
    (
        "a kind that is neither typical nor placeholder",
        _mutated(lambda d: d["values"]["baro_kpa"].update(kind="measured")),
        "values.baro_kpa.kind",
    ),
    (
        "a placeholder whose source does not say so",
        _mutated(lambda d: d["values"]["idle_air_learned"].update(source="E07.6 states 0.0")),
        "a placeholder's source must say it is a placeholder",
    ),
    (
        "an unknown key on a value",
        _mutated(lambda d: d["values"]["baro_kpa"].update(note="x")),
        "values.baro_kpa.note",
    ),
    (
        "another schema version",
        _mutated(lambda d: d.update(schema_version=2)),
        "schema_version",
    ),
    ("an empty file", "", "valid dictionary"),
    ("not YAML", "values: [unclosed", "sim_typical_values.yaml"),
]


@pytest.mark.parametrize(
    ("text", "expect"),
    [(text, expect) for _, text, expect in INVALID_TYPICAL_FILES],
    ids=[label for label, _, _ in INVALID_TYPICAL_FILES],
)
def test_an_invalid_typical_values_file_is_refused_by_name(
    tmp_path: Path, text: str, expect: str
) -> None:
    refused = _refusal(_profile_with(tmp_path, text))
    assert refused.reason == "typical_values_invalid"
    assert expect in refused.message, refused.message
    pickle.loads(pickle.dumps(refused))


TYPICAL_FIXTURE = (
    REPO_ROOT / "src" / "dashboard" / "static" / "src" / "test" / "romTypicalValues.fixture.json"
)


def test_the_front_end_fixture_is_exactly_what_the_daemon_serves() -> None:
    """The page's vitest and e2e tests fill their form from the real declared
    values, not a hand-written copy: this file is ``typical_values(PROFILE)``.
    Edit ``sim_typical_values.yaml`` and this fails until the fixture follows;
    ``CANARY_UPDATE_TYPICAL_FIXTURE=1 pytest tests/test_rom_editor.py -k fixture``
    rewrites it (then ``pnpm exec prettier --write
    src/test/romTypicalValues.fixture.json`` in ``src/dashboard/static``)."""
    real = rom_editor.typical_values(PROFILE)
    if os.environ.get("CANARY_UPDATE_TYPICAL_FIXTURE") == "1":
        TYPICAL_FIXTURE.write_text(json.dumps(real, indent=2) + "\n")
    assert json.loads(TYPICAL_FIXTURE.read_text()) == real


# -- a placeholder must stay unread (W09.11) --------------------------------------------------


def _mentions(src_root: Path, names: frozenset[str], base: Path) -> Counter[tuple[str, str, str]]:
    """Every mention of one of ``names`` in the Python under ``src_root``, as
    ``(path relative to base, kind, name)``: an attribute (``x.name``), a string
    (a dict key, ``.get("name")``, ``getattr(o, "name")``), a keyword argument, a
    bare name, or an import. A class's own field declaration (``name: float``) is a
    definition and is not counted. The dashboard's TypeScript is not Python and
    ``static`` is not walked."""
    found: Counter[tuple[str, str, str]] = Counter()
    for dirpath, dirnames, files in os.walk(src_root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in {"node_modules", "static", "__pycache__"}
        )
        for file in sorted(files):
            if not file.endswith(".py"):
                continue
            path = Path(dirpath) / file
            text = path.read_text()
            if not any(name in text for name in names):
                continue  # only a file that spells a name can mention it
            tree = ast.parse(text, filename=str(path))
            declarations = {
                id(stmt.target)
                for cls in ast.walk(tree)
                if isinstance(cls, ast.ClassDef)
                for stmt in cls.body
                if isinstance(stmt, ast.AnnAssign)
            }
            relative = path.relative_to(base).as_posix()
            for node in ast.walk(tree):
                kind = hit = None
                if isinstance(node, ast.Attribute) and node.attr in names:
                    kind, hit = "attribute", node.attr
                elif (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value in names
                ):
                    kind, hit = "string", node.value
                elif isinstance(node, ast.keyword) and node.arg in names:
                    kind, hit = "keyword", node.arg
                elif (
                    isinstance(node, ast.Name) and node.id in names and id(node) not in declarations
                ):
                    kind, hit = "name", node.id
                elif isinstance(node, ast.alias) and (node.name in names or node.asname in names):
                    kind, hit = "import", node.asname or node.name
                if kind is not None and hit is not None:
                    found[(relative, kind, hit)] += 1
    return found


#: The mentions of the two placeholder fields that are plumbing, not nodes. Exact
#: counts: a new mention in an allowed file fails as surely as one in a new file,
#: and an allowed mention that is gone fails too, so this list cannot go stale.
_PLUMBING = {
    # _LEARNED_FIELDS: the contract's boundary quantity -> the LearnedState field
    # (key and value), the only way these names reach a scenario's boundaries.
    ("src/analysis/paired_scenarios.py", "string"): 2,
    # SimRequest -> LearnedState(<name>=request.<name>) in rom_editor._template.
    ("src/analysis/rom_editor.py", "attribute"): 1,
    ("src/analysis/rom_editor.py", "keyword"): 1,
    # rom_editor.TYPICAL_FIELDS: the fields the typical-values button may fill.
    ("src/analysis/rom_editor.py", "string"): 1,
}
PLACEHOLDER_ALLOWED: dict[tuple[str, str, str], int] = {
    (path, kind, name): count
    for name in ("throttle_closed_learned", "idle_air_learned")
    for (path, kind), count in _PLUMBING.items()
}


def _placeholder_drift(
    src_root: Path, base: Path, placeholders: set[str]
) -> tuple[Counter[tuple[str, str, str]], Counter[tuple[str, str, str]]]:
    """(mentions the allowlist does not cover, allowed mentions that are gone)."""
    found = _mentions(src_root, frozenset(placeholders), base)
    allowed = Counter(
        {key: count for key, count in PLACEHOLDER_ALLOWED.items() if key[2] in placeholders}
    )
    return found - allowed, allowed - found


def _placeholders() -> set[str]:
    values = rom_editor.typical_values(PROFILE)["values"]
    return {name for name, declared in values.items() if declared["kind"] == "placeholder"}


def test_the_placeholders_are_the_two_learned_fields_no_code_reads() -> None:
    """``throttle_closed_learned`` and ``idle_air_learned`` are filled with 0.0 as
    PLACEHOLDERS. That is only honest while nothing computes from them. This lists
    every mention in ``src/`` against the plumbing allowlist and fails the moment a
    node starts to read one, rather than leaving a zero to pass as a measurement."""
    placeholders = _placeholders()
    assert {"throttle_closed_learned", "idle_air_learned"} <= placeholders
    stale = {name for _, _, name in PLACEHOLDER_ALLOWED} - placeholders
    assert not stale, (
        f"the allowlist names {sorted(stale)}, which are no longer placeholders: delete those "
        "entries"
    )
    new, gone = _placeholder_drift(REPO_ROOT / "src", REPO_ROOT, placeholders)
    assert not new, (
        "a node now reads a placeholder: replace the typical value with a sourced one or "
        f"require the operator to state it. New mentions (path, kind, name): {dict(new)}"
    )
    assert not gone, (
        "the placeholder allowlist is stale, an allowed mention is gone: update _PLUMBING. "
        f"Missing: {dict(gone)}"
    )


NODE_READING_PLACEHOLDERS = """\
class Declared:
    idle_air_learned: float  # a declaration: not a read

    def __init__(self, idle_air_learned: float = 0.0) -> None:
        \"\"\"idle_air_learned in a docstring is prose, not a read.\"\"\"
        # idle_air_learned in a comment is not a read either.


def node(learned, boundaries, other):
    a = learned.idle_air_learned
    b = boundaries["idle_air_learned"]
    c = boundaries.get("idle_air_learned")
    d = getattr(other, "idle_air_learned")
    e = idle_air_learned
    return dict(idle_air_learned=a), b, c, d, e
"""


def test_the_scan_sees_every_way_of_reading_a_field_and_not_a_declaration(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "node.py").write_text(NODE_READING_PLACEHOLDERS)
    (tmp_path / "src" / "static").mkdir()  # not Python's: never walked
    (tmp_path / "src" / "static" / "ignored.py").write_text(NODE_READING_PLACEHOLDERS)
    found = _mentions(tmp_path / "src", frozenset({"idle_air_learned"}), tmp_path)
    assert found == Counter(
        {
            ("src/node.py", "attribute", "idle_air_learned"): 1,
            ("src/node.py", "string", "idle_air_learned"): 3,
            ("src/node.py", "name", "idle_air_learned"): 1,
            ("src/node.py", "keyword", "idle_air_learned"): 1,
        }
    )


def test_the_guard_bites_on_a_node_that_reads_a_placeholder(tmp_path: Path) -> None:
    """On scratch copies of the two files that carry the plumbing: as shipped they
    match the allowlist exactly, and a read added to a new file, or to an
    allowlisted one, is reported."""
    placeholders = {"throttle_closed_learned", "idle_air_learned"}
    analysis = tmp_path / "src" / "analysis"
    analysis.mkdir(parents=True)
    for name in ("paired_scenarios.py", "rom_editor.py"):
        shutil.copy(REPO_ROOT / "src" / "analysis" / name, analysis / name)
    new, gone = _placeholder_drift(tmp_path / "src", tmp_path, placeholders)
    assert not new and not gone

    (analysis / "twin_node.py").write_text(
        "def fill_byte(boundaries):\n    return boundaries['idle_air_learned'].value\n"
    )
    new, gone = _placeholder_drift(tmp_path / "src", tmp_path, placeholders)
    assert dict(new) == {("src/analysis/twin_node.py", "string", "idle_air_learned"): 1}
    assert not gone

    # In an allowlisted file too: the count is exact, not "this file may mention it".
    with (analysis / "rom_editor.py").open("a") as handle:
        handle.write("\n_probe = {}['throttle_closed_learned']\n")
    new, _ = _placeholder_drift(tmp_path / "src", tmp_path, placeholders)
    assert new[("src/analysis/rom_editor.py", "string", "throttle_closed_learned")] == 1

    # And an allowed mention that disappears is a stale list.
    (analysis / "paired_scenarios.py").write_text("")
    _, gone = _placeholder_drift(tmp_path / "src", tmp_path, placeholders)
    assert gone[("src/analysis/paired_scenarios.py", "string", "idle_air_learned")] == 2


def test_the_kinds_the_loader_accepts_are_the_two_the_page_draws() -> None:
    import typing

    assert (
        typing.get_args(rom_editor.TypicalKind)
        == rom_editor.TYPICAL_KINDS
        == (
            "typical",
            "placeholder",
        )
    )
