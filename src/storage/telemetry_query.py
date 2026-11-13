"""DuckDB-backed read-only SQL over the telemetry Parquet ring buffer.

Moved here from ``src.mcp_server.parquet_query`` (R01.8, which still
re-exports it): ``src.control`` reads telemetry through this class, and
``control`` must not import ``mcp_server``.

The daemon writes telemetry as one Parquet chunk per (session, time window) in
the long-format schema declared in :mod:`src.storage.parquet_writer`. DuckDB
can SELECT across the whole tree in one query — this module wires that up,
plus a small validator so an LLM-issued query can't ATTACH a new DB, INSTALL
extensions, mutate state, or run forever.

The validator is intentionally simple: regex-based rejection of dangerous
keywords combined with a parser pass that requires the statement to be a
single SELECT. DuckDB's read-only connection is the defense in depth.

**``decoded`` rows are decoded on read, in the view (P02.8, operator
2026-09-26).** Nothing stores them now, and an older session's stored ones are
the decode of its recording day. The ``telemetry`` view therefore never shows
a stored ``decoded`` row: it is the stored rows with ``source <> 'decoded'``,
``UNION ALL`` each on-read binding's rows decoded from its raw CAN frames
(:func:`_decoded_select`, the storage decoder's rule), so raw SQL sees the
current catalog's decoded rows for old sessions and new alike, as every other
reader does. A binding outside the on-read set was never stored and has no
rows here either; :func:`decoded_rows_note` names that, and points a query
that finds nothing at the ``can:`` decode.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import duckdb

from src.decode.slices import sentinel_channel
from src.session import classify_session_name, parse_session_timestamp
from src.storage.session_files import PARQUET_PATTERN

if TYPE_CHECKING:
    from src.decode.bindings import Bindings, Signal
    from src.storage.catalog import SessionCatalog

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
#: Row cap for :meth:`TelemetryQuery.fetch_channel_rows` — Tasks 5.1/5.3 read
#: a whole scoped session's worth of samples across a handful of channels
#: (not an arbitrary corpus-wide sweep like ``run()``'s ad-hoc SQL), so this
#: is generous rather than the 5k default: a multi-hour drive at 10 Hz across
#: 3 channels is still well under it.
_ANALYSIS_ROW_LIMIT = 200_000


#: A chunk's name is what :data:`~src.storage.session_files.PARQUET_PATTERN`
#: matches; the walk below tests names itself (a ``startswith`` and an
#: ``endswith`` where ``fnmatch`` would cost more than the rest of the loop), so
#: it splits the one pattern at its one ``*`` rather than spelling it again.
_CHUNK_PREFIX, _star, _CHUNK_SUFFIX = PARQUET_PATTERN.partition("*")

#: What to do with one directory of the storage tree: ``(collect, descend)``,
#: its own chunks and/or the directories inside it. ``(False, False)`` skips it
#: without listing it. Called with the directory's name and whether it is the
#: storage root.
_Visit = Callable[[str, bool], tuple[bool, bool]]


def _chunk_files(root: Path, visit: _Visit) -> list[str]:
    """The ``telemetry-*.parquet`` paths :data:`_Visit` selects under ``root``,
    in :class:`~pathlib.Path` order (what ``sorted`` of the ``Path`` objects
    gives).

    One listing per directory it enters, none for a directory ``visit`` skips.
    ``sorted(root.rglob(...))`` listed every directory under the root (every
    ``_archive`` session, every capture) and built and compared a ``Path`` per
    chunk, to throw most away (R23.2). A root that is absent or unreadable is
    an empty tree, as it was for ``rglob``.
    """
    groups: list[tuple[Path, list[str]]] = []
    pending: list[tuple[Path, bool]] = [(root, True)]
    while pending:
        directory, is_root = pending.pop()
        collect, descend = visit(directory.name, is_root)
        if not (collect or descend):
            continue
        names: list[str] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    name = entry.name
                    if name.startswith(_CHUNK_PREFIX) and name.endswith(_CHUNK_SUFFIX):
                        names.append(name)
                    elif descend and entry.is_dir(follow_symlinks=False):
                        pending.append((directory / name, False))
        except OSError:
            continue
        if collect and names:
            groups.append((directory, names))
    return _in_path_order(groups)


def _in_path_order(groups: list[tuple[Path, list[str]]]) -> list[str]:
    """``directory / name`` for every name, ordered as ``Path`` objects order.

    ``Path`` ordering compares part by part, so chunks of one directory sort
    by name and directories sort by their parts -- unless one directory lies
    inside another that holds chunks too (``a/telemetry-0.parquet`` against
    ``a/b/telemetry-0.parquet``: ``b`` sorts before ``telemetry-0.parquet``),
    when the order depends on the names and the ``Path`` sort is the answer.
    Sorting 37k ``Path`` objects was most of the walk's time.
    """
    keyed = sorted(
        ((directory.parts, directory, names) for directory, names in groups),
        key=lambda group: group[0],
    )
    nested = any(later[0][: len(earlier[0])] == earlier[0] for earlier, later in pairwise(keyed))
    if nested:
        return [
            str(path)
            for path in sorted(directory / name for _, directory, names in keyed for name in names)
        ]
    paths: list[str] = []
    for _, directory, names in keyed:
        prefix = str(directory / "x")[:-1]  # the directory and its separator (none for ".")
        paths.extend(prefix + name for name in sorted(names))
    return paths


def _named(session: str) -> _Visit:
    """:data:`_Visit` for one session by directory name, at any depth under the
    root and under any ``_`` directory (``_archive``): its chunks are collected,
    and the sessions beside it are never listed. A directory named like a
    session is a session, not a layer, so nothing below it is searched."""

    def visit(name: str, is_root: bool) -> tuple[bool, bool]:
        return name == session, is_root or classify_session_name(name) is None

    return visit


class QueryValidationError(ValueError):
    """Raised when ``validate_query`` rejects a statement."""


#: A query that names the ``decoded`` source or a ``decoded:`` channel.
_DECODED_MENTION = re.compile(r"\bdecoded\b", re.IGNORECASE)

DECODED_ROWS_NOTE = (
    "decoded rows here are decoded on read from the raw CAN frames with the current "
    "bindings (P02.8: none is stored since 2026-09-26, and an older session's stored "
    "ones are never shown); only confirmed and persist: true bindings have them. Any "
    "other binding: read_channels with a can: spec, or hex(payload) on source='can'."
)


def decoded_rows_note(sql: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """The fields a raw query's result carries when it asks for ``decoded``
    rows: ``note`` always, and ``empty_reason`` when it found nothing -- no
    rows, or one row of zero/null aggregates (``count(*)`` is 0) -- so an
    empty answer says which kind of empty it is (AGENTS.md)."""
    if not _DECODED_MENTION.search(sql):
        return {}
    fields = {"note": DECODED_ROWS_NOTE}
    if _found_nothing(rows):
        fields["empty_reason"] = (
            "no decoded rows match: either the scope has no raw CAN frame of that "
            "binding's id, or the binding is not decoded on read (not confirmed or "
            "persist: true), which no session ever stored"
        )
    return fields


#: A query that names the ``can`` source (``source = 'can'``) or ``decoded``
#: rows, both of which follow the broadcast frames' storage (P02.10).
_BROADCAST_MENTION = re.compile(r"'can'|\bdecoded\b", re.IGNORECASE)

CAN_STORAGE_NOTE = (
    "sessions recorded since P02.10 (2026-09-26) store a CAN frame only when its payload "
    "changes, and re-write an unchanged id once per collection.broadcast_stale_timeout_seconds "
    "(one second before it); a chunk's parquet footer key canary.can_storage says which. "
    "In those sessions counts of can or decoded rows are payload changes, not frames, "
    "and a broadcast value at an instant is the last row before it (valid for the "
    "timeout). Frame counts and rates need a capture profile with store_every_frame."
)


def can_storage_note(sql: str) -> dict[str, str]:
    """``can_storage_note`` for a raw query that reads broadcast rows (P02.10):
    what a row count means depends on how the frames were stored."""
    if not _BROADCAST_MENTION.search(sql):
        return {}
    return {"can_storage_note": CAN_STORAGE_NOTE}


def _found_nothing(rows: Sequence[Mapping[str, Any]]) -> bool:
    """No rows, or one row whose every value is zero or null (an aggregate over
    nothing: ``count(*)`` is 0, ``avg``/``max`` are null)."""
    if not rows:
        return True
    if len(rows) > 1:
        return False
    return all(v is None or (isinstance(v, (int, float)) and v == 0) for v in rows[0].values())


def _parse_session_timestamp(session_dir_name: str) -> datetime | None:
    """Parse a session directory name as a naive UTC datetime, or ``None`` if
    it doesn't match the expected shape (e.g. an ``_archive``/``_replay-*``
    directory that ended up under the storage root)."""
    parsed = parse_session_timestamp(session_dir_name)
    return None if parsed is None else parsed.replace(tzinfo=None)


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
    directory name (matching :data:`src.storage.session_files.PARQUET_PATTERN`).
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

        The walk (:func:`_chunk_files`) lists each directory once and only the
        ones the query needs (R23.2): an unscoped sweep never enters a ``_``
        directory (its ~54k ``_archive`` chunks on the dev corpus), a
        date-filtered one never lists a session outside the range, and a named
        one lists only the layers above the sessions and the session itself.
        ``rglob`` listed every directory and built a ``Path`` per chunk to
        discard most of them. The other half of the win is unchanged: fewer
        paths handed to DuckDB's
        ``parquet_scan``, so it never opens/parses parquet content for sessions
        outside the requested scope. ``session`` matches the immediate parent
        directory name exactly (works regardless of profile-scoping depth
        between the storage root and the session dirs). A directory named like
        a session (:func:`src.session.classify_session_name`) holds chunks and
        is not searched for further sessions below it.
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
                    return [str(p) for p in sorted(Path(row.path).glob(PARQUET_PATTERN))]
            return _chunk_files(self._storage_path, _named(session))

        def dated(name: str, is_root: bool) -> tuple[bool, bool]:
            if not is_root and name.startswith("_"):
                return False, False
            if not date_filtered:
                return True, True
            ts = self._catalog_started_at(name) if self._catalog is not None else None
            if ts is None:
                ts = _parse_session_timestamp(name)
            if ts is None:
                # Not a dated session: a layer above the sessions (the profile
                # directory) is looked through, its own chunks are no session's.
                return False, True
            inside = (since_dt is None or ts >= since_dt) and (until_dt is None or ts <= until_dt)
            return inside, inside or is_root

        return _chunk_files(self._storage_path, dated)

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
                "NULL AS unit, NULL::BLOB AS payload, NULL AS metadata_json, "
                "NULL AS session WHERE 1=0"
            )
            return False
        from src.decode.on_read import on_read_catalog

        columns = {
            row[0] for row in self._con.execute(f"DESCRIBE SELECT * FROM {_scan(files)}").fetchall()
        }
        self._con.execute(view_sql(files, on_read_catalog(), decode="payload" in columns))
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

    def fetch_channel_rows(
        self,
        channel_keys: Sequence[str],
        *,
        session: str | None = None,
        since: str | None = None,
        until: str | None = None,
        row_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch ``(timestamp, session, channel_key, value_number)`` rows for
        the given ``source:channel`` keys — the shared long-format read Task
        5.1's binned analysis (:mod:`src.analysis.binned_analysis`) and Task
        5.3's session time-series read (:mod:`src.analysis.session_timeseries`)
        both pivot from, so neither has to hand-roll its own scoped SQL.

        Unlike :meth:`run`, this bypasses :func:`validate_query` — the SQL
        shape is fixed and internally built (same trust level as
        :meth:`_refresh_view`'s own view-construction SQL), and
        ``channel_keys`` is passed as bound DuckDB parameters rather than
        interpolated, since (unlike the file list) it *is* caller/query-string
        controlled. Rows with a null ``value_number`` (text/bool channels, or
        a channel that's simply absent from the scope) are excluded — both
        consumers bin/plot numeric series only.

        ``decoded:`` keys read the view's decoded rows, which are decoded on
        read from the raw frames (P02.8, module docstring): a stored
        ``decoded`` row is never read, so old sessions and new answer alike.
        """
        if not channel_keys:
            return []
        self._refresh_view(session=session, since=since, until=until)
        limit = row_limit if row_limit is not None else _ANALYSIS_ROW_LIMIT
        placeholders = ", ".join("?" for _ in channel_keys)
        # ``channel_key`` breaks timestamp ties, so which rows a row limit cuts
        # through one instant (one frame's several signals) is deterministic.
        sql = (
            "SELECT timestamp, session, (source || ':' || channel) AS channel_key, "
            "value_number "
            "FROM telemetry "
            f"WHERE (source || ':' || channel) IN ({placeholders}) "
            "AND value_number IS NOT NULL "
            f"ORDER BY timestamp, channel_key LIMIT {int(limit)}"
        )
        cursor = self._con.execute(sql, list(channel_keys))
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchall()
        return [dict(zip(columns, row, strict=False)) for row in rows]


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


