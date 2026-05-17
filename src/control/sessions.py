"""Session discovery and export helpers for the control API."""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.preprocessing.loader import PARQUET_PATTERN
from src.session import MANIFEST_FILENAME, TAGS_FILENAME

_SESSION_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}$")


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    session_path: Path
    started_at: str | None
    telemetry_chunks: int
    tag_count: int
    has_manifest: bool
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_path": str(self.session_path),
            "started_at": self.started_at,
            "telemetry_chunks": self.telemetry_chunks,
            "tag_count": self.tag_count,
            "has_manifest": self.has_manifest,
            "size_bytes": self.size_bytes,
        }


def list_sessions(root: Path) -> list[SessionSummary]:
    """Return timestamp-named session directories under `root`, newest last."""
    if not root.exists():
        return []
    sessions = [
        summarize_session(child)
        for child in root.iterdir()
        if child.is_dir() and _SESSION_DIR_RE.match(child.name)
    ]
    return sorted(sessions, key=lambda session: session.session_id)


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
    return SessionSummary(
        session_id=session.name,
        session_path=session,
        started_at=_parse_session_started_at(session.name),
        telemetry_chunks=len(chunks),
        tag_count=tag_count,
        has_manifest=(session / MANIFEST_FILENAME).exists(),
        size_bytes=_dir_size(session),
    )


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
    "list_sessions",
    "resolve_session",
    "summarize_session",
    "write_session_zip",
]
