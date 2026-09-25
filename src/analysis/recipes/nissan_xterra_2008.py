"""Behavior recipes over the 2008 Nissan Xterra's ECM DIDs (profile ``nissan-xterra-2008``).

These surfaces read VQ40 ECM Mode 22 identifiers (``did:ECM_*``), so they
are this vehicle's, not the shared catalog's (R05.7, operator decision
2026-09-25). Moved verbatim from ``refit.default_recipes``; their contract
fingerprints are pinned by ``tests/test_recipe_catalog_golden.py``, so the
Pi sees no ``contract_changed:recipe``. ``afr_bank_balance_robust`` is here
too: its ``derived:AFR_BANK_BALANCE`` input is the AF-sensor-voltage DID pair.
"""

from __future__ import annotations

from typing import Literal

from src.analysis.refit import Recipe, Surface2DRecipe, _sigma, _surface_1d, _surface_2d
from src.decode.behavior_models import DeviationConfig


def _cam_tracking_2d(role: Literal["primary", "secondary"], *, output: str) -> Surface2DRecipe:
    """Measured cam angle against its command and DID rpm, steady state only."""
    return _surface_2d(
        name=f"cam_{role}_vs_command_rpm_did",
        output=output,
        output_unit="deg",
        x_column="did:ECM_INTAKE_CAM_ANGLE_COMMANDED",
        y_column="did:ECM_RPM",
        x_axis_name="commanded_cam",
        y_axis_name="rpm",
        x_axis_unit="deg",
        y_axis_unit="rpm",
        min_samples=500,
        max_validation_warning_rate=0.03,
        max_abs_rate_of_change={
            "did:ECM_INTAKE_CAM_ANGLE_COMMANDED": 0.25,
            "did:ECM_RPM": 75.0,
        },
        deviation=_sigma(0.5),
        notes=(
            f"Neutral {role} measured-cam tracking surface; physical bank "
            "assignment remains intentionally unspecified. Evaluates only "
            "while commanded angle and RPM satisfy the persisted steady-state gates."
        ),
        diagnostic_capabilities=("cam command/position relationship drift",),
        diagnostic_limitations=(
            "does not identify the physical bank and does not evaluate command transitions",
        ),
    )


def recipes() -> list[Recipe]:
    """The Xterra's DID-dependent surfaces, in their pre-R05.7 catalog order."""
    return [
        # DID-dependent surfaces (skipped by refit until the DIDs are polled).
        _surface_2d(
            name="air_charge_vs_rpm_throttle_did",
            output="did:ECM_AIR_CHARGE",
            output_unit="raw",  # scale not yet locked; raw counts are stable for anomaly detection
            x_column="did:ECM_RPM",
            y_column="did:ECM_THROTTLE_POS_SENSOR_1",
            x_axis_name="rpm",
            y_axis_name="throttle_sensor_voltage",
            x_axis_unit="rpm",
            y_axis_unit="V",
            min_samples=500,
            # sigma_floor = 1 raw count (scale constant not yet pinned).
            deviation=_sigma(1.0),
            notes=(
                "Mode-22-synchronous air-charge surface. Detects a change in the "
                "ECM's air charge per intake event at the same DID RPM and throttle "
                "position. It says nothing about injected fuel (0x1208 ignores "
                "enrichment and does not drop in fuel cut)."
            ),
            diagnostic_capabilities=("air-charge map drift",),
            diagnostic_limitations=(
                "cannot uniquely identify intake restriction, MAF bias, "
                "or cam-phasing change; not a fuelling surface",
            ),
        ),
        _cam_tracking_2d("primary", output="did:ECM_INTAKE_CAM_ANGLE_MEASURED"),
        _cam_tracking_2d("secondary", output="did:ECM_INTAKE_CAM_ANGLE_MEASURED_B"),
        _surface_1d(
            name="afr_bank_balance_robust",
            output="derived:AFR_BANK_BALANCE",
            output_unit="V",
            x_column="derived:AFR_BANK_BALANCE",
            fit_method="robust_baseline",
            min_r_squared=None,
            min_samples=300,
            deviation=DeviationConfig(kind="absolute", warning_band=0.05, critical_band=0.15),
            notes=(
                "Wideband AFR sensor voltage delta between banks. Expected "
                "~0V; non-zero indicates bank-specific vacuum leak, "
                "single-injector wear, exhaust leak ahead of one sensor, "
                "single coil pack misfire. Sharpest bank-asymmetry detector "
                "available once AF sensor DIDs are polling."
            ),
        ),
        _surface_2d(
            name="afr_voltage_vs_rpm_throttle_did",
            output="did:ECM_AF_SENSOR_V_B1",
            output_unit="V",
            x_column="did:ECM_RPM",
            y_column="did:ECM_THROTTLE_POS_SENSOR_1",
            x_axis_name="rpm",
            y_axis_name="throttle_sensor_voltage",
            x_axis_unit="rpm",
            y_axis_unit="V",
            min_samples=500,
            deviation=DeviationConfig(kind="absolute", warning_band=0.10, critical_band=0.25),
            notes=(
                "Wideband AFR sensor voltage map. ~2.15V at stoich (closed "
                "loop), richer (lower V) at WOT, leaner (higher V) under "
                "decel-fuel-cut. Detects O2 sensor aging (whole-surface "
                "drift), systemic rich/lean (offset), closed-loop "
                "oscillation amplitude drift."
            ),
        ),
        _surface_1d(
            name="tps_sensor_1_vs_sensor_2_did",
            output="did:ECM_THROTTLE_POS_SENSOR_1",
            output_unit="V",
            x_column="did:ECM_THROTTLE_POS_SENSOR_2",
            x_axis_name="throttle_sensor_2_voltage",
            x_axis_unit="V",
            fit_method="poly",
            degree=1,
            min_samples=300,
            # min_rpm deliberately unset: the V-per-%% relationship is
            # engine-state-independent, and parked KOEO throttle sweeps are
            # the best full-range coverage this surface ever sees.
            # reference_std auto-fills from the fit residuals; sigma_floor
            # (4.2) = one LSB of the underlying ECM_THROTTLE_POS_SENSOR_1
            # voltage sensor (0.005 V, dids.yaml) — the pct denominator's
            # own quantization is secondary once pct > 1 (the div-by-zero
            # guard below), so the voltage LSB dominates the achievable
            # resolution of this ratio.
            deviation=DeviationConfig(
                kind="sigma", warning_band=5.0, critical_band=12.0, sigma_floor=0.005
            ),
            notes=(
                "Synchronous redundant TPS-voltage relationship. Drift between "
                "the two independent tracks can reveal calibration or sensor wear."
            ),
        ),
    ]
