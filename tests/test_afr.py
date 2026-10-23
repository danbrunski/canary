from __future__ import annotations

import inspect
import types
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import VehicleParameters
from src.decode.dids import load_repo_did_map
from src.preprocessing.channel_units import catalog_channel_unit, channel_units_for
from src.signals import vehicle_signals
from src.signals.afr import (
    AFR_ERROR_CHANNELS,
    BARO_MISSING,
    BARO_STALE,
    LOAD_AXIS_UNVERIFIED,
    TARGET_AFR,
    AfrCalibration,
    BaroSamples,
    LoadAxisUnavailableError,
    TargetAfrMap,
    VoltageAfrTransfer,
    afr_error_surface,
    append_afr_channels,
    load_afr_calibration,
    target_afr,
    voltage_to_afr,
)
from src.signals.vehicle_signals import LoadAxisChannel
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry
from src.yaml_load import load_yaml
from tests.afr_voltage_chain import baro_columns, transfer, voltage_chain

#: A synthetic vehicle's verified channel for the target map's load axis. The
#: real vehicle modules declare none (P02.13), so every test that needs the
#: lookup to run declares this one through :func:`_declare_axis`.
AXIS = "did:TEST_BASE_FUEL_SCHEDULE_MS"
#: The synthetic channel's catalog unit. Real channels resolve through
#: ``catalog_channel_unit``; this one is in no catalog.
UNITS = {AXIS: "ms"}.get
#: The repo catalogs, through the real resolver.
REPO_UNITS = channel_units_for(load_repo_did_map())
#: The synthetic map's revision, flashed before every test row (P02.16: each
#: row's map is the revision flashed at its timestamp).
HISTORY = TuneRevisionRegistry(
    revisions=[TuneRevision(revision="r1", flashed_at=datetime(2000, 1, 1, tzinfo=UTC))]
)


def _stamped(frame: pd.DataFrame) -> pd.DataFrame:
    """``frame`` with one-second UTC timestamps, all after :data:`HISTORY`'s flash."""
    stamps = pd.date_range("2026-01-01", periods=len(frame), freq="s", tz="UTC")
    return frame.assign(timestamp=stamps)


def _without_load_axes() -> tuple[types.SimpleNamespace, ...]:
    """The real vehicle modules minus their load-axis declarations."""
    return tuple(
        types.SimpleNamespace(**{k: v for k, v in vars(m).items() if k != "LOAD_AXIS_CHANNELS"})
        for m in vehicle_signals.vehicle_modules()
    )


def _undeclare_axis(monkeypatch: pytest.MonkeyPatch) -> None:
    """No vehicle declares a load-axis channel: the state before P02.14."""
    stripped = _without_load_axes()
    monkeypatch.setattr(vehicle_signals, "vehicle_modules", lambda: stripped)


def _declare_axis(monkeypatch: pytest.MonkeyPatch, *channels: LoadAxisChannel) -> None:
    """Make ``channels`` (default: :data:`AXIS` in ms) the only load-axis
    declaration. The real vehicles' own (the Xterra's
    ``derived:BASE_FUEL_SCHEDULE``, P02.14) are removed: :data:`UNITS` puts
    only the synthetic channel in a catalog, and these tests are about the
    lookup, not about which vehicle declares what."""
    declared = channels or (
        LoadAxisChannel(
            axis="base_fuel_schedule",
            channel=AXIS,
            evidence="synthetic test vehicle",
        ),
    )
    module = types.SimpleNamespace(LOAD_AXIS_CHANNELS=declared)
    real = _without_load_axes()
    monkeypatch.setattr(vehicle_signals, "vehicle_modules", lambda: (*real, module))


def _calibration() -> AfrCalibration:
    return AfrCalibration.model_validate(
        {
            "active_revision": "r1",
            **voltage_chain(
                [
                    {"voltage": 0.5, "afr": 10.0},
                    {"voltage": 2.0, "afr": 14.0},
                    {"voltage": 5.0, "afr": 20.0},
                ],
                censored_floor_afr=10.0,
            ),
            "target_maps": [
                {
                    "revision": "r1",
                    "rpm_breakpoints": [1000, 3000],
                    "load_breakpoints": [20, 100],
                    "afr": [[14.7, 13.0], [14.0, 12.0]],
                }
            ],
        }
    )


