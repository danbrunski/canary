"""Tests for the Mode 22 DID scanner (Task 4.E.5)."""

from __future__ import annotations

import pytest

from src.analysis.did_scan import (
    DidSnapshot,
    Mode22Transport,
    _extract_hex_bytes,
    classify_dids,
    parse_mode22_response,
    sweep_dids,
)

# ---------------------------------------------------------------------------
# parse_mode22_response
# ---------------------------------------------------------------------------


def test_parse_positive_response_returns_payload() -> None:
    assert parse_mode22_response(0x1100, bytes.fromhex("621100ABCD")) == bytes.fromhex("ABCD")


def test_parse_positive_response_empty_payload() -> None:
    assert parse_mode22_response(0x00F1, bytes.fromhex("6200F1")) == b""


def test_parse_negative_response_is_none() -> None:
    assert parse_mode22_response(0x1100, bytes.fromhex("7F2231")) is None  # NRC requestOutOfRange


def test_parse_rejects_too_short() -> None:
    assert parse_mode22_response(0x1100, bytes.fromhex("6211")) is None
    assert parse_mode22_response(0x1100, b"") is None


def test_parse_rejects_wrong_did_echo() -> None:
    assert parse_mode22_response(0x1100, bytes.fromhex("621101AB")) is None  # DID echo 0x1101 ≠ ask


def test_parse_rejects_non_62_service() -> None:
    assert parse_mode22_response(0x000C, bytes.fromhex("410C1234")) is None  # a Mode 01 reply


# ---------------------------------------------------------------------------
# sweep_dids + classify_dids
# ---------------------------------------------------------------------------


class _FakeTransport(Mode22Transport):
    """Returns canned data for a fixed map of DIDs; None otherwise."""

    def __init__(self, responses: dict[int, bytes]) -> None:
        self._responses = responses
        self.reads: list[int] = []

    def read_did(self, did: int) -> bytes | None:
        self.reads.append(did)
        return self._responses.get(did)


def test_sweep_keeps_only_responding_dids_and_reports_progress() -> None:
    transport = _FakeTransport({0x1100: b"\x01\x02", 0x1102: b"\x00"})
    seen: list[tuple[int, bytes | None]] = []
    snap = sweep_dids(
        transport, range(0x1100, 0x1104), on_progress=lambda d, v: seen.append((d, v))
    )
    assert set(snap.values) == {0x1100, 0x1102}
    assert snap.values[0x1100] == b"\x01\x02"
    assert transport.reads == [0x1100, 0x1101, 0x1102, 0x1103]
    assert [d for d, _ in seen] == [0x1100, 0x1101, 0x1102, 0x1103]
    assert snap.captured_at > 0


def test_snapshot_to_dict_is_json_friendly() -> None:
    snap = DidSnapshot(values={0x12AB: b"\xde\xad"}, captured_at=1.5, label="pass1")
    d = snap.to_dict()
    assert d["label"] == "pass1"
    assert d["values"] == {"0x12AB": "dead"}


def test_classify_splits_varying_constant_intermittent() -> None:
    a = DidSnapshot(values={0xAAAA: b"\x01", 0xBBBB: b"\x10", 0xCCCC: b"\x00"}, captured_at=1.0)
    b = DidSnapshot(values={0xAAAA: b"\x01", 0xBBBB: b"\x20", 0xDDDD: b"\x99"}, captured_at=2.0)
    result = classify_dids([a, b])
    assert result.constant == frozenset({0xAAAA})  # same in both
    assert result.varying == frozenset({0xBBBB})  # changed
    assert result.intermittent == frozenset({0xCCCC, 0xDDDD})  # only one snapshot each
    assert result.all_seen == frozenset({0xAAAA, 0xBBBB, 0xCCCC, 0xDDDD})


def test_classify_needs_two_snapshots() -> None:
    with pytest.raises(ValueError, match="at least 2 snapshots"):
        classify_dids([DidSnapshot(values={}, captured_at=1.0)])


# ---------------------------------------------------------------------------
# _extract_hex_bytes (ELM327 reply parsing)
# ---------------------------------------------------------------------------


def test_extract_single_line_response() -> None:
    assert _extract_hex_bytes("62 F1 90 11 22\r\r>") == bytes.fromhex("62F1901122")


def test_extract_lowercase_and_extra_whitespace() -> None:
    assert _extract_hex_bytes("  62 f1 90  \r >") == bytes.fromhex("62F190")


def test_extract_strips_isotp_frame_markers_and_length_header() -> None:
    reply = "014\r0: 62 F1 90 31 4A 4E\r1: 38 41 52 30 35 59\r2: 38 58 31 00 00 00\r>"
    out = _extract_hex_bytes(reply)
    # 014 (length header) dropped; "N:" markers dropped; 18 data bytes remain
    assert out[:3] == bytes.fromhex("62F190")
    assert len(out) == 18


def test_extract_returns_empty_on_error_tokens() -> None:
    assert _extract_hex_bytes("NO DATA\r>") == b""
    assert _extract_hex_bytes("?\r>") == b""
    assert _extract_hex_bytes("CAN ERROR\r>") == b""


def test_extract_handles_negative_response_text() -> None:
    # 7F 22 NRC is not an "error token" string — it parses to bytes, and
    # parse_mode22_response then rejects it on the 0x62 check.
    raw = _extract_hex_bytes("7F 22 31\r>")
    assert raw == bytes.fromhex("7F2231")
    assert parse_mode22_response(0x1100, raw) is None
