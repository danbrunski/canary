"""Ad-hoc read-only OBD/UDS request (``obd_request`` tool)."""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post

#: Extra headroom over the daemon-side request timeout for the HTTP round
#: trip itself (bus open/close, request marshalling) — keeps the MCP client
#: from timing out just before the daemon would have answered.
_HTTP_TIMEOUT_MARGIN_SECONDS = 5.0


async def obd_request(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    service: int,
    identifier: int | None = None,
    header: int | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """One-shot read-only OBD/UDS request against the live ECU(s).

    Whitelisted to Mode 01/02/03/06/07/09/0A/22 — the diagnostic services
    CANary already transmits on every drive as part of its normal poll
    scheduler; this just lets you ask for one specific service/identifier
    combination immediately instead of waiting for the scheduler's cadence
    (Mode 06 and DTC sit on a 5-minute tier). Enforced by the daemon
    (``CanObdClient.query``'s allowlist), not just documented here.

    ``identifier`` is the PID/DID/MID/InfoType byte(s) — omit it for the
    no-identifier services (Mode 03/07/0A). ``header`` overrides the default
    functional request id (``0x7DF``) for a physically-addressed request,
    e.g. ``0x7E0`` for the engine ECM, or another module's tester address —
    set it to reach a module standard Mode 01 polling never does (ABS,
    TCM, ...).

    Returns ``responded: false`` (not an error) when nothing answers in
    time — a real "unsupported" for this ECU is indistinguishable on the
    wire from a request that simply timed out. When another tool owns a
    diagnostic session with a module, arm ``set_passive_mode`` first so this
    request's own transmit can't contend with it.
    """
    body: dict[str, Any] = {"service": service}
    if identifier is not None:
        body["identifier"] = identifier
    if header is not None:
        body["header"] = header
    if timeout_seconds is not None:
        body["timeout_seconds"] = timeout_seconds
    timeout = None if timeout_seconds is None else timeout_seconds + _HTTP_TIMEOUT_MARGIN_SECONDS
    return await audited_post(
        client, audit, tool="obd_request", path="/control/obd_request", body=body, timeout=timeout
    )


__all__ = ["obd_request"]
