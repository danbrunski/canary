"""OBD request-rate override — src/collection/bus_budget.py."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import patch

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


def test_restore_preserves_source_and_remaining_ttl() -> None:
    bus_budget.set_override(120.0, source="operator", ttl_seconds=60.0, now=100.0)
    saved = bus_budget.snapshot(now=115.0)
    bus_budget.set_override(40.0, source="capture", ttl_seconds=None, now=115.0)

    bus_budget.restore(saved, now=115.0)

    restored = bus_budget.snapshot(now=115.0)
    assert restored["override_requests_per_second"] == 120.0
    assert restored["source"] == "operator"
    assert restored["ttl_remaining_seconds"] == 45.0


def test_collector_reads_the_override_live_not_at_startup() -> None:
    """Regression guard for the wiring bug this feature could easily have had.

    ``budget_rps`` is computed once before the poll loop. If the pacing sites
    used that captured value, an override armed mid-drive would do nothing
    until the daemon restarted — which is exactly the case the feature exists
    for. The loop must call ``effective()`` per iteration instead.
    """
    import inspect

    from src.collection import can_obd_loop

    source = inspect.getsource(can_obd_loop)
    # The pacing sites divide by the rate; every one of them must go through
    # the gate rather than the captured default. They all read the rate
    # through one method, `_slot()`: `_spend_budget_slot` (every request) and
    # the memory burst's up-front charge (B08.4).
    assert "1.0 / budget_rps" not in source, (
        "a pacing site still uses the startup-captured rate; an override "
        "armed mid-drive would not take effect"
    )
    assert "1.0 / self.budget_rps" not in source
    assert source.count("bus_budget.effective(self.budget_rps)") == 1
    assert source.count("self._spend_budget_slot()") >= 5


def test_status_reports_the_effective_rate_not_the_configured_one() -> None:
    """/status must not contradict the bus.

    The collector paces at `effective()`, so reporting the configured value
    here made `/status` read 8.0 while requests actually went out at 25/s.
    The override's whole documented value is that an unexpectedly high
    achieved rate is distinguishable from a fault, which requires the two
    numbers to agree.
    """
    from src.collection.can_obd_collector import CanObdLoopStats
    from src.collection.obd_scheduler import CanObdSpec, _scheduler_metrics
    from src.config import CanBusConfig

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=1.0,
        pids=("RPM",),
    )
    configured = _scheduler_metrics([], [], spec, CanObdLoopStats(), now=0.0).to_dict()
    assert configured["bus_budget_requests_per_second"] == 8.0

    bus_budget.set_override(25.0, source="test", ttl_seconds=None)
    overridden = _scheduler_metrics([], [], spec, CanObdLoopStats(), now=0.0).to_dict()
    assert overridden["bus_budget_requests_per_second"] == 25.0


@pytest.mark.asyncio
async def test_mcp_write_tool_can_express_no_ttl() -> None:
    """`ttl_seconds=None` is the documented opt-out; it must reach the daemon.

    Omitting the key instead handed the choice back to the daemon's own 600 s
    default, so a caller asking for an override that persists for the drive
    silently got one that auto-reverted mid-capture.
    """
    from src.mcp_server.write_tools import bus_budget as write_tool

    sent: dict[str, object] = {}

    async def _capture(
        client: object, audit: object, *, tool: str, path: str, body: dict[str, object]
    ) -> dict[str, object]:
        sent.update(body)
        return {}

    import src.mcp_server.write_tools.bus_budget as module

    with patch.object(module, "audited_post", _capture):
        await write_tool.set_bus_budget(
            cast(Any, None), cast(Any, None), requests_per_second=25.0, ttl_seconds=None
        )

    assert "ttl_seconds" in sent, "the opt-out never reached the daemon"
    assert sent["ttl_seconds"] is None
