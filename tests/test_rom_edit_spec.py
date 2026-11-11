"""E08.1: the edit spec, on synthetic images.

No ROM bytes are committed: the base is ``tests.test_rom_effects``'s modelled
synthetic image (``tests.rom_fixtures`` noise in the Xterra's layout) with
hand-picked Fuel Comp axes and cells written in, so every expected cell below
is worked by hand from those numbers:

- rpm axis ``fuel_comp_engine_speed_axis`` (rows) = :data:`RPM_AXIS_RAW`, x 50
  rpm: 600, 800, 1000, 1200, 1400, 1600, 1800, 2000, 2400, ...;
- fill axis ``fuel_comp_flow_axis`` (columns) = 16, 24, ..., 128, 144, x
  100/224 %: col 3 = 17.86, col 4 = 21.43, col 5 = 25.00, col 6 = 28.57,
  col 7 = 32.14;
- every slot-1 Fuel Comp cell 128 except :data:`SPECIAL`;
- slot-1 ``timing_trim_a`` row 0 starts :data:`TRIM_ROW_0` (then 0x14);
  rows 1..15 alternate flagged 0x94 and plain 0x14.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.analysis.ecu_application import ApplicationModel, load_application_model, program_sha256
from src.analysis.fuel_comp_inputs import load_fuel_comp_declaration
from src.analysis.fuel_comp_rom import RevisionImageResolver
from src.analysis.rom_edit_spec import (
    SCHEMA_REASONS,
    BaseImage,
    EditSpec,
    EditSpecRefusalError,
    build_candidate,
    load_candidate_file,
    load_edit_spec,
    parse_edit_spec,
    resolve_base,
    resolve_edits,
)
from src.analysis.rom_effects import CellEdit, RomEffectRefusalError, write_candidate
from src.analysis.rom_map import RomMap, TableAxes
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry
from src.yaml_load import load_yaml
from tests.rom_fixtures import (
    PROFILE,
    encrypted_backup_lookalike,
    load_byte_image,
    osiris_container,
    xterra_map,
)
from tests.test_rom_effects import modelled_image

FIXTURES = Path(__file__).parent / "fixtures" / "rom_edit_spec"
RPM_AXIS_RAW = [12, 16, 20, 24, 28, 32, 36, 40, 48, 56, 64, 72, 80, 96, 112, 128]
#: (row, col) -> raw, everything else in slot 1 is 128.
SPECIAL = {(0, 0): 10, (0, 1): 100, (0, 2): 125, (0, 3): 50}
#: Slot-1 ``timing_trim_a`` row 0 (decode mask 0x7F, bit 7 a flag the ECM
#: strips): flagged 5, plain 5, flagged 127, plain 0.
TRIM_ROW_0 = (0x85, 0x05, 0xFF, 0x00)


def hand_worked_image(model: ApplicationModel, rom_map: RomMap) -> bytes:
    """The modelled synthetic image with the module docstring's axes and cells."""
    image = bytearray(modelled_image(model))
    rpm_axis = rom_map.table("fuel_comp_engine_speed_axis")
    assert rpm_axis.offset is not None
    image[rpm_axis.offset : rpm_axis.offset + 16] = bytes(RPM_AXIS_RAW)
    fuel_comp = rom_map.table("fuel_compensation")
    start = rom_map.table_address(fuel_comp, 1)
    cells = [SPECIAL.get((r, c), 128) for r in range(16) for c in range(16)]
    image[start : start + 256] = bytes(cells)
    trim = rom_map.table_address(rom_map.table("timing_trim_a"), 1)
    rest = [0x94 if (r + c) % 2 else 0x14 for r in range(1, 16) for c in range(16)]
    image[trim : trim + 256] = bytes([*TRIM_ROW_0, *(0x14,) * 12, *rest])
    return bytes(image)


def repinned(model: ApplicationModel, image: bytes) -> ApplicationModel:
    """``model`` with its program identity pinned to ``image``'s synthetic
    program spans (the stand-in for "the same program")."""
    identity = model.program.identity.model_copy(update={"sha256": program_sha256(image, model)})
    return model.model_copy(
        update={"program": model.program.model_copy(update={"identity": identity})}
    )


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    model, sha = load_application_model(PROFILE)
    rom_map = xterra_map()
    plain = hand_worked_image(model, rom_map)
    root = tmp_path_factory.mktemp("rom_edit_spec")
    (root / "base.bin").write_bytes(plain)
    return {
        "image": plain,
        "sha": hashlib.sha256(plain).hexdigest(),
        "map": rom_map,
        "model": repinned(model, plain),
        "model_sha": sha,
        "root": root,
    }


def _spec(world: dict[str, Any], *edits: dict[str, Any], **top: Any) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "base": {"file": "base.bin", "sha256": world["sha"]},
        "label": "test",
        "edits": list(edits),
        **top,
    }


