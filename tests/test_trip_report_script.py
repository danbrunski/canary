"""Tests for ``scripts/trip_report.py`` (Task 1.7, engine-analysis-and-mapping.md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.trip_report as trip_report_cli
from src.analysis.trip_summary_sidecar import (  # noqa: E402
    get_or_compute_trip_summary,
    read_trip_summary,
)
from src.storage.parquet_writer import write_records_to_parquet  # noqa: E402
from src.telemetry import TelemetryRecord  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _log_messages(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """Parse the JSON-structured log lines ``configure_logging`` writes to
    stdout back into their ``message`` strings."""
    out = capsys.readouterr().out
    return [json.loads(line)["message"] for line in out.splitlines() if line.strip()]


def _write_driving_session(root: Path, name: str) -> Path:
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(10):
        t = 1_779_400_000.0 + i
        records.append(TelemetryRecord.obd(t, "SPEED", 60.0, "km/h"))
        records.append(TelemetryRecord.obd(t, "RPM", 2000.0, "rpm"))
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_trip_report_prints_the_summary_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _write_driving_session(tmp_path, "2026-07-19T120000")

    rc = trip_report_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--config",
            str(EXAMPLE_YAML),
            session.name,
        ]
    )
    assert rc == 0
    messages = _log_messages(capsys)
    joined = "\n".join(messages)
    assert "distance" in joined
    assert "avg speed" in joined
    assert "idle fraction" in joined

    # Sidecar persisted so a second run reads the cache instead of recomputing.
    sidecar = read_trip_summary(session)
    assert sidecar is not None
    assert sidecar["summary"]["distance_km"] > 0


def test_trip_report_errors_for_unknown_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = trip_report_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--config",
            str(EXAMPLE_YAML),
            "2099-01-01T000000",
        ]
    )
    assert rc == 1
    assert any("not found" in msg for msg in _log_messages(capsys))


def test_trip_report_rebuild_forces_recompute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = _write_driving_session(tmp_path, "2026-07-19T120000")
    trip_report_cli.main(
        ["--sessions-root", str(tmp_path), "--config", str(EXAMPLE_YAML), session.name]
    )

    calls: list[str] = []
    real = get_or_compute_trip_summary

    def _spy(session_dir: Path, config: object, *, force: bool = False) -> object | None:
        calls.append(session_dir.name)
        return real(session_dir, config, force=force)  # type: ignore[arg-type]

    monkeypatch.setattr(trip_report_cli, "get_or_compute_trip_summary", _spy)
    rc = trip_report_cli.main(
        [
            "--sessions-root",
            str(tmp_path),
            "--config",
            str(EXAMPLE_YAML),
            "--rebuild",
            session.name,
        ]
    )
    assert rc == 0
    assert calls == [session.name]
