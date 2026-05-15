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
