"""Ad-hoc OBD/UDS diagnostic or control request (``obd_request`` tool).

Backs ``POST /control/obd_request``. The service byte is intentionally
unfiltered: routine reads, session control, security access, memory reads, and
volatile control/actuation requests all use this path. CANary's boundary is no
ECU non-volatile-memory writes or reflashing. This module makes the same
one-shot client previously hand-written during field work callable through the
authenticated control plane.

Bus access model: rather than reach into the daemon's own collector thread
(which owns one long-lived ``Bus`` handle across its whole poll loop), each
call here opens its own short-lived ``python-can`` ``Bus`` on the same
interface, sends one request, and closes it. That's the same thing
``tools/scan_constant_dids.py`` and any other exploratory script already do
against this bus, and it works because SocketCAN (the deployed Waveshare HAT)
happily serves multiple concurrent handles on one interface — the daemon's
own polling is unaffected. A single-handle transport (``slcan``) can't do
that; opening a second handle there will raise, surfaced as
:class:`AdHocRequestBusError` rather than corrupting the daemon's stream.
Either way every ``bus.send`` — this module's included — still funnels
through :mod:`~src.collection.tx_gate`, so arming passive mode first (to keep
another tool's diagnostic session uncontended) works exactly as documented
there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import can

from src.collection.can_collector import bus_factory
from src.collection.can_obd_client import (
    DEFAULT_REQUEST_ID,
    PID_DECODERS,
    CanObdClient,
    parse_dtc_payload,
)
from src.collection.isotp import nrc_name
from src.collection.obd_scheduler import load_repo_did_map
from src.config import CanBusConfig
from src.decode.dids import apply_scaling, decode_did_raw, did_entries_by_hex

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 2.0
#: Cap independent of caller input — the control route is synchronous and a
#: runaway timeout would tie up an HTTP worker for no benefit; no read this
#: tool supports needs longer than this to answer or fail.
MAX_TIMEOUT_SECONDS = 10.0

#: DTC-shaped services (Mode 03/07/0A) all share the count-byte + 2-byte-per-code
#: wire format `parse_dtc_payload` decodes.
_DTC_SERVICES = frozenset({0x03, 0x07, 0x0A})


class AdHocRequestBusError(RuntimeError):
    """The bus couldn't be opened (port already exclusively held, etc.)."""


@dataclass(frozen=True)
class AdHocRequestResult:
    """One ``obd_request`` outcome — always returned, even on no response."""

    service: int
    identifier: int | None
    request_id: int
    #: The ECU answered — positively *or* with a negative response. Silence
    #: and an unsent request are the only ``False``. A refusal is an answer
    #: ("an empty result must say which kind of empty it is"): before
    #: 2026-09-22 an NRC was reported as ``responded: False`` and the W02.1
    #: probe could not tell "refused in this session" from "nothing on the
    #: bus".
    responded: bool
    responder_id: int | None = None
    #: Response payload (bytes after the service/PID echo), hex-encoded. For
    #: a negative response this is the whole ``7F <service> <nrc>`` reply.
    raw_hex: str | None = None
    #: Best-effort decode when a decoder/catalog entry exists for
    #: ``(service, identifier)``; ``None`` when nothing recognizes it — the
    #: raw bytes are still there for the caller to interpret.
    decoded: dict[str, Any] | None = None
    #: Set when the ECU refused: the NRC byte. ``responded`` is ``True``.
    negative_response_code: int | None = None

    @property
    def refused(self) -> bool:
        return self.negative_response_code is not None

    def to_dict(self) -> dict[str, Any]:
        nrc = self.negative_response_code
        return {
            "service": f"0x{self.service:02X}",
            "identifier": f"0x{self.identifier:04X}" if self.identifier is not None else None,
            "request_id": f"0x{self.request_id:03X}",
            "responded": self.responded,
            "responder_id": (
                f"0x{self.responder_id:03X}" if self.responder_id is not None else None
            ),
            "raw_hex": self.raw_hex,
            "decoded": self.decoded,
            "negative_response_code": f"0x{nrc:02X}" if nrc is not None else None,
            "negative_response": nrc_name(nrc) if nrc is not None else None,
        }


