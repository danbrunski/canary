"""ROM Editor read tools (W09.5): preview an edit, simulate it.

Thin wrappers over ``POST /control/rom/library/{sha}/edits/preview`` and
``POST /control/rom/library/{sha}/simulate`` (wire contract in
``docs/headless-api.md``, "ROM Editor: preview and simulate"). Neither route
stores, adds or downloads a candidate image or writes anything the daemon
keeps, so these are read tools: no audit record (a simulate response is
~370 KB and an audit entry would carry all of it). Every refusal reaches the
caller as a :class:`~src.mcp_server.client.DaemonError` whose message embeds
the daemon's ``{"detail": ...}`` body unchanged.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from src.mcp_server.client import DaemonClient

#: The simulate child's own limit is 900 s (``analysis_unavailable`` /
#: ``child_timed_out``); the HTTP call waits it out plus a margin so the
#: daemon's 503 arrives instead of a client-side ``ReadTimeout``.
_SIMULATE_TIMEOUT_SECONDS = 930.0
#: Preview builds one candidate in a threadpool; well past the client's 5 s.
_PREVIEW_TIMEOUT_SECONDS = 60.0


#: Set on ``cells.cells_filter`` whenever the cell list was filtered, so the
#: omission is stated in the payload and cannot be read as "the grid is this small".
CELLS_FILTER_NOTE = (
    "visited_only: cells with n_points == 0 are not included; "
    "pass visited_only=False for the full grid"
)


def visited_cells_only(result: dict[str, Any]) -> dict[str, Any]:
    """The simulate response with ``cells.cells`` cut to the cells a design point visited.

    A pure function: ``result`` is not modified. The kept cells are those with
    ``n_points > 0`` (the unvisited ones carry no measurement; they are most of a
    256-cell table and most of the ~370 KB). ``cells.cells_omitted`` counts the
    dropped cells and ``cells.cells_filter`` says why. Everything else, the
    top-level keys and the table's axes, ``status_counts``, ``overlay``,
    ``reach``, ``unplaced`` and the rest, is the daemon's, untouched, so
    positions and totals still account for the whole grid. A response with no
    ``cells.cells`` list is returned as it came.
    """
    table = result.get("cells")
    cells = table.get("cells") if isinstance(table, dict) else None
    if not isinstance(table, dict) or not isinstance(cells, list):
        return result
    kept = [cell for cell in cells if cell["n_points"] > 0]
    filtered = {
        **table,
        "cells": kept,
        "cells_omitted": len(cells) - len(kept),
        "cells_filter": CELLS_FILTER_NOTE,
    }
    return {**result, "cells": filtered}


def _library_path(sha: str, suffix: str) -> str:
    # ``safe=""``: a sha carrying ``/`` or ``?`` must stay inside the segment,
    # where the daemon answers it with 404, not reach another route.
    return f"/control/rom/library/{quote(sha, safe='')}/{suffix}"


async def preview_rom_edit(
    client: DaemonClient,
    *,
    sha: str,
    label: str,
    edits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Preview edits to a ROM library image: the edit spec and the edited tables.

    Nothing is stored, added to the library or offered for download, and a
    candidate is ``flashable: false`` on purpose. What to keep is the response's
    ``spec_yaml`` (hash: ``spec_sha256``): the same input the simulator takes on
    the laptop. A flash stays the Flash page's own explicit, per-operation act.

    ``sha`` is a library entry's sha256 (``GET /control/rom/library``). ``edits``
    is a list of E08.1 ``Edit`` objects, each ``{table, slot, <selector>,
    <operation>}``: one selector of ``cells: [[row, col], ...]``, ``all: true``
    or ``region: {rpm: [lo, hi], load: [lo, hi], units: {rpm: "rpm", load:
    "%"}}``, and one operation of ``set_raw: int``, ``add_raw: int`` or
    ``scale: float`` with ``rounding: "nearest" | "floor" | "ceil"`` (no default
    rounding). Editable: Map 1 only (``slot: 1``) of ``fuel_targets``,
    ``fuel_compensation`` and ``k_constant``; anything else is refused by name.

    The response carries ``spec``, ``spec_yaml``, ``spec_sha256``,
    ``candidate_sha256``, ``provenance`` (old -> new raw values per cell, the
    changed ranges, ``effect.changed_nodes`` and ``moved_lookups``), ``tables``
    (the candidate's edited tables) and ``sim_base``. ``sim_base.ok`` is whether
    ``simulate_rom_edit`` is offered: false unless the image is a recorded
    revision (``reason: "base_not_a_recorded_revision"``).

    Refusals are raised as :class:`~src.mcp_server.client.DaemonError` with the
    daemon's body in the message; read the status. **400** ``edit_refused`` with
    ``kind`` (``edit_spec`` / ``rom_effect``), ``reason`` (``table_not_editable``,
    ``slot_not_simulated``, ``no_edits``, ``duplicate_edit``, ``no_op_edit``,
    ``value_out_of_range``, ``rounding_undeclared``, ``selector_ambiguous``,
    ``region_unbounded``, ``wrong_program``, ...) and ``message``. **404**: unknown
    or malformed sha. **409**: no ROM map or Editor definition for this vehicle.
    **422**: a missing or unknown body field.
    """
    return await client.post_json(
        _library_path(sha, "edits/preview"),
        {"label": label, "edits": edits},
        timeout=_PREVIEW_TIMEOUT_SECONDS,
    )


