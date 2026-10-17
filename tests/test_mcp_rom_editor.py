"""Tests for the ROM Editor MCP tools (W09.5): preview and simulate pass-throughs."""

from __future__ import annotations

import json
import random
from typing import Any

import httpx
import pydantic_core
import pytest

from src.analysis.rom_editor import SimRequest
from src.mcp_server.client import DaemonClient, DaemonError
from src.mcp_server.tools import rom_editor as rom_editor_tool

SHA = "a" * 64
EDIT = {
    "table": "k_constant",
    "slot": 1,
    "all": True,
    "scale": 1.05,
    "rounding": "nearest",
}
SIM_REQUEST: dict[str, Any] = {
    "label": "K +5 %",
    "edits": [EDIT],
    "grid": "fuel_comp",
    "rpm_edges": [4000.0, 4800.0, 5600.0],
    "load_edges": [30.0, 60.0, 80.0, 100.0],
    "load_basis": "fill_fraction_as_manifold_ratio",
    "baro_kpa": 80.0,
    "iat_c": 25.0,
    "coolant_c": 88.0,
    "battery_v": 14.0,
    "rail_kpa": "working_assumption",
    "throttle_pct": 20.0,
    "pedal_flag": 0,
    "stft_pct": 0.0,
    "ltft_pct": 0.0,
    "throttle_closed_learned": 0.0,
    "idle_air_learned": 0.0,
    "evap_purge_area": 0.0,
    "candidate_events": "none",
    "draws": 200,
    "seed": 20261015,
}


async def _daemon(
    handler: Any, seen: list[httpx.Request] | None = None, *, token: str | None = "secret"
) -> DaemonClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    client = DaemonClient("http://daemon", token)
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://daemon",
        headers={"Authorization": f"Bearer {token}"} if token else {},
        transport=httpx.MockTransport(wrapped),
    )
    return client


OUTPUT_NAMES = (
    "base_fuel_schedule",
    "target_phi",
    "injector_pulse_ms",
    "fuel_trim_total_pct",
    "delivered_lambda",
    "fuel_per_event_g",
)
REASON = "closed_loop_trim_authority_undecoded"
#: The form's 16 x 14 grid on Fuel Comp's 16 x 16 table: 224 visited cells.
DENSE = {(row, col) for row in range(16) for col in range(14)}
VISITED = {(3, 3), (3, 4), (4, 3), (4, 4), (9, 12), (15, 15)}


def _float(rng: random.Random, scale: float = 1.0) -> float:
    """A float as the daemon writes one: seventeen digits, not a round number."""
    return rng.uniform(-1.0, 1.0) * scale


def _quantiles(rng: random.Random, scale: float) -> dict[str, float]:
    a, b, c = sorted(_float(rng, scale) for _ in range(3))
    return {"p05": a, "p50": b, "p95": c}


def _output(rng: random.Random, name: str, status: str, *, by_loop: bool = False) -> dict[str, Any]:
    """One ``OutputCell`` as ``CellTable.to_dict()`` writes it (W09.6 payload)."""
    if status == "no_points":
        return {
            "status": status,
            "pairs": 0,
            "supported_share": None,
            "unsupported_share": None,
            "support": [],
            "quantiles": None,
            "changed_share": None,
            "supported_effect": None,
        }
    if status == "unsupported":
        return {
            "status": status,
            "pairs": 20,
            "supported_share": 0.0,
            "unsupported_share": 1.0,
            "support": [
                {"status": "unsupported", "reasons": [REASON], "pairs": 20, "weight_share": 1.0}
            ],
            # The real run: a cell's own trim output has no quantiles when unsupported,
            # its by_loop headline entry has the computed ones.
            "quantiles": None
            if name == "fuel_trim_total_pct" and not by_loop
            else {
                "over": "computed_unsupported_pairs",
                "pairs": 20,
                "weight": 1.0,
                "delta": _quantiles(rng, 5.0),
                "relative_delta": None,
                "relative_undefined_pairs": 0,
            },
            "changed_share": None,
            "supported_effect": None,
        }
    partly = status == "partly_supported"
    return {
        "status": status,
        "pairs": 20,
        "supported_share": 0.6 if partly else 1.0,
        "unsupported_share": 0.4 if partly else 0.0,
        "support": [
            {"status": "supported", "reasons": [], "pairs": 12, "weight_share": 0.6},
            {
                "status": "unsupported",
                "reasons": ["fill_byte_outside_plant_range"],
                "pairs": 8,
                "weight_share": 0.4,
            },
        ]
        if partly
        else [{"status": "supported", "reasons": [], "pairs": 20, "weight_share": 1.0}],
        "quantiles": {
            "over": "supported_pairs" if partly else "all_pairs",
            "pairs": 20,
            "weight": 1.0,
            "delta": _quantiles(rng, 1.0 if status == "changed" else 0.0),
            "relative_delta": _quantiles(rng, 0.05 if status == "changed" else 0.0),
            "relative_undefined_pairs": 0,
        },
        "changed_share": 1.0 if status == "changed" else 0.0,
        "supported_effect": "changed" if partly else None,
    }


