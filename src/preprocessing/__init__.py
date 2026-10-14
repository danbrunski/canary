"""Preprocessing utilities for model-ready telemetry frames, resolved lazily.

This package imports nothing when it is imported: every
``src.preprocessing.*`` import executes this file first, and the collection
path used to import ``src.preprocessing.resampler`` (for column names)
through ``src.analysis.derived``. When this file imported every submodule
eagerly that pulled the loader, the feature builder, the segmenter and the
anomaly injector onto the main thread before any collector ran (S01.3). The
names below still resolve as ``src.preprocessing.<name>``; the module behind
one is imported on first access. Column names are not among them: they live in
:mod:`src.columns` (R09.2), and are imported from there.
"""

from __future__ import annotations

import importlib
from typing import Any

#: Export name -> the module that defines it.
_EXPORTS: dict[str, str] = {
    "DECODED_SOURCE": "src.preprocessing.decode",
    "AnomalyInjector": "src.preprocessing.anomaly_injection",
    "AnomalyKind": "src.preprocessing.anomaly_injection",
    "FeatureConfig": "src.preprocessing.features",
    "InjectionWindow": "src.preprocessing.anomaly_injection",
    "ResampleConfig": "src.preprocessing.resampler",
    "SchemaMismatchError": "src.preprocessing.normalization",
    "SegmenterConfig": "src.preprocessing.segmenter",
    "ZScoreScaler": "src.preprocessing.normalization",
    "channel_key": "src.telemetry",
    "compute_features": "src.preprocessing.features",
    "decode_can_rows": "src.preprocessing.decode",
    "decoded_channel": "src.preprocessing.decode",
    "discover_sessions": "src.preprocessing.session_reader",
    "mode_for_row": "src.preprocessing.segmenter",
    "read_session": "src.preprocessing.session_reader",
    "read_session_decoded": "src.preprocessing.decoded_reader",
    "read_sessions": "src.preprocessing.session_reader",
    "resample_wide": "src.preprocessing.resampler",
    "segment_operating_modes": "src.preprocessing.segmenter",
    "to_wide": "src.preprocessing.wide_frame",
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
