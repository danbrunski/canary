"""Rule-based operating-mode segmentation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import pandas as pd

from src.config import OperatingModeSpec
from src.preprocessing.resampler import gap_column

MODE_COLUMN = "operating_mode"
UNKNOWN_MODE = "unknown"

#: Signals whose coalesced value is nulled out on a stale (gap-flagged) row
#: before mode-rule evaluation, so a frozen sensor can't confidently mis-mode
#: a row — it falls to ``unknown`` instead. Narrowly scoped to coolant (the
#: signal that actually gates warmup/idle) rather than every signal: rpm/
#: speed/throttle are always-fast, always-gap-tracked already, and gating
#: them too would silently shrink segmentation coverage far beyond this
#: fix's intent.
_STALE_GATED_SIGNALS = frozenset({"coolant_temp"})

#: Derived signal name for the trailing-max ground speed (km/h) used to tell a
#: transient stop (recently driving) from a steady standing idle. Ephemeral:
#: computed here, consumed by the mode rules, never added to the output frame.
RECENT_SPEED_SIGNAL = "recent_speed"

#: Trailing window (s) for ``recent_speed``. Mirrors
#: ``PreprocessingConfig.idle_transient_lookback_seconds`` so direct callers
#: that don't thread config still segment like the configured default.
DEFAULT_RECENT_MOTION_WINDOW_SECONDS = 180.0

# Each signal maps to an ordered tuple of candidate wide columns; the segmenter
# coalesces them row-wise (first non-NaN in priority order). For ``speed`` the
# OBD wheel-speed PID (VSS) is the *primary* source: it reads a true 0 the
# instant the truck stops and tracks the drivetrain directly, so it's the
# reliable member. GPS ground-speed is the back-stop (used only when OBD is
# absent — e.g. rolling with the engine off, or a PID dropout): it has a
# stationary noise floor and over-reads on marginal fixes (observed 151 km/h vs
# OBD's 110), so it corroborates rather than gates. GPS↔OBD *disagreement* is
# surfaced separately as a sensor-committee residual (see analysis.committees),
# not by which one wins the coalesce.
DEFAULT_SIGNAL_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "rpm": ("obd:RPM", "obd:rpm"),
    "speed": ("obd:SPEED", "obd:speed", "gps:speed"),
    "throttle": ("obd:THROTTLE_POS", "obd:throttle_pos"),
    "coolant_temp": ("obd:COOLANT_TEMP", "obd:coolant_temp"),
}


@dataclass(frozen=True)
class SegmenterConfig:
    """Column mapping for operating-mode threshold inputs.

    Each signal maps either to a single wide column or to an ordered tuple of
    candidate columns coalesced row-wise (first non-NaN wins). A plain string is
    treated as a one-element candidate list, so single-source configs read the
    same as before.
    """

    signal_columns: Mapping[str, str | tuple[str, ...]]
    unknown_mode: str = UNKNOWN_MODE
    output_column: str = MODE_COLUMN
    #: Trailing window (s) for the derived ``recent_speed`` signal, and the
    #: columns its per-session rolling max needs. Computed only when a mode
    #: actually gates on ``recent_speed`` and a timestamp column is present;
    #: otherwise the signal is simply absent and recent-motion modes are skipped.
    recent_motion_window_seconds: float = DEFAULT_RECENT_MOTION_WINDOW_SECONDS
    session_column: str = "session"
    timestamp_column: str = "timestamp"


def default_segmenter_config(frame: pd.DataFrame) -> SegmenterConfig:
    """Build a config from canonical wide columns present in frame.

    Keeps *every* present candidate per signal (in priority order) rather than
    just the first, so the row-wise coalesce can fall back from GPS speed to OBD
    speed when only some rows have a fix.
    """
    mapping: dict[str, str | tuple[str, ...]] = {}
    for signal, candidates in DEFAULT_SIGNAL_COLUMNS.items():
        present = tuple(candidate for candidate in candidates if candidate in frame.columns)
        if present:
            mapping[signal] = present
    return SegmenterConfig(signal_columns=mapping)


def segment_operating_modes(
    frame: pd.DataFrame,
    operating_modes: Mapping[str, OperatingModeSpec],
    *,
    config: SegmenterConfig | None = None,
    recent_motion_window_seconds: float | None = None,
) -> pd.DataFrame:
    """Append an operating-mode label based on configured threshold rules.

    Modes are evaluated in mapping order; the first mode whose thresholds all
    match wins. Rows with missing required signals or no matching rule receive
    `unknown_mode`.

    ``recent_motion_window_seconds`` overrides ``config.recent_motion_window_seconds``
    so callers that don't build a ``SegmenterConfig`` (LSTM/labeling/replay) can
    still thread the operator-configured window and keep train/serve parity.
    """
    seg_config = default_segmenter_config(frame) if config is None else config
    window_s = (
        seg_config.recent_motion_window_seconds
        if recent_motion_window_seconds is None
        else recent_motion_window_seconds
    )
    result = frame.copy()
    labels = pd.Series(seg_config.unknown_mode, index=result.index, dtype="object")
    unassigned = pd.Series(True, index=result.index)

    # Coalesce each configured signal across its candidate columns once
    # (OBD-primary speed falls back to GPS per row). A signal whose candidate
    # columns are all absent from the frame is simply omitted, so any mode that
    # requires it is skipped — matching the prior "missing required signal"
    # behavior.
    coalesced: dict[str, pd.Series] = {}
    for signal in seg_config.signal_columns:
        series = _coalesced_series(
            result,
            _candidates(seg_config, signal),
            gap_masked=signal in _STALE_GATED_SIGNALS,
        )
        if series is not None:
            coalesced[signal] = series

    # Derive ``recent_speed`` (trailing per-session max ground speed) only when a
    # mode actually gates on it — it distinguishes a transient stop (driving
    # moments ago) from a steady standing idle. Ephemeral: used for the labels
    # below, never written to ``result``, so it can't leak into the feature set.
    if RECENT_SPEED_SIGNAL in _all_required_signals(operating_modes) and "speed" in coalesced:
        recent = _recent_speed_max(result, coalesced["speed"], seg_config, window_s)
        if recent is not None:
            coalesced[RECENT_SPEED_SIGNAL] = recent

    for mode_name, spec in operating_modes.items():
        required_signals = _required_signals(spec)
        if not required_signals:
            continue
        if any(signal not in coalesced for signal in required_signals):
            continue

        mask = unassigned.copy()
        for signal in required_signals:
            mask &= _threshold_mask(coalesced[signal], spec, signal)

        labels.loc[mask] = mode_name
        unassigned &= ~mask

    result[seg_config.output_column] = labels
    return result


def mode_for_row(
    row: pd.Series,
    operating_modes: Mapping[str, OperatingModeSpec],
    *,
    config: SegmenterConfig,
) -> str:
    """Return the first matching mode for one row."""
    for mode_name, spec in operating_modes.items():
        required_signals = _required_signals(spec)
        if not required_signals:
            continue
        if any(signal not in config.signal_columns for signal in required_signals):
            continue
        values: dict[str, float] = {}
        complete = True
        for signal in required_signals:
            value = _row_value(row, _candidates(config, signal))
            if value is None:
                complete = False
                break
            values[signal] = value
        if complete and all(
            _value_matches(values[signal], spec, signal) for signal in required_signals
        ):
            return mode_name
    return config.unknown_mode


def _required_signals(spec: OperatingModeSpec) -> list[str]:
    signals: list[str] = []
    if spec.rpm_min is not None or spec.rpm_max is not None:
        signals.append("rpm")
    if spec.speed_min is not None or spec.speed_max is not None:
        signals.append("speed")
    if spec.throttle_min is not None or spec.throttle_max is not None:
        signals.append("throttle")
    if spec.coolant_temp_min is not None or spec.coolant_temp_max is not None:
        signals.append("coolant_temp")
    if spec.recent_speed_min is not None or spec.recent_speed_max is not None:
        signals.append(RECENT_SPEED_SIGNAL)
    return signals


def _all_required_signals(operating_modes: Mapping[str, OperatingModeSpec]) -> set[str]:
    """Union of signals any mode constrains — used to skip deriving unused ones."""
    signals: set[str] = set()
    for spec in operating_modes.values():
        signals.update(_required_signals(spec))
    return signals


def _candidates(config: SegmenterConfig, signal: str) -> tuple[str, ...]:
    """Normalize a signal's column spec to an ordered candidate tuple."""
    value = config.signal_columns[signal]
    return (value,) if isinstance(value, str) else tuple(value)


