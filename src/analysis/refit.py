"""Refit behavior-model surfaces from recorded sessions (Task 4.E.8).

The Phase 4.E inferers (1D / 2D) fit one surface at a time. This module is the
batch driver: a declarative list of recipes describes which surfaces we want
fitted from a feature frame, the runtime calls the appropriate fitter per
recipe, and the results are gathered into a versioned ``BehaviorModels`` ready
for ``dump_behavior_models``.

Two rules borrowed from the IF / LSTM trainers:

* **Partial coverage beats crash.** A recipe whose required columns aren't in
  the frame (we don't have an injector-PW DID yet, or the session doesn't
  carry MAP) is *skipped with a warning*, not an exception. Successive refits
  pick up new surfaces as more data lands without code changes.
* **Auto-version on overwrite.** If a previous ``behavior_models.yaml`` already
  has a model with the same name, its ``version`` is incremented when the
  refit produces a new fit. Git diff then shows the version history; drift
  comparison (Phase 4.E goal) reads ``version`` to walk back.

Pure functions; the CLI shell (``scripts/refit_behavior_models.py``) handles
session discovery, the preprocessing pipeline, and writing the YAML.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from src.analysis.surface_1d import fit_binned_lookup, fit_polynomial
from src.analysis.surface_2d import fit_grid_binned_mean
from src.decode.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    DeviationConfig,
)
from src.preprocessing.resampler import SESSION_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN

log = logging.getLogger(__name__)

#: RPM column used by the engine-running gate.
RPM_COLUMN = "obd:RPM"

#: Matches the vehicle_state classifier's engine_running threshold. KOEO rows
#: read RPM == 0 with wildly off-map outputs (TIMING_ADVANCE ≈ 63° vs ~17° at
#: idle), and an unfiltered KOEO-heavy corpus poisons both the fitted cells
#: and the auto-edge quantiles — the May 2026 IAT surface shipped a
#: 62.9° ± 5.0 "expectation" in its idle cell that would fire critical on
#: every heat-soaked summer idle.
ENGINE_RUNNING_MIN_RPM = 400.0

# ---------------------------------------------------------------------------
# 4.6 — warmup-surface confounding
# ---------------------------------------------------------------------------
#
# The f(RUN_TIME) warmup surfaces (coolant_warmup, cat_warmup,
# injector_pw_warmup) only ever learn the warmup curve *as it happened on
# the sessions in the corpus* — engine load and ambient IAT are free
# variables the RUN_TIME axis doesn't account for, so a corpus dominated by
# one load/IAT regime bakes that regime's warmup shape into the "expected"
# curve and then flags a real warmup under different conditions as
# deviation. (The same lesson already retired the load-blind 1D timing
# surface, above.)
#
# Operator-approved decision rule (OVERNIGHT-QUEUE.md, ml-hardening 4.6):
# count cold-start sessions in the corpus that actually have usable
# load/IAT spread during warmup — if there are enough to fit a second axis
# without it collapsing to noise, add one (RUN_TIME × load/IAT, 2D); if
# not, widen the 1D bands and document the confound instead of pretending
# a data-starved 2D fit is better than a wide 1D one.
#
# Counted against this laptop's corpus as of 2026-07-19 (``data/sessions``,
# via ``discover_sessions`` + ``count_usable_cold_start_sessions``): 14
# discoverable sessions total, 2 carry a ``cold_start`` event, and only 1 of
# those has enough load/IAT spread in its warmup window to be "usable" —
# far under the floor. So the three warmup recipes below take option (b):
# widened bands (roughly +50% over the pre-4.6 values) instead of a context
# axis. Re-run the count once the corpus grows past the floor.

#: Minimum observed spread (max − min) in at least one of these columns,
#: within one session's ``warmup``-mode rows, for that session to count as
#: "usable" context data — a session where load/IAT barely moved during
#: warmup teaches a context axis nothing. Units match each column.
WARMUP_CONTEXT_MIN_SPREAD: Mapping[str, float] = {
    "obd:ENGINE_LOAD": 15.0,  # %
    "obd:INTAKE_TEMP": 8.0,  # degC
}

#: >= this many usable cold-start sessions → add a context axis to the
#: warmup recipes; fewer → widen bands + document (operator-approved,
#: OVERNIGHT-QUEUE.md).
WARMUP_CONTEXT_SESSION_FLOOR = 8


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RecipeBase:
    """Shared recipe fields. Subclasses add fitter-specific knobs."""

    name: str
    output: str  # the column that *carries* the actual value; also the model's `output` name
    output_unit: str = "unknown"
    operating_mode: str | None = None  # restrict the input frame to this mode before fitting
    deviation: DeviationConfig | None = None
    min_samples: int = 50  # below this, skip the recipe rather than fit a brittle surface
    notes: str | None = None
    #: Auto-detected events (see ``src.analysis.events``) the *source session*
    #: must contain for any of its rows to feed this recipe. Use to gate
    #: warmup-specific surfaces to sessions that actually start cold, etc.
    #: Empty tuple means "no event requirement" — back-compat default.
    require_session_events: tuple[str, ...] = ()
    #: Engine-running gate: drop rows where ``obd:RPM`` < this before fitting
    #: (and before 2D auto-edge computation, so the quantiles aren't dragged
    #: to 0 by KOEO mass). ``None`` = no gate — for surfaces that genuinely
    #: describe key-on-engine-off behavior. Catalog recipes that describe
    #: *running* behavior set ``ENGINE_RUNNING_MIN_RPM`` explicitly.
    min_rpm: float | None = None
    #: 4.1 publish gate (1D only — grid fits' R² isn't well-defined, see
    #: ``Surface2DRecipe.min_populated_cell_fraction``). A fit whose R²
    #: falls below this is skipped rather than published, same as a
    #: missing-column or too-few-samples skip. ``None`` disables the gate.
    min_r_squared: float | None = 0.6


@dataclass(frozen=True)
class Surface1DRecipe(_RecipeBase):
    """How to fit one ``BehaviorModel1D`` from a feature frame."""

    kind: Literal["surface_1d"] = "surface_1d"
    x_column: str = ""
    x_axis_name: str = "x"
    x_axis_unit: str = "unknown"
    domain: tuple[float, float] | None = None  # None → derived from x.min()/x.max()
    fit_method: Literal["poly", "binned_lookup"] = "poly"
    degree: int = 3
    n_knots: int = 10


@dataclass(frozen=True)
class Surface2DRecipe(_RecipeBase):
    """How to fit one ``BehaviorModel2D`` from a feature frame."""

    kind: Literal["surface_2d"] = "surface_2d"
    x_column: str = ""
    y_column: str = ""
    x_axis_name: str = "x"
    y_axis_name: str = "y"
    x_axis_unit: str = "unknown"
    y_axis_unit: str = "unknown"
    #: Explicit bin edges per axis. ``None`` triggers auto-edges from the data's
    #: quantiles (``auto_bin_count + 1`` edges); useful when the operating
    #: envelope isn't known upfront. Required to be explicit only when you want
    #: a specific grid layout (e.g. matching a published OEM map).
    x_edges: tuple[float, ...] | None = None
    y_edges: tuple[float, ...] | None = None
    auto_bin_count: int = 8
    #: Cells with fewer samples than this evaluate as off-map at serve time
    #: instead of grading against a one-/six-sample "expectation".
    min_cell_count: int = 30
    #: 4.1 publish gate — fraction of cells that must clear
    #: ``min_cell_count`` for the grid to publish. Catches the silent
    #: all-empty-grid case (0.0 coverage) as well as a corpus too thin to
    #: populate most of the operating envelope; R² isn't meaningful for a
    #: grid-binned-mean fit so coverage is this recipe family's quality gate.
    min_populated_cell_fraction: float = 0.3


Recipe = Surface1DRecipe | Surface2DRecipe


# ---------------------------------------------------------------------------
# Default catalog
# ---------------------------------------------------------------------------


def default_recipes() -> list[Recipe]:
    """The catalog the daemon refits by default.

    Surfaces that only need standard Mode 01 PIDs (Coolant, RPM, MAF,
    TIMING_ADVANCE, STFT, ENGINE_LOAD) — i.e. fittable today, no Mode 22
    DIDs required. The fuel / AFR / VE / ignition surfaces from
    ``docs/behavior-model-inference.md`` require the injector-PW DID
    (4.E.4) before they're worth adding here.

    Naming convention: ``<output>_vs_<axes>``. The dashboard's performance
    panel (5.12) will key off these names — keep them stable.
    """
    return [
        Surface1DRecipe(
            name="coolant_warmup",
            output="obd:COOLANT_TEMP",
            output_unit="degC",
            x_column="obd:RUN_TIME",
            x_axis_name="time_since_start",
            x_axis_unit="s",
            # Surface only describes the cold-to-warm transient — restrict to
            # ``warmup`` operating mode AND require sessions that actually
            # contain a cold_start event. Without these gates the model fit
            # to "all data" reverts to the R²=0.41 May 2026 surface that
            # flooded the dashboard with warmup-storm anomalies.
            operating_mode="warmup",
            require_session_events=("cold_start",),
            min_rpm=ENGINE_RUNNING_MIN_RPM,  # KOEO rows have RUN_TIME=0 — not warmup data
            fit_method="binned_lookup",  # warmup is sigmoid-ish, polys overshoot
            n_knots=12,
            min_samples=120,
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
        Surface1DRecipe(
            name="idle_rpm_vs_coolant",
            output="obd:RPM",
            output_unit="rpm",
            x_column="obd:COOLANT_TEMP",
            x_axis_name="coolant_temp",
            x_axis_unit="degC",
            operating_mode="idle",
            min_rpm=ENGINE_RUNNING_MIN_RPM,  # keep KOEO rows (rpm=0) out of the idle curve
            fit_method="binned_lookup",
            n_knots=8,
            min_samples=200,
            deviation=DeviationConfig(kind="absolute", warning_band=100.0, critical_band=250.0),
            notes=("Cold-idle RPM target curve. Vacuum leaks / IACV faults show as elevated idle."),
        ),
        # NOTE: the 1D ``timing_advance_vs_rpm`` rollup was removed — timing
        # advance is heavily load-dependent, so an RPM-only surface fit on an
        # idle-heavy corpus predicts the idle value (~23°) and fires "critical"
        # on every normal cruise (~39°): 292 false alarms vs 6 for the 2D
        # version on a known-good drive. The load-conditioned surface below
        # supersedes it.
        Surface2DRecipe(
            name="timing_advance_vs_rpm_load",
            output="obd:TIMING_ADVANCE",
            output_unit="degBTDC",
            x_column="obd:RPM",
            y_column="obd:ENGINE_LOAD",
            x_axis_name="rpm",
            y_axis_name="engine_load",
            x_axis_unit="rpm",
            y_axis_unit="%",
            min_samples=500,
            min_rpm=ENGINE_RUNNING_MIN_RPM,  # KOEO timing reads ~63°; see ENGINE_RUNNING_MIN_RPM
            # sigma_floor = one LSB of PID 0x0E (0.5°/bit): a cell fit on a
            # quantization-flat idle stretch can't alert on single-bit flicker.
            deviation=DeviationConfig(
                kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=0.5
            ),
            notes=(
                "Ignition surface. Knock-retard activity shows as drops vs. the baseline cell mean."
            ),
        ),
        Surface2DRecipe(
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
            deviation=DeviationConfig(
                kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=0.5
            ),
            notes=(
                "Thermal-compensation surface for ignition. Cold dense charge "
                "lets the ECU advance more; hot charge gets retarded to keep "
                "knock margin. The surface learns the ECU's actual (RPM, IAT) "
                "→ timing map, so we read deviations from what the engine "
                "*normally* does at this thermal+rpm operating point — "
                "ambient-invariant by construction (the same IAT bin sees "
                "summer afternoons and winter mornings).\n\n"
                "Catches:\n"
                "* Sustained knock-retard activity (negative residuals "
                "  clustered at the hot-IAT side of the surface — detonation "
                "  triggers spark pull-back).\n"
                "* IAT sensor lying to the ECU — timing tracks the *reported* "
                "  IAT, so a stuck or biased sensor leaves the surface's "
                "  predicted timing matching the wrong cell, and actual "
                "  timing drifts off the learned map.\n"
                "* Firmware / calibration drift after an ECU re-flash.\n\n"
                "Complementary to ``timing_advance_vs_rpm_load`` (load-axis "
                "compensation) — both feed the same timing output through "
                "different physical axes."
            ),
        ),
        Surface2DRecipe(
            name="fuel_trim_vs_rpm_load",
            output="obd:SHORT_FUEL_TRIM_1",
            output_unit="%",
            x_column="obd:RPM",
            y_column="obd:ENGINE_LOAD",
            x_axis_name="rpm",
            y_axis_name="engine_load",
            x_axis_unit="rpm",
            y_axis_unit="%",
            min_samples=500,
            min_rpm=ENGINE_RUNNING_MIN_RPM,  # no fueling KOEO — trims are frozen zeros there
            # sigma_floor = one LSB of the trim PIDs (100/128 ≈ 0.78 %).
            deviation=DeviationConfig(
                kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=100.0 / 128.0
            ),
            notes=(
                "STFT B1 surface. Vacuum leaks / injector wear / fuel-quality "
                "drift shift cells off zero."
            ),
        ),
        # ----- Phase 1: derived-signal surfaces (no DIDs required) --------
        Surface2DRecipe(
            name="air_mass_per_cycle_vs_rpm_load",
            output="derived:AIR_MASS_PER_CYCLE",
            output_unit="g/cycle",
            x_column="obd:RPM",
            y_column="obd:ENGINE_LOAD",
            x_axis_name="rpm",
            y_axis_name="engine_load",
            x_axis_unit="rpm",
            y_axis_unit="%",
            min_samples=500,
            min_rpm=ENGINE_RUNNING_MIN_RPM,
            # sigma_floor ≈ one MAF LSB (0.01 g/s) translated to g/cycle at idle.
            deviation=DeviationConfig(
                kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=0.002
            ),
            notes=(
                "Volumetric efficiency proxy — MAF translated to grams per "
                "four-stroke cycle. Detects intake leak (low VE), exhaust "
                "restriction (low VE at high RPM), MAF drift (whole-surface "
                "shift). Fed by ``compute_anomaly_signals``."
            ),
        ),
        Surface2DRecipe(
            name="ve_vs_rpm_load",
            output="derived:VE_PERCENT",
            output_unit="%",
            x_column="obd:RPM",
            y_column="obd:ENGINE_LOAD",
            x_axis_name="rpm",
            y_axis_name="engine_load",
            x_axis_unit="rpm",
            y_axis_unit="%",
            min_samples=500,
            min_rpm=ENGINE_RUNNING_MIN_RPM,  # no fill KOEO — VE is a frozen/meaningless value there
            # sigma_floor: same MAF-LSB reasoning as air_mass_per_cycle_vs_rpm_load
            # (0.002 g/cycle) expressed as a percent of a representative idle-cell
            # 100%-VE fill (~0.4 g/cycle for this displacement class) — approximate,
            # like every other LSB-derived floor in this catalog, not exact.
            deviation=DeviationConfig(
                kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=0.5
            ),
            notes=(
                "True VE table (Task 4.1): measured air mass per cycle (MAF/RPM) "
                "over the theoretical 100%-VE fill from MAP-or-baro/IAT/"
                "displacement — the physics-normalized upgrade of "
                "air_mass_per_cycle_vs_rpm_load above. Detects intake leak / "
                "restriction (VE drop), MAF drift (whole-surface shift), "
                "cam/VVT fault (localized cell shift). Fed by "
                "``compute_anomaly_signals``."
            ),
        ),
        Surface2DRecipe(
            name="total_trim_vs_rpm_load_b1",
            output="derived:TOTAL_TRIM_B1",
            output_unit="%",
            x_column="obd:RPM",
            y_column="obd:ENGINE_LOAD",
            x_axis_name="rpm",
            y_axis_name="engine_load",
            x_axis_unit="rpm",
            y_axis_unit="%",
            min_samples=500,
            min_rpm=ENGINE_RUNNING_MIN_RPM,
            # Same trim-PID LSB floor as fuel_trim_vs_rpm_load (100/128 ≈ 0.78%);
            # TOTAL_TRIM sums two channels sharing that LSB, so this is a lower
            # bound on the achievable resolution, not a doubled value.
            deviation=DeviationConfig(
                kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=100.0 / 128.0
            ),
            notes=(
                "Total fuel trim (LTFT+STFT), bank 1 (Task 4.3). The "
                "vacuum-leak-vs-MAF-skew discriminator: elevated only in "
                "low-rpm/low-load cells → unmetered air (vacuum leak); "
                "elevated across the whole surface → MAF or fuel-pressure "
                "bias. A human reads this map; no detection logic here."
            ),
        ),
        Surface2DRecipe(
            name="total_trim_vs_rpm_load_b2",
            output="derived:TOTAL_TRIM_B2",
            output_unit="%",
            x_column="obd:RPM",
            y_column="obd:ENGINE_LOAD",
            x_axis_name="rpm",
            y_axis_name="engine_load",
            x_axis_unit="rpm",
            y_axis_unit="%",
            min_samples=500,
            min_rpm=ENGINE_RUNNING_MIN_RPM,
            deviation=DeviationConfig(
                kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=100.0 / 128.0
            ),
            notes=(
                "Total fuel trim (LTFT+STFT), bank 2 — sister surface to "
                "total_trim_vs_rpm_load_b1, same discriminator."
            ),
        ),
        Surface1DRecipe(
            name="stft_bank_balance_vs_rpm",
            output="derived:STFT_BANK_BALANCE",
            output_unit="%",
            x_column="obd:RPM",
            x_axis_name="rpm",
            x_axis_unit="rpm",
            fit_method="poly",
            degree=1,  # Expected ~constant zero across RPM; linear fit highlights slope drift
            min_samples=300,
            min_rpm=ENGINE_RUNNING_MIN_RPM,
            deviation=DeviationConfig(kind="absolute", warning_band=3.0, critical_band=6.0),
            notes=(
                "STFT delta between banks, time-smoothed over "
                "STFT_BANK_BALANCE_SMOOTHING_SECONDS so routine transients "
                "(round-robin sampling skew) don't trip it — only sustained "
                "imbalance does. Expected ~0 everywhere; a non-zero intercept "
                "means bank-specific fuel bias. Fed by "
                "``compute_anomaly_signals`` from STFT_B1 - STFT_B2."
            ),
        ),
        Surface1DRecipe(
            name="ltft_bank_balance_vs_rpm",
            output="derived:LTFT_BANK_BALANCE",
            output_unit="%",
            x_column="obd:RPM",
            x_axis_name="rpm",
            x_axis_unit="rpm",
            fit_method="poly",
            degree=1,
            min_samples=300,
            min_rpm=ENGINE_RUNNING_MIN_RPM,
            deviation=DeviationConfig(kind="absolute", warning_band=4.0, critical_band=8.0),
            notes=(
                "LTFT delta between banks — the slow / drift channel "
                "(STFT is the fast one). Persistent non-zero indicates "
                "long-term bank asymmetry: gradual injector wear, "
                "exhaust leak, vacuum leak."
            ),
        ),
        Surface1DRecipe(
            name="cat_warmup",
            output="obd:CATALYST_TEMP_B1S1",
            output_unit="degC",
            x_column="obd:RUN_TIME",
            x_axis_name="time_since_start",
            x_axis_unit="s",
            # Cat light-off mirrors coolant warmup — gate on warmup + cold_start
            # for the same reasons (the surface only describes cold-start
            # transients; warm-restart and steady-state shouldn't fit it).
            operating_mode="warmup",
            require_session_events=("cold_start",),
            min_rpm=ENGINE_RUNNING_MIN_RPM,
            fit_method="binned_lookup",
            n_knots=12,
            min_samples=120,
            # 4.6: widened from (30.0, 80.0) — same corpus/rationale as
            # coolant_warmup above (WARMUP_CONTEXT_SESSION_FLOOR).
            deviation=DeviationConfig(kind="absolute", warning_band=45.0, critical_band=115.0),
            notes=(
                "Catalyst light-off curve. Detects cat aging (slower "
                "light-off), exhaust restriction (faster temp rise), "
                "secondary air failure. Sister surface to coolant_warmup."
            ),
        ),
        # ----- Phase 2: DID-dependent surfaces (skip until DIDs are polled) --
        Surface2DRecipe(
            name="injector_pw_vs_rpm_load",
            output="did:ECM_INJECTOR_PW_B1",
            output_unit="raw",  # scale not yet locked; raw counts are stable for anomaly detection
            x_column="obd:RPM",
            y_column="obd:ENGINE_LOAD",
            x_axis_name="rpm",
            y_axis_name="engine_load",
            x_axis_unit="rpm",
            y_axis_unit="%",
            min_samples=500,
            min_rpm=ENGINE_RUNNING_MIN_RPM,  # injectors don't fire KOEO — PW is a frozen zero
            # sigma_floor = 1 raw count (scale constant not yet pinned).
            deviation=DeviationConfig(
                kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=1.0
            ),
            notes=(
                "Direct fuel-delivery surface. Detects injector wear (PW "
                "creeps up over months for same load), fuel pressure drop "
                "(same signature), MAF drift (PW shifts because ECM thinks "
                "less air than reality). Needs ``ECM_INJECTOR_PW_B1`` in "
                "``obd_dids``."
            ),
        ),
        Surface1DRecipe(
            name="injector_pw_warmup",
            output="did:ECM_INJECTOR_PW_B1",
            output_unit="raw",
            x_column="obd:RUN_TIME",
            x_axis_name="time_since_start",
            x_axis_unit="s",
            operating_mode="warmup",
            require_session_events=("cold_start",),
            min_rpm=ENGINE_RUNNING_MIN_RPM,
            fit_method="binned_lookup",
            n_knots=12,
            min_samples=120,
            # reference_std is auto-filled from the fit residuals (raw-count
            # scaling isn't pinned, so it can't be declared upfront);
            # sigma_floor = 1 raw count bounds it from below. 4.6: widened
            # from (10.0, 25.0) — same corpus/rationale as coolant_warmup
            # (WARMUP_CONTEXT_SESSION_FLOOR).
            deviation=DeviationConfig(
                kind="sigma", warning_band=15.0, critical_band=38.0, sigma_floor=1.0
            ),
            notes=(
                "Cold-start enrichment curve. PW is elevated during warmup "
                "(typically 2-3× nominal) and decays smoothly as coolant "
                "rises. Detects CTS sensor drift (wrong enrichment), "
                "stuck-open injector, prime-pulse failure."
            ),
        ),
        Surface1DRecipe(
            name="afr_bank_balance_vs_rpm",
            output="derived:AFR_BANK_BALANCE",
            output_unit="V",
            x_column="obd:RPM",
            x_axis_name="rpm",
            x_axis_unit="rpm",
            fit_method="poly",
            degree=1,
            min_samples=300,
            min_rpm=ENGINE_RUNNING_MIN_RPM,  # unheated/idle-off AF sensors read garbage KOEO
            deviation=DeviationConfig(kind="absolute", warning_band=0.05, critical_band=0.15),
            notes=(
                "Wideband AFR sensor voltage delta between banks. Expected "
                "~0V; non-zero indicates bank-specific vacuum leak, "
                "single-injector wear, exhaust leak ahead of one sensor, "
                "single coil pack misfire. Sharpest bank-asymmetry detector "
                "available once AF sensor DIDs are polling."
            ),
        ),
        Surface2DRecipe(
            name="afr_voltage_vs_rpm_load",
            output="did:ECM_AF_SENSOR_V_B1",
            output_unit="V",
            x_column="obd:RPM",
            y_column="obd:ENGINE_LOAD",
            x_axis_name="rpm",
            y_axis_name="engine_load",
            x_axis_unit="rpm",
            y_axis_unit="%",
            min_samples=500,
            min_rpm=ENGINE_RUNNING_MIN_RPM,
            deviation=DeviationConfig(kind="absolute", warning_band=0.10, critical_band=0.25),
            notes=(
                "Wideband AFR sensor voltage map. ~2.15V at stoich (closed "
                "loop), richer (lower V) at WOT, leaner (higher V) under "
                "decel-fuel-cut. Detects O2 sensor aging (whole-surface "
                "drift), systemic rich/lean (offset), closed-loop "
                "oscillation amplitude drift."
            ),
        ),
        Surface1DRecipe(
            name="tps_voltage_per_pct",
            output="derived:TPS_VOLTAGE_PER_PCT",
            output_unit="V/%",
            x_column="obd:THROTTLE_POS",
            x_axis_name="throttle_pct",
            x_axis_unit="%",
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
                "Raw TPS voltage vs ECM-calculated throttle percent. "
                "Expected to be near-linear / constant ratio. Drift detects "
                "TPS calibration shift before any DTC fires."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# 4.6 — warmup confounding: general "how many usable cold-start sessions do
# we have" capability, decoupled from any particular corpus so it's testable
# against a synthetic one and re-runnable against the real one as the corpus
# grows (see the module-level count/decision recorded above).
# ---------------------------------------------------------------------------


def count_usable_cold_start_sessions(
    frame: pd.DataFrame,
    session_events: Mapping[str, set[str]],
    *,
    min_spread: Mapping[str, float] | None = None,
) -> int:
    """How many sessions in ``frame`` both carry a ``cold_start`` event AND
    have enough load/IAT spread during their ``warmup``-mode rows to be
    "usable" training data for a RUN_TIME × context 2D warmup surface.

    A session that starts cold but sits at a near-constant load/ambient for
    its whole warmup window can't teach a context axis anything — it's one
    more sample at the same context bin, not a new operating point. Usable
    means at least one of ``min_spread``'s columns swings by its floor
    within that session's warmup rows. Defaults to
    :data:`WARMUP_CONTEXT_MIN_SPREAD`.
    """
    thresholds = min_spread if min_spread is not None else WARMUP_CONTEXT_MIN_SPREAD
    cold_start_sessions = {s for s, events in session_events.items() if "cold_start" in events}
    if (
        not cold_start_sessions
        or SESSION_COLUMN not in frame.columns
        or MODE_COLUMN not in frame.columns
    ):
        return 0
    warmup = frame[
        (frame[MODE_COLUMN] == "warmup") & frame[SESSION_COLUMN].isin(cold_start_sessions)
    ]
    usable = 0
    for _session_id, rows in warmup.groupby(SESSION_COLUMN, observed=True):
        for column, floor in thresholds.items():
            if column not in rows.columns:
                continue
            values = pd.to_numeric(rows[column], errors="coerce").dropna()
            if len(values) < 2:
                continue
            if float(values.max() - values.min()) >= floor:
                usable += 1
                break
    return usable


def choose_warmup_confounding_strategy(
    usable_cold_start_sessions: int,
) -> Literal["context_axis", "widen_bands"]:
    """4.6 decision rule (operator-approved, OVERNIGHT-QUEUE.md): at least
    :data:`WARMUP_CONTEXT_SESSION_FLOOR` usable cold-start sessions → add a
    context axis; fewer → widen bands instead (what the catalog above does
    today, per the real-corpus count recorded there)."""
    if usable_cold_start_sessions >= WARMUP_CONTEXT_SESSION_FLOOR:
        return "context_axis"
    return "widen_bands"


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecipeResult:
    """One recipe's outcome: either a fitted model or a skip reason."""

    name: str
    model: BehaviorModel1D | BehaviorModel2D | None
    skipped: bool = False
    reason: str = ""

    @property
    def fitted(self) -> bool:
        return self.model is not None


