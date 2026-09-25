"""Tests for shared session helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src import session as session_mod
from src.session import (
    ALL_SESSION_KINDS,
    RECORDED_SESSION_KINDS,
    SessionKind,
    build_windows,
    classify_session_name,
    dir_size,
    format_session_timestamp,
    iter_sessions,
    load_tags,
    make_session_dir,
    make_unique_session_dir,
    parse_session_timestamp,
    session_sort_key,
)


def test_build_windows_sorts_backfilled_tags() -> None:
    tags = [
        {"t": 20.0, "label": "second"},
        {"t": 10.0, "label": "first"},
    ]
    windows = build_windows(tags, session_end=30.0)
    assert [(w.label, w.start, w.end) for w in windows] == [
        ("first", 10.0, 20.0),
        ("second", 20.0, 30.0),
    ]


def test_build_windows_rejects_duplicate_timestamps() -> None:
    tags = [
        {"t": 10.0, "label": "first"},
        {"t": 10.0, "label": "second"},
    ]
    with pytest.raises(ValueError, match="strictly increasing"):
        build_windows(tags, session_end=30.0)


def test_build_windows_adds_pre_tag_window() -> None:
    tags = [{"t": 10.0, "label": "first"}]
    windows = build_windows(tags, session_start=5.0, session_end=20.0)
    assert [(w.label, w.start, w.end) for w in windows] == [
        ("(pre-tags)", 5.0, 10.0),
        ("first", 10.0, 20.0),
    ]


def test_load_tags_skips_malformed_jsonl_lines(tmp_path: Path) -> None:
    path = tmp_path / "tags.jsonl"
    path.write_text(
        json.dumps({"t": 1.0, "label": "ok"}) + "\n"
        '{"t": 2.0, "label": "partial"\n'
        "{}\n" + json.dumps({"t": 3.0, "label": "also_ok"}) + "\n"
    )

    assert [tag["label"] for tag in load_tags(path)] == ["ok", "also_ok"]


# ---------------------------------------------------------------------------
# load_tags + invalidate corrections
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_load_tags_drops_event_tags_invalidated_by_label(tmp_path: Path) -> None:
    """An invalidate record without target_t drops EVERY tag of that label."""
    path = tmp_path / "tags.jsonl"
    _write_jsonl(
        path,
        [
            {"t": 1.0, "kind": "event", "label": "hvac_ac_on"},
            {"t": 2.0, "kind": "event", "label": "steady_25_flat"},
            {"t": 3.0, "kind": "event", "label": "hvac_ac_on"},  # second occurrence
            {
                "t": 99.0,
                "kind": "invalidate",
                "label": "invalidate:hvac_ac_on",
                "target_label": "hvac_ac_on",
                "reason": "AC never engaged",
            },
        ],
    )
    labels = [tag["label"] for tag in load_tags(path)]
    assert labels == ["steady_25_flat"]


def test_load_tags_invalidate_can_narrow_by_target_t(tmp_path: Path) -> None:
    """With target_t, ONLY the matching (label, t) tag is dropped; others stay."""
    path = tmp_path / "tags.jsonl"
    _write_jsonl(
        path,
        [
            {"t": 1.0, "kind": "event", "label": "right_turn_sharp"},
            {"t": 5.0, "kind": "event", "label": "right_turn_sharp"},  # the bad one
            {"t": 9.0, "kind": "event", "label": "right_turn_sharp"},
            {
                "t": 99.0,
                "kind": "invalidate",
                "label": "invalidate:right_turn_sharp",
                "target_label": "right_turn_sharp",
                "target_t": 5.0,
            },
        ],
    )
    kept = load_tags(path)
    assert [(t["label"], t["t"]) for t in kept] == [
        ("right_turn_sharp", 1.0),
        ("right_turn_sharp", 9.0),
    ]


def test_load_tags_filters_out_invalidate_records_themselves(tmp_path: Path) -> None:
    """Corrections are metadata, not windows — they must not appear in the output."""
    path = tmp_path / "tags.jsonl"
    _write_jsonl(
        path,
        [
            {"t": 1.0, "kind": "event", "label": "a"},
            {
                "t": 2.0,
                "kind": "invalidate",
                "label": "invalidate:nope",
                "target_label": "nope",
            },
        ],
    )
    out = load_tags(path)
    assert [t["kind"] for t in out] == ["event"]


def test_load_tags_ignores_malformed_invalidate_records(tmp_path: Path) -> None:
    """An invalidate missing target_label must not silently drop everything."""
    path = tmp_path / "tags.jsonl"
    _write_jsonl(
        path,
        [
            {"t": 1.0, "kind": "event", "label": "a"},
            {"t": 2.0, "kind": "event", "label": "b"},
            {"t": 3.0, "kind": "invalidate", "label": "broken"},  # no target_label
        ],
    )
    assert [t["label"] for t in load_tags(path)] == ["a", "b"]


def test_load_tags_invalidate_with_unparseable_target_t_falls_back_safely(
    tmp_path: Path,
) -> None:
    """A non-numeric target_t shouldn't widen the invalidation to all labels."""
    path = tmp_path / "tags.jsonl"
    _write_jsonl(
        path,
        [
            {"t": 1.0, "kind": "event", "label": "x"},
            {
                "t": 2.0,
                "kind": "invalidate",
                "label": "invalidate:x",
                "target_label": "x",
                "target_t": "not-a-number",
            },
        ],
    )
    # The invalidate is malformed — drop it but keep the event tag rather than
    # accidentally treating the malformed record as "invalidate all of label x".
    assert [t["label"] for t in load_tags(path)] == ["x"]


