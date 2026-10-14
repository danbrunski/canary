"""The vehicle-neutral recipe catalog (R17; was ``refit.shared_recipes``).

Surfaces that only need standard Mode 01 PIDs and derived channels. A vehicle's
own Mode 22 surfaces live in that profile's module under
:mod:`src.analysis.recipes` (R05.7, operator decision 2026-09-25).

Moved verbatim from ``refit.py``: names, order, fields and fingerprints are
pinned by ``tests/test_recipe_catalog_golden.py``. This module imports the
recipe contract and builders, never the fitting engine; ``refit`` consumes it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from src.analysis.recipe_builders import (
    TOTAL_TRIM_STEADY_RATES,
    rpm_load_2d_recipe,
    sigma_deviation,
    surface_1d_recipe,
    surface_2d_recipe,
    warmup_1d_recipe,
)
from src.analysis.recipe_contract import Recipe, Surface1DRecipe
from src.behavior.behavior_models import (
    BandExcessPersistence,
    DeviationConfig,
    LocalRangeCondition,
)
from src.operating import ENGINE_RUNNING_MIN_RPM
from src.signals.derived import (
    STFT_BANK_BALANCE_SMOOTHING_SECONDS,
    VE_AMBIENT_CHANNEL,
    VE_MANIFOLD_CHANNEL,
)

#: B05.2 — ``derived:TOTAL_TRIM_B*`` is STFT + LTFT summed in percentage
#: points: an additive index, not the multiplicative fuel correction
#: ``(1 + STFT)(1 + LTFT)`` and not a calibrated injector multiplier.
TOTAL_TRIM_UNIT = "percentage points (additive STFT+LTFT index)"

#: B07.6 — the warm-idle baseline grades steady idle only (operator decision
#: 2026-09-23). The limit is B05.2's recorded 10 rpm/s (TOTAL_TRIM_STEADY_RATES),
#: not re-derived: on the warm-idle training fold (drive 2026-09-21T194247, 631
#: idle rows, 627 of them locally steady) 75/20/10 rpm/s pass the same 561
#: rate-defined rows, so that fold cannot choose between limits, and no test
#: drive was consulted. Before the gate, rpm excursions inside ``idle`` mode
#: (a 30 s net change) were graded as idle deviations.
#: data/analyses/2026-09-23-behavior-refit-validation-report.md §11.
WARM_IDLE_STEADY_RATES: Mapping[str, float] = {"obd:RPM": 10.0}

#: B07.6 — and a local-range limit the net-change gate cannot express
#: (operator decision 2026-09-23): the rpm spread over the last 5 s. The limit
#: is B05.2's "locally transient" definition (5 s rpm range > 100), recorded in
#: data/analyses/2026-09-22-b04-b05-measurements.md §4, adopted as defined,
#: not tuned. The capture it was defined on lies in warm idle's test drive
#: group; on the warm-idle training fold 627 of 631 idle rows satisfy it.
WARM_IDLE_LOCAL_RANGES: tuple[LocalRangeCondition, ...] = (
    LocalRangeCondition(
        channel="obd:RPM",
        window_seconds=5.0,
        max_range=100.0,
        basis=(
            "B05.2 'locally transient' definition: 5 s rpm range > 100 "
            "(data/analyses/2026-09-22-b04-b05-measurements.md §4); adopted for warm "
            "idle by operator decision 2026-09-23 (B07.6)"
        ),
    ),
)

#: Operating modes the bank-balance baselines are conditioned on.
BANK_BALANCE_MODES: tuple[str, ...] = ("idle", "city", "highway")

#: B05.3 — the STFT bank-balance baselines are evented by accumulated excess
#: beyond their warning band, not by the daemon's 3-of-5 tick count. The gap
#: that restarts the accumulation is the signal's own memory: the balance is a
#: 30 s trailing mean (``STFT_BANK_BALANCE_SMOOTHING_SECONDS``), so a value
#: after a longer gap shares no sample with one before it. The limit is fitted
#: per refit on the training fold. Measured before the rule (2026-09-23 city
#: model v1, default gate): 5 events in 0.81 h on its training drives and 5 in
#: 1.75 h on its test drives, every one a run of <= 18 warning ticks just past
#: the 3.1-point band (peaks 3.1-5.7).
STFT_BANK_BALANCE_PERSISTENCE = BandExcessPersistence(
    reset_gap_seconds=STFT_BANK_BALANCE_SMOOTHING_SECONDS,
    basis=(
        "STFT_BANK_BALANCE_SMOOTHING_SECONDS: derived:STFT_BANK_BALANCE is a 30 s "
        "trailing mean, so an evaluated tick after a longer gap shares no sample "
        "with the accumulation before it (B05.3, 2026-09-26)"
    ),
)

_BANK_BALANCE_NOTES: Mapping[str, str] = {
    "stft": (
        "Mode-conditioned robust median/MAD bank-balance baseline. "
        "A flat healthy signal is valuable and is not gated on R²."
    ),
    "ltft": "Mode-conditioned robust median/MAD long-term bank-balance baseline.",
}


def _bank_balance(kind: Literal["stft", "ltft"], mode: str) -> Surface1DRecipe:
    """Mode-conditioned robust bank-balance baseline, closed loop only."""
    channel = f"derived:{kind.upper()}_BANK_BALANCE"
    return surface_1d_recipe(
        name=f"{kind}_bank_balance_{mode}",
        output=channel,
        output_unit="%",
        x_column=channel,
        operating_mode=mode,
        fit_method="robust_baseline",
        min_samples=150,
        min_r_squared=None,
        min_rpm=ENGINE_RUNNING_MIN_RPM,
        closed_loop_only=True,
        deviation=sigma_deviation(0.8),
        # B05.3 — STFT only: ordinary closed-loop oscillation is the STFT
        # channel's; the LTFT baselines keep the daemon gate.
        event_persistence=STFT_BANK_BALANCE_PERSISTENCE if kind == "stft" else None,
        notes=_BANK_BALANCE_NOTES[kind],
    )


def shared_recipes() -> list[Recipe]:
    """The vehicle-neutral catalog: standard Mode 01 PIDs and derived channels.

    Surfaces that only need standard Mode 01 PIDs (Coolant, RPM, MAF,
    TIMING_ADVANCE, STFT, ENGINE_LOAD) and the derived channels built from
    them. Nothing here names a manufacturer DID: surfaces over a vehicle's own
    Mode 22 identifiers live in that profile's module under
    :mod:`src.analysis.recipes` (R05.7, operator decision 2026-09-25).

    Naming convention: ``<output>_vs_<axes>``. The dashboard's performance
    panel (5.12) will key off these names — keep them stable.
    """
    recipes: list[Recipe] = [
        warmup_1d_recipe(
            name="coolant_warmup",
            output="obd:COOLANT_TEMP",
            output_unit="degC",
            x_column="obd:RUN_TIME",
            x_axis_name="time_since_start",
            x_axis_unit="s",
            # WARMUP_1D_FIELDS carries the cold-to-warm transient gates.
            n_knots=12,
            # 4.6: widened from (5.0, 15.0) — the corpus doesn't have enough
            # usable cold-start sessions to add a load/IAT context axis (see
            # WARMUP_CONTEXT_SESSION_FLOOR above), so the band absorbs
            # load/ambient-driven warmup-shape variance instead.
            deviation=DeviationConfig(kind="absolute", warning_band=8.0, critical_band=22.0),
            notes=(
                "Thermostat health — sticking open / stuck closed shows as a "
                "flatter / delayed curve."
            ),
        ),
        surface_1d_recipe(
            name="warm_idle_rpm_baseline",
            output="obd:RPM",
            output_unit="rpm",
            x_column="obd:RPM",
            operating_mode="idle",
            min_rpm=ENGINE_RUNNING_MIN_RPM,  # keep KOEO rows (rpm=0) out of the idle curve
            fit_method="robust_baseline",
            min_r_squared=None,
            min_samples=200,
            max_abs_rate_of_change=WARM_IDLE_STEADY_RATES,
            local_ranges=WARM_IDLE_LOCAL_RANGES,
            deviation=sigma_deviation(25.0),
            notes=(
                "Warm stabilized idle baseline. Accessory-load state is not available, "
                "so robust bands absorb ordinary A/C/electrical-load steps. Graded only "
                "at steady rpm (WARM_IDLE_STEADY_RATES, WARM_IDLE_LOCAL_RANGES)."
            ),
            diagnostic_capabilities=("sustained warm-idle speed shift",),
            diagnostic_limitations=(
                "steady idle only: an rpm excursion (net change above 10 rpm/s over the "
                "30 s feature horizon, or more than 100 rpm of spread within 5 s) is not "
                "graded",
                "does not detect elevated idle in general, nor identify its cause",
            ),
        ),
        warmup_1d_recipe(
            name="cold_start_idle_rpm_vs_coolant",
            output="obd:RPM",
            output_unit="rpm",
            x_column="obd:COOLANT_TEMP",
            x_axis_name="coolant_temp",
            x_axis_unit="degC",
            n_knots=8,
            deviation=DeviationConfig(kind="absolute", warning_band=150.0, critical_band=300.0),
            notes="Cold-start fast-idle curve, isolated from warm accessory-loaded idle.",
        ),
        # NOTE: the 1D ``timing_advance_vs_rpm`` rollup was removed — timing
        # advance is heavily load-dependent, so an RPM-only surface fit on an
        # idle-heavy corpus predicts the idle value (~23°) and fires "critical"
        # on every normal cruise (~39°): 292 false alarms vs 6 for the 2D
        # version on a known-good drive. The load-conditioned surface below
        # supersedes it.
        rpm_load_2d_recipe(
            name="timing_advance_vs_rpm_load",
            output="obd:TIMING_ADVANCE",
            output_unit="degBTDC",
            # sigma_floor = one LSB of PID 0x0E (0.5°/bit): a cell fit on a
            # quantization-flat idle stretch can't alert on single-bit flicker.
            deviation=sigma_deviation(0.5),
            notes=(
                "Ignition surface. A sustained drop versus the learned cell is an "
                "unexpected timing change; knock-control retard is one possible "
                "cause among IAT/coolant compensation, calibration change and "
                "transients, and this surface does not identify which (B04.3)."
            ),
            diagnostic_capabilities=("unexpected sustained timing change",),
            diagnostic_limitations=(
                "does not identify knock retard or any other cause of a timing change",
                "load and RPM only: IAT/coolant compensation is unmodelled context",
            ),
        ),
        surface_2d_recipe(
            name="timing_advance_vs_rpm_iat",
            output="obd:TIMING_ADVANCE",
            output_unit="degBTDC",
            x_column="obd:RPM",
            y_column="obd:INTAKE_TEMP",
            x_axis_name="rpm",
            y_axis_name="intake_temp",
            x_axis_unit="rpm",
            y_axis_unit="degC",
            min_samples=500,
            min_rpm=ENGINE_RUNNING_MIN_RPM,  # KOEO timing reads ~63°; see ENGINE_RUNNING_MIN_RPM
            # Same band shape as the load-axis sibling — timing magnitudes and
            # cell-spread are the same; what differs is what we're trying to
            # catch. sigma_floor = one LSB of PID 0x0E.
            deviation=sigma_deviation(0.5),
            notes=(
                "Thermal-compensation surface for ignition. Cold dense charge "
                "lets the ECU advance more; hot charge gets retarded to keep "
                "knock margin. The surface learns the ECU's actual (RPM, IAT) "
                "→ timing map, so we read deviations from what the engine "
                "*normally* does at this thermal+rpm operating point — "
                "ambient-invariant by construction (the same IAT bin sees "
                "summer afternoons and winter mornings).\n\n"
                "Can show (never identify):\n"
                "* Sustained negative residuals clustered at the hot-IAT side "
                "  of the surface — consistent with, not proof of, knock-control "
                "  retard.\n"
                "* Thermal-compensation or knock-control drift. A biased IAT "
                "  that the ECU itself uses for timing is a common-mode fault "
                "  and is not identifiable from this surface alone.\n"
                "* Firmware / calibration drift after an ECU re-flash.\n\n"
                "Complementary to ``timing_advance_vs_rpm_load`` (load-axis "
                "compensation) — both feed the same timing output through "
                "different physical axes."
            ),
            diagnostic_capabilities=("timing-map drift",),
            diagnostic_limitations=(
                "cannot identify an IAT bias also consumed by the ECU "
                "without an independent reference",
                "does not identify knock retard; RPM/IAT alone omits load",
            ),
        ),
        rpm_load_2d_recipe(
            name="fuel_trim_vs_rpm_load",
            output="obd:SHORT_FUEL_TRIM_1",
            output_unit="%",
            # sigma_floor = one LSB of the trim PIDs (100/128 ≈ 0.78 %).
            deviation=sigma_deviation(100.0 / 128.0),
            notes=(
                "STFT B1 surface. Vacuum leaks / injector wear / fuel-quality "
                "drift shift cells off zero."
            ),
        ),
        # ----- Phase 1: derived-signal surfaces (no DIDs required) --------
        rpm_load_2d_recipe(
            name="air_mass_per_cycle_vs_rpm_load",
            output="derived:AIR_MASS_PER_CYCLE",
            output_unit="g/cycle",
            # sigma_floor ≈ one MAF LSB (0.01 g/s) translated to g/cycle at idle.
            deviation=sigma_deviation(0.002),
            notes=(
                "Measured air mass per four-stroke cycle (MAF x 120 / RPM), the raw "
                "quantity behind both VE channels, kept for inspection. Not "
                "density-normalized: altitude and intake temperature move it at a "
                "fixed operating point. Fed by ``compute_anomaly_signals``."
            ),
            diagnostic_capabilities=("measured airflow relationship drift",),
            diagnostic_limitations=(
                "does not separate MAF bias, unmetered air, restriction or air density",
            ),
        ),
        # B04.5 — VE is two quantities depending on the reference pressure;
        # each recipe targets the channel for exactly one of them. The
        # pre-B04.5 artifact of this name was fitted on the legacy
        # ``derived:VE_PERCENT`` (baro-referenced on a vehicle without MAP);
        # its output no longer matches this recipe, so a carry-over is flagged
        # ``contract_changed:recipe`` rather than served as either definition.
        rpm_load_2d_recipe(
            name="ve_vs_rpm_load",
            output=VE_AMBIENT_CHANNEL,
            output_unit="%",
            # The channel exists only with measured baro AND measured IAT
            # (no 25 degC default), and its gap marks either stale, so no
            # separate context requirement is needed for them.
            # sigma_floor: same MAF-LSB reasoning as air_mass_per_cycle_vs_rpm_load
            # (0.002 g/cycle) expressed as a percent of a representative idle-cell
            # 100%-VE fill (~0.4 g/cycle for this displacement class) — approximate,
            # like every other LSB-derived floor in this catalog, not exact.
            deviation=sigma_deviation(0.5),
            notes=(
                "Ambient-referenced volumetric efficiency: measured air mass per "
                "cycle (MAF/RPM) over the ideal-gas fill at measured barometric "
                "pressure and measured IAT. Includes throttling, so it tracks load "
                "(~20-30 % at idle); it normalizes air density (altitude, "
                "temperature) out of air_mass_per_cycle_vs_rpm_load, nothing more. "
                "Fed by ``compute_anomaly_signals``."
            ),
            diagnostic_capabilities=("density-normalized airflow relationship drift",),
            diagnostic_limitations=(
                "cannot distinguish MAF, baro, IAT, or mechanical airflow causes by itself",
                "shares the MAF/RPM measurement with air_mass_per_cycle_vs_rpm_load; "
                "the two are not independent confirmation",
            ),
        ),
        rpm_load_2d_recipe(
            name="ve_manifold_vs_rpm_load",
            output=VE_MANIFOLD_CHANNEL,
            output_unit="%",
            deviation=sigma_deviation(0.5),
            notes=(
                "Manifold-referenced volumetric efficiency: measured air mass per "
                "cycle over the ideal-gas fill at measured MAP and measured IAT "
                "(~80-95 % naturally aspirated at any throttle). Needs a MAP sensor; "
                "skipped (missing column) on a vehicle without one. Fed by "
                "``compute_anomaly_signals``."
            ),
            diagnostic_capabilities=("air-model relationship drift",),
            diagnostic_limitations=(
                "cannot distinguish MAF, MAP, IAT, or mechanical airflow causes by itself",
                "a restriction moves both measured flow and MAP, so it can cancel",
            ),
        ),
        # B05.2 — total trim is an additive STFT+LTFT index in percentage
        # points, graded only in healthy closed loop (J1979 0x02: excludes
        # open-loop warmup 0x01, enrichment / fuel cut 0x04 and feedback
        # faults 0x08/0x10), outside the ``warmup`` mode (closed loop while
        # cold still carries cold enrichment and inhibited trim learning) and
        # at steady RPM/load (TOTAL_TRIM_STEADY_RATES). All persisted on the
        # contract, so fit and serve apply the same rows.
        *[
            rpm_load_2d_recipe(
                name=f"total_trim_vs_rpm_load_b{bank}",
                output=f"derived:TOTAL_TRIM_B{bank}",
                output_unit=TOTAL_TRIM_UNIT,
                closed_loop_only=True,
                excluded_operating_modes=("warmup",),
                max_abs_rate_of_change=TOTAL_TRIM_STEADY_RATES,
                # Same trim-PID LSB floor as fuel_trim_vs_rpm_load (100/128 ≈
                # 0.78 %); TOTAL_TRIM sums two channels sharing that LSB, so
                # this is a lower bound on the achievable resolution.
                deviation=sigma_deviation(100.0 / 128.0),
                notes=(
                    f"Total fuel trim, bank {bank}: STFT + LTFT as an additive "
                    "percentage-point index (not a calibrated fuel multiplier). "
                    "Elevated only at low rpm/load points suggests unmetered air; "
                    "elevated across the surface suggests a metering or fuel-supply "
                    "bias — a human reads the map; no detection logic here."
                ),
                diagnostic_capabilities=("closed-loop fuel correction drift",),
                diagnostic_limitations=(
                    "an additive index, not a calibrated injector multiplier",
                    "does not identify a particular injector, leak or sensor",
                    *(
                        ()
                        if bank == 1
                        else (
                            "gated on fuel system 1's status (PID 0x03 byte A); "
                            "system 2's own state is recorded but not decoded",
                        )
                    ),
                ),
            )
            for bank in (1, 2)
        ],
        *(_bank_balance("stft", mode) for mode in BANK_BALANCE_MODES),
        *(_bank_balance("ltft", mode) for mode in BANK_BALANCE_MODES),
        warmup_1d_recipe(
            name="cat_warmup",
            output="obd:CATALYST_TEMP_B1S1",
            output_unit="degC",
            x_column="obd:RUN_TIME",
            x_axis_name="time_since_start",
            x_axis_unit="s",
            # Cat light-off mirrors coolant warmup — gate on warmup + cold_start
            # for the same reasons (the surface only describes cold-start
            # transients; warm-restart and steady-state shouldn't fit it).
            n_knots=12,
            # 4.6: widened from (30.0, 80.0) — same corpus/rationale as
            # coolant_warmup above (WARMUP_CONTEXT_SESSION_FLOOR).
            deviation=DeviationConfig(kind="absolute", warning_band=45.0, critical_band=115.0),
            notes=(
                "Catalyst light-off curve. Detects cat aging (slower "
                "light-off), exhaust restriction (faster temp rise), "
                "secondary air failure. Sister surface to coolant_warmup."
            ),
        ),
    ]
    return recipes


__all__ = [
    "BANK_BALANCE_MODES",
    "STFT_BANK_BALANCE_PERSISTENCE",
    "TOTAL_TRIM_UNIT",
    "WARM_IDLE_LOCAL_RANGES",
    "WARM_IDLE_STEADY_RATES",
    "shared_recipes",
]