def test_target_afr_bilinear_and_out_of_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    _declare_axis(monkeypatch)
    target = _calibration().target_map("r1")
    assert target is not None
    values = target_afr(
        pd.DataFrame({"obd:RPM": [1000.0, 2000.0, 9999.0], AXIS: [20.0, 60.0, 60.0]}),
        target,
        channel_units=UNITS,
    )
    assert values.iloc[0] == pytest.approx(14.7)
    assert values.iloc[1] == pytest.approx((14.7 + 13.0 + 14.0 + 12.0) / 4)
    assert pd.isna(values.iloc[2])


def test_lambda_to_afr_target_error_and_censoring(monkeypatch: pytest.MonkeyPatch) -> None:
    _declare_axis(monkeypatch)
    frame = pd.DataFrame(
        {
            "obd:RPM": [1000.0, 3000.0],
            AXIS: [20.0, 100.0],
            "obd:O2_WR_LAMBDA_S1": [1.0, 0.5],
            "obd:O2_WR_LAMBDA_S2": [1.01, 0.5],
        }
    )
    out = append_afr_channels(
        _stamped(frame),
        VehicleParameters(),
        _calibration(),
        channel_units=UNITS,
        flash_history=HISTORY,
    )
    assert out["derived:AFR_B1"].iloc[0] == pytest.approx(14.7)
    assert bool(out["derived:AFR_CENSORED_B1"].iloc[1]) is True
    assert pd.isna(out["derived:AFR_ERROR_B1"].iloc[1])
    assert out["derived:TARGET_AFR"].tolist() == pytest.approx([14.7, 12.0])


def test_voltage_transfer_is_used_when_lambda_is_absent() -> None:
    frame = baro_columns(pd.DataFrame({"did:ECM_AF_SENSOR_V_B1": [2.0]}))
    out = append_afr_channels(
        _stamped(frame),
        VehicleParameters(),
        _calibration(),
        channel_units=UNITS,
        flash_history=HISTORY,
    )
    assert out["derived:AFR_B1"].iloc[0] == pytest.approx(14.0)


def test_target_grid_is_strictly_shaped() -> None:
    with pytest.raises(ValueError, match="grid"):
        TargetAfrMap(
            revision="bad",
            rpm_breakpoints=[1000, 2000],
            load_breakpoints=[20, 100],
            afr=[[14.7]],
        )


def test_afr_error_surface_excludes_censored_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    _declare_axis(monkeypatch)
    target = _calibration().target_map("r1")
    assert target is not None
    frame = pd.DataFrame(
        {
            "obd:RPM": [1500, 1500, 1500],
            AXIS: [40, 40, 40],
            "derived:AFR_ERROR_B1": [0.2, 0.4, -4.0],
            "derived:AFR_CENSORED_B1": [False, False, True],
        }
    )
    rows = afr_error_surface(frame, target, bank=1, min_samples=3, channel_units=UNITS)
    assert rows[0]["count"] == 2
    assert rows[0]["median_error_afr"] == pytest.approx(0.3)
    assert rows[0]["sparse"] is True
    with pytest.raises(ValueError, match="strictly increasing"):
        VoltageAfrTransfer.model_validate(
            transfer([{"voltage": 2, "afr": 14}, {"voltage": 1, "afr": 13}])
        )


