"""E07.2: whole-ROM loading and effect coverage, on synthetic images.

No ROM bytes are committed: the images are ``tests.rom_fixtures`` noise laid
out like the Xterra's, with every constant, condition and patch marker the
application model checks written in, and the model's program identity
re-pinned to the synthetic program spans (the stand-in for "the same
program"). ``tests/test_rom_effects_private_rom.py`` runs the same traces on
the operator's real images.
"""

from __future__ import annotations

import json
import struct
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from src.analysis.ecu_application import ApplicationModel, load_application_model, program_sha256
from src.analysis.rom_effects import (
    CellEdit,
    EffectReport,
    RomEffectRefusalError,
    compare_roms,
    load_rom,
    make_candidate,
    write_candidate,
)
from src.analysis.rom_map import RomMap
from src.analysis.sim_contract import load_contract
from tests.rom_fixtures import PROFILE, synthetic_image, xterra_map

OUTPUTS = ("target_equiv_ratio", "ignition_timing", "ignition_trim_selected")

Setup = tuple[bytes, RomMap, ApplicationModel, str]


def modelled_image(model: ApplicationModel) -> bytes:
    """A synthetic image that every node's decode describes."""
    image = bytearray(synthetic_image())
    for marker in model.program.patch.markers:
        raw = bytes.fromhex(marker.bytes)
        image[marker.offset : marker.offset + len(raw)] = raw
    for node in model.nodes:
        for ref in node.rom:
            if ref.offset is not None:
                assert ref.width is not None and ref.value is not None
                width = ref.width // 8
                image[ref.offset : ref.offset + width] = ref.value.to_bytes(width, "big")
        for c in node.calibration_conditions:
            for k in range(c.count):
                image[c.offset + k] = (image[c.offset + k] & ~c.mask & 0xFF) | c.equals
    return bytes(image)


@pytest.fixture(scope="module")
def setup() -> Setup:
    model, sha = load_application_model(PROFILE)
    image = modelled_image(model)
    identity = model.program.identity.model_copy(update={"sha256": program_sha256(image, model)})
    program = model.program.model_copy(update={"identity": identity})
    return image, xterra_map(), model.model_copy(update={"program": program}), sha


def _compare(
    setup: Setup,
    candidate: bytes,
    *,
    slot: int = 1,
    base: bytes | None = None,
    **kwargs: Any,
) -> EffectReport:
    image, rom_map, model, sha = setup
    return compare_roms(
        image if base is None else base,
        candidate,
        rom_map,
        model,
        sha,
        base_slot=slot,
        candidate_slot=slot,
        slot_source="default_slot",
        **kwargs,
    )


def _edit(setup: Setup, *edits: CellEdit) -> bytes:
    image, rom_map, _, _ = setup
    return make_candidate(image, rom_map, list(edits), base_label="synthetic").image


def _bump(
    image: bytes, rom_map: RomMap, table_id: str, slot: int | None, row: int, col: int
) -> CellEdit:
    table = rom_map.table(table_id)
    address = rom_map.table_address(table, slot) + (row * table.cols + col)
    return CellEdit(table_id, row, col, image[address] ^ 0x01, slot=slot)


# -- loading --------------------------------------------------------------------------------


def test_identical_roms_and_states_give_identical_results(setup: Setup) -> None:
    image, rom_map, model, sha = setup
    first = load_rom(image, rom_map, model, sha, slot=1, slot_source="default_slot")
    again = load_rom(bytes(image), rom_map, model, sha, slot=1, slot_source="default_slot")
    assert first.digest() == again.digest()
    assert first.outside_decode == ()
    report = _compare(setup, bytes(image))
    assert report.outcome == "identical"
    assert {o.status for o in report.outputs} == {"unchanged"}
    assert report.to_dict() == _compare(setup, bytes(image)).to_dict()
    # Another slot is another state: different reads, a different digest.
    other = load_rom(image, rom_map, model, sha, slot=2, slot_source="observed_checkpoint")
    assert other.digest() != first.digest()


