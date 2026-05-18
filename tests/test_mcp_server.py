"""Tests for the FastMCP server assembly."""

from __future__ import annotations

from pathlib import Path

from src.mcp_server.config import MCPConfig
from src.mcp_server.server import build_server


def _config(
    tmp_path: Path,
    storage: Path | None = None,
    signals: Path | None = None,
) -> MCPConfig:
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
    )


async def test_build_server_registers_phase1_and_phase2_tools(tmp_path: Path) -> None:
    """All Phase 1 + Phase 2 tools register when their preconditions hold.

    ``query_telemetry`` (storage) and ``signal_lookup`` (signals) gate on env
    paths and aren't expected here.
    """
    server = build_server(_config(tmp_path))
    tools = await server.list_tools()
    names = {t.name for t in tools}
    expected = {
        # Phase 1 read
        "get_status",
        "get_config",
        "get_recent_anomalies",
        "get_recent_telemetry",
        "get_behavior_live",
        "get_behavior_models",
        # Phase 1 write
        "start_capture",
        "tag_current_capture",
        "stop_capture",
        "get_current_capture",
        "record_feedback",
        # Phase 2 read
        "get_bindings",
        "get_dids",
        "get_obd_coverage",
        "list_sessions",
        "get_session",
        "get_dataset_health",
        "get_training_runs",
        "list_jobs",
        "get_job",
        "get_job_logs",
        "get_tuning",
        "get_feedback",
        "tail_daemon_log",
        # Phase 2 write
        "set_session_metadata",
        "pin_session",
        "unpin_session",
        "start_job",
        "cancel_job",
        "set_tuning",
        "estimate_tuning",
        "revert_tuning",
    }
    assert expected <= names
    assert "query_telemetry" not in names
    assert "signal_lookup" not in names


async def test_query_telemetry_registered_when_storage_set(tmp_path: Path) -> None:
    """``query_telemetry`` appears only when a storage path is configured."""
    storage = tmp_path / "storage"
    storage.mkdir()
    server = build_server(_config(tmp_path, storage=storage))
    tools = await server.list_tools()
    assert "query_telemetry" in {t.name for t in tools}


async def test_signal_lookup_registered_when_signals_path_set(tmp_path: Path) -> None:
    """``signal_lookup`` appears only when the FSM signal catalog path is set."""
    signals = tmp_path / "signals.json"
    signals.write_text("{}")
    server = build_server(_config(tmp_path, signals=signals))
    tools = await server.list_tools()
    assert "signal_lookup" in {t.name for t in tools}


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
