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

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml

from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN, arrived_column, gap_column
from src.dataframe_utils import require_columns as _require_columns
from src.decode.behavior_models import CLOSED_LOOP_FUEL_STATES
from src.decode.bindings import Bindings
from src.decode.engineering import (
    UnscaledDecodedChannelError,
    decoded_factors,
    is_decoded_column,
)
from src.operating import ENGINE_RUNNING_MIN_RPM
from src.storage.broadcast_storage import is_broadcast_channel

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
        # A03.1: nothing arrives during a dropout. Without this an
        # ``arrived_sample`` artifact would see the held value keep arriving —
        # a stuck sensor, not a dropout — and derived channels would inherit it.
        # A broadcast channel (P02.10) reads "arrived" while a stored row is
        # within the stale timeout (``resampler._arrival_flags_for``), so a real
        # dropout keeps it true up to the ceiling; the injection matches.
        arrived_col = arrived_column(channel)
        if arrived_col in self._df.columns:
            not_arrived = (
                (beyond_ceiling if has_pre_observation else mask)
                if is_broadcast_channel(channel)
                else mask
            )
            self._df.loc[not_arrived, arrived_col] = False

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


# ---------------------------------------------------------------------------
# A02.6 — realistic faults with severity sweeps
# ---------------------------------------------------------------------------
#
# ``src.models.synthetic_fault_eval._inject_one``'s kinds (spike = 5 x max +
# 1,000, scale x 5, drift = 10 sigma + 100) are plumbing tests: a range check
# catches every one of them by construction. The faults below are the ones a
# range check cannot catch — small, physically consistent, and applied to
# every channel that measures the same quantity — swept over severity so the
# report reads as recall against severity rather than one rate. They lived in
# ``synthetic_fault_eval`` until R09.2 moved them here, beneath both the
# detector harness and ``src.plausibility.offline``.

RealisticFaultKind = Literal["maf_gain", "lazy_o2", "thermostat", "bank_imbalance", "idle_air_leak"]

#: The vehicle-agnostic core of each channel role: SAE J1979 Mode 01 PIDs that
#: any OBD-II vehicle defines. A profile adds the channels that mirror the same
#: quantity on its own bus (Mode 22 DIDs, broadcast bindings) through its
#: ``fault_injection.yaml`` (:func:`load_fault_channel_roles`), so a fault is
#: applied consistently across related channels without naming one vehicle's
#: catalog in code.
DEFAULT_FAULT_CHANNEL_ROLES: dict[str, tuple[str, ...]] = {
    "airflow": ("obd:MAF",),
    "calculated_load": ("obd:ENGINE_LOAD", "obd:ABSOLUTE_LOAD"),
    "stft_b1": ("obd:SHORT_FUEL_TRIM_1",),
    "stft_b2": ("obd:SHORT_FUEL_TRIM_2",),
    "af_sensor_b1": ("obd:O2_WR_LAMBDA_S1",),
    "coolant": ("obd:COOLANT_TEMP",),
}

MAF_CHANNEL = "obd:MAF"
RPM_CHANNEL = "obd:RPM"
SPEED_CHANNEL = "obd:SPEED"
FUEL_STATUS_CHANNEL = "obd:FUEL_STATUS"

#: Seconds a stuck-open thermostat takes to pull coolant to its new plateau.
THERMOSTAT_RAMP_SECONDS = 120.0
#: Short-term trim authority. A leak that needs more than this is a code-setting
#: lean condition, not the subtle signature this sweep is for.
TRIM_AUTHORITY_PERCENT = 25.0
#: Floor on the airflow denominator of the leak signature (g/s), so a MAF
#: reading of ~0 during a stall cannot turn a small leak into an infinite trim.
MIN_LEAK_AIRFLOW_G_S = 0.5


@dataclass(frozen=True)
class RealisticFault:
    """One realistic fault and the severities it is swept over."""

    kind: RealisticFaultKind
    severity_unit: str
    #: Swept values, least detectable last is NOT implied — each kind states
    #: its own direction in ``description``.
    severities: tuple[float, ...]
    description: str


#: The channel roles each realistic fault writes (``inject_realistic_fault``
#: edits exactly these), for callers that must know which channels a fault
#: can touch — e.g. restricting a per-channel detector to them.
REALISTIC_FAULT_ROLES: dict[str, tuple[str, ...]] = {
    "maf_gain": ("airflow", "calculated_load", "stft_b1", "stft_b2"),
    "lazy_o2": ("af_sensor_b1",),
    "thermostat": ("coolant",),
    "bank_imbalance": ("stft_b1",),
    "idle_air_leak": ("stft_b1", "stft_b2"),
}

