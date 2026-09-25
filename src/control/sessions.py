"""Session discovery and export helpers for the control API."""

from __future__ import annotations

import json
import logging
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.control.capture_manifest import read_capture_manifest
from src.preprocessing.loader import PARQUET_PATTERN
from src.session import (
    ALL_SESSION_KINDS,
    LIVE_SESSION_KINDS,
    MANIFEST_FILENAME,
    RECORDED_SESSION_KINDS,
    TAGS_FILENAME,
    SessionKind,
    classify_session_name,
    dir_size,
    iter_sessions,
)
from src.storage.sidecar import read_json_object, write_json_sidecar

log = logging.getLogger(__name__)

METADATA_FILENAME = "control.json"
PIN_FILENAME = ".canary-pin"


def session_started_at(session: str | Path) -> datetime | None:
    """Parse a session directory's UTC start time from its name.

    The sanctioned API, a thin wrapper over
    :func:`src.session.classify_session_name` (R01.1), the one parser.
    Ordering sessions any other way (by directory name, by mtime) is a bug
    waiting for a capture prefix to expose it: ``_capture-`` sorts before
    ``_tuning-`` regardless of date, so a capture a month later compares as
    earlier.
    """
    name = session.name if isinstance(session, Path) else session
    parsed = classify_session_name(name)
    return None if parsed is None else parsed.started_at


def latest_session(root: Path) -> Path | None:
    """The session most recently *written*, captures included.

    Distinct from :func:`src.preprocessing.loader.discover_sessions`, which
    excludes ``_``-prefixed directories so captures stay out of the training
    corpus. That exclusion is right for training and wrong for "what is live":
    reusing it meant every live read — /status, /telemetry/latest, OBD
    coverage, the replay guard — silently fell back to the previous session
    for the whole duration of a capture, showing frozen data with nothing
    saying so. Two questions, two functions.
    """
    dated = [
        child
        for child in iter_sessions(root, kinds=ALL_SESSION_KINDS)
        # Must hold data. A session directory exists from the moment the
        # writer rotates into it, before the first chunk flushes, so without
        # this the newest directory is briefly an empty one and every live
        # read returns nothing — the same "shows the wrong session" failure
        # in a new costume. Mirrors `discover_sessions`' own predicate.
        if any(chunk.stat().st_size > 0 for chunk in child.glob("telemetry-*.parquet"))
    ]
    return dated[-1] if dated else None


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    session_path: Path
    started_at: str | None
    telemetry_chunks: int
    tag_count: int
    has_manifest: bool
    pinned: bool
    metadata: dict[str, Any]
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_path": str(self.session_path),
            "started_at": self.started_at,
            "telemetry_chunks": self.telemetry_chunks,
            "tag_count": self.tag_count,
            "has_manifest": self.has_manifest,
            "pinned": self.pinned,
            "metadata": self.metadata,
            "size_bytes": self.size_bytes,
        }


def list_sessions(root: Path, *, captures: bool = False) -> list[SessionSummary]:
    """Return timestamp-named session directories under `root`, newest last.

    Task 6.4(b) (eval-longitudinal-and-dtc.md): deliberately left reading the
    filesystem directly rather than the session catalog. This listing must
    show the *currently-recording* session too — which the catalog never has
    a row for until it's been classified/pinned/fingerprinted/digested at
    least once — so catalog-backing it would need to fall back to a
    filesystem check for exactly the freshest (most operationally relevant)
    session anyway. ``summarize_session`` per directory is also genuinely
    cheap (glob + a couple of small file reads), so there's no real perf win
    being left on the table today. Revisit if this listing's cost becomes a
    measured problem, not preemptively.
    """
    kinds = {SessionKind.CAPTURE} if captures else LIVE_SESSION_KINDS
    # Ordered by parsed start time (R01.1): with captures, name order sorted
    # by profile slug first, so ``filter_sessions(limit=N)`` kept the
    # lexically-last profiles rather than the newest captures.
    return [summarize_session(child) for child in iter_sessions(root, kinds=kinds)]


