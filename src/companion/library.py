"""Local CANary library — SQLite index over on-disk session/model files (6.32).

The offline companion (browse / chart / train / publish while the truck is
off) needs a durable local home for everything synced from the Pi. The
design (per ``docs/companion-app-plan.md``) is deliberately boring: *normal
files on disk* (a session is still a session directory, a model is still an
artifact + manifest) plus a **SQLite index** so the companion API can list,
filter, and join without walking the tree every request.

``~/.canary/library`` by default::

    library.db          # the index (this module owns the schema)
    sessions/<id>/      # imported session directories (verbatim)
    models/<name>/      # imported model artifacts + manifests
    datasets/<name>/    # exported datasets + their manifests

The DB is an *index/cache*, never the source of truth — every row points
at files that remain independently readable. Schema migrations are additive
(`CREATE TABLE IF NOT EXISTS`); a `schema_version` row guards a future
breaking change. Single-process use (the companion API serializes its own
writes); SQLite's file lock covers the occasional concurrent reader.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LIBRARY_ROOT",
    "SCHEMA_VERSION",
    "CanaryLibrary",
    "LibrarySession",
]

DEFAULT_LIBRARY_ROOT = Path.home() / ".canary" / "library"
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    started_at   TEXT,
    row_count    INTEGER,
    duration_s   REAL,
    size_bytes   INTEGER,
    pinned       INTEGER DEFAULT 0,
    training_candidate INTEGER DEFAULT 0,
    source_host  TEXT,
    imported_at  TEXT NOT NULL,
    quality_warnings TEXT  -- JSON list
);

CREATE TABLE IF NOT EXISTS session_tags (
    session_id TEXT NOT NULL,
    label      TEXT NOT NULL,
    PRIMARY KEY (session_id, label)
);

CREATE TABLE IF NOT EXISTS session_channels (
    session_id   TEXT NOT NULL,
    channel      TEXT NOT NULL,
    nan_fraction REAL,
    PRIMARY KEY (session_id, channel)
);

CREATE TABLE IF NOT EXISTS datasets (
    name        TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    purpose     TEXT,
    row_count   INTEGER,
    created_at  TEXT NOT NULL,
    manifest    TEXT  -- JSON
);

CREATE TABLE IF NOT EXISTS training_runs (
    run_id        TEXT PRIMARY KEY,
    model_type    TEXT NOT NULL,
    model_version TEXT,
    status        TEXT,
    created_at    TEXT,
    compute_target TEXT,
    publish_status TEXT,
    record        TEXT  -- full JSON TrainingRunRecord
);

CREATE TABLE IF NOT EXISTS model_artifacts (
    model_id    TEXT PRIMARY KEY,
    model_type  TEXT NOT NULL,
    version     TEXT,
    path        TEXT NOT NULL,
    manifest    TEXT,
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id    TEXT NOT NULL,
    target      TEXT NOT NULL,
    status      TEXT NOT NULL,
    at          TEXT NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS queued_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    params      TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    detail      TEXT
);
"""


