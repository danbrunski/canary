"""Tests for the UPS (X1202) power-loss auto-shutdown watchdog logic.

Only the hardware-free decision logic is exercised here; GPIO/i2c access is
lazily imported inside main(), so this module imports cleanly under the venv.
"""

from __future__ import annotations

import json

import scripts.ups_shutdown_watch as ups


def test_no_shutdown_while_power_good() -> None:
    d = ups.ShutdownDecider(delay_s=30.0)
    for t in range(0, 100, 1):
        assert d.update(power_lost=False, now=float(t)) is False
    assert d.counting_down is False


def test_shutdown_after_continuous_loss() -> None:
    d = ups.ShutdownDecider(delay_s=30.0)
    assert d.update(power_lost=True, now=0.0) is False  # countdown starts
    assert d.counting_down is True
    assert d.update(power_lost=True, now=29.0) is False  # not yet
    assert d.update(power_lost=True, now=30.0) is True  # threshold reached


def test_cranking_dip_does_not_trigger() -> None:
    """A short power drop (e.g. ACC dipping during cranking) must reset."""
    d = ups.ShutdownDecider(delay_s=30.0)
    d.update(power_lost=True, now=0.0)
    d.update(power_lost=True, now=3.0)
    assert d.update(power_lost=False, now=4.0) is False  # power restored -> reset
    assert d.counting_down is False
    # A later real key-off must count from scratch, not from the dip.
    d.update(power_lost=True, now=10.0)
    assert d.update(power_lost=True, now=39.0) is False  # only 29s in
    assert d.update(power_lost=True, now=40.0) is True


def test_restore_before_threshold_then_relose() -> None:
    d = ups.ShutdownDecider(delay_s=10.0)
    d.update(power_lost=True, now=0.0)
    d.update(power_lost=False, now=5.0)  # restored
    d.update(power_lost=True, now=6.0)  # lost again, restart clock
    assert d.update(power_lost=True, now=15.0) is False  # 9s since re-loss
    assert d.update(power_lost=True, now=16.0) is True  # 10s since re-loss


def test_seconds_lost_tracks_elapsed() -> None:
    d = ups.ShutdownDecider(delay_s=30.0)
    assert d.seconds_lost(5.0) == 0.0
    d.update(power_lost=True, now=10.0)
    assert d.seconds_lost(25.0) == 15.0


def test_presence_guard_disabled_with_zero_addr() -> None:
    # addr == 0 disables the i2c guard (always "present").
    assert ups.ups_present(bus=1, addr=0) is True


# --------------------------------------------------------------------------
# Discharge gate: bench (USB-C, battery idle) vs. key-off (battery draining)
# --------------------------------------------------------------------------
def test_discharge_estimator_needs_min_span() -> None:
    e = ups.DischargeEstimator(window_s=20.0, min_span_s=5.0)
    e.update(soc=80.0, now=0.0)
    assert e.rate_pct_per_hour() is None  # single sample
    e.update(soc=79.9, now=2.0)
    assert e.rate_pct_per_hour() is None  # span < min_span_s


def test_discharge_estimator_reports_negative_rate_on_drain() -> None:
    e = ups.DischargeEstimator(window_s=60.0, min_span_s=5.0)
    # ~1% SoC lost over 60s => -60 %/hr (a Pi 5 on a small cell).
    e.update(soc=80.0, now=0.0)
    e.update(soc=79.0, now=60.0)
    rate = e.rate_pct_per_hour()
    assert rate is not None and rate == -60.0


def test_bench_flat_soc_is_not_on_battery() -> None:
    """USB-C on the bench: SoC holds flat -> not discharging -> hold."""
    e = ups.DischargeEstimator(window_s=30.0)
    for t in range(0, 40, 2):
        e.update(soc=97.0, now=float(t))
    state = ups.on_battery(e.rate_pct_per_hour(), 97.0, min_discharge_rate=6.0, critical_soc=15.0)
    assert state is False


def test_keyoff_drain_is_on_battery() -> None:
    e = ups.DischargeEstimator(window_s=30.0)
    # Drop ~0.5%/10s = 180 %/hr, well past the 6 %/hr floor.
    e.update(soc=90.0, now=0.0)
    e.update(soc=89.5, now=10.0)
    state = ups.on_battery(e.rate_pct_per_hour(), 89.5, min_discharge_rate=6.0, critical_soc=15.0)
    assert state is True


def test_critical_soc_forces_on_battery_even_without_rate() -> None:
    # No rate yet, but SoC already critically low while input is lost.
    state = ups.on_battery(None, 12.0, min_discharge_rate=6.0, critical_soc=15.0)
    assert state is True


def test_on_battery_unknown_without_gauge_data() -> None:
    state = ups.on_battery(None, None, min_discharge_rate=6.0, critical_soc=15.0)
    assert state is None


def test_trickle_below_floor_is_not_on_battery() -> None:
    # Slow self-discharge (~2 %/hr) must not look like a real drain.
    state = ups.on_battery(-2.0, 88.0, min_discharge_rate=6.0, critical_soc=15.0)
    assert state is False


def test_power_truly_lost_requires_discharge_when_gated() -> None:
    # Input lost + battery idle (confirmed bench/external power) -> not actionable.
    assert ups.power_truly_lost(True, False, require_discharge=True) is False
    # Input lost + draining (key-off) -> actionable.
    assert ups.power_truly_lost(True, True, require_discharge=True) is True
    # Input lost + not-yet-determined -> counts (aborts only on confirmed external
    # power), so a real key-off shuts down predictably after the grace window.
    assert ups.power_truly_lost(True, None, require_discharge=True) is True
    # Input present -> never actionable regardless of battery.
    assert ups.power_truly_lost(False, True, require_discharge=True) is False
    assert ups.power_truly_lost(False, None, require_discharge=True) is False