def _resolve(world: dict[str, Any], *edits: dict[str, Any]) -> list[CellEdit]:
    return resolve_edits(parse_edit_spec(_spec(world, *edits)), world["map"], world["image"])


def _base(world: dict[str, Any]) -> BaseImage:
    spec = parse_edit_spec(_spec(world, _FC_SET))
    return resolve_base(spec.base, world["map"], spec_dir=world["root"])


def _build(world: dict[str, Any], spec: EditSpec, base: BaseImage | None = None) -> Any:
    return build_candidate(
        spec,
        world["map"],
        base or _base(world),
        model=world["model"],
        model_sha256=world["model_sha"],
        slot=1,
        slot_source="default_slot",
    )


def _reason(call: Any) -> str:
    """The refusal's reason. ``EditSpecRefusalError`` is a
    ``RomEffectRefusalError``, so one catch covers this module's own refusals
    and ``make_candidate``'s (``value_out_of_range``)."""
    with pytest.raises(RomEffectRefusalError) as refused:
        call()
    return refused.value.reason


_FC_SET = {"table": "fuel_compensation", "slot": 1, "cells": [[3, 4]], "set_raw": 140}
_TT = {"table": "timing_trim_a", "slot": 1}
#: Rows 2..6 (1000..1800 rpm, both ends exactly on a breakpoint) x columns
#: 4..6 (21.43, 25.00, 28.57 %).
_REGION = {
    "rpm": [1000, 1800],
    "load": [20, 30],
    "units": {"rpm": "rpm", "load": "%"},
}
_REGION_CELLS = {(r, c) for r in range(2, 7) for c in range(4, 7)}


# -- selectors and operations, by hand ------------------------------------------------------


def test_cells_set_raw_names_exactly_its_cells(world: dict[str, Any]) -> None:
    edits = _resolve(
        world, {"table": "fuel_compensation", "slot": 1, "cells": [[3, 4], [0, 0]], "set_raw": 140}
    )
    assert edits == [
        CellEdit("fuel_compensation", 3, 4, 140, slot=1),
        CellEdit("fuel_compensation", 0, 0, 140, slot=1),
    ]


def test_all_add_raw_on_an_absolute_axis_table(world: dict[str, Any]) -> None:
    edits = _resolve(world, {"table": "fuel_comp_flow_axis", "all": True, "add_raw": 1})
    flow = [*range(16, 136, 8), 144]
    assert [(e.row, e.col, e.raw, e.slot) for e in edits] == [
        (0, col, value + 1, None) for col, value in enumerate(flow)
    ]


def test_a_fuel_comp_region_selects_rpm_rows_and_fill_columns(world: dict[str, Any]) -> None:
    """rpm bounds rows through ``fuel_comp_engine_speed_axis``, load bounds
    columns through ``fuel_comp_flow_axis``: 5 x 3 cells, all 128 -> 131."""
    edits = _resolve(
        world,
        {
            "table": "fuel_compensation",
            "slot": 1,
            "region": _REGION,
            "scale": 1.02,
            "rounding": "nearest",
        },
    )
    assert {(e.row, e.col) for e in edits} == _REGION_CELLS
    assert {e.raw for e in edits} == {131}


def test_the_region_test_fails_under_transposed_axes(world: dict[str, Any]) -> None:
    """Mutation check: swap the table's axes (UpRev's labelling) and the same
    region selects rows 4..6 x columns 2..6, so the hand-worked set above
    would not match."""
    rom_map: RomMap = world["map"]
    tables = [
        t.model_copy(
            update={
                "axes": TableAxes(rows="fuel_comp_flow_axis", cols="fuel_comp_engine_speed_axis")
            }
        )
        if t.id == "fuel_compensation"
        else t
        for t in rom_map.tables
    ]
    transposed = rom_map.model_copy(update={"tables": tables})
    spec = parse_edit_spec(
        _spec(
            world,
            {
                "table": "fuel_compensation",
                "slot": 1,
                "region": _REGION,
                "scale": 1.02,
                "rounding": "nearest",
            },
        )
    )
    picked = {(e.row, e.col) for e in resolve_edits(spec, transposed, world["image"])}
    assert picked == {(r, c) for r in range(4, 7) for c in range(2, 7)}
    assert picked != _REGION_CELLS


def test_region_bounds_are_inclusive_and_one_sided(world: dict[str, Any]) -> None:
    # 25 % is raw 56 x 100/224 exactly on a breakpoint: inclusive keeps it.
    only = _resolve(
        world,
        {
            "table": "fuel_compensation",
            "slot": 1,
            "region": {"load": [25, 25], "units": {"load": "%"}},
            "add_raw": 1,
        },
    )
    assert {(e.row, e.col) for e in only} == {(r, 5) for r in range(16)}
    rows = _resolve(
        world,
        {
            "table": "fuel_compensation",
            "slot": 1,
            "region": {"rpm": [600, 800], "units": {"rpm": "rpm"}},
            "add_raw": 1,
        },
    )
    assert {(e.row, e.col) for e in rows} == {(r, c) for r in (0, 1) for c in range(16)}


