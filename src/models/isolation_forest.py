"""Isolation Forest baseline model for Phase 2 anomaly detection.

The trainer consumes the output of the preprocessing pipeline:

    resampled wide frame -> operating-mode labels -> rolling features

It fits one sklearn ``IsolationForest`` per operating mode, persists the
normalizer parameters into a manifest, and exposes a small scoring API for
replay/evaluation code. Scores are oriented so larger values are more
anomalous; sklearn's labels are preserved as ``1`` normal / ``-1`` anomaly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from src.coerce import optional_str as _optional_str
from src.dataframe_utils import require_columns as _require_columns
from src.models.manifest import (
    manifest_path_for,
    read_artifact,
    validate_model_feature_spec,
    write_artifact,
)
from src.models.sequence_training import coerce_timestamp
from src.preprocessing.feature_pipeline import build_feature_spec
from src.preprocessing.normalization import ZScoreScaler
from src.preprocessing.resampler import GAP_SUFFIX, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN
from src.timefmt import format_timestamp as _format_timestamp

ARTIFACT_FORMAT_VERSION = 1
MODEL_TYPE = "isolation_forest"
DEFAULT_RANDOM_STATE = 42
MODEL_MODE_COLUMN = "isolation_forest_model_mode"
SCORE_COLUMN = "isolation_forest_score"
LABEL_COLUMN = "isolation_forest_label"
MISSING_FEATURES_COLUMN = "isolation_forest_missing_features"
#: Fraction of the chosen model's trained features that were NaN on this row.
#: Recorded for every scored row (not just withheld ones) so partial
#: degradation is observable instead of collapsing to a single bool.
MISSING_FEATURE_FRACTION_COLUMN = "isolation_forest_missing_feature_fraction"
#: How much per-row missingness ``score`` will impute through rather than
#: withhold. Above this the row is withheld (flagged, score=inf) and the
#: daemon's fail-safe suppresses the emit.
#:
#: Requiring a *fully* populated row — the original rule — is unsatisfiable on
#: real telemetry: OBD PIDs arrive round-robin at 0.1-0.5 Hz against a 1 Hz
#: resample grid, so on the 2026-07-26 corpus (26,843 rows) exactly 0 rows had
#: every trained feature present. The IF therefore self-suppressed on 100% of
#: ticks, which is the ``missing_feature_rate: 1.0`` observed on the Pi.
#:
#: Missing cells are imputed to the training mean, which is 0 in the scaler's
#: z-space — the least-informative value available, so an imputed feature
#: pushes the verdict toward "normal" rather than fabricating an excursion.
#: That is only honest for a small minority of the vector, hence a low ceiling:
#: measured against the coverage-filtered feature set, 14.5% of rows need no
#: imputation at all and 48.4% fall within 5%. Raising this much further would
#: start scoring rows that are mostly imputed mean, i.e. blind in the opposite
#: direction (always "normal") — the failure mode this ceiling exists to avoid.
MAX_MISSING_FEATURE_FRACTION = 0.05
#: Phase 7.2 — per-mode calibrated decision boundary + verdict. ``score``
#: stays the raw larger-is-worse value; these add the held-out-calibrated
#: threshold and the boolean it produces. ``threshold`` is ``NaN`` and
#: ``anomaly`` is ``NA`` for a mode with no calibrated threshold (callers
#: then fall back to the legacy contamination label).
CALIBRATED_THRESHOLD_COLUMN = "isolation_forest_calibrated_threshold"
CALIBRATED_ANOMALY_COLUMN = "isolation_forest_calibrated_anomaly"

#: Below this many held-out rows a per-mode calibration is not trustworthy;
#: the mode falls back to the legacy contamination-label decision.
_MIN_CALIBRATION_SAMPLES = 10

#: A feature must be non-NaN in at least this fraction of a mode's rows to join
#: that mode's feature set. Excludes foreign-mode deviation surfaces that leak a
#: few rows across a boundary via the rolling window (which would otherwise empty
#: the mode in the ``dropna``). The populated/leaked split is wide (~0.95 vs
#: ~0.03), so the exact cutoff isn't delicate.
_MIN_MODE_FEATURE_COVERAGE = 0.5


class IsolationForestConfigLike(Protocol):
    """Subset of ``src.config.IsolationForestConfig`` needed by the trainer."""

    enabled: bool
    n_estimators: int
    contamination: float
    random_state: int
    calibration_holdout_fraction: float
    target_flag_rate: float
    min_samples_per_mode: int
    n_jobs: int


class MissingModelDependencyError(ImportError):
    """Raised when sklearn/joblib is unavailable in the current environment."""


@dataclass(frozen=True)
class ModeTrainingSummary:
    """Manifest record for one per-mode model."""

    mode: str
    n_samples: int
    contamination: float
    sessions: tuple[str, ...]
    start_timestamp: str | None
    end_timestamp: str | None
    #: Phase 7.2 held-out calibration. ``score_threshold`` is ``None`` when
    #: the mode fell back to the legacy contamination-label decision (too
    #: little holdout, or calibration disabled). All optional so manifests
    #: written before 7.2 still load (the 7.3 model-load gate stays happy).
    score_threshold: float | None = None
    #: Spread (std) of the held-out nominal scores — the scale 7.2 fusion
    #: sizes a fired IF source's severity against (the negated
    #: decision-function threshold is a poor scale; see ``alerts.fusion``).
    score_scale: float | None = None
    holdout_samples: int = 0
    target_flag_rate: float | None = None
    achieved_flag_rate: float | None = None

    @property
    def calibrated(self) -> bool:
        return self.score_threshold is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "n_samples": self.n_samples,
            "contamination": self.contamination,
            "sessions": list(self.sessions),
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "score_threshold": self.score_threshold,
            "score_scale": self.score_scale,
            "holdout_samples": self.holdout_samples,
            "target_flag_rate": self.target_flag_rate,
            "achieved_flag_rate": self.achieved_flag_rate,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ModeTrainingSummary:
        threshold = data.get("score_threshold")
        scale = data.get("score_scale")
        target = data.get("target_flag_rate")
        achieved = data.get("achieved_flag_rate")
        return cls(
            mode=str(data["mode"]),
            n_samples=int(data["n_samples"]),
            contamination=float(data["contamination"]),
            sessions=tuple(str(session) for session in data["sessions"]),
            start_timestamp=_optional_str(data.get("start_timestamp")),
            end_timestamp=_optional_str(data.get("end_timestamp")),
            score_threshold=None if threshold is None else float(threshold),
            score_scale=None if scale is None else float(scale),
            holdout_samples=int(data.get("holdout_samples", 0)),
            target_flag_rate=None if target is None else float(target),
            achieved_flag_rate=None if achieved is None else float(achieved),
        )


@dataclass(frozen=True)
class SkippedModeSummary:
    """Manifest record for a mode training skipped for having too few fit
    rows (Task 2.2) — no model was fit or persisted for it. Distinct from a
    mode absent for lack of any usable feature coverage: this one had rows
    and a feature set, just not enough of either to trust a 200-tree forest."""

    mode: str
    fit_rows: int
    min_samples_required: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fit_rows": self.fit_rows,
            "min_samples_required": self.min_samples_required,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SkippedModeSummary:
        return cls(
            mode=str(data["mode"]),
            fit_rows=int(data["fit_rows"]),
            min_samples_required=int(data["min_samples_required"]),
        )


@dataclass(frozen=True)
class IsolationForestManifest:
    """JSON-safe metadata for a trained Isolation Forest artifact."""

    artifact_format_version: int
    model_type: str
    model_version: str
    created_at: str
    training_data_hash: str
    training_start_timestamp: str | None
    training_end_timestamp: str | None
    training_sessions: tuple[str, ...]
    input_rows: int
    complete_rows: int
    dropped_missing_feature_rows: int
    hyperparams: Mapping[str, Any]
    scaler: Mapping[str, Any]
    schema: Mapping[str, Any]
    modes: tuple[ModeTrainingSummary, ...]
    #: Modes with a usable feature set but too few fit rows to trust a
    #: trained forest (Task 2.2) — no model was persisted for them. Empty
    #: for manifests written before 2.2, or when every mode cleared the floor.
    skipped_modes: tuple[SkippedModeSummary, ...] = ()
    calibration_sessions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_format_version": self.artifact_format_version,
            "model_type": self.model_type,
            "model_version": self.model_version,
            "created_at": self.created_at,
            "training_data_hash": self.training_data_hash,
            "training_start_timestamp": self.training_start_timestamp,
            "training_end_timestamp": self.training_end_timestamp,
            "training_sessions": list(self.training_sessions),
            "input_rows": self.input_rows,
            "complete_rows": self.complete_rows,
            "dropped_missing_feature_rows": self.dropped_missing_feature_rows,
            "hyperparams": dict(self.hyperparams),
            "scaler": dict(self.scaler),
            "schema": dict(self.schema),
            "modes": [mode.to_dict() for mode in self.modes],
            "skipped_modes": [mode.to_dict() for mode in self.skipped_modes],
            "calibration_sessions": list(self.calibration_sessions),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IsolationForestManifest:
        return cls(
            artifact_format_version=int(data["artifact_format_version"]),
            model_type=str(data["model_type"]),
            model_version=str(data["model_version"]),
            created_at=str(data["created_at"]),
            training_data_hash=str(data["training_data_hash"]),
            training_start_timestamp=_optional_str(data.get("training_start_timestamp")),
            training_end_timestamp=_optional_str(data.get("training_end_timestamp")),
            training_sessions=tuple(str(session) for session in data["training_sessions"]),
            input_rows=int(data["input_rows"]),
            complete_rows=int(data["complete_rows"]),
            dropped_missing_feature_rows=int(data["dropped_missing_feature_rows"]),
            hyperparams=dict(data["hyperparams"]),
            scaler=dict(data["scaler"]),
            schema=dict(data["schema"]),
            modes=tuple(ModeTrainingSummary.from_dict(mode) for mode in data["modes"]),
            skipped_modes=tuple(
                SkippedModeSummary.from_dict(mode) for mode in data.get("skipped_modes", [])
            ),
            calibration_sessions=tuple(str(x) for x in data.get("calibration_sessions", [])),
        )


@dataclass(frozen=True)
class TrainedIsolationForest:
    """A fitted per-mode Isolation Forest bundle."""

    models: Mapping[str, Any]
    scaler: ZScoreScaler
    manifest: IsolationForestManifest

    @property
    def feature_columns(self) -> tuple[str, ...]:
        return tuple(str(col) for col in self.manifest.schema["feature_columns"])

    @property
    def mode_column(self) -> str:
        return str(self.manifest.schema["mode_column"])

    @property
    def mode_feature_columns(self) -> dict[str, tuple[str, ...]]:
        """Per-mode feature subsets — each mode's model omits deviation
        surfaces gated to other modes (all-NaN in this mode). Empty for legacy
        artifacts, where callers fall back to the global ``feature_columns``."""
        raw = self.manifest.schema.get("mode_feature_columns", {})
        return {str(mode): tuple(str(c) for c in cols) for mode, cols in raw.items()}

    def _features_for(self, model_key: str) -> tuple[str, ...]:
        """Feature columns the ``model_key`` model was trained on (its mode's
        subset), falling back to the global set for legacy artifacts."""
        return self.mode_feature_columns.get(model_key, self.feature_columns)

    @property
    def score_thresholds(self) -> dict[str, float]:
        """Per-mode held-out-calibrated decision boundaries (7.2).

        Keyed by the model's mode name (the same key ``MODEL_MODE_COLUMN``
        records, so a single-model fallback route still resolves). Modes that
        fell back to the legacy contamination label are absent.
        """
        return {
            mode.mode: mode.score_threshold
            for mode in self.manifest.modes
            if mode.score_threshold is not None
        }

    @property
    def score_scales(self) -> dict[str, float]:
        """Per-mode held-out score spreads (7.2 fusion magnitude scale).

        Keyed like :attr:`score_thresholds`; modes with a degenerate (zero)
        spread are absent, so fusion falls back to the unscaled fired
        severity for them.
        """
        return {
            mode.mode: mode.score_scale
            for mode in self.manifest.modes
            if mode.score_scale is not None
        }

    def score(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Append Isolation Forest scores and labels to ``frame``.

        Rows with missing feature values cannot be evaluated by sklearn, so
        they are explicitly labeled anomalous. That makes sensor dropouts
        visible to the evaluator while preserving sklearn's normal/anomaly
        label convention for complete rows.
        """
        _require_columns(frame, [self.mode_column, *self.feature_columns])

        # A row whose operating mode has no per-mode model used to crash the
        # whole call. That blanked the live detector whenever the segmenter
        # emitted a mode the trainer never produced (sparse OBD → train/serve
        # skew). Degrade instead: a single-model bundle routes every mode
        # through that one model; otherwise the unmodeled rows are withheld
        # (flagged, not scored) so the daemon's missing-feature fail-safe
        # suppresses the emit while health still records the degraded state —
        # symmetric with the LSTM "wait for a clean window" contract.
        fallback_key = next(iter(self.models)) if len(self.models) == 1 else None

        scaled = self.scaler.transform(frame)
        out = frame.copy()
        out[SCORE_COLUMN] = np.nan
        out[LABEL_COLUMN] = pd.Series(pd.NA, index=out.index, dtype="Int64")
        out[MODEL_MODE_COLUMN] = pd.Series(pd.NA, index=out.index, dtype="object")
        out[MISSING_FEATURES_COLUMN] = False
        out[MISSING_FEATURE_FRACTION_COLUMN] = np.nan

        for mode, group in scaled.groupby(self.mode_column, sort=True, dropna=False):
            mode_key = str(mode)
            indexes = group.index
            model_key = mode_key if mode_key in self.models else fallback_key
            # Check missing features against the feature set the chosen model was
            # actually trained on (its mode's subset), not the global union — a
            # deviation surface gated to a different mode is NaN here by design
            # and must not count as a sensor-dropout that suppresses the verdict.
            feats = (
                list(self._features_for(model_key))
                if model_key is not None
                else list(self.feature_columns)
            )
            # Tolerate a *little* missingness instead of demanding a fully
            # populated row. Requiring zero NaN made the detector unable to
            # score at all on real telemetry: OBD arrives round-robin at
            # 0.1-0.5 Hz against a 1 Hz grid, so on the 2026-07-26 corpus
            # 0 of 26,843 rows had every trained feature present, which is
            # exactly the ``missing_feature_rate: 1.0`` the Pi reported. With
            # the coverage-filtered feature set, 14.5% of rows are already
            # complete and 48.4% are within this tolerance — the difference
            # between a detector that never fires and one that works.
            missing_fraction = group[feats].isna().mean(axis=1)
            withheld = missing_fraction > MAX_MISSING_FEATURE_FRACTION
            out.loc[indexes, MISSING_FEATURE_FRACTION_COLUMN] = missing_fraction

            if withheld.any():
                missing_indexes = withheld[withheld].index
                out.loc[missing_indexes, SCORE_COLUMN] = np.inf
                out.loc[missing_indexes, LABEL_COLUMN] = -1
                out.loc[missing_indexes, MODEL_MODE_COLUMN] = mode_key
                out.loc[missing_indexes, MISSING_FEATURES_COLUMN] = True

            complete_indexes = indexes.difference(withheld[withheld].index)
            if complete_indexes.empty:
                continue

            if model_key is None:
                # No model for this mode and no single-model fallback to route
                # through. Withhold rather than crash the tick: flag the rows
                # so the daemon suppresses the emit but health still sees them.
                out.loc[complete_indexes, SCORE_COLUMN] = np.inf
                out.loc[complete_indexes, LABEL_COLUMN] = -1
                out.loc[complete_indexes, MODEL_MODE_COLUMN] = mode_key
                out.loc[complete_indexes, MISSING_FEATURES_COLUMN] = True
                continue

            # Impute the tolerated NaNs to 0.0 — the training mean in the
            # scaler's z-space (see MAX_MISSING_FEATURE_FRACTION). sklearn
            # cannot consume NaN at all, so without this the "tolerated" rows
            # would raise instead of scoring.
            x = scaled.loc[complete_indexes, feats].fillna(0.0).to_numpy(dtype=np.float32)
            model = self.models[model_key]
            # sklearn's decision_function is positive for inliers and
            # negative for outliers. Negating gives a more natural
            # "higher means worse" anomaly score. ``MODEL_MODE_COLUMN`` records
            # the model actually used, so a fallback route is observable.
            out.loc[complete_indexes, SCORE_COLUMN] = -model.decision_function(x)
            out.loc[complete_indexes, LABEL_COLUMN] = model.predict(x).astype(int)
            out.loc[complete_indexes, MODEL_MODE_COLUMN] = model_key

        # Phase 7.2 calibrated decision. Map each row to the threshold of the
        # model actually used (MODEL_MODE_COLUMN, so the single-model fallback
        # resolves), then verdict = score >= threshold. inf scores (missing /
        # unmodeled) stay anomalous. A mode with no calibrated threshold gets
        # NaN/NA here and the emitter falls back to the legacy label.
        thresholds = self.score_thresholds
        if thresholds:
            thr = out[MODEL_MODE_COLUMN].map(thresholds).astype("float64")
        else:
            thr = pd.Series(np.nan, index=out.index, dtype="float64")
        out[CALIBRATED_THRESHOLD_COLUMN] = thr
        known = thr.notna()
        anomaly = pd.Series(pd.NA, index=out.index, dtype="boolean")
        if bool(known.any()):
            anomaly.loc[known] = out.loc[known, SCORE_COLUMN].to_numpy(dtype="float64") >= thr.loc[
                known
            ].to_numpy(dtype="float64")
        out[CALIBRATED_ANOMALY_COLUMN] = anomaly

        return out

    def save(self, artifact_path: Path, manifest_path: Path | None = None) -> None:
        """Write the joblib artifact and its JSON manifest."""
        save_isolation_forest_model(self, artifact_path, manifest_path=manifest_path)


