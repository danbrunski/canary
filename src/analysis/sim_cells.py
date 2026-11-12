"""Per-cell aggregation of a paired Monte Carlo, keeping each kind of empty distinct (E08.4).

:func:`aggregate_cells` takes an E08.3 :class:`~src.analysis.sim_montecarlo.
MonteCarloResult`, the E08.2 :class:`~src.analysis.sim_design.SimDesign` it
ran, the grid (:class:`CellAxes`) and the ROM-effect trace the run used, and
gives one row per cell of the grid (:class:`CellTable`), every cell present
whether the design visited it or not.

**Cell membership: the nearest breakpoint, in the lookup's own comparison
domain.** The ECM reads a 2-D table bilinearly
(:mod:`src.decode.ecm_lookup`): a coordinate between breakpoints ``i`` and
``i + 1`` reads cells ``i`` and ``i + 1``, weighted ``1 - f`` and ``f``. A
table cell sits *at* a breakpoint, so the grid cell of table cell ``(r, c)``
is every coordinate whose larger interpolation weight is on breakpoint ``r``
(rows) and ``c`` (columns): ``f < 1/2`` is the lower breakpoint, ``f > 1/2``
the upper, and an exact half (equal weights) the lower, the side the
firmware's search names. Past either end the lookup clamps to the end
breakpoint, and so does the cell. So grid cell ``(r, c)`` *is* table cell
``(r, c)``: every pair in it reads that cell, with at least half the weight
on each axis (so at least a quarter of the lookup, the largest share of the
four cells read), and an edit there shows there first. (Membership by the interval between
breakpoints would make a grid cell straddle two table cells and leave the
edited cell's own points split across four.) The axis hit is the firmware's
one search, :func:`~src.decode.ecm_lookup.search_axis`, never a second copy.

**A pair is placed by what that pair ran.** A design point's fill byte is an
interval and each draw samples it, so each (point, draw) pair goes in the cell
of the byte that draw used (``result.fill_bytes``), never the point's
midpoint; one point can sit in several cells. The rpm coordinate is the ECM's
engine-speed word from :func:`~src.analysis.paired_scenarios.rpm_word`, the
paired run's own conversion, never a copy of it. On the Target AFR grid the
load coordinate is the base ROM's X (from that pair's base BFS, through the
forced-column flag as the twin reads it); a pair with no base BFS is unplaced
under its reason, counted, never dropped.

What it makes impossible rather than documented:

* **No aggregate-first path.** The function takes the ``MonteCarloResult``
  itself (type-checked, and refused at run time as
  ``not_a_monte_carlo_result``), never a pre-reduced frame. Quantiles are over
  (point, draw) pairs, each weighted by its point's dwell divided by the draw
  count: a distribution of per-sample changes, not two aggregates compared.
* **Six statuses, never collapsed** (:data:`CellStatus`): ``changed``,
  ``unchanged`` (supported, every pair's delta exactly 0), ``unsupported``,
  ``partly_supported`` (both shares, quantiles over supported pairs only,
  labelled), ``no_points`` (the design never visited the cell) and
  ``not_reached`` (``compare_roms`` says the change cannot reach the output:
  a cell with points but no reachable change is never ``unchanged``).
* **Reach is checked, not trusted.** An output whose ROM dependencies are all
  ``unchanged`` and whose learned state the change leaves alone is
  ``not_reached``; a finite nonzero delta on such an output anywhere in the
  result refuses the table (``reach_contradicted``).
* **The inputs belong together.** A design, grid or effect trace that is not
  the run's is refused (``design_mismatch``, ``axes_image_mismatch``,
  ``effects_mismatch``).
* **Closed loop reports the trim.** A closed-loop pair holds the base's metered
  open time, so its lambda is the base's by construction; its headline is
  ``fuel_trim_total_pct``, reported over the computed pairs with the run's
  ``closed_loop_trim_authority_undecoded`` reason carried as given
  (:data:`HEADLINE_BY_LOOP`).

The table's JSON form is :meth:`CellTable.to_dict` (``schema_version`` 1,
pydantic ``extra="forbid"``); :func:`cell_table_schema` is its JSON schema.
Nothing here, or anything it imports, can reach the ECU: see
``tests/test_sim_cells.py``'s clean-process import test.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.air_charge_twin import AirChargeCalibration, AirChargeRefusalError
from src.analysis.ecu_application import ApplicationModelRefusalError, check_image
from src.analysis.ecu_state_models import TIMING_DELTA_CONDITIONS
from src.analysis.engine_dynamics import claim_refusals
from src.analysis.paired_scenarios import (
    _BFS_OUTPUT,
    _ECU_OUTPUTS,
    _TWIN_OUTPUT,
    TIMING_MODEL_OUTPUT,
    TIMING_OUTPUT,
    TIMING_STATE_UNSTATED,
    SimKit,
    TimingState,
    _rpm_quantum,
    base_timing_rom_ids,
    ignition_timing_at,
    ignition_timing_cells,
    rpm_word,
    timing_cell_summary,
    timing_tables,
)
from src.analysis.paired_steady import COMPUTED_HERE, SETPOINT_MOVED, TRIM_AUTHORITY_UNDECODED
from src.analysis.rom_effects import EffectReport, compare_roms
from src.analysis.rom_image import sha256_hex
from src.analysis.sim_contract import SimContract, SimulationRefusalError
from src.analysis.sim_design import SimDesign
from src.analysis.sim_montecarlo import MonteCarloResult
from src.analysis.target_phi import (
    ResolvedTable,
    TwinDefinition,
    TwinRefusalError,
    resolve_table,
    x_from_bfs,
)
from src.analysis.target_phi import predict as predict_phi
from src.decode.ecm_lookup import AxisHit, search_axis

#: Bump when the table's shape or meaning changes.
SCHEMA_VERSION: Final = 1
TOOL_VERSION = "1"
TOOL: Final = "sim_cells"

CellStatus = Literal[
    "changed", "unchanged", "unsupported", "partly_supported", "no_points", "not_reached"
]
STATUSES: tuple[CellStatus, ...] = (
    "changed",
    "unchanged",
    "unsupported",
    "partly_supported",
    "no_points",
    "not_reached",
)
Grid = Literal["fuel_comp", "target_afr"]
Coordinate = Literal["engine_speed_word", "fuel_comp_load_byte", "load_input_x"]

#: How a coordinate is placed in a cell (module docstring).
MEMBERSHIP: Final = "nearest_breakpoint"
MEMBERSHIP_RULE = (
    "nearest breakpoint in the lookup's comparison domain: the ECM interpolates "
    "bilinearly between breakpoints i and i+1 with fraction f; f < 1/2 belongs to "
    "breakpoint i, f > 1/2 to i+1, f = 1/2 (equal weights) to i, and a coordinate "
    "past either end clamps to the end breakpoint, so grid cell (r, c) is table cell "
    "(r, c): every pair in it reads that cell with weight >= 1/2 on each axis"
)
#: The quantiles reported, over (point, draw) pairs weighted by dwell.
QUANTILES: tuple[float, float, float] = (0.05, 0.50, 0.95)
#: Weighted quantile definition (numpy's, with weights).
QUANTILE_METHOD: Final = "inverted_cdf"

#: What each loop kind's headline output is. A closed-loop pair holds the
#: base's metered open time, so its lambda is the base's by construction: the
#: change shows in the trim the candidate needs. A moved setpoint leaves the
#: candidate's fuel side uncomputed; what moved is the commanded phi.
HEADLINE_BY_LOOP: dict[str, str] = {
    "open_loop": "delivered_lambda",
    "closed_loop": "fuel_trim_total_pct",
    SETPOINT_MOVED: "target_phi",
}
#: The trim is already a percentage of fuel; its ratio to the base trim is
#: not a quantity, so it has no relative delta.
NO_RELATIVE_DELTA: frozenset[str] = frozenset({"fuel_trim_total_pct"})
#: compare_roms outputs each simulated output is computed from. Target phi
#: reads X, which BFS forms; the fuel side reads both words.
ROM_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "base_fuel_schedule": (_BFS_OUTPUT,),
    "target_phi": (_BFS_OUTPUT, _TWIN_OUTPUT),
    "injector_pulse_ms": (_BFS_OUTPUT, _TWIN_OUTPUT),
    "fuel_trim_total_pct": (_BFS_OUTPUT, _TWIN_OUTPUT),
    "delivered_lambda": (_BFS_OUTPUT, _TWIN_OUTPUT),
    "fuel_per_event_g": (_BFS_OUTPUT, _TWIN_OUTPUT),
}
#: The learned trims a candidate event can reset (the fuel side reads them).
_LEARNED_KEYS: tuple[str, ...] = ("stft_pct", "ltft_pct")
#: The reason a Target AFR pair with no base BFS has no load coordinate.
X_NOT_COMPUTED = "load_input_x_not_computed"
#: Relative tolerance on the table's weight bookkeeping (float summation only).
_WEIGHT_RTOL = 1e-9

FloatArray = NDArray[np.float64]
IntArray = NDArray[np.int64]


# -- the grid -----------------------------------------------------------------------------


@dataclass(frozen=True)
class GridAxis:
    """One axis of a grid, on the base image, in its lookup's comparison domain."""

    #: The rom_map axis table.
    table: str
    coordinate: Coordinate
    #: The axis bytes as stored.
    raw: tuple[int, ...]
    #: What the lookup compares the coordinate with (``raw`` x its multiplier).
    breakpoints: tuple[int, ...]
    #: The rom_map decode of each breakpoint, for labels (``None``: no decode).
    values: tuple[float | None, ...]
    unit: str | None
    #: How the comparison domain is formed, one line.
    domain: str