def _coalesced_series(
    frame: pd.DataFrame,
    candidates: tuple[str, ...],
    *,
    gap_masked: bool = False,
) -> pd.Series | None:
    """Row-wise first-non-NaN over the candidate columns present in ``frame``.

    Returns ``None`` when none of the candidates is a column in the frame, so
    callers can treat the signal as missing.

    ``gap_masked`` treats a candidate's gap-flagged (stale/forward-filled)
    rows as absent for *that* candidate before the coalesce picks a value —
    so a frozen reading doesn't confidently drive a mode rule. If a
    ``<candidate>__gap`` column isn't present (segmenter called pre-1.2, or
    on a frame the resampler never touched), the candidate is treated as
    always-fresh — no gap column means no known staleness, not the reverse.
    """
    present = [column for column in candidates if column in frame.columns]
    if not present:
        return None

    def _values(column: str) -> pd.Series:
        values = pd.to_numeric(frame[column], errors="coerce")
        if gap_masked:
            gap_col = gap_column(column)
            if gap_col in frame.columns:
                stale = frame[gap_col].fillna(True).astype(bool)
                values = values.where(~stale)
        return values

    series = _values(present[0])
    for column in present[1:]:
        series = series.fillna(_values(column))
    return series


def _row_value(row: pd.Series, candidates: tuple[str, ...]) -> float | None:
    """First present, non-NaN candidate value in ``row`` (priority order)."""
    for column in candidates:
        if column in row.index and pd.notna(row[column]):
            try:
                return float(row[column])
            except (TypeError, ValueError):
                return None
    return None


