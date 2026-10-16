"""The ROM-edit simulation report: one self-contained HTML page (E08.6).

:func:`render_report` takes an E08.4 cell table (:meth:`~src.analysis.sim_cells.
CellTable.to_json`, ``schema_version`` 1) and, optionally, the run facts the
table does not carry (:class:`ReportRun`: the seed, the image labels, the edit
list, the design summary), and returns the page as one string. It is a pure
function of its input: the same table and run give the same bytes (every
mapping is walked in sorted order, nothing reads a clock or the environment).

What it makes impossible rather than documented:

* **The caveats come first.** The page opens with the plant's qualification
  (and why not), ``structural_error_not_sampled`` and
  ``parameter_uncertainty_only``, then every other caveat and every
  ``unresolved`` string the run gave, before the first ``<svg>``. A table
  without the two standing caveats is refused (``standing_caveat_missing``):
  E08.3 always writes them, so a table that lacks them did not come from a
  run whose spread can be read.
* **Only a computed value gets a colour.** ``changed`` and ``unchanged`` cells
  are coloured on the dashboard's diverging ramp, centred at zero
  (``lib/colorScale.ts``: blue negative, pale neutral, red positive, the same
  in both themes). ``unsupported``, ``partly_supported``, ``no_points`` and
  ``not_reached`` each have their own hatch and label. ``partly_supported``
  is never a fill: its hatch carries the supported pairs' value and the
  supported share as text. An unsupported cell with computed values (the
  closed-loop trim) shows the number as text on a hatch, never as a colour. A
  cell whose pairs are all in another loop is its own mark, naming the loop it
  does have.
* **Closed loop reads its own headline.** The headline maps follow the table's
  ``headline_by_loop``: each loop kind's pairs show that loop's output (open
  loop relative delta lambda, closed loop the total trim), from the cell's
  ``by_loop`` block, so a closed-loop lambda (the base's by construction) is
  never drawn as a lambda effect.
* **A forged table cannot colour what is unsupported.** A cell whose quantiles are
  over other pairs than its status names is refused (``quantile_basis_mismatch``),
  and a loop name the producer does not write is refused (``loop_unknown``),
  so a name never reaches an element id.
* **Nothing the page shows reaches the network.** CSS and SVG are inline, the
  page has no link, script, font or image, and SVG patterns are referenced
  by fragment only (``url(#...)``).
* **Every string from the JSON is escaped.** A reason or a label may contain
  ``<``; it is text, never markup.
* **The run facts are named, not free-form.** :data:`RUN_KEYS` is every key
  ``run`` may carry; an unknown one is ``run_key_unknown``. An image, design
  or point count in ``run`` that is not the table's is ``run_identity_mismatch``.

Nothing here, or anything it imports, can reach the ECU: see
``tests/test_sim_report.py``'s clean-process import test.
"""

from __future__ import annotations

import html
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.analysis.sim_cells import (
    HEADLINE_BY_LOOP,
    NO_RELATIVE_DELTA,
    STATUSES,
    Cell,
    CellTable,
    OutputCell,
)
from src.analysis.sim_contract import SimulationRefusalError

#: Bump when the page's content or layout changes meaning.
TOOL_VERSION = "1"
TOOL: Final = "sim_report"

#: The two caveats every Monte Carlo result carries (E08.3) and the report
#: prints above the first chart.
STRUCTURAL_ERROR_NOT_SAMPLED: Final = "structural_error_not_sampled"
PARAMETER_UNCERTAINTY_ONLY: Final = "parameter_uncertainty_only"
STANDING_CAVEATS: tuple[str, ...] = (STRUCTURAL_ERROR_NOT_SAMPLED, PARAMETER_UNCERTAINTY_ONLY)
#: The prefix of the plant's qualification reason (``fuel_plant.plant_qualification``).
PLANT_NOT_QUALIFIED: Final = "plant_not_qualified"

#: A heatmap cell's mark: one of the table's statuses, or ``other_loop`` (the
#: cell has pairs, none in this map's loop) or ``undefined`` (the relative
#: delta has no defined pair: every base is 0).
Mark = Literal[
    "changed",
    "unchanged",
    "unsupported",
    "partly_supported",
    "no_points",
    "not_reached",
    "other_loop",
    "undefined",
]
#: The label printed in a cell of each non-value mark, and in the legend.
MARK_LABELS: dict[str, str] = {
    "unsupported": "U",
    "partly_supported": "P",
    "no_points": "–",
    "not_reached": "NR",
    "undefined": "∅",
}
MARK_MEANING: dict[str, str] = {
    "changed": "changed: coloured by the value",
    "unchanged": "unchanged: every supported pair exactly 0",
    "unsupported": "unsupported: no supported pair (hatched; a computed value is text only)",
    "partly_supported": "partly supported: hatched, never coloured; label = supported value, share",
    "no_points": "no points: the design never visited the cell",
    "not_reached": "not reached: compare_roms says the change cannot reach this output",
    "other_loop": "other loop: the cell's pairs are all in the loop named",
    "undefined": "relative change undefined: every pair's base is 0",
    "edited": "edited cell (raw old \u2192 new in the hover)",
    "axis_moved": "a breakpoint the candidate moves (raw old \u2192 new on the tick's hover)",
}
#: Short names for the loops a cell's pairs ran in.
LOOP_ABBREVIATIONS: dict[str, str] = {
    "open_loop": "OL",
    "closed_loop": "CL",
    "closed_loop_setpoint_moved": "SP",
}
#: How each output is named on the page, and the unit of its delta.
OUTPUT_LABELS: dict[str, tuple[str, str]] = {
    "delivered_lambda": ("λ", ""),
    "base_fuel_schedule": ("BFS", "word"),
    "injector_pulse_ms": ("pulse", "ms"),
    "fuel_trim_total_pct": ("total fuel trim", "% points"),
    "target_phi": ("target φ", ""),
    "fuel_per_event_g": ("fuel per event", "g"),
}

# The dashboard's diverging ramp (src/dashboard/static/src/lib/colorScale.ts),
# so a delta here reads like a delta on the Engine Tuning pages.
_DIVERGING: tuple[tuple[int, int, int], ...] = ((96, 150, 235), (224, 229, 234), (230, 80, 65))
# A single-hue ramp for a width (>= 0): pale to violet, light enough for dark text.
_SEQUENTIAL: tuple[tuple[int, int, int], ...] = ((240, 240, 245), (188, 189, 220), (140, 132, 196))
_RAMP_STEPS = 21

_CELL_W = 38
_CELL_H = 22
_LEFT = 70
_TOP = 8
_TICK_GAP = 14
_MIN_WIDTH = 600

# -- the run facts the table does not carry -------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ImageRef(_Strict):
    """A ROM image's human name and its plain-image sha256 (checked against the table)."""

    label: str
    sha256: str


class EditRow(_Strict):
    """One edit as the operator wrote it (E08.1 ``spec_edits``) or one table a
    candidate file changed (E08.1 ``cells``, grouped): every key stated."""

    table: str
    slot: int | None
    #: The selector as written (``cells``/``all``/``region ...``); ``None`` for a file.
    selector: str | None
    #: The operation as written (``add_raw: 3``); ``None`` for a file.
    operation: str | None
    changed: int = Field(ge=0)
    unchanged: int = Field(ge=0)


class FillByteWidth(_Strict):
    max: int
    median: float


