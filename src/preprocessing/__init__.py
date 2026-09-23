"""Preprocessing utilities for model-ready telemetry frames, resolved lazily.

This package imports nothing when it is imported: every
``src.preprocessing.*`` import executes this file first, and the collection
path imports ``src.preprocessing.resampler`` (for column names) through
``src.analysis.derived``. When this file imported every submodule eagerly
that pulled the loader, the feature builder, the segmenter and the anomaly
injector onto the main thread before any collector ran (S01.3). The names
below still resolve as ``src.preprocessing.<name>``; the module behind one is
imported on first access.
"""

from __future__ import annotations

import importlib
from typing import Any

#: Export name -> the module that defines it.
_EXPORTS: dict[str, str] = {
    "DECODED_SOURCE": "src.preprocessing.decode",
    "MODE_COLUMN": "src.preprocessing.segmenter",
    "AnomalyInjector": "src.preprocessing.anomaly_injection",
    "AnomalyKind": "src.preprocessing.anomaly_injection",
    "FeatureConfig": "src.preprocessing.features",
    "InjectionWindow": "src.preprocessing.anomaly_injection",
    "ResampleConfig": "src.preprocessing.resampler",
    "SchemaMismatchError": "src.preprocessing.normalization",
    "SegmenterConfig": "src.preprocessing.segmenter",
    "UNKNOWN_MODE": "src.preprocessing.segmenter",
    "ZScoreScaler": "src.preprocessing.normalization",
    "channel_key": "src.preprocessing.loader",
    "compute_features": "src.preprocessing.features",
    "decode_can_rows": "src.preprocessing.decode",
    "decoded_channel": "src.preprocessing.decode",
    "discover_sessions": "src.preprocessing.loader",
    "gap_column": "src.preprocessing.resampler",
    "mean_column": "src.preprocessing.features",
    "mode_for_row": "src.preprocessing.segmenter",
    "read_session": "src.preprocessing.loader",
    "read_session_decoded": "src.preprocessing.decode",
    "read_sessions": "src.preprocessing.loader",
    "resample_wide": "src.preprocessing.resampler",
    "roc_column": "src.preprocessing.features",
    "segment_operating_modes": "src.preprocessing.segmenter",
    "std_column": "src.preprocessing.features",
    "to_wide": "src.preprocessing.loader",
    "windows_to_records": "src.preprocessing.anomaly_injection",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


__all__ = list(_EXPORTS)
