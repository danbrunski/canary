"""Whole-frame feature computation must equal the per-column loop it replaced.

`_features_one_session` used to call pandas once per channel — `windowed[col]
.mean()`, `windowed[col].std()`, and a three-call `shift`/subtract/divide for
ROC. Profiling the live tick found ~135 such calls per tick and 70.8 % of the
stage's runtime inside pandas machinery against 3.9 % in numpy: dispatch
overhead, not arithmetic. The whole-frame forms compute the same values in one
pass (measured 155 ms -> 51 ms on the workstation benchmark).

These features feed trained IF and sequence models, so "equivalent" is not
good enough and neither is `allclose`: the reference implementation below is
the verbatim pre-change code, and the outputs must match **exactly**, NaN
positions included. If a future refactor makes them diverge by one ULP, that
is a model-parity change and it should fail here first.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.preprocessing.features import (
    FEATURE_DTYPE,
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
    FeatureConfig,
    _rows_in_window,
    _value_columns,
    compute_features,
    mean_column,
    roc_column,
    std_column,
)


def _reference_one_session(
    group: pd.DataFrame, columns: list[str], cfg: FeatureConfig
) -> pd.DataFrame:
    """Verbatim per-column implementation, as it stood before batching."""
    ordered = group.sort_values(TIMESTAMP_COLUMN, kind="stable").reset_index(drop=True)
    timestamps = pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True)
    new_cols: dict[str, np.ndarray] = {}

    if cfg.include_mean or cfg.include_std:
        windowed = ordered.set_index(timestamps)[list(columns)].rolling(
            f"{cfg.window_seconds}s", min_periods=cfg.min_periods
        )
        if cfg.include_mean:
            for col in columns:
                new_cols[mean_column(col)] = windowed[col].mean().to_numpy(dtype=FEATURE_DTYPE)
        if cfg.include_std:
            for col in columns:
                new_cols[std_column(col)] = windowed[col].std().to_numpy(dtype=FEATURE_DTYPE)

    if cfg.include_roc:
        n_rows = _rows_in_window(timestamps, cfg.window_seconds)
        elapsed = (timestamps - timestamps.shift(n_rows)).dt.total_seconds()
        safe_elapsed = elapsed.where(elapsed > 0)
        for col in columns:
            roc = (ordered[col] - ordered[col].shift(n_rows)) / safe_elapsed
            new_cols[roc_column(col)] = roc.to_numpy(dtype=FEATURE_DTYPE)

    value_downcast = {col: FEATURE_DTYPE for col in columns}
    if not new_cols:
        return ordered.astype(value_downcast)
    to_replace = [c for c in new_cols if c in ordered.columns]
    base = ordered.drop(columns=to_replace) if to_replace else ordered
    base = base.astype({c: t for c, t in value_downcast.items() if c in base.columns})
    return pd.concat([base, pd.DataFrame(new_cols, index=base.index)], axis=1)


def _reference_compute(resampled: pd.DataFrame, cfg: FeatureConfig) -> pd.DataFrame:
    columns = list(_value_columns(resampled, None))
    parts = [
        _reference_one_session(group, columns, cfg)
        for _session, group in resampled.groupby(
            SESSION_COLUMN, sort=True, group_keys=False, dropna=False
        )
    ]
    return pd.concat(parts, ignore_index=True)


def _frame(n_rows: int, n_cols: int, *, sessions: int = 1, seed: int = 0) -> pd.DataFrame:
    """A resampled-shaped frame with the awkward cases in it: NaN holes, a
    constant column (zero variance), a duplicate timestamp, and a time gap."""
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2026-09-09T12:00:00Z")
    ts = [start + pd.Timedelta(seconds=i) for i in range(n_rows)]
    if n_rows > 12:
        ts[10] = ts[9]  # duplicate timestamp -> zero elapsed -> ROC must NaN
        for i in range(11, n_rows):
            ts[i] = ts[i] + pd.Timedelta(seconds=45)  # a seam, like a power-off gap
    data: dict[str, object] = {
        TIMESTAMP_COLUMN: ts,
        SESSION_COLUMN: [f"s{i % sessions}" for i in range(n_rows)],
    }
    for c in range(n_cols):
        values = rng.random(n_rows) * 100.0
        if c == 0:
            values[:] = 7.0  # constant -> std is exactly 0
        if c == 1 and n_rows > 5:
            values[2:5] = np.nan  # a hole
        data[f"chan{c}"] = values
    return pd.DataFrame(data)


CONFIGS = [
    FeatureConfig(),
    FeatureConfig(window_seconds=60),
    FeatureConfig(include_std=False),
    FeatureConfig(include_mean=False),
    FeatureConfig(include_roc=False),
    FeatureConfig(min_periods=3),
]


@pytest.mark.parametrize("cfg", CONFIGS, ids=lambda c: f"w{c.window_seconds}")
@pytest.mark.parametrize("shape", [(60, 8), (181, 40), (5, 3), (1, 2)])
def test_batched_matches_per_column_exactly(cfg: FeatureConfig, shape: tuple[int, int]) -> None:
    frame = _frame(*shape)
    expected = _reference_compute(frame, cfg)
    actual = compute_features(frame, config=cfg)

    assert list(actual.columns) == list(expected.columns), "column order changed"
    assert actual.shape == expected.shape

    for column in expected.columns:
        want, got = expected[column], actual[column]
        assert want.dtype == got.dtype, f"{column}: dtype {want.dtype} != {got.dtype}"
        if pd.api.types.is_numeric_dtype(want):
            assert np.array_equal(want.to_numpy(), got.to_numpy(), equal_nan=True), column
        else:
            assert want.equals(got), column


def test_batched_matches_per_column_across_session_boundaries() -> None:
    """Windows must not bleed between sessions — the batching groups first."""
    frame = _frame(90, 6, sessions=3, seed=7)
    cfg = FeatureConfig()

    expected = _reference_compute(frame, cfg)
    actual = compute_features(frame, config=cfg)

    for column in expected.columns:
        want, got = expected[column], actual[column]
        if pd.api.types.is_numeric_dtype(want):
            assert np.array_equal(want.to_numpy(), got.to_numpy(), equal_nan=True), column
        else:
            assert want.equals(got), column
