"""Tests for the UPS (X1202) power-loss auto-shutdown watchdog logic.

Only the hardware-free decision logic is exercised here; GPIO/i2c access is
lazily imported inside main(), so this module imports cleanly under the venv.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ups_shutdown_watch as ups  # noqa: E402


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