class DesignSummary(_Strict):
    """:func:`src.analysis.sim_design.summary`, key for key."""

    sha256: str
    kind: str
    base_revision: str
    points: int = Field(ge=0)
    weight: float
    weight_basis: str
    eligible_dwell_s: float
    reads: int = Field(ge=0)
    plant_states: dict[str, int]
    placement: dict[str, int]
    unplaced_dwell_s: dict[str, float]
    weight_by_regime: dict[str, float]
    points_by_throttle_source: dict[str, int]
    points_by_battery_source: dict[str, int]
    battery_unmeasured_points: int = Field(ge=0)
    battery_v_when_unmeasured: float | None
    fill_byte_width: FillByteWidth | None


class ReportRun(_Strict):
    """What the run knows that the cell table does not. Every key is optional;
    a missing one is printed as not stated, never guessed."""

    label: str | None = None
    seed: int | None = None
    base: ImageRef | None = None
    candidate: ImageRef | None = None
    edit_route: Literal["edit_spec", "candidate_file"] | None = None
    spec_sha256: str | None = None
    edits: list[EditRow] | None = None
    design: DesignSummary | None = None
    plant_version: str | None = None
    uncertainty_sha256: str | None = None


#: Every key ``run`` may carry.
RUN_KEYS: tuple[str, ...] = tuple(ReportRun.model_fields)


# -- input --------------------------------------------------------------------------------


def _refuse_constant(name: str) -> float:
    raise SimulationRefusalError("cell_table_invalid", f"the cell table holds {name}")


def _table(cell_table_json: str | bytes | Mapping[str, Any]) -> CellTable:
    if isinstance(cell_table_json, str | bytes):
        try:
            data = json.loads(cell_table_json, parse_constant=_refuse_constant)
        except json.JSONDecodeError as exc:
            raise SimulationRefusalError("cell_table_invalid", f"not JSON: {exc}") from exc
    else:
        data = cell_table_json
    if not isinstance(data, Mapping):
        raise SimulationRefusalError("cell_table_invalid", "a cell table is a JSON object")
    try:
        table = CellTable.model_validate(dict(data))
    except ValidationError as exc:
        raise SimulationRefusalError(
            "cell_table_invalid",
            f"not an E08.4 cell table (schema_version 1): {exc.error_count()} error(s), "
            f"first {exc.errors()[0]['loc']}: {exc.errors()[0]['msg']}",
        ) from exc
    missing = [c for c in STANDING_CAVEATS if c not in table.caveats]
    if missing:
        raise SimulationRefusalError(
            "standing_caveat_missing",
            f"the table's caveats lack {missing}: every Monte Carlo result states them, and the "
            "report prints them above the first chart",
        )
    _check_loops(table)
    _check_quantile_basis(table)
    return table


#: What each status's quantile block is over (``sim_cells._output_cell``). The
#: colour of a map cell comes from its quantiles, so a table whose block is
#: over other pairs than its status says would colour pairs the status calls
#: unsupported.
_QUANTILES_OVER: dict[str, tuple[str | None, ...]] = {
    "changed": ("all_pairs",),
    "unchanged": ("all_pairs",),
    "partly_supported": ("supported_pairs",),
    "unsupported": (None, "computed_unsupported_pairs"),
    "no_points": (None,),
    "not_reached": (None,),
}


def _check_loops(table: CellTable) -> None:
    """Loop names are the producer's own (:data:`HEADLINE_BY_LOOP`); one that is
    not never reaches the page, whatever it contains."""
    seen = set(table.headline_by_loop)
    for cell in table.cells:
        seen.update(b.loop for b in cell.by_loop)
        seen.update(cell.loop_shares)
    unknown = sorted(seen - set(HEADLINE_BY_LOOP))
    if unknown:
        raise SimulationRefusalError(
            "loop_unknown",
            f"loop names {[_e(u) for u in unknown]} are not ones the cell table's producer "
            f"writes ({sorted(HEADLINE_BY_LOOP)})",
        )
    wrong = {k: v for k, v in table.headline_by_loop.items() if HEADLINE_BY_LOOP[k] != v}
    if wrong:
        raise SimulationRefusalError(
            "headline_unknown",
            f"headline_by_loop {wrong} is not the producer's {HEADLINE_BY_LOOP}",
        )


def _check_quantile_basis(table: CellTable) -> None:
    for cell in table.cells:
        blocks = [(name, oc) for name, oc in cell.outputs.items()]
        blocks += [(f"{b.loop}:{b.output}", b.cell) for b in cell.by_loop]
        for name, oc in blocks:
            over = None if oc.quantiles is None else oc.quantiles.over
            if over not in _QUANTILES_OVER[oc.status]:
                raise SimulationRefusalError(
                    "quantile_basis_mismatch",
                    f"cell ({cell.row}, {cell.col}) {name} is {oc.status} but its quantiles are "
                    f"over {over}: a coloured value must come from the pairs the status names",
                )


def _run(run: Mapping[str, Any] | None, table: CellTable) -> ReportRun:
    if run is None:
        return ReportRun()
    unknown = sorted(set(run) - set(RUN_KEYS))
    if unknown:
        raise SimulationRefusalError(
            "run_key_unknown", f"run keys {unknown} are not report keys (RUN_KEYS: {RUN_KEYS})"
        )
    try:
        out = ReportRun.model_validate(dict(run))
    except ValidationError as exc:
        raise SimulationRefusalError(
            "run_invalid",
            f"{exc.error_count()} error(s), first {exc.errors()[0]['loc']}: "
            f"{exc.errors()[0]['msg']}",
        ) from exc
    wrong: list[str] = []
    if out.base is not None and out.base.sha256 != table.base_image_sha256:
        wrong.append("base.sha256")
    if out.candidate is not None and out.candidate.sha256 != table.candidate_image_sha256:
        wrong.append("candidate.sha256")
    if out.design is not None:
        if out.design.sha256 != table.design_sha256:
            wrong.append("design.sha256")
        if out.design.kind != table.design_kind:
            wrong.append("design.kind")
        if out.design.points != table.n_points:
            wrong.append("design.points")
        if not math.isclose(out.design.weight, table.weight_total, rel_tol=1e-9):
            wrong.append("design.weight")
    if wrong:
        raise SimulationRefusalError(
            "run_identity_mismatch", f"run {wrong} are not the cell table's: another run's facts"
        )
    return out


# -- formatting ---------------------------------------------------------------------------