@pytest.mark.parametrize(
    ("rounding", "expected"), [("nearest", 131), ("floor", 130), ("ceil", 131)]
)
def test_scale_rounding_is_declared_and_exact(
    world: dict[str, Any], rounding: str, expected: int
) -> None:
    """128 x 1.02 = 130.56."""
    (edit,) = _resolve(
        world,
        {
            "table": "fuel_compensation",
            "slot": 1,
            "cells": [[1, 1]],
            "scale": 1.02,
            "rounding": rounding,
        },
    )
    assert edit.raw == expected


def test_scale_is_decimal_exact_and_nearest_rounds_half_up(world: dict[str, Any]) -> None:
    # 100 x 1.1 is 110 exactly; float says 110.00000000000001, whose ceil is 111.
    assert 100 * 1.1 > 110
    (ceil,) = _resolve(
        world,
        {
            "table": "fuel_compensation",
            "slot": 1,
            "cells": [[0, 1]],
            "scale": 1.1,
            "rounding": "ceil",
        },
    )
    assert ceil.raw == 110
    # 125 x 1.02 = 127.5, a true tie.
    for rounding, expected in (("nearest", 128), ("floor", 127)):
        (tie,) = _resolve(
            world,
            {
                "table": "fuel_compensation",
                "slot": 1,
                "cells": [[0, 2]],
                "scale": 1.02,
                "rounding": rounding,
            },
        )
        assert tie.raw == expected


def test_a_scale_that_leaves_some_cells_is_not_a_no_op(world: dict[str, Any]) -> None:
    """10 x 1.02 = 10.2 rounds back to 10; 128 -> 131. The unchanged cell is
    recorded in the provenance, not edited."""
    spec = parse_edit_spec(
        _spec(
            world,
            {
                "table": "fuel_compensation",
                "slot": 1,
                "cells": [[0, 0], [1, 1]],
                "scale": 1.02,
                "rounding": "nearest",
            },
        )
    )
    assert resolve_edits(spec, world["map"], world["image"]) == [
        CellEdit("fuel_compensation", 1, 1, 131, slot=1)
    ]
    (record,) = _build(world, spec).provenance["spec_edits"]
    assert record["unchanged"] == [{"row": 0, "col": 0, "old": 10, "new": 10}]
    assert record["changed"] == [{"row": 1, "col": 1, "old": 128, "new": 131}]


def test_a_tie_with_an_even_floor_rounds_half_up(world: dict[str, Any]) -> None:
    """50 x 1.01 = 50.5: half-up gives 51, half-even would give 50."""
    (tie,) = _resolve(
        world,
        {
            "table": "fuel_compensation",
            "slot": 1,
            "cells": [[0, 3]],
            "scale": 1.01,
            "rounding": "nearest",
        },
    )
    assert tie.raw == 51


def test_arithmetic_on_a_masked_trim_keeps_the_flag(world: dict[str, Any]) -> None:
    """add_raw -3 on 0x85 (flag, 5) gives 0x82 and on 0x05 gives 0x02; a
    scale x2 on 0x85 gives 0x8A. Only the value bits move."""
    retard = _resolve(world, {**_TT, "cells": [[0, 0], [0, 1]], "add_raw": -3})
    assert [(e.row, e.col, e.raw) for e in retard] == [(0, 0, 0x82), (0, 1, 0x02)]
    (doubled,) = _resolve(world, {**_TT, "cells": [[0, 0]], "scale": 2, "rounding": "floor"})
    assert doubled.raw == 0x8A
    # A 3-count retard across rows 1..15 (the N01 spark-trim shape): rows
    # alternate flagged 0x94 / plain 0x14, and every flag survives.
    rows = [[r, c] for r in range(1, 16) for c in range(16)]
    edits = _resolve(world, {**_TT, "cells": rows, "add_raw": -3})
    assert len(edits) == 240
    assert sum(1 for e in edits if e.raw == 0x91) == 120
    assert sum(1 for e in edits if e.raw == 0x11) == 120


def test_set_raw_that_keeps_the_flag_is_allowed(world: dict[str, Any]) -> None:
    (edit,) = _resolve(world, {**_TT, "cells": [[0, 0]], "set_raw": 0x90})
    assert edit.raw == 0x90
    with pytest.raises(EditSpecRefusalError) as refused:
        _resolve(world, {**_TT, "cells": [[0, 0]], "set_raw": 0x05})
    assert refused.value.reason == "masked_bits_changed"
    assert "timing_trim_a [0, 0]" in refused.value.message and "0x85" in refused.value.message


