"""E09.14b: the ROM Editor's preview and ``tools/rom_sim.py inspect`` say the
same thing for the same edit.

``rom_levers.classify_edit`` words every lever and blocker once. Two front ends
show it: ``inspect`` (the laptop's command line) and ``rom_editor.preview_edits``
(the page's route). Neither may word its own, or the operator reads two
answers to one question. This file is the one test that calls both for the same
edit on the same image and compares the strings, plus the checks that make a
side that rewrites a string fail (a test of the test), and one that no
front-end source carries a classifier string of its own.

The edit goes the way an operator takes it: the page previews it, and the CLI
is given the spec the page made. The synthetic image and the
Xterra profile copy (its application model describing that image, its flash
history recording it) are the ones ``tests/test_rom_editor_api.py`` builds, so
no real ``.OsirisROM`` is needed; the real-image Fuel Comp case is in
``tests/test_rom_editor_private_rom.py``.

The ROM Editor page's vitest and e2e fixtures carry real classifier payloads
(``src/dashboard/static/src/test/romLevers.fixture.json``). They cannot be
hand-written: :func:`test_the_front_end_fixture_has_the_shape_of_the_real_payloads`
compares their shape to what the code produces, and
``CANARY_UPDATE_LEVERS_FIXTURE=1 pytest tests/test_rom_levers_parity.py``
regenerates them (then ``pnpm exec prettier --write src/test/romLevers.fixture.json``
in ``src/dashboard/static``: ``pnpm format:check`` covers the file).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.analysis import rom_editor, rom_levers
from src.analysis.ecu_application import program_sha256
from src.analysis.fuel_plant import plant_qualification
from src.analysis.rom_image import revision_match, sha256_hex
from src.analysis.rom_levers import Blocker, LeverClassification, LeverEntry, OutputBlockers
from src.analysis.sim_montecarlo import load_sim_kit
from src.vehicle.tune_revisions import load_tune_revisions
from tests.rom_fixtures import PROFILE
from tests.test_rom_editor import FC_EDIT, K_EDIT
from tests.test_sim_montecarlo import build_kit_and_image
from tools import rom_sim

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "src" / "dashboard" / "static"
FIXTURE = STATIC / "src" / "test" / "romLevers.fixture.json"
REVISION = "synthetic-w09"
REGISTRY = """\
schema_version: 1
revisions:
  - revision: {revision}
    calibration_id: 1ZS44A00000
    flashed_at: 2026-10-01T00:00:00Z
    image_sha256: {sha}
