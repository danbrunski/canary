"""In-memory pub/sub for live dashboard streams (Task 5.4).

The daemon produces two real-time streams:

* **telemetry** — one record per row arriving from the collectors (or, in
  v1, decoded into the wide frame). Pushed to ``/ws/telemetry``.
* **anomalies** — one ``AnomalyEvent`` per detection. Pushed to ``/ws/anomalies``.

``Broadcaster`` is the producer-agnostic fan-out: producers ``publish(...)``
and any number of subscriber asyncio queues receive the message. Designed
for short-lived per-connection subscribers (the FastAPI WebSocket handler
opens a queue at accept, closes it on disconnect).

Backpressure policy: each subscriber gets a bounded queue. If the consumer
falls behind and fills it, the *oldest* messages are dropped silently —
the dashboard sees a small gap rather than the daemon stalling. The
``dropped_count`` is observable per subscriber. A slow client doesn't
back up the producer.

Async-only — assumes the FastAPI event loop is running. The daemon's
existing asyncio path will publish into it via ``loop.call_soon_threadsafe``
when producing from a thread.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Subscription:
    """One subscriber's view of the channel.

    Use ``async for message in subscription`` (it's an async iterator) or
    pull manually with ``await subscription.get()``. ``dropped`` is the
    rolling count of messages this subscriber missed due to backpressure.
    """

    queue: asyncio.Queue[Any]
    dropped: int = 0

    async def __aiter__(self) -> AsyncIterator[Any]:
        while True:
            yield await self.queue.get()

    async def get(self) -> Any:
        return await self.queue.get()


@dataclass
class Broadcaster:
    """Fan-out pub/sub for one channel.

    Construct one per logical stream (``telemetry``, ``anomalies``).
    Thread-safe to call ``publish`` from any task on the event loop; not
    safe from a non-loop thread — use ``publish_threadsafe`` for that.
    """

    queue_size: int = 100
    _subscribers: list[Subscription] = field(default_factory=list)

    def subscribe(self) -> Subscription:
        sub = Subscription(queue=asyncio.Queue(maxsize=self.queue_size))
        self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subscribers:
            self._subscribers.remove(sub)

    def publish(self, message: Any) -> int:
        """Fan ``message`` out to every subscriber. Returns delivery count.

        If a subscriber's queue is full, we drop its oldest message to make
        room. The contract is "the freshest data wins" — better for a UI
        chart than "block the producer."
        """
        delivered = 0
        for sub in self._subscribers:
            if sub.queue.full():
                try:
                    sub.queue.get_nowait()  # discard oldest
                    sub.dropped += 1
                except asyncio.QueueEmpty:  # shouldn't happen — full+empty is a race
                    pass
            try:
                sub.queue.put_nowait(message)
                delivered += 1
            except asyncio.QueueFull:  # only on a truly tight race; treat as a drop
                sub.dropped += 1
        return delivered

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop, message: Any) -> None:
        """Schedule ``publish(message)`` on ``loop`` from a non-loop thread.

        The OBD / CAN supervisor threads (1.E.1) need this — they're not
        on the event loop, and ``queue.put_nowait`` from a different thread
        would race the loop's internal state. ``call_soon_threadsafe``
        is the documented escape hatch.
        """
        loop.call_soon_threadsafe(self.publish, message)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


__all__ = ["Broadcaster", "Subscription"]
