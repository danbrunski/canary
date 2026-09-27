"""Authoritative metadata for an isolated capture session."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from src.atomic_write import atomic_write_text
from src.storage.capture_losses import unknown_losses
from src.storage.sidecar import read_json_object, write_json_sidecar

CAPTURE_MANIFEST_FILENAME = "capture_profile.json"
CAPTURE_QUALITY_FILENAME = "capture_quality.json"
#: Written at arm time: the loss-counter epoch and generation the capture
#: started under (C03.5), so a stop — or a later recompute — can reconcile
#: against the counters as they were, not as they are.
CAPTURE_EPOCH_FILENAME = "capture_epoch.json"

#: Schema of the ``capture_quality.json`` sidecar (``collection.capture_quality``
#: writes it). Distinct from :class:`CaptureManifest`'s own ``schema_version``.
CAPTURE_QUALITY_SCHEMA_VERSION = 2

#: Scopes a verdict can carry. ``trailing_snapshot`` is what a v1 sidecar is
#: read back as: a 60 s window at stop time, not the capture.
SCOPE_LIVE = "live"
SCOPE_STOP = "stop"
SCOPE_RECOMPUTE = "recompute"
SCOPE_TRAILING = "trailing_snapshot"


class CaptureManifest(BaseModel):
    """The sidecar written by ``enter_capture_mode``."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    profile: str
    started_at: datetime
    tune_revision: str | None = None
    profile_snapshot: dict[str, Any]
    resolved_polling_plan: dict[str, Any] | None = None
    ended_at: datetime | None = None
    end_reason: (
        Literal["operator_stop", "profile_switch", "daemon_shutdown", "interrupted"] | None
    ) = None
    detected_at: datetime | None = None

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


def normalize_quality_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read any sidecar version with its scope made explicit.

    A v1 sidecar scored the trailing 60 s at stop and described cumulative
    loss counters; it is returned as written plus ``scope: trailing_snapshot``
    and ``losses`` marked unknown, so nothing downstream can mistake it for a
    capture-wide verdict. v2 payloads pass through unchanged.
    """
    version = int(payload.get("schema_version") or 1)
    if version >= CAPTURE_QUALITY_SCHEMA_VERSION:
        return dict(payload)
    out = dict(payload)
    out["scope"] = SCOPE_TRAILING
    out["losses"] = unknown_losses()
    out.setdefault("notes", [])
    out["notes"] = [
        *out["notes"],
        "schema_version 1 sidecar: the verdict covers the trailing 60 s at stop, not the "
        "capture; its storage counter was cumulative and loss inside the capture is unknown.",
    ]
    return out


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
    payload = read_json_object(Path(session_dir) / CAPTURE_QUALITY_FILENAME)
    return None if payload is None else normalize_quality_payload(payload)


def stamp_capture_end(
    session_dir: Path,
    end_reason: Literal["operator_stop", "profile_switch", "daemon_shutdown", "interrupted"],
    *,
    ended_at: datetime | None = None,
    detected_at: datetime | None = None,
) -> CaptureManifest:
    """Atomically close one manifest; an existing end record is immutable."""
    path = Path(session_dir) / CAPTURE_MANIFEST_FILENAME
    manifest = read_capture_manifest(session_dir)
    if manifest is None:
        raise ValueError(f"no capture manifest in {session_dir}")
    if manifest.end_reason is not None:
        return manifest
    now = ended_at or datetime.now(UTC)
    updated = manifest.model_copy(
        update={"ended_at": now, "end_reason": end_reason, "detected_at": detected_at}
    )
    atomic_write_text(path, updated.model_dump_json(indent=2) + "\n")
    return updated


def recover_interrupted_capture(
    storage_root: Path, *, detected_at: datetime | None = None
) -> tuple[Path, CaptureManifest] | None:
    """Stamp the newest unclosed capture once after an unclean daemon exit."""
    from src.storage.sessions import session_started_at

    root = Path(storage_root)
    candidates: list[tuple[datetime, Path]] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.is_dir():
            continue
        manifest = read_capture_manifest(child)
        if manifest is None or manifest.end_reason is not None:
            continue
        started = session_started_at(child)
        candidates.append((started or manifest.started_at, child))
    if not candidates:
        return None
    _, session_dir = max(candidates, key=lambda item: item[0])
    detected = detected_at or datetime.now(UTC)
    return session_dir, stamp_capture_end(
        session_dir,
        "interrupted",
        ended_at=detected,
        detected_at=detected,
    )


def latest_capture_summary(storage_root: Path) -> dict[str, Any] | None:
    """Return the newest capture by parsed start time, prefixes ignored."""
    from src.storage.sessions import session_started_at

    root = Path(storage_root)
    rows: list[tuple[datetime, Path, CaptureManifest]] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return None
    for child in children:
        manifest = read_capture_manifest(child)
        if manifest is None:
            continue
        rows.append((session_started_at(child) or manifest.started_at, child, manifest))
    if not rows:
        return None
    _, session_dir, manifest = max(rows, key=lambda item: item[0])
    quality = read_capture_quality(session_dir)
    return {
        "name": manifest.profile,
        "session_dir": str(session_dir),
        "started_at": manifest.started_at.isoformat(),
        "ended_at": None if manifest.ended_at is None else manifest.ended_at.isoformat(),
        "end_reason": manifest.end_reason,
        "quality_verdict": None if quality is None else quality.get("verdict"),
    }


def write_capture_quality(session_dir: Path, payload: dict[str, Any]) -> Path:
    path = Path(session_dir) / CAPTURE_QUALITY_FILENAME
    write_json_sidecar(path, payload)
    return path


def read_capture_epoch(session_dir: Path) -> dict[str, Any] | None:
    """The arm-time loss epoch, or ``None`` (a pre-C03 capture: counters unknown)."""
    return read_json_object(Path(session_dir) / CAPTURE_EPOCH_FILENAME)


def write_capture_epoch(session_dir: Path, payload: dict[str, Any]) -> Path:
    path = Path(session_dir) / CAPTURE_EPOCH_FILENAME
    write_json_sidecar(path, payload)
    return path


__all__ = [
    "CAPTURE_EPOCH_FILENAME",
    "CAPTURE_MANIFEST_FILENAME",
    "CAPTURE_QUALITY_FILENAME",
    "CAPTURE_QUALITY_SCHEMA_VERSION",
    "SCOPE_LIVE",
    "SCOPE_RECOMPUTE",
    "SCOPE_STOP",
    "SCOPE_TRAILING",
    "CaptureManifest",
    "normalize_quality_payload",
    "read_capture_epoch",
    "read_capture_manifest",
    "read_capture_quality",
    "recover_interrupted_capture",
    "latest_capture_summary",
    "stamp_capture_end",
    "write_capture_epoch",
    "write_capture_quality",
]
