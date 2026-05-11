"""Tests for shared session helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.session import build_windows, load_tags


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
