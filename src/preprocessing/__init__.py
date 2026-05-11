"""Preprocessing utilities for model-ready telemetry frames."""

from src.preprocessing.anomaly_injection import (
    AnomalyInjector,
    AnomalyKind,
    InjectionWindow,
    windows_to_records,
)
from src.preprocessing.features import (
    FeatureConfig,
    compute_features,
    mean_column,
    roc_column,
    std_column,
)
from src.preprocessing.loader import (
    channel_key,
    discover_sessions,
    read_session,
    read_sessions,
    to_wide,
)
from src.preprocessing.normalization import SchemaMismatchError, ZScoreScaler
from src.preprocessing.resampler import ResampleConfig, gap_column, resample_wide
from src.preprocessing.segmenter import (
    MODE_COLUMN,
    UNKNOWN_MODE,
    SegmenterConfig,
    mode_for_row,
    segment_operating_modes,
)

__all__ = [
    "MODE_COLUMN",
    "AnomalyInjector",
    "AnomalyKind",
    "FeatureConfig",
    "InjectionWindow",
    "ResampleConfig",
    "SchemaMismatchError",
    "SegmenterConfig",
    "UNKNOWN_MODE",
    "ZScoreScaler",
    "channel_key",
    "compute_features",
    "discover_sessions",
    "gap_column",
    "mean_column",
    "mode_for_row",
    "read_session",
    "read_sessions",
    "resample_wide",
    "roc_column",
    "segment_operating_modes",
    "std_column",
    "to_wide",
    "windows_to_records",
]
