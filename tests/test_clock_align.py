"""Tests for RPM-based clock alignment (roadmap Phase 4.1)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.clock_align import MIN_ALIGNMENT_R, AlignmentError, align_clocks


def _rpm_series(start: pd.Timestamp, seconds: float, hz: float, seed: int = 0) -> pd.Series:
    """A synthetic rpm trace with enough structure to correlate on."""
    n = int(seconds * hz)
    t = np.arange(n) / hz
    rng = np.random.default_rng(seed)
    rpm = 2000 + 1200 * np.sin(t / 3.0) + 300 * np.sin(t / 0.7) + rng.normal(0, 5, n)
    return pd.Series(rpm, index=start + pd.to_timedelta(t, unit="s"))


def test_recovers_a_known_offset() -> None:
    """The whole point: the two clocks differ by seconds and it must be measured."""
    t0 = pd.Timestamp("2026-08-05T12:00:00Z")
    reference = _rpm_series(t0, 120, 20)
    # Candidate's clock runs 1.6 s behind, the larger of the two real offsets.
    candidate = pd.Series(reference.to_numpy(), index=reference.index - pd.Timedelta(seconds=1.6))
    alignment = align_clocks(reference, candidate)
    assert alignment.offset_seconds == pytest.approx(1.6, abs=0.11)
    assert alignment.r > 0.99


def test_refuses_unrelated_windows_instead_of_fitting_noise() -> None:
    """A confidently-wrong offset is worse than none — everything inherits it.

    This is the 2026-07-29 case: 314 s of *nominal* overlap containing zero
    simultaneous samples, because connecting the second logger took the bus
    away from the first.
    """
    t0 = pd.Timestamp("2026-08-05T12:00:00Z")
    reference = _rpm_series(t0, 120, 20, seed=1)
    unrelated = _rpm_series(t0, 120, 20, seed=99)
    unrelated = pd.Series(
        np.random.default_rng(7).normal(2000, 400, len(unrelated)), index=unrelated.index
    )
    with pytest.raises(AlignmentError, match="below"):
        align_clocks(reference, unrelated)


def test_refuses_when_the_windows_do_not_overlap_at_all() -> None:
    t0 = pd.Timestamp("2026-08-05T12:00:00Z")
    reference = _rpm_series(t0, 60, 20)
    later = _rpm_series(t0 + pd.Timedelta(hours=3), 60, 20)
    with pytest.raises(AlignmentError):
        align_clocks(reference, later)


def test_threshold_is_strict_enough_to_matter() -> None:
    """0.99 is not arbitrary: real overlaps score 0.9998+."""
    assert MIN_ALIGNMENT_R == 0.99