REALISTIC_FAULTS: dict[str, RealisticFault] = {
    "maf_gain": RealisticFault(
        "maf_gain",
        "MAF gain",
        (0.90, 0.93, 0.95, 0.97),
        "MAF under-reads by the gain; airflow and calculated-load channels scale with it, "
        "and closed-loop short-term trims add (1/g - 1) x 100 % of fuel. 0.97 is subtlest.",
    ),
    "lazy_o2": RealisticFault(
        "lazy_o2",
        "time constant s",
        (2.0, 5.0, 10.0, 20.0),
        "The bank-1 A/F sensor responds through a first-order lag of this time constant, "
        "on every channel carrying that sensor. 2 s is subtlest.",
    ),
    "thermostat": RealisticFault(
        "thermostat",
        "coolant plateau drop degC",
        (3.0, 6.0, 10.0, 15.0),
        "A stuck-open thermostat: coolant falls this far below its reading over "
        f"{THERMOSTAT_RAMP_SECONDS:.0f} s and stays there, on every coolant channel. 3 degC "
        "is subtlest.",
    ),
    "bank_imbalance": RealisticFault(
        "bank_imbalance",
        "bank-1 lean %",
        (2.0, 4.0, 6.0, 10.0),
        "Bank 1 runs lean by this much; its closed-loop short-term trim adds it back while "
        "bank 2 is unchanged. 2 % is subtlest.",
    ),
    "idle_air_leak": RealisticFault(
        "idle_air_leak",
        "unmetered air g/s",
        (0.2, 0.4, 0.6, 1.0),
        "Unmetered air enters after the MAF: closed-loop short-term trims on both banks add "
        "100 x leak / MAF %, so the signature is largest at idle and fades with load. "
        "0.2 g/s is subtlest.",
    ),
}


@dataclass(frozen=True)
class RealisticInjection:
    """What one realistic injection actually changed — and what it could not."""

    kind: str
    severity: float
    start: pd.Timestamp
    end: pd.Timestamp
    #: Channels whose values changed inside the window.
    perturbed: tuple[str, ...]
    #: Role channels left untouched, each with the reason: ``absent`` from the
    #: frame, ``not observed`` in the window, or ``unscaled`` (a ``decoded:``
    #: channel with no pinned engineering scale to convert the fault into).
    not_perturbed: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "perturbed": list(self.perturbed),
            "not_perturbed": dict(self.not_perturbed),
        }


def load_fault_channel_roles(path: Path | None) -> dict[str, tuple[str, ...]]:
    """The default J1979 roles plus a profile's own mirrors of each quantity.

    ``path`` is a YAML file of ``roles: {<role>: [<channel>, ...]}``; unknown
    roles are refused (a typo would silently apply nothing). ``None`` or a
    missing file returns the defaults.
    """
    roles = {role: tuple(channels) for role, channels in DEFAULT_FAULT_CHANNEL_ROLES.items()}
    if path is None or not path.exists():
        return roles
    raw = yaml.safe_load(path.read_text()) or {}
    extra = raw.get("roles", {})
    if not isinstance(extra, Mapping):
        raise ValueError(f"{path}: 'roles' must be a mapping of role -> channel list")
    unknown = sorted(set(extra) - set(roles))
    if unknown:
        raise ValueError(f"{path}: unknown fault-channel roles {unknown}; known: {sorted(roles)}")
    for role, channels in extra.items():
        roles[role] = tuple(dict.fromkeys([*roles[role], *(str(c) for c in channels)]))
    return roles


def _running_mask(frame: pd.DataFrame) -> pd.Series:
    if RPM_CHANNEL not in frame.columns:
        return pd.Series(False, index=frame.index)
    return frame[RPM_CHANNEL].astype("float64") >= ENGINE_RUNNING_MIN_RPM


def _idle_mask(frame: pd.DataFrame) -> pd.Series:
    if SPEED_CHANNEL not in frame.columns:
        return pd.Series(False, index=frame.index)
    return _running_mask(frame) & (frame[SPEED_CHANNEL].astype("float64") <= 0.0)


