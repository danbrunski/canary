"""Behavior recipes over the 2008 Nissan Xterra's ECM DIDs (profile ``nissan-xterra-2008``).

These surfaces read VQ40 ECM Mode 22 identifiers (``did:ECM_*``), so they
are this vehicle's, not the shared catalog's (R05.7, operator decision
2026-09-25). Moved verbatim from ``refit.default_recipes``; their contract
fingerprints are pinned by ``tests/test_recipe_catalog_golden.py``, so the
Pi sees no ``contract_changed:recipe``. ``afr_bank_balance_robust`` is here
too: its input is the AF-sensor-voltage DID pair (B05.8 moved it from the
voltage difference ``derived:AFR_BANK_BALANCE`` to the λ difference
``derived:LAMBDA_BANK_BALANCE_AF_VOLTAGE``, a deliberate contract change; B06.7
renamed it ``..._AF_VOLTAGE_BARO`` when each bank's conversion gained the ECM's
barometric gain and became table 0xE644 itself, a second deliberate change: the
deployed fit is flagged ``contract_changed:recipe`` and needs a refit).
The two cam surfaces' outputs were renamed when the firmware named the banks
(D03.37, 2026-10-05: ``ECM_INTAKE_CAM_ANGLE_B1``/``_B2``), also deliberate.
"""

from __future__ import annotations

from typing import Literal

from src.analysis.refit import (
    TOTAL_TRIM_STEADY_RATES,
    Recipe,
    Surface2DRecipe,
    _sigma,
    _surface_1d,
    _surface_2d,
)
from src.behavior.behavior_models import BandExcessPersistence, DeviationConfig
from src.operating import ENGINE_RUNNING_MIN_RPM
from src.signals.afr import LAMBDA_BANK_BALANCE_AF_VOLTAGE

#: B05.8 — the λ bank balance reads two instantaneous DID samples, so the
#: accumulation's memory is the steady-state gate's own horizon: the rate
#: gates it persists are net changes over the feature pipeline's 30 s window.
#: The limit is fitted per refit on the training fold (B05.3's rule).
AFR_BANK_BALANCE_PERSISTENCE = BandExcessPersistence(
    reset_gap_seconds=30.0,
    basis=(
        "the persisted steady-state gates (TOTAL_TRIM_STEADY_RATES) are net changes "
        "over the feature pipeline's 30 s rolling horizon; evidence older than that "
        "horizon is not from the same steady stretch (B05.8, 2026-09-26)"
    ),
)


def _cam_tracking_2d(
    role: Literal["primary", "secondary"], *, bank: Literal[1, 2], output: str
) -> Surface2DRecipe:
    """Measured cam angle against its command and DID rpm, steady state only.

    ``role`` survives in the recipe name, which fitted artifacts and capture
    profiles reference; the bank is the firmware's (D03.37).
    """
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
            f"Bank {bank} measured-cam tracking surface ({role} in the name, which "
            "predates the firmware bank assignment). Which cylinder side is bank 1 "
            "is inferred. Evaluates only while commanded angle and RPM satisfy the "
            "persisted steady-state gates."
        ),
        diagnostic_capabilities=("cam command/position relationship drift",),
        diagnostic_limitations=(
            "does not evaluate command transitions; the bank's physical cylinder "
            "side is inferred, not measured",
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
        _cam_tracking_2d("primary", bank=1, output="did:ECM_INTAKE_CAM_ANGLE_B1"),
        _cam_tracking_2d("secondary", bank=2, output="did:ECM_INTAKE_CAM_ANGLE_B2"),
        # B05.8 — a λ residual, not volts: each bank's A/F sensor voltage goes
        # through the verified transfer before the difference. Graded only in
        # healthy closed loop (J1979 0x02), outside warmup and at steady
        # rpm/load (B05.2's measured TOTAL_TRIM_STEADY_RATES), so enrichment
        # and fuel-cut rails are never graded as asymmetry.
        _surface_1d(
            name="afr_bank_balance_robust",
            output=LAMBDA_BANK_BALANCE_AF_VOLTAGE,
            output_unit="lambda",
            x_column=LAMBDA_BANK_BALANCE_AF_VOLTAGE,
            fit_method="robust_baseline",
            min_r_squared=None,
            min_samples=300,
            min_rpm=ENGINE_RUNNING_MIN_RPM,
            closed_loop_only=True,
            excluded_operating_modes=("warmup",),
            max_abs_rate_of_change=TOTAL_TRIM_STEADY_RATES,
            # sigma_floor: one A/F-voltage DID LSB (0.005 V) at the transfer's
            # steepest near-stoich slope (3.9 AFR/V lean of stoich) is 0.0013 λ
            # per bank; a difference of two such channels, rounded up.
            deviation=_sigma(0.002),
            event_persistence=AFR_BANK_BALANCE_PERSISTENCE,
            notes=(
                "Bank 1 minus bank 2 measured lambda from the A/F sensor voltage DIDs, "
                "each through the profile's verified voltage->AFR transfer. Closed loop, "
                "warm and steady only. In closed loop the ECM drives each bank's "
                "measured lambda to target, so a delivery asymmetry its trims absorb "
                "shows in the STFT bank balance, not here; this residual shows a bank "
                "whose loop is not holding target."
            ),
            diagnostic_capabilities=("closed-loop bank lambda tracking asymmetry",),
            diagnostic_limitations=(
                "does not see a fuel-delivery asymmetry the closed-loop trims absorb "
                "(read the STFT bank balance for that)",
                "does not identify a particular injector, leak or sensor",
                "both banks share one voltage transfer: a common sensor-model error "
                "cancels, and a single-sensor bias is indistinguishable from mixture",
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
