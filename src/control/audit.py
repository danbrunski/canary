"""Audit logging for authenticated control-plane actions."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ControlAuditLogger:
    """Append-only JSONL audit log for remote control actions."""

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
            with self.path.open("a") as handle:
                handle.write(json.dumps(event, default=str) + "\n")
                handle.flush()
        return event


__all__ = ["ControlAuditLogger"]
