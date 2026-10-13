"""Authentication helpers for dashboard control routes."""

from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Header, HTTPException, Request, WebSocket, WebSocketException, status
from starlette.requests import HTTPConnection

from src.control.paired_devices import PairedDevices

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


def _paired_devices(connection: HTTPConnection) -> PairedDevices | None:
    """The app's paired-device store, if it has one (``app.state``)."""
    app = connection.scope.get("app")
    devices = getattr(getattr(app, "state", None), "paired_devices", None)
    return devices if isinstance(devices, PairedDevices) else None


def _require_control_auth(
    control_token: str | None,
    authorization: str | None,
    *,
    client_is_loopback: bool = False,
    devices: PairedDevices | None = None,
) -> None:
    """Bearer-token guard for mutating control-plane routes.

    Accepts the shared control token or a paired device's own token
    (``src.control.paired_devices``).
    """
    if (
        devices is not None
        and authorization is not None
        and authorization.startswith("Bearer ")
        and devices.authenticate(authorization[len("Bearer ") :]) is not None
    ):
        return
    if control_token is None or control_token == "":
        if client_is_loopback:
            return
        raise HTTPException(status_code=503, detail="control API token is not configured")
    presented = ""
    if authorization is not None and authorization.startswith("Bearer "):
        presented = authorization[len("Bearer ") :]
    # Bytes, not str: compare_digest raises TypeError on non-ASCII str, which
    # turned a stray latin-1 header into a 500 instead of a 401.
    if not secrets.compare_digest(presented.encode("utf-8"), control_token.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def control_auth_dependency(control_token: str | None) -> Callable[..., None]:
    """The dashboard's one control-plane auth dependency (R03.4).

    Bearer token compared in constant time; open to loopback peers only while
    no token is configured. Four route modules used to carry their own
    identical closure; every guarded route now depends on this one. Paired
    device tokens are found through the request's app, so no route has to
    pass the store along.
    """

    def require_control_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        _require_control_auth(
            control_token,
            authorization,
            client_is_loopback=_client_is_loopback(request),
            devices=_paired_devices(request),
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
                devices=_paired_devices(websocket),
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
