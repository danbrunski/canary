"""Which cell of a ROM table the ECM is reading, and which cells a log visited.

The ROM Maps page (W05) draws two operating points on a table. The *inferred*
lookup puts rpm and BFS on the table's axes; the *ECM* cell comes from the
map-trace memory channels (``data/dids.yaml`` ``memory:``), which are the
ECM's own coordinates into its tables. Keeping both makes a wrong axis decode,
a wrong BFS scale, or unmodelled interpolation visible as a disagreement.

A trace channel is only useful once it is known how its value lands on the
table's breakpoints. That is a measured constraint, so it lives in the vehicle
profile's ``rom_viewer.yaml`` as a required ``axis_mapping`` state per axis,
with its evidence, rather than in a comment (AGENTS.md, "Make it impossible,
not documented"). An ``unverified`` axis carries no scale at all — there is
nothing to guess with — and every consumer here refuses to place a cell on it,
naming the axis and what would settle it.

A verified axis also says *which* breakpoints its value is compared with
(``axis_units``): the axis in its displayed units, or the axis bytes as the
ROM stores them. A ``mem:`` channel is stored scaled by its ``data/dids.yaml``
decode, so a ``raw`` mapping first undoes that decode (and refuses values that
do not come back to whole counts). A scaled value is never compared with raw
bytes (W05.1: the fuel-target X trace is stored as raw x 0.250859, and the
ECM compares the raw byte with the raw BFS axis).

What the ECM's lookup does past an axis end is a program fact too, so it is
declared per table (``out_of_range``); left undeclared, a sample past an end
is counted as outside the axes and no cell is placed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, NamedTuple

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.channels import ChannelSpecError, as_of_refusal, read_channels
from src.decode.dids import MEMORY_SOURCE, load_did_map
from src.decode.slices import linear_factors
from src.signals.pairing import PairingPolicy, match_ordered, to_seconds

TableKind = Literal["fuel_targets", "fuel_compensation"]

#: Which breakpoints a verified trace axis is compared with. ``display``: the
#: table's axis in its displayed units (rpm, ms), mapped from the stored
#: value. ``raw``: the axis bytes as the ROM stores them, mapped from the
#: channel's raw counts (the stored value with its catalog decode undone).
AxisUnits = Literal["display", "raw"]

#: Which end of an axis a coordinate fell past.
AxisEdge = Literal["below", "above"]

#: How far a value undone by its catalog decode may sit from a whole count
#: and still be that count: float round-off, never a measurement.
WHOLE_COUNT_TOLERANCE = 1e-6

#: The X and Y traces of one table live in different memory windows, so each
#: pair is two reads. At the 8 Hz the retired ``rom-trace`` profile polled,
#: interleaved reads sit ~60 ms apart; 250 ms admits jitter without pairing a
#: sample with its neighbour's partner.
PAIR_MAX_SEPARATION = pd.Timedelta("250ms")

#: A pair's dwell is the time until the next pair, capped here so a polling
#: gap (a stalled session, a lapse) is not credited to whichever cell the
#: trace happened to sit in before it.
MAX_DWELL_S = 1.0


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AxisTrace(_Strict):
    """One axis of a table: which channel is the ECM's coordinate, and whether
    it is known how that coordinate lands on the axis breakpoints."""

    #: Channel spec as stored, e.g. ``mem:ECM_ENGINE_SPEED_RAM``.
    channel: str = Field(pattern=r"^[a-z]+:[A-Za-z0-9_]+$")
    axis_mapping: Literal["verified", "unverified"]
    #: Which breakpoints the mapped value lands on (:data:`AxisUnits`).
    #: Required when verified, so a mapping always says whether it compares
    #: with displayed values or raw axis bytes; forbidden when not.
    axis_units: AxisUnits | None = None
    #: ``axis = input x scale + offset``, where the input is the stored value
    #: (``display``) or the channel's raw count (``raw``). Required when
    #: verified; forbidden when not, so no scale is ever guessed.
    scale: float | None = None
    offset: float | None = None
    #: How the mapping was settled, or what would settle it.
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _mapping_state(self) -> AxisTrace:
        if self.axis_mapping == "verified":
            if self.scale is None or self.offset is None or self.scale == 0:
                raise ValueError("a verified axis mapping needs a nonzero scale and an offset")
            if self.axis_units is None:
                raise ValueError(
                    "a verified axis mapping must state axis_units: 'display' (the "
                    "axis's displayed values) or 'raw' (the ROM's axis bytes)"
                )
        elif self.scale is not None or self.offset is not None or self.axis_units is not None:
            raise ValueError(
                "an unverified axis mapping carries no scale, offset or axis_units; "
                "record the evidence that would settle it instead"
            )
        return self

    def to_axis(self, value: float) -> float:
        """The mapping's input (stored value, or raw count for ``raw``) in the
        units of the breakpoints it is compared with. Refuses an unverified
        mapping."""
        if self.axis_mapping != "verified" or self.scale is None or self.offset is None:
            raise ValueError(f"{self.channel}: axis mapping is unverified")
        return value * self.scale + self.offset


class EdgeBehaviour(_Strict):
    """What the ECM's lookup does with a coordinate past an end of an axis."""

    #: ``clamp``: it reads the first or last row/column.
    behaviour: Literal["clamp"]
    evidence: str = Field(min_length=1)