def test_every_modelled_table_constant_and_condition_resolves(setup: Setup) -> None:
    image, rom_map, model, sha = setup
    loaded = load_rom(image, rom_map, model, sha, slot=1, slot_source="default_slot")
    cited = {(node.id, ref.rom_map) for node in model.nodes for ref in node.rom if ref.rom_map}
    resolved = {(key.split("|")[0], key.split("|")[1]) for key in loaded.reads}
    assert cited <= resolved
    assert ("base_fuel_schedule", "fuel_compensation") in resolved
    assert ("base_fuel_schedule", "fuel_comp_flow_axis") in resolved  # its column axis
    assert ("target_afr_lookup", "fuel_targets") in resolved
    assert any(k.startswith("base_fuel_schedule|constant|") for k in loaded.reads)
    assert any(k.startswith("pedal_enrichment_flag|condition|") for k in loaded.reads)
    # The whole image is kept, unknown bytes and all, and every byte is classed.
    assert loaded.image == image
    assert sum(loaded.coverage.values()) == len(image)
    assert loaded.coverage["unknown"] > 0


def test_an_unsupported_program_is_unevaluable_by_name(setup: Setup) -> None:
    image, rom_map, model, sha = setup
    other = bytearray(image)
    other[0x030000] ^= 0x5A  # code changed under the same calibration ID
    report = _compare(setup, bytes(other))
    assert (report.outcome, report.reason, report.outputs) == ("unevaluable", "wrong_program", ())
    with pytest.raises(RomEffectRefusalError) as refused:
        load_rom(bytes(other), rom_map, model, sha, slot=1, slot_source="default_slot")
    assert refused.value.reason == "wrong_program"
    # The same program without UpRev's pointer redirect: the model's identity
    # re-pinned to those spans, the patch marker still missing.
    unpatched = bytearray(image)
    unpatched[0x055EC4] = 0x00
    identity = model.program.identity.model_copy(
        update={"sha256": program_sha256(bytes(unpatched), model)}
    )
    same = model.model_copy(
        update={"program": model.program.model_copy(update={"identity": identity})}
    )
    report = compare_roms(
        bytes(unpatched),
        bytes(unpatched),
        rom_map,
        same,
        sha,
        base_slot=1,
        candidate_slot=1,
        slot_source="default_slot",
    )
    assert (report.outcome, report.reason) == ("unevaluable", "patch_missing")


def test_an_unresolved_or_differing_slot_is_unevaluable_by_name(setup: Setup) -> None:
    image, rom_map, model, sha = setup
    unresolved = compare_roms(
        image,
        image,
        rom_map,
        model,
        sha,
        base_slot=None,
        candidate_slot=None,
        slot_source="unresolved",
    )
    assert (unresolved.outcome, unresolved.reason) == ("unevaluable", "active_slot_unresolved")
    differing = compare_roms(
        image,
        image,
        rom_map,
        model,
        sha,
        base_slot=1,
        candidate_slot=2,
        slot_source="observed_checkpoint",
    )
    assert (differing.outcome, differing.reason) == ("unevaluable", "slot_mismatch")


# -- tracing ----------------------------------------------------------------------------------


def test_a_fuel_comp_edit_reaches_bfs_x_target_afr_and_the_timing_trims(setup: Setup) -> None:
    """Fuel Comp is inside BFS: the edit reaches the X byte, so Target AFR and
    the timing trims are read at a moved column, and no output can be
    predicted while BFS is unmodelled and X is only observed."""
    image, rom_map, _, _ = setup
    candidate = _edit(setup, _bump(image, rom_map, "fuel_compensation", 1, 9, 12))
    report = _compare(setup, candidate)
    assert report.changed_nodes == ("base_fuel_schedule",)
    consumed = [r for r in report.ranges if r.byte_class == "consumed"]
    assert [r.what for r in consumed] == [("fuel_compensation (slot 1)",)]
    assert {o.output for o in report.outputs if o.path} == set(OUTPUTS)
    for output in report.outputs:
        assert output.status == "unevaluable"
        assert "unmodelled:base_fuel_schedule" in output.reasons
        assert "observed_not_predicted:load_input_x" in output.reasons
        assert {"base_fuel_schedule", "load_input_x"} <= set(output.path)
    moved = {m["node"]: m["tables"] for m in report.moved_lookups}
    assert "fuel_targets" in moved["target_afr_lookup"]
    assert "timing_trim_a" in moved["timing_trim_lookup"]
    twin = report.output("target_equiv_ratio").twin_node
    assert twin is not None and twin["reached"] and not twin["evaluable_at_equal_inputs"]
    assert report.outcome == "unevaluable"


