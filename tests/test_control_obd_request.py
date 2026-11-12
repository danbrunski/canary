"""Tests for the /control/obd_request route (ad-hoc read-only diagnostics)."""

from __future__ import annotations

import threading
from pathlib import Path

import can
import pytest
from fastapi.testclient import TestClient

from src.collection.isotp import build_request_payload
from src.config import CanaryConfig, CanBusConfig
from src.control.obd_request import encode_request_body
from src.dashboard.api import create_app
from src.validation.scripted_ecu import single_frame_reply

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
                single_frame_reply(responder_id=0x7E8, service=0x40 | service, pid=pid, body=body)
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


def _run_refusing_ecu(channel: str, *, nrc: int, stop: threading.Event) -> None:
    """An ECU that answers every request with ``7F <service> <nrc>``."""
    ecu = can.interface.Bus(interface="virtual", channel=channel)
    try:
        while not stop.is_set():
            frame = ecu.recv(timeout=0.1)
            if frame is None:
                continue
            service = bytes(frame.data)[1]
            reply = bytes([0x03, 0x7F, service, nrc]) + bytes(4)
            ecu.send(can.Message(arbitration_id=0x7E8, data=reply, is_extended_id=False))
    finally:
        ecu.shutdown()


def test_obd_request_reports_a_refusal_as_answered_not_silent(tmp_path: Path) -> None:
    """A negative response is an answer, not a timeout.

    W02.1, 2026-09-22 KOEO: ``21 10`` in the default session drew ``7F 21 80``
    from the ECM and this endpoint reported ``responded: false`` — the same
    as nothing on the bus. The two demand opposite responses from whoever
    reads them, so they must not look the same.
    """
    channel = "obdreq-refused"
    client = _api(tmp_path, channel=channel)
    stop = threading.Event()
    t = threading.Thread(
        target=_run_refusing_ecu, args=(channel,), kwargs={"nrc": 0x80, "stop": stop}, daemon=True
    )
    t.start()
    try:
        resp = client.post(
            "/control/obd_request",
            json={"service": 0x21, "identifier": 0x10, "header": 0x7E0, "timeout_seconds": 2.0},
            headers=_auth(),
        )
    finally:
        stop.set()
        t.join(timeout=1.0)

    assert resp.status_code == 200
    body = resp.json()["obd_request"]
    assert body["responded"] is True
    assert body["negative_response_code"] == "0x80"
    assert body["negative_response"] == "serviceNotSupportedInActiveDiagnosticSession"
    assert body["raw_hex"] == "7f2180"
    assert body["decoded"] is None


# --- D03.66: explicit identifier width, raw data, declared reply id ---------


def _run_recording_ecu(
    channel: str,
    *,
    request_id: int,
    responder_id: int,
    reply_frames: list[bytes],
    seen: list[can.Message],
    stop: threading.Event,
) -> None:
    """A module that records every frame and answers the first request on
    ``request_id`` with ``reply_frames`` — one single frame, or a First Frame
    whose remaining frames go out only after flow control arrives on
    ``request_id`` (the module's own tester id, as ISO-TP requires)."""
    ecu = can.interface.Bus(interface="virtual", channel=channel)
    pending: list[bytes] = []
    try:
        while not stop.is_set():
            frame = ecu.recv(timeout=0.1)
            if frame is None:
                continue
            seen.append(frame)
            if frame.arbitration_id != request_id:
                continue
            data = bytes(frame.data)
            if data[0] >> 4 == 0x3 and pending:
                for cf in pending:
                    ecu.send(
                        can.Message(arbitration_id=responder_id, data=cf, is_extended_id=False)
                    )
                pending = []
                continue
            if data[0] >> 4 == 0x0:
                first, *rest = reply_frames
                ecu.send(can.Message(arbitration_id=responder_id, data=first, is_extended_id=False))
                pending = list(rest)
    finally:
        ecu.shutdown()


def _post_against(
    tmp_path: Path,
    channel: str,
    body: dict[str, object],
    *,
    request_id: int,
    responder_id: int,
    reply_frames: list[bytes],
) -> tuple[dict[str, object], list[can.Message]]:
    client = _api(tmp_path, channel=channel)
    stop = threading.Event()
    seen: list[can.Message] = []
    t = threading.Thread(
        target=_run_recording_ecu,
        args=(channel,),
        kwargs={
            "request_id": request_id,
            "responder_id": responder_id,
            "reply_frames": reply_frames,
            "seen": seen,
            "stop": stop,
        },
        daemon=True,
    )
    t.start()
    try:
        resp = client.post("/control/obd_request", json=body, headers=_auth())
    finally:
        stop.set()
        t.join(timeout=1.0)
    assert resp.status_code == 200, resp.text
    return resp.json()["obd_request"], seen