def test_decel_fuel_cut_is_censored_like_the_rich_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overrun is no injection at all, not a lean mixture to correct."""
    _declare_axis(monkeypatch)
    frame = pd.DataFrame(
        {
            "obd:RPM": [2000.0, 2000.0, 2000.0],
            AXIS: [60.0, 60.0, 60.0],
            # Stoich, pegged rich, and decel fuel cut.
            "obd:O2_WR_LAMBDA_S1": [1.0, 0.6, 2.2],
        }
    )
    out = append_afr_channels(
        _stamped(frame),
        VehicleParameters(),
        _calibration(),
        channel_units=UNITS,
        flash_history=HISTORY,
    )
    censored = out["derived:AFR_CENSORED_B1"].tolist()
    assert censored == [False, True, True]

    # The censored rows carry no error, so they cannot skew a cell.
    assert pd.notna(out["derived:AFR_ERROR_B1"].iloc[0])
    assert out["derived:AFR_ERROR_B1"].iloc[1:].isna().all()

    target = _calibration().target_map("r1")
    assert target is not None
    cells = afr_error_surface(out, target, bank=1, min_samples=1, channel_units=UNITS)
    assert sum(cell["count"] for cell in cells) == 1


def test_bank_falls_back_to_voltage_when_lambda_column_is_empty() -> None:
    """An unsupported bank leaves a present-but-empty column behind."""
    frame = pd.DataFrame(
        {
            "obd:RPM": [2000.0, 2000.0],
            "obd:ENGINE_LOAD": [60.0, 60.0],
            "obd:O2_WR_LAMBDA_S5": [float("nan"), float("nan")],
            "did:ECM_AF_SENSOR_V_B2": [2.0, 2.0],
        }
    )
    frame = baro_columns(frame)
    out = append_afr_channels(
        _stamped(frame),
        VehicleParameters(),
        _calibration(),
        channel_units=UNITS,
        flash_history=HISTORY,
    )
    assert "derived:AFR_B2" in out
    assert out["derived:AFR_B2"].tolist() == pytest.approx([14.0, 14.0])


def test_transfer_rejects_a_floor_above_its_ceiling() -> None:
    with pytest.raises(ValueError, match="below censored_ceiling_afr"):
        VoltageAfrTransfer.model_validate(
            transfer(
                [{"voltage": 0.5, "afr": 10.0}, {"voltage": 2.0, "afr": 14.0}],
                censored_floor_afr=20.0,
                censored_ceiling_afr=18.0,
            )
        )


def test_bank_2_lambda_is_the_vehicle_declared_sensor_not_sensor_2() -> None:
    """J1979 sensor 2 (PID 0x25) is bank 1 sensor 2 under both O2 layouts.

    It used to be read as bank 2. The Xterra's PID 0x13 layout makes bank 2
    upstream sensor 5 (PID 0x28), and the PID wins over the voltage DID
    (D03.34).
    """
    frame = pd.DataFrame(
        {
            "obd:O2_WR_LAMBDA_S2": [0.5, 0.5],
            "obd:O2_WR_LAMBDA_S5": [1.0, 1.0],
            "did:ECM_AF_SENSOR_V_B2": [2.0, 2.0],
        }
    )
    frame = baro_columns(frame)
    out = append_afr_channels(
        _stamped(frame),
        VehicleParameters(),
        _calibration(),
        channel_units=UNITS,
        flash_history=HISTORY,
    )
    assert out["derived:AFR_B2"].tolist() == pytest.approx([14.7, 14.7])

    without_s5 = append_afr_channels(
        _stamped(frame.drop(columns=["obd:O2_WR_LAMBDA_S5"])),
        VehicleParameters(),
        _calibration(),
        channel_units=UNITS,
        flash_history=HISTORY,
    )
    assert without_s5["derived:AFR_B2"].tolist() == pytest.approx([14.0, 14.0])


# ---------------------------------------------------------------------------
# P02.13: the lookup's load input is the map's declared axis, nothing else
# ---------------------------------------------------------------------------


def _engine_load_frame() -> pd.DataFrame:
    """Rows whose calculated load (%) sits inside the test map's load grid, as
    obd:ENGINE_LOAD did inside the Xterra's 8.76-21.57 ms grid."""
    return pd.DataFrame(
        {
            "obd:RPM": [1500.0, 2500.0],
            "obd:ENGINE_LOAD": [40.0, 60.0],
            "obd:O2_WR_LAMBDA_S1": [1.0, 0.95],
        }
    )


