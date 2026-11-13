"""Unit tests for the shared bus-reconnect step (src/collection/reconnect.py).

These pin the outcome state-machine that was lifted verbatim out of the
two collectors' identical `if current_bus is None:` blocks, so a future
change to the shared helper can't silently alter in-vehicle reconnect
behaviour without a red test.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from src.collection.reconnect import (
    Backoff,
    BusReconnector,
    RebuildOutcome,
    ReconnectPolicy,
    run_with_reconnect,
    wait_or_stop,
)

LOG = logging.getLogger("test.reconnect")
# initial_delay 0 ⇒ wait_or_stop returns immediately, no real sleeping.
ZERO = ReconnectPolicy(initial_delay_seconds=0.0, max_delay_seconds=0.0)
BACKOFF = ReconnectPolicy(initial_delay_seconds=0.0, max_delay_seconds=8.0, multiplier=2.0)


def _reconnector(policy: ReconnectPolicy, max_reconnects: int | None) -> BusReconnector:
    return BusReconnector(policy, max_reconnects, LOG)


def test_rebuilt_returns_bus_and_resets_counters() -> None:
    r = _reconnector(BACKOFF, 3)
    r.failed_rebuilds = 2  # pretend we'd been failing
    r.delay = 4.0
    sentinel = object()

    outcome, bus = r.rebuild(
        lambda _arg: sentinel,
        "spec",
        stop_event=None,
        deadline_monotonic=None,
        frames_recorded=0,
    )

    assert outcome is RebuildOutcome.REBUILT
    assert bus is sentinel
    assert r.reconnects == 1
    assert r.failed_rebuilds == 0
    assert r.delay == BACKOFF.initial_delay_seconds


def test_retry_then_exhausted_caps_attempts() -> None:
    r = _reconnector(BACKOFF, 2)

    def boom(_arg: str) -> object:
        raise OSError("usb gone")

    o1, b1 = r.rebuild(boom, "s", stop_event=None, deadline_monotonic=None, frames_recorded=0)
    assert o1 is RebuildOutcome.RETRY
    assert b1 is None
    assert r.failed_rebuilds == 1
    assert r.delay == BACKOFF.next_delay(0.0)  # backoff advanced

    o2, b2 = r.rebuild(boom, "s", stop_event=None, deadline_monotonic=None, frames_recorded=7)
    assert o2 is RebuildOutcome.EXHAUSTED
    assert b2 is None
    assert r.failed_rebuilds == 2
    assert isinstance(r.last_error, OSError)


def test_unlimited_retries_when_max_is_none() -> None:
    r = _reconnector(ZERO, None)

    def boom(_arg: str) -> object:
        raise RuntimeError("adapter reset")

    for _ in range(50):
        outcome, _ = r.rebuild(
            boom, "s", stop_event=None, deadline_monotonic=None, frames_recorded=0
        )
        assert outcome is RebuildOutcome.RETRY
    assert r.failed_rebuilds == 50


def test_stop_event_short_circuits_before_build() -> None:
    r = _reconnector(BACKOFF, 3)
    # wait_or_stop only consults the stop event when the backoff delay is
    # > 0 (matches the original _wait_interruptible); a set event then
    # returns immediately (no real sleep).
    r.delay = 5.0
    stop = threading.Event()
    stop.set()
    built = False

    def factory(_arg: str) -> object:
        nonlocal built
        built = True
        return object()

    outcome, bus = r.rebuild(
        factory, "s", stop_event=stop, deadline_monotonic=None, frames_recorded=0
    )

    assert outcome is RebuildOutcome.STOP_SIGNAL
    assert bus is None
    assert built is False  # never attempted the rebuild


def test_deadline_short_circuits_before_build() -> None:
    r = _reconnector(ZERO, 3)
    built = False

    def factory(_arg: str) -> object:
        nonlocal built
        built = True
        return object()

    outcome, bus = r.rebuild(
        factory,
        "s",
        stop_event=None,
        deadline_monotonic=time.monotonic() - 1.0,
        frames_recorded=0,
    )

    assert outcome is RebuildOutcome.STOP_DEADLINE
    assert bus is None
    assert built is False


def test_wait_or_stop_semantics() -> None:
    assert wait_or_stop(None, 0.0) is False
    assert wait_or_stop(None, -1.0) is False
    fired = threading.Event()
    fired.set()
    assert wait_or_stop(fired, 5.0) is True  # returns immediately, not after 5s
    idle = threading.Event()
    assert wait_or_stop(idle, 0.0) is False


async def test_run_with_reconnect_retries_then_succeeds() -> None:
    stop_event = asyncio.Event()
    attempts = 0
    torn_down: list[str] = []

    async def attempt(capture: object) -> None:
        nonlocal attempts
        attempts += 1
        capture("resource")  # type: ignore[operator]
        if attempts < 3:
            raise RuntimeError("transient failure")
        stop_event.set()

    await asyncio.wait_for(
        run_with_reconnect(
            attempt,
            stop_event=stop_event,
            policy=ZERO,
            teardown=torn_down.append,
            log=LOG,
            reconnect_message="test collector failed; reconnecting",
        ),
        timeout=2.0,
    )

    assert attempts == 3
    assert torn_down == ["resource", "resource", "resource"]  # every attempt tears down


async def test_run_with_reconnect_reraises_value_error_immediately() -> None:
    stop_event = asyncio.Event()
    attempts = 0

    async def attempt(capture: object) -> None:
        nonlocal attempts
        attempts += 1
        raise ValueError("bad config")

    with pytest.raises(ValueError, match="bad config"):
        await asyncio.wait_for(
            run_with_reconnect(
                attempt,
                stop_event=stop_event,
                policy=ZERO,
                teardown=lambda _resource: None,
                log=LOG,
                reconnect_message="test collector failed; reconnecting",
            ),
            timeout=2.0,
        )

    assert attempts == 1  # no reconnect retry on a re-raised exception type


async def test_run_with_reconnect_no_policy_propagates_first_failure() -> None:
    stop_event = asyncio.Event()

    async def attempt(capture: object) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await asyncio.wait_for(
            run_with_reconnect(
                attempt,
                stop_event=stop_event,
                policy=None,
                teardown=lambda _resource: None,
                log=LOG,
                reconnect_message="test collector failed; reconnecting",
            ),
            timeout=2.0,
        )


def test_backoff_follows_the_policys_schedule_and_resets() -> None:
    policy = ReconnectPolicy(initial_delay_seconds=2.0, max_delay_seconds=7.0, multiplier=2.0)
    backoff = policy.backoff()
    assert isinstance(backoff, Backoff) and backoff.delay == 2.0
    seen = []
    for _ in range(4):
        seen.append(backoff.delay)
        backoff.grow()
    assert seen == [2.0, 4.0, 7.0, 7.0]  # capped at the policy's maximum
    backoff.reset()
    assert backoff.delay == 2.0
    # An independent schedule per loop: growing one does not move another.
    assert policy.backoff().delay == 2.0


async def test_backoff_wait_waits_then_grows_and_reports_a_stop() -> None:
    backoff = ReconnectPolicy(initial_delay_seconds=0.01, max_delay_seconds=0.04).backoff()
    stop = asyncio.Event()
    assert await backoff.wait(stop) is False
    assert backoff.delay == pytest.approx(0.02)
    stop.set()
    assert await backoff.wait(stop) is True
    assert backoff.delay == pytest.approx(0.04)
