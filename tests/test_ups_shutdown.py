"""Tests for the UPS (X1202) power-loss auto-shutdown watchdog logic.

Only the hardware-free decision logic is exercised here; GPIO/i2c access is
lazily imported inside main(), so this module imports cleanly under the venv.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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
def _record(**over: object) -> dict[str, object]:
    """A ``build_status_record`` call with an idle, input-present default."""
    kwargs: dict[str, object] = {
        "armed": True,
        "input_is_lost": False,
        "battery_state": False,
        "discharging_now": False,
        "discharge_rate": 0.0,
        "critical_now": False,
        "critical_soc_pct": 15.0,
        "min_discharge_rate": 6.0,
        "require_discharge": True,
        "soc": 87.654,
        "vcell": 4.1712,
        "seconds_lost": 12.345,
        "grace_s": 300.0,
        "keepalive_remaining_s": 0.0,
        "now_wall": 1700000000.0,
    }
    kwargs.update(over)
    return ups.build_status_record(**kwargs)  # type: ignore[arg-type]


def test_build_status_record_shape() -> None:
    assert _record() == {
        "armed": True,
        "input_lost": False,
        "on_battery": False,
        "discharging": False,
        "discharge_rate_pct_per_hour": 0.0,
        "min_discharge_rate_pct_per_hour": 6.0,
        "soc_critical": False,
        "critical_soc_pct": 15.0,
        "shutdown_state": "input_present",
        "countdown_reason": None,
        "soc": 87.654,
        "vcell": 4.1712,
        "seconds_lost": 12.3,
        "grace_s": 300.0,
        "keepalive_remaining_s": 0.0,
        "ts": 1700000000.0,
    }


def test_build_status_record_disarmed_has_none_fields() -> None:
    record = _record(
        armed=False,
        battery_state=None,
        discharging_now=None,
        discharge_rate=None,
        critical_now=None,
        soc=None,
        vcell=None,
        seconds_lost=0.0,
    )
    assert record["armed"] is False
    assert record["on_battery"] is None
    assert record["soc"] is None
    assert record["shutdown_state"] == "disarmed"
    assert record["discharge_rate_pct_per_hour"] is None


# --------------------------------------------------------------------------
# D03.67: the report names the condition instead of one ambiguous boolean
# --------------------------------------------------------------------------
def test_critical_soc_while_charging_with_input_present_is_not_on_battery() -> None:
    """2026-11-10 17:00Z: SoC 12.9 % and rising, input present."""
    rate = 40.0  # charging
    record = _record(
        battery_state=ups.on_battery(rate, 12.9, min_discharge_rate=6.0, critical_soc=15.0),
        discharging_now=ups.discharging(rate, min_discharge_rate=6.0),
        discharge_rate=rate,
        critical_now=ups.soc_critical(12.9, critical_soc=15.0),
        soc=12.9,
    )
    assert record["input_lost"] is False
    assert record["on_battery"] is False
    assert record["discharging"] is False
    assert record["soc_critical"] is True
    assert record["shutdown_state"] == "input_present"
    assert record["countdown_reason"] is None


def test_measured_discharge_with_input_present_says_discharging_not_on_battery() -> None:
    """2026-11-12 04:07Z: SoC 94.5 %, input present, SoC measurably falling."""
    rate = -20.0
    record = _record(
        battery_state=ups.on_battery(rate, 94.5, min_discharge_rate=6.0, critical_soc=15.0),
        discharging_now=ups.discharging(rate, min_discharge_rate=6.0),
        discharge_rate=rate,
        critical_now=False,
        soc=94.5,
    )
    assert record["input_lost"] is False
    assert record["discharging"] is True
    assert record["discharge_rate_pct_per_hour"] == -20.0
    assert record["on_battery"] is False  # legacy alias cannot contradict input_lost
    assert record["shutdown_state"] == "input_present"


def test_real_input_loss_while_draining_counts_down_with_reason() -> None:
    """2026-11-12 03:18Z: input lost, SoC 94.5 % falling -> countdown."""
    record = _record(
        input_is_lost=True,
        battery_state=True,
        discharging_now=True,
        discharge_rate=-30.0,
        critical_now=False,
        seconds_lost=120.0,
    )
    assert record["on_battery"] is True
    assert record["shutdown_state"] == "counting_down"
    assert record["countdown_reason"] == "input_lost_discharging"


def test_input_lost_at_critical_soc_names_the_floor() -> None:
    record = _record(
        input_is_lost=True,
        battery_state=True,
        discharging_now=None,
        discharge_rate=None,
        critical_now=True,
        soc=12.0,
    )
    assert record["shutdown_state"] == "counting_down"
    assert record["countdown_reason"] == "input_lost_soc_critical"


def test_input_lost_without_gauge_verdict_is_unconfirmed() -> None:
    record = _record(
        input_is_lost=True,
        battery_state=None,
        discharging_now=None,
        discharge_rate=None,
        critical_now=False,
    )
    assert record["on_battery"] is None
    assert record["shutdown_state"] == "counting_down"
    assert record["countdown_reason"] == "input_lost_unconfirmed"


def test_bench_input_lost_flat_soc_is_held_external() -> None:
    record = _record(input_is_lost=True, battery_state=False, discharging_now=False)
    assert record["on_battery"] is False
    assert record["shutdown_state"] == "held_external_power"
    assert record["countdown_reason"] is None


def test_keepalive_wins_over_critical_soc_in_the_report() -> None:
    record = _record(
        input_is_lost=True,
        battery_state=True,
        critical_now=True,
        soc=5.0,
        keepalive_remaining_s=600.0,
    )
    assert record["shutdown_state"] == "held_keepalive"
    assert record["countdown_reason"] == "input_lost_soc_critical"


def test_record_conditions_never_contradict_for_any_combination() -> None:
    """Every combination of the inputs: the legacy flag is never true beside
    input present, and a countdown reason exists exactly when one applies."""
    for input_lost in (False, True):
        for battery_state in (None, False, True):
            for discharging_now in (None, False, True):
                for critical_now in (None, False, True):
                    for hold in (0.0, 60.0):
                        for gate in (True, False):
                            r = _record(
                                input_is_lost=input_lost,
                                battery_state=battery_state,
                                discharging_now=discharging_now,
                                critical_now=critical_now,
                                keepalive_remaining_s=hold,
                                require_discharge=gate,
                            )
                            if not input_lost:
                                assert r["on_battery"] is False
                                assert r["shutdown_state"] == "input_present"
                            counting = r["shutdown_state"] in ("counting_down", "held_keepalive")
                            assert (r["countdown_reason"] is not None) is counting
                            if counting:
                                assert input_lost


class _FakeClock:
    """A ``time`` stand-in: ``sleep`` advances both clocks and ends the run."""

    def __init__(self, seconds: float) -> None:
        self.now = 1000.0
        self.end = 1000.0 + seconds
        self.stop: dict[str, bool] = {"stop": False}  # replaced by main()'s own dict

    def attach(self, main_stop: dict[str, bool]) -> None:
        self.stop = main_stop

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, dt: float) -> None:
        self.now += dt
        if self.now >= self.end:
            self.stop["stop"] = True


def _run_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    soc: float,
    gpio_level: int,
    hold_minutes: float,
    seconds: float,
) -> tuple[list[bool], dict[str, object]]:
    """Run ``main()`` against a fake GPIO line, fuel gauge and clock.

    Returns the ``_poweroff`` calls and the last status heartbeat written.
    """
    keepalive = tmp_path / "keepalive"
    status = tmp_path / "status"
    if hold_minutes > 0:
        keepalive.write_text(str(1000.0 + hold_minutes * 60.0))
    clock = _FakeClock(seconds)
    poweroffs: list[bool] = []

    class _Lgpio:
        SET_PULL_DOWN = 0

        @staticmethod
        def gpiochip_open(_chip: int) -> int:
            return 1

        @staticmethod
        def gpio_claim_input(_h: int, _pin: int, _flags: int) -> None:
            return None

        @staticmethod
        def gpio_read(_h: int, _pin: int) -> int:
            return gpio_level

        @staticmethod
        def gpiochip_close(_h: int) -> None:
            return None

    monkeypatch.setitem(sys.modules, "lgpio", _Lgpio)
    monkeypatch.setenv("UPS_KEEPALIVE_FILE", str(keepalive))
    monkeypatch.setenv("UPS_STATUS_FILE", str(status))
    monkeypatch.setenv("UPS_SHUTDOWN_DELAY", "30")
    monkeypatch.setattr(ups, "time", clock)
    monkeypatch.setattr(ups, "ups_present", lambda _b, _a: True)
    monkeypatch.setattr(ups, "read_soc", lambda _b, _a: soc)
    monkeypatch.setattr(ups, "read_vcell", lambda _b, _a: 3.6)
    monkeypatch.setattr(ups, "_poweroff", lambda dry_run: poweroffs.append(dry_run))
    monkeypatch.setattr(ups, "_install_signal_handlers", lambda main_stop: clock.attach(main_stop))
    assert ups.main() == 0
    return poweroffs, json.loads(status.read_text())


def test_keepalive_beats_critical_soc_in_the_main_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rule: an armed keep-alive holds even at SoC below the critical
    floor with input lost. Four grace periods pass and nothing powers off."""
    poweroffs, status = _run_loop(
        monkeypatch, tmp_path, soc=5.0, gpio_level=0, hold_minutes=30, seconds=120
    )
    assert poweroffs == []
    assert status["shutdown_state"] == "held_keepalive"
    assert status["countdown_reason"] == "input_lost_soc_critical"
    assert status["soc_critical"] is True
    assert status["seconds_lost"] == 0.0


def test_critical_soc_without_keepalive_powers_off_in_the_main_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Same inputs, no hold: the countdown runs and fires at the grace."""
    poweroffs, status = _run_loop(
        monkeypatch, tmp_path, soc=5.0, gpio_level=0, hold_minutes=0, seconds=120
    )
    assert poweroffs == [False]
    assert status["shutdown_state"] == "counting_down"
    assert status["countdown_reason"] == "input_lost_soc_critical"


def test_critical_soc_with_input_present_never_counts_in_the_main_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """2026-11-10: critically low SoC on a charging battery, input present."""
    poweroffs, status = _run_loop(
        monkeypatch, tmp_path, soc=12.9, gpio_level=1, hold_minutes=0, seconds=120
    )
    assert poweroffs == []
    assert status["input_lost"] is False
    assert status["on_battery"] is False
    assert status["soc_critical"] is True
    assert status["shutdown_state"] == "input_present"


def test_write_status_round_trips_json(tmp_path: Path) -> None:
    path = str(tmp_path / "ups_status")
    record = _record(
        input_is_lost=True,
        battery_state=True,
        soc=42.0,
        vcell=3.7,
        seconds_lost=5.0,
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