def execute_ad_hoc_request(
    can_config: CanBusConfig,
    *,
    service: int,
    identifier: int | None,
    request_id: int | None = None,
    timeout_seconds: float | None = None,
) -> AdHocRequestResult:
    """Send one OBD/UDS request and return whatever came back.

    ``service`` is passed through unfiltered — read, write and actuation
    services alike. Raises :class:`AdHocRequestBusError` when the bus can't
    be opened at all.
    """
    effective_timeout = min(
        MAX_TIMEOUT_SECONDS,
        DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else max(0.1, timeout_seconds),
    )
    try:
        bus = bus_factory(can_config)
    except (OSError, can.CanError) as exc:
        raise AdHocRequestBusError(
            f"couldn't open {can_config.interface}:{can_config.channel} — {exc}"
        ) from exc
    effective_request_id = DEFAULT_REQUEST_ID if request_id is None else request_id
    try:
        client = CanObdClient(bus, timeout_seconds=effective_timeout)
        response, nrc = client.query_or_nrc(
            service=service, identifier=identifier, request_id=request_id
        )
    finally:
        bus.shutdown()

    if response is None and nrc is not None:
        # Refused, not silent: the reply was ``7F <service> <nrc>``.
        return AdHocRequestResult(
            service=service,
            identifier=identifier,
            request_id=effective_request_id,
            responded=True,
            raw_hex=bytes([0x7F, service & 0xFF, nrc]).hex(),
            negative_response_code=nrc,
        )
    if response is None:
        return AdHocRequestResult(
            service=service,
            identifier=identifier,
            request_id=effective_request_id,
            responded=False,
        )
    return AdHocRequestResult(
        service=service,
        identifier=identifier,
        request_id=effective_request_id,
        responded=True,
        responder_id=response.responder_id,
        raw_hex=response.payload.hex(),
        decoded=_decode(service, identifier, response.payload),
    )


def _decode(service: int, identifier: int | None, payload: bytes) -> dict[str, Any] | None:
    if service in (0x01, 0x02) and identifier is not None:
        entry = PID_DECODERS.get(identifier)
        if entry is None:
            return None
        name, decoder = entry
        result = decoder(payload)
        if result is None:
            return None
        value, unit = result
        return {"channel": name, "value": value, "unit": unit}
    if service in _DTC_SERVICES:
        return {"dtcs": parse_dtc_payload(payload)}
    if service == 0x09:
        text = _try_ascii(payload)
        return {"ascii": text} if text is not None else None
    if service == 0x22 and identifier is not None:
        did_map = load_repo_did_map()
        if did_map is None:
            return None
        did_entry = did_entries_by_hex(did_map).get(f"{identifier:04X}")
        if did_entry is None:
            return None
        raw_int = decode_did_raw(did_entry, payload)
        if raw_int is None:
            return None
        scaled = apply_scaling(did_entry, raw_int)
        return {
            "name": did_entry.name,
            "value": scaled if scaled is not None else float(raw_int),
            "unit": did_entry.unit if scaled is not None else "raw",
        }
    return None


def _try_ascii(payload: bytes) -> str | None:
    """Decode InfoType payloads (VIN, calibration ID, ...) if they're text.

    Mode 09 responses are ASCII strings padded with trailing 0x00; anything
    else (a binary CVN, an OEM-specific blob) is left for the caller to read
    from ``raw_hex`` instead of forcing a garbled decode.
    """
    trimmed = payload.rstrip(b"\x00")
    if not trimmed or any(b < 0x20 or b > 0x7E for b in trimmed):
        return None
    return trimmed.decode("ascii")


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "AdHocRequestBusError",
    "AdHocRequestResult",
    "execute_ad_hoc_request",
]
