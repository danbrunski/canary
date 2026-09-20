"""Service-0x23 (ReadMemoryByAddress) channels, end to end.

The quantity that forced this path is the ECM's final commanded
equivalence ratio. It is not a DID — UpRev reads it from two absolute RAM
bytes — which is why 138 answering DIDs and a full broadcast sweep never
located it (data/analyses/2026-09-20-uprev-usb-road.md).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.collection.can_obd_client import ObdResponse
from src.collection.can_obd_collector import _decode_response
from src.collection.isotp import build_memory_read_payload
from src.collection.obd_scheduler import CanObdSpec, _PollRequest, build_poll_requests
from src.collection.request_eligibility import SESSION_PHASES, evaluate
from src.config import CanBusConfig
from src.decode.dids import (
    MemoryChannel,
    apply_memory_scaling,
    decode_memory_raw,
    is_memory_sentinel,
    memory_channels_for_revision,
)

_TARGET = MemoryChannel(
    name="ECM_TARGET_EQUIV_RATIO",
    address=0xFFFF26BE,
    length=2,
    byte_length=2,
    scale=1 / 16384,
    unit="phi",
    confidence="high",
    verified_tune_revision="ZS44A-k106",
    sentinel_raw=0,
    sentinel_meaning="fuel cut / no AFR target",
)


def test_request_frame_matches_the_form_verified_on_the_vehicle() -> None:
    """KWP32 body. KWP24 and UDS-ALFID both answered NRC 0x12 on this ECM."""
    assert build_memory_read_payload(0xFFFF26BE, 2) == bytes.fromhex("0723ffff26be0002")


@pytest.mark.parametrize(
    ("address", "length"),
    [(0x1_0000_0000, 2), (-1, 2), (0xFFFF26BE, 0), (0xFFFF26BE, 0x10000)],
)
def test_request_frame_refuses_out_of_range(address: int, length: int) -> None:
    with pytest.raises(ValueError):
        build_memory_read_payload(address, length)


@pytest.mark.parametrize(
    ("word", "expected_phi"),
    [(0x4000, 1.0), (0x4A80, 1.1640625), (0x4380, 1.0546875)],
)
def test_q14_word_decodes_to_the_documented_phi(word: int, expected_phi: float) -> None:
    raw = decode_memory_raw(_TARGET, word.to_bytes(2, "big"))
    assert raw == word
    assert apply_memory_scaling(_TARGET, raw) == pytest.approx(expected_phi)


def test_known_answer_target_afr() -> None:
    """0x4A80 is a 12.63 AFR target at gasoline stoich — the pinned value."""
    phi = apply_memory_scaling(_TARGET, 0x4A80)
    assert phi is not None
    assert 14.7 / phi == pytest.approx(12.63, abs=0.01)


def test_sentinel_decodes_to_nothing_rather_than_zero() -> None:
    """Scaling the fuel-cut word gives phi 0.0, which divides into infinity."""
    assert is_memory_sentinel(_TARGET, 0) is True
    assert apply_memory_scaling(_TARGET, 0) is None


def test_sentinel_needs_a_stated_meaning() -> None:
    with pytest.raises(ValidationError, match="sentinel_raw and sentinel_meaning"):
        MemoryChannel(
            name="X",
            address=0x1000,
            length=2,
            byte_length=2,
            confidence="low",
            verified_tune_revision="rev",
            sentinel_raw=0,
        )


def test_decoded_slice_must_fit_the_read() -> None:
    with pytest.raises(ValidationError, match="does not fit"):
        MemoryChannel(
            name="X",
            address=0x1000,
            length=2,
            byte_start=1,
            byte_length=2,
            confidence="low",
            verified_tune_revision="rev",
        )


def test_address_cannot_be_recorded_without_a_calibration() -> None:
    """The whole difference between a DID and an address."""
    with pytest.raises(ValidationError):
        MemoryChannel(name="X", address=0x1000, length=2, byte_length=2, confidence="low")


@pytest.mark.parametrize(
    ("running", "usable"),
    [("ZS44A-k106", True), ("ZS44A-k105", False), (None, False)],
)
def test_channel_is_withheld_on_another_calibration(running: str | None, usable: bool) -> None:
    ok, withheld = memory_channels_for_revision([_TARGET], running)
    assert bool(ok) is usable
    assert bool(withheld) is not usable


@pytest.mark.parametrize("phase", SESSION_PHASES)
def test_memory_is_gated_exactly_like_mode_22(phase: str) -> None:
    """Same diagnostic session, so it must not sit on the Mode 01-side row.

    On the Mode 01-side row every read would be issued in the one phase
    where this ECM is silent about service 0x23.
    """
    assert evaluate("memory", "default", phase) == evaluate("did", "default", phase)


def test_scheduler_builds_a_session_endpoint_request() -> None:
    spec = CanObdSpec(
        can=CanBusConfig(interface="socketcan", channel="can0", bitrate=500000),
        pids=(),
        memory=(_TARGET,),
        did_request_id=0x7E0,
    )
    requests = [r for r in build_poll_requests(None, spec) if r.kind == "memory"]

    assert len(requests) == 1
    request = requests[0]
    assert request.service == 0x23
    assert request.request_id == 0x7E0
    assert request.channel == "mem:ECM_TARGET_EQUIV_RATIO"
    assert request.memory_channel is _TARGET


def _request() -> _PollRequest:
    return _PollRequest(
        kind="memory",
        service=0x23,
        identifier=0,
        request_id=0x7E0,
        channel="mem:ECM_TARGET_EQUIV_RATIO",
        memory_channel=_TARGET,
    )


def _response(payload_hex: str) -> ObdResponse:
    return ObdResponse(
        service=0x63, pid=None, payload=bytes.fromhex(payload_hex), responder_id=0x7E8
    )


def test_decode_emits_the_engineering_value() -> None:
    response = _response("4a80")

    readings = _decode_response(_request(), response, timestamp=1.0)

    assert len(readings) == 1
    assert readings[0].pid == "mem:ECM_TARGET_EQUIV_RATIO"
    assert readings[0].value == pytest.approx(1.1640625)
    assert readings[0].unit == "phi"
    # The raw word survives so a corrected scale can be re-run over history.
    assert readings[0].payload == bytes.fromhex("4a80")


def test_fuel_cut_is_reported_not_dropped() -> None:
    """ "Polled, commanding nothing" must not look like "not polled"."""
    response = _response("0000")

    readings = _decode_response(_request(), response, timestamp=1.0)

    assert [r.pid for r in readings] == ["mem:ECM_TARGET_EQUIV_RATIO_SENTINEL"]
    assert "fuel cut" in str(readings[0].value)


def test_short_response_decodes_to_nothing() -> None:
    response = _response("40")
    assert _decode_response(_request(), response, timestamp=1.0) == []
