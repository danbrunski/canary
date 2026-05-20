"""Inbound bearer-token auth for the MCP HTTP transport.

FastMCP's auth machinery is OAuth-shaped, but the daemon install bakes a
single shared token into ``/etc/canary/control.token`` — the same token
used by the dashboard control plane, advertised in ``.mcp.json`` as
``Bearer ${CANARY_CONTROL_TOKEN}``. We wrap that into the
:class:`TokenVerifier` protocol so FastMCP can plug us into its
``RequireAuthMiddleware`` without dragging in scopes, refresh flows, or
an issuer.
"""

from __future__ import annotations

import hmac

from mcp.server.auth.provider import AccessToken, TokenVerifier


class StaticBearerVerifier(TokenVerifier):
    """Constant-time compare against a single shared bearer token.

    Returns a synthetic :class:`AccessToken` on match so FastMCP's
    ``RequireAuthMiddleware`` lets the request through. No scopes — the
    MCP server has its own per-tool allowlist (audit + write_tools) and
    doesn't need OAuth scope checks.
    """

    def __init__(self, expected_token: str) -> None:
        if not expected_token:
            raise ValueError("StaticBearerVerifier requires a non-empty token")
        self._expected = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not hmac.compare_digest(token, self._expected):
            return None
        return AccessToken(
            token=token,
            client_id="canary-control",
            scopes=[],
        )


__all__ = ["StaticBearerVerifier"]
