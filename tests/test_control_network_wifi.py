"""/control/network/wifi: the saved networks, asked of the root service (N5.3).

A fake service listens on a socket in ``tmp_path`` and answers what the real
``canary-provision`` would, so these cover the routes' own work: the token,
the request they send, the status they map each failure to, the event and
ledger entry a change leaves, and that a passphrase appears in none of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import grp
import json
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.dashboard.api import create_app
from src.provisioning import client
from src.provisioning.netsock import NetworkControlServer
from src.provisioning.network import profile_filename
from tests.test_provisioning_network import FakeRunner
from tests.test_provisioning_wifi import AP, HOTSPOT, PRECONFIGURED, STARLINK, WIRED
from tests.test_provisioning_wifi import cli as wifi_cli

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"
AUTH = {"Authorization": "Bearer secret"}
SECRET = "S3cretPassphrase!"

NETWORK = {
    "id": "u-1",
    "ssid": "Starlink",
    "priority": 50,
    "autoconnect": True,
    "hidden": False,
    "security": "wpa-psk",
    "active": True,
    "in_range": True,
    "signal": 82,
    "managed_by": "canary",
}
WIFI = {
    "networks": [NETWORK],
    "scan": [{"ssid": "Starlink", "signal": 82, "security": "WPA2", "saved": True}],
    "scanned_at": 1000.0,
    "interface": "wlan0",
}
OK = {"ok": True, "wifi": WIFI}
Responder = Callable[[dict[str, Any]], dict[str, Any] | bytes | None]


class FakeService:
    """Answers one request per connection, as the real socket does."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.requests: list[dict[str, Any]] = []
        self.respond: Responder = lambda request: OK
        self.release = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX)
        self._sock.bind(str(path))
        self._sock.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data)
                self.requests.append(request)
                reply = self.respond(request)
                if reply is None:  # never answers
                    self.release.wait(5)
                    continue
                body = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
                conn.sendall(body + b"\n")

    def close(self) -> None:
        self.release.set()
        self._sock.close()


def _always(reply: dict[str, Any]) -> Responder:
    """A responder that answers every request with ``reply``."""
    return lambda request: reply


@pytest.fixture
def service(tmp_path: Path) -> Iterator[FakeService]:
    fake = FakeService(tmp_path / "w.sock")
    yield fake
    fake.close()


def _api(tmp_path: Path, socket_path: Path | None = None) -> tuple[TestClient, Path]:
    audit = tmp_path / "audit.jsonl"
    app = create_app(
        CanaryConfig.from_yaml(EXAMPLE_YAML),
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        audit_logger=ControlAuditLogger(audit),
        network_socket_path=socket_path or tmp_path / "w.sock",
    )
    return TestClient(app), audit


def _ledger(audit: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []


def _failure(kind: str, detail: str = "why") -> dict[str, Any]:
    return {"ok": False, "error": kind, "detail": detail}


# --- the token ---------------------------------------------------------------------

ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/control/network/wifi", None),
    ("POST", "/control/network/wifi/scan", None),
    ("POST", "/control/network/wifi", {"ssid": "x", "join": False}),
    ("POST", "/control/network/wifi/u-1/connect", None),
    ("PUT", "/control/network/wifi/order", {"ids": ["u-1"]}),
    ("DELETE", "/control/network/wifi/u-1", None),
]


@pytest.mark.parametrize(("method", "url", "body"), ROUTES)
def test_every_wifi_route_needs_the_token(
    tmp_path: Path, service: FakeService, method: str, url: str, body: dict[str, Any] | None
) -> None:
    api, audit = _api(tmp_path)
    assert api.request(method, url, json=body).status_code == 401
    assert service.requests == [] and _ledger(audit) == []


# --- reads ----------------------------------------------------------------------------


def test_get_returns_the_state_the_service_gave(tmp_path: Path, service: FakeService) -> None:
    api, audit = _api(tmp_path)
    response = api.get("/control/network/wifi", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"wifi": WIFI}
    assert service.requests == [{"op": "list"}]
    assert _ledger(audit) == []  # a read is not a change


