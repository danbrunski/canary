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
from typing import Any

DEFAULT_DAEMON_URL = "http://127.0.0.1:8000"
DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_BIND_PORT = 8001
# Default data root matches ``scripts/install_pi.sh``'s ``CANARY_DATA_DIR``;
# the audit log lives one level under it so ``ControlAuditLogger``'s
# ``mkdir(parents=True)`` doesn't need write access under ``/var/lib``.
DEFAULT_DATA_DIR = Path("/data/canary")
DEFAULT_AUDIT_PATH = DEFAULT_DATA_DIR / "audit" / "mcp.jsonl"


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
    anomalies_dir: Path | None
    #: Path to canary.yaml ($CANARY_CONFIG), for tools that need the full
    #: typed CanaryConfig (get_behavior_deviations) rather than just the
    #: derived storage_path/anomalies_dir this module already extracts.
    config_path: Path | None = None
    #: Opt-in: pass stateless_http=True to FastMCP. See CANARY_MCP_STATELESS
    #: in load_from_env's docstring for the trade-off this exists for.
    mcp_stateless: bool = False

    @property
    def auth_required(self) -> bool:
        """HTTP transport requires a bearer token when control_token is set."""
        return self.control_token is not None and self.control_token != ""


def _load_canary_yaml(config_path: str | None) -> dict[str, Any]:
    """Best-effort load of canary.yaml. Returns ``{}`` when unreadable."""
    if not config_path:
        return {}
    import yaml

    try:
        with open(config_path) as handle:
            data = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _storage_from_config_yaml(config_path: str | None) -> Path | None:
    """Pull ``collection.storage_path`` out of ``canary.yaml`` for ergonomics."""
    data = _load_canary_yaml(config_path)
    raw = (data.get("collection") or {}).get("storage_path")
    return Path(raw) if isinstance(raw, str) and raw else None


def _anomalies_dir_from_config_yaml(config_path: str | None) -> Path | None:
    """Pull ``alerts.anomalies_dir`` out of canary.yaml.

    The daemon defaults this to ``data/anomalies`` (see :mod:`src.config`)
    — same fallback applied here when the key isn't present, so the
    anomalies resource works against a stock dev tree.
    """
    data = _load_canary_yaml(config_path)
    raw = (data.get("alerts") or {}).get("anomalies_dir")
    if isinstance(raw, str) and raw:
        return Path(raw)
    # Daemon default when nothing is set.
    return Path("data/anomalies") if config_path else None


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
      * ``CANARY_MCP_STATELESS`` — ``1`` to pass ``stateless_http=True`` to
        FastMCP: no per-connection session id, so a ``canary-mcp`` restart
        (redeploy, crash, systemd flap) never leaves an existing client
        holding a now-invalid session id — the failure mode this session
        actually hit mid-conversation once. Trade-off: the SSE resumable-
        stream / server-push machinery that depends on a persistent session
        stops working, so this defaults off (opt-in) rather than replacing
        the stateful default outright. Try it if `restart claude` /
        reconnect-after-``canary-mcp``-restart friction outweighs wanting
        that machinery; toggle back by unsetting it if something regresses.
    """
    config_env = os.environ.get("CANARY_CONFIG")
    storage_env = os.environ.get("CANARY_STORAGE_PATH")
    storage_path: Path | None
    if storage_env:
        storage_path = Path(storage_env)
    else:
        storage_path = _storage_from_config_yaml(config_env)
    anomalies_env = os.environ.get("CANARY_ANOMALIES_DIR")
    anomalies_dir: Path | None
    if anomalies_env:
        anomalies_dir = Path(anomalies_env)
    else:
        anomalies_dir = _anomalies_dir_from_config_yaml(config_env)
    signals_env = os.environ.get("CANARY_SIGNALS_PATH")
    log_fallback_env = os.environ.get("CANARY_MCP_LOG_FALLBACK")
    data_dir_env = os.environ.get("CANARY_DATA_DIR")
    if audit_env := os.environ.get("CANARY_MCP_AUDIT_PATH"):
        audit_path = Path(audit_env)
    elif data_dir_env:
        audit_path = Path(data_dir_env) / "audit" / "mcp.jsonl"
    else:
        audit_path = DEFAULT_AUDIT_PATH
    return MCPConfig(
        daemon_url=os.environ.get("CANARY_DAEMON_URL", DEFAULT_DAEMON_URL).rstrip("/"),
        daemon_token=os.environ.get("CANARY_CONTROL_TOKEN") or None,
        bind_host=os.environ.get("CANARY_MCP_HOST", DEFAULT_BIND_HOST),
        bind_port=int(os.environ.get("CANARY_MCP_PORT", str(DEFAULT_BIND_PORT))),
        control_token=os.environ.get("CANARY_CONTROL_TOKEN") or None,
        audit_path=audit_path,
        storage_path=storage_path,
        signals_path=Path(signals_env) if signals_env else None,
        log_fallback_path=Path(log_fallback_env) if log_fallback_env else None,
        daemon_unit=os.environ.get("CANARY_MCP_DAEMON_UNIT", "canary"),
        anomalies_dir=anomalies_dir,
        config_path=Path(config_env) if config_env else None,
        mcp_stateless=os.environ.get("CANARY_MCP_STATELESS", "").strip() == "1",
    )


__all__ = ["MCPConfig", "load_from_env", "DEFAULT_DAEMON_URL", "DEFAULT_BIND_PORT"]
