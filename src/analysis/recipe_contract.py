"""Recipe contracts: the typed description of one behavior surface (R17).

A recipe says how to fit one ``BehaviorModel1D`` / ``BehaviorModel2D`` and what
serving contract the fitted model persists. This module is the leaf of the
recipe layer: the shared catalog (:mod:`src.analysis.recipes.shared`), the
vehicle catalogs (``src.analysis.recipes.<profile>``) and the builders
(:mod:`src.analysis.recipe_builders`) import it, and the fitting engine
(:mod:`src.analysis.refit`) consumes the recipes it describes. It imports
neither the engine nor a catalog loader (``tests/test_recipe_boundaries.py``).

Everything here is moved verbatim from ``refit.py``: field order, defaults and
validation are the artifact contract, pinned by
``tests/test_recipe_catalog_golden.py``. ``refit`` keeps the old names as
direct aliases.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from src.behavior.behavior_eligibility import ServingSpec
from src.behavior.behavior_models import (
    BandExcessPersistence,
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
from src.columns import roc_column
from src.operating import CLOSED_LOOP_FUEL_STATES, ENGINE_RUNNING_MIN_RPM

#: RPM column used by the engine-running gate.
RPM_COLUMN = "obd:RPM"

# ``ENGINE_RUNNING_MIN_RPM`` (imported from ``src.operating``, R01.7) is the
# vehicle_state classifier's engine_running threshold. KOEO rows read RPM == 0
# with wildly off-map outputs (TIMING_ADVANCE ≈ 63° vs ~17° at idle), and an
# unfiltered KOEO-heavy corpus poisons both the fitted cells and the auto-edge
# quantiles — the May 2026 IAT surface shipped a 62.9° ± 5.0 "expectation" in
# its idle cell that would fire critical on every heat-soaked summer idle.


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


def recipe_columns(recipe: Recipe) -> list[str]:
    """All input columns a recipe needs present + non-NaN to fit."""
    if isinstance(recipe, Surface1DRecipe):
        return [recipe.x_column, recipe.output]
    return [recipe.x_column, recipe.y_column, recipe.output]


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
        inputs=tuple(recipe_columns(recipe)[:-1]),
        # The refit reads ``recipe.output`` verbatim as its column.
        output=recipe.output,
    )


def recipe_contract_fingerprint(recipe: Recipe) -> str:
    """The contract fingerprint the recipe's next fit would carry."""
    return fingerprint(
        contract_fingerprint_payload(
            kind=recipe.kind,
            output=recipe.output,
            inputs=recipe_columns(recipe)[:-1],
            operating_mode=recipe.operating_mode,
            conditions=recipe.rate_conditions(),
            eligibility=recipe.serving_eligibility(),
            deviation=recipe.deviation,
            event_persistence=recipe.event_persistence,
        )
    )


__all__ = [
    "RPM_COLUMN",
    "Recipe",
    "Surface1DRecipe",
    "Surface2DRecipe",
    "recipe_columns",
    "recipe_contract_fingerprint",
    "recipe_serving_spec",
]
