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
from src.signals.afr import load_afr_calibration
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
    # The firmware confirmed K (read at 0x03C6E0), and UpRev wrote it to all five slots.
    assert by_id["k_constant"].confidence == "verified"
    assert by_id["k_constant"].decode is None  # raw only: no scale is established
    assert (by_id["k_constant"].slot_offset, by_id["k_constant"].dtype) == (0x762, "be16")
    assert by_id["k_constant"].slots is None  # every slot
    assert by_id["fuel_targets_maps_2_5"].confidence == "inferred"
    assert rom_map.map_slots is not None
    assert (rom_map.map_slots.base, rom_map.map_slots.stride, rom_map.map_slots.count) == (
        0xE0000,
        0x800,
        5,
    )
    # The 2026-10-04 diff spans became tables once the code that reads them was
    # found; the spans left are the two slot areas nobody has identified.
    spans = {r.name: r for r in rom_map.map_slots.regions}
    assert "Ignition timing (probable)" not in spans
    assert "Fuel Compensation (span)" not in spans
    assert {(s.start, s.end, s.confidence) for s in spans.values()} == {
        (0x260, 0x45F, "unknown"),
        (0x460, 0x65F, "unknown"),
    }
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
    fuel["axes"]["rows"] = "fuel_comp_engine_speed_axis"  # 16 entries for 8 rows
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


# -- slot tables confirmed from the firmware (docs/ecu-firmware-analysis.md) --


def test_slot_tables_pack_the_slot_as_the_firmware_reads_it() -> None:
    """Each table at the RAM-copy offset its reader uses, at the size its lookup walks."""
    rom_map = load_rom_map(PROFILE)
    spans = {
        t.id: (t.slot_offset, t.slot_offset + t.size - 1)
        for t in rom_map.tables
        if t.slot_offset is not None and (t.slots is None or 1 in t.slots)
    }
    assert spans == {
        "timing_trim_a": (0x020, 0x11F),  # RAM 0xFFFFB060, 16 x 16 bytes
        "timing_trim_b": (0x120, 0x21F),  # RAM 0xFFFFB160
        "fuel_targets": (0x220, 0x25F),  # RAM 0xFFFFB260, 8 x 8
        "fuel_compensation": (0x660, 0x75F),  # RAM 0xFFFFB6A0, 16 x 16
        "load_point_scaler_slot_copy": (0x760, 0x761),
        "k_constant": (0x762, 0x763),  # RAM 0xFFFFB7A2, BE16
    }
    fuel_comp = rom_map.table("fuel_compensation")
    # The operator's one-cell UpRev edit (0x0E075F, 132 -> 131) is its last cell.
    last = rom_map.table_address(fuel_comp, 1) + fuel_comp.size - 1
    assert last == 0x0E075F


def test_every_slot_table_is_verified_in_map_1_and_inferred_in_maps_2_to_5() -> None:
    # No map switch has been observed live, so only the slot the ECM runs is verified.
    rom_map = load_rom_map(PROFILE)
    for kind in ("timing_trim_a", "timing_trim_b", "fuel_compensation"):
        copies = [t for t in rom_map.tables if t.kind == kind]
        assert {t.confidence: t.slots for t in copies} == {
            "verified": [1],
            "inferred": [2, 3, 4, 5],
        }, kind
        shapes = {
            (t.slot_offset, t.rows, t.cols, t.dtype, t.axes.rows, t.axes.cols) for t in copies
        }
        assert len(shapes) == 1, kind
        decodes = {(t.decode.scale, t.decode.mask) for t in copies if t.decode is not None}
        assert len(decodes) == 1, kind


def test_slot_table_axes_are_the_ones_the_lookup_wrappers_search() -> None:
    rom_map = load_rom_map(PROFILE)
    for table_id in ("timing_trim_a", "timing_trim_b"):
        trim = rom_map.table(table_id)
        rows = rom_map.table(trim.axes.rows or "")
        cols = rom_map.table(trim.axes.cols or "")
        # r5 = 0xCC93 is searched against engine speed; r6 = 0xCC83 against the X byte.
        assert (rows.offset, rows.rows, rows.confidence) == (0xCC93, 16, "verified")
        assert rows.decode is not None and (rows.decode.scale, rows.decode.unit) == (50, "rpm")
        assert (cols.offset, cols.cols, cols.decode) == (0xCC83, 16, None)
        assert trim.decode is not None and trim.decode.mask == 0x7F
    fuel_comp = rom_map.table("fuel_compensation")
    rows = rom_map.table(fuel_comp.axes.rows or "")
    cols = rom_map.table(fuel_comp.axes.cols or "")
    # Verified from the call site 0x02DC34-46 + wrapper 0x026A24, the reverse
    # of UpRev's editor labels: 0xCACD is the rpm rows, 0xCB29 the load columns.
    assert (rows.offset, rows.confidence) == (0xCACD, "verified")
    assert rows.decode is not None and (rows.decode.scale, rows.decode.confidence) == (
        50,
        "verified",
    )
    assert (cols.offset, cols.confidence, cols.decode) == (0xCB29, "verified", None)
    assert fuel_comp.decode is not None
    assert fuel_comp.decode.apply(132) == pytest.approx(103.125)  # UpRev showed 103.13
    assert fuel_comp.decode.apply(131) == pytest.approx(102.34375)  # and 102.34