def _ident(key: str) -> str:
    """A string made safe to be (part of) an element id and an ``url(#id)``."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", key)


def _e(value: object) -> str:
    """Every string that reaches the page goes through here."""
    return html.escape(str(value), quote=True)


def _g(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "0"
    return f"{value:.{digits}g}"


def _signed(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "0"
    return f"{value:+.{digits}g}"


def _pct(share: float | None) -> str:
    return "—" if share is None else f"{share * 100:.1f} %"


def _in_cell(value: float, *, signed: bool = True) -> str:
    """A value short enough to sit in a cell (a width is unsigned)."""
    if value == 0:
        return "0"
    sign = "+" if signed else ""
    mag = abs(value)
    if mag < 10:
        return f"{value:{sign}.2f}"
    if mag < 100:
        return f"{value:{sign}.1f}"
    return f"{value:{sign}.0f}"


def _tick(value: float | None, breakpoint: int) -> str:
    if value is None:
        return str(breakpoint)
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.4g}"


def _hex(rgb: Sequence[float]) -> str:
    return "#" + "".join(f"{int(round(v)):02x}" for v in rgb)


def _ramp(stops: Sequence[tuple[int, int, int]], t: float) -> str:
    t = 0.0 if not math.isfinite(t) else max(0.0, min(1.0, t))
    step = t * (len(stops) - 1)
    low = min(len(stops) - 2, int(math.floor(step)))
    blend = step - low
    a, b = stops[low], stops[low + 1]
    return _hex([a[i] + (b[i] - a[i]) * blend for i in range(3)])


def _output_name(output: str) -> str:
    return OUTPUT_LABELS.get(output, (output, ""))[0]


def _output_unit(output: str) -> str:
    return OUTPUT_LABELS.get(output, (output, ""))[1]


def _loops(cell: Cell) -> str:
    return "/".join(
        LOOP_ABBREVIATIONS.get(b.loop, b.loop[:2].upper())
        for b in sorted(cell.by_loop, key=lambda b: b.loop)
    )


def _shares(shares: Mapping[str, float]) -> str:
    if not shares:
        return "—"
    return ", ".join(f"{k} {_pct(v)}" for k, v in sorted(shares.items()))


# -- what a heatmap shows -----------------------------------------------------------------


@dataclass(frozen=True)
class _Pick:
    mark: Mark
    #: The value the colour encodes (``changed``/``unchanged``), or the label's
    #: (``partly_supported``).
    value: float | None
    #: A computed value shown as text only (an unsupported cell's computed pairs).
    text_value: float | None
    #: The output cell the pick came from, for the hover.
    source: OutputCell | None
    output: str


Quantity = Literal["p50", "width"]


@dataclass(frozen=True)
class _Panel:
    key: str
    title: str
    note: str
    unit: str
    scale: Literal["diverging", "sequential"]
    pick: Callable[[Cell], _Pick]


def _relative(output: str) -> bool:
    return output not in NO_RELATIVE_DELTA


def _value(oc: OutputCell, output: str, quantity: Quantity) -> float | None:
    """p50, or the p05-p95 width, of the relative delta in % (the delta itself
    for an output with no relative delta); ``None`` when undefined."""
    if oc.quantiles is None:
        return None
    if _relative(output):
        q = oc.quantiles.relative_delta
        scale = 100.0
    else:
        q = oc.quantiles.delta
        scale = 1.0
    if q is None:
        return None
    if quantity == "width":
        return (q.p95 - q.p05) * scale
    return q.p50 * scale


def _pick_output(oc: OutputCell, output: str, quantity: Quantity) -> _Pick:
    status = oc.status
    if status in ("no_points", "not_reached"):
        return _Pick(status, None, None, oc, output)
    value = _value(oc, output, quantity)
    if status == "unsupported":
        return _Pick("unsupported", None, value, oc, output)
    if value is None:
        return _Pick("undefined", None, None, oc, output)
    return _Pick(status, value, None, oc, output)


def _headline_pick(loop: str, output: str, quantity: Quantity) -> Callable[[Cell], _Pick]:
    def pick(cell: Cell) -> _Pick:
        if not cell.pairs:
            return _Pick("no_points", None, None, None, output)
        block = next((b for b in cell.by_loop if b.loop == loop), None)
        if block is None:
            return _Pick("other_loop", None, None, None, output)
        return _pick_output(block.cell, output, quantity)

    return pick


def _output_pick(output: str, quantity: Quantity) -> Callable[[Cell], _Pick]:
    def pick(cell: Cell) -> _Pick:
        oc = cell.outputs.get(output)
        if oc is None:
            return _Pick("no_points", None, None, None, output)
        return _pick_output(oc, output, quantity)

    return pick


def _loop_order(loop: str) -> tuple[int, str]:
    return ({"open_loop": 0, "closed_loop": 1}.get(loop, 2), loop)


def _quantity_unit(output: str) -> str:
    return "%" if _relative(output) else _output_unit(output)


def _panels(table: CellTable) -> tuple[list[_Panel], list[str]]:
    panels: list[_Panel] = []
    absent: list[str] = []
    present_loops = {b.loop for c in table.cells for b in c.by_loop}
    for loop, output in sorted(table.headline_by_loop.items(), key=lambda kv: _loop_order(kv[0])):
        if loop not in present_loops:
            absent.append(f"no {loop} pairs in this run, so no {_output_name(output)} map for it")
            continue
        name = _output_name(output)
        what = f"relative Δ{name} p50" if _relative(output) else f"Δ{name} p50"
        panels.append(
            _Panel(
                key=f"headline-{loop}",
                title=f"{what}, {loop} pairs ({output}, the table's headline for {loop})",
                note=(
                    "candidate − base over base; negative is richer"
                    if output == "delivered_lambda"
                    else "candidate − base"
                    + ("" if _relative(output) else "; this output has no relative change")
                ),
                unit=_quantity_unit(output),
                scale="diverging",
                pick=_headline_pick(loop, output, "p50"),
            )
        )
        if loop == "open_loop":
            panels.append(
                _Panel(
                    key=f"width-{loop}",
                    title=f"p05–p95 width of {what[: -len(' p50')]}, {loop} pairs",
                    note="the spread over (point, draw) pairs: parameter uncertainty and the "
                    "cell's operating-point mix, never structural error",
                    unit=_quantity_unit(output),
                    scale="sequential",
                    pick=_headline_pick(loop, output, "width"),
                )
            )
    for output, note in (
        ("base_fuel_schedule", "candidate − base over base; positive is more fuel"),
        ("injector_pulse_ms", "candidate − base, milliseconds"),
    ):
        if output not in table.outputs:
            absent.append(f"the table has no {output}")
            continue
        relative = output == "base_fuel_schedule"
        name = _output_name(output)
        panels.append(
            _Panel(
                key=f"output-{output}",
                title=(
                    f"relative Δ{name} p50, all pairs ({output})"
                    if relative
                    else f"Δ{name} p50, all pairs ({output})"
                ),
                note=note,
                unit="%" if relative else _output_unit(output),
                scale="diverging",
                pick=_output_pick(output, "p50") if relative else _delta_pick(output),
            )
        )
    return panels, absent


def _delta_pick(output: str) -> Callable[[Cell], _Pick]:
    """The absolute delta's p50 (the pulse map is in ms, not relative)."""

    def pick(cell: Cell) -> _Pick:
        oc = cell.outputs.get(output)
        if oc is None:
            return _Pick("no_points", None, None, None, output)
        base = _pick_output(oc, output, "p50")
        if base.mark in ("no_points", "not_reached"):
            return base
        delta = None if oc.quantiles is None else oc.quantiles.delta.p50
        if base.mark == "unsupported":
            return _Pick("unsupported", None, delta, oc, output)
        if delta is None:
            return _Pick("undefined", None, None, oc, output)
        mark: Mark = "partly_supported" if oc.status == "partly_supported" else oc.status
        return _Pick(mark, delta, None, oc, output)

    return pick


# -- per-cell detail ----------------------------------------------------------------------


def _quantile_lines(oc: OutputCell, output: str) -> list[str]:
    q = oc.quantiles
    if q is None:
        return []
    unit = _output_unit(output)
    lines = [
        f"over {q.over}: {q.pairs} pairs, weight {_g(q.weight)}",
        f"Δ p05/p50/p95 {_signed(q.delta.p05)} / {_signed(q.delta.p50)} / "
        f"{_signed(q.delta.p95)}{(' ' + unit) if unit else ''}",
    ]
    if q.relative_delta is not None:
        r = q.relative_delta
        lines.append(
            f"relative p05/p50/p95 {_signed(r.p05 * 100)} / {_signed(r.p50 * 100)} / "
            f"{_signed(r.p95 * 100)} %"
        )
    elif _relative(output):
        lines.append("relative: undefined")
    if q.relative_undefined_pairs:
        lines.append(f"relative undefined at {q.relative_undefined_pairs} pairs (base 0)")
    return lines


