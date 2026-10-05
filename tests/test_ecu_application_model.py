"""The ECM application model and coverage manifest (E05.1).

The profile's ``ecu_application_model.yaml`` traces the target-phi output
back through the firmware. These tests hold its contracts: the graph resolves
to the twin's observed inputs and W08's tables, every unresolved dependency
blocks the output, identity is the program plus the full image (never the
calibration ID), and the read model reports what is covered and what is not.

No private ROM: images here are synthetic. The check of the real images is
the last test and skips by name without them.
"""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.analysis.ecu_application import (
    ApplicationModel,
    ApplicationModelRefusalError,
    applicability,
    check_image,
    check_image_sha,
    check_observations,
    coverage_report,
    load_application_model,
    observed_specs,
    program_sha256,
)
from src.analysis.rom_map import load_rom_map
from src.analysis.target_phi import load_twin_definition
from src.config import CanaryConfig
from src.dashboard.routes import rom_maps
from src.decode.dids import load_did_map
from src.paths import DIDS_YAML

REPO = Path(__file__).resolve().parents[1]
PROFILE = REPO / "data" / "nissan-xterra-2008"
OUTPUT = "target_equiv_ratio"


def _raw() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((PROFILE / "ecu_application_model.yaml").read_text())
    return loaded


def _model() -> tuple[ApplicationModel, str]:
    return load_application_model(PROFILE)


def _node(raw: dict[str, Any], node_id: str) -> dict[str, Any]:
    found: dict[str, Any] = next(n for n in raw["nodes"] if n["id"] == node_id)
    return found


# -- the target-phi graph -----------------------------------------------------------


def test_target_phi_is_conditional_on_the_final_command_path_and_the_ram_copy() -> None:
    model, sha = _model()
    applied = applicability(model, sha, OUTPUT)
    assert applied.blockers == ("final_command_path_unverified", "ram_copy_unverified")
    assert applied.provenance()["sha256"] == sha
    output = model.output(OUTPUT)
    blocked = {n for b in output.blockers for n in b.nodes}
    # The lookup and gate are implemented, the RAM table assumed; everything
    # from the gated lookup to the final word is traced but not modelled.
    assert {"target_afr_lookup", "enrichment_gate"}.isdisjoint(blocked)
    assert {
        "enrichment_multiplier",
        "correction_sum",
        "curve_term",
        "final_command_word",
    } <= blocked


def test_the_graph_resolves_to_the_twins_inputs_and_w08_tables() -> None:
    model, _ = _model()
    definition, _ = load_twin_definition(PROFILE)
    output = model.output(OUTPUT)
    assert output.twin is not None and output.twin.definition == "target_phi_twin.yaml"
    assert definition.application_output == OUTPUT
    observed = observed_specs(model, output.twin.predicts)
    assert set(observed) == {
        *definition.inputs.channels().values(),
        *definition.map_identity.channels(),
    }
    # Every ROM table a node cites is one W08's map defines.
    rom_ids = {t.id for t in load_rom_map(PROFILE).tables}
    cited = {r.rom_map for n in model.nodes for r in n.rom if r.rom_map is not None}
    assert cited and cited <= rom_ids
    # Every memory observation reads the RAM the model says it does.
    memory = [(c.name, c.address) for c in load_did_map(DIDS_YAML).memory]
    assert check_observations(model, memory) == []


def test_an_observation_at_the_wrong_address_is_named() -> None:
    model, _ = _model()
    memory = [(c.name, c.address) for c in load_did_map(DIDS_YAML).memory]
    moved = [(n, a + 2 if n == "ECM_TARGET_EQUIV_RATIO" else a) for n, a in memory]
    assert check_observations(model, moved) == [
        "mem:ECM_TARGET_EQUIV_RATIO: catalog 0xFFFF26C0, model 0xFFFF26BE"
    ]


def test_every_unresolved_node_on_the_path_is_under_exactly_one_blocker() -> None:
    model, _ = _model()
    output = model.output(OUTPUT)
    path = model.path(output.written_by)
    unresolved = {n.id for n in path if n.twin in ("assumed", "not_modelled")}
    named = [n for b in output.blockers for n in b.nodes]
    assert sorted(named) == sorted(unresolved)
    # An observed input ends its branch: the pedal flag's own unidentified
    # inputs are not needed to use the measured flag.
    assert "pedal_enrichment_flag" in {n.id for n in path}
    assert model.node("pedal_enrichment_flag").twin == "observed"


def test_every_address_a_node_describes_is_one_it_reads_writes_or_observes() -> None:
    """The arithmetic and branch text is the behavioural contract; an address
    it cites but the node's reads/writes omit is an operand the graph does not
    know about (and whose width/signedness ``ram`` never declared)."""
    model, _ = _model()
    pattern = re.compile(r"0xFFFF[0-9A-Fa-f]{4}\b")
    missing = {}
    for node in model.nodes:
        text = " ".join([node.arithmetic, *(f"{b.when} {b.then}" for b in node.branches)])
        cited = {int(m, 16) for m in pattern.findall(text)}
        known = {*node.reads, *node.writes, *(o.address for o in node.observed_as)}
        if cited - known:
            missing[node.id] = sorted(f"0x{a:08X}" for a in cited - known)
    assert missing == {}