def test_scan_asks_for_a_rescan(tmp_path: Path, service: FakeService) -> None:
    api, audit = _api(tmp_path)
    response = api.post("/control/network/wifi/scan", headers=AUTH)
    assert response.json() == {"wifi": WIFI}
    assert service.requests == [{"op": "scan"}]
    assert _ledger(audit) == []


# --- add ------------------------------------------------------------------------------


def _add(service: FakeService, **body: Any) -> dict[str, Any]:
    service.respond = lambda request: {"ok": True, "network": NETWORK, "wifi": WIFI}
    return {"ssid": "Starlink", "password": SECRET, "join": True, **body}


def test_add_and_join(tmp_path: Path, service: FakeService) -> None:
    api, audit = _api(tmp_path)
    response = api.post("/control/network/wifi", json=_add(service), headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"network": NETWORK, "wifi": WIFI}
    assert service.requests == [
        {"op": "add", "ssid": "Starlink", "password": SECRET, "hidden": False, "join": True}
    ]
    (entry,) = _ledger(audit)
    assert (entry["action"], entry["result"]) == ("network.wifi_changed", "ok")
    assert entry["payload"] == {
        "op": "add",
        "ssid": "Starlink",
        "join": True,
        "hidden": False,
        "ok": True,
    }


def test_save_for_later_hidden_and_open(tmp_path: Path, service: FakeService) -> None:
    api, _ = _api(tmp_path)
    body = _add(service, join=False, hidden=True)
    del body["password"]
    assert api.post("/control/network/wifi", json=body, headers=AUTH).status_code == 200
    assert service.requests == [
        {"op": "add", "ssid": "Starlink", "password": None, "hidden": True, "join": False}
    ]
    # An empty passphrase is an open network, the same as none.
    api.post("/control/network/wifi", json=_add(service, password="", join=False), headers=AUTH)
    assert service.requests[-1]["password"] is None


def test_a_passphrase_is_in_no_response_event_or_ledger(
    tmp_path: Path, service: FakeService
) -> None:
    api, audit = _api(tmp_path)
    seen = []
    # Succeeds, then fails every way the service can fail -- one of them
    # carelessly echoing the passphrase in its detail.
    outcomes: list[dict[str, Any]] = [
        {"ok": True, "network": NETWORK, "wifi": WIFI},
        _failure("join_failed", "Secrets were required"),
        _failure("join_failed", f"nmcli said {SECRET} was wrong"),
        _failure("refused", "held by another profile"),
        _failure("invalid", "a WPA passphrase is 8-63 printable ASCII characters"),
        _failure("internal", "boom"),
    ]
    for outcome in outcomes:
        service.respond = _always(outcome)
        response = api.post(
            "/control/network/wifi",
            json={"ssid": "S", "password": SECRET, "join": True},
            headers=AUTH,
        )
        seen.append(response.text)
    # And the ways a body can be wrong, which FastAPI would normally echo back.
    for body in (
        {"ssid": "S", "password": SECRET},  # no join
        {"ssid": "S", "password": SECRET, "join": "maybe"},
        {"ssid": "S" * 40, "password": SECRET, "join": True},
        {"ssid": "S", "password": SECRET, "join": True, "extra": 1},
        {"ssid": "", "password": SECRET, "join": True},
        {"ssid": "S", "password": SECRET * 40, "join": True},
    ):
        response = api.post("/control/network/wifi", json=body, headers=AUTH)
        assert response.status_code == 422
        seen.append(response.text)
    # The service going away, too.
    service.close()
    seen.append(
        api.post(
            "/control/network/wifi",
            json={"ssid": "S", "password": SECRET, "join": True},
            headers=AUTH,
        ).text
    )
    assert any("***" in text for text in seen)  # the careless echo was scrubbed, not passed on
    assert SECRET not in "\n".join(seen)
    assert SECRET not in audit.read_text()
    assert len(_ledger(audit)) == 7  # six attempts reached the service; the 7th found it gone


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("join_failed", 409),
        ("refused", 409),
        ("invalid", 422),
        ("not_found", 404),
        ("internal", 502),
        ("unauthorized", 503),
        ("something_new", 502),
    ],
)
def test_service_failures_map_to_statuses_and_are_recorded(
    tmp_path: Path, service: FakeService, kind: str, status: int
) -> None:
    api, audit = _api(tmp_path)
    service.respond = lambda request: _failure(kind, "the reason")
    response = api.post("/control/network/wifi", json={"ssid": "S", "join": True}, headers=AUTH)
    assert response.status_code == status
    if kind != "unauthorized":
        assert response.json() == {"detail": "the reason"}
    else:
        assert "canary group" in response.json()["detail"]
    (entry,) = _ledger(audit)
    assert (entry["action"], entry["result"]) == ("network.wifi_changed", "failed")
    assert entry["payload"]["ok"] is False and entry["payload"]["error"] == kind
    assert entry["payload"]["ssid"] == "S"


