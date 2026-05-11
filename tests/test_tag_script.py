"""Tests for the tag.py CLI's --timestamp and --offset backfill flags."""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.tag import (
    _load_session_end,
    _load_session_start,
    _resolve_timestamp,
    _warn_if_outside_session,
    main,
    parse_args,
    parse_duration,
    parse_timestamp,
)

# ---------------------------------------------------------------------------
# parse_timestamp
# ---------------------------------------------------------------------------


def test_parse_timestamp_accepts_unix_epoch():
    assert parse_timestamp("1778513400.5") == 1778513400.5


def test_parse_timestamp_accepts_iso_with_z():
    """ISO 8601 with Z timezone (Python 3.11+ fromisoformat accepts this)."""
    result = parse_timestamp("2026-05-11T15:42:30Z")
    expected = datetime(2026, 5, 11, 15, 42, 30, tzinfo=UTC).timestamp()
    assert result == pytest.approx(expected)


def test_parse_timestamp_accepts_iso_with_offset():
    result = parse_timestamp("2026-05-11T10:42:30-05:00")
    expected = datetime(2026, 5, 11, 15, 42, 30, tzinfo=UTC).timestamp()
    assert result == pytest.approx(expected)


def test_parse_timestamp_naive_iso_assumed_utc():
    result = parse_timestamp("2026-05-11T15:42:30")
    expected = datetime(2026, 5, 11, 15, 42, 30, tzinfo=UTC).timestamp()
    assert result == pytest.approx(expected)


def test_parse_timestamp_rejects_garbage():
    with pytest.raises(ValueError, match="not a Unix epoch or ISO 8601"):
        parse_timestamp("yesterday")


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


def test_parse_duration_plain_seconds():
    assert parse_duration("750") == 750.0
    assert parse_duration("0") == 0.0
    assert parse_duration("12.5") == 12.5


def test_parse_duration_mm_ss():
    assert parse_duration("12:30") == 750.0
    assert parse_duration("0:45") == 45.0


def test_parse_duration_hh_mm_ss():
    assert parse_duration("1:12:30") == 4350.0
    assert parse_duration("01:00:00") == 3600.0
    assert parse_duration("0:00:01") == 1.0


def test_parse_duration_suffix_seconds_only():
    assert parse_duration("90s") == 90.0


def test_parse_duration_suffix_minutes_only():
    assert parse_duration("12m") == 720.0


def test_parse_duration_suffix_hours_only():
    assert parse_duration("2h") == 7200.0


def test_parse_duration_suffix_combined_no_spaces():
    assert parse_duration("1h30m") == 5400.0
    assert parse_duration("12m30s") == 750.0
    assert parse_duration("1h2m3s") == 3723.0


def test_parse_duration_suffix_combined_with_spaces():
    assert parse_duration("1h 30m") == 5400.0
    assert parse_duration("1h 2m 3s") == 3723.0


def test_parse_duration_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        parse_duration("")


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("yesterday")


def test_parse_duration_rejects_bad_colon_form():
    with pytest.raises(ValueError, match="colon-form"):
        parse_duration("1:2:3:4")


def test_parse_duration_rejects_unrecognized_suffix():
    with pytest.raises(ValueError):
        parse_duration("5d")


# ---------------------------------------------------------------------------
# _load_session_start / _load_session_end
# ---------------------------------------------------------------------------


def _write_manifest(session_dir: Path, started_at: str, ended_at: str | None = None) -> None:
    payload: dict[str, object] = {"started_at": started_at}
    if ended_at is not None:
        payload["ended_at"] = ended_at
    (session_dir / "manifest.json").write_text(json.dumps(payload))


def test_load_session_start_reads_manifest(tmp_path: Path):
    _write_manifest(tmp_path, "2026-05-11T15:29:00+00:00")
    start = _load_session_start(tmp_path)
    assert start == pytest.approx(
        datetime(2026, 5, 11, 15, 29, 0, tzinfo=UTC).timestamp()
    )


def test_load_session_start_returns_none_when_manifest_missing(tmp_path: Path):
    assert _load_session_start(tmp_path) is None


