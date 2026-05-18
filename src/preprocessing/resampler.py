"""Uniform time-grid resampling for telemetry sessions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd

from src.dataframe_utils import require_columns as _require_columns

SESSION_COLUMN = "session"
TIMESTAMP_COLUMN = "timestamp"
GAP_SUFFIX = "__gap"

DEFAULT_SLOW_CHANNEL_HINTS = frozenset(
    {
        "BARO",
        "BAROMETRIC",
        "COOLANT",
        "INTAKE_TEMP",
        "IAT",
    }
)


@dataclass(frozen=True)
class ResampleConfig:
    """Parameters for converting irregular telemetry to a uniform grid."""

    hz: float = 1.0
    max_gap_factor: float = 2.0
    slow_channels: frozenset[str] = DEFAULT_SLOW_CHANNEL_HINTS
    fast_channels: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.hz <= 0:
            raise ValueError("hz must be positive")
        if self.max_gap_factor <= 0:
            raise ValueError("max_gap_factor must be positive")

    @property
    def interval(self) -> pd.Timedelta:
        return pd.Timedelta(seconds=1.0 / self.hz)

    @property
    def max_gap(self) -> pd.Timedelta:
        return self.interval * self.max_gap_factor


def resample_wide(
    wide: pd.DataFrame,
    *,
    config: ResampleConfig | None = None,
    value_columns: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Resample a wide telemetry DataFrame without crossing session boundaries."""
    cfg = ResampleConfig() if config is None else config
    _require_columns(wide, [SESSION_COLUMN, TIMESTAMP_COLUMN])
    columns = _value_columns(wide, value_columns)
    if wide.empty:
        return _empty_resampled(columns, cfg)

    frames: list[pd.DataFrame] = []
    for session, group in wide.groupby(SESSION_COLUMN, sort=True, dropna=False):
        frames.append(_resample_one_session(str(session), group, columns, cfg))
    return pd.concat(frames, ignore_index=True)


def gap_column(channel: str) -> str:
    """Return the boolean gap-flag column name for a channel."""
    return f"{channel}{GAP_SUFFIX}"


def is_slow_channel(channel: str, hints: Iterable[str] = DEFAULT_SLOW_CHANNEL_HINTS) -> bool:
    """Infer whether a channel is slow-moving from its source/channel name."""
    normalized = channel.upper()
    return any(hint.upper() in normalized for hint in hints)


def _resample_one_session(
    session: str,
    group: pd.DataFrame,
    value_columns: list[str],
    config: ResampleConfig,
) -> pd.DataFrame:
    ordered = group.sort_values(TIMESTAMP_COLUMN, kind="stable").copy()
    ordered[TIMESTAMP_COLUMN] = pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True)
    indexed = ordered.set_index(TIMESTAMP_COLUMN)

    sampled = indexed[value_columns].resample(config.interval).last()
    sampled = sampled.ffill()
    sampled.insert(0, SESSION_COLUMN, session)
    sampled = sampled.reset_index()

    for column in _fast_channels(value_columns, config):
        flags = _gap_flags(indexed[column], sampled[TIMESTAMP_COLUMN], config.max_gap)
        sampled[gap_column(column)] = flags.to_numpy()

    return sampled


def _gap_flags(
    original: pd.Series,
    sampled_index: pd.Series,
    max_gap: pd.Timedelta,
) -> pd.Series:
    observed_at = pd.Series(original.dropna().index, index=original.dropna().index)
    if observed_at.empty:
        return pd.Series([True] * len(sampled_index), index=sampled_index.index)

    sampled_times = pd.DatetimeIndex(pd.to_datetime(sampled_index, utc=True))
    last_seen = observed_at.reindex(sampled_times, method="ffill")
    ages = sampled_times.to_series(index=sampled_index.index) - pd.Series(
        last_seen.to_numpy(),
        index=sampled_index.index,
    )
    return last_seen.isna().reset_index(drop=True) | (ages > max_gap)


def _fast_channels(value_columns: list[str], config: ResampleConfig) -> list[str]:
    if config.fast_channels is not None:
        return [column for column in value_columns if column in config.fast_channels]
    return [column for column in value_columns if not is_slow_channel(column, config.slow_channels)]


def _value_columns(
    wide: pd.DataFrame,
    value_columns: Iterable[str] | None,
) -> list[str]:
    if value_columns is not None:
        columns = list(value_columns)
    else:
        columns = [
            str(column)
            for column in wide.columns
            if column not in {SESSION_COLUMN, TIMESTAMP_COLUMN}
            and not str(column).endswith(GAP_SUFFIX)
        ]
    _require_columns(wide, columns)
    return columns


def _empty_resampled(value_columns: list[str], config: ResampleConfig) -> pd.DataFrame:
    gap_columns = [gap_column(column) for column in _fast_channels(value_columns, config)]
    return pd.DataFrame(columns=[SESSION_COLUMN, TIMESTAMP_COLUMN, *value_columns, *gap_columns])
