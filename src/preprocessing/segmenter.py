"""Rule-based operating-mode segmentation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from src.config import OperatingModeSpec

MODE_COLUMN = "operating_mode"
UNKNOWN_MODE = "unknown"

# Each signal maps to an ordered tuple of candidate wide columns; the segmenter
# coalesces them row-wise (first non-NaN in priority order). For ``speed`` the
# GPS ground-speed is preferred over the OBD wheel-speed PID — it's the more
# reliable mode-detection signal — and OBD is the fallback whenever GPS has no
# fix, so segmentation degrades gracefully when no GPS is present.
DEFAULT_SIGNAL_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "rpm": ("obd:RPM", "obd:rpm"),
    "speed": ("gps:speed", "obd:SPEED", "obd:speed"),
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
) -> pd.DataFrame:
    """Append an operating-mode label based on configured threshold rules.

    Modes are evaluated in mapping order; the first mode whose thresholds all
    match wins. Rows with missing required signals or no matching rule receive
    `unknown_mode`.
    """
    seg_config = default_segmenter_config(frame) if config is None else config
    result = frame.copy()
    labels = pd.Series(seg_config.unknown_mode, index=result.index, dtype="object")
    unassigned = pd.Series(True, index=result.index)

    # Coalesce each configured signal across its candidate columns once
    # (GPS-preferred speed falls back to OBD per row). A signal whose candidate
    # columns are all absent from the frame is simply omitted, so any mode that
    # requires it is skipped — matching the prior "missing required signal"
    # behavior.
    coalesced: dict[str, pd.Series] = {}
    for signal in seg_config.signal_columns:
        series = _coalesced_series(result, _candidates(seg_config, signal))
        if series is not None:
            coalesced[signal] = series

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
    return signals


def _candidates(config: SegmenterConfig, signal: str) -> tuple[str, ...]:
    """Normalize a signal's column spec to an ordered candidate tuple."""
    value = config.signal_columns[signal]
    return (value,) if isinstance(value, str) else tuple(value)


def _coalesced_series(frame: pd.DataFrame, candidates: tuple[str, ...]) -> pd.Series | None:
    """Row-wise first-non-NaN over the candidate columns present in ``frame``.

    Returns ``None`` when none of the candidates is a column in the frame, so
    callers can treat the signal as missing.
    """
    present = [column for column in candidates if column in frame.columns]
    if not present:
        return None
    series = pd.to_numeric(frame[present[0]], errors="coerce")
    for column in present[1:]:
        series = series.fillna(pd.to_numeric(frame[column], errors="coerce"))
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
    raise ValueError(f"unknown operating-mode signal {signal!r}")
