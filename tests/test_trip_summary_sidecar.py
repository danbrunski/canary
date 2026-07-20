"""Tests for the trip-summary sidecar (Task 1.4, engine-analysis-and-mapping.md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis.trip_summary_sidecar import (
    TRIP_SUMMARY_FILENAME,
    compute_trip_summary,
    get_or_compute_trip_summary,
    read_trip_summary,
    write_trip_summary,
)
from src.config import CanaryConfig
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _config() -> CanaryConfig:
    return CanaryConfig.from_yaml(EXAMPLE_YAML)


def _write_driving_session(root: Path, name: str) -> Path:
    """A short constant-speed drive: enough for distance/fuel/economy to be
    non-``None`` without needing a full accel-event trace."""
    session = root / name
    records: list[TelemetryRecord] = []
    for i in range(20):
        t = 1_779_400_000.0 + i
        records.extend(
            [
                TelemetryRecord.obd(t, "SPEED", 60.0, "km/h"),
                TelemetryRecord.obd(t, "RPM", 2000.0, "rpm"),
                TelemetryRecord.obd(t, "MAF", 12.0, "g/s"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_compute_trip_summary_raises_without_telemetry(tmp_path: Path) -> None:
    session = tmp_path / "2026-07-19T120000"
    session.mkdir()
    with pytest.raises(FileNotFoundError):
        compute_trip_summary(session, _config())


def test_compute_trip_summary_reads_real_channels(tmp_path: Path) -> None:
    session = _write_driving_session(tmp_path, "2026-07-19T120000")
    summary = compute_trip_summary(session, _config())
    assert summary.duration_s > 0
    assert summary.distance_km is not None and summary.distance_km > 0
    assert summary.fuel_used_l is not None and summary.fuel_used_l > 0
    assert summary.avg_speed_kph is not None


def test_write_trip_summary_is_atomic_tmp_rename(tmp_path: Path) -> None:
    session = _write_driving_session(tmp_path, "2026-07-19T120000")
    summary = compute_trip_summary(session, _config())
    path = write_trip_summary(session, summary)
    assert path == session / TRIP_SUMMARY_FILENAME
    # No leftover .tmp file after the rename.
    assert not (session / f"{TRIP_SUMMARY_FILENAME}.tmp").exists()

    sidecar = json.loads(path.read_text())
    assert sidecar["session_id"] == session.name
    assert sidecar["summary"]["distance_km"] == pytest.approx(summary.distance_km)
    assert sidecar["summary"]["avg_speed_kph"] == pytest.approx(summary.avg_speed_kph)

    loaded = read_trip_summary(session)
    assert loaded == sidecar


def test_get_or_compute_trip_summary_computes_once_then_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _write_driving_session(tmp_path, "2026-07-19T120000")
    config = _config()

    import src.analysis.trip_summary_sidecar as sidecar_mod

    calls: list[str] = []
    real_compute = sidecar_mod.compute_trip_summary

    def _spy(session_dir: Path, cfg: CanaryConfig) -> object:
        calls.append(session_dir.name)
        return real_compute(session_dir, cfg)

    monkeypatch.setattr(sidecar_mod, "compute_trip_summary", _spy)

    first = get_or_compute_trip_summary(session, config)
    assert first is not None
    assert calls == [session.name]

    second = get_or_compute_trip_summary(session, config)
    assert second == first
    # Cached on disk -> compute not called a second time.
    assert calls == [session.name]

    third = get_or_compute_trip_summary(session, config, force=True)
    assert third is not None
    assert calls == [session.name, session.name]


def test_get_or_compute_trip_summary_returns_none_for_missing_session(tmp_path: Path) -> None:
    assert get_or_compute_trip_summary(tmp_path / "nope", _config()) is None


def test_read_trip_summary_returns_none_when_absent(tmp_path: Path) -> None:
    session = tmp_path / "2026-07-19T120000"
    session.mkdir()
    assert read_trip_summary(session) is None
