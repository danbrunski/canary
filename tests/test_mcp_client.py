"""Tests for the async daemon HTTP client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.mcp_server.client import DaemonClient, DaemonError


async def _make_client(handler: httpx.MockTransport, token: str | None = None) -> DaemonClient:
    """Construct a :class:`DaemonClient` whose AsyncClient uses ``handler``."""
    client = DaemonClient("http://daemon", token=token)
    # Replace the internal AsyncClient with one wired to MockTransport so
    # we don't need a live server.
    await client._client.aclose()
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    client._client = httpx.AsyncClient(
        base_url="http://daemon",
        headers=headers,
        transport=handler,
    )
    return client


async def test_get_json_returns_parsed_body() -> None:
    """200 responses parse as JSON and bypass the error path."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/status"
        return httpx.Response(200, json={"ok": True, "free_mb": 123.4})

    client = await _make_client(httpx.MockTransport(handler))
    payload = await client.get_json("/status")
    await client.aclose()
    assert payload == {"ok": True, "free_mb": 123.4}


async def test_get_json_forwards_bearer_token() -> None:
    """When constructed with a token, every request carries ``Authorization``."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    client = await _make_client(httpx.MockTransport(handler), token="secret")
    await client.get_json("/status")
    await client.aclose()
    assert captured["auth"] == "Bearer secret"


async def test_post_json_sends_body() -> None:
    """POST bodies are serialised and the response is returned unchanged."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.read().decode()
        return httpx.Response(201, json={"id": "abc"})

    client = await _make_client(httpx.MockTransport(handler))
    result = await client.post_json("/control/captures", {"purpose": "test"})
    await client.aclose()
    assert "purpose" in captured["json"]
    assert result == {"id": "abc"}


async def test_error_response_raises_daemon_error_with_detail() -> None:
    """4xx/5xx responses raise :class:`DaemonError` with the JSON detail attached."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad since"})

    client = await _make_client(httpx.MockTransport(handler))
    with pytest.raises(DaemonError) as excinfo:
        await client.get_json("/anomalies", {"since": "garbage"})
    await client.aclose()
    assert excinfo.value.status == 400
    assert excinfo.value.detail == {"detail": "bad since"}


async def test_network_failure_raises_daemon_error() -> None:
    """Connection errors surface as :class:`DaemonError`, not raw httpx errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("daemon offline")

    client = await _make_client(httpx.MockTransport(handler))
    with pytest.raises(DaemonError):
        await client.get_json("/status")
    await client.aclose()


async def test_async_context_manager_closes() -> None:
    """``async with`` form closes the underlying client cleanly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with await _make_client(httpx.MockTransport(handler)) as client:
        await client.get_json("/status")
    # No explicit assertion: if aclose() didn't run, pytest-asyncio would flag
    # an unclosed connection at teardown.
