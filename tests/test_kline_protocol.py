"""KWP2000 over K-line at the wire, checked against the frames the CANary-HAT
captured on the Xterra (UpRev's session and its own, 2026-09-23)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.collection.kline import protocol as kp
from src.collection.kline.protocol import (
    AddressedFrame,
    DidDescriptor,
    IncludeDescriptor,
    KlineProtocolError,
    MemoryByteDescriptor,
    NegativeResponseError,
    ServiceRefusedError,
)
from src.collection.obd_scheduler import did_response_widths

FIXTURE = Path(__file__).parent / "fixtures" / "kline" / "uprev_session_frames.json"
FRAMES = {k: bytes.fromhex(v) for k, v in json.loads(FIXTURE.read_text())["frames"].items()}


def test_start_communication_is_byte_exact() -> None:
    assert kp.start_communication(0x10, 0xFC) == FRAMES["start_communication"]


def test_start_communication_reply_carries_addresses_and_key_bytes() -> None:
    reply = kp.parse_start_communication_reply(FRAMES["start_communication_reply"])
    assert (reply.target, reply.source, reply.key_bytes) == (0xFC, 0x10, b"\x5d\x8f")


def test_every_length_frame_in_the_capture_round_trips() -> None:
    for name, raw in FRAMES.items():
        if name.startswith("start_communication"):
            continue
        payload = kp.decode_length_frame(raw)
        assert kp.encode_length_frame(payload) == raw, name


def test_addressed_frames_round_trip() -> None:
    for name in ("start_communication", "start_communication_reply"):
        raw = FRAMES[name]
        assert kp.encode_addressed(kp.decode_addressed(raw)) == raw


def test_a_corrupted_byte_fails_the_checksum() -> None:
    raw = bytearray(FRAMES["poll_83_reply"])
    raw[10] ^= 0x01
    with pytest.raises(KlineProtocolError, match="checksum"):
        kp.decode_length_frame(bytes(raw))


def test_a_length_byte_that_disagrees_with_the_frame_is_refused() -> None:
    with pytest.raises(KlineProtocolError, match="length byte"):
        kp.decode_length_frame(FRAMES["read_did"][:-2] + FRAMES["read_did"][-1:])


def test_read_did_request_with_the_profile_trailer_is_byte_exact() -> None:
    payload = kp.read_did(0x1103, trailer=b"\x04\x01")
    assert kp.encode_length_frame(payload) == FRAMES["read_did"]


def test_did_reply_value_is_split_out() -> None:
    params = kp.expect_positive(0x22, kp.decode_length_frame(FRAMES["read_did_reply"]))
    assert kp.split_did_reply(0x1103, params) == b"\x9a"
    params = kp.expect_positive(0x22, kp.decode_length_frame(FRAMES["native_read_reply"]))
    assert kp.split_did_reply(0x1103, params) == b"\x99"


def test_negative_responses_raise_with_the_code_named() -> None:
    with pytest.raises(NegativeResponseError, match="0xa1.*subFunctionNotSupported") as refused:
        kp.expect_positive(0xA1, kp.decode_length_frame(FRAMES["probe_a1_refused"]))
    assert (refused.value.service, refused.value.code) == (0xA1, 0x12)
    with pytest.raises(NegativeResponseError, match="serviceNotSupported"):
        kp.expect_positive(0xA2, kp.decode_length_frame(FRAMES["probe_a2_refused"]))


def test_a_reply_to_a_different_service_is_an_error() -> None:
    with pytest.raises(KlineProtocolError, match="expected 0x61"):
        kp.expect_positive(0x21, kp.decode_length_frame(FRAMES["read_did_reply"]))


def _definitions() -> dict[int, tuple[kp.Descriptor, ...]]:
    out = {}
    for name in ("define_81", "define_82", "define_83"):
        lid, descriptors = kp.parse_define_local_id(kp.decode_length_frame(FRAMES[name]))
        out[lid] = descriptors
    return out


def test_uprev_definitions_parse_with_includes_and_duplicates_kept() -> None:
    defs = _definitions()
    assert set(defs) == {0x81, 0x82, 0x83}
    assert len(defs[0x81]) == 12
    assert all(isinstance(d, MemoryByteDescriptor) for d in defs[0x81])
    # UpRev defines RAM 0xFFFF13FE and 0xFFFF13FF twice each: legal on the wire.
    assert defs[0x81].count(MemoryByteDescriptor(0xFFFF13FE)) == 2
    assert defs[0x82][0] == IncludeDescriptor(0x81)
    assert defs[0x83][0] == IncludeDescriptor(0x82)
    assert DidDescriptor(0x1201) in defs[0x82]


def test_each_uprev_definition_is_acknowledged_positively() -> None:
    for lid in (0x81, 0x82, 0x83):
        params = kp.expect_positive(
            kp.SID_DEFINE_LOCAL_ID, kp.decode_length_frame(FRAMES[f"define_{lid:02x}_reply"])
        )
        assert params == bytes([lid])


def test_our_own_definitions_encode_like_uprevs_but_refuse_duplicates() -> None:
    lid, descriptors = kp.parse_define_local_id(kp.decode_length_frame(FRAMES["define_83"]))
    assert kp.encode_length_frame(kp.define_local_id(lid, descriptors)) == FRAMES["define_83"]
    _, with_duplicates = kp.parse_define_local_id(kp.decode_length_frame(FRAMES["define_81"]))
    with pytest.raises(KlineProtocolError, match="repeats"):
        kp.define_local_id(0x81, with_duplicates)


def test_poll_request_is_byte_exact() -> None:
    assert kp.encode_length_frame(kp.read_local_id(0x83, b"\x04\x01")) == FRAMES["poll_83"]


def test_the_poll_reply_expands_to_all_three_chained_sets() -> None:
    values = kp.expand_local_id(0x83, _definitions())
    # 12 RAM bytes (81) + 5 RAM bytes and 11 DIDs (82) + 10 DIDs (83).
    assert len(values) == 38
    ram = [v for v in values if isinstance(v, MemoryByteDescriptor)]
    assert len(ram) == 17


def test_unknown_catalog_widths_are_refused_not_guessed() -> None:
    """The real reply cannot be split with today's catalog: five of UpRev's
    DIDs have no recorded width. Refusing is the right answer."""
    widths, _ = did_response_widths()
    values = kp.expand_local_id(0x83, _definitions())
    with pytest.raises(KlineProtocolError, match="0x1117"):
        kp.value_widths(values, widths)


def test_the_reply_pins_the_unknown_widths_total() -> None:
    """Everything the catalog knows plus 17 RAM bytes is 43; the ECM sent 50,
    so the five DIDs without a width take exactly 7 bytes between them."""
    widths, _ = did_response_widths()
    values = kp.expand_local_id(0x83, _definitions())
    params = kp.expect_positive(0x21, kp.decode_length_frame(FRAMES["poll_83_reply"]))
    known = sum(
        1 if isinstance(v, MemoryByteDescriptor) else widths[v.did]
        for v in values
        if isinstance(v, MemoryByteDescriptor) or v.did in widths
    )
    unknown = sorted(
        {v.did for v in values if isinstance(v, DidDescriptor) and v.did not in widths}
    )
    assert unknown == [0x1117, 0x1126, 0x1204, 0x1209, 0x122B]
    assert len(params) - 1 - known == 7

    # Any assignment summing to 7 splits cleanly; a wrong total is refused.
    hypothesis = {**widths, 0x1117: 1, 0x1126: 1, 0x1204: 1, 0x1209: 2, 0x122B: 2}
    split = kp.split_local_id_reply(0x83, params, kp.value_widths(values, hypothesis))
    assert len(split) == 38
    wrong = {**hypothesis, 0x1209: 1}
    with pytest.raises(KlineProtocolError, match="carried 50 bytes"):
        kp.split_local_id_reply(0x83, params, kp.value_widths(values, wrong))


def test_a_local_id_that_includes_itself_is_refused() -> None:
    with pytest.raises(KlineProtocolError, match="includes itself"):
        kp.expand_local_id(
            0x81, {0x81: (IncludeDescriptor(0x82),), 0x82: (IncludeDescriptor(0x81),)}
        )


def test_an_undefined_include_is_refused() -> None:
    with pytest.raises(KlineProtocolError, match="never defined"):
        kp.expand_local_id(0x82, {0x82: (IncludeDescriptor(0x81),)})


@pytest.mark.parametrize("service", [0x14, 0x27, 0x2E, 0x31, 0x34, 0x36, 0x37, 0x3B, 0x3D])
def test_every_write_service_is_refused_by_name(service: int) -> None:
    with pytest.raises(ServiceRefusedError, match="does not write ECU memory"):
        kp.check_request(bytes([service, 0x00]))


def test_services_off_the_allowlist_are_refused() -> None:
    with pytest.raises(ServiceRefusedError, match="allowlist"):
        kp.check_request(bytes([0xA1, 0x00]))  # UpRev's own probe: not ours to send


def test_the_programming_session_is_refused() -> None:
    kp.check_request(bytes([0x10, 0xC0]))
    with pytest.raises(ServiceRefusedError, match="programming session"):
        kp.check_request(bytes([0x10, 0x85]))


def test_every_request_this_client_builds_passes_its_own_allowlist() -> None:
    for payload in (
        kp.read_did(0x1201),
        kp.read_local_id(0x81, b"\x04\x01"),
        kp.define_local_id(0x81, [DidDescriptor(0x1201), MemoryByteDescriptor(0xFFFF26BE)]),
        bytes([kp.SID_STOP_COMMUNICATION]),
        kp.decode_addressed(kp.start_communication(0x10, 0xFC)).payload,
    ):
        kp.check_request(payload)


def test_functional_addressing_sets_the_format_bits() -> None:
    frame = kp.encode_addressed(AddressedFrame(0x33, 0xF1, b"\x81", functional=True))
    assert frame == bytes.fromhex("c1 33 f1 81 66")


def test_direction_comes_from_the_service_byte_alone() -> None:
    """K01.1: a reply whose request the sniffer missed still reads as a reply."""
    requests = [sid for sid in kp.SERVICE_NAMES]
    assert requests and not any(kp.is_response_sid(sid) for sid in requests)
    assert all(kp.is_response_sid(sid + kp.POSITIVE_OFFSET) for sid in requests)
    assert kp.is_response_sid(kp.SID_NEGATIVE_RESPONSE)


def test_naming_a_service_is_not_permission_to_send_it() -> None:
    assert kp.SERVICE_NAMES[kp.SID_IO_CONTROL_BY_LOCAL_ID] == "InputOutputControlByLocalIdentifier"
    with pytest.raises(kp.ServiceRefusedError):
        kp.check_request(bytes([kp.SID_IO_CONTROL_BY_LOCAL_ID, 0x01]))
    assert set(kp.ALLOWED_SERVICES) <= set(kp.SERVICE_NAMES)
