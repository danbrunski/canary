"""Ad-hoc raw CAN activity capture (``can_activity`` tool)."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post

#: Extra headroom over the capture duration for the HTTP round trip itself.
_HTTP_TIMEOUT_MARGIN_SECONDS = 5.0


async def can_activity(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    seconds: float,
    ids: list[int] | None = None,
) -> dict[str, Any]:
    """Capture N seconds of raw CAN traffic; per-ID frame rate + which byte
    positions changed.

    The RE workhorse: call it bracketing a physical action (press the brake,
    flip a switch — tag what you *did*, per AGENTS.md's "tag physical
    actions, not screen selections" convention) and `changed_byte_indices`
    on each id is the answer to "which ID carries that signal," without an
    offline candump-and-diff round trip. Pure listen — no CAN frames are
    transmitted, so this has no interaction with passive mode.

    `ids` restricts the capture to those arbitration ids (useful for a
    follow-up call once a wide capture has narrowed the candidates); omit
    for the whole bus. `seconds` is capped at 30 server-side.
    """
    body: dict[str, Any] = {"seconds": seconds}
    if ids is not None:
        body["ids"] = ids
    return await audited_post(
        client,
        audit,
        tool="can_activity",
        path="/control/can_activity",
        body=body,
        timeout=seconds + _HTTP_TIMEOUT_MARGIN_SECONDS,
    )


__all__ = ["can_activity"]
