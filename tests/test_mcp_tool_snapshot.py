"""Every MCP tool's name, description and schemas, pinned (D03.23).

``src/mcp_server/server.py`` is the assembly layer between FastMCP and the
``tools/`` / ``write_tools/`` delegates. Refactoring it has one silent failure
mode: FastMCP infers each tool's input schema and description from whatever
function it is handed, so a changed signature, a leaked ``client``/``audit``
parameter, a lost docstring or a dropped registration all still "work" — the
server starts, clients just see a different contract. This pins the whole
listing, built with every conditional tool's precondition satisfied.

Regenerate after an *intended* contract change::

    CANARY_UPDATE_MCP_SNAPSHOT=1 .venv/bin/python -m pytest -n0 tests/test_mcp_tool_snapshot.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from src.mcp_server.config import MCPConfig
from src.mcp_server.server import build_server

SNAPSHOT = Path(__file__).parent / "fixtures" / "mcp_tool_snapshot.json"


def _full_config(tmp_path: Path) -> MCPConfig:
    """Satisfy every conditional registration (storage, signals, config)."""
    storage = tmp_path / "storage"
    storage.mkdir()
    signals = tmp_path / "signals.json"
    signals.write_text("{}")
    config_file = tmp_path / "canary.yaml"
    config_file.write_text("collection:\n  storage_path: /var/canary/sessions\n")
    return MCPConfig(
        daemon_url="http://127.0.0.1:8000",
        daemon_token=None,
        bind_host="127.0.0.1",
        bind_port=8001,
        control_token=None,
        audit_path=tmp_path / "audit.jsonl",
        storage_path=storage,
        signals_path=signals,
        log_fallback_path=None,
        daemon_unit="canary",
        anomalies_dir=tmp_path / "anomalies",
        config_path=config_file,
        mcp_stateless=False,
    )


async def _listing(tmp_path: Path) -> dict[str, Any]:
    server = build_server(_full_config(tmp_path))
    tools = await server.list_tools()
    return {
        tool.name: tool.model_dump(mode="json", exclude_none=True)
        for tool in sorted(tools, key=lambda t: t.name)
    }


async def test_tool_listing_matches_snapshot(tmp_path: Path) -> None:
    listing = await _listing(tmp_path)
    if os.environ.get("CANARY_UPDATE_MCP_SNAPSHOT"):
        SNAPSHOT.write_text(json.dumps(listing, indent=2, sort_keys=True) + "\n")
    expected = json.loads(SNAPSHOT.read_text())
    assert sorted(listing) == sorted(expected), "tool registrations added or dropped"
    for name, tool in expected.items():
        assert listing[name] == tool, f"tool {name!r} contract changed"


async def test_no_tool_exposes_injected_dependencies(tmp_path: Path) -> None:
    """``client``/``audit``/paths are injected by the assembly layer; a
    delegate's own signature leaking through would expose them as inputs."""
    listing = await _listing(tmp_path)
    injected = {"client", "audit", "query_runner", "config_path", "signals_path"}
    for name, tool in listing.items():
        params = set(tool["inputSchema"].get("properties", {}))
        assert not params & injected, (name, params & injected)
        assert tool.get("description"), f"{name} has no description"


async def test_delegated_tool_forwards_injected_client_and_audit(tmp_path: Path) -> None:
    """A table row reaches its delegate with ``client``/``audit`` bound and the
    tool inputs as keywords — through FastMCP's own call path."""
    from src.mcp_server import server as server_module

    seen: dict[str, Any] = {}

    async def fake_pin(client: Any, audit: Any, *, session_id: str) -> dict[str, Any]:
        seen.update(client=client, audit=audit, session_id=session_id)
        return {"ok": True}

    fake_pin.__name__ = "unpin_session"
    entry = server_module.Delegation(fake_pin, needs_audit=True)
    client, audit = object(), object()
    tool = server_module.bind_delegation(entry, client, audit)  # type: ignore[arg-type]
    mcp = server_module.FastMCP(name="t")
    mcp.tool()(tool)
    await mcp.call_tool("unpin_session", {"session_id": "2026-09-10T142711"})
    assert seen == {"client": client, "audit": audit, "session_id": "2026-09-10T142711"}
    listed = {t.name: t for t in await mcp.list_tools()}
    assert set(listed["unpin_session"].inputSchema["properties"]) == {"session_id"}


def test_mis_shaped_delegate_is_refused_at_registration() -> None:
    """A delegate whose leading parameters are not the injected ones would
    bind ``client`` to a tool input; registration refuses it instead."""
    from src.mcp_server import server as server_module

    async def positional_input(client: Any, session_id: str) -> dict[str, Any]:
        return {}

    async def no_audit(client: Any, *, session_id: str) -> dict[str, Any]:
        return {}

    with pytest.raises(TypeError):
        server_module.bind_delegation(server_module.Delegation(positional_input), object(), None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        server_module.bind_delegation(
            server_module.Delegation(no_audit, needs_audit=True),
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )
