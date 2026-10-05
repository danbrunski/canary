"""The ECU twin contract's language-neutral vectors (E06.1/E06.2).

Builds ``tests/fixtures/ecu_twin/lookup_vectors.json`` (stateless kernels)
and ``state_vectors.json`` (the UpRev selection state), and runs any vector
through the Python reference. ``tests/test_ecu_twin_contract.py`` and
``tests/test_ecu_twin_state_vectors.py`` check that the committed files are
exactly what this builds and that the reference reproduces every vector; the
Rust trial (``rust/ecu_twin_core/tests/vectors.rs``) runs the same files.

Three kinds of vector, named in each vector's ``source``:

- ``hand:`` / ``capture:`` -- known answers worked by hand from the decoded
  arithmetic or read from a capture (copied from the tests and fixtures they
  cite). Their ``pinned`` block is the independent answer; ``out`` must
  contain it. These are the firmware evidence: Python parity alone is not.
- ``generated:`` -- edge cases (axis edges, clamps, rounding, integer
  boundaries, branches, sentinels, reset and missing history) whose ``out``
  is the reference's answer. They make the implementations agree; they do
  not make either right.
- ``kind: control`` -- a wrong table, decode or branch applied to a known
  answer; ``must_differ`` names the answer it must *not* reproduce.

Regenerate after a reviewed change to the reference (and a contract/kernel
version bump where the arithmetic changed)::

    .venv/bin/python -m tests.ecu_twin_vectors
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import yaml

from src.decode import ecm_lookup, fuel_comp_load, target_phi_command

REPO = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO / "data" / "ecu_twin_contract.yaml"
FIXTURE_DIR = REPO / "tests" / "fixtures" / "ecu_twin"
LOOKUP_PATH = FIXTURE_DIR / "lookup_vectors.json"
STATE_PATH = FIXTURE_DIR / "state_vectors.json"
TARGET_PHI_EXPECT = REPO / "tests" / "fixtures" / "ground_truth" / "target_phi.yaml"

FORMAT = "canary-ecu-twin-vectors"

Json = dict[str, Any]


def contract() -> Json:
    data: Json = yaml.safe_load(CONTRACT_PATH.read_text())
    return data


# -- named objects --------------------------------------------------------------

#: ZS44A-20260920 Map 1 (tests/test_ecm_lookup.py, afr_calibration.yaml).
MAP1 = [
    [0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x92],
    [0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x95],
    [0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x95],
    [0x80, 0x80, 0x80, 0x80, 0x8E, 0x90, 0x90, 0x95],
    [0x80, 0x80, 0x80, 0x93, 0x96, 0x95, 0x95, 0x95],
    [0x80, 0x80, 0x90, 0x93, 0x96, 0x99, 0x99, 0x99],
    [0x80, 0x8B, 0x97, 0x99, 0x9D, 0x9B, 0x9B, 0x9B],
    [0x93, 0x98, 0x9D, 0xA0, 0xA3, 0x9F, 0x9F, 0x9F],
]
RPM = [b * 4 for b in (0x18, 0x20, 0x28, 0x38, 0x48, 0x50, 0x58, 0x68)]
LOAD = [0x34, 0x44, 0x5D, 0x64, 0x6C, 0x6E, 0x7F, 0x80]

#: Synthetic curves of tests/test_fuel_comp_load.py.
AREA_AXIS = [100 * i for i in range(32)]
AREA_VALUES = [200 + 30 * i * i for i in range(32)]
FILL_LOW = [
    *(0, 8000, 16000, 22000, 27000, 31000, 34000, 36500),
    *(38500, 40000, 41200, 42200, 43000, 43600, 44000, 44000),
]
FILL_HIGH = [*range(44000, 57001, 1000), 57344, 57344]

#: The profile's gate constants (data/nissan-xterra-2008/target_phi_twin.yaml).
TWIN_CONSTANTS = {
    "flag_bit": 0,
    "load_bits": 8,
    "floor_cell": 128,
    "threshold_cell": 130,
    "hysteresis_cells": 1,
    "cell_to_word_shift": 7,
}

UPREV_MAGIC = 0xAA5555AA


def _rng_table(rng: random.Random, rows: int, columns: int) -> Json:
    def axis(n: int) -> list[int]:
        return sorted(rng.sample(range(0, 0x10000), n))

    return {
        "cells": [[rng.randrange(0x10000) for _ in range(columns)] for _ in range(rows)],
        "row_breakpoints": axis(rows),
        "column_breakpoints": axis(columns),
    }


def named_objects() -> Json:
    rng = random.Random(0xE06)
    transposed = [list(column) for column in zip(*MAP1, strict=True)]
    return {
        "axes": {
            "load": LOAD,
            "rpm": RPM,
            "empty": [],
            "single": [500],
            "repeated": [10, 10, 20, 20, 30],
            "falling": [300, 200, 100],
            "word_edges": [0, 1, 0x7FFF, 0x8000, 0xFFFE, 0xFFFF],
        },
        "tables": {
            "map1": {"cells": MAP1, "row_breakpoints": RPM, "column_breakpoints": LOAD},
            # Rows 0x80 -> 0x84 over the first load span: load 0x38 at the
            # first rpm breakpoint looks up 129, inside the gate's band.
            "hysteresis": {
                "cells": [[128, 132] + [128] * 6 for _ in range(8)],
                "row_breakpoints": RPM,
                "column_breakpoints": LOAD,
            },
            # Wrong-table controls: the same bytes read the wrong way round.
            "map1_transposed": {
                "cells": transposed,
                "row_breakpoints": RPM,
                "column_breakpoints": LOAD,
            },
            "map1_7_rows": {"cells": MAP1[:7], "row_breakpoints": RPM, "column_breakpoints": LOAD},
            "empty_rows": {"cells": [], "row_breakpoints": [], "column_breakpoints": LOAD},
            "one_cell": {"cells": [[0x1234]], "row_breakpoints": [7], "column_breakpoints": [9]},
            "word_cells": _rng_table(rng, 6, 9),
            "word_extremes": {
                "cells": [[0, 0xFFFF, 0], [0xFFFF, 0, 0xFFFF], [0x7FFF, 0x8000, 0x7FFF]],
                "row_breakpoints": [0, 0x8000, 0xFFFF],
                "column_breakpoints": [0, 1, 0xFFFF],
            },
        },
        "curves": {
            "area": {"values": AREA_VALUES, "breakpoints": AREA_AXIS},
            "area_shifted": {"values": AREA_VALUES, "breakpoints": [b + 50 for b in AREA_AXIS]},
            "area_falling": {"values": AREA_VALUES[::-1], "breakpoints": AREA_AXIS},
            "area_short": {"values": AREA_VALUES[:-1], "breakpoints": AREA_AXIS},
            "empty": {"values": [], "breakpoints": []},
        },
        "tables16": {
            "fill_low": FILL_LOW,
            "fill_high": FILL_HIGH,
            "fill_high_60000": [*FILL_HIGH[:15], 60000],
            "fill_low_15": FILL_LOW[:15],
            "word_extremes": [0xFFFF if i % 2 else 0 for i in range(16)],
        },
        "constants": {
            "twin": TWIN_CONSTANTS,
            "flag_bit_32": {**TWIN_CONSTANTS, "flag_bit": 32},
            "load_bits_32": {**TWIN_CONSTANTS, "load_bits": 32},
            "shift_17": {**TWIN_CONSTANTS, "cell_to_word_shift": 17},
            "flag_bit_31_load_16": {**TWIN_CONSTANTS, "flag_bit": 31, "load_bits": 16},
            "negative_band": {**TWIN_CONSTANTS, "threshold_cell": 2, "hysteresis_cells": 5},
        },
        "load_curves": {
            "synthetic": _curves(),
            "axis_repeats": _curves(area_breakpoints=[0, 0, *AREA_AXIS[2:]]),
            "area_falls": _curves(area_values=[300, 200, *AREA_VALUES[2:]]),
            "split_falls": _curves(fill_high=[40000, *FILL_HIGH[1:]]),
            "fill_low_not_word": _curves(fill_low=[*FILL_LOW[:15], 70000]),
            "fill_low_15": _curves(fill_low=FILL_LOW[:15]),
            "numerator_zero": _curves(reciprocal_numerator=0),
            "saturating": _curves(rpm_multiplier=255, reciprocal_numerator=0xFFFFFFFF),
        },
    }


def _curves(**overrides: Any) -> Json:
    fields: Json = {
        "area_breakpoints": AREA_AXIS,
        "area_values": AREA_VALUES,
        "fill_low": FILL_LOW,
        "fill_high": FILL_HIGH,
        "rpm_multiplier": 150,
        "reciprocal_numerator": 10_000_000,
        "idle_air_cap": 1000,
        "limp_home_area": 700,
    }
    fields.update(overrides)
    return fields


# -- the Python reference, per operation ---------------------------------------


def _hit(hit: ecm_lookup.AxisHit | None) -> Json | None:
    if hit is None:
        return None
    return {
        "index": hit.index,
        "numerator": int(hit.numerator),
        "span": int(hit.span),
        "edge": hit.edge,
        "fraction_q16": hit.fraction_q16,
    }


def _lookup(result: ecm_lookup.LookupResult) -> Json:
    return {
        "value": result.value,
        "row": _hit(result.row),
        "column": _hit(result.column),
        "row_fraction": result.row_fraction,
        "column_fraction": result.column_fraction,
        "cells": [list(cell) for cell in result.cells],
    }


#: An UpRev state in a vector: these six fields, in this order (null unknown).
_STATE_FIELDS = ("countdown", "debounce", "selector", "magic", "active_map", "history")


def _uprev_state(state: Any) -> Json:
    return {name: getattr(state, name) for name in _STATE_FIELDS}


def _op_search_axis(i: Json, named: Json) -> Json:
    axis = named["axes"][i["axis"]] if "axis" in i else i["breakpoints"]
    return {"hit": _hit(ecm_lookup.search_axis(axis, i["value"]))}


def _op_interpolate(i: Json, named: Json) -> Json:
    return {"value": ecm_lookup.interpolate_q16(i["a"], i["b"], i["fraction"])}


def _op_lookup_2d(i: Json, named: Json) -> Json:
    t = named["tables"][i["table"]]
    return _lookup(
        ecm_lookup.lookup_2d(
            t["cells"],
            t["row_breakpoints"],
            t["column_breakpoints"],
            i["row_value"],
            i["column_value"],
        )
    )


def _op_curve(i: Json, named: Json) -> Json:
    c = named["curves"][i["curve"]]
    return {"value": ecm_lookup.lookup_curve_q16(c["values"], c["breakpoints"], i["value"])}


def _op_shift12(i: Json, named: Json) -> Json:
    return {"value": ecm_lookup.lookup_shift12_q16(named["tables16"][i["table16"]], i["value"])}


def _op_target_command(i: Json, named: Json) -> Json:
    t = named["tables"][i["table"]]
    k = named["constants"][i["constants"]]
    result = target_phi_command.target_command(
        t["cells"],
        t["row_breakpoints"],
        t["column_breakpoints"],
        rpm_raw=i["rpm_raw"],
        load_raw=i["load_raw"],
        flag_raw=i["flag_raw"],
        flag_bit=k["flag_bit"],
        load_bits=k["load_bits"],
        gate=target_phi_command.GateConstants(
            k["floor_cell"], k["threshold_cell"], k["hysteresis_cells"]
        ),
        cell_to_word_shift=k["cell_to_word_shift"],
    )
    return {
        "branch": result.branch,
        "cell": result.cell,
        "word": result.word,
        "reason": result.reason,
        "lookup": _lookup(result.lookup),
    }


def _op_x_from_bfs(i: Json, named: Json) -> Json:
    return {
        "value": target_phi_command.x_from_bfs(
            i["bfs"], i["scaler"], shift=i["shift"], cap=i["cap"]
        )
    }


def _load_curves(name: str, named: Json) -> fuel_comp_load.LoadInputCurves:
    c = named["load_curves"][name]
    return fuel_comp_load.LoadInputCurves(
        area_breakpoints=tuple(c["area_breakpoints"]),
        area_values=tuple(c["area_values"]),
        fill_low=tuple(c["fill_low"]),
        fill_high=tuple(c["fill_high"]),
        rpm_multiplier=c["rpm_multiplier"],
        reciprocal_numerator=c["reciprocal_numerator"],
        idle_air_cap=c["idle_air_cap"],
        limp_home_area=c["limp_home_area"],
    )


def _op_load_input_curves(i: Json, named: Json) -> Json:
    _load_curves(i["curves"], named)
    return {"valid": True}


def _op_fuel_comp_load(i: Json, named: Json) -> Json:
    trace = fuel_comp_load.reconstruct(
        throttle_counts=i["throttle_counts"],
        rpm_word=i["rpm_word"],
        idle_air=i["idle_air"],
        purge_area=i["purge_area"],
        curves=_load_curves(i["curves"], named),
        limp_home=i["limp_home"],
    )
    return {
        "byte": trace.byte,
        "x": trace.x,
        "area": trace.area,
        "w1124": trace.w1124,
        "w116a": trace.w116a,
    }


def _state_models() -> Any:
    # The E05.3 state models; imported only by the state operations, so the
    # stateless vectors run without them.
    from src.analysis import ecu_state_models

    return ecu_state_models


def _to_state(raw: list[int | None]) -> Any:
    return _state_models().UpRevState(**dict(zip(_STATE_FIELDS, raw, strict=True)))


def _op_uprev_power_on(i: Json, named: Json) -> Json:
    state, copied = _state_models().uprev_power_on(_to_state(i["retained"]))
    return {"state": _uprev_state(state), "copied": copied}


def _cycle(step: Any) -> Json:
    if step.value is None:
        return {"reason": step.reason}
    cycle = step.value
    return {
        "state": _uprev_state(cycle.state),
        "copied": cycle.copied,
        "indicator": cycle.indicator,
    }


def _op_uprev_step(i: Json, named: Json) -> Json:
    return _cycle(_state_models().uprev_step(_to_state(i["state"]), i["inputs_2ee0"]))


def _op_uprev_sequence(i: Json, named: Json) -> Json:
    """Cycles in order, each from the last one's state; ends at the first
    unevaluable cycle (nothing is assumed past it)."""
    state, steps = _to_state(i["state"]), []
    for inputs in i["inputs"]:
        step = _state_models().uprev_step(state, inputs)
        steps.append(_cycle(step))
        if step.value is None:
            break
        state = step.value.state
    return {"steps": steps}


def _op_loaded_slot(i: Json, named: Json) -> Json:
    step = _state_models().loaded_slot(i["magic"], i["active_map"])
    if step.value is None:
        return {"reason": step.reason}
    slot, source = step.value
    return {"slot": slot, "source": source}


OPS: dict[str, Callable[[Json, Json], Json]] = {
    "search_axis": _op_search_axis,
    "interpolate_q16": _op_interpolate,
    "lookup_2d": _op_lookup_2d,
    "lookup_curve_q16": _op_curve,
    "lookup_shift12_q16": _op_shift12,
    "target_command": _op_target_command,
    "x_from_bfs": _op_x_from_bfs,
    "load_input_curves": _op_load_input_curves,
    "fuel_comp_load": _op_fuel_comp_load,
    "uprev_power_on": _op_uprev_power_on,
    "uprev_step": _op_uprev_step,
    "uprev_sequence": _op_uprev_sequence,
    "loaded_slot": _op_loaded_slot,
}

#: Vector operations that are harness compositions of a contract operation.
COMPOSED = {"uprev_sequence": "uprev_step"}


def refusal_kind(error: Exception, refusals: dict[str, str]) -> str:
    """The contract's name for a reference refusal (by the contract's regex)."""
    if isinstance(error, ZeroDivisionError):
        return "division_by_zero"
    for kind, pattern in refusals.items():
        if kind != "division_by_zero" and re.search(pattern, str(error)):
            return kind
    raise AssertionError(f"refusal {error!r} matches no contract refusal kind") from error


def run(vector: Json, named: Json, refusals: dict[str, str]) -> Json:
    """``{"out": ...}`` or ``{"refuse": kind}`` from the Python reference."""
    try:
        return {"out": OPS[vector["op"]](vector["in"], named)}
    except (ValueError, ZeroDivisionError) as error:
        return {"refuse": refusal_kind(error, refusals)}


# -- the vectors -----------------------------------------------------------------


def _v(op: str, source: str, inputs: Json, *, pinned: Json | None = None) -> Json:
    vector: Json = {"op": op, "source": source, "in": inputs}
    if pinned is not None:
        vector["pinned"] = pinned
    return vector


def _control(op: str, source: str, inputs: Json, must_differ: Json) -> Json:
    return {"op": op, "source": source, "kind": "control", "in": inputs, "must_differ": must_differ}


ECM = "hand:tests/test_ecm_lookup.py::"
FCL = "hand:tests/test_fuel_comp_load.py::"
TPH = "hand:tests/test_target_phi.py::"


def _hand_lookup() -> Iterator[Json]:
    t = "test_a_value_on_a_breakpoint_is_that_index_with_no_fraction"
    yield _v(
        "search_axis",
        ECM + t,
        {"axis": "load", "value": 0x5D},
        pinned={"hit": {"index": 2, "numerator": 0, "span": 1, "edge": None, "fraction_q16": 0}},
    )
    yield _v(
        "search_axis",
        ECM + t,
        {"axis": "load", "value": 0x80},
        pinned={"hit": {"index": 7, "numerator": 0, "span": 1, "edge": None, "fraction_q16": 0}},
    )
    t = "test_a_value_between_breakpoints_is_the_lower_index_and_a_q16_fraction"
    for axis, value, index, fraction in (
        ("load", 0x68, 3, 32768),
        ("load", 0x6D, 4, 32768),
        ("load", 0x35, 0, 4096),
        ("load", 0x43, 0, 61440),
        ("rpm", 300, 4, 24576),
    ):
        yield _v(
            "search_axis",
            ECM + t,
            {"axis": axis, "value": value},
            pinned={"hit.index": index, "hit.fraction_q16": fraction, "hit.edge": None},
        )
    t = "test_the_fraction_truncates_like_the_firmware_divide"
    for axis, value, fraction in (("load", 0x45, 2621), ("rpm", 129, 2048)):
        yield _v(
            "search_axis",
            ECM + t,
            {"axis": axis, "value": value},
            pinned={"hit.fraction_q16": fraction},
        )
    t = "test_past_either_end_the_search_clamps_and_names_the_edge"
    yield _v(
        "search_axis",
        ECM + t,
        {"axis": "load", "value": 0x10},
        pinned={"hit": {"index": 0, "numerator": 0, "span": 1, "edge": "below", "fraction_q16": 0}},
    )
    yield _v(
        "search_axis",
        ECM + t,
        {"axis": "load", "value": 0xFF},
        pinned={"hit": {"index": 7, "numerator": 0, "span": 1, "edge": "above", "fraction_q16": 0}},
    )
    yield _v(
        "search_axis",
        ECM + t,
        {"axis": "rpm", "value": 2000},
        pinned={"hit": {"index": 7, "numerator": 0, "span": 1, "edge": "above", "fraction_q16": 0}},
    )
    t = "test_an_empty_axis_or_a_non_finite_value_has_no_position"
    yield _v("search_axis", ECM + t, {"axis": "empty", "value": 3}, pinned={"hit": None})
    # Listing: 0x026F10 compares with cmp/hs (unsigned) on operands every
    # caller zero-extends (extu.w/extu.b), so an operand at or above 0x8000
    # is a large positive word, never negative. Worked by hand from that:
    # 0x7000 -> 0x9000 by half is 0x7000 + (0x2000 * 0x8000 >> 16) = 0x8000;
    # 0x9000 -> 0x7000 by half is 0x9000 - 0x1000 = 0x8000; 0x0000 -> 0xFFFF
    # by 0xFFFF is (0xFFFF * 0xFFFF) >> 16 = 0xFFFE. A signed compare would
    # take the other branch on the first two.
    unsigned = "hand:listing 0x026F10 cmp/hs, callers extu.w/extu.b"
    for a, b, f, value in (
        (0x7000, 0x9000, 0x8000, 0x8000),
        (0x9000, 0x7000, 0x8000, 0x8000),
        (0x7FFF, 0x8001, 0xFFFF, 0x8000),
        (0x0000, 0xFFFF, 0xFFFF, 0xFFFE),
    ):
        yield _v(
            "interpolate_q16", unsigned, {"a": a, "b": b, "fraction": f}, pinned={"value": value}
        )
    # The same through a curve whose entries sit above 0x8000: 0x026C28 at
    # 0x1800 is half way from 44,000 to 45,000 on fill_high = 45,500.
    yield _v(
        "lookup_shift12_q16",
        unsigned,
        {"table16": "fill_high", "value": 0x1800},
        pinned={"value": 45500},
    )
    t = "test_interpolation_truncates_toward_the_first_operand_both_ways"
    for a, b, f, value in (
        (128, 146, 57344, 143),
        (146, 128, 57344, 131),
        (128, 131, 21845, 128),
        (131, 128, 21845, 131),
        (150, 150, 40000, 150),
    ):
        yield _v(
            "interpolate_q16", ECM + t, {"a": a, "b": b, "fraction": f}, pinned={"value": value}
        )
    for t, rpm, load, value, cells in (
        ("test_a_cell_on_both_breakpoints_reads_that_cell_alone", 288, 0x6C, 0x96, [[4, 4]]),
        (
            "test_an_interior_point_reads_four_cells_rows_then_columns",
            256,
            0x6D,
            146,
            [[3, 4], [3, 5], [4, 4], [4, 5]],
        ),
        (
            "test_a_point_on_a_column_breakpoint_interpolates_down_the_column_only",
            408,
            0x34,
            144,
            [[6, 0], [7, 0]],
        ),
        (
            "test_a_point_on_a_row_breakpoint_interpolates_along_the_row_only",
            352,
            0x45,
            0x8B,
            [[6, 1], [6, 2]],
        ),
        ("test_edges_clamp_to_the_last_row_and_column", 600, 0xFF, 0x9F, [[7, 7]]),
        ("test_edges_clamp_to_the_last_row_and_column", 10, 0, 0x80, [[0, 0]]),
        *(
            ("test_edges_clamp_to_the_last_row_and_column", w, 0xFF, 0x95, None)
            for w in (128, 236, 276, 288)
        ),
    ):
        pinned: Json = {"value": value}
        if cells is not None:
            pinned["cells"] = cells
        yield _v(
            "lookup_2d",
            ECM + t,
            {"table": "map1", "row_value": rpm, "column_value": load},
            pinned=pinned,
        )
    t = "test_a_fractional_coordinate_is_refused_not_truncated"
    yield _v(
        "lookup_2d",
        ECM + t,
        {"table": "map1", "row_value": 236, "column_value": 25.0859},
        pinned={"refuse": "not_whole_count"},
    )
    t = "test_a_table_that_does_not_fit_its_axes_is_refused"
    yield _v(
        "lookup_2d",
        ECM + t,
        {"table": "map1_7_rows", "row_value": 236, "column_value": 0x40},
        pinned={"refuse": "shape_mismatch"},
    )

    # tests/test_fuel_comp_load.py
    t = "test_a_curve_on_a_breakpoint_reads_that_entry"
    for value, out in ((0, 200), (500, 200 + 30 * 25), (3100, AREA_VALUES[-1])):
        yield _v(
            "lookup_curve_q16", FCL + t, {"curve": "area", "value": value}, pinned={"value": out}
        )
    t = "test_a_curve_between_breakpoints_interpolates_and_truncates"
    for value, out in ((150, 275), (101, 230), (199, 319)):
        yield _v(
            "lookup_curve_q16", FCL + t, {"curve": "area", "value": value}, pinned={"value": out}
        )
    t = "test_a_curve_clamps_past_its_last_breakpoint_and_never_extrapolates"
    for value in (3101, 0xFFFF):
        yield _v(
            "lookup_curve_q16",
            FCL + t,
            {"curve": "area", "value": value},
            pinned={"value": AREA_VALUES[-1]},
        )
    yield _v(
        "lookup_curve_q16",
        FCL + t,
        {"curve": "area_shifted", "value": 0},
        pinned={"value": AREA_VALUES[0]},
    )
    t = "test_a_curve_falling_between_entries_truncates_toward_the_first"
    yield _v(
        "lookup_curve_q16",
        FCL + t,
        {"curve": "area_falling", "value": 50},
        pinned={"value": AREA_VALUES[-1] - (1830 * 32768 >> 16)},
    )
    t = "test_a_curve_input_that_is_not_a_word_is_refused"
    yield _v(
        "lookup_curve_q16",
        FCL + t,
        {"curve": "area", "value": 1.5},
        pinned={"refuse": "not_whole_count"},
    )
    for bad in (-1, 0x10000):
        yield _v(
            "lookup_curve_q16",
            FCL + t,
            {"curve": "area", "value": bad},
            pinned={"refuse": "out_of_domain"},
        )
    t = "test_a_curve_needs_one_entry_per_breakpoint"
    yield _v(
        "lookup_curve_q16",
        FCL + t,
        {"curve": "area_short", "value": 10},
        pinned={"refuse": "curve_shape"},
    )
    t = "test_shift12_reads_entries_at_multiples_of_4096"
    for index in range(15):
        yield _v(
            "lookup_shift12_q16",
            FCL + t,
            {"table16": "fill_high", "value": index << 12},
            pinned={"value": FILL_HIGH[index]},
        )
    t = "test_shift12_fraction_is_the_low_12_bits_moved_up_to_q16"
    for value, out in ((0x0800, 4000), (0x0001, 1), (0xEFFF, 44000), (0x1FFF, 15998)):
        yield _v(
            "lookup_shift12_q16",
            FCL + t,
            {"table16": "fill_low", "value": value},
            pinned={"value": out},
        )
    t = "test_shift12_from_0xf000_reads_the_last_entry_outright"
    for value in (0xF000, 0xFFFF):
        yield _v(
            "lookup_shift12_q16",
            FCL + t,
            {"table16": "fill_high_60000", "value": value},
            pinned={"value": 60000},
        )
    t = "test_shift12_needs_sixteen_entries_and_a_word_input"
    yield _v(
        "lookup_shift12_q16",
        FCL + t,
        {"table16": "fill_low_15", "value": 0},
        pinned={"refuse": "table_length"},
    )
    yield _v(
        "lookup_shift12_q16",
        FCL + t,
        {"table16": "fill_low", "value": 0x10000},
        pinned={"refuse": "out_of_domain"},
    )
    base = {
        "curves": "synthetic",
        "throttle_counts": 50,
        "idle_air": 100,
        "purge_area": 40,
        "limp_home": False,
    }
    t = "test_rpm_reciprocal_saturates_the_product_and_clamps_the_quotient"
    for word, w116a in ((80, 833), (437, 152), (1000, 152), (0, 0), (1, 0xFFFF)):
        yield _v("fuel_comp_load", FCL + t, {**base, "rpm_word": word}, pinned={"w116a": w116a})
    t = "test_reconstruct_end_to_end_by_hand"
    yield _v(
        "fuel_comp_load",
        FCL + t,
        {**base, "rpm_word": 64},
        pinned={"w116a": 1041, "area": 320, "w1124": 220, "x": 467, "byte": 14},
    )
    yield _v(
        "fuel_comp_load",
        FCL + t,
        {**base, "rpm_word": 64, "limp_home": True},
        pinned={"w1124": 700},
    )
    t = "test_effective_area_clamps_at_zero_and_reads_idle_air_signed"
    yield _v(
        "fuel_comp_load",
        FCL + t,
        {**base, "rpm_word": 64, "idle_air": 1001},
        pinned={"refuse": "idle_air_out_of_range"},
    )
    t = "test_curves_that_would_break_monotonicity_are_refused"
    for name, kind in (
        ("axis_repeats", "axis_not_rising"),
        ("area_falls", "non_monotone"),
        ("split_falls", "non_monotone"),
        ("fill_low_not_word", "out_of_domain"),
        ("fill_low_15", "table_length"),
    ):
        yield _v("load_input_curves", FCL + t, {"curves": name}, pinned={"refuse": kind})
    yield _v("load_input_curves", FCL + t, {"curves": "synthetic"}, pinned={"valid": True})

    # tests/test_target_phi.py (the profile's own gate constants)
    t = "test_the_pedal_flag_forces_the_last_column_whatever_the_load_byte"
    for load in (0, 10, 100, 128):
        yield _v(
            "target_command",
            TPH + t,
            {"table": "map1", "constants": "twin", "rpm_raw": 236, "load_raw": load, "flag_raw": 1},
            pinned={"branch": "override", "cell": 149, "word": 19072},
        )
    yield _v(
        "target_command",
        TPH + t,
        {"table": "map1", "constants": "twin", "rpm_raw": 236, "load_raw": 10, "flag_raw": 2},
        pinned={"branch": "ordinary"},
    )
    t = "test_an_ordinary_lookup_below_the_enrichment_threshold_commands_stoich"
    yield _v(
        "target_command",
        TPH + t,
        {"table": "map1", "constants": "twin", "rpm_raw": 236, "load_raw": 10, "flag_raw": 0},
        pinned={"cell": 128, "word": 16384},
    )
    yield _v(
        "target_command",
        TPH + t,
        {"table": "map1", "constants": "twin", "rpm_raw": 256, "load_raw": 0x6D, "flag_raw": 0},
        pinned={"word": 146 << 7},
    )
    t = "test_a_lookup_inside_the_gates_hysteresis_band_is_ambiguous"
    yield _v(
        "target_command",
        TPH + t,
        {
            "table": "hysteresis",
            "constants": "twin",
            "rpm_raw": 96,
            "load_raw": 0x38,
            "flag_raw": 0,
        },
        pinned={"cell": 129, "word": None, "reason": "enrichment_gate_state_unknown"},
    )


def _capture_target_phi() -> Iterator[Json]:
    """The two October 4 enrichment commands (target_phi.yaml known_override):
    observed word 0x4A80 at the captured rpm word, forced column."""
    expect = yaml.safe_load(TARGET_PHI_EXPECT.read_text())
    for n, case in enumerate(expect["known_override"]):
        source = f"capture:tests/fixtures/ground_truth/target_phi.yaml#known_override[{n}]"
        inputs = {
            "table": "map1",
            "constants": "twin",
            "rpm_raw": case["rpm_raw"],
            "load_raw": 0,
            "flag_raw": 1,
        }
        yield _v(
            "target_command",
            source,
            inputs,
            pinned={"cell": case["predicted_cell"], "word": case["observed_word"]},
        )
        # Controls: the wrong rpm decode (50 rpm/count, the axis's scale), the
        # wrong flag bit, and the right bytes read the wrong way round must
        # all fail to reproduce the observed word.
        wrong_rpm = round(case["rpm_raw"] * 12.5 / 50)
        yield _control(
            "target_command",
            source + ":rpm_at_axis_scale",
            {**inputs, "rpm_raw": wrong_rpm},
            {"word": case["observed_word"]},
        )
        yield _control(
            "target_command",
            source + ":flag_bit_1",
            {**inputs, "flag_raw": 2},
            {"word": case["observed_word"]},
        )
        yield _control(
            "target_command",
            source + ":table_transposed",
            {**inputs, "table": "map1_transposed"},
            {"word": case["observed_word"]},
        )
    # The same shape of control on the hand-worked interior value 146.
    yield _control(
        "lookup_2d",
        ECM + "test_an_interior_point_reads_four_cells_rows_then_columns" + ":table_transposed",
        {"table": "map1_transposed", "row_value": 256, "column_value": 0x6D},
        {"value": 146},
    )
    # Rounding to nearest instead of truncating gives 144 here.
    yield _control(
        "interpolate_q16",
        ECM + "test_interpolation_truncates_toward_the_first_operand_both_ways:operands_swapped",
        {"a": 146, "b": 128, "fraction": 57344},
        {"value": 143},
    )


WORD_EDGES = [0, 1, 2, 0x7F, 0x80, 0xFF, 0x100, 0x7FFF, 0x8000, 0x8001, 0xFFFE, 0xFFFF]


def _around(points: list[int]) -> list[int]:
    out = set(WORD_EDGES)
    for p in points:
        out.update(v for v in (p - 1, p, p + 1) if 0 <= v <= 0xFFFF)
    return sorted(out)


def _generated_lookup() -> Iterator[Json]:
    g = "generated:"
    for axis, points in (
        ("load", LOAD),
        ("rpm", RPM),
        ("single", [500]),
        ("repeated", [10, 20, 30]),
        ("falling", [100, 200, 300]),
        ("word_edges", WORD_EDGES),
    ):
        for value in _around(points):
            yield _v("search_axis", g + "axis_edges", {"axis": axis, "value": value})
    operands = [0, 1, 0x7F, 0x80, 128, 146, 0x7FFF, 0x8000, 0xFFFE, 0xFFFF]
    for a in operands:
        for b in operands:
            for f in (0, 1, 0x7FFF, 0x8000, 57344, 0xFFFF):
                yield _v(
                    "interpolate_q16",
                    g + "interpolation_boundaries",
                    {"a": a, "b": b, "fraction": f},
                )
    for rpm in _around(RPM):
        for load in _around(LOAD)[::2]:
            yield _v(
                "lookup_2d",
                g + "map1_axis_edges",
                {"table": "map1", "row_value": rpm, "column_value": load},
            )
    rng = random.Random(0xE062)
    t = named_objects()["tables"]["word_cells"]
    for _ in range(300):
        yield _v(
            "lookup_2d",
            g + "word_cells_random",
            {
                "table": "word_cells",
                "row_value": rng.randrange(0x10000),
                "column_value": rng.randrange(0x10000),
            },
        )
    for rpm in _around(t["row_breakpoints"])[::3]:
        for load in _around(t["column_breakpoints"])[::3]:
            yield _v(
                "lookup_2d",
                g + "word_cells_edges",
                {"table": "word_cells", "row_value": rpm, "column_value": load},
            )
    for rpm in WORD_EDGES:
        for load in WORD_EDGES:
            yield _v(
                "lookup_2d",
                g + "word_extremes",
                {"table": "word_extremes", "row_value": rpm, "column_value": load},
            )
            yield _v(
                "lookup_2d",
                g + "one_cell",
                {"table": "one_cell", "row_value": rpm, "column_value": load},
            )
    for row, column in ((-1, 0x40), (0x10000, 0x40), (236, -1), (236, 0x10000), (236.5, 0x40)):
        yield _v(
            "lookup_2d",
            g + "coordinate_domain",
            {"table": "map1", "row_value": row, "column_value": column},
        )
    yield _v(
        "lookup_2d", g + "empty_axis", {"table": "empty_rows", "row_value": 0, "column_value": 0}
    )
    for curve in ("area", "area_shifted", "area_falling"):
        for value in _around(AREA_AXIS)[::2]:
            yield _v("lookup_curve_q16", g + "curve_edges", {"curve": curve, "value": value})
    yield _v("lookup_curve_q16", g + "curve_shape", {"curve": "empty", "value": 0})
    for table in ("fill_low", "fill_high", "fill_high_60000", "word_extremes"):
        for value in _around([i << 12 for i in range(16)] + [0xF000]):
            yield _v("lookup_shift12_q16", g + "shift12_edges", {"table16": table, "value": value})
    yield _v("lookup_shift12_q16", g + "shift12_domain", {"table16": "fill_low", "value": -1})

    # The lookup itself is covered above; this grid exercises the branch and
    # the gate around every breakpoint, the flag word's ends and both widths.
    loads = _around(LOAD)[::3] + [0xFF]
    grid = (
        ("twin", _around(RPM)[::3], (0, 1, 2)),
        ("flag_bit_31_load_16", _around(RPM)[::6], (1, 0x80000000, 0xFFFFFFFF)),
        ("negative_band", _around(RPM)[::6], (0,)),
    )
    for constants, rpms, flags in grid:
        for rpm in rpms:
            for load in loads:
                for flag in flags:
                    yield _v(
                        "target_command",
                        g + "branches_and_gate",
                        {
                            "table": "map1",
                            "constants": constants,
                            "rpm_raw": rpm,
                            "load_raw": load,
                            "flag_raw": flag,
                        },
                    )
    for rpm in (96, 97, 128):
        for load in range(0x34, 0x45):
            yield _v(
                "target_command",
                g + "hysteresis_band",
                {
                    "table": "hysteresis",
                    "constants": "twin",
                    "rpm_raw": rpm,
                    "load_raw": load,
                    "flag_raw": 0,
                },
            )
    for constants in ("flag_bit_32", "load_bits_32", "shift_17"):
        yield _v(
            "target_command",
            g + "constants_domain",
            {
                "table": "map1",
                "constants": constants,
                "rpm_raw": 236,
                "load_raw": 10,
                "flag_raw": 1,
            },
        )
    # Raw inputs are never truncated: a fraction, a negative flag and a flag
    # past 32 bits are refused (E06.1 verifier finding).
    tuple_base = {"table": "map1", "constants": "twin", "rpm_raw": 236, "load_raw": 10}
    for field, bad in (
        ("rpm_raw", 236.5),
        ("load_raw", 10.5),
        ("rpm_raw", 0x10000),
        ("flag_raw", -1),
        ("flag_raw", 2**32),
        ("flag_raw", 0.5),
    ):
        yield _v("target_command", g + "tuple_domain", {**tuple_base, "flag_raw": 0, field: bad})
    for bfs in WORD_EDGES + [44018, 44019, 44020, 343, 344]:
        for scaler in (1, 343, 44019, 0xFFFF):
            for shift, cap in ((7, 255), (0, 0xFFFF), (16, 0xFFFF)):
                yield _v(
                    "x_from_bfs",
                    g + "x_boundaries",
                    {"bfs": bfs, "scaler": scaler, "shift": shift, "cap": cap},
                )
    yield _v("x_from_bfs", g + "x_domain", {"bfs": 1000, "scaler": 0, "shift": 7, "cap": 255})
    yield _v("x_from_bfs", g + "x_domain", {"bfs": 1000.5, "scaler": 44019, "shift": 7, "cap": 255})
    yield _v("x_from_bfs", g + "x_domain", {"bfs": -1, "scaler": 44019, "shift": 7, "cap": 255})
    yield _v("x_from_bfs", g + "x_domain", {"bfs": 1000, "scaler": 44019, "shift": 17, "cap": 255})

    yield _v("load_input_curves", g + "curves_domain", {"curves": "numerator_zero"})
    yield _v("load_input_curves", g + "curves_domain", {"curves": "saturating"})
    for curves in ("synthetic", "saturating"):
        for throttle in (0, 1, 25, 50, 793, 0x3FFF, 0x4000, 0xFFFF):
            for rpm in (0, 1, 64, 437, 1000, 0xFFFF):
                for idle in (-0x8000, -80, 1000):
                    for purge in (0, 0xFFFF):
                        yield _v(
                            "fuel_comp_load",
                            g + "load_chain",
                            {
                                "curves": curves,
                                "throttle_counts": throttle,
                                "rpm_word": rpm,
                                "idle_air": idle,
                                "purge_area": purge,
                                "limp_home": False,
                            },
                        )
                yield _v(
                    "fuel_comp_load",
                    g + "load_chain_limp",
                    {
                        "curves": curves,
                        "throttle_counts": throttle,
                        "rpm_word": rpm,
                        "idle_air": 0,
                        "purge_area": 40,
                        "limp_home": True,
                    },
                )
    base = {
        "curves": "synthetic",
        "throttle_counts": 50,
        "rpm_word": 64,
        "idle_air": 100,
        "purge_area": 40,
        "limp_home": False,
    }
    for field, bad in (
        ("idle_air", -0x8001),
        ("idle_air", 1001),
        ("purge_area", 0x10000),
        ("throttle_counts", 0x10000),
        ("rpm_word", -1),
    ):
        yield _v("fuel_comp_load", g + "load_chain_domain", {**base, field: bad})
    # The limp-home branch never reads idle air, so it is not refused there.
    yield _v("fuel_comp_load", g + "load_chain_limp", {**base, "idle_air": 5000, "limp_home": True})
    # A saturated area term skips the purge term, but its input is still a word.
    yield _v(
        "fuel_comp_load",
        g + "load_chain_domain",
        {
            **base,
            "throttle_counts": 0xFFFF,
            "rpm_word": 1,
            "idle_air": -0x8000,
            "purge_area": 0x10000,
        },
    )


def _state(**fields: Any) -> Json:
    base: Json = dict.fromkeys(_STATE_FIELDS)
    base.update(fields)
    return base


def _packed(**fields: Any) -> list[int | None]:
    """A state as a vector input: the six fields in order."""
    return [_state(**fields)[name] for name in _STATE_FIELDS]


SM = "hand:tests/test_ecu_state_models.py::"


def _hand_state() -> Iterator[Json]:
    """The E05.3 tests' hand-worked selection cases (synthetic, from code)."""
    t = "test_power_on_initialises_only_without_the_magic_and_never_from_unknown_ram"
    cold = _state(countdown=0, debounce=0, selector=0, magic=UPREV_MAGIC, active_map=0, history=0)
    yield _v(
        "uprev_power_on",
        SM + t,
        {"retained": [5, 8, 3, 0x12345678, 4, 0x42]},
        pinned={"state": cold, "copied": 1},
    )
    warm = [0, 0, 0, UPREV_MAGIC, 3, 0x123]
    yield _v(
        "uprev_power_on",
        SM + t,
        {"retained": warm},
        pinned={"state": _state(**dict(zip(_STATE_FIELDS, warm, strict=True))), "copied": None},
    )
    yield _v(
        "uprev_power_on",
        SM + t,
        {"retained": [None] * 6},
        pinned={"refuse": "retained_magic_unknown"},
    )
    t = "test_two_presses_while_holding_commit_map_two_and_blink_it_twice"
    presses = [0x08, 0x0A, 0x0A, 0x08, 0x0A, 0x08, 0x00]
    yield _v(
        "uprev_sequence",
        SM + t,
        {"state": [0, 0, 0, UPREV_MAGIC, 0, 0], "inputs": presses + [0x00] * 130},
        pinned={
            "steps.6.copied": 2,
            "steps.6.state.active_map": 2,
            "steps.6.state.selector": 2,
            "steps.6.state.countdown": 128,
            "steps.6.state.history": 2,
            "steps.136.state.countdown": 0,
            "steps.136.state.selector": 0,
            "steps.136.indicator": None,
        },
    )
    t = "test_a_release_from_both_bits_or_an_invalid_selector_commits_nothing"
    start = [0, 0, 0, UPREV_MAGIC, 3, 0]
    yield _v(
        "uprev_sequence",
        SM + t,
        {"state": start, "inputs": [0x08, 0x0A, 0x00, 0x00]},
        pinned={
            "steps.2.copied": None,
            "steps.2.state.active_map": 3,
            "steps.2.state.selector": 1,
            "steps.3.state.selector": 0,
        },
    )
    yield _v(
        "uprev_sequence",
        SM + t,
        {"state": start, "inputs": [0x08] + [0x0A, 0x08] * 6 + [0x00]},
        pinned={
            "steps.13.copied": None,
            "steps.13.state.active_map": 3,
            "steps.13.state.history": 6,
        },
    )
    yield _v(
        "uprev_step",
        SM + t,
        {"state": [0, None, 0, UPREV_MAGIC, 3, None], "inputs_2ee0": 0x08},
        pinned={"reason": "uprev_debounce_unknown"},
    )
    t = "test_the_loaded_slot_follows_the_committed_byte_or_the_init_default"
    for magic, active, pinned in (
        (UPREV_MAGIC, 0, {"slot": 1, "source": "init_default"}),
        (UPREV_MAGIC, 4, {"slot": 4, "source": "committed"}),
        (0, 0, {"reason": "uprev_not_initialised"}),
        (None, 0, {"reason": "uprev_state_unobserved"}),
        (UPREV_MAGIC, 7, {"reason": "uprev_active_map_invalid"}),
    ):
        yield _v("loaded_slot", SM + t, {"magic": magic, "active_map": active}, pinned=pinned)