def test_a_fuel_comp_axis_edit_is_the_same_path(setup: Setup) -> None:
    image, rom_map, _, _ = setup
    candidate = _edit(setup, _bump(image, rom_map, "fuel_comp_flow_axis", None, 0, 7))
    report = _compare(setup, candidate)
    assert report.changed_nodes == ("base_fuel_schedule",)
    assert [r.what for r in report.ranges] == [("fuel_comp_flow_axis",)]
    assert report.output("ignition_trim_selected").status == "unevaluable"


def test_a_target_afr_cell_edit_reaches_only_the_command(setup: Setup) -> None:
    image, rom_map, _, _ = setup
    candidate = _edit(setup, _bump(image, rom_map, "fuel_targets", 1, 6, 7))
    report = _compare(setup, candidate)
    assert report.changed_nodes == ("target_afr_lookup",)
    assert report.output("ignition_timing").status == "unchanged"
    assert report.output("ignition_trim_selected").status == "unchanged"
    command = report.output("target_equiv_ratio")
    assert command.status == "unevaluable"
    assert "unmodelled:final_command_word" in command.reasons
    # The twin's own node is computed at equal inputs by implemented logic.
    assert command.twin_node is not None and command.twin_node["evaluable_at_equal_inputs"]
    assert report.outcome == "partial"


def test_another_slots_copy_is_inactive_unless_it_is_the_active_slot(setup: Setup) -> None:
    image, rom_map, _, _ = setup
    candidate = _edit(setup, _bump(image, rom_map, "fuel_targets_maps_2_5", 3, 2, 2))
    on_slot_1 = _compare(setup, candidate)
    assert [r.byte_class for r in on_slot_1.ranges] == ["inactive_for_scenario"]
    assert {o.status for o in on_slot_1.outputs} == {"unchanged"}
    assert on_slot_1.outcome == "evaluable"
    on_slot_3 = _compare(setup, candidate, slot=3)
    assert on_slot_3.changed_nodes == ("target_afr_lookup",)


def test_unknown_bytes_are_never_assumed_to_have_no_effect(setup: Setup) -> None:
    image, rom_map, model, sha = setup
    loaded = load_rom(image, rom_map, model, sha, slot=1, slot_source="default_slot")
    assert loaded.coverage["unknown"] > 0
    unknown = bytearray(image)
    unknown[0x8100] ^= 0xFF  # calibration block, mapped by nothing
    report = _compare(setup, bytes(unknown))
    assert [r.byte_class for r in report.ranges] == ["unknown"]
    assert {o.status for o in report.outputs} == {"unknown"}
    assert all("unknown_bytes_changed" in o.reasons for o in report.outputs)
    assert report.outcome == "unevaluable"
    # Mixed with a modelled edit, the modelled path is no longer evaluable either.
    mixed = bytearray(_edit(setup, _bump(image, rom_map, "fuel_targets", 1, 0, 0)))
    mixed[0x8100] ^= 0xFF
    command = _compare(setup, bytes(mixed)).output("target_equiv_ratio")
    assert command.twin_node is not None and not command.twin_node["evaluable_at_equal_inputs"]


def test_a_mapped_table_no_node_reads_is_unmodelled_not_inert(setup: Setup) -> None:
    """The Load Point Scaler slot copy: which copy the ECU consumes is open."""
    image, rom_map, _, _ = setup
    table = rom_map.table("load_point_scaler_slot_copy")
    address = rom_map.table_address(table, 1)
    (old,) = struct.unpack_from(">H", image, address)
    candidate = _edit(setup, CellEdit(table.id, 0, 0, old ^ 0x0101, slot=1))
    report = _compare(setup, candidate)
    assert [r.byte_class for r in report.ranges] == ["mapped_unmodelled"]
    assert {o.status for o in report.outputs} == {"unknown"}


