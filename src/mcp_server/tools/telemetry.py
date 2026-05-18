"""Telemetry read + ad-hoc SQL tools."""

from __future__ import annotations

from typing import Any

from src.mcp_server.client import DaemonClient
from src.mcp_server.parquet_query import (
    QueryValidationError,
    TelemetryQuery,
)


async def get_recent_telemetry(
    client: DaemonClient,
    *,
    n: int = 100,
    channel: str | None = None,
    per_channel: bool = False,
) -> dict[str, Any]:
    """The last ``n`` rows from the newest session.

    Returns rows in the daemon's long-format schema: ``timestamp``, ``source``,
    ``channel``, ``value_number`` / ``value_text`` / ``value_bool``, ``unit``,
    plus metadata. Set ``per_channel=True`` to get one latest row per channel
    (a state snapshot) instead of the last ``n`` records globally. ``channel``
    is a client-side filter applied after fetch.
    """
    params: dict[str, Any] = {"n": int(n), "per_channel": str(per_channel).lower()}
    payload = await client.get_json("/telemetry/latest", params=params)
    rows: list[dict[str, Any]] = list(payload.get("rows", []))
    if channel is not None:
        rows = [r for r in rows if r.get("channel") == channel]
    return {"rows": rows, "count": len(rows)}


def query_telemetry(
    query_runner: TelemetryQuery,
    *,
    sql: str,
    row_limit: int | None = None,
) -> dict[str, Any]:
    """Run an ad-hoc SELECT against the telemetry Parquet ring buffer.

    The view name is ``telemetry`` (long-format columns plus a derived
    ``session`` column from the parent directory). Only single SELECT or WITH
    statements are accepted; the connection is read-only and an outer LIMIT
    is applied even if the query omits one.

    Examples
    --------
    Pull a single channel from one session:

        SELECT timestamp, value_number FROM telemetry
        WHERE channel = 'RPM' AND session = '2026-05-17T120000'
        ORDER BY timestamp

    Count anomaly-adjacent samples by mode (joining the anomalies log is the
    caller's responsibility; this view is telemetry only).
    """
    try:
        rows = query_runner.run(sql, row_limit=row_limit)
    except QueryValidationError as exc:
        return {"ok": False, "error": str(exc), "rows": [], "count": 0}
    return {"ok": True, "rows": rows, "count": len(rows)}


__all__ = ["get_recent_telemetry", "query_telemetry"]