def test_a_gapped_mask_refuses_arithmetic(world: dict[str, Any]) -> None:
    rom_map: RomMap = world["map"]
    tables = []
    for t in rom_map.tables:
        if t.id == "timing_trim_a":
            assert t.decode is not None
            t = t.model_copy(update={"decode": t.decode.model_copy(update={"mask": 0x5F})})
        tables.append(t)
    gapped = rom_map.model_copy(update={"tables": tables})
    spec = parse_edit_spec(_spec(world, {**_TT, "cells": [[0, 0]], "add_raw": 1}))
    assert _reason(lambda: resolve_edits(spec, gapped, world["image"])) == (
        "arithmetic_on_masked_cells"
    )


def test_a_breakpoint_inexact_in_float_is_still_inside_its_exact_bound(
    world: dict[str, Any],
) -> None:
    """Fill raw 24 decodes to 24 x 0.44642857142857145 = 10.714285714285715 in
    float; the exact value 75/7 is 10.714285714285714. A bound written as the
    exact value keeps column 1."""
    assert 24 * 0.44642857142857145 > 75 / 7
    edits = _resolve(
        world,
        {
            "table": "fuel_compensation",
            "slot": 1,
            "region": {"load": [75 / 7, 75 / 7], "units": {"load": "%"}},
            "add_raw": 1,
        },
    )
    assert {e.col for e in edits} == {1}


# -- refusals, by name ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"base": {"revision": "r", "file": "f", "sha256": "0" * 64}}, "base_ambiguous"),
        ({"base": {}}, "base_ambiguous"),
        ({"base": {"revision": "r", "sha256": "0" * 64}}, "base_ambiguous"),
        ({"base": {"file": "base.bin"}}, "base_sha256_required"),
        ({"edits": []}, "no_edits"),
        (
            {"edits": [{"table": "fuel_compensation", "slot": 1, "set_raw": 1}]},
            "selector_ambiguous",
        ),
        (
            {"edits": [{"table": "t", "all": True, "cells": [[0, 0]], "set_raw": 1}]},
            "selector_ambiguous",
        ),
        ({"edits": [{"table": "t", "all": True}]}, "operation_ambiguous"),
        (
            {"edits": [{"table": "t", "all": True, "set_raw": 1, "add_raw": 1}]},
            "operation_ambiguous",
        ),
        ({"edits": [{"table": "t", "all": True, "scale": 1.02}]}, "rounding_undeclared"),
        (
            {"edits": [{"table": "t", "all": True, "set_raw": 1, "rounding": "floor"}]},
            "rounding_without_scale",
        ),
        ({"edits": [{"table": "t", "region": {"units": {}}, "set_raw": 1}]}, "region_unbounded"),
        (
            {
                "edits": [
                    {"table": "t", "region": {"rpm": [2, 1], "units": {"rpm": "rpm"}}, "set_raw": 1}
                ]
            },
            "region_inverted",
        ),
        ({"edits": [{"table": "t", "region": {"rpm": [1, 2]}, "set_raw": 1}]}, "unit_undeclared"),
        (
            {
                "edits": [
                    {
                        "table": "t",
                        "region": {"rpm": [1, 2], "units": {"rpm": "rpm", "load": "%"}},
                        "set_raw": 1,
                    }
                ]
            },
            "unit_undeclared",
        ),
        ({"comment": "an unknown key"}, "spec_invalid"),
        ({"schema_version": 2}, "spec_invalid"),
        ({"edits": [{"table": "t", "all": True, "set_raw": "1"}]}, "spec_invalid"),
        ({"edits": [{"table": "t", "all": False, "set_raw": 1}]}, "spec_invalid"),
        ({"edits": [{"table": "t", "all": True, "scale": 0, "rounding": "floor"}]}, "spec_invalid"),
    ],
)
def test_schema_refusals_are_named(
    world: dict[str, Any], change: dict[str, Any], reason: str
) -> None:
    assert _reason(lambda: parse_edit_spec({**_spec(world, _FC_SET), **change})) == reason


def test_every_named_schema_reason_is_exercised() -> None:
    """The parametrised table above covers each reason the schema can raise."""
    named = {
        "base_ambiguous",
        "base_sha256_required",
        "no_edits",
        "selector_ambiguous",
        "operation_ambiguous",
        "rounding_undeclared",
        "rounding_without_scale",
        "region_unbounded",
        "region_inverted",
        "unit_undeclared",
    }
    assert named == SCHEMA_REASONS