def fit_recipe(
    recipe: Recipe,
    frame: pd.DataFrame,
    *,
    sessions: Sequence[str] = (),
    next_version_for: dict[str, int] | None = None,
    session_events: Mapping[str, set[str]] | None = None,
    prior_model: BehaviorModel1D | BehaviorModel2D | None = None,
    regrid: bool = False,
) -> RecipeResult:
    """Run one recipe; return a ``RecipeResult`` (never raises on missing data).

    ``next_version_for`` lets the batch driver pre-compute the version each
    recipe's output should claim — typically one more than any previously
    persisted version of the same-named model. Default is version 1.

    ``session_events`` (when given) maps session_id → set of detected event
    kinds (from each session's classifier sidecar). Rows from sessions
    missing any of ``recipe.require_session_events`` are filtered out
    before fitting — that's the structural fix for "coolant_warmup fit on
    warm-restart sessions", because warm restarts don't contain a
    ``cold_start`` event.

    ``prior_model`` (when given) is the same-named model from the previous
    ``behavior_models.yaml`` — a 2D fit reuses its bin edges instead of
    recomputing quantiles (4.3) unless ``regrid`` is set, and any
    successful fit whose ``training_data_hash`` matches the prior's keeps
    the prior's ``version`` instead of bumping (4.5 — a no-op refit
    shouldn't churn the version).
    """
    version = (next_version_for or {}).get(recipe.name, 1)

    # 0. engine-running gate — applied before everything else so KOEO rows
    # can't reach the fitters OR the 2D auto-edge quantiles.
    sub = frame
    if recipe.min_rpm is not None:
        if RPM_COLUMN not in sub.columns:
            # The gate is load-bearing (KOEO contamination); silently fitting
            # without it would re-introduce the bug it exists to prevent.
            return RecipeResult(
                recipe.name,
                None,
                skipped=True,
                reason=f"no {RPM_COLUMN} column in frame; can't enforce min_rpm gate",
            )
        rpm = pd.to_numeric(sub[RPM_COLUMN], errors="coerce")
        sub = sub[rpm >= recipe.min_rpm]
        if sub.empty:
            return RecipeResult(
                recipe.name,
                None,
                skipped=True,
                reason=f"no rows with {RPM_COLUMN} >= {recipe.min_rpm} (engine-running gate)",
            )

    # 1. operating_mode filter (cheap, scope-narrowing)
    if recipe.operating_mode is not None:
        if MODE_COLUMN not in sub.columns:
            return RecipeResult(
                recipe.name, None, skipped=True, reason=f"no {MODE_COLUMN} column in frame"
            )
        sub = sub[sub[MODE_COLUMN] == recipe.operating_mode]
        if sub.empty:
            return RecipeResult(
                recipe.name,
                None,
                skipped=True,
                reason=f"no rows in operating_mode={recipe.operating_mode!r}",
            )

    # 1b. require_session_events filter — drop rows from sessions that don't
    # carry every event kind in ``recipe.require_session_events``. Only
    # applies when ``session_events`` is provided; otherwise no-op so
    # callers that haven't wired up the classifier still work.
    if recipe.require_session_events and session_events is not None:
        if SESSION_COLUMN not in sub.columns:
            return RecipeResult(
                recipe.name,
                None,
                skipped=True,
                reason=f"no {SESSION_COLUMN} column in frame; can't enforce event gate",
            )
        required_events = set(recipe.require_session_events)
        eligible_sessions = {
            session_id
            for session_id, events in session_events.items()
            if required_events.issubset(events)
        }
        if not eligible_sessions:
            return RecipeResult(
                recipe.name,
                None,
                skipped=True,
                reason=(
                    f"no session in corpus carries every required event "
                    f"{sorted(required_events)!r} (have {len(session_events)} classified)"
                ),
            )
        sub = sub[sub[SESSION_COLUMN].isin(eligible_sessions)]
        if sub.empty:
            return RecipeResult(
                recipe.name,
                None,
                skipped=True,
                reason=(
                    f"no rows in operating_mode={recipe.operating_mode!r} from sessions "
                    f"that contain required events {sorted(required_events)!r}"
                ),
            )

    # 2. required columns present?
    required = _recipe_columns(recipe)
    missing = [c for c in required if c not in sub.columns]
    if missing:
        return RecipeResult(recipe.name, None, skipped=True, reason=f"missing columns {missing}")

    # 3. enough clean samples?
    clean = sub.dropna(subset=required)
    if len(clean) < recipe.min_samples:
        return RecipeResult(
            recipe.name,
            None,
            skipped=True,
            reason=f"{len(clean)} clean samples < min_samples={recipe.min_samples}",
        )

    # 4. dispatch
    try:
        if isinstance(recipe, Surface1DRecipe):
            model: BehaviorModel1D | BehaviorModel2D = _fit_1d(recipe, clean, sessions, version)
        else:
            model = _fit_2d(
                recipe, clean, sessions, version, prior_model=prior_model, regrid=regrid
            )
    except Exception as exc:  # noqa: BLE001 — log + skip; one bad recipe shouldn't kill a batch
        log.warning(
            "recipe failed to fit",
            extra={"recipe": recipe.name, "error": f"{type(exc).__name__}: {exc}"},
        )
        return RecipeResult(recipe.name, None, skipped=True, reason=str(exc))

    # 5. publish gate (4.1) — a fit that succeeded numerically but is too
    # poor / too sparse to trust is refused the same way a missing-column or
    # too-few-samples input is: a skip with a visible reason, not a silent
    # publish.
    gate_reason = _check_publish_gate(recipe, model)
    if gate_reason is not None:
        log.warning(
            "recipe fit but failed the publish gate",
            extra={"recipe": recipe.name, "reason": gate_reason},
        )
        return RecipeResult(recipe.name, None, skipped=True, reason=gate_reason)

    # 6. no-op-refit version suppression (4.5) — identical training rows
    # (same hash) means an identical fit; don't churn the version just
    # because the refit job ran again.
    if (
        prior_model is not None
        and model.fit.training_data_hash
        and model.fit.training_data_hash == prior_model.fit.training_data_hash
    ):
        model = model.model_copy(update={"version": prior_model.version})

    return RecipeResult(recipe.name, model)