def filter_sessions(
    sessions: list[SessionSummary],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> list[SessionSummary]:
    """Apply UTC start-time bounds and keep the newest ``limit`` matches."""

    def as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    selected = sessions
    if since is not None:
        lower = as_utc(since)
        selected = [
            session
            for session in selected
            if session.started_at is not None
            and as_utc(datetime.fromisoformat(session.started_at)) >= lower
        ]
    if until is not None:
        upper = as_utc(until)
        selected = [
            session
            for session in selected
            if session.started_at is not None
            and as_utc(datetime.fromisoformat(session.started_at)) <= upper
        ]
    return selected[-limit:] if limit is not None else selected


def is_session_dir_name(name: str) -> bool:
    """True for directory names that are *this vehicle's own recording* — a
    plain live session (``2026-09-10T142711``) or a capture
    (``_capture-tuning-did-2026-09-17T231700``).

    The one predicate for "may a post-hoc sidecar be computed over this
    directory?". Replay output (``_replay-*``), quarantine (``_archive``) and
    scratch directories are deliberately not sessions: their rows are
    re-emitted or condemned data, so a digest/classification over them
    describes nothing real. ``resolve_session`` has enforced this for every
    HTTP read since it was written — which is exactly why the trip-end hook,
    the one caller that bypassed it, computed a digest over a 5.9 M-row
    ``_replay-*`` directory and got the daemon OOM-killed (D03.6 item 1). The
    predicate lives here so a non-HTTP caller gets the same refusal without
    having to remember it.
    """
    parsed = classify_session_name(name)
    return parsed is not None and parsed.kind in RECORDED_SESSION_KINDS


def resolve_session(root: Path, session_id: str) -> Path:
    """Resolve a session id under root, rejecting traversal and unknown ids."""
    if not is_session_dir_name(session_id):
        raise FileNotFoundError(f"session not found: {session_id}")
    session = root / session_id
    try:
        resolved = session.resolve()
        resolved_root = root.resolve()
    except OSError as exc:
        raise FileNotFoundError(f"session not found: {session_id}") from exc
    if resolved.parent != resolved_root or not resolved.is_dir():
        raise FileNotFoundError(f"session not found: {session_id}")
    return resolved


def summarize_session(session: Path) -> SessionSummary:
    chunks = sorted(session.glob(PARQUET_PATTERN))
    tags = session / TAGS_FILENAME
    tag_count = 0
    if tags.exists():
        tag_count = sum(1 for line in tags.read_text().splitlines() if line.strip())
    metadata = read_session_metadata(session)
    capture = read_capture_manifest(session)
    if capture is not None:
        metadata = {
            **metadata,
            "capture_profile": capture.profile,
            "tune_revision": capture.tune_revision,
            "capture_started_at": capture.started_at.isoformat(),
            "corpus_eligible": capture.corpus_eligible,
        }
    return SessionSummary(
        session_id=session.name,
        session_path=session,
        started_at=_started_at_iso(session.name),
        telemetry_chunks=len(chunks),
        tag_count=tag_count,
        has_manifest=(session / MANIFEST_FILENAME).exists(),
        pinned=is_session_pinned(session),
        metadata=metadata,
        size_bytes=dir_size(session),
    )


def read_session_metadata(session: Path) -> dict[str, Any]:
    return read_json_object(session / METADATA_FILENAME) or {}


def write_session_metadata(
    session: Path,
    metadata: dict[str, Any],
    *,
    merge: bool = True,
) -> dict[str, Any]:
    existing = read_session_metadata(session) if merge else {}
    updated = {**existing, **metadata, "updated_at": datetime.now(UTC).isoformat()}
    path = session / METADATA_FILENAME
    write_json_sidecar(path, updated, default=str)
    return updated


def is_session_pinned(session: Path) -> bool:
    return (session / PIN_FILENAME).exists()


def pin_session(
    session: Path, *, reason: str | None = None, catalog_db: Path | None = None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"pinned": True, "pinned_at": datetime.now(UTC).isoformat()}
    if reason is not None:
        metadata["pin_reason"] = reason
    (session / PIN_FILENAME).write_text(json.dumps(metadata, sort_keys=True) + "\n")
    result = write_session_metadata(session, metadata)
    # Lazy: src.storage.catalog imports this module's package at module level.
    from src.storage.catalog import record_session_best_effort

    record_session_best_effort(catalog_db, session)
    return result


def unpin_session(session: Path, *, catalog_db: Path | None = None) -> dict[str, Any]:
    (session / PIN_FILENAME).unlink(missing_ok=True)
    result = write_session_metadata(
        session,
        {"pinned": False, "unpinned_at": datetime.now(UTC).isoformat()},
    )
    # Lazy: src.storage.catalog imports this module's package at module level.
    from src.storage.catalog import record_session_best_effort

    record_session_best_effort(catalog_db, session)
    return result


def delete_session(session: Path, *, force: bool = False) -> None:
    if is_session_pinned(session) and not force:
        raise PermissionError("session is pinned; pass force=true to delete")
    shutil.rmtree(session)


def write_session_zip(session: Path, out_path: Path) -> Path:
    """Write a zip archive containing every regular file in `session`."""
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(session.rglob("*")):
            if not path.is_file():
                continue
            archive.write(path, arcname=str(Path(session.name) / path.relative_to(session)))
    return out_path


def _started_at_iso(session_id: str) -> str | None:
    started = session_started_at(session_id)
    return None if started is None else started.isoformat()


__all__ = [
    "SessionSummary",
    "delete_session",
    "filter_sessions",
    "is_session_dir_name",
    "is_session_pinned",
    "list_sessions",
    "pin_session",
    "read_session_metadata",
    "resolve_session",
    "summarize_session",
    "unpin_session",
    "write_session_metadata",
    "write_session_zip",
]
