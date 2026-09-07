"""The capture-lifetime ledger (roadmap C03.2 / C03.4 / C03.11).

Every case here is a shape the trailing-window statistics could not see:
a late start, a stall older than the 60 s window, a missing tail, a pause
that must be excluded by name and an engine-off gap that must not be.
"""

from __future__ import annotations

import pytest

from src.collection import capture_lifetime
from src.collection.capture_lifetime import (
    LONG_GAP_CAPACITY,
    RESERVOIR_CAPACITY,
    CaptureLedger,
    ChannelAccumulator,
    InterarrivalReservoir,
    subtract_windows,
)


@pytest.fixture(autouse=True)
def _fresh_ledger():
    capture_lifetime._reset_for_tests()
    yield
    capture_lifetime._reset_for_tests()


def _feed(ledger: CaptureLedger, channel: str, start: float, stop: float, period: float) -> None:
    t = start
    while t <= stop + 1e-9:
        ledger.observe(channel, 1000.0 + t)
        t += period


def test_samples_only_in_the_last_minute_of_five_are_a_240s_missing_start() -> None:
    """The C03 counterexample, at the ledger: 240 s missing, not 0.25 s max gap."""
    ledger = CaptureLedger()
    ledger.begin(1000.0, profile="fuel-map", generation=3)
    _feed(ledger, "RPM", 240.0, 300.0, 0.25)
    snap = ledger.snapshot(1300.0)
    assert snap is not None
    rpm = snap.channels["RPM"]
    assert rpm.missing_initial_s == pytest.approx(240.0)
    assert rpm.missing_final_s == pytest.approx(0.0)
    assert rpm.max_gap_s == pytest.approx(240.0)
    assert rpm.max_interarrival_s == pytest.approx(0.25)
    assert rpm.median_interarrival_s == pytest.approx(0.25)
    assert rpm.cadence_hz == pytest.approx(4.0)
    assert snap.elapsed_s == pytest.approx(300.0)


def test_a_mid_capture_stall_older_than_the_trailing_window_is_kept() -> None:
    ledger = CaptureLedger()
    ledger.begin(1000.0, profile="p", generation=1)
    _feed(ledger, "A", 0.0, 60.0, 0.5)
    _feed(ledger, "A", 150.0, 400.0, 0.5)  # 90 s stall, ended 250 s before now
    snap = ledger.snapshot(1400.0)
    assert snap is not None
    row = snap.channels["A"]
    assert row.max_interarrival_s == pytest.approx(90.0)
    assert row.long_gaps == ((60.0, 150.0),)
    assert row.long_gap_total_s == pytest.approx(90.0)
    # Cadence is the interarrival distribution, not observations / elapsed.
    assert row.cadence_hz == pytest.approx(2.0)


def test_a_missing_tail_is_the_final_interval_not_a_slow_rate() -> None:
    ledger = CaptureLedger()
    ledger.begin(1000.0, profile="p", generation=1)
    _feed(ledger, "A", 0.0, 100.0, 0.1)
    row = ledger.snapshot(1300.0).channels["A"]  # type: ignore[union-attr]
    assert row.missing_final_s == pytest.approx(200.0)
    assert row.missing_initial_s == pytest.approx(0.0)
    assert row.cadence_hz == pytest.approx(10.0)


def test_pause_windows_are_recorded_with_their_reasons_on_the_capture_clock() -> None:
    ledger = CaptureLedger()
    ledger.begin(1000.0, profile="p", generation=1)
    ledger.mark_paused(True, reason="operator", now=1010.0)
    ledger.mark_paused(True, reason="passive", now=1012.0)
    ledger.mark_paused(False, reason="operator", now=1015.0)
    # Still paused: passive is in force until it clears.
    open_snap = ledger.snapshot(1018.0)
    assert open_snap is not None
    assert open_snap.paused_s == pytest.approx(8.0)
    ledger.mark_paused(False, reason="passive", now=1020.0)
    snap = ledger.snapshot(1030.0)
    assert snap is not None
    assert [(w.start_s, w.end_s, w.reasons) for w in snap.pause_windows] == [
        (10.0, 20.0, ("operator", "passive"))
    ]
    assert snap.paused_s == pytest.approx(10.0)
    assert snap.intended_active_s == pytest.approx(20.0)
    assert snap.to_dict()["pause_windows"] == [[10.0, 20.0]]


