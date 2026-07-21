"""Tests for the four-tier training-disposition classifier (Task 1.4)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from src.analysis.training_disposition import (
    MIN_BASELINE_SAMPLES,
    ChannelBaseline,
    GoldenBaselines,
    classify_dispositions,
    compute_golden_baselines,
    compute_quarantine_mask,
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


# ---------------------------------------------------------------------------
# Task 1.6 — channel-type-aware (discrete/bitmask) disposition screen
# ---------------------------------------------------------------------------


def _discrete_frame(n: int = 200, *, states: tuple[int, ...] = (0, 1, 2, 3)) -> pd.DataFrame:
    """An engine-state-byte-like channel: a handful of legitimate states,
    every value observed in this same window."""
    return pd.DataFrame(
        {
            "operating_mode": ["idle"] * n,
            "can:ENGINE_STATE": [states[i % len(states)] for i in range(n)],
        }
    )


def test_discrete_channel_every_observed_value_is_accepted_window_local() -> None:
    """Window-local discrete screening is self-referential by construction —
    every value in the window is, by definition, a member of its own
    observed set. This is exactly the Quarantine-flood fix (Task 1.6): a
    median/MAD band on a 4-state byte would collapse to a point and flag
    most of it; the value-set baseline doesn't."""
    frame = _discrete_frame()
    summary = classify_dispositions(frame, reject_counts={}, channels=["can:ENGINE_STATE"])
    group = summary.groups[0]
    assert group.is_discrete is True
    assert group.quarantine == 0
    assert group.accept == len(frame)
    assert group.baseline_values == (0, 1, 2, 3)


def test_continuous_channel_is_not_misclassified_discrete() -> None:
    """A genuinely continuous channel (many distinct float values) must stay
    on the median/MAD path — Task 1.6 must not swallow the continuous
    residue the review explicitly wants preserved."""
    frame = _baseline_frame(outlier_count=5)
    summary = classify_dispositions(frame, reject_counts={}, channels=["obd:SHORT_FUEL_TRIM_1"])
    group = summary.groups[0]
    assert group.is_discrete is False
    assert group.quarantine == 5  # unchanged continuous-path behavior


def test_wide_integer_state_channel_with_low_cardinality_fraction_is_discrete() -> None:
    """Above the absolute cap, integer-valued + low unique-fraction is still
    discrete (e.g. a gear number sampled for hours) — not a hand-listed
    channel, purely from the data's own shape."""
    n = 2000
    frame = pd.DataFrame(
        {
            "operating_mode": ["highway"] * n,
            "obd:GEAR": [(i % 20) for i in range(n)],  # 20 states, 1% of 2000 rows
        }
    )
    summary = classify_dispositions(frame, reject_counts={}, channels=["obd:GEAR"])
    assert summary.groups[0].is_discrete is True


def test_discrete_channel_screened_via_quarantine_mask() -> None:
    frame = _discrete_frame()
    mask = compute_quarantine_mask(frame, channels=["can:ENGINE_STATE"])
    assert not mask["can:ENGINE_STATE"].any()  # every value is in its own observed set


# ---------------------------------------------------------------------------
# Task 3.7 — golden-anchored baselines
# ---------------------------------------------------------------------------


def test_compute_golden_baselines_freezes_continuous_and_discrete_groups() -> None:
    continuous = _baseline_frame(outlier_count=0)
    discrete = _discrete_frame()
    frame = pd.concat([continuous.drop(columns=["session"]), discrete], axis=0, ignore_index=True)
    baselines = compute_golden_baselines(
        frame, channels=["obd:SHORT_FUEL_TRIM_1", "can:ENGINE_STATE"]
    )
    fuel_trim = baselines.get("obd:SHORT_FUEL_TRIM_1", "idle")
    assert fuel_trim is not None
    assert fuel_trim.is_discrete is False
    assert fuel_trim.median is not None

    engine_state = baselines.get("can:ENGINE_STATE", "idle")
    assert engine_state is not None
    assert engine_state.is_discrete is True
    assert engine_state.baseline_values == (0, 1, 2, 3)


