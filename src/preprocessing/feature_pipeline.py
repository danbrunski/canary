"""Shared offline feature pipeline: recorded sessions → model-ready feature frame.

Both anomaly trainers (Isolation Forest and LSTM autoencoder) consume the same
preprocessing path: read recorded Parquet sessions, decode CAN payloads via
``bindings.yaml`` into numeric columns, pivot to wide, resample to a uniform
grid, segment operating modes (or fall back to a single ``"all"`` mode when no
OBD PIDs are present), and compute rolling features. Keeping it here means the
training CLIs stay thin and behave identically.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import CanaryConfig
from src.decode.behavior_decoder import append_deviation_columns
from src.decode.behavior_models import (
    DEFAULT_BEHAVIOR_MODELS_PATH,
    load_behavior_models,
)
from src.decode.bindings import load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, decode_can_rows_dbc, load_dbc_dir
from src.preprocessing.decode import decode_can_rows
from src.preprocessing.features import FeatureConfig, compute_features
from src.preprocessing.loader import read_session, to_wide
from src.preprocessing.resampler import ResampleConfig, resample_wide
from src.preprocessing.segmenter import (
    MODE_COLUMN,
    SegmenterConfig,
    segment_operating_modes,
)

log = logging.getLogger(__name__)

#: Default location of the hand-edited CAN signal bindings file.
DEFAULT_BINDINGS_PATH = Path("data/bindings.yaml")

#: Mode label used when no OBD PIDs are present and the threshold segmenter
#: can't run — everything becomes one mode and the model fits a single global
#: detector. Per-mode models materialize once OBD data exists.
ALL_MODE = "all"

#: OBD channels the operating-mode segmenter needs as inputs.
_SEGMENTER_SIGNAL_COLUMNS = {
    "rpm": "obd:RPM",
    "speed": "obd:SPEED",
    "throttle": "obd:THROTTLE_POS",
}

FEATURE_SPEC_VERSION = 1


def build_feature_frame(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    behavior_models_path: Path = DEFAULT_BEHAVIOR_MODELS_PATH,
    dbc_dir: Path = DEFAULT_DBC_DIR,
) -> pd.DataFrame:
    """Run the full preprocessing pipeline over ``session_dirs`` into a feature frame.

    ``read_session`` → ``decode_can_rows`` (bindings.yaml, if present) →
    ``decode_can_rows_dbc`` (any ``.dbc`` files under ``dbc_dir``, if present —
    Task 1.8 interop hook) → ``to_wide(coerce_numeric=True)`` → ``resample_wide``
    → operating-mode segment (or constant ``"all"`` mode) →
    ``append_deviation_columns`` (behavior models, if present — Task 4.E.7) →
    ``compute_features``.

    The deviation columns land *before* ``compute_features`` so they pick up
    rolling mean/std/roc derivatives just like raw / decoded channels. The
    ``ZScoreScaler`` then normalizes them through ``trainable_feature_columns``
    without changes to either module.
    """
    bindings = load_bindings(bindings_path) if bindings_path.exists() else None
    behavior_models = (
        load_behavior_models(behavior_models_path) if behavior_models_path.exists() else None
    )
    dbc_database = load_dbc_dir(dbc_dir)

    # Decode → pivot → resample one session at a time. resample_wide and
    # compute_features already group by session (no cross-session bleed), so
    # per-session-then-concat is identical to concat-then-resample — but peak
    # memory is now one session's raw bus-rate pivot instead of all of them
    # concatenated, which OOM-kills the Pi on a full corpus.
    resample_config = ResampleConfig(hz=config.preprocessing.resample_hz)
    resampled_parts: list[pd.DataFrame] = []
    skipped: list[str] = []
    for session_dir in session_dirs:
        try:
            long = read_session(session_dir)
            if bindings is not None:
                long = decode_can_rows(long, bindings)
            if dbc_database is not None:
                long = decode_can_rows_dbc(long, dbc_database)
            # Raw CAN frames are ~98% of the rows and carry no numeric value
            # (the bytes live in `payload`); they exist only to be decoded into
            # the decoded:* / dbc:* rows appended above. Once decoded they're
            # dead weight: their pivot columns are all-NaN and dropped by
            # trainable_feature_columns anyway (the model has zero can:*
            # features). Dropping them here is output-equivalent but collapses
            # the to_wide pivot's row volume ~50x — the actual OOM cause.
            long = long[long["source"] != "can"]
            wide = to_wide(long, coerce_numeric=True)
            resampled_parts.append(resample_wide(wide, config=resample_config))
        except Exception as exc:  # noqa: BLE001 — one bad session must not kill the corpus
            skipped.append(session_dir.name)
            log.warning(
                "skipping unreadable session",
                extra={"session": str(session_dir), "error": f"{type(exc).__name__}: {exc}"},
            )
    if not resampled_parts:
        raise ValueError(
            f"no readable sessions among the {len(session_dirs)} given "
            f"(all skipped: {', '.join(skipped) or 'none'})"
        )
    if skipped:
        log.warning(
            "feature frame built from %d/%d sessions; %d skipped: %s",
            len(resampled_parts),
            len(session_dirs),
            len(skipped),
            ", ".join(skipped),
        )
    resampled = pd.concat(resampled_parts, ignore_index=True)

    resampled = segment_or_constant(resampled, config)
    if behavior_models is not None and behavior_models.models:
        resampled = append_deviation_columns(resampled, behavior_models)
    return compute_features(
        resampled,
        config=FeatureConfig(window_seconds=config.preprocessing.rolling_window_seconds),
    )


def segment_or_constant(resampled: pd.DataFrame, config: CanaryConfig) -> pd.DataFrame:
    """Apply the operating-mode segmenter when its inputs exist, else add a
    constant ``operating_mode = "all"`` column so a single model is fit."""
    seg_cfg = SegmenterConfig(signal_columns=dict(_SEGMENTER_SIGNAL_COLUMNS))
    have_inputs = all(col in resampled.columns for col in seg_cfg.signal_columns.values())
    if have_inputs:
        return segment_operating_modes(
            resampled, config.preprocessing.operating_modes, config=seg_cfg
        )
    out = resampled.copy()
    out[MODE_COLUMN] = ALL_MODE
    log.info("no OBD PID columns present; using a single operating mode", extra={"mode": ALL_MODE})
    return out


def trainable_feature_columns(features: pd.DataFrame) -> list[str]:
    """Numeric feature columns with at least one non-NaN value.

    Excludes structural columns (``session``, ``timestamp``, the mode column)
    and gap flags; keeps ``decoded:*`` and OBD channels plus their
    ``__mean``/``__std``/``__roc`` derivatives. Drops all-NaN columns (signals
    whose CAN ID never appeared this session) so the z-score scaler and the
    sequence-window builder don't choke.
    """
    cols: list[str] = []
    for col in candidate_feature_columns(features):
        if not bool(features[col].notna().any()):
            continue
        cols.append(col)
    return cols


def candidate_feature_columns(features: pd.DataFrame) -> list[str]:
    """Numeric, non-structural feature candidates before all-NaN filtering."""
    skip_exact = {"session", "timestamp", MODE_COLUMN}
    cols: list[str] = []
    for raw in features.columns:
        col = str(raw)
        if col in skip_exact or col.endswith("__gap"):
            continue
        if pd.api.types.is_numeric_dtype(features[col]):
            cols.append(col)
    return cols


def build_feature_spec(
    features: pd.DataFrame,
    feature_columns: Sequence[str],
    *,
    config: CanaryConfig | None = None,
    sequence_length: int | None = None,
    sequence_stride: int | None = None,
) -> dict[str, Any]:
    """Return the versioned feature contract persisted in model manifests.

    This is the first explicit train/serve schema artifact for Phase 7:
    ordered feature columns, their dtypes, the preprocessing knobs that shape
    them, source families, and a compact health report. Later 7.1 work can
    compare this spec against the live source before scoring.
    """
    columns = [str(col) for col in feature_columns]
    candidate_columns = candidate_feature_columns(features)
    spec: dict[str, Any] = {
        "feature_spec_version": FEATURE_SPEC_VERSION,
        "feature_columns": columns,
        "feature_dtypes": {column: str(features[column].dtype) for column in columns},
        "source_prefixes": sorted({_source_prefix(column) for column in columns}),
        "behavior_model_columns": sorted(
            {_base_feature_name(column) for column in columns if column.startswith("deviation:")}
        ),
        "feature_health": summarize_feature_health(
            features,
            columns,
            candidate_columns=candidate_columns,
        ),
    }
    if config is not None:
        spec["preprocessing"] = {
            "resample_hz": config.preprocessing.resample_hz,
            "rolling_window_seconds": config.preprocessing.rolling_window_seconds,
            "operating_modes": sorted(config.preprocessing.operating_modes),
        }
    if sequence_length is not None:
        spec["sequence_length"] = sequence_length
    if sequence_stride is not None:
        spec["sequence_stride"] = sequence_stride
    return spec


def summarize_feature_health(
    features: pd.DataFrame,
    feature_columns: Sequence[str],
    *,
    candidate_columns: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Summarize missingness, finiteness, and constant features.

    ``candidate_columns`` lets callers report numeric columns dropped before
    training, especially all-NaN decoded/deviation columns.
    """
    columns = [str(col) for col in feature_columns]
    candidates = [str(col) for col in (candidate_columns or columns)]
    selected = set(columns)

    all_nan_columns: list[str] = []
    constant_columns: list[str] = []
    non_finite_columns: list[str] = []
    column_health: dict[str, dict[str, Any]] = {}

    for column in columns:
        health = _column_health(features[column])
        column_health[column] = health
        if health["non_null_count"] == 0:
            all_nan_columns.append(column)
        if health["non_finite_count"] > 0:
            non_finite_columns.append(column)
        if health["constant"]:
            constant_columns.append(column)

    dropped_all_nan_columns = [
        column
        for column in candidates
        if column not in selected
        and column in features.columns
        and pd.api.types.is_numeric_dtype(features[column])
        and not bool(features[column].notna().any())
    ]

    return {
        "row_count": int(len(features)),
        "candidate_feature_count": len(candidates),
        "selected_feature_count": len(columns),
        "dropped_all_nan_columns": dropped_all_nan_columns,
        "all_nan_columns": all_nan_columns,
        "constant_columns": constant_columns,
        "non_finite_columns": non_finite_columns,
        "columns": column_health,
    }


