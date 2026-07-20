"""Tests for the synthetic anomaly injector."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from src.preprocessing.anomaly_injection import (
    AnomalyInjector,
    InjectionWindow,
    windows_to_records,
)
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN


def _clean_frame(n: int = 60, channels: dict[str, list[float]] | None = None) -> pd.DataFrame:
    """A clean session frame at 1Hz starting at 2026-05-11T15:00:00Z."""
    timestamps = pd.date_range(start="2026-05-11T15:00:00Z", periods=n, freq="1s")
    data: dict[str, object] = {
        SESSION_COLUMN: ["clean"] * n,
        TIMESTAMP_COLUMN: timestamps,
    }
    if channels is None:
        channels = {"obd:RPM": [800.0] * n, "obd:COOLANT": [85.0] * n}
    for name, vals in channels.items():
        assert len(vals) == n
        data[name] = vals
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# Constructor & basic invariants
# ---------------------------------------------------------------------------


def test_constructor_requires_session_and_timestamp_columns():
    bad = pd.DataFrame({"x": [1.0, 2.0]})
    with pytest.raises(KeyError, match="missing required columns"):
        AnomalyInjector(bad)


def test_original_dataframe_is_not_mutated():
    df = _clean_frame(10)
    original = df.copy()
    inj = AnomalyInjector(df)
    inj.spike(
        channel="obd:RPM",
        start="2026-05-11T15:00:03Z",
        end="2026-05-11T15:00:05Z",
        value=4000.0,
    )
    pd.testing.assert_frame_equal(df, original)


def test_unknown_channel_raises():
    inj = AnomalyInjector(_clean_frame(5))
    with pytest.raises(KeyError, match="channel"):
        inj.spike(
            channel="obd:DOES_NOT_EXIST",
            start="2026-05-11T15:00:00Z",
            end="2026-05-11T15:00:01Z",
            value=100.0,
        )


def test_window_end_before_start_raises():
    inj = AnomalyInjector(_clean_frame(5))
    with pytest.raises(ValueError, match="precedes start"):
        inj.spike(
            channel="obd:RPM",
            start="2026-05-11T15:00:05Z",
            end="2026-05-11T15:00:01Z",
            value=4000.0,
        )


def test_naive_timestamps_assumed_utc():
    """String/naive timestamps without tz info should land on UTC."""
    inj = AnomalyInjector(_clean_frame(5))
    inj.spike(
        channel="obd:RPM",
        start="2026-05-11 15:00:02",  # no tz
        end="2026-05-11 15:00:04",
        value=4000.0,
    )
    assert inj.windows[0].start.tzname() == "UTC"


# ---------------------------------------------------------------------------
# spike
# ---------------------------------------------------------------------------


def test_spike_replaces_values_in_window():
    inj = AnomalyInjector(_clean_frame(10))
    inj.spike(
        channel="obd:RPM",
        start="2026-05-11T15:00:03Z",
        end="2026-05-11T15:00:06Z",  # rows 3, 4, 5 inclusive (end is exclusive)
        value=4000.0,
    )
    rpm = inj.df["obd:RPM"]
    assert rpm.iloc[2] == 800.0  # before spike
    assert rpm.iloc[3] == 4000.0  # inside spike
    assert rpm.iloc[4] == 4000.0
    assert rpm.iloc[5] == 4000.0
    assert rpm.iloc[6] == 800.0  # after spike (end is exclusive)


def test_spike_records_window():
    inj = AnomalyInjector(_clean_frame(10))
    inj.spike(
        channel="obd:RPM",
        start="2026-05-11T15:00:03Z",
        end="2026-05-11T15:00:06Z",
        value=4000.0,
    )
    w = inj.windows[0]
    assert w.kind == "spike"
    assert w.channel == "obd:RPM"
    assert w.parameter == 4000.0


def test_spike_only_affects_target_channel():
    inj = AnomalyInjector(_clean_frame(10))
    inj.spike(
        channel="obd:RPM",
        start="2026-05-11T15:00:03Z",
        end="2026-05-11T15:00:05Z",
        value=4000.0,
    )
    # COOLANT is untouched
    assert (inj.df["obd:COOLANT"] == 85.0).all()


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------


def test_drift_ramps_linearly():
    """Drift should add 0 at start, total_delta at end, linearly between."""
    df = _clean_frame(11, channels={"obd:COOLANT": [85.0] * 11, "obd:RPM": [800.0] * 11})
    inj = AnomalyInjector(df)
    # 10-second window, 0→+20 ramp
    inj.drift(
        channel="obd:COOLANT",
        start="2026-05-11T15:00:00Z",
        end="2026-05-11T15:00:10Z",
        total_delta=20.0,
    )
    coolant = inj.df["obd:COOLANT"]
    # 11 rows at 1s spacing. row 0 at t=0 fraction=0; row 5 at t=5
    # fraction=0.5; row 10 at t=10 is outside the window (end is exclusive).
    assert coolant.iloc[0] == pytest.approx(85.0)  # +0
    assert coolant.iloc[5] == pytest.approx(95.0)  # +10
    assert coolant.iloc[9] == pytest.approx(85.0 + 18.0)  # +18 (fraction 0.9)
    # Row 10 is at t=10s, end is t=10s, end is exclusive → no drift applied
    assert coolant.iloc[10] == pytest.approx(85.0)


def test_drift_outside_window_unchanged():
    df = _clean_frame(20, channels={"obd:COOLANT": [85.0] * 20})
    inj = AnomalyInjector(df)
    inj.drift(
        channel="obd:COOLANT",
        start="2026-05-11T15:00:05Z",
        end="2026-05-11T15:00:10Z",
        total_delta=10.0,
    )
    coolant = inj.df["obd:COOLANT"]
    # Before window: unchanged
    assert (coolant.iloc[:5] == 85.0).all()
    # After window: returns to baseline (drift not persistent)
    assert (coolant.iloc[10:] == 85.0).all()


def test_drift_records_total_delta_in_window():
    inj = AnomalyInjector(_clean_frame(10))
    inj.drift(
        channel="obd:RPM",
        start="2026-05-11T15:00:01Z",
        end="2026-05-11T15:00:05Z",
        total_delta=500.0,
    )
    assert inj.windows[0].parameter == 500.0


def test_drift_with_empty_window_still_records():
    """Window outside the frame's timestamps should still register so labels
    can reflect the intent."""
    df = _clean_frame(5)
    inj = AnomalyInjector(df)
    inj.drift(
        channel="obd:RPM",
        start="2027-01-01T00:00:00Z",  # way after the frame
        end="2027-01-01T00:01:00Z",
        total_delta=100.0,
    )
    assert len(inj.windows) == 1
    # No actual values changed
    assert (inj.df["obd:RPM"] == 800.0).all()


# ---------------------------------------------------------------------------
# dropout
# ---------------------------------------------------------------------------


def test_dropout_holds_last_value_then_nans_past_the_ceiling():
    """8.1: production never sees a raw NaN the instant a channel stops
    reporting — resample_wide forward-fills up to a ceiling first. A rising
    RPM channel proves this is a genuine *hold*, not a coincidence (COOLANT's
    old test used a flat channel, so a bug that just left the value alone
    entirely would have passed it too)."""
    rpm_values = [700.0 + 10 * i for i in range(10)]
    inj = AnomalyInjector(_clean_frame(10, channels={"obd:RPM": rpm_values}))
    inj.dropout(
        channel="obd:RPM",
        start="2026-05-11T15:00:03Z",
        end="2026-05-11T15:00:07Z",  # rows 3,4,5,6
        max_gap_seconds=2.0,
    )
    rpm = inj.df["obd:RPM"]
    gap = inj.df["obd:RPM__gap"]
    # Pre-window: unaffected, no gap flag.
    assert rpm.iloc[2] == 720.0
    assert not gap.iloc[2]
    # Within the ceiling (age 0, 1 second < 2s): held at row 2's value (720),
    # NOT the channel's real (would-be) value at that row — a plausible but
    # stale reading, the insidious part of a real dropout. The gap flag is
    # NOT yet set here: production flips value and flag together at the same
    # threshold (resampler._gap_flags's `ages > max_gap` drives both), so a
    # detector reading the mask sees nothing unusual yet either.
    assert rpm.iloc[3] == 720.0
    assert rpm.iloc[4] == 720.0
    assert not gap.iloc[3]
    assert not gap.iloc[4]
    # Past the ceiling (age 2, 3 seconds >= 2s): NaN AND the gap flag flip
    # together, same as resample_wide's own bounded-ffill cutoff.
    assert np.isnan(rpm.iloc[5])
    assert np.isnan(rpm.iloc[6])
    assert gap.iloc[5]
    assert gap.iloc[6]
    # After the window: untouched, gap flag stays False.
    assert rpm.iloc[7] == 770.0
    assert not gap.iloc[7]


def test_dropout_at_session_start_has_nothing_to_hold():
    """No pre-window observation exists → there was never a fresh value for
    the resampler to have been forward-filling, so the window goes NaN AND
    gap-flagged immediately (matching resampler._gap_flags: an all-empty
    observation history is stale everywhere, not just past the ceiling) —
    the large max_gap_seconds below would otherwise keep the gap flag False
    for a while, proving this is genuinely the no-pre-observation branch."""
    inj = AnomalyInjector(_clean_frame(5))
    inj.dropout(
        channel="obd:RPM",
        start="2026-05-11T15:00:00Z",
        end="2026-05-11T15:00:02Z",
        max_gap_seconds=10.0,
    )
    rpm = inj.df["obd:RPM"]
    gap = inj.df["obd:RPM__gap"]
    assert np.isnan(rpm.iloc[0])
    assert np.isnan(rpm.iloc[1])
    assert gap.iloc[0]
    assert gap.iloc[1]


def test_dropout_rejects_non_positive_max_gap_seconds():
    inj = AnomalyInjector(_clean_frame(5))
    with pytest.raises(ValueError, match="max_gap_seconds must be positive"):
        inj.dropout(
            channel="obd:RPM",
            start="2026-05-11T15:00:01Z",
            end="2026-05-11T15:00:03Z",
            max_gap_seconds=0.0,
        )


def test_dropout_records_window_kind():
    inj = AnomalyInjector(_clean_frame(5))
    inj.dropout(
        channel="obd:RPM",
        start="2026-05-11T15:00:01Z",
        end="2026-05-11T15:00:03Z",
        max_gap_seconds=10.0,
    )
    assert inj.windows[0].kind == "dropout"
    assert inj.windows[0].parameter is None


# ---------------------------------------------------------------------------
# stuck
# ---------------------------------------------------------------------------


def test_stuck_freezes_value_from_before_window():
    rpm_values = [800.0, 820.0, 840.0, 860.0, 880.0, 900.0, 920.0, 940.0, 960.0, 980.0]
    df = _clean_frame(10, channels={"obd:RPM": rpm_values})
    inj = AnomalyInjector(df)
    inj.stuck(
        channel="obd:RPM",
        start="2026-05-11T15:00:05Z",
        end="2026-05-11T15:00:09Z",  # rows 5..8 stuck
    )
    rpm = inj.df["obd:RPM"]
    # Frozen at row 4's value (880.0), because row 4 was the last pre-window row
    assert rpm.iloc[5] == 880.0
    assert rpm.iloc[6] == 880.0
    assert rpm.iloc[7] == 880.0
    assert rpm.iloc[8] == 880.0
    # After window: back to original (row 9 = 980)
    assert rpm.iloc[9] == 980.0
    # Records the frozen value as the parameter
    assert inj.windows[0].parameter == 880.0


def test_stuck_at_session_start_uses_first_value():
    df = _clean_frame(
        5,
        channels={"obd:RPM": [700.0, 720.0, 740.0, 760.0, 780.0]},
    )
    inj = AnomalyInjector(df)
    inj.stuck(
        channel="obd:RPM",
        start="2026-05-11T15:00:00Z",
        end="2026-05-11T15:00:03Z",
    )
    rpm = inj.df["obd:RPM"]
    # No pre-window data → use first available value (700.0)
    assert rpm.iloc[0] == 700.0
    assert rpm.iloc[1] == 700.0
    assert rpm.iloc[2] == 700.0
    assert rpm.iloc[3] == 760.0  # after window


def test_stuck_raises_when_all_values_are_nan():
    df = _clean_frame(5, channels={"obd:RPM": [np.nan] * 5})
    inj = AnomalyInjector(df)
    with pytest.raises(ValueError, match="no non-NaN"):
        inj.stuck(
            channel="obd:RPM",
            start="2026-05-11T15:00:01Z",
            end="2026-05-11T15:00:03Z",
        )


# ---------------------------------------------------------------------------
# intermittent (6.5)
# ---------------------------------------------------------------------------


def test_intermittent_alternates_on_off_within_window():
    """2s on / 1s off: the anomalous value holds for two ticks, then the
    true reading returns for one, repeating for the whole window."""
    inj = AnomalyInjector(_clean_frame(12))
    inj.intermittent(
        channel="obd:RPM",
        start="2026-05-11T15:00:02Z",
        end="2026-05-11T15:00:11Z",  # rows 2..10
        value=4000.0,
        on_seconds=2.0,
        off_seconds=1.0,
    )
    rpm = inj.df["obd:RPM"]
    # Period is on,on,off starting at row 2: rows 2,3 on; row 4 off;
    # rows 5,6 on; row 7 off; rows 8,9 on; row 10 off.
    assert rpm.iloc[1] == 800.0  # before window
    assert list(rpm.iloc[2:11]) == [
        4000.0,
        4000.0,
        800.0,
        4000.0,
        4000.0,
        800.0,
        4000.0,
        4000.0,
        800.0,
    ]
    assert rpm.iloc[11] == 800.0  # after window (end exclusive)


def test_intermittent_never_holds_three_consecutive_on_ticks():
    """The whole point (6.5): a strict 3-consecutive persistence gate can
    never latch onto this pattern because no on-burst is 3 ticks long."""
    inj = AnomalyInjector(_clean_frame(20))
    inj.intermittent(
        channel="obd:RPM",
        start="2026-05-11T15:00:00Z",
        end="2026-05-11T15:00:20Z",
        value=4000.0,
        on_seconds=2.0,
        off_seconds=1.0,
    )
    on_mask = (inj.df["obd:RPM"] == 4000.0).to_numpy()
    max_run = 0
    run = 0
    for on in on_mask:
        run = run + 1 if on else 0
        max_run = max(max_run, run)
    assert max_run == 2


def test_intermittent_records_window_kind_and_value():
    inj = AnomalyInjector(_clean_frame(10))
    inj.intermittent(
        channel="obd:RPM",
        start="2026-05-11T15:00:01Z",
        end="2026-05-11T15:00:07Z",
        value=4000.0,
        on_seconds=2.0,
        off_seconds=1.0,
    )
    w = inj.windows[0]
    assert w.kind == "intermittent"
    assert w.channel == "obd:RPM"
    assert w.parameter == 4000.0


def test_intermittent_only_affects_target_channel():
    inj = AnomalyInjector(_clean_frame(10))
    inj.intermittent(
        channel="obd:RPM",
        start="2026-05-11T15:00:01Z",
        end="2026-05-11T15:00:07Z",
        value=4000.0,
        on_seconds=2.0,
        off_seconds=1.0,
    )
    assert (inj.df["obd:COOLANT"] == 85.0).all()


def test_intermittent_rejects_non_positive_on_off_seconds():
    inj = AnomalyInjector(_clean_frame(5))
    with pytest.raises(ValueError, match="on_seconds and off_seconds must be positive"):
        inj.intermittent(
            channel="obd:RPM",
            start="2026-05-11T15:00:00Z",
            end="2026-05-11T15:00:03Z",
            value=4000.0,
            on_seconds=0.0,
            off_seconds=1.0,
        )


# ---------------------------------------------------------------------------
# labels
# ---------------------------------------------------------------------------


def test_labels_marks_only_injected_rows():
    inj = AnomalyInjector(_clean_frame(20))
    inj.spike(
        channel="obd:RPM",
        start="2026-05-11T15:00:05Z",
        end="2026-05-11T15:00:08Z",  # rows 5,6,7
        value=4000.0,
    )
    labels = inj.labels()
    assert not labels.iloc[4]
    assert labels.iloc[5]
    assert labels.iloc[6]
    assert labels.iloc[7]
    assert not labels.iloc[8]


def test_labels_unions_across_multiple_windows():
    inj = AnomalyInjector(_clean_frame(20))
    inj.spike(
        channel="obd:RPM",
        start="2026-05-11T15:00:03Z",
        end="2026-05-11T15:00:05Z",
        value=4000.0,
    )
    inj.dropout(
        channel="obd:COOLANT",
        start="2026-05-11T15:00:10Z",
        end="2026-05-11T15:00:12Z",
        max_gap_seconds=10.0,
    )
    labels = inj.labels()
    assert labels.iloc[3]
    assert labels.iloc[4]
    assert not labels.iloc[5]
    assert not labels.iloc[9]
    assert labels.iloc[10]
    assert labels.iloc[11]
    assert not labels.iloc[12]


def test_labels_overlapping_windows_collapse_to_or():
    inj = AnomalyInjector(_clean_frame(10))
    inj.spike(
        channel="obd:RPM",
        start="2026-05-11T15:00:02Z",
        end="2026-05-11T15:00:06Z",
        value=4000.0,
    )
    inj.dropout(
        channel="obd:COOLANT",
        start="2026-05-11T15:00:04Z",
        end="2026-05-11T15:00:08Z",
        max_gap_seconds=10.0,
    )
    labels = inj.labels()
    # Union: rows 2..7 are all anomalous (some on RPM, some on COOLANT, overlap on 4-5)
    assert not labels.iloc[1]
    assert labels.iloc[2:8].all()
    assert not labels.iloc[8]


def test_labels_empty_when_no_injections():
    inj = AnomalyInjector(_clean_frame(10))
    labels = inj.labels()
    assert len(labels) == 10
    assert not labels.any()


# ---------------------------------------------------------------------------
# Chaining
# ---------------------------------------------------------------------------


def test_chaining_returns_self_and_accumulates_windows():
    inj = AnomalyInjector(_clean_frame(20))
    result = (
        inj.spike(
            channel="obd:RPM",
            start="2026-05-11T15:00:02Z",
            end="2026-05-11T15:00:04Z",
            value=4000.0,
        )
        .drift(
            channel="obd:COOLANT",
            start="2026-05-11T15:00:06Z",
            end="2026-05-11T15:00:12Z",
            total_delta=20.0,
        )
        .dropout(
            channel="obd:COOLANT",
            start="2026-05-11T15:00:14Z",
            end="2026-05-11T15:00:16Z",
            max_gap_seconds=10.0,
        )
    )
    assert result is inj
    assert [w.kind for w in inj.windows] == ["spike", "drift", "dropout"]


# ---------------------------------------------------------------------------
# windows_to_records (serialization helper)
# ---------------------------------------------------------------------------


def test_windows_to_records_roundtrips_through_json():
    inj = AnomalyInjector(_clean_frame(10))
    inj.spike(
        channel="obd:RPM",
        start="2026-05-11T15:00:02Z",
        end="2026-05-11T15:00:04Z",
        value=4000.0,
    )
    records = windows_to_records(inj.windows)
    assert len(records) == 1
    blob = json.dumps(records)
    loaded = json.loads(blob)
    assert loaded[0]["kind"] == "spike"
    assert loaded[0]["channel"] == "obd:RPM"
    assert loaded[0]["parameter"] == 4000.0


# ---------------------------------------------------------------------------
# Frozen-dataclass invariants
# ---------------------------------------------------------------------------


def test_injection_window_is_immutable():
    w = InjectionWindow(
        channel="x",
        kind="spike",
        start=pd.Timestamp("2026-05-11T15:00:00Z"),
        end=pd.Timestamp("2026-05-11T15:00:01Z"),
        parameter=42.0,
    )
    with pytest.raises((AttributeError, Exception)):
        w.channel = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Integration with normalization pipeline
# ---------------------------------------------------------------------------


def test_injected_spike_amplifies_under_z_score_scaling():
    """The scaler should turn an injected spike into a high-magnitude z-score —
    that's what makes the IsolationForest fire."""
    from src.preprocessing.normalization import ZScoreScaler

    # Clean training data on a tight idle distribution
    train = _clean_frame(
        100,
        channels={"obd:RPM": [800.0 + (i % 5) * 2.0 for i in range(100)]},
    )
    scaler = ZScoreScaler.fit(train, columns=["obd:RPM"])

    # Inject a spike into a copy
    inj = AnomalyInjector(train)
    inj.spike(
        channel="obd:RPM",
        start="2026-05-11T15:00:50Z",
        end="2026-05-11T15:00:55Z",
        value=4000.0,
    )
    scaled = scaler.transform(inj.df)
    labels = inj.labels()

    # Z-scores at spike rows should be massive; non-anomalous rows should be ~0
    anomalous_z = scaled.loc[labels, "obd:RPM"].abs()
    clean_z = scaled.loc[~labels, "obd:RPM"].abs()
    assert anomalous_z.min() > 100.0  # spike is way out of distribution
    assert clean_z.max() < 5.0  # clean data stays bounded
