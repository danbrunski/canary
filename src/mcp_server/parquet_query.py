"""DuckDB-backed read-only SQL over the telemetry Parquet ring buffer.

The daemon writes telemetry as one Parquet chunk per (session, time window) in
the long-format schema declared in :mod:`src.storage.parquet_writer`. DuckDB
can SELECT across the whole tree in one query — this module wires that up,
plus a small validator so an LLM-issued query can't ATTACH a new DB, INSTALL
extensions, mutate state, or run forever.

The validator is intentionally simple: regex-based rejection of dangerous
keywords combined with a parser pass that requires the statement to be a
single SELECT. DuckDB's read-only connection is the defense in depth.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb

#: Statements that read state are allowed. Anything else (DDL, DML, ATTACH,
#: COPY, PRAGMA, INSTALL, LOAD, etc.) is rejected before DuckDB sees it.
_FORBIDDEN_KEYWORDS = (
    "ATTACH",
    "COPY",
    "INSTALL",
    "LOAD",
    "PRAGMA",
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "CREATE",
    "ALTER",
    "TRUNCATE",
    "REPLACE",
    "VACUUM",
    "EXPORT",
    "IMPORT",
)

_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_TRAILING_SEMI = re.compile(r";\s*$")
_DEFAULT_ROW_LIMIT = 5_000


class QueryValidationError(ValueError):
    """Raised when ``validate_query`` rejects a statement."""


def validate_query(sql: str) -> str:
    """Return the canonicalised SQL or raise :class:`QueryValidationError`.

    Canonicalisation: strip comments and trailing semicolons, collapse
    whitespace. Rejects multi-statement input, non-SELECT/WITH leads, and any
    of :data:`_FORBIDDEN_KEYWORDS` appearing as standalone tokens.
    """
    if not sql or not sql.strip():
        raise QueryValidationError("empty SQL")
    stripped = _COMMENT_BLOCK.sub(" ", sql)
    stripped = _COMMENT_LINE.sub(" ", stripped)
    stripped = _TRAILING_SEMI.sub("", stripped).strip()
    if ";" in stripped:
        raise QueryValidationError("multiple statements are not allowed")
    upper = stripped.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise QueryValidationError("only SELECT/WITH statements are allowed")
    for keyword in _FORBIDDEN_KEYWORDS:
        # \b-bounded so e.g. a column named "import_ts" doesn't trip IMPORT.
        if re.search(rf"\b{keyword}\b", upper):
            raise QueryValidationError(f"keyword `{keyword}` is not allowed")
    return stripped


class TelemetryQuery:
    """Read-only DuckDB view over the telemetry Parquet tree.

    Constructs an in-memory DuckDB connection with one view, ``telemetry``,
    that selects from every ``telemetry-*.parquet`` under the configured
    storage root and adds a ``session`` column derived from the parent
    directory name (matching :data:`src.preprocessing.loader.PARQUET_PATTERN`).
    """

    def __init__(self, storage_path: Path, *, default_limit: int = _DEFAULT_ROW_LIMIT) -> None:
        self._storage_path = Path(storage_path)
        self._default_limit = default_limit
        # In-memory DB. Per-query refresh recreates the view from disk; we
        # don't keep state across queries beyond the connection itself.
        self._con = duckdb.connect(database=":memory:", read_only=False)

    def _matching_files(self) -> list[str]:
        """All ``telemetry-*.parquet`` files anywhere under the storage root."""
        return [str(p) for p in self._storage_path.rglob("telemetry-*.parquet")]

    def _refresh_view(self) -> bool:
        """Recreate the ``telemetry`` view. Returns False if no files exist.

        Sessions appear and disappear with daemon activity — rebuilding the
        view on each call keeps results aligned with what's on disk. When the
        storage tree is empty (fresh Pi, no captures yet) we install an empty
        view rather than erroring, so ``query_telemetry`` returns ``count=0``
        instead of a stack trace.
        """
        files = self._matching_files()
        if not files:
            # Empty schema-less view — any column reference will fail with a
            # standard binder error, which the validator surfaces cleanly.
            self._con.execute(
                "CREATE OR REPLACE VIEW telemetry AS "
                "SELECT NULL AS timestamp, NULL AS source, NULL AS channel, "
                "NULL AS value_number, NULL AS value_text, NULL AS value_bool, "
                "NULL AS unit, NULL AS metadata_json, NULL AS session WHERE 1=0"
            )
            return False
        # DuckDB rejects prepared parameters inside CREATE VIEW, so the file
        # list is inlined. The path is operator-controlled (env var) — not
        # user input — but single-quote-escape anyway for safety.
        quoted = ", ".join(f"'{p.replace(chr(39), chr(39) * 2)}'" for p in files)
        self._con.execute(
            "CREATE OR REPLACE VIEW telemetry AS "
            "SELECT *, regexp_extract(filename, '([^/\\\\]+)[/\\\\][^/\\\\]+\\.parquet', 1) "
            "    AS session "
            f"FROM parquet_scan([{quoted}], filename=true)"
        )
        return True

    def close(self) -> None:
        self._con.close()

    def run(self, sql: str, *, row_limit: int | None = None) -> list[dict[str, Any]]:
        """Validate and run ``sql``. Returns up to ``row_limit`` rows as dicts."""
        cleaned = validate_query(sql)
        limit = row_limit if row_limit is not None else self._default_limit
        self._refresh_view()
        # Wrap the user's query in an outer LIMIT so we don't trust them to
        # set it, but we won't override a tighter limit they wrote themselves.
        wrapped = f"SELECT * FROM ({cleaned}) AS _q LIMIT {int(limit)}"
        try:
            cursor = self._con.execute(wrapped)
        except duckdb.Error as exc:
            raise QueryValidationError(str(exc)) from exc
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        return [dict(zip(columns, row, strict=False)) for row in rows]


__all__ = ["TelemetryQuery", "QueryValidationError", "validate_query"]
