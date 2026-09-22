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
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.channels import ChannelSpecError, read_channels
from src.analysis.pairing import PairingPolicy, match_ordered, to_seconds
from src.decode.dids import MEMORY_SOURCE, load_did_map

TableKind = Literal["fuel_targets", "fuel_compensation"]

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
    #: ``axis = stored value x scale + offset``, in the axis's displayed units.
    #: Required when verified; forbidden when not, so no scale is ever guessed.
    scale: float | None = None
    offset: float | None = None
    #: How the mapping was settled, or what would settle it.
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _mapping_state(self) -> AxisTrace:
        if self.axis_mapping == "verified":
            if self.scale is None or self.offset is None or self.scale == 0:
                raise ValueError("a verified axis mapping needs a nonzero scale and an offset")
        elif self.scale is not None or self.offset is not None:
            raise ValueError(
                "an unverified axis mapping carries no scale or offset; record the "
                "evidence that would settle it instead"
            )
        return self

    def to_axis(self, value: float) -> float:
        """The trace value in axis units. Refuses an unverified mapping."""
        if self.axis_mapping != "verified" or self.scale is None or self.offset is None:
            raise ValueError(f"{self.channel}: axis mapping is unverified")
        return value * self.scale + self.offset


class TableTrace(_Strict):
    """The ECM's coordinates into one table (W05.1)."""

    x: AxisTrace
    y: AxisTrace
    #: Shown when no capture profile polls the channels: why, and when they
    #: are expected back. Vehicle facts, so data rather than page copy.
    unavailable_note: str | None = None

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


def trace_payload(trace: TableTrace | None, dids_path: Path) -> dict[str, Any] | None:
    """``trace`` as the inspect response carries it, with request channels."""
    if trace is None:
        return None
    payload = trace.model_dump()
    for axis in ("x", "y"):
        payload[axis]["request_channel"] = request_channel(payload[axis]["channel"], dids_path)
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


def nearest_index(axis: list[float], value: float) -> int | None:
    """Nearest breakpoint, or ``None`` outside the axis. Ties go low, as the
    page's inferred lookup does."""
    if not axis or not np.isfinite(value) or value < axis[0] or value > axis[-1]:
        return None
    distances = [abs(point - value) for point in axis]
    return int(min(range(len(axis)), key=lambda i: (distances[i], i)))


def _table_axes(
    kind: str, revision: str | None, calibration: Any
) -> tuple[list[float], list[float]] | dict[str, Any]:
    """(x_axis, y_axis) of ``kind`` on ``revision``, or the named reason not."""
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
    return list(target.load_breakpoints), list(target.rpm_breakpoints)


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
    page draws. Samples outside either axis are counted, not dropped silently.
    Every "could not" is a named ``reason`` beside ``available: false``,
    never an empty grid.
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
    axes = _table_axes(table.kind, revision, calibration)
    if isinstance(axes, dict):
        return {**base, "available": False, **axes}
    x_axis, y_axis = axes

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

    xs = long[long["spec"] == trace.x.channel].reset_index(drop=True)
    ys = long[long["spec"] == trace.y.channel].reset_index(drop=True)
    policy = PairingPolicy(max_separation=PAIR_MAX_SEPARATION)
    x_times = to_seconds(xs["timestamp"])
    y_times = to_seconds(ys["timestamp"])
    matched = match_ordered(x_times, y_times, policy)

    counts = [[0 for _ in x_axis] for _ in y_axis]
    dwell = [[0.0 for _ in x_axis] for _ in y_axis]
    outside = 0
    pair_times = np.maximum(x_times[matched.index_a], y_times[matched.index_b])
    order = np.argsort(pair_times, kind="stable")
    times = pair_times[order]
    gaps = np.append(np.diff(times), 0.0) if len(times) else np.array([])
    gaps = np.clip(gaps, 0.0, MAX_DWELL_S)
    for position, pair in enumerate(order):
        x_value = trace.x.to_axis(float(xs["value"].iloc[matched.index_a[pair]]))
        y_value = trace.y.to_axis(float(ys["value"].iloc[matched.index_b[pair]]))
        column = nearest_index(x_axis, x_value)
        row = nearest_index(y_axis, y_value)
        if row is None or column is None:
            outside += 1
            continue
        counts[row][column] += 1
        dwell[row][column] += float(gaps[position])

    return {
        **base,
        "available": True,
        "reason": None,
        "x_channel": trace.x.channel,
        "y_channel": trace.y.channel,
        "x_axis": x_axis,
        "y_axis": y_axis,
        "counts": counts,
        "dwell_s": [[round(v, 3) for v in row] for row in dwell],
        "samples": matched.n_pairs,
        "outside_axes": outside,
        "unpaired": {"x": matched.unmatched_a, "y": matched.unmatched_b},
        "assignment": "nearest_breakpoint",
        "pairing": {**policy.as_dict(), "max_dwell_s": MAX_DWELL_S},
    }


__all__ = [
    "MAX_DWELL_S",
    "PAIR_MAX_SEPARATION",
    "AxisTrace",
    "TableInfo",
    "TableTrace",
    "ViewerDefinition",
    "cell_residency",
    "load_viewer_definition",
    "mapping_refusal",
    "nearest_index",
    "request_channel",
    "trace_payload",
]
