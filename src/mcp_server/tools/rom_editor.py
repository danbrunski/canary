"""ROM Editor read tools (W09.5): preview an edit, simulate it.

Thin wrappers over ``POST /control/rom/library/{sha}/edits/preview`` and
``POST /control/rom/library/{sha}/simulate`` (wire contract in
``docs/headless-api.md``, "ROM Editor: preview and simulate"). Neither route
stores, adds or downloads a candidate image or writes anything the daemon
keeps, so these are read tools: no audit record (a simulate response is
~0.75 MB and an audit entry would carry all of it). Every refusal reaches the
caller as a :class:`~src.mcp_server.client.DaemonError` whose message embeds
the daemon's ``{"detail": ...}`` body unchanged.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from src.mcp_server.client import DaemonClient

#: The simulate child's own limit is 900 s (``analysis_unavailable`` /
#: ``child_timed_out``); the HTTP call waits it out plus a margin so the
#: daemon's 503 arrives instead of a client-side ``ReadTimeout``.
_SIMULATE_TIMEOUT_SECONDS = 930.0
#: Preview builds one candidate in a threadpool; well past the client's 5 s.
_PREVIEW_TIMEOUT_SECONDS = 60.0


SimulateDetail = Literal["headline", "summary", "cells", "full"]
#: Every value ``detail`` takes, default first, smallest result first. The tool
#: refuses anything else before the daemon is called (a simulate run is up to
#: 15 minutes).
SIMULATE_DETAILS: tuple[str, ...] = ("headline", "summary", "cells", "full")

#: Set on ``cells.cells_filter`` for ``detail="cells"``, so the omission is stated
#: in the payload and cannot be read as "the grid is this small".
CELLS_FILTER_NOTE = (
    "detail='cells': cells with n_points == 0 are not included; "
    "pass detail='full' for the full grid"
)

#: What a ``detail="summary"`` cell keeps (everything else is named, from the
#: data, in ``cells.detail_note``).
_SUMMARY_CELL_KEYS = (
    "row",
    "col",
    "row_value",
    "col_value",
    "n_points",
    "pairs",
    "weight",
    "loop_shares",
    "edited",
    "row_axis_moved",
    "col_axis_moved",
)
_SUMMARY_OUTPUT_KEYS = ("status", "supported_share")
_SUMMARY_QUANTILE_KEYS = ("over", "delta", "relative_delta")
#: ``relative_delta`` keeps only these quantiles; p05 and p95 are the first
#: thing given up when a summary is too large (never a cell or a status).
_SUMMARY_RELATIVE_KEYS = ("p50",)


def _summary_output(output: dict[str, Any]) -> dict[str, Any]:
    """One output of a visited cell, as ``detail="summary"`` keeps it."""
    kept: dict[str, Any] = {k: output[k] for k in _SUMMARY_OUTPUT_KEYS if k in output}
    # The distinct reasons across the support entries: "unsupported" and
    # "nothing there" are different answers, and the reason says which.
    kept["reasons"] = list(
        dict.fromkeys(
            reason for entry in output.get("support") or [] for reason in entry.get("reasons") or []
        )
    )
    block = output.get("quantiles")
    if isinstance(block, dict):
        quantiles = {k: block[k] for k in _SUMMARY_QUANTILE_KEYS if k in block}
        relative = quantiles.get("relative_delta")
        if isinstance(relative, dict):
            quantiles["relative_delta"] = {
                k: relative[k] for k in _SUMMARY_RELATIVE_KEYS if k in relative
            }
        kept["quantiles"] = quantiles
    else:
        kept["quantiles"] = block
    return kept


def _summary_cell(cell: dict[str, Any]) -> dict[str, Any]:
    kept = {k: cell[k] for k in _SUMMARY_CELL_KEYS if k in cell}
    kept["outputs"] = {name: _summary_output(o) for name, o in (cell.get("outputs") or {}).items()}
    return kept


def _dropped(items: list[dict[str, Any]], kept: tuple[str, ...]) -> list[str]:
    """Keys present on any of ``items`` that the summary does not keep, in order seen."""
    return list(dict.fromkeys(k for item in items for k in item if k not in kept))


def _summary_note(visited: list[dict[str, Any]], omitted: int) -> str:
    """Names what ``detail="summary"`` dropped, from the cells it was given, so the
    note cannot drift from the code when the daemon adds a key."""
    outputs = [o for cell in visited for o in (cell.get("outputs") or {}).values()]
    blocks = [o["quantiles"] for o in outputs if isinstance(o.get("quantiles"), dict)]
    cell_keys = ", ".join(_dropped(visited, _SUMMARY_CELL_KEYS + ("outputs",)))
    # ``reasons`` is built from ``support``; ``support`` itself is what goes.
    output_keys = ", ".join(_dropped(outputs, _SUMMARY_OUTPUT_KEYS + ("quantiles",)))
    block_keys = ", ".join(_dropped(blocks, _SUMMARY_QUANTILE_KEYS))
    return (
        "detail='summary': one compact row per visited cell (n_points > 0), "
        f"{len(visited)} listed. Each keeps {', '.join(_SUMMARY_CELL_KEYS)} and, per "
        "output, status, supported_share, reasons (the distinct reasons across "
        "support[]) and quantiles {over, delta, relative_delta}. "
        f"Dropped per cell: {cell_keys or 'nothing'}. "
        f"Dropped per output: {output_keys or 'nothing'} (reasons replaces support). "
        f"Dropped per quantile block: {block_keys or 'nothing'}; relative_delta keeps "
        f"{', '.join(_SUMMARY_RELATIVE_KEYS)} only (its p05 and p95 are dropped). "
        f"{omitted} unvisited cells (n_points == 0) are not listed; they are counted in "
        "status_counts and read no_points. "
        "detail='cells' returns every visited cell whole; detail='full' the daemon's body."
    )


#: What a ``detail="headline"`` cell keeps, besides its ``headline`` list.
_HEADLINE_CELL_KEYS = (
    "row",
    "col",
    "row_value",
    "col_value",
    "n_points",
    "edited",
    "row_axis_moved",
    "col_axis_moved",
    "loop_shares",
)
#: ``by_loop`` entry keys the headline reads (``cell`` is that loop's headline
#: output, an ``OutputCell``); the rest of the entry is named as dropped.
_HEADLINE_LOOP_KEYS = ("loop", "output", "cell")
_HEADLINE_OUTPUT_KEYS = ("status", "support", "quantiles")
_HEADLINE_BLOCK_KEYS = ("over", "delta", "relative_delta")


def _headline_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """One loop kind's headline output in a visited cell: its status, why, and
    the median change (``over`` says which pairs the median is over)."""
    output = entry.get("cell") or {}
    kept: dict[str, Any] = {"loop": entry.get("loop"), "output": entry.get("output")}
    kept["status"] = output.get("status")
    kept["reasons"] = list(
        dict.fromkeys(
            reason for item in output.get("support") or [] for reason in item.get("reasons") or []
        )
    )
    block = output.get("quantiles")
    if isinstance(block, dict):
        kept["over"] = block.get("over")
        kept["delta_p50"] = (block.get("delta") or {}).get("p50")
        # The trim is already a percentage: it has no relative delta (None).
        relative = block.get("relative_delta")
        kept["relative_delta_p50"] = relative.get("p50") if isinstance(relative, dict) else None
    return kept


def _headline_cell(cell: dict[str, Any]) -> dict[str, Any]:
    kept = {k: cell[k] for k in _HEADLINE_CELL_KEYS if k in cell}
    kept["headline"] = [_headline_entry(e) for e in cell.get("by_loop") or []]
    return kept


def _headline_note(visited: list[dict[str, Any]], omitted: int) -> str:
    """Names what ``detail="headline"`` dropped, from the cells it was given."""
    entries = [e for cell in visited for e in cell.get("by_loop") or []]
    outputs = [e["cell"] for e in entries if isinstance(e.get("cell"), dict)]
    blocks = [o["quantiles"] for o in outputs if isinstance(o.get("quantiles"), dict)]
    dropped = [
        *_dropped(visited, _HEADLINE_CELL_KEYS),
        *(f"by_loop.{k}" for k in _dropped(entries, _HEADLINE_LOOP_KEYS)),
        *(f"by_loop.cell.{k}" for k in _dropped(outputs, _HEADLINE_OUTPUT_KEYS)),
        *(f"by_loop.cell.quantiles.{k}" for k in _dropped(blocks, _HEADLINE_BLOCK_KEYS)),
    ]
    return (
        "detail='headline': one compact row per visited cell (n_points > 0), "
        f"{len(visited)} listed. Each keeps {', '.join(_HEADLINE_CELL_KEYS)} and "
        "headline: for each loop kind in the cell, its headline output "
        "(headline_by_loop) with status, reasons (the distinct reasons behind an "
        "unsupported share, from support[]), over (which pairs the medians are "
        "over; computed_unsupported_pairs is not a supported result), delta_p50 "
        "and relative_delta_p50 (null where the output has none). "
        f"Dropped per visited cell: {', '.join(dropped) or 'nothing'}. "
        "outputs is the status, support and quantiles of every other output. "
        f"{omitted} unvisited cells (n_points == 0) are not listed; they are counted in "
        "status_counts and read no_points, and status_counts covers every output. "
        "detail='summary' keeps every output's status and quantiles per visited "
        "cell; detail='cells' every visited cell whole; detail='full' the daemon's body."
    )


def _is_unchanged_cell(cell: dict[str, Any]) -> bool:
    """A visited cell whose every headline output is a computed ``unchanged``.

    Never true for an edited cell, a cell on a moved axis breakpoint, or one whose
    headline outputs we cannot read (no ``by_loop``): those are always kept.
    """
    if cell.get("n_points", 0) <= 0:
        return False
    if cell.get("edited") is not None or cell.get("row_axis_moved") or cell.get("col_axis_moved"):
        return False
    blocks = cell.get("by_loop") or []
    return bool(blocks) and all(
        isinstance(block.get("cell"), dict) and block["cell"].get("status") == "unchanged"
        for block in blocks
    )


def _changed_only_note(unchanged_omitted: int, omitted: int) -> str:
    return (
        f"changed_only=True: {unchanged_omitted} visited cells whose every headline "
        "output is unchanged (a computed zero change, not a missing result) are not "
        "listed, and they are not in cells_omitted, which stays the "
        f"{omitted} cells no design point visited (no_points). Cells listed + "
        "unchanged_omitted + cells_omitted = the table's cells. Edited cells, cells on "
        "a moved axis breakpoint and every cell with another status (changed, "
        "partly_supported, unsupported, not_reached, ...) are always listed. "
        "status_counts is the daemon's and still counts every omitted cell. "
        "Pass changed_only=False to list the unchanged cells."
    )


def shape_simulate_result(
    result: dict[str, Any], detail: str, changed_only: bool = False
) -> dict[str, Any]:
    """The simulate response at the requested ``detail``; the one place it is cut.

    A pure function: ``result`` is not modified. ``"full"`` is ``result`` itself,
    the daemon's body byte for byte (only while ``changed_only`` is False).
    ``"cells"`` cuts ``cells.cells`` to the cells
    a design point visited (``n_points > 0``) and states it in
    ``cells.cells_omitted`` / ``cells.cells_filter``; everything else is whole.
    ``"summary"`` and ``"headline"`` keep every top-level key in the daemon's
    order (caveats first) and the table's own fields, and reduce ``cells.cells``
    to one compact row per visited cell (``_summary_cell``: every output's
    status and quantiles; ``_headline_cell``: only each loop kind's headline
    output); ``cells.detail``, ``cells.detail_note`` and ``cells.cells_omitted``
    come first in the table and name what was dropped and how to get it. A
    visited cell is never dropped; unvisited cells are counted in
    ``status_counts``. A response with no ``cells.cells`` list is returned as it
    came. Any other ``detail`` raises :class:`ValueError`.

    ``changed_only`` is applied after the level: it drops the visited cells whose
    every headline output is ``unchanged`` (``_is_unchanged_cell``), and adds
    ``cells.changed_only`` (true), ``cells.unchanged_omitted`` (how many) and a
    ``cells.detail_note`` sentence; ``cells.cells_omitted`` stays the
    no-points count, so listed + ``unchanged_omitted`` + ``cells_omitted`` is
    the table, and ``status_counts`` is never touched. At the ``"full"`` level
    the cells list is filtered the same way (and ``detail`` / ``detail_note``
    are added to say so).
    """
    if detail not in SIMULATE_DETAILS:
        raise ValueError(
            f"detail must be one of {', '.join(map(repr, SIMULATE_DETAILS))}; got {detail!r}"
        )
    if not isinstance(changed_only, bool):
        raise ValueError(f"changed_only must be true or false; got {changed_only!r}")
    table = result.get("cells")
    cells = table.get("cells") if isinstance(table, dict) else None
    if not isinstance(table, dict) or not isinstance(cells, list):
        return result
    if detail == "full" and not changed_only:
        return result
    visited = [cell for cell in cells if cell["n_points"] > 0]
    omitted = len(cells) - len(visited)
    kept = visited
    stated: dict[str, Any] = {}
    if changed_only:
        kept = [cell for cell in visited if not _is_unchanged_cell(cell)]
        stated = {"changed_only": True, "unchanged_omitted": len(visited) - len(kept)}
    note = _changed_only_note(len(visited) - len(kept), omitted) if changed_only else ""
    rest = {k: v for k, v in table.items() if k != "cells"}
    if detail == "full":
        shaped = {
            "detail": "full",
            "detail_note": "detail='full' with " + note,
            **stated,
            "cells_omitted": omitted,
            **rest,
            "cells": kept,
        }
    elif detail == "cells":
        shaped = {
            **table,
            "cells": kept,
            "cells_omitted": omitted,
            "cells_filter": CELLS_FILTER_NOTE,
            **stated,
        }
        if changed_only:
            shaped["detail"] = "cells"
            shaped["detail_note"] = "detail='cells' with " + note
    elif detail == "headline":
        shaped = {
            "detail": "headline",
            "detail_note": " ".join((_headline_note(kept, omitted), note)).strip(),
            **stated,
            "cells_omitted": omitted,
            **rest,
            "cells": [_headline_cell(cell) for cell in kept],
        }
    else:
        shaped = {
            "detail": "summary",
            "detail_note": " ".join((_summary_note(kept, omitted), note)).strip(),
            **stated,
            "cells_omitted": omitted,
            **rest,
            "cells": [_summary_cell(cell) for cell in kept],
        }
    return {**result, "cells": shaped}


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
    (the candidate's edited tables), ``levers`` and ``sim_base``. ``levers`` says
    which lever the edit pulls and, per output, whether a number can be given or
    what blocks it (not mapped, plant not qualified, closed loop: trim
    authority undecoded, and for the ignition outputs the torque stage's
    blockers: "torque production built, not validated (owed H07.10, ...)",
    "spark response unmeasured", "no torque plant" while that stage is
    unbuilt). The last line refuses a torque/power claim for the edit by name
    ("torque/power claim for this edit (<controls>): refused; ..."). Read
    ``levers.messages``, one line each, the same text ``rom_sim inspect``
    prints. ``sim_base.ok`` is whether
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
    detail: SimulateDetail = "headline",
    changed_only: bool = False,
) -> dict[str, Any]:
    """Simulate edits to a ROM library image: paired Monte Carlo over a stated grid.

    The base must be a recorded revision (``preview_rom_edit``'s
    ``sim_base.ok``). Runs in the workbench child (up to 900 s); the call waits
    it out. Nothing is stored or written. The daemon's body is large (757 KB
    on the form's 224-point grid: every cell of the 16 x 16 table, six outputs
    each), so ``detail`` chooses how much of ``cells.cells`` comes back. Every
    level keeps every top-level key whole and in the daemon's order (read
    ``caveats`` first) and the table's own fields whole (axes, ``outputs``,
    ``headline_by_loop``, ``status_counts``, ``overlay``, ``reach``,
    ``unplaced``, hashes); only ``cells.cells`` differs, and
    ``cells.detail`` / ``cells.detail_note`` (first in the table) name what was
    dropped and how to get it. ``cells.cells_omitted`` counts the unvisited
    cells (``n_points == 0``), which ``status_counts`` still counts and which
    read ``no_points``, not ``unchanged``. Sizes are measured on a real
    224-visited-cell run, compact JSON / as the indented text FastMCP sends:

    - ``"headline"`` (the default, 112 KB / 182 KB): one compact row per
      **visited** cell: row, col, row_value, col_value, n_points, edited, the
      two ``*_axis_moved`` flags, loop_shares and ``headline``, one entry per
      loop kind in the cell for that loop's headline output
      (``headline_by_loop``): ``loop``, ``output``, ``status``, ``reasons``
      (the distinct reasons behind an unsupported share), ``over`` (which
      pairs the medians are over; ``computed_unsupported_pairs`` is not a
      supported result), ``delta_p50`` and ``relative_delta_p50`` (null for the
      trim, which has none). The other outputs of a cell are not listed;
      ``status_counts`` has every output's counts.
    - ``"summary"`` (308 KB / 596 KB): the same rows plus every output's
      ``status``, ``supported_share``, ``reasons`` and ``quantiles`` {``over``,
      ``delta`` p05/p50/p95, ``relative_delta`` p50}.
    - ``"cells"`` (715 KB / 1,457 KB): every visited cell whole; unvisited ones
      counted in ``cells.cells_omitted`` / ``cells.cells_filter``.
    - ``"full"`` (757 KB / 1,531 KB): the daemon's body byte for byte.

    No visited cell is dropped at any level unless you ask for ``changed_only``.
    A coarse grid is smaller still.

    ``changed_only=True`` (default ``False``) is for "what did my edit change?",
    where a region edit reaches a handful of cells. Applied after ``detail``, it
    drops the visited cells whose every headline output is ``unchanged`` (a
    computed zero change, not a missing result). It never drops an edited cell,
    a cell on a moved axis breakpoint, or a cell with any other status (changed,
    partly_supported, unsupported, not_reached, ...). The table says so:
    ``cells.changed_only`` is true, ``cells.unchanged_omitted`` counts the
    dropped cells, ``cells.cells_omitted`` stays the cells no design point
    visited, so cells listed + ``unchanged_omitted`` + ``cells_omitted`` is the
    whole table, and ``status_counts`` is the daemon's, still counting every
    cell. ``detail="full"`` is the daemon's body byte for byte only while
    ``changed_only`` is False; with it on, the cells list is filtered the same
    way and ``detail_note`` says so. **For a dense grid prefer
    ``changed_only=True`` or a coarse grid** (the Coarse preset is about a
    quarter of the points): headline alone is 112 KB (182 KB as FastMCP text)
    on 224 visited cells, and an edit that changes few cells drops to the
    daemon's own non-cell content plus a few rows (37.5 KB as FastMCP text,
    derived from the real payload with six cells changed; 24.5 KB of its 28.9 KB
    compact is the non-cell content, so there the top-level keys and table
    skeleton dominate). That content grew with E08.10: ``assumptions`` is 19
    strings (4.4 KB) and ``caveats`` repeats them (4.6 KB), as does
    ``cells.caveats``, 13.5 KB in all; all three stay whole. An edit that
    changes every visited cell (a K scale) loses nothing to it.
    Any other ``detail`` is a ``ValueError`` naming the allowed values, raised
    before the run starts. ``cells`` is ``sim_cells.CellTable.to_dict()``;
    ``caveats``, ``assumptions``, ``unresolved``, ``coverage`` and ``outcome``
    ride beside it as the daemon returns them.

    ``request`` is one object and **has no defaults**: this tool invents none,
    and the daemon answers a missing field with a 422 naming it and an unknown
    one with ``extra_forbidden``. All 23 fields are required:

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
    - ``high_det`` (``"set"`` or ``"clear"``) and ``knock_control``
      (``"active"`` or ``"fallback"``): the ignition chain's two ECU states,
      which nothing logs, so the call states both (E09.7). ``high_det`` is the
      HI DET latch (0xFFFF283F bit 6): warm timing reads trim B while it is
      ``set`` and trim A while it is ``clear``, so an edit to the trim the
      stated latch does not read shows 0 with ``trim_a_not_read:...`` /
      ``trim_b_not_read:...``. ``knock_control`` ``fallback`` is the knock
      sensor fault state; neither implies the other. Both change with the
      truck (the 2026-10-08 read was ``set`` / ``fallback``, KS1 fault), so
      state what you are asking about, not what you last saw. The result's
      ``ignition_timing`` block echoes them (``state``);
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
    # Refused before the run: a bad ``detail`` must not cost up to 15 minutes.
    shape_simulate_result({}, detail, changed_only)
    result = await client.post_json(
        _library_path(sha, "simulate"), request, timeout=_SIMULATE_TIMEOUT_SECONDS
    )
    return shape_simulate_result(result, detail, changed_only)


__all__ = [
    "SIMULATE_DETAILS",
    "SimulateDetail",
    "preview_rom_edit",
    "shape_simulate_result",
    "simulate_rom_edit",
]