class TableTrace(_Strict):
    """The ECM's coordinates into one table (W05.1)."""

    x: AxisTrace
    y: AxisTrace
    #: Shown when no capture profile polls the channels: why, and when they
    #: are expected back. Vehicle facts, so data rather than page copy.
    unavailable_note: str | None = None
    #: Both axes, both ends. ``None``: not established, so a sample past an
    #: end is outside the axes and places no cell.
    out_of_range: EdgeBehaviour | None = None

    @property
    def clamps(self) -> bool:
        return self.out_of_range is not None and self.out_of_range.behaviour == "clamp"

    def channels(self) -> list[str]:
        return list(dict.fromkeys([self.x.channel, self.y.channel]))

    def unverified_axes(self) -> list[tuple[str, AxisTrace]]:
        return [
            (name, axis)
            for name, axis in (("x", self.x), ("y", self.y))
            if axis.axis_mapping != "verified"
        ]


class TableInfo(_Strict):
    kind: TableKind
    title: str
    x_label: str
    x_unit: str
    y_label: str
    y_unit: str
    unit: str
    trace: TableTrace | None = None


class ViewerDefinition(_Strict):
    schema_version: Literal[1]
    map_slots: list[int] = Field(min_length=1)
    target_definition: str
    tables: list[TableInfo]
    warnings: list[str] = Field(default_factory=list)

    def table(self, kind: str) -> TableInfo | None:
        return next((info for info in self.tables if info.kind == kind), None)


def load_viewer_definition(profile_dir: Path) -> ViewerDefinition:
    """The vehicle profile's ``rom_viewer.yaml``. Raises ``FileNotFoundError``."""
    return ViewerDefinition.model_validate(
        yaml.safe_load((profile_dir / "rom_viewer.yaml").read_text())
    )


def request_channel(spec: str, dids_path: Path) -> str | None:
    """The scheduler request that carries ``spec``: ``mem:<window>``, or
    ``mem:<name>`` for an unwindowed channel. ``None`` when it is not a
    memory channel in the catalog — the page then has no per-request counters
    to show, and says so rather than guessing a row."""
    source, _, name = spec.partition(":")
    if source != MEMORY_SOURCE:
        return None
    try:
        catalog = load_did_map(dids_path)
    except (OSError, ValueError):
        return None
    channel = next((c for c in catalog.memory if c.name == name), None)
    if channel is None:
        return None
    return f"{MEMORY_SOURCE}:{channel.window or channel.name}"


