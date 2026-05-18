"""Shared offline feature pipeline: recorded sessions → model-ready feature frame.

Both anomaly trainers (Isolation Forest and LSTM autoencoder) consume the same
preprocessing path: read recorded Parquet sessions, decode CAN payloads via
``bindings.yaml`` into numeric columns, pivot to wide, resample to a uniform
grid, segment operating modes (or fall back to a single ``"all"`` mode when no
OBD PIDs are present), and compute rolling features. Keeping it here means the
training CLIs stay thin and behave identically.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
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
from src.preprocessing.loader import PARQUET_PATTERN, read_session, to_wide
from src.preprocessing.resampler import ResampleConfig, resample_wide
from src.preprocessing.segmenter import (
    MODE_COLUMN,
    SegmenterConfig,
    segment_operating_modes,
)

log = logging.getLogger(__name__)

#: Default location of the hand-edited CAN signal bindings file.
DEFAULT_BINDINGS_PATH = Path("data/bindings.yaml")

#: Default retrain-time cache for model-ready feature frames.
DEFAULT_FEATURE_CACHE_DIR = Path("data/cache/feature_frames")

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
FEATURE_FRAME_CACHE_VERSION = 1


def build_feature_frame(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    behavior_models_path: Path = DEFAULT_BEHAVIOR_MODELS_PATH,
    dbc_dir: Path = DEFAULT_DBC_DIR,
    cache_dir: Path | None = None,
    force_rebuild: bool = False,
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
    session_dirs = [Path(session_dir) for session_dir in session_dirs]
    cache_path: Path | None = None
    cache_metadata: dict[str, Any] | None = None
    if cache_dir is not None:
        cache_metadata = _feature_frame_cache_metadata(
            session_dirs,
            config,
            bindings_path=bindings_path,
            behavior_models_path=behavior_models_path,
            dbc_dir=dbc_dir,
        )
        cache_path = Path(cache_dir) / f"{cache_metadata['cache_key']}.pkl"
        if not force_rebuild and cache_path.exists():
            try:
                cached = pd.read_pickle(cache_path)
                if not isinstance(cached, pd.DataFrame):
                    raise TypeError(f"cached object is {type(cached).__name__}, not DataFrame")
                log.info(
                    "loaded feature frame cache",
                    extra={
                        "cache": str(cache_path),
                        "rows": len(cached),
                        "columns": len(cached.columns),
                    },
                )
                return cached
            except Exception as exc:  # noqa: BLE001 - cache corruption should not fail retrain
                log.warning(
                    "feature frame cache unreadable; rebuilding",
                    extra={"cache": str(cache_path), "error": str(exc)},
                )

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
    features = compute_features(
        resampled,
        config=FeatureConfig(window_seconds=config.preprocessing.rolling_window_seconds),
    )
    if cache_path is not None and cache_metadata is not None:
        _write_feature_frame_cache(cache_path, features, cache_metadata)
    return features


def build_feature_frame_cache_key(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    behavior_models_path: Path = DEFAULT_BEHAVIOR_MODELS_PATH,
    dbc_dir: Path = DEFAULT_DBC_DIR,
) -> str:
    """Return the deterministic cache key for a feature-frame build."""
    return str(
        _feature_frame_cache_metadata(
            [Path(session_dir) for session_dir in session_dirs],
            config,
            bindings_path=bindings_path,
            behavior_models_path=behavior_models_path,
            dbc_dir=dbc_dir,
        )["cache_key"]
    )


def _feature_frame_cache_metadata(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path,
    behavior_models_path: Path,
    dbc_dir: Path,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cache_version": FEATURE_FRAME_CACHE_VERSION,
        "sessions": [_session_cache_entry(session_dir) for session_dir in session_dirs],
        "preprocessing": config.preprocessing.model_dump(mode="json"),
        "bindings": _file_cache_entry(bindings_path),
        "behavior_models": _file_cache_entry(behavior_models_path),
        "dbc": _directory_cache_entry(dbc_dir, pattern="*.dbc"),
    }
    return {**payload, "cache_key": _stable_hash(payload)}


def _session_cache_entry(session_dir: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for file in sorted(session_dir.glob(PARQUET_PATTERN)):
        try:
            stat = file.stat()
        except OSError:
            continue
        files.append(
            {
                "name": file.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return {
        "path": _stable_path(session_dir),
        "name": session_dir.name,
        "files": files,
    }


def _directory_cache_entry(directory: Path, *, pattern: str) -> dict[str, Any]:
    if not directory.exists():
        return {"path": _stable_path(directory), "exists": False, "files": []}
    files = [
        _file_cache_entry(file)
        for file in sorted(directory.glob(pattern))
        if file.is_file()
    ]
    return {"path": _stable_path(directory), "exists": True, "files": files}


def _file_cache_entry(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {"path": _stable_path(path), "exists": False}
    stat = path.stat()
    return {
        "path": _stable_path(path),
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": _file_sha256(path),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def _stable_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_feature_frame_cache(
    cache_path: Path,
    features: pd.DataFrame,
    metadata: Mapping[str, Any],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.tmp")
    meta_path = cache_path.with_suffix(".json")
    meta_tmp_path = meta_path.with_name(f".{meta_path.name}.{os.getpid()}.tmp")
    try:
        features.to_pickle(tmp_path)
        tmp_path.replace(cache_path)
        sidecar = {
            **dict(metadata),
            "rows": int(len(features)),
            "columns": [str(column) for column in features.columns],
        }
        meta_tmp_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n")
        meta_tmp_path.replace(meta_path)
        log.info(
            "wrote feature frame cache",
            extra={
                "cache": str(cache_path),
                "rows": len(features),
                "columns": len(features.columns),
            },
        )
    except Exception as exc:  # noqa: BLE001 - cache write failures should not fail training
        for path in (tmp_path, meta_tmp_path):
            try:
                path.unlink()
            except OSError as cleanup_exc:
                log.debug(
                    "failed to remove incomplete feature-frame cache file",
                    extra={"path": str(path), "error": str(cleanup_exc)},
                )
        log.warning(
            "feature frame cache write failed; continuing without cache",
            extra={"cache": str(cache_path), "error": str(exc)},
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


#: Prefix of behavior-model deviation columns (raw + __mean/__std/__roc).
DEVIATION_PREFIX = "deviation:"


def sequence_feature_columns(features: pd.DataFrame) -> list[str]:
    """LSTM-autoencoder feature columns: dense sensor sequences only.

    Differs from :func:`trainable_feature_columns` (which the Isolation Forest
    uses) in two ways the sequence windower requires, because
    ``_window_has_missing_features`` rejects a whole window on *any* NaN:

    * **Drops the entire ``deviation:`` family** (raw and rolling aggregates).
      Deviations are behavior-model-derived scores that are NaN by contract
      whenever the model can't speak (off operating-regime) — scattered NaN,
      not a gap. They are designed for the IF path, which imputes; an
      autoencoder reconstructing sensor *sequences* should not consume them,
      and keeping them collapses every window outside their regime.
    * **Keeps only columns observed in every session.** A channel present in
      some drives but not this one is all-NaN for that session; in the
      cross-session union that silently rejects every window in the sessions
      missing it. Requiring presence in all sessions yields a schema that is
      dense by construction (the resampler already forward-fills within a
      session), so windows actually form.
    """
    base = [
        col
        for col in trainable_feature_columns(features)
        if not col.startswith(DEVIATION_PREFIX)
    ]
    if "session" not in features.columns or features["session"].nunique() <= 1:
        return base
    notna = features[base].notna()
    observed_per_session = notna.groupby(features["session"].to_numpy()).any()
    return [col for col in base if bool(observed_per_session[col].all())]


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