#: The ``session`` column: the chunk file's parent directory name.
_SESSION_COLUMN = "regexp_extract(filename, '([^/\\\\]+)[/\\\\][^/\\\\]+\\.parquet', 1) AS session"


def _scan(files: Sequence[str]) -> str:
    return "parquet_scan([" + ", ".join(_sql_text(p) for p in files) + "], filename=true)"


def view_sql(files: Sequence[str], catalog: Bindings | None, *, decode: bool = True) -> str:
    """The ``telemetry`` view over ``files`` (module docstring): the stored
    rows that are not ``decoded``, and each on-read signal of ``catalog``
    decoded from its frames. DuckDB rejects prepared parameters inside
    ``CREATE VIEW``, so everything is inlined; the file paths are
    operator-controlled and the channel names come from the bindings file,
    but both are quote-escaped anyway."""
    scan = _scan(files)
    selects = [f"SELECT *, {_SESSION_COLUMN} FROM {scan} WHERE source <> 'decoded'"]
    # Files written before raw payloads were stored (2026-07-29) have no
    # ``payload`` column, and a scan made only of them cannot decode anything.
    if decode:
        for signal in catalog.signals if catalog is not None else []:
            selects.extend(_decoded_select(signal, scan))
    return "CREATE OR REPLACE VIEW telemetry AS " + " UNION ALL BY NAME ".join(selects)


