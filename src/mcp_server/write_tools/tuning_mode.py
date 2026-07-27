"""Focused tuning poll-profile write tool."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post


async def set_tuning_mode(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    enabled: bool,
    channels: list[str] | None = None,
) -> dict[str, Any]:
    """Arm/disarm the focused high-rate tuning poll profile.

    While armed the Mode 01 rotation is restricted to a small tuning set
    (RPM, load, MAF, timing, TPS, IAT, lambda B1/B2, STFT/LTFT B1/B2,
    commanded equivalence ratio) and the Mode-22 DID windows are suspended,
    so each channel gets a far larger slice of the same bus budget.

    Use for a tuning capture where you need enough samples in a single
    rpm x load cell to draw a conclusion. Normal polling lands each channel
    at 0.06-0.5 Hz, which yields roughly 10 samples per cell over a whole
    drive -- not enough to say anything.

    Everything outside the tuning set stops updating while armed. That is
    expected, not a fault; `get_status` reports `tuning_mode.active`.
    Disarm when the capture is done -- disabling also restores the default
    channel set, so a narrow one-off cannot persist into the next capture.
    """
    body: dict[str, Any] = {"enabled": enabled}
    if channels is not None:
        body["channels"] = channels
    return await audited_post(
        client, audit, tool="set_tuning_mode", path="/control/tuning-mode", body=body
    )


__all__ = ["set_tuning_mode"]
