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
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from src.config import CanaryConfig
from src.decode.bindings import load_bindings
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


def build_feature_frame(
    session_dirs: Sequence[Path],
    config: CanaryConfig,
    *,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
) -> pd.DataFrame:
    """Run the full preprocessing pipeline over ``session_dirs`` into a feature frame.

    ``read_session`` → ``decode_can_rows`` (if a bindings file is present) →
    ``to_wide(coerce_numeric=True)`` → ``resample_wide`` → operating-mode segment
    (or a constant ``"all"`` mode) → ``compute_features``.
    """
    bindings = load_bindings(bindings_path) if bindings_path.exists() else None

    long_parts: list[pd.DataFrame] = []
    for session_dir in session_dirs:
        long = read_session(session_dir)
        if bindings is not None:
            long = decode_can_rows(long, bindings)
        long_parts.append(long)
    long_frame = pd.concat(long_parts, ignore_index=True)

    wide = to_wide(long_frame, coerce_numeric=True)
    resampled = resample_wide(wide, config=ResampleConfig(hz=config.preprocessing.resample_hz))
    resampled = segment_or_constant(resampled, config)
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
    skip_exact = {"session", "timestamp", MODE_COLUMN}
    cols: list[str] = []
    for raw in features.columns:
        col = str(raw)
        if col in skip_exact or col.endswith("__gap"):
            continue
        if not pd.api.types.is_numeric_dtype(features[col]):
            continue
        if not bool(features[col].notna().any()):
            continue
        cols.append(col)
    return cols
