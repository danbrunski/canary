"""MCP resources — URI-addressable read-only snapshots.

Resources are how MCP exposes data the client can *pin* or *reference* —
e.g. attach a session manifest to a conversation so the model has stable
context across turns. They complement tools: a tool *does* something, a
resource *is* something.

URI scheme: ``canary://<topic>[/<id>]``. Topics:

- ``canary://config`` — daemon ``canary.yaml`` as JSON
- ``canary://status`` — daemon status snapshot
- ``canary://behavior-models`` — learned behavior surfaces
- ``canary://sessions/{session_id}`` — one session: metadata, parquet inventory
- ``canary://anomalies/{date}`` — one day's anomaly events (raw JSONL contents)
- ``canary://audit/mcp`` — recent MCP write-tool audit entries
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.mcp_server.client import DaemonClient

#: Anomaly log file format is ``anomalies-YYYY-MM-DD.jsonl`` — mirrors
#: :mod:`src.alerts.event_logger`.
_ANOMALY_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


async def read_config(client: DaemonClient) -> str:
    """``canary://config`` body — daemon's loaded YAML as pretty-printed JSON."""
    payload = await client.get_json("/config")
    return json.dumps(payload, indent=2, sort_keys=True)


async def read_status(client: DaemonClient) -> str:
    """``canary://status`` body — daemon health snapshot."""
    payload = await client.get_json("/status")
    return json.dumps(payload, indent=2, sort_keys=True)


async def read_behavior_models(client: DaemonClient) -> str:
    """``canary://behavior-models`` body — static behavior surfaces."""
    payload = await client.get_json("/models/behavior")
    return json.dumps(payload, indent=2, sort_keys=True)


async def read_session(client: DaemonClient, *, session_id: str) -> str:
    """``canary://sessions/{id}`` body — metadata + chunk inventory.

    Combines ``GET /control/sessions/{id}`` (metadata) with the parquet
    file list read directly from disk when ``storage_path`` is available;
    the daemon's response already includes the chunk inventory, so we
    return it verbatim as pretty JSON.
    """
    payload = await client.get_json(f"/control/sessions/{session_id}")
    return json.dumps(payload, indent=2, sort_keys=True)


def read_anomalies_for_date(anomalies_dir: Path, *, date: str) -> str:
    """``canary://anomalies/{YYYY-MM-DD}`` body — the daily JSONL file contents.

    Returned as text rather than parsed JSON so the client can stream/pin
    the raw events. An empty string is returned when the file doesn't
    exist; a malformed date raises :class:`ValueError`.
    """
    if not _ANOMALY_DATE_RE.match(date):
        raise ValueError(f"date must be YYYY-MM-DD, got {date!r}")
    path = anomalies_dir / f"anomalies-{date}.jsonl"
    if not path.exists():
        return ""
    return path.read_text()


def read_audit_log(audit_path: Path, *, tail: int = 200) -> str:
    """``canary://audit/mcp`` body — the last ``tail`` MCP audit entries.

    Read on demand. Defaults to the last 200 lines because audit logs can
    grow unbounded; clients that want the full history can request fewer
    via a search tool we'd add later.
    """
    if not audit_path.exists():
        return ""
    lines = audit_path.read_text(errors="replace").splitlines()[-tail:]
    return "\n".join(lines)


__all__ = [
    "read_config",
    "read_status",
    "read_behavior_models",
    "read_session",
    "read_anomalies_for_date",
    "read_audit_log",
]