def _closed_loop_mask(frame: pd.DataFrame) -> pd.Series:
    """Rows where the ECM trims from O2 feedback. Without a fuel-status
    channel every running row is assumed closed-loop (the trims only move when
    it is, so this over-applies at worst to open-loop rows). Fuel status is a
    slowly polled state: it holds until the next reading, so it is carried
    forward past the resampler's staleness ceiling."""
    running = _running_mask(frame)
    if FUEL_STATUS_CHANNEL not in frame.columns:
        return running
    status = frame[FUEL_STATUS_CHANNEL].astype("float64").ffill()
    return running & status.isin([float(s) for s in CLOSED_LOOP_FUEL_STATES])


def _window_eligible(frame: pd.DataFrame, mask: pd.Series, kind: str) -> bool:
    rows = frame.loc[mask]
    if rows.empty:
        return False
    if kind == "idle_air_leak":
        return bool(_idle_mask(rows).mean() >= 0.25) and _observed(rows, MAF_CHANNEL)
    if kind == "thermostat":
        return _observed(rows, "obd:COOLANT_TEMP")
    if kind == "lazy_o2":
        return bool(_running_mask(rows).mean() >= 0.5) and _observed(rows, "obd:O2_WR_LAMBDA_S1")
    if kind in {"maf_gain", "bank_imbalance"}:
        return bool(_closed_loop_mask(rows).mean() >= 0.5)
    raise ValueError(f"unsupported realistic fault kind: {kind!r}")


def _observed(frame: pd.DataFrame, column: str) -> bool:
    return column in frame.columns and bool(frame[column].notna().any())


def choose_fault_window(
    resampled: pd.DataFrame,
    *,
    kind: str,
    length_seconds: float,
    lead_seconds: float,
    rng: np.random.Generator,
    attempts: int = 64,
) -> tuple[pd.Timestamp, pd.Timestamp] | str:
    """A window where ``kind`` can physically show, or the reason there is none.

    ``lead_seconds`` of clean rows must precede the window (the scorers'
    warm-up). The window must hold the fault's operating condition — idle for
    a leak, closed loop for a trim fault, the sensor observed for a sensor
    fault. A drive with no such stretch returns a string naming why, which the
    harness reports as *skipped*, not as a miss.
    """
    timestamps = pd.to_datetime(resampled[TIMESTAMP_COLUMN], utc=True).reset_index(drop=True)
    if timestamps.empty:
        return "no rows"
    first = timestamps.iloc[0] + pd.Timedelta(seconds=lead_seconds)
    last_start = timestamps.iloc[-1] - pd.Timedelta(seconds=length_seconds)
    candidates = np.flatnonzero(((timestamps >= first) & (timestamps <= last_start)).to_numpy())
    if candidates.size == 0:
        return f"drive shorter than {lead_seconds:.0f} s lead + {length_seconds:.0f} s window"
    frame = resampled.reset_index(drop=True)
    for position in rng.permutation(candidates)[:attempts]:
        start = timestamps.iloc[int(position)]
        end = start + pd.Timedelta(seconds=length_seconds)
        if _window_eligible(frame, (timestamps >= start) & (timestamps < end), kind):
            return start, end
    return f"no {length_seconds:.0f} s stretch meets the {kind} condition"


def _physical_apply(
    frame: pd.DataFrame,
    column: str,
    mask: pd.Series,
    fn: Callable[[pd.Series], pd.Series],
    bindings: Bindings | None,
) -> str | None:
    """Apply ``fn`` (physical units in, physical units out) to ``column`` on
    ``mask``; return the reason when the column cannot be perturbed."""
    if column not in frame.columns:
        return "absent"
    window = mask & frame[column].notna()
    if not bool(window.any()):
        return "not observed"
    values = frame.loc[window, column].astype("float64")
    if is_decoded_column(column):
        try:
            scale, offset = decoded_factors(column, bindings)
        except UnscaledDecodedChannelError:
            return "unscaled"
        changed = (fn(values * scale + offset) - offset) / scale
    else:
        changed = fn(values)
    # Keep the column's float width (the resampler emits float32 channels);
    # a non-float column is widened rather than truncating the fault.
    if pd.api.types.is_float_dtype(frame[column].dtype):
        frame.loc[window, column] = changed.astype(frame[column].dtype)
    else:
        frame[column] = frame[column].astype("float64")
        frame.loc[window, column] = changed
    return None


