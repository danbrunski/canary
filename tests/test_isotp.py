"""Unit tests for `src/collection/isotp.py`'s negative-response handling.

Covers the gap this closes: a formal ISO-TP negative response
(`0x7F <service> <NRC>`) used to be visible only as a bool, discarding
the NRC byte that distinguishes "ECU says no, here's why" from a true
no-frame timeout.
"""

from __future__ import annotations

from src.collection.isotp import (
    IsoTpResponseAssembler,
    is_negative_response,
    negative_response_code,
    nrc_name,
)


def _sf(*body: int) -> bytes:
    """Single-frame CAN payload: PCI byte (length) + body, padded to 8 bytes."""
    data = bytes([len(body), *body])
    return data + bytes(8 - len(data))


def test_negative_response_code_extracts_request_out_of_range() -> None:
    data = _sf(0x7F, 0x22, 0x31)
    assert is_negative_response(data, 0x22) is True
    assert negative_response_code(data, 0x22) == 0x31


def test_negative_response_code_extracts_sub_function_not_supported() -> None:
    data = _sf(0x7F, 0x01, 0x12)
    assert is_negative_response(data, 0x01) is True
    assert negative_response_code(data, 0x01) == 0x12


def test_negative_response_code_extracts_conditions_not_correct() -> None:
    data = _sf(0x7F, 0x22, 0x22)
    assert is_negative_response(data, 0x22) is True
    assert negative_response_code(data, 0x22) == 0x22


def test_negative_response_code_none_for_positive_response() -> None:
    # 62 11 03 0A 5C — a normal Mode-22 positive response, not a refusal.
    data = _sf(0x62, 0x11, 0x03, 0x0A, 0x5C)
    assert is_negative_response(data, 0x22) is False
    assert negative_response_code(data, 0x22) is None


def test_negative_response_code_none_when_service_does_not_match() -> None:
    # 7F 11 31 is a refusal of service 0x11, not the 0x22 we asked about.
    data = _sf(0x7F, 0x11, 0x31)
    assert is_negative_response(data, 0x22) is False
    assert negative_response_code(data, 0x22) is None


def test_negative_response_code_none_when_code_byte_is_missing() -> None:
    # Malformed: length byte claims only 2 body bytes (7F 22), no NRC byte.
    data = bytes([0x02, 0x7F, 0x22, 0x00, 0x00, 0x00, 0x00, 0x00])
    assert is_negative_response(data, 0x22) is False
    assert negative_response_code(data, 0x22) is None


def test_nrc_name_known_codes() -> None:
    assert nrc_name(0x11) == "serviceNotSupported"
    assert nrc_name(0x12) == "subFunctionNotSupported"
    assert nrc_name(0x31) == "requestOutOfRange"


def test_nrc_name_falls_back_to_raw_hex_for_unknown_code() -> None:
    assert nrc_name(0x99) == "unknown (0x99)"


def test_assembler_populates_negative_response_code() -> None:
    assembler = IsoTpResponseAssembler(service=0x22, identifier=0x1103)
    result = assembler.accept(0x7E8, _sf(0x7F, 0x22, 0x31))
    assert result.negative_response is True
    assert result.negative_response_code == 0x31
    assert result.response is None


def test_assembler_positive_response_has_no_negative_response_code() -> None:
    assembler = IsoTpResponseAssembler(service=0x22, identifier=0x1103)
    result = assembler.accept(0x7E8, _sf(0x62, 0x11, 0x03, 0x0A, 0x5C))
    assert result.negative_response is False
    assert result.negative_response_code is None
    assert result.response is not None