def _statuses(row: int, col: int) -> dict[str, str]:
    """A visited cell's status per output: closed-loop cells are unsupported on
    four of six (the real run's pattern), open-loop ones are changed/unchanged."""
    closed = (row + col) % 3 != 0
    return {
        "base_fuel_schedule": "changed",
        "target_phi": "unchanged" if (row * col) % 5 else "changed",
        "injector_pulse_ms": "unsupported" if closed else "changed",
        "fuel_trim_total_pct": "unsupported" if closed else "unchanged",
        "delivered_lambda": "unsupported" if closed else "changed",
        "fuel_per_event_g": "partly_supported"
        if row == 0
        else "unsupported"
        if closed
        else "changed",
    }


def _cell(rng: random.Random, row: int, col: int, *, visited: bool) -> dict[str, Any]:
    statuses = _statuses(row, col) if visited else dict.fromkeys(OUTPUT_NAMES, "no_points")
    loop = "closed_loop" if (row + col) % 3 else "open_loop"
    # Column 0 is a mixed cell: both loop kinds visit it.
    loops = ["open_loop", "closed_loop"] if col == 0 else [loop]
    return {
        "row": row,
        "col": col,
        "row_breakpoint": 4000 + 100 * row,
        "col_breakpoint": 30 + 4 * col,
        "row_value": 400.0 * (row + 1) if visited else 400.0 * (row + 1),
        "col_value": 100.0 * col / 14,
        "n_points": 1 if visited else 0,
        "points": [row * 14 + col] if visited else [],
        "pairs": 20 if visited else 0,
        "weight": 1.0 if visited else 0.0,
        "regime_shares": {"unknown": 1.0} if visited else {},
        "loop_shares": {k: 1.0 / len(loops) for k in loops} if visited else {},
        "outputs": {name: _output(rng, name, statuses[name]) for name in OUTPUT_NAMES},
        "by_loop": [
            {
                "loop": kind,
                "weight_share": 1.0 / len(loops),
                "pairs": 20,
                "output": "fuel_trim_total_pct" if kind == "closed_loop" else "delivered_lambda",
                "cell": _output(
                    rng,
                    "fuel_trim_total_pct" if kind == "closed_loop" else "delivered_lambda",
                    "unsupported" if kind == "closed_loop" else "changed",
                    by_loop=True,
                ),
            }
            for kind in loops
        ]
        if visited
        else [],
        "edited": {"old": 128, "new": 131} if visited and row == col else None,
        "row_axis_moved": False,
        "col_axis_moved": False,
    }


def _axis(name: str, n: int) -> dict[str, Any]:
    return {
        "table": "fuel_compensation",
        "coordinate": name,
        "raw": list(range(n)),
        "breakpoints": list(range(n)),
        "values": [float(i * 400) for i in range(n)],
        "unit": "rpm",
        "domain": "rpm",
        "cell_ranges": [[i * 400, i * 400 + 399] for i in range(n)],
    }


def _sim_body(visited: set[tuple[int, int]] = VISITED) -> dict[str, Any]:
    """A simulate response shaped as the daemon writes it (``rom_editor.simulate_edits``:
    the key set and order of the W09.6 payload, 256 cells, every visited cell with all
    six outputs). ``DENSE`` is the form's 224-visited-cell grid."""
    rng = random.Random(20261015)
    cells = [
        _cell(rng, row, col, visited=(row, col) in visited)
        for row in range(16)
        for col in range(16)
    ]
    status_counts: dict[str, dict[str, int]] = {name: {} for name in OUTPUT_NAMES}
    for cell in cells:
        for name, output in cell["outputs"].items():
            counts = status_counts[name]
            counts[output["status"]] = counts.get(output["status"], 0) + 1
    return {
        "tool": "rom_editor",
        "tool_version": "1",
        "label": "simulation",
        "synthetic": True,
        "caveats": [
            "synthetic plant, not validated against the truck",
            "no_points is not unchanged",
        ],
        "assumptions": ["rail pressure is the working assumption", "no purge"],
        "unresolved": ["closed-loop trim authority is undecoded"],
        "inert_parameters": [],
        "coverage": {name: 0.4 for name in OUTPUT_NAMES},
        "outcome": "reached",
        "learned_state": {"stft_pct": 0.0, "ltft_pct": 0.0},
        "provenance": {"draws": 200, "seed": 20261015, "plant_version": "1"},
        "design": {
            "points": len(visited),
            "weight": float(len(visited)),
            "grid": {"rpm_edges": []},
        },
        "candidate": {"label": "K +5 %", "changed": []},
        "cells": {
            "schema_version": 1,
            "tool": "rom_editor",
            "tool_version": "1",
            "grid": "fuel_comp",
            "table": "fuel_compensation",
            "slot": 1,
            "membership": "interpolation_corners",
            "membership_rule": "a point belongs to every cell it interpolates from, by weight",
            "quantiles": [0.05, 0.5, 0.95],
            "quantile_method": "linear",
            "weight_basis": "interpolation weight",
            "base_image_sha256": "b" * 64,
            "candidate_image_sha256": "c" * 64,
            "design_sha256": "d" * 64,
            "design_kind": "grid",
            "result_digest": "e" * 64,
            "draws": 200,
            "n_points": len(visited),
            "outputs": list(OUTPUT_NAMES),
            "headline_by_loop": {
                "open_loop": "delivered_lambda",
                "closed_loop": "fuel_trim_total_pct",
            },
            "rows": _axis("rpm", 16),
            "cols": _axis("load", 16),
            "reach": {
                name: {"status": "reached", "basis": ["fuel_comp_lookup"]} for name in OUTPUT_NAMES
            },
            "caveats": ["synthetic plant, not validated against the truck"],
            "unresolved": ["closed-loop trim authority is undecoded"],
            "rom_outcome": "reached",
            "weight_total": float(len(visited)),
            "unplaced": {},
            "status_counts": status_counts,
            "overlay": {
                "edited_cells": [{"row": 3, "col": 3, "old": 128, "new": 131}],
                "axis_edits": [],
                "other_edits": [],
                "moved_lookups": [],
            },
            "cells": cells,
        },
        "spec_yaml": "# CANary ROM Editor edit spec\nschema_version: 1\n",
        "spec_sha256": "ab" * 32,
    }


