"""Tests for the FastMCP server assembly."""

from __future__ import annotations

from pathlib import Path

from src.mcp_server.config import MCPConfig
from src.mcp_server.server import build_server


def _config(tmp_path: Path, storage: Path | None = None) -> MCPConfig:
    return MCPConfig(
        daemon_url="http://127.0.0.1:8000",
        daemon_token=None,
        bind_host="127.0.0.1",
        bind_port=8001,
        control_token=None,
        audit_path=tmp_path / "audit.jsonl",
        storage_path=storage,
    )


async def test_build_server_registers_all_read_and_write_tools(tmp_path: Path) -> None:
    """The baseline tool set is registered when no storage path is set.

    Read tools and write tools both show up; ``query_telemetry`` does not,
    because it requires CANARY_STORAGE_PATH.
    """
    server = build_server(_config(tmp_path))
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert {
        "get_status",
        "get_config",
        "get_recent_anomalies",
        "get_recent_telemetry",
        "get_behavior_live",
        "get_behavior_models",
        "start_capture",
        "tag_current_capture",
        "stop_capture",
        "get_current_capture",
        "record_feedback",
    } <= names
    assert "query_telemetry" not in names


async def test_query_telemetry_registered_when_storage_set(tmp_path: Path) -> None:
    """``query_telemetry`` appears only when a storage path is configured."""
    storage = tmp_path / "storage"
    storage.mkdir()
    server = build_server(_config(tmp_path, storage=storage))
    tools = await server.list_tools()
    assert "query_telemetry" in {t.name for t in tools}


async def test_every_tool_has_non_empty_description(tmp_path: Path) -> None:
    """LLMs route off tool descriptions; an empty one would be useless.

    Spot-check via length rather than keyword match because keyword tests turn
    into busywork every time we rephrase a docstring.
    """
    server = build_server(_config(tmp_path))
    tools = await server.list_tools()
    for tool in tools:
        description = tool.description or ""
        assert len(description) >= 20, f"{tool.name} description too short: {description!r}"