#: The BCM's registered-sensor-id read on 2026-11-11 (``21 1F`` -> ``61 1F``
#: + four 3-byte ids + an empty fifth slot), 17 bytes: a multi-frame reply.
_BCM_ID_REPLY = bytes.fromhex("611f0a640e506a0ec39d0f8f980f000000")


def test_kwp_local_identifier_to_the_bcm_puts_the_exact_bytes_on_the_wire(
    tmp_path: Path,
) -> None:
    """D03.66: ``21 1F`` to 0x745, reply on 0x765. Before, the endpoint had no
    way to name 0x765: the reply sat outside the 0x7E8-0x7EF window and
    ``header + 8`` (0x74D) was the only responder it would accept, so the read
    was done with ``cansend`` from a shell on the Pi."""
    total = len(_BCM_ID_REPLY)
    first = bytes([0x10 | (total >> 8), total & 0xFF]) + _BCM_ID_REPLY[:6]
    cf1 = bytes([0x21]) + _BCM_ID_REPLY[6:13]
    cf2 = bytes([0x22]) + _BCM_ID_REPLY[13:] + bytes(8 - 1 - len(_BCM_ID_REPLY[13:]))
    result, seen = _post_against(
        tmp_path,
        "obdreq-bcm-lid",
        {
            "service": 0x21,
            "identifier": 0x1F,
            "identifier_bytes": 1,
            "header": 0x745,
            "response_id": 0x765,
            "timeout_seconds": 2.0,
        },
        request_id=0x745,
        responder_id=0x765,
        reply_frames=[first, cf1, cf2],
    )

    requests = [m for m in seen if m.arbitration_id == 0x745]
    # The request: ISO-TP single frame ``02 21 1F``, zero-padded to 8 bytes as
    # the original (inferred-width) path pads.
    assert bytes(requests[0].data) == bytes([0x02, 0x21, 0x1F, 0, 0, 0, 0, 0])
    assert bytes(requests[0].data) == build_request_payload(0x21, 0x1F)
    # Flow control for the multi-frame reply went to the BCM's tester id, not
    # responder - 8 (0x75D).
    assert bytes(requests[1].data)[0] == 0x30
    assert not [m for m in seen if m.arbitration_id == 0x75D]
    assert result["responded"] is True
    assert result["responder_id"] == "0x765"
    assert result["response_id"] == "0x765"
    assert result["request_id"] == "0x745"
    assert result["identifier"] == "0x1F"
    assert result["identifier_bytes"] == 1
    assert result["request_hex"] == "211f"
    assert result["identifier_echoed"] is True
    assert result["raw_hex"] == _BCM_ID_REPLY[2:].hex()


def test_a_two_byte_did_below_0x100_is_sent_as_two_bytes(tmp_path: Path) -> None:
    """The inferred width sends DID 0x0005 as ``22 05``; ``identifier_bytes=2``
    sends ``22 00 05`` and checks the two-byte echo."""
    reply = bytes([0x05, 0x62, 0x00, 0x05, 0xAB, 0xCD, 0, 0])
    result, seen = _post_against(
        tmp_path,
        "obdreq-did2",
        {
            "service": 0x22,
            "identifier": 0x05,
            "identifier_bytes": 2,
            "header": 0x7E0,
            "timeout_seconds": 2.0,
        },
        request_id=0x7E0,
        responder_id=0x7E8,
        reply_frames=[reply],
    )
    assert bytes(seen[0].data) == bytes([0x03, 0x22, 0x00, 0x05, 0, 0, 0, 0])
    assert result["identifier"] == "0x0005"
    assert result["request_hex"] == "220005"
    assert result["identifier_echoed"] is True
    assert result["raw_hex"] == "abcd"


def test_data_hex_is_the_whole_argument_after_the_service_byte(tmp_path: Path) -> None:
    """``17 FF 00`` (the BCM's stored-code read) has no identifier to name."""
    reply = bytes([0x02, 0x57, 0x00, 0, 0, 0, 0, 0])
    result, seen = _post_against(
        tmp_path,
        "obdreq-data",
        {
            "service": 0x17,
            "data_hex": "ff 00",
            "header": 0x745,
            "response_id": 0x765,
            "timeout_seconds": 2.0,
        },
        request_id=0x745,
        responder_id=0x765,
        reply_frames=[reply],
    )
    assert bytes(seen[0].data) == bytes([0x03, 0x17, 0xFF, 0x00, 0, 0, 0, 0])
    assert result["request_hex"] == "17ff00"
    assert result["identifier"] is None
    assert result["identifier_echoed"] is None
    assert result["raw_hex"] == "00"