def _support_lines(oc: OutputCell) -> list[str]:
    lines: list[str] = []
    if oc.supported_share is not None:
        lines.append(
            f"supported {_pct(oc.supported_share)}, unsupported {_pct(oc.unsupported_share)}"
        )
    if oc.changed_share is not None:
        lines.append(f"changed share of supported weight {_pct(oc.changed_share)}")
    if oc.supported_effect is not None:
        lines.append(f"supported pairs show: {oc.supported_effect}")
    for entry in oc.support:
        if entry.reasons:
            lines.append(
                f"reasons ({_pct(entry.weight_share)}, {entry.pairs} pairs): "
                + ", ".join(entry.reasons)
            )
    return lines


def _cell_head(table: CellTable, cell: Cell, weight_unit: str) -> list[str]:
    rows, cols = table.rows, table.cols
    lines = [
        f"cell ({cell.row}, {cell.col}) · {rows.unit or 'raw'} "
        f"{_tick(cell.row_value, cell.row_breakpoint)} · {cols.unit or 'raw'} "
        f"{_tick(cell.col_value, cell.col_breakpoint)}",
    ]
    if cell.pairs:
        lines.append(
            f"n {cell.n_points} points · {cell.pairs} pairs · weight "
            f"{_g(cell.weight)} {weight_unit} ({_pct(cell.weight / table.weight_total)} of the run)"
        )
        lines.append(
            f"regimes (the design's recorded label of each point): {_shares(cell.regime_shares)}"
        )
        lines.append(f"loops (the simulated loop of each pair): {_shares(cell.loop_shares)}")
    else:
        lines.append("no points: the design never visited this cell")
    if cell.edited is not None:
        lines.append(f"edited: raw {cell.edited.old} → {cell.edited.new}")
    if cell.row_axis_moved:
        lines.append(f"row breakpoint moves ({rows.table})")
    if cell.col_axis_moved:
        lines.append(f"column breakpoint moves ({cols.table})")
    return lines


def _p50(oc: OutputCell, output: str, *, absolute: bool = False) -> str:
    """One output's median change in a few words, with its status when not plain.
    ``absolute`` gives the delta itself, and the relative one after it."""
    q = oc.quantiles
    status = oc.status.replace("_", " ")
    if q is None:
        return status
    relative = q.relative_delta
    if absolute:
        text = _signed(q.delta.p50)
        if relative is not None:
            text += f" ({_signed(relative.p50 * 100)} %)"
    elif _relative(output) and relative is not None:
        text = f"{_signed(relative.p50 * 100)} %"
    else:
        text = _signed(q.delta.p50)
    if oc.status == "partly_supported":
        return f"{text} (partly supported, {_pct(oc.supported_share)} supported)"
    if oc.status == "unsupported":
        return f"{text} (unsupported, computed only)"
    return text


def _other_outputs(cell: Cell, shown: str) -> str | None:
    """What else moves in this cell, over all its pairs: ΔBFS and Δλ always, and
    the target phi change when it moves (a candidate whose load axis shifts
    changes the column the target is read from, which a lambda map alone hides)."""
    parts: list[str] = []
    for output, name in (
        ("base_fuel_schedule", "\u0394BFS p50"),
        ("delivered_lambda", "\u0394\u03bb p50"),
        ("target_phi", "\u0394target \u03c6 p50"),
    ):
        oc = cell.outputs.get(output)
        if oc is None or output == shown:
            continue
        q = oc.quantiles
        if output == "target_phi" and (
            q is None or not any((q.delta.p05, q.delta.p50, q.delta.p95))
        ):
            continue
        text = _p50(oc, output, absolute=output == "target_phi")
        parts.append(f"{name} {text}")
    return "same cell, other outputs (all pairs): " + " \u00b7 ".join(parts) if parts else None


def _hover(table: CellTable, cell: Cell, pick: _Pick, weight_unit: str) -> str:
    lines = _cell_head(table, cell, weight_unit)
    if cell.pairs:
        lines.append(f"{pick.output}: {pick.mark.replace('_', ' ')}")
        if pick.mark == "other_loop":
            lines.append(f"this cell's pairs are {_loops(cell)}")
        if pick.source is not None:
            lines.extend(_quantile_lines(pick.source, pick.output))
            lines.extend(_support_lines(pick.source))
        other = _other_outputs(cell, pick.output)
        if other is not None:
            lines.append(other)
    return "\n".join(lines)


# -- SVG ----------------------------------------------------------------------------------

_PATTERN_INK = "#59636e"


def _defs(pid: str) -> str:
    """Per-chart patterns: fixed colours, the same in both themes (data, not chrome)."""
    ink = _PATTERN_INK
    return (
        "<defs>"
        f'<pattern id="{pid}-unsupported" width="6" height="6" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)">'
        '<rect width="6" height="6" fill="#e3e6ea"/>'
        f'<line x1="0" y1="0" x2="0" y2="6" stroke="{ink}" stroke-width="2.2"/></pattern>'
        f'<pattern id="{pid}-not_reached" width="6" height="6" patternUnits="userSpaceOnUse">'
        '<rect width="6" height="6" fill="#e3e6ea"/>'
        f'<line x1="0" y1="3" x2="6" y2="3" stroke="{ink}" stroke-width="1.6"/></pattern>'
        f'<pattern id="{pid}-no_points" width="6" height="6" patternUnits="userSpaceOnUse">'
        '<rect width="6" height="6" fill="#f7f8fa"/>'
        '<circle cx="3" cy="3" r="0.9" fill="#8a96a3"/></pattern>'
        f'<pattern id="{pid}-partly_supported" width="5" height="5" '
        'patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">'
        '<rect width="5" height="5" fill="#f4f1e6"/>'
        f'<line x1="0" y1="0" x2="0" y2="5" stroke="{ink}" stroke-width="1"/>'
        f'<line x1="0" y1="2.5" x2="5" y2="2.5" stroke="{ink}" stroke-width="1"/></pattern>'
        f'<pattern id="{pid}-other_loop" width="8" height="8" patternUnits="userSpaceOnUse">'
        '<rect width="8" height="8" fill="#eef0f3"/>'
        '<path d="M0 4H8M4 0V8" stroke="#9aa5b1" stroke-width="1"/></pattern>'
        f'<pattern id="{pid}-undefined" width="8" height="8" patternUnits="userSpaceOnUse">'
        '<rect width="8" height="8" fill="#eef0f3"/>'
        '<path d="M1 1L7 7M7 1L1 7" stroke="#9aa5b1" stroke-width="1"/></pattern>'
        "</defs>"
    )


def _colour(panel: _Panel, value: float, extent: float) -> str:
    if panel.scale == "diverging":
        return _ramp(_DIVERGING, 0.5 + (value / extent if extent > 0 else 0.0) / 2)
    return _ramp(_SEQUENTIAL, value / extent if extent > 0 else 0.0)


#: Only a value every pair supports is a colour. ``partly_supported`` is a
#: hatch and a label (its value and its supported share), never a fill: a
#: colour would read as the cell's value when part of its weight is unsupported.
COLOURED_MARKS: tuple[str, ...] = ("changed", "unchanged")


