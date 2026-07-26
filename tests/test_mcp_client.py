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


async def test_get_bytes_returns_raw_body() -> None:
    """`get_bytes` skips JSON parsing (used for session export zips)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/control/sessions/x/export"
        return httpx.Response(200, content=b"zip-bytes")

    client = await _make_client(httpx.MockTransport(handler))
    payload = await client.get_bytes("/control/sessions/x/export")
    await client.aclose()
    assert payload == b"zip-bytes"


async def test_download_file_writes_full_body(tmp_path: Any) -> None:
    """A clean download (no pre-existing `.part`) writes the whole body."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Range" not in request.headers
        return httpx.Response(200, content=b"0123456789")

    client = await _make_client(httpx.MockTransport(handler))
    dest = tmp_path / "session.zip"
    result = await client.download_file("/control/sessions/x/export", dest)
    await client.aclose()

    assert result == dest
    assert dest.read_bytes() == b"0123456789"
    assert not dest.with_name(dest.name + ".part").exists()


async def test_download_file_resumes_from_partial_file(tmp_path: Any) -> None:
    """An existing `.part` file sends a `Range` request and appends the rest."""
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["range"] = request.headers.get("Range")
        return httpx.Response(
            206,
            content=b"56789",
            headers={"Content-Range": "bytes 5-9/10"},
        )

    dest = tmp_path / "session.zip"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"01234")

    client = await _make_client(httpx.MockTransport(handler))
    result = await client.download_file("/control/sessions/x/export", dest)
    await client.aclose()

    assert captured["range"] == "bytes=5-"
    assert result == dest
    assert dest.read_bytes() == b"0123456789"
    assert not part.exists()


async def test_download_file_restarts_when_range_not_satisfiable(tmp_path: Any) -> None:
    """A stale, too-large `.part` (server returns 416) is discarded and refetched."""
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("Range"))
        if request.headers.get("Range"):
            return httpx.Response(416)
        return httpx.Response(200, content=b"fresh-copy")

    dest = tmp_path / "session.zip"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"stale-partial-data-longer-than-the-real-file")

    client = await _make_client(httpx.MockTransport(handler))
    result = await client.download_file("/control/sessions/x/export", dest)
    await client.aclose()

    assert calls == ["bytes=44-", None]
    assert result == dest
    assert dest.read_bytes() == b"fresh-copy"


async def test_download_file_raises_daemon_error_on_http_failure(tmp_path: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    client = await _make_client(httpx.MockTransport(handler))
    dest = tmp_path / "session.zip"
    with pytest.raises(DaemonError):
        await client.download_file("/control/sessions/x/export", dest)
    await client.aclose()
    # The failed attempt leaves no completed file behind.
    assert not dest.exists()


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


async def test_empty_transport_error_names_the_exception_class() -> None:
    """``str(httpx.ReadTimeout(""))`` is empty — the DaemonError message must
    still say *why* (seen live: ``"GET /sessions/x/trip-summary: "`` with no
    cause when the compute-on-demand endpoint outran the 5 s default)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("")

    client = await _make_client(httpx.MockTransport(handler))
    with pytest.raises(DaemonError, match="ReadTimeout"):
        await client.get_json("/sessions/x/trip-summary")
    await client.aclose()


async def test_per_request_timeout_reaches_the_transport() -> None:
    """``get_json(..., timeout=...)`` overrides the client default so slow
    compute-on-demand endpoints (digest / trip-summary) get their own budget."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={})

    client = await _make_client(httpx.MockTransport(handler))
    await client.get_json("/control/sessions/x/digest", timeout=120.0)
    await client.aclose()
    assert seen["timeout"]["read"] == 120.0


async def test_async_context_manager_closes() -> None:
    """``async with`` form closes the underlying client cleanly."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with await _make_client(httpx.MockTransport(handler)) as client:
        await client.get_json("/status")
    # No explicit assertion: if aclose() didn't run, pytest-asyncio would flag
    # an unclosed connection at teardown.