def stored_decode(spec: str, dids_path: Path) -> tuple[float, float] | None:
    """``(scale, offset)`` a catalog ``mem:`` channel is stored under, so a
    ``raw`` mapping can undo it. An unscaled channel is stored as raw counts
    (``(1.0, 0.0)``). ``None`` when ``spec`` is not a catalog memory channel:
    there is then no known decode to undo, and a raw mapping refuses."""
    source, _, name = spec.partition(":")
    if source != MEMORY_SOURCE:
        return None
    try:
        catalog = load_did_map(dids_path)
    except (OSError, ValueError):
        return None
    channel = next((c for c in catalog.memory if c.name == name), None)
    if channel is None:
        return None
    return linear_factors(channel.scale, channel.offset) or (1.0, 0.0)


def raw_counts(stored: np.ndarray, decode: tuple[float, float]) -> np.ndarray | None:
    """Stored values with their catalog decode undone, as whole counts.

    A memory channel's raw value is an integer read from the ECM, so undoing
    the decode it was stored under must land on whole counts. ``None`` when
    it does not: the log was stored under some other decode, and its raw
    counts cannot be recovered with this one."""
    scale, offset = decode
    raw = (np.asarray(stored, dtype=float) - offset) / scale
    whole = np.rint(raw)
    finite = np.isfinite(raw)
    if not np.all(np.abs(raw[finite] - whole[finite]) <= WHOLE_COUNT_TOLERANCE):
        return None
    return whole


def trace_payload(trace: TableTrace | None, dids_path: Path) -> dict[str, Any] | None:
    """``trace`` as the inspect response carries it, with each axis's request
    channel and the catalog decode its stored values carry."""
    if trace is None:
        return None
    payload = trace.model_dump()
    for axis in ("x", "y"):
        spec = payload[axis]["channel"]
        payload[axis]["request_channel"] = request_channel(spec, dids_path)
        decode = stored_decode(spec, dids_path)
        payload[axis]["stored_decode"] = (
            None if decode is None else {"scale": decode[0], "offset": decode[1]}
        )
    return payload


def mapping_refusal(trace: TableTrace | None) -> dict[str, Any] | None:
    """The named reason no cell may be placed from ``trace``, or ``None``."""
    if trace is None:
        return {
            "reason": "no_trace_mapping",
            "message": "This vehicle's rom_viewer.yaml maps no trace channels to this table.",
            "missing": [],
        }
    unverified = trace.unverified_axes()
    if unverified:
        names = ", ".join(f"{name} ({axis.channel})" for name, axis in unverified)
        return {
            "reason": "axis_mapping_unverified",
            "message": (
                f"How the {names} trace lands on this table's breakpoints is not "
                "established; no ECM cell is placed. "
                + " ".join(axis.evidence.strip() for _, axis in unverified)
            ),
            "missing": [],
        }
    return None


def axis_position(
    breakpoints: Sequence[float], value: float, *, clamp: bool
) -> tuple[float, AxisEdge | None] | None:
    """Where ``value`` sits on ``breakpoints`` as a fractional index.

    ``i + t`` means ``t`` of the way from breakpoint ``i`` to ``i + 1``: the
    weight a linear (per axis, so bilinear per table) lookup gives the upper
    breakpoint. A value equal to a breakpoint is exactly that index. Past an
    end it is that end's index and names the edge when ``clamp`` (the lookup
    reads the edge row or column), and ``None`` otherwise.
    """
    if not breakpoints or not math.isfinite(value):
        return None
    if value < breakpoints[0]:
        return (0.0, "below") if clamp else None
    if value > breakpoints[-1]:
        return (float(len(breakpoints) - 1), "above") if clamp else None
    upper = next(i for i, point in enumerate(breakpoints) if point >= value)
    if breakpoints[upper] == value:
        return float(upper), None
    lower = upper - 1
    span = breakpoints[upper] - breakpoints[lower]
    return lower + (value - breakpoints[lower]) / span, None


def nearest_cell_index(position: float) -> int:
    """Nearest breakpoint to a fractional position. A tie goes low, as the
    page's inferred lookup does."""
    lower = math.floor(position)
    return int(lower if position - lower <= 0.5 else lower + 1)


