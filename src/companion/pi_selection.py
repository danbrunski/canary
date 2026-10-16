"""Which Pi the companion talks to, remembered across runs (``<library>/pi.json``).

``scripts/canary_companion.py --pi-host`` and ``CANARY_HOST`` configure a
default; a Pi found on the network and chosen in the dashboard
(``POST /companion/pi``) is recorded here and wins over the configured one
until it is cleared. The record carries the daemon's identity as probed at
selection time, so the dashboard can say *which* truck it is linked to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.companion.discovery import DaemonIdentity, base_url_of

FILENAME = "pi.json"


class PiSelection:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or not payload.get("base_url"):
            return None
        return payload

    def base_url(self) -> str | None:
        record = self.read()
        return None if record is None else str(record["base_url"])

    def select(self, base_url: str, identity: DaemonIdentity, *, source: str) -> dict[str, Any]:
        record = {
            "base_url": base_url_of(base_url),
            "identity": identity.to_dict(),
            "source": source,
            "selected_at": datetime.now(UTC).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        return record

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def describe(self, *, configured: str | None) -> dict[str, Any] | None:
        """The Pi in use: the selection, else the configured default, else none."""
        record = self.read()
        if record is not None:
            return record
        if configured:
            return {
                "base_url": base_url_of(configured),
                "identity": None,
                "source": "configured",
                "selected_at": None,
            }
        return None


__all__ = ["FILENAME", "PiSelection"]
