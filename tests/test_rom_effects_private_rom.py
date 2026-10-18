"""E07.2 private-ROM checks: whole-image effect traces on the operator's images.

Kept apart from ``tests/test_rom_effects.py`` (synthetic images) as E02.3
asks: these never pass *because* an image was absent. Each test skips with a
named reason when an image is not on this machine. Images go through a
temporary W08 :class:`~src.control.rom_library.RomLibrary` and are fetched
back by plain-image sha256, as a deployment would; no ROM bytes are committed
and nothing is written under the repo.

- ZS44A-20260920 -> ZS44A-20261004 (the Fuel Comp restore): the load axis and
  slot-1 cells land in BFS formation, so the change reaches X, Target AFR and
  the timing trims; every output names BFS and the observed X as why it cannot
  be predicted.
- ZS44A-k106 -> ZS44A-20260920 (one Fuel Targets cell): only the command is
  reached, and the twin's own node is computable at equal inputs.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from src.analysis.ecu_application import load_application_model
from src.analysis.rom_effects import EffectReport, compare_roms, load_rom
from src.analysis.sim_contract import load_contract
from src.control.rom_library import RomLibrary
from src.vehicle.tune_revisions import load_tune_revisions
from tests.rom_fixtures import PROFILE, operator_image

#: Revision -> (operator file key, plain-image sha256 from tune_revisions.yaml).
IMAGES = {
    "ZS44A-k106": ("k106", "e01cc0cb45d5beb879f82931be3054c79b7be3955220b3d93045deaabd23f18e"),
    "ZS44A-20260920": (
        "r20260920",
        "a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50",
    ),
    "ZS44A-20261004": (
        "r20261004",
        "5587f7e1e6dbd0fcb9ffe0c857cc0cafebbe11bf306636c0f3265b903254d253",
    ),
}
OUTPUTS = {
    "target_equiv_ratio",
    "ignition_timing",
    "ignition_trim_selected",
    "base_fuel_schedule_word",
}


@pytest.fixture(scope="module")
def library(tmp_path_factory: pytest.TempPathFactory) -> RomLibrary:
    lib = RomLibrary(
        tmp_path_factory.mktemp("rom_library"),
        profile_dir=PROFILE,
        registry=lambda: load_tune_revisions(PROFILE / "tune_revisions.yaml"),
    )
    for revision, (key, sha) in IMAGES.items():
        image = operator_image(key)
        if image is not None and hashlib.sha256(image).hexdigest() == sha:
            lib.add_image(image, source="upload_plain", filename=f"{revision}.bin")
    return lib


def _image(library: RomLibrary, revision: str) -> bytes:
    _key, sha = IMAGES[revision]
    try:
        return library.image(sha)
    except Exception:  # RomNotFoundError: absent or not the expected bytes
        pytest.skip(
            f"private ROM {revision} (plain sha256 {sha[:8]}...) is not on this machine: "
            "the image-backed effect trace did not run"
        )


def _compare(library: RomLibrary, base: str, candidate: str, **kwargs: Any) -> EffectReport:
    model, sha = load_application_model(PROFILE)
    return compare_roms(
        _image(library, base),
        _image(library, candidate),
        library.rom_map(),
        model,
        sha,
        base_slot=1,
        candidate_slot=1,
        slot_source="default_slot",
        **kwargs,
    )


def test_every_registry_image_loads_whole_and_identically(library: RomLibrary) -> None:
    model, sha = load_application_model(PROFILE)
    for revision in IMAGES:
        image = _image(library, revision)
        first = load_rom(image, library.rom_map(), model, sha, slot=1, slot_source="default_slot")
        again = load_rom(
            bytes(image), library.rom_map(), model, sha, slot=1, slot_source="default_slot"
        )
        assert first.digest() == again.digest(), revision
        assert first.outside_decode == (), revision
        assert sum(first.coverage.values()) == len(image)
        assert first.coverage["unknown"] > 0  # kept, and counted as unknown
        report = _compare(library, revision, revision)
        assert report.outcome == "identical"


def test_the_fuel_comp_restore_reaches_bfs_x_target_afr_and_timing(library: RomLibrary) -> None:
    contract, _ = load_contract(PROFILE)
    report = _compare(
        library,
        "ZS44A-20260920",
        "ZS44A-20261004",
        contract=contract,
        mode="conditional_rom_comparison",
    )
    assert report.changed_nodes == ("fuel_comp_lookup",)
    by_class: dict[str, set[str]] = {}
    for r in report.ranges:
        by_class.setdefault(r.byte_class, set()).update(r.what)
    # Nothing outside the modelled read and the checksum words changed.
    assert set(by_class) == {"consumed", "checksum"}
    assert by_class["consumed"] == {"fuel_comp_flow_axis", "fuel_compensation (slot 1)"}
    assert {o.output for o in report.outputs if o.path} == OUTPUTS
    for output in report.outputs:
        assert output.status == "unevaluable", output.output
        assert "unmodelled:manifold_fill_filter" in output.reasons
        if output.output != "base_fuel_schedule_word":
            assert "observed_not_predicted:load_input_x" in output.reasons
        assert "unknown_bytes_changed" not in output.reasons
    moved = {m["node"] for m in report.moved_lookups}
    assert {"target_afr_lookup", "timing_trim_lookup"} <= moved
    twin = report.output("target_equiv_ratio").twin_node
    assert twin is not None and twin["reached"] and not twin["evaluable_at_equal_inputs"]


def test_the_k106_target_cell_edit_reaches_the_command_and_spark_through_the_charge_word(
    library: RomLibrary,
) -> None:
    report = _compare(library, "ZS44A-k106", "ZS44A-20260920")
    assert report.changed_nodes == ("target_afr_lookup",)
    consumed = [r for r in report.ranges if r.byte_class == "consumed"]
    assert sum(r.changed for r in consumed) == 1
    assert {r.byte_class for r in report.ranges} == {"consumed", "checksum"}
    # Through the enrichment multiplier into charge word 1B24 and term 27C6.
    spark = report.output("ignition_timing")
    assert spark.status == "unevaluable"
    assert "observed_not_predicted:timing_charge_word" in spark.reasons
    assert report.output("ignition_trim_selected").status == "unchanged"
    command = report.output("target_equiv_ratio")
    assert command.twin_node is not None
    assert command.twin_node["evaluable_at_equal_inputs"]
    assert "unmodelled:final_command_word" in command.reasons
    assert report.outcome == "partial"
