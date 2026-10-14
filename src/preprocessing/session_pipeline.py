"""The cached session pipeline: ``read → reduce/wide → resample → segment`` (R13.2).

The top of the preprocessing chain. :func:`resample_and_segment` chooses the
policy the lower modules must not: the bounded ``value_number`` reduction
(optionally through the derived store, with the session's IMU frame) or the
whole-session wide pivot. It consumes the readers (``session_reader``), the
frame transforms (``wide_frame``, ``imu_decimation``) and the reduction,
cache and IMU services (``derived``, ``derived_store``, ``imu_stream``), and
nothing imports it except the ``loader`` compatibility facade and external
callers. The reader and the services below it never import this module, which
is what removed the ``loader`` ↔ ``derived`` / ``derived_store`` /
``imu_stream`` cycle.

The reduction, cache and IMU services are imported inside
:func:`resample_and_segment`'s ``value_number`` branch, **for start-up cost,
not for a cycle** (nothing imports this module from below). ``loader`` re-exports
this function and ``src.models.sequence_dataset`` imports it, which puts it on
the daemon's recording path (``tests/test_cold_start_imports.py``); importing
those services eagerly added 13 modules and ~22 ms (warm, laptop) to that boot
before any recording could start.
``tests/test_preprocessing_boundaries.py`` pins that ``import loader`` loads
none of them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd

from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.config import CanaryConfig
from src.preprocessing.imu_decimation import decimate_imu_wide
from src.preprocessing.resampler import resample_config_for, resample_wide
from src.preprocessing.segmenter import segment_operating_modes
from src.preprocessing.session_contracts import TelemetryValueKind
from src.preprocessing.session_reader import read_sessions
from src.preprocessing.wide_frame import numeric_value_columns, to_wide

log = logging.getLogger(__name__)


def resample_and_segment(
    session_dirs: Iterable[Path],
    config: CanaryConfig,
    *,
    value: TelemetryValueKind = "value_number",
) -> pd.DataFrame:
    """Load session Parquet and run the shared ``read → wide → resample →
    segment`` chain used by every batch preprocessing entry point.

    Returns the segmented (but not yet feature-computed) frame. Empty input
    (no sessions, no rows, no numeric channels) flows through to an empty or
    minimal-column result rather than raising — callers decide what an empty
    result means for them (skip, quarantine, raise).
    """
    sessions = [Path(path) for path in session_dirs]
    resample_config = resample_config_for(config)
    if value == "value_number":
        # One session at a time, each read part by part (P02.1): a part of
        # ~SESSION_PART_ROWS is reduced to the cells the resampler reads before
        # the next is read, so memory is one part plus the session's reduced
        # cells, never the whole session. That holds with or without a store:
        # the store (laptop tools, P01.2) only reads back the same cells
        # instead of rebuilding them. It is never active on the Pi
        # (``active_store``), where the digest and classification children run
        # this same bounded chain.
        # H02.4: each session's resolved imu_frame.json adds imu:ACCEL_LON/LAT
        # inside that reduction (``imu_stream``), and ``attrs[IMU_FRAME_ATTR]``
        # says per session whether it did and, if not, which kind of absent.
        from src.preprocessing.derived import reduce_numeric_session, resample_numeric_reduced
        from src.preprocessing.derived_store import active_store
        from src.preprocessing.imu_stream import IMU_FRAME_ATTR, plan_imu_frame

        store = active_store()
        reduced: list[pd.DataFrame] = []
        imu_frames: dict[str, dict[str, Any]] = {}
        for session in sessions:
            plan = plan_imu_frame(session)
            session_cells = (
                store.load_numeric(session, resample_config)[0]
                if store is not None
                else reduce_numeric_session(session, resample_config, imu_plan=plan)
            )
            use = plan.use(session_cells.channels)
            if use.status in ("unresolved", "stale", "refused"):
                log.info("%s: IMU frame not applied (%s): %s", use.session, use.status, use.reason)
            imu_frames[use.session] = use.as_dict()
            reduced.append(resample_numeric_reduced(session_cells, resample_config))
            del session_cells
        resampled = pd.concat(reduced, ignore_index=True) if reduced else pd.DataFrame()
        segmented = segment_operating_modes(
            resampled,
            config.preprocessing.operating_modes,
            recent_motion_window_seconds=config.preprocessing.idle_transient_lookback_seconds,
            mode_dwell_ticks=config.preprocessing.mode_dwell_ticks,
        )
        segmented.attrs[IMU_FRAME_ATTR] = imu_frames
        return segmented

    long = read_sessions(sessions)
    wide = to_wide(long, value=value)
    # 2a.3 — collapse any raw-rate imu:ACCEL_LON/LAT into per-resample-tick
    # MAX/RMS/JERK_MAX aggregates before the resampler's own .last() would
    # otherwise pick one arbitrary raw sample per tick. No-op when neither
    # column is present (every session recorded before Phase 2b hardware).
    wide = decimate_imu_wide(wide, hz=config.preprocessing.resample_hz)
    value_columns = numeric_value_columns(wide)
    if value_columns:
        resampled = resample_wide(
            wide,
            config=resample_config,
            value_columns=value_columns,
        )
    else:
        resampled = pd.DataFrame(
            {
                SESSION_COLUMN: wide[SESSION_COLUMN].copy()
                if SESSION_COLUMN in wide.columns
                else pd.Series(dtype="object"),
                TIMESTAMP_COLUMN: pd.to_datetime(wide[TIMESTAMP_COLUMN], utc=True)
                if TIMESTAMP_COLUMN in wide.columns
                else pd.Series(dtype="datetime64[ns, UTC]"),
            }
        )
    return segment_operating_modes(
        resampled,
        config.preprocessing.operating_modes,
        recent_motion_window_seconds=config.preprocessing.idle_transient_lookback_seconds,
        mode_dwell_ticks=config.preprocessing.mode_dwell_ticks,
    )
