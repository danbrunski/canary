"""Tests for the DuckDB-backed telemetry query module."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.mcp_server.parquet_query import (
    QueryValidationError,
    TelemetryQuery,
    validate_query,
)

_SCHEMA = pa.schema(
    [
        ("timestamp", pa.timestamp("us", tz="UTC")),
        ("source", pa.string()),
        ("channel", pa.string()),
        ("value_number", pa.float64()),
        ("value_text", pa.string()),
        ("value_bool", pa.bool_()),
        ("unit", pa.string()),
        ("metadata_json", pa.string()),
    ]
)


def _write_fixture(root: Path, session_id: str, rows: list[dict[str, object]]) -> Path:
    session_dir = root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=_SCHEMA)
    out = session_dir / "telemetry-001.parquet"
    pq.write_table(table, out)
    return out


# ---------------------------------------------------------------------------
# validate_query
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from telemetry where channel = 'RPM'",
        "  WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT 1;",
        "/* leading comment */ SELECT 1",
        "-- comment\nSELECT 1",
    ],
)
def test_validate_query_accepts_selects(sql: str) -> None:
    """SELECT and WITH heads pass; trailing semicolons and comments are tolerated."""
    cleaned = validate_query(sql)
    assert cleaned.upper().startswith(("SELECT", "WITH"))


@pytest.mark.parametrize(
    "sql,reason",
    [
        ("", "empty"),
        ("INSERT INTO telemetry VALUES (1)", "INSERT"),
        ("UPDATE telemetry SET channel = 'x'", "UPDATE"),
        ("DELETE FROM telemetry", "DELETE"),
        ("DROP VIEW telemetry", "DROP"),
        ("CREATE TABLE foo AS SELECT 1", "CREATE"),
        ("ATTACH 'foo.db'", "ATTACH"),
        ("PRAGMA threads=8", "PRAGMA"),
        ("INSTALL httpfs", "INSTALL"),
        ("LOAD httpfs", "LOAD"),
        ("SELECT 1; SELECT 2", "multi"),
    ],
)
def test_validate_query_rejects_dangerous(sql: str, reason: str) -> None:
    """Anything that mutates state or stacks statements is rejected."""
    with pytest.raises(QueryValidationError):
        validate_query(sql)


def test_validate_query_allows_column_named_with_forbidden_substring() -> None:
    """A column named ``import_ts`` shouldn't trip the IMPORT keyword check."""
    # 'import_ts' is fine because the regex is \bIMPORT\b — the trailing _ts
    # makes import_ts a single token.
    validate_query("SELECT import_ts FROM telemetry")


# ---------------------------------------------------------------------------
# TelemetryQuery
# ---------------------------------------------------------------------------


def test_telemetry_query_empty_storage_returns_zero(tmp_path: Path) -> None:
    """Empty storage tree is a normal startup state — no error, no rows."""
    q = TelemetryQuery(tmp_path)
    result = q.run("SELECT * FROM telemetry")
    assert result == []


def test_telemetry_query_reads_real_parquet(tmp_path: Path) -> None:
    """End-to-end: write a Parquet fixture, query it, verify column projection."""
    t0 = dt.datetime(2026, 5, 18, 12, 0, tzinfo=dt.UTC)
    _write_fixture(
        tmp_path,
        "2026-05-18T120000",
        [
            {
                "timestamp": t0,
                "source": "obd",
                "channel": "RPM",
                "value_number": 800.0,
                "value_text": None,
                "value_bool": None,
                "unit": "rpm",
                "metadata_json": None,
            },
            {
                "timestamp": t0,
                "source": "obd",
                "channel": "SPEED",
                "value_number": 0.0,
                "value_text": None,
                "value_bool": None,
                "unit": "kph",
                "metadata_json": None,
            },
        ],
    )
    q = TelemetryQuery(tmp_path)
    rows = q.run("SELECT channel, value_number, session FROM telemetry ORDER BY channel")
    assert [r["channel"] for r in rows] == ["RPM", "SPEED"]
    assert rows[0]["value_number"] == 800.0
    assert rows[0]["session"] == "2026-05-18T120000"


