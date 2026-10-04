"""W08.1: the ROM map is validated vehicle data, and cannot drift from rom_targets.yaml."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from src.analysis.rom_map import RomMap, check_against_targets, load_rom_map
from src.analysis.rom_targets import Definition
from tests.rom_fixtures import PROFILE


def _shipped() -> dict[str, Any]:
    data = yaml.safe_load((PROFILE / "rom_map.yaml").read_text())
    assert isinstance(data, dict)
    return data


def _targets() -> Definition:
    return Definition.model_validate(yaml.safe_load((PROFILE / "rom_targets.yaml").read_text()))


def test_shipped_map_loads_and_matches_every_verified_rom_targets_fact() -> None:
    rom_map = load_rom_map(PROFILE)
    targets = _targets()
    assert rom_map.image_size == targets.image_size
    assert rom_map.checksums == targets.checksums
    (fuel,) = [
        t for t in rom_map.tables if t.kind == targets.table and targets.map_slot in (t.slots or [])
    ]
    assert rom_map.table_address(fuel, targets.map_slot) == targets.table_offset
    assert (fuel.rows, fuel.cols, fuel.dtype) == (8, 8, "u8")
    assert fuel.confidence == "verified"
    assert rom_map.table(fuel.axes.rows or "").offset == targets.rpm_axis_offset
    assert rom_map.table(fuel.axes.cols or "").offset == targets.load_axis_offset
    assert fuel.decode is not None and fuel.decode.numerator == targets.afr_numerator


def test_shipped_map_carries_the_2026_10_04_findings_with_their_confidence() -> None:
    rom_map = load_rom_map(PROFILE)
    by_id = {t.id: t for t in rom_map.tables}
    assert by_id["k_constant"].confidence == "inferred"
    assert by_id["k_constant"].decode is None  # raw only: no scale is established
    assert (by_id["k_constant"].slot_offset, by_id["k_constant"].dtype) == (0x762, "be16")
    assert by_id["fuel_comp_rpm_axis"].offset == 0xCB29
    assert by_id["fuel_comp_rpm_axis"].confidence == "inferred"
    assert by_id["fuel_targets_maps_2_5"].confidence == "inferred"
    assert rom_map.map_slots is not None
    assert (rom_map.map_slots.base, rom_map.map_slots.stride, rom_map.map_slots.count) == (
        0xE0000,
        0x800,
        5,
    )
    spans = {r.name: r for r in rom_map.map_slots.regions}
    assert spans["Ignition timing (probable)"].confidence == "inferred"
    assert (spans["Fuel Compensation (span)"].start, spans["Fuel Compensation (span)"].end) == (
        0x670,
        0x761,
    )
    regions = {(r.start, r.end): r for r in rom_map.regions}
    assert regions[(0x000000, 0x000007)].confidence == "verified"  # boot vectors
    assert regions[(0x020004, 0x02000B)].confidence == "verified"  # app vectors
    assert regions[(0x008204, 0x00820E)].kind == "calibration"
    for erased in [(0x012000, 0x01FFFF), (0x0DF000, 0x0DFFFF), (0x0E3000, 0x0FEFFF)]:
        assert regions[erased].kind == "erased" and regions[erased].confidence == "verified"
    assert regions[(0x0DE000, 0x0DEFFF)].added_by_uprev
    assert regions[(0x0FFF80, 0x0FFF87)].confidence == "unknown"
    assert rom_map.calibration_id.offset == 0x8204
    assert [s.index for s in rom_map.erase_sectors] == list(range(16))


def _refuse(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        RomMap.model_validate(data)


@pytest.mark.parametrize("where", ["region", "table", "slot_region", "erase_sector"])
def test_missing_evidence_is_refused(where: str) -> None:
    data = _shipped()
    target = {
        "region": lambda d: d["regions"][0],
        "table": lambda d: d["tables"][0],
        "slot_region": lambda d: d["map_slots"]["regions"][0],
        "erase_sector": lambda d: d["erase_sectors"][0],
    }[where](data)
    del target["evidence"]
    _refuse(data, "evidence")


def test_empty_evidence_is_refused() -> None:
    data = _shipped()
    data["tables"][0]["evidence"] = ""
    _refuse(data, "evidence")


def test_unknown_confidence_is_refused() -> None:
    data = _shipped()
    data["tables"][0]["confidence"] = "probably"
    _refuse(data, "confidence")
    data = _shipped()
    data["regions"][0]["confidence"] = "likely"
    _refuse(data, "confidence")


def test_missing_added_by_uprev_is_refused() -> None:
    data = _shipped()
    del data["regions"][0]["added_by_uprev"]
    _refuse(data, "added_by_uprev")


def test_overlapping_tables_in_one_slot_are_refused() -> None:
    data = _shipped()
    k = next(t for t in data["tables"] if t["id"] == "k_constant")
    clash = copy.deepcopy(k)
    clash.update(id="k_shadow", slot_offset=0x763)  # one byte into K's word
    data["tables"].append(clash)
    _refuse(data, "overlapping tables")


def test_tables_overlapping_only_in_disjoint_slots_are_allowed() -> None:
    # The shipped map already relies on this: fuel_targets (slot 1) and
    # fuel_targets_maps_2_5 share an offset in different slots.
    data = _shipped()
    RomMap.model_validate(data)
    shifted = next(t for t in data["tables"] if t["id"] == "fuel_targets_maps_2_5")
    shifted["slots"] = [1, 2]
    _refuse(data, "overlapping tables")


def test_overlapping_absolute_tables_are_refused() -> None:
    data = _shipped()
    axis = next(t for t in data["tables"] if t["id"] == "fuel_targets_rpm_axis")
    clash = copy.deepcopy(axis)
    clash.update(id="rpm_axis_copy", offset=0xCAA9)
    data["tables"].append(clash)
    _refuse(data, "overlapping tables")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["regions"].append(
            {
                **d["regions"][0],
                "name": "past the end",
                "start": 0x0FFFF0,
                "end": 0x100000,
            }
        ),
        lambda d: d["tables"][0].update(slot_offset=0x7F0),  # 64 cells overrun a 0x800 slot
        lambda d: d["tables"].append(
            {
                **next(t for t in d["tables"] if t["id"] == "fuel_targets_bfs_axis"),
                "id": "too_far",
                "offset": 0x0FFFFC,
            }
        ),
        lambda d: d["calibration_id"].update(offset=0x0FFFFA),
        lambda d: d["map_slots"].update(count=70),
        lambda d: d["erase_sectors"][15].update(end=0x100FFF),
    ],
    ids=["region", "slot-table", "table", "calibration-id", "slots", "erase-sector"],
)
def test_an_address_beyond_the_image_is_refused(mutate: Any) -> None:
    data = _shipped()
    mutate(data)
    with pytest.raises(ValidationError, match="beyond|does not fit"):
        RomMap.model_validate(data)


def test_partially_overlapping_regions_are_refused_but_nesting_is_allowed() -> None:
    data = _shipped()
    data["regions"].append(
        {**data["regions"][0], "name": "straddles boot", "start": 0x007000, "end": 0x008FFF}
    )
    _refuse(data, "overlapping spans")


def test_axis_must_reference_a_one_dimensional_table_of_the_right_length() -> None:
    data = _shipped()
    fuel = next(t for t in data["tables"] if t["id"] == "fuel_targets")
    fuel["axes"]["rows"] = "fuel_comp_rpm_axis"  # 16 entries for 8 rows
    _refuse(data, "16 entries for 8 rows")
    data = _shipped()
    next(t for t in data["tables"] if t["id"] == "fuel_targets")["axes"]["cols"] = "nowhere"
    _refuse(data, "not a table id")


def test_unconfirmed_decode_needs_a_declared_confidence() -> None:
    data = _shipped()
    k = next(t for t in data["tables"] if t["id"] == "k_constant")
    k["decode"] = {
        "kind": "linear",
        "quantity": "k",
        "unit": "",
        "scale": 1 / 27089,
        "confidence": "unknown",
        "evidence": "a guess",
    }
    _refuse(data, "confidence")


def test_drift_from_rom_targets_is_refused_by_name(tmp_path: Path) -> None:
    rom_map = load_rom_map(PROFILE)
    targets = _targets()
    moved = targets.model_copy(update={"table_offset": targets.table_offset + 1})
    with pytest.raises(ValueError, match="disagrees with rom_targets.yaml"):
        check_against_targets(rom_map, moved)
    other_axis = targets.model_copy(update={"rpm_axis_offset": 0xCB29})
    with pytest.raises(ValueError, match="rows axis"):
        check_against_targets(rom_map, other_axis)
    # And the loader runs the check: a drifted copy of the profile refuses.
    for name in ("rom_map.yaml", "rom_targets.yaml"):
        (tmp_path / name).write_text((PROFILE / name).read_text())
    drifted = yaml.safe_load((tmp_path / "rom_targets.yaml").read_text())
    drifted["checksums"]["sum_offset"] = 0x95A4
    drifted["checksums"]["excluded"] = [0x20000, 0x9598, 0x95A4]
    (tmp_path / "rom_targets.yaml").write_text(yaml.safe_dump(drifted))
    with pytest.raises(ValueError, match="checksums differ"):
        load_rom_map(tmp_path)
