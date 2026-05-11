"""Reconnect/backoff helpers shared by live collectors."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


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
