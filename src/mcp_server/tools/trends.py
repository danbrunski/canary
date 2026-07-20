"""Session-fingerprint drift trend read tool (Task 4.5)."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_trends(client: DaemonClient) -> dict[str, Any]:
    """Per-stat drift trend over the fingerprint history: latest value,
    reference band, Mann-Kendall trend verdict, CUSUM alarm state.

    ``available: false`` (with a ``reason``) when no reference epoch has
    been frozen yet (``scripts/fingerprint.py freeze``) — there's nothing to
    trend against.
    """
    return await client.get_json("/control/trends")


__all__ = ["get_trends"]