def nearest_index(axis: list[float], value: float) -> int | None:
    """Nearest breakpoint, or ``None`` outside the axis. Ties go low, as the
    page's inferred lookup does."""
    position = axis_position(axis, value, clamp=False)
    return None if position is None else nearest_cell_index(position[0])


class EcmCell(NamedTuple):
    """The cell a pair of ECM trace coordinates points at."""

    row: int
    column: int
    #: Fractional positions on each axis (:func:`axis_position`).
    y_position: float
    x_position: float
    #: The edge a clamped coordinate fell past, or ``None``.
    y_edge: AxisEdge | None
    x_edge: AxisEdge | None


class TraceAxes(NamedTuple):
    """A table's breakpoints on one revision, as its trace axes compare them."""

    #: What each trace axis is compared with: displayed or raw breakpoints,
    #: per that axis's ``axis_units``.
    x: list[float]
    y: list[float]
    #: The displayed axes, which the page draws and a residency grid names.
    x_display: list[float]
    y_display: list[float]


def ecm_cell(axes: TraceAxes, x: float, y: float, *, clamp: bool) -> EcmCell | None:
    """The nearest cell to trace coordinates ``(x, y)`` (each already in its
    axis's comparison units), or ``None`` when either is past an axis end and
    the table declares no clamp."""
    x_at = axis_position(axes.x, x, clamp=clamp)
    y_at = axis_position(axes.y, y, clamp=clamp)
    if x_at is None or y_at is None:
        return None
    return EcmCell(
        row=nearest_cell_index(y_at[0]),
        column=nearest_cell_index(x_at[0]),
        y_position=y_at[0],
        x_position=x_at[0],
        y_edge=y_at[1],
        x_edge=x_at[1],
    )


class _Axes(BaseModel):
    """One revision's axes of a table, displayed and raw (``None`` when the
    calibration records no raw bytes)."""

    x: list[float]
    y: list[float]
    x_raw: list[float] | None
    y_raw: list[float] | None


def _table_axes(kind: str, revision: str | None, calibration: Any) -> _Axes | dict[str, Any]:
    """The axes of ``kind`` on ``revision``, or the named reason not."""
    if kind != "fuel_targets":
        return {
            "reason": "table_axes_unverified",
            "message": "This table has no verified layout, so it has no axes to bin against.",
            "missing": [],
        }
    if revision is None:
        return {
            "reason": "revision_unknown",
            "message": (
                "No tune revision resolves for this log, so which ROM's axes it ran "
                "against is unknown."
            ),
            "missing": [],
        }
    target = None if calibration is None else calibration.target_map(revision)
    if target is None:
        return {
            "reason": "axes_not_recorded",
            "message": f"No target table is recorded for revision {revision!r}.",
            "missing": [],
        }
    raw_load = getattr(target, "raw_load_breakpoints", None)
    raw_rpm = getattr(target, "raw_rpm_breakpoints", None)
    return _Axes(
        x=list(target.load_breakpoints),
        y=list(target.rpm_breakpoints),
        x_raw=None if raw_load is None else [float(v) for v in raw_load],
        y_raw=None if raw_rpm is None else [float(v) for v in raw_rpm],
    )


def _breakpoints(name: str, axis: AxisTrace, axes: _Axes) -> list[float] | dict[str, Any]:
    """The breakpoints ``axis`` is compared with, or the named reason none."""
    if axis.axis_units == "display":
        return axes.x if name == "x" else axes.y
    raw = axes.x_raw if name == "x" else axes.y_raw
    if raw is None:
        return {
            "reason": "raw_axes_not_recorded",
            "message": (
                f"The {name.upper()} trace ({axis.channel}) maps onto this table's raw axis "
                "bytes, and the calibration records none for this revision."
            ),
            "missing": [],
        }
    return raw