@dataclass(frozen=True)
class CellAxes:
    """A grid: one table's rows and columns on the base image (:func:`fuel_comp_axes`,
    :func:`target_afr_axes`)."""

    grid: Grid
    #: The rom_map table whose cells the grid's cells are, and its slot.
    table: str
    slot: int | None
    base_image_sha256: str
    rows: GridAxis
    cols: GridAxis
    #: rpm per engine-speed word count (the contract's ``rpm_word`` quantum).
    rpm_quantum: float
    #: The contract whose ``rpm_word`` quantization places a point's rpm
    #: (:func:`~src.analysis.paired_scenarios.rpm_word`, as the paired run does).
    contract: SimContract
    #: Target AFR only: the base's resolved table and the twin, for X.
    target: ResolvedTable | None = None
    twin: TwinDefinition | None = None


def _increasing(axis: str, breakpoints: Sequence[int]) -> None:
    if not breakpoints or any(b >= a for a, b in zip(breakpoints[1:], breakpoints, strict=False)):
        raise SimulationRefusalError(
            "axis_not_increasing",
            f"{axis} breakpoints {list(breakpoints)} do not strictly rise: nearest-breakpoint "
            "membership needs a rising axis",
        )


def fuel_comp_axes(kit: SimKit, base_image: bytes, *, slot: int) -> CellAxes:
    """The default grid: Fuel Comp's own rpm x load breakpoints on the base image.

    The axes are the ones the paired run's BFS reads
    (:class:`~src.analysis.air_charge_twin.AirChargeCalibration`). Rows: the
    rpm axis, compared by the firmware with the engine-speed word, so its
    breakpoint is the decoded rpm / quantum, refused unless whole
    (``axis_not_on_the_rpm_word``). Columns: the fill-byte axis, compared byte
    for byte."""
    image_sha256 = sha256_hex(base_image)
    try:
        cal = AirChargeCalibration.from_image(
            base_image, model=kit.model, rom_map=kit.rom_map, slot=slot, image_sha256=image_sha256
        )
    except (AirChargeRefusalError, ApplicationModelRefusalError) as exc:
        raise SimulationRefusalError(f"fuel_comp_unreadable:{exc.reason}", str(exc)) from exc
    table = kit.rom_map.table("fuel_compensation")
    if table.axes.rows is None or table.axes.cols is None:
        raise SimulationRefusalError("axis_not_found", "fuel_compensation names no axes")
    quantum = _rpm_quantum(kit.contract)
    row_table = kit.rom_map.table(table.axes.rows)
    col_table = kit.rom_map.table(table.axes.cols)
    decode = row_table.decode
    if decode is None or decode.quantity != "rpm":
        raise SimulationRefusalError(
            "axis_not_on_the_rpm_word",
            f"{row_table.id} has no rpm decode, so its breakpoints have no engine-speed word",
        )
    rpm_values = [decode.apply(v) for v in cal.fuel_comp_rpm_axis]
    words = []
    for raw, rpm in zip(cal.fuel_comp_rpm_axis, rpm_values, strict=True):
        word = None if rpm is None else rpm / quantum
        if word is None or word != int(word):
            raise SimulationRefusalError(
                "axis_not_on_the_rpm_word",
                f"{row_table.id} raw {raw} decodes to {rpm} rpm, not a whole {quantum}-rpm word",
            )
        words.append(int(word))
    col_decode = col_table.decode
    rows = GridAxis(
        table=row_table.id,
        coordinate="engine_speed_word",
        raw=cal.fuel_comp_rpm_axis,
        breakpoints=tuple(words),
        values=tuple(rpm_values),
        unit=decode.unit,
        domain=f"engine-speed word ({quantum:g} rpm/count) vs decoded rpm / {quantum:g}",
    )
    cols = GridAxis(
        table=col_table.id,
        coordinate="fuel_comp_load_byte",
        raw=cal.fuel_comp_load_axis,
        breakpoints=cal.fuel_comp_load_axis,
        values=tuple(
            None if col_decode is None else col_decode.apply(v) for v in cal.fuel_comp_load_axis
        ),
        unit=None if col_decode is None else col_decode.unit,
        domain="the fill byte 0xFFFF118A vs the axis byte, byte for byte",
    )
    _increasing(rows.table, rows.breakpoints)
    _increasing(cols.table, cols.breakpoints)
    return CellAxes(
        grid="fuel_comp",
        table=table.id,
        slot=slot,
        base_image_sha256=image_sha256,
        rows=rows,
        cols=cols,
        rpm_quantum=quantum,
        contract=kit.contract,
    )


