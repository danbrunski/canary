"""``trip_summary.json`` session sidecar (Task 1.4, engine-analysis-and-mapping.md).

:func:`~src.analysis.derived.summarize_trip` is pure (frame in, ``TripSummary``
out); this module is the on-demand compute + persist wrapper the HTTP
endpoint, MCP tool, and CLI (1.5/1.7) share. There is no session-close hook
yet — the trip-end hook proposed in ``eval-longitudinal-and-dtc.md`` Phase 5.4
hasn't landed in this codebase (grepped: no ``digest``/trip-end/session-close
wiring exists) — so :func:`get_or_compute_trip_summary` computes from the
stored session the first time it's asked for and caches the result, the same
"compute once, read the cache after" shape ``compute_fingerprint`` /
``write_fingerprint`` use for the fingerprint sidecar (Task 4.1).

Loads via the same ``read → wide → resample → segment``
(:func:`~src.preprocessing.loader.resample_and_segment`) chain
``compute_fingerprint`` and ``classify_session`` use, so this sidecar can
never see a differently-shaped frame than the other post-hoc sidecars.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.analysis.derived import TripSummary, summarize_trip
from src.config import CanaryConfig
from src.preprocessing.loader import resample_and_segment
from src.timefmt import format_timestamp as _format_timestamp

TRIP_SUMMARY_FILENAME = "trip_summary.json"

#: Bump when the sidecar shape changes so a stale on-disk copy can be
#: detected (mirrors ``session_classifier.CLASSIFIER_VERSION`` /
#: ``fingerprint.FINGERPRINT_VERSION``). Nothing recomputes on a version
#: bump automatically yet — ``force=True`` is the escape hatch.
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TripSummarySidecar:
    """Full sidecar payload for one session."""

    schema_version: int
    computed_at: str
    session_id: str
    summary: TripSummary

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "computed_at": self.computed_at,
            "session_id": self.session_id,
            "summary": self.summary.to_dict(),
        }


def compute_trip_summary(session_dir: Path, config: CanaryConfig) -> TripSummary:
    """Compute the :class:`TripSummary` for one recorded session.

    Raises ``FileNotFoundError`` when the session has no telemetry parquet
    (mirrors ``compute_fingerprint`` / ``classify_session`` — caller treats
    it as "skip", not "quarantine").
    """
    chunks = sorted(session_dir.glob("telemetry-*.parquet"))
    if not chunks:
        raise FileNotFoundError(f"no telemetry parquet under {session_dir}")
    frame = resample_and_segment([session_dir], config, value="value_number")
    return summarize_trip(frame, config.vehicle.parameters)


def write_trip_summary(
    session_dir: Path,
    summary: TripSummary,
    *,
    now: datetime | None = None,
) -> Path:
    """Write the ``trip_summary.json`` sidecar (atomic tmp+rename, matching
    ``control.json`` — same pattern as ``classification.json`` / ``fingerprint.json``
    / ``dtc.json``). Returns the path. Idempotent."""
    sidecar = TripSummarySidecar(
        schema_version=SCHEMA_VERSION,
        computed_at=_format_timestamp(datetime.now(UTC) if now is None else now),
        session_id=session_dir.name,
        summary=summary,
    )
    path = session_dir / TRIP_SUMMARY_FILENAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sidecar.to_dict(), indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return path


def read_trip_summary(session_dir: Path) -> dict[str, Any] | None:
    """Load the sidecar dict, or ``None`` if absent / unreadable."""
    path = session_dir / TRIP_SUMMARY_FILENAME
    if not path.exists():
        return None
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def get_or_compute_trip_summary(
    session_dir: Path,
    config: CanaryConfig,
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    """Serve the sidecar, computing + persisting it on first request.

    This is the "on demand" half of Task 1.4: no live session-close hook
    exists yet, so the first caller (HTTP endpoint, MCP tool, or
    ``scripts/trip_report.py``) after a drive pays the one-time compute cost
    and every caller after that reads the cached file. Returns ``None`` when
    the session directory doesn't exist or has no telemetry recorded yet —
    never raises, so callers can turn that straight into a 404.
    """
    if not session_dir.is_dir():
        return None
    if not force:
        cached = read_trip_summary(session_dir)
        if cached is not None:
            return cached
    try:
        summary = compute_trip_summary(session_dir, config)
    except FileNotFoundError:
        return None
    write_trip_summary(session_dir, summary)
    return read_trip_summary(session_dir)


__all__ = [
    "SCHEMA_VERSION",
    "TRIP_SUMMARY_FILENAME",
    "TripSummarySidecar",
    "compute_trip_summary",
    "get_or_compute_trip_summary",
    "read_trip_summary",
    "write_trip_summary",
]
