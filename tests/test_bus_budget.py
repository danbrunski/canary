"""OBD request-rate override — src/collection/bus_budget.py."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.collection import bus_budget


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    """Process-wide state; never leak between tests."""
    bus_budget.reset()
    yield
    bus_budget.reset()


def test_default_is_no_override() -> None:
    assert bus_budget.override() is None
    assert bus_budget.effective(8.0) == 8.0


def test_override_wins_over_configured_default() -> None:
    bus_budget.set_override(100.0, source="test")
    assert bus_budget.effective(8.0) == 100.0


def test_clearing_restores_the_configured_default() -> None:
    bus_budget.set_override(100.0, source="test")
    previous = bus_budget.set_override(None, source="test")
    assert previous == 100.0
    assert bus_budget.effective(8.0) == 8.0


def test_out_of_range_raises_rather_than_clamping() -> None:
    """An operator asking for 500 req/s has a wrong model of the bus.

    Silently handing back 200 would hide that, and they would go on believing
    the capture ran at the rate they asked for.
    """
    with pytest.raises(bus_budget.BudgetOutOfRangeError):
        bus_budget.set_override(500.0)
    with pytest.raises(bus_budget.BudgetOutOfRangeError):
        bus_budget.set_override(0.5)
    assert bus_budget.override() is None


def test_ttl_auto_reverts() -> None:
    bus_budget.set_override(50.0, ttl_seconds=10.0, now=1000.0)
    assert bus_budget.check_expiry(now=1005.0) is False
    assert bus_budget.effective(8.0) == 50.0

    assert bus_budget.check_expiry(now=1011.0) is True
    assert bus_budget.effective(8.0) == 8.0
    # Idempotent — a second poll after reverting is a no-op.
    assert bus_budget.check_expiry(now=1012.0) is False


def test_ttl_defaults_on() -> None:
    """Unlike passive mode, an override auto-reverts unless opted out.

    A forgotten passive mode is loud (nothing gets logged); a forgotten rate
    override is silent, so the safe default is inverted here.
    """
    bus_budget.set_override(50.0, now=0.0)
    assert bus_budget.check_expiry(now=bus_budget.DEFAULT_TTL_SECONDS + 1.0) is True
    assert bus_budget.override() is None


def test_explicit_none_ttl_persists() -> None:
    bus_budget.set_override(50.0, ttl_seconds=None, now=0.0)
    assert bus_budget.check_expiry(now=1e6) is False
    assert bus_budget.effective(8.0) == 50.0


def test_clearing_cancels_a_pending_ttl() -> None:
    bus_budget.set_override(50.0, ttl_seconds=10.0, now=0.0)
    bus_budget.set_override(None, now=1.0)
    bus_budget.set_override(75.0, ttl_seconds=None, now=2.0)
    # The first override's TTL must not fire against the second one.
    assert bus_budget.check_expiry(now=100.0) is False
    assert bus_budget.effective(8.0) == 75.0


def test_snapshot_reports_state_and_remaining_ttl() -> None:
    bus_budget.set_override(120.0, source="control", ttl_seconds=60.0, now=0.0)
    snap = bus_budget.snapshot(now=15.0)
    assert snap["override_requests_per_second"] == 120.0
    assert snap["source"] == "control"
    assert snap["ttl_remaining_seconds"] == 45.0
    assert snap["max_requests_per_second"] == bus_budget.MAX_BUDGET_REQUESTS_PER_SECOND


def test_collector_reads_the_override_live_not_at_startup() -> None:
    """Regression guard for the wiring bug this feature could easily have had.

    ``budget_rps`` is computed once before the poll loop. If the pacing sites
    used that captured value, an override armed mid-drive would do nothing
    until the daemon restarted — which is exactly the case the feature exists
    for. The loop must call ``effective()`` per iteration instead.
    """
    import inspect

    from src.collection import can_obd_collector

    source = inspect.getsource(can_obd_collector)
    # The pacing sites divide by the rate; every one of them must go through
    # the gate rather than the captured default.
    assert "1.0 / budget_rps" not in source, (
        "a pacing site still uses the startup-captured rate; an override "
        "armed mid-drive would not take effect"
    )
    assert source.count("bus_budget.effective(budget_rps)") >= 3
