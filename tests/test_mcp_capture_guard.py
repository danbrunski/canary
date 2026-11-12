"""R29.1: the local whole-corpus MCP tools refuse while the daemon is capturing.

``query_telemetry``, ``get_behavior_deviations``, ``search_anomaly_log`` (and the
two tuning tools) run inside ``canary-mcp``, on the Pi beside the collector, so
they ask the daemon whether a capture is recording
(``src/mcp_server/capture_guard.py``). A connection that cannot be made is
fail-open (the laptop with a pulled corpus); anything that connects and then does
not give a readable answer (a stall, an HTTP error, a body that is not JSON) is
fail-closed.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from src.mcp_server import capture_guard
from src.mcp_server import server as server_module
from src.mcp_server.capture_guard import capture_refusal
from src.mcp_server.client import DaemonClient
from src.mcp_server.config import MCPConfig
from src.storage.parquet_writer import write_records_to_parquet
from src.storage.telemetry_query import TelemetryQuery
from src.telemetry import TelemetryRecord

LOCAL_TOOLS = (
    "query_telemetry",
    "get_behavior_deviations",
    "search_anomaly_log",
    "get_delivery_error",
    "compare_tuning_sessions",
)
SESSION = "2026-07-19T120000"


def _refused_url() -> str:
    """A loopback port nothing listens on: the connection is refused at once."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def _client(respond: Any) -> DaemonClient:
    client = DaemonClient("http://daemon")
    client._client = httpx.AsyncClient(
        base_url="http://daemon", transport=httpx.MockTransport(respond)
    )
    return client


def _status(*, active: bool, name: str = "tuning") -> Any:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status"
        assert request.url.params["view"] == "compact"  # the 4 ms view, never the 1-2 s one
        return httpx.Response(200, json={"capture_profile": {"name": name, "active": active}})

    return respond


async def test_an_active_capture_is_refused_by_name_with_the_laptop_path() -> None:
    refusal = await capture_refusal(
        _client(_status(active=True)), tool="query_telemetry", storage_path=Path("/data/canary")
    )
    assert refusal is not None
    assert refusal["ok"] is False
    assert (refusal["error"], refusal["reason"]) == ("analysis_deferred", "capture_active")
    assert refusal["profile"] == "tuning"
    assert refusal["tool"] == "query_telemetry"
    assert "'tuning' is recording" in refusal["message"]
    assert "tar -cf - -C /data/canary" in refusal["laptop_path"]


async def test_no_capture_runs_the_tool() -> None:
    assert await capture_refusal(_client(_status(active=False, name="default")), tool="t") is None


async def test_no_daemon_answering_is_fail_open_for_the_laptop() -> None:
    """canary-mcp on the laptop over a pulled corpus has no daemon: connection
    refused must not refuse the analysis the laptop path exists to run."""
    client = DaemonClient(_refused_url())
    try:
        assert await capture_refusal(client, tool="query_telemetry") is None
    finally:
        await client.aclose()


async def test_a_connect_timeout_is_fail_open_like_a_refused_connection() -> None:
    """The connect itself timing out (a laptop on another network, the Pi's
    address black-holed) is still "nothing to connect to"."""

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=request)

    assert await capture_refusal(_client(unreachable), tool="query_telemetry") is None


async def test_a_daemon_that_stalls_past_the_probe_is_fail_closed() -> None:
    """A connection that was made and then goes quiet is the busiest daemon, the
    one a capture is recording on. It is refused, never waved through."""

    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    refusal = await capture_refusal(_client(stall), tool="query_telemetry")
    assert refusal is not None
    assert (refusal["ok"], refusal["error"]) == (False, "capture_state_unknown")
    assert refusal["tool"] == "query_telemetry"
    assert "did not answer" in refusal["message"]


