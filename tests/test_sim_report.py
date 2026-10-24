"""E08.6: the self-contained HTML report of a cell table.

Two kinds of input. A *hand-built* table (:func:`hand_table`, validated
through :class:`~src.analysis.sim_cells.CellTable` so it is a real E08.4
table) puts every status on one small grid, plus a closed-loop cell, an
edited cell, a moved column breakpoint and strings that carry markup. A
*run* table comes from ``aggregate_cells`` over ``run_monte_carlo`` on
E07.6's synthetic image, so the page is checked against what the producer
actually writes.
"""

from __future__ import annotations

import copy
import html
import json
import re
import subprocess
import sys
import textwrap
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from src.analysis.rom_effects import CellEdit, make_candidate
from src.analysis.sim_cells import STATUSES, CellTable, aggregate_cells, fuel_comp_axes
from src.analysis.sim_contract import SimulationRefusalError
from src.analysis.sim_design import summary
from src.analysis.sim_montecarlo import UNCERTAINTY_FILE, load_uncertainty
from src.analysis.sim_report import (
    _CELL_H,
    _CELL_W,
    _LEFT,
    _TOP,
    MARK_MEANING,
    PARAMETER_UNCERTAINTY_ONLY,
    RUN_KEYS,
    STRUCTURAL_ERROR_NOT_SAMPLED,
    render_report,
)
from tests.rom_fixtures import PROFILE

BASE_SHA = "a" * 64
CAND_SHA = "c" * 64
DESIGN_SHA = "d" * 64
#: Strings that would be markup if not escaped.
SCRIPT = "<script>alert('x')</script>"
BOLD_REASON = "gate:<b>enrichment</b> & 'state' \"unknown\""
QUALIFICATION = "plant_not_qualified: baseline na-<stock> failed its held-out bounds"
UNRESOLVED = ["battery_voltage_unmeasured", "plant:rail_pressure_unmeasured", BOLD_REASON]

OUTPUTS = [
    "base_fuel_schedule",
    "target_phi",
    "injector_pulse_ms",
    "fuel_trim_total_pct",
    "delivered_lambda",
    "fuel_per_event_g",
]
HEADLINE = {
    "open_loop": "delivered_lambda",
    "closed_loop": "fuel_trim_total_pct",
    "closed_loop_setpoint_moved": "target_phi",
}
ROW_RPM = [800.0, 1600.0, 2400.0]
COL_PCT = [10.0, 20.0, 30.0, 40.0]


def _q(a: float, b: float, c: float) -> dict[str, float]:
    return {"p05": a, "p50": b, "p95": c}


def _oc(
    status: str,
    *,
    rel: tuple[float, float, float] | None = None,
    delta: tuple[float, float, float] = (0.0, 0.0, 0.0),
    over: str = "all_pairs",
    reasons: tuple[str, ...] = (),
    supported: float = 1.0,
    pairs: int = 4,
    weight: float = 2.0,
) -> dict[str, Any]:
    """One output cell in the E08.4 schema."""
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
    support = []
    if supported > 0:
        support.append(
            {"status": "supported", "reasons": [], "pairs": 2, "weight_share": supported}
        )
    if supported < 1:
        support.append(
            {
                "status": "unsupported",
                "reasons": list(reasons),
                "pairs": 2,
                "weight_share": 1 - supported,
            }
        )
    quantiles = None
    if status in ("changed", "unchanged", "partly_supported") or over == (
        "computed_unsupported_pairs"
    ):
        quantiles = {
            "over": over,
            "pairs": pairs,
            "weight": weight,
            "delta": _q(*delta),
            "relative_delta": None if rel is None else _q(*rel),
            "relative_undefined_pairs": 0,
        }
    changed = {"changed": 1.0, "unchanged": 0.0, "partly_supported": 1.0}.get(status)
    return {
        "status": status,
        "pairs": pairs,
        "supported_share": supported,
        "unsupported_share": 1 - supported,
        "support": support,
        "quantiles": quantiles,
        "changed_share": changed,
        "supported_effect": "changed" if status == "partly_supported" else None,
    }


#: (row, col) -> per-output status for the visited cells; open loop unless named.
_VISITED: dict[tuple[int, int], str] = {
    (0, 0): "changed",
    (0, 1): "unchanged",
    (0, 2): "unsupported",
    (1, 0): "partly_supported",
    (1, 1): "closed_loop",
}


