"""Shared websocket/broadcast helpers for the dashboard app."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from src.dashboard.broadcast import Broadcaster


async def _run_websocket(websocket: WebSocket, bus: Broadcaster) -> None:
    """Hold one WebSocket open, pumping messages from ``bus`` until disconnect."""
    await websocket.accept()
    subscription = bus.subscribe()
    try:
        while True:
            message = await subscription.get()
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
