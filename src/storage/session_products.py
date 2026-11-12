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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from src.storage.sidecar import read_json_object
from src.timefmt import parse_iso_utc

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


# A session can hold several trips (D03.63): a whole-drive capture, or a key
# cycle the daemon rode through, ends more than one drive inside one directory.
# ``digest.json`` / ``digest_skipped.json`` stay what they always were, the
# session's *current* answer, and are always the newest trip's. Each trip's own
# answer is kept as well, in ``trip_digests/``, keyed by when the trip began.

#: Per-trip records, one directory per session: ``<key>.json`` is a trip's
#: digest, ``<key>.skipped.json`` the refusal that stands in for one.
TRIP_DIGESTS_DIRNAME = "trip_digests"

#: The key of a digest that names no trip (computed on demand, or by a caller
#: with no tracker edge): it covers the session as a whole.
SESSION_TRIP_KEY = "session"

_SKIPPED_SUFFIX = ".skipped.json"


def trip_started_at(trip: Mapping[str, Any] | None) -> datetime | None:
    """When the trip began, parsed (UTC), or ``None`` for a record naming no trip.

    The one way to order trips. Never compare :func:`trip_key` strings or file
    names for "which is newer": a key is an identity, not a clock, and the same
    instant can be written with ``Z`` or ``+00:00`` or fractional seconds.
    """
    started = trip.get("started_at") if trip else None
    if not isinstance(started, str):
        return None
    try:
        return parse_iso_utc(started)
    except ValueError:
        return None


def trip_key(trip: Mapping[str, Any] | None) -> str:
    """The key a digest or refusal is filed under, from its ``trip`` block.

    The trip's first motion (``trip.started_at``) identifies it: unique inside
    a session, stable between the tracker's edge and the shutdown fallback that
    may name the same trip again, and the same whichever way the instant is
    spelled (``Z`` or ``+00:00``). A record with no trip block (on-demand, or
    written before D03.63 by a caller without one) is :data:`SESSION_TRIP_KEY`.
    """
    started = trip_started_at(trip)
    if started is None:
        return SESSION_TRIP_KEY
    fraction = f"{started.microsecond:06d}" if started.microsecond else ""
    return started.strftime("%Y%m%dT%H%M%S") + fraction + "Z"


def trip_digest_path(session_dir: Path, key: str) -> Path:
    """Where trip ``key``'s digest is filed."""
    return session_dir / TRIP_DIGESTS_DIRNAME / f"{key}.json"


def trip_digest_skipped_path(session_dir: Path, key: str) -> Path:
    """Where trip ``key``'s refusal is filed."""
    return session_dir / TRIP_DIGESTS_DIRNAME / f"{key}{_SKIPPED_SUFFIX}"


def read_trip_digest(session_dir: Path, key: str) -> dict[str, Any] | None:
    """The digest filed for trip ``key``, or ``None``.

    A session digested before D03.63 holds only ``digest.json``; it is that
    trip's digest when its own trip block says so.
    """
    stored = read_json_object(trip_digest_path(session_dir, key))
    if stored is not None:
        return stored
    legacy = read_digest(session_dir)
    return legacy if legacy is not None and trip_key(legacy.get("trip")) == key else None


def read_trip_digest_skipped(session_dir: Path, key: str) -> dict[str, Any] | None:
    """The refusal filed for trip ``key``, or ``None`` (legacy rule as above)."""
    stored = read_json_object(trip_digest_skipped_path(session_dir, key))
    if stored is not None:
        return stored
    legacy = read_digest_skipped(session_dir)
    return legacy if legacy is not None and trip_key(legacy.get("trip")) == key else None


def list_trip_records(session_dir: Path) -> list[dict[str, Any]]:
    """Every trip's answer in this session, oldest trip first.

    One entry per trip: ``{"trip_key", "status", "trip", "record"}`` with
    ``status`` ``"digest"`` or ``"skipped"`` (a digest wins if a directory ever
    holds both) and ``record`` the stored payload. A session digested before
    D03.63 contributes its top-level answer as one entry. Empty when nothing
    was ever attempted.
    """
    found: dict[str, dict[str, Any]] = {}
    directory = session_dir / TRIP_DIGESTS_DIRNAME
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            skipped = path.name.endswith(_SKIPPED_SUFFIX)
            key = path.name[: -len(_SKIPPED_SUFFIX)] if skipped else path.stem
            record = read_json_object(path)
            if record is None or (skipped and key in found):
                continue
            found[key] = {
                "trip_key": key,
                "status": "skipped" if skipped else "digest",
                "trip": record.get("trip"),
                "record": record,
            }
    for status, record in (
        ("digest", read_digest(session_dir)),
        ("skipped", read_digest_skipped(session_dir)),
    ):
        if record is None:
            continue
        key = trip_key(record.get("trip"))
        if key not in found:
            found[key] = {
                "trip_key": key,
                "status": status,
                "trip": record.get("trip"),
                "record": record,
            }
    # Oldest trip first by parsed start time; the trip-less session record leads.
    epoch = datetime.min.replace(tzinfo=UTC)

    def order(key: str) -> tuple[bool, datetime, str]:
        started = trip_started_at(found[key]["trip"])
        return (key != SESSION_TRIP_KEY, started or epoch, key)

    return [found[key] for key in sorted(found, key=order)]


def trip_index(session_dir: Path) -> list[dict[str, Any]]:
    """A summary line per trip (:func:`list_trip_records` without the payloads).

    ``trip_key``, ``status``, the trip's ``started_at`` / ``stopped_at`` /
    ``end_session`` when it names them, and ``reason`` / ``detail`` for a
    refusal; what the digest route attaches as ``trips`` so a reader of the
    session's newest answer can see the others exist.
    """
    index: list[dict[str, Any]] = []
    for entry in list_trip_records(session_dir):
        trip = entry["trip"] if isinstance(entry["trip"], dict) else {}
        line: dict[str, Any] = {
            "trip_key": entry["trip_key"],
            "status": entry["status"],
            "started_at": trip.get("started_at"),
            "stopped_at": trip.get("stopped_at"),
            "end_session": trip.get("end_session"),
        }
        if entry["status"] == "skipped":
            line["reason"] = entry["record"].get("reason")
            line["detail"] = entry["record"].get("detail")
        index.append(line)
    return index


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
    "SESSION_TRIP_KEY",
    "TRIP_DIGESTS_DIRNAME",
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
    "read_trip_digest",
    "read_trip_digest_skipped",
    "list_trip_records",
    "trip_digest_path",
    "trip_index",
    "trip_digest_skipped_path",
    "trip_key",
    "trip_started_at",
]