def test_power_truly_lost_legacy_pld_only() -> None:
    # With the gate off, PLD alone decides (old behavior).
    assert ups.power_truly_lost(True, None, require_discharge=False) is True
    assert ups.power_truly_lost(False, True, require_discharge=False) is False


def test_pld_polarity_matches_vendor() -> None:
    # Vendor pld.py: GPIO6 HIGH (1) = power OK, LOW (0) = power lost.
    assert ups.input_lost(0) is True  # LOW  -> lost
    assert ups.input_lost(1) is False  # HIGH -> OK


def test_powered_high_is_not_actionable_end_to_end() -> None:
    """Real barrel-jack power: GPIO6 HIGH -> not lost -> never counts down,
    even though the battery (charging) obviously isn't discharging."""
    decider = ups.ShutdownDecider(delay_s=30.0)
    fired = False
    for t in range(0, 120, 2):
        input_is_lost = ups.input_lost(1)  # powered: HIGH
        lost = ups.power_truly_lost(input_is_lost, False, require_discharge=True)
        if decider.update(power_lost=lost, now=float(t)):
            fired = True
    assert fired is False
    assert decider.counting_down is False


def test_keyoff_drain_shuts_down_after_grace() -> None:
    """Real key-off: input lost + battery draining -> shuts down ~grace later."""
    decider = ups.ShutdownDecider(delay_s=300.0)
    e = ups.DischargeEstimator(window_s=120.0, min_span_s=40.0)
    soc = 80.0
    fired_at = None
    for t in range(0, 400, 2):
        soc -= 2 * (14.0 / 3600.0)  # ~14%/hr drain
        e.update(soc, float(t))
        st = ups.on_battery(e.rate_pct_per_hour(), soc, min_discharge_rate=6.0, critical_soc=15.0)
        lost = ups.power_truly_lost(True, st, require_discharge=True)
        if decider.update(power_lost=lost, now=float(t)) and fired_at is None:
            fired_at = t
    assert fired_at is not None
    assert 300 <= fired_at <= 302  # counts from input-loss (None counts), fires at grace


def test_keepalive_remaining() -> None:
    assert ups.keepalive_remaining("1000.0", 900.0) == 100.0  # 100s left
    assert ups.keepalive_remaining("1000", 1000.0) == 0.0  # exactly expired
    assert ups.keepalive_remaining("1000", 1100.0) == 0.0  # expired
    assert ups.keepalive_remaining(None, 0.0) == 0.0  # no hold file
    assert ups.keepalive_remaining("garbage", 0.0) == 0.0  # unparseable


def test_bench_never_counts_down_end_to_end() -> None:
    """Fused pipeline: PLD high + flat SoC must never reach shutdown."""
    decider = ups.ShutdownDecider(delay_s=30.0)
    e = ups.DischargeEstimator(window_s=30.0)
    fired = False
    for t in range(0, 300, 2):  # 5 minutes on the bench
        e.update(soc=96.0, now=float(t))
        state = ups.on_battery(
            e.rate_pct_per_hour(), 96.0, min_discharge_rate=6.0, critical_soc=15.0
        )
        lost = ups.power_truly_lost(True, state, require_discharge=True)
        if decider.update(power_lost=lost, now=float(t)):
            fired = True
    assert fired is False
    assert decider.counting_down is False


# --------------------------------------------------------------------------
# Status heartbeat (task 1.2): what the daemon-side power reader consumes
# --------------------------------------------------------------------------
def test_build_status_record_shape() -> None:
    record = ups.build_status_record(
        armed=True,
        input_is_lost=False,
        on_battery=False,
        soc=87.654,
        vcell=4.1712,
        seconds_lost=12.345,
        grace_s=300.0,
        keepalive_remaining_s=0.0,
        now_wall=1700000000.0,
    )
    assert record == {
        "armed": True,
        "input_lost": False,
        "on_battery": False,
        "soc": 87.654,
        "vcell": 4.1712,
        "seconds_lost": 12.3,
        "grace_s": 300.0,
        "keepalive_remaining_s": 0.0,
        "ts": 1700000000.0,
    }


def test_build_status_record_disarmed_has_none_fields() -> None:
    record = ups.build_status_record(
        armed=False,
        input_is_lost=False,
        on_battery=None,
        soc=None,
        vcell=None,
        seconds_lost=0.0,
        grace_s=300.0,
        keepalive_remaining_s=0.0,
        now_wall=0.0,
    )
    assert record["armed"] is False
    assert record["on_battery"] is None
    assert record["soc"] is None


def test_write_status_round_trips_json(tmp_path) -> None:
    path = str(tmp_path / "ups_status")
    record = ups.build_status_record(
        armed=True,
        input_is_lost=True,
        on_battery=True,
        soc=42.0,
        vcell=3.7,
        seconds_lost=5.0,
        grace_s=300.0,
        keepalive_remaining_s=0.0,
        now_wall=123.0,
    )
    ups.write_status(path, record)
    with open(path) as f:
        loaded = json.load(f)
    assert loaded == record
    # No leftover temp file after the atomic rename.
    assert not (tmp_path / "ups_status.tmp").exists()


def test_write_status_swallows_write_failure() -> None:
    # A directory that doesn't exist -> OSError on open(), must not raise.
    ups.write_status("/nonexistent-dir-xyz/ups_status", {"a": 1})