def test_a_branch_setting_off_its_decode_makes_its_outputs_unevaluable(setup: Setup) -> None:
    image, _, model, _ = setup
    (condition,) = model.node("alternate_multiplier_writer").calibration_conditions
    live = bytearray(image)
    live[condition.offset] ^= condition.mask  # the dead alternate writer becomes live
    report = _compare(setup, bytes(live))
    assert report.changed_nodes == ("alternate_multiplier_writer",)
    command = report.output("target_equiv_ratio")
    assert "outside_decode:alternate_multiplier_writer" in command.reasons
    assert command.status == "unevaluable"


def test_a_mode_that_holds_a_changed_node_fixed_is_named(setup: Setup) -> None:
    """The pedal flag's threshold curve feeds the flag, which the comparison
    holds as a stated boundary: the change cannot propagate there."""
    image, _, _, _ = setup
    contract, _ = load_contract(PROFILE)
    curve = bytearray(image)
    curve[0xCEDB] = 190
    report = _compare(setup, bytes(curve), contract=contract, mode="conditional_rom_comparison")
    assert report.changed_nodes == ("pedal_enrichment_flag",)
    assert "held_fixed:pedal_flag" in report.output("target_equiv_ratio").reasons
    bare = _compare(setup, bytes(curve))
    assert (
        "observed_not_predicted:pedal_enrichment_flag" in bare.output("target_equiv_ratio").reasons
    )


# -- isolated candidates ----------------------------------------------------------------------


def test_a_candidate_changes_only_its_cells_and_stays_unflashable(setup: Setup) -> None:
    image, rom_map, _, _ = setup
    edit = _bump(image, rom_map, "fuel_compensation", 1, 3, 4)
    candidate = make_candidate(image, rom_map, [edit], base_label="synthetic")
    diff = [i for i, (a, b) in enumerate(zip(image, candidate.image, strict=True)) if a != b]
    table = rom_map.table("fuel_compensation")
    assert diff == [rom_map.table_address(table, 1) + 3 * table.cols + 4]
    checks = rom_map.checksums
    assert (
        candidate.image[checks.sum_offset : checks.sum_offset + 4]
        == image[checks.sum_offset : checks.sum_offset + 4]
    )
    assert checks.values(candidate.image) != checks.values(image)
    assert candidate.provenance["flashable"] is False
    assert candidate.provenance["edits"][0]["new"] == edit.raw


@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (CellEdit("not_a_table", 0, 0, 1), "table_unknown"),
        (CellEdit("fuel_compensation", 0, 0, 1), "slot_invalid"),
        (CellEdit("fuel_comp_flow_axis", 0, 0, 1, slot=1), "slot_invalid"),
        (CellEdit("fuel_targets", 0, 0, 1, slot=2), "slot_invalid"),
        (CellEdit("fuel_compensation", 16, 0, 1, slot=1), "cell_outside_table"),
        (CellEdit("fuel_compensation", 0, 0, 256, slot=1), "value_out_of_range"),
    ],
)
def test_a_candidate_edit_outside_the_map_is_refused(
    setup: Setup, edit: CellEdit, reason: str
) -> None:
    image, rom_map, _, _ = setup
    with pytest.raises(RomEffectRefusalError) as refused:
        make_candidate(image, rom_map, [edit], base_label="synthetic")
    assert refused.value.reason == reason


def test_candidates_are_written_apart_from_git_and_the_rom_library(
    setup: Setup, tmp_path: Path
) -> None:
    image, rom_map, _, _ = setup
    candidate = make_candidate(
        image, rom_map, [_bump(image, rom_map, "fuel_targets", 1, 1, 1)], base_label="s"
    )
    library = tmp_path / "rom_library"
    with pytest.raises(RomEffectRefusalError) as refused:
        write_candidate(candidate, PROFILE / "_sim", rom_library_dir=library)
    assert refused.value.reason == "inside_checkout"
    for root in (library, library / "sim", tmp_path):
        with pytest.raises(RomEffectRefusalError) as refused:
            write_candidate(candidate, root, rom_library_dir=library)
        assert refused.value.reason == "inside_rom_library"
    written = write_candidate(candidate, tmp_path / "sim", rom_library_dir=library)
    assert (written / "image.sim").read_bytes() == candidate.image
    record = json.loads((written / "provenance.json").read_text())
    assert record["candidate_sha256"] == candidate.image_sha256
    assert record["flashable"] is False
    assert not any(written.glob("*.bin"))