@pytest.mark.parametrize(
    ("service", "identifier", "header", "expected"),
    [
        (0x01, 0x0C, None, bytes([0x02, 0x01, 0x0C, 0, 0, 0, 0, 0])),
        (0x22, 0x1101, 0x7E0, bytes([0x03, 0x22, 0x11, 0x01, 0, 0, 0, 0])),
        (0x21, 0x10, 0x7E0, bytes([0x02, 0x21, 0x10, 0, 0, 0, 0, 0])),
        (0x03, None, None, bytes([0x01, 0x03, 0, 0, 0, 0, 0, 0])),
    ],
)
def test_the_default_encoding_is_unchanged(
    tmp_path: Path, service: int, identifier: int | None, header: int | None, expected: bytes
) -> None:
    """Without ``identifier_bytes``/``data_hex`` a caller sends exactly what it
    sent before D03.66: the width follows the identifier's value."""
    body: dict[str, object] = {"service": service, "timeout_seconds": 0.3}
    if identifier is not None:
        body["identifier"] = identifier
    if header is not None:
        body["header"] = header
    request_id = 0x7DF if header is None else header
    result, seen = _post_against(
        tmp_path,
        f"obdreq-default-{service:02x}",
        body,
        request_id=request_id,
        responder_id=0x7E8,
        reply_frames=[bytes(8)],
    )
    assert bytes(seen[0].data) == expected
    assert result["identifier_bytes"] is None
    assert result["response_id"] is None


@pytest.mark.parametrize(
    ("body", "names"),
    [
        ({"service": 0x21, "identifier": 0x1F1F, "identifier_bytes": 1}, "not truncated"),
        ({"service": 0x21, "identifier_bytes": 1}, "needs an identifier"),
        ({"service": 0x21, "identifier": 0x1F, "data_hex": "1f"}, "cannot be combined"),
        ({"service": 0x21, "identifier_bytes": 2, "data_hex": "1f"}, "cannot be combined"),
        ({"service": 0x17, "data_hex": "f"}, "not whole hex bytes"),
        ({"service": 0x17, "data_hex": "zz"}, "not whole hex bytes"),
        ({"service": 0x17, "data_hex": ""}, "data_hex is empty"),
        ({"service": 0x23, "data_hex": "00112233445566"}, "at most 6"),
        ({"service": 0x21, "identifier": 0x1F, "identifier_bytes": 3}, "identifier_bytes"),
        ({"service": 0x21, "identifier": 0x1F, "response_id": 0x800}, "response_id"),
    ],
)
def test_a_body_that_does_not_fit_is_refused_by_name(
    tmp_path: Path, body: dict[str, object], names: str
) -> None:
    client = _api(tmp_path, channel="obdreq-refuse-body")
    resp = client.post("/control/obd_request", json=body, headers=_auth())
    assert resp.status_code == 422
    assert names in resp.text


def test_encode_request_body_refuses_rather_than_truncates() -> None:
    """The domain function enforces the same contract for a non-HTTP caller."""
    assert encode_request_body(identifier=0x1F, identifier_bytes=None, data=None) is None
    assert encode_request_body(identifier=0x1F, identifier_bytes=1, data=None) == b"\x1f"
    assert encode_request_body(identifier=0x1F, identifier_bytes=2, data=None) == b"\x00\x1f"
    assert encode_request_body(identifier=None, identifier_bytes=None, data=b"\xff\x00") == (
        b"\xff\x00"
    )
    with pytest.raises(ValueError, match="does not fit identifier_bytes=1"):
        encode_request_body(identifier=0x100, identifier_bytes=1, data=None)


def test_without_response_id_a_reply_on_0x765_is_never_heard(tmp_path: Path) -> None:
    """Why ``response_id`` exists: the default window is 0x7E8-0x7EF and a
    physical header is answered by ``header + 8`` (0x74D), so the BCM's reply
    is dropped and the request reads as silence."""
    result, seen = _post_against(
        tmp_path,
        "obdreq-bcm-no-rid",
        {"service": 0x21, "identifier": 0x1F, "header": 0x745, "timeout_seconds": 0.3},
        request_id=0x745,
        responder_id=0x765,
        reply_frames=[bytes([0x03, 0x61, 0x1F, 0x00, 0, 0, 0, 0])],
    )
    assert bytes(seen[0].data) == bytes([0x02, 0x21, 0x1F, 0, 0, 0, 0, 0])
    assert result["responded"] is False
