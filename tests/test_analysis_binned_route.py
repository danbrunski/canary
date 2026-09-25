"""Tests for `GET /analysis/binned` (Task 5.1)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.dashboard.api import create_app
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import isolated_config


def _client_and_storage(tmp_path: Path) -> tuple[TestClient, Path]:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True)
    config = isolated_config(
        tmp_path, storage=storage, models={"isolation_forest": {"enabled": False}}
    )
    app = create_app(config, anomalies_dir=tmp_path / "anomalies")
    return TestClient(app), storage


def _write_session(storage: Path, session_id: str) -> None:
    session_dir = storage / session_id
    records = []
    t0 = 1_780_000_000.0
    for i in range(60):
        t = t0 + i
        records.append(TelemetryRecord.obd(t, "RPM", 800.0 + i * 20, "rpm"))
        records.append(TelemetryRecord.obd(t, "ENGINE_LOAD", 20.0 + i * 0.5, "%"))
        records.append(TelemetryRecord.obd(t, "SHORT_FUEL_TRIM_1", float(i % 5) - 2.0, "%"))
    write_records_to_parquet(records, session_dir, max_rows=10_000)


def test_binned_analysis_2d_no_auth_required(tmp_path: Path) -> None:
    client, storage = _client_and_storage(tmp_path)
    _write_session(storage, "2026-05-17T120000")

    response = client.get(
        "/analysis/binned",
        params={
            "value": "obd:SHORT_FUEL_TRIM_1",
            "x": "obd:RPM",
            "y": "obd:ENGINE_LOAD",
            "x_bins": 3,
            "y_bins": 3,
            "agg": "mean",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["kind"] == "2d"
    assert body["rows"] == 3
    assert body["cols"] == 3
    assert len(body["cells"]) == 9
    assert body["sample_count"] > 0


def test_binned_analysis_1d_omits_y(tmp_path: Path) -> None:
    client, storage = _client_and_storage(tmp_path)
    _write_session(storage, "2026-05-17T120000")

    response = client.get(
        "/analysis/binned",
        params={"value": "obd:SHORT_FUEL_TRIM_1", "x": "obd:RPM", "x_bins": 4, "agg": "count"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "1d"
    assert len(body["cells"]) == 4
    assert "y_edges" not in body


def test_binned_analysis_rejects_bad_agg(tmp_path: Path) -> None:
    client, storage = _client_and_storage(tmp_path)
    _write_session(storage, "2026-05-17T120000")

    response = client.get(
        "/analysis/binned",
        params={"value": "obd:RPM", "x": "obd:RPM", "agg": "bogus"},
    )

    assert response.status_code == 400


def test_binned_analysis_unavailable_for_missing_channel(tmp_path: Path) -> None:
    client, storage = _client_and_storage(tmp_path)
    _write_session(storage, "2026-05-17T120000")

    response = client.get(
        "/analysis/binned",
        params={"value": "obd:DOES_NOT_EXIST", "x": "obd:RPM"},
    )

    assert response.status_code == 200
    assert response.json()["available"] is False