def _check_publish_gate(recipe: Recipe, model: BehaviorModel1D | BehaviorModel2D) -> str | None:
    """4.1 — refuse to publish a numerically-successful fit that isn't
    trustworthy. Returns a skip reason, or ``None`` when the fit clears the
    gate.

    1D: raw R² against the recipe's floor. 2D grid fits don't have a
    meaningful R² (the "model" is just per-cell empirical means —
    ``surface_2d.py``), so their gate is populated-cell coverage instead —
    this is what actually catches the silent all-empty-grid case, since an
    empty grid's R² would be undefined either way.
    """
    if isinstance(recipe, Surface1DRecipe):
        floor = recipe.min_r_squared
        r2 = model.fit.r_squared
        if floor is not None and r2 is not None and r2 < floor:
            return f"r_squared={r2:.3f} below min_r_squared={floor}"
        return None
    assert isinstance(recipe, Surface2DRecipe)
    assert isinstance(model, BehaviorModel2D)
    cells = model.fit.cells
    populated = sum(1 for c in cells if c.count >= model.min_cell_count)
    fraction = populated / len(cells) if cells else 0.0
    if fraction < recipe.min_populated_cell_fraction:
        return (
            f"populated-cell coverage {fraction:.2f} ({populated}/{len(cells)}) below "
            f"min_populated_cell_fraction={recipe.min_populated_cell_fraction}"
        )
    return None


