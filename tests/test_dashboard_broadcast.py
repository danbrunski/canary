"""Tests for the in-memory pub/sub broadcaster (Task 5.4)."""

from __future__ import annotations

import asyncio

from src.daemon.broadcast import Broadcaster


async def test_publish_delivers_to_every_subscriber() -> None:
    bus = Broadcaster()
    a = bus.subscribe()
    b = bus.subscribe()
    delivered = bus.publish({"x": 1})
    assert delivered == 2
    assert await a.get() == {"x": 1}
    assert await b.get() == {"x": 1}


async def test_publish_with_no_subscribers_is_noop() -> None:
    bus = Broadcaster()
    delivered = bus.publish("hello")
    assert delivered == 0


async def test_unsubscribe_stops_delivery() -> None:
    bus = Broadcaster()
    sub = bus.subscribe()
    bus.unsubscribe(sub)
    delivered = bus.publish("msg")
    assert delivered == 0


async def test_full_queue_drops_oldest_not_newest() -> None:
    """Backpressure rule: a slow consumer sees the newest, loses the oldest.

    Beats blocking the producer in a real-time UI feed.
    """
    bus = Broadcaster(queue_size=2)
    sub = bus.subscribe()
    # 3 messages → queue size 2 → first message dropped
    bus.publish("a")
    bus.publish("b")
    bus.publish("c")
    assert sub.dropped == 1
    received = [await sub.get(), await sub.get()]
    assert received == ["b", "c"]


async def test_dropped_counter_persists_across_publishes() -> None:
    bus = Broadcaster(queue_size=1)
    sub = bus.subscribe()
    bus.publish(1)
    bus.publish(2)
    bus.publish(3)
    assert sub.dropped == 2


async def test_publish_threadsafe_schedules_onto_loop() -> None:
    bus = Broadcaster()
    sub = bus.subscribe()
    loop = asyncio.get_running_loop()
    # Simulate the call coming from another thread by going through
    # call_soon_threadsafe ourselves.
    bus.publish_threadsafe(loop, "from-thread")
    received = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert received == "from-thread"


async def test_async_iterator_yields_messages() -> None:
    bus = Broadcaster()
    sub = bus.subscribe()

    async def producer() -> None:
        for i in range(3):
            bus.publish(i)
            await asyncio.sleep(0)

    async def consume(n: int) -> list[int]:
        out: list[int] = []
        async for msg in sub:
            out.append(msg)
            if len(out) >= n:
                break
        return out

    task = asyncio.create_task(consume(3))
    await producer()
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == [0, 1, 2]


async def test_subscriber_count_tracks_lifecycle() -> None:
    bus = Broadcaster()
    assert bus.subscriber_count == 0
    a = bus.subscribe()
    b = bus.subscribe()
    assert bus.subscriber_count == 2
    bus.unsubscribe(a)
    assert bus.subscriber_count == 1
    bus.unsubscribe(b)
    assert bus.subscriber_count == 0


async def test_unsubscribe_unknown_subscription_is_safe() -> None:
    bus = Broadcaster()
    sub = bus.subscribe()
    bus.unsubscribe(sub)
    bus.unsubscribe(sub)  # second time — should be a no-op, not an error


async def test_publish_does_not_block_when_one_subscriber_is_full() -> None:
    """A slow subscriber must not stall delivery to the rest."""
    bus = Broadcaster(queue_size=1)
    slow = bus.subscribe()
    fast = bus.subscribe()

    bus.publish("a")
    bus.publish("b")
    bus.publish("c")
    # slow lost 2, fast also lost 2 (same queue_size; not differentiated yet);
    # but the publish never blocked.
    assert slow.dropped == 2
    assert fast.dropped == 2
    assert await slow.get() == "c"
    assert await fast.get() == "c"


# Tests are auto-marked as asyncio by pyproject.toml's asyncio_mode = "auto".
# No fixtures needed; each test owns its loop.
