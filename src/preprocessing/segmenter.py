"""Rule-based operating-mode segmentation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from src.config import OperatingModeSpec

MODE_COLUMN = "operating_mode"
UNKNOWN_MODE = "unknown"

DEFAULT_SIGNAL_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "rpm": ("obd:RPM", "obd:rpm"),
    "speed": ("obd:SPEED", "obd:speed"),
    "throttle": ("obd:THROTTLE_POS", "obd:throttle_pos"),
}


@dataclass(frozen=True)
class SegmenterConfig:
    """Column mapping for operating-mode threshold inputs."""

    signal_columns: Mapping[str, str]
    unknown_mode: str = UNKNOWN_MODE
    output_column: str = MODE_COLUMN


def default_segmenter_config(frame: pd.DataFrame) -> SegmenterConfig:
    """Build a config from canonical wide columns present in frame."""
    mapping: dict[str, str] = {}
    for signal, candidates in DEFAULT_SIGNAL_COLUMNS.items():
        for candidate in candidates:
            if candidate in frame.columns:
                mapping[signal] = candidate
                break
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

    for mode_name, spec in operating_modes.items():
        required_signals = _required_signals(spec)
        if not required_signals:
            continue
        if any(signal not in seg_config.signal_columns for signal in required_signals):
            continue

        mask = unassigned.copy()
        for signal in required_signals:
            column = seg_config.signal_columns[signal]
            values = pd.to_numeric(result[column], errors="coerce")
            mask &= _threshold_mask(values, spec, signal)

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
        if all(
            pd.notna(row[config.signal_columns[signal]])
            and _value_matches(float(row[config.signal_columns[signal]]), spec, signal)
            for signal in required_signals
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
    return signals


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
    raise ValueError(f"unknown operating-mode signal {signal!r}")
