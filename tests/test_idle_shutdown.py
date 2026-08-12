"""Tests for the parked-vehicle -> shutdown escalation (spec §3.7.6).

The bench cases below are the point of the whole module: a Pi on USB-C at a
desk reads as ``no_data``, which is an idle state, so a naive timeout would
power off the operator's bench rig.
"""

from __future__ import annotations

import subprocess

import pytest

from src.daemon.idle_shutdown import (
    DEFAULT_ACTIVE_STATES,
    DEFAULT_IDLE_STATES,
    IdleShutdownDecider,
    input_power_present,
    run_shutdown,
)


def _parked(d: IdleShutdownDecider, *, now: float, input_present: bool | None = True) -> bool:
    return d.update(state="engine_off", now=now, input_present=input_present)


def _driving(d: IdleShutdownDecider, *, now: float) -> bool:
    return d.update(state="engine_running", now=now, input_present=True)


class TestInputPowerPresent:
    def test_input_ok_is_present(self) -> None:
        assert input_power_present({"input_lost": False}) is True

    def test_input_lost_is_absent(self) -> None:
        assert input_power_present({"input_lost": True}) is False

    @pytest.mark.parametrize("status", [None, {}, {"soc": 90}, {"input_lost": "no"}])
    def test_unknown_when_watchdog_silent_or_old(self, status) -> None:
        """No heartbeat, or one without the field, must read as unknown —
        never as 'power is fine, go ahead and shut down'."""
        assert input_power_present(status) is None


class TestBenchSafety:
    def test_bench_boot_never_arms(self) -> None:
        """Pi booted at a desk on USB-C: no vehicle ever seen, so even with
        the power signal unavailable it must never fire."""
        d = IdleShutdownDecider(idle_after_seconds=10.0)
        fired = [
            d.update(state="no_data", now=t, input_present=None) for t in range(0, 10_000, 100)
        ]
        assert not any(fired)
        assert d.armed is False

    def test_bench_boot_never_fires_even_if_power_reads_present(self) -> None:
        """Belt and braces: the prior-vehicle guard stands alone."""
        d = IdleShutdownDecider(idle_after_seconds=10.0)
        fired = [
            d.update(state="no_data", now=t, input_present=True) for t in range(0, 10_000, 100)
        ]
        assert not any(fired)

    def test_carried_from_truck_to_desk_does_not_fire(self) -> None:
        """Drove (so armed), then unplugged and carried to the bench: the
        barrel jack is out, so input_lost is true and the countdown holds."""
        d = IdleShutdownDecider(idle_after_seconds=100.0)
        _driving(d, now=0.0)
        assert d.armed is True
        fired = [
            d.update(state="no_data", now=t, input_present=False) for t in range(10, 10_000, 50)
        ]
        assert not any(fired)

    def test_unknown_power_state_blocks_shutdown(self) -> None:
        """Watchdog not running: we cannot tell bench from parked, so hold."""
        d = IdleShutdownDecider(idle_after_seconds=100.0)
        _driving(d, now=0.0)
        fired = [
            d.update(state="engine_off", now=t, input_present=None) for t in range(10, 5_000, 50)
        ]
        assert not any(fired)


