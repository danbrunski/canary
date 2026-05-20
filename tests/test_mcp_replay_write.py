"""Tests for the replay MCP write tools (start_replay / stop_replay)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools import replay as replay_tool


async def _fake_daemon(handler: Any) -> DaemonClient:
    client = DaemonClient("http://daemon")
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        base_url="http://daemon",
        transport=httpx.MockTransport(handler),
    )
    return client


async def test_start_replay_posts_body_and_audits(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.read())
        return httpx.Response(
            201,
            json={
                "replay": {
                    "running": True,
                    "replay_id": "2026-05-19T180000",
                    "source_session_id": "2026-05-18T120000",
                }
            },
        )

    client = await _fake_daemon(handler)
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    result = await replay_tool.start_replay(
        client,
        audit,
        session_id="2026-05-18T120000",
        speed=2.0,
        loop=False,
    )
    await client.aclose()

    assert captured["path"] == "/control/replay"
    assert captured["body"] == {"session_id": "2026-05-18T120000", "speed": 2.0, "loop": False}
    assert result["replay"]["running"] is True
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.start_replay"
    assert entry["result"] == "ok"
    assert entry["payload"]["actor"] == "mcp"


async def test_stop_replay_audits_response(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"replay": {"running": False}})

    client = await _fake_daemon(handler)
    audit = MCPAuditLogger(tmp_path / "audit.jsonl")
    result = await replay_tool.stop_replay(client, audit)
    await client.aclose()

    assert result["replay"]["running"] is False
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[0])
    assert entry["action"] == "mcp.stop_replay"
    assert entry["result"] == "ok"