def test_the_listed_images_are_the_registrys() -> None:
    """Only images a session can run under: tune_revisions.yaml revisions and
    reference images, by both hash and name."""
    model, _ = _model()
    raw = yaml.safe_load((PROFILE / "tune_revisions.yaml").read_text())
    registry = {
        **{r["image_sha256"]: r["revision"] for r in raw["revisions"] if r.get("image_sha256")},
        **{r["image_sha256"]: r["name"] for r in raw.get("reference_images", [])},
    }
    for image in model.program.images:
        assert registry.get(image.sha256) == image.revision, image.revision


def test_the_schedule_orders_the_multiplier_before_the_readers_of_it() -> None:
    """Update order is data: the corrections and the curve term read this
    cycle's multiplier because 0x055DB2 runs first in the task."""
    model, _ = _model()
    order = model.schedule.order()
    assert order.index("enrichment_multiplier") < order.index("curve_term")
    assert order.index("correction_sum") < order.index("final_command_word")
    assert order[-1] == "final_command_word"


# -- refusals in the schema -------------------------------------------------------------


def _refuses(raw: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ApplicationModel.model_validate(raw)


def test_an_unresolved_dependency_cannot_be_dropped_silently() -> None:
    raw = _raw()
    blocker = raw["outputs"][0]["blockers"][0]
    blocker["nodes"] = [n for n in blocker["nodes"] if n != "curve_term"]
    _refuses(raw, "curve_term.*block nothing")


def test_a_blocker_cannot_outlive_its_cause() -> None:
    """Marking the RAM copy observed (as E02.2's read would) without removing
    ram_copy_unverified is refused, not carried."""
    raw = _raw()
    node = _node(raw, "target_afr_ram_copy")
    node["twin"] = "observed"
    del node["assumption"]
    node["observed_as"] = [
        {"spec": "mem:ECM_TARGET_AFR_RAM", "address": 0xFFFFB260, "relation": "identity"}
    ]
    _refuses(raw, "ram_copy_unverified names target_afr_ram_copy")


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda r: _node(r, "target_afr_lookup").pop("implemented_by"), "implemented_by"),
        (
            lambda r: _node(r, "target_afr_lookup").update(confidence="inferred"),
            "cannot be implemented",
        ),
        (
            lambda r: _node(r, "alternate_multiplier_writer").update(calibration_conditions=[]),
            "kill",
        ),
        (lambda r: _node(r, "correction_sum")["reads"].append(0xFFFF0000), "not declared"),
        (
            lambda r: _node(r, "correction_sum").update(outside_schedule="elsewhere"),
            "either called once",
        ),
        (lambda r: _node(r, "curve_term")["depends_on"].append("nowhere"), "undeclared nodes"),
        (lambda r: r["program"]["identity"].update(regions=[[0x20000, 0x10000]]), "ordered"),
        (lambda r: r["program"].update(images=r["program"]["images"][1:]), "decoded from"),
        (lambda r: r["outputs"][0]["twin"].update(predicts="nowhere"), "not on its path"),
        (lambda r: r["ram"].append(dict(r["ram"][0])), "declared more than once"),
        (
            lambda r: _node(r, "load_input_x")["observed_as"][0].update(address=0xFFFF0002),
            "observations read RAM not declared",
        ),
        (
            lambda r: r["outputs"][0]["observed_as"][0].update(address=0xFFFF0002),
            "observations read RAM not declared",
        ),
    ],
)
def test_the_model_refuses_an_incomplete_or_inconsistent_trace(edit: Any, match: str) -> None:
    raw = copy.deepcopy(_raw())
    edit(raw)
    _refuses(raw, match)


# -- identity: program plus full image, never the calibration ID ------------------------


def _synthetic(model: ApplicationModel) -> tuple[bytes, ApplicationModel]:
    image = bytearray(b"\xa5" * 0x100000)
    for marker in model.program.patch.markers:
        raw = bytes.fromhex(marker.bytes)
        image[marker.offset : marker.offset + len(raw)] = raw
    for node in model.nodes:
        for c in node.calibration_conditions:
            for k in range(c.count):
                image[c.offset + k] = (image[c.offset + k] & ~c.mask & 0xFF) | c.equals
    identity = model.program.identity.model_copy(
        update={"sha256": program_sha256(bytes(image), model)}
    )
    program = model.program.model_copy(update={"identity": identity})
    return bytes(image), model.model_copy(update={"program": program})