def _threshold_mask(values: pd.Series, spec: OperatingModeSpec, signal: str) -> pd.Series:
    minimum, maximum = _bounds(spec, signal)
    mask = values.notna()
    if minimum is not None:
        mask &= values >= minimum
    if maximum is not None:
        mask &= values <= maximum
    return mask


def _value_matches(value: float, spec: OperatingModeSpec, signal: str) -> bool:
    minimum, maximum = _bounds(spec, signal)
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def _bounds(spec: OperatingModeSpec, signal: str) -> tuple[float | None, float | None]:
    if signal == "rpm":
        return spec.rpm_min, spec.rpm_max
    if signal == "speed":
        return spec.speed_min, spec.speed_max
    if signal == "throttle":
        return spec.throttle_min, spec.throttle_max
    if signal == "coolant_temp":
        return spec.coolant_temp_min, spec.coolant_temp_max
    if signal == RECENT_SPEED_SIGNAL:
        return spec.recent_speed_min, spec.recent_speed_max
    raise ValueError(f"unknown operating-mode signal {signal!r}")


def _recent_speed_max(
    frame: pd.DataFrame,
    speed: pd.Series,
    config: SegmenterConfig,
    window_seconds: float,
) -> pd.Series | None:
    """Trailing (causal) max ground speed over ``window_seconds``, per session.

    Returns ``None`` when there's no timestamp column to window over, so the
    caller treats ``recent_speed`` as absent and recent-motion modes are skipped.
    Grouped by session so motion never bleeds across sessions; computed only on
    the past, so it's identical offline and in the live ring buffer.
    """
    ts_col = config.timestamp_column
    if ts_col not in frame.columns:
        return None
    timestamps = pd.to_datetime(frame[ts_col], utc=True, errors="coerce")
    if timestamps.isna().all():
        return None
    window = f"{max(int(round(window_seconds)), 1)}s"
    values = pd.to_numeric(speed, errors="coerce")
    work = pd.DataFrame({"v": values.to_numpy(), "ts": timestamps.to_numpy()}, index=frame.index)
    if config.session_column in frame.columns:
        groups: Iterable[tuple[object, pd.DataFrame]] = work.groupby(
            frame[config.session_column].to_numpy(), sort=False
        )
    else:
        groups = [(None, work)]
    out = pd.Series(float("nan"), index=frame.index, dtype="float64")
    for _, sub in groups:
        ordered = sub.sort_values("ts", kind="stable")
        rolled = ordered.set_index("ts")["v"].rolling(window).max()
        out.loc[ordered.index] = rolled.to_numpy()
    return out
