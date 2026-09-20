from __future__ import annotations

import io
import json

import pytest

from src.collection.can_obd_client import RawReply
from src.collection.isotp import ObdResponse
from tools.can_memory_log import kwp32_memory_body, log_memory


class _Client:
    def __init__(self, replies: list[RawReply]) -> None:
        self.replies = iter(replies)
        self.sessions: list[tuple[int, int | None]] = []
        self.requests: list[tuple[int, bytes, int | None]] = []
        self.did_requests: list[tuple[int, int | None]] = []

    def enter_diagnostic_session(self, subfunction: int, *, request_id: int | None = None) -> bool:
        self.sessions.append((subfunction, request_id))
        return True

    def query_raw(self, service: int, body: bytes, *, request_id: int | None = None) -> RawReply:
        self.requests.append((service, body, request_id))
        return next(self.replies)

    def query_mode_22(self, did: int, *, request_id: int | None = None) -> ObdResponse | None:
        self.did_requests.append((did, request_id))
        return ObdResponse(service=0x62, pid=did, payload=b"\x01\x23", responder_id=0x7E8)


def test_kwp32_body_is_the_vehicle_proven_wire_form() -> None:
    assert kwp32_memory_body(0xFFFF26BE, 2) == bytes.fromhex("ffff26be0002")
    with pytest.raises(ValueError, match="32 bits"):
        kwp32_memory_body(0x1_0000_0000, 2)
    with pytest.raises(ValueError, match="1-65535"):
        kwp32_memory_body(0, 0)


def test_log_memory_stamps_and_preserves_positive_and_negative_reads() -> None:
    client = _Client(
        [
            RawReply(sent=True, payload=bytes.fromhex("4000")),
            RawReply(sent=True, negative_response_code=0x31),
        ]
    )
    out = io.StringIO()
    wall = iter([100.0, 100.01, 100.02, 100.03, 100.1, 100.11, 100.12, 100.13])
    ticks = iter([0.0, 0.0, 0.01, 0.1, 0.11, 0.2])

    summary = log_memory(
        client,
        out,
        address=0xFFFF26BE,
        length=2,
        samples=2,
        interval_seconds=0.0,
        request_id=0x7E0,
        session=0xC0,
        dids=(0x1206,),
        monotonic=lambda: next(ticks),
        wall_time=lambda: next(wall),
    )

    rows = [json.loads(line) for line in out.getvalue().splitlines()]
    assert rows[0]["form"] == "kwp32"
    assert rows[1]["payload_hex"] == "4000"
    assert rows[2]["type"] == "did"
    assert rows[2]["did"] == "0x1206"
    assert rows[2]["payload_hex"] == "0123"
    assert rows[3]["nrc"] == "0x31"
    assert client.sessions == [(0xC0, 0x7E0)]
    assert client.requests == [
        (0x23, bytes.fromhex("ffff26be0002"), 0x7E0),
        (0x23, bytes.fromhex("ffff26be0002"), 0x7E0),
    ]
    assert client.did_requests == [(0x1206, 0x7E0), (0x1206, 0x7E0)]
    assert summary["reads_ok"] == 1
    assert summary["reads_missing"] == 1
    assert summary["did_reads_ok"] == 2
    assert summary["did_reads_missing"] == 0


def test_log_memory_stops_when_passive_is_not_usable() -> None:
    from tools.did_log import PassiveStatus

    client = _Client([RawReply(sent=True, payload=b"\x00")])
    out = io.StringIO()
    summary = log_memory(
        client,
        out,
        address=0,
        length=1,
        samples=1,
        interval_seconds=0,
        request_id=0x7E0,
        session=0xC0,
        passive_checker=lambda: PassiveStatus(False, None, "off"),
        monotonic=lambda: 0.0,
    )

    assert str(summary["reason"]).startswith("passive_")
    assert client.sessions == []
    assert client.requests == []
