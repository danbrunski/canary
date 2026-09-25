"""A shared SQLite-connection base for the project's small index databases.

R01.5 (refactor review 2026-09-24). :class:`src.storage.catalog.SessionCatalog`
was modelled on :class:`src.companion.library.CanaryLibrary` and copied its
``_tx`` helper verbatim, then added WAL and a busy timeout that the library
never got. One base, so both have them.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

#: How long a writer waits on another process's write lock before raising
#: ``SQLITE_BUSY``.
BUSY_TIMEOUT_MS = 5000


class SqliteStore:
    """One shared connection, a private re-entrant lock, WAL and a busy timeout.

    WAL + a busy timeout: several independent processes can legitimately
    touch these files at once (the daemon's async tasks, a control-route
    request, a script run by hand). WAL lets readers proceed against an
    in-progress writer instead of hitting ``SQLITE_BUSY``, and the timeout
    makes a genuine writer-vs-writer collision retry instead of erroring
    immediately.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back and re-raise on any exception."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise


__all__ = ["BUSY_TIMEOUT_MS", "SqliteStore"]
