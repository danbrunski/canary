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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import duckdb

if TYPE_CHECKING:
    from src.storage.catalog import SessionCatalog

#: Session directory names are exactly this shape (src.session.make_session_dir:
#: ``started_at.strftime("%Y-%m-%dT%H%M%S")``, always UTC).
_SESSION_DIR_FORMAT = "%Y-%m-%dT%H%M%S"

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


def _parse_session_timestamp(session_dir_name: str) -> datetime | None:
    """Parse a session directory name as a naive UTC datetime, or ``None`` if
    it doesn't match the expected shape (e.g. an ``_archive``/``_replay-*``
    directory that ended up under the storage root)."""
    try:
        return datetime.strptime(session_dir_name, _SESSION_DIR_FORMAT)
    except ValueError:
        return None


def _parse_bound(value: str, *, label: str) -> datetime:
    """Parse a ``since``/``until`` bound: ISO-8601 (date or datetime, ``Z`` or
    an offset allowed) or the bare session-id shape. Returned as naive UTC so
    it compares directly against ``_parse_session_timestamp``'s output."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = _parse_session_timestamp(value) or _raise_bad_bound(value, label)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _raise_bad_bound(value: str, label: str) -> NoReturn:
    raise QueryValidationError(f"{label}={value!r} is not a valid ISO-8601 timestamp or session id")


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

    def __init__(
        self,
        storage_path: Path,
        *,
        default_limit: int = _DEFAULT_ROW_LIMIT,
        catalog: SessionCatalog | None = None,
    ) -> None:
        self._storage_path = Path(storage_path)
        self._default_limit = default_limit
        self._catalog = catalog
        # In-memory DB. Per-query refresh recreates the view from disk; we
        # don't keep state across queries beyond the connection itself.
        self._con = duckdb.connect(database=":memory:", read_only=False)

    def _matching_files(
        self,
        *,
        session: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[str]:
        """``telemetry-*.parquet`` files under the storage root, optionally
        scoped to one session or a session-timestamp range.

        **Storage-isolation convention** (``AGENTS.md``): a directory whose
        name starts with ``_`` (``_replay-*``, ``_archive``, ``_tuning-*``) is
        excluded from a default/unscoped sweep — the same convention
        ``src.preprocessing.loader.discover_sessions`` already applies to
        training discovery. This check is on any path component between the
        storage root and the file (the session dir itself, or an intervening
        ``_archive`` layer for a quarantined session moved under it), so it
        holds regardless of nesting depth. An **explicit** ``session=`` name
        always resolves, underscore-prefixed or not — callers that scope a
        query at a named ``_tuning-*``/``_replay-*`` session (e.g.
        ``engine-tuning-support.md`` Phase 2) need that escape hatch; only the
        unscoped sweep excludes them.

        The directory walk itself (``rglob``) always touches every session's
        directory entries — that's just filesystem metadata, cheap even for a
        large corpus. The scoping win is downstream: fewer paths handed to
        DuckDB's ``parquet_scan``, so it never opens/parses parquet content
        for sessions outside the requested scope. ``session`` matches the
        immediate parent directory name exactly (works regardless of
        profile-scoping depth between the storage root and the session dirs).
        ``since``/``until`` bound the session's own timestamp (parsed from its
        directory name), inclusive on both ends. A session directory that
        doesn't parse as a timestamp (e.g. stray non-session content) is kept
        when no date filter is requested, excluded when one is — an unparseable
        name can't be range-checked.

        When a :class:`~src.storage.catalog.SessionCatalog` was given at
        construction, it's used to shortcut two things: an explicit
        ``session=`` name resolves via a direct catalog lookup instead of
        walking the whole tree (falling back to the walk when the catalog
        doesn't know about it yet — never a correctness dependency); and a
        date-filtered sweep reads a catalogued session's ``started_at``
        straight from its row instead of re-parsing it from the directory
        name. Neither ever narrows the walk itself for the ranged case — an
        uncatalogued, freshly-started session must still show up.
        """
        since_dt = _parse_bound(since, label="since") if since else None
        until_dt = _parse_bound(until, label="until") if until else None
        date_filtered = since_dt is not None or until_dt is not None

        if session is not None:
            if self._catalog is not None:
                row = self._catalog.get_session(session)
                if row is not None and Path(row.path).is_dir():
                    return [str(p) for p in sorted(Path(row.path).glob("telemetry-*.parquet"))]
            return [
                str(p)
                for p in sorted(self._storage_path.rglob("telemetry-*.parquet"))
                if p.parent.name == session
            ]

        matches: list[str] = []
        for p in sorted(self._storage_path.rglob("telemetry-*.parquet")):
            session_name = p.parent.name
            relative_dirs = p.relative_to(self._storage_path).parts[:-1]
            if any(part.startswith("_") for part in relative_dirs):
                continue
            if date_filtered:
                ts = self._catalog_started_at(session_name) if self._catalog is not None else None
                if ts is None:
                    ts = _parse_session_timestamp(session_name)
                if ts is None:
                    continue
                if since_dt is not None and ts < since_dt:
                    continue
                if until_dt is not None and ts > until_dt:
                    continue
            matches.append(str(p))
        return matches

    def _catalog_started_at(self, session_name: str) -> datetime | None:
        """``started_at`` for a catalogued session, naive UTC (matching
        :func:`_parse_session_timestamp`'s output) — ``None`` when the catalog
        doesn't have this session yet, so the caller falls back to parsing
        the directory name itself."""
        assert self._catalog is not None
        row = self._catalog.get_session(session_name)
        if row is None or row.started_at is None:
            return None
        try:
            return datetime.fromisoformat(row.started_at).astimezone(UTC).replace(tzinfo=None)
        except ValueError:
            return None

    def _refresh_view(
        self,
        *,
        session: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> bool:
        """Recreate the ``telemetry`` view. Returns False if no files exist.

        Sessions appear and disappear with daemon activity — rebuilding the
        view on each call keeps results aligned with what's on disk. When the
        storage tree is empty (fresh Pi, no captures yet) we install an empty
        view rather than erroring, so ``query_telemetry`` returns ``count=0``
        instead of a stack trace.
        """
        files = self._matching_files(session=session, since=since, until=until)
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

    def run(
        self,
        sql: str,
        *,
        row_limit: int | None = None,
        session: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """Validate and run ``sql``. Returns up to ``row_limit`` rows as dicts.

        ``session``/``since``/``until`` scope which parquet files back the
        ``telemetry`` view for this call (see ``_matching_files``) — cheaper
        and less error-prone than relying on every caller to write
        ``WHERE session = '...'`` by hand, though that still works too since
        the ``session`` column is always present.
        """
        cleaned = validate_query(sql)
        limit = row_limit if row_limit is not None else self._default_limit
        self._refresh_view(session=session, since=since, until=until)
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
