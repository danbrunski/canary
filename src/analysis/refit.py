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
from dataclasses import dataclass, replace
from typing import Literal

import numpy as np
import pandas as pd

from src.analysis.behavior_validation_evaluation import evaluate_rows, fit_band_excess_persistence
from src.analysis.behavior_validation_evidence import (
    ValidationContext,
    build_evidence,
    carry_over_flags,
)
from src.analysis.behavior_validation_inputs import (
    AxisPairingRefusedError,
    UnverifiedProfileError,
    profile_refusals,
)
from src.analysis.behavior_validation_metrics import fit_baseline
from src.analysis.behavior_validation_schema import BehaviorValidationDocument
from src.analysis.recipe_builders import (
    CATALOG_1D_DEFAULTS,
    CATALOG_2D_DEFAULTS,
    RPM_LOAD_2D_FIELDS,
    TOTAL_TRIM_STEADY_RATES,
    WARMUP_1D_FIELDS,
    rpm_load_2d_recipe,
    sigma_deviation,
    surface_1d_recipe,
    surface_2d_recipe,
    warmup_1d_recipe,
)
from src.analysis.recipe_contract import (
    RPM_COLUMN,
    Recipe,
    Surface1DRecipe,
    Surface2DRecipe,
    recipe_columns,
    recipe_contract_fingerprint,
    recipe_serving_spec,
)
from src.analysis.recipes.catalog import default_recipes
from src.analysis.recipes.shared import (
    BANK_BALANCE_MODES,
    STFT_BANK_BALANCE_PERSISTENCE,
    TOTAL_TRIM_UNIT,
    WARM_IDLE_LOCAL_RANGES,
    WARM_IDLE_STEADY_RATES,
    shared_recipes,
)
from src.analysis.splits import (
    DriveSplit,
    UnorderedSessionsError,
    chronological_split,
    group_drives,
    parent_session,
    piece_time_ranges,
)
from src.analysis.surface_1d import fit_binned_lookup, fit_polynomial, fit_robust_baseline
from src.analysis.surface_2d import fit_grid_binned_mean
from src.behavior.behavior_eligibility import (
    ServingSpec,
    eligibility_reasons,
    eligible_mask,
    reason_code,
    summarize_reasons,
)
from src.behavior.behavior_models import (
    CURRENT_SCHEMA_VERSION,
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    RateOfChangeCondition,
    ServingEligibility,
)
from src.behavior.local_range import append_local_ranges, local_range_column
from src.behavior.session_context import (
    StartContextSpec,
    append_start_context,
    start_context_columns,
)
from src.columns import MODE_COLUMN, SESSION_COLUMN
from src.operating import ENGINE_RUNNING_MIN_RPM
from src.storage.sessions import session_started_at

log = logging.getLogger(__name__)

# The recipe definitions moved out of this module (R17): contracts to
# ``recipe_contract``, builders and family defaults to ``recipe_builders``, the
# catalogs to ``recipes/``. The old names stay importable from here as direct
# aliases (compatibility; internal modules import the owners).
_recipe_columns = recipe_columns
_surface_1d = surface_1d_recipe
_surface_2d = surface_2d_recipe
_warmup_1d = warmup_1d_recipe
_rpm_load_2d = rpm_load_2d_recipe
_sigma = sigma_deviation

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
# surface from the shared catalog.)
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
# far under the floor. So the three warmup recipes in the shared catalog
# (:mod:`src.analysis.recipes.shared`) take option (b):
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
    taxonomy and provenance recorded with that evidence, and the session
    directories (``reads``) a recipe whose criteria declare ``axis_pairing``
    is graded from per read (B04.9); without them that recipe is skipped as
    ``axis_pairing_no_reads``. Fitting reads the grid either way.
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

    B04.9 — a recipe whose criteria declare ``axis_pairing`` is graded per
    read (:func:`build_evidence`); the baseline below stays a grid fit, as
    in the evaluator. Missing session directories skip the recipe by name.
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
    try:
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
    except AxisPairingRefusedError as exc:
        # B04.9 — criteria that pair axes per read grade only from the
        # session directories (``context.reads``); the grid is never graded
        # in their place. The fit stays a candidate with no evidence.
        raise _RecipeSkipError(str(exc), candidate=model) from exc
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


# Helpers consumers may want to inspect. The recipe-definition names below
# (contracts, catalog constants, ``default_recipes``/``shared_recipes``,
# fingerprint and serving-spec helpers) are compatibility aliases of their R17
# owners; new code imports the owner.
__all__ = [
    "BANK_BALANCE_MODES",
    "CATALOG_1D_DEFAULTS",
    "CATALOG_2D_DEFAULTS",
    "ENGINE_RUNNING_MIN_RPM",
    "RPM_COLUMN",
    "RPM_LOAD_2D_FIELDS",
    "STFT_BANK_BALANCE_PERSISTENCE",
    "TOTAL_TRIM_STEADY_RATES",
    "TOTAL_TRIM_UNIT",
    "WARMUP_1D_FIELDS",
    "WARM_IDLE_LOCAL_RANGES",
    "WARM_IDLE_STEADY_RATES",
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