def test_telemetry_query_picks_up_new_session(tmp_path: Path) -> None:
    """View is refreshed on each query — files added after construction appear."""
    q = TelemetryQuery(tmp_path)
    assert q.run("SELECT * FROM telemetry") == []
    _write_fixture(
        tmp_path,
        "2026-05-18T120000",
        [
            {
                "timestamp": dt.datetime(2026, 5, 18, 12, 0, tzinfo=dt.UTC),
                "source": "obd",
                "channel": "RPM",
                "value_number": 1200.0,
                "value_text": None,
                "value_bool": None,
                "unit": "rpm",
                "metadata_json": None,
            }
        ],
    )
    rows = q.run("SELECT channel FROM telemetry")
    assert rows == [{"channel": "RPM"}]


def test_telemetry_query_enforces_row_limit(tmp_path: Path) -> None:
    """Outer LIMIT is applied even when the SQL omits one."""
    t0 = dt.datetime(2026, 5, 18, 12, 0, tzinfo=dt.UTC)
    rows = []
    for i in range(20):
        rows.append(
            {
                "timestamp": t0 + dt.timedelta(seconds=i),
                "source": "obd",
                "channel": "RPM",
                "value_number": 800.0 + i,
                "value_text": None,
                "value_bool": None,
                "unit": "rpm",
                "metadata_json": None,
            }
        )
    _write_fixture(tmp_path, "S", rows)
    q = TelemetryQuery(tmp_path, default_limit=5)
    result = q.run("SELECT value_number FROM telemetry ORDER BY value_number")
    assert len(result) == 5
    # Caller-supplied limit overrides the default.
    result_caller = q.run("SELECT value_number FROM telemetry", row_limit=3)
    assert len(result_caller) == 3


def test_telemetry_query_rejects_non_select(tmp_path: Path) -> None:
    """The validator runs before DuckDB sees the statement."""
    q = TelemetryQuery(tmp_path)
    with pytest.raises(QueryValidationError):
        q.run("DROP VIEW telemetry")


# ---------------------------------------------------------------------------
# session / since / until scoping (task 3.5)
# ---------------------------------------------------------------------------


def _row(t: dt.datetime, rpm: float) -> dict[str, object]:
    return {
        "timestamp": t,
        "source": "obd",
        "channel": "RPM",
        "value_number": rpm,
        "value_text": None,
        "value_bool": None,
        "unit": "rpm",
        "metadata_json": None,
    }


def _three_sessions(tmp_path: Path) -> Path:
    """Three sessions, one per day, each with a distinct RPM so a query can
    tell which session(s) a scoped run actually touched."""
    _write_fixture(
        tmp_path, "2026-07-01T090000", [_row(dt.datetime(2026, 7, 1, 9, tzinfo=dt.UTC), 100.0)]
    )
    _write_fixture(
        tmp_path, "2026-07-10T090000", [_row(dt.datetime(2026, 7, 10, 9, tzinfo=dt.UTC), 200.0)]
    )
    _write_fixture(
        tmp_path, "2026-07-20T090000", [_row(dt.datetime(2026, 7, 20, 9, tzinfo=dt.UTC), 300.0)]
    )
    return tmp_path


def test_matching_files_session_scopes_to_one_session(tmp_path: Path) -> None:
    _three_sessions(tmp_path)
    q = TelemetryQuery(tmp_path)
    files = q._matching_files(session="2026-07-10T090000")
    assert len(files) == 1
    assert "2026-07-10T090000" in files[0]


def test_matching_files_session_scope_works_regardless_of_nesting_depth(tmp_path: Path) -> None:
    """A session id matches the immediate parent dir name even when the
    storage root sits above a profile-slug layer (multi-vehicle deployments)."""
    scoped_root = tmp_path / "nissan-xterra-2008"
    _write_fixture(
        scoped_root, "2026-07-10T090000", [_row(dt.datetime(2026, 7, 10, 9, tzinfo=dt.UTC), 200.0)]
    )
    q = TelemetryQuery(tmp_path)  # points ABOVE the profile-slug directory
    files = q._matching_files(session="2026-07-10T090000")
    assert len(files) == 1


# ---------------------------------------------------------------------------
# Task 6.4(a) — the standing bug: bare rglob swept _replay-*/_archive in
# ---------------------------------------------------------------------------