def test_passive_mode_already_on_at_arm_opens_a_window_at_zero() -> None:
    capture_lifetime.mark_paused(True, reason="passive", now=900.0)
    capture_lifetime.begin(1000.0, profile="p", generation=1)
    snap = capture_lifetime.snapshot(1005.0)
    assert snap is not None
    assert snap.pause_windows[0].start_s == 0.0
    assert snap.pause_windows[0].end_s is None
    assert snap.intended_active_s == pytest.approx(0.0)


def test_a_profile_switch_resets_the_ledger_and_a_reconnect_is_a_gap() -> None:
    ledger = CaptureLedger()
    ledger.begin(1000.0, profile="a", generation=1)
    _feed(ledger, "A", 0.0, 50.0, 1.0)
    ledger.begin(1000.0, profile="b", generation=2)
    assert ledger.snapshot(1000.0).channels == {}  # type: ignore[union-attr]
    # A bus reconnect inside the capture shows as an interarrival, nothing else.
    _feed(ledger, "A", 0.0, 10.0, 1.0)
    _feed(ledger, "A", 40.0, 50.0, 1.0)
    row = ledger.snapshot(1150.0).channels["A"]  # type: ignore[union-attr]
    assert row.long_gaps == ((10.0, 40.0),)
    assert row.observations == 22


def test_observations_before_arm_or_without_a_capture_are_ignored() -> None:
    ledger = CaptureLedger()
    ledger.observe("A", 5.0)
    assert ledger.snapshot(10.0) is None
    ledger.begin(1000.0, profile="p", generation=1)
    ledger.observe("A", 999.0)
    assert ledger.snapshot(1001.0).channels == {}  # type: ignore[union-attr]


def test_a_clock_regression_is_counted_not_folded_into_a_negative_gap() -> None:
    acc = ChannelAccumulator("A")
    for offset in (0.0, 1.0, 2.0, 1.5, 3.0):
        acc.observe(offset)
    row = acc.snapshot(4.0)
    assert row.clock_regressions == 1
    assert row.observations == 5
    assert row.max_interarrival_s == pytest.approx(1.0)


def test_the_cadence_summary_is_bounded_and_still_right() -> None:
    reservoir = InterarrivalReservoir()
    for i in range(50_000):
        reservoir.add(0.1 if i % 2 else 0.3)
    assert reservoir.seen == 50_000
    assert len(reservoir._values) == RESERVOIR_CAPACITY
    assert reservoir.quantile(0.95) == pytest.approx(0.3)
    assert reservoir.quantile(0.5) in (0.1, 0.3)

    acc = ChannelAccumulator("A")
    t = 0.0
    last = 0.0
    for i in range(LONG_GAP_CAPACITY * 3):
        acc.observe(t)
        last = t
        t += 3.0 + (i % 7)
    row = acc.snapshot(t)
    assert len(row.long_gaps) == LONG_GAP_CAPACITY
    assert row.long_gap_count == LONG_GAP_CAPACITY * 3 - 1
    # The sum stays exact even though only the largest gaps are listed.
    assert row.long_gap_total_s == pytest.approx(last)


def test_subtract_windows_removes_only_the_overlap() -> None:
    assert subtract_windows(10.0, 100.0, [(20.0, 90.0)]) == pytest.approx(20.0)
    assert subtract_windows(10.0, 100.0, [(0.0, 5.0)]) == pytest.approx(90.0)
    assert subtract_windows(10.0, 100.0, [(0.0, 200.0)]) == pytest.approx(0.0)
    assert subtract_windows(10.0, 10.0, []) == 0.0
