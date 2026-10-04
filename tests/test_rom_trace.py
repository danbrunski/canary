"""W05 — the ECM's own coordinates on a ROM table, and per-cell residency.

The mapping from a trace channel onto a table's breakpoints is a measured
constraint, so it is a required state in ``rom_viewer.yaml``: these tests pin
that an unverified axis cannot carry a scale, that nothing places a cell on
one, and that the residency endpoint answers every "could not" with a named
reason instead of an empty grid.

W05.1 settled the fuel-target X trace from the ECM program
(``docs/ecu-firmware-analysis.md``): its raw byte is compared directly with the
raw BFS axis bytes, interpolated bilinearly and clamped at the edges. The
known-answer tests below pin that on synthetic values through the shipped
mapping and on a committed slice of the 2026-10-04 drive
(``tests/fixtures/rom_trace/_capture-ram-study-2026-10-04T204557``: every
``mem:ECM_FUEL_TARGET_MAP_X_TRACE``, ``mem:ECM_AIR_CHARGE_SLOT_103A`` (the
BFS word at ``0xFFFF103A``, read in the same window as the X byte) and
``mem:ECM_ENGINE_SPEED_RAM`` row of that capture, copied unchanged).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.analysis.channels import read_channels
from src.analysis.rom_trace import (
    MAX_DWELL_S,
    AxisTrace,
    EdgeBehaviour,
    TableInfo,
    TableTrace,
    axis_coordinates,
    axis_position,
    cell_residency,
    ecm_cell,
    load_viewer_definition,
    mapping_refusal,
    nearest_cell_index,
    nearest_index,
    raw_counts,
    request_channel,
    stored_decode,
    trace_axes,
    trace_payload,
)
from src.config import CanaryConfig
from src.control import workbench_jobs
from src.dashboard.routes import workbench as workbench_routes
from src.signals.afr import load_afr_calibration
from src.signals.pairing import PairingPolicy, match_ordered, to_seconds
from src.storage.capture_manifest import CAPTURE_MANIFEST_FILENAME
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

BINDINGS = Path("data/bindings.yaml")
DIDS = Path("data/dids.yaml")
PROFILE = Path("data/nissan-xterra-2008")
T0 = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
X = "mem:ECM_FUEL_TARGET_MAP_X_TRACE"
Y = "mem:ECM_ENGINE_SPEED_RAM"
X_AXIS = [8.0, 12.0, 16.0]
Y_AXIS = [1200.0, 2000.0, 2800.0]
AUTH = {"Authorization": "Bearer secret"}
#: The catalog decode the X trace is stored under (``data/dids.yaml``).
X_STORED_SCALE = 0.250859
#: The shipped Target AFR axes (``afr_calibration.yaml``, every revision).
RAW_BFS_AXIS = [0x34, 0x44, 0x5D, 0x64, 0x6C, 0x6E, 0x7F, 0x80]
RPM_AXIS = [1200, 1600, 2000, 2800, 3600, 4000, 4400, 5200]


def _verified_trace(*, clamp: bool = False) -> TableTrace:
    """A synthetic display-units mapping: stored value = axis value."""
    return TableTrace(
        x=AxisTrace(
            channel=X,
            axis_mapping="verified",
            axis_units="display",
            scale=1.0,
            offset=0.0,
            evidence="synthetic",
        ),
        y=AxisTrace(
            channel=Y,
            axis_mapping="verified",
            axis_units="display",
            scale=1.0,
            offset=0.0,
            evidence="synthetic",
        ),
        out_of_range=EdgeBehaviour(behaviour="clamp", evidence="synthetic") if clamp else None,
    )


def _unverified_trace() -> TableTrace:
    """The state W05.1 left behind for the fuel-target X axis until 2026-10-04,
    kept as a schema state for any table whose axis is not yet settled."""
    trace = _verified_trace()
    return trace.model_copy(
        update={"x": AxisTrace(channel=X, axis_mapping="unverified", evidence="Not settled.")}
    )


def _shipped_trace() -> TableTrace:
    table = load_viewer_definition(PROFILE).table("fuel_targets")
    assert table is not None and table.trace is not None
    return table.trace


def _table(trace: TableTrace | None) -> TableInfo:
    return TableInfo(
        kind="fuel_targets",
        title="Target AFR",
        x_label="Base Fuel Schedule",
        x_unit="ms",
        y_label="Rotations Per Minute",
        y_unit="rpm",
        unit="AFR",
        trace=trace,
    )


CALIBRATION = SimpleNamespace(
    target_map=lambda revision: (
        SimpleNamespace(load_breakpoints=X_AXIS, rpm_breakpoints=Y_AXIS)
        if revision == "REV-A"
        else None
    )
)
#: The shipped axes, displayed and raw, as one revision of the calibration.
SHIPPED_AXES = SimpleNamespace(
    target_map=lambda revision: SimpleNamespace(
        load_breakpoints=[8.76, 11.46, 15.67, 16.85, 18.20, 18.54, 21.40, 21.57],
        rpm_breakpoints=RPM_AXIS,
        raw_load_breakpoints=RAW_BFS_AXIS,
        raw_rpm_breakpoints=[0x18, 0x20, 0x28, 0x38, 0x48, 0x50, 0x58, 0x68],
    )
)


def _write(
    root: Path, name: str, points: list[tuple[float, float]], *, step_s: float = 0.125
) -> Path:
    """A session whose trace channels read ``(x, y)`` at ``step_s`` intervals."""
    records: list[TelemetryRecord] = []
    for i, (x, y) in enumerate(points):
        ts = T0 + timedelta(seconds=i * step_s)
        records.append(TelemetryRecord(timestamp=ts, source="obd", channel=X, value=x))
        records.append(
            TelemetryRecord(
                timestamp=ts + timedelta(milliseconds=40), source="obd", channel=Y, value=y
            )
        )
    out = root / name
    write_records_to_parquet(records, out)
    return out


# --------------------------------------------------------------------------- schema


def test_an_unverified_axis_cannot_carry_a_scale() -> None:
    with pytest.raises(ValidationError, match="carries no scale"):
        AxisTrace(channel=X, axis_mapping="unverified", scale=0.25, offset=0.0, evidence="guess")


def test_a_verified_axis_must_state_its_scale() -> None:
    with pytest.raises(ValidationError, match="nonzero scale"):
        AxisTrace(channel=X, axis_mapping="verified", axis_units="raw", evidence="settled")


def test_a_verified_axis_must_state_which_breakpoints_it_lands_on() -> None:
    """A stored (scaled) value compared with raw axis bytes is the W05.1 trap:
    a verified mapping has to say whether it means displayed values or raw
    bytes, so neither can be assumed."""
    with pytest.raises(ValidationError, match="axis_units"):
        AxisTrace(channel=X, axis_mapping="verified", scale=1.0, offset=0.0, evidence="settled")
    with pytest.raises(ValidationError, match="carries no scale, offset or axis_units"):
        AxisTrace(channel=X, axis_mapping="unverified", axis_units="raw", evidence="guess")


def test_to_axis_refuses_an_unverified_mapping() -> None:
    axis = AxisTrace(channel=X, axis_mapping="unverified", evidence="not settled")
    with pytest.raises(ValueError, match="unverified"):
        axis.to_axis(40.0)


def test_shipped_profile_maps_the_fuel_target_trace_with_both_axes_verified() -> None:
    """W05.1 as shipped since 2026-10-04: the X byte lands on the raw BFS axis
    bytes unchanged, rpm lands on the rpm axis in rpm, and both clamp."""
    trace = _shipped_trace()
    assert trace.x.channel == X and trace.x.axis_mapping == "verified"
    assert (trace.x.axis_units, trace.x.scale, trace.x.offset) == ("raw", 1.0, 0.0)
    assert "docs/ecu-firmware-analysis.md" in trace.x.evidence
    assert "_capture-ram-study-2026-10-04T204557" in trace.x.evidence
    assert trace.y.channel == Y and trace.y.axis_mapping == "verified"
    assert (trace.y.axis_units, trace.y.scale, trace.y.offset) == ("display", 1.0, 0.0)
    assert trace.clamps
    assert mapping_refusal(trace) is None
    # The decode a raw mapping undoes is the catalog's, never a copied constant.
    assert stored_decode(X, DIDS) == (X_STORED_SCALE, 0.0)
    assert stored_decode(Y, DIDS) == (12.5, 0.0)
    assert stored_decode("did:ECM_RPM", DIDS) is None
    payload = trace_payload(trace, DIDS)
    assert payload is not None
    assert payload["x"]["stored_decode"] == {"scale": X_STORED_SCALE, "offset": 0.0}
    assert payload["x"]["axis_units"] == "raw"
    assert payload["out_of_range"]["behaviour"] == "clamp"


def test_an_unverified_axis_still_refuses_by_name() -> None:
    """No shipped table keeps an unverified trace axis after W05.1, but the
    state stays: an axis whose mapping is not settled still places no cell."""
    refusal = mapping_refusal(_unverified_trace())
    assert refusal is not None and refusal["reason"] == "axis_mapping_unverified"
    assert X in refusal["message"] and "Not settled." in refusal["message"]
    assert mapping_refusal(None) is not None
    with pytest.raises(ValueError, match="both verified"):
        trace_axes(_table(_unverified_trace()), "REV-A", CALIBRATION)


def test_shipped_fuel_compensation_table_still_has_no_trace() -> None:
    """Fuel Compensation has no verified layout or x input: no trace mapping,
    so nothing places a cell on it."""
    table = load_viewer_definition(PROFILE).table("fuel_compensation")
    assert table is not None and table.trace is None
    refusal = mapping_refusal(table.trace)
    assert refusal is not None and refusal["reason"] == "no_trace_mapping"


def test_request_channel_names_the_memory_window_that_carries_the_trace() -> None:
    assert request_channel(X, DIDS) == "mem:fuel_axis"
    assert request_channel(Y, DIDS) == "mem:spark"
    assert request_channel("mem:ECM_THROTTLE_MAP_X_TRACE", DIDS) == "mem:ECM_THROTTLE_MAP_X_TRACE"
    assert request_channel("did:ECM_RPM", DIDS) is None


def test_nearest_index_is_none_outside_the_axis() -> None:
    assert nearest_index(X_AXIS, 9.9) == 0
    assert nearest_index(X_AXIS, 10.0) == 0  # tie goes low, like the page
    assert nearest_index(X_AXIS, 15.0) == 2
    assert nearest_index(X_AXIS, 7.9) is None
    assert nearest_index(X_AXIS, 16.1) is None


# --------------------------------------------------------------------------- known answers


def _shipped_axes():
    table = load_viewer_definition(PROFILE).table("fuel_targets")
    assert table is not None
    axes = trace_axes(table, "any", SHIPPED_AXES)
    assert not isinstance(axes, dict)
    return axes


def test_an_x_byte_on_a_breakpoint_lands_exactly_on_that_column() -> None:
    """Known answer from the code: the raw X byte is compared directly with the
    raw BFS axis bytes. Stored as the collector stores it (raw x 0.250859),
    each axis byte comes back as exactly that column, through the shipped
    mapping and the catalog's decode."""
    trace = _shipped_trace()
    axes = _shipped_axes()
    assert axes.x == [float(v) for v in RAW_BFS_AXIS]
    assert axes.y == [float(v) for v in RPM_AXIS]
    stored = np.array([raw * X_STORED_SCALE for raw in RAW_BFS_AXIS])
    counts = axis_coordinates("x", trace.x, stored, DIDS)
    assert isinstance(counts, np.ndarray)
    assert counts.tolist() == [float(v) for v in RAW_BFS_AXIS]
    for column, value in enumerate(counts):
        assert axis_position(axes.x, float(value), clamp=True) == (float(column), None)
        cell = ecm_cell(axes, float(value), 2000.0, clamp=True)
        assert cell is not None and (cell.row, cell.column) == (2, column)


