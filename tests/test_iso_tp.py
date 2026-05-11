"""Tests for ISO-TP reassembly and UDS decoding."""

from __future__ import annotations

from pathlib import Path

from src.analysis.iso_tp import (
    Frame,
    decode_session,
    decode_uds,
    iter_frames,
    parse_candump_line,
    reassemble,
)


def _f(ts: float, can_id: int, hex_data: str) -> Frame:
    return Frame(timestamp=ts, channel="vcan0", can_id=can_id, data=bytes.fromhex(hex_data))


# ---------------------------------------------------------------------------
# candump parser
# ---------------------------------------------------------------------------


def test_parse_candump_basic():
    f = parse_candump_line("(1778471186.033136) vcan0 7E0#0210C0FFFFFFFFFF R")
    assert f is not None
    assert f.timestamp == 1778471186.033136
    assert f.channel == "vcan0"
    assert f.can_id == 0x7E0
    assert f.data == bytes.fromhex("0210C0FFFFFFFFFF")


def test_parse_candump_no_flag_suffix():
    # Flag char (R/T) is optional in candump output.
    f = parse_candump_line("(1.0) can0 123#DEADBEEF")
    assert f is not None
    assert f.can_id == 0x123
    assert f.data == bytes.fromhex("DEADBEEF")


def test_parse_candump_returns_none_on_garbage():
    assert parse_candump_line("not a candump line") is None
    assert parse_candump_line("") is None
    assert parse_candump_line("\n") is None


def test_iter_frames_skips_blanks_and_garbage(tmp_path: Path):
    log = tmp_path / "frames.log"
    log.write_text(
        "(1.0) vcan0 100#AABB\n"
        "\n"
        "garbage line\n"
        "(2.0) vcan0 200#CCDD R\n"
    )
    frames = list(iter_frames(log))
    assert [f.can_id for f in frames] == [0x100, 0x200]


# ---------------------------------------------------------------------------
# ISO-TP reassembly
# ---------------------------------------------------------------------------


def test_single_frame_yielded_directly():
    # PCI 0x05 = SF, 5-byte payload "62 11 03 0A 5C"
    msgs = list(reassemble([_f(1.0, 0x7E8, "0562110305A3000000")]))
    assert len(msgs) == 1
    assert msgs[0].payload == bytes.fromhex("62110305A3")
    assert msgs[0].timestamp == 1.0


def test_first_plus_consecutive_frames_reassemble():
    # FF: PCI 10 + len-byte 0D = 13 bytes total, then 6 bytes "61 83 5A 53 30 34"
    # CF #1: seq 1, next 7 bytes "43 00 41 06 04 00 00"
    frames = [
        _f(1.0, 0x7E8, "10 0D 61 83 5A 53 30 34".replace(" ", "")),
        _f(1.1, 0x7E8, "21 43 00 41 06 04 00 00".replace(" ", "")),
    ]
    msgs = list(reassemble(frames))
    assert len(msgs) == 1
    # 6 bytes from FF + 7 from CF = 13 = declared length. CF padding bytes
    # past that point are dropped by the chunk_len min(7, remaining) clamp.
    expected = bytes.fromhex("6183 5A 53 30 34 43 00 41 06 04 00 00".replace(" ", ""))
    assert msgs[0].payload == expected
    assert msgs[0].timestamp == 1.0  # timestamp of first frame in the message


def test_consecutive_frame_with_wrong_seq_drops_message():
    frames = [
        _f(1.0, 0x7E8, "100D618300000000"),
        _f(1.1, 0x7E8, "23DEADBEEF000000"),  # seq 3, expected seq 1
    ]
    msgs = list(reassemble(frames))
    assert msgs == []


def test_consecutive_without_first_is_ignored():
    # A stray CF with no in-flight FF must be dropped, not yield garbage.
    msgs = list(reassemble([_f(1.0, 0x7E8, "21AABBCCDDEEFF00")]))
    assert msgs == []


def test_flow_control_frames_emit_nothing():
    msgs = list(reassemble([_f(1.0, 0x7E0, "300000FFFFFFFFFF")]))
    assert msgs == []