@dataclass(frozen=True)
class _ModeFitPlan:
    """One mode's feature set + time-ordered fit/calibration-holdout split
    (Task 2.4), computed against the *raw* frame before the scaler is fit —
    NaN positions are unaffected by scaling, so this can run first and hand
    the scaler exactly the rows models will train on."""

    feats: list[str]
    complete_index: pd.Index
    fit_index: pd.Index
    holdout_index: pd.Index
    n_holdout: int
    calibrate: bool


def _calibration_split(
    frame: pd.DataFrame,
    complete_index: pd.Index,
    *,
    timestamp_column: str,
    holdout_fraction: float,
) -> tuple[pd.Index, pd.Index, int, bool]:
    """Task 3.5: the per-mode calibration split, extracted out of
    ``fit_isolation_forest``'s per-mode loop (Phase 7.2).

    Time-orders one mode's complete rows and reserves the tail as a
    held-out calibration set the model never trains on, so the per-mode
    score threshold is set on genuinely unseen nominal data rather than the
    training distribution. Falls back to fitting on all rows (legacy
    contamination label, no calibration) when there isn't enough holdout.
    Returns ``(fit_index, holdout_index, n_holdout, calibrate)``.
    """
    ordered_index = (
        pd.to_datetime(frame.loc[complete_index, timestamp_column], utc=True)
        .sort_values(kind="stable")
        .index
    )
    n = len(ordered_index)
    n_holdout = int(np.floor(n * holdout_fraction)) if holdout_fraction > 0 else 0
    calibrate = (n - n_holdout) >= 1 and n_holdout >= _MIN_CALIBRATION_SAMPLES
    if calibrate:
        return ordered_index[: n - n_holdout], ordered_index[n - n_holdout :], n_holdout, True
    return ordered_index, ordered_index[:0], 0, False