def test_old_bare_rglob_would_have_swept_in_underscore_prefixed_dirs(tmp_path: Path) -> None:
    """Proves the bug was real, against the exact pre-fix implementation
    (a bare ``rglob`` with no underscore filtering) — not just a check that
    the new code happens to exclude them."""
    _write_fixture(
        tmp_path, "2026-07-10T090000", [_row(dt.datetime(2026, 7, 10, 9, tzinfo=dt.UTC), 200.0)]
    )
    _write_fixture(
        tmp_path,
        "_replay-2026-07-11T090000",
        [_row(dt.datetime(2026, 7, 11, 9, tzinfo=dt.UTC), 1.0)],
    )

    old_behavior = [str(p) for p in tmp_path.rglob("telemetry-*.parquet")]
    assert any("_replay-" in p for p in old_behavior), (
        "fixture setup is wrong: the old bare-rglob behavior should include the replay dir"
    )

    q = TelemetryQuery(tmp_path)
    fixed = q._matching_files()
    assert not any("_replay-" in p for p in fixed)
    assert any("2026-07-10T090000" in p for p in fixed)


def test_matching_files_excludes_replay_and_archive_by_default(tmp_path: Path) -> None:
    _write_fixture(
        tmp_path, "2026-07-10T090000", [_row(dt.datetime(2026, 7, 10, 9, tzinfo=dt.UTC), 200.0)]
    )
    _write_fixture(
        tmp_path,
        "_replay-2026-07-11T090000",
        [_row(dt.datetime(2026, 7, 11, 9, tzinfo=dt.UTC), 1.0)],
    )
    # `_archive/` nests real quarantined sessions one level deeper.
    _write_fixture(
        tmp_path / "_archive",
        "2026-07-12T090000",
        [_row(dt.datetime(2026, 7, 12, 9, tzinfo=dt.UTC), 2.0)],
    )
    _write_fixture(
        tmp_path,
        "_tuning-2026-07-13T090000",
        [_row(dt.datetime(2026, 7, 13, 9, tzinfo=dt.UTC), 3.0)],
    )

    q = TelemetryQuery(tmp_path)
    files = q._matching_files()

    assert len(files) == 1
    assert "2026-07-10T090000" in files[0]


def test_matching_files_explicit_underscore_session_name_still_resolves(tmp_path: Path) -> None:
    """The escape hatch: a query that names an underscore-prefixed session
    explicitly (e.g. a `_tuning-*` capture, engine-tuning-support.md Phase 2)
    must still resolve it — only the *default* sweep excludes them."""
    _write_fixture(
        tmp_path,
        "_tuning-2026-07-13T090000",
        [_row(dt.datetime(2026, 7, 13, 9, tzinfo=dt.UTC), 3.0)],
    )

    q = TelemetryQuery(tmp_path)
    files = q._matching_files(session="_tuning-2026-07-13T090000")
    assert len(files) == 1
    assert "_tuning-2026-07-13T090000" in files[0]


def test_matching_files_uses_catalog_for_explicit_session_lookup(tmp_path: Path) -> None:
    """When a catalog is wired, an explicit ``session=`` resolves via a direct
    row lookup instead of walking the tree — and still falls back to the walk
    for a session the catalog doesn't know about yet."""
    from src.storage.catalog import SessionCatalog

    catalogued = _write_fixture(
        tmp_path, "2026-07-10T090000", [_row(dt.datetime(2026, 7, 10, 9, tzinfo=dt.UTC), 200.0)]
    ).parent
    uncatalogued = _write_fixture(
        tmp_path, "2026-07-11T090000", [_row(dt.datetime(2026, 7, 11, 9, tzinfo=dt.UTC), 1.0)]
    ).parent

    with SessionCatalog(tmp_path / "catalog.db") as catalog:
        catalog.record_session(catalogued)

        q = TelemetryQuery(tmp_path, catalog=catalog)
        assert len(q._matching_files(session=catalogued.name)) == 1
        # Falls back to the tree walk for a session never indexed.
        assert len(q._matching_files(session=uncatalogued.name)) == 1
        assert q._matching_files(session="does-not-exist") == []


def test_query_telemetry_session_scope_excludes_other_sessions(tmp_path: Path) -> None:
    _three_sessions(tmp_path)
    q = TelemetryQuery(tmp_path)
    rows = q.run("SELECT value_number, session FROM telemetry", session="2026-07-10T090000")
    assert [r["value_number"] for r in rows] == [200.0]
    assert rows[0]["session"] == "2026-07-10T090000"


