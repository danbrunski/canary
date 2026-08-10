"""Tests for the /control/obd_request route (ad-hoc read-only diagnostics)."""

from __future__ import annotations

import threading
from pathlib import Path

import can
from fastapi.testclient import TestClient

from src.config import CanaryConfig, CanBusConfig
from src.dashboard.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _api(tmp_path: Path, *, channel: str) -> TestClient:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config.hardware.can = CanBusConfig(interface="virtual", channel=channel, bitrate=500000)
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def _single_frame_reply(
    *, responder_id: int, service: int, pid: int | None, body: bytes
) -> can.Message:
    payload = bytes([service]) if pid is None else bytes([service, pid])
    payload += body
    frame = bytes([len(payload)]) + payload
    frame += bytes(8 - len(frame))
    return can.Message(arbitration_id=responder_id, data=frame, is_extended_id=False)


def _run_fake_ecu(
    channel: str, *, service: int, pid: int | None, body: bytes, stop: threading.Event
) -> None:
    ecu = can.interface.Bus(interface="virtual", channel=channel)
    try:
        while not stop.is_set():
            frame = ecu.recv(timeout=0.1)
            if frame is None:
                continue
            ecu.send(
                _single_frame_reply(responder_id=0x7E8, service=0x40 | service, pid=pid, body=body)
            )
    finally:
        ecu.shutdown()


def test_obd_request_requires_auth(tmp_path: Path) -> None:
    client = _api(tmp_path, channel="obdreq-auth")
    resp = client.post("/control/obd_request", json={"service": 0x03})
    assert resp.status_code == 401


def test_obd_request_accepts_any_service(tmp_path: Path) -> None:
    """No service allowlist: Mode 04 (CLEAR_DTC, a write) is accepted and
    sent like any other. Nothing answers on the virtual bus, so the route
    reports `responded: false` rather than rejecting the request."""
    client = _api(tmp_path, channel="obdreq-anyservice")
    resp = client.post(
        "/control/obd_request",
        json={"service": 0x04, "timeout_seconds": 0.2},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["obd_request"]["responded"] is False


def test_obd_request_reports_no_response_on_timeout(tmp_path: Path) -> None:
    client = _api(tmp_path, channel="obdreq-timeout")
    resp = client.post(
        "/control/obd_request",
        json={"service": 0x03, "timeout_seconds": 0.2},
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.json()["obd_request"]
    assert body["responded"] is False
    assert body["raw_hex"] is None


def test_obd_request_decodes_mode_01_pid(tmp_path: Path) -> None:
    channel = "obdreq-mode01"
    client = _api(tmp_path, channel=channel)
    stop = threading.Event()
    # RPM=1552 (0x1840)
    t = threading.Thread(
        target=_run_fake_ecu,
        args=(channel,),
        kwargs={"service": 0x01, "pid": 0x0C, "body": bytes([0x18, 0x40]), "stop": stop},
        daemon=True,
    )
    t.start()
    try:
        resp = client.post(
            "/control/obd_request",
            json={"service": 0x01, "identifier": 0x0C, "timeout_seconds": 2.0},
            headers=_auth(),
        )
    finally:
        stop.set()
        t.join(timeout=1.0)

    assert resp.status_code == 200
    body = resp.json()["obd_request"]
    assert body["responded"] is True
    assert body["decoded"] == {"channel": "RPM", "value": 1552.0, "unit": "rpm"}


def test_obd_request_accepts_a_header_override(tmp_path: Path) -> None:
    """`header` should reach the request id, not just the default 0x7DF —
    the whole point of the parameter is addressing a non-ECM module."""
    channel = "obdreq-header"
    client = _api(tmp_path, channel=channel)
    stop = threading.Event()
    t = threading.Thread(
        target=_run_fake_ecu,
        args=(channel,),
        kwargs={"service": 0x03, "pid": None, "body": bytes([0x00]), "stop": stop},
        daemon=True,
    )
    t.start()
    try:
        resp = client.post(
            "/control/obd_request",
            json={"service": 0x03, "header": 0x7E1, "timeout_seconds": 2.0},
            headers=_auth(),
        )
    finally:
        stop.set()
        t.join(timeout=1.0)

    assert resp.status_code == 200
    body = resp.json()["obd_request"]
    assert body["request_id"] == "0x7E1"