def _calibrate_holdout_threshold(
    model: Any,
    holdout_features: pd.DataFrame,
    *,
    target_flag_rate: float,
) -> tuple[float, float | None, float]:
    """Task 3.5: the per-mode holdout-threshold calibration, extracted out
    of ``fit_isolation_forest``'s per-mode loop (Phase 7.2).

    Scores the (never-trained-on) holdout rows with the freshly-fit model
    and picks the ``1 - target_flag_rate`` quantile as the calibrated
    decision boundary. Returns ``(score_threshold, score_scale,
    achieved_flag_rate)`` — ``score_scale`` is the holdout score spread
    (``None`` when degenerate, i.e. every holdout row scored identically).
    """
    hx = holdout_features.to_numpy(dtype=np.float32)
    holdout_scores = -model.decision_function(hx)
    score_threshold = float(np.quantile(holdout_scores, 1.0 - target_flag_rate))
    achieved_flag_rate = float(np.mean(holdout_scores >= score_threshold))
    spread = float(np.std(holdout_scores))
    score_scale = spread if spread > 0.0 else None
    return score_threshold, score_scale, achieved_flag_rate


def fit_isolation_forest(
    frame: pd.DataFrame,
    config: IsolationForestConfigLike,
    *,
    feature_columns: Iterable[str] | None = None,
    feature_spec: Mapping[str, Any] | None = None,
    mode_column: str = MODE_COLUMN,
    timestamp_column: str = TIMESTAMP_COLUMN,
    session_column: str = SESSION_COLUMN,
    model_version: str | None = None,
    fitted_at: datetime | pd.Timestamp | str | None = None,
    random_state: int | None = None,
    calibration_frame: pd.DataFrame | None = None,
) -> TrainedIsolationForest:
    """Fit one Isolation Forest per operating mode.

    The scaler and models are fit on the same complete rows: any row missing
    one or more selected feature values is dropped from training and counted
    in the manifest. Modes with a usable feature set but too few fit rows are
    skipped entirely (Task 2.2) rather than persisting a meaningless forest.
    """
    _validate_config(config)
    _require_columns(frame, [mode_column, timestamp_column, session_column])
    selected_features = _resolve_feature_columns(frame, feature_columns, mode_column)
    _require_columns(frame, selected_features)
    schema = (
        dict(feature_spec)
        if feature_spec is not None
        else build_feature_spec(frame, selected_features)
    )
    validate_model_feature_spec(schema, context="IsolationForest training feature spec")

    seed = config.random_state if random_state is None else random_state
    holdout_fraction, target_flag_rate = _calibration_params(config)
    min_samples_per_mode = _min_samples_per_mode(config)

    # First pass, against the *raw* (unscaled) frame: work out each mode's
    # feature subset and time-ordered fit/calibration-holdout split. NaN
    # positions don't depend on scaling, so this can run before the scaler
    # exists — which is exactly what Task 2.4 needs: the scaler is fit below
    # on the union of these ``fit_index`` rows only, not the calibration
    # holdout tail (the previous leak) and not modes 2.2 goes on to skip.
    mode_plans: dict[str, _ModeFitPlan] = {}
    skipped_modes: list[SkippedModeSummary] = []
    for raw_mode, group in frame.groupby(mode_column, sort=True, dropna=False):
        mode = str(raw_mode)
        if len(group) == 0:
            continue
        # Per-mode feature set: only the columns densely populated in this
        # mode. A deviation surface gated to a *different* operating_mode is
        # all-NaN here, so it's excluded — rather than dropping every row of
        # this mode (the global dropna that silently blanked the detector,
        # because each mode-gated surface is NaN outside its own mode).
        # ``.any()`` is too permissive, though: a foreign-mode deviation leaks
        # into a handful of this mode's rows through the rolling window at a
        # mode boundary (its 30 s mean/std carries a few rows past the
        # transition). That sparse non-NaN qualified the column, then the
        # ``dropna`` below emptied the whole mode on it (e.g. warmup deviations,
        # ~3 % populated in idle, zeroed the idle model entirely). Require a real
        # coverage fraction so only genuinely-present features count.
        mode_feats = [
            c
            for c in selected_features
            if float(group[c].notna().mean()) >= _MIN_MODE_FEATURE_COVERAGE
        ]
        if not mode_feats:
            continue
        mode_complete = group.dropna(subset=mode_feats)
        if mode_complete.empty:
            continue

        fit_index, holdout_index, n_holdout, calibrate = _calibration_split(
            frame,
            mode_complete.index,
            timestamp_column=timestamp_column,
            holdout_fraction=0.0 if calibration_frame is not None else holdout_fraction,
        )

        if len(fit_index) == 0:
            continue

        # Task 2.2: a forest fit on a handful of rows is a meaningless model
        # that still emits with full sklearn-default confidence. Skip it
        # (no model persisted) instead, and record why in the manifest.
        if len(fit_index) < min_samples_per_mode:
            skipped_modes.append(
                SkippedModeSummary(
                    mode=mode,
                    fit_rows=len(fit_index),
                    min_samples_required=min_samples_per_mode,
                )
            )
            continue

        mode_plans[mode] = _ModeFitPlan(
            feats=mode_feats,
            complete_index=mode_complete.index,
            fit_index=fit_index,
            holdout_index=holdout_index,
            n_holdout=n_holdout,
            calibrate=calibrate,
        )

    if not mode_plans:
        raise ValueError("no per-mode IsolationForest models were trained")
    if set(mode_plans) == {"unknown"} and getattr(config, "allow_unknown_only", None) is False:
        raise ValueError(
            "IsolationForest would train only operating_mode='unknown'; fix segmentation "
            "coverage or explicitly set allow_unknown_only=true"
        )

    # 2.4: fit the scaler on exactly the rows the forests below train on —
    # every kept mode's fit rows, none of their calibration-holdout rows, and
    # none of a skipped mode's rows. Still NaN-aware per column, so a
    # deviation surface gated to one mode gets its stats from that mode's
    # own fit rows same as before.
    scaler_fit_rows = sorted({idx for plan in mode_plans.values() for idx in plan.fit_index})
    # A feature column exclusive to a mode 2.2's floor skips entirely (e.g. a
    # ``deviation:*`` surface mode-gated to that one mode — see
    # ``feature_pipeline``'s mode-gated-feature docstrings) is NaN across the
    # *whole* surviving-mode row union: no surviving mode's ``plan.feats``
    # lists it, so it never has real values outside the now-skipped mode.
    # Fitting the scaler over ``selected_features`` unconditionally used to
    # crash on that column (``ZScoreScaler.fit`` rejects all-NaN columns)
    # even though no surviving model ever reads it. Fit only on columns some
    # surviving mode actually uses, then backfill identity placeholder stats
    # (mean 0 / std 1) for the rest — dead parameters no model consults, kept
    # only so ``scaler.columns`` still matches the manifest's full
    # ``feature_columns`` schema, exactly as before.
    used_columns = [
        column
        for column in selected_features
        if any(column in plan.feats for plan in mode_plans.values())
    ]
    scaler = ZScoreScaler.fit(frame.loc[scaler_fit_rows], columns=used_columns)
    unused_columns = [column for column in selected_features if column not in used_columns]
    if unused_columns:
        scaler = ZScoreScaler(
            means={**scaler.means, **dict.fromkeys(unused_columns, 0.0)},
            stds={**scaler.stds, **dict.fromkeys(unused_columns, 1.0)},
            columns=tuple(selected_features),
            std_floor=scaler.std_floor,
        )
    isolation_forest_cls = _isolation_forest_cls()
    models: dict[str, Any] = {}
    mode_feature_columns: dict[str, list[str]] = {}
    modes: list[ModeTrainingSummary] = []
    used_index: list[Any] = []

    for mode, plan in mode_plans.items():
        contamination = _contamination_for_mode(config, mode)
        x = _scaled_array(frame.loc[plan.fit_index, plan.feats], scaler, plan.feats)
        model = isolation_forest_cls(
            n_estimators=config.n_estimators,
            contamination=contamination,
            random_state=seed,
            n_jobs=int(getattr(config, "n_jobs", 1)),
        )
        model.fit(x)
        models[mode] = model
        mode_feature_columns[mode] = plan.feats
        used_index.extend(plan.complete_index.tolist())

        external_holdout = None
        if calibration_frame is not None and mode_column in calibration_frame.columns:
            candidate = calibration_frame[calibration_frame[mode_column].astype(str) == mode]
            external_holdout = candidate.dropna(subset=plan.feats)
        use_external = (
            external_holdout is not None and len(external_holdout) >= _MIN_CALIBRATION_SAMPLES
        )
        if calibration_frame is not None and not use_external:
            count = 0 if external_holdout is None else len(external_holdout)
            raise ValueError(
                f"held-out calibration session has only {count} complete rows for mode "
                f"{mode!r}; need at least {_MIN_CALIBRATION_SAMPLES}"
            )
        calibration_rows = external_holdout if use_external else None
        score_threshold, score_scale, achieved_flag_rate = (
            _calibrate_holdout_threshold(
                model,
                pd.DataFrame(
                    _scaled_array(
                        calibration_rows.loc[:, plan.feats]
                        if calibration_rows is not None
                        else frame.loc[plan.holdout_index, plan.feats],
                        scaler,
                        plan.feats,
                    ),
                    columns=plan.feats,
                ),
                target_flag_rate=target_flag_rate,
            )
            if use_external or plan.calibrate
            else (None, None, None)
        )
        calibration_count = (
            len(calibration_rows) if calibration_rows is not None else plan.n_holdout
        )

        modes.append(
            _mode_summary(
                mode=mode,
                contamination=contamination,
                frame=frame.loc[plan.complete_index],
                session_column=session_column,
                timestamp_column=timestamp_column,
                score_threshold=score_threshold,
                score_scale=score_scale,
                holdout_samples=calibration_count,
                target_flag_rate=target_flag_rate if use_external or plan.calibrate else None,
                achieved_flag_rate=achieved_flag_rate,
            )
        )

    # The rows actually used across all per-mode models (disjoint by mode) —
    # for the manifest's data hash, span, sessions, and complete-row count.
    complete = frame.loc[sorted(set(used_index))]
    dropped_fraction = (len(frame) - len(complete)) / len(frame) if len(frame) else 1.0
    maximum_dropped = float(getattr(config, "max_dropped_row_fraction", 1.0))
    if dropped_fraction > maximum_dropped:
        raise ValueError(
            f"IsolationForest dropped {dropped_fraction:.1%} of input rows; maximum is "
            f"{maximum_dropped:.1%}"
        )

    created_at = _format_timestamp(coerce_timestamp(fitted_at))
    version = model_version or f"isolation_forest_{created_at.replace(':', '')}"
    manifest = IsolationForestManifest(
        artifact_format_version=ARTIFACT_FORMAT_VERSION,
        model_type=MODEL_TYPE,
        model_version=version,
        created_at=created_at,
        training_data_hash=_training_data_hash(
            complete,
            feature_columns=selected_features,
            mode_column=mode_column,
            session_column=session_column,
            timestamp_column=timestamp_column,
        ),
        training_start_timestamp=_min_timestamp(complete[timestamp_column]),
        training_end_timestamp=_max_timestamp(complete[timestamp_column]),
        training_sessions=_unique_strings(complete[session_column]),
        input_rows=len(frame),
        complete_rows=len(complete),
        dropped_missing_feature_rows=len(frame) - len(complete),
        hyperparams={
            "n_estimators": config.n_estimators,
            "contamination": config.contamination,
            "per_mode_contamination": _per_mode_contamination(config),
            "random_state": seed,
            "calibration_holdout_fraction": holdout_fraction,
            "target_flag_rate": target_flag_rate,
            "min_samples_per_mode": min_samples_per_mode,
            "n_jobs": int(getattr(config, "n_jobs", 1)),
        },
        scaler=scaler.to_dict(),
        schema={
            **schema,
            "mode_column": mode_column,
            "mode_feature_columns": mode_feature_columns,
            "timestamp_column": timestamp_column,
            "session_column": session_column,
            "score_column": SCORE_COLUMN,
            "label_column": LABEL_COLUMN,
        },
        modes=tuple(modes),
        skipped_modes=tuple(skipped_modes),
        calibration_sessions=(
            ()
            if calibration_frame is None or session_column not in calibration_frame.columns
            else _unique_strings(calibration_frame[session_column])
        ),
    )
    return TrainedIsolationForest(models=models, scaler=scaler, manifest=manifest)


