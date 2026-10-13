"""/control/network: the network read (P2.3) and the BLE provisioning window (N1.2)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.dashboard.api import create_app
from src.provisioning.gate import read_window_remaining, write_window
from src.provisioning.network import RunResult
from src.provisioning.reader import NetworkStatusReader
from tests.test_provisioning_reader import HEALTHY, FakeRunner

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"
AUTH = {"Authorization": "Bearer secret"}


def _api(
    tmp_path: Path,
    window: Path | None = None,
    reader: NetworkStatusReader | None = None,
) -> tuple[TestClient, Path]:
    window = window or tmp_path / "provision_window"
    app = create_app(
        CanaryConfig.from_yaml(EXAMPLE_YAML),
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        provision_window_path=window,
        network_reader=reader,
    )
    return TestClient(app), window


def test_window_routes_require_the_token(tmp_path: Path) -> None:
    client, window = _api(tmp_path)
    assert client.get("/control/network").status_code == 401
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


def test_network_read_reports_the_pi_and_the_window(tmp_path: Path) -> None:
    runner = FakeRunner(HEALTHY)
    client, window = _api(tmp_path, reader=NetworkStatusReader(runner))
    write_window(5, window)
    body = client.get("/control/network", headers=AUTH).json()
    network = body["network"]
    assert (network["mode"], network["ssid"]) == ("client", "Starlink")
    assert network["addresses"]["wlan0"][0] == "192.168.1.23"
    # The configured dashboard port, by IP.
    port = CanaryConfig.from_yaml(EXAMPLE_YAML).dashboard.port
    assert network["dashboard_url"] == f"http://192.168.1.23:{port}/"
    assert network["error"] is None
    assert 290 < body["provisioning"]["window_remaining_s"] <= 300


def test_network_read_is_cached_across_polls(tmp_path: Path) -> None:
    runner = FakeRunner(HEALTHY)
    client, _ = _api(tmp_path, reader=NetworkStatusReader(runner, ttl_s=60))
    for _ in range(10):
        assert client.get("/control/network", headers=AUTH).status_code == 200
    assert len(runner.calls) == 4


def test_missing_networkmanager_is_an_error_field_not_a_500(tmp_path: Path) -> None:
    missing = RunResult(127, "", "not found")
    runner = FakeRunner({("nmcli",): missing, ("ip",): missing})
    client, _ = _api(tmp_path, reader=NetworkStatusReader(runner))
    response = client.get("/control/network", headers=AUTH)
    assert response.status_code == 200
    network = response.json()["network"]
    assert network["mode"] == "unknown"
    assert "nmcli is not installed" in network["error"]


def test_unwired_reader_says_so(tmp_path: Path) -> None:
    client, _ = _api(tmp_path)
    network = client.get("/control/network", headers=AUTH).json()["network"]
    assert network["error"] == "network status is not wired into this app"
    assert network["addresses"] == {}


def test_status_carries_the_network_without_waiting_on_nmcli(tmp_path: Path) -> None:
    runner = FakeRunner(HEALTHY)
    spawned: list[Callable[[], None]] = []
    client, _ = _api(tmp_path, reader=NetworkStatusReader(runner, spawn=spawned.append))
    # First poll: nothing read yet, and the refresh is handed to a thread.
    assert client.get("/status").json()["network"] is None
    assert runner.calls == []
    spawned.pop()()
    network = client.get("/status").json()["network"]
    assert network["ssid"] == "Starlink"
    assert network["dashboard_url"].startswith("http://192.168.1.23:")
    # The read is shared: /control/network inside its TTL forks nothing more.
    client.get("/control/network", headers=AUTH)
    assert len(runner.calls) == 4


def test_status_network_is_null_when_unwired(tmp_path: Path) -> None:
    client, _ = _api(tmp_path)
    assert client.get("/status").json()["network"] is None
