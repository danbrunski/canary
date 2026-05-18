"""Tests for the MCP audit-log wrapper."""

from __future__ import annotations

import json
from pathlib import Path

from src.mcp_server.audit import MCPAuditLogger


def test_record_stamps_actor_and_action_prefix(tmp_path: Path) -> None:
    """Every entry is namespaced ``mcp.<tool>`` and tagged ``actor="mcp"``."""
    logger = MCPAuditLogger(tmp_path / "audit.jsonl")
    logger.record(tool="start_capture", result="ok", payload={"purpose": "test"})
    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["action"] == "mcp.start_capture"
    assert entries[0]["result"] == "ok"
    assert entries[0]["payload"]["actor"] == "mcp"
    assert entries[0]["payload"]["purpose"] == "test"


def test_record_appends_multiple(tmp_path: Path) -> None:
    """Successive calls append lines; existing entries remain."""
    logger = MCPAuditLogger(tmp_path / "audit.jsonl")
    logger.record(tool="a", result="ok")
    logger.record(tool="b", result="error", payload={"err": "boom"})
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == "mcp.a"
    assert json.loads(lines[1])["action"] == "mcp.b"
    assert json.loads(lines[1])["payload"]["err"] == "boom"


def test_record_creates_parent_dir(tmp_path: Path) -> None:
    """The audit log's parent directory is created on first write."""
    nested = tmp_path / "var" / "lib" / "audit.jsonl"
    logger = MCPAuditLogger(nested)
    logger.record(tool="x", result="ok")
    assert nested.exists()
