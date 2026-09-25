"""The Engine Tuning capture browser (W04.1).

A listing, not an analysis: everything here comes from sidecars and Parquet
footers (row counts, row-group timestamp statistics), so it never loads a
session and needs no child process. Ordered newest first by
:func:`~src.control.sessions.session_started_at` — never by directory name,
since ``_capture-…`` prefixes sort before dates.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.analysis.tune_revisions import TuneRevisionRegistry
from src.analysis.workbench import revision_boundaries, session_revision, session_span
from src.config import CanaryConfig
from src.control.calibration import active_registry
from src.control.capture_manifest import read_capture_manifest, read_capture_quality
from src.control.sessions import is_session_dir_name, session_started_at
from src.session import TAGS_FILENAME

#: Default page of the browser. Each row opens every chunk's footer, so an
#: unbounded listing over months of ordinary sessions is not free.
DEFAULT_LOG_LIMIT = 100


def is_capture_dir(session: Path) -> bool:
    """Captures carry a storage prefix (``_capture-…``); plain sessions do not."""
    return session.name.startswith("_")


def summarize_log(session: Path, registry: TuneRevisionRegistry | None) -> dict[str, Any]:
    manifest = read_capture_manifest(session)
    quality = read_capture_quality(session)
    started = session_started_at(session)
    first, last, rows = session_span(session)
    ended = None if manifest is None else manifest.ended_at
    begin = started or first
    finish = ended or last
    duration = (
        (finish - begin).total_seconds()
        if isinstance(begin, datetime) and isinstance(finish, datetime)
        else None
    )
    tags = session / TAGS_FILENAME
    tag_count = (
        sum(1 for line in tags.read_text().splitlines() if line.strip()) if tags.exists() else 0
    )
    return {
        "session_id": session.name,
        "is_capture": is_capture_dir(session),
        "profile": None if manifest is None else manifest.profile,
        "started_at": None if started is None else started.isoformat(),
        "duration_s": duration,
        "duration_source": (
            "capture_manifest" if ended is not None else ("parquet_span" if last else None)
        ),
        "row_count": rows,
        "revision": session_revision(session, registry),
        "revision_boundaries": revision_boundaries(registry, begin, last or ended),
        "quality": (
            None
            if quality is None
            else {"verdict": quality.get("verdict"), "scope": quality.get("scope")}
        ),
        "end_reason": None if manifest is None else manifest.end_reason,
        "tag_count": tag_count,
    }


def list_logs(
    config: CanaryConfig,
    *,
    include_sessions: bool = False,
    limit: int = DEFAULT_LOG_LIMIT,
) -> dict[str, Any]:
    """Captures (and, when asked, ordinary sessions), newest first."""
    root = config.collection.storage_path
    registry = active_registry(config)
    candidates: list[tuple[datetime, Path]] = []
    if root.exists():
        for child in root.iterdir():
            if not child.is_dir() or not is_session_dir_name(child.name):
                continue
            if not include_sessions and not is_capture_dir(child):
                continue
            started = session_started_at(child)
            if started is not None:
                candidates.append((started, child))
    candidates.sort(key=lambda item: item[0], reverse=True)
    total = len(candidates)
    page = candidates[: max(1, limit)]
    return {
        "include_sessions": include_sessions,
        "total": total,
        "limit": limit,
        "capped": total > len(page),
        "logs": [summarize_log(session, registry) for _started, session in page],
    }


__all__ = ["DEFAULT_LOG_LIMIT", "is_capture_dir", "list_logs", "summarize_log"]
