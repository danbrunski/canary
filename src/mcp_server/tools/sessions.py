"""Session and dataset/training-run read tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def list_sessions(client: DaemonClient) -> dict[str, Any]:
    """All known sessions on disk, oldest-first.

    Use this for navigation; combine with ``get_session`` to drill into one.
    """
    return await client.get_json("/control/sessions")


async def get_session(client: DaemonClient, *, session_id: str) -> dict[str, Any]:
    """Detail for one session: tags, control.json, chunk count, metadata."""
    return await client.get_json(f"/control/sessions/{session_id}")


async def get_dataset_health(client: DaemonClient) -> dict[str, Any]:
    """Corpus readiness: per-mode sample counts, gaps, last-seen times.

    Useful when deciding whether retraining now is worthwhile.
    """
    return await client.get_json("/control/datasets/health")


async def get_training_runs(client: DaemonClient) -> dict[str, Any]:
    """Training-run history (the latest record per run from the ledger)."""
    return await client.get_json("/control/training-runs")


async def get_current_capture(client: DaemonClient) -> dict[str, Any]:
    """Read the currently active capture window (pure read)."""
    return await client.get_json("/control/captures/current")


__all__ = [
    "list_sessions",
    "get_session",
    "get_dataset_health",
    "get_training_runs",
    "get_current_capture",
]
