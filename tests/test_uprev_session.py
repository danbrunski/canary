"""Pure tests for captured UpRev K-line request/response structure."""

from __future__ import annotations

import pytest

from src.collection.uprev_protocol import UprvMessage
from src.collection.uprev_session import (
    UpRevDiagnosticSession,
    UpRevNegativeResponseError,
    UpRevSessionError,
    build_did_request,
    parse_did_response,
    supported_dids,
)


def test_build_did_request_matches_continuous_captured_poll() -> None:
    assert build_did_request(0x1103) == bytes.fromhex("02002211030401")


def test_build_did_request_checks_range() -> None:
    with pytest.raises(ValueError, match="DID out of range"):
        build_did_request(0x10000)


def test_parse_one_byte_did_response() -> None:
    assert parse_did_response(bytes.fromhex("6604621103a721"), 0x1103) == b"\xa7"


def test_parse_multi_byte_support_response() -> None:
    assert parse_did_response(
        bytes.fromhex("6607621100e4419a61"), 0x1100, checksummed=False
    ) == bytes.fromhex("e4419a61")


def test_parse_did_negative_response() -> None:
    with pytest.raises(UpRevNegativeResponseError) as caught:
        parse_did_response(bytes.fromhex("66037f2212"), 0x1103)
    assert caught.value.service == 0x22
    assert caught.value.code == 0x12


def test_parse_did_refuses_wrong_identifier() -> None:
    with pytest.raises(UpRevSessionError, match="identifier mismatch"):
        parse_did_response(bytes.fromhex("6604621103a721"), 0x1104)


def test_parse_did_refuses_bad_checksum() -> None:
    with pytest.raises(UpRevSessionError, match="checksum mismatch"):
        parse_did_response(bytes.fromhex("6604621103a720"), 0x1103)


def test_supported_dids_expands_masks_but_excludes_next_group_bit() -> None:
    assert supported_dids({0x1100: bytes.fromhex("80000001"), 0x1120: bytes(4)}) == (0x1101,)


def test_supported_dids_requires_four_byte_masks() -> None:
    with pytest.raises(UpRevSessionError, match="expected 4"):
        supported_dids({0x1100: b"\x80"})


class _FakeTransport:
    """Replays a fixed reply queue; records what was sent."""

    def __init__(self, replies: list[bytes]) -> None:
        self._replies = list(replies)
        self.sent: list[bytes] = []

    def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    def receive(self, *, timeout_seconds: float = 1.0) -> UprvMessage:
        if not self._replies:
            raise TimeoutError("no more replies")
        return UprvMessage(payload=self._replies.pop(0))


def test_read_did_drains_a_stale_reply_for_a_previous_did() -> None:
    """A queued reply for an earlier DID is not this request's answer.

    The cable leaves complete replies queued after a bulk-write timeout
    (reproduced on hardware 2026-09-20). Treating one as a fatal identifier
    mismatch fails a read that would otherwise have succeeded.
    """
    transport = _FakeTransport(
        [
            bytes.fromhex("6604621103a721"),  # stale: answers 0x1103
            bytes.fromhex("6604621104b833"),  # the reply actually requested
        ]
    )
    session = UpRevDiagnosticSession(transport)  # type: ignore[arg-type]

    assert session.read_did(0x1104) == b"\xb8"
    assert len(transport.sent) == 1


def test_read_did_drains_a_stale_stream_reply() -> None:
    transport = _FakeTransport(
        [
            bytes.fromhex("6605618100d88d4c"),  # stale dynamic-stream reply
            bytes.fromhex("6604621103a721"),
        ]
    )
    session = UpRevDiagnosticSession(transport)  # type: ignore[arg-type]

    assert session.read_did(0x1103) == b"\xa7"


def test_read_did_still_surfaces_a_malformed_reply() -> None:
    """Draining must not swallow a genuinely broken answer to this request."""
    transport = _FakeTransport([bytes.fromhex("6604621103a720")] * 3)
    session = UpRevDiagnosticSession(transport)  # type: ignore[arg-type]

    with pytest.raises(UpRevSessionError, match="checksum mismatch"):
        session.read_did(0x1103)