def refit_all(
    recipes: Sequence[Recipe],
    frame: pd.DataFrame,
    *,
    sessions: Sequence[str] = (),
    existing: BehaviorModels | None = None,
    session_events: Mapping[str, set[str]] | None = None,
    catalog: Sequence[Recipe] | None = None,
    regrid_recipes: Collection[str] = (),
) -> tuple[BehaviorModels, list[RecipeResult]]:
    """Fit ``recipes``; return the new ``BehaviorModels`` + per-recipe results.

    ``recipes`` is the set being refit this round. ``catalog`` is the
    canonical full set — used only for retirement decisions: priors whose
    name isn't in ``catalog`` are dropped, and priors that *are* in
    ``catalog`` but *aren't* in ``recipes`` (the caller chose not to refit
    them this round, e.g. ``--recipe foo`` on the CLI) are carried over
    from ``existing`` unchanged. When ``catalog`` is ``None``, ``recipes``
    is treated as the catalog — appropriate for a full refit.

    Models that successfully refit have their ``version`` bumped one above
    the prior, unless the refit was a no-op (identical ``training_data_hash``
    — 4.5) or the recipe failed the publish gate — see :func:`fit_recipe`.
    Models that ran but were skipped (e.g. insufficient samples) carry over
    from ``existing`` unchanged. ``regrid_recipes`` names 2D recipes that
    should recompute auto-edges (and bump ``grid_version``) this round
    instead of reusing the prior model's stored edges (4.3).
    """
    catalog_names = {r.name for r in (catalog if catalog is not None else recipes)}
    refit_names = {r.name for r in recipes}

    next_version_for: dict[str, int] = {}
    carry_over: dict[str, BehaviorModel1D | BehaviorModel2D] = {}
    if existing is not None:
        for prior_model in existing.models:
            name = prior_model.name
            if name not in catalog_names:
                continue  # retired
            carry_over[name] = prior_model
            if name in refit_names:
                next_version_for[name] = prior_model.version + 1

    results: list[RecipeResult] = []
    fitted: dict[str, BehaviorModel1D | BehaviorModel2D] = {}
    for recipe in recipes:
        res = fit_recipe(
            recipe,
            frame,
            sessions=sessions,
            next_version_for=next_version_for,
            session_events=session_events,
            prior_model=carry_over.get(recipe.name),
            regrid=recipe.name in regrid_recipes,
        )
        results.append(res)
        if res.model is not None:
            fitted[recipe.name] = res.model
        elif recipe.name in carry_over:
            # Skipped this round, but we know it from before — keep it
            fitted[recipe.name] = carry_over[recipe.name]

    # Carry over canonical priors the caller didn't refit this round, so
    # `--recipe foo` doesn't clobber the other surfaces on disk.
    for name, prior_model in carry_over.items():
        if name not in fitted and name not in refit_names:
            fitted[name] = prior_model

    # Output order: refit recipes first in their declared order, then any
    # carried-over priors in their original `existing` order.
    ordered: list[str] = []
    seen: set[str] = set()
    for recipe in recipes:
        if recipe.name in fitted and recipe.name not in seen:
            ordered.append(recipe.name)
            seen.add(recipe.name)
    if existing is not None:
        for prior_model in existing.models:
            if prior_model.name in fitted and prior_model.name not in seen:
                ordered.append(prior_model.name)
                seen.add(prior_model.name)

    schema_version = existing.schema_version if existing is not None else 1
    return BehaviorModels(
        schema_version=schema_version,
        models=[fitted[name] for name in ordered],
    ), results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _recipe_columns(recipe: Recipe) -> list[str]:
    """All input columns a recipe needs present + non-NaN to fit."""
    if isinstance(recipe, Surface1DRecipe):
        return [recipe.x_column, recipe.output]
    return [recipe.x_column, recipe.y_column, recipe.output]