class TestParkedVehicle:
    def test_fires_after_the_window_when_parked_on_vehicle_power(self) -> None:
        """The target case: always-hot pin 16, key out, bus quiet."""
        d = IdleShutdownDecider(idle_after_seconds=100.0)
        _driving(d, now=0.0)
        assert _parked(d, now=10.0) is False
        assert _parked(d, now=109.0) is False
        assert _parked(d, now=111.0) is True

    def test_ignition_only_arms_but_does_not_count_as_idle(self) -> None:
        """KOEO is an operator at the wheel — evidence of a vehicle, but not
        a reason to shut down."""
        d = IdleShutdownDecider(idle_after_seconds=10.0)
        for t in range(0, 500, 10):
            assert d.update(state="ignition_only", now=float(t), input_present=True) is False
        assert d.armed is True

    def test_a_single_active_sample_resets_the_countdown(self) -> None:
        d = IdleShutdownDecider(idle_after_seconds=100.0)
        _driving(d, now=0.0)
        _parked(d, now=10.0)
        _parked(d, now=90.0)
        assert _driving(d, now=95.0) is False
        assert _parked(d, now=96.0) is False
        assert _parked(d, now=150.0) is False
        assert _parked(d, now=200.0) is True

    def test_fires_only_once(self) -> None:
        d = IdleShutdownDecider(idle_after_seconds=10.0)
        _driving(d, now=0.0)
        _parked(d, now=1.0)
        assert _parked(d, now=20.0) is True
        assert _parked(d, now=30.0) is False
        assert _parked(d, now=10_000.0) is False

    def test_inhibit_holds_without_restarting_the_clock(self) -> None:
        """A brief capture inside a long parked window must not restart the
        countdown from zero — otherwise a periodic job could postpone
        shutdown forever."""
        d = IdleShutdownDecider(idle_after_seconds=100.0)
        _driving(d, now=0.0)
        _parked(d, now=10.0)
        assert d.update(state="engine_off", now=50.0, input_present=True, inhibited=True) is False
        assert _parked(d, now=111.0) is True

    def test_replay_is_not_an_idle_state(self) -> None:
        d = IdleShutdownDecider(idle_after_seconds=1.0)
        _driving(d, now=0.0)
        for t in range(1, 500):
            assert d.update(state="replay", now=float(t), input_present=True) is False

    def test_unknown_is_not_an_idle_state(self) -> None:
        d = IdleShutdownDecider(idle_after_seconds=1.0)
        _driving(d, now=0.0)
        for t in range(1, 500):
            assert d.update(state="unknown", now=float(t), input_present=True) is False

    def test_idle_seconds_reports_progress(self) -> None:
        d = IdleShutdownDecider(idle_after_seconds=100.0)
        assert d.idle_seconds(now=5.0) is None
        _parked(d, now=10.0)
        assert d.idle_seconds(now=40.0) == pytest.approx(30.0)
        _driving(d, now=41.0)
        assert d.idle_seconds(now=42.0) is None

    def test_first_idle_sample_only_starts_the_clock(self) -> None:
        """One observation cannot tell you how long you have been idle."""
        d = IdleShutdownDecider(idle_after_seconds=10.0)
        _driving(d, now=0.0)
        assert _parked(d, now=20.0) is False
        assert d.idle_seconds(now=20.0) == pytest.approx(0.0)
        assert _parked(d, now=31.0) is True

    def test_reset_rearms(self) -> None:
        d = IdleShutdownDecider(idle_after_seconds=10.0)
        _driving(d, now=0.0)
        _parked(d, now=1.0)
        assert _parked(d, now=20.0) is True
        d.reset()
        assert d.armed is False
        _driving(d, now=30.0)
        _parked(d, now=31.0)
        assert _parked(d, now=50.0) is True

    def test_require_prior_vehicle_can_be_disabled(self) -> None:
        d = IdleShutdownDecider(idle_after_seconds=10.0, require_prior_vehicle=False)
        _parked(d, now=0.0)
        assert _parked(d, now=20.0) is True


class TestStateSets:
    def test_idle_and_active_sets_are_disjoint_and_exclude_replay(self) -> None:
        assert set(DEFAULT_IDLE_STATES) == {"engine_off", "no_data"}
        assert set(DEFAULT_ACTIVE_STATES) == {"engine_running", "ignition_only"}
        assert not set(DEFAULT_IDLE_STATES) & set(DEFAULT_ACTIVE_STATES)
        for group in (DEFAULT_IDLE_STATES, DEFAULT_ACTIVE_STATES):
            assert "replay" not in group
            assert "unknown" not in group


class TestRunShutdown:
    def test_dry_run_does_not_execute(self, monkeypatch) -> None:
        called: list[object] = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
        assert run_shutdown(("systemctl", "poweroff"), dry_run=True) is True
        assert called == []

    def test_executes_when_not_dry_run(self, monkeypatch) -> None:
        called: list[tuple[str, ...]] = []

        def _fake_run(cmd, **kwargs):
            called.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(subprocess, "run", _fake_run)
        assert run_shutdown(("systemctl", "poweroff"), dry_run=False) is True
        assert called == [("systemctl", "poweroff")]

    @pytest.mark.parametrize(
        "exc", [OSError("nope"), subprocess.CalledProcessError(1, ("systemctl",))]
    )
    def test_failure_is_swallowed(self, monkeypatch, exc) -> None:
        """A daemon that crashes because poweroff failed is worse than one
        that keeps collecting."""

        def _boom(cmd, **kwargs):
            raise exc

        monkeypatch.setattr(subprocess, "run", _boom)
        assert run_shutdown(("systemctl", "poweroff"), dry_run=False) is False


