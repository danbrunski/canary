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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from src.analysis.surface_1d import evaluate_1d
from src.analysis.surface_2d import evaluate_2d
from src.decode.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    DeviationConfig,
    GridBinnedMeanFit,
)
from src.preprocessing.segmenter import MODE_COLUMN

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


# ---------------------------------------------------------------------------
# Scalar API — for the daemon's row-by-row evaluation
# ---------------------------------------------------------------------------


def expected_value(
    model: BehaviorModel1D | BehaviorModel2D,
    operating_point: Mapping[str, float],
) -> float | None:
    """Look up the model's expected output at the given operating point.

    ``operating_point`` is a column-name → value mapping (typically one wide
    row converted to a dict). Returns ``None`` when:

    * ``operating_mode`` is set on the model and ``operating_point[MODE_COLUMN]``
      doesn't match,
    * a required input column is missing or NaN,
    * the point is outside the fit domain / grid extent,
    * (2D only) the grid cell is empty (``count == 0``).
    """
    if not _operating_mode_matches(model, operating_point):
        return None
    if isinstance(model, BehaviorModel1D):
        x = _scalar(operating_point.get(model.axis.channel))
        if x is None:
            return None
        return evaluate_1d(model, x)
    # 2D
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
) -> DeviationResult:
    """Compute ``actual − expected`` and grade it against the deviation bands.

    Returns ``DeviationResult`` with severity ``"ok"`` / ``"warning"`` /
    ``"critical"`` per the model's ``DeviationConfig``. With no
    ``DeviationConfig`` set, severity is always ``"ok"`` (we still return the
    deviation number for logging).
    """
    expected = expected_value(model, operating_point)
    if expected is None or actual is None or _scalar(actual) is None:
        return DeviationResult(expected=expected, actual=actual, deviation=None, severity="ok")
    delta = float(actual) - expected
    severity = _grade_severity(model, operating_point, delta)
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
    has no expectation. The same operating-mode / domain / empty-cell rules
    as the scalar API apply.
    """
    result = np.full(len(frame), np.nan, dtype=float)
    in_mode = _operating_mode_mask(frame, model)
    if not in_mode.any():
        return pd.Series(result, index=frame.index, dtype=float)

    if isinstance(model, BehaviorModel1D):
        column = model.axis.channel
        if column not in frame.columns:
            return pd.Series(result, index=frame.index, dtype=float)
        x_vals = pd.to_numeric(frame[column], errors="coerce").to_numpy()
        for i in np.flatnonzero(in_mode):
            xi = x_vals[i]
            if not np.isfinite(xi):
                continue
            y = evaluate_1d(model, float(xi))
            if y is not None:
                result[i] = y
        return pd.Series(result, index=frame.index, dtype=float)

    # 2D
    cx, cy = model.axes[0].channel, model.axes[1].channel
    if cx not in frame.columns or cy not in frame.columns:
        return pd.Series(result, index=frame.index, dtype=float)
    x_vals = pd.to_numeric(frame[cx], errors="coerce").to_numpy()
    y_vals = pd.to_numeric(frame[cy], errors="coerce").to_numpy()
    for i in np.flatnonzero(in_mode):
        xi, yi = x_vals[i], y_vals[i]
        if not (np.isfinite(xi) and np.isfinite(yi)):
            continue
        cell = evaluate_2d(model, float(xi), float(yi))
        if cell is not None:
            result[i] = cell.mean
    return pd.Series(result, index=frame.index, dtype=float)


def append_deviation_columns(
    frame: pd.DataFrame,
    models: BehaviorModels,
    *,
    output_columns: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """Append ``deviation:<model_name>`` columns to ``frame`` (Task 4.E.7).

    For each model in ``models``, looks up the actual ``output`` value in
    ``frame`` (resolved via ``output_columns`` if the canonical column name
    isn't the model's ``output`` field — e.g. ``ECM_INJECTOR_PW`` →
    ``decoded:ECM_INJECTOR_PW``), computes ``actual − expected`` row by
    row, and writes the result into a new column. NaN where the model has
    no expectation OR the actual value is missing. The frame is copied;
    callers can chain through other preprocessing steps unchanged.

    ``output_columns`` defaults to ``decoded:<output>`` for each model —
    the convention bindings.yaml decoded signals use after ``to_wide``.
    """
    out = frame.copy()
    for model in models.models:
        actual_col = (output_columns or {}).get(model.name) or f"decoded:{model.output}"
        expected = expected_value_column(out, model)
        if actual_col in out.columns:
            actual = pd.to_numeric(out[actual_col], errors="coerce")
            delta = actual - expected
        else:
            log.info(
                "actual column not present; deviation will be NaN",
                extra={"model": model.name, "actual_column": actual_col},
            )
            delta = pd.Series(np.nan, index=out.index, dtype=float)
        out[f"{DEVIATION_COLUMN_PREFIX}{model.name}"] = delta
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


def _operating_mode_matches(
    model: BehaviorModel1D | BehaviorModel2D, point: Mapping[str, object]
) -> bool:
    if model.operating_mode is None:
        return True
    actual_mode = point.get(MODE_COLUMN)
    return actual_mode == model.operating_mode


def _operating_mode_mask(
    frame: pd.DataFrame, model: BehaviorModel1D | BehaviorModel2D
) -> np.ndarray:
    if model.operating_mode is None:
        return np.ones(len(frame), dtype=bool)
    if MODE_COLUMN not in frame.columns:
        return np.zeros(len(frame), dtype=bool)
    return (frame[MODE_COLUMN].astype(object) == model.operating_mode).to_numpy()


def _grade_severity(
    model: BehaviorModel1D | BehaviorModel2D,
    operating_point: Mapping[str, float],
    delta: float,
) -> Severity:
    dev_cfg = model.deviation
    if dev_cfg is None:
        return "ok"

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


def _deviation_score(
    model: BehaviorModel1D | BehaviorModel2D,
    operating_point: Mapping[str, float],
    delta: float,
    dev_cfg: DeviationConfig,
) -> float | None:
    """Score to compare against the bands: absolute |delta| or |delta|/std.

    Returns ``None`` when the relative-mode local std is unavailable
    (empty grid cell, missing reference_std) — caller treats that as "ok".
    """
    abs_delta = abs(delta)
    if dev_cfg.kind == "absolute":
        return abs_delta
    # relative
    local_std = _local_std(model, operating_point, dev_cfg)
    if local_std is None or local_std <= 0:
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


__all__ = [
    "DEVIATION_COLUMN_PREFIX",
    "DeviationResult",
    "Severity",
    "append_deviation_columns",
    "deviation",
    "expected_value",
    "expected_value_column",
    # Re-exported for convenience so callers don't need a second import.
    "BehaviorModels",
    "GridBinnedMeanFit",
]