@pytest.mark.parametrize(
    ("edits", "reason"),
    [
        ([{"table": "not_a_table", "all": True, "set_raw": 1}], "table_unknown"),
        ([{"table": "fuel_compensation", "all": True, "set_raw": 1}], "slot_invalid"),
        ([{"table": "fuel_compensation", "slot": 2, "all": True, "set_raw": 1}], "slot_invalid"),
        ([{"table": "fuel_comp_flow_axis", "slot": 1, "all": True, "set_raw": 1}], "slot_invalid"),
        ([{**_FC_SET, "cells": [[16, 0]]}], "cell_outside_table"),
        ([{**_FC_SET, "cells": [[1, 1], [1, 1]]}], "duplicate_edit"),
        ([_FC_SET, {**_FC_SET, "cells": [[2, 2], [3, 4]], "set_raw": 1}], "duplicate_edit"),
        (
            [
                {
                    "table": "fuel_compensation",
                    "slot": 1,
                    "region": {"rpm": [810, 990], "units": {"rpm": "rpm"}},
                    "set_raw": 1,
                }
            ],
            "region_empty",
        ),
        (
            [
                {
                    "table": "fuel_compensation",
                    "slot": 1,
                    "region": {"load": [20, 30], "units": {"load": "raw"}},
                    "set_raw": 1,
                }
            ],
            "unit_mismatch",
        ),
        (
            [
                {
                    "table": "fuel_comp_flow_axis",
                    "region": {"rpm": [0, 9000], "units": {"rpm": "rpm"}},
                    "set_raw": 1,
                }
            ],
            "axis_not_found",
        ),
        ([{**_FC_SET, "cells": [[1, 1]], "set_raw": 128}], "no_op_edit"),
        ([{**_FC_SET, "cells": [[1, 1]], "set_raw": 256}], "value_out_of_range"),
        ([{**_FC_SET, "cells": [[1, 1]], "set_raw": None, "add_raw": 200}], "value_out_of_range"),
        ([{**_FC_SET, "cells": [[0, 0]], "set_raw": None, "add_raw": -11}], "value_out_of_range"),
        (
            [{**_FC_SET, "cells": [[1, 1]], "set_raw": None, "scale": 2.5, "rounding": "floor"}],
            "value_out_of_range",
        ),
        # Masked timing trims (0x7F): row 0 is 0x85, 0x05, 0xFF, 0x00.
        ([{**_TT, "cells": [[0, 2]], "add_raw": 1}], "value_out_of_range"),
        ([{**_TT, "cells": [[0, 3]], "add_raw": -1}], "value_out_of_range"),
        ([{**_TT, "cells": [[0, 0]], "scale": 30, "rounding": "floor"}], "value_out_of_range"),
        ([{**_TT, "cells": [[0, 0]], "set_raw": 0x05}], "masked_bits_changed"),
        ([{**_TT, "cells": [[0, 1]], "set_raw": 0x85}], "masked_bits_changed"),
        ([{**_TT, "all": True, "set_raw": 50}], "masked_bits_changed"),
        # A only leaves [0, 0] (10 x 1.02 rounds back) so make_candidate never
        # sees it; B sets it. Only the spec's own overlap check catches this.
        (
            [
                {
                    **_FC_SET,
                    "cells": [[0, 0], [1, 1]],
                    "set_raw": None,
                    "scale": 1.02,
                    "rounding": "nearest",
                },
                {**_FC_SET, "cells": [[0, 0]]},
            ],
            "duplicate_edit",
        ),
    ],
)
def test_resolution_refusals_are_named(
    world: dict[str, Any], edits: list[dict[str, Any]], reason: str
) -> None:
    clean = [{k: v for k, v in e.items() if v is not None} for e in edits]
    assert _reason(lambda: _resolve(world, *clean)) == reason


def test_value_out_of_range_comes_from_make_candidate_and_is_never_clamped(
    world: dict[str, Any],
) -> None:
    with pytest.raises(RomEffectRefusalError) as refused:
        _resolve(world, {**_FC_SET, "cells": [[1, 1]], "set_raw": 300})
    assert refused.value.reason == "value_out_of_range"
    assert "300 does not fit a u8 cell" in refused.value.message


def test_a_main_calibration_word_edits_at_its_absolute_offset(world: dict[str, Any]) -> None:
    """A 1 x 1 table outside the map slots takes ``cells: [[0, 0]]`` with no
    slot, and the candidate differs from the base in that word alone."""
    spec = parse_edit_spec(
        _spec(world, {"table": "first_gear_ratio_threshold", "cells": [[0, 0]], "set_raw": 0x7FFF})
    )
    made = _build(world, spec)
    changed = [i for i, (a, b) in enumerate(zip(world["image"], made.image, strict=True)) if a != b]
    assert set(changed) <= {0x9308, 0x9309} and changed
    assert made.image[0x9308:0x930A] == b"\x7f\xff"


@pytest.mark.parametrize(
    ("table", "raw"),
    [
        ("first_gear_torque_cap", 0x8000),  # a negative torque limit to a signed compare
        ("first_gear_ratio_threshold", 0xFFFF),
        ("output_shaft_speed_constant", 0),  # a divisor
    ],
)
def test_a_value_outside_a_tables_raw_range_is_refused_not_clamped(
    world: dict[str, Any], table: str, raw: int
) -> None:
    with pytest.raises(RomEffectRefusalError) as refused:
        _resolve(world, {"table": table, "cells": [[0, 0]], "set_raw": raw})
    assert refused.value.reason == "value_out_of_range"
    assert f"{raw} is outside {table}'s raw_range" in refused.value.message


