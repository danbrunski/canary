"""Tests for the MCP resource handlers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.mcp_server.client import DaemonClient
from src.mcp_server.resources import (
    read_anomalies_for_date,
    read_audit_log,
    read_behavior_models,
    read_config,
    read_session,
    read_status,
)


async def _fake_daemon(routes: dict[str, Any]) -> DaemonClient:
    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        if key not in routes:
            return httpx.Response(404, json={"detail": f"no fixture for {key}"})
        return httpx.Response(200, json=routes[key])

    client = DaemonClient("http://daemon")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://daemon",
        transport=httpx.MockTransport(handler),
    )
    return client


async def test_config_resource_returns_pretty_json() -> None:
    """``canary://config`` body is JSON-formatted text the client can pin."""
    client = await _fake_daemon({"GET /config": {"vehicle": "Xterra", "year": 2008}})
    body = await read_config(client)
    await client.aclose()
    parsed = json.loads(body)
    assert parsed["vehicle"] == "Xterra"
    assert "\n" in body  # pretty-printed, not single-line


async def test_status_and_behavior_resources_passthrough() -> None:
    """Status + behavior resources forward the daemon body and pretty-print."""
    client = await _fake_daemon(
        {
            "GET /status": {"free_mb": 100, "model_versions": {}},
            "GET /models/behavior": {"surfaces": [], "version": 2},
        }
    )
    status_body = await read_status(client)
    behavior_body = await read_behavior_models(client)
    await client.aclose()
    assert json.loads(status_body)["free_mb"] == 100
    assert json.loads(behavior_body)["version"] == 2


async def test_session_resource_calls_control_sessions_id() -> None:
    """``canary://sessions/{id}`` hits the dashboard's session-detail route."""
    client = await _fake_daemon(
        {"GET /control/sessions/S1": {"session": {"id": "S1", "chunks": 3}}}
    )
    body = await read_session(client, session_id="S1")
    await client.aclose()
    assert json.loads(body)["session"]["chunks"] == 3


def test_anomalies_resource_reads_jsonl_text(tmp_path: Path) -> None:
    """Returns the file contents verbatim so clients can stream events."""
    log = tmp_path / "anomalies-2026-05-18.jsonl"
    log.write_text('{"id":"a"}\n{"id":"b"}\n')
    body = read_anomalies_for_date(tmp_path, date="2026-05-18")
    assert body.count("\n") == 2


def test_anomalies_resource_missing_file_returns_empty(tmp_path: Path) -> None:
    """Missing day → empty body, not an exception (the day may have no events)."""
    assert read_anomalies_for_date(tmp_path, date="2026-05-19") == ""


def test_anomalies_resource_rejects_malformed_date(tmp_path: Path) -> None:
    """A bad ``date`` (defends against path traversal too) raises ValueError."""
    with pytest.raises(ValueError):
        read_anomalies_for_date(tmp_path, date="../../etc/passwd")


def test_audit_log_resource_tails_last_n_lines(tmp_path: Path) -> None:
    """Default ``tail=200`` keeps the resource bounded."""
    log = tmp_path / "mcp.jsonl"
    log.write_text("\n".join(f"line {i}" for i in range(500)) + "\n")
    body = read_audit_log(log, tail=10)
    lines = body.splitlines()
    assert len(lines) == 10
    assert lines[-1] == "line 499"


def test_audit_log_resource_missing_file_returns_empty(tmp_path: Path) -> None:
    """No audit log yet → empty string (a fresh install)."""
    assert read_audit_log(tmp_path / "absent.jsonl") == ""
