"""Behavior-model state read tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_behavior_live(
    client: DaemonClient, *, include_series: bool = False
) -> dict[str, Any]:
    """Live ``you-are-here`` overlay for the behavior surfaces.

    Returns per-model ``expected`` / ``actual`` / ``deviation`` / ``severity``
    for the most recent tick. ``include_series=True`` also returns each
    model's rolling trail (~4 min at 1 Hz) — the default is ``False`` because
    with enough registered surfaces the full trail can exceed the MCP
    tool-result size cap; pass ``True`` only when you actually need the
    trajectory (e.g. plotting a Δ sparkline), not for a one-shot status check.
    Equivalent to ``GET /models/behavior/live?series=...`` — see
    :mod:`src.dashboard.behavior_live` for the schema.
    """
    return await client.get_json("/models/behavior/live", params={"series": include_series})


async def get_behavior_models(client: DaemonClient) -> dict[str, Any]:
    """Static behavior surfaces (the learned "normal" maps).

    1D/2D surfaces over operating-mode-segmented data. Use this with
    ``get_behavior_live`` to compare "where the engine should be" with
    "where it is right now."
    """
    return await client.get_json("/models/behavior")


__all__ = ["get_behavior_live", "get_behavior_models"]
