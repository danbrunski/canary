"""Reconnect/backoff helpers shared by live collectors."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import TypeVar


@dataclass(frozen=True)
class ReconnectPolicy:
    """Bounded exponential backoff for adapter reconnect loops."""

    initial_delay_seconds: float = 10.0
    max_delay_seconds: float = 60.0
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds must be non-negative")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds must be >= initial_delay_seconds")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")

    def next_delay(self, current_delay: float) -> float:
        return min(self.max_delay_seconds, current_delay * self.multiplier)


async def wait_for_stop_or_timeout(stop_event: asyncio.Event, delay_seconds: float) -> bool:
    """Wait for either stop_event or delay; return True when stop_event fired."""
    if delay_seconds <= 0:
        await asyncio.sleep(0)
        return stop_event.is_set()
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_seconds)
    except TimeoutError:
        return stop_event.is_set()
    return True


def wait_or_stop(stop_event: threading.Event | None, seconds: float) -> bool:
    """Sleep `seconds`, waking early if `stop_event` fires (sync sibling of
    `wait_for_stop_or_timeout`).

    Returns True if the wait was cut short by the stop event — the caller
    should treat that as a clean stop signal.
    """
    if seconds <= 0:
        return False
    if stop_event is None:
        time.sleep(seconds)
        return False
    return stop_event.wait(seconds)


BusT = TypeVar("BusT")
ArgT = TypeVar("ArgT")


class RebuildOutcome(Enum):
    """What a single `BusReconnector.rebuild` attempt resolved to."""

    REBUILT = auto()
    RETRY = auto()
    STOP_SIGNAL = auto()
    STOP_DEADLINE = auto()
    EXHAUSTED = auto()


@dataclass
class BusReconnector:
    """One bounded bus-rebuild attempt + its backoff accounting.

    Lifted verbatim from the identical `if current_bus is None:` blocks in
    `can_collector.record` and `can_obd_collector.record_can_obd`. The caller
    still owns its capture loop and decides what to do with the outcome — the
    bus is built via a thin `build()` thunk, so no control flow crosses the
    boundary (the collector's recv/write/manifest logic is untouched).
    """

    policy: ReconnectPolicy
    max_reconnects: int | None
    logger: logging.Logger
    delay: float = -1.0
    failed_rebuilds: int = 0
    reconnects: int = 0
    last_error: BaseException | None = None

    def __post_init__(self) -> None:
        if self.delay < 0:
            self.delay = self.policy.initial_delay_seconds

    def rebuild(
        self,
        factory: Callable[[ArgT], BusT],
        factory_arg: ArgT,
        *,
        stop_event: threading.Event | None,
        deadline_monotonic: float | None,
        frames_recorded: int,
    ) -> tuple[RebuildOutcome, BusT | None]:
        """Wait the backoff, then attempt exactly one ``factory(factory_arg)``.

        Mutates the backoff counters. On success returns the new bus; on a
        failed build either RETRY (caller `continue`s) or EXHAUSTED (caller
        stops with ``reconnect_exhausted`` and ``self.last_error``). A stop /
        deadline during the backoff short-circuits.
        """
        if wait_or_stop(stop_event, self.delay):
            return RebuildOutcome.STOP_SIGNAL, None
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            return RebuildOutcome.STOP_DEADLINE, None
        try:
            bus = factory(factory_arg)
        except Exception as build_exc:
            self.failed_rebuilds += 1
            self.logger.warning(
                "bus rebuild failed; will retry",
                extra={
                    "error": f"{type(build_exc).__name__}: {build_exc}",
                    "next_delay_seconds": self.delay,
                    "failed_rebuilds": self.failed_rebuilds,
                    "max_reconnects": self.max_reconnects,
                },
            )
            if self.max_reconnects is not None and self.failed_rebuilds >= self.max_reconnects:
                self.logger.error(
                    "reconnect attempts exhausted; stopping recording",
                    extra={
                        "failed_rebuilds": self.failed_rebuilds,
                        "frames_recorded": frames_recorded,
                    },
                )
                self.last_error = build_exc
                return RebuildOutcome.EXHAUSTED, None
            self.delay = self.policy.next_delay(self.delay)
            return RebuildOutcome.RETRY, None
        self.reconnects += 1
        self.failed_rebuilds = 0
        self.delay = self.policy.initial_delay_seconds
        self.logger.info("bus reconnected", extra={"total_reconnects": self.reconnects})
        return RebuildOutcome.REBUILT, bus


ResourceT = TypeVar("ResourceT")


async def run_with_reconnect(
    attempt: Callable[[Callable[[ResourceT], None]], Awaitable[None]],
    *,
    stop_event: asyncio.Event,
    policy: ReconnectPolicy | None,
    teardown: Callable[[ResourceT], None],
    log: logging.Logger,
    reconnect_message: str,
    log_extra: Callable[[], Mapping[str, object]] | None = None,
    reraise: tuple[type[BaseException], ...] = (ValueError,),
) -> None:
    """Shared async collector skeleton: build a resource, run until it fails
    or `stop_event` fires, reconnect with backoff on any other error.

    Lifted from four near-identical copies (`can_collector.collect_can_from_config`,
    `obd_collector.collect_obd`, `can_obd_collector.collect_can_obd_from_config`,
    `gps_collector.collect_gps_from_config`) — the GPS copy was missing the
    fail-fast re-raise on bad config (`reraise`), so an invalid device spec
    spun forever in the reconnect loop instead of surfacing immediately.

    `attempt` builds its resource and hands it to the `capture` callback it
    receives *before* awaiting the run, so `teardown` still runs if the
    resource fails mid-run rather than only on a clean return. `policy is
    None` disables reconnecting entirely — the first failure propagates,
    matching every collector's existing "no policy = crash" contract.
    """
    delay = policy.initial_delay_seconds if policy is not None else 0.0
    while not stop_event.is_set():
        resource: ResourceT | None = None

        def _capture(built: ResourceT) -> None:
            nonlocal resource
            resource = built

        try:
            await attempt(_capture)
            return
        except reraise:
            raise
        except Exception as exc:
            if stop_event.is_set():
                return
            if policy is None:
                raise
            log.warning(
                reconnect_message,
                extra={
                    **(dict(log_extra()) if log_extra is not None else {}),
                    "error": f"{type(exc).__name__}: {exc}",
                    "reconnect_delay_seconds": delay,
                },
            )
            if await wait_for_stop_or_timeout(stop_event, delay):
                return
            delay = policy.next_delay(delay)
        finally:
            if resource is not None:
                teardown(resource)