def _scaled_array(
    frame: pd.DataFrame, scaler: ZScoreScaler, columns: Sequence[str]
) -> np.ndarray[Any, np.dtype[np.float32]]:
    """Scale only the matrix a mode consumes, avoiding a full-frame copy."""
    values = frame.loc[:, list(columns)].to_numpy(dtype=np.float32, copy=True)
    means = np.asarray([scaler.means[column] for column in columns], dtype=np.float32)
    denoms = np.asarray(
        [
            scaler.stds[column] if scaler.stds[column] >= scaler.std_floor else 1.0
            for column in columns
        ],
        dtype=np.float32,
    )
    values -= means
    values /= denoms
    return values


def save_isolation_forest_model(
    trained: TrainedIsolationForest,
    artifact_path: Path,
    *,
    manifest_path: Path | None = None,
) -> None:
    """Persist a trained model bundle with joblib and a JSON manifest."""
    joblib = _joblib_module()
    manifest = trained.manifest.to_dict()
    payload = {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "model_type": MODEL_TYPE,
        "models": dict(trained.models),
        "scaler": trained.scaler.to_dict(),
        "manifest": manifest,
    }
    write_artifact(
        artifact_path,
        lambda path: joblib.dump(payload, path),
        manifest,
        manifest_path=manifest_path,
    )


