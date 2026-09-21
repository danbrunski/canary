"""Authoritative metadata for an isolated capture session."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.atomic_write import atomic_write_text
from src.collection.capture_quality import normalize_quality_payload

CAPTURE_MANIFEST_FILENAME = "capture_profile.json"
CAPTURE_QUALITY_FILENAME = "capture_quality.json"
#: Written at arm time: the loss-counter epoch and generation the capture
#: started under (C03.5), so a stop — or a later recompute — can reconcile
#: against the counters as they were, not as they are.
CAPTURE_EPOCH_FILENAME = "capture_epoch.json"


class CaptureManifest(BaseModel):
    """The sidecar written by ``enter_capture_mode``."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    profile: str
    started_at: datetime
    tune_revision: str | None = None
    profile_snapshot: dict[str, Any]
    resolved_polling_plan: dict[str, Any] | None = None

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
    def did_continuous(self) -> bool:
        """Whether the capture held the Mode-22 session open (Mode 01 off).

        Read from the snapshot, never inferred from the profile's name: names
        are the operator's to choose, and the digest once keyed this on
        ``profile == "tuning-did"``.
        """
        return bool(self.profile_snapshot.get("did_continuous", False))

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

    A ``schema_version`` 1 sidecar is returned with ``scope:
    "trailing_snapshot"`` and its losses marked unknown: it scored the trailing
    60 s at stop against a cumulative drop counter, and nothing downstream may
    read it as a capture-wide verdict (C03.9).
    """
    try:
        payload = json.loads((Path(session_dir) / CAPTURE_QUALITY_FILENAME).read_text())
    except (OSError, ValueError):
        return None
    return normalize_quality_payload(payload) if isinstance(payload, dict) else None


def write_capture_quality(session_dir: Path, payload: dict[str, Any]) -> Path:
    path = Path(session_dir) / CAPTURE_QUALITY_FILENAME
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def read_capture_epoch(session_dir: Path) -> dict[str, Any] | None:
    """The arm-time loss epoch, or ``None`` (a pre-C03 capture: counters unknown)."""
    try:
        payload = json.loads((Path(session_dir) / CAPTURE_EPOCH_FILENAME).read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_capture_epoch(session_dir: Path, payload: dict[str, Any]) -> Path:
    path = Path(session_dir) / CAPTURE_EPOCH_FILENAME
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


__all__ = [
    "CAPTURE_EPOCH_FILENAME",
    "CAPTURE_MANIFEST_FILENAME",
    "CAPTURE_QUALITY_FILENAME",
    "CaptureManifest",
    "read_capture_epoch",
    "read_capture_manifest",
    "read_capture_quality",
    "write_capture_epoch",
    "write_capture_quality",
]
