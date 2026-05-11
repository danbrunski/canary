"""Tests for the tag sidecar."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from src.collection.tags import TAGS_FILENAME, find_latest_session, write_tag


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


def test_find_latest_session_picks_newest_mtime(tmp_path: Path):
    older = tmp_path / "2026-05-10T100000"
    newer = tmp_path / "2026-05-10T200000"
    older.mkdir()
    time.sleep(0.01)  # ensure mtime ordering
    newer.mkdir()
    assert find_latest_session(tmp_path) == newer


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
