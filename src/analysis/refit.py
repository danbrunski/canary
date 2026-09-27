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
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.analysis.behavior_validation import (
    BehaviorValidationDocument,
    DriveSplit,
    UnorderedSessionsError,
    UnverifiedProfileError,
    ValidationContext,
    build_evidence,
    carry_over_flags,
    chronological_split,
    evaluate_rows,
    fit_band_excess_persistence,
    fit_baseline,
    group_drives,
    parent_session,
    piece_time_ranges,
    profile_refusals,
)
from src.analysis.derived import (
    STFT_BANK_BALANCE_SMOOTHING_SECONDS,
    VE_AMBIENT_CHANNEL,
    VE_MANIFOLD_CHANNEL,
)
from src.analysis.surface_1d import fit_binned_lookup, fit_polynomial, fit_robust_baseline
from src.analysis.surface_2d import fit_grid_binned_mean
from src.columns import MODE_COLUMN, SESSION_COLUMN, roc_column
from src.decode.behavior_eligibility import (
    ServingSpec,
    eligibility_reasons,
    eligible_mask,
    reason_code,
    summarize_reasons,
)
from src.decode.behavior_models import (
    CLOSED_LOOP_FUEL_STATES,
    CURRENT_SCHEMA_VERSION,
    BandExcessPersistence,
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    ColdStartRequirement,
    DeviationConfig,
    EngineStateRequirement,
    FuelControlRequirement,
    LocalRangeCondition,
    RateOfChangeCondition,
    RequiredContext,
    ServingEligibility,
    TrainingSelection,
    ValidationCriteria,
    contract_fingerprint_payload,
    fingerprint,
    refuse_non_trainable_sources,
)
from src.operating import ENGINE_RUNNING_MIN_RPM
from src.preprocessing.local_range import append_local_ranges, local_range_column
from src.preprocessing.session_context import (
    StartContextSpec,
    append_start_context,
    start_context_columns,
)
from src.storage.sessions import session_started_at

log = logging.getLogger(__name__)

#: RPM column used by the engine-running gate.
RPM_COLUMN = "obd:RPM"

# ``ENGINE_RUNNING_MIN_RPM`` (imported from ``src.operating``, R01.7) is the
# vehicle_state classifier's engine_running threshold. KOEO rows read RPM == 0
# with wildly off-map outputs (TIMING_ADVANCE ≈ 63° vs ~17° at idle), and an
# unfiltered KOEO-heavy corpus poisons both the fitted cells and the auto-edge
# quantiles — the May 2026 IAT surface shipped a 62.9° ± 5.0 "expectation" in
# its idle cell that would fire critical on every heat-soaked summer idle.

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

#: B05.2 — ``derived:TOTAL_TRIM_B*`` is STFT + LTFT summed in percentage
#: points: an additive index, not the multiplicative fuel correction
#: ``(1 + STFT)(1 + LTFT)`` and not a calibrated injector multiplier.
TOTAL_TRIM_UNIT = "percentage points (additive STFT+LTFT index)"

#: B05.2 — steady-state gates the total-trim surfaces persist. A rate gate
#: reads ``<channel>__roc``: the *net* change over the feature pipeline's 30 s
#: rolling horizon divided by elapsed time, so these allow at most ~300 rpm
#: and ~15 % load of net change across the last 30 s. Measured on the
#: dense-trim capture _capture-tuning-2026-09-21T223734 (closed loop, not
#: warmup, 801 rows): the cam recipes' 75 rpm/s passed 724 rows, 443 of them
#: locally transient (5 s rpm range > 100 or load range > 5 %), STFT sigma
#: 3.5 %; these limits pass 247 rows, 74 locally transient, STFT sigma 1.2 %
#: (the locally-steady figure). A net-change gate cannot see an up-and-back
#: excursion inside the horizon — see
#: data/analyses/2026-09-22-b04-b05-measurements.md (B05.2).
TOTAL_TRIM_STEADY_RATES: Mapping[str, float] = {
    "obd:RPM": 10.0,
    "obd:ENGINE_LOAD": 0.5,
}

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