def _fit_1d(
    recipe: Surface1DRecipe,
    frame: pd.DataFrame,
    sessions: Sequence[str],
    version: int,
) -> BehaviorModel1D:
    kwargs: dict[str, object] = dict(
        x_column=recipe.x_column,
        y_column=recipe.output,
        name=recipe.name,
        output=recipe.output,
        output_unit=recipe.output_unit,
        axis_name=recipe.x_axis_name,
        axis_unit=recipe.x_axis_unit,
        domain=recipe.domain,
        sessions=sessions,
        operating_mode=recipe.operating_mode,
        deviation=recipe.deviation,
        version=version,
        notes=recipe.notes,
    )
    if recipe.fit_method == "poly":
        return fit_polynomial(frame, degree=recipe.degree, **kwargs)  # type: ignore[arg-type]
    return fit_binned_lookup(frame, n_knots=recipe.n_knots, **kwargs)  # type: ignore[arg-type]


def _fit_2d(
    recipe: Surface2DRecipe,
    frame: pd.DataFrame,
    sessions: Sequence[str],
    version: int,
    *,
    prior_model: BehaviorModel1D | BehaviorModel2D | None = None,
    regrid: bool = False,
) -> BehaviorModel2D:
    # 4.3 — freeze bin edges across versions: reuse the prior model's stored
    # edges unless the recipe declares fixed edges (never drift, by
    # construction) or the caller explicitly asked to re-grid. Only an
    # explicit re-grid on an auto-edged recipe bumps grid_version — fixed
    # recipe edges are a constant, not something that "drifts" between
    # refits, so there's nothing to bump.
    prior_2d = prior_model if isinstance(prior_model, BehaviorModel2D) else None
    x_edges = _resolve_axis_edges(
        frame[recipe.x_column],
        recipe.x_edges,
        prior_2d.axes[0].bin_edges if prior_2d is not None else None,
        recipe.auto_bin_count,
        regrid,
    )
    y_edges = _resolve_axis_edges(
        frame[recipe.y_column],
        recipe.y_edges,
        prior_2d.axes[1].bin_edges if prior_2d is not None else None,
        recipe.auto_bin_count,
        regrid,
    )
    if prior_2d is None:
        grid_version = 1
    elif recipe.x_edges is None and recipe.y_edges is None and regrid:
        grid_version = prior_2d.grid_version + 1
    else:
        grid_version = prior_2d.grid_version
    return fit_grid_binned_mean(
        frame,
        x_column=recipe.x_column,
        y_column=recipe.y_column,
        output_column=recipe.output,
        name=recipe.name,
        output=recipe.output,
        x_edges=x_edges,
        y_edges=y_edges,
        x_axis_name=recipe.x_axis_name,
        y_axis_name=recipe.y_axis_name,
        x_axis_unit=recipe.x_axis_unit,
        y_axis_unit=recipe.y_axis_unit,
        output_unit=recipe.output_unit,
        sessions=sessions,
        operating_mode=recipe.operating_mode,
        deviation=recipe.deviation,
        version=version,
        notes=recipe.notes,
        min_cell_count=recipe.min_cell_count,
        grid_version=grid_version,
    )


