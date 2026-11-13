"""R23.2: three request paths stopped walking the whole corpus; same answers.

``latest_session`` (polled by ``/status``), ``list_sessions`` (behind
``GET /control/sessions?limit=``) and ``TelemetryQuery._matching_files`` (every
unscoped, dated or named query) each did their work for every session on disk
to answer a question about the newest, a few, or one. They were rewritten to
walk newest first, to apply bounds to parsed directory names before summarizing,
and to list only the directories a query needs.

The old implementations are kept here verbatim as the reference
("old answer"). Every case runs both over a synthetic tree built to hurt the
rewrite: captures newer than any live session, an empty newest directory, a
zero-byte chunk, ``_archive`` and ``_replay-*``, directory names whose string
order is not their time order, a directory that holds chunks and a directory
inside it that holds chunks, and names that merely look like a chunk's.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.session import ALL_SESSION_KINDS, iter_sessions
from src.storage import sessions as sessions_module
from src.storage import telemetry_query
from src.storage.catalog import SessionCatalog
from src.storage.session_files import PARQUET_PATTERN, has_telemetry
from src.storage.sessions import (
    SessionSummary,
    filter_sessions,
    latest_session,
    list_sessions,
    session_started_at,
)
from src.storage.telemetry_query import TelemetryQuery, _parse_bound, _parse_session_timestamp

# -- the old answers, verbatim -------------------------------------------------


def _old_latest_session(root: Path) -> Path | None:
    dated = [
        child for child in iter_sessions(root, kinds=ALL_SESSION_KINDS) if has_telemetry(child)
    ]
    return dated[-1] if dated else None


def _old_filter_sessions(
    sessions: list[SessionSummary],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int | None = None,
) -> list[SessionSummary]:
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


def _old_matching_files(
    query: TelemetryQuery,
    *,
    session: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[str]:
    """``TelemetryQuery._matching_files`` as it was before R23.2 (``rglob``)."""
    storage_path = query._storage_path
    since_dt = _parse_bound(since, label="since") if since else None
    until_dt = _parse_bound(until, label="until") if until else None
    date_filtered = since_dt is not None or until_dt is not None

    if session is not None:
        if query._catalog is not None:
            row = query._catalog.get_session(session)
            if row is not None and Path(row.path).is_dir():
                return [str(p) for p in sorted(Path(row.path).glob("telemetry-*.parquet"))]
        return [
            str(p)
            for p in sorted(storage_path.rglob("telemetry-*.parquet"))
            if p.parent.name == session
        ]

    matches: list[str] = []
    for p in sorted(storage_path.rglob("telemetry-*.parquet")):
        session_name = p.parent.name
        relative_dirs = p.relative_to(storage_path).parts[:-1]
        if any(part.startswith("_") for part in relative_dirs):
            continue
        if date_filtered:
            ts = query._catalog_started_at(session_name) if query._catalog is not None else None
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


# -- the tree built to hurt a rewrite --------------------------------------------


def _chunks(directory: Path, count: int = 2, *, size: int = 8) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (directory / f"telemetry-{index:05d}.parquet").write_bytes(b"x" * size)


def _tree(root: Path) -> Path:
    """Names and what each is for. ``root`` holds the sessions directly."""
    # Live sessions, with several chunks each.
    for name in (
        "2026-09-01T100000",
        "2026-09-02T100000",
        "2026-09-03T100000",
        "2026-10-20T080000",
    ):
        _chunks(root / name, 3)
    # Newer than every live session by time, but ``_capture-`` sorts before
    # ``_tuning-`` and both before a digit's successor: by name this is "oldest".
    _chunks(root / "_capture-fuel-map-2026-11-01T120000", 2)
    _chunks(root / "_capture-aaa-2026-11-05T090000", 1)
    # Sorts last by name (``_t`` > ``_c``) yet earlier than both captures by time.
    _chunks(root / "_tuning-2026-10-25T000000", 1)
    # Runtime replay output is "live" to latest_session and never to training.
    _chunks(root / "_replay-2026-08-01T000000", 2)
    # Quarantine: nested one layer down, under an underscore directory.
    _chunks(root / "_archive" / "2026-09-02T100000", 2)
    _chunks(root / "_archive" / "2026-07-01T100000", 1)
    # The newest directory holds nothing (the writer has just rotated into it),
    # and the one before it holds only a zero-byte chunk (a crash mid-write).
    (root / "2026-12-01T000000").mkdir()
    (root / "2026-11-30T000000").mkdir()
    (root / "2026-11-30T000000" / "telemetry-00000.parquet").write_bytes(b"")
    # Names that look like chunks and are not (and one directory that is not a session).
    _chunks(root / "2026-09-04T100000", 1)
    for lookalike in (
        "telemetry-00000.parquet.tmp",
        "telemetry.parquet",
        "xtelemetry-00000.parquet",
    ):
        (root / "2026-09-04T100000" / lookalike).write_bytes(b"x")
    (root / "notes").mkdir()
    (root / "notes" / "readme.txt").write_text("not a session")
    # A profile layer above its sessions, as a root one level up would see.
    _chunks(root / "profile-x" / "2026-09-10T100000", 2)
    _chunks(root / "profile-x" / "2026-11-10T100000", 1)
    # A directory that holds chunks with a directory inside it that holds chunks:
    # as Path objects, loose/inner/... sorts before loose/telemetry-... (and
    # "loose-b/..." after both, though "-" < "/" as characters).
    _chunks(root / "loose", 2)
    _chunks(root / "loose" / "inner", 1)
    _chunks(root / "loose-b", 1)
    return root


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return _tree(tmp_path / "sessions")


@pytest.fixture
def above(tree: Path) -> Path:
    """A root one level above ``tree``: its sessions sit under a layer."""
    return tree.parent


@pytest.fixture
def catalog(tree: Path, tmp_path: Path) -> Iterator[SessionCatalog]:
    """A catalog that knows some sessions, one of them under a different start
    than its name says (the catalog wins in a dated sweep), and not the rest."""
    with SessionCatalog(tmp_path / "catalog.db") as cat:
        for name in ("2026-09-01T100000", "2026-09-03T100000", "2026-10-20T080000"):
            cat.record_session(tree / name)
        moved = cat.get_session("2026-09-03T100000")
        assert moved is not None
        cat._upsert(dataclasses.replace(moved, started_at="2026-12-25T00:00:00+00:00"))
        yield cat


# -- latest_session --------------------------------------------------------------


def _spy_on_has_telemetry(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record each session directory ``latest_session`` asks about."""
    asked: list[str] = []

    def spy(session_dir: Path) -> bool:
        asked.append(session_dir.name)
        return has_telemetry(session_dir)

    monkeypatch.setattr("src.storage.sessions.has_telemetry", spy)
    return asked


