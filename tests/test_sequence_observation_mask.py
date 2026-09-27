"""A03.1 — the sequence mask means "a sample arrived", gated per artifact.

Offline windows (training/calibration) and the live serve buffer must build
the same mask for the same rows, under whichever rule the artifact recorded:
``arrived_sample`` for new training, ``fresh_within_max_gap`` for artifacts
trained before the record existed (the deployed 2026-09-20 model).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.models.inference import SequenceWindowBuffer
from src.models.sequence_dataset import (
    MASK_ARRIVED_SAMPLE,
    MASK_FRESH_WITHIN_MAX_GAP,
    OBSERVATION_MASK_SCHEMA_KEY,
    build_sequence_windows,
    observation_mask_from_schema,
    observation_mask_record,
)


def _frame() -> pd.DataFrame:
    """Baro polled on rows 0 and 5, fresh (within the slow ceiling) throughout;
    rpm arrives every row but row 3; a derived column has no arrival flag."""
    rows = 8
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["s"] * rows,
            TIMESTAMP_COLUMN: pd.date_range("2026-09-18T13:43:45Z", periods=rows, freq="s"),
            "obd:BARO": [80.0] * rows,
            "obd:BARO__gap": [False] * rows,
            "obd:BARO__arrived": [True, False, False, False, False, True, False, False],
            "obd:RPM": [800.0 + i for i in range(rows)],
            "obd:RPM__gap": [False] * rows,
            "obd:RPM__arrived": [True, True, True, False, True, True, True, True],
            "obd:RPM__mean": [800.0] * rows,
            "derived:X": [1.0] * rows,
            "derived:X__gap": [False] * 6 + [True] * 2,
        }
    )


FEATURES = ("obd:BARO", "obd:RPM", "obd:RPM__mean", "derived:X")


def test_legacy_rule_counts_forward_fills_as_observed() -> None:
    build = build_sequence_windows(
        _frame(),
        sequence_length=4,
        feature_columns=FEATURES,
        observation_mask=MASK_FRESH_WITHIN_MAX_GAP,
    )
    assert build.observation_mask == MASK_FRESH_WITHIN_MAX_GAP
    assert build.mask[:, 0].tolist() == [1.0] * 8  # baro: every row "observed"


def test_arrival_rule_masks_by_arrival_and_names_its_fallbacks() -> None:
    build = build_sequence_windows(
        _frame(), sequence_length=4, feature_columns=FEATURES, observation_mask=MASK_ARRIVED_SAMPLE
    )
    assert build.observation_mask == MASK_ARRIVED_SAMPLE
    assert build.mask[:, 0].tolist() == [1, 0, 0, 0, 0, 1, 0, 0]
    # Δt now counts seconds since baro's real sample, not since row 0 of the gap.
    assert build.delta[:, 0].tolist() == [0, 1, 2, 3, 4, 0, 1, 2]
    rpm = [1, 1, 1, 0, 1, 1, 1, 1]
    assert build.mask[:, 1].tolist() == rpm
    assert build.mask[:, 2].tolist() == rpm  # rolling sibling reads its base channel
    assert build.mask[:, 3].tolist() == [1] * 6 + [0] * 2  # freshness fallback
    assert build.freshness_fallback_columns == ("derived:X",)


def test_auto_rule_follows_the_frame_and_refuses_a_frame_without_arrivals() -> None:
    assert (
        build_sequence_windows(
            _frame(), sequence_length=4, feature_columns=FEATURES
        ).observation_mask
        == MASK_ARRIVED_SAMPLE
    )
    old = _frame().drop(columns=["obd:BARO__arrived", "obd:RPM__arrived"])
    assert (
        build_sequence_windows(old, sequence_length=4, feature_columns=FEATURES).observation_mask
        == MASK_FRESH_WITHIN_MAX_GAP
    )
    with pytest.raises(ValueError, match="no <channel>__arrived flags"):
        build_sequence_windows(
            old, sequence_length=4, feature_columns=FEATURES, observation_mask=MASK_ARRIVED_SAMPLE
        )


def test_schema_without_a_record_is_the_legacy_rule_and_unknown_rules_are_refused() -> None:
    assert observation_mask_from_schema({}) == (MASK_FRESH_WITHIN_MAX_GAP, frozenset())
    record = observation_mask_record(MASK_ARRIVED_SAMPLE, ["derived:X"])
    assert observation_mask_from_schema({OBSERVATION_MASK_SCHEMA_KEY: record}) == (
        MASK_ARRIVED_SAMPLE,
        frozenset({"derived:X"}),
    )
    with pytest.raises(ValueError, match="unknown observation mask rule"):
        observation_mask_from_schema({OBSERVATION_MASK_SCHEMA_KEY: {"rule": "guess"}})


@pytest.mark.parametrize("rule", [MASK_FRESH_WITHIN_MAX_GAP, MASK_ARRIVED_SAMPLE])
def test_serve_buffer_builds_the_offline_mask_and_delta_for_the_same_rows(rule: str) -> None:
    """Train/serve parity under each rule: the live buffer, fed the frame's rows
    one at a time, reproduces the offline window's mask and Δt exactly."""
    frame = _frame()
    offline = build_sequence_windows(
        frame, sequence_length=8, feature_columns=FEATURES, observation_mask=rule
    )
    buffer = SequenceWindowBuffer(
        feature_columns=FEATURES,
        sequence_length=8,
        observation_mask=rule,
        freshness_fallback_columns=frozenset(offline.freshness_fallback_columns),
    )
    for _, row in frame.iterrows():
        buffer.append(row.to_dict())
    assert buffer.ready
    np.testing.assert_array_equal(buffer.mask_array(), offline.mask.astype(np.float32))
    np.testing.assert_array_equal(buffer.delta_array(), offline.delta.astype(np.float32))


def test_serve_buffer_reads_a_missing_arrival_flag_as_not_arrived() -> None:
    buffer = SequenceWindowBuffer(
        feature_columns=("obd:RPM",), sequence_length=1, observation_mask=MASK_ARRIVED_SAMPLE
    )
    buffer.append({SESSION_COLUMN: "s", TIMESTAMP_COLUMN: "2026-09-18T00:00:00Z", "obd:RPM": 800.0})
    assert buffer.mask_array().tolist() == [[0.0]]
