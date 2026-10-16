"""W09.1: the ROM Editor's preview and simulate as pure functions.

Everything runs on E07.6's synthetic image (``tests.test_sim_montecarlo``'s
kit: the Xterra's layout, flat Fuel Comp 128, K 28,715), with the registry
entry that makes it a recorded revision stated here, never read from
``tune_revisions.yaml``. No real ``.OsirisROM`` is needed (W09.6 runs those).
"""

from __future__ import annotations

import json
import pickle
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from src.analysis import rom_editor, sim_montecarlo
from src.analysis.ecu_application import ApplicationModelRefusalError
from src.analysis.fuel_plant import FuelPlantRefusalError
from src.analysis.paired_scenarios import OUTPUTS, SimKit
from src.analysis.paired_steady import TRIM_AUTHORITY_UNDECODED
from src.analysis.rom_edit_spec import build_candidate, load_edit_spec
from src.analysis.rom_effects import RomEffectRefusalError
from src.analysis.rom_image import ImageFormatError, revision_match, sha256_hex
from src.analysis.sim_contract import SimulationRefusalError
from src.analysis.sim_montecarlo import MonteCarloResult
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