def trace_axes(
    table: TableInfo, revision: str | None, calibration: Any
) -> TraceAxes | dict[str, Any]:
    """The breakpoints each of ``table``'s trace axes is compared with on
    ``revision``, or the named reason there are none. Call after
    :func:`mapping_refusal` has passed."""
    trace = table.trace
    if trace is None or trace.unverified_axes():
        raise ValueError("trace_axes needs a table whose trace axes are both verified")
    axes = _table_axes(table.kind, revision, calibration)
    if isinstance(axes, dict):
        return axes
    x = _breakpoints("x", trace.x, axes)
    if isinstance(x, dict):
        return x
    y = _breakpoints("y", trace.y, axes)
    if isinstance(y, dict):
        return y
    return TraceAxes(x=x, y=y, x_display=axes.x, y_display=axes.y)


def axis_coordinates(
    name: str, axis: AxisTrace, stored: np.ndarray, dids_path: Path
) -> np.ndarray | dict[str, Any]:
    """Stored trace values in the units of the breakpoints ``axis`` is
    compared with, or the named reason they cannot be."""
    if axis.scale is None or axis.offset is None:
        raise ValueError(f"{axis.channel}: axis mapping is unverified")
    stored = np.asarray(stored, dtype=float)
    if axis.axis_units == "display":
        return stored * axis.scale + axis.offset
    decode = stored_decode(axis.channel, dids_path)
    if decode is None:
        return {
            "reason": "stored_decode_unknown",
            "message": (
                f"The {name.upper()} trace ({axis.channel}) maps raw counts onto the axis, but "
                "it is not a memory channel in the DID catalog, so its stored values cannot "
                "be returned to raw counts."
            ),
            "missing": [],
        }
    raw = raw_counts(stored, decode)
    if raw is None:
        return {
            "reason": "stored_not_whole_counts",
            "message": (
                f"{axis.channel} values in this log do not return to whole raw counts under "
                f"the catalog's decode (scale {decode[0]}, offset {decode[1]}): the log was "
                "stored under another decode, so its raw counts cannot be recovered."
            ),
            "missing": [],
        }
    return raw * axis.scale + axis.offset


