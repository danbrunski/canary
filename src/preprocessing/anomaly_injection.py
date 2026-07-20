"""Synthetic anomaly injection for model evaluation.

Splices labeled anomalies into a clean resampled session so model recall /
false-positive rate can be measured against ground truth. Four kinds, each
modeling a real sensor failure mode:

  * **spike**   — channel reads a fixed (anomalous) value for a short
                  window. Models a momentary glitch, EMI event, or a
                  fault state the ECU briefly publishes.
  * **drift**   — channel ramps linearly away from baseline over the
                  window. Models slow miscalibration or gradual degradation
                  (O2 sensor aging, thermostat sticking).
  * **dropout** — channel holds its last value (with its `__gap` flag set)
                  until a per-channel-class ceiling, then goes NaN — the
                  production-visible signature of a sensor losing power,
                  disconnecting, or the bus dropping frames (see
                  `resample_wide`'s bounded forward-fill), not an
                  immediately-obvious raw NaN.
  * **stuck**   — channel is frozen at its pre-window value. Models a
                  sensor that fails-in-place — one of the most insidious
                  failures because the value still looks plausible.
  * **intermittent** — alternates the `spike` mechanism's fixed anomalous
                  value with brief returns to the true reading, repeating
                  for the whole window. Models a marginal connector or
                  flickering sensor (6.5): a fire-gap-fire pattern real
                  enough that a strict N-consecutive persistence gate
                  structurally cannot latch onto it, but an M-of-N gate
                  (6.1) can.

The injector operates on the resampler / features output (wide format,
session + timestamp columns + per-channel numeric columns). It produces a
modified DataFrame and a boolean label Series, plus the precise window
metadata so evaluators can compute per-kind precision/recall.

Used by Task 2.8 (IsolationForest evaluation), Phase 3 (LSTM evaluation),
and any future replay-harness work.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from src.dataframe_utils import require_columns as _require_columns
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column

AnomalyKind = Literal["spike", "drift", "dropout", "stuck", "scale", "intermittent"]


@dataclass(frozen=True)
class InjectionWindow:
    """One injected anomaly, including the data needed to label it later."""

    channel: str
    kind: AnomalyKind
    start: pd.Timestamp  # inclusive
    end: pd.Timestamp  # exclusive
    # Kind-specific parameters captured for downstream reporting:
    #   spike        → value used during the window
    #   drift        → total_delta added by the end of the window
    #   stuck        → the frozen value (filled in at inject time)
    #   dropout      → unused
    #   intermittent → value used during "on" ticks
    parameter: float | None = None


@dataclass
class AnomalyInjector:
    """Mutable builder for splicing labeled anomalies into a clean session.

    Construct from a DataFrame, chain `.spike() / .drift() / .dropout() /
    .stuck()` calls, then read `.df`, `.windows`, and `.labels()` for the
    evaluator. The original DataFrame is not modified — we work on a deep
    copy.
    """

    _df: pd.DataFrame
    _windows: list[InjectionWindow] = field(default_factory=list)

    def __init__(self, df: pd.DataFrame) -> None:
        # Defensive copy so callers can keep using the original frame.
        # __init__ on dataclass to bypass field-default copying semantics.
        _require_columns(df, [SESSION_COLUMN, TIMESTAMP_COLUMN])
        object.__setattr__(self, "_df", df.copy())
        object.__setattr__(self, "_windows", [])

    # ------------------------------------------------------------------
    # Read-only views
    # ------------------------------------------------------------------

    @property
    def df(self) -> pd.DataFrame:
        """The DataFrame with all injections applied so far."""
        return self._df

    @property
    def windows(self) -> list[InjectionWindow]:
        """Immutable snapshot of all injected windows in insertion order."""
        return list(self._windows)

    def labels(self) -> pd.Series:
        """Boolean Series, same index as `df`. True iff any injected
        window's [start, end) covers the row's timestamp.

        This is the ground truth for evaluating an anomaly detector:
        compare the detector's predicted-anomaly mask against this.
        """
        timestamps = pd.to_datetime(self._df[TIMESTAMP_COLUMN], utc=True)
        labels = pd.Series(False, index=self._df.index)
        for window in self._windows:
            mask = (timestamps >= window.start) & (timestamps < window.end)
            labels = labels | mask
        return labels

    # ------------------------------------------------------------------
    # Injection kinds
    # ------------------------------------------------------------------

    def spike(
        self,
        *,
        channel: str,
        start: pd.Timestamp | str,
        end: pd.Timestamp | str,
        value: float,
    ) -> AnomalyInjector:
        """Inject a constant anomalous value for `[start, end)` on `channel`.

        Models a momentary fault read or an EMI spike. The channel returns
        to its previous values after the window.
        """
        s, e = _normalize_window(start, end)
        self._require_channel(channel)
        mask = self._mask_for(s, e)
        self._df.loc[mask, channel] = value
        self._windows.append(
            InjectionWindow(channel=channel, kind="spike", start=s, end=e, parameter=value)
        )
        return self

    def drift(
        self,
        *,
        channel: str,
        start: pd.Timestamp | str,
        end: pd.Timestamp | str,
        total_delta: float,
    ) -> AnomalyInjector:
        """Linear ramp: add `0` at `start`, ramping to `+total_delta` at `end`.

        After the window, the channel returns to its original baseline. Set
        `end` to the dataframe's final timestamp to model a drift that
        persists for the rest of the session.
        """
        s, e = _normalize_window(start, end)
        self._require_channel(channel)
        mask = self._mask_for(s, e)
        if not mask.any():
            self._windows.append(
                InjectionWindow(
                    channel=channel,
                    kind="drift",
                    start=s,
                    end=e,
                    parameter=total_delta,
                )
            )
            return self

        # Ramp from 0 → total_delta across the window, evaluated at each row.
        timestamps = pd.to_datetime(self._df.loc[mask, TIMESTAMP_COLUMN], utc=True)
        span_seconds = (e - s).total_seconds()
        if span_seconds <= 0:
            fractions = np.zeros(len(timestamps))
        else:
            elapsed = (timestamps - s).dt.total_seconds().to_numpy()
            fractions = np.clip(elapsed / span_seconds, 0.0, 1.0)
        # `fractions` is a float64 ndarray, so `existing + fractions * total_delta`
        # upcasts to float64 even when `channel` is float32 (real resampled
        # frames narrow numeric channels to float32 for memory — see
        # `loader.to_wide`). Assigning a float64 array back into a float32
        # column that shares a consolidated block with other same-dtype
        # columns (the normal shape of a real wide frame, not the small
        # single-column fixtures unit tests use) raises pandas'
        # `LossySetitemError` — cast back to the column's own dtype first so
        # this works on real recorded sessions, not just float64 test frames.
        existing_dtype = self._df[channel].dtype
        drifted = self._df.loc[mask, channel].to_numpy() + fractions * total_delta
        self._df.loc[mask, channel] = drifted.astype(existing_dtype, copy=False)
        self._windows.append(
            InjectionWindow(
                channel=channel,
                kind="drift",
                start=s,
                end=e,
                parameter=total_delta,
            )
        )
        return self

    def dropout(
        self,
        *,
        channel: str,
        start: pd.Timestamp | str,
        end: pd.Timestamp | str,
        max_gap_seconds: float,
    ) -> AnomalyInjector:
        """Simulate a sensor power loss, adapter disconnect, or dropped frames
        for `[start, end)` — the production-visible signature (8.1), not an
        immediately-obvious raw NaN.

        ``resample_wide`` never emits raw NaN the instant a channel stops
        reporting: it forward-fills the last real value for up to a
        per-channel-class ceiling (``max_gap_seconds`` — pass
        ``ResampleConfig.max_gap.total_seconds()`` for a fast channel or
        ``ResampleConfig.slow_channel_max_gap_seconds`` for a slow one, e.g.
        via ``resampler.is_slow_channel``), then NaNs the cell *and* flips its
        ``__gap`` flag once the gap exceeds that ceiling — both transitions
        happen in the same instant (``resampler._gap_flags``'s ``ages >
        max_gap`` drives both the NaN-out and the gap column together; there
        is no real state where a channel is held at a stale-but-plausible
        value while its ``__gap`` flag is already set). This mirrors that
        exactly: the channel holds its last pre-window value (a
        plausible-looking, undetectable-by-flag reading — the insidious part
        of a real dropout) until ``max_gap_seconds`` in, then goes NaN with
        ``__gap`` set True at the same row. The `<channel>__gap` column is
        created ``False`` elsewhere if absent.

        Age is measured from `start` (not from the channel's true last
        observation, which may differ by a fraction of a row-interval if the
        channel wasn't observed on the exact row before `start`) — an
        acceptable simplification for fault-injection benchmarking, not a
        scientific replica.
        """
        s, e = _normalize_window(start, end)
        self._require_channel(channel)
        if max_gap_seconds <= 0:
            raise ValueError("max_gap_seconds must be positive")

        timestamps = pd.to_datetime(self._df[TIMESTAMP_COLUMN], utc=True)
        mask = (timestamps >= s) & (timestamps < e)

        # Held value = last real observation strictly before `start` (same
        # lookup `stuck()` uses). No pre-window data → there was never a
        # fresh value for the resampler to have been forward-filling, so the
        # whole window goes NaN immediately rather than holding a fabricated
        # reading.
        pre = self._df.loc[timestamps < s, channel].dropna()
        has_pre_observation = not pre.empty
        held_value = float(pre.iloc[-1]) if has_pre_observation else np.nan

        ceiling = pd.Timedelta(seconds=max_gap_seconds)
        age_since_start = timestamps - s
        within_ceiling = mask & (age_since_start < ceiling)
        beyond_ceiling = mask & ~within_ceiling

        self._df.loc[within_ceiling, channel] = held_value
        self._df.loc[beyond_ceiling, channel] = np.nan

        gap_col = gap_column(channel)
        if gap_col not in self._df.columns:
            self._df[gap_col] = False
        if has_pre_observation:
            # Flips in lockstep with the NaN transition — see the docstring.
            self._df.loc[beyond_ceiling, gap_col] = True
        else:
            # No real observation exists anywhere before `start` to forward-
            # fill from — resampler._gap_flags treats an all-empty history as
            # stale everywhere (`observed_at.empty → True` for every row), not
            # just past the ceiling, so the whole window is flagged too.
            self._df.loc[mask, gap_col] = True

        self._windows.append(InjectionWindow(channel=channel, kind="dropout", start=s, end=e))
        return self

    def stuck(
        self,
        *,
        channel: str,
        start: pd.Timestamp | str,
        end: pd.Timestamp | str,
    ) -> AnomalyInjector:
        """Freeze `channel` at its pre-window value for `[start, end)`.

        Captures the row immediately before `start` and replicates that
        value across the window. If `start` is at or before the first row,
        the first available value is used instead. Models the most
        dangerous failure mode: a sensor reading that LOOKS plausible
        but is actually frozen.
        """
        s, e = _normalize_window(start, end)
        self._require_channel(channel)
        timestamps = pd.to_datetime(self._df[TIMESTAMP_COLUMN], utc=True)
        # Pick the value from the latest pre-window row, or the first
        # available value if no pre-window data exists.
        pre = self._df.loc[timestamps < s, channel].dropna()
        if not pre.empty:
            stuck_value = float(pre.iloc[-1])
        else:
            available = self._df[channel].dropna()
            if available.empty:
                raise ValueError(f"cannot stick channel {channel!r}: no non-NaN values to freeze")
            stuck_value = float(available.iloc[0])

        mask = self._mask_for(s, e)
        self._df.loc[mask, channel] = stuck_value
        self._windows.append(
            InjectionWindow(channel=channel, kind="stuck", start=s, end=e, parameter=stuck_value)
        )
        return self

    def scale(
        self,
        *,
        channel: str,
        start: pd.Timestamp | str,
        end: pd.Timestamp | str,
        factor: float,
    ) -> AnomalyInjector:
        """Multiply ``channel`` by ``factor`` for ``[start, end)``.

        Models a *scale-error* sensor fault — the reading remains shape-
        plausible but its magnitude is wrong by a constant multiplier
        (gain drift, miscalibrated divider). Distinct from ``spike``
        (fixed value, no shape) and ``drift`` (linear ramp, no factor).
        """
        if not np.isfinite(factor):
            raise ValueError("scale factor must be finite")
        s, e = _normalize_window(start, end)
        self._require_channel(channel)
        mask = self._mask_for(s, e)
        self._df.loc[mask, channel] = self._df.loc[mask, channel].to_numpy() * factor
        self._windows.append(
            InjectionWindow(channel=channel, kind="scale", start=s, end=e, parameter=factor)
        )
        return self

    def intermittent(
        self,
        *,
        channel: str,
        start: pd.Timestamp | str,
        end: pd.Timestamp | str,
        value: float,
        on_seconds: float,
        off_seconds: float,
    ) -> AnomalyInjector:
        """Fire-gap-fire: alternate `on_seconds` of the fixed anomalous
        `value` (the same fixed-value mechanism `spike` uses) with
        `off_seconds` back at the channel's true reading, repeating for the
        whole `[start, end)` window (6.5).

        Models a marginal connector or flickering sensor — mostly faulted
        with brief self-clearing gaps, not a full failure. Unlike `spike`
        (constant for the whole window), the gaps mean a strict
        N-consecutive persistence gate can structurally never latch onto
        this pattern (`on_seconds` sized below the gate's consecutive
        requirement), while an M-of-N gate — tolerant of a bounded number
        of misses within its trailing window — catches it. This is the
        motivating scenario for 6.1's M-of-N replacement, so it deliberately
        reuses `spike`'s exact injection mechanism rather than inventing a
        new fault physics — the only difference from `spike` is the on/off
        timing.
        """
        s, e = _normalize_window(start, end)
        self._require_channel(channel)
        if on_seconds <= 0 or off_seconds <= 0:
            raise ValueError("on_seconds and off_seconds must be positive")
        timestamps = pd.to_datetime(self._df[TIMESTAMP_COLUMN], utc=True)
        mask = (timestamps >= s) & (timestamps < e)
        period = on_seconds + off_seconds
        elapsed = (timestamps - s).dt.total_seconds()
        phase = elapsed % period
        on_mask = mask & (phase < on_seconds)
        self._df.loc[on_mask, channel] = value
        self._windows.append(
            InjectionWindow(channel=channel, kind="intermittent", start=s, end=e, parameter=value)
        )
        return self

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_channel(self, channel: str) -> None:
        if channel not in self._df.columns:
            raise KeyError(f"channel {channel!r} not in DataFrame")

    def _mask_for(self, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
        timestamps = pd.to_datetime(self._df[TIMESTAMP_COLUMN], utc=True)
        return (timestamps >= start) & (timestamps < end)


def windows_to_records(windows: Iterable[InjectionWindow]) -> list[dict[str, object]]:
    """Flatten windows to a list of dicts for serialization / reporting.

    Useful when an evaluation script wants to persist the injected
    anomalies alongside the predictions for later analysis.
    """
    records: list[dict[str, object]] = []
    for window in windows:
        records.append(
            {
                "channel": window.channel,
                "kind": window.kind,
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "parameter": window.parameter,
            }
        )
    return records


def _normalize_window(
    start: pd.Timestamp | str, end: pd.Timestamp | str
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Parse window bounds into UTC pd.Timestamps and validate ordering."""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    if s.tzinfo is None:
        s = s.tz_localize("UTC")
    else:
        s = s.tz_convert("UTC")
    if e.tzinfo is None:
        e = e.tz_localize("UTC")
    else:
        e = e.tz_convert("UTC")
    if e < s:
        raise ValueError(f"window end {e} precedes start {s}")
    return s, e
