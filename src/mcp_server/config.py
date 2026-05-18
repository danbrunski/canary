"""Runtime config for the MCP server.

Resolved from environment variables at process start. Kept thin: the heavy
config (vehicle, hardware, storage paths) lives in ``CanaryConfig`` and is
loaded by the daemon — the MCP server reads daemon state through HTTP and
doesn't need to parse ``canary.yaml`` itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DAEMON_URL = "http://127.0.0.1:8000"
DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_BIND_PORT = 8001
DEFAULT_AUDIT_PATH = Path("/var/lib/canary/audit/mcp.jsonl")


@dataclass(frozen=True)
class MCPConfig:
    """Resolved MCP server configuration."""

    daemon_url: str
    daemon_token: str | None
    bind_host: str
    bind_port: int
    control_token: str | None
    audit_path: Path
    storage_path: Path | None
    signals_path: Path | None
    log_fallback_path: Path | None
    daemon_unit: str

    @property
    def auth_required(self) -> bool:
        """HTTP transport requires a bearer token when control_token is set."""
        return self.control_token is not None and self.control_token != ""


def _storage_from_config_yaml(config_path: str | None) -> Path | None:
    """Pull ``collection.storage_path`` out of ``canary.yaml`` for ergonomics.

    The daemon and MCP server share an env file on the Pi; rather than asking
    operators to duplicate the storage path there, we read it from the same
    config the daemon uses. Failures (missing file, parse error, no key) just
    return ``None`` — ``query_telemetry`` stays disabled in that case.
    """
    if not config_path:
        return None
    import yaml

    try:
        with open(config_path) as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return None
    raw = (data.get("collection") or {}).get("storage_path")
    return Path(raw) if isinstance(raw, str) and raw else None


def load_from_env() -> MCPConfig:
    """Build :class:`MCPConfig` from the process environment.

    Variables:
      * ``CANARY_DAEMON_URL`` — base URL of the daemon (default localhost:8000)
      * ``CANARY_CONTROL_TOKEN`` — shared with the daemon's control plane;
        forwarded as ``Authorization: Bearer`` on outbound requests AND
        required from inbound HTTP/SSE clients when set
      * ``CANARY_MCP_HOST`` / ``CANARY_MCP_PORT`` — bind address for the HTTP
        transport; defaults to ``0.0.0.0:8001`` so the in-vehicle LAN can
        reach it
      * ``CANARY_MCP_AUDIT_PATH`` — JSONL audit log path for write tools
      * ``CANARY_STORAGE_PATH`` — Parquet storage root for ``query_telemetry``.
        Falls back to ``collection.storage_path`` in ``$CANARY_CONFIG`` so the
        operator doesn't have to duplicate the path across two env files.
    """
    storage_env = os.environ.get("CANARY_STORAGE_PATH")
    storage_path: Path | None
    if storage_env:
        storage_path = Path(storage_env)
    else:
        storage_path = _storage_from_config_yaml(os.environ.get("CANARY_CONFIG"))
    signals_env = os.environ.get("CANARY_SIGNALS_PATH")
    log_fallback_env = os.environ.get("CANARY_MCP_LOG_FALLBACK")
    return MCPConfig(
        daemon_url=os.environ.get("CANARY_DAEMON_URL", DEFAULT_DAEMON_URL).rstrip("/"),
        daemon_token=os.environ.get("CANARY_CONTROL_TOKEN") or None,
        bind_host=os.environ.get("CANARY_MCP_HOST", DEFAULT_BIND_HOST),
        bind_port=int(os.environ.get("CANARY_MCP_PORT", str(DEFAULT_BIND_PORT))),
        control_token=os.environ.get("CANARY_CONTROL_TOKEN") or None,
        audit_path=Path(os.environ.get("CANARY_MCP_AUDIT_PATH", str(DEFAULT_AUDIT_PATH))),
        storage_path=storage_path,
        signals_path=Path(signals_env) if signals_env else None,
        log_fallback_path=Path(log_fallback_env) if log_fallback_env else None,
        daemon_unit=os.environ.get("CANARY_MCP_DAEMON_UNIT", "canary"),
    )


__all__ = ["MCPConfig", "load_from_env", "DEFAULT_DAEMON_URL", "DEFAULT_BIND_PORT"]