def test_latest_session_answers_what_it_always_did(tree: Path) -> None:
    assert latest_session(tree) == _old_latest_session(tree)


def test_latest_session_counts_captures_and_orders_by_time_not_name(tree: Path) -> None:
    # `_capture-aaa-2026-11-05` is by time the newest session holding data, though
    # it sorts first by name; `2026-12-01` is empty and `2026-11-30` has a zero-byte
    # chunk, so neither is "live".
    assert latest_session(tree) == tree / "_capture-aaa-2026-11-05T090000"
    assert session_started_at(tree / "_tuning-2026-10-25T000000") is not None


def test_latest_session_skips_an_empty_newest_directory_and_stops_there(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _spy_on_has_telemetry(monkeypatch)

    assert latest_session(tree) == tree / "_capture-aaa-2026-11-05T090000"
    # the empty 12-01 and the zero-byte 11-30, then the answer; nothing older was asked
    assert asked == ["2026-12-01T000000", "2026-11-30T000000", "_capture-aaa-2026-11-05T090000"]


def test_latest_session_asks_once_when_the_newest_holds_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for day in range(1, 31):
        _chunks(tmp_path / f"2026-09-{day:02d}T100000")
    asked = _spy_on_has_telemetry(monkeypatch)

    assert latest_session(tmp_path) == tmp_path / "2026-09-30T100000"
    assert asked == ["2026-09-30T100000"]


def test_latest_session_is_none_without_data(tmp_path: Path) -> None:
    assert latest_session(tmp_path / "absent") is None
    assert latest_session(tmp_path) is None
    (tmp_path / "2026-09-01T100000").mkdir()
    (tmp_path / "_archive").mkdir()
    assert latest_session(tmp_path) is None == _old_latest_session(tmp_path)


def test_latest_session_matches_the_old_answer_as_a_tree_grows(tmp_path: Path) -> None:
    steps: list[Callable[[], None]] = [
        lambda: None,
        lambda: (tmp_path / "2026-09-01T100000").mkdir(),
        lambda: _chunks(tmp_path / "2026-09-01T100000"),
        lambda: (tmp_path / "2026-09-02T100000").mkdir(),
        lambda: _chunks(tmp_path / "_capture-x-2026-09-01T120000"),
        lambda: _chunks(tmp_path / "_tuning-2026-09-03T000000"),
        lambda: _chunks(tmp_path / "_replay-2026-09-04T000000"),
        lambda: _chunks(tmp_path / "2026-09-02T100000"),
        lambda: _chunks(tmp_path / "_archive" / "2026-12-31T000000"),
    ]
    for step in steps:
        step()
        assert latest_session(tmp_path) == _old_latest_session(tmp_path)


# -- list_sessions ----------------------------------------------------------------

_D = datetime
_BOUNDS: list[tuple[datetime | None, datetime | None]] = [
    (None, None),
    (_D(2026, 9, 2, tzinfo=UTC), None),
    (None, _D(2026, 9, 3, tzinfo=UTC)),
    (_D(2026, 9, 1), _D(2026, 9, 3, 10)),  # naive means UTC
    (_D(2026, 9, 2, 10, tzinfo=UTC), _D(2026, 9, 2, 10, tzinfo=UTC)),  # inclusive: one instant
    (
        _D(2026, 9, 1, 5, tzinfo=timezone(timedelta(hours=-6))),
        _D(2026, 9, 3, 19, tzinfo=timezone(timedelta(hours=9))),
    ),
    (_D(2027, 1, 1, tzinfo=UTC), None),  # nothing
    (_D(2026, 9, 3, tzinfo=UTC), _D(2026, 9, 1, tzinfo=UTC)),  # inverted: nothing
]


def _ids(summaries: list[SessionSummary]) -> list[str]:
    return [summary.session_id for summary in summaries]


@pytest.mark.parametrize("captures", [False, True])
@pytest.mark.parametrize("limit", [None, 1, 2, 3, 100, 0])
@pytest.mark.parametrize(("since", "until"), _BOUNDS)
def test_list_sessions_with_bounds_is_the_old_list_then_filter(
    tree: Path, captures: bool, since: datetime | None, until: datetime | None, limit: int | None
) -> None:
    old = _old_filter_sessions(
        list_sessions(tree, captures=captures), since=since, until=until, limit=limit
    )
    new = list_sessions(tree, captures=captures, since=since, until=until, limit=limit)

    assert [s.to_dict() for s in new] == [s.to_dict() for s in old]
    # filter_sessions (now sharing the bound test) is still what it was
    unlimited = list_sessions(tree, captures=captures)
    assert _ids(filter_sessions(unlimited, since=since, until=until, limit=limit)) == _ids(old)


def test_list_sessions_keeps_the_newest_by_time_with_captures(tree: Path) -> None:
    newest = list_sessions(tree, captures=True, limit=2)
    # `_tuning-` sorts after `_capture-` by name; the captures are newer by time
    assert _ids(newest) == [
        "_capture-fuel-map-2026-11-01T120000",
        "_capture-aaa-2026-11-05T090000",
    ]
    assert _ids(list_sessions(tree, limit=2)) == ["2026-11-30T000000", "2026-12-01T000000"]


def test_list_sessions_leaves_replay_archive_and_strays_out(tree: Path) -> None:
    listed = _ids(list_sessions(tree)) + _ids(list_sessions(tree, captures=True))
    assert not any(name.startswith(("_replay", "_archive")) for name in listed)
    assert "notes" not in listed and "loose" not in listed


def test_list_sessions_summarizes_only_what_it_returns(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summarized: list[str] = []
    real = sessions_module.summarize_session

    def spy(session: Path) -> SessionSummary:
        summarized.append(session.name)
        return real(session)

    monkeypatch.setattr(sessions_module, "summarize_session", spy)

    listed = list_sessions(tree, limit=2)
    assert summarized == _ids(listed) and len(summarized) == 2

    summarized.clear()
    listed = list_sessions(
        tree, since=_D(2026, 9, 2, tzinfo=UTC), until=_D(2026, 9, 3, 23, tzinfo=UTC)
    )
    assert summarized == ["2026-09-02T100000", "2026-09-03T100000"]

    summarized.clear()
    list_sessions(tree, since=_D(2030, 1, 1, tzinfo=UTC))
    assert summarized == []


# -- TelemetryQuery._matching_files -----------------------------------------------

_DATE_SCENARIOS: dict[str, dict[str, str]] = {
    "unscoped": {},
    "since": {"since": "2026-09-02"},
    "until": {"until": "2026-09-03"},
    "range": {"since": "2026-09-01", "until": "2026-09-10T100000"},
    "session-id bounds": {"since": "2026-09-02T100000", "until": "2026-09-03T100000"},
    "exact instant": {"since": "2026-09-02T100000", "until": "2026-09-02T100000"},
    "iso with offsets": {
        "since": "2026-09-01T05:00:00-06:00",
        "until": "2026-09-03T19:00:00+09:00",
    },
    "empty": {"since": "2030-01-01"},
    "inverted": {"since": "2026-09-03", "until": "2026-09-01"},
}


def _named_scenarios() -> dict[str, dict[str, str]]:
    return {
        f"session={name}": {"session": name}
        for name in (
            "2026-09-01T100000",
            "2026-09-02T100000",  # also in _archive
            "2026-07-01T100000",  # only in _archive
            "2026-12-01T000000",  # empty
            "_capture-aaa-2026-11-05T090000",
            "_tuning-2026-10-25T000000",
            "_replay-2026-08-01T000000",
            "2026-09-04T100000",  # chunk lookalikes beside real chunks
            "2026-09-10T100000",  # under a layer
            "loose",
            "inner",
            "loose-b",
            "notes",
            "_archive",
            "profile-x",
            "does-not-exist",
        )
    }


@pytest.mark.parametrize("scenario", list(_DATE_SCENARIOS) + list(_named_scenarios()))
@pytest.mark.parametrize("where", ["sessions", "above"])
def test_matching_files_is_the_old_walk(tree: Path, above: Path, where: str, scenario: str) -> None:
    root = tree if where == "sessions" else above
    query = TelemetryQuery(root)
    kwargs = {**_DATE_SCENARIOS, **_named_scenarios()}[scenario]

    assert query._matching_files(**kwargs) == _old_matching_files(query, **kwargs)


@pytest.mark.parametrize("scenario", list(_DATE_SCENARIOS) + list(_named_scenarios()))
def test_matching_files_with_a_catalog_is_the_old_walk(
    tree: Path, catalog: SessionCatalog, scenario: str
) -> None:
    query = TelemetryQuery(tree, catalog=catalog)
    kwargs = {**_DATE_SCENARIOS, **_named_scenarios()}[scenario]

    assert query._matching_files(**kwargs) == _old_matching_files(query, **kwargs)


def test_the_catalogs_start_wins_over_the_name_in_a_dated_sweep(
    tree: Path, catalog: SessionCatalog
) -> None:
    """The reference's behavior, held: 2026-09-03 is catalogued as starting on
    Christmas, so a September range loses it and a December one finds it."""
    query = TelemetryQuery(tree, catalog=catalog)
    september = query._matching_files(since="2026-09-03", until="2026-09-04")
    december = query._matching_files(since="2026-12-24", until="2026-12-26")

    assert not any("2026-09-03T100000" in path for path in september)
    assert [Path(p).parent.name for p in december] == ["2026-09-03T100000"] * 3


def test_unscoped_sweep_never_leaves_the_underscore_directories_unlisted_but_skips_them(
    tree: Path,
) -> None:
    files = TelemetryQuery(tree)._matching_files()
    parents = {Path(p).parent.name for p in files}
    assert not any(name.startswith("_") for name in parents)
    assert "_archive" not in "".join(files) and "_capture" not in "".join(files)
    # a directory that is not a session still contributes its chunks, undated
    assert {"loose", "inner", "loose-b", "2026-11-30T000000"} <= parents
    assert not any(
        p.endswith((".tmp", "telemetry.parquet", "xtelemetry-00000.parquet")) for p in files
    )


def test_path_order_is_kept_where_a_directory_holds_a_directory(tree: Path) -> None:
    files = TelemetryQuery(tree)._matching_files()
    loose = [Path(p).relative_to(tree).as_posix() for p in files if "/loose" in p]
    assert loose == [
        "loose/inner/telemetry-00000.parquet",
        "loose/telemetry-00000.parquet",
        "loose/telemetry-00001.parquet",
        "loose-b/telemetry-00000.parquet",
    ]
    assert files == sorted(files, key=lambda p: Path(p).parts)


def test_a_dated_sweep_lists_no_directory_outside_the_range(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: list[str] = []
    real = os.scandir

    def spy(path: Any) -> Any:
        listed.append(Path(path).name)
        return real(path)

    monkeypatch.setattr("src.storage.telemetry_query.os.scandir", spy)

    files = TelemetryQuery(tree)._matching_files(since="2026-09-02", until="2026-09-03T235959")

    assert [Path(p).parent.name for p in files] == ["2026-09-02T100000"] * 3 + [
        "2026-09-03T100000"
    ] * 3
    # the root and the layers that are not dated sessions, then the two in range
    assert sorted(listed) == sorted(
        [
            "sessions",
            "2026-09-02T100000",
            "2026-09-03T100000",
            "notes",
            "profile-x",
            "loose",
            "loose-b",
            "inner",
        ]
    )


def test_an_unscoped_sweep_never_enters_an_underscore_directory(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: list[Path] = []
    real = os.scandir

    def spy(path: Any) -> Any:
        listed.append(Path(path))
        return real(path)

    monkeypatch.setattr("src.storage.telemetry_query.os.scandir", spy)

    TelemetryQuery(tree)._matching_files()

    assert listed and not any(
        part.startswith("_") for p in listed for part in p.relative_to(tree).parts
    )


def test_a_named_lookup_lists_the_layers_and_the_session_only(
    above: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: list[Path] = []
    real = os.scandir

    def spy(path: Any) -> Any:
        listed.append(Path(path))
        return real(path)

    monkeypatch.setattr("src.storage.telemetry_query.os.scandir", spy)

    files = TelemetryQuery(above)._matching_files(session="2026-09-02T100000")

    assert len(files) == 3 + 2  # the live session and its quarantined namesake
    names = sorted(p.relative_to(above).as_posix() for p in listed)
    assert names == [
        ".",
        "sessions",
        "sessions/2026-09-02T100000",
        "sessions/_archive",
        "sessions/_archive/2026-09-02T100000",
        "sessions/loose",
        "sessions/loose-b",
        "sessions/loose/inner",
        "sessions/notes",
        "sessions/profile-x",
    ]


def test_an_absent_or_empty_root_is_an_empty_tree(tmp_path: Path) -> None:
    assert TelemetryQuery(tmp_path / "absent")._matching_files() == []
    assert TelemetryQuery(tmp_path / "absent")._matching_files(since="2026-01-01") == []
    assert TelemetryQuery(tmp_path / "absent")._matching_files(session="x") == []
    tmp_path.joinpath("empty").mkdir()
    assert TelemetryQuery(tmp_path / "empty")._matching_files() == []


def test_the_walk_matches_chunk_names_as_the_loader_pattern_does() -> None:
    """The walk splits ``PARQUET_PATTERN`` at its one ``*``; a pattern with a
    second wildcard would no longer be a prefix and a suffix."""
    assert PARQUET_PATTERN.count("*") == 1
    for name in (
        "telemetry-00000.parquet",
        "telemetry-.parquet",
        "telemetry-00000.parquet.tmp",
        "telemetry.parquet",
        "xtelemetry-00000.parquet",
        "telemetry-00000.parquetx",
        "telemetry-parquet",
    ):
        walked = name.startswith(telemetry_query._CHUNK_PREFIX) and name.endswith(
            telemetry_query._CHUNK_SUFFIX
        )
        assert walked == fnmatch.fnmatchcase(name, PARQUET_PATTERN), name


def test_a_relative_root_gives_the_paths_a_path_join_gives(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tree)
    for root in (".", "", "./"):
        query = TelemetryQuery(Path(root))
        assert query._matching_files() == _old_matching_files(query)
        assert query._matching_files(session="loose") == _old_matching_files(query, session="loose")
    monkeypatch.chdir(tree.parent)
    query = TelemetryQuery(Path("sessions"))
    assert query._matching_files() == _old_matching_files(query)