def test_an_x_byte_between_breakpoints_interpolates_proportionally() -> None:
    """Bilinear interpolation (0x026F40): 80 counts is 12/25 of the way from
    0x44 (68) to 0x5D (93); 1,400 rpm is halfway from 1,200 to 1,600."""
    trace = _shipped_trace()
    axes = _shipped_axes()
    counts = axis_coordinates("x", trace.x, np.array([80 * X_STORED_SCALE]), DIDS)
    assert isinstance(counts, np.ndarray) and counts.tolist() == [80.0]
    position, edge = axis_position(axes.x, 80.0, clamp=True)  # type: ignore[misc]
    assert position == pytest.approx(1 + 12 / 25, abs=1e-12) and edge is None
    cell = ecm_cell(axes, 80.0, 1400.0, clamp=True)
    assert cell is not None
    assert cell.x_position == pytest.approx(1.48, abs=1e-12)
    assert cell.y_position == pytest.approx(0.5, abs=1e-12)
    # Nearest cell: 1.48 rounds to column 1; the exact rpm midpoint ties low.
    assert (cell.row, cell.column) == (0, 1)
    assert nearest_cell_index(1.5) == 1 and nearest_cell_index(1.51) == 2


def test_an_x_byte_past_the_axis_reads_the_edge_column() -> None:
    """The lookup clamps past either end (0x026F40), and the profile says so;
    without that declaration a sample past an end places no cell."""
    axes = _shipped_axes()
    low = ecm_cell(axes, 17.0, 750.0, clamp=True)  # idle: BFS ~2.9 ms, below 1,200 rpm
    assert low is not None and (low.row, low.column) == (0, 0)
    assert (low.x_edge, low.y_edge) == ("below", "below")
    high = ecm_cell(axes, 255.0, 6000.0, clamp=True)
    assert high is not None and (high.row, high.column) == (7, 7)
    assert (high.x_edge, high.y_edge) == ("above", "above")
    assert ecm_cell(axes, 17.0, 2000.0, clamp=False) is None


