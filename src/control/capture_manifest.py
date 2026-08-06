"""Authoritative metadata for an isolated capture session."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

CAPTURE_MANIFEST_FILENAME = "capture_profile.json"


class CaptureManifest(BaseModel):
    """The sidecar written by ``enter_capture_mode``."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    profile: str
    started_at: datetime
    tune_revision: str | None = None
    profile_snapshot: dict[str, Any]

    @property
    def corpus_eligible(self) -> bool:
        return bool(self.profile_snapshot.get("corpus_eligible", False))


def read_capture_manifest(session_dir: Path) -> CaptureManifest | None:
    """Read a valid capture sidecar, or ``None`` for normal/invalid sessions."""
    try:
        payload = json.loads((Path(session_dir) / CAPTURE_MANIFEST_FILENAME).read_text())
        return CaptureManifest.model_validate(payload)
    except (OSError, ValueError):
        return None


__all__ = ["CAPTURE_MANIFEST_FILENAME", "CaptureManifest", "read_capture_manifest"]