def test_since_until_bound_inclusive_range(tmp_path: Path) -> None:
    _three_sessions(tmp_path)
    q = TelemetryQuery(tmp_path)
    rows = q.run(
        "SELECT value_number FROM telemetry ORDER BY value_number",
        since="2026-07-05",
        until="2026-07-15",
    )
    assert [r["value_number"] for r in rows] == [200.0]  # only the middle session


def test_since_only_is_an_open_upper_bound(tmp_path: Path) -> None:
    _three_sessions(tmp_path)
    q = TelemetryQuery(tmp_path)
    rows = q.run("SELECT value_number FROM telemetry ORDER BY value_number", since="2026-07-10")
    assert [r["value_number"] for r in rows] == [200.0, 300.0]


def test_since_until_accepts_bare_session_id_as_bound(tmp_path: Path) -> None:
    """A caller can pass a session id itself as the bound, not just ISO-8601."""
    _three_sessions(tmp_path)
    q = TelemetryQuery(tmp_path)
    rows = q.run(
        "SELECT value_number FROM telemetry",
        since="2026-07-10T090000",
        until="2026-07-10T090000",
    )
    assert [r["value_number"] for r in rows] == [200.0]


def test_bad_since_raises_query_validation_error(tmp_path: Path) -> None:
    q = TelemetryQuery(tmp_path)
    with pytest.raises(QueryValidationError):
        q.run("SELECT * FROM telemetry", since="not-a-date")


def test_date_filter_excludes_unparseable_session_dirs(tmp_path: Path) -> None:
    """A non-session directory under the storage root (e.g. leftover content)
    can't be range-checked, so a date-filtered query skips it rather than
    guessing; an unscoped query still includes it (existing full-scan behavior)."""
    weird = tmp_path / "not-a-session-dir"
    weird.mkdir()
    table = pa.Table.from_pylist(
        [_row(dt.datetime(2026, 7, 10, 9, tzinfo=dt.UTC), 999.0)], schema=_SCHEMA
    )
    pq.write_table(table, weird / "telemetry-001.parquet")

    q = TelemetryQuery(tmp_path)
    assert len(q.run("SELECT * FROM telemetry")) == 1  # unscoped: included
    assert q.run("SELECT * FROM telemetry", since="2026-01-01") == []  # date-filtered: excluded


# ---------------------------------------------------------------------------
# fetch_channel_rows (Tasks 5.1/5.3)
# ---------------------------------------------------------------------------


def test_fetch_channel_rows_filters_by_source_channel_key(tmp_path: Path) -> None:
    t0 = dt.datetime(2026, 5, 18, 12, 0, tzinfo=dt.UTC)
    _write_fixture(
        tmp_path,
        "2026-05-18T120000",
        [
            _row(t0, 800.0),
            {
                "timestamp": t0,
                "source": "obd",
                "channel": "SPEED",
                "value_number": 0.0,
                "value_text": None,
                "value_bool": None,
                "unit": "kph",
                "metadata_json": None,
            },
        ],
    )
    q = TelemetryQuery(tmp_path)
    rows = q.fetch_channel_rows(["obd:RPM"])
    assert len(rows) == 1
    assert rows[0]["channel_key"] == "obd:RPM"
    assert rows[0]["value_number"] == 800.0
    assert rows[0]["session"] == "2026-05-18T120000"


def test_fetch_channel_rows_empty_channel_list_short_circuits(tmp_path: Path) -> None:
    q = TelemetryQuery(tmp_path)
    assert q.fetch_channel_rows([]) == []


def test_fetch_channel_rows_scopes_by_session(tmp_path: Path) -> None:
    _three_sessions(tmp_path)
    q = TelemetryQuery(tmp_path)
    rows = q.fetch_channel_rows(["obd:RPM"], session="2026-07-10T090000")
    assert [r["value_number"] for r in rows] == [200.0]


def test_fetch_channel_rows_excludes_null_value_number(tmp_path: Path) -> None:
    t0 = dt.datetime(2026, 5, 18, 12, 0, tzinfo=dt.UTC)
    _write_fixture(
        tmp_path,
        "2026-05-18T120000",
        [
            {
                "timestamp": t0,
                "source": "obd",
                "channel": "VIN",
                "value_number": None,
                "value_text": "1N4AL11D75C109151",
                "value_bool": None,
                "unit": "",
                "metadata_json": None,
            }
        ],
    )
    q = TelemetryQuery(tmp_path)
    assert q.fetch_channel_rows(["obd:VIN"]) == []
