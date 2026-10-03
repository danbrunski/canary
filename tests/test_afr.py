from __future__ import annotations

import inspect
import types
from datetime import UTC, datetime

import pandas as pd
import pytest

from src.config import VehicleParameters
from src.decode.dids import load_repo_did_map
from src.preprocessing.channel_units import catalog_channel_unit, channel_units_for
from src.signals import vehicle_signals
from src.signals.afr import (
    AFR_ERROR_CHANNELS,
    LOAD_AXIS_UNVERIFIED,
    TARGET_AFR,
    AfrCalibration,
    LoadAxisUnavailableError,
    TargetAfrMap,
    VoltageAfrTransfer,
    afr_error_surface,
    append_afr_channels,
    target_afr,
)
from src.signals.vehicle_signals import LoadAxisChannel
from src.vehicle.tune_revisions import TuneRevision, TuneRevisionRegistry

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
            "voltage_transfer": {
                "censored_floor_afr": 10.0,
                "points": [
                    {"voltage": 0.5, "afr": 10.0},
                    {"voltage": 2.0, "afr": 14.0},
                    {"voltage": 5.0, "afr": 20.0},
                ],
            },
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
    frame = pd.DataFrame({"did:ECM_AF_SENSOR_V_B1": [2.0]})
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
            {
                "points": [
                    {"voltage": 2, "afr": 14},
                    {"voltage": 1, "afr": 13},
                ]
            }
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
            {
                "points": [{"voltage": 0.5, "afr": 10.0}, {"voltage": 2.0, "afr": 14.0}],
                "censored_floor_afr": 20.0,
                "censored_ceiling_afr": 18.0,
            }
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
