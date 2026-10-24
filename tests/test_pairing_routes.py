"""/pair/request and /control/devices over HTTP, with a scripted key."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from src.config import CanaryConfig
from src.control.key_cycle_pairing import KeyCyclePairing
from src.control.paired_devices import PairedDevices
from src.dashboard.api import create_app
from src.dashboard.security import control_caller

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

    def pair(self, name: str = "Android phone") -> dict[str, Any]:
        created = self.client.post("/pair/request", json={"name": name})
        assert created.status_code == 201
        req = created.json()
        self.key(ACC, ON, ACC)
        return self.poll(req)

    def poll(self, req: dict[str, Any]) -> dict[str, Any]:
        response = self.client.get(
            f"/pair/request/{req['id']}", headers={"X-Pairing-Secret": req["secret"]}
        )
        assert response.status_code == 200, response.text
        return cast(dict[str, Any], response.json())


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


def test_removing_a_device_closes_its_open_control_socket(rig: Rig) -> None:
    done = rig.pair()
    with rig.client.websocket_connect(f"/ws/control?token={done['token']}") as ws:
        removed = rig.client.delete(
            f"/control/devices/{done['device']['id']}", headers=_bearer("shared-secret")
        )
        assert removed.status_code == 200
        # The removal's own event is the next message: it is not delivered.
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1008


def test_a_shared_token_socket_keeps_receiving(rig: Rig) -> None:
    done = rig.pair()
    with rig.client.websocket_connect("/ws/control?token=shared-secret") as ws:
        rig.client.delete(
            f"/control/devices/{done['device']['id']}", headers=_bearer("shared-secret")
        )
        assert ws.receive_json()["action"] == "pairing.removed"


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


SHARED = _bearer("shared-secret")


def test_pairing_routes_need_a_token(rig: Rig) -> None:
    req = rig.client.post("/pair/request", json={"name": "Laptop"}).json()
    assert rig.client.get("/control/pairing/requests").status_code == 401
    assert rig.client.post(f"/control/pairing/requests/{req['id']}/approve").status_code == 401
    assert rig.client.get("/control/devices/me").status_code == 401
    assert rig.client.patch("/control/devices/x", json={"name": "y"}).status_code == 401
    # A pending request's own secret is not a control token.
    assert (
        rig.client.get("/control/pairing/requests", headers=_bearer(req["secret"])).status_code
        == 401
    )
    assert rig.poll(req)["status"] == "waiting"


def test_a_paired_device_approves_the_request_it_picked(rig: Rig) -> None:
    phone = rig.pair()
    first = rig.client.post("/pair/request", json={"name": "Laptop"}).json()
    second = rig.client.post("/pair/request", json={"name": "Tablet"}).json()
    listed = rig.client.get("/control/pairing/requests", headers=_bearer(phone["token"]))
    assert listed.status_code == 200
    requests = listed.json()["requests"]
    assert [r["name"] for r in requests] == ["Laptop", "Tablet"]
    assert first["secret"] not in listed.text and second["secret"] not in listed.text

    approved = rig.client.post(
        f"/control/pairing/requests/{second['id']}/approve", headers=_bearer(phone["token"])
    )
    assert approved.status_code == 200
    assert approved.json() == {"approved": second["id"], "by": phone["device"]["id"]}
    done = rig.poll(second)
    assert done["device"]["method"] == f"approved-by:{phone['device']['id']}"
    assert rig.client.get("/control/power", headers=_bearer(done["token"])).status_code == 200
    assert rig.poll(first)["status"] == "waiting"


def test_shared_token_approval_is_recorded_as_such(rig: Rig) -> None:
    req = rig.client.post("/pair/request", json={"name": "Laptop"}).json()
    approve = f"/control/pairing/requests/{req['id']}/approve"
    assert rig.client.post(approve, headers=SHARED).json()["by"] == "shared-token"
    # Approved but not yet collected: no longer waiting.
    assert rig.client.post(approve, headers=SHARED).status_code == 409
    assert rig.poll(req)["device"]["method"] == "approved-by:shared-token"
    # Collected: the request is gone.
    assert rig.client.post(approve, headers=SHARED).status_code == 404
    unknown = "/control/pairing/requests/nope/approve"
    assert rig.client.post(unknown, headers=SHARED).status_code == 404


def test_devices_me_names_the_caller(rig: Rig) -> None:
    done = rig.pair()
    mine = rig.client.get("/control/devices/me", headers=_bearer(done["token"])).json()
    assert mine["kind"] == "device" and mine["shared_token"] is False
    assert mine["device"]["id"] == done["device"]["id"]
    assert "token_sha256" not in mine["device"]
    shared = rig.client.get("/control/devices/me", headers=SHARED).json()
    assert shared == {"kind": "shared-token", "shared_token": True, "device": None}


def test_rename_is_durable(rig: Rig, tmp_path: Path) -> None:
    done = rig.pair()
    device_id = done["device"]["id"]
    renamed = rig.client.patch(
        f"/control/devices/{device_id}", json={"name": "Dan's Pixel"}, headers=SHARED
    )
    assert renamed.status_code == 200 and renamed.json()["device"]["name"] == "Dan's Pixel"
    reloaded = PairedDevices(tmp_path / "paired_devices.json")
    assert [d.name for d in reloaded.list()] == ["Dan's Pixel"]
    missing = rig.client.patch("/control/devices/nope", json={"name": "x"}, headers=SHARED)
    assert missing.status_code == 404
    empty = rig.client.patch(f"/control/devices/{device_id}", json={"name": ""}, headers=SHARED)
    assert empty.status_code == 422


def test_a_pairing_request_reaches_control_subscribers(tmp_path: Path) -> None:
    # The daemon's own coordinator, not the Rig's injected one: this pins the
    # create_app wiring that hands it publish_control_event.
    app = create_app(
        CanaryConfig.from_yaml(EXAMPLE_YAML),
        anomalies_dir=tmp_path / "anomalies",
        control_token="shared-secret",
        paired_devices=PairedDevices(tmp_path / "paired_devices.json"),
        ups_status_path=tmp_path / "ups_status",
        ups_keepalive_path=tmp_path / "ups_keepalive",
    )
    client = TestClient(app)
    with client.websocket_connect("/ws/control?token=shared-secret") as socket:
        req = client.post("/pair/request", json={"name": "Laptop"}).json()
        event = socket.receive_json()
        approved = client.post(f"/control/pairing/requests/{req['id']}/approve", headers=SHARED)
        assert approved.status_code == 200
        second = socket.receive_json()
    assert event["action"] == "pairing.requested"
    assert event["payload"] == {"id": req["id"], "name": "Laptop"}
    assert second["action"] == "pairing.approved"
    assert second["payload"] == {"id": req["id"], "name": "Laptop", "by": "shared-token"}


def test_an_unguarded_route_cannot_name_a_caller() -> None:
    # "Who approved this" must never silently read as nobody.
    request = Request({"type": "http", "path": "/x", "headers": [], "query_string": b""})
    with pytest.raises(RuntimeError, match="not behind control_auth_dependency"):
        control_caller(request)


def test_non_ascii_bearer_is_refused_not_a_crash(rig: Rig) -> None:
    # Sent as raw latin-1 bytes, the way a misbehaving client can.
    response = rig.client.get("/control/power", headers={"Authorization": b"Bearer caf\xe9"})  # type: ignore[arg-type]
    assert response.status_code == 401
