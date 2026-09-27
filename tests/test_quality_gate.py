"""Tests for the physical-impossibility reject gate (Task 1.2)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from src.preprocessing.quality_gate import apply_quality_gate, is_physically_valid
from src.signals.fsm import PhysicalLimit

_LIMITS = {
    "obd:RPM": PhysicalLimit(min=0.0, max=10_000.0, unit="rpm", reason="test"),
    "obd:COOLANT_TEMP": PhysicalLimit(min=-39.0, max=150.0, unit="degC", reason="test"),
}


# ---------------------------------------------------------------------------
# is_physically_valid — single-value (live) path
# ---------------------------------------------------------------------------


def test_is_physically_valid_within_limit() -> None:
    assert is_physically_valid("obd:RPM", 2500.0, limits=_LIMITS) is True


def test_is_physically_valid_outside_limit() -> None:
    assert is_physically_valid("obd:RPM", 16383.0, limits=_LIMITS) is False


def test_is_physically_valid_channel_absent_from_table_is_always_valid() -> None:
    # Absence means "no reject" — never a guessed bound.
    assert is_physically_valid("obd:MYSTERY_CHANNEL", 1e12, limits=_LIMITS) is True


def test_is_physically_valid_none_and_nan_pass_through() -> None:
    # NaN/None are already "missing", not a quality-gate concern.
    assert is_physically_valid("obd:RPM", None, limits=_LIMITS) is True
    assert is_physically_valid("obd:RPM", float("nan"), limits=_LIMITS) is True


def test_is_physically_valid_non_numeric_passes_through() -> None:
    assert is_physically_valid("obd:RPM", "not-a-number", limits=_LIMITS) is True  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# apply_quality_gate — vectorized (batch/training) path
# ---------------------------------------------------------------------------


def test_apply_quality_gate_nans_per_cell_not_whole_row() -> None:
    frame = pd.DataFrame(
        {
            "obd:RPM": [2000.0, 16383.0, 3000.0],
            "obd:COOLANT_TEMP": [90.0, 91.0, -40.0],
            "obd:MAF": [5.0, 6.0, 7.0],  # untouched — no curated limit
        }
    )
    gated, result = apply_quality_gate(frame, limits=_LIMITS)

    # Row 1's bad RPM is rejected, but its coolant reading (which is fine)
    # survives — this is a per-cell reject, not a row drop.
    assert math.isnan(gated.loc[1, "obd:RPM"])
    assert gated.loc[1, "obd:COOLANT_TEMP"] == 91.0
    # Row 2's bad coolant reading is rejected, its good RPM survives.
    assert math.isnan(gated.loc[2, "obd:COOLANT_TEMP"])
    assert gated.loc[2, "obd:RPM"] == 3000.0
    # Untouched channel is bit-for-bit identical.
    assert gated["obd:MAF"].tolist() == [5.0, 6.0, 7.0]
    assert len(gated) == len(frame)  # no rows dropped


def test_apply_quality_gate_reports_per_channel_reject_counts() -> None:
    frame = pd.DataFrame(
        {
            "obd:RPM": [2000.0, 16383.0, -1.0],
            "obd:COOLANT_TEMP": [90.0, 91.0, -40.0],
        }
    )
    _, result = apply_quality_gate(frame, limits=_LIMITS)
    assert result.reject_counts == {"obd:RPM": 2, "obd:COOLANT_TEMP": 1}
    assert result.total_rejected == 3


def test_apply_quality_gate_no_rejects_is_a_no_op_copy() -> None:
    frame = pd.DataFrame({"obd:RPM": [1000.0, 2000.0]})
    gated, result = apply_quality_gate(frame, limits=_LIMITS)
    assert result.reject_counts == {}
    assert result.total_rejected == 0
    assert gated is frame  # cheap no-op: same object when nothing rejected


def test_apply_quality_gate_ignores_columns_absent_from_limits() -> None:
    frame = pd.DataFrame({"obd:UNKNOWN": [1e9, -1e9]})
    gated, result = apply_quality_gate(frame, limits=_LIMITS)
    assert gated["obd:UNKNOWN"].tolist() == [1e9, -1e9]
    assert result.reject_counts == {}


def test_apply_quality_gate_preexisting_nan_not_double_counted() -> None:
    frame = pd.DataFrame({"obd:RPM": [np.nan, 16383.0]})
    _, result = apply_quality_gate(frame, limits=_LIMITS)
    assert result.reject_counts == {"obd:RPM": 1}