async def simulate_rom_edit(
    client: DaemonClient,
    *,
    sha: str,
    request: dict[str, Any],
    visited_only: bool = True,
) -> dict[str, Any]:
    """Simulate edits to a ROM library image: paired Monte Carlo over a stated grid.

    The base must be a recorded revision (``preview_rom_edit``'s
    ``sim_base.ok``). Runs in the workbench child (up to 900 s); the call waits
    it out. Nothing is stored or written. The daemon's response is large (about
    370 KB: every cell of the table, row-major), so by default
    (``visited_only=True``) ``cells.cells`` keeps only the cells a design point
    visited (``n_points > 0``): ``cells.cells_omitted`` counts the dropped cells
    and ``cells.cells_filter`` says so. Nothing else is touched: ``cells`` is
    ``sim_cells.CellTable.to_dict()`` with its axes, ``status_counts``,
    ``overlay``, ``reach`` and ``unplaced`` whole, and ``caveats``,
    ``assumptions``, ``unresolved``, ``coverage`` and ``outcome`` ride beside it
    as the daemon returns them (read the caveats first). Pass
    ``visited_only=False`` for the daemon's body byte for byte, every cell.

    ``request`` is one object and **has no defaults**: this tool invents none,
    and the daemon answers a missing field with a 422 naming it and an unknown
    one with ``extra_forbidden``. All 21 fields are required:

    - ``label`` (non-empty str) and ``edits`` (list of E08.1 ``Edit`` objects,
      as in ``preview_rom_edit``);
    - ``grid``: ``"fuel_comp"`` or ``"target_afr"`` (the aggregation axes);
    - ``rpm_edges``, ``load_edges``: strictly rising cell edges (one design
      point at each cell centre); ``load_edges`` are in Fuel Comp's load axis
      units, % of the fill model's ceiling, within 0-100;
    - ``load_basis``: ``"fill_fraction_as_manifold_ratio"``;
    - ``baro_kpa``, ``iat_c``, ``coolant_c``, ``battery_v``: the environment;
    - ``rail_kpa``: a number, or the string ``"working_assumption"`` (the
      plant's own, said so);
    - ``throttle_pct`` (float), ``pedal_flag`` (int 0-255);
    - ``stft_pct``, ``ltft_pct``: fuel trims, percent;
    - ``throttle_closed_learned``, ``idle_air_learned``, ``evap_purge_area``:
      the learned state, each stated;
    - ``candidate_events``: ``"none"`` (both ROMs start from the same learned
      state) or ``"ecu_reflash"`` (the flash resets what the contract says it
      resets on the candidate);
    - ``draws`` (int >= 1, at most 2000) and ``seed`` (int >= 0).

    The grid is at most 1024 cells. Refusals are raised as
    :class:`~src.mcp_server.client.DaemonError` with the daemon's body in the
    message; read the status. **400** ``sim_refused`` with ``kind``
    (``edit_spec`` / ``rom_effect`` / ``sim_design`` / ``simulation``),
    ``reason`` (any ``preview_rom_edit`` reason, ``grid_edges_invalid``,
    ``load_outside_model``, ``grid_too_large``, ``draws_too_many``,
    ``base_not_a_recorded_revision``, ``uncertainty_missing``) and ``message``.
    **404**: unknown or malformed sha. **409** ``analysis_deferred`` (a capture
    is recording; ``laptop_path`` says how to run the spec on the laptop) or
    ``analysis_busy`` (another analysis is running; retry later), or no ROM map.
    **422**: a missing, unknown or mistyped field. **503** ``analysis_unavailable``
    (``child_timed_out`` / ``child_failed`` / ``child_out_of_memory``).
    """
    result = await client.post_json(
        _library_path(sha, "simulate"), request, timeout=_SIMULATE_TIMEOUT_SECONDS
    )
    return visited_cells_only(result) if visited_only else result


__all__ = ["preview_rom_edit", "simulate_rom_edit", "visited_cells_only"]
