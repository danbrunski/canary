"""D03.18 — a lost ISO-TP continuation ends at N_Cr, not at the request deadline.

0.5 % of the ECM's multi-frame replies on 2026-09-23 stopped after the First
Frame (or a CF), and each held its request for the full 1.0 s timeout; two in
a row left a 2.48 s hole in Mode 01. ``await_response`` now bounds the wait for
each Consecutive Frame, re-sends flow control once when a First Frame draws no
CF at all, and reports the loss by name.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.collection.isotp import (
    CONSECUTIVE_FRAME_TIMEOUT_SECONDS,
    IsoTpResponseAssembler,
    await_response,
)

REQUEST = 0x7E0
RESPONDER = 0x7E8
N_CR = CONSECUTIVE_FRAME_TIMEOUT_SECONDS
#: 62 11 01 + 17 data bytes = 20: a First Frame (6) and two CFs (7 + 7).
BODY = bytes([0x62, 0x11, 0x01, *range(17)])
FF = bytes([0x10, len(BODY), *BODY[:6]])
CF1 = bytes([0x21, *BODY[6:13]])
CF2 = bytes([0x22, *BODY[13:20]])


@dataclass
class Frame:
    arbitration_id: int
    data: bytes


class Wire:
    """Frames scheduled at fixed times on a fake clock; ``recv`` advances it."""

    def __init__(self, events: list[tuple[float, bytes]]) -> None:
        self.now = 0.0
        self.events = sorted(events)
        self.flow_controls: list[float] = []

    def clock(self) -> float:
        return self.now

    def recv(self, timeout: float) -> Frame | None:
        if self.events and self.events[0][0] <= self.now + timeout:
            at, data = self.events.pop(0)
            self.now = max(self.now, at)
            return Frame(RESPONDER, data)
        self.now += timeout
        return None

    def send_flow_control(self, _arbitration_id: int) -> None:
        self.flow_controls.append(self.now)


def _exchange(wire: Wire, **kwargs: float | None):  # type: ignore[no-untyped-def]
    return await_response(
        wire.recv,
        wire.send_flow_control,
        IsoTpResponseAssembler(service=0x22, identifier=0x1101),
        REQUEST,
        1.0,
        clock=wire.clock,
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_healthy_multi_frame_response_is_unaffected() -> None:
    wire = Wire([(0.010, FF), (0.013, CF1), (0.016, CF2)])
    exchange = _exchange(wire)
    assert exchange.response is not None
    assert exchange.response.payload == BODY[3:]
    assert (exchange.continuation_lost, exchange.flow_control_resent) == (False, False)
    assert wire.flow_controls == [0.010]


def test_a_first_frame_with_no_cf_resends_flow_control_once_then_ends_early() -> None:
    wire = Wire([(0.010, FF)])
    exchange = _exchange(wire)
    assert exchange.response is None
    assert exchange.continuation_lost is True
    assert exchange.flow_control_resent is True
    assert exchange.first_frame is not None
    # Flow control at the FF and once more N_Cr later; the request ends after
    # a second N_Cr, not at its 1.0 s deadline.
    assert wire.flow_controls == pytest.approx([0.010, 0.010 + N_CR])
    assert wire.now == pytest.approx(0.010 + 2 * N_CR)
    assert wire.now < 0.5


def test_a_resent_flow_control_can_still_rescue_the_response() -> None:
    """Ours may not have reached the ECM, which waits its own N_Bs for it."""
    resend_at = 0.010 + N_CR
    wire = Wire([(0.010, FF), (resend_at + 0.003, CF1), (resend_at + 0.006, CF2)])
    exchange = _exchange(wire)
    assert exchange.response is not None
    assert exchange.response.payload == BODY[3:]
    assert exchange.flow_control_resent is True
    assert exchange.continuation_lost is False


def test_a_cf_lost_mid_response_ends_at_n_cr_without_a_resend() -> None:
    """CFs were flowing, so flow control evidently arrived: resending it
    would not bring CF2 back (the 2026-09-23 02.422 case: receive-side loss)."""
    wire = Wire([(0.010, FF), (0.013, CF1)])
    exchange = _exchange(wire)
    assert exchange.response is None
    assert exchange.continuation_lost is True
    assert exchange.flow_control_resent is False
    assert wire.flow_controls == [0.010]
    assert wire.now == pytest.approx(0.013 + N_CR)


def test_no_bound_waits_to_the_deadline_as_before() -> None:
    wire = Wire([(0.010, FF)])
    exchange = _exchange(wire, consecutive_frame_timeout=None)
    assert exchange.response is None
    assert exchange.continuation_lost is True  # still named, just not early
    assert exchange.flow_control_resent is False
    assert wire.now == pytest.approx(1.0)


def test_silence_and_single_frames_are_not_continuation_losses() -> None:
    silent = _exchange(Wire([]))
    assert (silent.response, silent.continuation_lost) == (None, False)
    single = _exchange(Wire([(0.01, bytes([0x05, 0x62, 0x11, 0x01, 0xAA, 0xBB, 0, 0]))]))
    assert single.response is not None
    assert single.continuation_lost is False
