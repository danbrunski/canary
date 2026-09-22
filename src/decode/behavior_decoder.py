"""Runtime decoder for behavior models (Task 4.E.6).

The Phase 4.E inferers fit ``BehaviorModel1D`` / ``BehaviorModel2D``
artifacts; this module is the *runtime* side that looks them up at an
operating point and reports both the expected value and the deviation from
actual. Two paths consume it:

* **Daemon inference (Phase 5):** evaluate models row-by-row, attach
  deviation severity (``info`` / ``warning`` / ``critical``) to alert
  reasoning.
* **Preprocessing (Task 4.E.7):** vectorized batch evaluation over a wide
  DataFrame; the resulting ``deviation:<model_name>`` columns flow into
  ``compute_features`` like any other numeric channel, so the IF + LSTM see
  deviations as ordinary features without changes to those modules.

Both ``expected_value`` and ``deviation`` return ``None`` when the model
can't speak for a point — operating point outside the fit domain, empty
grid cell, ``operating_mode`` mismatch, missing input column. Returning
``None`` rather than fabricating a number is deliberate; the preprocessing
layer turns those into ``NaN`` and the existing scaler / feature pipeline
already handles those correctly.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, cast

import numpy as np
import pandas as pd

from src.analysis.surface_1d import evaluate_1d
from src.analysis.surface_2d import evaluate_2d
from src.decode.accepted_baselines import AcceptedBaselineOverride
from src.decode.behavior_eligibility import (
    ACTUAL_MISSING,
    ACTUAL_STALE,
    OFF_MAP,
    eligibility_reasons,
    eligible_mask,
    point_eligibility,
    point_flag,
    resolve_output_column,
    serving_spec,
)
from src.decode.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    DeviationConfig,
    GridBinnedMeanFit,
    PermittedUse,
    serving_status,
)
from src.preprocessing.features import ROC_SUFFIX, FeatureConfig, compute_rate_of_change
from src.preprocessing.resampler import gap_column
from src.preprocessing.session_context import append_start_context, start_context_specs

log = logging.getLogger(__name__)


Severity = Literal["ok", "warning", "critical"]
DEVIATION_COLUMN_PREFIX = "deviation:"


@dataclass(frozen=True)
class DeviationResult:
    """Per-evaluation outcome.

    ``expected`` and ``actual`` may both be ``None`` when the model didn't
    speak for the operating point; in that case ``deviation`` and
    ``severity`` are also ``None`` / ``"ok"`` and the runtime treats the
    row as "no signal." The scalar caller distinguishes "ok" (model spoke,
    within bounds) from "no signal" by ``expected is None``.
    """

    expected: float | None
    actual: float | None
    deviation: float | None
    severity: Severity
    #: B01.4 — why no deviation was produced (``None`` when one was): the
    #: first unmet eligibility condition from
    #: ``src.decode.behavior_eligibility``, ``off_map``, or a missing/stale
    #: actual. An ineligible point is never an ``ok`` measurement: callers
    #: that count or alert must check this, not ``severity`` alone.
    reason: str | None = None


# ---------------------------------------------------------------------------
# Scalar API — for the daemon's row-by-row evaluation
# ---------------------------------------------------------------------------


def expected_value(
    model: BehaviorModel1D | BehaviorModel2D,
    operating_point: Mapping[str, float],
) -> float | None:
    """Look up the model's expected output at the given operating point.

    ``operating_point`` is a column-name → value mapping (typically one wide
    row converted to a dict). Returns ``None`` whenever
    :func:`explain_expected_value` names a reason — an unmet eligibility
    condition (operating mode, engine state, fuel control, cold-start
    context, required context, stale/missing input, rate gate) or an
    off-map point.
    """
    return explain_expected_value(model, operating_point)[0]


def explain_expected_value(
    model: BehaviorModel1D | BehaviorModel2D,
    operating_point: Mapping[str, object],
) -> tuple[float | None, str | None]:
    """``(expected, reason)`` — exactly one of the two is ``None``."""
    reason = point_eligibility(operating_point, model)
    if reason is not None:
        return None, reason
    expected = _surface_value(model, operating_point)
    if expected is None:
        return None, OFF_MAP
    return expected, None


def _surface_value(
    model: BehaviorModel1D | BehaviorModel2D, operating_point: Mapping[str, object]
) -> float | None:
    if isinstance(model, BehaviorModel1D):
        x = _scalar(operating_point.get(model.axis.channel))
        return None if x is None else evaluate_1d(model, x)
    x = _scalar(operating_point.get(model.axes[0].channel))
    y = _scalar(operating_point.get(model.axes[1].channel))
    if x is None or y is None:
        return None
    cell = evaluate_2d(model, x, y)
    return None if cell is None else cell.mean


def deviation(
    model: BehaviorModel1D | BehaviorModel2D,
    actual: float | None,
    operating_point: Mapping[str, float],
    *,
    override: AcceptedBaselineOverride | None = None,
) -> DeviationResult:
    """Compute ``actual − expected`` and grade it against the deviation bands.

    Returns ``DeviationResult`` with severity ``"ok"`` / ``"warning"`` /
    ``"critical"`` per the model's ``DeviationConfig``. With no
    ``DeviationConfig`` set, severity is always ``"ok"`` (we still return the
    deviation number for logging). When no deviation can be produced,
    ``reason`` names why (B01.4); ``severity`` is then ``"ok"`` only for API
    compatibility and must not be counted as a healthy measurement.

    ``override`` (2.2, online-training-strategy.md Phase 2) is an active,
    non-expired ``behavior_model``-targeted ``AcceptedBaselineOverride`` —
    the caller resolves it via ``AcceptedBaselines.active_override_for``
    (already ``None`` past ``review_by``, so an expired override is
    silently inert here exactly as the doc requires). The center is still
    the model's own fitted ``expected_value`` — an override never
    recenters a behavior_model target, it only contributes the one-sided
    band (``override.to_deviation_config()`` replaces the model's own
    ``deviation`` config for this call only; the persisted model is
    untouched). ``None`` (no override, or the caller didn't resolve one)
    grades against the model's own static ``deviation`` config unchanged —
    the symmetric path's behavior is exactly as before.
    """
    expected, reason = explain_expected_value(model, operating_point)
    if expected is None:
        return DeviationResult(
            expected=None, actual=actual, deviation=None, severity="ok", reason=reason
        )
    actual_value = _scalar(actual)
    if actual_value is None:
        output = serving_spec(model).output
        stale = point_flag(operating_point, gap_column(output))
        return DeviationResult(
            expected=expected,
            actual=actual,
            deviation=None,
            severity="ok",
            reason=f"{ACTUAL_STALE if stale else ACTUAL_MISSING}:{output}",
        )
    delta = actual_value - expected
    if override is not None:
        dev_cfg = override.to_deviation_config()
        local_std = _local_std(model, operating_point, dev_cfg) if dev_cfg.std_scaled else None
        severity = _grade_directional(dev_cfg, delta, local_std)
    else:
        severity = _grade_severity(model, operating_point, delta)
    return DeviationResult(expected=expected, actual=actual, deviation=delta, severity=severity)


def grade_deviation(
    model: BehaviorModel1D | BehaviorModel2D,
    operating_point: Mapping[str, object],
    delta: float,
) -> Severity:
    """Grade an already-computed ``actual − expected`` exactly as
    :func:`deviation` does (the model's own bands, no override). For
    evaluators that computed ``expected`` over a whole frame (B02)."""
    return _grade_severity(model, cast("Mapping[str, float]", operating_point), delta)


def band_half_widths(
    model: BehaviorModel1D | BehaviorModel2D,
    operating_point: Mapping[str, object],
) -> tuple[float | None, float | None]:
    """``(warning, critical)`` band half-widths in *output units* at a point.

    ``absolute`` bands are their own half-width; ``sigma`` bands scale by the
    local std (cell std / ``reference_std``) floored by ``sigma_floor`` —
    the same arithmetic :func:`deviation` grades with. ``None`` where a band
    is unset or the local std is unavailable. A "3 σ" band says nothing about
    resolution until it is expressed like this (B02.5).
    """
    dev_cfg = model.deviation
    if dev_cfg is None:
        return None, None
    point = cast("Mapping[str, float]", operating_point)
    scale = 1.0
    if dev_cfg.kind == "sigma" or (dev_cfg.kind == "directional" and dev_cfg.std_scaled):
        local_std = _local_std(model, point, dev_cfg)
        if local_std is None:
            return None, None
        local_std = max(local_std, dev_cfg.sigma_floor or 0.0)
        if local_std <= 0:
            return None, None
        scale = local_std
    warning = None if dev_cfg.warning_band is None else dev_cfg.warning_band * scale
    critical = None if dev_cfg.critical_band is None else dev_cfg.critical_band * scale
    return warning, critical


def deviation_for_override(
    override: AcceptedBaselineOverride,
    actual: float | None,
) -> DeviationResult:
    """Grade a raw-channel override directly against ``accepted_center``
    (2.2) — there is no fitted surface for a raw channel, so this bypasses
    ``expected_value``/``BehaviorModel`` entirely; ``accepted_center`` (the
    schema requires it for a ``channel`` target) stands in as ``expected``.

    Caller resolves ``override`` via ``AcceptedBaselines.active_override_for``
    first (already ``None`` past ``review_by``) — there's nothing to grade
    against once it's expired, same as the ``deviation()`` behavior_model
    path.
    """
    expected = override.accepted_center
    a = _scalar(actual)
    if expected is None or a is None:
        return DeviationResult(expected=expected, actual=actual, deviation=None, severity="ok")
    delta = a - expected
    dev_cfg = override.to_deviation_config()
    severity = _grade_directional(dev_cfg, delta, local_std=None)
    return DeviationResult(expected=expected, actual=actual, deviation=delta, severity=severity)


# ---------------------------------------------------------------------------
# Vectorized API — for the preprocessing pipeline (Task 4.E.7)
# ---------------------------------------------------------------------------


def expected_value_column(
    frame: pd.DataFrame,
    model: BehaviorModel1D | BehaviorModel2D,
) -> pd.Series:
    """Row-vectorized ``expected_value`` over a wide DataFrame.

    Returns a float series indexed like ``frame`` with NaN where the model
    has no expectation. The same eligibility / domain / empty-cell rules as
    the scalar API apply (one evaluator, B01.4).
    """
    expected, _ = explain_expected_column(frame, model)
    return pd.Series(expected, index=frame.index, dtype=float)


def explain_expected_column(
    frame: pd.DataFrame,
    model: BehaviorModel1D | BehaviorModel2D,
) -> tuple[np.ndarray, np.ndarray]:
    """``(expected, reasons)`` arrays — the frame form of
    :func:`explain_expected_value`, row for row identical to it."""
    result = np.full(len(frame), np.nan, dtype=float)
    reasons = eligibility_reasons(frame, model)
    eligible = eligible_mask(reasons)
    if not eligible.any():
        return result, reasons

    if isinstance(model, BehaviorModel1D):
        x_vals = pd.to_numeric(frame[model.axis.channel], errors="coerce").to_numpy()
        for i in np.flatnonzero(eligible):
            y = evaluate_1d(model, float(x_vals[i]))
            if y is None:
                reasons[i] = OFF_MAP
            else:
                result[i] = y
        return result, reasons

    cx, cy = model.axes[0].channel, model.axes[1].channel
    x_vals = pd.to_numeric(frame[cx], errors="coerce").to_numpy()
    y_vals = pd.to_numeric(frame[cy], errors="coerce").to_numpy()
    for i in np.flatnonzero(eligible):
        cell = evaluate_2d(model, float(x_vals[i]), float(y_vals[i]))
        if cell is None:
            reasons[i] = OFF_MAP
        else:
            result[i] = cell.mean
    return result, reasons


def append_behavior_prerequisites(
    frame: pd.DataFrame,
    models: BehaviorModels,
    *,
    rate_window_seconds: int | None = None,
) -> pd.DataFrame:
    """Build the context behavior eligibility reads, before decoding (B01.2).

    * ``<base>__roc`` for every base channel a persisted rate gate names and
      ``frame`` lacks, via :func:`~src.preprocessing.features.
      compute_rate_of_change` — the one rate-of-change implementation, with
      ``compute_features``' session grouping, horizon and units (it later
      rebuilds identical values for every channel). Only base telemetry is
      differentiated: a gate on a ``__roc`` of a ``__roc``, or on a
      ``deviation:*`` column (behavior depending on behavior), is logged and
      left to refuse as ``rate_unavailable``.
    * ``context:*`` engine-start evidence for cold-start contracts when
      ``frame`` lacks it (batch), folded per session by
      :func:`~src.preprocessing.session_context.append_start_context`. The
      live source supplies its own, which outlives its ring buffer.

    Returns ``frame`` itself when nothing is needed.
    """
    roc_bases: list[str] = []
    for model in models.models:
        for condition in model.conditions:
            base = condition.channel.removesuffix(ROC_SUFFIX)
            if condition.channel == base or condition.channel in frame.columns:
                continue
            if base.endswith(ROC_SUFFIX) or base.startswith(DEVIATION_COLUMN_PREFIX):
                log.warning(
                    "behavior rate gate names a derived/behavior column; not computed",
                    extra={"model": model.name, "channel": condition.channel},
                )
                continue
            if base not in roc_bases:
                roc_bases.append(base)
    out = frame
    if roc_bases:
        config = (
            FeatureConfig()
            if rate_window_seconds is None
            else FeatureConfig(window_seconds=rate_window_seconds)
        )
        out = compute_rate_of_change(out, roc_bases, config=config)
    specs = [
        spec
        for spec in start_context_specs(models)
        if spec.observed_column not in out.columns or spec.start_coolant_column not in out.columns
    ]
    if specs:
        out = append_start_context(out, specs)
    return out


def append_deviation_columns(
    frame: pd.DataFrame,
    models: BehaviorModels,
    *,
    output_columns: Mapping[str, str] | None = None,
    rate_window_seconds: int | None = None,
) -> pd.DataFrame:
    """Append ``deviation:<model_name>`` columns to ``frame`` (Task 4.E.7).

    For each model in ``models``, looks up the actual ``output`` value in
    ``frame`` (resolved via ``output_columns`` if the canonical column name
    isn't the model's ``output`` field — e.g. ``ECM_INJECTOR_PW`` →
    ``decoded:ECM_INJECTOR_PW``), computes ``actual − expected`` row by
    row, and writes the result into a new column. NaN where the model has
    no expectation OR the actual value is missing. The frame is copied;
    callers can chain through other preprocessing steps unchanged.

    Column resolution for each model, in precedence order: an explicit
    ``output_columns[model.name]`` override; else ``model.output`` verbatim
    when it is already a qualified ``source:channel`` name (e.g.
    ``obd:COOLANT_TEMP`` from the refit recipes — it has a ``:``); else the
    legacy ``decoded:<output>`` for a bare CAN signal name (e.g.
    ``ECM_INJECTOR_PW`` → ``decoded:ECM_INJECTOR_PW``).

    Eligibility (B01.4) is the shared evaluator's. The context it reads is
    built here first by :func:`append_behavior_prerequisites` (B01.2), so no
    caller can forget it: ``rate_window_seconds`` must be the rolling horizon
    the models were fitted at (``preprocessing.rolling_window_seconds``;
    ``None`` means the ``FeatureConfig`` default, the same default
    ``compute_features`` uses). The ``deviation:<name>__gap`` flag is set on
    every row where no deviation was produced, whatever the reason.
    """
    out = append_behavior_prerequisites(frame, models, rate_window_seconds=rate_window_seconds)
    if out is frame:
        out = frame.copy()  # never mutate the caller's frame
    new_columns: dict[str, pd.Series] = {}
    for model in models.models:
        actual_col = (output_columns or {}).get(model.name) or resolve_output_column(model.output)
        expected, _ = explain_expected_column(out, model)
        if actual_col in out.columns:
            actual = pd.to_numeric(out[actual_col], errors="coerce").to_numpy(dtype=float)
            delta = actual - expected
            stale = ~np.isfinite(delta)
            actual_gap = gap_column(actual_col)
            if actual_gap in out.columns:
                stale |= out[actual_gap].astype(object).fillna(True).astype(bool).to_numpy()
                delta = np.where(stale, np.nan, delta)
        else:
            log.info(
                "actual column not present; deviation will be NaN",
                extra={"model": model.name, "actual_column": actual_col},
            )
            delta = np.full(len(out), np.nan)
            stale = np.ones(len(out), dtype=bool)
        column = f"{DEVIATION_COLUMN_PREFIX}{model.name}"
        new_columns[column] = pd.Series(delta, index=out.index, dtype=float)
        new_columns[gap_column(column)] = pd.Series(stale, index=out.index, dtype=bool)
    for name, values in new_columns.items():
        out[name] = values
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _scalar(value: object) -> float | None:
    """Cast to float; ``None`` for NaN / missing / non-numeric."""
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]  # any-cast: handled by except
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return f


def _grade_severity(
    model: BehaviorModel1D | BehaviorModel2D,
    operating_point: Mapping[str, float],
    delta: float,
) -> Severity:
    dev_cfg = model.deviation
    if dev_cfg is None:
        return "ok"
    if dev_cfg.kind == "directional":
        local_std = _local_std(model, operating_point, dev_cfg) if dev_cfg.std_scaled else None
        return _grade_directional(dev_cfg, delta, local_std)

    score = _deviation_score(model, operating_point, delta, dev_cfg)
    if score is None:
        return "ok"
    # Bands are inclusive lower-bounds: ``score >= band`` → that severity.
    # "score exactly at the warning_band" should already be a warning; the
    # alternative (>) makes the threshold semantics fiddly to reason about.
    if dev_cfg.critical_band is not None and score >= dev_cfg.critical_band:
        return "critical"
    if dev_cfg.warning_band is not None and score >= dev_cfg.warning_band:
        return "warning"
    return "ok"


def _grade_directional(
    dev_cfg: DeviationConfig,
    delta: float,
    local_std: float | None,
) -> Severity:
    """Grade a signed, already-centered ``delta`` against a directional band
    (2.1/2.2). ``delta`` must already be ``actual − center`` — the model's
    own ``expected_value`` for a behavior_model target, or
    ``accepted_center`` for a raw-channel one; this function doesn't care
    which, it only needs the sign.

    ``worse_direction`` picks the sign that counts as "worsening"; anything
    within ``tolerance`` of center, in *either* direction, is ``"ok"`` — and
    so is anything past tolerance in the *improving* direction
    (``better_direction: ignore`` is the only value today, and this is what
    implements it: a negative worsening-signed score is always ``<=
    tolerance`` since ``tolerance >= 0``). Past tolerance on the worse side,
    grades ``warning``/``critical`` directly off ``warning_band``/
    ``critical_band`` — the same "score >= band" convention every other
    kind uses, measured straight from center (matching the doc's own
    "> center + 3 → warning" phrasing), not from the tolerance edge.
    """
    sign = 1.0 if dev_cfg.worse_direction == "positive" else -1.0
    score = delta * sign
    if dev_cfg.std_scaled:
        # Same degenerate-std handling as sigma: no std → no grading;
        # sigma_floor bounds a present-but-tiny std from below.
        if local_std is None:
            return "ok"
        local_std = max(local_std, dev_cfg.sigma_floor or 0.0)
        if local_std <= 0:
            return "ok"
        score = score / local_std
    tolerance = dev_cfg.tolerance or 0.0
    if score <= tolerance:
        return "ok"
    if dev_cfg.critical_band is not None and score >= dev_cfg.critical_band:
        return "critical"
    if dev_cfg.warning_band is not None and score >= dev_cfg.warning_band:
        return "warning"
    return "ok"


def _deviation_score(
    model: BehaviorModel1D | BehaviorModel2D,
    operating_point: Mapping[str, float],
    delta: float,
    dev_cfg: DeviationConfig,
) -> float | None:
    """Score to compare against the bands: absolute |delta| or |delta|/std.

    Returns ``None`` when the sigma-mode local std is unavailable
    (empty grid cell, missing reference_std) — caller treats that as "ok".
    """
    abs_delta = abs(delta)
    if dev_cfg.kind == "absolute":
        return abs_delta
    # sigma (9.4, formerly "relative") — ``None`` std means off-map /
    # unconfigured (no grading);
    # the sigma_floor then bounds a *present* std from below so a cell fit
    # on quantization-flat data can neither hair-trigger (std below the
    # sensor LSB) nor go silently dead (std exactly 0).
    local_std = _local_std(model, operating_point, dev_cfg)
    if local_std is None:
        return None
    if dev_cfg.sigma_floor is not None:
        local_std = max(local_std, dev_cfg.sigma_floor)
    if local_std <= 0:
        return None
    return abs_delta / local_std


def _local_std(
    model: BehaviorModel1D | BehaviorModel2D,
    operating_point: Mapping[str, float],
    dev_cfg: DeviationConfig,
) -> float | None:
    if isinstance(model, BehaviorModel1D):
        return dev_cfg.reference_std
    # 2D: use the cell's std
    x = _scalar(operating_point.get(model.axes[0].channel))
    y = _scalar(operating_point.get(model.axes[1].channel))
    if x is None or y is None:
        return None
    cell = evaluate_2d(model, x, y)
    if cell is None:
        return None
    return cell.std


def deviation_model_name(column: str) -> str | None:
    """``deviation:<name>[__mean|__std|__roc|__gap]`` → ``<name>``; else ``None``."""
    if not column.startswith(DEVIATION_COLUMN_PREFIX):
        return None
    name = column.removeprefix(DEVIATION_COLUMN_PREFIX)
    for suffix in ("__mean", "__std", "__roc", "__gap"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def withheld_learned_feature_columns(
    columns: Iterable[str], models: BehaviorModels | None
) -> dict[str, str]:
    """``{column: reason}`` for deviation columns a new detector may not select (B03.2).

    A column is withheld when its behavior model's permitted uses exclude
    ``learned_feature`` (a profile policy entry — see
    ``behavior_policy.yaml``). This governs *selection for new training
    only*: :func:`append_deviation_columns` keeps producing every column, so
    an existing manifest trained on one is never starved of its input.
    """
    if models is None:
        return {}
    status_by_name = {model.name: serving_status(model) for model in models.models}
    out: dict[str, str] = {}
    for raw in columns:
        column = str(raw)
        name = deviation_model_name(column)
        status = status_by_name.get(name) if name is not None else None
        if status is None or status.permits(PermittedUse.LEARNED_FEATURE):
            continue
        out[column] = (
            f"behavior model {name!r} is {status.lifecycle}: learned_feature not permitted"
        )
    return out


__all__ = [
    "DEVIATION_COLUMN_PREFIX",
    "deviation_model_name",
    "withheld_learned_feature_columns",
    "DeviationResult",
    "Severity",
    "append_behavior_prerequisites",
    "append_deviation_columns",
    "band_half_widths",
    "grade_deviation",
    "deviation",
    "deviation_for_override",
    "expected_value",
    "expected_value_column",
    "explain_expected_column",
    "explain_expected_value",
    # Re-exported for convenience so callers don't need a second import.
    "BehaviorModels",
    "GridBinnedMeanFit",
]