async def test_a_real_socket_that_accepts_and_never_answers_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same through real httpx: the kernel completes the connect on a
    listening socket nobody ``accept()``s, then the read times out."""
    monkeypatch.setattr(capture_guard, "CAPTURE_PROBE_TIMEOUT_SECONDS", 0.2)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client = DaemonClient(f"http://127.0.0.1:{listener.getsockname()[1]}")
        try:
            refusal = await capture_refusal(client, tool="get_delivery_error")
        finally:
            await client.aclose()
    assert refusal is not None
    assert refusal["error"] == "capture_state_unknown"


@pytest.mark.parametrize(
    "respond",
    [
        lambda request: httpx.Response(500, json={"detail": "boom"}),
        lambda request: httpx.Response(200, json={"vehicle_state": {}}),
        lambda request: httpx.Response(200, json={"capture_profile": {"name": "x"}}),
        lambda request: httpx.Response(200, json={"capture_profile": {"active": "yes"}}),
        lambda request: httpx.Response(200, json=["not", "an", "object"]),
        lambda request: httpx.Response(200, content=b"<html>502 from a proxy</html>"),
        lambda request: httpx.Response(200, content=b'{"capture_profile": {"act'),
    ],
    ids=[
        "http_500",
        "no_capture_profile",
        "no_active",
        "active_not_bool",
        "not_an_object",
        "body_not_json",
        "body_truncated",
    ],
)
async def test_an_answer_that_cannot_be_read_is_fail_closed(respond: Any) -> None:
    refusal = await capture_refusal(_client(respond), tool="search_anomaly_log")
    assert refusal is not None
    assert (refusal["ok"], refusal["error"]) == (False, "capture_state_unknown")


def _mcp(tmp_path: Path, client: DaemonClient) -> FastMCP:
    storage = tmp_path / "storage"
    write_records_to_parquet(
        [TelemetryRecord.obd(1_779_400_000.0 + i, "RPM", 2000.0, "rpm") for i in range(5)],
        storage / SESSION,
        max_rows=10_000,
    )
    anomalies = tmp_path / "anomalies"
    anomalies.mkdir()
    config_path = tmp_path / "canary.yaml"
    config_path.write_text("{}\n")  # only its existence registers the tools
    cfg = MCPConfig(
        daemon_url="http://daemon",
        daemon_token=None,
        bind_host="127.0.0.1",
        bind_port=8001,
        control_token=None,
        audit_path=tmp_path / "audit.jsonl",
        storage_path=storage,
        signals_path=None,
        log_fallback_path=None,
        daemon_unit="canary",
        anomalies_dir=anomalies,
        config_path=config_path,
    )
    mcp = FastMCP(name="t")
    server_module._register_read_tools(mcp, client, TelemetryQuery(storage), cfg)
    return mcp


_CALLS: dict[str, dict[str, Any]] = {
    "query_telemetry": {"sql": "SELECT count(*) AS n FROM telemetry"},
    "get_behavior_deviations": {"session": SESSION},
    "search_anomaly_log": {},
    "get_delivery_error": {"session": SESSION},
    "compare_tuning_sessions": {"session": SESSION, "against": SESSION, "value": "obd:RPM"},
}


async def _structured(mcp: FastMCP, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The tool's structured result (every tool here returns a dict)."""
    result = await mcp.call_tool(name, arguments)
    assert isinstance(result, tuple)
    return cast("dict[str, Any]", result[1])


async def test_every_local_tool_is_refused_while_a_capture_records(tmp_path: Path) -> None:
    mcp = _mcp(tmp_path, _client(_status(active=True)))
    registered = {tool.name for tool in await mcp.list_tools()}
    assert set(LOCAL_TOOLS) <= registered
    for name in LOCAL_TOOLS:
        structured = await _structured(mcp, name, _CALLS[name])
        assert structured["error"] == "analysis_deferred", name
        assert structured["tool"] == name
        assert structured["profile"] == "tuning"


async def test_the_local_tools_still_run_with_no_capture_and_with_no_daemon(
    tmp_path: Path,
) -> None:
    mcp = _mcp(tmp_path, _client(_status(active=False, name="default")))
    rows = await _structured(mcp, "query_telemetry", _CALLS["query_telemetry"])
    assert "error" not in rows and rows["rows"][0]["n"] == 5

    unreachable = DaemonClient(_refused_url())
    try:
        mcp = _mcp(tmp_path / "laptop", unreachable)
        rows = await _structured(mcp, "query_telemetry", _CALLS["query_telemetry"])
        assert "error" not in rows and rows["rows"][0]["n"] == 5
        found = await _structured(mcp, "search_anomaly_log", {})
        assert found.get("error") is None
    finally:
        await unreachable.aclose()
