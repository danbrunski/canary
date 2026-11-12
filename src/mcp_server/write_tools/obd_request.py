"""Ad-hoc OBD/UDS request (``obd_request`` tool)."""

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
    identifier_bytes: int | None = None,
    data_hex: str | None = None,
    response_id: int | None = None,
) -> dict[str, Any]:
    """One-shot OBD/UDS request against the live ECU(s).

    ``service`` takes any service byte — read, write and actuation alike.
    Nothing filters it, so a Mode 04 or a UDS 0x2E/0x2F goes out exactly as
    asked; the caller decides what is appropriate to send. For the ordinary
    read services this just lets you ask for one specific service/identifier
    combination immediately instead of waiting for the poll scheduler's
    cadence (Mode 06 and DTC sit on a 5-minute tier).

    ``identifier`` is the PID/DID/MID/InfoType byte(s) — omit it for the
    no-identifier services (Mode 03/07/0A). ``header`` overrides the default
    functional request id (``0x7DF``) for a physically-addressed request,
    e.g. ``0x7E0`` for the engine ECM, or another module's tester address —
    set it to reach a module standard Mode 01 polling never does (ABS,
    TCM, ...).

    By default the identifier's width follows its value (one byte up to
    0xFF, two above). ``identifier_bytes`` (1 or 2) fixes it: ``1`` for a
    KWP ReadDataByLocalIdentifier (``service=0x21, identifier=0x1F``), ``2``
    for a DID below 0x100 (``22 00 05``). ``data_hex`` replaces the
    identifier with the exact bytes after the service byte (up to 6), for a
    service that takes no identifier or an arbitrary argument. A module
    whose reply id is not ``header + 8`` needs ``response_id`` (the BCM:
    ``header=0x745, response_id=0x765``), or its reply is never heard. An
    identifier too wide for ``identifier_bytes`` is refused, not truncated.
    ``request_hex`` in the result is what went on the wire.

    Returns ``responded: false`` (not an error) when nothing answers in
    time — a real "unsupported" for this ECU is indistinguishable on the
    wire from a request that simply timed out. When another tool owns a
    diagnostic session with a module, arm ``set_passive_mode`` first so this
    request's own transmit can't contend with it.
    """
    body: dict[str, Any] = {"service": service}
    if identifier is not None:
        body["identifier"] = identifier
    if identifier_bytes is not None:
        body["identifier_bytes"] = identifier_bytes
    if data_hex is not None:
        body["data_hex"] = data_hex
    if header is not None:
        body["header"] = header
    if response_id is not None:
        body["response_id"] = response_id
    if timeout_seconds is not None:
        body["timeout_seconds"] = timeout_seconds
    timeout = None if timeout_seconds is None else timeout_seconds + _HTTP_TIMEOUT_MARGIN_SECONDS
    return await audited_post(
        client, audit, tool="obd_request", path="/control/obd_request", body=body, timeout=timeout
    )


__all__ = ["obd_request"]
