"""Capture-profile catalog read tool."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def list_capture_profiles(client: DaemonClient) -> dict[str, Any]:
    return await client.get_json("/control/capture/profiles")


__all__ = ["list_capture_profiles"]
