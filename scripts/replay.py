#!/usr/bin/env python3
"""Replay recorded Parquet sessions through preprocessing and optional IF scoring.

This is the no-hardware path for exercising the model pipeline:

  session Parquet -> wide frame -> resample -> segment -> rolling features
      -> optional IsolationForest score

The script intentionally reports intermediate row/column counts. CAN-only
captures currently produce payload rows but no useful numeric value channels;
that should show up clearly in the report rather than failing mysteriously.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from _cli import add_common_args, load_scoped_config  # type: ignore[import-not-found]
except ImportError:  # loaded as `scripts.replay` — repo root on path, not scripts/
    from scripts._cli import add_common_args, load_scoped_config

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
from src.features.feature_pipeline import featurize_wide_frame
from src.models.isolation_forest import (
    LABEL_COLUMN,
    SCORE_COLUMN,
    load_isolation_forest_model,
)
from src.models.isolation_forest_evaluation import score_distribution
from src.preprocessing.loader import TelemetryValueKind, read_sessions, to_wide
from src.preprocessing.resampler import resample_config_for, resample_wide
from src.timefmt import format_timestamp


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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="replay",
        description=(
            "Replay one or more Parquet session directories through CANary's "
            "preprocessing pipeline and optionally score with an IsolationForest artifact."
        ),
    )
    parser.add_argument("session_dir", type=Path, nargs="+")
    add_common_args(parser, help="Path to canary.yaml (default: ./canary.yaml).")
    parser.add_argument(
        "--model",
        type=Path,
        default=None,
        help="Optional IsolationForest joblib artifact to score after feature generation.",
    )
    parser.add_argument(
        "--value",
        choices=["value_number", "value_bool"],
        default="value_number",
        help="Telemetry value column to pivot into numeric features (default: value_number).",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON report.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        # The daemon's featurization reads the scoped config (P02.11).
        config = load_scoped_config(args, None)
        result = replay_sessions(
            args.session_dir,
            config,
            value=args.value,
            model_path=args.model,
        )
    except (FileNotFoundError, KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    indent = 2 if args.pretty else None
    print(json.dumps(result.report, indent=indent, sort_keys=True))
    return 0


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


if __name__ == "__main__":
    sys.exit(main())
