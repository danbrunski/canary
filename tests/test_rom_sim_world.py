"""R30.5: ``tools/rom_sim.py`` loads and compares through the library, once.

The CLI used to carry its own loader, its own ``compare_roms`` call and trace,
and its own ``LEARNED_HELD`` / ``LEARNED_SOURCE`` / ``SLOT_SOURCE`` beside the
ROM Editor's, so a new kit field or ``LearnedState`` field had to be added
twice. These tests hold the lift in place: the tool builds none of the
simulation's objects itself, the two drivers' learned states differ only by
their source, and :class:`~src.analysis.rom_sim_world.SimWorld` refuses by
name. ``tests/test_rom_sim_cli.py`` pins the tool's output end to end.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import shutil
from pathlib import Path
from typing import Any

import pytest

from src.analysis import rom_editor
from src.analysis.rom_edit_spec import BaseRef
from src.analysis.rom_effects import RomEffectRefusalError
from src.analysis.rom_sim_world import SimWorld, load_spec
from src.analysis.sim_contract import SimulationRefusalError
from src.analysis.sim_montecarlo import MODE
from tests.sim_builders import request
from tests.sim_builders import world as world  # noqa: F401 - fixture
from tools import rom_sim

TOOL = Path(rom_sim.__file__)

#: What only the library builds: a learned state, a paired scenario, a
#: comparison, a candidate, the kit, the revision resolver. A call to one of
#: these in the tool is a second copy of what ``rom_editor`` / ``rom_sim_world``
#: already do.
LIBRARY_ONLY = frozenset(
    {
        "LearnedState",
        "PairedScenario",
        "EcuIdentity",
        "scenario_template",
        "compare_roms",
        "compare_images",
        "build_candidate",
        "load_candidate_file",
        "load_edit_spec",
        "resolve_base",
        "load_sim_kit",
        "load_fuel_comp_declaration",
        "load_tune_revisions",
        "RevisionImageResolver",
    }
)
#: Module constants the tool used to declare as its own copies.
SHARED_CONSTANTS = frozenset({"LEARNED_HELD", "LEARNED_SOURCE", "SLOT_SOURCE"})


def _called(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _assigned(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for stmt in tree.body:
        targets = (
            stmt.targets
            if isinstance(stmt, ast.Assign)
            else [stmt.target]
            if isinstance(stmt, ast.AnnAssign)
            else []
        )
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def test_the_tool_builds_none_of_the_simulation_itself() -> None:
    tree = ast.parse(TOOL.read_text())
    assert not _called(tree) & LIBRARY_ONLY, (
        "tools/rom_sim.py builds a simulation object itself: call rom_editor / rom_sim_world, "
        f"the one copy the ROM Editor uses too ({sorted(_called(tree) & LIBRARY_ONLY)})"
    )
    assert not _assigned(tree) & SHARED_CONSTANTS, (
        "tools/rom_sim.py declares its own copy of a constant rom_editor holds: "
        f"{sorted(_assigned(tree) & SHARED_CONSTANTS)}"
    )


def test_the_scan_sees_a_second_copy() -> None:
    """Not vacuous: the checks fire on the shapes the tool used to have."""
    bad = ast.parse(
        "LEARNED_HELD = {}\n"
        "def f(kit):\n"
        "    return compare_roms(1, 2), LearnedState(stft_pct=0.0), x.build_candidate()\n"
    )
    assert _called(bad) & LIBRARY_ONLY == {"compare_roms", "LearnedState", "build_candidate"}
    assert _assigned(bad) & SHARED_CONSTANTS == {"LEARNED_HELD"}


def test_the_two_drivers_learned_states_differ_only_by_source() -> None:
    """The CLI's trims plus ``LEARNED_HELD`` and an editor request stating the
    same five values give the same state, under each driver's own source."""
    body: dict[str, Any] = request(stft_pct=1.5, ltft_pct=-2.0, **rom_editor.LEARNED_HELD)
    editor = rom_editor.learned_state(
        rom_editor.SimRequest.model_validate(body), origin="rom_editor"
    )
    cli = rom_sim._learned(argparse.Namespace(stft_pct=1.5, ltft_pct=-2.0))
    assert cli.source == rom_editor.LEARNED_SOURCES["cli"]
    assert editor.source == rom_editor.LEARNED_SOURCES["rom_editor"]
    assert cli != editor
    assert dataclasses.replace(cli, source=editor.source) == editor
    # A changed trim is a different state (the comparison above is not vacuous).
    other = rom_sim._learned(argparse.Namespace(stft_pct=1.5, ltft_pct=-1.0))
    assert dataclasses.replace(other, source=editor.source) != editor