def test_engine_load_never_reaches_the_target_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calculated load in the frame produces no target and no error.

    With the real vehicle modules the axis is the Xterra's
    ``derived:BASE_FUEL_SCHEDULE`` (P02.14), which this frame lacks, so there
    is no target, and the lookup names the missing channel. With no
    declaration at all it refuses by name. Neither computes one from load."""
    frame = _engine_load_frame()
    target = _calibration().target_map("r1")
    assert target is not None
    out = append_afr_channels(
        _stamped(frame),
        VehicleParameters(),
        _calibration(),
        channel_units=REPO_UNITS,
        flash_history=HISTORY,
    )
    assert "derived:AFR_B1" in out, "measured AFR is unaffected"
    for column in (TARGET_AFR, *AFR_ERROR_CHANNELS):
        assert column not in out
    with pytest.raises(KeyError, match="derived:BASE_FUEL_SCHEDULE"):
        target_afr(frame, target, channel_units=REPO_UNITS)
    assert afr_error_surface(out, target, bank=1, channel_units=REPO_UNITS) == []

    _undeclare_axis(monkeypatch)
    out = append_afr_channels(
        _stamped(frame),
        VehicleParameters(),
        _calibration(),
        channel_units=UNITS,
        flash_history=HISTORY,
    )
    for column in (TARGET_AFR, *AFR_ERROR_CHANNELS):
        assert column not in out
    with pytest.raises(LoadAxisUnavailableError, match=LOAD_AXIS_UNVERIFIED) as refused:
        target_afr(frame, target, channel_units=REPO_UNITS)
    assert "base_fuel_schedule (ms)" in refused.value.reason
    with pytest.raises(LoadAxisUnavailableError, match=LOAD_AXIS_UNVERIFIED):
        afr_error_surface(out, target, bank=1, channel_units=REPO_UNITS)


def test_the_lookup_takes_no_load_series_of_its_own() -> None:
    """``target_afr`` used to take ``(rpm, load, target)``, and the caller fed
    it ``obd:ENGINE_LOAD``. The load now comes only from the frame, through
    the vehicle's declaration, so there is no argument to put a load in."""
    assert list(inspect.signature(target_afr).parameters) == [
        "frame",
        "target",
        "channel_units",
        "rpm_column",
    ]


@pytest.mark.parametrize(
    ("channel", "why"),
    [
        # Calculated load, and the absolute load this truck also polls: % in
        # the Mode 01 registry, whatever a declaration claims.
        ("obd:ENGINE_LOAD", "catalog unit %"),
        ("obd:ABSOLUTE_LOAD", "catalog unit %"),
        # BFS itself, but in raw counts: the DID stays unscaled (a served feature),
        # and only its P02.14 view, derived:BASE_FUEL_SCHEDULE, is in ms.
        ("did:ECM_AIR_CHARGE", "gives it no unit"),
        # RAM channels: one unscaled, one in another unit.
        ("mem:ECM_ENGINE_PERIOD", "gives it no unit"),
        ("mem:ECM_TARGET_EQUIV_RATIO", "catalog unit phi"),
        # decoded: rows are raw counts; derived: has no unit catalog.
        ("decoded:ECM_RPM", "gives it no unit"),
        ("derived:BASE_FUEL_SCHEDULE_MS", "gives it no unit"),
    ],
)
def test_an_axis_channel_is_refused_unless_its_catalog_unit_is_the_axis_unit(
    monkeypatch: pytest.MonkeyPatch, channel: str, why: str
) -> None:
    """The unit is the channel catalog's, never the declaration's (verifier,
    P02.13): a declaration alone cannot put a % load or raw counts on a ms axis."""
    _declare_axis(
        monkeypatch, LoadAxisChannel(axis="base_fuel_schedule", channel=channel, evidence="x")
    )
    frame = _engine_load_frame().assign(**{channel: [15.0, 16.0]})
    target = _calibration().target_map("r1")
    assert target is not None
    with pytest.raises(LoadAxisUnavailableError, match=LOAD_AXIS_UNVERIFIED) as refused:
        target_afr(frame, target, channel_units=REPO_UNITS)
    assert channel in refused.value.reason and why in refused.value.reason
    out = append_afr_channels(
        _stamped(frame),
        VehicleParameters(),
        _calibration(),
        channel_units=REPO_UNITS,
        flash_history=HISTORY,
    )
    assert TARGET_AFR not in out


def test_the_catalog_check_passes_a_channel_the_catalog_puts_in_ms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal can pass: a DID the catalog scales to ms is admitted. (The
    injector pulse is not the load axis; it only shows the check is not a
    constant refusal.)"""
    channel = "did:ECM_INJECTOR_PULSE_1"
    assert catalog_channel_unit(channel, did_map=load_repo_did_map()) == "ms"
    _declare_axis(
        monkeypatch, LoadAxisChannel(axis="base_fuel_schedule", channel=channel, evidence="x")
    )
    frame = _engine_load_frame().assign(**{channel: [40.0, 60.0]})
    out = append_afr_channels(
        _stamped(frame),
        VehicleParameters(),
        _calibration(),
        channel_units=REPO_UNITS,
        flash_history=HISTORY,
    )
    assert out[TARGET_AFR].notna().all()


def test_no_catalog_means_no_target(monkeypatch: pytest.MonkeyPatch) -> None:
    _declare_axis(monkeypatch)
    frame = _engine_load_frame().assign(**{AXIS: [15.0, 16.0]})
    out = append_afr_channels(
        _stamped(frame),
        VehicleParameters(),
        _calibration(),
        channel_units=None,
        flash_history=HISTORY,
    )
    assert TARGET_AFR not in out


def test_a_verified_axis_channel_absent_from_the_frame_yields_no_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declared but not captured: absent, and calculated load does not stand in."""
    _declare_axis(monkeypatch)
    out = append_afr_channels(
        _stamped(_engine_load_frame()),
        VehicleParameters(),
        _calibration(),
        channel_units=UNITS,
        flash_history=HISTORY,
    )
    assert TARGET_AFR not in out


