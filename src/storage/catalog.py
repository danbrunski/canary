"""Session catalog — a rebuildable SQLite index over session sidecars (Task 6.1,
eval-longitudinal-and-dtc.md Phase 6A).

Modeled directly on :mod:`src.companion.library` (``_SCHEMA``, ``SCHEMA_VERSION``
+ a ``meta`` migration-guard table, a ``_tx`` transaction helper) — same
SQLite-wrapping style, daemon-side instead of laptop-side.

**Sidecars remain the source of truth; this catalog is a rebuildable index,
never a second copy of real data.** Every column here is reconstructable
from files already on disk for a session (``classification.json`` /
``fingerprint.json`` / ``dtc.json`` / ``digest.json`` / the Parquet chunks
themselves) via :func:`SessionCatalog.record_session`. Corruption recovery is
``scripts/catalog.py rebuild`` — delete the ``.db`` file (or let a corrupt one
be replaced) and rebuild from disk, never a restore-from-backup story. Every
write site that keeps this index current does so through
:func:`SessionCatalog.record_session` as a best-effort hook (never allowed to
block or fail the real sidecar write it rides along with) — callers pass an
explicit ``catalog_db: Path | None`` rather than this module reaching into
``DaemonConfig`` itself, so a test/tool that never wires a catalog never
touches the filesystem for one (mirrors the existing ``emitter: ... | None``
dependency-injection shape already used throughout the control layer).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.analysis.digest import DIGEST_FILENAME
from src.analysis.fingerprint import read_fingerprint
from src.analysis.session_classifier import read_classification
from src.control.dtc_state import SIDECAR_FILENAME as DTC_FILENAME

log = logging.getLogger(__name__)

__all__ = [
    "SCHEMA_VERSION",
    "CatalogSessionRow",
    "SessionCatalog",
]

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
    session_id         TEXT PRIMARY KEY,
    path                TEXT NOT NULL,
    started_at          TEXT,
    duration_s          REAL,
    row_count           INTEGER,
    size_bytes          INTEGER,
    pinned              INTEGER DEFAULT 0,
    disposition         TEXT,
    classifier_version  TEXT,
    mode_seconds        TEXT,  -- JSON
    event_counts        TEXT,  -- JSON
    quality             TEXT,  -- JSON
    fingerprint         TEXT,  -- JSON
    dtc                 TEXT,  -- JSON
    digest              TEXT,  -- JSON
    updated_at          TEXT NOT NULL
);
"""