def test_raw_counts_refuse_values_stored_under_another_decode() -> None:
    assert raw_counts(np.array([68 * X_STORED_SCALE]), (X_STORED_SCALE, 0.0)).tolist() == [68.0]  # type: ignore[union-attr]
    assert raw_counts(np.array([68 * 0.25]), (X_STORED_SCALE, 0.0)) is None


RAM_STUDY = Path(__file__).parent / "fixtures/rom_trace/_capture-ram-study-2026-10-04T204557"
#: The BFS word at 0xFFFF103A: what DID 0x1208 reports, read in the X byte's window.
BFS_WORD = "mem:ECM_AIR_CHARGE_SLOT_103A"
#: UpRev's Load Point Scaler (calibration 0x9416): X = floor(BFS x 128 / 44019).
LOAD_POINT_SCALER = 44019
#: DID 0x1208's confirmed decode, ms per count (P02.14).
BFS_MS_PER_COUNT = 0.00049
#: The firmware doc's transient tolerance: misses are within 4 counts.
TRANSIENT_COUNTS = 4


def test_real_drive_ecm_cell_lands_where_bfs_and_rpm_put_it() -> None:
    """Known answer on real data: the 2026-10-04 drive, read through the real
    chain (``read_channels`` -> shipped mapping -> catalog decode undone).

    The ECM cell comes from the X byte on the raw axis and the rpm word; the
    inferred cell from the BFS word of the same window read, in ms on the
    displayed axis, and the same rpm word. Rows agree by construction (rpm is
    its own axis unit); the columns are the W05.1 claim.

    Most of the drive sits below the first load breakpoint, where both sides
    clamp to column 0 and agree whatever the mapping, so the evidence is the
    *on-axis* pairs: those whose BFS lies inside the axis (the reference
    alone decides, so every candidate mapping is scored on the same pairs).
    There every column disagreement must be one column, with the X byte
    within the transient tolerance of the BFS word's count. Then the same
    on-axis check is run with wrong mappings (the stored value compared with
    the raw bytes, and raw counts shifted or scaled), which must fail, so the
    agreement is evidence and not a property of the data.
    """
    trace = _shipped_trace()
    calibration = load_afr_calibration(PROFILE / "afr_calibration.yaml")
    # The drive ran ZS44A-20260920 (flashed 2026-09-21); every revision
    # shares these axes.
    revision = calibration.active_revision
    assert revision == "ZS44A-20260920"
    axes = trace_axes(load_viewer_definition(PROFILE).table("fuel_targets"), revision, calibration)  # type: ignore[arg-type]
    assert not isinstance(axes, dict)

    long = read_channels(
        RAM_STUDY, [X, BFS_WORD, Y], bindings_path=BINDINGS, dids_path=DIDS, acquired_time=True
    )
    xs = long[long["spec"] == X].reset_index(drop=True)
    words = long[long["spec"] == BFS_WORD].reset_index(drop=True)
    rpms = long[long["spec"] == Y].reset_index(drop=True)
    assert (len(xs), len(words), len(rpms)) == (194, 194, 180)
    # One window read: the X byte and the BFS word share each acquisition.
    assert (xs["timestamp"].to_numpy() == words["timestamp"].to_numpy()).all()

    x_counts = axis_coordinates("x", trace.x, xs["value"].to_numpy(dtype=float), DIDS)
    assert isinstance(x_counts, np.ndarray)
    word = words["value"].to_numpy(dtype=float)
    # The firmware doc's formula, as the drive showed it: 168 of 194 exact,
    # the rest within the transient tolerance, both signs.
    formula = np.floor(word * 128 / LOAD_POINT_SCALER)
    assert int((x_counts == formula).sum()) == 168
    assert np.abs(x_counts - formula).max() <= TRANSIENT_COUNTS
    assert (x_counts > formula).any() and (x_counts < formula).any()

    # rpm comes from another window, ~0.6 s away (median); the page pairs an
    # ECM point within 750 ms (ECM_TRACE_MAX_SKEW_MS in lib/romTrace.ts).
    matched = match_ordered(
        to_seconds(xs["timestamp"]),
        to_seconds(rpms["timestamp"]),
        PairingPolicy(max_separation=pd.Timedelta("750ms")),
    )
    assert matched.n_pairs == 141
    # Candidate mappings of the X trace onto the raw axis bytes: the shipped
    # one, and controls that must fail.
    candidates = {
        "raw": lambda ix: float(x_counts[ix]),
        "stored_scaled": lambda ix: float(xs["value"].iloc[ix]),
        "raw_plus_4": lambda ix: float(x_counts[ix]) + 4,
        "raw_times_1.1": lambda ix: float(x_counts[ix]) * 1.1,
        "raw_minus_8": lambda ix: float(x_counts[ix]) - 8,
    }
    on_axis_agree = dict.fromkeys(candidates, 0)
    agree, on_axis, both_clamped, columns = 0, 0, 0, set()
    for ix, iy in zip(matched.index_a, matched.index_b, strict=True):
        rpm = float(rpms["value"].iloc[iy])
        ecm = ecm_cell(axes, float(x_counts[ix]), rpm, clamp=True)
        bfs_column = axis_position(axes.x_display, word[ix] * BFS_MS_PER_COUNT, clamp=True)
        rpm_row = axis_position(axes.y_display, rpm, clamp=True)
        assert ecm is not None and bfs_column is not None and rpm_row is not None
        inferred = (nearest_cell_index(rpm_row[0]), nearest_cell_index(bfs_column[0]))
        assert ecm.row == inferred[0]
        agree += (ecm.row, ecm.column) == inferred
        if bfs_column[1] is not None:
            # Off the axis: both sides clamp, and agree whatever the mapping.
            both_clamped += ecm.x_edge == bfs_column[1]
            continue
        on_axis += 1
        columns.add(ecm.column)
        if ecm.column != inferred[1]:
            assert abs(ecm.column - inferred[1]) == 1
            bfs_count = word[ix] * 128 / LOAD_POINT_SCALER
            assert abs(x_counts[ix] - bfs_count) <= TRANSIENT_COUNTS
        for name, mapped in candidates.items():
            cell = ecm_cell(axes, mapped(ix), rpm, clamp=True)
            assert cell is not None
            on_axis_agree[name] += cell.column == inferred[1]
    # 103 of the 141 pairs sit below 8.76 ms, where both sides clamp to
    # column 0: the all-pairs figure (139/141) includes them.
    assert (agree, both_clamped) == (139, 103)
    # On the axis: four load columns reached, 36 of 38 in the same cell.
    assert on_axis == 38 and columns == {0, 1, 2, 3}
    assert on_axis_agree == {
        "raw": 36,
        # Compared scaled against the raw bytes, every stored value is below
        # 0x34 (52), so it clamps to column 0.
        "stored_scaled": 6,
        "raw_plus_4": 27,
        "raw_times_1.1": 19,
        "raw_minus_8": 18,
    }