def _cell_lines(pick: _Pick, cell: Cell, *, signed: bool) -> list[str]:
    """The text in a map cell, one or two short lines."""
    if pick.mark in ("changed", "unchanged"):
        assert pick.value is not None
        return ["0" if pick.mark == "unchanged" else _in_cell(pick.value, signed=signed)]
    if pick.mark == "partly_supported":
        assert pick.value is not None and pick.source is not None
        share = pick.source.supported_share
        return [
            f"P{_in_cell(pick.value, signed=signed)}",
            "\u00b7 " + ("?" if share is None else f"{share * 100:.0f}%"),
        ]
    if pick.mark == "unsupported" and pick.text_value is not None:
        # The hatch says unsupported; the computed number is text, never a colour.
        return [_in_cell(pick.text_value, signed=signed)]
    if pick.mark == "other_loop":
        return [_loops(cell)]
    if pick.mark == "no_points":
        return []  # the dots say it; most of a grid is unvisited, and a label there is noise
    return [MARK_LABELS[pick.mark]]


def _chart(table: CellTable, panel: _Panel, weight_unit: str) -> str:
    n_rows, n_cols = len(table.rows.breakpoints), len(table.cols.breakpoints)
    pid = f"m-{_ident(panel.key)}"
    picks = [(cell, panel.pick(cell)) for cell in table.cells]
    coloured = [abs(p.value) for _, p in picks if p.mark in COLOURED_MARKS and p.value is not None]
    extent = max(coloured) if coloured else 0.0
    grid_w, grid_h = n_cols * _CELL_W, n_rows * _CELL_H
    legend_y = _TOP + grid_h + 46
    # Wide enough for the longest legend line, however few columns the grid has.
    width = max(_LEFT + grid_w + 12, _MIN_WIDTH)
    marks_in_use: list[str] = sorted({p.mark for _, p in picks}, key=_mark_order)
    if any(c.edited is not None for c in table.cells):
        marks_in_use.append("edited")
    if table.overlay.axis_edits:
        marks_in_use.append("axis_moved")
    height = legend_y + 40 + len(marks_in_use) * 18
    out = [
        f'<svg class="heatmap" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_e(panel.title)}">',
        _defs(pid),
    ]
    edited: list[str] = []
    for cell, pick in picks:
        x = _LEFT + cell.col * _CELL_W
        y = _TOP + cell.row * _CELL_H
        title = _e(_hover(table, cell, pick, weight_unit))
        out.append(f'<g class="cell {pick.mark}"><title>{title}</title>')
        if pick.mark in COLOURED_MARKS:
            assert pick.value is not None
            fill = _colour(panel, pick.value, extent)
        else:
            fill = f"url(#{pid}-{pick.mark})"
        out.append(f'<rect x="{x}" y="{y}" width="{_CELL_W}" height="{_CELL_H}" fill="{fill}"/>')
        lines = _cell_lines(pick, cell, signed=panel.scale == "diverging")
        for k, text in enumerate(lines):
            two = len(lines) == 2
            ty = y + _CELL_H / 2 + (-1.0 + 8.5 * k if two else 3.5)
            out.append(
                f'<text class="v{" small" if two else ""}" x="{x + _CELL_W / 2:g}" '
                f'y="{ty:g}">{_e(text)}</text>'
            )
        out.append("</g>")
        if cell.edited is not None:
            edited.append(
                f'<rect class="edited" x="{x + 1:g}" y="{y + 1:g}" width="{_CELL_W - 2}" '
                f'height="{_CELL_H - 2}"/>'
                f'<rect class="edited-inner" x="{x + 2.2:g}" y="{y + 2.2:g}" '
                f'width="{_CELL_W - 4.4:g}" height="{_CELL_H - 4.4:g}"/>'
            )
    out.append(f'<rect class="frame" x="{_LEFT}" y="{_TOP}" width="{grid_w}" height="{grid_h}"/>')
    out.extend(edited)
    out.append(_axes(table, grid_w, grid_h))
    out.append(_legend(panel, pid, extent, legend_y, grid_w, marks_in_use, coloured=bool(coloured)))
    out.append("</svg>")
    return "".join(out)


def _mark_order(mark: str) -> int:
    order = [*STATUSES, "other_loop", "undefined"]
    return order.index(mark)


def _axes(table: CellTable, grid_w: int, grid_h: int) -> str:
    rows, cols = table.rows, table.cols
    moved_rows = {e.index: e for e in table.overlay.axis_edits if e.axis == "rows"}
    moved_cols = {e.index: e for e in table.overlay.axis_edits if e.axis == "cols"}
    out: list[str] = []
    for i, (bp, value) in enumerate(zip(rows.breakpoints, rows.values, strict=True)):
        y = _TOP + i * _CELL_H + _CELL_H / 2
        moved = moved_rows.get(i)
        cls = "tick moved" if moved else "tick"
        label = _tick(value, bp)
        hover = f"row {i}: {label} {rows.unit or 'raw'} (breakpoint {bp}, raw {rows.raw[i]})"
        if moved:
            hover += f"; moves in the candidate: raw {moved.old} → {moved.new}"
            out.append(
                f'<path class="axis-moved" d="M{_LEFT - 2} {y:g}l-6 -4v8z"><title>{_e(hover)}'
                "</title></path>"
            )
        out.append(
            f'<text class="{cls}" x="{_LEFT - 10}" y="{y + 3.5:g}" text-anchor="end">'
            f"<title>{_e(hover)}</title>{_e(label)}</text>"
        )
    for j, (bp, value) in enumerate(zip(cols.breakpoints, cols.values, strict=True)):
        x = _LEFT + j * _CELL_W + _CELL_W / 2
        moved = moved_cols.get(j)
        cls = "tick moved" if moved else "tick"
        label = _tick(value, bp)
        hover = f"column {j}: {label} {cols.unit or 'raw'} (breakpoint {bp}, raw {cols.raw[j]})"
        y = _TOP + grid_h
        if moved:
            hover += f"; moves in the candidate: raw {moved.old} → {moved.new}"
            out.append(
                f'<path class="axis-moved" d="M{x:g} {y + 2}l-4 6h8z"><title>{_e(hover)}'
                "</title></path>"
            )
        out.append(
            f'<text class="{cls}" x="{x:g}" y="{y + _TICK_GAP + 4}" text-anchor="middle">'
            f"<title>{_e(hover)}</title>{_e(label)}</text>"
        )
    out.append(
        f'<text class="axis-title" x="{_LEFT + grid_w / 2:g}" y="{_TOP + grid_h + 36}" '
        f'text-anchor="middle">{_e(cols.table)} ({_e(cols.unit or "raw")})</text>'
    )
    cy = _TOP + grid_h / 2
    out.append(
        f'<text class="axis-title" x="12" y="{cy:g}" text-anchor="middle" '
        f'transform="rotate(-90 12 {cy:g})">{_e(rows.table)} ({_e(rows.unit or "raw")})</text>'
    )
    return "".join(out)