@dataclass(frozen=True)
class _RecipeBase:
    """Shared recipe fields. Subclasses add fitter-specific knobs."""

    name: str
    output: str  # the column that *carries* the actual value; also the model's `output` name
    output_unit: str = "unknown"
    operating_mode: str | None = None  # restrict the input frame to this mode before fitting
    deviation: DeviationConfig | None = None
    min_samples: int = 50  # below this, skip the recipe rather than fit a brittle surface
    #: Independent drives required after all row/event/mode filters. A long
    #: single drive is not independent evidence of a stable vehicle behavior.
    min_sessions: int = 1
    #: Rows a session must contribute before it counts toward ``min_sessions``.
    min_session_samples: int = 30
    min_validation_coverage: float = 0.5
    max_validation_warning_rate: float = 0.10
    max_validation_critical_rate: float = 0.02
    diagnostic_capabilities: tuple[str, ...] = ()
    diagnostic_limitations: tuple[str, ...] = ()
    #: B01.3 serving rule — rows must belong to an observed engine start
    #: whose coolant was at or below this (see ``ColdStartRequirement``).
    max_initial_coolant_c: float | None = None
    #: Window after a start within which it counts as observed and its first
    #: coolant reading stands in for the initial temperature.
    cold_start_evidence_seconds: float = 60.0
    #: Training-only corpus rule: drop whole sessions whose run-time clock
    #: resets or gaps. Persisted as provenance, never evaluated at serve.
    require_contiguous_runtime: bool = False
    #: B01.4 — explicit fuel-status membership. ``closed_loop_only`` is the
    #: common case and means ``CLOSED_LOOP_FUEL_STATES``; ``fuel_states``
    #: names any other explicit set. Never "any nonzero status".
    closed_loop_only: bool = False
    fuel_states: tuple[int, ...] | None = None
    fuel_status_channel: str = "obd:FUEL_STATUS"
    #: B05.2 — operating modes the model never speaks for (persisted on the
    #: contract, refused at fit and serve alike).
    excluded_operating_modes: tuple[str, ...] = ()
    #: Measured quantities the output's meaning depends on; each tuple is an
    #: any-of group that must be present and fresh on the row.
    required_context: tuple[tuple[str, ...], ...] = ()
    #: B07.6 — persisted local-range steady limits (``LocalRangeCondition``);
    #: the refit builds their context before selecting rows, like serving.
    local_ranges: tuple[LocalRangeCondition, ...] = ()
    #: B05.3 — the model's own event persistence rule. Its limit is fitted on
    #: the training fold only (``fit_band_excess_persistence``) and persisted
    #: on the model; ``None`` leaves the daemon's configured M-of-N gate.
    event_persistence: BandExcessPersistence | None = None
    #: Persisted steady-state constraints. Keys are base telemetry columns;
    #: values are maximum absolute per-second rates over the feature
    #: pipeline's rolling horizon. The fitter applies them and the resulting
    #: model stores the corresponding ``__roc`` conditions for live serving.
    max_abs_rate_of_change: Mapping[str, float] = field(default_factory=dict)
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
    #: B02 acceptance criteria from the vehicle profile's
    #: ``behavior_validation.yaml`` (attach with :func:`attach_criteria`).
    #: ``None`` → the model may publish, but its evidence verdict is
    #: ``insufficient_evidence: no_declared_criteria`` and it cannot alert.
    validation: ValidationCriteria | None = None
    #: 4.1 publish gate (1D only — grid fits' R² isn't well-defined, see
    #: ``Surface2DRecipe.min_populated_cell_fraction``). A fit whose R²
    #: falls below this is skipped rather than published, same as a
    #: missing-column or too-few-samples skip. ``None`` disables the gate.
    min_r_squared: float | None = 0.6

    def fuel_control_states(self) -> tuple[int, ...] | None:
        if self.fuel_states is not None:
            return self.fuel_states
        return CLOSED_LOOP_FUEL_STATES if self.closed_loop_only else None

    def serving_eligibility(self) -> ServingEligibility:
        """The typed contract this recipe persists on its model (B01.3)."""
        fuel_states = self.fuel_control_states()
        return ServingEligibility(
            engine_state=EngineStateRequirement(
                state="engine_running" if self.min_rpm is not None else "any",
                rpm_channel=RPM_COLUMN,
                min_running_rpm=self.min_rpm or ENGINE_RUNNING_MIN_RPM,
            ),
            fuel_control=(
                FuelControlRequirement(
                    channel=self.fuel_status_channel, allowed_states=list(fuel_states)
                )
                if fuel_states is not None
                else None
            ),
            cold_start=(
                ColdStartRequirement(
                    rpm_channel=RPM_COLUMN,
                    min_running_rpm=self.min_rpm or ENGINE_RUNNING_MIN_RPM,
                    max_start_runtime_s=self.cold_start_evidence_seconds,
                    max_initial_coolant_c=self.max_initial_coolant_c,
                )
                if self.max_initial_coolant_c is not None
                else None
            ),
            required_context=[
                RequiredContext(any_of=list(group)) for group in self.required_context
            ],
            excluded_operating_modes=list(self.excluded_operating_modes),
            local_ranges=list(self.local_ranges),
            training_selection=TrainingSelection(
                require_session_events=list(self.require_session_events),
                require_contiguous_runtime=self.require_contiguous_runtime,
                min_sessions=self.min_sessions,
            ),
        )

    def rate_conditions(self) -> list[RateOfChangeCondition]:
        return [
            RateOfChangeCondition(channel=roc_column(channel), max_abs_per_second=limit)
            for channel, limit in self.max_abs_rate_of_change.items()
        ]

    def __post_init__(self) -> None:
        invalid = {
            channel: limit
            for channel, limit in self.max_abs_rate_of_change.items()
            if not channel or limit <= 0
        }
        if invalid:
            raise ValueError(
                f"max_abs_rate_of_change requires named channels and positive limits: {invalid}"
            )
        # H01.1 — refused at construction, before any corpus is read: a
        # recipe naming an external-sensor input can never fit.
        refuse_non_trainable_sources(self.name, self._named_channels())

    def _named_channels(self) -> list[str]:
        """Every telemetry channel this recipe names (axes, output, gates)."""
        axes = [
            str(getattr(self, field_name))
            for field_name in ("x_column", "y_column")
            if getattr(self, field_name, "")
        ]
        return [
            *axes,
            self.output,
            self.fuel_status_channel,
            *self.max_abs_rate_of_change,
            *(channel for group in self.required_context for channel in group),
            *(condition.channel for condition in self.local_ranges),
        ]


@dataclass(frozen=True)
class Surface1DRecipe(_RecipeBase):
    """How to fit one ``BehaviorModel1D`` from a feature frame."""

    kind: Literal["surface_1d"] = "surface_1d"
    x_column: str = ""
    x_axis_name: str = "x"
    x_axis_unit: str = "unknown"
    domain: tuple[float, float] | None = None  # None → derived from x.min()/x.max()
    fit_method: Literal["poly", "binned_lookup", "robust_baseline"] = "poly"
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
    min_cell_sessions: int = 1
    min_cell_dwell_seconds: float = 0.0


Recipe = Surface1DRecipe | Surface2DRecipe


# ---------------------------------------------------------------------------
# Default catalog
# ---------------------------------------------------------------------------


#: Catalog family defaults (R05.7). Applied *under* each recipe's own
#: fields, so a value a recipe declares wins. Until 2026-09 a trailing loop in
#: :func:`default_recipes` ``replace``-d these over every recipe instead,
#: silently discarding any per-recipe minimum.
CATALOG_1D_DEFAULTS: Mapping[str, Any] = {"min_sessions": 3}
CATALOG_2D_DEFAULTS: Mapping[str, Any] = {
    "min_sessions": 3,
    "min_cell_sessions": 2,
    "min_cell_dwell_seconds": 10.0,
}

#: The cold-start transient family: warmup mode, sessions with a cold start
#: from <= 40 degC and a contiguous run-time clock, engine running (KOEO rows
#: have RUN_TIME=0 — not warmup data), and a binned lookup (warmup is
#: sigmoid-ish; polynomials overshoot). Without these gates a fit to "all
#: data" reverts to the R²=0.41 May 2026 surface that flooded the dashboard
#: with warmup-storm anomalies.
WARMUP_1D_FIELDS: Mapping[str, Any] = {
    "operating_mode": "warmup",
    "require_session_events": ("cold_start",),
    "max_initial_coolant_c": 40.0,
    "require_contiguous_runtime": True,
    "min_rpm": ENGINE_RUNNING_MIN_RPM,
    "fit_method": "binned_lookup",
    "min_samples": 120,
}

#: The rpm x calculated-load grid shared by the Mode 01 2D surfaces, running
#: engine only (KOEO timing reads ~63°, trims are frozen zeros, and there is
#: no fill to measure).
RPM_LOAD_2D_FIELDS: Mapping[str, Any] = {
    "x_column": "obd:RPM",
    "y_column": "obd:ENGINE_LOAD",
    "x_axis_name": "rpm",
    "y_axis_name": "engine_load",
    "x_axis_unit": "rpm",
    "y_axis_unit": "%",
    "min_samples": 500,
    "min_rpm": ENGINE_RUNNING_MIN_RPM,
}

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