def test_an_axis_declaration_must_name_its_evidence() -> None:
    with pytest.raises(ValueError, match="verification evidence"):
        LoadAxisChannel(axis="base_fuel_schedule", channel=AXIS, evidence=" ")
    with pytest.raises(ValueError, match="source namespace"):
        LoadAxisChannel(axis="base_fuel_schedule", channel="BFS", evidence="x")


# ---------------------------------------------------------------------------
# B06.7: the voltage route applies the ECM's barometric gain, or refuses
# ---------------------------------------------------------------------------


def _profile_calibration() -> AfrCalibration:
    return load_afr_calibration(Path("data/nissan-xterra-2008/afr_calibration.yaml"))


def test_voltage_to_afr_requires_baro_and_applies_the_ecm_gain_about_stoich() -> None:
    """x2' = 880 + (x2 - 880) x 128 / k: stoich (2.2 V) is the fixed point,
    a rich voltage moves richer at 80 kPa, and sea level is gain 1."""
    calibration = _profile_calibration()
    assert "baro" in inspect.signature(voltage_to_afr).parameters
    assert inspect.signature(voltage_to_afr).parameters["baro"].default is inspect.Parameter.empty
    volts = pd.Series([2.2, 1.4])
    sea = voltage_to_afr(volts, calibration, baro=BaroSamples(np.full(2, 101.0), np.zeros(2)))
    alt = voltage_to_afr(volts, calibration, baro=BaroSamples(np.full(2, 80.0), np.zeros(2)))
    assert sea.gain.tolist() == [1.0, 1.0]
    # k at 80.5 kPa (the middle of PID 0x33's floored 80) on the 0xD00F table.
    k = 109 + (80.5 - 76.80) / (82.47 - 76.80) * (115 - 109)
    assert alt.gain.iloc[0] == pytest.approx(128 / k, rel=1e-3)
    assert alt.afr.iloc[0] == pytest.approx(sea.afr.iloc[0])
    expected_x2 = 880 + (1.4 / 0.0025 - 880) * 128 / k
    curve = calibration.voltage_transfer
    assert curve is not None
    assert alt.afr.iloc[1] == pytest.approx(
        float(curve.curve_afr(np.array([expected_x2 * 0.0025]))[0])
    )
    assert alt.afr.iloc[1] < sea.afr.iloc[1]


def test_missing_or_stale_baro_is_a_named_refusal_never_gain_1() -> None:
    calibration = _profile_calibration()
    volts = pd.Series([1.4, 1.4, 1.4])
    baro = BaroSamples.as_of(np.array([0.0, 100.0, 200.0]), np.array([90.0]), np.array([80.0]))
    out = voltage_to_afr(volts, calibration, baro=baro)
    assert out.refusals() == {BARO_MISSING: 1, BARO_STALE: 1}
    assert out.baro_missing.tolist() == [True, False, False]
    assert out.baro_stale.tolist() == [False, False, True]
    assert out.afr.isna().tolist() == [True, False, True]


def test_a_wide_frame_without_baro_has_no_voltage_afr() -> None:
    frame = pd.DataFrame({"did:ECM_AF_SENSOR_V_B1": [2.0, 2.0]})
    out = append_afr_channels(
        _stamped(frame),
        VehicleParameters(),
        _calibration(),
        channel_units=UNITS,
        flash_history=HISTORY,
    )
    assert "derived:AFR_B1" not in out


def test_a_wide_baro_cell_with_no_arrival_in_the_frame_is_stale() -> None:
    frame = _stamped(pd.DataFrame({"obd:BAROMETRIC_PRESSURE": [80.0, 80.0, 80.0]}))
    frame["obd:BAROMETRIC_PRESSURE__arrived"] = [False, True, False]
    baro = BaroSamples.from_wide(frame, "obd:BAROMETRIC_PRESSURE")
    assert np.isnan(baro.age_s[0])
    assert baro.age_s[1:].tolist() == [0.0, 1.0]


