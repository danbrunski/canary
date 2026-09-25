"""Tests for the ``scripts/autotag.py`` CLI (Phase 2a.5)."""

from __future__ import annotations

import json
from pathlib import Path

import scripts.autotag as autotag_cli
from src.storage.parquet_writer import write_records_to_parquet  # noqa: E402
from src.telemetry import TelemetryRecord  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _write_gps_session(root: Path, *, name: str = "2026-05-20T120000") -> Path:
    """A session with a real ``stopped`` window (gps:speed ~0 throughout) —
    the GPS-only tag path this task requires to work today."""
    session = root / name
    records: list[TelemetryRecord] = []
    t0 = 1_779_300_000.0
    for i in range(30):
        t = t0 + i
        records.append(TelemetryRecord.gps(t, "speed", 0.3, "km/h"))
        records.append(TelemetryRecord.obd(t, "RPM", 800.0, "rpm"))
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_main_writes_stopped_tag_for_a_gps_session(tmp_path: Path) -> None:
    session = _write_gps_session(tmp_path)
    rc = autotag_cli.main(
        ["--sessions-root", str(tmp_path), "--session", session.name, "--config", str(EXAMPLE_YAML)]
    )
    assert rc == 0
    tags_path = session / "tags.jsonl"
    assert tags_path.exists()
    records = [json.loads(line) for line in tags_path.read_text().splitlines()]
    assert any(r["label"] == "stopped" and r["source"] == "autotag" for r in records)


def test_main_is_idempotent_on_rerun(tmp_path: Path) -> None:
    session = _write_gps_session(tmp_path)
    args = [
        "--sessions-root",
        str(tmp_path),
        "--session",
        session.name,
        "--config",
        str(EXAMPLE_YAML),
    ]
    assert autotag_cli.main(args) == 0
    first_count = len((session / "tags.jsonl").read_text().splitlines())
    assert autotag_cli.main(args) == 0
    second_count = len((session / "tags.jsonl").read_text().splitlines())
    assert second_count == first_count


def test_dry_run_does_not_write_tags(tmp_path: Path) -> None:
    session = _write_gps_session(tmp_path)
    rc = autotag_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--session",
            session.name,
            "--config",
            str(EXAMPLE_YAML),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert not (session / "tags.jsonl").exists()


def test_missing_session_returns_error(tmp_path: Path) -> None:
    rc = autotag_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--session",
            "nonexistent",
            "--config",
            str(EXAMPLE_YAML),
        ]
    )
    assert rc == 1
