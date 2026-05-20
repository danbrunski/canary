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
from collections.abc import Mapping, Sequence
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
            fit_method="binned_lookup",  # warmup is sigmoid-ish, polys overshoot
            n_knots=12,
            min_samples=120,
            deviation=DeviationConfig(kind="absolute", warning_band=5.0, critical_band=15.0),
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
            fit_method="binned_lookup",
            n_knots=8,
            min_samples=200,
            deviation=DeviationConfig(kind="absolute", warning_band=100.0, critical_band=250.0),
            notes=("Cold-idle RPM target curve. Vacuum leaks / IACV faults show as elevated idle."),
        ),
        Surface1DRecipe(
            name="timing_advance_vs_rpm",
            output="obd:TIMING_ADVANCE",
            output_unit="degBTDC",
            x_column="obd:RPM",
            x_axis_name="rpm",
            x_axis_unit="rpm",
            fit_method="poly",
            degree=2,
            min_samples=300,
            deviation=DeviationConfig(kind="absolute", warning_band=4.0, critical_band=10.0),
            notes="Heavy load-dependence — better as a 2D surface; this is the 1D rollup.",
        ),
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
            deviation=DeviationConfig(kind="relative", warning_band=3.0, critical_band=6.0),
            notes=(
                "Ignition surface. Knock-retard activity shows as drops vs. the baseline cell mean."
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
            deviation=DeviationConfig(kind="relative", warning_band=3.0, critical_band=6.0),
            notes=(
                "STFT B1 surface. Vacuum leaks / injector wear / fuel-quality "
                "drift shift cells off zero."
            ),
        ),
    ]


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
    """
    version = (next_version_for or {}).get(recipe.name, 1)

    # 1. operating_mode filter (cheap, scope-narrowing)
    sub = frame
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
            model = _fit_2d(recipe, clean, sessions, version)
    except Exception as exc:  # noqa: BLE001 — log + skip; one bad recipe shouldn't kill a batch
        log.warning(
            "recipe failed to fit",
            extra={"recipe": recipe.name, "error": f"{type(exc).__name__}: {exc}"},
        )
        return RecipeResult(recipe.name, None, skipped=True, reason=str(exc))

    return RecipeResult(recipe.name, model)


def refit_all(
    recipes: Sequence[Recipe],
    frame: pd.DataFrame,
    *,
    sessions: Sequence[str] = (),
    existing: BehaviorModels | None = None,
    session_events: Mapping[str, set[str]] | None = None,
) -> tuple[BehaviorModels, list[RecipeResult]]:
    """Fit every recipe; return the new ``BehaviorModels`` + per-recipe results.

    Models present in ``existing`` whose recipe successfully refitted have
    their ``version`` bumped one above the prior one. Models that didn't
    refit (skipped) carry over from ``existing`` unchanged — partial
    refits don't lose previously-known surfaces. Models in ``existing``
    that aren't in the recipe set are dropped on the floor (a recipe
    catalog change is how surfaces retire).
    """
    next_version_for: dict[str, int] = {}
    carry_over: dict[str, BehaviorModel1D | BehaviorModel2D] = {}
    if existing is not None:
        prior = {m.name: m for m in existing.models}
        recipe_names = {r.name for r in recipes}
        for name, prior_model in prior.items():
            if name in recipe_names:
                next_version_for[name] = prior_model.version + 1
                carry_over[name] = prior_model
            # else: retired recipe, drop the prior model

    results: list[RecipeResult] = []
    fitted: dict[str, BehaviorModel1D | BehaviorModel2D] = {}
    for recipe in recipes:
        res = fit_recipe(
            recipe,
            frame,
            sessions=sessions,
            next_version_for=next_version_for,
            session_events=session_events,
        )
        results.append(res)
        if res.model is not None:
            fitted[recipe.name] = res.model
        elif recipe.name in carry_over:
            # Skipped this round, but we know it from before — keep it
            fitted[recipe.name] = carry_over[recipe.name]

    schema_version = existing.schema_version if existing is not None else 1
    return BehaviorModels(
        schema_version=schema_version,
        models=[fitted[name] for name in (r.name for r in recipes) if name in fitted],
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
) -> BehaviorModel2D:
    x_edges = recipe.x_edges or _auto_edges(frame[recipe.x_column], recipe.auto_bin_count)
    y_edges = recipe.y_edges or _auto_edges(frame[recipe.y_column], recipe.auto_bin_count)
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
    )


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
    "Recipe",
    "RecipeResult",
    "Surface1DRecipe",
    "Surface2DRecipe",
    "default_recipes",
    "fit_recipe",
    "refit_all",
]