def load_isolation_forest_model(artifact_path: Path) -> TrainedIsolationForest:
    """Load a model previously written by ``save_isolation_forest_model``."""
    joblib = _joblib_module()
    payload = read_artifact(
        artifact_path,
        joblib.load,
        model_type=MODEL_TYPE,
        expected_format_version=ARTIFACT_FORMAT_VERSION,
    )

    manifest = IsolationForestManifest.from_dict(payload["manifest"])
    scaler = ZScoreScaler.from_dict(payload["scaler"])
    feature_columns = validate_model_feature_spec(
        manifest.schema,
        context=f"IsolationForest manifest {artifact_path}",
    )
    _validate_loaded_artifact(
        payload=payload,
        manifest=manifest,
        scaler=scaler,
        feature_columns=feature_columns,
    )
    return TrainedIsolationForest(
        models=dict(payload["models"]),
        scaler=scaler,
        manifest=manifest,
    )


def default_manifest_path(artifact_path: Path) -> Path:
    """Return the conventional JSON manifest path for an artifact."""
    return manifest_path_for(artifact_path)


def _validate_config(config: IsolationForestConfigLike) -> None:
    if not config.enabled:
        raise ValueError("isolation forest training requested but config is disabled")
    if config.n_estimators <= 0:
        raise ValueError("n_estimators must be positive")
    _validate_contamination(config.contamination, label="contamination")
    for mode, contamination in _per_mode_contamination(config).items():
        _validate_contamination(contamination, label=f"per_mode_contamination[{mode!r}]")
    holdout_fraction, target_flag_rate = _calibration_params(config)
    if not 0.0 <= holdout_fraction < 1.0:
        raise ValueError("calibration_holdout_fraction must be in [0, 1)")
    if not 0.0 < target_flag_rate <= 0.5:
        raise ValueError("target_flag_rate must be in (0, 0.5]")
    if _min_samples_per_mode(config) < 0:
        raise ValueError("min_samples_per_mode must be non-negative")


