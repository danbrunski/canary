from __future__ import annotations

import pandas as pd
import pytest

from src.analysis.afr import (
    AfrCalibration,
    TargetAfrMap,
    VoltageAfrTransfer,
    afr_error_surface,
    append_afr_channels,
    target_afr,
)
from src.config import VehicleParameters


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


def test_target_afr_bilinear_and_out_of_domain() -> None:
    target = _calibration().target_map()
    assert target is not None
    values = target_afr(
        pd.Series([1000.0, 2000.0, 9999.0]),
        pd.Series([20.0, 60.0, 60.0]),
        target,
    )
    assert values.iloc[0] == pytest.approx(14.7)
    assert values.iloc[1] == pytest.approx((14.7 + 13.0 + 14.0 + 12.0) / 4)
    assert pd.isna(values.iloc[2])


def test_lambda_to_afr_target_error_and_censoring() -> None:
    frame = pd.DataFrame(
        {
            "obd:RPM": [1000.0, 3000.0],
            "obd:ENGINE_LOAD": [20.0, 100.0],
            "obd:O2_WR_LAMBDA_S1": [1.0, 0.5],
            "obd:O2_WR_LAMBDA_S2": [1.01, 0.5],
        }
    )
    out = append_afr_channels(frame, VehicleParameters(), _calibration())
    assert out["derived:AFR_B1"].iloc[0] == pytest.approx(14.7)
    assert bool(out["derived:AFR_CENSORED_B1"].iloc[1]) is True
    assert pd.isna(out["derived:AFR_ERROR_B1"].iloc[1])
    assert out["derived:TARGET_AFR"].tolist() == pytest.approx([14.7, 12.0])


def test_voltage_transfer_is_used_when_lambda_is_absent() -> None:
    frame = pd.DataFrame({"did:ECM_AF_SENSOR_V_B1": [2.0]})
    out = append_afr_channels(frame, VehicleParameters(), _calibration())
    assert out["derived:AFR_B1"].iloc[0] == pytest.approx(14.0)


def test_target_grid_is_strictly_shaped() -> None:
    with pytest.raises(ValueError, match="grid"):
        TargetAfrMap(
            revision="bad",
            rpm_breakpoints=[1000, 2000],
            load_breakpoints=[20, 100],
            afr=[[14.7]],
        )


def test_afr_error_surface_excludes_censored_samples() -> None:
    target = _calibration().target_map()
    assert target is not None
    frame = pd.DataFrame(
        {
            "obd:RPM": [1500, 1500, 1500],
            "obd:ENGINE_LOAD": [40, 40, 40],
            "derived:AFR_ERROR_B1": [0.2, 0.4, -4.0],
            "derived:AFR_CENSORED_B1": [False, False, True],
        }
    )
    rows = afr_error_surface(frame, target, bank=1, min_samples=3)
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


def test_decel_fuel_cut_is_censored_like_the_rich_floor() -> None:
    """Overrun is no injection at all, not a lean mixture to correct."""
    frame = pd.DataFrame(
        {
            "obd:RPM": [2000.0, 2000.0, 2000.0],
            "obd:ENGINE_LOAD": [60.0, 60.0, 60.0],
            # Stoich, pegged rich, and decel fuel cut.
            "obd:O2_WR_LAMBDA_S1": [1.0, 0.6, 2.2],
        }
    )
    out = append_afr_channels(frame, VehicleParameters(), _calibration())
    censored = out["derived:AFR_CENSORED_B1"].tolist()
    assert censored == [False, True, True]

    # The censored rows carry no error, so they cannot skew a cell.
    assert pd.notna(out["derived:AFR_ERROR_B1"].iloc[0])
    assert out["derived:AFR_ERROR_B1"].iloc[1:].isna().all()

    target = _calibration().target_map()
    assert target is not None
    cells = afr_error_surface(out, target, bank=1, min_samples=1)
    assert sum(cell["count"] for cell in cells) == 1


def test_bank_falls_back_to_voltage_when_lambda_column_is_empty() -> None:
    """An unsupported bank leaves a present-but-empty column behind."""
    frame = pd.DataFrame(
        {
            "obd:RPM": [2000.0, 2000.0],
            "obd:ENGINE_LOAD": [60.0, 60.0],
            "obd:O2_WR_LAMBDA_S2": [float("nan"), float("nan")],
            "did:ECM_AF_SENSOR_V_B2": [2.0, 2.0],
        }
    )
    out = append_afr_channels(frame, VehicleParameters(), _calibration())
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