def test_a_scale_that_rounds_every_cell_back_names_the_cells(world: dict[str, Any]) -> None:
    with pytest.raises(EditSpecRefusalError) as refused:
        _resolve(
            world,
            {
                "table": "fuel_compensation",
                "slot": 1,
                "cells": [[1, 1], [0, 0]],
                "scale": 1.001,
                "rounding": "nearest",
            },
        )
    assert refused.value.reason == "no_op_edit"
    assert "[1, 1]=128" in refused.value.message and "[0, 0]=10" in refused.value.message


def test_a_region_through_an_unverified_axis_is_refused(world: dict[str, Any]) -> None:
    rom_map: RomMap = world["map"]
    tables = [
        t.model_copy(update={"confidence": "inferred"}) if t.id == "fuel_comp_flow_axis" else t
        for t in rom_map.tables
    ]
    unverified = rom_map.model_copy(update={"tables": tables})
    spec = parse_edit_spec(
        _spec(world, {"table": "fuel_compensation", "slot": 1, "region": _REGION, "add_raw": 1})
    )
    assert _reason(lambda: resolve_edits(spec, unverified, world["image"])) == "axis_unverified"
    # cells and all never read an axis, so they stay usable on that map.
    cells = parse_edit_spec(_spec(world, _FC_SET))
    assert resolve_edits(cells, unverified, world["image"])


# -- the base -------------------------------------------------------------------------------


def test_a_file_base_is_hash_checked(world: dict[str, Any], tmp_path: Path) -> None:
    rom_map = world["map"]
    base = _base(world)
    assert base.image == world["image"]
    assert base.identity == {
        "source": "file",
        "revision": None,
        "file": "base.bin",
        "image_sha256": world["sha"],
        "image_format": "plain",
        "container_sha256": None,
    }
    wrong = parse_edit_spec(
        {**_spec(world, _FC_SET), "base": {"file": "base.bin", "sha256": "1" * 64}}
    )
    assert (
        _reason(lambda: resolve_base(wrong.base, rom_map, spec_dir=world["root"]))
        == "image_hash_mismatch"
    )
    missing = parse_edit_spec(
        {**_spec(world, _FC_SET), "base": {"file": "absent.bin", "sha256": world["sha"]}}
    )
    assert (
        _reason(lambda: resolve_base(missing.base, rom_map, spec_dir=world["root"]))
        == "image_not_found"
    )
    # An .OsirisROM base whose spec quotes the plain hash.
    (tmp_path / "base.OsirisROM").write_bytes(osiris_container(world["image"]))
    osiris = parse_edit_spec(
        {**_spec(world, _FC_SET), "base": {"file": "base.OsirisROM", "sha256": world["sha"]}}
    )
    loaded = resolve_base(osiris.base, rom_map, spec_dir=tmp_path)
    assert loaded.image == world["image"] and loaded.identity["image_format"] == "osirisrom"


def test_the_uprev_backup_is_refused_by_name(world: dict[str, Any], tmp_path: Path) -> None:
    (tmp_path / "backup.bin").write_bytes(encrypted_backup_lookalike())
    spec = parse_edit_spec(
        {**_spec(world, _FC_SET), "base": {"file": "backup.bin", "sha256": world["sha"]}}
    )
    with pytest.raises(EditSpecRefusalError) as refused:
        resolve_base(spec.base, world["map"], spec_dir=tmp_path)
    assert refused.value.reason == "image_unreadable"
    assert "UpRev encrypted ROM backup" in refused.value.message
    with pytest.raises(EditSpecRefusalError) as refused:
        load_candidate_file(
            tmp_path / "backup.bin",
            world["map"],
            base=_base(world),
            model=world["model"],
            model_sha256=world["model_sha"],
            slot=1,
            slot_source="default_slot",
        )
    assert refused.value.reason == "image_unreadable"
    assert "UpRev encrypted ROM backup" in refused.value.message