def _validate_contamination(value: float, *, label: str) -> None:
    if not 0.0 < value <= 0.5:
        raise ValueError(f"{label} must be in (0, 0.5]")


def _calibration_params(config: IsolationForestConfigLike) -> tuple[float, float]:
    """Read the Phase 7.2 calibration knobs defensively.

    Configs predating 7.2 (and the lightweight test doubles) don't carry
    these attributes; absent ⇒ calibration disabled (``holdout_fraction``
    ``0.0``), which preserves the exact legacy fit-on-all behavior.
    """
    holdout_fraction = float(getattr(config, "calibration_holdout_fraction", 0.0))
    target_flag_rate = float(getattr(config, "target_flag_rate", 0.02))
    return holdout_fraction, target_flag_rate


def _min_samples_per_mode(config: IsolationForestConfigLike) -> int:
    """Read the Task 2.2 fit-row floor defensively — same pattern as
    :func:`_calibration_params`. Configs/test doubles predating 2.2 don't
    carry ``min_samples_per_mode``; absence means "no floor" (``0``),
    preserving the exact legacy behavior of training on however few rows a
    mode has. ``src.config.IsolationForestConfig`` defaults it to 100.
    """
    return int(getattr(config, "min_samples_per_mode", 0))


def _validate_loaded_artifact(
    *,
    payload: Mapping[str, Any],
    manifest: IsolationForestManifest,
    scaler: ZScoreScaler,
    feature_columns: tuple[str, ...],
) -> None:
    scaler_columns = tuple(str(column) for column in scaler.columns)
    if scaler_columns != feature_columns:
        raise ValueError(
            "IsolationForest scaler columns do not match manifest feature_columns: "
            f"scaler={list(scaler_columns)!r}, manifest={list(feature_columns)!r}"
        )

    manifest_modes = tuple(mode.mode for mode in manifest.modes)
    if not manifest_modes:
        raise ValueError("IsolationForest manifest contains no trained modes")
    # Phase 7.2 introduced per-mode calibrated thresholds. An artifact whose
    # ``modes`` carry no ``score_threshold`` is pre-7.2 (or training broke
    # silently). Fusion treats null thresholds as "never fire", so the
    # detector loads cleanly but is functionally a no-op. Catch that here
    # rather than discovering it in production.
    if not any(mode.calibrated for mode in manifest.modes):
        raise ValueError(
            "IsolationForest manifest has no calibrated score_threshold on any "
            "mode — likely a pre-Phase-7.2 artifact, or calibration was disabled "
            "at training time. Retrain on current code (calibration_holdout_fraction"
            " > 0 in canary.yaml) before deploying."
        )
    model_modes = {str(mode) for mode in dict(payload["models"])}
    missing_modes = [mode for mode in manifest_modes if mode not in model_modes]
    if missing_modes:
        raise ValueError(
            "IsolationForest artifact missing models for manifest modes: "
            + ", ".join(missing_modes)
        )


