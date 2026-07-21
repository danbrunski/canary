"""Tests for the four-tier training-disposition classifier (Task 1.4)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from src.analysis.training_disposition import (
    MIN_BASELINE_SAMPLES,
    classify_dispositions,
)
from src.decode.accepted_baselines import (
    AcceptedBaselineOverride,
    AcceptedBaselines,
    DirectionalBand,
    Provenance,
)


def _override(
    channel: str = "obd:SHORT_FUEL_TRIM_1", operating_mode: str | None = "idle"
) -> AcceptedBaselineOverride:
    return AcceptedBaselineOverride(
        channel=channel,
        operating_mode=operating_mode,
        accepted_center=12.0,
        band=DirectionalBand(
            tolerance=3.0, worse_direction="positive", worse_warning=3.0, worse_critical=6.0
        ),
        provenance=Provenance(
            declared_by="dan",
            declared_at=date(2026, 5, 17),
            review_by=date(2026, 11, 17),
            reason="test",
        ),
    )


def _baseline_frame(n: int = 200, *, outlier_count: int = 5) -> pd.DataFrame:
    """A well-behaved channel clustered around 0 with a handful of clear
    outliers, all in one operating mode."""
    rng = np.random.default_rng(0)
    values = rng.normal(loc=0.0, scale=1.0, size=n).tolist()
    for i in range(outlier_count):
        values[i] = 500.0 + i  # unambiguously outside any reasonable band
    return pd.DataFrame(
        {
            "session": ["s0"] * n,
            "operating_mode": ["idle"] * n,
            "obd:SHORT_FUEL_TRIM_1": values,
        }
    )


# ---------------------------------------------------------------------------
# Reject tier — pass-through from the quality gate, per channel only
# ---------------------------------------------------------------------------


def test_reject_counts_pass_through_from_quality_gate() -> None:
    frame = _baseline_frame()
    summary = classify_dispositions(frame, reject_counts={"obd:RPM": 7})
    assert summary.reject_counts == {"obd:RPM": 7}
    assert summary.totals["reject"] == 7


def test_no_reject_counts_defaults_to_empty() -> None:
    frame = _baseline_frame()
    summary = classify_dispositions(frame, reject_counts={})
    assert summary.reject_counts == {}
    assert summary.totals["reject"] == 0


# ---------------------------------------------------------------------------
# Accept tier
# ---------------------------------------------------------------------------


def test_within_baseline_values_are_accepted() -> None:
    frame = _baseline_frame(outlier_count=0)
    summary = classify_dispositions(frame, reject_counts={}, channels=["obd:SHORT_FUEL_TRIM_1"])
    group = summary.groups[0]
    assert group.quarantine == 0
    assert group.accept_recenter == 0
    assert group.accept == len(frame)


# ---------------------------------------------------------------------------
# Quarantine tier — outside baseline, no override
# ---------------------------------------------------------------------------


def test_outlier_without_override_is_quarantined() -> None:
    frame = _baseline_frame(outlier_count=5)
    summary = classify_dispositions(frame, reject_counts={}, channels=["obd:SHORT_FUEL_TRIM_1"])
    group = summary.groups[0]
    assert group.quarantine == 5
    assert group.accept_recenter == 0
    assert group.accept == len(frame) - 5
    assert summary.totals["quarantine"] == 5


# ---------------------------------------------------------------------------
# Accept-recenter tier — outside baseline, covered by an active override
# ---------------------------------------------------------------------------


def test_outlier_with_active_override_is_accept_recentered() -> None:
    frame = _baseline_frame(outlier_count=5)
    overrides = AcceptedBaselines(schema_version=1, overrides=[_override()])
    summary = classify_dispositions(
        frame, reject_counts={}, channels=["obd:SHORT_FUEL_TRIM_1"], overrides=overrides
    )
    group = summary.groups[0]
    assert group.accept_recenter == 5
    assert group.quarantine == 0


def test_outlier_with_mode_scoped_override_only_covers_that_mode() -> None:
    idle = _baseline_frame(outlier_count=5)
    # A second, large-enough group in a mode the override doesn't scope to,
    # with its own outliers — must be quarantined, not covered.
    highway = _baseline_frame(outlier_count=5)
    highway["operating_mode"] = "highway"
    frame = pd.concat([idle, highway], ignore_index=True)

    overrides = AcceptedBaselines(schema_version=1, overrides=[_override(operating_mode="idle")])
    summary = classify_dispositions(
        frame, reject_counts={}, channels=["obd:SHORT_FUEL_TRIM_1"], overrides=overrides
    )
    by_mode = {g.operating_mode: g for g in summary.groups}
    assert by_mode["idle"].accept_recenter > 0
    assert by_mode["highway"].quarantine >= 1  # not covered — different mode
    assert by_mode["highway"].accept_recenter == 0


def test_expired_override_does_not_cover() -> None:
    frame = _baseline_frame(outlier_count=5)
    expired = AcceptedBaselineOverride(
        channel="obd:SHORT_FUEL_TRIM_1",
        operating_mode="idle",
        accepted_center=12.0,
        band=DirectionalBand(
            tolerance=3.0, worse_direction="positive", worse_warning=3.0, worse_critical=6.0
        ),
        provenance=Provenance(
            declared_by="dan",
            declared_at=date(2020, 1, 1),
            review_by=date(2020, 6, 1),  # long expired relative to date.today()
            reason="test",
        ),
    )
    overrides = AcceptedBaselines(schema_version=1, overrides=[expired])
    summary = classify_dispositions(
        frame, reject_counts={}, channels=["obd:SHORT_FUEL_TRIM_1"], overrides=overrides
    )
    group = summary.groups[0]
    assert group.quarantine == 5
    assert group.accept_recenter == 0


# ---------------------------------------------------------------------------
# Sparse groups — not enough samples to trust a baseline
# ---------------------------------------------------------------------------


def test_sparse_group_below_min_samples_is_all_accept() -> None:
    n = MIN_BASELINE_SAMPLES - 1
    frame = pd.DataFrame(
        {
            "operating_mode": ["idle"] * n,
            "obd:SHORT_FUEL_TRIM_1": [1000.0] * n,  # would be an obvious outlier if scored
        }
    )
    summary = classify_dispositions(frame, reject_counts={}, channels=["obd:SHORT_FUEL_TRIM_1"])
    group = summary.groups[0]
    assert group.quarantine == 0
    assert group.accept == n
    assert group.baseline_low is None
    assert group.baseline_high is None


# ---------------------------------------------------------------------------
# Regime-gate-then-disposition ordering — mode grouping is respected
# ---------------------------------------------------------------------------


def test_disposition_is_scoped_per_operating_mode() -> None:
    """A value that's an outlier in `idle` but ordinary in `highway` must be
    screened against ITS OWN mode's baseline, not a pooled one — that's what
    "regime gate runs before disposition" means in practice."""
    rng = np.random.default_rng(1)
    idle_values = rng.normal(loc=0.0, scale=1.0, size=100).tolist()
    highway_values = rng.normal(loc=50.0, scale=1.0, size=100).tolist()
    frame = pd.DataFrame(
        {
            "operating_mode": ["idle"] * 100 + ["highway"] * 100,
            "obd:RPM": idle_values + highway_values,
        }
    )
    summary = classify_dispositions(frame, reject_counts={}, channels=["obd:RPM"])
    by_mode = {g.operating_mode: g for g in summary.groups}
    # Neither mode's own values are outliers relative to its own baseline.
    assert by_mode["idle"].quarantine == 0
    assert by_mode["highway"].quarantine == 0


def test_classify_dispositions_without_mode_column_is_one_ungrouped_group() -> None:
    frame = pd.DataFrame({"obd:RPM": list(range(50))})
    summary = classify_dispositions(frame, reject_counts={}, channels=["obd:RPM"])
    assert len(summary.groups) == 1
    assert summary.groups[0].operating_mode is None