def _wire_bytes(value: Any) -> int:
    """The size FastMCP puts on the wire for a dict result (``indent=2`` text)."""
    return len(pydantic_core.to_json(value, indent=2))


def _compact_bytes(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")))


# -- detail: headline / summary / cells / full ----------------------------------------------


def test_detail_values_are_the_four_and_headline_is_the_default() -> None:
    import inspect

    assert rom_editor_tool.SIMULATE_DETAILS == ("headline", "summary", "cells", "full")
    params = inspect.signature(rom_editor_tool.simulate_rom_edit).parameters
    assert params["detail"].default == "headline"
    assert "visited_only" not in params


def test_full_is_the_daemon_body_byte_for_byte() -> None:
    body = _sim_body(DENSE)
    assert rom_editor_tool.shape_simulate_result(body, "full") is body


def test_cells_keeps_exactly_the_visited_cells_and_states_the_omission() -> None:
    body = _sim_body()
    out = rom_editor_tool.shape_simulate_result(body, "cells")
    table = out["cells"]
    assert {(c["row"], c["col"]) for c in table["cells"]} == VISITED
    assert table["cells"] == [c for c in body["cells"]["cells"] if c["n_points"] > 0]
    assert table["cells_omitted"] + len(table["cells"]) == len(body["cells"]["cells"]) == 256
    assert table["cells_omitted"] == 250
    assert table["cells_filter"] == rom_editor_tool.CELLS_FILTER_NOTE
    assert "detail='full'" in table["cells_filter"]
    assert set(table) - set(body["cells"]) == {"cells_omitted", "cells_filter"}
    for key in body:
        if key != "cells":
            assert out[key] == body[key]
    for key in body["cells"]:
        if key != "cells":
            assert table[key] == body["cells"][key]


def _summary(body: dict[str, Any]) -> dict[str, Any]:
    return rom_editor_tool.shape_simulate_result(body, "summary")


def test_summary_keeps_every_top_level_key_in_order_and_whole() -> None:
    body = _sim_body(DENSE)
    out = _summary(body)
    assert list(out) == list(body)
    assert (
        next(k for k in out if k not in ("tool", "tool_version", "label", "synthetic")) == "caveats"
    )
    for key in body:
        if key != "cells":
            assert out[key] == body[key], key  # incl. spec_yaml and spec_sha256


def test_summary_keeps_the_table_level_fields_and_status_counts_untouched() -> None:
    body = _sim_body(DENSE)
    table = _summary(body)["cells"]
    for key, value in body["cells"].items():
        if key != "cells":
            assert table[key] == value, key
    assert table["status_counts"] == body["cells"]["status_counts"]
    # The table says it is a summary before anything else in it.
    assert list(table)[:3] == ["detail", "detail_note", "cells_omitted"]
    assert table["detail"] == "summary"


def test_summary_lists_every_visited_cell_and_counts_the_rest() -> None:
    body = _sim_body(DENSE)
    table = _summary(body)["cells"]
    assert len(table["cells"]) == 224
    assert table["cells_omitted"] + len(table["cells"]) == len(body["cells"]["cells"]) == 256
    assert table["cells_omitted"] == 32
    assert {(c["row"], c["col"]) for c in table["cells"]} == DENSE


def test_summary_cell_rows_are_the_decided_compact_shape() -> None:
    body = _sim_body(DENSE)
    full = {(c["row"], c["col"]): c for c in body["cells"]["cells"]}
    for cell in _summary(body)["cells"]["cells"]:
        src = full[(cell["row"], cell["col"])]
        assert list(cell) == [
            "row", "col", "row_value", "col_value", "n_points", "pairs", "weight",
            "loop_shares", "edited", "row_axis_moved", "col_axis_moved", "outputs",
        ]  # fmt: skip
        for key in cell:
            if key != "outputs":
                assert cell[key] == src[key]
        assert list(cell["outputs"]) == list(src["outputs"])
        for name, out in cell["outputs"].items():
            full_out = src["outputs"][name]
            assert list(out) == ["status", "supported_share", "reasons", "quantiles"]
            assert out["status"] == full_out["status"]  # no status is ever lost
            assert out["supported_share"] == full_out["supported_share"]
            assert out["reasons"] == list(
                dict.fromkeys(r for s in full_out["support"] for r in s["reasons"])
            )
            block, full_block = out["quantiles"], full_out["quantiles"]
            if full_block is None:
                assert block is None
                continue
            assert list(block) == ["over", "delta", "relative_delta"]
            assert block["over"] == full_block["over"]
            assert block["delta"] == full_block["delta"]
            if full_block["relative_delta"] is None:
                assert block["relative_delta"] is None
            else:
                assert block["relative_delta"] == {"p50": full_block["relative_delta"]["p50"]}


def test_summary_keeps_unsupported_reasons_and_unsupported_status() -> None:
    cells = _summary(_sim_body(DENSE))["cells"]["cells"]
    unsupported = [o for c in cells for o in c["outputs"].values() if o["status"] == "unsupported"]
    assert unsupported
    assert all(o["reasons"] == [REASON] and o["supported_share"] == 0.0 for o in unsupported)
    assert any(
        o["status"] == "partly_supported" and o["reasons"]
        for c in cells
        for o in c["outputs"].values()
    )


def test_summary_note_names_everything_it_dropped_and_how_to_get_it() -> None:
    body = _sim_body(DENSE)
    note = _summary(body)["cells"]["detail_note"]
    sample = next(c for c in body["cells"]["cells"] if c["n_points"] > 0)
    kept_cell = {
        "row", "col", "row_value", "col_value", "n_points", "pairs", "weight",
        "loop_shares", "edited", "row_axis_moved", "col_axis_moved", "outputs",
    }  # fmt: skip
    for key in set(sample) - kept_cell:
        assert key in note, key  # row_breakpoint, col_breakpoint, points, regime_shares, by_loop
    kept_output = {"status", "supported_share", "quantiles"}
    for key in set(sample["outputs"]["target_phi"]) - kept_output:
        assert key in note, (
            key
        )  # pairs, unsupported_share, support, changed_share, supported_effect
    block = sample["outputs"]["target_phi"]["quantiles"]
    for key in set(block) - {"over", "delta", "relative_delta"}:
        assert key in note, key
    assert "p05" in note and "p95" in note and "relative_delta" in note
    assert "detail='cells'" in note and "detail='full'" in note
    assert "32 unvisited" in note and "status_counts" in note


def _headline(body: dict[str, Any]) -> dict[str, Any]:
    return rom_editor_tool.shape_simulate_result(body, "headline")


def test_headline_keeps_every_top_level_key_in_order_and_whole() -> None:
    body = _sim_body(DENSE)
    out = _headline(body)
    assert list(out) == list(body)
    assert list(out)[4] == "caveats"
    for key in body:
        if key != "cells":
            assert out[key] == body[key], key


def test_headline_keeps_the_table_skeleton_and_says_it_is_a_headline() -> None:
    body = _sim_body(DENSE)
    table = _headline(body)["cells"]
    for key, value in body["cells"].items():
        if key != "cells":
            assert table[key] == value, key  # axes, outputs, headline_by_loop, status_counts, ...
    assert list(table)[:3] == ["detail", "detail_note", "cells_omitted"]
    assert table["detail"] == "headline"
    assert table["cells_omitted"] + len(table["cells"]) == 256 and table["cells_omitted"] == 32
    assert {(c["row"], c["col"]) for c in table["cells"]} == DENSE


def test_headline_cell_rows_carry_each_loops_headline_output() -> None:
    body = _sim_body(DENSE)
    table = _headline(body)["cells"]
    full = {(c["row"], c["col"]): c for c in body["cells"]["cells"]}
    headline_by_loop = body["cells"]["headline_by_loop"]
    mixed = 0
    for cell in table["cells"]:
        src = full[(cell["row"], cell["col"])]
        assert list(cell) == [
            "row", "col", "row_value", "col_value", "n_points", "edited",
            "row_axis_moved", "col_axis_moved", "loop_shares", "headline",
        ]  # fmt: skip
        for key in cell:
            if key != "headline":
                assert cell[key] == src[key]
        assert [e["loop"] for e in cell["headline"]] == list(src["loop_shares"])
        mixed += len(cell["headline"]) > 1
        for entry, loop in zip(cell["headline"], src["by_loop"], strict=True):
            out = loop["cell"]
            assert entry["output"] == loop["output"] == headline_by_loop[entry["loop"]]
            assert entry["status"] == out["status"]  # the status is never lost
            assert entry["reasons"] == list(
                dict.fromkeys(r for s in out["support"] for r in s["reasons"])
            )
            block = out["quantiles"]
            if block is None:
                assert "over" not in entry
                continue
            assert entry["over"] == block["over"]
            assert entry["delta_p50"] == block["delta"]["p50"]
            relative = block["relative_delta"]
            assert entry["relative_delta_p50"] == (None if relative is None else relative["p50"])
    assert mixed == 16  # column 0 of 16 rows: both loop kinds


def test_headline_keeps_unsupported_status_and_reason_and_the_trim_delta() -> None:
    cells = _headline(_sim_body(DENSE))["cells"]["cells"]
    trim = [e for c in cells for e in c["headline"] if e["output"] == "fuel_trim_total_pct"]
    assert trim
    for entry in trim:
        assert entry["status"] == "unsupported" and entry["reasons"] == [REASON]
        assert entry["over"] == "computed_unsupported_pairs"
        assert entry["relative_delta_p50"] is None and entry["delta_p50"] is not None


def test_headline_note_names_everything_it_dropped_and_the_other_levels() -> None:
    body = _sim_body(DENSE)
    note = _headline(body)["cells"]["detail_note"]
    sample = next(c for c in body["cells"]["cells"] if c["n_points"] > 0)
    kept = {
        "row", "col", "row_value", "col_value", "n_points", "edited",
        "row_axis_moved", "col_axis_moved", "loop_shares",
    }  # fmt: skip
    for key in set(sample) - kept:
        assert key in note, (
            key
        )  # row_breakpoint, points, pairs, weight, regime_shares, outputs, by_loop
    entry = sample["by_loop"][0]
    for key in set(entry) - {"loop", "output", "cell"}:
        assert f"by_loop.{key}" in note, key
    for key in set(entry["cell"]) - {"status", "support", "quantiles"}:
        assert f"by_loop.cell.{key}" in note, key
    for key in set(entry["cell"]["quantiles"]) - {"over", "delta", "relative_delta"}:
        assert f"by_loop.cell.quantiles.{key}" in note, key
    assert "detail='summary'" in note and "detail='cells'" in note and "detail='full'" in note
    assert "32 unvisited" in note and "status_counts" in note


def test_headline_of_sparse_grid_does_not_claim_a_drop_it_did_not_make() -> None:
    table = _headline(_sim_body(set()))["cells"]
    assert table["cells"] == [] and table["cells_omitted"] == 256
    assert "nothing" in table["detail_note"]


# -- changed_only ---------------------------------------------------------------------------

#: A light-load region edit on the form's grid: nearly every visited cell computes
#: a zero change. The cells that must survive, and why.
EDITED = (2, 3)
MOVED = (5, 5)
UNSUPPORTED = (6, 6)
PARTLY = (7, 7)
CHANGED = (8, 8)
MIXED_ONE_CHANGED = (9, 0)
MIXED_BOTH_UNCHANGED = (10, 0)
KEPT = {EDITED, MOVED, UNSUPPORTED, PARTLY, CHANGED, MIXED_ONE_CHANGED}


def _zeroed(output: dict[str, Any]) -> None:
    """Make an ``OutputCell`` a computed ``unchanged``: pairs, support, zero deltas."""
    output.update(
        status="unchanged",
        supported_share=1.0,
        unsupported_share=0.0,
        support=[{"status": "supported", "reasons": [], "pairs": 20, "weight_share": 1.0}],
        changed_share=0.0,
        supported_effect=None,
    )
    zero = {"p05": 0.0, "p50": 0.0, "p95": 0.0}
    output["quantiles"] = {
        "over": "all_pairs",
        "pairs": 20,
        "weight": 1.0,
        "delta": dict(zero),
        "relative_delta": dict(zero),
        "relative_undefined_pairs": 0,
    }


def _region_body() -> dict[str, Any]:
    """The dense grid after a region edit: all visited cells unchanged except KEPT."""
    body = _sim_body(DENSE)
    for cell in body["cells"]["cells"]:
        if cell["n_points"] == 0:
            continue
        where = (cell["row"], cell["col"])
        cell["edited"] = {"old": 128, "new": 131} if where == EDITED else None
        for block in cell["by_loop"]:
            if where == MIXED_ONE_CHANGED and block["loop"] == "closed_loop":
                continue  # keep the fixture's own non-unchanged block
            if where in (UNSUPPORTED, PARTLY, CHANGED):
                continue
            _zeroed(block["cell"])
        if where == CHANGED:
            for block in cell["by_loop"]:
                block["cell"]["status"] = "changed"
        if where == PARTLY:
            for block in cell["by_loop"]:
                block["cell"]["status"] = "partly_supported"
        if where == UNSUPPORTED:
            for block in cell["by_loop"]:
                block["cell"]["status"] = "unsupported"
        if where == MOVED:
            cell["row_axis_moved"] = True
        if where == MIXED_ONE_CHANGED:
            for block in cell["by_loop"]:
                if block["loop"] == "open_loop":
                    _zeroed(block["cell"])
    return body


def _shape(body: dict[str, Any], detail: str) -> dict[str, Any]:
    return rom_editor_tool.shape_simulate_result(body, detail, changed_only=True)


def test_changed_only_is_off_by_default_and_adds_nothing_when_off() -> None:
    import inspect

    params = inspect.signature(rom_editor_tool.simulate_rom_edit).parameters
    assert params["changed_only"].default is False
    body = _region_body()
    for detail in rom_editor_tool.SIMULATE_DETAILS:
        off = rom_editor_tool.shape_simulate_result(body, detail)
        assert "changed_only" not in off["cells"] and "unchanged_omitted" not in off["cells"]
    assert len(_headline(body)["cells"]["cells"]) == 224


@pytest.mark.parametrize("detail", rom_editor_tool.SIMULATE_DETAILS)
def test_changed_only_keeps_exactly_the_cells_that_say_something(detail: str) -> None:
    body = _region_body()
    table = _shape(body, detail)["cells"]
    kept = {(c["row"], c["col"]) for c in table["cells"]}
    assert kept == KEPT  # edited, axis-moved, unsupported/partly/changed, a half-changed mixed cell
    assert (MIXED_BOTH_UNCHANGED) not in kept
    # The three counts add up to the table; cells_omitted is still the no-points count.
    assert table["changed_only"] is True
    assert table["cells_omitted"] == 32
    assert table["unchanged_omitted"] == 224 - len(KEPT)
    assert len(table["cells"]) + table["unchanged_omitted"] + table["cells_omitted"] == 256
    assert table["detail"] == detail
    # Nothing is hidden: the daemon's counts are untouched and still count every cell.
    assert table["status_counts"] == body["cells"]["status_counts"]


@pytest.mark.parametrize("detail", rom_editor_tool.SIMULATE_DETAILS)
def test_changed_only_note_says_the_dropped_cells_are_real_zero_results(detail: str) -> None:
    note = _shape(_region_body(), detail)["cells"]["detail_note"]
    assert "unchanged" in note and "computed" in note and "not a missing result" in note
    assert f"{224 - len(KEPT)} visited cells" in note
    assert "changed_only=False" in note and "status_counts" in note
    assert "cells_omitted" in note and "32" in note


def test_changed_only_full_filters_the_cells_list_and_says_so() -> None:
    body = _region_body()
    out = _shape(body, "full")
    table = out["cells"]
    assert out is not body and table["detail"] == "full"
    assert "changed_only" in table["detail_note"]
    for key, value in body["cells"].items():
        if key != "cells":
            assert table[key] == value, key
    # Kept cells are the daemon's own, whole.
    assert table["cells"] == [c for c in body["cells"]["cells"] if (c["row"], c["col"]) in KEPT]
    # Off, 'full' is still the daemon's body itself.
    assert rom_editor_tool.shape_simulate_result(body, "full", False) is body


def test_changed_only_keeps_visited_cell_rows_whole_at_each_level() -> None:
    body = _region_body()
    full = {(c["row"], c["col"]): c for c in body["cells"]["cells"]}
    assert _shape(body, "cells")["cells"]["cells"] == [full[k] for k in sorted(KEPT)]
    kept = {(c["row"], c["col"]): c for c in _shape(body, "headline")["cells"]["cells"]}
    assert kept[EDITED]["edited"] == {"old": 128, "new": 131}
    assert kept[MOVED]["row_axis_moved"] is True
    unsupported = kept[UNSUPPORTED]["headline"]
    assert unsupported and all(e["status"] == "unsupported" for e in unsupported)
    assert {e["status"] for e in kept[MIXED_ONE_CHANGED]["headline"]} >= {"unchanged"}


def test_changed_only_keeps_a_cell_it_cannot_classify() -> None:
    """No by_loop blocks: nothing says 'unchanged', so the cell is not dropped."""
    body = _region_body()
    victim = next(c for c in body["cells"]["cells"] if (c["row"], c["col"]) == (12, 12))
    victim["by_loop"] = []
    kept = {(c["row"], c["col"]) for c in _shape(body, "headline")["cells"]["cells"]}
    assert kept == KEPT | {(12, 12)}


def test_changed_only_does_not_mutate_and_refuses_a_non_bool() -> None:
    body = _region_body()
    before = json.dumps(body)
    for detail in rom_editor_tool.SIMULATE_DETAILS:
        _shape(body, detail)
    assert json.dumps(body) == before
    with pytest.raises(ValueError, match="changed_only must be true or false"):
        rom_editor_tool.shape_simulate_result(body, "headline", "false")  # type: ignore[arg-type]


def test_measured_size_of_a_region_edit_headline_changed_only() -> None:
    """The light-load-region shape: a handful of cells say something, 218 are a
    computed zero. The daemon's own non-cell content is most of what is left."""
    body = _region_body()
    out = _shape(body, "headline")
    compact, wire = _compact_bytes(out), _wire_bytes(out)
    skeleton = {**out, "cells": {**out["cells"], "cells": []}}
    print(
        f"region edit, detail=headline changed_only: {compact} B compact, {wire} B as FastMCP "
        f"text ({_wire_bytes(skeleton)} B of it with no cells listed)"
    )
    # The fixture's own top-level keys are lean (9.5 KB with no cells). Derived from the
    # real 224-cell payload (all but six cells' headline statuses set to unchanged) the
    # same call measured 28,912 B compact / 37,540 B as FastMCP text (E08.10 grew the
    # assumptions and caveats): the real top-level keys and table skeleton are 24.5 KB compact.
    assert wire < 40_000
    assert wire < 0.25 * _wire_bytes(_headline(body))


async def test_simulate_passes_changed_only_through() -> None:
    body = _region_body()
    client = await _daemon(lambda _r: httpx.Response(200, json=body))
    out = await rom_editor_tool.simulate_rom_edit(
        client, sha=SHA, request=SIM_REQUEST, changed_only=True
    )
    await client.aclose()
    assert out == _shape(body, "headline")


def test_summary_of_sparse_grid_does_not_claim_a_drop_it_did_not_make() -> None:
    """An empty visited list drops nothing, and the note says so instead of naming keys."""
    body = _sim_body(set())
    table = _summary(body)["cells"]
    assert table["cells"] == []
    assert table["cells_omitted"] == 256
    assert "nothing" in table["detail_note"]


def test_shaping_does_not_mutate_the_daemon_body() -> None:
    body = _sim_body(DENSE)
    before = json.dumps(body)
    for detail in rom_editor_tool.SIMULATE_DETAILS:
        rom_editor_tool.shape_simulate_result(body, detail)
    assert json.dumps(body) == before


def test_a_response_without_cells_is_returned_as_it_came() -> None:
    for detail in rom_editor_tool.SIMULATE_DETAILS:
        assert rom_editor_tool.shape_simulate_result({"outcome": "x"}, detail) == {"outcome": "x"}
        grid_only = {"cells": {"grid": "fuel_comp"}}
        assert rom_editor_tool.shape_simulate_result(grid_only, detail) == grid_only


def test_the_dense_fixture_is_the_size_of_the_real_payload() -> None:
    """W09.6 measured 705,543 B on the form's grid; the same grid through the real
    simulator on the synthetic image (scratch run, 2026-10-15; 750,280 B before E08.10 grew
    the assumptions, 757,401 B after) is 757,401 B compact
    with 224 visited cells. The fixture must be that size or the measurements
    below say nothing about a real call."""
    body = _sim_body(DENSE)
    assert sum(1 for c in body["cells"]["cells"] if c["n_points"] > 0) == 224
    assert 600_000 < _compact_bytes(body) < 900_000


def test_measured_sizes_on_the_dense_grid() -> None:
    body = _sim_body(DENSE)
    sizes = {
        detail: (
            _compact_bytes(rom_editor_tool.shape_simulate_result(body, detail)),
            _wire_bytes(rom_editor_tool.shape_simulate_result(body, detail)),
        )
        for detail in ("full", "cells", "summary", "headline")
    }
    for detail, (compact, wire) in sizes.items():
        print(f"224-cell grid, detail={detail}: {compact} B compact JSON, {wire} B as FastMCP text")
    full, cells, summary, headline = (sizes[d][0] for d in ("full", "cells", "summary", "headline"))
    assert headline < summary < cells < full
    # What each level buys, as regression bounds (not the ~100 KB client cap, which the
    # headline does not meet on 224 visited cells: 112 KB compact, 182 KB as FastMCP text
    # on the real run).
    assert summary < 0.5 * full
    assert headline < 0.2 * full


async def test_simulate_defaults_to_headline() -> None:
    body = _sim_body(DENSE)
    client = await _daemon(lambda _r: httpx.Response(200, json=body))
    default = await rom_editor_tool.simulate_rom_edit(client, sha=SHA, request=SIM_REQUEST)
    cells = await rom_editor_tool.simulate_rom_edit(
        client, sha=SHA, request=SIM_REQUEST, detail="cells"
    )
    full = await rom_editor_tool.simulate_rom_edit(
        client, sha=SHA, request=SIM_REQUEST, detail="full"
    )
    summary = await rom_editor_tool.simulate_rom_edit(
        client, sha=SHA, request=SIM_REQUEST, detail="summary"
    )
    await client.aclose()
    assert default == _headline(body)
    assert default["cells"]["detail"] == "headline"
    assert summary == _summary(body)
    assert cells == rom_editor_tool.shape_simulate_result(body, "cells")
    assert json.dumps(full) == json.dumps(body)


@pytest.mark.parametrize("bad", ["Summary", "visited_only", "", "all", "compact"])
async def test_an_unknown_detail_is_refused_by_name_before_the_daemon_is_called(bad: str) -> None:
    seen: list[httpx.Request] = []
    client = await _daemon(lambda _r: httpx.Response(200, json=_sim_body()), seen)
    with pytest.raises(
        ValueError, match="detail must be one of 'headline', 'summary', 'cells', 'full'"
    ) as err:
        await rom_editor_tool.simulate_rom_edit(
            client,
            sha=SHA,
            request=SIM_REQUEST,
            detail=bad,  # type: ignore[arg-type]
        )
    await client.aclose()
    assert repr(bad) in str(err.value)
    assert seen == []  # a 15-minute run is not started for a typo


def test_visited_only_is_gone_from_the_tool_surface() -> None:
    import inspect

    source = inspect.getsource(rom_editor_tool)
    assert "visited_only" not in source
    assert not hasattr(rom_editor_tool, "visited_cells_only")


def test_sim_request_fixture_is_the_whole_contract() -> None:
    """The fixture states every SimRequest field, so a field added to the model
    fails here until the tool's docstring and this fixture learn it."""
    assert set(SIM_REQUEST) == set(SimRequest.model_fields)
    assert len(SIM_REQUEST) == 21
    SimRequest.model_validate(SIM_REQUEST)


def test_simulate_docstring_lists_every_required_field() -> None:
    doc = rom_editor_tool.simulate_rom_edit.__doc__ or ""
    missing = [name for name in SimRequest.model_fields if f"``{name}``" not in doc]
    assert missing == []
    assert "no defaults" in doc


def test_simulate_tool_has_no_default_for_request() -> None:
    import inspect

    params = inspect.signature(rom_editor_tool.simulate_rom_edit).parameters
    assert params["request"].default is inspect.Parameter.empty
    assert params["sha"].default is inspect.Parameter.empty


async def test_preview_posts_label_and_edits_and_returns_body_unchanged() -> None:
    body = {"tool": "rom_editor", "spec_yaml": "schema_version: 1\n", "sim_base": {"ok": True}}
    seen: list[httpx.Request] = []
    client = await _daemon(lambda _r: httpx.Response(200, json=body), seen)
    result = await rom_editor_tool.preview_rom_edit(client, sha=SHA, label="K +5 %", edits=[EDIT])
    await client.aclose()
    assert result == body
    (request,) = seen
    assert request.method == "POST"
    assert request.url.path == f"/control/rom/library/{SHA}/edits/preview"
    assert request.headers["authorization"] == "Bearer secret"
    assert json.loads(request.content) == {"label": "K +5 %", "edits": [EDIT]}


async def test_simulate_posts_request_verbatim_and_returns_body_unchanged() -> None:
    body = _sim_body()
    seen: list[httpx.Request] = []
    client = await _daemon(lambda _r: httpx.Response(200, json=body), seen)
    result = await rom_editor_tool.simulate_rom_edit(
        client, sha=SHA, request=SIM_REQUEST, detail="full"
    )
    await client.aclose()
    assert json.dumps(result) == json.dumps(body)
    (request,) = seen
    assert request.url.path == f"/control/rom/library/{SHA}/simulate"
    # Verbatim: no field added, none dropped, none defaulted.
    assert json.loads(request.content) == SIM_REQUEST


async def test_simulate_waits_out_the_child_timeout() -> None:
    """The simulate child may run 900 s; the HTTP call must outlast it, or a
    slow run reads as a client-side ReadTimeout instead of the daemon's 503."""
    seen: list[httpx.Request] = []
    client = await _daemon(lambda _r: httpx.Response(200, json={}), seen)
    await rom_editor_tool.simulate_rom_edit(client, sha=SHA, request=SIM_REQUEST)
    await rom_editor_tool.preview_rom_edit(client, sha=SHA, label="x", edits=[EDIT])
    await client.aclose()
    sim, preview = seen
    assert sim.extensions["timeout"]["read"] > 900.0
    assert preview.extensions["timeout"]["read"] > 5.0


async def test_malformed_sha_cannot_leave_its_path_segment() -> None:
    seen: list[httpx.Request] = []
    client = await _daemon(lambda _r: httpx.Response(404, json={"detail": "no"}), seen)
    with pytest.raises(DaemonError):
        await rom_editor_tool.preview_rom_edit(client, sha="../x?y=1", label="l", edits=[EDIT])
    await client.aclose()
    (request,) = seen
    assert request.url.raw_path.startswith(b"/control/rom/library/..%2Fx%3Fy%3D1/edits/preview")
    assert request.url.query == b""


REFUSALS: list[tuple[str, int, Any]] = [
    (
        "preview",
        400,
        {
            "error": "edit_refused",
            "kind": "edit_spec",
            "reason": "rounding_undeclared",
            "message": "scale needs rounding",
        },
    ),
    (
        "simulate",
        400,
        {
            "error": "sim_refused",
            "kind": "sim_design",
            "reason": "grid_too_large",
            "message": "2000 cells",
        },
    ),
    ("preview", 404, f"no ROM library entry '{SHA}'"),
    ("simulate", 404, f"no ROM library entry '{SHA}'"),
    ("simulate", 409, "ROM map definition unavailable for this vehicle: rom_map.yaml"),
    (
        "simulate",
        409,
        {
            "error": "analysis_deferred",
            "reason": "capture_active",
            "message": "a capture is recording",
            "laptop_path": "download the image and run the spec on the laptop",
        },
    ),
    (
        "simulate",
        409,
        {"error": "analysis_busy", "reason": "child_busy", "message": "another analysis"},
    ),
    (
        "simulate",
        503,
        {"error": "analysis_unavailable", "reason": "child_timed_out", "message": "900 s"},
    ),
    (
        "simulate",
        422,
        [{"type": "missing", "loc": ["body", "seed"], "msg": "Field required"}],
    ),
    (
        "preview",
        422,
        [{"type": "extra_forbidden", "loc": ["body", "bogus"], "msg": "Extra inputs"}],
    ),
]


@pytest.mark.parametrize(("tool", "status", "detail"), REFUSALS)
async def test_daemon_refusals_reach_the_caller_with_status_and_body(
    tool: str, status: int, detail: Any
) -> None:
    client = await _daemon(lambda _r: httpx.Response(status, json={"detail": detail}))
    with pytest.raises(DaemonError) as exc_info:
        if tool == "preview":
            await rom_editor_tool.preview_rom_edit(client, sha=SHA, label="l", edits=[EDIT])
        else:
            await rom_editor_tool.simulate_rom_edit(client, sha=SHA, request=SIM_REQUEST)
    await client.aclose()
    assert exc_info.value.status == status
    assert exc_info.value.detail == {"detail": detail}
    # The reason is in the message, not only on the attribute.
    for word in (
        [detail["reason"]]
        if isinstance(detail, dict)
        else ["seed"]
        if isinstance(detail, list) and tool == "simulate"
        else []
    ):
        assert word in str(exc_info.value)
