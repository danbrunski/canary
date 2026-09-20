"""Regression tests pinned to the 2026-09-20 UpRev USB captures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.collection.uprev_protocol import (
    STREAM_POLL,
    UprvProtocolError,
    UprvStreamDecoder,
    build_memory_byte_stream_definition,
    build_mixed_stream_definition,
    build_stream_definition,
    encode_message,
    parse_memory_byte_stream_definition,
    parse_stream_data,
    parse_stream_definition,
    parse_stream_response,
)

FIXTURE = Path(__file__).parent / "fixtures" / "uprev_usb" / "stream_transcript.jsonl"


def _fixture_rows() -> list[dict[str, object]]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def test_cable_ping_envelope_matches_capture() -> None:
    assert encode_message(b"\x07") == bytes.fromhex("55505256000100000000000707")


def test_stream_decoder_handles_fragmented_and_coalesced_reads() -> None:
    decoder = UprvStreamDecoder()
    wire = encode_message(b"\x06") + encode_message(bytes.fromhex("6602ec816f"))

    assert decoder.feed(wire[:9]) == []
    messages = decoder.feed(wire[9:15]) + decoder.feed(wire[15:])

    assert [message.payload for message in messages] == [b"\x06", bytes.fromhex("6602ec816f")]
    assert decoder.discarded_bytes == 0


def test_stream_decoder_resynchronizes_after_leading_noise() -> None:
    decoder = UprvStreamDecoder()
    messages = decoder.feed(b"noise" + encode_message(b"\x06"))
    assert [message.payload for message in messages] == [b"\x06"]
    assert decoder.discarded_bytes == 5


def test_stream_decoder_rejects_bad_outer_checksum() -> None:
    decoder = UprvStreamDecoder()
    wire = bytearray(encode_message(b"\x07"))
    wire[11] = 0x08
    with pytest.raises(UprvProtocolError, match="checksum mismatch"):
        decoder.feed(bytes(wire))


def test_fixture_definitions_round_trip() -> None:
    definitions = [row for row in _fixture_rows() if row["kind"] == "stream_definition"]
    assert definitions
    for row in definitions:
        payload = bytes.fromhex(str(row["payload"]))
        identifiers = tuple(int(token, 16) for token in row["dids"])
        assert parse_stream_definition(payload) == identifiers
        assert build_stream_definition(identifiers) == payload
    assert STREAM_POLL == bytes.fromhex("020021810401")


def test_target_afr_memory_definition_round_trip() -> None:
    addresses = (0xFFFF26BE, 0xFFFF26BF)
    payload = bytes.fromhex("0200ac8183ffff26be83ffff26bf")

    assert build_memory_byte_stream_definition(addresses) == payload
    assert parse_memory_byte_stream_definition(payload) == addresses


def test_mixed_definition_preserves_did_then_memory_order() -> None:
    assert build_mixed_stream_definition(
        [0x1201, 0x121A], [0xFFFF26BE, 0xFFFF26BF]
    ) == bytes.fromhex("0200ac8102120102121a83ffff26be83ffff26bf")


def test_target_afr_memory_reply_decodes_in_descriptor_order() -> None:
    # 0x4000 was returned in all 525 captured Target AFR samples.
    payload = bytes.fromhex("66046181400026")
    assert parse_stream_data(payload, [1, 1]) == (b"\x40", b"\x00")


def test_memory_definition_rejects_did_descriptor() -> None:
    with pytest.raises(UprvProtocolError, match="malformed memory-stream descriptors"):
        parse_memory_byte_stream_definition(bytes.fromhex("0200ac81021208"))


def test_fixture_stream_responses_decode() -> None:
    responses = [row for row in _fixture_rows() if row["kind"] == "stream_response"]
    assert responses
    widths = {0x1101: 1, 0x1125: 1, 0x1201: 2}
    for row in responses:
        identifiers = tuple(int(token, 16) for token in row["dids"])
        readings = parse_stream_response(bytes.fromhex(str(row["payload"])), identifiers, widths)
        assert {f"0x{did:04x}": value.hex() for did, value in readings.items()} == row["values"]


def test_parse_stream_response_rejects_bad_inner_checksum() -> None:
    with pytest.raises(UprvProtocolError, match="inner checksum mismatch"):
        parse_stream_response(
            bytes.fromhex("660561814e006094"),
            [0x1101, 0x1201],
            {0x1101: 1, 0x1201: 2},
        )


def test_parse_stream_response_refuses_unknown_width() -> None:
    with pytest.raises(UprvProtocolError, match="unknown response width for DID 0x1125"):
        parse_stream_response(
            bytes.fromhex("660461818e5fd3"),
            [0x1101, 0x1125],
            {0x1101: 1},
        )