@pytest.mark.parametrize(
    "body",
    [
        {"ssid": "x" * 33, "join": True},
        {"ssid": "é" * 17, "join": True},
        {"ssid": "", "join": True},
        {"join": True},
        {"ssid": "x"},
        {"ssid": "x", "join": True, "hidden": "maybe"},
        {"ssid": "x", "join": True, "password": 5},
        {"ssid": "x", "join": True, "unknown": 1},
    ],
)
def test_a_bad_add_body_is_a_422_before_the_service_is_asked(
    tmp_path: Path, service: FakeService, body: dict[str, Any]
) -> None:
    api, audit = _api(tmp_path)
    response = api.post("/control/network/wifi", json=body, headers=AUTH)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)  # text the page can show as it is
    assert service.requests == [] and _ledger(audit) == []


def test_a_name_of_exactly_32_bytes_is_accepted(tmp_path: Path, service: FakeService) -> None:
    api, _ = _api(tmp_path)
    body = _add(service, ssid="é" * 16)
    assert api.post("/control/network/wifi", json=body, headers=AUTH).status_code == 200


# --- connect, order, forget -----------------------------------------------------------


def test_connect(tmp_path: Path, service: FakeService) -> None:
    api, audit = _api(tmp_path)
    response = api.post("/control/network/wifi/u-1/connect", headers=AUTH)
    assert response.json() == {"wifi": WIFI}
    assert service.requests == [{"op": "connect", "id": "u-1"}]
    assert _ledger(audit)[0]["payload"] == {"op": "connect", "id": "u-1", "ok": True}


def test_forget(tmp_path: Path, service: FakeService) -> None:
    api, audit = _api(tmp_path)
    response = api.delete("/control/network/wifi/u-1", headers=AUTH)
    assert response.json() == {"wifi": WIFI}
    assert service.requests == [{"op": "forget", "id": "u-1"}]
    assert _ledger(audit)[0]["payload"] == {"op": "forget", "id": "u-1", "ok": True}


def test_order(tmp_path: Path, service: FakeService) -> None:
    api, audit = _api(tmp_path)
    response = api.put("/control/network/wifi/order", json={"ids": ["b", "a"]}, headers=AUTH)
    assert response.json() == {"wifi": WIFI}
    assert service.requests == [{"op": "order", "ids": ["b", "a"]}]
    assert _ledger(audit)[0]["payload"] == {"op": "order", "ids": ["b", "a"], "ok": True}


def test_order_with_a_bad_body_asks_nobody(tmp_path: Path, service: FakeService) -> None:
    api, _ = _api(tmp_path)
    for body in ({}, {"ids": "u-1"}, {"ids": [1]}, {"ids": [], "extra": 1}):
        assert api.put("/control/network/wifi/order", json=body, headers=AUTH).status_code == 422
    assert service.requests == []


@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", "/control/network/wifi/nope/connect", None),
        ("DELETE", "/control/network/wifi/nope", None),
    ],
)
def test_unknown_and_refused_profiles(
    tmp_path: Path, service: FakeService, method: str, url: str, body: Any
) -> None:
    api, _ = _api(tmp_path)
    service.respond = lambda request: _failure("not_found", "no saved network 'nope'")
    assert api.request(method, url, headers=AUTH).status_code == 404
    service.respond = lambda request: _failure("refused", "'canary-wired' is not a WiFi profile")
    refused = api.request(method, url, headers=AUTH)
    assert refused.status_code == 409 and "canary-wired" in refused.json()["detail"]


