"""Recipe builders and the catalog's family defaults (R17).

The helpers a catalog uses to declare recipes without repeating the fields
every recipe of a family shares, and the constants those families and the
total-trim surfaces are defined by. Moved verbatim from ``refit.py``; the
shared catalog (:mod:`src.analysis.recipes.shared`) and the vehicle catalogs
import them, the fitting engine does not own them. Imports
:mod:`src.analysis.recipe_contract` only (never the engine or a catalog).

Precedence is unchanged: catalog family defaults sit *under* each recipe's own
fields, so a value a recipe declares wins.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.analysis.recipe_contract import Surface1DRecipe, Surface2DRecipe
from src.behavior.behavior_models import DeviationConfig
from src.operating import ENGINE_RUNNING_MIN_RPM

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

#: Catalog family defaults (R05.7). Applied *under* each recipe's own
#: fields, so a value a recipe declares wins. Until 2026-09 a trailing loop in
#: ``default_recipes`` (now :mod:`src.analysis.recipes.catalog`) ``replace``-d
#: these over every recipe instead, silently discarding any per-recipe minimum.
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


def surface_1d_recipe(**fields: Any) -> Surface1DRecipe:
    return Surface1DRecipe(**{**CATALOG_1D_DEFAULTS, **fields})


def surface_2d_recipe(**fields: Any) -> Surface2DRecipe:
    return Surface2DRecipe(**{**CATALOG_2D_DEFAULTS, **fields})


def warmup_1d_recipe(**fields: Any) -> Surface1DRecipe:
    return surface_1d_recipe(**{**WARMUP_1D_FIELDS, **fields})


def rpm_load_2d_recipe(**fields: Any) -> Surface2DRecipe:
    return surface_2d_recipe(**{**RPM_LOAD_2D_FIELDS, **fields})


def sigma_deviation(
    sigma_floor: float, *, warning_band: float = 3.0, critical_band: float = 6.0
) -> DeviationConfig:
    """The catalog's usual sigma band (3 / 6); ``sigma_floor`` is the output's LSB."""
    return DeviationConfig(
        kind="sigma",
        warning_band=warning_band,
        critical_band=critical_band,
        sigma_floor=sigma_floor,
    )


__all__ = [
    "CATALOG_1D_DEFAULTS",
    "CATALOG_2D_DEFAULTS",
    "RPM_LOAD_2D_FIELDS",
    "TOTAL_TRIM_STEADY_RATES",
    "WARMUP_1D_FIELDS",
    "rpm_load_2d_recipe",
    "sigma_deviation",
    "surface_1d_recipe",
    "surface_2d_recipe",
    "warmup_1d_recipe",
]
