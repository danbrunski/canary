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
    fields: dict[str, Any] | None = None,
    vehicle_state: str | None = None,
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
    fields:
        Arbitrary JSON recorded alongside the capture — tune revision, ROM
        id, what the run is meant to prove. Named ``fields`` to match
        ``CaptureStartRequest`` and :func:`tag_current_capture`; it was
        previously sent as ``metadata``, which the daemon rejects outright
        (``extra="forbid"``), so the parameter had never worked.
    vehicle_state:
        Operator's note of engine state at capture start.
    """
    body: dict[str, Any] = {"purpose": purpose, "protocol": protocol}
    if fields:
        body["fields"] = fields
    if vehicle_state is not None:
        body["vehicle_state"] = vehicle_state
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

    Raises :class:`~src.mcp_server.client.DaemonError` when no capture window
    is open (HTTP 409) — the tag was *not* recorded. The window is in-memory
    only, so a daemon restart ends it while recording continues; the refusal
    says so. Never returns a success with a null tag: a lost tag cannot be
    recovered after the drive, so it has to fail loudly at the call.
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
    """Close the active capture. ``reason`` is recorded for later review.

    Raises :class:`~src.mcp_server.client.DaemonError` when no window is open
    (HTTP 409), naming the restart that usually dropped it, rather than
    reporting a stop that did not happen.
    """
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