def _state_vectors() -> Iterator[Json]:
    yield from _hand_state()
    g = "generated:"
    for countdown in (None, 0, 1, 0x20, 0x21, 0xFFFF):
        for debounce in (None, 0, 2, 8, 10):
            for selector in (None, 0, 1, 5, 0xFF):
                for history in (None, 0xFFFFFFFF):
                    for inputs in (0x00, 0x02, 0x08, 0xF5):
                        state = _packed(
                            countdown=countdown,
                            debounce=debounce,
                            selector=selector,
                            magic=UPREV_MAGIC,
                            active_map=3,
                            history=history,
                        )
                        yield _v(
                            "uprev_step", g + "uprev_cycle", {"state": state, "inputs_2ee0": inputs}
                        )
    yield _v(
        "uprev_step",
        g + "uprev_domain",
        {"state": _packed(countdown=0, debounce=0), "inputs_2ee0": 0x100},
    )
    for magic in (None, 0, UPREV_MAGIC, 0x12345678):
        retained = _packed(
            countdown=7, debounce=8, selector=2, magic=magic, active_map=2, history=0x21
        )
        yield _v("uprev_power_on", g + "uprev_reset", {"retained": retained})
    for magic in (None, 0, UPREV_MAGIC):
        for active in (None, 0, 1, 5, 6, 0xFF):
            yield _v("loaded_slot", g + "loaded_slot", {"magic": magic, "active_map": active})
    known = {
        "countdown": 0,
        "debounce": 0,
        "selector": 0,
        "magic": UPREV_MAGIC,
        "active_map": 0,
        "history": 0,
    }
    # Select slot 2 (hold, step twice, release) then let the countdown run out.
    select_2 = [0x08, 0x0A, 0x08, 0x0A, 0x08, 0x00] + [0x00] * 130
    yield _v("uprev_sequence", g + "uprev_select", {"state": _packed(**known), "inputs": select_2})
    # Six steps wrap past slot 5: the commit writes history but no slot.
    select_6 = [0x08] + [0x0A, 0x08] * 6 + [0x00, 0x00]
    yield _v("uprev_sequence", g + "uprev_select", {"state": _packed(**known), "inputs": select_6})
    # 256 steps wrap the selector byte to 0; a full history shifts out its top.
    wrap = [0x08] + [0x0A, 0x08] * 256 + [0x00]
    yield _v(
        "uprev_sequence",
        g + "uprev_selector_wrap",
        {"state": _packed(**{**known, "history": 0xFFFFFFFF}), "inputs": wrap},
    )
    # Missing history: an unknown debounce ends the sequence at once; an
    # unknown selector survives until a step or commit needs it.
    missing: tuple[tuple[Json, list[int]], ...] = (
        ({"debounce": None}, [0x08, 0x0A]),
        ({"debounce": 8, "selector": None}, [0x08, 0x0A, 0x08]),
        ({"selector": None, "history": None}, [0x08, 0x0A, 0x08, 0x00]),
        ({"countdown": None}, [0x00]),
    )
    for edit, cycle_inputs in missing:
        yield _v(
            "uprev_sequence",
            g + "uprev_missing_history",
            {"state": _packed(**{**known, **edit}), "inputs": cycle_inputs},
        )