def _scaled_by(gain: float) -> Callable[[pd.Series], pd.Series]:
    def apply(values: pd.Series) -> pd.Series:
        return values * gain

    return apply


def _offset_by(delta: float | pd.Series) -> Callable[[pd.Series], pd.Series]:
    """Add a constant, or a per-row series aligned on the frame index."""

    def apply(values: pd.Series) -> pd.Series:
        if isinstance(delta, pd.Series):
            return values + delta.loc[values.index]
        return values + delta

    return apply


def inject_realistic_fault(
    resampled: pd.DataFrame,
    *,
    kind: str,
    severity: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
    roles: Mapping[str, Sequence[str]] = DEFAULT_FAULT_CHANNEL_ROLES,
    bindings: Bindings | None = None,
) -> tuple[pd.DataFrame, RealisticInjection]:
    """Inject one realistic fault into a copy of a clean resampled frame.

    Every channel in the fault's roles is changed consistently, in physical
    units (``decoded:`` counts go through the binding's scale). The derived
    channels — air mass, total trim, bank balances, VE — are recomputed from
    these by ``featurize_wide_frame``, so they follow without being touched
    here. Returns the frame and what was, and was not, perturbed.
    """
    if kind not in REALISTIC_FAULTS:
        raise ValueError(f"unsupported realistic fault kind: {kind!r}")
    frame = resampled.copy()
    timestamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
    in_window = (timestamps >= start) & (timestamps < end)
    closed_loop = in_window & _closed_loop_mask(frame)
    edits: list[tuple[str, pd.Series, Callable[[pd.Series], pd.Series]]] = []

    def role(name: str) -> Sequence[str]:
        return roles.get(name, ())

    if kind == "maf_gain":
        gain = float(severity)
        compensation = (1.0 / gain - 1.0) * 100.0
        for column in (*role("airflow"), *role("calculated_load")):
            edits.append((column, in_window, _scaled_by(gain)))
        for column in (*role("stft_b1"), *role("stft_b2")):
            edits.append((column, closed_loop, _offset_by(compensation)))
    elif kind == "bank_imbalance":
        for column in role("stft_b1"):
            edits.append((column, closed_loop, _offset_by(float(severity))))
    elif kind == "thermostat":
        elapsed = (timestamps - start).dt.total_seconds()
        ramp = (elapsed / THERMOSTAT_RAMP_SECONDS).clip(lower=0.0, upper=1.0)
        drop = float(severity) * ramp
        for column in role("coolant"):
            edits.append((column, in_window, _offset_by(-drop)))
    elif kind == "lazy_o2":
        tau = float(severity)

        def lag(values: pd.Series) -> pd.Series:
            times = timestamps.loc[values.index]
            steps = times.diff().dt.total_seconds().fillna(0.0).to_numpy()
            alpha = 1.0 - np.exp(-steps / tau)
            raw = values.to_numpy(dtype="float64")
            out = np.empty_like(raw)
            out[0] = raw[0]
            for i in range(1, raw.size):
                out[i] = out[i - 1] + alpha[i] * (raw[i] - out[i - 1])
            return pd.Series(out, index=values.index)

        for column in role("af_sensor_b1"):
            edits.append((column, in_window, lag))
    elif kind == "idle_air_leak":
        if MAF_CHANNEL in frame.columns:
            airflow = frame[MAF_CHANNEL].astype("float64").clip(lower=MIN_LEAK_AIRFLOW_G_S)
            added = (100.0 * float(severity) / airflow).clip(upper=TRIM_AUTHORITY_PERCENT)
            leak_rows = closed_loop & added.notna()
        else:
            added = pd.Series(np.nan, index=frame.index)
            leak_rows = pd.Series(False, index=frame.index)
        for column in (*role("stft_b1"), *role("stft_b2")):
            edits.append((column, leak_rows, _offset_by(added)))

    perturbed: list[str] = []
    not_perturbed: dict[str, str] = {}
    for column, mask, fn in edits:
        reason = _physical_apply(frame, column, mask, fn, bindings)
        if reason is None:
            perturbed.append(column)
        else:
            not_perturbed[column] = reason
    return frame, RealisticInjection(
        kind=kind,
        severity=float(severity),
        start=pd.Timestamp(start),
        end=pd.Timestamp(end),
        perturbed=tuple(dict.fromkeys(perturbed)),
        not_perturbed=not_perturbed,
    )