"""

#: A Fuel Targets edit the page can make: the two cells E08.1's own tests use.
FT_EDIT: dict[str, Any] = {
    "table": "fuel_targets",
    "slot": 1,
    "cells": [[7, 7], [7, 6]],
    "set_raw": 151,
}
#: What the page can send, by name. The timing trims became editable with
#: E09.7 (their preview); the MAF curve cannot be edited through the page.
TRIM_EDIT: dict[str, Any] = {"table": "timing_trim_a", "slot": 1, "cells": [[0, 0]], "add_raw": -1}
PAGE_EDITS: dict[str, list[dict[str, Any]]] = {
    "fuel_comp": [FC_EDIT],
    "k": [K_EDIT],
    "fuel_targets": [FT_EDIT],
    "fuel_comp_and_k": [FC_EDIT, K_EDIT],
}
LABEL = "levers parity"


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    kit, image = build_kit_and_image()
    root = tmp_path_factory.mktemp("levers_parity")
    profile = root / "xterra"
    shutil.copytree(PROFILE, profile, ignore=shutil.ignore_patterns("_candidates", "_prior"))
    real = (PROFILE / "ecu_application_model.yaml").read_text()
    real_sha = next(
        line.split(":", 1)[1].strip()
        for line in real.splitlines()
        if line.strip().startswith("sha256:")
    )
    old = kit.model.program.identity.sha256
    assert program_sha256(image, kit.model) == old and old != real_sha
    (profile / "ecu_application_model.yaml").write_text(real.replace(real_sha, old, 1))
    sha = sha256_hex(image)
    (profile / "tune_revisions.yaml").write_text(REGISTRY.format(revision=REVISION, sha=sha))
    registry = load_tune_revisions(profile / "tune_revisions.yaml")
    assert registry is not None
    found = revision_match(sha, registry)
    assert found["status"] == "matched" and found["revision"] == REVISION
    (root / "base.bin").write_bytes(image)
    return {
        "root": root,
        "profile": profile,
        "image": image,
        "sha": sha,
        "matched": found,
        "kit": kit,
    }


def _preview(world: dict[str, Any], edits: list[dict[str, Any]]) -> dict[str, Any]:
    """The page's preview: no kit seam, the profile's own, as the route runs it."""
    return rom_editor.preview_edits(
        world["image"], world["matched"], LABEL, edits, world["profile"], sha256=world["sha"]
    )


def _inspect(
    world: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    *selector: str | Path,
) -> dict[str, Any]:
    capsys.readouterr()
    code = rom_sim.main(
        [
            "inspect",
            "--profile",
            str(world["profile"]),
            *map(str, selector),
            "--rom",
            str(world["root"] / "base.bin"),
            "--sha256",
            world["sha"],
        ]
    )
    out, err = capsys.readouterr()
    assert code == 0, out + err
    payload: dict[str, Any] = json.loads(out)
    return payload


def _cli_for(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str], preview: dict[str, Any], name: str
) -> dict[str, Any]:
    """``inspect`` on the spec the page previewed. The page's spec names the
    base by revision, which the CLI resolves only from real ROM directories
    (a synthetic revision has none), so the base is named by file and hash:
    the same edits on the same bytes, which is all the classification reads."""
    spec = world["root"] / f"{name}.edit-spec.yaml"
    body = {**preview["spec"], "base": {"file": "base.bin", "sha256": world["sha"]}}
    spec.write_text(yaml.safe_dump(body, sort_keys=False))
    return _inspect(world, capsys, "--edits", spec)


# -- the same edit, the same strings ---------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PAGE_EDITS))
def test_the_page_and_the_cli_show_the_same_strings_for_the_same_edit(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str], name: str
) -> None:
    page = _preview(world, PAGE_EDITS[name])
    cli = _cli_for(world, capsys, page, name)
    # The candidate is the same bytes on both sides (so the comparison is of one edit).
    assert page["candidate_sha256"] == cli["candidate_sha256"]
    assert page["levers"]["messages"], "the classifier always says something"
    assert page["levers"]["messages"] == cli["levers"]["messages"]
    # And the whole structured answer, not only the text the page shows.
    assert page["levers"] == cli["levers"]
    assert page["levers"]["schema_version"] == 1 and page["levers"]["regime"] is None


def test_a_fuel_comp_edit_says_what_a_number_can_and_cannot_be_given_for(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """E09.14's Accept: the ECU outputs get a number (with the caveat on what the
    points must state); the plant's outputs are blocked by the plant and by
    the closed loop. The same lines reach the CLI."""
    page = _preview(world, PAGE_EDITS["fuel_comp"])
    messages = page["levers"]["messages"]
    said = {
        m.split(" -> ", 1)[1].split(": ", 1)[0]: m.split(": ", 1)[1]
        for m in messages
        if " -> " in m
    }
    # The two ECU outputs the run computes: BFS and the Target AFR word.
    for output in ("base_fuel_schedule_word", "target_equiv_ratio"):
        assert said[output].startswith("a number can be given (for points that state only"), said
    # The plant's four: unqualified, and unable to say what the closed loop does.
    plant_outputs = (
        "injector_pulse_ms",
        "fuel_trim_total_pct",
        "delivered_lambda",
        "fuel_per_event_g",
    )
    for output in plant_outputs:
        assert said[output].startswith("plant not qualified: "), said
        assert said[output].endswith("; closed loop: trim authority undecoded"), said
    assert messages == _cli_for(world, capsys, page, "fc_accept")["levers"]["messages"]


def test_a_timing_trim_and_the_maf_curve_are_the_clis_alone(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The MAF curve (0xE27E, in no table until E09.8) reaches the classifier
    only through ``inspect``. A timing trim is editable on the page since
    E09.7, and its preview says what ``inspect`` says, word for word; what
    each says is pinned here and in ``tests/test_rom_levers.py``."""
    trim = _trim_levers(world, capsys)["messages"]
    assert _preview(world, [TRIM_EDIT])["levers"]["messages"] == trim
    assert any(
        "Timing trim A -> ignition_trim_selected: engine torque not computed in "
        "conditional_rom_comparison; torque production built, not validated" in m
        for m in trim
    ), trim
    maf = _maf_levers(world, capsys)["messages"]
    assert any("0xE27E" in m and "not mapped" in m for m in maf), maf


