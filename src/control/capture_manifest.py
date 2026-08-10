"""Authoritative metadata for an isolated capture session."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

CAPTURE_MANIFEST_FILENAME = "capture_profile.json"
CAPTURE_QUALITY_FILENAME = "capture_quality.json"


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

    @property
    def behavior_model_recipes(self) -> tuple[str, ...]:
        raw = self.profile_snapshot.get("behavior_model_recipes", ())
        if not isinstance(raw, list | tuple):
            return ()
        return tuple(str(item) for item in raw if str(item).strip())

    @property
    def mode01_channels(self) -> tuple[str, ...]:
        """Mode-01 channels this capture actually asked for.

        The authoritative answer to "what was this capture measuring", as
        opposed to whatever a downstream registry happens to resolve. A report
        that picks its own channel list can silently omit the slow ones — which
        are exactly the ones that limit an analysis.
        """
        raw = self.profile_snapshot.get("mode01") or {}
        if not isinstance(raw, dict):
            return ()
        return tuple(sorted(str(name).upper() for name in raw if str(name).strip()))

    @property
    def required_tuple(self) -> tuple[str, ...]:
        raw = self.profile_snapshot.get("required_tuple") or ()
        if not isinstance(raw, list | tuple):
            return ()
        return tuple(str(item).upper() for item in raw if str(item).strip())


def read_capture_manifest(session_dir: Path) -> CaptureManifest | None:
    """Read a valid capture sidecar, or ``None`` for normal/invalid sessions."""
    try:
        payload = json.loads((Path(session_dir) / CAPTURE_MANIFEST_FILENAME).read_text())
        return CaptureManifest.model_validate(payload)
    except (OSError, ValueError):
        return None


def read_capture_quality(session_dir: Path) -> dict[str, Any] | None:
    """The contract verdict written when this capture stopped, if any.

    ``None`` means the sidecar is absent — an older capture, a daemon without a
    health monitor, or a stop that failed to write it. That is *unknown*, not a
    pass, and every caller must render it as such.
    """
    try:
        payload = json.loads((Path(session_dir) / CAPTURE_QUALITY_FILENAME).read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = [
    "CAPTURE_MANIFEST_FILENAME",
    "CAPTURE_QUALITY_FILENAME",
    "CaptureManifest",
    "read_capture_manifest",
    "read_capture_quality",
]
