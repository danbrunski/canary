"""/pair/request and /control/devices over HTTP, with a scripted key."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.config import CanaryConfig
from src.control.key_cycle_pairing import KeyCyclePairing
from src.control.paired_devices import PairedDevices
from src.dashboard.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"
ACC, ON = "engine_off", "ignition_only"


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.state = ACC
        self.now = 1000.0
        devices = PairedDevices(tmp_path / "paired_devices.json")
        self.pairing = KeyCyclePairing(
            devices,
            lambda: self.state,
            window_path=tmp_path / "provision_window",
            clock=lambda: self.now,
        )
        app = create_app(
            CanaryConfig.from_yaml(EXAMPLE_YAML),
            anomalies_dir=tmp_path / "anomalies",
            control_token="shared-secret",
            paired_devices=devices,
            pairing=self.pairing,
            ups_status_path=tmp_path / "ups_status",
            ups_keepalive_path=tmp_path / "ups_keepalive",
        )
        self.client = TestClient(app)

    def key(self, *states: str) -> None:
        for state in states:
            self.state = state
            for _ in range(2):
                self.now += 1
                self.pairing.tick()

    def pair(self, name: str = "Android phone") -> dict:
        created = self.client.post("/pair/request", json={"name": name})
        assert created.status_code == 201
        req = created.json()
        self.key(ACC, ON, ACC)
        return self.poll(req)

    def poll(self, req: dict) -> dict:
        response = self.client.get(
            f"/pair/request/{req['id']}", headers={"X-Pairing-Secret": req["secret"]}
        )
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_key_cycle_gives_a_working_token(rig: Rig) -> None:
    done = rig.pair()
    assert done["status"] == "approved"
    token = done["token"]
    # Any guarded route accepts it -- the shared dependency, not a per-route opt-in.
    assert rig.client.get("/control/power", headers=_bearer(token)).status_code == 200
    listed = rig.client.get("/control/devices", headers=_bearer(token)).json()["devices"]
    assert [(d["name"], d["method"]) for d in listed] == [("Android phone", "key-cycle")]
    assert "token_sha256" not in listed[0]


def test_progress_is_reported_step_by_step(rig: Rig) -> None:
    req = rig.client.post("/pair/request", json={"name": "Pixel"}).json()
    rig.key(ACC)
    assert rig.poll(req)["step"] == "turn_on"
    rig.key(ON)
    assert rig.poll(req)["step"] == "back_to_acc"
    assert rig.poll(req)["vehicle_state"] == ON


def test_device_token_opens_the_control_websocket(rig: Rig) -> None:
    token = rig.pair()["token"]
    with rig.client.websocket_connect(f"/ws/control?token={token}"):
        pass
    with (
        pytest.raises(WebSocketDisconnect),
        rig.client.websocket_connect("/ws/control?token=cd_bad"),
    ):
        pass


def test_request_routes_need_the_request_secret(rig: Rig) -> None:
    req = rig.client.post("/pair/request", json={}).json()
    wrong = {"X-Pairing-Secret": "guess"}
    assert rig.client.get(f"/pair/request/{req['id']}", headers=wrong).status_code == 404
    assert rig.client.delete(f"/pair/request/{req['id']}", headers=wrong).status_code == 404
    assert rig.client.get(f"/pair/request/{req['id']}").status_code == 422
    ok = {"X-Pairing-Secret": req["secret"]}
    assert rig.client.delete(f"/pair/request/{req['id']}", headers=ok).status_code == 200


def test_too_many_requests_is_429(rig: Rig) -> None:
    codes = [rig.client.post("/pair/request", json={}).status_code for _ in range(5)]
    assert codes == [201, 201, 201, 201, 429]


def test_removed_device_loses_access(rig: Rig) -> None:
    done = rig.pair()
    token, device_id = done["token"], done["device"]["id"]
    removed = rig.client.delete(f"/control/devices/{device_id}", headers=_bearer("shared-secret"))
    assert removed.status_code == 200
    assert rig.client.get("/control/power", headers=_bearer(token)).status_code == 401
    missing = rig.client.delete(f"/control/devices/{device_id}", headers=_bearer("shared-secret"))
    assert missing.status_code == 404


def test_device_list_needs_a_token(rig: Rig) -> None:
    assert rig.client.get("/control/devices").status_code == 401
    assert rig.client.get("/control/devices", headers=_bearer("shared-secret")).status_code == 200


def test_non_ascii_bearer_is_refused_not_a_crash(rig: Rig) -> None:
    # Sent as raw latin-1 bytes, the way a misbehaving client can.
    response = rig.client.get("/control/power", headers={"Authorization": b"Bearer caf\xe9"})
    assert response.status_code == 401