@dataclass(frozen=True)
class LibrarySession:
    session_id: str
    path: str
    started_at: str | None
    row_count: int | None
    duration_s: float | None
    size_bytes: int | None
    pinned: bool
    training_candidate: bool
    source_host: str | None
    imported_at: str
    quality_warnings: list[str]
    tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "path": self.path,
            "started_at": self.started_at,
            "row_count": self.row_count,
            "duration_s": self.duration_s,
            "size_bytes": self.size_bytes,
            "pinned": self.pinned,
            "training_candidate": self.training_candidate,
            "source_host": self.source_host,
            "imported_at": self.imported_at,
            "quality_warnings": self.quality_warnings,
            "tags": self.tags,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class CanaryLibrary:
    """The local library: filesystem layout + SQLite index."""

    def __init__(self, root: Path = DEFAULT_LIBRARY_ROOT) -> None:
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.models_dir = self.root / "models"
        self.datasets_dir = self.root / "datasets"
        for d in (self.sessions_dir, self.models_dir, self.datasets_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "library.db"
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(_SCHEMA)
        cur = self._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'")
        row = cur.fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CanaryLibrary:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- sessions -------------------------------------------------------

    def import_session(
        self,
        src_dir: Path,
        *,
        config: Any | None = None,
        source_host: str | None = None,
        copy: bool = True,
    ) -> LibrarySession:
        """Bring a session directory into the library and index it.

        ``copy=True`` (default) copies the directory verbatim under
        ``sessions/`` (idempotent — re-import refreshes the index, doesn't
        duplicate files). When a ``CanaryConfig`` is given, the richer
        ``datasets.health.analyze_session`` supplies channel inventory +
        quality warnings; otherwise a lightweight ``summarize_session`` is
        used so an import never *requires* the decode/feature stack.
        """
        src_dir = Path(src_dir)
        session_id = src_dir.name
        dest = self.sessions_dir / session_id
        if copy and src_dir.resolve() != dest.resolve():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src_dir, dest)
        target = dest if copy else src_dir

        channels: list[tuple[str, float | None]] = []
        warnings: list[str] = []
        tags: list[str] = []
        row_count: int | None = None
        duration_s: float | None = None
        started_at: str | None = None
        pinned = False
        training_candidate = False

        if config is not None:
            from src.datasets.health import analyze_session

            h = analyze_session(target, config)
            row_count = h.row_count
            duration_s = h.duration_seconds
            warnings = h.warnings
            tags = h.tags
            pinned = h.pinned
            training_candidate = h.training_candidate
            channels = [(c, h.channel_nan_fraction.get(c)) for c in h.channels]
        else:
            from src.control.sessions import summarize_session

            summary = summarize_session(target)
            pinned = summary.pinned
            training_candidate = bool(summary.metadata.get("training_candidate", False))
            tags = []  # cheap path doesn't parse tag labels
            started_at = summary.started_at  # already an ISO string | None

        manifest_path = target / "manifest.json"
        if manifest_path.exists():
            try:
                meta = json.loads(manifest_path.read_text())
                started_at = meta.get("started_at", started_at)
            except (OSError, ValueError):
                pass

        size_bytes = _dir_size(target)
        imported_at = _now()
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO sessions
                   (session_id, path, started_at, row_count, duration_s, size_bytes,
                    pinned, training_candidate, source_host, imported_at, quality_warnings)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     path=excluded.path, started_at=excluded.started_at,
                     row_count=excluded.row_count, duration_s=excluded.duration_s,
                     size_bytes=excluded.size_bytes, pinned=excluded.pinned,
                     training_candidate=excluded.training_candidate,
                     source_host=excluded.source_host, imported_at=excluded.imported_at,
                     quality_warnings=excluded.quality_warnings""",
                (
                    session_id,
                    str(target),
                    started_at,
                    row_count,
                    duration_s,
                    size_bytes,
                    int(pinned),
                    int(training_candidate),
                    source_host,
                    imported_at,
                    json.dumps(warnings),
                ),
            )
            conn.execute("DELETE FROM session_tags WHERE session_id = ?", (session_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO session_tags (session_id, label) VALUES (?,?)",
                [(session_id, t) for t in tags],
            )
            conn.execute("DELETE FROM session_channels WHERE session_id = ?", (session_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO session_channels "
                "(session_id, channel, nan_fraction) VALUES (?,?,?)",
                [(session_id, c, nf) for c, nf in channels],
            )
        result = self.get_session(session_id)
        assert result is not None
        return result

    def get_session(self, session_id: str) -> LibrarySession | None:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        tags = [
            r["label"]
            for r in self._conn.execute(
                "SELECT label FROM session_tags WHERE session_id = ? ORDER BY label",
                (session_id,),
            )
        ]
        return LibrarySession(
            session_id=row["session_id"],
            path=row["path"],
            started_at=row["started_at"],
            row_count=row["row_count"],
            duration_s=row["duration_s"],
            size_bytes=row["size_bytes"],
            pinned=bool(row["pinned"]),
            training_candidate=bool(row["training_candidate"]),
            source_host=row["source_host"],
            imported_at=row["imported_at"],
            quality_warnings=json.loads(row["quality_warnings"] or "[]"),
            tags=tags,
        )

    def list_sessions(self) -> list[LibrarySession]:
        ids = [
            r["session_id"]
            for r in self._conn.execute("SELECT session_id FROM sessions ORDER BY session_id")
        ]
        return [s for sid in ids if (s := self.get_session(sid)) is not None]

    # -- training runs --------------------------------------------------

    def record_training_run(
        self, record: dict[str, Any], *, compute_target: str = "unknown"
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO training_runs
                   (run_id, model_type, model_version, status, created_at,
                    compute_target, publish_status, record)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     status=excluded.status, publish_status=excluded.publish_status,
                     record=excluded.record""",
                (
                    str(record.get("run_id")),
                    str(record.get("model_type", "")),
                    record.get("model_version"),
                    record.get("status"),
                    record.get("created_at"),
                    compute_target,
                    record.get("publish_status", "unpublished"),
                    json.dumps(record, sort_keys=True),
                ),
            )

    def list_training_runs(self) -> list[dict[str, Any]]:
        return [
            json.loads(r["record"])
            for r in self._conn.execute("SELECT record FROM training_runs ORDER BY created_at")
        ]

    # -- model artifacts + publish history ------------------------------

    def add_model_artifact(
        self,
        model_id: str,
        *,
        model_type: str,
        artifact_path: Path,
        manifest: dict[str, Any] | None = None,
        version: str | None = None,
        copy: bool = True,
    ) -> Path:
        artifact_path = Path(artifact_path)
        stored = artifact_path
        if copy:
            dest_dir = self.models_dir / model_id
            dest_dir.mkdir(parents=True, exist_ok=True)
            stored = dest_dir / artifact_path.name
            shutil.copy2(artifact_path, stored)
            if manifest is not None:
                (dest_dir / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO model_artifacts
                   (model_id, model_type, version, path, manifest, added_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    model_id,
                    model_type,
                    version,
                    str(stored),
                    json.dumps(manifest) if manifest is not None else None,
                    _now(),
                ),
            )
        return stored

    def list_model_artifacts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM model_artifacts")]

    def record_publish(
        self, model_id: str, *, target: str, status: str, detail: str | None = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO publish_history (model_id, target, status, at, detail) "
                "VALUES (?,?,?,?,?)",
                (model_id, target, status, _now(), detail),
            )

    def publish_history(self, model_id: str | None = None) -> list[dict[str, Any]]:
        if model_id is None:
            rows = self._conn.execute("SELECT * FROM publish_history ORDER BY id")
        else:
            rows = self._conn.execute(
                "SELECT * FROM publish_history WHERE model_id = ? ORDER BY id", (model_id,)
            )
        return [dict(r) for r in rows]

    # -- datasets -------------------------------------------------------

    def add_dataset(self, name: str, *, path: Path, manifest: dict[str, Any]) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO datasets
                   (name, path, purpose, row_count, created_at, manifest)
                   VALUES (?,?,?,?,?,?)""",
                (
                    name,
                    str(path),
                    manifest.get("purpose"),
                    manifest.get("row_count"),
                    manifest.get("created_at", _now()),
                    json.dumps(manifest, sort_keys=True),
                ),
            )

    def list_datasets(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM datasets ORDER BY name")]

    # -- queued Pi actions ---------------------------------------------

    def queue_action(self, kind: str, params: dict[str, Any] | None = None) -> int:
        now = _now()
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO queued_actions (kind, params, status, created_at, updated_at) "
                "VALUES (?,?, 'queued', ?, ?)",
                (kind, json.dumps(params or {}), now, now),
            )
            return int(cur.lastrowid or 0)

    def update_action(self, action_id: int, status: str, *, detail: str | None = None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE queued_actions SET status = ?, detail = ?, updated_at = ? WHERE id = ?",
                (status, detail, _now(), action_id),
            )

    def list_actions(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is None:
            rows = self._conn.execute("SELECT * FROM queued_actions ORDER BY id")
        else:
            rows = self._conn.execute(
                "SELECT * FROM queued_actions WHERE status = ? ORDER BY id", (status,)
            )
        return [dict(r) for r in rows]
