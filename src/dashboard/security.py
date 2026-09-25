"""Authentication helpers for dashboard control routes."""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Header, HTTPException, Request, WebSocket, WebSocketException, status
from starlette.requests import HTTPConnection

#: Attribute set on every shared auth dependency, naming its rule. The route
#: snapshot (``tests/test_route_table_snapshot.py``) reads it to prove every
#: ``/control/*`` route carries the one shared dependency, not a copy.
AUTH_MARKER = "canary_auth"


def _client_is_loopback(request: HTTPConnection) -> bool:
    """True iff the connection's socket peer is a loopback address."""
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _require_control_auth(
    control_token: str | None,
    authorization: str | None,
    *,
    client_is_loopback: bool = False,
) -> None:
    """Bearer-token guard for mutating control-plane routes."""
    if control_token is None or control_token == "":
        if client_is_loopback:
            return
        raise HTTPException(status_code=503, detail="control API token is not configured")
    presented = ""
    if authorization is not None and authorization.startswith("Bearer "):
        presented = authorization[len("Bearer ") :]
    if not secrets.compare_digest(presented, control_token):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def control_auth_dependency(control_token: str | None) -> Callable[..., None]:
    """The dashboard's one control-plane auth dependency (R03.4).

    Bearer token compared in constant time; open to loopback peers only while
    no token is configured. Four route modules used to carry their own
    identical closure; every guarded route now depends on this one.
    """

    def require_control_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        _require_control_auth(
            control_token,
            authorization,
            client_is_loopback=_client_is_loopback(request),
        )

    setattr(require_control_auth, AUTH_MARKER, "control")
    return require_control_auth


def websocket_control_auth_dependency(
    control_token: str | None,
) -> Callable[[WebSocket], Awaitable[None]]:
    """The same rule for ``/ws/control``, whose token rides the query string.

    Browsers cannot set headers on a WebSocket handshake, so the token is
    ``?token=``. It used to be compared with ``!=`` and had no loopback
    exemption; it now goes through :func:`_require_control_auth` like every
    HTTP control route. A refusal closes the handshake with 1008 (policy
    violation), as before.
    """

    async def require_control_auth(websocket: WebSocket) -> None:
        token = websocket.query_params.get("token")
        try:
            _require_control_auth(
                control_token,
                None if token is None else f"Bearer {token}",
                client_is_loopback=_client_is_loopback(websocket),
            )
        except HTTPException as exc:
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION) from exc

    setattr(require_control_auth, AUTH_MARKER, "control")
    return require_control_auth


__all__ = [
    "AUTH_MARKER",
    "_client_is_loopback",
    "_require_control_auth",
    "control_auth_dependency",
    "websocket_control_auth_dependency",
]
