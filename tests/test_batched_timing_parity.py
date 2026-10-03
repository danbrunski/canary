"""Timing parity for batched Mode 01 reads (D03.24 b).

A batched Mode 01 request returns up to six PIDs in one response, so all six
readings carry **one** timestamp where six single reads each had their own
(~1.5 ms apart on this ECM). Batching has been in daily use since 2026-07-26;
nothing had checked that the feature pipeline treats the two shapes alike.
The risks are specific: the pivot could split or merge rows differently, a
shared timestamp could land six channels on a resample-bin edge together, the
``__gap`` bookkeeping could disagree when a whole batch goes missing at once,
and ``__roc`` divides by elapsed time, which is zero between rows that share a
timestamp.

Both paths are covered: the batch chain (``to_wide`` -> ``resample_wide`` ->
``compute_features``, as ``build_feature_frame`` runs it per session) and the
live serve path (``LiveFeatureSource`` gap flags and bounded values).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.columns import gap_column, roc_column
from src.config import CanaryConfig
from src.features.live import LiveFeatureSource
from src.preprocessing.features import compute_features
from src.preprocessing.loader import to_wide
from src.preprocessing.resampler import ResampleConfig, resample_wide
from src.telemetry import TelemetryRecord
from tests.configs import example_config

#: Six fast Mode 01 channels — one full batch (the ECM's six-PID limit).
BATCH = ("RPM", "SPEED", "ENGINE_LOAD", "THROTTLE_POS", "MAF", "TIMING_ADVANCE")
COLUMNS = tuple(f"obd:{name}" for name in BATCH)
BASE = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
#: Single reads on this ECM land ~1.5 ms apart (two DIDs are two requests).
SINGLE_READ_SPACING_S = 0.0015
POLL_PERIOD_S = 0.2
DURATION_S = 60.0
#: Every channel silent at once — what a failed batched request looks like.
OUTAGE_S = (25.0, 31.0)


def _value(channel_index: int, t: float) -> float:
    # A distinct linear ramp per channel: the true derivative is known, so
    # ``__roc`` can be checked against it rather than only between shapes.
    # Kept inside every channel's plausible range (load/throttle <= 100 %,
    # timing <= 63.5 deg), or the live quality gate would reject the reading.
    return 5.0 * (channel_index + 1) + _slope(channel_index) * t


def _slope(channel_index: int) -> float:
    return 0.05 * (channel_index + 1)


def _long_frame(*, batched: bool) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    # Offset 0.3 s keeps every read clear of a 1 Hz bin edge, so the two
    # shapes differ *only* in the shared-vs-staggered timestamps.
    t = 0.3
    while t < DURATION_S:
        if not OUTAGE_S[0] <= t < OUTAGE_S[1]:
            for i, column in enumerate(COLUMNS):
                offset = 0.0 if batched else i * SINGLE_READ_SPACING_S
                rows.append(
                    {
                        "session": "s",
                        "timestamp": BASE + timedelta(seconds=t + offset),
                        "source_channel": column,
                        "value": _value(i, t),
                    }
                )
        t = round(t + POLL_PERIOD_S, 6)
    return pd.DataFrame(rows)


def _chain(long: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    wide = to_wide(long, coerce_numeric=True)
    resampled = resample_wide(wide, config=ResampleConfig(hz=1.0))
    return wide, compute_features(resampled)


def test_batched_rows_pivot_to_one_fully_populated_row_per_batch() -> None:
    batched_wide, _ = _chain(_long_frame(batched=True))
    single_wide, _ = _chain(_long_frame(batched=False))
    reads = len(_long_frame(batched=True)) // len(COLUMNS)
    assert len(batched_wide) == reads
    assert not batched_wide[list(COLUMNS)].isna().any().any()
    # Single reads pivot to one sparse row per read.
    assert len(single_wide) == reads * len(COLUMNS)


def test_batched_and_single_reads_resample_and_featurize_identically() -> None:
    _, batched = _chain(_long_frame(batched=True))
    _, single = _chain(_long_frame(batched=False))
    assert list(batched["timestamp"]) == list(single["timestamp"])
    for column in COLUMNS:
        for name in (
            column,
            gap_column(column),
            roc_column(column),
            f"{column}__mean",
            f"{column}__std",
        ):
            np.testing.assert_array_equal(
                batched[name].to_numpy(), single[name].to_numpy(), err_msg=name
            )


def test_whole_batch_outage_flags_every_channel_together() -> None:
    _, features = _chain(_long_frame(batched=True))
    gaps = features[[gap_column(c) for c in COLUMNS]].astype(bool)
    # All six flip on exactly the same grid rows — never a partial batch.
    assert (gaps.all(axis=1) == gaps.any(axis=1)).all()
    flagged = gaps.all(axis=1)
    assert flagged.any()
    elapsed = (features["timestamp"] - features["timestamp"].iloc[0]).dt.total_seconds()
    # Grid 0 precedes the first read (0.3 s). The last read before the outage
    # is 24.9 s, so grid 25/26 carry it within max_gap (2 s) and grid 27..31
    # are stale until reads resume at 31.1 s.
    assert list(elapsed[flagged]) == [0.0, 27.0, 28.0, 29.0, 30.0, 31.0]
    # A stale cell is NaN, not a carried value, and its derivative too.
    for column in COLUMNS:
        assert features.loc[flagged, column].isna().all()
        assert features.loc[flagged, roc_column(column)].isna().all()


def test_roc_matches_the_known_slope_away_from_the_outage() -> None:
    """``__roc`` over a batch is the true slope, never a 0/0 from rows that
    share a timestamp. Rows whose 30 s look-back lands on grid 25/26 are
    excluded: those cells hold the 24.9 s reading carried forward inside
    max_gap, a resampler property that the parity test above shows batched
    and single reads share."""
    _, features = _chain(_long_frame(batched=True))
    elapsed = (features["timestamp"] - features["timestamp"].iloc[0]).dt.total_seconds()
    clean = (elapsed >= 32) & (elapsed <= 54)
    for i, column in enumerate(COLUMNS):
        roc = features.loc[clean, roc_column(column)].to_numpy()
        assert np.isfinite(roc).all(), column
        np.testing.assert_allclose(roc, _slope(i), rtol=1e-4, err_msg=column)


@pytest.fixture
def config() -> CanaryConfig:
    return example_config(Path("canary.example.yaml"))


def _live_gap_history(config: CanaryConfig, *, batched: bool) -> list[dict[str, object]]:
    src = LiveFeatureSource(config=config)
    history: list[dict[str, object]] = []
    for second in range(6):
        if second in (0, 1):  # two batches, then the whole batch goes silent
            for i, name in enumerate(BATCH):
                offset = 0.0 if batched else i * SINGLE_READ_SPACING_S
                stamp = (BASE + timedelta(seconds=second + offset)).timestamp()
                src.observe(TelemetryRecord.obd(stamp, name, _value(i, second), ""))
        # An unrelated always-fresh channel keeps the tick from being
        # fully-stale (a separate rule); this is about the batch's own flags.
        src.observe(
            TelemetryRecord.obd(
                (BASE + timedelta(seconds=second)).timestamp(), "FUEL_LEVEL", 50.0, "%"
            )
        )
        row = src.tick(BASE + timedelta(seconds=second, milliseconds=10))
        assert row is not None
        history.append(
            {
                "gaps": tuple(bool(row[gap_column(c)].iloc[0]) for c in COLUMNS),
                "present": tuple(bool(pd.notna(row[c].iloc[0])) for c in COLUMNS),
            }
        )
    return history


def test_live_path_ages_a_batch_as_one(config: CanaryConfig) -> None:
    batched = _live_gap_history(config, batched=True)
    single = _live_gap_history(config, batched=False)
    assert batched == single
    for tick in batched:
        # Six channels observed at one instant age together: never split.
        assert len(set(tick["gaps"])) == 1  # type: ignore[arg-type]
        assert tick["present"] == tuple(not g for g in tick["gaps"])  # type: ignore[union-attr]
    assert [tick["gaps"][0] for tick in batched] == [False, False, False, True, True, True]  # type: ignore[index]