def test_the_world_resolves_traces_and_templates_on_the_declared_slot(
    world: dict[str, Any],
) -> None:
    sim = SimWorld.load(world["profile"], plant_version=None, rom_dirs=[])
    assert sim.slot == rom_editor.declared_slot(world["profile"])
    spec = load_spec(world["spec"])
    base = sim.load_base(
        BaseRef(file=str(world["root"] / "base.bin"), sha256=world["sha"]),
        spec_dir=world["root"],
    )
    candidate = sim.candidate(base, spec=spec, candidate_rom=None)
    assert candidate.image_sha256 == world["candidate_sha"]
    from_file = sim.candidate(base, spec=None, candidate_rom=world["root"] / "candidate.bin")
    assert from_file.image == candidate.image
    for in_mode in (False, True):
        expected = rom_editor.compare_images(
            base.image, candidate.image, sim.kit, slot=sim.slot, in_mode=in_mode
        ).to_dict()
        trace = sim.trace(base, candidate, in_mode=in_mode)
        assert trace["mode"] == (MODE if in_mode else None)
        assert trace["outcome"] == expected["outcome"]
        assert [o["output"] for o in trace["outputs"]] == [o["output"] for o in expected["outputs"]]
    learned = rom_sim._learned(argparse.Namespace(stft_pct=0.0, ltft_pct=2.34))
    template = sim.template(base, candidate, learned=learned, candidate_events=())
    assert template.base.slot == template.candidate.slot == sim.slot
    assert template.base.slot_source == rom_editor.SLOT_SOURCE
    assert template.learned is learned


def test_the_world_refuses_by_name(world: dict[str, Any], tmp_path: Path) -> None:
    with pytest.raises(RomEffectRefusalError) as missing:
        load_spec(tmp_path / "nope.yaml")
    assert missing.value.reason == "edit_spec_not_found"
    sim = SimWorld.load(world["profile"], plant_version=None, rom_dirs=[])
    base = sim.load_base(
        BaseRef(file=str(world["root"] / "base.bin"), sha256=world["sha"]),
        spec_dir=world["root"],
    )
    with pytest.raises(SimulationRefusalError) as unstated:
        sim.candidate(base, spec=None, candidate_rom=None)
    assert unstated.value.reason == "candidate_unstated"
    profile = tmp_path / "profile"
    shutil.copytree(world["profile"], profile)
    (profile / "tune_revisions.yaml").unlink()
    with pytest.raises(SimulationRefusalError) as unregistered:
        SimWorld.load(profile, plant_version=None, rom_dirs=[])
    assert unregistered.value.reason == "no_tune_registry"


def test_a_learned_field_the_cli_does_not_hold_is_refused_at_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rom_editor runs _check_learned_held at import, so a LearnedValues field
    added without a LEARNED_HELD key (or a stray key) cannot load. Called here
    directly with each kind of mismatch."""
    rom_editor._check_learned_held()  # the shipped pair agrees
    held = dict(rom_editor.LEARNED_HELD)
    short = {name: value for name, value in held.items() if name != next(iter(held))}
    for mismatched in (short, {**held, "bogus": 0.0}):
        monkeypatch.setattr(rom_editor, "LEARNED_HELD", mismatched)
        with pytest.raises(TypeError, match="name different fields"):
            rom_editor._check_learned_held()