def _decoded_select(signal: Signal, scan: str) -> list[str]:
    """``signal``'s ``decoded`` rows, decoded from its raw CAN frames in SQL.

    The storage decoder's rule (``src.preprocessing.decode.decode_on_read``):
    the slice of the payload as a float (two's-complement when the binding is
    ``signed``), no row for a payload too
    short for the slice, and the ``<NAME>_SENTINEL`` state row (value text =
    the stated meaning, no number) for the declared sentinel word; ``unit``
    and ``metadata_json`` as the stored rows had them. The frames are matched
    under every era's channel spelling (R04.14). Done in DuckDB, so the rows
    keep the stored rows' types and a query's filter can prune the branch.
    """
    from src.decode.on_read import can_channel_spellings

    start, length = signal.byte_start, signal.byte_length
    pairs = [f"substring(hex(f.payload), {2 * (start + i) + 1}, 2)" for i in range(length)]
    if signal.byte_order == "little":
        pairs.reverse()
    raw = f"CAST('0x' || {' || '.join(pairs)} AS UBIGINT)"
    if signal.signed:
        # Subtracted in HUGEINT (an 8-byte slice's 2^64 does not fit BIGINT),
        # then held as BIGINT, which every signed slice fits: HUGEINT ->
        # DOUBLE rounds differently from the int64 -> float64 the session
        # decoder does, so a large 8-byte value would differ in its last bit.
        bits = 8 * length
        raw = (
            f"CAST(CAST({raw} AS HUGEINT) - CASE WHEN {raw} >= {1 << (bits - 1)} "
            f"THEN '{1 << bits}'::HUGEINT ELSE 0::HUGEINT END AS BIGINT)"
        )
    spellings = ", ".join(_sql_text(c) for c in can_channel_spellings([signal.can_id]))
    meta = json.dumps(
        {"can_id": signal.can_id, "binding_confidence": signal.confidence}, sort_keys=True
    )
    frames = (
        # ``f.``-qualified: the select list names its own ``payload``/``source``/
        # ``channel`` columns, and DuckDB would bind a bare name to those.
        f"FROM {scan} AS f WHERE f.source = 'can' AND f.channel IN ({spellings}) "
        f"AND octet_length(f.payload) >= {start + length}"
    )

    def select(channel: str, number: str, text: str, unit: str) -> str:
        return (
            "SELECT f.timestamp, 'decoded' AS source, "
            f"{_sql_text(channel)} AS channel, {number} AS value_number, "
            f"{text} AS value_text, NULL::BOOLEAN AS value_bool, "
            f"{_sql_text(unit)} AS unit, NULL::BLOB AS payload, "
            f"{_sql_text(meta)} AS metadata_json, f.filename, {_SESSION_COLUMN} "
        )

    number = select(signal.name, f"CAST({raw} AS DOUBLE)", "NULL::VARCHAR", signal.display_unit)
    if signal.sentinel_raw is None:
        return [number + frames]
    sentinel = int(signal.sentinel_raw)
    meaning = _sql_text(signal.sentinel_meaning or "sentinel")
    state = select(sentinel_channel(signal.name), "NULL::DOUBLE", meaning, "")
    return [
        number + frames + f" AND {raw} <> {sentinel}",
        state + frames + f" AND {raw} = {sentinel}",
    ]


__all__ = [
    "DECODED_ROWS_NOTE",
    "QueryValidationError",
    "TelemetryQuery",
    "can_storage_note",
    "decoded_rows_note",
    "validate_query",
    "view_sql",
]
