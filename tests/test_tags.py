"""Tests for the tag sidecar."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from src.collection.tags import TAGS_FILENAME, find_latest_session, write_correction, write_tag


def _read_tags(session_dir: Path) -> list[dict]:
    text = (session_dir / TAGS_FILENAME).read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_write_tag_appends_jsonl(tmp_path: Path):
    write_tag(tmp_path, "hello")
    write_tag(tmp_path, "world", kind="phase_start", phase="warmup")

    tags = _read_tags(tmp_path)
    assert len(tags) == 2
    assert tags[0]["label"] == "hello"
    assert tags[0]["kind"] == "event"
    assert isinstance(tags[0]["t"], float)
    assert tags[1] == {**tags[1], "label": "world", "kind": "phase_start", "phase": "warmup"}


def test_write_tag_uses_explicit_timestamp(tmp_path: Path):
    record = write_tag(tmp_path, "backfilled", timestamp=1234567890.5)
    assert record["t"] == 1234567890.5
    assert _read_tags(tmp_path)[0]["t"] == 1234567890.5


def test_write_tag_rejects_t_in_extra_fields(tmp_path: Path):
    """`t` would silently overwrite the record's timestamp — must error."""
    with pytest.raises(ValueError, match="reserved tag field 't'"):
        write_tag(tmp_path, "x", t=999.0)


def test_write_tag_concurrent_writers_dont_interleave(tmp_path: Path):
    """A second terminal tagging while the RE runner also tags must produce
    valid JSONL — no half-written lines or merged records."""
    n_per_thread = 50
    n_threads = 8

    def worker(tid: int) -> None:
        for i in range(n_per_thread):
            write_tag(tmp_path, f"t{tid}-i{i}", thread=tid, seq=i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tags = _read_tags(tmp_path)
    assert len(tags) == n_per_thread * n_threads
    # Every line must be parseable JSON with the expected fields — proves no
    # interleaving of partial writes.
    assert all("label" in tag and "thread" in tag for tag in tags)


def test_find_latest_session_picks_newest_timestamped_name(tmp_path: Path):
    older = tmp_path / "2026-05-10T100000"
    newer = tmp_path / "2026-05-10T200000"
    older.mkdir()
    newer.mkdir()
    write_tag(older, "touch older")
    assert find_latest_session(tmp_path) == newer


def test_find_latest_session_ignores_non_timestamp_directories(tmp_path: Path):
    (tmp_path / "scratch").mkdir()
    sess = tmp_path / "2026-05-10T100000"
    sess.mkdir()
    assert find_latest_session(tmp_path) == sess


def test_find_latest_session_returns_an_open_capture(tmp_path: Path):
    """R01.1: "which session is live?" includes captures.

    The plain-name-only ``max(name)`` tagged the *previous* session for the
    whole of a ``_capture-*`` recording, which is exactly when an operator
    tags physical actions.
    """
    (tmp_path / "2026-09-20T100000").mkdir()
    capture = tmp_path / "_capture-fuel-map-2026-09-20T101500"
    capture.mkdir()
    assert find_latest_session(tmp_path) == capture


def test_find_latest_session_orders_by_time_not_name(tmp_path: Path):
    """``_capture-`` sorts before every plain name as a string."""
    (tmp_path / "_capture-fuel-map-2026-09-20T101500").mkdir()
    newer = tmp_path / "2026-09-20T110000"
    newer.mkdir()
    assert find_latest_session(tmp_path) == newer


def test_find_latest_session_never_picks_replay_output(tmp_path: Path):
    """A tag records a physical action; replay rows are re-emitted."""
    sess = tmp_path / "2026-09-20T100000"
    sess.mkdir()
    (tmp_path / "_replay-2026-09-20T120000").mkdir()
    assert find_latest_session(tmp_path) == sess


def test_find_latest_session_errors_when_empty(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="no session"):
        find_latest_session(tmp_path)


def test_find_latest_session_errors_when_root_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        find_latest_session(tmp_path / "nonexistent")


def test_find_latest_session_ignores_files(tmp_path: Path):
    (tmp_path / "stray-file").write_text("not a session")
    sess = tmp_path / "2026-05-10T100000"
    sess.mkdir()
    assert find_latest_session(tmp_path) == sess


# ---------------------------------------------------------------------------
# write_correction
# ---------------------------------------------------------------------------


def test_write_correction_appends_invalidate_record(tmp_path: Path):
    write_correction(tmp_path, "hvac_ac_on", reason="never engaged")
    tags = _read_tags(tmp_path)
    assert len(tags) == 1
    record = tags[0]
    assert record["kind"] == "invalidate"
    assert record["target_label"] == "hvac_ac_on"
    assert record["reason"] == "never engaged"
    assert "target_t" not in record  # absent → invalidate-all-by-label
    assert record["label"].startswith("invalidate:")


def test_write_correction_narrows_by_target_t(tmp_path: Path):
    write_correction(tmp_path, "right_turn_sharp", target_t=1778811025.0123)
    record = _read_tags(tmp_path)[0]
    assert record["target_t"] == 1778811025.0123


def test_write_correction_uses_explicit_timestamp(tmp_path: Path):
    record = write_correction(tmp_path, "x", timestamp=42.0)
    assert record["t"] == 42.0


def test_write_correction_coexists_with_event_tags(tmp_path: Path):
    """Real-world: event tags written during the session, corrections appended after."""
    write_tag(tmp_path, "hvac_ac_on")
    write_tag(tmp_path, "steady_25_flat")
    write_correction(tmp_path, "hvac_ac_on", reason="missed it")
    tags = _read_tags(tmp_path)
    assert [t["kind"] for t in tags] == ["event", "event", "invalidate"]