def pinned_value(result: Json, key: str) -> Any:
    """The value a ``pinned`` key names: ``refuse``, or a dotted path in ``out``."""
    if key == "refuse":
        return result.get("refuse")
    value: Any = result.get("out", {})
    for part in key.split("."):
        if isinstance(value, list):
            value = value[int(part)]
        else:
            value = value.get(part) if isinstance(value, dict) else None
    return value


def build(vectors: Iterator[Json], *, kind: str) -> Json:
    """One vector file: every vector run through the reference, ids assigned,
    pinned answers checked against the reference before anything is written."""
    c = contract()
    named = named_objects()
    out: list[Json] = []
    for n, vector in enumerate(vectors):
        result = run(vector, named, c["refusals"])
        vector = {"id": f"{kind}-{n:05d}", **vector}
        if vector.get("kind") == "control":
            assert "out" in result, (vector, result)
            for key, value in vector["must_differ"].items():
                assert result["out"][key] != value, ("control reproduced", vector)
        vector.update(result)
        for key, value in vector.get("pinned", {}).items():
            got = pinned_value(vector, key)
            assert got == value, (vector["id"], key, got, value)
        out.append(vector)
    return {
        "format": FORMAT,
        "contract_version": c["contract_version"],
        "kernel_version": c["kernel_version"],
        "state_model_version": c["state_model_version"],
        "kind": kind,
        "named": named if kind == "lookup" else {},
        "vectors": out,
    }


def build_lookup() -> Json:
    def vectors() -> Iterator[Json]:
        yield from _hand_lookup()
        yield from _capture_target_phi()
        yield from _generated_lookup()

    return build(vectors(), kind="lookup")


def build_state() -> Json:
    return build(_state_vectors(), kind="state")


def dumps(document: Json) -> str:
    """One vector per line: reviewable diffs, stable bytes."""
    head = {k: v for k, v in document.items() if k not in ("vectors", "named")}
    lines = ["{"]
    for key, value in head.items():
        lines.append(f"  {json.dumps(key)}: {json.dumps(value)},")
    lines.append(f'  "named": {json.dumps(document["named"], sort_keys=True)},')
    lines.append('  "vectors": [')
    body = [f"    {json.dumps(v, sort_keys=True)}" for v in document["vectors"]]
    lines.append(",\n".join(body))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    LOOKUP_PATH.write_text(dumps(build_lookup()))
    STATE_PATH.write_text(dumps(build_state()))
    print(f"wrote {LOOKUP_PATH.relative_to(REPO)} and {STATE_PATH.relative_to(REPO)}")


if __name__ == "__main__":
    main()
