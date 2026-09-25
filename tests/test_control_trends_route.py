"""Tests for the `/control/trends` HTTP route (Task 4.5)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.analysis.fingerprint import write_fingerprint
from src.analysis.reference_epoch import ReferenceEpoch, StatSummary, write_reference_epoch
from src.dashboard.api import create_app
from tests.configs import isolated_config


def _client(tmp_path: Path) -> TestClient:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True)
    config = isolated_config(
        tmp_path, storage=storage, daemon={"reference_epoch": tmp_path / "reference_epoch.json"}
    )
    anomalies = tmp_path / "anomalies"
    anomalies.mkdir()
    app = create_app(config, anomalies_dir=anomalies, control_token="secret")
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_get_trends_requires_auth(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/control/trends")
    assert resp.status_code == 401


def test_get_trends_unavailable_without_reference_epoch(tmp_path: Path) -> None:
    client = _client(tmp_path)
    resp = client.get("/control/trends", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["stats"] == {}


def test_get_trends_reports_stat_state(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True)
    reference_epoch_path = tmp_path / "reference_epoch.json"
    epoch = ReferenceEpoch(
        frozen_at="2026-06-01T00:00:00+00:00",
        sessions=("s0",),
        stats={
            "idle_rpm_mean": StatSummary(
                mean=800.0, std=10.0, quantiles={"p10": 790.0, "p90": 810.0}, n=5
            )
        },
    )
    write_reference_epoch(reference_epoch_path, epoch)
    session_dir = storage / "2026-06-01T000000"
    session_dir.mkdir(parents=True)
    write_fingerprint(session_dir, {"idle_rpm_mean": 800.0})

    config = isolated_config(
        tmp_path, storage=storage, daemon={"reference_epoch": reference_epoch_path}
    )
    anomalies = tmp_path / "anomalies"
    anomalies.mkdir()
    app = create_app(config, anomalies_dir=anomalies, control_token="secret")
    client = TestClient(app)

    resp = client.get("/control/trends", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert body["stats"]["idle_rpm_mean"]["latest_session"] == "2026-06-01T000000"