def test_real_drive_residency_through_the_shipped_mapping() -> None:
    """The residency path on the same slice: the refusal is gone, the raw
    decode comes back to whole counts on real stored values, and the clamp is
    counted. Only one X read has an rpm word within PAIR_MAX_SEPARATION: the
    ram-study profile read the two windows every 5 s, a median 0.6 s apart."""
    calibration = load_afr_calibration(PROFILE / "afr_calibration.yaml")
    table = load_viewer_definition(PROFILE).table("fuel_targets")
    assert table is not None
    result = cell_residency(
        RAM_STUDY,
        table=table,
        revision=calibration.active_revision,
        revision_source="test",
        calibration=calibration,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert result["available"] is True, result
    assert result["axis_units"] == {"x": "raw", "y": "display"}
    assert result["edges"] == "clamp"
    assert result["x_axis"] == [8.76, 11.46, 15.67, 16.85, 18.20, 18.54, 21.40, 21.57]
    assert result["samples"] == 1 and result["unpaired"] == {"x": 193, "y": 179}
    assert result["counts"][0][0] == 1 and result["clamped_samples"] == 1
    assert result["outside_axes"] == 0


# --------------------------------------------------------------------------- residency


def _residency(session: Path, trace: TableTrace | None, revision: str | None = "REV-A"):
    return cell_residency(
        session,
        table=_table(trace),
        revision=revision,
        revision_source="capture_manifest",
        calibration=CALIBRATION,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )


def test_residency_counts_samples_and_dwell_per_cell(tmp_path: Path) -> None:
    points = [(8.2, 1210.0)] * 4 + [(15.5, 2750.0)] * 3 + [(30.0, 2000.0)]
    result = _residency(_write(tmp_path, "2026-09-21T120000", points), _verified_trace())
    assert result["available"] is True
    assert result["counts"] == [[4, 0, 0], [0, 0, 0], [0, 0, 3]]
    assert result["samples"] == 8
    assert result["outside_axes"] == 1  # counted, never dropped silently
    assert result["dwell_s"][0][0] == pytest.approx(0.5)
    assert result["dwell_s"][2][2] == pytest.approx(0.375)
    assert result["assignment"] == "nearest_breakpoint"
    assert result["x_axis"] == X_AXIS and result["y_axis"] == Y_AXIS


def test_residency_dwell_is_capped_across_a_polling_gap(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-21T120000", [(8.0, 1200.0), (12.0, 2000.0)], step_s=30.0)
    result = _residency(session, _verified_trace())
    assert result["dwell_s"][0][0] == pytest.approx(MAX_DWELL_S)


def test_residency_names_a_log_without_trace_channels(tmp_path: Path) -> None:
    records = [TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=900.0)]
    write_records_to_parquet(records, tmp_path / "2026-09-21T120000")
    result = _residency(tmp_path / "2026-09-21T120000", _verified_trace())
    assert result["available"] is False
    assert result["reason"] == "not_captured"
    assert result["missing"] == [X, Y]
    assert "counts" not in result


def test_residency_refuses_an_unverified_mapping_before_reading(tmp_path: Path) -> None:
    result = _residency(tmp_path / "does-not-exist", _unverified_trace())
    assert (result["available"], result["reason"]) == (False, "axis_mapping_unverified")


def test_residency_clamps_past_the_axes_when_the_table_declares_it(tmp_path: Path) -> None:
    points = [(8.2, 1210.0), (2.0, 700.0), (30.0, 2000.0), (15.5, 9000.0)]
    session = _write(tmp_path, "2026-09-21T120000", points)
    result = _residency(session, _verified_trace(clamp=True))
    assert result["available"] is True and result["edges"] == "clamp"
    assert result["counts"] == [[2, 0, 0], [0, 0, 1], [0, 0, 1]]
    assert result["outside_axes"] == 0 and result["clamped_samples"] == 3
    assert result["clamped"] == {"x_below": 1, "x_above": 1, "y_below": 1, "y_above": 1}


def test_residency_bins_raw_counts_on_the_raw_axis(tmp_path: Path) -> None:
    """The shipped mapping on stored values: the decode is undone, and the
    counts land on the raw BFS bytes (0x44 = 68 is column 1, 100 = 0x64 is
    column 3), never the stored value on the raw axis."""
    points = [(68 * X_STORED_SCALE, 2000.0)] * 3 + [(100 * X_STORED_SCALE, 2800.0)] * 2
    session = _write(tmp_path, "2026-09-21T120000", points)
    table = load_viewer_definition(PROFILE).table("fuel_targets")
    assert table is not None
    result = cell_residency(
        session,
        table=table,
        revision="any",
        revision_source="test",
        calibration=SHIPPED_AXES,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert result["available"] is True, result
    assert result["counts"][2][1] == 3 and result["counts"][3][3] == 2
    assert result["samples"] == 5 and result["clamped_samples"] == 0


def test_residency_names_values_stored_under_another_decode(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-21T120000", [(68 * 0.25, 2000.0)])
    table = load_viewer_definition(PROFILE).table("fuel_targets")
    assert table is not None
    result = cell_residency(
        session,
        table=table,
        revision="any",
        revision_source="test",
        calibration=SHIPPED_AXES,
        bindings_path=BINDINGS,
        dids_path=DIDS,
    )
    assert (result["available"], result["reason"]) == (False, "stored_not_whole_counts")
    assert X in result["message"]


def test_residency_names_a_revision_without_raw_axes(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-21T120000", [(68 * X_STORED_SCALE, 2000.0)])
    table = load_viewer_definition(PROFILE).table("fuel_targets")
    assert table is not None
    result = _residency(session, table.trace)
    assert (result["available"], result["reason"]) == (False, "raw_axes_not_recorded")


def test_residency_names_an_unknown_revision(tmp_path: Path) -> None:
    session = _write(tmp_path, "2026-09-21T120000", [(8.0, 1200.0)])
    result = _residency(session, _verified_trace(), revision=None)
    assert (result["available"], result["reason"]) == (False, "revision_unknown")
    result = _residency(session, _verified_trace(), revision="REV-B")
    assert result["reason"] == "axes_not_recorded"


# --------------------------------------------------------------------------- route


def _profile(tmp_path: Path, *, verified: bool) -> Path:
    profile = tmp_path / "profile"
    profile.mkdir(exist_ok=True)
    viewer = yaml.safe_load((PROFILE / "rom_viewer.yaml").read_text())
    trace = _verified_trace() if verified else _unverified_trace()
    viewer["tables"][0]["trace"] = trace.model_dump(exclude_none=True)
    (profile / "rom_viewer.yaml").write_text(yaml.safe_dump(viewer))
    afr = [[14.7] * len(X_AXIS) for _ in Y_AXIS]
    (profile / "afr_calibration.yaml").write_text(
        yaml.safe_dump(
            {
                "commanded_equiv_ratio_convention": "phi",
                "target_maps": [
                    {
                        "revision": "REV-A",
                        "rpm_breakpoints": Y_AXIS,
                        "load_breakpoints": X_AXIS,
                        "afr": afr,
                    }
                ],
            }
        )
    )
    return profile


def _config(tmp_path: Path, *, verified: bool) -> CanaryConfig:
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    profile = _profile(tmp_path, verified=verified)
    storage = tmp_path / "storage"
    storage.mkdir(exist_ok=True)
    return config.model_copy(
        update={
            "collection": config.collection.model_copy(update={"storage_path": storage}),
            "daemon": config.daemon.model_copy(
                update={
                    "afr_calibration": profile / "afr_calibration.yaml",
                    "jobs_dir": tmp_path / "control" / "jobs",
                }
            ),
        }
    )


def _client(config: CanaryConfig, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    workbench_routes.register_workbench_routes(
        app,
        config=config,
        bindings_path=BINDINGS,
        dids_path=DIDS,
        resolved_control_token="secret",
    )
    monkeypatch.setattr(workbench_jobs, "_spawn_pool", lambda: ThreadPoolExecutor(max_workers=1))
    monkeypatch.setattr(workbench_jobs, "limit_address_space", lambda nbytes: (nbytes, nbytes))
    return TestClient(app)


def _capture_session(storage: Path, points: list[tuple[float, float]]) -> str:
    name = "_capture-rom-trace-2026-09-21T120000"
    session = _write(storage, name, points)
    manifest: dict[str, Any] = {
        "profile": "rom-trace",
        "started_at": T0.isoformat(),
        "profile_snapshot": {},
        "tune_revision": "REV-A",
    }
    (session / CAPTURE_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    return name


def test_cell_residency_route_runs_through_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, verified=True)
    name = _capture_session(config.collection.storage_path, [(12.0, 2000.0)] * 5)
    with _client(config, monkeypatch) as client:
        assert (
            client.get(f"/control/sessions/{name}/cell-residency?table=fuel_targets").status_code
            == 401
        )
        response = client.get(
            f"/control/sessions/{name}/cell-residency?table=fuel_targets", headers=AUTH
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["revision"] == "REV-A" and body["revision_source"] == "capture_manifest"
    assert body["counts"][1][1] == 5


def test_cell_residency_route_refuses_unverified_mapping_without_a_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, verified=False)
    name = _capture_session(config.collection.storage_path, [(12.0, 2000.0)])

    async def no_child(*args: object, **kwargs: object) -> dict[str, Any]:
        raise AssertionError("an unverified mapping must not spawn a whole-session read")

    monkeypatch.setattr(workbench_routes, "run_off_loop", no_child)
    with _client(config, monkeypatch) as client:
        response = client.get(
            f"/control/sessions/{name}/cell-residency?table=fuel_targets", headers=AUTH
        )
        missing = client.get(
            f"/control/sessions/{name}/cell-residency?table=ignition", headers=AUTH
        )
    assert response.status_code == 200
    assert response.json()["reason"] == "axis_mapping_unverified"
    assert missing.status_code == 404


def test_cell_residency_route_defers_while_a_capture_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, verified=True)
    name = _capture_session(config.collection.storage_path, [(12.0, 2000.0)])
    monkeypatch.setattr(workbench_jobs.tuning_profile, "is_active", lambda: True)
    monkeypatch.setattr(
        workbench_jobs.tuning_profile,
        "active_profile",
        lambda: type("P", (), {"name": "tuning"})(),
    )
    with _client(config, monkeypatch) as client:
        response = client.get(
            f"/control/sessions/{name}/cell-residency?table=fuel_targets", headers=AUTH
        )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "analysis_deferred"


def test_cell_residency_route_names_a_log_without_trace_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, verified=True)
    storage = config.collection.storage_path
    name = "_capture-tuning-2026-09-21T120000"
    records = [TelemetryRecord(timestamp=T0, source="obd", channel="RPM", value=900.0)]
    write_records_to_parquet(records, storage / name)
    manifest = {"profile": "tuning", "started_at": T0.isoformat(), "profile_snapshot": {}}
    manifest["tune_revision"] = "REV-A"
    (storage / name / CAPTURE_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    with _client(config, monkeypatch) as client:
        response = client.get(
            f"/control/sessions/{name}/cell-residency?table=fuel_targets", headers=AUTH
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["available"], body["reason"]) == (False, "not_captured")
    assert body["missing"] == [X, Y]
    assert "counts" not in body
