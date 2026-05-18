"""Authentication helpers for dashboard control routes."""

from __future__ import annotations

import ipaddress
import secrets

from fastapi import HTTPException, Request


def _client_is_loopback(request: Request) -> bool:
    """True iff the request's socket peer is a loopback address."""
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


__all__ = ["_client_is_loopback", "_require_control_auth"]
