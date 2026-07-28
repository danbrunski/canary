"""Session discovery and export helpers for the control API."""

from __future__ import annotations

import json
import logging
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.atomic_write import atomic_write_text
from src.preprocessing.loader import PARQUET_PATTERN
from src.session import MANIFEST_FILENAME, TAGS_FILENAME

log = logging.getLogger(__name__)

METADATA_FILENAME = "control.json"
PIN_FILENAME = ".canary-pin"

_SESSION_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}$")


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


def list_sessions(root: Path) -> list[SessionSummary]:
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
    if not root.exists():
        return []
    sessions = [
        summarize_session(child)
        for child in root.iterdir()
        if child.is_dir() and _SESSION_DIR_RE.match(child.name)
    ]
    return sorted(sessions, key=lambda session: session.session_id)


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


def resolve_session(root: Path, session_id: str) -> Path:
    """Resolve a session id under root, rejecting traversal and unknown ids."""
    if not _SESSION_DIR_RE.match(session_id):
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
    return SessionSummary(
        session_id=session.name,
        session_path=session,
        started_at=_parse_session_started_at(session.name),
        telemetry_chunks=len(chunks),
        tag_count=tag_count,
        has_manifest=(session / MANIFEST_FILENAME).exists(),
        pinned=is_session_pinned(session),
        metadata=metadata,
        size_bytes=_dir_size(session),
    )


def read_session_metadata(session: Path) -> dict[str, Any]:
    path = session / METADATA_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_session_metadata(
    session: Path,
    metadata: dict[str, Any],
    *,
    merge: bool = True,
) -> dict[str, Any]:
    existing = read_session_metadata(session) if merge else {}
    updated = {**existing, **metadata, "updated_at": datetime.now(UTC).isoformat()}
    path = session / METADATA_FILENAME
    atomic_write_text(path, json.dumps(updated, indent=2, sort_keys=True, default=str) + "\n")
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
    _update_catalog(catalog_db, session)
    return result


def unpin_session(session: Path, *, catalog_db: Path | None = None) -> dict[str, Any]:
    (session / PIN_FILENAME).unlink(missing_ok=True)
    result = write_session_metadata(
        session,
        {"pinned": False, "unpinned_at": datetime.now(UTC).isoformat()},
    )
    _update_catalog(catalog_db, session)
    return result


def _update_catalog(catalog_db: Path | None, session: Path) -> None:
    """Best-effort catalog re-index (Task 6.3) — the catalog is a rebuildable
    index, never allowed to block a real pin/unpin. ``None`` (the default)
    skips it entirely so callers that never wire one up never touch that
    file."""
    if catalog_db is None:
        return
    try:
        from src.storage.catalog import SessionCatalog

        with SessionCatalog(catalog_db) as catalog:
            catalog.record_session(session)
    except Exception:  # noqa: BLE001 — see docstring
        log.exception("catalog update failed for %s", session.name)


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


def _parse_session_started_at(session_id: str) -> str | None:
    try:
        return datetime.strptime(session_id, "%Y-%m-%dT%H%M%S").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


__all__ = [
    "SessionSummary",
    "delete_session",
    "filter_sessions",
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
