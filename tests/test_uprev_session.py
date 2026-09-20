"""Pure tests for captured UpRev K-line request/response structure."""

from __future__ import annotations

import pytest

from src.collection.uprev_session import (
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
