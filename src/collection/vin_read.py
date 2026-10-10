"""Read the VIN on demand through the running OBD collector (2026-10-10).

The dashboard's profile editor offers "read VIN from the connected vehicle".
The request goes out on the collector's own bus handle, between two of its
polls, rather than on a second handle opened beside it: the loop already owns
the diagnostic exchange, its receive tap records the reply like any other, and
a single-handle transport (``slcan``) cannot open a second one at all.

The control route hands a :class:`VinReadTicket` to this process-wide bridge
(:func:`submit`) and waits; ``run_can_obd_loop`` takes it on its next tick
(:func:`take`), sends Mode 09 InfoType 0x02 functionally (``0x7DF``), and
fulfils it. The loop only takes a ticket while it is polling, so every gate
that stops polling stops this read too: passive mode and an ECM-session holder
(the loop issues nothing then), a paused capture (the loop leaves the ticket
untaken, as it does the calibration-id read), and a parked or bench vehicle
with ``collection.obd_sleeps_with_bus`` (the loop is not running). It also waits
for its own bus-budget slot. The route checks those gates first and refuses by
name; a ticket nobody takes is reported as ``not_polling``, never as silence
from the ECU.

The VIN read here is returned, never saved: the operator confirms it in the
form.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Literal

#: Why a read produced no VIN. ``key_off``, ``passive_armed``, ``ecm_held``,
#: ``capture_paused``, ``replay`` and ``unsupported`` are refusals before
#: anything is sent.
VinReadRefusal = Literal[
    "unsupported",
    "replay",
    "passive_armed",
    "ecm_held",
    "capture_paused",
    "key_off",
    "not_polling",
    "no_answer",
    "refused",
    "implausible",
]

#: Mode 09 VehicleInfo, InfoType 0x02 (VIN).
VIN_SERVICE = 0x09
VIN_INFO_TYPE = 0x02


@dataclass(frozen=True)
class VinReadOutcome:
    """One VIN read: the VIN, or the named reason there is none."""

    vin: str | None
    refusal: VinReadRefusal | None
    detail: str
    #: The decoded reply text as received, when there was one.
    raw: str | None = None
    negative_response_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.vin is not None

    def to_dict(self) -> dict[str, Any]:
        nrc = self.negative_response_code
        return {
            "ok": self.ok,
            "vin": self.vin,
            "refusal": self.refusal,
            "detail": self.detail,
            "raw": self.raw,
            "negative_response_code": f"0x{nrc:02X}" if nrc is not None else None,
        }


class VinReadTicket:
    """One pending read, shared by every caller that asked while it waited."""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self.taken = False
        self._done = threading.Event()
        self._outcome: VinReadOutcome | None = None

    def fulfil(self, outcome: VinReadOutcome) -> None:
        self._outcome = outcome
        self._done.set()

    def wait(self, seconds: float) -> VinReadOutcome | None:
        """The outcome once fulfilled within ``seconds``, else ``None``."""
        if not self._done.wait(seconds):
            return None
        return self._outcome


_lock = threading.Lock()
_pending: VinReadTicket | None = None


def submit(timeout_seconds: float) -> VinReadTicket:
    """Queue a read for the collector, or join the one already queued."""
    global _pending
    with _lock:
        if _pending is None:
            _pending = VinReadTicket(timeout_seconds)
        return _pending


def take() -> VinReadTicket | None:
    """The loop's side: the queued ticket, now claimed, or ``None``."""
    global _pending
    with _lock:
        ticket, _pending = _pending, None
        if ticket is not None:
            ticket.taken = True
        return ticket


def withdraw(ticket: VinReadTicket) -> bool:
    """Drop ``ticket`` if no loop has taken it yet; whether it was dropped."""
    global _pending
    with _lock:
        if _pending is ticket and not ticket.taken:
            _pending = None
            return True
        return False


def request_vin(*, timeout_seconds: float, pickup_seconds: float) -> VinReadOutcome:
    """Ask the collector for the VIN and wait for its answer.

    ``pickup_seconds`` bounds the wait for the loop to take the ticket (it
    serves it between polls, outside a Mode 22 window); ``timeout_seconds``
    is the ECU's reply timeout, which the loop applies.
    """
    ticket = submit(timeout_seconds)
    outcome = ticket.wait(pickup_seconds)
    if outcome is None and not withdraw(ticket):
        # Taken: the request is on the wire; give the ECU its timeout.
        outcome = ticket.wait(timeout_seconds + 1.0)
    if outcome is not None:
        return outcome
    if ticket.taken:
        return VinReadOutcome(
            None, "no_answer", "the collector sent the request but did not finish it in time"
        )
    return VinReadOutcome(
        None,
        "not_polling",
        f"the OBD collector did not take the request within {pickup_seconds:.0f} s: it is not "
        "polling (stopped, reconnecting, or waiting for the vehicle to wake)",
    )


def outcome_from_reply(payload: bytes | None, negative_response_code: int | None) -> VinReadOutcome:
    """Classify one Mode 09 0x02 exchange into a :class:`VinReadOutcome`."""
    from src.collection.obd_registry import decode_vin

    if payload is None:
        if negative_response_code is not None:
            return VinReadOutcome(
                None,
                "refused",
                f"the ECU refused Mode 09 VIN (negative response 0x{negative_response_code:02X})",
                negative_response_code=negative_response_code,
            )
        return VinReadOutcome(
            None,
            "no_answer",
            "no answer to Mode 09 VIN: the ECU is asleep (key off) or does not report a VIN",
        )
    decoded = decode_vin(payload)
    text = decoded[0] if decoded is not None else ""
    vin = "".join(ch for ch in text if ch.isalnum()).upper()
    if len(vin) != 17:
        return VinReadOutcome(
            None,
            "implausible",
            f"the ECU answered with {text!r}, which is not a 17-character VIN",
            raw=text or payload.hex(),
        )
    return VinReadOutcome(vin, None, "read from the connected vehicle", raw=text)


def reset() -> None:
    """Drop any queued ticket (process-state reset between tests)."""
    global _pending
    with _lock:
        _pending = None


__all__ = [
    "VIN_INFO_TYPE",
    "VIN_SERVICE",
    "VinReadOutcome",
    "VinReadRefusal",
    "VinReadTicket",
    "outcome_from_reply",
    "request_vin",
    "reset",
    "submit",
    "take",
    "withdraw",
]
