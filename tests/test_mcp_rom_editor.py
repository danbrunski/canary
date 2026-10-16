"""Tests for the ROM Editor MCP tools (W09.5): preview and simulate pass-throughs."""

from __future__ import annotations

import json
from typing import Any

import httpx
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


def _quantiles(scale: float) -> dict[str, float]:
    return {"p05": -0.8 * scale, "p50": 0.1 * scale, "p95": 1.3 * scale}


def _output() -> dict[str, Any]:
    """One ``OutputCell`` as ``CellTable.to_dict()`` writes it, fully populated."""
    block = {
        "over": "supported_pairs",
        "pairs": 200,
        "weight": 0.97,
        "delta": _quantiles(1.0),
        "relative_delta": _quantiles(0.01),
        "relative_undefined_pairs": 0,
    }
    return {
        "status": "changed",
        "pairs": 200,
        "supported_share": 0.97,
        "unsupported_share": 0.03,
        "support": [
            {
                "status": "supported",
                "reasons": [],
                "pairs": 194,
                "weight_share": 0.97,
            },
            {
                "status": "unsupported",
                "reasons": ["trim_authority_undecoded"],
                "pairs": 6,
                "weight_share": 0.03,
            },
        ],
        "quantiles": block,
        "changed_share": 0.88,
        "supported_effect": None,
    }


OUTPUT_NAMES = (
    "base_fuel_schedule",
    "base_fuel_schedule_word",
    "target_phi",
    "target_equiv_ratio",
    "injector_pulse_width",
    "stft_b1",
)


def _cell(row: int, col: int, *, visited: bool) -> dict[str, Any]:
    cell: dict[str, Any] = {
        "row": row,
        "col": col,
        "row_breakpoint": 4000 + 100 * row,
        "col_breakpoint": 30 + 4 * col,
        "row_value": 4000.0 + 100 * row,
        "col_value": 30.0 + 4 * col,
        "n_points": 1 if visited else 0,
        "points": [row * 16 + col] if visited else [],
        "pairs": 200 if visited else 0,
        "weight": 1.0 if visited else 0.0,
        "regime_shares": {"wot": 1.0} if visited else {},
        "loop_shares": {"open_loop": 0.9, "closed_loop": 0.1} if visited else {},
        "outputs": {name: _output() if visited else _empty_output() for name in OUTPUT_NAMES},
        "by_loop": [
            {
                "loop": loop,
                "weight_share": share,
                "pairs": 100,
                "output": "base_fuel_schedule",
                "cell": _output(),
            }
            for loop, share in (("open_loop", 0.9), ("closed_loop", 0.1))
        ]
        if visited
        else [],
        "edited": {"old": 128, "new": 131} if visited else None,
        "row_axis_moved": False,
        "col_axis_moved": False,
    }
    return cell


def _empty_output() -> dict[str, Any]:
    return {
        "status": "no_points",
        "pairs": 0,
        "supported_share": None,
        "unsupported_share": None,
        "support": [],
        "quantiles": None,
        "changed_share": None,
        "supported_effect": None,
    }


VISITED = {(3, 3), (3, 4), (4, 3), (4, 4), (9, 12), (15, 15)}


def _sim_body() -> dict[str, Any]:
    """A simulate response shaped as the doc has it: 256 cells, six visited,
    every visited cell with full outputs."""
    cells = [
        _cell(row, col, visited=(row, col) in VISITED) for row in range(16) for col in range(16)
    ]
    return {
        "tool": "rom_editor",
        "tool_version": "1",
        "label": "simulation",
        "synthetic": True,
        "caveats": ["synthetic plant", "unvalidated"],
        "assumptions": ["rail pressure is the working assumption"],
        "unresolved": [],
        "outcome": "reached",
        "coverage": {"base_fuel_schedule": 0.97},
        "spec_sha256": "ab" * 32,
        "cells": {
            "schema_version": 1,
            "grid": "fuel_comp",
            "table": "fuel_compensation",
            "slot": 1,
            "rows": {"raw": list(range(16)), "breakpoints": list(range(16))},
            "cols": {"raw": list(range(16)), "breakpoints": list(range(16))},
            "reach": {"status": "reached", "basis": ["fuel_comp_lookup"]},
            "status_counts": {"changed": 6, "no_points": 250},
            "overlay": {"edited_cells": [{"row": 3, "col": 3, "old": 128, "new": 131}]},
            "unplaced": None,
            "cells": cells,
        },
    }


def test_visited_only_keeps_exactly_the_visited_cells() -> None:
    body = _sim_body()
    out = rom_editor_tool.visited_cells_only(body)
    kept = out["cells"]["cells"]
    assert {(c["row"], c["col"]) for c in kept} == VISITED
    assert all(c["n_points"] > 0 for c in kept)
    assert kept == [c for c in body["cells"]["cells"] if c["n_points"] > 0]


def test_visited_only_states_the_omission() -> None:
    body = _sim_body()
    out = rom_editor_tool.visited_cells_only(body)
    table = out["cells"]
    assert table["cells_omitted"] + len(table["cells"]) == len(body["cells"]["cells"]) == 256
    assert table["cells_omitted"] == 250
    assert table["cells_filter"] == rom_editor_tool.CELLS_FILTER_NOTE
    assert "visited_only=False" in table["cells_filter"]


def test_visited_only_touches_nothing_else_and_does_not_mutate() -> None:
    body = _sim_body()
    before = json.dumps(body)
    out = rom_editor_tool.visited_cells_only(body)
    assert json.dumps(body) == before
    assert list(out) == list(body)  # same top-level keys, same order (caveats first)
    for key in body:
        if key != "cells":
            assert out[key] == body[key]
    extra = {"cells_omitted", "cells_filter"}
    assert set(out["cells"]) - set(body["cells"]) == extra
    for key in body["cells"]:
        if key != "cells":
            assert out["cells"][key] == body["cells"][key]
    assert out["cells"]["status_counts"] == {"changed": 6, "no_points": 250}


def test_visited_only_leaves_a_response_without_cells_alone() -> None:
    assert rom_editor_tool.visited_cells_only({"outcome": "x"}) == {"outcome": "x"}
    assert rom_editor_tool.visited_cells_only({"cells": {"grid": "fuel_comp"}}) == {
        "cells": {"grid": "fuel_comp"}
    }


def test_visited_only_response_fits_an_mcp_result() -> None:
    body = _sim_body()
    full = len(json.dumps(body))
    filtered = len(json.dumps(rom_editor_tool.visited_cells_only(body)))
    print(f"fixture simulate body: full {full} bytes, visited_only {filtered} bytes")
    assert filtered < 60_000
    assert filtered < full


async def test_simulate_defaults_to_visited_only() -> None:
    body = _sim_body()
    client = await _daemon(lambda _r: httpx.Response(200, json=body))
    default = await rom_editor_tool.simulate_rom_edit(client, sha=SHA, request=SIM_REQUEST)
    full = await rom_editor_tool.simulate_rom_edit(
        client, sha=SHA, request=SIM_REQUEST, visited_only=False
    )
    await client.aclose()
    assert len(default["cells"]["cells"]) == len(VISITED)
    assert default["cells"]["cells_omitted"] == 250
    assert json.dumps(full) == json.dumps(body)


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
        client, sha=SHA, request=SIM_REQUEST, visited_only=False
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
