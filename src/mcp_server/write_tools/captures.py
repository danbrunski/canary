"""Capture-control write tools — labeled windows on the current telemetry session."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post


async def start_capture(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    purpose: str,
    protocol: str = "drive",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Begin a labeled capture window on the current telemetry session.

    A capture is *not* a second recorder — the daemon already records
    continuously. ``start_capture`` writes a start marker to ``tags.jsonl``
    and updates ``control.json`` so the segment is retrievable later.

    Parameters
    ----------
    purpose:
        Free-text label for what this capture is for ("highway test drive",
        "post-fuel-pump replacement baseline").
    protocol:
        ``"drive"`` (default), ``"idle"``, ``"diagnostic"``, etc. — the
        operator's intent for downstream filtering.
    metadata:
        Arbitrary JSON; written into the capture's metadata block.
    """
    body: dict[str, Any] = {"purpose": purpose, "protocol": protocol}
    if metadata:
        body["metadata"] = metadata
    return await audited_post(
        client, audit, tool="start_capture", path="/control/captures", body=body
    )


async def tag_current_capture(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    label: str,
    fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drop a tag onto the active capture mid-drive.

    Tags are append-only events in ``tags.jsonl`` — used to mark transitions
    ("entered city", "started towing") so downstream analysis can slice.
    """
    body: dict[str, Any] = {"label": label}
    if fields:
        body["fields"] = fields
    return await audited_post(
        client,
        audit,
        tool="tag_current_capture",
        path="/control/captures/current/tags",
        body=body,
    )


async def stop_capture(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Close the active capture. ``reason`` is recorded for later review."""
    body: dict[str, Any] = {}
    if reason is not None:
        body["reason"] = reason
    return await audited_post(
        client,
        audit,
        tool="stop_capture",
        path="/control/captures/current/stop",
        body=body,
    )


__all__ = [
    "start_capture",
    "tag_current_capture",
    "stop_capture",
]
