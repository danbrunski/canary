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


def test_segment_operating_modes_treats_min_and_max_bounds_as_inclusive() -> None:
    modes = {
        "band": OperatingModeSpec(
            rpm_min=1000,
            rpm_max=2000,
            speed_min=10,
            speed_max=20,
            throttle_min=30,
            throttle_max=40,
        )
    }
    frame = pd.DataFrame(
        [
            {"obd:RPM": 1000.0, "obd:SPEED": 10.0, "obd:THROTTLE_POS": 30.0},
            {"obd:RPM": 2000.0, "obd:SPEED": 20.0, "obd:THROTTLE_POS": 40.0},
            {"obd:RPM": 999.9, "obd:SPEED": 10.0, "obd:THROTTLE_POS": 30.0},
            {"obd:RPM": 1000.0, "obd:SPEED": 20.1, "obd:THROTTLE_POS": 30.0},
            {"obd:RPM": 1000.0, "obd:SPEED": 10.0, "obd:THROTTLE_POS": 40.1},
        ]
    )

    segmented = segment_operating_modes(frame, modes)

    assert list(segmented[MODE_COLUMN]) == [
        "band",
        "band",
        UNKNOWN_MODE,
        UNKNOWN_MODE,
        UNKNOWN_MODE,
    ]


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

    # Each signal keeps the (ordered) tuple of present candidate columns.
    assert config.signal_columns == {
        "rpm": ("obd:rpm",),
        "speed": ("obd:speed",),
        "throttle": ("obd:throttle_pos",),
    }


def test_mode_for_row_returns_first_matching_mode() -> None:
    row = pd.Series({"rpm": 850.0, "speed": 0.0, "throttle": 5.0})
    config = SegmenterConfig(
        signal_columns={"rpm": "rpm", "speed": "speed", "throttle": "throttle"}
    )

    assert mode_for_row(row, _modes(), config=config) == "idle"


def test_segment_operating_modes_uses_coolant_temp_to_split_warmup_from_idle() -> None:
    """Adding ``coolant_temp_max`` to a mode pulls cold-start data into its own
    bucket so the behavior models don't have to cover both regimes with one
    surface (the May 2026 R²=0.41 coolant_warmup mess).
    """
    modes = {
        "warmup": OperatingModeSpec(speed_max=0, coolant_temp_max=70),
        "idle": OperatingModeSpec(rpm_max=900, speed_max=0, coolant_temp_min=70),
    }
    frame = pd.DataFrame(
        [
            # Cold idle (cold coolant) — should go to warmup
            {"obd:RPM": 800.0, "obd:SPEED": 0.0, "obd:COOLANT_TEMP": 30.0},
            # Warm idle (warm coolant) — should go to idle
            {"obd:RPM": 800.0, "obd:SPEED": 0.0, "obd:COOLANT_TEMP": 85.0},
            # Boundary at coolant_temp_max=70 — inclusive, so 70.0 matches warmup
            {"obd:RPM": 800.0, "obd:SPEED": 0.0, "obd:COOLANT_TEMP": 70.0},
        ]
    )
    segmented = segment_operating_modes(frame, modes)
    assert list(segmented[MODE_COLUMN]) == ["warmup", "idle", "warmup"]


def test_segment_operating_modes_marks_warmup_unknown_when_coolant_temp_missing() -> None:
    """If a mode requires coolant_temp but it's not in the frame, that mode is
    simply skipped — rows fall through to the next matching rule or unknown.
    """
    modes = {
        "warmup": OperatingModeSpec(speed_max=0, coolant_temp_max=70),
        "idle": OperatingModeSpec(rpm_max=900, speed_max=0),
    }
    frame = pd.DataFrame(
        [
            {"obd:RPM": 800.0, "obd:SPEED": 0.0},
        ]
    )
    segmented = segment_operating_modes(frame, modes)
    # warmup requires coolant_temp, which isn't in the frame, so warmup is
    # skipped; idle still matches.
    assert list(segmented[MODE_COLUMN]) == ["idle"]


def test_default_segmenter_config_picks_up_coolant_temp_column() -> None:
    frame = pd.DataFrame(columns=["obd:RPM", "obd:SPEED", "obd:THROTTLE_POS", "obd:COOLANT_TEMP"])
    config = default_segmenter_config(frame)
    assert config.signal_columns["coolant_temp"] == ("obd:COOLANT_TEMP",)


def test_default_segmenter_config_prefers_gps_speed_with_obd_fallback() -> None:
    """When both speed sources are present, GPS leads and OBD trails."""
    frame = pd.DataFrame(columns=["obd:RPM", "obd:SPEED", "gps:speed"])
    config = default_segmenter_config(frame)
    assert config.signal_columns["speed"] == ("gps:speed", "obd:SPEED")


def test_segment_coalesces_gps_speed_over_obd_per_row() -> None:
    """GPS speed wins where present; rows without a GPS fix fall back to OBD."""
    frame = pd.DataFrame(
        [
            # GPS fix present, reads highway speed; OBD disagrees → GPS wins.
            {"obd:RPM": 2200.0, "obd:SPEED": 5.0, "gps:speed": 95.0},
            # No GPS fix this row (NaN) → coalesce falls back to OBD city speed.
            {"obd:RPM": 1500.0, "obd:SPEED": 35.0, "gps:speed": float("nan")},
        ]
    )
    modes = {
        "city": OperatingModeSpec(speed_max=50),
        "highway": OperatingModeSpec(speed_min=80),
    }
    segmented = segment_operating_modes(frame, modes)
    assert list(segmented[MODE_COLUMN]) == ["highway", "city"]


def test_segment_uses_gps_speed_when_obd_speed_absent() -> None:
    """Pure-GPS frame (no OBD speed column at all) still segments by speed."""
    frame = pd.DataFrame(
        [
            {"obd:RPM": 2200.0, "gps:speed": 95.0},
            {"obd:RPM": 1500.0, "gps:speed": 30.0},
        ]
    )
    modes = {
        "city": OperatingModeSpec(speed_max=50),
        "highway": OperatingModeSpec(speed_min=80),
    }
    segmented = segment_operating_modes(frame, modes)
    assert list(segmented[MODE_COLUMN]) == ["highway", "city"]