def validate_feature_health(feature_health: Mapping[str, Any]) -> None:
    """Fail fast on selected columns that cannot produce meaningful scores."""
    all_nan = [str(col) for col in feature_health.get("all_nan_columns", [])]
    if all_nan:
        raise ValueError("selected feature columns are all-NaN: " + ", ".join(all_nan))


def _column_health(series: pd.Series) -> dict[str, Any]:
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=np.float64, copy=False)
    finite_mask = np.isfinite(values)
    finite_values = values[finite_mask]
    non_null_count = int(series.notna().sum())
    finite_count = int(finite_mask.sum())
    unique_finite_count = int(pd.Series(finite_values).nunique(dropna=True))
    return {
        "dtype": str(series.dtype),
        "non_null_count": non_null_count,
        "finite_count": finite_count,
        "nan_count": int(series.isna().sum()),
        "non_finite_count": non_null_count - finite_count,
        "unique_finite_count": unique_finite_count,
        "constant": finite_count > 0 and unique_finite_count <= 1,
    }


def _base_feature_name(column: str) -> str:
    for suffix in ("__mean", "__std", "__roc"):
        if column.endswith(suffix):
            return column[: -len(suffix)]
    return column


def _source_prefix(column: str) -> str:
    base = _base_feature_name(column)
    if ":" not in base:
        return "derived"
    return base.split(":", 1)[0]