def _legend(
    panel: _Panel,
    pid: str,
    extent: float,
    y: int,
    grid_w: int,
    marks: Sequence[str],
    *,
    coloured: bool,
) -> str:
    out: list[str] = []
    if not coloured:
        out.append(
            f'<text class="legend" x="{_LEFT}" y="{y + 10}">no cell in this map holds a '
            "supported value, so nothing is coloured</text>"
        )
        out.extend(_swatches(pid, panel, extent, y, marks))
        return "".join(out)
    bar_w = min(grid_w, 240)
    step = bar_w / _RAMP_STEPS
    for k in range(_RAMP_STEPS):
        t = k / (_RAMP_STEPS - 1)
        value = (2 * t - 1) * extent if panel.scale == "diverging" else t * extent
        out.append(
            f'<rect x="{_LEFT + k * step:.2f}" y="{y}" width="{step + 0.4:.2f}" height="10" '
            f'fill="{_colour(panel, value, extent)}"/>'
        )
    unit = f" {panel.unit}" if panel.unit else ""
    lo = f"{-extent:+.3g}{unit}" if panel.scale == "diverging" and extent else f"0{unit}"
    hi = f"{extent:+.3g}{unit}" if panel.scale == "diverging" and extent else f"{extent:.3g}{unit}"
    out.append(f'<text class="tick" x="{_LEFT}" y="{y + 22}">{_e(lo)}</text>')
    if panel.scale == "diverging":
        out.append(
            f'<text class="tick" x="{_LEFT + bar_w / 2:g}" y="{y + 22}" '
            f'text-anchor="middle">0</text>'
        )
    out.append(
        f'<text class="tick" x="{_LEFT + bar_w:g}" y="{y + 22}" text-anchor="end">{_e(hi)}</text>'
    )
    out.extend(_swatches(pid, panel, extent, y, marks))
    return "".join(out)


def _swatches(pid: str, panel: _Panel, extent: float, y: int, marks: Sequence[str]) -> list[str]:
    out: list[str] = []
    for k, mark in enumerate(marks):
        sx = _LEFT
        sy = y + 32 + k * 18
        meaning = _e(MARK_MEANING[mark])
        if mark == "edited":
            out.append(
                f'<rect x="{sx:g}" y="{sy}" width="22" height="13" fill="url(#{pid}-no_points)"/>'
                f'<rect class="edited swatch" x="{sx + 1:g}" y="{sy + 1}" width="20" height="11"/>'
                f'<text class="legend" x="{sx + 28:g}" y="{sy + 10}">{meaning}</text>'
            )
            continue
        if mark == "axis_moved":
            out.append(
                f'<path class="axis-moved" d="M{sx + 11:g} {sy + 2}l-5 8h10z"/>'
                f'<text class="legend" x="{sx + 28:g}" y="{sy + 10}">{meaning}</text>'
            )
            continue
        if mark in COLOURED_MARKS:
            fill = _colour(panel, extent if mark == "changed" else 0.0, extent)
            sample = "+" if mark == "changed" else "0"
        else:
            fill = f"url(#{pid}-{mark})"
            sample = {"other_loop": "OL", "no_points": ""}.get(mark, MARK_LABELS.get(mark, ""))
        out.append(f'<rect x="{sx:g}" y="{sy}" width="22" height="13" fill="{fill}"/>')
        out.append(f'<text class="v" x="{sx + 11:g}" y="{sy + 10}">{_e(sample)}</text>')
        out.append(f'<text class="legend" x="{sx + 28:g}" y="{sy + 10}">{meaning}</text>')
    return out


# -- page sections ------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark;
  --bg: #f6f8fa; --elev: #ffffff; --fg: #141a21; --dim: #4f5b66; --faint: #8a96a3;
  --border: #d5dbe1; --warn-bg: #fff4dc; --warn-border: #d99a3c; --mono-bg: #eef1f4;
  --edit: #111111; --edit-inner: #ffffff; --moved: #b0620f; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0b0f14; --elev: #141a21; --fg: #eef3f8; --dim: #8a98a5; --faint: #5b6772;
    --border: #29333d; --warn-bg: #2a2214; --warn-border: #ffb547; --mono-bg: #1b232c;
    --edit: #111111; --edit-inner: #ffffff; --moved: #ffb547; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 16px 14px 48px; }
h1 { font-size: 1.35rem; margin: 0.2rem 0 0.8rem; }
h2 { font-size: 1.1rem; margin: 1.6rem 0 0.5rem; border-bottom: 1px solid var(--border);
  padding-bottom: 0.2rem; }
h3 { font-size: 0.98rem; margin: 1rem 0 0.3rem; }
p, li { margin: 0.25rem 0; }
code, .mono { font-family: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace;
  font-size: 0.86em; background: var(--mono-bg); padding: 0 0.25em; border-radius: 4px;
  overflow-wrap: anywhere; }
.caveats { background: var(--warn-bg); border: 1px solid var(--warn-border);
  border-radius: 12px; padding: 10px 16px 12px; }
.caveats h2 { border: 0; margin-top: 0.3rem; }
.caveats ul { padding-left: 1.2rem; }
dl.facts { display: grid; grid-template-columns: minmax(9rem, max-content) 1fr;
  gap: 0.2rem 0.9rem; margin: 0.4rem 0; }
dl.facts dt { color: var(--dim); }
dl.facts dd { margin: 0; overflow-wrap: anywhere; }
.scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; font-size: 0.86rem; margin: 0.4rem 0; }
th, td { border: 1px solid var(--border); padding: 3px 6px; text-align: left;
  vertical-align: top; }
th { background: var(--elev); color: var(--dim); font-weight: 600; }
table.detail td { min-width: 6rem; max-width: 19rem; }
.status { font-weight: 600; }
figure { margin: 1rem 0 1.4rem; background: var(--elev); border: 1px solid var(--border);
  border-radius: 12px; padding: 10px; }
figcaption { font-weight: 600; margin-bottom: 0.2rem; }
figcaption .note { display: block; font-weight: 400; color: var(--dim); font-size: 0.88rem; }
svg.heatmap { display: block; width: 100%; min-width: 560px; height: auto; }
svg .tick { fill: var(--dim); font-size: 10px; }
svg .tick.moved { fill: var(--moved); font-weight: 700; }
svg .axis-moved { fill: var(--moved); }
svg .axis-title { fill: var(--fg); font-size: 11px; }
svg .legend { fill: var(--fg); font-size: 10px; }
svg .v.small { font-size: 7.5px; }
svg .v { fill: #111111; font-size: 9px; text-anchor: middle; pointer-events: none;
  paint-order: stroke; stroke: #ffffff; stroke-opacity: 0.8; stroke-width: 2.4px;
  stroke-linejoin: round;
  font-family: ui-monospace, Menlo, monospace; }
svg .frame { fill: none; stroke: var(--border); }
svg .edited { fill: none; stroke: var(--edit); stroke-width: 1.8; pointer-events: none; }
svg .edited-inner { fill: none; stroke: var(--edit-inner); stroke-width: 0.6;
  pointer-events: none; }