def test_the_same_program_passes_and_each_mismatch_refuses_by_name() -> None:
    model, _ = _model()
    image, same = _synthetic(model)
    check_image(image, same)
    cases = {
        # Code changed under an unchanged calibration ID: a different program.
        "wrong_program": (0x030000, 0x00, same),
        "patch_missing": (0x055EC4, 0x00, None),
        "unmodelled_logic": (0x8D51, 0x01, None),
    }
    for reason, (offset, value, pinned) in cases.items():
        edited = bytearray(image)
        edited[offset] = value
        against = pinned if pinned is not None else _synthetic(model)[1]
        if pinned is None:
            # Re-pin to the edited image's program so only the named check fails.
            identity = against.program.identity.model_copy(
                update={"sha256": program_sha256(bytes(edited), against)}
            )
            against = against.model_copy(
                update={"program": against.program.model_copy(update={"identity": identity})}
            )
        with pytest.raises(ApplicationModelRefusalError) as refused:
            check_image(bytes(edited), against)
        assert refused.value.reason == reason, reason


def test_the_calibration_region_does_not_change_the_program() -> None:
    """A reflash rewrites 0x8000-0x1FFFF and the map slots: the program hash
    ignores them, so a new calibration is the same program, identified by its
    own full-image hash."""
    model, _ = _model()
    image, same = _synthetic(model)
    edited = bytearray(image)
    edited[0x8204:0x820F] = b"1ZS99Z00000"  # the calibration ID
    edited[0x0E0220] ^= 0xFF  # a Target AFR cell in slot 1
    assert program_sha256(bytes(edited), same) == program_sha256(image, same)
    assert hashlib.sha256(bytes(edited)).hexdigest() != hashlib.sha256(image).hexdigest()


def test_without_the_bytes_only_a_listed_image_is_accepted() -> None:
    model, _ = _model()
    for known in model.program.images:
        check_image_sha(known.sha256, model)
    with pytest.raises(ApplicationModelRefusalError) as refused:
        check_image_sha("0" * 64, model)
    assert refused.value.reason == "program_unverified"
    with pytest.raises(ApplicationModelRefusalError) as refused:
        model.output("ignition_advance")
    assert refused.value.reason == "output_undeclared"


# -- the read model and its route ----------------------------------------------------------


def test_the_report_names_qualified_outputs_and_uncovered_logic() -> None:
    model, sha = _model()
    running = model.program.images[0].sha256
    report = coverage_report(model, sha, image_sha256=running)
    assert report["model"]["model_version"] == model.model_version
    assert report["image"] == {
        "sha256": running,
        "known": True,
        "revision": "ZS44A-20260920",
        "role": "decoded",
    }
    (target,) = report["outputs"]
    assert (target["id"], target["status"]) == (OUTPUT, "conditional")
    assert [b["name"] for b in target["blockers"]] == list(
        applicability(model, sha, OUTPUT).blockers
    )
    roles = target["by_role"]
    assert {"target_afr_lookup", "enrichment_gate"} <= set(roles["implemented"])
    assert roles["assumed"] == ["target_afr_ram_copy"]
    assert "alternate_multiplier_writer" in roles["calibration_dead"]
    assert {r["status"] for r in target["regions"]} == {"conditional", "unevaluable"}
    assert {u["id"] for u in report["uncovered"]} >= {"multiplier_hold_counter", "slot_0x260_map"}
    assert coverage_report(model, sha, image_sha256="f" * 64)["image"]["known"] is False


def test_the_route_serves_the_report_behind_the_control_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rom_maps, "vehicle_profile_dir", lambda _config: PROFILE)
    monkeypatch.setattr(rom_maps, "active_registry", lambda _config: None)
    app = FastAPI()
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    rom_maps.register_rom_routes(app, config=config, resolved_control_token="secret")
    with TestClient(app) as client:
        assert client.get("/control/ecu/application-model").status_code == 401
        response = client.get(
            "/control/ecu/application-model", headers={"Authorization": "Bearer secret"}
        )
    assert response.status_code == 200
    assert response.json()["outputs"][0]["id"] == OUTPUT
    with pytest.raises(FileNotFoundError):
        rom_maps.application_model_report(tmp_path, revisions=None)


# -- the real images (private, skip by name) -------------------------------------------------


def test_every_listed_local_image_is_this_program() -> None:
    from tests.rom_fixtures import OPERATOR_ROMS, UPREV, operator_image

    model, _ = _model()
    candidates: list[bytes] = []
    ecu_read = Path.home() / "ecu-re" / "roms" / "ZS44A-20260920.ecuread.bin"
    if ecu_read.exists():
        candidates.append(ecu_read.read_bytes())
    for name in OPERATOR_ROMS:
        image = operator_image(name)
        if image is not None:
            candidates.append(image)
    restored = UPREV / "Tuned ROMs" / "ZS44A-Tuned-20261004.OsirisROM"
    if restored.exists():
        from src.analysis.osiris import Container

        candidates.append(Container.parse(restored.read_bytes()).decoded())
    listed = {i.sha256 for i in model.program.images}
    present = [c for c in candidates if hashlib.sha256(c).hexdigest() in listed]
    if not present:
        pytest.skip("no private ZS44A image is on this machine: the program check did not run")
    for image in present:
        check_image(image, model)