def test_per_can_id_state_isolated():
    """Two different CAN IDs reassembling concurrently must not interfere."""
    # Each FF declares 10 total bytes: 6 in FF + 4 more in CF.
    frames = [
        _f(1.0, 0x7E8, "10 0A 61 12 33 34 35 36".replace(" ", "")),  # FF on 7E8
        _f(1.1, 0x7E9, "10 0A 62 56 77 78 79 7A".replace(" ", "")),  # FF on 7E9
        _f(1.2, 0x7E8, "21 37 38 39 3A 00 00 00".replace(" ", "")),  # CF on 7E8
        _f(1.3, 0x7E9, "21 7B 7C 7D 7E 00 00 00".replace(" ", "")),  # CF on 7E9
    ]
    msgs = list(reassemble(frames))
    assert len(msgs) == 2
    by_id = {m.can_id: m for m in msgs}
    assert by_id[0x7E8].payload == bytes.fromhex("611233343536373839 3A".replace(" ", ""))
    assert by_id[0x7E9].payload == bytes.fromhex("6256777879 7A 7B 7C 7D 7E".replace(" ", ""))


def test_first_frame_invalidates_prior_partial():
    """A new FF arriving while one is in-flight on the same ID restarts cleanly."""
    frames = [
        _f(1.0, 0x7E8, "100A 6112 3334 35".replace(" ", "")),  # FF #1, abandoned
        _f(1.1, 0x7E8, "0362110A".ljust(16, "F")),               # SF arrives instead
    ]
    msgs = list(reassemble(frames))
    assert len(msgs) == 1
    assert msgs[0].payload == bytes.fromhex("62110A")


# ---------------------------------------------------------------------------
# UDS decoding
# ---------------------------------------------------------------------------


def test_uds_mode_22_request_extracts_did():
    # ISO-TP SF "03 22 11 03 ..." after PCI strip → "22 11 03"
    from src.analysis.iso_tp import IsoTpMessage

    iso = IsoTpMessage(timestamp=1.0, can_id=0x7E0, payload=bytes.fromhex("221103"))
    msg = decode_uds(iso)
    assert msg is not None
    assert msg.is_response is False
    assert msg.service_id == 0x22
    assert msg.service_name == "ReadDataByIdentifier"
    assert msg.did == 0x1103
    assert msg.body == b""


def test_uds_mode_22_response_extracts_did_and_data():
    from src.analysis.iso_tp import IsoTpMessage

    iso = IsoTpMessage(timestamp=1.0, can_id=0x7E8, payload=bytes.fromhex("6211030A5C"))
    msg = decode_uds(iso)
    assert msg is not None
    assert msg.is_response is True
    assert msg.is_negative is False
    assert msg.service_id == 0x22
    assert msg.did == 0x1103
    assert msg.body == bytes.fromhex("0A5C")


def test_uds_negative_response_extracts_nrc():
    from src.analysis.iso_tp import IsoTpMessage

    iso = IsoTpMessage(timestamp=1.0, can_id=0x7E8, payload=bytes.fromhex("7F2231"))
    msg = decode_uds(iso)
    assert msg is not None
    assert msg.is_negative is True
    assert msg.service_id == 0x22
    assert msg.nrc == 0x31


def test_uds_unknown_request_service_decoded_as_unknown():
    from src.analysis.iso_tp import IsoTpMessage

    # 0x33 is below the response range (0x40+) so it's treated as a request SID.
    iso = IsoTpMessage(timestamp=1.0, can_id=0x7E0, payload=bytes.fromhex("33CD"))
    msg = decode_uds(iso)
    assert msg is not None
    assert msg.is_response is False
    assert msg.service_id == 0x33
    assert "0x33" in msg.service_name


def test_decode_uds_empty_payload_returns_none():
    from src.analysis.iso_tp import IsoTpMessage

    assert decode_uds(IsoTpMessage(timestamp=1.0, can_id=0x7E0, payload=b"")) is None


# ---------------------------------------------------------------------------
# end-to-end
# ---------------------------------------------------------------------------


def test_decode_session_end_to_end_filters_and_decodes():
    """Non-diagnostic frames are filtered; diag frames pass through."""
    frames = [
        _f(1.0, 0x002, "0102030405060708"),  # broadcast — must be filtered out
        _f(1.1, 0x7E0, "03221103FFFFFFFF"),  # Mode 22 request
        _f(1.2, 0x7E8, "056211030A5C0000"),  # Mode 22 response
    ]
    msgs = list(decode_session(frames))
    assert len(msgs) == 2
    assert msgs[0].did == 0x1103 and not msgs[0].is_response
    assert msgs[1].did == 0x1103 and msgs[1].is_response
    assert msgs[1].body == bytes.fromhex("0A5C")


def test_decode_session_custom_diag_ids():
    frames = [_f(1.0, 0x123, "022183FFFFFFFFFF")]
    # default diag_ids would skip 0x123; explicit set includes it.
    msgs = list(decode_session(frames, diag_ids={0x123}))
    assert len(msgs) == 1
    assert msgs[0].service_id == 0x21
