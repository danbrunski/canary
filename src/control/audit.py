"""Audit logging for authenticated control-plane actions."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.jsonl_store import append_jsonl_line


class ControlAuditLogger:
    """Append-only JSONL audit log for remote control actions.

    Doesn't fit :class:`~src.jsonl_store.JsonlStore` (Task 3.4): write-only
    (no read-side at all), no JSON-safe record *model* (a plain dict
    payload), and thread-locked since control-plane requests can call it
    concurrently — still reuses :func:`~src.jsonl_store.append_jsonl_line`
    for its own append body.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        action: str,
        result: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action,
            "result": result,
            "payload": payload or {},
        }
        with self._lock:
            append_jsonl_line(self.path, event, sort_keys=False)
        return event


__all__ = ["ControlAuditLogger"]
