"""Unit tests for `src/collection/isotp.py`'s negative-response handling.

Covers the gap this closes: a formal ISO-TP negative response
(`0x7F <service> <NRC>`) used to be visible only as a bool, discarding
the NRC byte that distinguishes "ECU says no, here's why" from a true
no-frame timeout.
"""

from __future__ import annotations

import pytest

from src.collection.isotp import (
    PERMANENT_REFUSAL_NRCS,
    RESPONSE_PENDING_NRC,
    IsoTpResponseAssembler,
    await_response,
    build_body_request_payload,
    build_request_payload,
    is_negative_response,
    negative_response_code,
    nrc_name,
)


def _sf(*body: int) -> bytes:
    """Single-frame CAN payload: PCI byte (length) + body, padded to 8 bytes."""
    data = bytes([len(body), *body])
    return data + bytes(8 - len(data))


def test_response_pending_is_not_a_refusal() -> None:
    """ISO 14229 0x78 means "received you, still working" — the tester must wait.

    Scoring it as a refusal is how one busy moment on the 2026-08-07 hybrid
    capture got recorded as "this ECU has no fast exit" and killed Mode 01 for
    the rest of the run.
    """
    assembler = IsoTpResponseAssembler(service=0x10, identifier=0x81)
    result = assembler.accept(0x7E8, _sf(0x7F, 0x10, RESPONSE_PENDING_NRC))
    assert result.response_pending is True
    assert result.negative_response is False
    assert result.negative_response_code == RESPONSE_PENDING_NRC


def test_a_real_refusal_is_still_a_refusal() -> None:
    assembler = IsoTpResponseAssembler(service=0x10, identifier=0x81)
    result = assembler.accept(0x7E8, _sf(0x7F, 0x10, 0x12))
    assert result.response_pending is False
    assert result.negative_response is True
    assert result.negative_response_code == 0x12


def test_only_never_means_never() -> None:
    """The NRCs that justify disabling a capability rather than retrying it."""
    assert 0x11 in PERMANENT_REFUSAL_NRCS  # serviceNotSupported
    assert 0x12 in PERMANENT_REFUSAL_NRCS  # subFunctionNotSupported
    for retryable in (0x21, 0x22, RESPONSE_PENDING_NRC):
        assert retryable not in PERMANENT_REFUSAL_NRCS


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


# --- D03.66: flow control target and the explicit-bytes request -------------


class _Frame:
    def __init__(self, arbitration_id: int, data: bytes) -> None:
        self.arbitration_id = arbitration_id
        self.data = data


def _flow_control_target(*, request_id: int, responder: int, window: tuple[int, int]) -> int:
    """Run one multi-frame exchange through ``await_response`` and return the
    id its flow control went to."""
    frames = iter(
        [
            _Frame(responder, bytes([0x10, 0x09, 0x61, 0x1F, 1, 2, 3, 4])),
            _Frame(responder, bytes([0x21, 5, 6, 7, 0, 0, 0, 0])),
        ]
    )
    sent: list[int] = []
    now = [0.0]

    def recv(_timeout: float) -> _Frame | None:
        now[0] += 0.001
        return next(frames, None)

    exchange = await_response(
        recv,
        sent.append,
        IsoTpResponseAssembler(service=0x21, identifier=0x1F),
        request_id,
        1.0,
        clock=lambda: now[0],
        response_ids=window,
    )
    assert exchange.response is not None
    assert exchange.response.payload == bytes([1, 2, 3, 4, 5, 6, 7])
    return sent[0]


def test_flow_control_goes_to_the_declared_physical_request_id() -> None:
    """The BCM answers 0x745 on 0x765; ``responder - 8`` (0x75D) is nobody."""
    assert _flow_control_target(request_id=0x745, responder=0x765, window=(0x765, 0x765)) == 0x745


def test_flow_control_is_unchanged_where_the_pairing_is_plus_eight() -> None:
    assert _flow_control_target(request_id=0x7E0, responder=0x7E8, window=(0x7E8, 0x7E8)) == 0x7E0
    # Functional request: the responder's tester id has to be inferred.
    assert _flow_control_target(request_id=0x7DF, responder=0x7E9, window=(0x7E8, 0x7EF)) == 0x7E1


def test_body_request_payload_pads_like_the_inferred_form() -> None:
    assert build_body_request_payload(0x21, b"\x1f") == build_request_payload(0x21, 0x1F)
    assert build_body_request_payload(0x22, b"\x00\x05") == bytes([3, 0x22, 0, 5, 0, 0, 0, 0])
    assert build_body_request_payload(0x3E, b"") == build_request_payload(0x3E, None)
    with pytest.raises(ValueError, match="at most 6"):
        build_body_request_payload(0x23, bytes(7))
