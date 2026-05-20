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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from src.dataframe_utils import require_columns as _require_columns
from src.models.manifest import validate_model_feature_spec
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


class IsolationForestConfigLike(Protocol):
    """Subset of ``src.config.IsolationForestConfig`` needed by the trainer."""

    enabled: bool
    n_estimators: int
    contamination: float
    random_state: int
    calibration_holdout_fraction: float
    target_flag_rate: float


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

        for mode, group in scaled.groupby(self.mode_column, sort=True, dropna=False):
            mode_key = str(mode)
            indexes = group.index
            missing_features = group[list(self.feature_columns)].isna().any(axis=1)

            if missing_features.any():
                missing_indexes = missing_features[missing_features].index
                out.loc[missing_indexes, SCORE_COLUMN] = np.inf
                out.loc[missing_indexes, LABEL_COLUMN] = -1
                out.loc[missing_indexes, MODEL_MODE_COLUMN] = mode_key
                out.loc[missing_indexes, MISSING_FEATURES_COLUMN] = True

            complete_indexes = indexes.difference(missing_features[missing_features].index)
            if complete_indexes.empty:
                continue

            model_key = mode_key if mode_key in self.models else fallback_key
            if model_key is None:
                # No model for this mode and no single-model fallback to route
                # through. Withhold rather than crash the tick: flag the rows
                # so the daemon suppresses the emit but health still sees them.
                out.loc[complete_indexes, SCORE_COLUMN] = np.inf
                out.loc[complete_indexes, LABEL_COLUMN] = -1
                out.loc[complete_indexes, MODEL_MODE_COLUMN] = mode_key
                out.loc[complete_indexes, MISSING_FEATURES_COLUMN] = True
                continue

            x = scaled.loc[complete_indexes, list(self.feature_columns)].to_numpy(dtype=np.float32)
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
) -> TrainedIsolationForest:
    """Fit one Isolation Forest per operating mode.

    The scaler and models are fit on the same complete rows: any row missing
    one or more selected feature values is dropped from training and counted
    in the manifest.
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

    complete = frame.dropna(subset=selected_features).copy()
    if complete.empty:
        raise ValueError("no complete rows available to train IsolationForest")

    scaler = ZScoreScaler.fit(complete, columns=selected_features)
    scaled = scaler.transform(complete)
    seed = config.random_state if random_state is None else random_state

    isolation_forest_cls = _isolation_forest_cls()
    models: dict[str, Any] = {}
    modes: list[ModeTrainingSummary] = []

    holdout_fraction, target_flag_rate = _calibration_params(config)
    for raw_mode, group in scaled.groupby(mode_column, sort=True, dropna=False):
        mode = str(raw_mode)
        if len(group) == 0:
            continue
        contamination = _contamination_for_mode(config, mode)

        # Phase 7.2: time-order the mode's rows and reserve the tail as a
        # held-out calibration set the model never sees, so the per-mode
        # score threshold is set on genuinely unseen nominal data rather
        # than on the training distribution. Fall back to fitting on all
        # rows (legacy contamination label) when there isn't enough holdout.
        ordered_index = (
            pd.to_datetime(complete.loc[group.index, timestamp_column], utc=True)
            .sort_values(kind="stable")
            .index
        )
        n = len(ordered_index)
        n_holdout = int(np.floor(n * holdout_fraction)) if holdout_fraction > 0 else 0
        calibrate = (n - n_holdout) >= 1 and n_holdout >= _MIN_CALIBRATION_SAMPLES
        if calibrate:
            fit_index = ordered_index[: n - n_holdout]
            holdout_index = ordered_index[n - n_holdout :]
        else:
            fit_index, holdout_index, n_holdout = ordered_index, ordered_index[:0], 0

        x = group.loc[fit_index, list(selected_features)].to_numpy(dtype=np.float32)
        if len(x) == 0:
            continue
        model = isolation_forest_cls(
            n_estimators=config.n_estimators,
            contamination=contamination,
            random_state=seed,
        )
        model.fit(x)
        models[mode] = model

        score_threshold: float | None = None
        score_scale: float | None = None
        achieved_flag_rate: float | None = None
        if calibrate:
            hx = group.loc[holdout_index, list(selected_features)].to_numpy(dtype=np.float32)
            holdout_scores = -model.decision_function(hx)
            score_threshold = float(np.quantile(holdout_scores, 1.0 - target_flag_rate))
            achieved_flag_rate = float(np.mean(holdout_scores >= score_threshold))
            spread = float(np.std(holdout_scores))
            score_scale = spread if spread > 0.0 else None

        modes.append(
            _mode_summary(
                mode=mode,
                contamination=contamination,
                frame=complete.loc[group.index],
                session_column=session_column,
                timestamp_column=timestamp_column,
                score_threshold=score_threshold,
                score_scale=score_scale,
                holdout_samples=n_holdout,
                target_flag_rate=target_flag_rate if calibrate else None,
                achieved_flag_rate=achieved_flag_rate,
            )
        )

    if not models:
        raise ValueError("no per-mode IsolationForest models were trained")

    created_at = _format_timestamp(_coerce_timestamp(fitted_at))
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
        },
        scaler=scaler.to_dict(),
        schema={
            **schema,
            "mode_column": mode_column,
            "timestamp_column": timestamp_column,
            "session_column": session_column,
            "score_column": SCORE_COLUMN,
            "label_column": LABEL_COLUMN,
        },
        modes=tuple(modes),
    )
    return TrainedIsolationForest(models=models, scaler=scaler, manifest=manifest)


def save_isolation_forest_model(
    trained: TrainedIsolationForest,
    artifact_path: Path,
    *,
    manifest_path: Path | None = None,
) -> None:
    """Persist a trained model bundle with joblib and a JSON manifest."""
    joblib = _joblib_module()
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = trained.manifest.to_dict()
    payload = {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "model_type": MODEL_TYPE,
        "models": dict(trained.models),
        "scaler": trained.scaler.to_dict(),
        "manifest": manifest,
    }
    joblib.dump(payload, artifact_path)

    path = manifest_path or artifact_path.with_suffix(".manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def load_isolation_forest_model(artifact_path: Path) -> TrainedIsolationForest:
    """Load a model previously written by ``save_isolation_forest_model``."""
    joblib = _joblib_module()
    payload = joblib.load(artifact_path)
    if int(payload["artifact_format_version"]) != ARTIFACT_FORMAT_VERSION:
        raise ValueError(
            f"unsupported IsolationForest artifact format {payload['artifact_format_version']!r}"
        )
    if str(payload["model_type"]) != MODEL_TYPE:
        raise ValueError(f"unexpected model type {payload['model_type']!r}")

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
    return artifact_path.with_suffix(".manifest.json")


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


def _coerce_timestamp(value: datetime | pd.Timestamp | str | None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp(datetime.now(tz=UTC))
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


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