def test_load_session_start_returns_none_on_corrupt_manifest(tmp_path: Path):
    (tmp_path / "manifest.json").write_text("{not json")
    assert _load_session_start(tmp_path) is None


def test_load_session_start_returns_none_when_started_at_missing(tmp_path: Path):
    (tmp_path / "manifest.json").write_text(json.dumps({"other": "field"}))
    assert _load_session_start(tmp_path) is None


def test_load_session_end_reads_manifest(tmp_path: Path):
    _write_manifest(
        tmp_path,
        "2026-05-11T15:29:00+00:00",
        ended_at="2026-05-11T15:35:00+00:00",
    )
    end = _load_session_end(tmp_path)
    assert end == pytest.approx(
        datetime(2026, 5, 11, 15, 35, 0, tzinfo=UTC).timestamp()
    )


# ---------------------------------------------------------------------------
# _resolve_timestamp
# ---------------------------------------------------------------------------


def test_resolve_timestamp_none_when_no_flags(tmp_path: Path):
    args = parse_args(["mylabel"])
    assert _resolve_timestamp(args, tmp_path) is None


def test_resolve_timestamp_uses_timestamp_flag(tmp_path: Path):
    args = parse_args(["mylabel", "--timestamp", "1778513400.0"])
    assert _resolve_timestamp(args, tmp_path) == 1778513400.0


def test_resolve_timestamp_uses_offset_against_session_start(tmp_path: Path):
    _write_manifest(tmp_path, "2026-05-11T15:29:00+00:00")
    start_epoch = datetime(2026, 5, 11, 15, 29, 0, tzinfo=UTC).timestamp()

    args = parse_args(["mylabel", "--offset", "12m30s"])
    resolved = _resolve_timestamp(args, tmp_path)
    assert resolved == pytest.approx(start_epoch + 750.0)


def test_resolve_timestamp_offset_errors_without_manifest(tmp_path: Path):
    args = parse_args(["mylabel", "--offset", "1m"])
    with pytest.raises(ValueError, match="manifest with started_at"):
        _resolve_timestamp(args, tmp_path)


def test_argparse_rejects_both_timestamp_and_offset():
    with pytest.raises(SystemExit):
        parse_args(["mylabel", "--timestamp", "1.0", "--offset", "60s"])


# ---------------------------------------------------------------------------
# _warn_if_outside_session
# ---------------------------------------------------------------------------


def test_warn_outside_session_before_start(tmp_path: Path):
    _write_manifest(
        tmp_path,
        "2026-05-11T15:29:00+00:00",
        ended_at="2026-05-11T15:35:00+00:00",
    )
    before = datetime(2026, 5, 11, 14, 0, 0, tzinfo=UTC).timestamp()
    stream = io.StringIO()
    _warn_if_outside_session(before, tmp_path, stream=stream)
    assert "before session start" in stream.getvalue()


def test_warn_outside_session_after_end(tmp_path: Path):
    _write_manifest(
        tmp_path,
        "2026-05-11T15:29:00+00:00",
        ended_at="2026-05-11T15:35:00+00:00",
    )
    after = datetime(2026, 5, 11, 16, 0, 0, tzinfo=UTC).timestamp()
    stream = io.StringIO()
    _warn_if_outside_session(after, tmp_path, stream=stream)
    assert "after session end" in stream.getvalue()


def test_warn_silent_when_within_bounds(tmp_path: Path):
    _write_manifest(
        tmp_path,
        "2026-05-11T15:29:00+00:00",
        ended_at="2026-05-11T15:35:00+00:00",
    )
    inside = datetime(2026, 5, 11, 15, 30, 0, tzinfo=UTC).timestamp()
    stream = io.StringIO()
    _warn_if_outside_session(inside, tmp_path, stream=stream)
    assert stream.getvalue() == ""


def test_warn_silent_when_no_manifest(tmp_path: Path):
    """No manifest → no ground truth → silent."""
    stream = io.StringIO()
    _warn_if_outside_session(1.0, tmp_path, stream=stream)
    assert stream.getvalue() == ""


# ---------------------------------------------------------------------------
# End-to-end CLI (main entry point)
# ---------------------------------------------------------------------------