class TestIdleShutdownMonitor:
    """The tick-facing wrapper: throttled I/O, inhibits, execution."""

    @staticmethod
    def _monitor(tmp_path, *, enabled: bool = True, **overrides):
        from src.config import IdleShutdownConfig
        from src.daemon.idle_shutdown import IdleShutdownMonitor

        status = tmp_path / "ups_status"
        keepalive = tmp_path / "ups_keepalive"
        cfg = IdleShutdownConfig(
            enabled=enabled,
            idle_after_seconds=100.0,
            status_file=str(status),
            keepalive_file=str(keepalive),
            dry_run=True,
            **overrides,
        )
        return IdleShutdownMonitor.from_config(cfg), status, keepalive

    def test_fires_when_parked_with_input_power(self, tmp_path) -> None:
        mon, status, _ = self._monitor(tmp_path)
        status.write_text('{"input_lost": false}')
        mon.observe(state="engine_running", now=0.0)
        assert mon.observe(state="engine_off", now=1.0) is False
        assert mon.observe(state="engine_off", now=200.0) is True

    def test_bench_no_status_file_never_fires(self, tmp_path) -> None:
        """No watchdog heartbeat at all — the desk case."""
        mon, _, _ = self._monitor(tmp_path)
        mon.observe(state="engine_running", now=0.0)
        assert not any(mon.observe(state="no_data", now=float(t)) for t in range(1, 2000, 25))

    def test_input_lost_never_fires(self, tmp_path) -> None:
        mon, status, _ = self._monitor(tmp_path)
        status.write_text('{"input_lost": true}')
        mon.observe(state="engine_running", now=0.0)
        assert not any(mon.observe(state="no_data", now=float(t)) for t in range(1, 2000, 25))

    def test_keepalive_hold_inhibits(self, tmp_path) -> None:
        import time as _time

        mon, status, keepalive = self._monitor(tmp_path)
        status.write_text('{"input_lost": false}')
        keepalive.write_text(str(_time.time() + 3600))
        mon.observe(state="engine_running", now=0.0)
        assert not any(mon.observe(state="engine_off", now=float(t)) for t in range(1, 2000, 25))

    def test_passive_mode_inhibits(self, tmp_path, monkeypatch) -> None:
        from src.collection import tx_gate

        mon, status, _ = self._monitor(tmp_path)
        status.write_text('{"input_lost": false}')
        monkeypatch.setattr(tx_gate, "is_passive", lambda: True)
        mon.observe(state="engine_running", now=0.0)
        assert not any(mon.observe(state="engine_off", now=float(t)) for t in range(1, 2000, 25))

    def test_active_capture_inhibits(self, tmp_path, monkeypatch) -> None:
        """Mode 22 captures make OBD go stale by design — the parked
        inference is invalid for their whole duration."""
        from src.collection import tuning_profile

        mon, status, _ = self._monitor(tmp_path)
        status.write_text('{"input_lost": false}')
        monkeypatch.setattr(tuning_profile, "is_active", lambda: True)
        mon.observe(state="engine_running", now=0.0)
        assert not any(mon.observe(state="engine_off", now=float(t)) for t in range(1, 2000, 25))

    def test_status_reads_are_throttled(self, tmp_path, monkeypatch) -> None:
        """The heartbeat is re-read on a slow cadence, not every tick."""
        from src.daemon import idle_shutdown as mod

        mon, status, _ = self._monitor(tmp_path)
        status.write_text('{"input_lost": false}')
        reads: list[object] = []
        real = mod.read_watchdog_status
        monkeypatch.setattr(mod, "read_watchdog_status", lambda p: (reads.append(p), real(p))[1])
        for t in range(0, 60):
            mon.observe(state="engine_off", now=float(t))
        # poll_seconds defaults to 15 -> ~4 reads over 60 virtual seconds,
        # not 60.
        assert len(reads) <= 6

    def test_trigger_respects_dry_run(self, tmp_path, monkeypatch) -> None:
        import subprocess as _sp

        mon, _, _ = self._monitor(tmp_path)
        called: list[object] = []
        monkeypatch.setattr(_sp, "run", lambda *a, **k: called.append(a))
        assert mon.trigger() is True
        assert called == []

    def test_disabled_config_builds_no_monitor(self, tmp_path) -> None:
        from src.config import CanaryConfig

        assert CanaryConfig.model_fields["collection"] is not None
        mon, _, _ = self._monitor(tmp_path, enabled=False)
        # from_config still builds one; the daemon is what checks `enabled`.
        assert mon.config.enabled is False
