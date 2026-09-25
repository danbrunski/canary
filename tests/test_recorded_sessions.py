"""The "has data" predicate and the third session question (R06.6).

``loader.has_telemetry`` replaces six hand-rolled checks that disagreed (any
chunk vs. a non-empty chunk). ``control.sessions.recorded_sessions`` answers
"every recording on disk", captures included. It stays distinct from "which
may train?" (``discover_sessions``) and "which is live?" (``latest_session``).
"""

from __future__ import annotations

from pathlib import Path

from src.control.sessions import latest_session, recorded_sessions
from src.preprocessing.loader import discover_sessions, has_telemetry


def _session(root: Path, name: str, *, data: bool = True) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "telemetry-000000.parquet").write_bytes(b"PAR1" if data else b"")
    return path


def test_has_telemetry_needs_a_non_empty_chunk(tmp_path: Path) -> None:
    empty_dir = tmp_path / "none"
    empty_dir.mkdir()
    assert not has_telemetry(empty_dir)
    assert not has_telemetry(_session(tmp_path, "zero", data=False))
    assert has_telemetry(_session(tmp_path, "one"))


def test_recorded_sessions_includes_captures_and_orders_by_start_time(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    early_capture = _session(root, "_capture-aaa-2026-09-05T100000")
    early_live = _session(root, "2026-09-10T100000")
    mid_live = _session(root, "2026-09-15T100000")
    _session(root, "2026-09-16T100000", data=False)  # rotated into, nothing flushed
    _session(root, "_replay-2026-09-17T100000")
    (root / "_archive").mkdir()
    _session(root / "_archive", "2026-09-01T100000")

    assert recorded_sessions([root]) == [early_capture, early_live, mid_live]
    # Name order would have put the capture last ("2" < "_").
    assert sorted(recorded_sessions([root])) != recorded_sessions([root])


def test_recorded_sessions_counts_a_drive_pulled_twice_once(tmp_path: Path) -> None:
    first = _session(tmp_path / "a", "2026-09-10T100000")
    _session(tmp_path / "b", "2026-09-10T100000")
    other = _session(tmp_path / "b", "2026-09-11T100000")
    assert recorded_sessions([tmp_path / "a", tmp_path / "b", tmp_path / "missing"]) == [
        first,
        other,
    ]


def test_three_questions_three_answers(tmp_path: Path) -> None:
    live = _session(tmp_path, "2026-09-10T100000")
    capture = _session(tmp_path, "_capture-tuning-2026-09-11T100000")
    assert discover_sessions(tmp_path) == [live]  # which may train: no captures
    assert latest_session(tmp_path) == capture  # which is live: the newest, captures too
    assert recorded_sessions([tmp_path]) == [live, capture]  # every recording
