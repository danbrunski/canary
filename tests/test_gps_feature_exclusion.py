"""GPS channels are logged, never modeled (H00 operator decision 1, H01.1).

Absolute position would teach the detectors location identity, and since
H01.1 no external sensor trains the detectors at all: ``gps:speed`` and its
rolling derivatives are refused by the source gate too.
"""

from __future__ import annotations

import pandas as pd

from src.features.feature_pipeline import (
    NON_TRAINABLE_GPS_CHANNELS,
    candidate_feature_columns,
    trainable_feature_columns,
)


def _gps_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "session": ["s", "s"],
            "timestamp": pd.to_datetime(["2026-05-24T00:00:00Z", "2026-05-24T00:00:01Z"]),
            "operating_mode": ["city", "city"],
            "obd:RPM": [800.0, 810.0],
            "gps:speed": [40.0, 41.0],
            "gps:speed__roc": [0.0, 1.0],
            "gps:latitude": [39.9, 39.9],
            "gps:latitude__mean": [39.9, 39.9],
            "gps:longitude": [-105.2, -105.2],
            "gps:altitude": [1600.0, 1601.0],
            "gps:heading": [90.0, 91.0],
            "gps:fix_quality": [1, 1],
            "gps:num_satellites": [7, 7],
            "gps:hdop": [1.1, 1.1],
        }
    )


def test_gps_speed_is_not_a_feature_candidate() -> None:
    cols = candidate_feature_columns(_gps_frame())
    assert "gps:speed" not in cols
    assert "gps:speed__roc" not in cols
    assert cols == ["obd:RPM"]


def test_gps_position_and_quality_channels_are_excluded() -> None:
    cols = candidate_feature_columns(_gps_frame())
    for excluded in NON_TRAINABLE_GPS_CHANNELS:
        assert excluded not in cols
    # Rolling derivatives of an excluded channel are excluded too.
    assert "gps:latitude__mean" not in cols


def test_trainable_columns_also_drop_position() -> None:
    cols = trainable_feature_columns(_gps_frame())
    assert "gps:speed" not in cols
    assert "gps:latitude" not in cols
    assert "gps:num_satellites" not in cols
