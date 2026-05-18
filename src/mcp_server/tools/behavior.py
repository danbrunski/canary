"""Behavior-model state read tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_behavior_live(client: DaemonClient) -> dict[str, Any]:
    """Live ``you-are-here`` overlay for the behavior surfaces.

    Returns per-model ``expected`` / ``actual`` / ``deviation`` / ``severity``
    for the most recent tick, plus a short rolling trail (~4 min at 1 Hz).
    Empty (not an error) when the daemon hasn't wired a live-inference task.
    Equivalent to ``GET /models/behavior/live`` — see
    :mod:`src.dashboard.behavior_live` for the schema.
    """
    return await client.get_json("/models/behavior/live")


async def get_behavior_models(client: DaemonClient) -> dict[str, Any]:
    """Static behavior surfaces (the learned "normal" maps).

    1D/2D surfaces over operating-mode-segmented data. Use this with
    ``get_behavior_live`` to compare "where the engine should be" with
    "where it is right now."
    """
    return await client.get_json("/models/behavior")


__all__ = ["get_behavior_live", "get_behavior_models"]
