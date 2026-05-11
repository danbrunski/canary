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
  * **dropout** — channel becomes NaN for the window. Models a sensor
                  losing power, disconnecting, or the bus dropping frames.
  * **stuck**   — channel is frozen at its pre-window value. Models a
                  sensor that fails-in-place — one of the most insidious
                  failures because the value still looks plausible.

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

from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN

AnomalyKind = Literal["spike", "drift", "dropout", "stuck"]


@dataclass(frozen=True)
class InjectionWindow:
    """One injected anomaly, including the data needed to label it later."""

    channel: str
    kind: AnomalyKind
    start: pd.Timestamp  # inclusive
    end: pd.Timestamp  # exclusive
    # Kind-specific parameters captured for downstream reporting:
    #   spike   → value used during the window
    #   drift   → total_delta added by the end of the window
    #   stuck   → the frozen value (filled in at inject time)
    #   dropout → unused
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
            InjectionWindow(
                channel=channel, kind="spike", start=s, end=e, parameter=value
            )
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
        self._df.loc[mask, channel] = (
            self._df.loc[mask, channel].to_numpy() + fractions * total_delta
        )
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
    ) -> AnomalyInjector:
        """Set `channel` to NaN for `[start, end)`.

        Models a sensor power loss, adapter disconnect, or dropped frames.
        Downstream code that doesn't gracefully handle NaN will surface
        the problem; the IsolationForest pipeline's plausibility checks
        already exercise this.
        """
        s, e = _normalize_window(start, end)
        self._require_channel(channel)
        mask = self._mask_for(s, e)
        self._df.loc[mask, channel] = np.nan
        self._windows.append(
            InjectionWindow(channel=channel, kind="dropout", start=s, end=e)
        )
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
                raise ValueError(
                    f"cannot stick channel {channel!r}: no non-NaN values to freeze"
                )
            stuck_value = float(available.iloc[0])

        mask = self._mask_for(s, e)
        self._df.loc[mask, channel] = stuck_value
        self._windows.append(
            InjectionWindow(
                channel=channel, kind="stuck", start=s, end=e, parameter=stuck_value
            )
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


def _require_columns(df: pd.DataFrame, columns: Iterable[str]) -> None:
    missing = set(columns) - set(df.columns)
    if missing:
        raise KeyError(f"missing required columns: {sorted(missing)}")