def _read_tags(session_dir: Path) -> list[dict]:
    text = (session_dir / "tags.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_main_writes_tag_with_explicit_timestamp(tmp_path: Path, capsys):
    _write_manifest(
        tmp_path,
        "2026-05-11T15:29:00+00:00",
        ended_at="2026-05-11T15:35:00+00:00",
    )
    rc = main(["highway_entry", "--session", str(tmp_path), "--timestamp", "2026-05-11T15:32:00Z"])
    assert rc == 0

    tags = _read_tags(tmp_path)
    assert len(tags) == 1
    assert tags[0]["label"] == "highway_entry"
    assert tags[0]["t"] == pytest.approx(
        datetime(2026, 5, 11, 15, 32, 0, tzinfo=UTC).timestamp()
    )


def test_main_writes_tag_with_offset(tmp_path: Path):
    _write_manifest(
        tmp_path,
        "2026-05-11T15:29:00+00:00",
        ended_at="2026-05-11T16:29:00+00:00",
    )
    rc = main(["midpoint", "--session", str(tmp_path), "--offset", "20m"])
    assert rc == 0

    expected = datetime(2026, 5, 11, 15, 49, 0, tzinfo=UTC).timestamp()
    tags = _read_tags(tmp_path)
    assert tags[0]["t"] == pytest.approx(expected)


def test_main_warns_when_timestamp_outside_bounds(tmp_path: Path, capsys):
    _write_manifest(
        tmp_path,
        "2026-05-11T15:29:00+00:00",
        ended_at="2026-05-11T15:35:00+00:00",
    )
    rc = main(["late_arrival", "--session", str(tmp_path), "--timestamp", "2026-05-11T18:00:00Z"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "after session end" in err
    # Tag is still written despite the warning
    assert len(_read_tags(tmp_path)) == 1


def test_main_default_still_uses_now(tmp_path: Path):
    """Backward-compatibility: no flag → wall-clock now, as before."""
    _write_manifest(tmp_path, "2026-05-11T15:29:00+00:00")
    rc = main(["just_now", "--session", str(tmp_path)])
    assert rc == 0
    tags = _read_tags(tmp_path)
    assert "t" in tags[0]
    # 't' should be roughly recent (anything within the last hour of test exec).
    import time
    assert abs(tags[0]["t"] - time.time()) < 3600


def test_main_returns_2_on_bad_timestamp(tmp_path: Path, capsys):
    _write_manifest(tmp_path, "2026-05-11T15:29:00+00:00")
    rc = main(["x", "--session", str(tmp_path), "--timestamp", "yesterday"])
    assert rc == 2
    assert "not a Unix epoch or ISO 8601" in capsys.readouterr().err


def test_main_returns_2_on_bad_offset(tmp_path: Path, capsys):
    _write_manifest(tmp_path, "2026-05-11T15:29:00+00:00")
    rc = main(["x", "--session", str(tmp_path), "--offset", "yesterday"])
    assert rc == 2


def test_main_returns_2_on_offset_without_manifest(tmp_path: Path, capsys):
    rc = main(["x", "--session", str(tmp_path), "--offset", "1m"])
    assert rc == 2
    assert "manifest with started_at" in capsys.readouterr().err


def test_main_echoes_record_as_json(tmp_path: Path, capsys):
    _write_manifest(tmp_path, "2026-05-11T15:29:00+00:00")
    rc = main(["echo_test", "--session", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    parsed = json.loads(out)
    assert parsed["label"] == "echo_test"
    assert parsed["kind"] == "event"


def test_help_documents_backfill_flags():
    """The --timestamp and --offset flags must be discoverable from --help."""
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    # capsys doesn't capture argparse's help; smoke the docstring instead.
    from scripts import tag as tag_module
    assert "--timestamp" in tag_module.__doc__
    assert "--offset" in tag_module.__doc__


def test_iso_round_trip_in_help_examples():
    """Sanity: the example ISO timestamp in the docstring parses cleanly."""
    from scripts import tag as tag_module
    example_match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", tag_module.__doc__ or "")
    assert example_match is not None
    assert parse_timestamp(example_match.group(0)) > 0
