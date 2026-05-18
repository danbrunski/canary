"""Decode-metadata read tools — bindings, DIDs, OBD coverage."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_bindings(client: DaemonClient) -> dict[str, Any]:
    """Decoded CAN signal metadata — ``data/bindings.yaml`` as JSON.

    The daemon re-reads on every request, so an edit during reverse
    engineering shows up here without restart.
    """
    return await client.get_json("/bindings")


async def get_dids(client: DaemonClient) -> dict[str, Any]:
    """Nissan Mode-22 DID catalog (schema_version + per-DID entries).

    Returns an empty doc if the catalog file isn't present.
    """
    return await client.get_json("/dids")


async def get_obd_coverage(client: DaemonClient) -> dict[str, Any]:
    """Honesty surface for OBD coverage.

    Lists which standard PIDs the ECU supports, which CANary decodes, and
    which are gaps — useful when debugging "why don't I see X?"
    """
    return await client.get_json("/obd/coverage")


__all__ = ["get_bindings", "get_dids", "get_obd_coverage"]