def test_a_revision_base_resolves_through_the_registry_by_plain_sha256(tmp_path: Path) -> None:
    image = load_byte_image()
    digest = hashlib.sha256(image).hexdigest()
    registry = TuneRevisionRegistry(
        revisions=[
            TuneRevision(revision="lost", flashed_at=datetime(2026, 8, 1, tzinfo=UTC)),
            TuneRevision(
                revision="synthetic-a",
                flashed_at=datetime(2026, 8, 2, tzinfo=UTC),
                rom_file="synthetic-a.OsirisROM",
                image_sha256=digest,
            ),
        ]
    )
    (tmp_path / "synthetic-a.OsirisROM").write_bytes(osiris_container(image))
    declaration = load_fuel_comp_declaration(PROFILE)
    rom_map = xterra_map()
    resolver = RevisionImageResolver(
        registry, rom_map, declaration.rom, rom_dirs=[tmp_path], slot=declaration.program.map_slot
    )

    def base(revision: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "base": {"revision": revision},
            "label": "x",
            "edits": [_FC_SET],
        }

    spec = parse_edit_spec(base("synthetic-a"))
    resolved = resolve_base(spec.base, rom_map, spec_dir=tmp_path, resolver=resolver)
    assert resolved.image == image
    assert resolved.identity["revision"] == "synthetic-a"
    assert resolved.identity["image_sha256"] == digest
    assert resolved.identity["file"] == "synthetic-a.OsirisROM"
    for revision, reason in (("lost", "no_surviving_image"), ("nope", "revision_unknown")):
        named = parse_edit_spec(base(revision))
        assert (
            _reason(lambda: resolve_base(named.base, rom_map, spec_dir=tmp_path, resolver=resolver))  # noqa: B023
            == reason
        )
    assert (
        _reason(lambda: resolve_base(spec.base, rom_map, spec_dir=tmp_path))
        == "revision_resolver_missing"
    )


def test_a_spec_is_never_applied_to_another_base(world: dict[str, Any], tmp_path: Path) -> None:
    other = bytearray(world["image"])
    other[0x8100] ^= 0xFF
    (tmp_path / "other.bin").write_bytes(bytes(other))
    other_spec = parse_edit_spec(
        {
            **_spec(world, _FC_SET),
            "base": {"file": "other.bin", "sha256": hashlib.sha256(bytes(other)).hexdigest()},
        }
    )
    other_base = resolve_base(other_spec.base, world["map"], spec_dir=tmp_path)
    spec = parse_edit_spec(_spec(world, _FC_SET))
    assert _reason(lambda: _build(world, spec, other_base)) == "base_mismatch"


# -- candidates and provenance --------------------------------------------------------------


def test_the_example_spec_parses_and_resolves(world: dict[str, Any]) -> None:
    spec = load_edit_spec(FIXTURES / "fuel_comp_light_load.yaml")
    assert spec.base.revision == "ZS44A-20261004"
    edits = resolve_edits(spec, world["map"], world["image"])
    assert {(e.row, e.col) for e in edits} == _REGION_CELLS | {(0, 0)}
    assert {(e.row, e.col): e.raw for e in edits}[(0, 0)] == 8


def test_the_spec_hash_is_over_content_not_layout(world: dict[str, Any], tmp_path: Path) -> None:
    text = (FIXTURES / "fuel_comp_light_load.yaml").read_text()
    reflowed = tmp_path / "reflowed.yaml"
    reflowed.write_text("# another comment\n" + yaml.safe_dump(load_yaml(text)))
    first = load_edit_spec(FIXTURES / "fuel_comp_light_load.yaml")
    assert load_edit_spec(reflowed).sha256() == first.sha256()
    changed = load_yaml(text)
    changed["edits"][1]["add_raw"] = -3
    assert parse_edit_spec(changed).sha256() != first.sha256()
    broken = tmp_path / "broken.yaml"
    broken.write_text("edits: [")
    assert _reason(lambda: load_edit_spec(broken)) == "spec_invalid"


def test_a_spec_candidate_records_cells_ranges_spec_and_base(
    world: dict[str, Any], tmp_path: Path
) -> None:
    spec = parse_edit_spec(
        _spec(
            world,
            {
                "table": "fuel_compensation",
                "slot": 1,
                "region": _REGION,
                "scale": 1.02,
                "rounding": "nearest",
            },
            _FC_SET | {"cells": [[0, 0]]},
        )
    )
    candidate = _build(world, spec)
    record = candidate.provenance
    assert record["route"] == "edit_spec" and record["flashable"] is False
    assert record["spec_sha256"] == spec.sha256()
    assert record["base"]["image_sha256"] == world["sha"] == record["base_sha256"]
    assert record["candidate_sha256"] == candidate.image_sha256
    cells = {(c["row"], c["col"]): (c["old"], c["new"]) for c in record["cells"]}
    assert cells == {**{rc: (128, 131) for rc in _REGION_CELLS}, (0, 0): (10, 140)}
    assert [r["what"] for r in record["ranges"]] == [["fuel_compensation (slot 1)"]] * len(
        record["ranges"]
    )
    assert sum(r["changed"] for r in record["ranges"]) == 16
    assert record["effect"]["changed_nodes"] == ["fuel_comp_lookup"]
    region = record["spec_edits"][0]["region"]
    assert region["rpm"]["axis"] == "fuel_comp_engine_speed_axis"
    assert region["rpm"]["role"] == "rows"
    assert region["rpm"]["breakpoints"] == [1000.0, 1200.0, 1400.0, 1600.0, 1800.0]
    assert region["load"]["axis"] == "fuel_comp_flow_axis"
    assert region["load"]["indices"] == [4, 5, 6]
    written = write_candidate(candidate, tmp_path / "sim", rom_library_dir=tmp_path / "lib")
    assert (written / "image.sim").read_bytes() == candidate.image


