"""Daemon status / health read tools."""

from __future__ import annotations

from typing import Any, Literal

from src.mcp_server.client import DaemonClient

StatusView = Literal["full", "compact"]


async def get_status(client: DaemonClient, *, view: StatusView = "full") -> dict[str, Any]:
    """Snapshot of daemon liveness, loaded models, storage, and emitter counters.

    Mirrors ``GET /status`` on the dashboard. Use this first when debugging
    "is the pipeline alive?" — it tells you storage path / free MB, the latest
    session id, model versions loaded, anomaly emitter counters, and (when the
    daemon has wired one in) the inference-health block (latency, freshness,
    missing-feature rate, drift).

    ``view="compact"`` returns the pollable projection instead: measured
    ~4 ms / 1.3 KB against ~42 ms / 26 KB for the full payload on the dev
    laptop, and the full payload takes 1.3–2.6 s on the Pi. It carries
    ``vehicle_state`` plus the counters a watchdog acts on — per-sink
    ``dropped_records`` and queue depth/high-water, ``can0``, the inference
    tick/miss counters, OBD request/response/timeout totals, ``capture_profile``
    name + active, ``passive``, ``free_mb`` and ``health.system`` — each at the
    same path it occupies in the full payload, and stamped with ``served_at``.
    Use it for any repeated poll, and for bracketing a counter measurement.
    Take the full view when you need per-channel rates, the request table,
    discovery, model provenance or GPS.
    """
    return await client.get_json("/status", {"view": view} if view != "full" else None)


async def get_config(client: DaemonClient) -> dict[str, Any]:
    """Echo of the daemon's loaded ``canary.yaml`` as JSON.

    Useful when a tuning or threshold question depends on the configured
    operating-mode boundaries, sample rate, retention, etc.
    """
    return await client.get_json("/config")


__all__ = ["StatusView", "get_status", "get_config"]
