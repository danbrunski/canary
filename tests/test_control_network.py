"""The /control/network/provisioning routes (BLE provisioning window, N1.2)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.dashboard.api import create_app
from src.provisioning.gate import read_window_remaining

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"
AUTH = {"Authorization": "Bearer secret"}


def _api(tmp_path: Path, window: Path | None = None) -> tuple[TestClient, Path]:
    window = window or tmp_path / "provision_window"
    app = create_app(
        CanaryConfig.from_yaml(EXAMPLE_YAML),
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        provision_window_path=window,
    )
    return TestClient(app), window


def test_window_routes_require_the_token(tmp_path: Path) -> None:
    client, window = _api(tmp_path)
    assert client.get("/control/network/provisioning").status_code == 401
    response = client.post("/control/network/provisioning/window", json={"minutes": 5})
    assert response.status_code == 401
    assert not window.exists()


def test_open_read_and_close_the_window(tmp_path: Path) -> None:
    client, window = _api(tmp_path)
    assert client.get("/control/network/provisioning", headers=AUTH).json() == {
        "provisioning": {"window_remaining_s": 0.0, "max_minutes": 30.0}
    }
    opened = client.post("/control/network/provisioning/window", json={"minutes": 5}, headers=AUTH)
    assert opened.status_code == 200
    assert 290 < opened.json()["provisioning"]["window_remaining_s"] <= 300
    assert 290 < read_window_remaining(window) <= 300
    closed = client.post("/control/network/provisioning/window", json={"minutes": 0}, headers=AUTH)
    assert closed.json()["expiry"] == 0.0
    assert read_window_remaining(window) == 0.0


def test_window_length_is_validated(tmp_path: Path) -> None:
    client, window = _api(tmp_path)
    for body in ({"minutes": 31}, {"minutes": -1}, {"minutes": 5, "extra": 1}):
        response = client.post("/control/network/provisioning/window", json=body, headers=AUTH)
        assert response.status_code == 422
    assert not window.exists()


def test_unwritable_window_file_is_503(tmp_path: Path) -> None:
    client, _ = _api(tmp_path, tmp_path / "missing-dir" / "provision_window")
    response = client.post(
        "/control/network/provisioning/window", json={"minutes": 5}, headers=AUTH
    )
    assert response.status_code == 503
    assert "not writable" in response.json()["detail"]
