"""Tests for operating-mode segmentation."""

from __future__ import annotations

import pandas as pd

from src.config import OperatingModeSpec
from src.preprocessing.segmenter import (
    MODE_COLUMN,
    UNKNOWN_MODE,
    SegmenterConfig,
    default_segmenter_config,
    mode_for_row,
    segment_operating_modes,
)


def _modes() -> dict[str, OperatingModeSpec]:
    return {
        "idle": OperatingModeSpec(rpm_max=900, speed_max=0),
        "city": OperatingModeSpec(speed_max=50),
        "highway": OperatingModeSpec(speed_min=80),
        "wot": OperatingModeSpec(throttle_min=80),
    }


def test_segment_operating_modes_labels_rows_by_thresholds() -> None:
    frame = pd.DataFrame(
        [
            {"obd:RPM": 900.0, "obd:SPEED": 0.0, "obd:THROTTLE_POS": 5.0},
            {"obd:RPM": 1500.0, "obd:SPEED": 35.0, "obd:THROTTLE_POS": 20.0},
            {"obd:RPM": 2200.0, "obd:SPEED": 90.0, "obd:THROTTLE_POS": 30.0},
            {"obd:RPM": 3200.0, "obd:SPEED": 65.0, "obd:THROTTLE_POS": 82.0},
            {"obd:RPM": 1000.0, "obd:SPEED": 65.0, "obd:THROTTLE_POS": 20.0},
        ]
    )

    segmented = segment_operating_modes(frame, _modes())

    assert list(segmented[MODE_COLUMN]) == ["idle", "city", "highway", "wot", UNKNOWN_MODE]


def test_segment_operating_modes_uses_configured_mode_order_for_precedence() -> None:
    frame = pd.DataFrame([{"obd:RPM": 2000.0, "obd:SPEED": 35.0, "obd:THROTTLE_POS": 90.0}])

    segmented = segment_operating_modes(frame, _modes())

    assert segmented.loc[0, MODE_COLUMN] == "city"


def test_segment_operating_modes_supports_custom_mode_names() -> None:
    frame = pd.DataFrame([{"obd:RPM": 1800.0, "obd:SPEED": 20.0, "obd:THROTTLE_POS": 25.0}])
    modes = {
        "technical_trail": OperatingModeSpec(speed_max=30, throttle_max=40),
    }

    segmented = segment_operating_modes(frame, modes)

    assert segmented.loc[0, MODE_COLUMN] == "technical_trail"


def test_segment_operating_modes_marks_missing_required_values_unknown() -> None:
    frame = pd.DataFrame(
        [
            {"obd:RPM": None, "obd:SPEED": 0.0, "obd:THROTTLE_POS": 5.0},
            {"obd:RPM": 800.0, "obd:SPEED": None, "obd:THROTTLE_POS": 5.0},
        ]
    )

    segmented = segment_operating_modes(frame, {"idle": _modes()["idle"]})

    assert list(segmented[MODE_COLUMN]) == [UNKNOWN_MODE, UNKNOWN_MODE]


def test_segment_operating_modes_ignores_empty_mode_specs() -> None:
    frame = pd.DataFrame([{"obd:RPM": 850.0, "obd:SPEED": 0.0, "obd:THROTTLE_POS": 5.0}])

    segmented = segment_operating_modes(frame, {"empty": OperatingModeSpec()})

    assert segmented.loc[0, MODE_COLUMN] == UNKNOWN_MODE


def test_segment_operating_modes_allows_explicit_signal_columns() -> None:
    frame = pd.DataFrame([{"rpm": 850.0, "speed_kmh": 0.0, "throttle_pct": 8.0}])
    config = SegmenterConfig(
        signal_columns={
            "rpm": "rpm",
            "speed": "speed_kmh",
            "throttle": "throttle_pct",
        },
        unknown_mode="unclassified",
        output_column="mode",
    )

    segmented = segment_operating_modes(frame, _modes(), config=config)

    assert segmented.loc[0, "mode"] == "idle"


def test_default_segmenter_config_detects_lowercase_columns() -> None:
    frame = pd.DataFrame(columns=["obd:rpm", "obd:speed", "obd:throttle_pos"])

    config = default_segmenter_config(frame)

    assert config.signal_columns == {
        "rpm": "obd:rpm",
        "speed": "obd:speed",
        "throttle": "obd:throttle_pos",
    }


def test_mode_for_row_returns_first_matching_mode() -> None:
    row = pd.Series({"rpm": 850.0, "speed": 0.0, "throttle": 5.0})
    config = SegmenterConfig(
        signal_columns={"rpm": "rpm", "speed": "speed", "throttle": "throttle"}
    )

    assert mode_for_row(row, _modes(), config=config) == "idle"