def test_order_ids_not_the_saved_set_is_a_409(tmp_path: Path, service: FakeService) -> None:
    # The service calls it `invalid`; for an order it is a conflict with what is
    # saved (the list may have changed under the page), and the contract says 409.
    api, audit = _api(tmp_path)
    service.respond = lambda request: _failure("invalid", "missing from the order: u-2")
    response = api.put("/control/network/wifi/order", json={"ids": ["u-1"]}, headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {"detail": "missing from the order: u-2"}
    assert _ledger(audit)[0]["result"] == "failed"


# --- the service is not there -----------------------------------------------------------


@pytest.mark.parametrize(("method", "url", "body"), ROUTES)
def test_an_absent_service_is_a_503_naming_the_socket(
    tmp_path: Path, method: str, url: str, body: dict[str, Any] | None
) -> None:
    missing = tmp_path / "absent.sock"
    api, _ = _api(tmp_path, missing)
    response = api.request(method, url, json=body, headers=AUTH)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail.startswith(f"WiFi control service not reachable at {missing}: ")
    assert detail.endswith("(is canary-provision running?)")
    assert "No such file" in detail


def test_a_dead_service_with_a_stale_socket_file_is_a_503(tmp_path: Path) -> None:
    stale = tmp_path / "stale.sock"
    old = socket.socket(socket.AF_UNIX)
    old.bind(str(stale))
    old.close()
    api, audit = _api(tmp_path, stale)
    response = api.get("/control/network/wifi", headers=AUTH)
    assert response.status_code == 503 and "refused" in response.json()["detail"].lower()
    assert _ledger(audit) == []


def test_a_failed_change_because_the_service_is_gone_is_still_recorded(tmp_path: Path) -> None:
    api, audit = _api(tmp_path, tmp_path / "absent.sock")
    api.delete("/control/network/wifi/u-1", headers=AUTH)
    (entry,) = _ledger(audit)
    assert entry["result"] == "failed" and entry["payload"]["error"] == client.KIND_UNREACHABLE


def test_a_service_that_does_not_answer_is_a_504(
    tmp_path: Path, service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "READ_TIMEOUT_S", 0.3)
    service.respond = lambda request: None
    api, _ = _api(tmp_path)
    response = api.get("/control/network/wifi", headers=AUTH)
    assert response.status_code == 504
    assert "did not answer" in response.json()["detail"]
    assert str(service.path) in response.json()["detail"]


def test_a_join_waits_longer_than_a_read(
    tmp_path: Path, service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.JOIN_TIMEOUT_S >= 60 > client.READ_TIMEOUT_S
    seen: list[float] = []
    real = client.call

    def spy(path: Path | None, payload: dict[str, Any], *, timeout_s: float = 0) -> dict[str, Any]:
        seen.append(timeout_s)
        return real(path, payload, timeout_s=timeout_s)

    monkeypatch.setattr(client, "call", spy)
    service.respond = lambda request: {"ok": True, "network": NETWORK, "wifi": WIFI}
    api, _ = _api(tmp_path)
    api.post(
        "/control/network/wifi", json={"ssid": "S", "password": "x", "join": True}, headers=AUTH
    )
    api.post(
        "/control/network/wifi", json={"ssid": "S", "password": "x", "join": False}, headers=AUTH
    )
    api.post("/control/network/wifi/u-1/connect", headers=AUTH)
    api.get("/control/network/wifi", headers=AUTH)
    assert seen == [
        client.JOIN_TIMEOUT_S,
        client.READ_TIMEOUT_S,
        client.JOIN_TIMEOUT_S,
        client.READ_TIMEOUT_S,
    ]


@pytest.mark.parametrize("reply", [b"not json", b"[1]", b'{"ok": "yes"}', b'{"ok": true}', b""])
def test_an_odd_reply_is_a_502(tmp_path: Path, service: FakeService, reply: bytes) -> None:
    service.respond = lambda request: reply
    api, _ = _api(tmp_path)
    assert api.get("/control/network/wifi", headers=AUTH).status_code == 502


def test_the_socket_path_defaults_from_the_environment(
    tmp_path: Path, service: FakeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CANARY_NETWORK_SOCKET", str(service.path))
    app = create_app(
        CanaryConfig.from_yaml(EXAMPLE_YAML),
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
    )
    assert TestClient(app).get("/control/network/wifi", headers=AUTH).json() == {"wifi": WIFI}


# --- the whole way: route -> client -> socket -> NetworkManager operations ------------


@pytest.fixture
def real_service(tmp_path: Path) -> Iterator[tuple[Path, FakeRunner, Path]]:
    """The real socket server over the real operations, with canned nmcli."""
    keyfiles = tmp_path / "nm"
    keyfiles.mkdir()
    nm, runner = wifi_cli(keyfiles, [PRECONFIGURED, STARLINK, HOTSPOT, WIRED, AP])
    path = tmp_path / "r.sock"
    loop = asyncio.new_event_loop()
    server = NetworkControlServer(nm, path=path, group=grp.getgrgid(os.getgid()).gr_name)
    tasks: list[asyncio.Task[None]] = []

    def run() -> None:
        asyncio.set_event_loop(loop)
        tasks.append(loop.create_task(server.serve()))
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(tasks[0])

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(300):
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
            break
        except OSError:
            time.sleep(0.01)
        finally:
            probe.close()
    yield path, runner, keyfiles
    loop.call_soon_threadsafe(tasks[0].cancel)
    thread.join(5)
    loop.close()


def test_the_contract_end_to_end(
    tmp_path: Path, real_service: tuple[Path, FakeRunner, Path]
) -> None:
    path, runner, keyfiles = real_service
    api, audit = _api(tmp_path, path)

    wifi = api.get("/control/network/wifi", headers=AUTH).json()["wifi"]
    assert set(wifi) == {"networks", "scan", "scanned_at", "interface"}
    assert [n["ssid"] for n in wifi["networks"]] == ["Starlink", "PhoneHotspot", "HomeWifi"]
    assert {n["managed_by"] for n in wifi["networks"]} == {"canary", "other"}
    assert all(
        set(n)
        == {
            "id",
            "ssid",
            "priority",
            "autoconnect",
            "hidden",
            "security",
            "active",
            "in_range",
            "signal",
            "managed_by",
        }
        for n in wifi["networks"]
    )
    assert set(wifi["scan"][0]) == {"ssid", "signal", "security", "saved"}

    scanned = api.post("/control/network/wifi/scan", headers=AUTH).json()["wifi"]
    assert scanned["scanned_at"] is not None

    added = api.post(
        "/control/network/wifi",
        json={"ssid": "PhoneHotspot", "password": SECRET, "join": False},
        headers=AUTH,
    )
    assert added.status_code == 200
    assert added.json()["network"]["ssid"] == "PhoneHotspot"
    assert f"psk={SECRET}" in (keyfiles / profile_filename("PhoneHotspot")).read_text()
    assert not runner.called("nmcli", "connection", "up")

    short = api.post(
        "/control/network/wifi",
        json={"ssid": "PhoneHotspot", "password": "short", "join": False},
        headers=AUTH,
    )
    assert short.status_code == 422 and "passphrase" in short.json()["detail"]

    held = api.post(
        "/control/network/wifi",
        json={"ssid": "HomeWifi", "password": SECRET, "join": False},
        headers=AUTH,
    )
    assert held.status_code == 409 and "forget it first" in held.json()["detail"]

    assert api.post("/control/network/wifi/u-pre/connect", headers=AUTH).status_code == 200
    assert api.post("/control/network/wifi/u-wired/connect", headers=AUTH).status_code == 409
    assert api.delete("/control/network/wifi/u-ap", headers=AUTH).status_code == 409
    assert api.delete("/control/network/wifi/u-wired", headers=AUTH).status_code == 409
    assert api.delete("/control/network/wifi/nope", headers=AUTH).status_code == 404
    assert api.delete("/control/network/wifi/u-pre", headers=AUTH).status_code == 200
    assert runner.called("nmcli", "connection", "delete", "uuid", "u-pre")
    assert not runner.called("nmcli", "connection", "delete", "uuid", "u-wired")

    ids = [n["id"] for n in wifi["networks"]]
    assert (
        api.put("/control/network/wifi/order", json={"ids": ids}, headers=AUTH).status_code == 200
    )
    partial = api.put("/control/network/wifi/order", json={"ids": ids[:1]}, headers=AUTH)
    assert partial.status_code == 409 and "missing from the order" in partial.json()["detail"]

    assert SECRET not in audit.read_text()
