"""Read tools for the post-hoc session classifier."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient


async def get_session_classification(
    client: DaemonClient,
    *,
    session_id: str,
) -> dict[str, Any]:
    """Modes + events + disposition sidecar for one session.

    Returns ``404`` (raised as :class:`DaemonError`) when the session has
    no sidecar yet — call :func:`classify_session` (write tool) or run
    ``scripts/label_sessions.py`` to generate it.
    """
    return await client.get_json(f"/control/sessions/{session_id}/classification")


async def get_corpus_mode_summary(client: DaemonClient) -> dict[str, Any]:
    """Aggregate mode-coverage + event counts across every classified session.

    Useful for "do I have enough warmup data to retrain the
    coolant_warmup behavior model?" — the answer is in
    ``mode_seconds.warmup`` and ``event_counts.cold_start``.
    """
    return await client.get_json("/control/classifications/summary")


async def list_events_by_kind(
    client: DaemonClient,
    *,
    kind: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Corpus-wide search through every session's ``derived_tags.jsonl``.

    ``kind`` filters to events of one type (``hard_accel``, ``cold_start``,
    ``cruise``, etc.); ``None`` returns all kinds. Results are newest-first
    and capped at ``limit``.
    """
    params: dict[str, Any] = {"limit": limit}
    if kind is not None:
        params["kind"] = kind
    return await client.get_json("/control/classifications/events", params=params)


__all__ = [
    "get_corpus_mode_summary",
    "get_session_classification",
    "list_events_by_kind",
]
