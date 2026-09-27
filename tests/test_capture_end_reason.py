"""W03 durable capture termination and restart recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from src.storage.capture_manifest import (
    latest_capture_summary,
    read_capture_manifest,
    recover_interrupted_capture,
    stamp_capture_end,
)


def write_manifest(session: Path, profile: str = "tuning") -> None:
    session.mkdir(parents=True)
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": profile,
                "started_at": "2026-09-21T18:00:00+00:00",
                "profile_snapshot": {},
            }
        )
    )


def test_interrupted_capture_is_stamped_exactly_once_across_startups(tmp_path: Path) -> None:
    older = tmp_path / "_capture-z-2026-09-20T220000"
    newest = tmp_path / "_capture-a-2026-09-21T180000"
    write_manifest(older, "older")
    write_manifest(newest, "newest")
    stamp_capture_end(older, "operator_stop")
    detected = datetime(2026, 9, 21, 19, tzinfo=UTC)

    first = recover_interrupted_capture(tmp_path, detected_at=detected)
    second = recover_interrupted_capture(tmp_path, detected_at=detected)

    assert first is not None and first[0] == newest
    assert second is None
    manifest = read_capture_manifest(newest)
    assert manifest is not None
    assert manifest.end_reason == "interrupted"
    assert manifest.ended_at == detected
    assert manifest.detected_at == detected


def test_existing_end_record_cannot_be_rewritten(tmp_path: Path) -> None:
    session = tmp_path / "_capture-tuning-2026-09-21T180000"
    write_manifest(session)
    ended = datetime(2026, 9, 21, 18, 5, tzinfo=UTC)
    stamp_capture_end(session, "operator_stop", ended_at=ended)

    result = stamp_capture_end(session, "daemon_shutdown")

    assert result.end_reason == "operator_stop"
    assert result.ended_at == ended


def test_latest_capture_uses_parsed_timestamp_not_prefix(tmp_path: Path) -> None:
    lexically_later = tmp_path / "_capture-zeta-2026-09-20T220000"
    chronologically_later = tmp_path / "_capture-alpha-2026-09-21T180000"
    write_manifest(lexically_later, "old")
    write_manifest(chronologically_later, "new")
    stamp_capture_end(chronologically_later, "daemon_shutdown")

    summary = latest_capture_summary(tmp_path)

    assert summary is not None
    assert summary["name"] == "new"
    assert summary["end_reason"] == "daemon_shutdown"
    assert summary["session_dir"] == str(chronologically_later)