def test_the_file_route_and_the_spec_route_make_the_same_candidate(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """An ``.OsirisROM`` carrying the bytes a spec makes: same sha256, same
    cells, same ranges, same provenance keys."""
    spec = parse_edit_spec(
        _spec(
            world,
            {
                "table": "fuel_compensation",
                "slot": 1,
                "region": _REGION,
                "scale": 1.02,
                "rounding": "nearest",
            },
            {"table": "fuel_comp_flow_axis", "cells": [[0, 7]], "add_raw": 1},
        )
    )
    from_spec = _build(world, spec)
    saved = tmp_path / "edited-in-uprev.OsirisROM"
    saved.write_bytes(osiris_container(from_spec.image))
    from_file = load_candidate_file(
        saved,
        world["map"],
        base=_base(world),
        model=world["model"],
        model_sha256=world["model_sha"],
        slot=1,
        slot_source="default_slot",
    )
    assert from_file.image_sha256 == from_spec.image_sha256
    a, b = from_spec.provenance, from_file.provenance
    assert a.keys() == b.keys()
    assert a["cells"] == b["cells"] and len(a["cells"]) == 16
    assert a["ranges"] == b["ranges"]
    assert a["effect"] == b["effect"]
    assert b["route"] == "candidate_file" and b["spec_sha256"] is None
    assert b["candidate_file"]["format"] == "osirisrom"


def test_an_identical_candidate_file_is_the_negative_control(world: dict[str, Any]) -> None:
    same = load_candidate_file(
        world["root"] / "base.bin",
        world["map"],
        base=_base(world),
        model=world["model"],
        model_sha256=world["model_sha"],
        slot=1,
        slot_source="default_slot",
    )
    assert same.image_sha256 == world["sha"]
    assert same.provenance["cells"] == [] and same.provenance["ranges"] == []
    assert same.provenance["effect"]["outcome"] == "identical"


def test_an_untraceable_candidate_is_refused_by_compare_roms_reason(
    world: dict[str, Any], tmp_path: Path
) -> None:
    other = bytearray(world["image"])
    other[0x030000] ^= 0x5A  # code changed under the same calibration ID
    (tmp_path / "other-program.bin").write_bytes(bytes(other))
    with pytest.raises(EditSpecRefusalError) as refused:
        load_candidate_file(
            tmp_path / "other-program.bin",
            world["map"],
            base=_base(world),
            model=world["model"],
            model_sha256=world["model_sha"],
            slot=1,
            slot_source="default_slot",
        )
    assert refused.value.reason == "wrong_program"


# -- no write path ---------------------------------------------------------------------------

#: Modules that can transmit on a bus or authorize a non-volatile write.
_WRITE_PATHS = ("src.control", "src.daemon", "src.dashboard", "src.mcp_server", "can", "serial")
_ALLOWED_COLLECTION = {"src.collection", "src.collection.kline", "src.collection.kline.names"}


def test_resolving_and_building_requests_no_ecu_write(tmp_path: Path) -> None:
    script = textwrap.dedent(
        f"""
        import json, sys
        from pathlib import Path
        import src.analysis.rom_edit_spec as spec_module
        from tests.test_rom_edit_spec import FIXTURES, hand_worked_image, repinned
        from src.analysis.ecu_application import load_application_model
        from src.analysis.rom_effects import write_candidate
        from tests.rom_fixtures import PROFILE, xterra_map
        model, sha = load_application_model(PROFILE)
        image = hand_worked_image(model, xterra_map())
        model = repinned(model, image)
        rom_map = xterra_map()
        root = Path({str(tmp_path)!r})
        (root / "base.bin").write_bytes(image)
        loaded = spec_module.load_edit_spec(FIXTURES / "fuel_comp_light_load.yaml")
        spec = spec_module.parse_edit_spec(
            {{**loaded.model_dump(mode="json"),
              "base": {{"file": "base.bin", "sha256": spec_module.sha256_hex(image)}}}}
        )
        base = spec_module.resolve_base(spec.base, rom_map, spec_dir=root)
        candidate = spec_module.build_candidate(
            spec, rom_map, base, model=model, model_sha256=sha, slot=1,
            slot_source="default_slot",
        )
        written = write_candidate(candidate, root / "sim", rom_library_dir=root / "lib")
        spec_module.load_candidate_file(
            written / "image.sim", rom_map, base=base, model=model, model_sha256=sha,
            slot=1, slot_source="default_slot",
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
    assert "src.analysis.rom_edit_spec" in loaded
    reached = [
        m
        for m in loaded
        if any(m == p or m.startswith(p + ".") for p in _WRITE_PATHS)
        or (m.startswith("src.collection") and m not in _ALLOWED_COLLECTION)
    ]
    assert reached == []