def _outputs(kind: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in OUTPUTS:
        if name == "injector_pulse_ms":
            out[name] = _oc("not_reached", supported=1.0)
        elif kind == "closed_loop":
            reach_ok = name in ("base_fuel_schedule", "target_phi")
            out[name] = (
                _oc("unchanged", rel=(0, 0, 0))
                if reach_ok
                else _oc(
                    "unsupported", supported=0.0, reasons=("closed_loop_trim_authority_undecoded",)
                )
            )
        elif kind == "changed":
            out[name] = _oc(
                "changed",
                rel=(-0.031, -0.0158, 0.002),
                delta=(-0.02, -0.011, 0.001),
            )
        elif kind == "unchanged":
            out[name] = _oc("unchanged", rel=(0, 0, 0))
        elif kind == "unsupported":
            out[name] = _oc("unsupported", supported=0.0, reasons=(BOLD_REASON,))
        else:
            out[name] = _oc(
                "partly_supported",
                rel=(0.004, 0.0123, 0.03),
                delta=(0.1, 0.2, 0.3),
                over="supported_pairs",
                reasons=(BOLD_REASON,),
                supported=0.5,
            )
    if kind != "closed_loop":
        out["fuel_trim_total_pct"]["quantiles"] = (
            None
            if out["fuel_trim_total_pct"]["quantiles"] is None
            else {**out["fuel_trim_total_pct"]["quantiles"], "relative_delta": None}
        )
    return out


def _by_loop(kind: str, outputs: dict[str, Any]) -> list[dict[str, Any]]:
    if kind == "closed_loop":
        trim = _oc(
            "unsupported",
            delta=(-2.5, -2.19, -1.7),
            over="computed_unsupported_pairs",
            supported=0.0,
            reasons=("closed_loop_trim_authority_undecoded",),
        )
        return [
            {
                "loop": "closed_loop",
                "weight_share": 1.0,
                "pairs": 4,
                "output": "fuel_trim_total_pct",
                "cell": trim,
            }
        ]
    return [
        {
            "loop": "open_loop",
            "weight_share": 1.0,
            "pairs": 4,
            "output": "delivered_lambda",
            "cell": outputs["delivered_lambda"],
        }
    ]


def hand_table() -> dict[str, Any]:
    """A 3 x 4 Fuel Comp grid with every status (see :data:`_VISITED`)."""
    cells: list[dict[str, Any]] = []
    for r, rpm in enumerate(ROW_RPM):
        for c, pct in enumerate(COL_PCT):
            kind = _VISITED.get((r, c))
            visited = kind is not None
            outputs = _outputs(kind) if kind is not None else {n: _oc("no_points") for n in OUTPUTS}
            cells.append(
                {
                    "row": r,
                    "col": c,
                    "row_breakpoint": int(rpm / 12.5),
                    "col_breakpoint": int(pct * 2.24),
                    "row_value": rpm,
                    "col_value": pct,
                    "n_points": 2 if visited else 0,
                    "points": [2 * (r * 4 + c), 2 * (r * 4 + c) + 1] if visited else [],
                    "pairs": 4 if visited else 0,
                    "weight": 2.0 if visited else 0.0,
                    "regime_shares": {"unknown": 0.25, "closed_loop": 0.75} if visited else {},
                    "loop_shares": (
                        {"closed_loop" if kind == "closed_loop" else "open_loop": 1.0}
                        if visited
                        else {}
                    ),
                    "outputs": outputs,
                    "by_loop": _by_loop(kind, outputs) if kind is not None else [],
                    "edited": {"old": 128, "new": 131} if (r, c) == (0, 0) else None,
                    "row_axis_moved": False,
                    "col_axis_moved": c == 2,
                }
            )
    counts: dict[str, dict[str, int]] = {}
    for name in OUTPUTS:
        tally: dict[str, int] = {}
        for cell in cells:
            s = cell["outputs"][name]["status"]
            tally[s] = tally.get(s, 0) + 1
        counts[name] = tally

    def axis(table: str, coord: str, values: list[float], unit: str, mult: float) -> Any:
        bps = [int(v * mult) for v in values]
        return {
            "table": table,
            "coordinate": coord,
            "raw": bps,
            "breakpoints": bps,
            "values": values,
            "unit": unit,
            "domain": "test <domain>",
            "cell_ranges": [[None, None] for _ in bps],
        }

    data = {
        "schema_version": 1,
        "tool": "sim_cells",
        "tool_version": "1",
        "grid": "fuel_comp",
        "table": "fuel_compensation",
        "slot": 1,
        "membership": "nearest_breakpoint",
        "membership_rule": "nearest breakpoint",
        "quantiles": [0.05, 0.5, 0.95],
        "quantile_method": "inverted_cdf",
        "weight_basis": "pair weight = the point's design weight / draws",
        "base_image_sha256": BASE_SHA,
        "candidate_image_sha256": CAND_SHA,
        "design_sha256": DESIGN_SHA,
        "design_kind": "drive",
        "result_digest": "e" * 64,
        "draws": 2,
        "n_points": 12,
        "outputs": OUTPUTS,
        "headline_by_loop": HEADLINE,
        "rows": axis("fuel_comp_engine_speed_axis", "engine_speed_word", ROW_RPM, "rpm", 1 / 12.5),
        "cols": axis("fuel_comp_flow_axis", "fuel_comp_load_byte", COL_PCT, "%", 2.24),
        "reach": {
            name: (
                {"status": "not_reached", "basis": ["rom:base_fuel_schedule_word:unchanged"]}
                if name == "injector_pulse_ms"
                else {"status": "reached", "basis": ["rom:base_fuel_schedule_word:changed"]}
            )
            for name in OUTPUTS
        },
        "caveats": [
            STRUCTURAL_ERROR_NOT_SAMPLED,
            QUALIFICATION,
            PARAMETER_UNCERTAINTY_ONLY,
            SCRIPT,
        ],
        "unresolved": UNRESOLVED,
        "rom_outcome": "evaluable",
        "weight_total": 12.0,
        "unplaced": {"load_input_x_not_computed": {"pairs": 2, "weight": 2.0}},
        "status_counts": counts,
        "overlay": {
            "edited_cells": [{"row": 0, "col": 0, "old": 128, "new": 131}],
            "axis_edits": [
                {"axis": "cols", "table": "fuel_comp_flow_axis", "index": 2, "old": 67, "new": 70}
            ],
            "other_edits": [{"table": "fuel_targets", "slot": 1, "cells": 3}],
            "moved_lookups": [
                {
                    "node": "fuel_comp_lookup",
                    "tables": ["fuel_comp_flow_axis"],
                    "on_this_grid": True,
                }
            ],
        },
        "cells": cells,
    }
    return CellTable.model_validate(data).to_dict()


def hand_run() -> dict[str, Any]:
    return {
        "label": f"test {SCRIPT}",
        "seed": 4242,
        "base": {"label": "ZS44A-<base>", "sha256": BASE_SHA},
        "candidate": {"label": "candidate", "sha256": CAND_SHA},
        "edit_route": "edit_spec",
        "spec_sha256": "f" * 64,
        "edits": [
            {
                "table": "fuel_compensation",
                "slot": 1,
                "selector": "cells [[0, 0]]",
                "operation": "add_raw: 3",
                "changed": 1,
                "unchanged": 0,
            }
        ],
        "design": None,
        "plant_version": "fuel-mv-1",
        "uncertainty_sha256": "9" * 64,
    }


class _Tags(HTMLParser):
    """The element and attribute names a browser would see (an escaped
    ``&lt;svg onload=`` inside an attribute value is text, not either)."""

    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []
        self.attrs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.names.append(tag)
        self.attrs.extend(name for name, _ in attrs)


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _charts(page: str) -> list[str]:
    return re.findall(r"<svg\b.*?</svg>", page, flags=re.S)


def _cell_groups(chart: str) -> dict[tuple[int, int], tuple[str, str]]:
    """(row, col) -> (mark, the group's markup) for one chart."""
    out = {}
    for mark, body in re.findall(r'<g class="cell ([a-z_]+)">(.*?)</g>', chart, flags=re.S):
        rc = re.search(r"cell \((\d+), (\d+)\)", body)
        assert rc is not None
        out[(int(rc[1]), int(rc[2]))] = (mark, body)
    return out


def _chart(page: str, key: str) -> str:
    fig = re.search(rf'<figure id="{key}">(.*?)</figure>', page, flags=re.S)
    assert fig is not None, key
    return fig[1]


# -- the page's order and labels ----------------------------------------------------------


def test_every_status_has_its_label_and_the_caveats_come_before_the_first_svg() -> None:
    page = render_report(json.dumps(hand_table()), run=hand_run())
    first_svg = page.index("<svg")
    head = page[:first_svg]
    # The caveat block, then images, edits, design, draws and seed, all before any chart.
    order = [head.index(f'id="{s}"') for s in ("caveats", "identity", "edits", "design")]
    order.append(head.index('id="sampling"'))
    assert order == sorted(order)
    assert "The plant is not qualified." in head
    assert "baseline na-&lt;stock&gt; failed its held-out bounds" in head
    for standing in (STRUCTURAL_ERROR_NOT_SAMPLED, PARAMETER_UNCERTAINTY_ONLY):
        assert f"<code>{standing}</code>" in head
    for text in UNRESOLVED:
        assert f"<code>{_esc(text)}</code>" in head
    assert f"<code>{_esc(SCRIPT)}</code>" in head  # every other caveat, as given
    for fact in (BASE_SHA, CAND_SHA, DESIGN_SHA, "ZS44A-&lt;base&gt;", "add_raw: 3", "4242"):
        assert fact in head
    assert "<dt>draws</dt><dd>2</dd>" in head and "<dt>kind</dt><dd><code>drive</code>" in head
    assert "<dt>cells visited</dt><dd>5 of 12</dd>" in head

    # Every status is drawn, with its own fill: a pattern for every non-value mark.
    lam = _cell_groups(_chart(page, "headline-open_loop"))
    bfs = _cell_groups(_chart(page, "output-base_fuel_schedule"))
    pulse = _cell_groups(_chart(page, "output-injector_pulse_ms"))
    marks = {m for g in (lam, bfs, pulse) for m, _ in g.values()}
    assert set(STATUSES) <= marks
    assert lam[(1, 1)][0] == "other_loop" and ">CL</text>" in lam[(1, 1)][1]
    assert pulse[(0, 0)][0] == "not_reached" and ">NR</text>" in pulse[(0, 0)][1]
    assert bfs[(0, 2)][0] == "unsupported" and ">U</text>" in bfs[(0, 2)][1]
    assert bfs[(1, 0)][0] == "partly_supported"
    # Value and supported share, as text.
    assert ">P+1.23</text>" in bfs[(1, 0)][1] and ">\u00b7 50%</text>" in bfs[(1, 0)][1]
    assert bfs[(0, 0)][0] == "changed" and ">-1.58</text>" in bfs[(0, 0)][1]
    assert bfs[(0, 1)][0] == "unchanged" and ">0</text>" in bfs[(0, 1)][1]
    assert bfs[(2, 3)][0] == "no_points"
    for (_r, _c), (mark, body) in bfs.items():
        fill = re.search(r'<rect [^>]*fill="([^"]+)"', body)
        assert fill is not None
        if mark in ("changed", "unchanged"):
            assert fill[1].startswith("#"), mark
        else:
            assert fill[1] == f"url(#m-output-base_fuel_schedule-{mark})", mark
    # partly_supported is never a colour: one rect, its hatch, with no fill under it.
    assert bfs[(1, 0)][1].count("<rect ") == 1
    # Each pattern is defined once per chart, and each mark's legend line is drawn.
    chart = _chart(page, "output-base_fuel_schedule")
    for mark in ("unsupported", "partly_supported", "no_points", "not_reached"):
        assert chart.count(f'<pattern id="m-output-base_fuel_schedule-{mark}"') == 1
    for mark in ("changed", "unchanged", "unsupported", "partly_supported", "no_points"):
        assert _esc(MARK_MEANING[mark]) in chart
    assert _esc(MARK_MEANING["not_reached"]) in _chart(page, "output-injector_pulse_ms")


def test_closed_loop_cells_show_the_trim_headline_as_text_on_a_hatch() -> None:
    page = render_report(hand_table())
    trim = _cell_groups(_chart(page, "headline-closed_loop"))
    mark, body = trim[(1, 1)]
    assert mark == "unsupported"
    assert 'fill="url(#m-headline-closed_loop-unsupported)"' in body
    assert ">-2.19</text>" in body  # the computed p50, text only
    assert "over computed_unsupported_pairs" in body
    assert "closed_loop_trim_authority_undecoded" in body
    # The open-loop cells have no closed-loop pairs: named, not blank.
    assert trim[(0, 0)][0] == "other_loop" and ">OL</text>" in trim[(0, 0)][1]
    # No closed-loop cell is coloured, so the map says so instead of a scale.
    assert "nothing is coloured" in _chart(page, "headline-closed_loop")
    # No setpoint-moved pairs: no map for them, and the page says why.
    assert 'id="headline-closed_loop_setpoint_moved"' not in page
    assert "no closed_loop_setpoint_moved pairs in this run" in page


def test_the_width_map_is_the_p05_to_p95_span_unsigned() -> None:
    page = render_report(hand_table())
    width = _cell_groups(_chart(page, "width-open_loop"))
    # (0, 0): relative -3.1 % .. +0.2 % -> 3.30 percentage points of width.
    assert width[(0, 0)][0] == "changed" and ">3.30</text>" in width[(0, 0)][1]


def test_axes_carry_decoded_breakpoints_and_units_and_moved_breakpoints_are_marked() -> None:
    page = render_report(hand_table())
    chart = _chart(page, "output-base_fuel_schedule")
    for rpm in ("800", "1600", "2400"):
        assert re.search(rf'<text class="tick"[^>]*><title>[^<]*</title>{rpm}</text>', chart)
    assert "fuel_comp_engine_speed_axis (rpm)" in chart
    assert "fuel_comp_flow_axis (%)" in chart
    moved = re.findall(r'<text class="tick moved"[^>]*><title>([^<]*)</title>([^<]*)</text>', chart)
    assert moved == [
        (
            "column 2: 30 % (breakpoint 67, raw 67); moves in the candidate: raw 67 → 70",
            "30",
        )
    ]
    assert chart.count('class="axis-moved"') == 2  # the tick's marker and its legend line


def test_edited_cells_are_outlined_on_every_map() -> None:
    table = hand_table()
    page = render_report(table)
    charts = _charts(page)
    assert len(charts) == 5
    expected = {
        (_LEFT + e["col"] * _CELL_W + 1, _TOP + e["row"] * _CELL_H + 1)
        for e in table["overlay"]["edited_cells"]
    }
    for chart in charts:
        outlined = {
            (int(x), int(y))
            for x, y in re.findall(r'<rect class="edited" x="([\d.]+)" y="([\d.]+)"', chart)
        }
        assert outlined == expected
    # Nothing else is outlined when no cell is edited.
    for cell in table["cells"]:
        cell["edited"] = None
    table["overlay"]["edited_cells"] = []
    assert '<rect class="edited"' not in render_report(table)


def test_per_cell_detail_is_in_the_hover_and_the_table() -> None:
    page = render_report(hand_table())
    _, body = _cell_groups(_chart(page, "output-base_fuel_schedule"))[(1, 0)]
    hover = re.search(r"<title>(.*?)</title>", body, flags=re.S)
    assert hover is not None
    text = hover[1]
    for part in (
        "n 2 points",
        "4 pairs",
        "weight 2 s",
        "regimes (the design&#x27;s recorded label of each point): closed_loop 75.0 %, "
        "unknown 25.0 %",
        "loops (the simulated loop of each pair): open_loop 100.0 %",
        "over supported_pairs: 4 pairs",
        "relative p05/p50/p95 +0.4 / +1.23 / +3 %",
        "supported 50.0 %, unsupported 50.0 %",
        _esc(f"reasons (50.0 %, 2 pairs): {BOLD_REASON}"),
    ):
        assert part in text, part
    edited = re.search(r"<title>(cell \(0, 0\).*?)</title>", page, flags=re.S)
    assert edited is not None and "edited: raw 128 → 131" in edited[1]
    row = re.search(r'<tr id="cell-1-0">(.*?)</tr>', page, flags=re.S)
    assert row is not None
    assert "partly supported" in row[1] and _esc(BOLD_REASON) in row[1]
    # Visited and edited cells have rows; unvisited, unedited ones do not.
    assert len(re.findall(r'<tr id="cell-', page)) == 5
    assert 'id="cell-2-3"' not in page


# -- purity, escaping, no network ---------------------------------------------------------


def _reordered(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _reordered(value[k]) for k in sorted(value, reverse=True)}
    if isinstance(value, list):
        return [_reordered(v) for v in value]
    return value


def test_rendering_is_a_pure_function_of_the_json() -> None:
    table = hand_table()
    text = CellTable.model_validate(table).to_json()
    page = render_report(text, run=hand_run())
    assert render_report(text, run=hand_run()) == page
    assert render_report(text.encode(), run=hand_run()) == page
    assert render_report(table, run=hand_run()) == page
    assert render_report(json.dumps(_reordered(table)), run=_reordered(hand_run())) == page
    moved = copy.deepcopy(table)
    moved["cells"][0]["outputs"]["base_fuel_schedule"]["quantiles"]["relative_delta"]["p50"] = -0.02
    assert render_report(moved, run=hand_run()) != page


def test_every_string_from_the_json_is_escaped() -> None:
    page = render_report(hand_table(), run=hand_run())
    for raw in ("<script", "<b>", "<stock>", "<base>", "<domain>"):
        assert raw not in page, raw
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in page
    assert "gate:&lt;b&gt;enrichment&lt;/b&gt; &amp; &#x27;state&#x27; &quot;unknown&quot;" in page
    # The run's label too, in the title and the heading.
    assert "<title>test &lt;script&gt;" in page and "<h1>test &lt;script&gt;" in page


_FETCHES = re.compile(
    r"https?:|//[a-z0-9]|\b(?:src|href|xlink:href|srcset|action)\s*=|@import|"
    r"url\((?!#)|<(?:link|script|img|iframe|object|embed|base)\b",
    re.I,
)


def test_no_external_url_appears_in_the_output() -> None:
    page = render_report(hand_table(), run=hand_run())
    assert _FETCHES.findall(page) == []
    assert re.findall(r"url\(([^)]*)\)", page)  # patterns are referenced ...
    assert all(ref.startswith("#m-") for ref in re.findall(r"url\(([^)]*)\)", page))  # by fragment
    assert "prefers-color-scheme: dark" in page and 'name="color-scheme"' in page


# -- the run facts ------------------------------------------------------------------------


def test_run_keys_are_named_and_checked_against_the_table() -> None:
    table = hand_table()
    assert set(hand_run()) == set(RUN_KEYS)

    def refused(*, data: Any = None, run: Any = None) -> str:
        with pytest.raises(SimulationRefusalError) as err:
            render_report(table if data is None else data, run=run)
        return err.value.reason

    assert refused(run={"seed": 1, "edit_list": []}) == "run_key_unknown"
    assert refused(run={"seed": "one"}) == "run_invalid"
    assert refused(run={"edits": [{"table": "t", "slot": None}]}) == "run_invalid"
    assert refused(run={"base": {"label": "x", "sha256": "0" * 64}}) == "run_identity_mismatch"
    assert refused(run={"candidate": {"label": "x", "sha256": BASE_SHA}}) == "run_identity_mismatch"
    no_caveat = copy.deepcopy(table)
    no_caveat["caveats"].remove(STRUCTURAL_ERROR_NOT_SAMPLED)
    assert refused(data=no_caveat) == "standing_caveat_missing"
    extra = copy.deepcopy(table)
    extra["note"] = "x"
    assert refused(data=extra) == "cell_table_invalid"
    v2 = copy.deepcopy(table)
    v2["schema_version"] = 2
    assert refused(data=v2) == "cell_table_invalid"
    assert refused(data='{"schema_version": NaN}') == "cell_table_invalid"
    assert refused(data="[1]") == "cell_table_invalid"
    # Without run, every fact the table lacks is said to be missing, not guessed.
    page = render_report(table)
    head = page[: page.index("<svg")]
    for key in ("run.seed", "run.edits", "run.design", "run.edit_route"):
        assert f"not stated by the caller ({key})" in head


# -- on a real run ------------------------------------------------------------------------


def test_a_real_aggregated_run_renders_with_its_design_summary() -> None:
    from tests.test_sim_cells import _design, _run
    from tests.test_sim_montecarlo import build_kit_and_image

    kit, image = build_kit_and_image()
    candidate = make_candidate(
        image, kit.rom_map, [CellEdit("fuel_compensation", 4, 4, 148, slot=1)], base_label="t"
    ).image
    rpms = (1700.0, 2000.0, 2300.0, 4800.0)
    design = _design(image, kit, *(dict(rpm=r, fill_byte_lo=40, fill_byte_hi=100) for r in rpms))
    uncertainty = load_uncertainty(PROFILE / UNCERTAINTY_FILE)
    result, effects = _run(kit, image, candidate, design, uncertainty, draws=6)
    table = aggregate_cells(result, design, fuel_comp_axes(kit, image, slot=1), effects=effects)
    prov = result.provenance
    run = {
        "seed": prov["seed"],
        "base": {"label": "synthetic", "sha256": prov["base_image_sha256"]},
        "candidate": {"label": "fuel comp +20", "sha256": prov["candidate_image_sha256"]},
        "design": summary(design),  # sim_design.summary, key for key
        "plant_version": prov["plant_version"],
        "uncertainty_sha256": prov["uncertainty_sha256"],
    }
    page = render_report(table.to_json(), run=run)
    assert render_report(table.to_json(), run=run) == page
    head = page[: page.index("<svg")]
    assert f"<code>{STRUCTURAL_ERROR_NOT_SAMPLED}</code>" in head
    assert all(f"<code>{_esc(u)}</code>" in head for u in table.unresolved)
    assert f"<dt>seed</dt><dd>{prov['seed']}</dd>" in head
    assert _FETCHES.findall(page) == []
    edited = {(e.row, e.col) for e in table.overlay.edited_cells}
    assert edited == {(4, 4)}
    for chart in _charts(page):
        outlined = re.findall(r'<rect class="edited" x="([\d.]+)" y="([\d.]+)"', chart)
        assert outlined == [(str(_LEFT + 4 * _CELL_W + 1), str(_TOP + 4 * _CELL_H + 1))]
    bfs = _cell_groups(_chart(page, "output-base_fuel_schedule"))
    assert bfs[(4, 4)][0] == table.cell(4, 4).outputs["base_fuel_schedule"].status == "changed"
    assert {m for m, _ in bfs.values()} <= set(STATUSES)
    assert sum(1 for m, _ in bfs.values() if m == "no_points") == sum(
        1 for c in table.cells if not c.pairs
    )


# -- no write path ------------------------------------------------------------------------

_WRITE_PATHS = ("src.control", "src.daemon", "src.dashboard", "src.mcp_server", "can", "serial")
_ALLOWED_COLLECTION = {"src.collection", "src.collection.kline", "src.collection.kline.names"}


def test_rendering_a_report_imports_nothing_that_talks_to_the_ecu() -> None:
    script = textwrap.dedent(
        """
        import json, sys
        from src.analysis.sim_report import render_report
        from tests.test_sim_report import hand_run, hand_table
        render_report(json.dumps(hand_table()), run=hand_run())
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
    assert "src.analysis.sim_report" in loaded
    reached = [
        m
        for m in loaded
        if any(m == p or m.startswith(p + ".") for p in _WRITE_PATHS)
        or (m.startswith("src.collection") and m not in _ALLOWED_COLLECTION)
    ]
    assert reached == []


# -- forged tables, hostile names, and the remaining marks ----------------------------------

HOSTILE = 'x"><svg onload=alert(9)>'


def test_a_loop_name_the_producer_does_not_write_never_reaches_an_id() -> None:
    def refused(table: dict[str, Any]) -> str:
        with pytest.raises(SimulationRefusalError) as err:
            render_report(table)
        return err.value.reason

    in_headline = hand_table()
    in_headline["headline_by_loop"][HOSTILE] = "delivered_lambda"
    assert refused(in_headline) == "loop_unknown"
    in_block = hand_table()
    in_block["cells"][0]["by_loop"][0]["loop"] = HOSTILE
    assert refused(in_block) == "loop_unknown"
    in_shares = hand_table()
    in_shares["cells"][0]["loop_shares"] = {HOSTILE: 1.0}
    assert refused(in_shares) == "loop_unknown"
    wrong_output = hand_table()
    wrong_output["headline_by_loop"]["open_loop"] = "injector_pulse_ms"
    assert refused(wrong_output) == "headline_unknown"


def test_every_id_and_fragment_reference_is_sanitised_even_for_a_hostile_panel_key() -> None:
    from src.analysis.sim_report import _chart, _ident, _Panel, _Pick

    assert _ident(HOSTILE) == "x___svg_onload_alert_9__"
    assert _ident("headline-open_loop") == "headline-open_loop"
    table = CellTable.model_validate(hand_table())
    panel = _Panel(
        key=f"headline-{HOSTILE}",
        title=f"t {HOSTILE}",
        note="",
        unit="",
        scale="diverging",
        pick=lambda cell: _Pick("no_points", None, None, None, "x"),
    )
    chart = _chart(table, panel, "s")
    tags = _Tags()
    tags.feed(chart)
    assert tags.names.count("svg") == 1
    assert not [a for a in tags.attrs if a.startswith("on")]
    ids = re.findall(r'\bid="([^"]*)"', chart)
    assert ids and all(re.fullmatch(r"[A-Za-z0-9_-]+", i) for i in ids)
    refs = re.findall(r"url\(#([^)]*)\)", chart)
    assert refs and all(re.fullmatch(r"[A-Za-z0-9_-]+", r) for r in refs)
    assert set(refs) <= set(ids) | {f"{i}" for i in ids}
    # The same holds for the real page: every id and reference is plain.
    page = render_report(hand_table(), run=hand_run())
    for i in re.findall(r'\bid="([^"]*)"', page):
        assert re.fullmatch(r"[A-Za-z0-9_-]+", i), i
    assert page.count("<svg") == 5


def test_a_forged_quantile_basis_cannot_colour_unsupported_pairs() -> None:
    def refused(mutate: Any) -> str:
        table = hand_table()
        mutate(table)
        with pytest.raises(SimulationRefusalError) as err:
            render_report(table)
        return err.value.reason

    def partly_over_all(t: dict[str, Any]) -> None:
        t["cells"][4]["outputs"]["base_fuel_schedule"]["quantiles"]["over"] = "all_pairs"

    def changed_over_supported(t: dict[str, Any]) -> None:
        t["cells"][0]["outputs"]["base_fuel_schedule"]["quantiles"]["over"] = "supported_pairs"

    def partly_without_quantiles(t: dict[str, Any]) -> None:
        t["cells"][4]["outputs"]["base_fuel_schedule"]["quantiles"] = None

    def block_over_all(t: dict[str, Any]) -> None:
        t["cells"][5]["by_loop"][0]["cell"]["quantiles"]["over"] = "all_pairs"

    assert refused(partly_over_all) == "quantile_basis_mismatch"
    assert refused(changed_over_supported) == "quantile_basis_mismatch"
    assert refused(partly_without_quantiles) == "quantile_basis_mismatch"
    assert refused(block_over_all) == "quantile_basis_mismatch"


def test_partly_supported_is_a_hatch_with_value_and_share_never_a_fill() -> None:
    page = render_report(hand_table())
    for key in ("headline-open_loop", "output-base_fuel_schedule"):
        mark, body = _cell_groups(_chart(page, key))[(1, 0)]
        assert mark == "partly_supported"
        fills = re.findall(r'fill="([^"]+)"', body)
        assert fills == [f"url(#m-{key}-partly_supported)"]
    chart = _chart(page, "output-base_fuel_schedule")
    pattern = re.search(
        r'<pattern id="m-output-base_fuel_schedule-partly_supported".*?</pattern>', chart
    )
    assert pattern is not None and "<rect" in pattern[0]
    # The scale's extent comes from changed/unchanged cells only: (1, 0) at +1.23 % is
    # smaller than (0, 0)'s -1.58 %, so lift (1, 0) far above it and the scale must not move.
    table = hand_table()
    big = table["cells"][4]["outputs"]["base_fuel_schedule"]["quantiles"]
    big["relative_delta"] = {"p05": 0.5, "p50": 0.9, "p95": 1.0}
    page = render_report(table)
    assert "+1.58 %" in _chart(page, "output-base_fuel_schedule")
    assert "+90" not in _chart(page, "output-base_fuel_schedule").split("<title>")[0]


def test_an_undefined_relative_change_and_a_partly_supported_pulse_have_their_own_marks() -> None:
    table = hand_table()
    # Every base is 0: no relative delta, though the delta is computed and supported.
    cell = table["cells"][0]
    cell["outputs"]["base_fuel_schedule"]["quantiles"]["relative_delta"] = None
    cell["outputs"]["base_fuel_schedule"]["quantiles"]["relative_undefined_pairs"] = 4
    # A partly supported pulse (the delta is in ms, not relative).
    pulse = table["cells"][4]["outputs"]["injector_pulse_ms"]
    pulse.update(
        _oc(
            "partly_supported",
            rel=None,
            delta=(0.1, 0.4, 0.9),
            over="supported_pairs",
            reasons=("battery_voltage_unmeasured",),
            supported=0.25,
        )
    )
    page = render_report(table)
    bfs = _cell_groups(_chart(page, "output-base_fuel_schedule"))
    mark, body = bfs[(0, 0)]
    assert mark == "undefined" and ">\u2205</text>" in body
    assert 'fill="url(#m-output-base_fuel_schedule-undefined)"' in body
    assert "relative undefined at 4 pairs (base 0)" in body
    assert _esc(MARK_MEANING["undefined"]) in _chart(page, "output-base_fuel_schedule")
    mark, body = _cell_groups(_chart(page, "output-injector_pulse_ms"))[(1, 0)]
    assert mark == "partly_supported"
    assert ">P+0.40</text>" in body and ">\u00b7 25%</text>" in body
    assert 'fill="url(#m-output-injector_pulse_ms-partly_supported)"' in body
    assert "battery_voltage_unmeasured" in body


def test_a_cell_hover_names_what_else_moves_in_the_same_cell() -> None:
    page = render_report(hand_table())
    # (0, 0) is changed: the BFS map's hover names lambda and target phi, not BFS itself.
    hover = re.search(
        r"<title>(cell \(0, 0\).*?)</title>", _chart(page, "output-base_fuel_schedule"), re.S
    )
    assert hover is not None
    text = hover[1]
    other = next(line for line in text.split("\n") if line.startswith("same cell, other outputs"))
    assert "\u0394\u03bb p50 -1.58 %" in other
    assert "\u0394target \u03c6 p50 -0.011 (-1.58 %)" in other
    assert "\u0394BFS" not in other
    # The lambda map's hover names BFS and target phi; the unchanged cell's target phi, at
    # 0, is not listed, but its BFS is.
    lam = _chart(page, "headline-open_loop")
    unchanged = re.search(r"<title>(cell \(0, 1\).*?)</title>", lam, re.S)
    assert unchanged is not None
    other = next(x for x in unchanged[1].split("\n") if x.startswith("same cell, other"))
    assert "\u0394BFS p50 0 %" in other and "target" not in other
    # A cell nobody visited has no such line.
    title = re.search(r"<title>(cell \(2, 3\).*?)</title>", lam, re.S)
    assert title is not None
    assert "same cell, other" not in title[1]


def test_run_identity_checks_each_catch_their_own_mismatch() -> None:
    table = hand_table()
    summary_run = {
        "sha256": DESIGN_SHA,
        "kind": "drive",
        "base_revision": "r",
        "points": 12,
        "weight": 12.0,
        "weight_basis": "dwell_s",
        "eligible_dwell_s": 12.0,
        "reads": 12,
        "plant_states": {"eligible": 12},
        "placement": {"placed": 12},
        "unplaced_dwell_s": {},
        "weight_by_regime": {"closed_loop": 12.0},
        "points_by_throttle_source": {"pid:0x11": 12},
        "points_by_battery_source": {"measured": 12},
        "battery_unmeasured_points": 0,
        "battery_v_when_unmeasured": None,
        "fill_byte_width": {"max": 4, "median": 2.0},
    }
    # The consistent summary renders, and prints its facts.
    ok = render_report(table, run={"design": summary_run})
    assert "<dt>reads</dt><dd>12</dd>" in ok
    for key, bad in (
        ("sha256", "0" * 64),
        ("kind", "grid"),
        ("points", 13),
        ("weight", 11.0),
    ):
        with pytest.raises(SimulationRefusalError) as err:
            render_report(table, run={"design": {**summary_run, key: bad}})
        assert err.value.reason == "run_identity_mismatch", key
        assert f"design.{key}" in str(err.value), key
