"""Tests for the MCP bearer-token verifier and its wiring into FastMCP."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.mcp_server.auth import StaticBearerVerifier
from src.mcp_server.config import MCPConfig
from src.mcp_server.server import _build_auth, build_server


def _cfg(tmp_path: Path, control_token: str | None) -> MCPConfig:
    return MCPConfig(
        daemon_url="http://127.0.0.1:8000",
        daemon_token=None,
        bind_host="0.0.0.0",
        bind_port=8001,
        control_token=control_token,
        audit_path=tmp_path / "audit.jsonl",
        storage_path=None,
        signals_path=None,
        log_fallback_path=None,
        daemon_unit="canary",
        anomalies_dir=None,
    )


def test_static_bearer_rejects_empty_token() -> None:
    """An empty expected token would silently accept everyone — refuse it."""
    with pytest.raises(ValueError):
        StaticBearerVerifier("")


async def test_static_bearer_accepts_match() -> None:
    """The verifier returns a synthetic AccessToken on a constant-time match."""
    verifier = StaticBearerVerifier("expected")
    access = await verifier.verify_token("expected")
    assert access is not None
    assert access.client_id == "canary-control"
    assert access.token == "expected"


@pytest.mark.parametrize("bad", ["", "expecte", "expected ", "EXPECTED", "wrong"])
async def test_static_bearer_rejects_mismatch(bad: str) -> None:
    """Anything that isn't byte-equal to the expected token returns None."""
    verifier = StaticBearerVerifier("expected")
    assert await verifier.verify_token(bad) is None


def test_build_auth_disabled_when_token_unset(tmp_path: Path) -> None:
    """No token → no auth settings, no verifier — HTTP transport stays open."""
    auth, verifier = _build_auth(_cfg(tmp_path, control_token=None))
    assert auth is None
    assert verifier is None


def test_build_auth_enabled_when_token_set(tmp_path: Path) -> None:
    """With a token, both auth settings and verifier are produced."""
    auth, verifier = _build_auth(_cfg(tmp_path, control_token="secret"))
    assert auth is not None
    assert verifier is not None
    # bind_host=0.0.0.0 should resolve to a loopback for the WWW-Authenticate URL
    assert "127.0.0.1" in str(auth.resource_server_url)
    assert "8001" in str(auth.resource_server_url)


def test_build_server_wires_token_verifier_when_token_set(tmp_path: Path) -> None:
    """Round-trip: build_server with a token leaves FastMCP with a verifier."""
    server = build_server(_cfg(tmp_path, control_token="secret"))
    assert server._token_verifier is not None
    assert server.settings.auth is not None


def test_build_server_skips_auth_when_token_unset(tmp_path: Path) -> None:
    """Without a token, FastMCP stays unauthenticated (dev / tunnelled mode)."""
    server = build_server(_cfg(tmp_path, control_token=None))
    assert server._token_verifier is None
    assert server.settings.auth is None