# --------------------------------------------------------------------------- #
# R01.1 — one session-name module
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("2026-08-05T142720", SessionKind.LIVE),
        ("_capture-fuel-map-2026-08-05T142720", SessionKind.CAPTURE),
        ("_tuning-2026-08-05T142720", SessionKind.TUNING),
        ("_replay-2026-08-05T142720", SessionKind.REPLAY),
    ],
)
def test_classify_session_name_kinds(name: str, kind: SessionKind) -> None:
    parsed = classify_session_name(name)
    assert parsed is not None
    assert parsed.kind is kind
    assert parsed.started_at == datetime(2026, 8, 5, 14, 27, 20, tzinfo=UTC)


@pytest.mark.parametrize(
    "name",
    [
        "_archive",
        "scratch",
        "2026-13-45T999999",
        "_capture-2026-08-05T142720",
        "x2026-08-05T142720",
    ],
)
def test_classify_session_name_refuses_non_sessions(name: str) -> None:
    assert classify_session_name(name) is None


def test_format_and_parse_round_trip() -> None:
    moment = datetime(2026, 9, 24, 7, 5, 9, tzinfo=UTC)
    stamp = format_session_timestamp(moment)
    assert stamp == "2026-09-24T070509"
    assert parse_session_timestamp(stamp) == moment


def test_session_sort_key_is_total_over_unparseable_names() -> None:
    """``evaluate_detectors._spread_in_time`` keyed on
    ``(session_started_at(n), n)`` and raised ``TypeError`` comparing ``None``
    with a datetime."""
    names = ["_tuning-2026-08-01T100000", "manual-import", "2026-07-01T100000"]
    assert sorted(names, key=session_sort_key) == [
        "manual-import",
        "2026-07-01T100000",
        "_tuning-2026-08-01T100000",
    ]


def test_iter_sessions_orders_by_time_and_filters_kinds(tmp_path: Path) -> None:
    for name in (
        "_tuning-2026-08-01T100000",
        "_capture-fuel-map-2026-09-01T100000",
        "2026-08-15T100000",
        "_replay-2026-09-02T100000",
        "_archive",
    ):
        (tmp_path / name).mkdir()
    (tmp_path / "2026-10-01T100000").write_text("a file, not a session")

    assert [p.name for p in iter_sessions(tmp_path, kinds=ALL_SESSION_KINDS)] == [
        "_tuning-2026-08-01T100000",
        "2026-08-15T100000",
        "_capture-fuel-map-2026-09-01T100000",
        "_replay-2026-09-02T100000",
    ]
    assert [p.name for p in iter_sessions(tmp_path, kinds=RECORDED_SESSION_KINDS)] == [
        "_tuning-2026-08-01T100000",
        "2026-08-15T100000",
        "_capture-fuel-map-2026-09-01T100000",
    ]
    assert iter_sessions(tmp_path / "missing", kinds=ALL_SESSION_KINDS) == []


