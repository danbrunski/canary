"""P02.15: the target map clamps to its edge where the ECM does, and has no
value while the ECM is in decel fuel cut.

UpRev logs the ECM's own TARGET AFR beside B-FUEL SCHDL (ms) and rpm. On the
2026-08-04 logs (ZS44A-k106) a base fuel schedule below the first breakpoint
(8.76 ms) reads the first column, and an rpm above the last breakpoint
(5,200) reads the last row. Both are declared, evidenced properties of the map
in ``afr_calibration.yaml`` (``load_axis_out_of_range`` /
``rpm_axis_out_of_range``), not shared code. These tests reproduce the
declarations' numbers from a committed slice of those logs, show the
alternatives fail on the same rows, and pin what stays NaN: rpm below 1,200,
the undeclared BFS upper edge, any map without the declarations, and every
row in fuel cut.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from src.config import VehicleParameters
from src.decode.dids import load_repo_did_map
from src.paths import DATA_DIR
from src.preprocessing.channel_units import channel_units_for
from src.signals.afr import (
    COMMANDED_EQUIV_RATIO_COLUMN,
    TARGET_AFR,
    AxisEdgeClamp,
    TargetAfrMap,
    append_afr_channels,
    fuel_cut_rows,
    load_afr_calibration,
    target_afr,
)
from src.vehicle.tune_revisions import load_tune_revisions

FIXTURE = Path(__file__).parent / "fixtures/load_axis_clamp/uprev_2026-08-04_target_afr.csv"
XTERRA = DATA_DIR / "nissan-xterra-2008"
CALIBRATION = load_afr_calibration(XTERRA / "afr_calibration.yaml")
#: The real declared axis channel, through the real catalog.
REPO_UNITS = channel_units_for(load_repo_did_map())
BFS = "derived:BASE_FUEL_SCHEDULE"
#: UpRev's decel fuel-cut display: no target is commanded.
FUEL_CUT_DISPLAY = 30.0


def _k106() -> TargetAfrMap:
    target = CALIBRATION.target_map("ZS44A-k106")
    assert target is not None
    return target


def _edge(target: TargetAfrMap, axis: str, edge: str) -> AxisEdgeClamp:
    edges = getattr(target, f"{axis}_axis_out_of_range")
    assert edges is not None
    record = getattr(edges, edge)
    assert isinstance(record, AxisEdgeClamp)
    return record


@pytest.fixture(scope="module")
def logged() -> pd.DataFrame:
    """The committed UpRev slice, with CANary's lookup beside UpRev's target."""
    rows = pd.read_csv(FIXTURE, comment="#")
    frame = pd.DataFrame({"obd:RPM": rows["rpm"], BFS: rows["bfs_ms"]})
    rows["lookup"] = target_afr(frame, _k106(), channel_units=REPO_UNITS)
    rows["error"] = (rows["uprev_target_afr"] - rows["lookup"]).abs()
    return rows


def _below_first_band(logged: pd.DataFrame) -> pd.DataFrame:
    """The rows the load declaration's evidence counts: past the lower edge,
    in its rpm band, and not fuel cut."""
    clamp = _edge(_k106(), "load", "below_first")
    return logged[
        (logged["bfs_ms"] < _k106().load_breakpoints[0])
        & logged["rpm"].between(clamp.rpm_min, clamp.rpm_max)
        & (logged["uprev_target_afr"] != FUEL_CUT_DISPLAY)
    ]


def _above_last_rpm(logged: pd.DataFrame) -> pd.DataFrame:
    return logged[
        (logged["rpm"] > _k106().rpm_breakpoints[-1])
        & (logged["uprev_target_afr"] != FUEL_CUT_DISPLAY)
    ]


def test_the_clamped_target_is_the_ecms_on_the_logged_rows(logged: pd.DataFrame) -> None:
    """Known answer, BFS below the axis: the clamped lookup equals UpRev's
    TARGET AFR, and the declaration's own numbers are what this slice
    reproduces."""
    clamp = _edge(_k106(), "load", "below_first")
    band = _below_first_band(logged)
    assert band["lookup"].notna().all()
    assert len(band) == clamp.rows == 46
    within = int((band["error"] <= clamp.tolerance_afr).sum())
    assert within == clamp.rows_within_tolerance == 32
    assert band["error"].max() == pytest.approx(clamp.max_error_afr, abs=5e-4)
    assert band["error"].median() == pytest.approx(clamp.median_error_afr, abs=5e-4)
    assert int((band["error"] <= 0.10).sum()) == 44
    # P02.14's figure, strictly inside the band: 30 of 44 within 0.05.
    strict = band[(band["rpm"] > clamp.rpm_min) & (band["rpm"] < clamp.rpm_max)]
    assert (len(strict), int((strict["error"] <= 0.05).sum())) == (44, 30)


def test_the_alternatives_fail_on_the_same_rows(logged: pd.DataFrame) -> None:
    """The relation can fail: extrapolating from the first two columns, or
    reading the second column, misses nearly every row the clamp matches."""
    target = _k106()
    band = _below_first_band(logged)
    rpm_axis = np.asarray(target.rpm_breakpoints)
    grid = np.asarray(target.afr)
    first = np.interp(band["rpm"], rpm_axis, grid[:, 0])
    second = np.interp(band["rpm"], rpm_axis, grid[:, 1])
    loads = target.load_breakpoints
    extrapolated = first + (second - first) * (band["bfs_ms"] - loads[0]) / (loads[1] - loads[0])
    observed = band["uprev_target_afr"].to_numpy()
    assert int((np.abs(observed - extrapolated) <= 0.05).sum()) == 2
    assert int((np.abs(observed - second) <= 0.05).sum()) == 0
    np.testing.assert_allclose(band["lookup"], first, rtol=1e-12)


def test_above_5200_rpm_the_target_is_the_last_rows_on_the_logged_rows(
    logged: pd.DataFrame,
) -> None:
    """Known answer, rpm above the axis: the last rpm row at the row's BFS
    matches UpRev on the declaration's 62 of 105 rows, and extrapolating
    along rpm matches none. 40 of the 43 misses read 11.83, the last load
    column at 5,200 rpm: pedal enrichment picks that column, not the row."""
    target = _k106()
    clamp = _edge(target, "rpm", "above_last")
    band = _above_last_rpm(logged)
    assert len(band) == clamp.rows == 105
    assert (band["rpm"].min(), band["rpm"].max()) == (clamp.rpm_min, clamp.rpm_max)
    assert band["lookup"].notna().all()
    within = band["error"] <= clamp.tolerance_afr
    assert int(within.sum()) == clamp.rows_within_tolerance == 62
    assert band["error"].max() == pytest.approx(clamp.max_error_afr, abs=5e-4)
    assert band["error"].median() == pytest.approx(clamp.median_error_afr, abs=5e-4)
    misses = band.loc[~within, "uprev_target_afr"]
    assert int(np.isclose(misses, 11.83, atol=0.005).sum()) == 40

    grid = np.asarray(target.afr)
    loads = np.asarray(target.load_breakpoints)
    load = np.clip(band["bfs_ms"].to_numpy(), loads[0], loads[-1])
    last = np.interp(load, loads, grid[-1])
    previous = np.interp(load, loads, grid[-2])
    rpms = target.rpm_breakpoints
    slope = (last - previous) / (rpms[-1] - rpms[-2])
    extrapolated = last + slope * (band["rpm"].to_numpy() - rpms[-1])
    observed = band["uprev_target_afr"].to_numpy()
    assert int((np.abs(observed - extrapolated) <= 0.05).sum()) == 0
    np.testing.assert_allclose(band["lookup"], last, rtol=1e-12)


def test_below_4400_rpm_every_non_cut_row_is_the_first_columns_14_7(
    logged: pd.DataFrame,
) -> None:
    target = _k106()
    low = logged[
        (logged["bfs_ms"] < target.load_breakpoints[0])
        & (logged["rpm"] >= target.rpm_breakpoints[0])
        & (logged["rpm"] < 4400)
        & (logged["uprev_target_afr"] != FUEL_CUT_DISPLAY)
    ]
    assert len(low) == 404
    assert (low["uprev_target_afr"] == 14.7).all()
    assert low["lookup"].to_numpy() == pytest.approx(14.7)


def test_rows_below_1200_rpm_stay_nan(logged: pd.DataFrame) -> None:
    """Every cell below 1,200 rpm would be 14.7, so the logs cannot say
    whether the ECM clamps there. There is no declaration and the rows stay
    NaN, on the load axis or below it."""
    target = _k106()
    assert not target.clamps_below_first_rpm
    idle = logged[logged["rpm"] < target.rpm_breakpoints[0]]
    assert len(idle) == 74
    assert int((idle["bfs_ms"] < target.load_breakpoints[0]).sum()) == 68
    assert idle["lookup"].isna().all()


def test_the_undeclared_bfs_upper_edge_stays_nan() -> None:
    """No logged row reaches 21.57 ms, so that edge has no record."""
    target = _k106()
    assert target.clamps_below_first_load and not target.clamps_above_last_load
    values = target_afr(
        pd.DataFrame({"obd:RPM": [3000.0, 3000.0, 6000.0], BFS: [21.57, 21.58, 30.0]}),
        target,
        channel_units=REPO_UNITS,
    )
    assert np.isfinite(values.iloc[0]) and values.iloc[1:].isna().all()


def test_a_map_without_the_declarations_keeps_nan_off_both_axes() -> None:
    bare = _k106().model_copy(
        update={"load_axis_out_of_range": None, "rpm_axis_out_of_range": None}
    )
    frame = pd.DataFrame({"obd:RPM": [4800.0, 4800.0, 5600.0], BFS: [5.0, 10.0, 12.0]})
    values = target_afr(frame, bare, channel_units=REPO_UNITS)
    assert pd.isna(values.iloc[0]) and np.isfinite(values.iloc[1]) and pd.isna(values.iloc[2])
    clamped = target_afr(frame, _k106(), channel_units=REPO_UNITS)
    assert clamped.iloc[0] == pytest.approx((14.7 + 12.8) / 2)
    assert clamped.iloc[2] == pytest.approx(
        target_afr(
            pd.DataFrame({"obd:RPM": [5200.0], BFS: [12.0]}), bare, channel_units=REPO_UNITS
        ).iloc[0]
    )


def test_the_k106_era_maps_declare_two_edges_and_the_base_map_none() -> None:
    """The edges are ECM program behaviour logged on k106; the 2026-09-20
    flash changed one calibration cell and inherits them through the YAML
    anchor. The UpRev base map (the first two flashes) declares none: no log
    from that era was matched, so it stays NaN off the axes."""
    revisions = [m.revision for m in CALIBRATION.target_maps]
    assert revisions == ["ZS44A-k106", "ZS44A-20260920", "ZS44A-osiris-initial", "ZS44A-k105"]
    for target in CALIBRATION.target_maps[:2]:
        assert target.clamps_below_first_load and target.clamps_above_last_rpm
        assert not target.clamps_above_last_load and not target.clamps_below_first_rpm
    for target in CALIBRATION.target_maps[2:]:
        assert target.load_axis_out_of_range is None and target.rpm_axis_out_of_range is None


def test_fuel_cut_rows_have_no_target(logged: pd.DataFrame) -> None:
    """A sentinel is a state, not a number. UpRev shows the ECM's target as
    30.00 in decel fuel cut, the state PID 0x44 reports as a commanded ratio
    of exactly 0. 374 fixture rows are fuel cut, all of them below the BFS
    axis and inside 1,200-5,200 rpm, where the clamp would otherwise give
    them 14.7 or the first column. They get no target, through the
    production path."""
    cut = logged["uprev_target_afr"] == FUEL_CUT_DISPLAY
    assert int(cut.sum()) == 374
    assert int((cut & (logged["bfs_ms"] < 8.76)).sum()) == 374
    registry = load_tune_revisions(XTERRA / "tune_revisions.yaml")
    assert registry is not None
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(logged["timestamp"], utc=True),
            "obd:RPM": logged["rpm"],
            BFS: logged["bfs_ms"],
            COMMANDED_EQUIV_RATIO_COLUMN: np.where(cut, 0.0, 1.0),
        }
    )
    assert (fuel_cut_rows(frame).to_numpy() == cut.to_numpy()).all()
    out = append_afr_channels(
        frame,
        VehicleParameters(),
        CALIBRATION,
        channel_units=REPO_UNITS,
        flash_history=registry,
    )
    assert out.loc[cut.to_numpy(), TARGET_AFR].isna().all()
    without_ratio = append_afr_channels(
        frame.drop(columns=[COMMANDED_EQUIV_RATIO_COLUMN]),
        VehicleParameters(),
        CALIBRATION,
        channel_units=REPO_UNITS,
        flash_history=registry,
    )
    # The same rows with no commanded ratio: the clamp gives most of them a
    # value, which is what fuel cut withholds. Non-cut rows are unchanged.
    assert int(without_ratio.loc[cut.to_numpy(), TARGET_AFR].notna().sum()) == 374
    pd.testing.assert_series_equal(
        out.loc[~cut.to_numpy(), TARGET_AFR], without_ratio.loc[~cut.to_numpy(), TARGET_AFR]
    )


def test_fuel_cut_is_an_exact_zero_ratio_and_unknown_is_not_cut() -> None:
    frame = pd.DataFrame({COMMANDED_EQUIV_RATIO_COLUMN: [0.0, 1e-6, 1.02, 0.98, np.nan]})
    assert fuel_cut_rows(frame).tolist() == [True, False, False, False, False]
    assert not fuel_cut_rows(pd.DataFrame({"obd:RPM": [900.0]})).any()


def test_a_clamp_cannot_be_declared_without_its_evidence() -> None:
    with pytest.raises(ValidationError):
        AxisEdgeClamp.model_validate({"behaviour": "clamp"})
    record = _edge(_k106(), "load", "below_first")
    with pytest.raises(ValidationError, match="cannot exceed rows"):
        AxisEdgeClamp.model_validate(
            {**record.model_dump(), "rows_within_tolerance": record.rows + 1}
        )
    with pytest.raises(ValidationError):
        TargetAfrMap.model_validate(
            {
                **_k106().model_dump(exclude={"rpm_axis_out_of_range"}),
                "rpm_axis_out_of_range": {"above_last": "clamp"},
            }
        )