footer { margin-top: 2rem; color: var(--faint); font-size: 0.82rem; }
"""


def _facts(items: Sequence[tuple[str, str]]) -> str:
    """``items`` are (label, already-escaped HTML)."""
    return (
        '<dl class="facts">' + "".join(f"<dt>{_e(k)}</dt><dd>{v}</dd>" for k, v in items) + "</dl>"
    )


def _code(value: object) -> str:
    return f"<code>{_e(value)}</code>"


def _not_stated(what: str) -> str:
    return f"<em>not stated by the caller ({_e(what)})</em>"


def _caveat_block(table: CellTable) -> str:
    qualification = next((c for c in table.caveats if c.startswith(PLANT_NOT_QUALIFIED)), None)
    if qualification is not None:
        why = qualification.split(":", 1)[1].strip() if ":" in qualification else qualification
        plant = (
            f"<li><strong>The plant is not qualified.</strong> {_e(why)} "
            f"({_code(qualification)}). Nothing below is a qualified prediction.</li>"
        )
    else:
        plant = (
            "<li><strong>Plant qualification:</strong> the run states no "
            f"{_code(PLANT_NOT_QUALIFIED)} caveat.</li>"
        )
    standing = (
        f"<li><strong>Structural error is not sampled</strong> "
        f"({_code(STRUCTURAL_ERROR_NOT_SAMPLED)}). The plant's own model error is real and "
        "large; it is not a parameter, so no band below contains it.</li>"
        f"<li><strong>Parameter uncertainty only</strong> ({_code(PARAMETER_UNCERTAINTY_ONLY)}). "
        "The spread is over the declared parameter brackets and the drawn fill bytes; base and "
        "candidate share every draw, so the deltas are the product, not absolute values.</li>"
    )
    shown = {*STANDING_CAVEATS, qualification}
    others = [c for c in table.caveats if c not in shown]
    other_html = "".join(f"<li>{_code(c)}</li>" for c in others) or "<li>none</li>"
    unresolved = "".join(f"<li>{_code(u)}</li>" for u in table.unresolved) or "<li>none</li>"
    return (
        '<section class="caveats" id="caveats"><h2>Read this first</h2>'
        f"<ul>{plant}{standing}</ul>"
        f"<h3>Every other caveat the run states ({len(others)})</h3><ul>{other_html}</ul>"
        f"<h3>Unresolved ({len(table.unresolved)}): what the result rests on but does not "
        f"know</h3><ul>{unresolved}</ul></section>"
    )


def _image(ref: ImageRef | None, sha: str) -> str:
    label = _e(ref.label) if ref is not None else _not_stated("run.base / run.candidate label")
    return f"{label}<br>{_code(sha)}"


def _identity(table: CellTable, run: ReportRun) -> str:
    return (
        '<section id="identity"><h2>Images</h2>'
        + _facts(
            [
                ("base", _image(run.base, table.base_image_sha256)),
                ("candidate", _image(run.candidate, table.candidate_image_sha256)),
                ("compare_roms outcome", _code(table.rom_outcome)),
            ]
        )
        + "</section>"
    )


def _edits(table: CellTable, run: ReportRun) -> str:
    out = ['<section id="edits"><h2>Edits</h2>']
    facts = [
        ("route", _code(run.edit_route) if run.edit_route else _not_stated("run.edit_route")),
    ]
    if run.edit_route == "edit_spec" or run.spec_sha256 is not None:
        facts.append(
            ("spec sha256", _code(run.spec_sha256) if run.spec_sha256 else _not_stated("spec"))
        )
    out.append(_facts(facts))
    if run.edits is None:
        out.append(f"<p>Edit list: {_not_stated('run.edits')}.</p>")
    elif not run.edits:
        out.append("<p>No edits: the candidate is identical (the negative control).</p>")
    else:
        rows = "".join(
            f"<tr><td>{_code(e.table)}</td><td>{_e('' if e.slot is None else e.slot)}</td>"
            f"<td>{_e(e.selector or 'file')}</td><td>{_e(e.operation or 'as saved')}</td>"
            f"<td>{e.changed}</td><td>{e.unchanged}</td></tr>"
            for e in run.edits
        )
        out.append(
            '<div class="scroll"><table><thead><tr><th>table</th><th>slot</th>'
            "<th>selector</th><th>operation</th><th>cells changed</th>"
            f"<th>cells left</th></tr></thead><tbody>{rows}</tbody></table></div>"
        )
    ov = table.overlay
    slot = "" if table.slot is None else f" slot {table.slot}"
    lines = [
        f"<li>{len(ov.edited_cells)} edited cell(s) of {_code(table.table)}{_e(slot)}, outlined "
        "on every map</li>"
    ]
    for axis in ("rows", "cols"):
        moves = [e for e in ov.axis_edits if e.axis == axis]
        if moves:
            what = ", ".join(f"{e.index}: {e.old}→{e.new}" for e in moves)
            name = table.rows.table if axis == "rows" else table.cols.table
            lines.append(
                f"<li>{len(moves)} {_e(axis)} breakpoint(s) of {_code(name)} move (raw "
                f"{_e(what)}), marked ▲ on the axis</li>"
            )
    for o in ov.other_edits:
        oslot = "" if o.slot is None else f" slot {o.slot}"
        lines.append(f"<li>{o.cells} cell(s) of {_code(o.table)}{_e(oslot)}, off this grid</li>")
    for m in ov.moved_lookups:
        where = "on this grid" if m.on_this_grid else "off this grid"
        tables = ", ".join(m.tables)
        lines.append(f"<li>moved lookup {_code(m.node)} ({_e(tables)}), {_e(where)}</li>")
    out.append(f"<h3>On the grid (the table's overlay)</h3><ul>{''.join(lines)}</ul>")
    reach = "".join(
        f"<tr><td>{_code(name)}</td><td class='status'>{_e(r.status)}</td>"
        f"<td>{_e(', '.join(r.basis))}</td></tr>"
        for name, r in sorted(table.reach.items())
    )
    out.append(
        "<h3>Reach per output</h3><div class='scroll'><table><thead><tr><th>output</th>"
        f"<th>reach</th><th>basis</th></tr></thead><tbody>{reach}</tbody></table></div>"
    )
    out.append("</section>")
    return "".join(out)


def _weight_unit(table: CellTable) -> str:
    return "s" if table.design_kind == "drive" else "grid weight"


def _counts(counts: Mapping[str, int | float]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{k} {_g(v)}" for k, v in sorted(counts.items()))


def _design(table: CellTable, run: ReportRun) -> str:
    unit = _weight_unit(table)
    visited = sum(1 for c in table.cells if c.pairs)
    placed = math.fsum(c.weight for c in table.cells)
    unplaced = (
        ", ".join(
            f"{_code(why)} {u.pairs} pairs, {_e(_g(u.weight))} {_e(unit)}"
            for why, u in sorted(table.unplaced.items())
        )
        or "none"
    )
    share = _pct(placed / table.weight_total)
    facts = [
        ("kind", _code(table.design_kind)),
        ("design sha256", _code(table.design_sha256)),
        ("points", _e(table.n_points)),
        ("cells visited", _e(f"{visited} of {len(table.cells)}")),
        ("weight placed", _e(f"{_g(placed)} of {_g(table.weight_total)} {unit} ({share})")),
        ("unplaced", unplaced),
    ]
    d = run.design
    if d is None:
        facts.append(("design summary", _not_stated("run.design")))
    else:
        facts.extend(
            [
                ("base revision", _code(d.base_revision)),
                ("weight basis", _code(d.weight_basis)),
                ("reads", _e(d.reads)),
                ("plant states", _e(_counts(d.plant_states))),
                ("placement", _e(_counts(d.placement))),
                ("eligible dwell", _e(f"{_g(d.eligible_dwell_s)} s")),
                ("unplaced dwell", _e(_counts(d.unplaced_dwell_s))),
                ("weight by regime", _e(_counts(d.weight_by_regime))),
                ("throttle sources", _e(_counts(d.points_by_throttle_source))),
                ("battery sources", _e(_counts(d.points_by_battery_source))),
                (
                    "battery unmeasured",
                    _e(
                        f"{d.battery_unmeasured_points} points"
                        + (
                            ""
                            if d.battery_v_when_unmeasured is None
                            else f" at a stated {_g(d.battery_v_when_unmeasured)} V"
                        )
                    ),
                ),
                (
                    "fill-byte interval width",
                    _e(
                        "—"
                        if d.fill_byte_width is None
                        else f"median {_g(d.fill_byte_width.median)}, max "
                        f"{d.fill_byte_width.max} counts"
                    ),
                ),
            ]
        )
    return '<section id="design"><h2>Design and coverage</h2>' + _facts(facts) + "</section>"


def _sampling(table: CellTable, run: ReportRun) -> str:
    facts = [
        ("draws", _e(table.draws)),
        ("seed", _e(run.seed) if run.seed is not None else _not_stated("run.seed")),
        (
            "plant version",
            _code(run.plant_version) if run.plant_version else _not_stated("run.plant_version"),
        ),
        (
            "uncertainty sha256",
            _code(run.uncertainty_sha256)
            if run.uncertainty_sha256
            else _not_stated("run.uncertainty_sha256"),
        ),
        ("result digest", _code(table.result_digest)),
        (
            "quantiles",
            _e(
                ", ".join(f"{q:g}" for q in table.quantiles)
                + f" ({table.quantile_method}, weighted)"
            ),
        ),
        ("pair weight", _e(table.weight_basis)),
        (
            "grid",
            _e(f"{table.grid}: {table.table}")
            + _e("" if table.slot is None else f" slot {table.slot}"),
        ),
        ("cell membership", _e(f"{table.membership}: {table.membership_rule}")),
        ("rows", _e(f"{table.rows.table}, {table.rows.domain}")),
        ("columns", _e(f"{table.cols.table}, {table.cols.domain}")),
    ]
    return '<section id="sampling"><h2>Draws and seed</h2>' + _facts(facts) + "</section>"


def _maps(table: CellTable) -> str:
    panels, absent = _panels(table)
    unit = _weight_unit(table)
    out = [
        '<section id="maps"><h2>Maps</h2>',
        "<p>Each map is the grid of the table being edited, rows and columns at the decoded "
        "breakpoints. Hover a cell for n, weight, quantiles, regime mix and reasons; the table "
        "below has the same detail. Edited cells are outlined; a breakpoint the candidate moves "
        "is marked ▲. Colour encodes only a computed value; every other state has its own "
        "hatch and label.</p>",
    ]
    if absent:
        out.append("<ul>" + "".join(f"<li>{_e(a)}</li>" for a in absent) + "</ul>")
    for panel in panels:
        out.append(
            f'<figure id="{_ident(panel.key)}"><figcaption>{_e(panel.title)}'
            f'<span class="note">{_e(panel.note)}'
            f"{_e(f' ({panel.unit})') if panel.unit else ''}</span></figcaption>"
            f'<div class="scroll">{_chart(table, panel, unit)}</div></figure>'
        )
    out.append("</section>")
    return "".join(out)


def _output_td(oc: OutputCell, output: str) -> str:
    lines = [f'<div class="status">{_e(oc.status.replace("_", " "))}</div>']
    lines.extend(f"<div>{_e(x)}</div>" for x in _quantile_lines(oc, output))
    lines.extend(f"<div>{_e(x)}</div>" for x in _support_lines(oc))
    return f"<td>{''.join(lines)}</td>"


def _detail(table: CellTable) -> str:
    unit = _weight_unit(table)
    status_rows = "".join(
        f"<tr><td>{_code(name)}</td>"
        + "".join(f"<td>{table.status_counts.get(name, {}).get(s, 0)}</td>" for s in STATUSES)
        + "</tr>"
        for name in table.outputs
    )
    out = [
        '<section id="cells"><h2>Cells</h2><h3>Status counts</h3>',
        '<div class="scroll"><table><thead><tr><th>output</th>'
        + "".join(f"<th>{_e(s)}</th>" for s in STATUSES)
        + f"</tr></thead><tbody>{status_rows}</tbody></table></div>",
    ]
    shown = [c for c in table.cells if c.pairs or c.edited is not None]
    head = (
        "<th>cell</th>"
        f"<th>{_e(table.rows.unit or 'raw')}</th><th>{_e(table.cols.unit or 'raw')}</th>"
        f"<th>n</th><th>pairs</th><th>weight ({_e(unit)})</th><th>regime mix (design label)</th>"
        "<th>loop headline (simulated loop)</th><th>edit</th>"
        + "".join(f"<th>{_e(name)}</th>" for name in table.outputs)
    )
    body: list[str] = []
    for c in shown:
        loops = "".join(
            f"<div>{_e(b.loop)} {_e(_pct(b.weight_share))} → {_e(b.output)}: "
            f"{_e(b.cell.status.replace('_', ' '))}</div>"
            + "".join(f"<div>{_e(x)}</div>" for x in _quantile_lines(b.cell, b.output))
            + "".join(f"<div>{_e(x)}</div>" for x in _support_lines(b.cell))
            for b in c.by_loop
        )
        edit = []
        if c.edited is not None:
            edit.append(f"raw {c.edited.old} → {c.edited.new}")
        if c.row_axis_moved:
            edit.append("row breakpoint moves")
        if c.col_axis_moved:
            edit.append("column breakpoint moves")
        outputs = "".join(
            _output_td(c.outputs[name], name) if name in c.outputs else "<td>—</td>"
            for name in table.outputs
        )
        body.append(
            f'<tr id="cell-{c.row}-{c.col}"><td>({c.row}, {c.col})</td>'
            f"<td>{_e(_tick(c.row_value, c.row_breakpoint))}</td>"
            f"<td>{_e(_tick(c.col_value, c.col_breakpoint))}</td>"
            f"<td>{c.n_points}</td><td>{c.pairs}</td><td>{_e(_g(c.weight))}</td>"
            f"<td>{_e(_shares(c.regime_shares))}</td><td>{loops or '—'}</td>"
            f"<td>{''.join(f'<div>{_e(x)}</div>' for x in edit) or '—'}</td>{outputs}</tr>"
        )
    not_shown = len(table.cells) - len(shown)
    out.append(
        f"<h3>Visited or edited cells ({len(shown)}; {not_shown} unvisited, unedited cells "
        "are no_points for every output)</h3>"
    )
    out.append(
        f'<div class="scroll"><table class="detail"><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div></section>"
    )
    return "".join(out)


def render_report(
    cell_table_json: str | bytes | Mapping[str, Any], *, run: Mapping[str, Any] | None = None
) -> str:
    """The report page for one cell table (module docstring).

    ``cell_table_json`` is :meth:`CellTable.to_json` (or its parsed mapping);
    ``run`` carries only :data:`RUN_KEYS`. Refusals: ``cell_table_invalid``,
    ``standing_caveat_missing``, ``loop_unknown``, ``headline_unknown``,
    ``quantile_basis_mismatch``, ``run_key_unknown``, ``run_invalid``,
    ``run_identity_mismatch``."""
    table = _table(cell_table_json)
    facts = _run(run, table)
    title = facts.label or f"ROM edit simulation: {table.table}"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{_e(title)}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>{_e(title)}</h1>"
        + _caveat_block(table)
        + _identity(table, facts)
        + _edits(table, facts)
        + _design(table, facts)
        + _sampling(table, facts)
        + _maps(table)
        + _detail(table)
        + f"<footer>{_e(TOOL)} {_e(TOOL_VERSION)} · cell table {_e(table.tool)} "
        f"{_e(table.tool_version)}, schema {table.schema_version}</footer>"
        "</main></body></html>\n"
    )


__all__ = [
    "MARK_LABELS",
    "MARK_MEANING",
    "OUTPUT_LABELS",
    "PARAMETER_UNCERTAINTY_ONLY",
    "PLANT_NOT_QUALIFIED",
    "RUN_KEYS",
    "STANDING_CAVEATS",
    "STRUCTURAL_ERROR_NOT_SAMPLED",
    "TOOL_VERSION",
    "DesignSummary",
    "EditRow",
    "ImageRef",
    "ReportRun",
    "render_report",
]
