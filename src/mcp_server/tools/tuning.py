"""Threshold-tuning + feedback read tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_tuning(client: DaemonClient) -> dict[str, Any]:
    """Currently active alert-tuning config (thresholds, dedupe, etc.)."""
    return await client.get_json("/control/tuning")


async def get_feedback(client: DaemonClient) -> dict[str, Any]:
    """All recorded operator feedback labels + summary counts."""
    return await client.get_json("/control/feedback")


__all__ = ["get_tuning", "get_feedback"]
