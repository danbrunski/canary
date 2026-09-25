"""Uniform time-grid resampling for telemetry sessions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from src.dataframe_utils import require_columns as _require_columns

if TYPE_CHECKING:
    from src.config import CanaryConfig

SESSION_COLUMN = "session"
TIMESTAMP_COLUMN = "timestamp"
GAP_SUFFIX = "__gap"
#: A03.1 — per-cell "a sample arrived this tick" flag. ``True`` at grid row
#: ``t`` when at least one (quality-gated, numeric) sample of the channel has a
#: timestamp in ``(t - interval, t]``: the sample was delivered by the row's own
#: time, since the previous row. ``__gap`` answers a different question ("is
#: the newest sample younger than ``max_gap``?") and stays the dead-sensor
#: signal; the two differ on every forward-filled row inside ``max_gap``.
#: The live source (``LiveFeatureSource``) emits the same flag for the same
#: stream: "the cell's value was delivered after the previous tick".
ARRIVED_SUFFIX = "__arrived"
#: Every per-channel bookkeeping suffix the resampler emits. A column ending in
#: one of these is never a value channel: use :func:`is_flag_column` rather
#: than testing one suffix, so a new flag cannot leak into rolling features,
#: scalers or model schemas at the one call site that forgot it.
FLAG_SUFFIXES: tuple[str, ...] = (GAP_SUFFIX, ARRIVED_SUFFIX)

#: Default forward-fill ceiling for slow channels (seconds) — generous
#: because slow sensors (coolant, IAT, baro) legitimately update on the
#: order of tens of seconds; this only catches a channel that's actually
#: gone dead, not normal slow-poll cadence.
DEFAULT_SLOW_CHANNEL_MAX_GAP_SECONDS = 120.0

#: Channels whose *physics* updates far slower than the resample grid, so the
#: fast-channel ``max_gap`` would flag ordinary poll cadence as a dead sensor.
#: The test is "does this quantity change meaningfully within one grid
#: interval?" — not "is it convenient to ffill".
#:
#: ``RUN_TIME`` is the engine-run clock: monotonic, perfectly predictable
#: between samples, and polled on the medium tier (~8 s observed, 17.5%
#: coverage on the 2026-07-26 corpus). Classifying it fast set its ``__gap``
#: flag on 82.5% of rows, which silently vetoed every cold-start warmup recipe
#: — ``require_contiguous_runtime`` rejects a session if ``RUN_TIME__gap`` is
#: true anywhere, so two genuine cold starts (first coolant 27 °C and 34 °C,
#: both with monotonic run-time) were discarded for a staleness artifact rather
#: than any real discontinuity. Its siblings ``DISTANCE_SINCE_DTC_CLEAR`` /
#: ``WARMUPS_SINCE_DTC_CLEAR`` / ``DISTANCE_W_MIL`` are the same shape of
#: slow-polled counter and are candidates for the same treatment; left out here
#: to keep this change to the one channel with a demonstrated consequence.
DEFAULT_SLOW_CHANNEL_HINTS = frozenset(
    {
        "BARO",
        "BAROMETRIC",
        "COOLANT",
        "INTAKE_TEMP",
        "IAT",
        "RUN_TIME",
    }
)


@dataclass(frozen=True)
class ResampleConfig:
    """Parameters for converting irregular telemetry to a uniform grid."""

    hz: float = 1.0
    max_gap_factor: float = 2.0
    slow_channels: frozenset[str] = DEFAULT_SLOW_CHANNEL_HINTS
    fast_channels: frozenset[str] | None = None
    #: Forward-fill ceiling for slow channels (seconds) — independent of
    #: ``max_gap_factor`` because slow sensors legitimately update far less
    #: often than the resample grid. Beyond this age a slow channel's
    #: ffilled cell goes NaN and its ``__gap`` flag sets, same as a fast
    #: channel past ``max_gap``.
    slow_channel_max_gap_seconds: float = DEFAULT_SLOW_CHANNEL_MAX_GAP_SECONDS
    session_gap_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.hz <= 0:
            raise ValueError("hz must be positive")
        if self.max_gap_factor <= 0:
            raise ValueError("max_gap_factor must be positive")
        if self.slow_channel_max_gap_seconds <= 0:
            raise ValueError("slow_channel_max_gap_seconds must be positive")
        if self.session_gap_seconds <= 0:
            raise ValueError("session_gap_seconds must be positive")

    @property
    def interval(self) -> pd.Timedelta:
        return pd.Timedelta(seconds=1.0 / self.hz)

    @property
    def max_gap(self) -> pd.Timedelta:
        return self.interval * self.max_gap_factor

    @property
    def slow_channel_max_gap(self) -> pd.Timedelta:
        return pd.Timedelta(seconds=self.slow_channel_max_gap_seconds)

    def is_fast(self, channel: str) -> bool:
        """Whether ``channel`` gets the grid-derived ``max_gap`` (fast) or the
        slow-channel ceiling. ``fast_channels``, when set, is the explicit fast
        list; otherwise every channel not matching ``slow_channels`` is fast.

        The one statement of the rule (R04.7): the resampler, the live
        feature source's ``__gap`` flags and the synthetic-dropout injector
        all read it here, so a non-default ``fast_channels`` cannot flag a
        channel stale at serve that training held fresh.
        """
        if self.fast_channels is not None:
            return channel in self.fast_channels
        return not is_slow_channel(channel, self.slow_channels)

    def max_gap_for(self, channel: str) -> pd.Timedelta:
        """The forward-fill ceiling for ``channel`` (see :meth:`is_fast`)."""
        return self.max_gap if self.is_fast(channel) else self.slow_channel_max_gap

    def max_gap_seconds_for(self, channel: str) -> float:
        """:meth:`max_gap_for` in seconds."""
        return float(self.max_gap_for(channel).total_seconds())


def resample_config_for(config: CanaryConfig, *, hz: float | None = None) -> ResampleConfig:
    """The :class:`ResampleConfig` of the training recipe, built from ``config``.

    Every resample that means "the grid the models were trained on" — batch
    feature builds, refit, analysis, benchmarks, replay, dataset health, the
    manifest's ``max_gap_seconds`` and the live source's stale ceilings — goes
    through here, so no caller can pick up some of
    ``config.preprocessing``'s gap rules and silently default the rest
    (D03.27: ``analyze_session`` dropped ``max_gap_factor`` and
    ``session_gap_seconds``; the manifest's ``max_gap_seconds`` dropped
    ``max_gap_factor``). ``tests/test_resample_config_construction.py`` fails
    on a direct ``ResampleConfig(...)`` anywhere outside its allowlist.

    ``hz`` overrides only the grid rate, for a caller that is deliberately on
    a different rate (dataset export at a requested rate). The gap rules still
    come from ``config``: ``max_gap_factor`` is in grid intervals, so it scales
    with the rate; the slow-channel and session-gap ceilings are wall-clock.
    """
    preprocessing = config.preprocessing
    return ResampleConfig(
        hz=preprocessing.resample_hz if hz is None else hz,
        max_gap_factor=preprocessing.max_gap_factor,
        slow_channel_max_gap_seconds=preprocessing.slow_channel_max_gap_seconds,
        session_gap_seconds=preprocessing.session_gap_seconds,
    )


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
        ordered = group.sort_values(TIMESTAMP_COLUMN, kind="stable").copy()
        timestamps = pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True)
        segment_ids = timestamps.diff().gt(pd.Timedelta(seconds=cfg.session_gap_seconds)).cumsum()
        segment_count = int(segment_ids.max()) + 1 if len(segment_ids) else 0
        for segment_id, segment in ordered.groupby(segment_ids, sort=True):
            logical_session = str(session)
            if segment_count > 1:
                logical_session = f"{session}#segment-{int(segment_id) + 1:03d}"
            frames.append(_resample_one_session(logical_session, segment, columns, cfg))
    return pd.concat(frames, ignore_index=True)


def resample_long(
    long: pd.DataFrame,
    *,
    config: ResampleConfig | None = None,
    value_column: str = "value_number",
    channel_column: str = "source_channel",
) -> pd.DataFrame:
    """Resample long telemetry before constructing a wide frame.

    Unlike :func:`resample_wide`, memory scales with the final time grid,
    not ``raw timestamps × channels``. This is the batch path for persisted
    telemetry, where bus-rate timestamps are mostly unique and a raw pivot
    creates an enormous sparse intermediate.
    """
    cfg = ResampleConfig() if config is None else config
    _require_columns(long, [SESSION_COLUMN, TIMESTAMP_COLUMN, channel_column, value_column])
    if long.empty:
        return _empty_resampled([], cfg)

    frames: list[pd.DataFrame] = []
    ordered = long.sort_values([SESSION_COLUMN, TIMESTAMP_COLUMN], kind="stable").copy()
    ordered[TIMESTAMP_COLUMN] = pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True)
    for session, group in ordered.groupby(SESSION_COLUMN, sort=True, dropna=False):
        timestamps = group[TIMESTAMP_COLUMN]
        segment_ids = timestamps.diff().gt(pd.Timedelta(seconds=cfg.session_gap_seconds)).cumsum()
        segment_count = int(segment_ids.max()) + 1 if len(segment_ids) else 0
        for segment_id, segment in group.groupby(segment_ids, sort=True):
            logical_session = str(session)
            if segment_count > 1:
                logical_session = f"{session}#segment-{int(segment_id) + 1:03d}"
            frames.append(
                _resample_one_long(
                    logical_session,
                    segment,
                    cfg,
                    value_column=value_column,
                    channel_column=channel_column,
                )
            )
    nonempty = [frame for frame in frames if not frame.empty]
    return pd.concat(nonempty, ignore_index=True) if nonempty else _empty_resampled([], cfg)


def _resample_one_long(
    session: str,
    group: pd.DataFrame,
    config: ResampleConfig,
    *,
    value_column: str,
    channel_column: str,
) -> pd.DataFrame:
    numeric = group.loc[group[value_column].notna()].copy()
    if numeric.empty:
        return _empty_resampled([], config)
    numeric[value_column] = pd.to_numeric(numeric[value_column], errors="coerce").astype("float32")
    numeric = numeric.loc[numeric[value_column].notna()]
    if numeric.empty:
        return _empty_resampled([], config)

    channels = sorted(str(channel) for channel in numeric[channel_column].unique())
    start = numeric[TIMESTAMP_COLUMN].min().floor(config.interval)
    end = numeric[TIMESTAMP_COLUMN].max().floor(config.interval)
    sampled_times = pd.Series(pd.date_range(start=start, end=end, freq=config.interval))
    values: dict[str, np.ndarray] = {}
    gaps: dict[str, np.ndarray] = {}
    arrivals: dict[str, np.ndarray] = {}
    for channel in channels:
        rows = numeric.loc[numeric[channel_column].astype(str) == channel]
        series = pd.Series(
            rows[value_column].to_numpy(),
            index=pd.DatetimeIndex(rows[TIMESTAMP_COLUMN]),
            dtype="float32",
        )
        # Match pivot_table(..., aggfunc="last") for duplicate observations.
        series = series.groupby(level=0, sort=True).last()
        sampled = series.resample(config.interval).last().reindex(sampled_times.array).ffill()
        stale = _gap_flags(series, sampled_times, config.max_gap_for(channel)).to_numpy()
        values[channel] = sampled.where(~stale, np.nan).to_numpy(dtype="float32")
        gaps[gap_column(channel)] = stale
        arrivals[arrived_column(channel)] = _arrival_flags(series, sampled_times, config.interval)

    return pd.concat(
        [
            pd.DataFrame(
                {
                    SESSION_COLUMN: session,
                    TIMESTAMP_COLUMN: sampled_times,
                }
            ),
            pd.DataFrame(values),
            pd.DataFrame(gaps),
            pd.DataFrame(arrivals),
        ],
        axis=1,
    )


def gap_column(channel: str) -> str:
    """Return the boolean gap-flag column name for a channel."""
    return f"{channel}{GAP_SUFFIX}"


def arrived_column(channel: str) -> str:
    """Return the boolean "sample arrived this tick" column name (A03.1)."""
    return f"{channel}{ARRIVED_SUFFIX}"


def is_flag_column(column: object) -> bool:
    """``True`` for per-channel bookkeeping columns (``__gap``, ``__arrived``)."""
    return str(column).endswith(FLAG_SUFFIXES)


def _arrival_flags(
    original: pd.Series,
    sampled_index: pd.Series,
    interval: pd.Timedelta,
) -> np.ndarray:
    """``True`` at each grid point ``t`` with an observation in ``(t - interval, t]``.

    The causal twin of the live source's arrival flag: a sample at 10.4 s is
    flagged on the 11 s row (delivered since the 10 s row), however the grid's
    *value* binning labels it.
    """
    sampled_times = pd.DatetimeIndex(pd.to_datetime(sampled_index, utc=True))
    observed = original.dropna()
    if observed.empty or len(sampled_times) == 0:
        return np.zeros(len(sampled_times), dtype=bool)
    # Both sides in nanoseconds: a DatetimeIndex may carry us or ns units.
    stamps = np.sort(_epoch_ns(pd.DatetimeIndex(pd.to_datetime(observed.index, utc=True))))
    grid = _epoch_ns(sampled_times)
    upto_now = np.searchsorted(stamps, grid, side="right")
    upto_previous = np.searchsorted(stamps, grid - int(interval.value), side="right")
    return np.asarray(upto_now > upto_previous, dtype=bool)


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

    # Bounded ffill (1.1) + gap flags (1.2) for every channel — fast channels
    # use the resample-grid-derived max_gap, slow channels (which legitimately
    # update far less often) use their own generous ceiling. Beyond the limit
    # the ffilled cell is NaN'd back out rather than carried forever, so a
    # dead sensor stops silently masquerading as a live reading.
    #
    # Both the masked values and the gap flags are collected up front and
    # attached in a single concat — assigning them column-by-column fragments
    # the frame (one block per channel) on wide corpora.
    masked: dict[str, np.ndarray] = {}
    gaps: dict[str, np.ndarray] = {}
    arrivals: dict[str, np.ndarray] = {}
    for column in value_columns:
        stale = _gap_flags(indexed[column], sampled[TIMESTAMP_COLUMN], config.max_gap_for(column))
        stale_mask = stale.to_numpy()
        gaps[gap_column(column)] = stale_mask
        masked[column] = sampled[column].where(~stale_mask, np.nan).to_numpy()
        arrivals[arrived_column(column)] = _arrival_flags(
            pd.to_numeric(indexed[column], errors="coerce"),
            sampled[TIMESTAMP_COLUMN],
            config.interval,
        )

    return pd.concat(
        [
            sampled[[SESSION_COLUMN, TIMESTAMP_COLUMN]],
            pd.DataFrame(masked, index=sampled.index),
            pd.DataFrame(gaps, index=sampled.index),
            pd.DataFrame(arrivals, index=sampled.index),
        ],
        axis=1,
    )


def _epoch_ns(index: pd.DatetimeIndex) -> np.ndarray:
    """UTC epoch nanoseconds, whatever the index's stored unit."""
    return index.tz_convert("UTC").as_unit("ns").tz_localize(None).to_numpy().astype(np.int64)


