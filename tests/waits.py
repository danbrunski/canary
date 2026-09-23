"""Wait for a condition, never for an amount of wall clock (T01).

A test that sleeps N seconds and then asserts something happened, or bounds a
collector loop with ``deadline_monotonic=time.monotonic() + 0.5``, measures the
machine as much as the code: on 2026-09-23 such tests failed under an ordinary
parallel gate beside other agent sessions and passed alone. Raising N only
moves the load at which they fail.

So a test waits for the thing it is about to assert on, and the only number
left is :data:`HANG_GUARD_SECONDS`: a cap that turns a hung test into a failure
instead of a stuck suite. It is never a pass/fail threshold. A test whose
subject *is* a time bound (a timeout fires, an interval is honoured) says so
at the call site and keeps its own number.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

T = TypeVar("T")

#: How long a condition wait may take before the test is declared hung.
#: Deliberately far above any healthy run under load. The slowest measured
#: wait (2026-09-23, two busy-loop burners per core, load average 27-36 on 12
#: cores) is ``run_daemon``'s shutdown at 6.0 s: it waits for the dashboard
#: app, whose cold import in its build thread took 5.7 s there, and that is
#: real work, not a stall. A test that needs anywhere near 30 s is broken.
HANG_GUARD_SECONDS = 30.0


async def wait_until(
    predicate: Callable[[], bool],
    *,
    what: str,
    poll_seconds: float = 0.01,
) -> None:
    """Return once ``predicate()`` holds; fail naming ``what`` if it never does."""
    loop = asyncio.get_running_loop()
    give_up_at = loop.time() + HANG_GUARD_SECONDS
    while not predicate():
        if loop.time() >= give_up_at:
            raise AssertionError(f"hung: {what} (not reached within {HANG_GUARD_SECONDS:.0f} s)")
        await asyncio.sleep(poll_seconds)


async def settle(awaitable: Awaitable[T]) -> T:
    """Await a task/event/queue read that the test has already made due.

    ``asyncio.wait_for`` with the hang guard: the awaited thing finishing is
    the condition, the cap only stops a hang.
    """
    return await asyncio.wait_for(awaitable, timeout=HANG_GUARD_SECONDS)


def hang_guard_deadline() -> float:
    """``deadline_monotonic`` for a collector loop the test stops on a condition."""
    return time.monotonic() + HANG_GUARD_SECONDS


class StopWhen(Generic[T]):
    """An ``on_reading`` sink that sets ``stop`` once ``done(items)`` holds.

    Pairs with :func:`hang_guard_deadline`: the loop ends on the condition the
    test asserts, not on how much it managed to do before a short deadline.
    ``met`` says which of the two ended it.
    """

    def __init__(self, done: Callable[[list[T]], bool], stop: threading.Event | None = None):
        self.items: list[T] = []
        self.stop = stop if stop is not None else threading.Event()
        self.met = False
        self._done = done

    def __call__(self, item: T) -> None:
        self.items.append(item)
        if not self.met and self._done(self.items):
            self.met = True
            self.stop.set()