def target_afr_axes(kit: SimKit, base_image: bytes, *, slot: int) -> CellAxes:
    """The other built-in grid: Target AFR's rpm x X breakpoints on the base
    image, as the twin resolves the table (``resolve_table``'s refusals keep
    their reasons, prefixed ``target_table:``)."""
    image_sha256 = sha256_hex(base_image)
    try:
        table = resolve_table(
            base_image,
            kit.rom_map,
            kit.twin,
            viewer=kit.viewer,
            application_model=(kit.model, kit.model_sha256),
            expected_sha256=image_sha256,
            slot=slot,
        )
    except TwinRefusalError as exc:
        raise SimulationRefusalError(f"target_table:{exc.reason}", str(exc)) from exc
    if table.load_point_scaler is None:
        raise SimulationRefusalError(
            "load_point_scaler_unresolved", "the base image's Load Point Scaler was not read"
        )

    def decoded(axis_id: str, raw: Sequence[int]) -> tuple[tuple[float | None, ...], str | None]:
        decode = kit.rom_map.table(axis_id).decode
        if decode is None:
            return tuple(None for _ in raw), None
        return tuple(decode.apply(v) for v in raw), decode.unit

    row_values, row_unit = decoded(table.row_axis_id, table.row_axis_raw)
    col_values, col_unit = decoded(table.column_axis_id, table.column_axis_raw)
    ref = kit.twin.table
    rows = GridAxis(
        table=table.row_axis_id,
        coordinate="engine_speed_word",
        raw=table.row_axis_raw,
        breakpoints=table.row_breakpoints,
        values=row_values,
        unit=row_unit,
        domain=f"engine-speed word vs axis byte x {ref.rows.compare_multiplier}",
    )
    cols = GridAxis(
        table=table.column_axis_id,
        coordinate="load_input_x",
        raw=table.column_axis_raw,
        breakpoints=table.column_breakpoints,
        values=col_values,
        unit=col_unit,
        domain=(
            f"the base ROM's X (from that pair's base BFS) vs axis byte x "
            f"{ref.columns.compare_multiplier}; the forced-column flag reads the last column"
        ),
    )
    _increasing(rows.table, rows.breakpoints)
    _increasing(cols.table, cols.breakpoints)
    return CellAxes(
        grid="target_afr",
        table=table.table_id,
        slot=slot,
        base_image_sha256=image_sha256,
        rows=rows,
        cols=cols,
        rpm_quantum=_rpm_quantum(kit.contract),
        contract=kit.contract,
        target=table,
        twin=kit.twin,
    )


def nearest_index(hit: AxisHit) -> int:
    """The breakpoint carrying the larger interpolation weight of one axis hit;
    an exact half goes to the lower one (the hit's own index)."""
    if not hit.numerator:
        return hit.index
    return hit.index + 1 if 2 * hit.numerator > hit.span else hit.index


def nearest_breakpoint(breakpoints: Sequence[int], value: int) -> int:
    """:func:`nearest_index` of the firmware's search for ``value``."""
    hit = search_axis(breakpoints, value)
    if hit is None:
        raise SimulationRefusalError("coordinate_invalid", f"{value!r} on {list(breakpoints)}")
    return nearest_index(hit)


def cell_ranges(breakpoints: Sequence[int]) -> list[tuple[int | None, int | None]]:
    """The whole coordinate values each index holds under the nearest rule,
    inclusive; ``None`` is unbounded (the clamp)."""
    out: list[tuple[int | None, int | None]] = []
    lo: int | None = None
    for i in range(len(breakpoints)):
        if i == len(breakpoints) - 1:
            out.append((lo, None))
            break
        # v goes to i + 1 iff 2 (v - bp_i) > bp_{i+1} - bp_i.
        hi = (breakpoints[i] + breakpoints[i + 1]) // 2
        out.append((lo, hi))
        lo = hi + 1
    return out


# -- the table's schema -------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Quantiles(_Strict):
    p05: float
    p50: float
    p95: float


class QuantileBlock(_Strict):
    """Weighted quantiles over the pairs ``over`` names."""

    over: Literal["all_pairs", "supported_pairs", "computed_unsupported_pairs"]
    pairs: int = Field(ge=1)
    weight: float = Field(gt=0)
    delta: Quantiles
    #: ``None`` for an output with no relative delta (the trim), or when no
    #: pair has a nonzero base.
    relative_delta: Quantiles | None
    #: Pairs left out of ``relative_delta`` because the base is 0.
    relative_undefined_pairs: int = Field(ge=0)


class SupportEntry(_Strict):
    status: Literal["supported", "unsupported"]
    reasons: list[str]
    pairs: int = Field(ge=1)
    weight_share: float = Field(ge=0, le=1)


class OutputCell(_Strict):
    """One output in one cell (or one loop kind of a cell)."""

    status: CellStatus
    pairs: int = Field(ge=0)
    #: Shares of the cell's weight with the output supported / not.
    supported_share: float | None
    unsupported_share: float | None
    support: list[SupportEntry]
    quantiles: QuantileBlock | None
    #: Share of the supported weight whose delta is not exactly 0.
    changed_share: float | None
    #: ``partly_supported``: what the supported pairs show.
    supported_effect: Literal["changed", "unchanged"] | None = None


class LoopBlock(_Strict):
    """One loop kind's pairs in a cell, and its headline output."""

    loop: str
    weight_share: float = Field(ge=0, le=1)
    pairs: int = Field(ge=1)
    output: str
    cell: OutputCell


class EditMark(_Strict):
    old: int
    new: int


class Cell(_Strict):
    row: int = Field(ge=0)
    col: int = Field(ge=0)
    row_breakpoint: int
    col_breakpoint: int
    row_value: float | None
    col_value: float | None
    n_points: int = Field(ge=0)
    points: list[int]
    pairs: int = Field(ge=0)
    weight: float = Field(ge=0)
    #: Shares of the cell's weight by the design's regime label.
    regime_shares: dict[str, float]
    #: Shares by the simulated loop (``open_loop``/``closed_loop``/...).
    loop_shares: dict[str, float]
    outputs: dict[str, OutputCell]
    by_loop: list[LoopBlock]
    #: The overlay: this table cell is edited (raw old -> new).
    edited: EditMark | None
    #: This cell's row or column breakpoint moves in the candidate.
    row_axis_moved: bool
    col_axis_moved: bool


class AxisInfo(_Strict):
    table: str
    coordinate: Coordinate
    raw: list[int]
    breakpoints: list[int]
    values: list[float | None]
    unit: str | None
    domain: str
    #: Whole coordinate values each index holds, inclusive (``None`` unbounded).
    cell_ranges: list[tuple[int | None, int | None]]


class Reach(_Strict):
    """Whether the change can reach an output: ``reached``; ``not_reached``
    (every ROM dependency ``unchanged``, no learned reset it reads);
    ``no_change`` (identical images and learned state)."""

    status: Literal["reached", "not_reached", "no_change"]
    basis: list[str]


class EditedCell(_Strict):
    row: int
    col: int
    old: int
    new: int


class AxisEdit(_Strict):
    axis: Literal["rows", "cols"]
    table: str
    index: int
    old: int
    new: int


class OtherEdit(_Strict):
    table: str
    slot: int | None
    cells: int = Field(ge=1)


class MovedLookup(_Strict):
    node: str
    tables: list[str]
    #: One of its tables is this grid's table or one of its axes.
    on_this_grid: bool


class Overlay(_Strict):
    """Where the edit is, on the same grid as where its effect shows."""

    edited_cells: list[EditedCell]
    axis_edits: list[AxisEdit]
    other_edits: list[OtherEdit]
    moved_lookups: list[MovedLookup]


class Unplaced(_Strict):
    pairs: int = Field(ge=1)
    weight: float = Field(gt=0)


