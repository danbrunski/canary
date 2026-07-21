"""Post-hoc classification of a recorded session.

The daemon writes ``telemetry-*.parquet`` and operator-emitted
``tags.jsonl`` per ignition cycle, but doesn't persist the operating
mode or any auto-detected physical events. ``classify_session`` is the
retrospective pass that closes that gap: it re-runs the same preprocessing
the live daemon does (``read → wide → resample → segment``), walks the
``operating_mode`` column to extract contiguous mode windows, runs the
event-detector library over the resampled frame, and writes one
``classification.json`` sidecar into the session directory.

The sidecar is self-contained — it carries a schema version, a
``classifier_version`` string, mode durations, the segment timeline,
detected events, a quality block, and an overall disposition
(``ready`` / ``review`` / ``quarantine``). Re-running with a newer
classifier overwrites in place; older sidecars from a previous
classifier version are detected via ``classifier_version`` so a
corpus-wide rerun can skip already-current sessions.

This module deliberately stays out of the daemon hot path. Live
inference still sees ``operating_mode`` via the existing segmenter; the
classifier exists to label completed sessions for retrospective
analysis and (more importantly) to gate per-mode behavior-model refits.
See [[project_capture_paths_and_archive]] for related quarantine
mechanics; sessions classified ``quarantine`` are eligible to be
moved under ``_archive/`` by ``scripts/classify_sessions.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from src.atomic_write import atomic_write_text
from src.config import CanaryConfig
from src.preprocessing.loader import numeric_value_columns, resample_and_segment
from src.preprocessing.resampler import (
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
)
from src.preprocessing.segmenter import (
    MODE_COLUMN,
    UNKNOWN_MODE,
)
from src.timefmt import format_timestamp as _format_timestamp

CLASSIFICATION_FILENAME = "classification.json"
DERIVED_TAGS_FILENAME = "derived_tags.jsonl"

#: Increment when the classifier semantics change (new detectors, changed
#: disposition rules, new fields). Stale sidecars are detected via
#: comparison against this so the corpus can be re-classified in batch.
CLASSIFIER_VERSION = "0.2.0"

SCHEMA_VERSION = 1

Disposition = Literal["ready", "review", "quarantine"]

#: When more than this fraction of session rows have ``operating_mode ==
#: unknown``, the session is quarantined — the segmenter couldn't
#: classify the regime, so per-mode behavior models can't fit on it.
#: This is the *primary* quality gate because OBD round-robin polling
#: structurally means most rows have many NaN feature columns at any
#: given second (we round-robin one PID per second across ~30 PIDs).
#: A row-level "complete features" check would mark every real session
#: as bad data; mode coverage is the meaningful signal.
_DEFAULT_QUARANTINE_UNKNOWN_FRACTION = 0.9

#: Sessions between ``_DEFAULT_REVIEW_UNKNOWN_FRACTION`` and
#: ``_DEFAULT_QUARANTINE_UNKNOWN_FRACTION`` go to ``review`` so the
#: operator can decide whether to use them as training input.
_DEFAULT_REVIEW_UNKNOWN_FRACTION = 0.3


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
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
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


# --------------------------------------------------------------------------- #
# Classifier driver
# --------------------------------------------------------------------------- #


def classify_session(
    session_dir: Path,
    config: CanaryConfig,
    *,
    now: datetime | None = None,
    event_detectors: Iterable[
        EventDetectorCallable  # forward ref — defined below
    ]
    | None = None,
) -> SessionClassification:
    """Classify one recorded session into modes + events + disposition.

    Pure-ish: reads Parquet from ``session_dir``, doesn't mutate the
    session. Caller persists via :func:`write_classification` if desired.

    Raises ``FileNotFoundError`` when the session has no Parquet chunks
    (caller should treat as "skip", not "quarantine").
    """
    chunks = sorted(session_dir.glob("telemetry-*.parquet"))
    if not chunks:
        raise FileNotFoundError(f"no telemetry parquet under {session_dir}")

    segmented = resample_and_segment([session_dir], config, value="value_number")
    if segmented.empty:
        return _empty_classification(session_dir.name, now)

    mode_segments = _extract_mode_segments(segmented)
    mode_seconds = _aggregate_mode_seconds(mode_segments)
    duration_s = float(sum(mode_seconds.values()))

    detectors = list(event_detectors) if event_detectors is not None else _default_detectors()
    events: list[DetectedEvent] = []
    for detector in detectors:
        try:
            events.extend(detector(segmented, mode_segments))
        except Exception:  # noqa: BLE001 — one bad detector shouldn't kill the rest
            pass
    events = sorted(events, key=lambda event: event.timestamp)
    event_counts = _aggregate_event_counts(events)

    quality = _quality_block(segmented, mode_seconds, duration_s)
    disposition = _decide_disposition(quality, mode_seconds, duration_s)

    classified_at = _format_timestamp(datetime.now(UTC) if now is None else now)
    return SessionClassification(
        schema_version=SCHEMA_VERSION,
        classifier_version=CLASSIFIER_VERSION,
        classified_at=classified_at,
        session_id=session_dir.name,
        duration_seconds=duration_s,
        mode_seconds=mode_seconds,
        event_counts=event_counts,
        mode_segments=tuple(mode_segments),
        events=tuple(events),
        quality=quality,
        disposition=disposition,
    )


def write_classification(session_dir: Path, classification: SessionClassification) -> Path:
    """Write the sidecar JSON and return its path. Idempotent."""
    path = session_dir / CLASSIFICATION_FILENAME
    atomic_write_text(path, json.dumps(classification.to_dict(), indent=2, sort_keys=True) + "\n")
    _write_derived_tags(session_dir, classification)
    return path


def read_classification(session_dir: Path) -> SessionClassification | None:
    """Load the sidecar, or ``None`` if absent / unreadable."""
    path = session_dir / CLASSIFICATION_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return SessionClassification.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def is_stale(classification: SessionClassification | None) -> bool:
    """True when a sidecar is missing or its classifier_version is behind."""
    if classification is None:
        return True
    return classification.classifier_version != CLASSIFIER_VERSION


# --------------------------------------------------------------------------- #
# Helpers (pure)
# --------------------------------------------------------------------------- #


def _empty_classification(session_id: str, now: datetime | None) -> SessionClassification:
    classified_at = _format_timestamp(datetime.now(UTC) if now is None else now)
    return SessionClassification(
        schema_version=SCHEMA_VERSION,
        classifier_version=CLASSIFIER_VERSION,
        classified_at=classified_at,
        session_id=session_id,
        duration_seconds=0.0,
        mode_seconds={},
        event_counts={},
        mode_segments=(),
        events=(),
        quality={"reason": "empty_session"},
        disposition="quarantine",
    )


def _extract_mode_segments(segmented: pd.DataFrame) -> list[ModeSegment]:
    """Walk the ``operating_mode`` column and emit contiguous segments.

    Assumes ``segmented`` is sorted by ``TIMESTAMP_COLUMN`` (the
    resampler enforces this). A segment ends when the mode label changes
    or the session boundary appears.
    """
    if segmented.empty or MODE_COLUMN not in segmented.columns:
        return []
    ts = pd.to_datetime(segmented[TIMESTAMP_COLUMN], utc=True)
    modes = segmented[MODE_COLUMN].astype("string")
    sessions = (
        segmented[SESSION_COLUMN].astype("string")
        if SESSION_COLUMN in segmented.columns
        else pd.Series([""] * len(segmented), index=segmented.index)
    )

    segments: list[ModeSegment] = []
    if len(segmented) == 0:
        return segments

    seg_start_idx = 0
    for idx in range(1, len(segmented)):
        mode_changed = modes.iloc[idx] != modes.iloc[seg_start_idx]
        session_changed = sessions.iloc[idx] != sessions.iloc[seg_start_idx]
        if mode_changed or session_changed:
            segments.append(_segment_from_slice(ts, modes, seg_start_idx, idx))
            seg_start_idx = idx
    segments.append(_segment_from_slice(ts, modes, seg_start_idx, len(segmented)))
    return segments


def _segment_from_slice(
    ts: pd.Series,
    modes: pd.Series,
    start_idx: int,
    end_idx: int,
) -> ModeSegment:
    start_ts = ts.iloc[start_idx]
    # End-exclusive: a segment of length-1 sample takes one sampling interval
    # by convention. For length-N segments the end is the next sample's
    # timestamp; for length-1 segments we approximate by using the same
    # start_ts (zero duration), which the aggregator clamps to >= 0.
    if end_idx < len(ts):
        end_ts = ts.iloc[end_idx]
    else:
        # Final segment: extend by the typical sample period (last-1 → last
        # delta) so the session's total duration roughly matches wall time.
        if len(ts) >= 2:
            delta = ts.iloc[-1] - ts.iloc[-2]
        else:
            delta = pd.Timedelta(seconds=1)
        end_ts = ts.iloc[-1] + delta
    duration = max(0.0, (end_ts - start_ts).total_seconds())
    return ModeSegment(
        start_timestamp=_format_timestamp(start_ts),
        end_timestamp=_format_timestamp(end_ts),
        mode=str(modes.iloc[start_idx]) if modes.iloc[start_idx] is not None else UNKNOWN_MODE,
        duration_seconds=duration,
    )


def _aggregate_mode_seconds(segments: Iterable[ModeSegment]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for segment in segments:
        totals[segment.mode] = totals.get(segment.mode, 0.0) + segment.duration_seconds
    return totals


def _aggregate_event_counts(events: Iterable[DetectedEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event.kind] = counts.get(event.kind, 0) + 1
    return counts


def _quality_block(
    segmented: pd.DataFrame,
    mode_seconds: Mapping[str, float],
    duration_seconds: float,
) -> dict[str, Any]:
    """Per-session quality metrics that feed the disposition decision."""
    feature_columns = numeric_value_columns(segmented)
    if not feature_columns or segmented.empty:
        return {"complete_row_fraction": 0.0, "feature_columns": 0}
    complete = segmented[feature_columns].notna().all(axis=1).sum()
    complete_fraction = float(complete / max(len(segmented), 1))
    unknown_seconds = mode_seconds.get(UNKNOWN_MODE, 0.0)
    unknown_fraction = unknown_seconds / duration_seconds if duration_seconds > 0 else 0.0
    return {
        "complete_row_fraction": round(complete_fraction, 4),
        "unknown_mode_fraction": round(unknown_fraction, 4),
        "feature_columns": len(feature_columns),
        "row_count": int(len(segmented)),
    }


def _decide_disposition(
    quality: Mapping[str, Any],
    mode_seconds: Mapping[str, float],
    duration_seconds: float,
) -> Disposition:
    """Decide based on operating-mode coverage, not per-row feature completeness.

    Rationale: OBD polling is round-robin so any one row has most
    columns NaN. The per-row "complete features" metric is informational
    but not a usable disposition signal — the IF / LSTM trainers already
    drop missing-feature rows internally and still produce a working
    model from a session with 10% complete rows. What actually matters
    is whether the segmenter could assign modes to most rows.
    """
    if duration_seconds <= 0:
        return "quarantine"
    unknown_fraction = float(quality.get("unknown_mode_fraction", 0.0))
    if unknown_fraction >= _DEFAULT_QUARANTINE_UNKNOWN_FRACTION:
        return "quarantine"
    if unknown_fraction >= _DEFAULT_REVIEW_UNKNOWN_FRACTION:
        return "review"
    return "ready"


def _write_derived_tags(session_dir: Path, classification: SessionClassification) -> None:
    """Append auto-emitted events to ``derived_tags.jsonl``.

    Kept separate from the operator-emitted ``tags.jsonl`` so the daemon's
    capture controller never accidentally re-emits an auto-tag as if it
    were an operator action — the two flows must stay distinguishable.
    """
    if not classification.events:
        return
    path = session_dir / DERIVED_TAGS_FILENAME
    # Overwrite (idempotent): classifier output is derived, not append-only.
    lines: list[str] = []
    for event in classification.events:
        record = {
            "label": event.kind,
            "kind": "derived",
            "timestamp": event.timestamp,
            "classifier_version": classification.classifier_version,
            "fields": dict(event.fields),
        }
        lines.append(json.dumps(record, sort_keys=True))
    path.write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Detector protocol + default registry
# --------------------------------------------------------------------------- #


from typing import Protocol  # noqa: E402 — placed late to keep imports clean


class EventDetectorCallable(Protocol):
    """One detector function: takes a segmented frame + mode timeline, returns events."""

    def __call__(
        self,
        segmented: pd.DataFrame,
        mode_segments: list[ModeSegment],
    ) -> list[DetectedEvent]: ...


def _default_detectors() -> list[EventDetectorCallable]:
    """Wired-in detectors that ship with the classifier.

    Deferred import to avoid a hard dependency cycle with the events
    package (each detector imports ``DetectedEvent`` / ``ModeSegment``
    from this module).
    """
    from src.analysis.events import default_detectors

    return list(default_detectors())


__all__ = [
    "CLASSIFICATION_FILENAME",
    "CLASSIFIER_VERSION",
    "DERIVED_TAGS_FILENAME",
    "DetectedEvent",
    "Disposition",
    "EventDetectorCallable",
    "ModeSegment",
    "SCHEMA_VERSION",
    "SessionClassification",
    "classify_session",
    "is_stale",
    "read_classification",
    "write_classification",
]
