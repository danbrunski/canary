"""``GET``/``POST /control/update`` (phone-ui P3.2, P3.4)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.collection import tuning_profile, tx_gate
from src.config import CanaryConfig
from src.control.updater import (
    PICKUP_TIMEOUT_S,
    UpdatePaths,
    new_request,
    parse_request,
    utc_iso,
    write_request,
    write_status,
)
from src.dashboard.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"
AUTH = {"Authorization": "Bearer secret"}
_ROUTE_HEADER = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
_DEFAULT_ROUTE = _ROUTE_HEADER + "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\n"


def _paths(tmp_path: Path, *, run_dir: Path | None = None) -> UpdatePaths:
    run = run_dir or tmp_path / "run"
    if run_dir is None:
        run.mkdir(exist_ok=True)
    net = tmp_path / "net"
    net.mkdir(exist_ok=True)
    (net / "route").write_text(_DEFAULT_ROUTE)
    trigger = tmp_path / "canary-update.path"
    trigger.write_text("[Path]\n")
    return UpdatePaths(
        request=run / "update_request",
        taken=run / "update_request.taken",
        status=run / "update_status",
        trigger_unit=trigger,
        proc_net=net,
    )


def _client(paths: UpdatePaths, tmp_path: Path) -> TestClient:
    app = create_app(
        CanaryConfig.from_yaml(EXAMPLE_YAML),
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        update_paths=paths,
    )
    return TestClient(app)


@pytest.fixture
def paths(tmp_path: Path) -> UpdatePaths:
    return _paths(tmp_path)


@pytest.fixture
def client(paths: UpdatePaths, tmp_path: Path) -> TestClient:
    return _client(paths, tmp_path)


def test_update_routes_require_the_token(client: TestClient, paths: UpdatePaths) -> None:
    assert client.get("/control/update").status_code == 401
    assert client.post("/control/update", json={"action": "check"}).status_code == 401
    assert not paths.request.exists()


def test_get_reports_installed_status_and_readiness(client: TestClient, paths: UpdatePaths) -> None:
    check = {"ok": True, "behind": 2, "current": "aaa1111", "upstream": "ccc3333"}
    write_status({"state": "done", "outcome": "checked", "last_check": check}, paths.status)

    body = client.get("/control/update", headers=AUTH).json()["update"]

    assert set(body["installed"]) == {"version", "sha"}
    assert body["status"]["state"] == "done"
    assert "last_check" not in body["status"]
    assert body["last_check"] == check
    assert body["pending_request"] is None
    assert body["trigger_installed"] is True
    assert body["internet"] == {"default_route": True}
    assert body["blocked_by"] == []


def test_post_files_a_request_the_runner_accepts(client: TestClient, paths: UpdatePaths) -> None:
    response = client.post("/control/update", json={"action": "update", "full": True}, headers=AUTH)

    assert response.status_code == 202
    filed = parse_request(paths.request.read_text())
    assert (filed.action, filed.full) == ("update", True)
    assert response.json()["request"]["request_id"] == filed.request_id
    assert response.json()["update"]["pending_request"]["action"] == "update"


@pytest.mark.parametrize(
    "body",
    [{"action": "reflash"}, {"action": "update", "force": True}, {"action": "check", "full": True}],
)
def test_post_validates_the_body(client: TestClient, paths: UpdatePaths, body: dict) -> None:
    assert client.post("/control/update", json=body, headers=AUTH).status_code == 422
    assert not paths.request.exists()


def test_update_is_refused_while_a_capture_records(
    client: TestClient, paths: UpdatePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tuning_profile,
        "snapshot",
        lambda: {"active": True, "name": "fuel-map", "session_dir": "_capture-fuel-map-1"},
    )

    response = client.post("/control/update", json={"action": "update"}, headers=AUTH)

    assert response.status_code == 409
    assert "capture 'fuel-map' is recording" in response.json()["detail"]
    assert not paths.request.exists()
    # A check changes nothing on the Pi, so it is allowed during a capture.
    assert client.post("/control/update", json={"action": "check"}, headers=AUTH).status_code == 202


def test_update_is_refused_while_an_ecu_backup_runs(client: TestClient, paths: UpdatePaths) -> None:
    tx_gate.claim_ecm_session("rom_backup")
    try:
        response = client.post("/control/update", json={"action": "update"}, headers=AUTH)
        blocked = client.get("/control/update", headers=AUTH).json()["update"]["blocked_by"]
    finally:
        tx_gate.release_ecm_session("rom_backup")

    assert response.status_code == 409
    assert "ECU backup" in response.json()["detail"]
    assert len(blocked) == 1
    assert not paths.request.exists()


def test_a_running_update_refuses_a_second_request(client: TestClient, paths: UpdatePaths) -> None:
    write_status(
        {
            "state": "updating",
            "action": "update",
            "phase": "Restarting services",
            "updated_at": utc_iso(time.time()),
        },
        paths.status,
    )

    response = client.post("/control/update", json={"action": "check"}, headers=AUTH)

    assert response.status_code == 409
    assert "already running (Restarting services)" in response.json()["detail"]


def test_a_lost_runner_does_not_block_forever(client: TestClient, paths: UpdatePaths) -> None:
    write_status({"state": "updating", "updated_at": "2020-01-01T00:00:00+00:00"}, paths.status)

    assert client.get("/control/update", headers=AUTH).json()["update"]["status"]["state"] == "lost"
    assert client.post("/control/update", json={"action": "check"}, headers=AUTH).status_code == 202


def test_a_waiting_request_refuses_until_it_is_stuck(
    client: TestClient, paths: UpdatePaths
) -> None:
    write_request(new_request("check"), paths.request)
    response = client.post("/control/update", json={"action": "update"}, headers=AUTH)
    assert response.status_code == 409
    assert "already waiting" in response.json()["detail"]

    write_request(new_request("check", now=time.time() - PICKUP_TIMEOUT_S - 5), paths.request)
    pending = client.get("/control/update", headers=AUTH).json()["update"]["pending_request"]
    assert pending["stuck"] is True
    assert client.post("/control/update", json={"action": "check"}, headers=AUTH).status_code == 202


def test_no_default_route_says_no_internet_before_filing(
    client: TestClient, paths: UpdatePaths
) -> None:
    (paths.proc_net / "route").write_text(_ROUTE_HEADER)

    response = client.post("/control/update", json={"action": "check"}, headers=AUTH)

    assert response.status_code == 409
    assert response.json()["detail"].startswith("no internet: the Pi has no default route")
    assert not paths.request.exists()


def test_a_missing_trigger_unit_is_503(client: TestClient, paths: UpdatePaths) -> None:
    paths.trigger_unit.unlink()

    response = client.post("/control/update", json={"action": "check"}, headers=AUTH)

    assert response.status_code == 503
    assert "canary-update.path is not installed" in response.json()["detail"]
    assert (
        client.get("/control/update", headers=AUTH).json()["update"]["trigger_installed"] is False
    )


def test_an_unwritable_run_dir_is_503(tmp_path: Path) -> None:
    paths = _paths(tmp_path, run_dir=tmp_path / "no-run-canary")
    client = _client(paths, tmp_path)

    response = client.post("/control/update", json={"action": "check"}, headers=AUTH)

    assert response.status_code == 503
    assert "not writable" in response.json()["detail"]