def _per_mode_contamination(config: IsolationForestConfigLike) -> dict[str, float]:
    overrides = getattr(config, "per_mode_contamination", {})
    return {str(mode): float(value) for mode, value in dict(overrides).items()}


def _contamination_for_mode(config: IsolationForestConfigLike, mode: str) -> float:
    return _per_mode_contamination(config).get(mode, config.contamination)


def _resolve_feature_columns(
    frame: pd.DataFrame,
    override: Iterable[str] | None,
    mode_column: str,
) -> tuple[str, ...]:
    if override is not None:
        columns = tuple(str(column) for column in override)
        _require_columns(frame, columns)
        for column in columns:
            if not pd.api.types.is_numeric_dtype(frame[column]):
                raise TypeError(
                    f"column {column!r} is not numeric; IsolationForest needs numeric features"
                )
        return columns

    skip = {SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN, mode_column}
    discovered: list[str] = []
    for raw in frame.columns:
        column = str(raw)
        if column in skip:
            continue
        if column.endswith(GAP_SUFFIX):
            continue
        if not pd.api.types.is_numeric_dtype(frame[column]):
            continue
        discovered.append(column)
    if not discovered:
        raise ValueError("no numeric feature columns available for IsolationForest")
    return tuple(discovered)


def _mode_summary(
    *,
    mode: str,
    contamination: float,
    frame: pd.DataFrame,
    session_column: str,
    timestamp_column: str,
    score_threshold: float | None = None,
    score_scale: float | None = None,
    holdout_samples: int = 0,
    target_flag_rate: float | None = None,
    achieved_flag_rate: float | None = None,
) -> ModeTrainingSummary:
    return ModeTrainingSummary(
        mode=mode,
        n_samples=len(frame),
        contamination=contamination,
        sessions=_unique_strings(frame[session_column]),
        start_timestamp=_min_timestamp(frame[timestamp_column]),
        end_timestamp=_max_timestamp(frame[timestamp_column]),
        score_threshold=score_threshold,
        score_scale=score_scale,
        holdout_samples=holdout_samples,
        target_flag_rate=target_flag_rate,
        achieved_flag_rate=achieved_flag_rate,
    )