def _gap_flags(
    original: pd.Series,
    sampled_index: pd.Series,
    max_gap: pd.Timedelta,
) -> pd.Series:
    """True at each grid point where the channel's newest observation at or
    before it is missing or older than ``max_gap``."""
    sampled_times = pd.DatetimeIndex(pd.to_datetime(sampled_index, utc=True))
    observed = original.dropna()
    if observed.empty:
        return pd.Series(True, index=sampled_index.index)

    # ``unique()`` because ``reindex`` rejects a duplicated source index, and a
    # channel can legitimately report twice at the same timestamp. The rows are
    # already sorted, so the result stays monotonic for the ffill below.
    observed_at = pd.DatetimeIndex(pd.to_datetime(observed.index, utc=True)).unique()
    # Stay on tz-aware DatetimeIndex the whole way. Round-tripping the ffilled
    # Series through ``.to_numpy()`` silently drops the UTC offset when every
    # entry is NaT — the case where a channel's first observation lands after
    # the final grid point (e.g. one gps:altitude sample in the last
    # sub-second) — and the age subtraction then raised "Cannot subtract
    # tz-naive and tz-aware", killing the whole session.
    last_seen = pd.DatetimeIndex(
        pd.Series(observed_at, index=observed_at).reindex(sampled_times, method="ffill")
    )
    never_seen = last_seen.isna()
    stale = never_seen | ((sampled_times - last_seen) > max_gap)
    return pd.Series(np.asarray(stale), index=sampled_index.index)


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
            if column not in {SESSION_COLUMN, TIMESTAMP_COLUMN} and not is_flag_column(column)
        ]
    _require_columns(wide, columns)
    return columns


def _empty_resampled(value_columns: list[str], config: ResampleConfig) -> pd.DataFrame:
    gap_columns = [gap_column(column) for column in value_columns]
    arrived_columns = [arrived_column(column) for column in value_columns]
    return pd.DataFrame(
        columns=[SESSION_COLUMN, TIMESTAMP_COLUMN, *value_columns, *gap_columns, *arrived_columns]
    )