def cell_residency(
    session: Path,
    *,
    table: TableInfo,
    revision: str | None,
    revision_source: str,
    calibration: Any,
    bindings_path: Path,
    dids_path: Path,
) -> dict[str, Any]:
    """Per-cell sample count and dwell seconds for one log (W05.6).

    Cells are the nearest breakpoint on each axis, the same assignment the
    page draws, compared in the units the profile's mapping names (raw axis
    bytes for a ``raw`` axis). Past an axis end a sample is placed in the edge
    cell when the table declares that the ECM clamps there, and counted under
    ``clamped``; otherwise it is counted under ``outside_axes``. Nothing is
    dropped silently. Every "could not" is a named ``reason`` beside
    ``available: false``, never an empty grid.
    """
    base = {
        "session_id": session.name,
        "table": table.kind,
        "revision": revision,
        "revision_source": revision_source,
    }
    refusal = mapping_refusal(table.trace)
    if refusal is not None:
        return {**base, "available": False, **refusal}
    trace = table.trace
    assert trace is not None
    axes = trace_axes(table, revision, calibration)
    if isinstance(axes, dict):
        return {**base, "available": False, **axes}

    specs = trace.channels()
    try:
        long = read_channels(
            session,
            specs,
            bindings_path=bindings_path,
            dids_path=dids_path,
            acquired_time=True,
        )
    except (ChannelSpecError, KeyError, ValueError):
        long = pd.DataFrame(columns=["timestamp", "spec", "value", "unit"])
    if not long.empty:
        # A ``_SENTINEL``-style flag row is a state, not a coordinate.
        long = long[long["unit"] != "flag"]
    present = set(long["spec"].unique()) if not long.empty else set()
    missing = [spec for spec in specs if spec not in present]
    if missing:
        return {
            **base,
            "available": False,
            "reason": "not_captured",
            "message": (
                "This log holds no " + ", ".join(missing) + "; its capture profile "
                "did not poll the trace channels, so there are no ECM coordinates to bin."
            ),
            "missing": missing,
        }

    # Samples are pairs of stored rows, counted and timed as dwell: a
    # change-only broadcast channel (P02.10) stores one row per payload
    # change, so its rows would bin how often it changed, not where it was.
    change_only = as_of_refusal(long, reader="ROM trace cell binning", specs=specs)
    if change_only is not None:
        return {
            **base,
            "available": False,
            "reason": "change_only_storage",
            "message": change_only,
        }

    xs = long[long["spec"] == trace.x.channel].reset_index(drop=True)
    ys = long[long["spec"] == trace.y.channel].reset_index(drop=True)
    policy = PairingPolicy(max_separation=PAIR_MAX_SEPARATION)
    x_times = to_seconds(xs["timestamp"])
    y_times = to_seconds(ys["timestamp"])
    matched = match_ordered(x_times, y_times, policy)

    x_stored = pd.to_numeric(xs["value"]).to_numpy(dtype=float)[matched.index_a]
    y_stored = pd.to_numeric(ys["value"]).to_numpy(dtype=float)[matched.index_b]
    x_values = axis_coordinates("x", trace.x, x_stored, dids_path)
    if isinstance(x_values, dict):
        return {**base, "available": False, **x_values}
    y_values = axis_coordinates("y", trace.y, y_stored, dids_path)
    if isinstance(y_values, dict):
        return {**base, "available": False, **y_values}

    x_display, y_display = axes.x_display, axes.y_display
    counts = [[0 for _ in x_display] for _ in y_display]
    dwell = [[0.0 for _ in x_display] for _ in y_display]
    outside = 0
    clamped = {"x_below": 0, "x_above": 0, "y_below": 0, "y_above": 0}
    clamped_samples = 0
    pair_times = np.maximum(x_times[matched.index_a], y_times[matched.index_b])
    order = np.argsort(pair_times, kind="stable")
    times = pair_times[order]
    gaps = np.append(np.diff(times), 0.0) if len(times) else np.array([])
    gaps = np.clip(gaps, 0.0, MAX_DWELL_S)
    for position, pair in enumerate(order):
        cell = ecm_cell(axes, float(x_values[pair]), float(y_values[pair]), clamp=trace.clamps)
        if cell is None:
            outside += 1
            continue
        if cell.x_edge is not None:
            clamped[f"x_{cell.x_edge}"] += 1
        if cell.y_edge is not None:
            clamped[f"y_{cell.y_edge}"] += 1
        if cell.x_edge is not None or cell.y_edge is not None:
            clamped_samples += 1
        counts[cell.row][cell.column] += 1
        dwell[cell.row][cell.column] += float(gaps[position])

    return {
        **base,
        "available": True,
        "reason": None,
        "x_channel": trace.x.channel,
        "y_channel": trace.y.channel,
        "x_axis": x_display,
        "y_axis": y_display,
        "axis_units": {"x": trace.x.axis_units, "y": trace.y.axis_units},
        "counts": counts,
        "dwell_s": [[round(v, 3) for v in row] for row in dwell],
        "samples": matched.n_pairs,
        "outside_axes": outside,
        "edges": "clamp" if trace.clamps else "outside",
        "clamped_samples": clamped_samples,
        "clamped": clamped,
        "unpaired": {"x": matched.unmatched_a, "y": matched.unmatched_b},
        "assignment": "nearest_breakpoint",
        "pairing": {**policy.as_dict(), "max_dwell_s": MAX_DWELL_S},
    }


__all__ = [
    "MAX_DWELL_S",
    "PAIR_MAX_SEPARATION",
    "WHOLE_COUNT_TOLERANCE",
    "AxisEdge",
    "AxisTrace",
    "AxisUnits",
    "EcmCell",
    "EdgeBehaviour",
    "TableInfo",
    "TableTrace",
    "TraceAxes",
    "ViewerDefinition",
    "axis_coordinates",
    "axis_position",
    "cell_residency",
    "ecm_cell",
    "load_viewer_definition",
    "mapping_refusal",
    "nearest_cell_index",
    "nearest_index",
    "raw_counts",
    "request_channel",
    "stored_decode",
    "trace_axes",
    "trace_payload",
]