def test_load_point_scaler_reproduces_every_displayed_bfs_breakpoint() -> None:
    """X = BFS x 128 / scaler, so raw x scaler / 128 x 0.00049 ms is UpRev's BFS axis."""
    rom_map = load_rom_map(PROFILE)
    scaler = rom_map.table("load_point_scaler")
    assert (scaler.offset, scaler.dtype, scaler.confidence) == (0x9416, "be16", "verified")
    assert scaler.decode is not None
    full_scale_ms = scaler.decode.apply(44019)  # the value at 0x9416 in every ZS44A image
    assert full_scale_ms is not None and round(full_scale_ms, 2) == 21.57
    calibration = load_afr_calibration(PROFILE / "afr_calibration.yaml")
    checked = 0
    for target in calibration.target_maps:
        if target.raw_load_breakpoints is None:
            continue
        derived = [round(raw * full_scale_ms / 128, 2) for raw in target.raw_load_breakpoints]
        assert derived == pytest.approx(target.load_breakpoints, abs=1e-9), target.revision
        checked += 1
    assert checked >= 1
    copy_ = rom_map.table("load_point_scaler_slot_copy")
    assert (copy_.slot_offset, copy_.confidence) == (0x760, "inferred")  # no code reads it


def test_a_decode_mask_applies_before_the_scale_and_is_shown() -> None:
    rom_map = load_rom_map(PROFILE)
    trim = rom_map.table("timing_trim_a").decode
    assert trim is not None
    assert trim.apply(0xBD) == 0x3D  # the flag bit is not part of the value
    assert trim.apply(0x3D) == 0x3D
    assert trim.formula == "timing trim = (raw & 0x7F) × 1"
    assert rom_map.table("fuel_compensation").decode is not None
    assert rom_map.table("fuel_compensation").decode.formula == (  # type: ignore[union-attr]
        "fuel compensation = raw × 0.78125"
    )


@pytest.mark.parametrize(
    ("mask", "match"),
    [(0x1FF, "wider than a u8 cell"), (0, "greater than or equal to 1")],
)
def test_a_mask_must_fit_the_cell(mask: int, match: str) -> None:
    data = _shipped()
    trim = next(t for t in data["tables"] if t["id"] == "timing_trim_a")
    trim["decode"]["mask"] = mask
    _refuse(data, match)


FUEL_COMP_IDS = {"fuel_compensation", "fuel_compensation_maps_2_5"}


def test_shipped_map_flags_uprevs_swapped_fuel_comp_labels_on_every_copy_and_nowhere_else() -> None:
    """UpRev's Fuel Comp grid matches memory cell for cell; only its two
    breakpoint lists are swapped (operator check 2026-10-04). That is data on
    both Fuel Comp copies, so every slot's view warns, and on nothing else."""
    rom_map = load_rom_map(PROFILE)
    flagged = {t.id for t in rom_map.tables if t.editor_disagreement is not None}
    assert flagged == FUEL_COMP_IDS
    records = {t.id: t.editor_disagreement for t in rom_map.tables if t.id in FUEL_COMP_IDS}
    assert len({r.model_dump_json() for r in records.values() if r is not None}) == 1
    record = records["fuel_compensation"]
    assert record is not None
    assert record.editor == "UpRev"
    assert record.positions_match is True
    assert "0xCB29" in record.editor_row_labels and "RPM" in record.editor_row_labels
    assert "0xCACD" in record.editor_col_labels
    assert "Volumetric Flow Ratio" in record.editor_col_labels
    assert "x 128" in record.translation and "/ 128" in record.translation


def _fuel_comp(data: dict[str, Any]) -> dict[str, Any]:
    (table,) = [t for t in data["tables"] if t["id"] == "fuel_compensation"]
    return table


@pytest.mark.parametrize("missing", ["rows", "cols"])
def test_editor_disagreement_needs_both_axes(missing: str) -> None:
    data = _shipped()
    del _fuel_comp(data)["axes"][missing]
    _refuse(data, "editor_disagreement .* needs both axes.rows and axes.cols")


def test_editor_disagreement_is_refused_on_a_table_without_axes() -> None:
    data = _shipped()
    (k,) = [t for t in data["tables"] if t["id"] == "k_constant"]
    k["editor_disagreement"] = copy.deepcopy(_fuel_comp(data)["editor_disagreement"])
    _refuse(data, "editor_disagreement")


@pytest.mark.parametrize(
    "field",
    [
        "editor",
        "editor_row_labels",
        "editor_col_labels",
        "positions_match",
        "consequence",
        "translation",
        "evidence",
    ],
)
def test_editor_disagreement_fields_are_required(field: str) -> None:
    data = _shipped()
    record = copy.deepcopy(_fuel_comp(data)["editor_disagreement"])
    del record[field]
    _fuel_comp(data)["editor_disagreement"] = record
    _refuse(data, field)
