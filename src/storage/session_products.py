"""Readers of a session's analysis products: digest, fingerprint, classification.

Each product is a JSON sidecar in the session directory. ``analysis`` computes
and writes them (``analysis.digest``, ``analysis.fingerprint``,
``analysis.session_classifier``); everything that only *reads* one -- the
session catalog, model evaluation, the control plane -- reads it here, so a
reader never has to import the workbench (R09.3). The sidecar types the
readers return and their schema versions live here with them; the computing
side imports them back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from src.storage.sidecar import read_json_object

# --------------------------------------------------------------------------- #
# Digest (analysis.digest)
# --------------------------------------------------------------------------- #

DIGEST_FILENAME = "digest.json"

#: Refusal sidecar (D03.6). A digest that did not run leaves this instead of
#: nothing, so a reader can tell the three states apart: ``digest.json``
#: present = computed; ``digest_skipped.json`` present = refused/failed, with
#: the reason; neither = never attempted. Before this existed, a session the
#: daemon was OOM-killed computing looked exactly like a session nobody had
#: got round to.
DIGEST_SKIPPED_FILENAME = "digest_skipped.json"


def read_digest(session_dir: Path) -> dict[str, Any] | None:
    """Load the sidecar dict, or ``None`` if absent / unreadable."""
    return read_json_object(session_dir / DIGEST_FILENAME)


def read_digest_skipped(session_dir: Path) -> dict[str, Any] | None:
    """Load the refusal sidecar, or ``None`` if absent / unreadable."""
    return read_json_object(session_dir / DIGEST_SKIPPED_FILENAME)


# --------------------------------------------------------------------------- #
# Rolling circumference (analysis.rolling_circumference)
# --------------------------------------------------------------------------- #

#: The driven tires' rolling circumference over one drive, or why there is
#: none (D03.35). Its own product, not a digest field: a drive refused a digest
#: (a capture over the row budget, or one recorded before P02.1 lifted the row
#: budget from ordinary drives) still has steady cruise to measure, and those
#: long drives are the measurement's best input.
ROLLING_CIRCUMFERENCE_FILENAME = "rolling_circumference.json"


def read_rolling_circumference(session_dir: Path) -> dict[str, Any] | None:
    """Load the sidecar dict, or ``None`` if absent / unreadable."""
    return read_json_object(session_dir / ROLLING_CIRCUMFERENCE_FILENAME)


# --------------------------------------------------------------------------- #
# Fingerprint (analysis.fingerprint)
# --------------------------------------------------------------------------- #

FINGERPRINT_FILENAME = "fingerprint.json"

FINGERPRINT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SessionFingerprint:
    """Full fingerprint sidecar payload for one session."""

    schema_version: int
    fingerprint_version: str
    computed_at: str
    session_id: str
    stats: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fingerprint_version": self.fingerprint_version,
            "computed_at": self.computed_at,
            "session_id": self.session_id,
            "stats": dict(self.stats),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionFingerprint:
        return cls(
            schema_version=int(data.get("schema_version", FINGERPRINT_SCHEMA_VERSION)),
            fingerprint_version=str(data["fingerprint_version"]),
            computed_at=str(data["computed_at"]),
            session_id=str(data["session_id"]),
            stats={str(k): float(v) for k, v in dict(data.get("stats") or {}).items()},
        )


def read_fingerprint(session_dir: Path) -> SessionFingerprint | None:
    """Load the sidecar, or ``None`` if absent / unreadable."""
    data = read_json_object(session_dir / FINGERPRINT_FILENAME)
    if data is None:
        return None
    try:
        return SessionFingerprint.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Classification (analysis.session_classifier)
# --------------------------------------------------------------------------- #

CLASSIFICATION_FILENAME = "classification.json"
DERIVED_TAGS_FILENAME = "derived_tags.jsonl"

CLASSIFICATION_SCHEMA_VERSION = 1

Disposition = Literal["ready", "review", "quarantine"]


@dataclass(frozen=True)
class ModeSegment:
    """One contiguous run of a single operating mode within a session."""

    start_timestamp: str  # ISO 8601 UTC
    end_timestamp: str  # ISO 8601 UTC, exclusive
    mode: str
    duration_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start_timestamp,
            "end": self.end_timestamp,
            "mode": self.mode,
            "duration_seconds": round(self.duration_seconds, 3),
        }


@dataclass(frozen=True)
class DetectedEvent:
    """One auto-detected physical event (``cold_start``, ``hard_accel``, ...)."""

    timestamp: str  # ISO 8601 UTC — the event's reference moment
    kind: str
    fields: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "kind": self.kind,
            "fields": dict(self.fields),
        }


@dataclass(frozen=True)
class SessionClassification:
    """Full classifier output for one session."""

    schema_version: int
    classifier_version: str
    classified_at: str
    session_id: str
    duration_seconds: float
    mode_seconds: Mapping[str, float]
    event_counts: Mapping[str, int]
    mode_segments: tuple[ModeSegment, ...]
    events: tuple[DetectedEvent, ...]
    quality: Mapping[str, Any]
    disposition: Disposition

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "classifier_version": self.classifier_version,
            "classified_at": self.classified_at,
            "session_id": self.session_id,
            "duration_seconds": round(self.duration_seconds, 3),
            "summary": {
                "mode_seconds": {k: round(v, 3) for k, v in self.mode_seconds.items()},
                "event_counts": dict(self.event_counts),
            },
            "mode_segments": [segment.to_dict() for segment in self.mode_segments],
            "events": [event.to_dict() for event in self.events],
            "quality": dict(self.quality),
            "disposition": self.disposition,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionClassification:
        summary = dict(data.get("summary") or {})
        return cls(
            schema_version=int(data.get("schema_version", CLASSIFICATION_SCHEMA_VERSION)),
            classifier_version=str(data["classifier_version"]),
            classified_at=str(data["classified_at"]),
            session_id=str(data["session_id"]),
            duration_seconds=float(data["duration_seconds"]),
            mode_seconds={k: float(v) for k, v in dict(summary.get("mode_seconds", {})).items()},
            event_counts={k: int(v) for k, v in dict(summary.get("event_counts", {})).items()},
            mode_segments=tuple(
                ModeSegment(
                    start_timestamp=str(item["start"]),
                    end_timestamp=str(item["end"]),
                    mode=str(item["mode"]),
                    duration_seconds=float(item["duration_seconds"]),
                )
                for item in data.get("mode_segments", [])
            ),
            events=tuple(
                DetectedEvent(
                    timestamp=str(item["timestamp"]),
                    kind=str(item["kind"]),
                    fields=dict(item.get("fields") or {}),
                )
                for item in data.get("events", [])
            ),
            quality=dict(data.get("quality") or {}),
            disposition=str(data.get("disposition", "ready")),  # type: ignore[arg-type]
        )


def read_classification(session_dir: Path) -> SessionClassification | None:
    """Load the sidecar, or ``None`` if absent / unreadable."""
    data = read_json_object(session_dir / CLASSIFICATION_FILENAME)
    if data is None:
        return None
    try:
        return SessionClassification.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


__all__ = [
    "CLASSIFICATION_FILENAME",
    "CLASSIFICATION_SCHEMA_VERSION",
    "DERIVED_TAGS_FILENAME",
    "DIGEST_FILENAME",
    "DIGEST_SKIPPED_FILENAME",
    "FINGERPRINT_FILENAME",
    "FINGERPRINT_SCHEMA_VERSION",
    "ROLLING_CIRCUMFERENCE_FILENAME",
    "DetectedEvent",
    "Disposition",
    "ModeSegment",
    "SessionClassification",
    "SessionFingerprint",
    "read_classification",
    "read_digest",
    "read_digest_skipped",
    "read_fingerprint",
    "read_rolling_circumference",
]