# -- no write path ---------------------------------------------------------------------------

#: Modules that can transmit on a bus or authorize a non-volatile write.
_WRITE_PATHS = ("src.control", "src.daemon", "src.dashboard", "src.mcp_server", "can", "serial")
_ALLOWED_COLLECTION = {"src.collection", "src.collection.kline", "src.collection.kline.names"}


def test_loading_tracing_and_writing_candidates_requests_no_ecu_write(tmp_path: Path) -> None:
    script = textwrap.dedent(
        f"""
        import json, sys
        from pathlib import Path
        from tests.test_rom_effects import modelled_image, _bump
        from src.analysis.ecu_application import load_application_model, program_sha256
        from src.analysis.rom_effects import compare_roms, make_candidate, write_candidate
        from src.analysis.sim_contract import load_contract
        from tests.rom_fixtures import PROFILE, xterra_map
        model, sha = load_application_model(PROFILE)
        image = modelled_image(model)
        identity = model.program.identity.model_copy(
            update={{"sha256": program_sha256(image, model)}}
        )
        model = model.model_copy(
            update={{"program": model.program.model_copy(update={{"identity": identity}})}}
        )
        rom_map = xterra_map()
        contract, _ = load_contract(PROFILE)
        candidate = make_candidate(
            image, rom_map, [_bump(image, rom_map, "fuel_compensation", 1, 1, 1)],
            base_label="synthetic",
        )
        report = compare_roms(
            image, candidate.image, rom_map, model, sha, base_slot=1, candidate_slot=1,
            slot_source="default_slot", contract=contract, mode="conditional_rom_comparison",
        )
        assert report.changed_nodes == ("base_fuel_schedule",)
        write_candidate(
            candidate, Path({str(tmp_path)!r}) / "sim",
            rom_library_dir=Path({str(tmp_path)!r}) / "lib",
        )
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


# -- verifier findings, 2026-10-05 ------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "node", "output"),
    [
        (0x8B39, "enrichment_gate", "target_equiv_ratio"),  # gate constant
        (0xD247, "enrichment_excess_scale", "target_equiv_ratio"),  # excess-scale curve
        (0x8CCD, "timing_trim_lookup", "ignition_trim_selected"),  # cold threshold
    ],
)
def test_a_twin_node_off_its_decode_is_not_evaluable_at_equal_inputs(
    setup: Setup, offset: int, node: str, output: str
) -> None:
    image = bytearray(setup[0])
    image[offset] ^= 0x01
    effect = _compare(setup, bytes(image)).output(output)
    assert effect.twin_node is not None and effect.twin_node["reached"]
    assert f"outside_decode:{node}" in effect.twin_node["reasons"]
    assert not effect.twin_node["evaluable_at_equal_inputs"]
    assert effect.status == "unevaluable"


def test_a_bit_outside_a_conditions_mask_is_unknown_not_inert(setup: Setup) -> None:
    """Cal 0x8ADC: the trim lookup tests bit 1 only. A change to bit 0 is a
    bit nothing decodes, so no output may read ``unchanged``."""
    image, _, model, _ = setup
    (condition,) = [
        c for c in model.node("timing_trim_lookup").calibration_conditions if c.offset == 0x8ADC
    ]
    assert condition.mask == 0x02
    other_bit = bytearray(image)
    other_bit[0x8ADC] ^= 0x01
    report = _compare(setup, bytes(other_bit))
    assert [r.byte_class for r in report.ranges] == ["unknown"]
    assert report.changed_nodes == ()
    assert {o.status for o in report.outputs} == {"unknown"}
    tested_bit = bytearray(image)
    tested_bit[0x8ADC] ^= 0x02
    consumed = _compare(setup, bytes(tested_bit))
    assert consumed.changed_nodes == ("timing_trim_lookup",)
    assert "outside_decode:timing_trim_lookup" in consumed.output("ignition_trim_selected").reasons
