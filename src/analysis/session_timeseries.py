"""Historical session time-series read (Task 5.3, engine-analysis-and-mapping.md).

Turns a stored session's raw telemetry back into plottable per-channel
arrays — the read side of "load a stored session instead of the live
stream" (``ChartsPanel`` session mode). Two shapes, picked by whether the
caller wants a shared alignment:

* No ``resample_hz``: each channel keeps its own native timestamps (an OBD
  PID and a CAN signal were never sampled in lockstep, and forcing them onto
  one grid here would silently manufacture values a plain "show me this
  session's throttle trace" request never asked for).
* ``resample_hz`` given: every channel is aligned onto one shared uniform
  time grid via :func:`src.analysis.binned_analysis.rows_to_resampled_frame`
  (the same forward-fill alignment Task 5.1's binning and the training
  pipeline use) — for overlaying channels that need to line up sample-for-
  sample (comparing a derived channel against its OBD inputs, etc).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from src.analysis.binned_analysis import rows_to_resampled_frame


def _iso(ts: Any) -> str:
    return pd.Timestamp(ts).isoformat()


def compute_time_series(
    rows: Sequence[Mapping[str, Any]],
    *,
    channels: Sequence[str],
    resample_hz: float | None = None,
) -> dict[str, Any]:
    """One entry per channel: ``{"timestamps": [...], "values": [...]}``,
    plus ``resampled``/``hz`` describing which of the two shapes above was
    used.

    A channel absent from ``rows`` (never captured this session, or a typo)
    still gets an entry with empty arrays — the caller doesn't need to
    special-case "channel missing" vs. "channel present but empty".
    """
    if not rows:
        return {
            "resampled": False,
            "hz": None,
            "channels": {ch: {"timestamps": [], "values": []} for ch in channels},
        }

    if resample_hz is None:
        frame = pd.DataFrame(rows)
        result: dict[str, Any] = {}
        for ch in channels:
            sub = frame[frame["channel_key"] == ch].sort_values("timestamp")
            result[ch] = {
                "timestamps": [_iso(ts) for ts in sub["timestamp"]],
                "values": [float(v) for v in sub["value_number"]],
            }
        return {"resampled": False, "hz": None, "channels": result}

    resampled = rows_to_resampled_frame(rows, channels, resample_hz=resample_hz)
    if resampled.empty:
        return {
            "resampled": True,
            "hz": resample_hz,
            "channels": {ch: {"timestamps": [], "values": []} for ch in channels},
        }
    timestamps = [_iso(ts) for ts in resampled["timestamp"]]
    result = {}
    for ch in channels:
        if ch not in resampled.columns:
            result[ch] = {"timestamps": [], "values": []}
            continue
        col = pd.to_numeric(resampled[ch], errors="coerce")
        result[ch] = {
            "timestamps": timestamps,
            "values": [None if pd.isna(v) else float(v) for v in col],
        }
    return {"resampled": True, "hz": resample_hz, "channels": result}


__all__ = ["compute_time_series"]
