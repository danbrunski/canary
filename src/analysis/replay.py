"""Offline replay of recorded sessions through preprocessing and optional IF scoring.

The library operation behind ``scripts/replay.py`` and
:func:`src.analysis.replay_window.replay_window` (R12):

  session Parquet -> wide frame -> resample -> segment -> rolling features
      -> optional IsolationForest score

The report states intermediate row/column counts. CAN-only captures currently
produce payload rows but no useful numeric value channels; that shows up
clearly in the report rather than failing mysteriously.

This is *offline* replay: it reads stored Parquet and featurizes it with the
shared :func:`featurize_wide_frame`. It is a different API from the daemon's
runtime replay (``src.collection.replay`` / ``start_replay``), which re-emits a
session through the live pipeline. Nothing here prints; argument parsing,
console output and exit codes belong to the script.

Behavior *events* (what the daemon's behavior detector would persist, emit
and fuse, served set vs a candidate) are not this module's job: use
``tools/replay_behavior_compare.py`` (B03.7), which ticks the live pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.behavior.behavior_models import load_behavior_models
from src.columns import (
    MEAN_SUFFIX,
    MODE_COLUMN,
    ROC_SUFFIX,
    SESSION_COLUMN,
    STD_SUFFIX,
    TIMESTAMP_COLUMN,
    is_flag_column,
)
from src.config import CanaryConfig
from src.features.feature_transform import featurize_wide_frame
from src.models.isolation_forest import (
    LABEL_COLUMN,
    SCORE_COLUMN,
    load_isolation_forest_model,
)
from src.models.isolation_forest_evaluation import score_distribution
from src.preprocessing.loader import TelemetryValueKind, read_sessions, to_wide
from src.preprocessing.resampler import resample_config_for, resample_wide
from src.timefmt import format_timestamp

__all__ = ["ReplayFrames", "ReplayResult", "replay_sessions"]


@dataclass(frozen=True)
class ReplayFrames:
    """Intermediate frames from one replay run."""

    long: pd.DataFrame
    wide: pd.DataFrame
    resampled: pd.DataFrame
    segmented: pd.DataFrame
    features: pd.DataFrame
    scored: pd.DataFrame | None = None


@dataclass(frozen=True)
class ReplayResult:
    """Replay output: frames for programmatic use plus JSON-safe report."""

    frames: ReplayFrames
    report: Mapping[str, Any]


def replay_sessions(
    session_dirs: Iterable[Path],
    config: CanaryConfig,
    *,
    value: TelemetryValueKind = "value_number",
    model_path: Path | None = None,
    behavior_models_path: Path | None = None,
) -> ReplayResult:
    """Run recorded sessions through preprocessing and optional IF scoring.

    The segment → anomaly-signal → committee → behavior-deviation →
    rolling-feature tail is :func:`featurize_wide_frame` — the same
    function ``build_feature_frame`` (training) and ``LiveFeatureSource.tick``
    (serving) call — so a replayed session's feature columns match what the
    model actually saw, instead of an open-coded subset (2.1: this used to
    call ``compute_features`` directly and silently dropped the
    anomaly-signal / committee / behavior-deviation columns).

    ``behavior_models_path`` defaults to the scoped ``config.daemon.behavior_models``
    (P02.12): the repo-level default it had named a file that does not exist,
    so a replay featurized without the deviations the daemon serves.
    """
    sessions = [Path(path) for path in session_dirs]
    if not sessions:
        raise ValueError("at least one session directory is required")

    long = read_sessions(sessions)
    wide = to_wide(long, value=value)
    numeric_columns = _numeric_value_columns(wide)
    resampled = _resample_numeric(wide, numeric_columns, config)
    if behavior_models_path is None:
        behavior_models_path = config.daemon.behavior_models
    behavior_models = (
        load_behavior_models(behavior_models_path) if behavior_models_path.exists() else None
    )
    # read_sessions -> to_wide decodes no Mode 22 DID, so no DID catalog
    # describes this frame (P02.14: no DID view, no DID-backed load axis).
    featurized = featurize_wide_frame(
        resampled, config, behavior_models=behavior_models, did_map=None
    )
    segmented = featurized.operating_frame
    features = featurized.features

    scored: pd.DataFrame | None = None
    scoring_report: dict[str, Any] | None = None
    if model_path is not None:
        trained = load_isolation_forest_model(model_path)
        scored = trained.score(features)
        scoring_report = _scoring_report(scored, trained.manifest.model_version)

    report = _report(
        session_dirs=sessions,
        value=value,
        long=long,
        wide=wide,
        resampled=resampled,
        segmented=segmented,
        features=features,
        numeric_columns=numeric_columns,
        scoring_report=scoring_report,
    )
    return ReplayResult(
        frames=ReplayFrames(
            long=long,
            wide=wide,
            resampled=resampled,
            segmented=segmented,
            features=features,
            scored=scored,
        ),
        report=report,
    )


def _resample_numeric(
    wide: pd.DataFrame,
    numeric_columns: Sequence[str],
    config: CanaryConfig,
) -> pd.DataFrame:
    if not numeric_columns:
        return pd.DataFrame(
            {
                SESSION_COLUMN: wide[SESSION_COLUMN].copy(),
                TIMESTAMP_COLUMN: pd.to_datetime(wide[TIMESTAMP_COLUMN], utc=True),
            }
        )
    return resample_wide(
        wide,
        config=resample_config_for(config),
        value_columns=numeric_columns,
    )


def _report(
    *,
    session_dirs: Sequence[Path],
    value: str,
    long: pd.DataFrame,
    wide: pd.DataFrame,
    resampled: pd.DataFrame,
    segmented: pd.DataFrame,
    features: pd.DataFrame,
    numeric_columns: Sequence[str],
    scoring_report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    rolling_feature_columns = _rolling_feature_columns(features)
    model_input_columns = _numeric_value_columns(features)
    return {
        "session_dirs": [str(path) for path in session_dirs],
        "sessions": _value_counts(long.get(SESSION_COLUMN, pd.Series(dtype="object"))),
        "value": value,
        "time_range": _time_range(long),
        "rows": {
            "long": len(long),
            "wide": len(wide),
            "resampled": len(resampled),
            "features": len(features),
        },
        "sources": _value_counts(long.get("source", pd.Series(dtype="object"))),
        "channels": {
            "count": int(long["source_channel"].nunique()) if "source_channel" in long else 0,
            "by_source": _channels_by_source(long),
        },
        "numeric_value_columns": list(numeric_columns),
        "numeric_value_column_count": len(numeric_columns),
        "rolling_feature_columns": rolling_feature_columns,
        "rolling_feature_column_count": len(rolling_feature_columns),
        "model_input_columns": model_input_columns,
        "model_input_column_count": len(model_input_columns),
        "complete_model_input_rows": _complete_rows(features, model_input_columns),
        "mode_counts": _value_counts(segmented.get(MODE_COLUMN, pd.Series(dtype="object"))),
        "scoring": scoring_report,
    }


def _scoring_report(scored: pd.DataFrame, model_version: str) -> dict[str, Any]:
    predictions = scored[LABEL_COLUMN].astype("Int64").eq(-1).fillna(False)
    by_mode: dict[str, int] = {}
    if MODE_COLUMN in scored:
        for mode, group in scored.groupby(MODE_COLUMN, sort=True, dropna=False):
            by_mode[str(mode)] = int(predictions.loc[group.index].sum())
    return {
        "model_version": model_version,
        "rows": len(scored),
        "predicted_anomalies": int(predictions.sum()),
        "predicted_anomalies_by_mode": by_mode,
        "score_distribution": score_distribution(scored[SCORE_COLUMN]).to_dict(),
    }


def _numeric_value_columns(df: pd.DataFrame) -> list[str]:
    skip = {SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN}
    return [
        str(column)
        for column in df.columns
        if str(column) not in skip
        and pd.api.types.is_numeric_dtype(df[column])
        and not is_flag_column(column)
    ]


def _rolling_feature_columns(df: pd.DataFrame) -> list[str]:
    suffixes = (MEAN_SUFFIX, STD_SUFFIX, ROC_SUFFIX)
    return [str(column) for column in df.columns if str(column).endswith(suffixes)]


def _complete_rows(df: pd.DataFrame, columns: Sequence[str]) -> int:
    if not columns:
        return 0
    return int(df[list(columns)].notna().all(axis=1).sum())


def _time_range(frame: pd.DataFrame) -> dict[str, str | None]:
    if frame.empty or TIMESTAMP_COLUMN not in frame:
        return {"start": None, "end": None}
    timestamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
    return {
        "start": format_timestamp(timestamps.min()),
        "end": format_timestamp(timestamps.max()),
    }


def _channels_by_source(frame: pd.DataFrame) -> dict[str, int]:
    if frame.empty or "source" not in frame or "channel" not in frame:
        return {}
    grouped = frame.groupby("source", sort=True)["channel"].nunique()
    return {str(source): int(count) for source, count in grouped.items()}


def _value_counts(series: pd.Series) -> dict[str, int]:
    return {str(key): int(value) for key, value in series.value_counts(dropna=False).items()}
