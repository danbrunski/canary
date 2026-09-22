"""Reading the ECU's own calibration id over the Canable poll loop (W02.2).

The id comes from service ``0x21`` (ReadDataByLocalIdentifier) and is ASCII:
``1ZS44A00000`` on the dev vehicle's ZS44A flashes. Which local identifier and
which diagnostic session it needs are vehicle facts, so they come from the
``access.calibration_id`` block of ``data/dids.yaml`` — nothing here names a
vehicle.

When the read needs the Mode-22 session (``extended``, or ``unverified`` —
only ever observed inside it), it rides a DID window the scheduler opens
anyway; it never opens a session of its own, so it cannot cause a Mode-01
blackout. When a probe has recorded ``default``, it is read bare as soon as
the loop connects.

It is read at connect, retried until it answers (a key-off boot has a
sleeping ECM), and re-read every few minutes after that: a reflash happens
under a running daemon with the Pi's bus never dropping, and the whole point
of W02.3 is noticing that without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.decode.dids import DidAccess

#: The service byte. A read; it cannot alter the ECU.
READ_LOCAL_IDENTIFIER_SERVICE = 0x21
#: Unanswered read (ECM asleep, or no window has opened yet): try again after.
CALIBRATION_ID_RETRY_SECONDS = 30.0
#: After an answer, re-read this often so a reflash under a running daemon is
#: noticed within minutes. One request per interval.
CALIBRATION_ID_REFRESH_SECONDS = 300.0


@dataclass(frozen=True)
class CalibrationIdSpec:
    """What the poll loop needs to read the id, resolved from the access block."""

    local_identifier: int
    request_id: int
    #: True when the read must ride the Mode-22 session window.
    needs_session: bool
    retry_seconds: float = CALIBRATION_ID_RETRY_SECONDS
    refresh_seconds: float = CALIBRATION_ID_REFRESH_SECONDS


def resolve_calibration_id_spec(access: DidAccess | None) -> CalibrationIdSpec | None:
    """The loop's read policy, or ``None`` when the vehicle declares no id read."""
    if access is None or access.calibration_id is None:
        return None
    return CalibrationIdSpec(
        local_identifier=access.calibration_id.local_identifier,
        request_id=access.ecu_request_id,
        needs_session=access.calibration_id.needs_session,
    )


def decode_calibration_id(payload: bytes | None) -> str | None:
    """ASCII id from the bytes after the ``61 <lid>`` echo; ``None`` if nothing printable.

    Padding (NUL, 0xFF, spaces) is dropped; any other non-printable byte
    means this is not an ASCII id and the read is treated as unanswered
    rather than cached as garbage.
    """
    if not payload:
        return None
    body = bytes(payload).rstrip(b"\x00\xff ").lstrip(b"\x00 ")
    if not body or any(byte < 0x20 or byte > 0x7E for byte in body):
        return None
    return body.decode("ascii")


class CalibrationIdSchedule:
    """When the next read is owed, and in which session phase it may go out."""

    def __init__(self, spec: CalibrationIdSpec, *, now: float) -> None:
        self.spec = spec
        self.next_read_at = now
        self.reads_sent = 0
        self.answers = 0

    def due(self, now: float) -> bool:
        return now >= self.next_read_at

    def phase_allows(self, phase: str) -> bool:
        """``phase`` is the session controller's (``none`` without one).

        A session read goes out only while a window holds the session; a
        default-session read only while no window does.
        """
        if self.spec.needs_session:
            return phase == "window"
        return phase in ("none", "mode01")

    def on_result(self, calibration_id: str | None, *, now: float) -> None:
        self.reads_sent += 1
        if calibration_id is None:
            self.next_read_at = now + self.spec.retry_seconds
        else:
            self.answers += 1
            self.next_read_at = now + self.spec.refresh_seconds


__all__ = [
    "CALIBRATION_ID_REFRESH_SECONDS",
    "CALIBRATION_ID_RETRY_SECONDS",
    "READ_LOCAL_IDENTIFIER_SERVICE",
    "CalibrationIdSchedule",
    "CalibrationIdSpec",
    "decode_calibration_id",
    "resolve_calibration_id_spec",
]
