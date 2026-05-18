"""Build the configured :class:`FastMCP` instance with all tools registered.

Tools live in :mod:`src.mcp_server.tools` (read) and
:mod:`src.mcp_server.write_tools` (write). This module is the assembly layer:
it constructs the shared :class:`DaemonClient`, :class:`TelemetryQuery`, and
:class:`MCPAuditLogger`, then registers thin closures that delegate to the
tool functions. Keeping the tools themselves dependency-free makes them
trivially unit-testable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.config import MCPConfig, load_from_env
from src.mcp_server.parquet_query import TelemetryQuery
from src.mcp_server.tools import (
    anomalies as anomalies_tool,
)
from src.mcp_server.tools import (
    behavior as behavior_tool,
)
from src.mcp_server.tools import (
    status as status_tool,
)
from src.mcp_server.tools import (
    telemetry as telemetry_tool,
)
from src.mcp_server.write_tools import (
    captures as captures_tool,
)
from src.mcp_server.write_tools import (
    feedback as feedback_tool,
)

logger = logging.getLogger(__name__)


def _resolve_storage_path(config: MCPConfig) -> Path | None:
    """Storage path from env, or ``None`` if unset (DuckDB tool stays disabled)."""
    if config.storage_path is not None:
        return config.storage_path
    return None


def build_server(config: MCPConfig | None = None) -> FastMCP:
    """Construct a :class:`FastMCP` with all CANary tools registered."""
    cfg = config or load_from_env()
    mcp = FastMCP(
        name="canary",
        instructions=(
            "CANary on-vehicle anomaly-detection daemon. Read tools surface "
            "current and historical telemetry, anomaly events, and model "
            "state. Write tools manage captures, tag events, and record "
            "feedback. All write tools are audit-logged with actor=mcp."
        ),
        host=cfg.bind_host,
        port=cfg.bind_port,
        # 0.0.0.0 binds to the LAN — TransportSecuritySettings warns about
        # that by default; we know and accept it.
    )

    client = DaemonClient(cfg.daemon_url, cfg.daemon_token)
    audit = MCPAuditLogger(cfg.audit_path)
    storage_path = _resolve_storage_path(cfg)
    query_runner: TelemetryQuery | None = (
        TelemetryQuery(storage_path) if storage_path is not None else None
    )

    _register_read_tools(mcp, client, query_runner)
    _register_write_tools(mcp, client, audit)
    return mcp


def _register_read_tools(
    mcp: FastMCP,
    client: DaemonClient,
    query_runner: TelemetryQuery | None,
) -> None:
    """Register all read tools as MCP tools."""

    @mcp.tool()
    async def get_status() -> dict[str, Any]:
        """Daemon liveness, loaded models, storage, anomaly emitter counters."""
        return await status_tool.get_status(client)

    @mcp.tool()
    async def get_config() -> dict[str, Any]:
        """Echo the daemon's loaded canary.yaml as JSON."""
        return await status_tool.get_config(client)

    @mcp.tool()
    async def get_recent_anomalies(
        since: str | None = None,
        limit: int = 100,
        source: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Anomaly events since `since` (ISO-8601), optionally filtered by source/mode."""
        return await anomalies_tool.get_recent_anomalies(
            client, since=since, limit=limit, source=source, mode=mode
        )

    @mcp.tool()
    async def get_recent_telemetry(
        n: int = 100,
        channel: str | None = None,
        per_channel: bool = False,
    ) -> dict[str, Any]:
        """Last `n` telemetry rows; `channel` filters; `per_channel` returns latest-per-channel."""
        return await telemetry_tool.get_recent_telemetry(
            client, n=n, channel=channel, per_channel=per_channel
        )

    @mcp.tool()
    async def get_behavior_live() -> dict[str, Any]:
        """Live per-model expected/actual/deviation/severity + rolling trail."""
        return await behavior_tool.get_behavior_live(client)

    @mcp.tool()
    async def get_behavior_models() -> dict[str, Any]:
        """Static behavior surfaces (the learned 'normal' maps)."""
        return await behavior_tool.get_behavior_models(client)

    if query_runner is not None:
        @mcp.tool()
        def query_telemetry(sql: str, row_limit: int | None = None) -> dict[str, Any]:
            """Run a single SELECT/WITH against the telemetry Parquet view.

            View name is `telemetry` with columns timestamp, source, channel,
            value_number, value_text, value_bool, unit, metadata_json, and a
            derived `session` column. Non-SELECT statements are rejected.
            """
            return telemetry_tool.query_telemetry(query_runner, sql=sql, row_limit=row_limit)
    else:
        logger.info(
            "query_telemetry disabled — CANARY_STORAGE_PATH unset; daemon HTTP "
            "tools remain available."
        )


def _register_write_tools(
    mcp: FastMCP,
    client: DaemonClient,
    audit: MCPAuditLogger,
) -> None:
    """Register all write tools as MCP tools."""

    @mcp.tool()
    async def start_capture(
        purpose: str,
        protocol: str = "drive",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Begin a labeled capture window on the current telemetry session."""
        return await captures_tool.start_capture(
            client, audit, purpose=purpose, protocol=protocol, metadata=metadata
        )

    @mcp.tool()
    async def tag_current_capture(
        label: str,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Drop a tag onto the active capture mid-drive."""
        return await captures_tool.tag_current_capture(
            client, audit, label=label, fields=fields
        )

    @mcp.tool()
    async def stop_capture(reason: str | None = None) -> dict[str, Any]:
        """Close the active capture; `reason` is recorded for later review."""
        return await captures_tool.stop_capture(client, audit, reason=reason)

    @mcp.tool()
    async def get_current_capture() -> dict[str, Any]:
        """Read the currently active capture window."""
        return await captures_tool.get_current_capture(client)

    @mcp.tool()
    async def record_feedback(
        anomaly_id: str,
        label: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record true_positive/false_positive/unknown verdict on an anomaly."""
        # Cast at the boundary — FastMCP infers the schema from the str
        # annotation so the model passes a free string, and we validate it
        # against the Literal alias inside the tool body.
        if label not in ("true_positive", "false_positive", "unknown"):
            return {
                "ok": False,
                "error": (
                    "label must be one of 'true_positive', 'false_positive', 'unknown'"
                ),
            }
        return await feedback_tool.record_feedback(
            client, audit, anomaly_id=anomaly_id, label=label, note=note  # type: ignore[arg-type]
        )


__all__ = ["build_server"]