def test_golden_anchored_screen_catches_drift_a_window_local_screen_misses() -> None:
    """The central self-referential-baseline blind spot the review
    identified: a slow drift dominating a window redefines the window-local
    median as "normal". Anchoring to a frozen golden baseline instead still
    catches it."""
    rng_values = [0.0] * 150  # golden: tightly clustered at 0
    golden_frame = pd.DataFrame(
        {"operating_mode": ["idle"] * 150, "obd:SHORT_FUEL_TRIM_1": rng_values}
    )
    golden_baselines = compute_golden_baselines(
        golden_frame, channels=["obd:SHORT_FUEL_TRIM_1"], min_baseline_samples=20
    )

    # A later candidate window has drifted almost entirely to 20 — window-
    # local statistics would call 20 the new median and flag nothing.
    drifted = pd.DataFrame(
        {
            "operating_mode": ["idle"] * 150,
            "obd:SHORT_FUEL_TRIM_1": [20.0] * 148 + [0.0, 0.0],
        }
    )

    window_local = classify_dispositions(
        drifted, reject_counts={}, channels=["obd:SHORT_FUEL_TRIM_1"]
    )
    assert window_local.groups[0].quarantine == 0  # the blind spot, unfixed

    golden_anchored = classify_dispositions(
        drifted,
        reject_counts={},
        channels=["obd:SHORT_FUEL_TRIM_1"],
        golden_baselines=golden_baselines,
    )
    assert golden_anchored.groups[0].quarantine == 148  # anchored to golden's frozen median


def test_golden_baselines_missing_channel_falls_back_to_window_local() -> None:
    """A channel golden never covered (e.g. added to the corpus later) must
    fall back to window-local screening, not silently pass everything."""
    empty_golden = GoldenBaselines(baselines=())
    frame = _baseline_frame(outlier_count=5)
    summary = classify_dispositions(
        frame,
        reject_counts={},
        channels=["obd:SHORT_FUEL_TRIM_1"],
        golden_baselines=empty_golden,
    )
    assert summary.groups[0].quarantine == 5  # same as no golden_baselines at all


def test_golden_baselines_round_trip_to_dict() -> None:
    baselines = GoldenBaselines(
        baselines=(
            ChannelBaseline(
                channel="obd:RPM",
                operating_mode="idle",
                is_discrete=False,
                median=900.0,
                scaled_mad=12.0,
            ),
            ChannelBaseline(
                channel="can:ENGINE_STATE",
                operating_mode="idle",
                is_discrete=True,
                baseline_values=(0, 1, 2, 3),
            ),
        )
    )
    restored = GoldenBaselines.from_dict(baselines.to_dict())
    assert restored.get("obd:RPM", "idle") == baselines.get("obd:RPM", "idle")
    assert restored.get("can:ENGINE_STATE", "idle") == baselines.get("can:ENGINE_STATE", "idle")


# ---------------------------------------------------------------------------
# Task 3.6 — compute_quarantine_mask (cell-level companion to classify_dispositions)
# ---------------------------------------------------------------------------


def test_quarantine_mask_flags_exactly_the_quarantine_cells() -> None:
    frame = _baseline_frame(outlier_count=5)
    mask = compute_quarantine_mask(frame, channels=["obd:SHORT_FUEL_TRIM_1"])
    assert mask["obd:SHORT_FUEL_TRIM_1"].sum() == 5
    # The flagged rows are exactly the first 5 (see _baseline_frame).
    assert list(mask.index[mask["obd:SHORT_FUEL_TRIM_1"]]) == [0, 1, 2, 3, 4]


def test_quarantine_mask_respects_active_override() -> None:
    frame = _baseline_frame(outlier_count=5)
    overrides = AcceptedBaselines(schema_version=1, overrides=[_override()])
    mask = compute_quarantine_mask(frame, channels=["obd:SHORT_FUEL_TRIM_1"], overrides=overrides)
    assert not mask["obd:SHORT_FUEL_TRIM_1"].any()  # covered -> Accept-recenter, not Quarantine
