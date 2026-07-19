"""Tests for the FastMCP server assembly."""

from __future__ import annotations

from pathlib import Path

from src.mcp_server.config import MCPConfig
from src.mcp_server.server import build_server


def _config(
    tmp_path: Path,
    storage: Path | None = None,
    signals: Path | None = None,
    anomalies: Path | None = None,
    config_path: Path | None = None,
    mcp_stateless: bool = False,
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
        anomalies_dir=anomalies,
        config_path=config_path,
        mcp_stateless=mcp_stateless,
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
        "search_anomaly_log",
        # Phase 2 write
        "set_session_metadata",
        "pin_session",
        "unpin_session",
        "start_job",
        "cancel_job",
        "set_tuning",
        "estimate_tuning",
        "revert_tuning",
        # Phase 3 write
        "upload_model_artifact",
        # Replay window
        "replay_window",
    }
    assert expected <= names
    assert "query_telemetry" not in names
    assert "signal_lookup" not in names
    assert "get_behavior_deviations" not in names


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


async def test_get_behavior_deviations_registered_when_config_path_set(tmp_path: Path) -> None:
    """``get_behavior_deviations`` appears only when CANARY_CONFIG resolved to a
    readable file — it needs the full typed CanaryConfig, not just storage_path."""
    config_file = tmp_path / "canary.yaml"
    config_file.write_text("collection:\n  storage_path: /var/canary/sessions\n")
    server = build_server(_config(tmp_path, config_path=config_file))
    tools = await server.list_tools()
    assert "get_behavior_deviations" in {t.name for t in tools}


async def test_get_behavior_deviations_absent_when_config_path_unreadable(
    tmp_path: Path,
) -> None:
    server = build_server(_config(tmp_path, config_path=tmp_path / "does-not-exist.yaml"))
    tools = await server.list_tools()
    assert "get_behavior_deviations" not in {t.name for t in tools}


def test_stateless_http_defaults_off(tmp_path: Path) -> None:
    server = build_server(_config(tmp_path))
    assert server.settings.stateless_http is False


def test_stateless_http_opts_in(tmp_path: Path) -> None:
    """CANARY_MCP_STATELESS=1 reaches FastMCP — no per-connection session id,
    so a canary-mcp restart never strands a client on an invalidated
    session (task 4.2)."""
    server = build_server(_config(tmp_path, mcp_stateless=True))
    assert server.settings.stateless_http is True


async def test_resources_registered(tmp_path: Path) -> None:
    """Phase 3 resources show up; the anomalies template requires its dir."""
    server = build_server(_config(tmp_path, anomalies=tmp_path / "anom"))
    resources = await server.list_resources()
    templates = await server.list_resource_templates()
    static_uris = {str(r.uri) for r in resources}
    assert "canary://config" in static_uris
    assert "canary://status" in static_uris
    assert "canary://behavior-models" in static_uris
    assert "canary://audit/mcp" in static_uris
    template_uris = {t.uriTemplate for t in templates}
    assert "canary://sessions/{session_id}" in template_uris
    assert "canary://anomalies/{date}" in template_uris


async def test_anomalies_resource_omitted_when_dir_unset(tmp_path: Path) -> None:
    """Without anomalies_dir, only the four static + one template stay."""
    server = build_server(_config(tmp_path))
    templates = {t.uriTemplate for t in await server.list_resource_templates()}
    assert "canary://anomalies/{date}" not in templates
    assert "canary://sessions/{session_id}" in templates


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
