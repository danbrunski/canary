"""Shared websocket/broadcast helpers for the dashboard app."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect, status

from src.daemon.broadcast import Broadcaster


async def _run_websocket(
    websocket: WebSocket,
    bus: Broadcaster,
    *,
    still_authorized: Callable[[], bool] | None = None,
) -> None:
    """Hold one WebSocket open, pumping messages from ``bus`` until disconnect.

    ``still_authorized`` is checked before every message: auth is checked once
    at the handshake, so without it a socket opened by a device that is later
    removed would keep receiving control events until it reconnected.
    """
    await websocket.accept()
    subscription = bus.subscribe()
    try:
        while True:
            message = await subscription.get()
            if still_authorized is not None and not still_authorized():
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(subscription)


def _publish_on_app_loop(
    bus: Broadcaster,
    event: dict[str, Any],
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    if loop is None:
        bus.publish(event)
        return
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is loop:
        bus.publish(event)
        return
    try:
        bus.publish_threadsafe(loop, event)
    except RuntimeError:
        bus.publish(event)


__all__ = ["_publish_on_app_loop", "_run_websocket"]