def _surface_1d(**fields: Any) -> Surface1DRecipe:
    return Surface1DRecipe(**{**CATALOG_1D_DEFAULTS, **fields})


def _surface_2d(**fields: Any) -> Surface2DRecipe:
    return Surface2DRecipe(**{**CATALOG_2D_DEFAULTS, **fields})


def _warmup_1d(**fields: Any) -> Surface1DRecipe:
    return _surface_1d(**{**WARMUP_1D_FIELDS, **fields})


def _rpm_load_2d(**fields: Any) -> Surface2DRecipe:
    return _surface_2d(**{**RPM_LOAD_2D_FIELDS, **fields})


def _sigma(
    sigma_floor: float, *, warning_band: float = 3.0, critical_band: float = 6.0
) -> DeviationConfig:
    """The catalog's usual sigma band (3 / 6); ``sigma_floor`` is the output's LSB."""
    return DeviationConfig(
        kind="sigma",
        warning_band=warning_band,
        critical_band=critical_band,
        sigma_floor=sigma_floor,
    )


def _bank_balance(kind: Literal["stft", "ltft"], mode: str) -> Surface1DRecipe:
    """Mode-conditioned robust bank-balance baseline, closed loop only."""
    channel = f"derived:{kind.upper()}_BANK_BALANCE"
    return _surface_1d(
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
        deviation=_sigma(0.8),
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
        _warmup_1d(
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
        _surface_1d(
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
            deviation=_sigma(25.0),
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
        _warmup_1d(
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
        _rpm_load_2d(
            name="timing_advance_vs_rpm_load",
            output="obd:TIMING_ADVANCE",
            output_unit="degBTDC",
            # sigma_floor = one LSB of PID 0x0E (0.5°/bit): a cell fit on a
            # quantization-flat idle stretch can't alert on single-bit flicker.
            deviation=_sigma(0.5),
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
        _surface_2d(
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
            deviation=_sigma(0.5),
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
        _rpm_load_2d(
            name="fuel_trim_vs_rpm_load",
            output="obd:SHORT_FUEL_TRIM_1",
            output_unit="%",
            # sigma_floor = one LSB of the trim PIDs (100/128 ≈ 0.78 %).
            deviation=_sigma(100.0 / 128.0),
            notes=(
                "STFT B1 surface. Vacuum leaks / injector wear / fuel-quality "
                "drift shift cells off zero."
            ),
        ),
        # ----- Phase 1: derived-signal surfaces (no DIDs required) --------
        _rpm_load_2d(
            name="air_mass_per_cycle_vs_rpm_load",
            output="derived:AIR_MASS_PER_CYCLE",
            output_unit="g/cycle",
            # sigma_floor ≈ one MAF LSB (0.01 g/s) translated to g/cycle at idle.
            deviation=_sigma(0.002),
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
        _rpm_load_2d(
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
            deviation=_sigma(0.5),
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
        _rpm_load_2d(
            name="ve_manifold_vs_rpm_load",
            output=VE_MANIFOLD_CHANNEL,
            output_unit="%",
            deviation=_sigma(0.5),
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
            _rpm_load_2d(
                name=f"total_trim_vs_rpm_load_b{bank}",
                output=f"derived:TOTAL_TRIM_B{bank}",
                output_unit=TOTAL_TRIM_UNIT,
                closed_loop_only=True,
                excluded_operating_modes=("warmup",),
                max_abs_rate_of_change=TOTAL_TRIM_STEADY_RATES,
                # Same trim-PID LSB floor as fuel_trim_vs_rpm_load (100/128 ≈
                # 0.78 %); TOTAL_TRIM sums two channels sharing that LSB, so
                # this is a lower bound on the achievable resolution.
                deviation=_sigma(100.0 / 128.0),
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
        _warmup_1d(
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


def default_recipes(profile: str | None) -> list[Recipe]:
    """The catalog the daemon refits for ``profile``: shared, then the vehicle's own.

    ``profile`` is the vehicle profile slug (required, so no caller can forget
    it and silently fit another vehicle's DID surfaces, or none); ``None``
    asks for the shared catalog only. The profile's recipes come from
    :func:`src.analysis.recipes.profile_recipes` and follow the shared ones.
    """
    from src.analysis.recipes import profile_recipes

    recipes = shared_recipes()
    own = profile_recipes(profile)
    clash = {recipe.name for recipe in recipes} & {recipe.name for recipe in own}
    if clash:
        raise ValueError(f"profile {profile!r} recipes shadow shared ones: {sorted(clash)}")
    return [*recipes, *own]


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
    candidate: BehaviorModel1D | BehaviorModel2D | None = None

    @property
    def fitted(self) -> bool:
        return self.model is not None


class _RecipeSkipError(Exception):
    """A phase of :func:`fit_recipe` refused; caught once and turned into a
    skipped :class:`RecipeResult` carrying ``reason`` (and a ``candidate``
    when a fit exists but failed held-out validation)."""

    def __init__(
        self, reason: str, *, candidate: BehaviorModel1D | BehaviorModel2D | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.candidate = candidate


def _require_sessions(frame: pd.DataFrame, reason: str) -> None:
    """The one session-id precondition; each phase names why it needs them."""
    if SESSION_COLUMN not in frame.columns:
        raise _RecipeSkipError(reason)


def _authorize_captures(
    recipe: Recipe,
    frame: pd.DataFrame,
    allowlists: Mapping[str, Collection[str]] | None,
) -> pd.DataFrame:
    """0. Capture evidence is authorized per recipe.

    General sessions are unrestricted here; isolated captures are present in
    ``allowlists`` only when their manifest explicitly names the recipes they
    may inform.
    """
    if not allowlists:
        return frame
    _require_sessions(
        frame,
        f"no {SESSION_COLUMN} column in frame; can't enforce capture behavior-model allowlists",
    )
    disallowed = {
        session_id
        for session_id, allowed_recipes in allowlists.items()
        if recipe.name not in allowed_recipes
    }
    sub = frame[~frame[SESSION_COLUMN].astype(str).isin(disallowed)]
    if sub.empty:
        raise _RecipeSkipError("no rows from sessions authorized for this behavior-model recipe")
    return sub


def _refuse_unenforceable_contract(
    recipe: Recipe, sub: pd.DataFrame, eligibility: ServingEligibility
) -> None:
    """0a. Refuse when a contract column is absent from the whole frame —
    silently fitting without an enforceable gate would re-introduce the
    contamination it exists to prevent (KOEO timing, open-loop trims)."""
    if eligibility.engine_state.state == "engine_running" and RPM_COLUMN not in sub.columns:
        raise _RecipeSkipError(f"no {RPM_COLUMN} column in frame; can't enforce min_rpm gate")
    if recipe.operating_mode is not None and MODE_COLUMN not in sub.columns:
        raise _RecipeSkipError(f"no {MODE_COLUMN} column in frame")


def _select_event_sessions(
    recipe: Recipe, sub: pd.DataFrame, session_events: Mapping[str, set[str]] | None
) -> pd.DataFrame:
    """1b. Training-only selection: drop rows from sessions that don't carry
    every event kind in ``recipe.require_session_events``.

    Only applies when ``session_events`` is provided; otherwise a no-op so
    callers that haven't wired up the classifier still work. Recorded in
    ``TrainingSelection``; never a serve-time rule.
    """
    if not recipe.require_session_events or session_events is None:
        return sub
    _require_sessions(sub, f"no {SESSION_COLUMN} column in frame; can't enforce event gate")
    required_events = set(recipe.require_session_events)
    eligible_sessions = {
        session_id
        for session_id, events in session_events.items()
        if required_events.issubset(events)
    }
    if not eligible_sessions:
        raise _RecipeSkipError(
            f"no session in corpus carries every required event "
            f"{sorted(required_events)!r} (have {len(session_events)} classified)"
        )
    sub = sub[sub[SESSION_COLUMN].isin(eligible_sessions)]
    if sub.empty:
        raise _RecipeSkipError(
            f"no rows in operating_mode={recipe.operating_mode!r} from sessions "
            f"that contain required events {sorted(required_events)!r}"
        )
    return sub


def _refuse_missing_columns(
    recipe: Recipe,
    sub: pd.DataFrame,
    eligibility: ServingEligibility,
    conditions: Sequence[RateOfChangeCondition],
) -> list[str]:
    """1c/1d/2. Fuel-status, rate-gate and fitted columns must all exist.

    Steady-state conditions are a model contract, not merely a corpus
    cleanup: refuse if a derivative cannot be enforced. Returns the recipe's
    required columns.
    """
    if eligibility.fuel_control is not None and eligibility.fuel_control.channel not in sub.columns:
        raise _RecipeSkipError(
            f"no {eligibility.fuel_control.channel} column; can't enforce closed-loop-only fit"
        )
    for condition in conditions:
        if condition.channel not in sub.columns:
            raise _RecipeSkipError(
                f"missing {condition.channel}; can't enforce steady-state rate gate"
            )
    required = _recipe_columns(recipe)
    missing = [c for c in required if c not in sub.columns]
    if missing:
        raise _RecipeSkipError(f"missing columns {missing}")
    return required


def _append_contexts(sub: pd.DataFrame, eligibility: ServingEligibility) -> pd.DataFrame:
    """2a/2a'. Context the serving contract reads, built from whole sessions.

    Cold-start context is the same engine-start fold the live source and
    batch featurizer run, derived here when the frame was built without a
    model that needed it; B07.6 local-range context is built as serving
    builds it from the live window. Both run before any row-level selection,
    while ``sub`` still holds whole sessions (only session-level selections
    ran so far), so each fold sees the pre-start rows.
    """
    if eligibility.cold_start is not None:
        observed_col, coolant_col = start_context_columns(eligibility.cold_start)
        if observed_col not in sub.columns or coolant_col not in sub.columns:
            _require_sessions(sub, "cold-start eligibility requires session ids")
            sub = append_start_context(
                sub, [StartContextSpec.from_requirement(eligibility.cold_start)]
            )
    missing_ranges = [
        condition
        for condition in eligibility.local_ranges
        if local_range_column(condition) not in sub.columns
    ]
    if missing_ranges:
        absent = [c.channel for c in missing_ranges if c.channel not in sub.columns]
        if absent:
            raise _RecipeSkipError(f"missing {absent}; can't enforce local-range steady gate")
        sub = append_local_ranges(sub, missing_ranges)
    return sub


def _select_contiguous_runtime(recipe: Recipe, sub: pd.DataFrame) -> pd.DataFrame:
    """2b. Training-only: whole sessions with a broken run-time clock are
    poor warmup evidence (provenance in ``TrainingSelection``)."""
    if not recipe.require_contiguous_runtime:
        return sub
    _require_sessions(sub, "runtime-continuity selection requires session ids")
    contiguous: list[str] = []
    for session_id, rows in sub.groupby(SESSION_COLUMN, sort=False):
        ordered = rows.sort_values("timestamp") if "timestamp" in rows else rows
        if "obd:RUN_TIME" not in ordered:
            continue
        runtime = pd.to_numeric(ordered["obd:RUN_TIME"], errors="coerce").dropna()
        if len(runtime) < 2 or bool((runtime.diff().dropna() < 0).any()):
            continue
        if "obd:RUN_TIME__gap" in ordered and bool(ordered["obd:RUN_TIME__gap"].astype(bool).any()):
            continue
        contiguous.append(str(session_id))
    sub = sub[sub[SESSION_COLUMN].astype(str).isin(contiguous)]
    if sub.empty:
        raise _RecipeSkipError("no session satisfies the run-time continuity selection")
    return sub


def _eligible_rows(
    recipe: Recipe, sub: pd.DataFrame, spec: ServingSpec, required: Sequence[str]
) -> pd.DataFrame:
    """3. The per-row serving contract (engine state, fuel control,
    cold-start context, required context, fresh inputs and actual, rate
    gates) — the *same* evaluator the decoder, daemon and replay use, so the
    fit cannot learn from rows its model would refuse to grade.

    B05.3 — plus the profile rule the daemon applies by capture state (R03.12):
    a row stamped as a non-default-profile session is refused by name, and a
    frame carrying capture/tuning pieces with no stamp refuses the recipe."""
    try:
        profile = profile_refusals(sub)
    except UnverifiedProfileError as exc:
        raise _RecipeSkipError(str(exc)) from exc
    reasons = eligibility_reasons(sub, spec, include_actual=True)
    reasons = np.where(pd.isna(profile), reasons, profile).astype(object)
    eligible_rows = eligible_mask(reasons)
    if not eligible_rows.any():
        codes = {reason_code(str(reason)) for reason in reasons if reason is not None}
        if codes == {"operating_mode_mismatch"}:
            detail = f"no rows in operating_mode={recipe.operating_mode!r}"
        elif codes == {"engine_not_running"}:
            detail = f"no rows with {RPM_COLUMN} >= {recipe.min_rpm} (engine-running gate)"
        else:
            detail = f"no rows satisfy the serving contract: {summarize_reasons(reasons)}"
        raise _RecipeSkipError(detail)
    clean = sub.loc[eligible_rows]
    if any(column.startswith("did:") for column in required):
        gap_columns = [f"{column}__gap" for column in required if column.startswith("did:")]
        if any(column not in clean.columns for column in gap_columns):
            raise _RecipeSkipError(
                "DID surface lacks per-channel freshness flags for synchronous fitting"
            )
    if len(clean) < recipe.min_samples:
        raise _RecipeSkipError(f"{len(clean)} clean samples < min_samples={recipe.min_samples}")
    return clean


@dataclass(frozen=True)
class FoldPlan:
    """3b's outcome: the training rows and, when held out, the other folds."""

    clean: pd.DataFrame
    clean_sessions: tuple[str, ...]
    split: DriveSplit | None
    test_rows: pd.DataFrame
    calibration_rows: pd.DataFrame


def _plan_folds(
    recipe: Recipe,
    sub: pd.DataFrame,
    clean: pd.DataFrame,
    *,
    sessions: Sequence[str],
    validation: ValidationContext,
    prior_model: BehaviorModel1D | BehaviorModel2D | None,
    regrid: bool,
) -> FoldPlan:
    """3b. B02.3 — grouped folds by independent physical drive, ordered by
    parsed time. Pieces of one drive (captures, ``#segment`` splits) never
    cross folds; bin edges, bands and the baseline come from train only."""
    clean_sessions: tuple[str, ...] = tuple(sessions)
    split: DriveSplit | None = None
    test_rows = sub.iloc[0:0]
    calibration_rows = sub.iloc[0:0]
    wants_holdout = recipe.min_sessions > 1 or recipe.validation is not None
    if SESSION_COLUMN in clean.columns and not wants_holdout:
        # One drive is enough and nothing is held out: no ordering needed.
        # Recorded chronologically where a stamp parses (never by name alone).
        clean_sessions = tuple(
            sorted(
                {str(piece) for piece in clean[SESSION_COLUMN].astype(str).unique()},
                key=_piece_time_key,
            )
        )
    elif SESSION_COLUMN in clean.columns:
        try:
            drives = group_drives(
                piece_time_ranges(sub), join_gap_seconds=validation.join_gap_seconds
            )
        except UnorderedSessionsError as exc:
            raise _RecipeSkipError(str(exc)) from exc
        clean_counts = clean[SESSION_COLUMN].astype(str).value_counts()
        qualifying = [
            drive
            for drive in drives
            if int(sum(clean_counts.get(piece, 0) for piece in drive.pieces))
            >= recipe.min_session_samples
        ]
        if len(qualifying) < recipe.min_sessions:
            raise _RecipeSkipError(
                f"{len(qualifying)} independent drives with >= "
                f"{recipe.min_session_samples} samples < min_sessions={recipe.min_sessions}"
            )
        by_time = {piece: i for i, drive in enumerate(drives) for piece in drive.pieces}
        if wants_holdout and len(qualifying) > 1:
            criteria = recipe.validation
            n_calibration = criteria.calibration_drives if criteria is not None else 0
            n_test = criteria.min_validation_drives if criteria is not None else 1
            # Never train on nothing: shrink the held-out folds to leave one
            # training drive; the evidence then records too few test drives.
            n_calibration = min(n_calibration, len(qualifying) - 2)
            n_test = max(1, min(n_test, len(qualifying) - 1 - max(0, n_calibration)))
            split = chronological_split(
                qualifying,
                n_test=n_test,
                n_calibration=max(0, n_calibration),
                join_gap_seconds=validation.join_gap_seconds,
                excluded=[drive for drive in drives if drive not in qualifying],
            )
            pieces = sub[SESSION_COLUMN].astype(str)
            test_rows = sub[pieces.isin(split.pieces("test"))]
            calibration_rows = sub[pieces.isin(split.pieces("calibration"))]
            clean = clean[clean[SESSION_COLUMN].astype(str).isin(split.pieces("train"))]
            if len(clean) < recipe.min_samples:
                raise _RecipeSkipError(
                    f"{len(clean)} training samples after holding out "
                    f"{len(split.test)} test drive(s) < min_samples={recipe.min_samples}"
                )
            leaked = _prior_grid_leak(prior_model, split, regrid=regrid, recipe=recipe)
            if leaked:
                raise _RecipeSkipError(
                    "prior grid edges were fitted on test drive piece(s) "
                    f"{', '.join(leaked)}; re-grid (--regrid) so test drives cannot "
                    "choose the bins"
                )
        clean_sessions = tuple(
            sorted(
                {str(piece) for piece in clean[SESSION_COLUMN].astype(str).unique()},
                key=lambda piece: (by_time.get(piece, len(by_time)), piece),
            )
        )
    return FoldPlan(clean, clean_sessions, split, test_rows, calibration_rows)


def _keep_prior_version(
    model: BehaviorModel1D | BehaviorModel2D,
    prior_model: BehaviorModel1D | BehaviorModel2D | None,
) -> BehaviorModel1D | BehaviorModel2D:
    """6. No-op-refit version suppression (4.5) — identical training rows
    (same hash) means an identical fit; don't churn the version just because
    the refit job ran again."""
    if (
        prior_model is not None
        and model.fit.training_data_hash
        and model.fit.training_data_hash == prior_model.fit.training_data_hash
        and model.conditions == prior_model.conditions
        and model.eligibility == prior_model.eligibility
        and model.event_persistence == prior_model.event_persistence
        and model.fit.tune_revision == prior_model.fit.tune_revision
        and _same_evidence_context(model, prior_model)
    ):
        return model.model_copy(update={"version": prior_model.version})
    return model


def fit_recipe(
    recipe: Recipe,
    frame: pd.DataFrame,
    *,
    sessions: Sequence[str] = (),
    next_version_for: dict[str, int] | None = None,
    session_events: Mapping[str, set[str]] | None = None,
    prior_model: BehaviorModel1D | BehaviorModel2D | None = None,
    regrid: bool = False,
    tune_revision: str | None = None,
    capture_recipe_allowlists: Mapping[str, Collection[str]] | None = None,
    validation_context: ValidationContext | None = None,
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

    B02 — when the recipe declares criteria (``recipe.validation``) or needs
    several sessions, rows are split into grouped train/calibration/test
    folds by independent physical drive (parsed time, never directory
    names), the model is fitted on train only, and the untouched test drives
    produce the :class:`ValidationEvidence` that ``serving_status`` reads.
    ``validation_context`` carries the event policy, decoder identity, mode
    taxonomy and provenance recorded with that evidence.
    """
    version = (next_version_for or {}).get(recipe.name, 1)
    validation = validation_context or ValidationContext()
    try:
        return RecipeResult(
            recipe.name,
            _fit_recipe_phases(
                recipe,
                frame,
                version=version,
                validation=validation,
                sessions=sessions,
                session_events=session_events,
                prior_model=prior_model,
                regrid=regrid,
                tune_revision=tune_revision,
                capture_recipe_allowlists=capture_recipe_allowlists,
            ),
        )
    except _RecipeSkipError as skip:
        return RecipeResult(
            recipe.name, None, skipped=True, reason=skip.reason, candidate=skip.candidate
        )


def _fit_recipe_phases(
    recipe: Recipe,
    frame: pd.DataFrame,
    *,
    version: int,
    validation: ValidationContext,
    sessions: Sequence[str],
    session_events: Mapping[str, set[str]] | None,
    prior_model: BehaviorModel1D | BehaviorModel2D | None,
    regrid: bool,
    tune_revision: str | None,
    capture_recipe_allowlists: Mapping[str, Collection[str]] | None,
) -> BehaviorModel1D | BehaviorModel2D:
    """:func:`fit_recipe`'s phases, in order; any of them raises :class:`_RecipeSkipError`."""
    sub = _authorize_captures(recipe, frame, capture_recipe_allowlists)
    # B01.3/B01.4 — the serving contract this recipe will persist.
    spec = recipe_serving_spec(recipe)
    eligibility = recipe.serving_eligibility()
    conditions = list(spec.conditions)
    _refuse_unenforceable_contract(recipe, sub, eligibility)
    sub = _select_event_sessions(recipe, sub, session_events)
    required = _refuse_missing_columns(recipe, sub, eligibility, conditions)
    sub = _append_contexts(sub, eligibility)
    sub = _select_contiguous_runtime(recipe, sub)
    clean = _eligible_rows(recipe, sub, spec, required)
    plan = _plan_folds(
        recipe,
        sub,
        clean,
        sessions=sessions,
        validation=validation,
        prior_model=prior_model,
        regrid=regrid,
    )

    # 4. dispatch
    try:
        if isinstance(recipe, Surface1DRecipe):
            model: BehaviorModel1D | BehaviorModel2D = _fit_1d(
                recipe, plan.clean, plan.clean_sessions, version
            )
        else:
            model = _fit_2d(
                recipe,
                plan.clean,
                plan.clean_sessions,
                version,
                prior_model=prior_model,
                regrid=regrid,
            )
    except Exception as exc:  # noqa: BLE001 — log + skip; one bad recipe shouldn't kill a batch
        log.warning(
            "recipe failed to fit",
            extra={"recipe": recipe.name, "error": f"{type(exc).__name__}: {exc}"},
        )
        raise _RecipeSkipError(str(exc)) from exc

    model = model.model_copy(
        update={
            "conditions": conditions,
            "eligibility": eligibility,
            "fit": model.fit.model_copy(update={"tune_revision": tune_revision}),
        }
    )
    model = _fit_event_persistence(recipe, model, sub, plan, spec, validation)

    if plan.split is not None:
        validation_reason, model = _validate_held_out(
            recipe,
            model,
            split=plan.split,
            test_rows=plan.test_rows,
            calibration_rows=plan.calibration_rows,
            training_rows=plan.clean,
            spec=spec,
            context=validation,
            tune_revision=tune_revision,
        )
        if validation_reason is not None:
            raise _RecipeSkipError(validation_reason, candidate=model)

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
        raise _RecipeSkipError(gate_reason)

    return _keep_prior_version(model, prior_model)


def _fit_event_persistence(
    recipe: Recipe,
    model: BehaviorModel1D | BehaviorModel2D,
    sub: pd.DataFrame,
    plan: FoldPlan,
    spec: ServingSpec,
    validation: ValidationContext,
) -> BehaviorModel1D | BehaviorModel2D:
    """4b. B05.3 — fit the recipe's event persistence on the training fold.

    Every row of the training drives is scored through the serving contract
    (refused rows included, so the accumulation freezes where serving would),
    in time order per drive group; test and calibration drives never enter.
    A fold with no excursion beyond the band refuses the recipe by name.
    """
    rule = recipe.event_persistence
    if rule is None:
        return model
    if plan.split is not None and SESSION_COLUMN in sub.columns:
        train_pieces = plan.split.pieces("train")
        rows = sub[sub[SESSION_COLUMN].astype(str).isin(train_pieces)]
        drive_of_piece = {
            piece: drive.drive_id for drive in plan.split.train for piece in drive.pieces
        }
    else:
        rows = sub
        drive_of_piece = {}
    train_ev = evaluate_rows(
        model, rows, spec=spec, interval_seconds=validation.policy.interval_seconds
    )
    groups = np.asarray(
        [drive_of_piece.get(str(piece), parent_session(str(piece))) for piece in train_ev.pieces],
        dtype=object,
    )
    fitted = fit_band_excess_persistence(rule, train_ev, groups)
    if isinstance(fitted, str):
        raise _RecipeSkipError(fitted)
    return model.model_copy(update={"event_persistence": fitted})


def recipe_serving_spec(recipe: Recipe) -> ServingSpec:
    """The serving contract a recipe persists, before any model exists.

    Also the row-selection contract for reproducing a legacy artifact that
    predates persisted contracts (B02.1): its catalog recipe says which rows
    it was fitted and validated on.
    """
    return ServingSpec(
        operating_mode=recipe.operating_mode,
        conditions=tuple(recipe.rate_conditions()),
        eligibility=recipe.serving_eligibility(),
        inputs=tuple(_recipe_columns(recipe)[:-1]),
        # The refit reads ``recipe.output`` verbatim as its column.
        output=recipe.output,
    )


def legacy_reproduction_spec(
    recipe: Recipe, model: BehaviorModel1D | BehaviorModel2D
) -> tuple[ServingSpec, str | None]:
    """Row-selection contract for a legacy artifact (no persisted contract).

    Its catalog recipe says which rows it was fitted and validated on. When
    the recipe has since been retargeted to a different output or axes
    (B04.5 moved ``ve_vs_rpm_load`` off the legacy ``derived:VE_PERCENT``),
    the recipe's row gates still apply but the model's *own* output and axes
    are what it predicts, so they replace the recipe's — and the second
    element names that substitution so a report cannot present the artifact
    as the retargeted quantity. ``None`` when nothing was substituted.

    Rate gates are the artifact's own persisted ``conditions``, never the
    recipe's, and the recipe's local-range gates are dropped (no legacy
    artifact was fitted under one): a gate added to the recipe after the fit (B07.6 gave warm idle a
    steady-rpm gate on 2026-09-23) was not applied to the rows the artifact
    was fitted and validated on, so reproducing it with the new gate would
    silently re-select its held-out rows.
    """
    spec = recipe_serving_spec(recipe)
    model_inputs = (
        (model.axis.channel,)
        if isinstance(model, BehaviorModel1D)
        else tuple(axis.channel for axis in model.axes)
    )
    notes: list[str] = []
    model_conditions = tuple(model.conditions)
    if spec.conditions != model_conditions:
        notes.append(
            f"recipe now gates {[c.channel for c in spec.conditions]}; artifact reproduced "
            f"with its own fitted gates {[c.channel for c in model_conditions]}"
        )
        spec = replace(spec, conditions=model_conditions)
    eligibility = spec.eligibility
    if eligibility is not None and eligibility.local_ranges:
        # Local-range gates (B07.6, 2026-09-23) postdate every legacy artifact.
        notes.append(
            "recipe now gates local ranges on "
            f"{[c.channel for c in eligibility.local_ranges]}; not applied to an artifact "
            "fitted before them"
        )
        spec = replace(spec, eligibility=eligibility.model_copy(update={"local_ranges": []}))
    if spec.output != model.output or spec.inputs != model_inputs:
        notes.append(
            f"recipe now targets {spec.output} on {list(spec.inputs)}; "
            f"artifact evaluated on its own {model.output} on {list(model_inputs)}"
        )
        spec = replace(spec, output=model.output, inputs=model_inputs)
    return spec, ("; ".join(notes) or None)


def _piece_time_key(piece: str) -> tuple[int, str, str]:
    started = session_started_at(parent_session(piece))
    return (0, started.isoformat(), piece) if started is not None else (1, "", piece)


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
    evidence_sessions = len(model.fit.sessions) + len(model.fit.calibration_sessions)
    if recipe.min_sessions > 1 and evidence_sessions < recipe.min_sessions:
        return (
            f"independent evidence session count {evidence_sessions} below "
            f"min_sessions={recipe.min_sessions}"
        )
    if isinstance(recipe, Surface1DRecipe):
        # A fit-quality sanity floor for *publication* only. Alerting never
        # rests on training R²: without passed held-out evidence the model is
        # diagnostic-only whatever this says (``serving_status``, B02.7).
        floor = recipe.min_r_squared
        r2 = (
            model.fit.validation_r_squared
            if model.fit.validation_r_squared is not None
            else model.fit.r_squared
        )
        if floor is not None and r2 is not None and r2 < floor:
            return f"r_squared={r2:.3f} below min_r_squared={floor}"
        return None
    assert isinstance(recipe, Surface2DRecipe)
    assert isinstance(model, BehaviorModel2D)
    cells = model.fit.cells
    populated = sum(
        1
        for cell in cells
        if cell.count >= model.min_cell_count
        and cell.session_count >= recipe.min_cell_sessions
        and cell.dwell_seconds >= recipe.min_cell_dwell_seconds
    )
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
    tune_revision: str | None = None,
    capture_recipe_allowlists: Mapping[str, Collection[str]] | None = None,
    validation_context: ValidationContext | None = None,
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

    B02.7 — every prior that survives as a carry-over (not refit this round,
    or refit but skipped) is checked against the current recipe contract,
    decoder identity, mode taxonomy and declared criteria. A mismatch adds a
    named ``publication_flags`` entry (so ``serving_status`` reports it
    diagnostic-only on load and hot reload); its evidence is never rewritten.
    """
    context = validation_context or ValidationContext()
    catalog_names = {r.name for r in (catalog if catalog is not None else recipes)}
    refit_names = {r.name for r in recipes}

    next_version_for: dict[str, int] = {}
    carry_over: dict[str, BehaviorModel1D | BehaviorModel2D] = {}
    if existing is not None:
        catalog_by_name = {
            recipe.name: recipe for recipe in (catalog if catalog is not None else recipes)
        }
        for prior_model in existing.models:
            name = prior_model.name
            if name not in catalog_names:
                continue  # retired
            recipe_for_prior = catalog_by_name[name]
            gate_reason = _check_publish_gate(recipe_for_prior, prior_model)
            if gate_reason is not None:
                log.warning("retiring prior behavior model %s: %s", name, gate_reason)
                continue
            flags = carry_over_flags(
                prior_model,
                recipe_contract=recipe_contract_fingerprint(recipe_for_prior),
                criteria=recipe_for_prior.validation,
                context=context,
            )
            new_flags = [flag for flag in flags if flag not in prior_model.publication_flags]
            if new_flags:
                log.warning("carried-over behavior model %s loses trust: %s", name, new_flags)
                prior_model = prior_model.model_copy(
                    update={"publication_flags": [*prior_model.publication_flags, *new_flags]}
                )
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
            tune_revision=tune_revision,
            capture_recipe_allowlists=capture_recipe_allowlists,
            validation_context=context,
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

    # Newly fitted models carry a serving contract (schema 2). A carried-over
    # legacy prior keeps ``eligibility=None`` and therefore stays
    # diagnostic-only: rewriting the document never blesses it (B01.6).
    return BehaviorModels(
        schema_version=CURRENT_SCHEMA_VERSION,
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
        diagnostic_capabilities=recipe.diagnostic_capabilities,
        diagnostic_limitations=recipe.diagnostic_limitations,
    )
    if recipe.fit_method == "poly":
        return fit_polynomial(frame, degree=recipe.degree, **kwargs)  # type: ignore[arg-type]
    if recipe.fit_method == "robust_baseline":
        return fit_robust_baseline(frame, **kwargs)  # type: ignore[arg-type]
    return fit_binned_lookup(frame, n_knots=recipe.n_knots, **kwargs)  # type: ignore[arg-type]


def _validate_held_out(
    recipe: Recipe,
    model: BehaviorModel1D | BehaviorModel2D,
    *,
    split: DriveSplit,
    test_rows: pd.DataFrame,
    calibration_rows: pd.DataFrame,
    training_rows: pd.DataFrame,
    spec: ServingSpec,
    context: ValidationContext,
    tune_revision: str | None,
) -> tuple[str | None, BehaviorModel1D | BehaviorModel2D]:
    """Score the held-out drives with the shared evaluator (B02).

    The test drives' rows are scored whole — every denominator from rows to
    evaluated — and become the model's :class:`ValidationEvidence`; the
    legacy ``validation_*`` fit fields are filled from the same numbers.
    The recipe's publish checks (coverage, per-row warning/critical rate)
    read the calibration fold when one exists, so the final test drives never
    decide anything but the recorded verdict.
    """
    criteria = recipe.validation
    baseline_kind = criteria.baseline if criteria is not None else "training_mode_median"
    modes = (
        training_rows[MODE_COLUMN].astype(str).to_numpy()
        if MODE_COLUMN in training_rows.columns
        else np.full(len(training_rows), "all", dtype=object)
    )
    baseline = fit_baseline(
        pd.to_numeric(training_rows[recipe.output], errors="coerce").to_numpy(dtype=float),
        modes,
        baseline_kind,
    )
    evidence, test_ev = build_evidence(
        model,
        split=split,
        test_frame=test_rows,
        calibration_frame=calibration_rows,
        criteria=criteria,
        baseline=baseline,
        context=context,
        tune_revision=tune_revision,
        spec=spec,
    )
    test = evidence.test
    assert test is not None
    residuals = test_ev.residual[test_ev.evaluated]
    fit = model.fit.model_copy(
        update={
            "calibration_sessions": sorted(split.pieces("test")),
            "validation_sample_count": test.evaluated,
            "validation_coverage": test.coverage if test.coverage is not None else 0.0,
            "validation_warning_rate": (
                test.warning_row_rate if test.warning_row_rate is not None else 1.0
            ),
            "validation_critical_rate": (
                test.critical_row_rate if test.critical_row_rate is not None else 1.0
            ),
            "validation_residual_median": (float(np.median(residuals)) if residuals.size else None),
            "validation_residual_mad": (
                float(np.median(np.abs(residuals - np.median(residuals))))
                if residuals.size
                else None
            ),
            "validation_r_squared": test.r_squared,
        }
    )
    model = model.model_copy(update={"fit": fit, "evidence": evidence})
    gate = evidence.calibration if evidence.calibration is not None else test
    coverage = gate.coverage if gate.coverage is not None else 0.0
    warning_rate = gate.warning_row_rate if gate.warning_row_rate is not None else 1.0
    critical_rate = gate.critical_row_rate if gate.critical_row_rate is not None else 1.0
    if coverage < recipe.min_validation_coverage:
        return (
            f"held-out coverage {coverage:.1%} below "
            f"min_validation_coverage={recipe.min_validation_coverage:.1%}",
            model,
        )
    if critical_rate > recipe.max_validation_critical_rate:
        return (
            f"held-out critical rate {critical_rate:.1%} above "
            f"max_validation_critical_rate={recipe.max_validation_critical_rate:.1%}",
            model,
        )
    if warning_rate > recipe.max_validation_warning_rate:
        return (
            f"held-out warning rate {warning_rate:.1%} above "
            f"max_validation_warning_rate={recipe.max_validation_warning_rate:.1%}",
            model,
        )
    return None, model


def _prior_grid_leak(
    prior_model: BehaviorModel1D | BehaviorModel2D | None,
    split: DriveSplit,
    *,
    regrid: bool,
    recipe: Recipe,
) -> list[str]:
    """Test pieces the prior's reused bin edges were fitted on (B02.3).

    Reused auto-edges are a data-driven choice made on the prior's training
    rows; if those include a drive now in the test fold, the test drive has
    chosen the bins. Recipe-declared edges and an explicit re-grid are safe.
    """
    if (
        regrid
        or not isinstance(prior_model, BehaviorModel2D)
        or not isinstance(recipe, Surface2DRecipe)
        or (recipe.x_edges is not None and recipe.y_edges is not None)
    ):
        return []
    trained = {parent_session(piece) for piece in prior_model.fit.sessions}
    return sorted(piece for piece in split.pieces("test") if parent_session(piece) in trained)


def _same_evidence_context(
    model: BehaviorModel1D | BehaviorModel2D, prior: BehaviorModel1D | BehaviorModel2D
) -> bool:
    """A no-op refit keeps its version only if decoder, taxonomy and criteria
    are unchanged too — the training-row hash cannot see those (B02.7)."""
    new, old = model.evidence, prior.evidence
    if new is None or old is None:
        return new is None and old is None
    return (
        new.decoder_identity == old.decoder_identity
        and sorted(new.mode_taxonomy) == sorted(old.mode_taxonomy)
        and new.criteria == old.criteria
    )


def recipe_contract_fingerprint(recipe: Recipe) -> str:
    """The contract fingerprint the recipe's next fit would carry."""
    return fingerprint(
        contract_fingerprint_payload(
            kind=recipe.kind,
            output=recipe.output,
            inputs=_recipe_columns(recipe)[:-1],
            operating_mode=recipe.operating_mode,
            conditions=recipe.rate_conditions(),
            eligibility=recipe.serving_eligibility(),
            deviation=recipe.deviation,
            event_persistence=recipe.event_persistence,
        )
    )


def attach_criteria(
    recipes: Sequence[Recipe], document: BehaviorValidationDocument | None
) -> list[Recipe]:
    """Attach each recipe's declared criteria from profile data (B02.6)."""
    if document is None:
        return list(recipes)
    unknown = sorted(set(document.recipes) - {recipe.name for recipe in recipes})
    if unknown:
        log.warning("behavior_validation.yaml declares criteria for unknown recipes: %s", unknown)
    return [replace(recipe, validation=document.recipes.get(recipe.name)) for recipe in recipes]


def _validation_r_squared(actual: list[float], expected: list[float]) -> float | None:
    if len(actual) < 2:
        return None
    actual_array = np.asarray(actual, dtype=float)
    denominator = float(np.sum((actual_array - actual_array.mean()) ** 2))
    if denominator <= 0:
        return None
    residual = actual_array - np.asarray(expected, dtype=float)
    return float(1.0 - np.sum(residual**2) / denominator)


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
    adaptive_bin_count = max(
        2,
        min(
            recipe.auto_bin_count,
            int(np.sqrt(max(1, len(frame)) / max(1, recipe.min_cell_count))),
        ),
    )
    x_edges = _resolve_axis_edges(
        frame[recipe.x_column],
        recipe.x_edges,
        prior_2d.axes[0].bin_edges if prior_2d is not None else None,
        adaptive_bin_count,
        regrid,
    )
    y_edges = _resolve_axis_edges(
        frame[recipe.y_column],
        recipe.y_edges,
        prior_2d.axes[1].bin_edges if prior_2d is not None else None,
        adaptive_bin_count,
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
        diagnostic_capabilities=recipe.diagnostic_capabilities,
        diagnostic_limitations=recipe.diagnostic_limitations,
        min_cell_count=recipe.min_cell_count,
        min_cell_sessions=recipe.min_cell_sessions,
        min_cell_dwell_seconds=recipe.min_cell_dwell_seconds,
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
    "attach_criteria",
    "recipe_contract_fingerprint",
    "recipe_serving_spec",
    "Surface1DRecipe",
    "Surface2DRecipe",
    "choose_warmup_confounding_strategy",
    "count_usable_cold_start_sessions",
    "default_recipes",
    "shared_recipes",
    "fit_recipe",
    "refit_all",
]