def _resolve_axis_edges(
    column: pd.Series,
    explicit: tuple[float, ...] | None,
    prior_edges: Sequence[float] | None,
    auto_bin_count: int,
    regrid: bool,
) -> list[float]:
    """One axis's edges for a 2D fit (4.3): recipe-declared edges always
    win; otherwise reuse the prior model's edges unless ``regrid`` is set
    (or there's no prior — first fit), in which case recompute quantiles."""
    if explicit is not None:
        return list(explicit)
    if prior_edges is not None and not regrid:
        return list(prior_edges)
    return _auto_edges(column, auto_bin_count)


def _auto_edges(series: pd.Series, n_bins: int) -> list[float]:
    """Quantile-spaced edges, deduplicated and widened by a small slack so
    every observed value lands inside the grid (a value equal to the top
    edge is closed-side-inclusive in the fitter)."""
    values = pd.to_numeric(series, errors="coerce").dropna().to_numpy(dtype=float)
    if values.size == 0:
        raise ValueError(f"can't auto-edge an empty column ({series.name!r})")
    quantiles = np.linspace(0.0, 1.0, n_bins + 1)
    edges = np.unique(np.quantile(values, quantiles))
    if edges.size < 2:
        # All values identical — manufacture a tiny range so fitter doesn't reject
        center = float(edges[0])
        edges = np.array([center - 0.5, center + 0.5])
    return [float(e) for e in edges]


# Helpers consumers may want to inspect
__all__ = [
    "ENGINE_RUNNING_MIN_RPM",
    "RPM_COLUMN",
    "WARMUP_CONTEXT_MIN_SPREAD",
    "WARMUP_CONTEXT_SESSION_FLOOR",
    "Recipe",
    "RecipeResult",
    "Surface1DRecipe",
    "Surface2DRecipe",
    "choose_warmup_confounding_strategy",
    "count_usable_cold_start_sessions",
    "default_recipes",
    "fit_recipe",
    "refit_all",
]
