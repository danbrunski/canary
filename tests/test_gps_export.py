"""Tests for GPX/KML session GPS track export (Task 5.6a)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.analysis.gps_export import read_gps_track, to_gpx, to_kml
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord


def _write_session(root: Path, *, with_altitude: bool = True) -> Path:
    session = root / "2026-07-19T000000"
    records = []
    t0 = 1_780_000_000.0
    for i in range(5):
        t = t0 + i
        records.append(TelemetryRecord.obd(t, "RPM", 1000.0, "rpm"))  # non-GPS noise
        records.append(TelemetryRecord.gps(t, "latitude", 34.0 + i * 0.001, "deg"))
        records.append(TelemetryRecord.gps(t, "longitude", -118.0 - i * 0.001, "deg"))
        if with_altitude:
            records.append(TelemetryRecord.gps(t, "altitude", 100.0 + i, "m"))
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def test_read_gps_track_returns_ordered_fixes(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    track = read_gps_track(session)
    assert len(track) == 5
    assert track[0].latitude == 34.0
    assert track[-1].latitude == 34.004
    assert track[0].altitude == 100.0
    assert track == sorted(track, key=lambda p: p.timestamp)


def test_read_gps_track_reads_its_columns_and_finds_the_same_fixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P02.2: three value columns, not every column; the same fixes."""
    from src.analysis import gps_export

    session = _write_session(tmp_path)
    narrowed = read_gps_track(session)
    real = gps_export.read_session
    monkeypatch.setattr(
        gps_export, "read_session", lambda path, **kw: real(path, filters=kw["filters"])
    )
    assert len(narrowed) == 5 and narrowed == read_gps_track(session)


def test_read_gps_track_without_altitude_leaves_it_none(tmp_path: Path) -> None:
    session = _write_session(tmp_path, with_altitude=False)
    track = read_gps_track(session)
    assert len(track) == 5
    assert all(p.altitude is None for p in track)


def test_read_gps_track_empty_session_returns_empty_list(tmp_path: Path) -> None:
    session = tmp_path / "2026-07-19T000000"
    records = [TelemetryRecord.obd(1_780_000_000.0, "RPM", 800.0, "rpm")]
    write_records_to_parquet(records, session, max_rows=10_000)
    assert read_gps_track(session) == []


def test_to_gpx_produces_one_trkpt_per_point(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    track = read_gps_track(session)
    gpx = to_gpx(track, name="test session")
    assert gpx.count("<trkpt") == 5
    assert "test session" in gpx
    assert "34.0000000" in gpx


def test_to_kml_produces_one_linestring_with_all_coordinates(tmp_path: Path) -> None:
    session = _write_session(tmp_path)
    track = read_gps_track(session)
    kml = to_kml(track, name="test session")
    assert kml.count("<coordinates>") == 1
    # lon,lat,alt order (KML convention) — first point.
    assert "-118.0000000,34.0000000,100.00" in kml


def test_to_gpx_and_kml_handle_empty_track(tmp_path: Path) -> None:
    assert "<trkseg>" in to_gpx([], name="empty")
    assert "<coordinates></coordinates>" in to_kml([], name="empty")