def test_a_transfer_without_the_gain_or_its_residual_sources_is_refused() -> None:
    raw = load_yaml(Path("data/nissan-xterra-2008/afr_calibration.yaml").read_text())
    without_gain = {k: v for k, v in raw.items() if k != "baro_gain"}
    with pytest.raises(ValueError, match="baro_gain is required"):
        AfrCalibration.model_validate(without_gain)
    without_offset = dict(raw, voltage_residual_sources=raw["voltage_residual_sources"][:1])
    with pytest.raises(ValueError, match="missing \\['rpm_bfs_offset'\\]"):
        AfrCalibration.model_validate(without_offset)
    edited = dict(raw)
    edited["voltage_transfer"] = dict(raw["voltage_transfer"])
    edited["voltage_transfer"]["points"] = [
        dict(p, afr=p["afr"] * 1.01) for p in raw["voltage_transfer"]["points"]
    ]
    with pytest.raises(ValueError, match="must equal ECM table 0xE644"):
        AfrCalibration.model_validate(edited)


def test_the_profile_curve_is_ecm_table_0xe644_at_gain_1() -> None:
    """Operator decision 2026-10-07: the transfer IS table 0xE644. The voltage
    axis is the DID word at 0.005 V; x2 = 2 x word; the 880 knot is 2.2 V."""
    calibration = _profile_calibration()
    transfer_ = calibration.voltage_transfer
    gain = calibration.baro_gain
    assert transfer_ is not None and gain is not None
    provenance = transfer_.provenance
    assert provenance is not None and provenance.table is not None
    assert provenance.method == "rom_table"
    assert provenance.table.address == "0xE644"
    assert provenance.volts_per_count == 0.0025
    assert gain.stoich_x2_counts * provenance.volts_per_count == pytest.approx(2.2)
    assert [(p.voltage, p.afr) for p in transfer_.points][5] == pytest.approx((2.2, 14.774))
    assert set(provenance.excludes) == {"baro_gain", "learned_rear_o2_offset", "rpm_bfs_offset"}
    assert calibration.residual_source("rpm_bfs_offset").bounds_afr[1] == (-0.014, 0.074)
    assert gain.skip_kpa == pytest.approx((99.52, 111.70), abs=0.01)


def test_the_example_calibration_loads_without_a_baro_gain() -> None:
    """B06.7 is per vehicle: a vehicle that declares no ECM gain needs no
    provenance and no baro, and its voltage route is the curve alone."""
    calibration = load_afr_calibration(Path("data/afr_calibration.example.yaml"))
    assert calibration.voltage_transfer is not None
    assert calibration.baro_gain is None and calibration.voltage_residual_sources == []
    out = voltage_to_afr(pd.Series([2.15]), calibration, baro=None)
    assert out.afr.iloc[0] == pytest.approx(14.7)
    assert out.refusals() == {BARO_MISSING: 0, BARO_STALE: 0}
    with pytest.raises(ValueError, match="no barometric gain"):
        voltage_to_afr(pd.Series([2.15]), calibration, baro=BaroSamples.unobserved(1))
    frame = _stamped(pd.DataFrame({"did:ECM_AF_SENSOR_V_B1": [2.15]}))
    appended = append_afr_channels(
        frame, VehicleParameters(), calibration, channel_units=UNITS, flash_history=HISTORY
    )
    assert appended["derived:AFR_B1"].iloc[0] == pytest.approx(14.7)


def test_a_declared_gain_needs_baro_and_a_gain_needs_its_term_declared() -> None:
    calibration = _profile_calibration()
    with pytest.raises(ValueError, match="needs baro"):
        voltage_to_afr(pd.Series([2.0]), calibration, baro=None)
    raw = load_yaml(Path("data/nissan-xterra-2008/afr_calibration.yaml").read_text())
    transfer_ = dict(raw["voltage_transfer"])
    transfer_["provenance"] = dict(transfer_["provenance"], excludes=[])
    with pytest.raises(ValueError, match="does not name baro_gain"):
        AfrCalibration.model_validate(
            dict(raw, voltage_transfer=transfer_, voltage_residual_sources=[])
        )