def test_list_sessions_with_captures_and_limit_returns_newest_by_time(tmp_path: Path) -> None:
    """R01.1: sorted by ``session_id``, "newest N" was the lexically-last
    profile slugs, not the newest captures."""
    from src.control.sessions import filter_sessions, list_sessions

    for name in (
        "_capture-zeta-2026-08-01T100000",
        "_capture-alpha-2026-09-01T100000",
        "_capture-beta-2026-09-02T100000",
    ):
        (tmp_path / name).mkdir()

    newest = filter_sessions(list_sessions(tmp_path, captures=True), limit=2)

    assert [s.session_id for s in newest] == [
        "_capture-alpha-2026-09-01T100000",
        "_capture-beta-2026-09-02T100000",
    ]


def test_tuning_sessions_are_http_resolvable(tmp_path: Path) -> None:
    """R01.1, operator decision 2026-09-25: ``_tuning-*`` is this vehicle's
    own recording, so HTTP reads resolve it and sidecars may be computed over
    it. Training still skips it (the ``_`` prefix); replay output is still
    not a session."""
    from src.control.sessions import (
        is_session_dir_name,
        latest_session,
        list_sessions,
        recorded_sessions,
        resolve_session,
    )
    from src.preprocessing.loader import discover_sessions
    from src.storage.parquet_writer import write_records_to_parquet
    from src.telemetry import TelemetryRecord

    assert SessionKind.TUNING in RECORDED_SESSION_KINDS
    assert is_session_dir_name("_tuning-2026-08-05T142720")
    assert is_session_dir_name("_capture-fuel-map-2026-08-05T142720")
    assert not is_session_dir_name("_replay-2026-08-05T142720")

    names = ("2026-08-05T100000", "_tuning-2026-08-05T142720", "_replay-2026-08-06T100000")
    for name in names:
        write_records_to_parquet(
            [TelemetryRecord.obd(1_778_000_000.0, "RPM", 800.0, "rpm")], tmp_path / name
        )
    tuning = tmp_path / "_tuning-2026-08-05T142720"
    assert resolve_session(tmp_path, tuning.name) == tuning
    assert [p.name for p in recorded_sessions([tmp_path])] == [
        "2026-08-05T100000",
        "_tuning-2026-08-05T142720",
    ]
    assert [s.session_id for s in list_sessions(tmp_path, captures=True)] == [tuning.name]
    # "Which is live?" already answered over every kind; the newest is replay.
    assert latest_session(tmp_path) == tmp_path / "_replay-2026-08-06T100000"
    # "Which may train?" still excludes it.
    assert [p.name for p in discover_sessions(tmp_path)] == ["2026-08-05T100000"]


def test_dir_size_counts_nested_files(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"123")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"45")
    assert dir_size(tmp_path) == 5


def test_make_unique_session_dir_bumps_past_taken_seconds_in_order(tmp_path: Path) -> None:
    started = datetime(2026, 9, 25, 23, 30, 19, tzinfo=UTC)
    made = [make_unique_session_dir(tmp_path, started).root for _ in range(3)]
    assert [path.name for path in made] == [
        "2026-09-25T233019",
        "2026-09-25T233020",
        "2026-09-25T233021",
    ]
    # Still plain, parseable stamps, ordered by start under the one sort key.
    assert all(classify_session_name(path.name) is not None for path in made)
    assert sorted(made, key=session_sort_key) == made
    # The strict form keeps refusing for callers that rely on it.
    with pytest.raises(FileExistsError):
        make_session_dir(tmp_path, started)


def test_make_unique_session_dir_gives_up_naming_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_mod, "UNIQUE_SESSION_DIR_ATTEMPTS", 2)
    started = datetime(2026, 9, 25, 23, 30, 19, tzinfo=UTC)
    make_unique_session_dir(tmp_path, started)
    make_unique_session_dir(tmp_path, started)
    with pytest.raises(FileExistsError, match="within 2 s of 2026-09-25T233019"):
        make_unique_session_dir(tmp_path, started)