def _training_data_hash(
    frame: pd.DataFrame,
    *,
    feature_columns: Sequence[str],
    mode_column: str,
    session_column: str,
    timestamp_column: str,
) -> str:
    columns = [session_column, timestamp_column, mode_column, *feature_columns]
    normalized = frame[columns].copy()
    normalized[timestamp_column] = pd.to_datetime(normalized[timestamp_column], utc=True).astype(
        "int64"
    )
    normalized = normalized.sort_values(columns, kind="stable").reset_index(drop=True)

    digest = hashlib.sha256()
    digest.update(json.dumps({"columns": columns}, sort_keys=True).encode("utf-8"))
    row_hashes = pd.util.hash_pandas_object(normalized, index=False).to_numpy(dtype=np.uint64)
    digest.update(row_hashes.tobytes())
    return f"sha256:{digest.hexdigest()}"


def _unique_strings(series: pd.Series) -> tuple[str, ...]:
    return tuple(sorted(str(value) for value in series.dropna().unique()))


def _min_timestamp(series: pd.Series) -> str | None:
    timestamps = pd.to_datetime(series, utc=True)
    if timestamps.empty:
        return None
    return _format_timestamp(timestamps.min())


def _max_timestamp(series: pd.Series) -> str | None:
    timestamps = pd.to_datetime(series, utc=True)
    if timestamps.empty:
        return None
    return _format_timestamp(timestamps.max())


def _isolation_forest_cls() -> Any:
    try:
        from sklearn.ensemble import IsolationForest
    except ImportError as exc:
        raise MissingModelDependencyError(
            "scikit-learn is required to train IsolationForest models"
        ) from exc
    return IsolationForest


def _joblib_module() -> Any:
    try:
        import joblib
    except ImportError as exc:
        raise MissingModelDependencyError(
            "joblib is required to save/load IsolationForest artifacts"
        ) from exc
    return joblib
