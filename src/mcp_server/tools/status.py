"""Daemon status / health read tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_status(client: DaemonClient) -> dict[str, Any]:
    """Snapshot of daemon liveness, loaded models, storage, and emitter counters.

    Mirrors ``GET /status`` on the dashboard. Use this first when debugging
    "is the pipeline alive?" — it tells you storage path / free MB, the latest
    session id, model versions loaded, anomaly emitter counters, and (when the
    daemon has wired one in) the inference-health block (latency, freshness,
    missing-feature rate, drift).
    """
    return await client.get_json("/status")


async def get_config(client: DaemonClient) -> dict[str, Any]:
    """Echo of the daemon's loaded ``canary.yaml`` as JSON.

    Useful when a tuning or threshold question depends on the configured
    operating-mode boundaries, sample rate, retention, etc.
    """
    return await client.get_json("/config")


__all__ = ["get_status", "get_config"]