#: Session directory names that are "live sessions" in the normal sense.
_LIVE_SESSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}$")
#: Underscore-prefixed variants that carry a real timestamp after a known
#: marker — ``_replay-<ts>`` (replay output) and ``_tuning-<ts>`` (engine-tuning
#: captures, ``engine-tuning-support.md``). Anything else underscore-prefixed
#: (e.g. bare ``_archive``, ``scratch``) has no timestamp of its own.
_PREFIXED_SESSION_RE = re.compile(r"^_(?:replay|tuning)-(\d{4}-\d{2}-\d{2}T\d{6})$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_started_at(session_id: str) -> str | None:
    """Best-effort ISO-8601 ``started_at`` from a session directory name."""
    candidate = session_id
    if match := _PREFIXED_SESSION_RE.match(session_id):
        candidate = match.group(1)
    try:
        return datetime.strptime(candidate, "%Y-%m-%dT%H%M%S").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _parquet_row_count(session_dir: Path) -> int | None:
    """Sum of row counts across ``telemetry-*.parquet`` chunks, via Parquet
    footer metadata only (no data read). ``None`` when nothing is readable —
    distinguishes "unknown" from "genuinely zero rows"."""
    try:
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover - pyarrow is a hard dependency elsewhere
        return None
    total = 0
    seen = False
    for chunk in sorted(session_dir.glob("telemetry-*.parquet")):
        try:
            if chunk.stat().st_size == 0:
                continue
            total += pq.ParquetFile(chunk).metadata.num_rows  # type: ignore[no-untyped-call]
            seen = True
        except Exception:  # noqa: BLE001 - a corrupt/truncated chunk just doesn't count
            continue
    return total if seen else None


def _read_dtc_sidecar(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / DTC_FILENAME
    if not path.exists():
        return None
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def _read_digest_sidecar(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / DIGEST_FILENAME
    if not path.exists():
        return None
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


@dataclass(frozen=True)
class CatalogSessionRow:
    session_id: str
    path: str
    started_at: str | None
    duration_s: float | None
    row_count: int | None
    size_bytes: int | None
    pinned: bool
    disposition: str | None
    classifier_version: str | None
    mode_seconds: dict[str, float]
    event_counts: dict[str, int]
    quality: dict[str, Any]
    fingerprint: dict[str, float] | None
    dtc: dict[str, Any] | None
    digest: dict[str, Any] | None
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "path": self.path,
            "started_at": self.started_at,
            "duration_s": self.duration_s,
            "row_count": self.row_count,
            "size_bytes": self.size_bytes,
            "pinned": self.pinned,
            "disposition": self.disposition,
            "classifier_version": self.classifier_version,
            "mode_seconds": self.mode_seconds,
            "event_counts": self.event_counts,
            "quality": self.quality,
            "fingerprint": self.fingerprint,
            "dtc": self.dtc,
            "digest": self.digest,
            "updated_at": self.updated_at,
        }

    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> CatalogSessionRow:
        return cls(
            session_id=row["session_id"],
            path=row["path"],
            started_at=row["started_at"],
            duration_s=row["duration_s"],
            row_count=row["row_count"],
            size_bytes=row["size_bytes"],
            pinned=bool(row["pinned"]),
            disposition=row["disposition"],
            classifier_version=row["classifier_version"],
            mode_seconds=json.loads(row["mode_seconds"] or "{}"),
            event_counts=json.loads(row["event_counts"] or "{}"),
            quality=json.loads(row["quality"] or "{}"),
            fingerprint=json.loads(row["fingerprint"]) if row["fingerprint"] else None,
            dtc=json.loads(row["dtc"]) if row["dtc"] else None,
            digest=json.loads(row["digest"]) if row["digest"] else None,
            updated_at=row["updated_at"],
        )


class SessionCatalog:
    """The session catalog: one SQLite file, one ``sessions`` table.

    Single-process-friendly (a private lock guards the shared connection,
    same as ``CanaryLibrary``); SQLite's own file lock covers the occasional
    concurrent reader/writer across the daemon's async tasks.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL + a busy timeout: several independent processes can legitimately
        # touch this file at once (the daemon's async tasks, a control-route
        # request, scripts/catalog.py run by hand) — WAL lets readers proceed
        # against an in-progress writer instead of hitting SQLITE_BUSY, and
        # the timeout makes a genuine writer-vs-writer collision retry instead
        # of erroring immediately.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(_SCHEMA)
        cur = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
        if cur.fetchone() is None:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SessionCatalog:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # -- incremental update ---------------------------------------------

    def record_session(self, session_dir: Path) -> CatalogSessionRow:
        """Read whatever sidecars currently exist on disk for one session and
        upsert its catalog row. This is THE incremental-update hook every
        sidecar write-site calls, and the same logic ``rebuild`` uses per
        session — one code path, so the catalog can never drift from what a
        rebuild would independently reconstruct.
        """
        session_dir = Path(session_dir)
        session_id = session_dir.name
        classification = read_classification(session_dir)
        fingerprint = read_fingerprint(session_dir)
        dtc = _read_dtc_sidecar(session_dir)
        digest = _read_digest_sidecar(session_dir)

        duration_s = classification.duration_seconds if classification is not None else None
        row = CatalogSessionRow(
            session_id=session_id,
            path=str(session_dir),
            started_at=_parse_started_at(session_id),
            duration_s=duration_s,
            row_count=_parquet_row_count(session_dir),
            size_bytes=_dir_size(session_dir) if session_dir.is_dir() else None,
            pinned=(session_dir / ".canary-pin").exists(),
            disposition=classification.disposition if classification is not None else None,
            classifier_version=(
                classification.classifier_version if classification is not None else None
            ),
            mode_seconds=dict(classification.mode_seconds) if classification is not None else {},
            event_counts=dict(classification.event_counts) if classification is not None else {},
            quality=dict(classification.quality) if classification is not None else {},
            fingerprint=dict(fingerprint.stats) if fingerprint is not None else None,
            dtc=dtc,
            digest=digest,
            updated_at=_now(),
        )
        self._upsert(row)
        return row

    def _upsert(self, row: CatalogSessionRow) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO sessions
                   (session_id, path, started_at, duration_s, row_count, size_bytes,
                    pinned, disposition, classifier_version, mode_seconds, event_counts,
                    quality, fingerprint, dtc, digest, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     path=excluded.path, started_at=excluded.started_at,
                     duration_s=excluded.duration_s, row_count=excluded.row_count,
                     size_bytes=excluded.size_bytes, pinned=excluded.pinned,
                     disposition=excluded.disposition,
                     classifier_version=excluded.classifier_version,
                     mode_seconds=excluded.mode_seconds, event_counts=excluded.event_counts,
                     quality=excluded.quality, fingerprint=excluded.fingerprint,
                     dtc=excluded.dtc, digest=excluded.digest, updated_at=excluded.updated_at""",
                (
                    row.session_id,
                    row.path,
                    row.started_at,
                    row.duration_s,
                    row.row_count,
                    row.size_bytes,
                    int(row.pinned),
                    row.disposition,
                    row.classifier_version,
                    json.dumps(row.mode_seconds),
                    json.dumps(row.event_counts),
                    json.dumps(row.quality),
                    json.dumps(row.fingerprint) if row.fingerprint is not None else None,
                    json.dumps(row.dtc) if row.dtc is not None else None,
                    json.dumps(row.digest) if row.digest is not None else None,
                    row.updated_at,
                ),
            )

    def delete_session(self, session_id: str) -> None:
        """Drop one row (the retention-sweep hook). A no-op if it never had one."""
        with self._tx() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    # -- reads ------------------------------------------------------------

    def get_session(self, session_id: str) -> CatalogSessionRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return None if row is None else CatalogSessionRow._from_row(row)

    def list_sessions(self) -> list[CatalogSessionRow]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM sessions ORDER BY session_id").fetchall()
        return [CatalogSessionRow._from_row(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM sessions")
            return int(cur.fetchone()[0])

    def sessions_in_range(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[CatalogSessionRow]:
        """Sessions whose ``started_at`` falls within ``[since, until]`` (inclusive
        on both ends, either bound optional) — the date-pushdown query
        ``parquet_query`` uses instead of parsing every session directory name
        itself. Rows with no ``started_at`` (unparseable name) are excluded,
        matching the caller's own "can't range-check it" convention."""
        clauses = ["started_at IS NOT NULL"]
        params: list[str] = []
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since.astimezone(UTC).isoformat())
        if until is not None:
            clauses.append("started_at <= ?")
            params.append(until.astimezone(UTC).isoformat())
        query = f"SELECT * FROM sessions WHERE {' AND '.join(clauses)} ORDER BY session_id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [CatalogSessionRow._from_row(row) for row in rows]

    def fingerprints_by_session(self) -> list[tuple[str, dict[str, float]]]:
        """``(session_id, stats)`` for every catalog row carrying a fingerprint,
        oldest-first by session id (ISO timestamp names sort chronologically) —
        what ``src.control.trends`` needs instead of globbing sidecars itself."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, fingerprint FROM sessions "
                "WHERE fingerprint IS NOT NULL ORDER BY session_id"
            ).fetchall()
        return [(row["session_id"], json.loads(row["fingerprint"])) for row in rows]


def rebuild_catalog(catalog: SessionCatalog, session_dirs: Sequence[Path]) -> int:
    """Re-index every given session directory from its sidecars. Returns the
    count indexed. Callers (``scripts/catalog.py rebuild``) are responsible
    for deciding which directories on disk count as "sessions" — this just
    drives :meth:`SessionCatalog.record_session` over the list, so corruption
    recovery is always "point this at the same discovery walk again"."""
    count = 0
    for session_dir in session_dirs:
        try:
            catalog.record_session(session_dir)
            count += 1
        except Exception:  # noqa: BLE001 - one bad session must not abort the rebuild
            log.exception("catalog rebuild failed for %s", session_dir)
    return count


__all__.append("rebuild_catalog")