class CellTable(_Strict):
    """The per-cell table: E08.5 writes it, E08.6 renders it."""

    schema_version: Literal[1]
    tool: Literal["sim_cells"]
    tool_version: str
    grid: Grid
    table: str
    slot: int | None
    membership: Literal["nearest_breakpoint"]
    membership_rule: str
    quantiles: list[float]
    quantile_method: str
    #: Pair weight is the point's weight / draws.
    weight_basis: str
    base_image_sha256: str
    candidate_image_sha256: str
    design_sha256: str
    design_kind: str
    result_digest: str
    draws: int = Field(ge=1)
    n_points: int = Field(ge=1)
    outputs: list[str]
    headline_by_loop: dict[str, str]
    rows: AxisInfo
    cols: AxisInfo
    reach: dict[str, Reach]
    #: The run's caveats and unresolved dependencies, as given.
    caveats: list[str]
    unresolved: list[str]
    rom_outcome: str
    weight_total: float = Field(gt=0)
    unplaced: dict[str, Unplaced]
    status_counts: dict[str, dict[str, int]]
    overlay: Overlay
    #: Every cell of the grid, row-major, visited or not.
    cells: list[Cell]

    @model_validator(mode="after")
    def _accounted(self) -> CellTable:
        n_rows, n_cols = len(self.rows.breakpoints), len(self.cols.breakpoints)
        expected = [(r, c) for r in range(n_rows) for c in range(n_cols)]
        found = [(cell.row, cell.col) for cell in self.cells]
        if found != expected:
            missing = sorted(set(expected) - set(found))[:3]
            repeated = sorted(k for k, n in Counter(found).items() if n > 1)[:3]
            raise ValueError(
                "a cell table holds every cell of its grid once, row-major: "
                f"{len(found)} cells for {n_rows} x {n_cols}, missing {missing}, "
                f"repeated {repeated}, out of grid "
                f"{sorted(set(found) - set(expected))[:3]}"
            )
        for cell in self.cells:
            here = (
                self.rows.breakpoints[cell.row],
                self.cols.breakpoints[cell.col],
                self.rows.values[cell.row],
                self.cols.values[cell.col],
            )
            if (cell.row_breakpoint, cell.col_breakpoint, cell.row_value, cell.col_value) != here:
                raise ValueError(
                    f"cell ({cell.row}, {cell.col}) names breakpoints "
                    f"{(cell.row_breakpoint, cell.col_breakpoint)}; its axes say {here[:2]}"
                )
        placed = math.fsum(c.weight for c in self.cells)
        unplaced = math.fsum(u.weight for u in self.unplaced.values())
        if not math.isclose(placed + unplaced, self.weight_total, rel_tol=_WEIGHT_RTOL):
            raise ValueError(
                f"cells hold {placed} and unplaced {unplaced} of weight {self.weight_total}"
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = self.model_dump(mode="json")
        return out

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, no NaN (a NaN here is a bug, refused)."""
        return json.dumps(self.to_dict(), sort_keys=True, allow_nan=False)

    def cell(self, row: int, col: int) -> Cell:
        return self.cells[row * len(self.cols.breakpoints) + col]


def cell_table_schema() -> dict[str, Any]:
    """The JSON schema of :meth:`CellTable.to_dict`."""
    return CellTable.model_json_schema()


# -- aggregation --------------------------------------------------------------------------


def _check_inputs(
    result: MonteCarloResult, design: SimDesign, axes: CellAxes, effects: EffectReport
) -> None:
    if not isinstance(result, MonteCarloResult):
        raise SimulationRefusalError(
            "not_a_monte_carlo_result",
            f"aggregate_cells takes the MonteCarloResult itself, not a {type(result).__name__}: "
            "quantiles are over (point, draw) pairs, never over a pre-reduced frame",
        )
    if not isinstance(design, SimDesign):
        raise SimulationRefusalError(
            "design_mismatch", f"the design must be the run's SimDesign, not {type(design)}"
        )
    prov = result.provenance
    if prov.get("design_sha256") != design.sha256():
        raise SimulationRefusalError(
            "design_mismatch",
            f"the result ran design {str(prov.get('design_sha256'))[:12]}; this design is "
            f"{design.sha256()[:12]}",
        )
    weights = np.array([p.weight for p in design.points], dtype=float)
    if weights.shape != result.weights.shape or not np.array_equal(weights, result.weights):
        raise SimulationRefusalError(
            "design_mismatch", "the design's points or weights are not the result's"
        )
    lo = np.array([p.fill_byte_lo for p in design.points])[:, None]
    hi = np.array([p.fill_byte_hi for p in design.points])[:, None]
    outside = (result.fill_bytes < lo) | (result.fill_bytes > hi)
    if outside.any():
        i, k = (int(v) for v in np.argwhere(outside)[0])
        raise SimulationRefusalError(
            "design_mismatch",
            f"point {i} draw {k} used byte {result.fill_bytes[i, k]}, outside the design's "
            f"[{lo[i, 0]}, {hi[i, 0]}]",
        )
    if axes.base_image_sha256 != prov.get("base_image_sha256"):
        raise SimulationRefusalError(
            "axes_image_mismatch",
            f"the grid is on image {axes.base_image_sha256[:12]}; the run's base is "
            f"{str(prov.get('base_image_sha256'))[:12]}",
        )
    report = result.rom_report
    mine = {o.output: {"status": o.status, "reasons": list(o.reasons)} for o in effects.outputs}
    if (
        effects.base_sha256 != prov.get("base_image_sha256")
        or effects.candidate_sha256 != prov.get("candidate_image_sha256")
        or effects.outcome != report.get("outcome")
        or mine != report.get("outputs")
        or list(effects.changed_nodes) != report.get("changed_nodes")
    ):
        raise SimulationRefusalError(
            "effects_mismatch",
            "the effect trace is not the one the run used (compare_roms on the run's two "
            "images, with its contract and mode)",
        )


def _reach(result: MonteCarloResult) -> dict[str, Reach]:
    report = result.rom_report
    statuses = {k: v["status"] for k, v in report.get("outputs", {}).items()}
    learned = result.learned_state
    resets = [
        f"learned:{key}:{learned['base'][key]}->{learned['candidate'][key]}"
        for key in _LEARNED_KEYS
        if learned["base"][key] != learned["candidate"][key]
    ]
    identical = report.get("outcome") == "identical"
    out: dict[str, Reach] = {}
    for name in result.outputs:
        deps = ROM_DEPENDENCIES.get(name)
        if deps is None:
            raise SimulationRefusalError(
                "output_unknown", f"{name} has no declared ROM dependencies (ROM_DEPENDENCIES)"
            )
        basis = [f"rom:{d}:{statuses.get(d, 'not_traced')}" for d in deps]
        fuel_side = name not in _ECU_OUTPUTS
        learned_basis = resets if fuel_side else []
        if learned_basis:
            out[name] = Reach(status="reached", basis=basis + learned_basis)
        elif identical:
            out[name] = Reach(status="no_change", basis=["rom:identical"])
        elif all(statuses.get(d) == "unchanged" for d in deps):
            out[name] = Reach(status="not_reached", basis=basis)
        else:
            out[name] = Reach(status="reached", basis=basis)
    return out


def _check_reach(result: MonteCarloResult, reach: Mapping[str, Reach]) -> None:
    for name, r in reach.items():
        if r.status == "reached":
            continue
        base, cand = result.base[name], result.candidate[name]
        both = np.isfinite(base) & np.isfinite(cand)
        moved = both & (cand != base)
        if moved.any():
            i, d = (int(v) for v in np.argwhere(moved)[0])
            raise SimulationRefusalError(
                "reach_contradicted",
                f"{name} is {r.status} ({r.basis}) but moves at point {i}, draw {d}: "
                f"{base[i, d]} -> {cand[i, d]}",
            )


def _coordinates(
    result: MonteCarloResult, design: SimDesign, axes: CellAxes
) -> tuple[IntArray, IntArray, NDArray[np.object_]]:
    """Row and column index per (point, draw); -1 where unplaced, with the reason."""
    n, d = result.n_points, result.draws
    words = [rpm_word(p.rpm, axes.contract) for p in design.points]
    row_of = {w: nearest_breakpoint(axes.rows.breakpoints, w) for w in set(words)}
    rows = np.repeat(np.array([row_of[w] for w in words], dtype=np.int64)[:, None], d, axis=1)
    reason = np.full((n, d), None, dtype=object)
    if axes.grid == "fuel_comp":
        bytes_ = result.fill_bytes
        col_of = {
            int(b): nearest_breakpoint(axes.cols.breakpoints, int(b)) for b in np.unique(bytes_)
        }
        cols = np.vectorize(lambda b: col_of[int(b)], otypes=[np.int64])(bytes_)
        return rows, cols, reason
    assert axes.target is not None and axes.twin is not None
    table, twin = axes.target, axes.twin
    assert table.load_point_scaler is not None
    bfs = result.base["base_fuel_schedule"]
    cols = np.full((n, d), -1, dtype=np.int64)
    cache: dict[tuple[int, int, int], int] = {}
    for i, p in enumerate(design.points):
        for k in range(d):
            word = bfs[i, k]
            if not math.isfinite(word):
                reason[i, k] = X_NOT_COMPUTED
                rows[i, k] = -1
                continue
            x = x_from_bfs(int(word), table.load_point_scaler, twin)
            key = (x, words[i], p.pedal_flag)
            if key not in cache:
                hit = predict_phi(table, twin, load_raw=x, rpm_raw=words[i], flag_raw=p.pedal_flag)
                cache[key] = nearest_index(hit.lookup.column)
            cols[i, k] = cache[key]
    return rows, cols, reason


def _quantiles(values: FloatArray, weights: FloatArray) -> Quantiles:
    q = np.quantile(values, list(QUANTILES), weights=weights, method=QUANTILE_METHOD)
    return Quantiles(p05=float(q[0]), p50=float(q[1]), p95=float(q[2]))


def _block(
    name: str,
    over: Literal["all_pairs", "supported_pairs", "computed_unsupported_pairs"],
    delta: FloatArray,
    rel: FloatArray,
    w: FloatArray,
    qw: FloatArray,
) -> QuantileBlock:
    rel_q: Quantiles | None = None
    undefined = 0
    if name not in NO_RELATIVE_DELTA:
        ok = np.isfinite(rel)
        undefined = int((~ok).sum())
        if ok.any():
            rel_q = _quantiles(rel[ok], qw[ok])
    return QuantileBlock(
        over=over,
        pairs=int(delta.size),
        weight=float(math.fsum(w)),
        delta=_quantiles(delta, qw),
        relative_delta=rel_q,
        relative_undefined_pairs=undefined,
    )


@dataclass(frozen=True)
class _Pairs:
    """The flat (point * draws + draw) indices of one cell, with their weights."""

    idx: IntArray
    #: Pair weight: the point's design weight / draws (cell weights sum it).
    w: FloatArray
    #: The point's design weight itself, for the quantiles: the same up to the
    #: common 1/draws factor, which quantiles ignore, with no division in it.
    qw: FloatArray


def _output_cell(
    result: MonteCarloResult,
    name: str,
    pairs: _Pairs,
    reach: Reach,
    empty: NDArray[np.bool_],
    *,
    computed_when_unsupported: bool = False,
) -> OutputCell:
    if pairs.idx.size == 0:
        return OutputCell(
            status="no_points",
            pairs=0,
            supported_share=None,
            unsupported_share=None,
            support=[],
            quantiles=None,
            changed_share=None,
        )
    idx, w, qw = pairs.idx, pairs.w, pairs.qw
    total = math.fsum(w)
    ids = result.support_index[name].ravel()[idx]
    ok = empty[ids]
    support = []
    for sid in sorted(set(ids.tolist())):
        sel = ids == sid
        support.append(
            SupportEntry(
                status="unsupported" if result.reason_sets[sid] else "supported",
                reasons=list(result.reason_sets[sid]),
                pairs=int(sel.sum()),
                weight_share=math.fsum(w[sel]) / total,
            )
        )
    sup_w = math.fsum(w[ok])
    supported_share = sup_w / total
    unsupported_share = 1.0 - supported_share
    delta = result.delta(name).ravel()[idx]
    rel = result.relative_delta(name).ravel()[idx]
    if reach.status == "not_reached":
        return OutputCell(
            status="not_reached",
            pairs=int(idx.size),
            supported_share=supported_share,
            unsupported_share=unsupported_share,
            support=support,
            quantiles=None,
            changed_share=None,
        )
    if not ok.any():
        quantiles = None
        computed = np.isfinite(delta)
        if computed_when_unsupported and computed.any():
            quantiles = _block(
                name,
                "computed_unsupported_pairs",
                delta[computed],
                rel[computed],
                w[computed],
                qw[computed],
            )
        return OutputCell(
            status="unsupported",
            pairs=int(idx.size),
            supported_share=supported_share,
            unsupported_share=unsupported_share,
            support=support,
            quantiles=quantiles,
            changed_share=None,
        )
    nonzero = ok & (delta != 0.0)
    changed_share = math.fsum(w[nonzero]) / sup_w
    effect: Literal["changed", "unchanged"] = "changed" if nonzero.any() else "unchanged"
    partly = not ok.all()
    return OutputCell(
        status="partly_supported" if partly else effect,
        pairs=int(idx.size),
        supported_share=supported_share,
        unsupported_share=unsupported_share,
        support=support,
        quantiles=_block(
            name, "supported_pairs" if partly else "all_pairs", delta[ok], rel[ok], w[ok], qw[ok]
        ),
        changed_share=changed_share,
        supported_effect=effect if partly else None,
    )


def _shares(labels: Sequence[str], w: FloatArray) -> dict[str, float]:
    total = math.fsum(w)
    acc: dict[str, list[float]] = {}
    for label, weight in zip(labels, w.tolist(), strict=True):
        acc.setdefault(label, []).append(weight)
    return {k: math.fsum(v) / total for k, v in sorted(acc.items())}


def _overlay(
    result: MonteCarloResult, axes: CellAxes, effects: EffectReport
) -> tuple[Overlay, dict[tuple[int, int], EditMark], set[int], set[int]]:
    edited: list[EditedCell] = []
    marks: dict[tuple[int, int], EditMark] = {}
    axis_edits: list[AxisEdit] = []
    moved_rows: set[int] = set()
    moved_cols: set[int] = set()
    other: Counter[tuple[str, int | None]] = Counter()
    for c in result.affected_cells:
        table, slot = c["table"], c.get("slot")
        if table == axes.table and slot == axes.slot:
            edited.append(EditedCell(row=c["row"], col=c["col"], old=c["old"], new=c["new"]))
            marks[(c["row"], c["col"])] = EditMark(old=c["old"], new=c["new"])
            continue
        # An axis table is one row or one column: its breakpoint index is the other.
        index = max(c["row"], c["col"])
        if table == axes.rows.table:
            axis_edits.append(
                AxisEdit(axis="rows", table=table, index=index, old=c["old"], new=c["new"])
            )
            moved_rows.add(index)
        if table == axes.cols.table:
            axis_edits.append(
                AxisEdit(axis="cols", table=table, index=index, old=c["old"], new=c["new"])
            )
            moved_cols.add(index)
        if table not in (axes.rows.table, axes.cols.table):
            other[(table, slot)] += 1
    grid_tables = {axes.table, axes.rows.table, axes.cols.table}
    moved_lookups = [
        MovedLookup(
            node=str(m["node"]),
            tables=[str(t) for t in m["tables"]],
            on_this_grid=bool(grid_tables & set(m["tables"])),
        )
        for m in effects.moved_lookups
    ]
    overlay = Overlay(
        edited_cells=edited,
        axis_edits=axis_edits,
        other_edits=[
            OtherEdit(table=t, slot=s, cells=n)
            for (t, s), n in sorted(other.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0))
        ],
        moved_lookups=moved_lookups,
    )
    return overlay, marks, moved_rows, moved_cols


def _axis_info(axis: GridAxis) -> AxisInfo:
    return AxisInfo(
        table=axis.table,
        coordinate=axis.coordinate,
        raw=list(axis.raw),
        breakpoints=list(axis.breakpoints),
        values=list(axis.values),
        unit=axis.unit,
        domain=axis.domain,
        cell_ranges=cell_ranges(axis.breakpoints),
    )


def aggregate_cells(
    result: MonteCarloResult,
    design: SimDesign,
    axes: CellAxes,
    *,
    effects: EffectReport,
) -> CellTable:
    """One row per cell of ``axes``' grid (module docstring).

    ``effects`` is :func:`~src.analysis.rom_effects.compare_roms` on the run's
    two images with its contract and mode (the run's ``rom_report`` is checked
    against it): it gives the moved lookups for the overlay."""
    _check_inputs(result, design, axes, effects)
    reach = _reach(result)
    _check_reach(result, reach)
    rows, cols, unplaced_reason = _coordinates(result, design, axes)
    n, d = result.n_points, result.draws
    pair_w = np.repeat((result.weights / d)[:, None], d, axis=1)
    regime = np.repeat(
        np.array([p.regime for p in design.points], dtype=object)[:, None], d, axis=1
    )
    empty = np.array([not r for r in result.reason_sets], dtype=bool)

    flat_rows, flat_cols, flat_w = rows.ravel(), cols.ravel(), pair_w.ravel()
    flat_qw = np.repeat(result.weights, d)
    flat_reason = unplaced_reason.ravel()
    flat_regime, flat_loop = regime.ravel(), result.loop.ravel()
    point_of = np.repeat(np.arange(n), d)
    unplaced: dict[str, Unplaced] = {}
    for why in sorted({str(r) for r in flat_reason if r is not None}):
        sel = flat_reason == why
        unplaced[why] = Unplaced(pairs=int(sel.sum()), weight=math.fsum(flat_w[sel]))

    overlay, marks, moved_rows, moved_cols = _overlay(result, axes, effects)
    n_cols = len(axes.cols.breakpoints)
    cell_id = np.where(flat_rows >= 0, flat_rows * n_cols + flat_cols, -1)
    order = np.argsort(cell_id, kind="stable")
    sorted_ids = cell_id[order]
    cells: list[Cell] = []
    counts: dict[str, Counter[str]] = {name: Counter() for name in result.outputs}
    for r in range(len(axes.rows.breakpoints)):
        for c in range(n_cols):
            k = r * n_cols + c
            lo, hi = np.searchsorted(sorted_ids, [k, k + 1])
            idx = np.asarray(order[lo:hi], dtype=np.int64)
            pairs = _Pairs(idx, flat_w[idx], flat_qw[idx])
            outputs = {
                name: _output_cell(result, name, pairs, reach[name], empty)
                for name in result.outputs
            }
            for name, oc in outputs.items():
                counts[name][oc.status] += 1
            by_loop: list[LoopBlock] = []
            if idx.size:
                total = math.fsum(pairs.w)
                for loop in sorted(set(flat_loop[idx].tolist())):
                    sel = flat_loop[idx] == loop
                    sub = _Pairs(idx[sel], pairs.w[sel], pairs.qw[sel])
                    headline = HEADLINE_BY_LOOP.get(loop)
                    if headline is None:
                        raise SimulationRefusalError(
                            "loop_unknown", f"no headline output for loop {loop!r}"
                        )
                    by_loop.append(
                        LoopBlock(
                            loop=loop,
                            weight_share=math.fsum(sub.w) / total,
                            pairs=int(sub.idx.size),
                            output=headline,
                            cell=_output_cell(
                                result,
                                headline,
                                sub,
                                reach[headline],
                                empty,
                                computed_when_unsupported=loop == "closed_loop",
                            ),
                        )
                    )
            points = sorted({int(p) for p in point_of[idx]})
            cells.append(
                Cell(
                    row=r,
                    col=c,
                    row_breakpoint=axes.rows.breakpoints[r],
                    col_breakpoint=axes.cols.breakpoints[c],
                    row_value=axes.rows.values[r],
                    col_value=axes.cols.values[c],
                    n_points=len(points),
                    points=points,
                    pairs=int(idx.size),
                    weight=math.fsum(pairs.w) if idx.size else 0.0,
                    regime_shares=_shares([str(x) for x in flat_regime[idx]], pairs.w)
                    if idx.size
                    else {},
                    loop_shares=_shares([str(x) for x in flat_loop[idx]], pairs.w)
                    if idx.size
                    else {},
                    outputs=outputs,
                    by_loop=by_loop,
                    edited=marks.get((r, c)),
                    row_axis_moved=r in moved_rows,
                    col_axis_moved=c in moved_cols,
                )
            )
    prov = result.provenance
    return CellTable(
        schema_version=SCHEMA_VERSION,
        tool=TOOL,
        tool_version=TOOL_VERSION,
        grid=axes.grid,
        table=axes.table,
        slot=axes.slot,
        membership=MEMBERSHIP,
        membership_rule=MEMBERSHIP_RULE,
        quantiles=list(QUANTILES),
        quantile_method=QUANTILE_METHOD,
        weight_basis="pair weight = the point's design weight / draws",
        base_image_sha256=str(prov["base_image_sha256"]),
        candidate_image_sha256=str(prov["candidate_image_sha256"]),
        design_sha256=design.sha256(),
        design_kind=design.kind,
        result_digest=result.digest(),
        draws=d,
        n_points=n,
        outputs=list(result.outputs),
        headline_by_loop=dict(HEADLINE_BY_LOOP),
        rows=_axis_info(axes.rows),
        cols=_axis_info(axes.cols),
        reach=reach,
        caveats=list(result.caveats),
        unresolved=list(result.unresolved),
        rom_outcome=str(result.rom_report.get("outcome")),
        weight_total=math.fsum(result.weights.tolist()),
        unplaced=unplaced,
        status_counts={
            name: {s: counts[name][s] for s in STATUSES if counts[name][s]}
            for name in result.outputs
        },
        overlay=overlay,
        cells=cells,
    )


# -- E09.7: ignition timing at the design's points --------------------------------------


def _load_point_scaler(kit: SimKit, image: bytes, *, slot: int) -> int:
    try:
        table = resolve_table(
            image,
            kit.rom_map,
            kit.twin,
            viewer=kit.viewer,
            application_model=(kit.model, kit.model_sha256),
            expected_sha256=sha256_hex(image),
            slot=slot,
        )
    except TwinRefusalError as exc:
        raise SimulationRefusalError(f"target_table:{exc.reason}", str(exc)) from exc
    if table.load_point_scaler is None:
        raise SimulationRefusalError(
            "load_point_scaler_unresolved", "an image's Load Point Scaler was not read"
        )
    return int(table.load_point_scaler)


def _torque_and_knock(kit: SimKit) -> dict[str, Any]:
    """What a timing change says about torque and knock: E07.5's own refusals
    (no subsystem standing is supplied, so none is validated), never a claim."""
    rollout = next(m.id for m in kit.contract.modes if m.kind == "coupled_rollout")
    torque = claim_refusals(kit.contract, {}, rollout, "torque", changed_controls=["spark"])
    knock = claim_refusals(kit.contract, {}, rollout, "knock", changed_controls=["spark"])
    spark = [r for r in torque if r.startswith("spark_response_unmeasured")]
    return {
        "torque": {
            "status": "unsupported" if torque else "supported",
            # The refusal about the change itself, before the standing ones.
            "reason": next(iter(spark or torque), None),
            "reasons": torque,
        },
        "knock": {"status": "unsupported" if knock else "supported", "reasons": knock},
    }


#: A changed byte other than the timing trims reaches ``ignition_timing``
#: (``compare_roms``): suffixed with the node it changes. The trims' own grid
#: holds every other term equal, so such an edit gives no timing there.
TIMING_INPUT_CHANGED = "timing_input_changed"
#: ``compare_roms`` provenance for the timing reach: the caller's map slot.
_TIMING_SLOT_SOURCE = "timing_block_caller_slot"


def timing_reach_reasons(
    kit: SimKit, base_image: bytes, candidate_image: bytes, *, slot: int
) -> list[str]:
    """Why ``ignition_timing_deg`` cannot be given for this edit beyond what
    the timing computation reads itself: every changed byte outside the two
    timing trims and the base-sum terms' tables
    (:func:`~src.analysis.paired_scenarios.base_timing_rom_ids`: the 0xFFFF27C6
    chain and 0xFFFF279A's inputs, computed or refused per point) is traced
    with :func:`~src.analysis.rom_effects.compare_roms`, and one that reaches
    the ``ignition_timing`` output names its node
    (``timing_input_changed:<node>``); a byte no node reads is
    ``unknown_bytes_changed``, since nothing says it cannot. Checksum words and
    inactive slot copies reach nothing. Empty when only those tables (or
    nothing) changed."""
    report = compare_roms(
        base_image,
        candidate_image,
        kit.rom_map,
        kit.model,
        kit.model_sha256,
        base_slot=slot,
        candidate_slot=slot,
        slot_source=_TIMING_SLOT_SOURCE,
    )
    if report.outcome == "identical":
        return []
    if report.reason is not None:
        return [f"effects_unevaluable:{report.reason}"]
    trims = {t.id for t in kit.rom_map.tables if t.kind in ("timing_trim_a", "timing_trim_b")}
    trims |= base_timing_rom_ids(kit.rom_map)
    path = set(report.output(TIMING_MODEL_OUTPUT).path)
    reasons: list[str] = []
    for r in report.ranges:
        if r.byte_class in ("checksum", "inactive_for_scenario"):
            continue
        if r.byte_class in ("unknown", "mapped_unmodelled"):
            reasons.append("unknown_bytes_changed")
            continue
        if {w.split(" (slot ", 1)[0] for w in r.what} <= trims:
            continue
        reasons += [f"{TIMING_INPUT_CHANGED}:{n}" for n in r.nodes if n in path]
    return list(dict.fromkeys(reasons))


def _carried_by_points(kit: SimKit, node: str) -> bool:
    """Whether a changed node reaches ``ignition_timing`` only through what
    :func:`ignition_timing_points` computes per ROM: the contract quantities
    the slice computes (``COMPUTED_HERE``: BFS, and X from it through each
    ROM's Load Point Scaler). Such a change (K, Fuel Comp, the scaler) is in
    each ROM's own X and BFS. The points compute the trims and 0xFFFF27C6 from
    those, but not everything downstream of them: the charge word 0xFFFF1B24
    reads X (through 0xFFFF1736) and BFS (through 0xFFFF175C), neither
    modelled, so :func:`ignition_timing_at` sweeps it on each ROM
    independently and names ``charge_word_inputs_moved``; 0xFFFF279A reads BFS
    and is ``term_279a_unmodelled`` unless provably 0. That is why dropping
    ``timing_input_changed`` for such a node is sound: its unmodelled routes
    are named per pair instead. Any other route to the timing writer is still
    named."""
    computed = {n for n, q in kit.contract.by_manifest_node().items() if q.id in COMPUTED_HERE}
    if node in computed:
        return True
    children: dict[str, set[str]] = {}
    for n in kit.model.nodes:
        for dep in n.depends_on:
            children.setdefault(dep, set()).add(n.id)
    writer = kit.model.output(TIMING_MODEL_OUTPUT).written_by
    seen, todo = {node}, [node]
    while todo:
        for child in children.get(todo.pop(), ()):
            if child == writer:
                return False
            if child not in seen and child not in computed:
                seen.add(child)
                todo.append(child)
    return True


def ignition_timing_block(
    kit: SimKit,
    base_image: bytes,
    candidate_image: bytes,
    state: TimingState | None,
    *,
    slot: int,
    coolant_c: float | None,
    all_cells: bool,
) -> dict[str, Any]:
    """``ignition_timing_deg`` on the trims' own grid (E09.7), the one block
    the CLI and the ROM Editor both show. An edit that changes a trim refuses
    without a stated timing state (``timing_state_unstated``): which trim the
    ECM reads warm is 0xFFFF283F bit 6, which nothing reads, so there is no
    default. An edit with any other change that reaches ``ignition_timing``
    (:func:`timing_reach_reasons`) is ``unevaluable`` with those reasons and
    no cells: the trims' grid holds that change's terms equal, so its "0
    changed" would be a claim the chain cannot make. ``all_cells`` False keeps
    the cells that change or name a reason beyond ``clamp_headroom_unknown``."""
    check_image(base_image, kit.model)
    check_image(candidate_image, kit.model)
    base_t = timing_tables(base_image, kit.rom_map, slot=slot)
    cand_t = timing_tables(candidate_image, kit.rom_map, slot=slot)
    trims = (base_t.a, base_t.b) != (cand_t.a, cand_t.b)
    base_terms = base_t.base_term != cand_t.base_term
    edited = trims or base_terms
    reach = timing_reach_reasons(kit, base_image, candidate_image, slot=slot)
    block: dict[str, Any] = {
        "output": TIMING_OUTPUT,
        "model_output": TIMING_MODEL_OUTPUT,
        "units": "deg_btdc",
        "side": "ecu: the timing byte's chain, no plant",
        "trims_changed": trims,
        "base_timing_tables_changed": base_terms,
        "other_inputs_changed": reach,
        "conditions": list(TIMING_DELTA_CONDITIONS),
        **_torque_and_knock(kit),
    }
    if state is None:
        if edited:
            raise SimulationRefusalError(
                TIMING_STATE_UNSTATED,
                "the edit changes a timing trim or a base timing table: state the HI DET latch "
                "(set|clear) and knock control (active|fallback); nothing reads 0xFFFF283F bit 6, "
                "so there is no default",
            )
        return {**block, "status": "unevaluable", "reasons": [TIMING_STATE_UNSTATED, *reach]}
    if coolant_c is None:
        raise SimulationRefusalError(
            "timing_coolant_unstated", "the timing cells need a stated coolant temperature"
        )
    stated = {
        "high_det": state.high_det,
        "knock_control": state.knock_control,
        "source": state.source,
    }
    if reach:
        return {**block, "status": "unevaluable", "reasons": reach, "state": stated}
    cells = ignition_timing_cells(base_t, cand_t, state, coolant_c=coolant_c)
    quiet = {"clamp_headroom_unknown"}
    shown = [c for c in cells if c["delta"] not in (None, 0) or set(c["reasons"]) - quiet]
    return {
        **block,
        "status": "evaluated",
        "state": stated,
        "cells_at": {
            "coolant_c": coolant_c,
            "pedal_flag": False,
            "grid": "the trims' own nodes: rpm word = axis byte x 4, X = load axis byte",
        },
        "summary": timing_cell_summary(cells),
        "cells": cells if all_cells else shown,
    }


def _lookup_byte(kit: SimKit, phi: float) -> int | None:
    """The gated Target AFR byte 0xFFFF174B behind a twin phi: phi is
    ``(174B << cell_to_word_shift) / 2**q_bits``. ``None`` when phi is not
    known (the gate's hysteresis band) or is not a whole byte."""
    if not math.isfinite(phi):
        return None
    scale = 1 << (kit.twin.reference.q_bits - kit.twin.table.cell_to_word_shift)
    byte = phi * scale
    return int(byte) if byte.is_integer() and 0 <= byte <= 0xFF else None


#: What each design-point outcome carries from :func:`ignition_timing_at`.
_POINT_KEYS: tuple[str, ...] = (
    "status",
    "delta",
    "delta_min",
    "delta_max",
    "trim_term_delta",
    "term_27c6_delta",
    "term_279a",
    "charge_word_routes",
    "reasons",
)


def ignition_timing_points(
    kit: SimKit,
    result: MonteCarloResult,
    design: SimDesign,
    base_image: bytes,
    candidate_image: bytes,
    state: TimingState | None,
    *,
    slot: int,
) -> list[dict[str, Any]]:
    """``ignition_timing_deg`` at every design point (E09.7), per (point, draw)
    pair and grouped per point by outcome.

    Each ROM's own X comes from that pair's own BFS through that ROM's Load
    Point Scaler (as the twin computes it), so a K or Fuel Comp change reaches
    the trims' load column and 0xFFFF27C6, which is computed per ROM at its
    own X (a range across the swept 0xFFFF1B24, ``charge_word_1b24_unresolved``).
    A pair whose X, BFS or phi moved moves 1B24's inputs (0xFFFF1736 reads X,
    0xFFFF175C reads BFS, 0xFFFF26C0 follows the Target AFR lookup; none is
    modelled), so 1B24 is swept on each ROM independently and the pair names
    ``charge_word_inputs_moved`` with its ``charge_word_routes``; a moved BFS
    makes a 0xFFFF279A that is not provably 0 unevaluable
    (``term_279a_unmodelled``). A pair whose Target AFR phi moved, or whose X
    moved where either ROM's gated Target AFR byte may enrich
    (:func:`_lookup_byte`; :class:`EnrichmentRetardCals`), is held
    (``enrichment_retard_unmodelled``): the retard's cap 0xFFFF174C reads X. A change whose route to
    ``ignition_timing`` runs only through BFS and X (:func:`_carried_by_points`)
    is accounted for that way; any other change that reaches it
    (:func:`timing_reach_reasons`) leaves every pair ``unevaluable`` with its
    reasons, the trim term's change still shown. Design points carry no
    logged timing, so the absolute is never given here."""
    if not isinstance(result, MonteCarloResult) or not isinstance(design, SimDesign):
        raise SimulationRefusalError(
            "not_a_monte_carlo_result", "the timing points take the run's result and design"
        )
    prov = result.provenance
    for name, got in (
        ("design_sha256", design.sha256()),
        ("base_image_sha256", sha256_hex(base_image)),
        ("candidate_image_sha256", sha256_hex(candidate_image)),
    ):
        if prov.get(name) != got:
            raise SimulationRefusalError(
                "design_mismatch", f"{name} {got[:12]} is not the run's {prov.get(name)}"
            )
    reach = [
        r
        for r in timing_reach_reasons(kit, base_image, candidate_image, slot=slot)
        if not (
            r.startswith(f"{TIMING_INPUT_CHANGED}:") and _carried_by_points(kit, r.split(":", 1)[1])
        )
    ]
    tb, tc = (
        timing_tables(base_image, kit.rom_map, slot=slot),
        timing_tables(candidate_image, kit.rom_map, slot=slot),
    )
    lps = (
        _load_point_scaler(kit, base_image, slot=slot),
        _load_point_scaler(kit, candidate_image, slot=slot),
    )
    bit = kit.twin.inputs.forced_column_flag.bit
    bfs = (result.base["base_fuel_schedule"], result.candidate["base_fuel_schedule"])
    phi = (result.base["target_phi"], result.candidate["target_phi"])
    rows: list[dict[str, Any]] = []
    for i, p in enumerate(design.points):
        word = rpm_word(p.rpm, kit.contract)
        pedal = bool((p.pedal_flag >> bit) & 1)
        outcomes: Counter[str] = Counter()
        detail: dict[str, dict[str, Any]] = {}
        # ``ignition_timing_at`` is a pure function of the point's own rpm word,
        # coolant and pedal flag and of what each pair gives it (the two X, the
        # two moved flags, the two lookup bytes), and X is a quantized byte, so
        # the draws of one point repeat those inputs. Each distinct input is
        # computed once; the point's outcome (the dict and its JSON key) is
        # shared by every pair that gave it. Per point, never across points, so
        # no two rows hold the same dict or list.
        memo: dict[tuple[int, int, bool, bool, int | None, int | None], tuple[str, dict[str, Any]]]
        memo = {}
        not_computed: tuple[str, dict[str, Any]] | None = None
        for k in range(result.draws):
            words = (bfs[0][i, k], bfs[1][i, k])
            if not all(math.isfinite(w) for w in words):
                if not_computed is None:
                    at: dict[str, Any] = {
                        "status": "unevaluable",
                        "delta": None,
                        "delta_min": None,
                        "delta_max": None,
                        "trim_term_delta": None,
                        "term_27c6_delta": None,
                        "term_279a": None,
                        "charge_word_routes": None,
                        "reasons": [X_NOT_COMPUTED],
                        "x_base": None,
                        "x_candidate": None,
                    }
                    not_computed = (json.dumps(at, sort_keys=True), at)
                key, at = not_computed
            else:
                xb = x_from_bfs(int(words[0]), lps[0], kit.twin)
                xc = x_from_bfs(int(words[1]), lps[1], kit.twin)
                pb, pc = float(phi[0][i, k]), float(phi[1][i, k])
                moved = math.isfinite(pb) != math.isfinite(pc) or (math.isfinite(pb) and pb != pc)
                bfs_moved = int(words[0]) != int(words[1])
                lookup = (_lookup_byte(kit, pb), _lookup_byte(kit, pc))
                inputs = (xb, xc, moved, bfs_moved, *lookup)
                if inputs not in memo:
                    full = ignition_timing_at(
                        tb,
                        tc,
                        state,
                        rpm_word=word,
                        x_base=xb,
                        x_candidate=xc,
                        coolant_c=p.coolant_c,
                        pedal_flag=pedal,
                        phi_moved=moved,
                        bfs_moved=bfs_moved,
                        lookup_bytes=lookup,
                    )
                    at = {key: full[key] for key in _POINT_KEYS}
                    at.update(x_base=xb, x_candidate=xc)
                    if reach:
                        at.update(
                            status="unevaluable",
                            delta=None,
                            delta_min=None,
                            delta_max=None,
                            reasons=list(dict.fromkeys([*at["reasons"], *reach])),
                        )
                    memo[inputs] = (json.dumps(at, sort_keys=True), at)
                key, at = memo[inputs]
            outcomes[key] += 1
            detail[key] = at
        rows.append(
            {
                "point": i,
                "rpm": p.rpm,
                "rpm_word": word,
                "coolant_c": p.coolant_c,
                "pedal_flag": pedal,
                "outcomes": [{**detail[key], "draws": n} for key, n in sorted(outcomes.items())],
            }
        )
    return rows


__all__ = [
    "TIMING_INPUT_CHANGED",
    "ignition_timing_block",
    "ignition_timing_points",
    "timing_reach_reasons",
    "HEADLINE_BY_LOOP",
    "MEMBERSHIP",
    "NO_RELATIVE_DELTA",
    "QUANTILES",
    "ROM_DEPENDENCIES",
    "SCHEMA_VERSION",
    "STATUSES",
    "TOOL_VERSION",
    "TRIM_AUTHORITY_UNDECODED",
    "X_NOT_COMPUTED",
    "CellAxes",
    "CellStatus",
    "CellTable",
    "GridAxis",
    "aggregate_cells",
    "cell_ranges",
    "cell_table_schema",
    "fuel_comp_axes",
    "nearest_breakpoint",
    "nearest_index",
    "target_afr_axes",
]