def _trim_levers(world: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    spec = world["root"] / "trim.edit-spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "base": {"file": "base.bin", "sha256": world["sha"]},
                "label": "trim",
                "edits": [TRIM_EDIT],
            }
        )
    )
    timing = ("--high-det", "clear", "--knock-control", "active", "--timing-coolant-c", "90")
    levers: dict[str, Any] = _inspect(world, capsys, "--edits", spec, *timing)["levers"]
    return levers


def _maf_levers(world: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    image = bytearray(world["image"])
    image[0xE27E] ^= 0x01
    saved = world["root"] / "maf.bin"
    saved.write_bytes(bytes(image))
    levers: dict[str, Any] = _inspect(world, capsys, "--candidate-rom", saved)["levers"]
    return levers


# -- a side that words its own strings fails ---------------------------------------------------


def _reword(real: Any) -> Any:
    """``classify_edit`` with every message reworded: what a front end that
    wrote its own text, or trimmed the classifier's, would show."""

    def classify(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        return result.model_copy(
            update={"messages": tuple(m.replace("a number", "the number") for m in result.messages)}
        )

    return classify


@pytest.mark.parametrize("side", ["page", "cli"])
def test_a_side_that_rewords_a_string_fails_the_comparison(
    world: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    side: str,
) -> None:
    """The comparison above has teeth: reword one side and it is not equal. (The
    other hazard, both sides reworded the same way, is one function's edit and
    is pinned by ``tests/test_rom_levers.py``.)"""
    page = _preview(world, PAGE_EDITS["fuel_comp"])
    cli = _cli_for(world, capsys, page, "reword")
    assert page["levers"]["messages"] == cli["levers"]["messages"]
    target = rom_editor if side == "page" else rom_sim
    monkeypatch.setattr(target, "classify_edit", _reword(target.classify_edit))
    page2 = _preview(world, PAGE_EDITS["fuel_comp"])
    cli2 = _cli_for(world, capsys, page2, "reword")
    assert any("a number can be given" in m for m in cli["levers"]["messages"])
    assert page2["levers"]["messages"] != cli2["levers"]["messages"]


def test_a_side_that_drops_a_message_fails_the_comparison(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = rom_levers.classify_edit

    def dropped(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        return result.model_copy(update={"messages": result.messages[:-1]})

    monkeypatch.setattr(rom_editor, "classify_edit", dropped)
    page = _preview(world, PAGE_EDITS["k"])
    cli = _cli_for(world, capsys, page, "dropped")
    assert page["levers"]["messages"] != cli["levers"]["messages"]


def test_the_page_classifies_with_the_simulators_plant_not_the_contracts(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sim_kit.yaml`` declares the plant the simulator runs; the contract's
    default plant is another version. A preview qualified on the wrong one says
    a different thing about "plant not qualified" than ``inspect`` does."""
    page = _preview(world, PAGE_EDITS["fuel_comp"])
    cli = _cli_for(world, capsys, page, "plant")
    assert page["levers"] == cli["levers"]
    contract_plant = world["kit"].plant
    declared = load_sim_kit(world["profile"]).kit.plant
    other = plant_qualification(contract_plant)
    if (contract_plant.plant_version, other) == (
        declared.plant_version,
        plant_qualification(declared),
    ):
        pytest.skip("the contract's default plant is the declared one: nothing to tell apart")
    monkeypatch.setattr(rom_editor, "plant_qualification", lambda _plant: other)
    wrong = _preview(world, PAGE_EDITS["fuel_comp"])
    assert wrong["levers"]["messages"] != cli["levers"]["messages"]


# -- no front end words a classifier string ----------------------------------------------------


def _front_end_sources() -> list[Path]:
    page = [
        path
        for path in (STATIC / "src").rglob("*")
        if path.suffix in (".ts", ".tsx")
        and not re.search(r"(\.test\.|/test/)", path.as_posix())
        and re.search(r"rom(editor|sim)", path.as_posix().lower())
    ]
    return [REPO / "src" / "analysis" / "rom_editor.py", *page]


def test_no_front_end_source_carries_a_classifier_string_of_its_own() -> None:
    """The page and the preview route show ``messages`` and nothing else of
    the classifier's wording: one of its phrases in their source is a
    hand-written copy that would outlive the next change to the classifier."""
    phrases = (
        rom_levers.TEXT_NOT_MAPPED,
        rom_levers.TEXT_NO_TORQUE_PLANT,
        rom_levers.TEXT_CLOSED_LOOP,
        "a number can be given",
        "plant not qualified",
    )
    sources = _front_end_sources()
    assert len(sources) > 5, sources
    found = {
        f"{path.relative_to(REPO)}: {phrase}"
        for path in sources
        for phrase in phrases
        if phrase in path.read_text()
    }
    assert not found, sorted(found)


# -- the page's fixtures are real ---------------------------------------------------------------


def _real_payloads(world: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """The three accept cases' ``levers``: a Fuel Comp edit as the page previews
    it, a timing-trim edit and a MAF-curve byte as ``inspect`` classifies them."""
    return {
        "fuel_comp": _preview(world, PAGE_EDITS["fuel_comp"])["levers"],
        "timing_trim": _trim_levers(world, capsys),
        "maf_curve": _maf_levers(world, capsys),
    }


def _assert_the_shape_of_the_type(levers: dict[str, Any]) -> None:
    """The keys at each level are the pydantic models' fields (plus the three
    values ``to_dict`` adds to an output): the shape ``types/romEditor.ts``
    mirrors, independent of which edit or which manifest made the payload."""
    assert set(levers) == set(LeverClassification.model_fields)
    for entry in levers["levers"]:
        assert set(entry) == set(LeverEntry.model_fields)
        assert all(set(b) == set(Blocker.model_fields) for b in entry["blockers"])
    for output in levers["outputs"]:
        assert set(output) == set(OutputBlockers.model_fields) | {
            "reached",
            "number",
            "design_dependent",
        }
        assert all(set(b) == set(Blocker.model_fields) for b in output["blockers"])


def test_the_front_end_fixture_is_real_classifier_output(
    world: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The vitest and e2e fixtures are copies of real ``levers`` payloads:
    the page's Fuel Comp preview, and ``inspect`` on a timing trim and on the
    MAF curve. Their keys are checked against the classifier's types, so a
    renamed or added field fails here; regenerate with
    ``CANARY_UPDATE_LEVERS_FIXTURE=1`` and prettier (see the module). The
    values are what the code said when they were made and are not compared: a
    new manifest changes the strings, and the page renders whatever strings it
    is given."""
    real = _real_payloads(world, capsys)
    for levers in real.values():
        _assert_the_shape_of_the_type(levers)
    if os.environ.get("CANARY_UPDATE_LEVERS_FIXTURE") == "1":
        FIXTURE.write_text(json.dumps(real, indent=2) + "\n")
    fixture = json.loads(FIXTURE.read_text())
    assert fixture.keys() == real.keys()
    for levers in fixture.values():
        _assert_the_shape_of_the_type(levers)
        assert levers["messages"]
    # The three cases say what E09.14 requires of them.
    said = {name: "\n".join(levers["messages"]) for name, levers in fixture.items()}
    assert "torque production built, not validated" in said["timing_trim"]
    assert "spark response unmeasured" in said["timing_trim"]
    assert "not mapped" in said["maf_curve"]
    assert "closed loop: trim authority undecoded" in said["fuel_comp"]
    assert "plant not qualified" in said["fuel_comp"]
