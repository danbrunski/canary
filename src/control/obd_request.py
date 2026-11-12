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
    DEFAULT_RESPONSE_ID_MAX,
    DEFAULT_RESPONSE_ID_MIN,
    PID_DECODERS,
    CanObdClient,
    parse_dtc_payload,
)
from src.collection.isotp import (
    MAX_SINGLE_FRAME_REQUEST_BODY,
    build_body_request_payload,
    build_request_payload,
    nrc_name,
)
from src.config import CanBusConfig
from src.decode.dids import apply_scaling, decode_did_raw, did_entries_by_hex, load_repo_did_map

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 2.0
#: Cap independent of caller input — the control route is synchronous and a
#: runaway timeout would tie up an HTTP worker for no benefit; no read this
#: tool supports needs longer than this to answer or fail.
MAX_TIMEOUT_SECONDS = 10.0

#: DTC-shaped services (Mode 03/07/0A) all share the count-byte + 2-byte-per-code
#: wire format `parse_dtc_payload` decodes.
_DTC_SERVICES = frozenset({0x03, 0x07, 0x0A})

#: Widths an explicit identifier may take on the wire: one byte (a KWP
#: ReadDataByLocalIdentifier ``21 <lid>``, a Mode 01 PID) or two (a Mode 22
#: DID, including one below 0x100 such as ``22 00 05``).
IDENTIFIER_WIDTHS: tuple[int, ...] = (1, 2)


class AdHocRequestBusError(RuntimeError):
    """The bus couldn't be opened (port already exclusively held, etc.)."""


def parse_data_hex(data_hex: str | None) -> bytes | None:
    """``data_hex`` (``"1f"``, ``"FF 00"``) as bytes; ``None`` stays ``None``.

    Raises ``ValueError`` naming the field when it is not whole hex bytes.
    """
    if data_hex is None:
        return None
    try:
        return bytes.fromhex(data_hex)
    except ValueError as exc:
        raise ValueError(
            f"data_hex {data_hex!r} is not whole hex bytes (e.g. '1f' or 'ff 00')"
        ) from exc


def encode_request_body(
    *,
    identifier: int | None,
    identifier_bytes: int | None,
    data: bytes | None,
) -> bytes | None:
    """The exact bytes after the service byte, or ``None`` for the inferred form.

    ``None`` (neither ``identifier_bytes`` nor ``data`` given) is the request
    as it has always been encoded: the identifier's width follows its value
    (one byte up to 0xFF, two above), so every existing caller sends the same
    bytes as before. ``identifier_bytes`` fixes the width; ``data`` is the
    whole argument for a service that takes no identifier (or an arbitrary
    one). An identifier that does not fit its width is refused, never
    truncated. Raises ``ValueError``; the HTTP schema calls this, so a bad
    combination is a 422 naming the field rather than a malformed frame.
    """
    if data is not None:
        if identifier is not None or identifier_bytes is not None:
            raise ValueError(
                "data_hex is the whole request after the service byte; it cannot be "
                "combined with identifier or identifier_bytes"
            )
        if not data:
            raise ValueError(
                "data_hex is empty; omit it (and identifier) to send the service byte alone"
            )
        if len(data) > MAX_SINGLE_FRAME_REQUEST_BODY:
            raise ValueError(
                f"data_hex is {len(data)} bytes; a single-frame request carries at most "
                f"{MAX_SINGLE_FRAME_REQUEST_BODY} after the service byte"
            )
        return data
    if identifier_bytes is None:
        return None
    if identifier_bytes not in IDENTIFIER_WIDTHS:
        raise ValueError(
            f"identifier_bytes must be one of {IDENTIFIER_WIDTHS}, got {identifier_bytes}"
        )
    if identifier is None:
        raise ValueError("identifier_bytes needs an identifier to encode")
    limit = (1 << (8 * identifier_bytes)) - 1
    if not 0 <= identifier <= limit:
        raise ValueError(
            f"identifier 0x{identifier:X} does not fit identifier_bytes={identifier_bytes} "
            f"(at most 0x{limit:X}); it is refused, not truncated"
        )
    return identifier.to_bytes(identifier_bytes, "big")


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
    #: The explicit identifier width the caller asked for; ``None`` when the
    #: width was inferred from the value (the original encoding).
    identifier_bytes: int | None = None
    #: The request as sent, hex: the service byte and everything after it,
    #: without the ISO-TP length byte or the padding (``"211f"``).
    request_hex: str = ""
    #: The reply id the caller declared (``response_id``); ``None`` when the
    #: default response window applied.
    response_id: int | None = None
    #: Explicit-identifier form only: whether the positive reply echoed the
    #: identifier sent. ``True`` — ``raw_hex`` follows the echo; ``False`` —
    #: ``raw_hex`` is everything after the positive service byte and nothing
    #: is decoded. ``None`` when no echo was checked (the inferred form, which
    #: matches it while receiving; ``data_hex``; no positive reply).
    identifier_echoed: bool | None = None

    @property
    def refused(self) -> bool:
        return self.negative_response_code is not None

    def to_dict(self) -> dict[str, Any]:
        nrc = self.negative_response_code
        digits = 4 if self.identifier_bytes is None else 2 * self.identifier_bytes
        return {
            "service": f"0x{self.service:02X}",
            "identifier": (
                f"0x{self.identifier:0{digits}X}" if self.identifier is not None else None
            ),
            "identifier_bytes": self.identifier_bytes,
            "request_hex": self.request_hex,
            "request_id": f"0x{self.request_id:03X}",
            "response_id": (f"0x{self.response_id:03X}" if self.response_id is not None else None),
            "identifier_echoed": self.identifier_echoed,
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
    identifier_bytes: int | None = None,
    data: bytes | None = None,
    response_id: int | None = None,
) -> AdHocRequestResult:
    """Send one OBD/UDS request and return whatever came back.

    ``service`` is passed through unfiltered — read, write and actuation
    services alike. Raises :class:`AdHocRequestBusError` when the bus can't
    be opened at all, and ``ValueError`` (from :func:`encode_request_body`)
    for an identifier that does not fit ``identifier_bytes`` or a ``data``
    combined with an identifier.

    With neither ``identifier_bytes`` nor ``data`` the request is encoded as
    it always was (width inferred from the identifier's value). ``response_id``
    pins the reply to one arbitration id — needed for a module whose pairing
    is not ``request + 8`` (the BCM, 0x745 -> 0x765), whose reply is otherwise
    outside the default 0x7E8-0x7EF window and never heard; flow control for
    its multi-frame reply then goes to ``request_id``.
    """
    body = encode_request_body(identifier=identifier, identifier_bytes=identifier_bytes, data=data)
    effective_timeout = min(
        MAX_TIMEOUT_SECONDS,
        DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else max(0.1, timeout_seconds),
    )
    wire = (
        build_request_payload(service, identifier)
        if body is None
        else build_body_request_payload(service, body)
    )
    request_hex = wire[1 : 1 + wire[0]].hex()
    try:
        bus = bus_factory(can_config)
    except (OSError, can.CanError) as exc:
        raise AdHocRequestBusError(
            f"couldn't open {can_config.interface}:{can_config.channel} — {exc}"
        ) from exc
    effective_request_id = DEFAULT_REQUEST_ID if request_id is None else request_id
    # A declared reply id is a window of one, which also pins the responder
    # (``isotp.expected_responder``); otherwise the client's default window.
    lo, hi = (
        (DEFAULT_RESPONSE_ID_MIN, DEFAULT_RESPONSE_ID_MAX)
        if response_id is None
        else (response_id, response_id)
    )
    try:
        client = CanObdClient(
            bus, timeout_seconds=effective_timeout, response_id_min=lo, response_id_max=hi
        )
        if body is None:
            response, nrc = client.query_or_nrc(
                service=service, identifier=identifier, request_id=request_id
            )
        else:
            response, nrc = client.query_body_or_nrc(
                service=service, body=body, request_id=request_id
            )
    finally:
        bus.shutdown()

    common: dict[str, Any] = {
        "service": service,
        "identifier": identifier,
        "request_id": effective_request_id,
        "identifier_bytes": identifier_bytes,
        "request_hex": request_hex,
        "response_id": response_id,
    }
    if response is None and nrc is not None:
        # Refused, not silent: the reply was ``7F <service> <nrc>``.
        return AdHocRequestResult(
            **common,
            responded=True,
            raw_hex=bytes([0x7F, service & 0xFF, nrc]).hex(),
            negative_response_code=nrc,
        )
    if response is None:
        return AdHocRequestResult(**common, responded=False)
    payload = bytes(response.payload)
    echoed: bool | None = None
    decoded: dict[str, Any] | None = None
    if body is None:
        decoded = _decode(service, identifier, payload)
    elif identifier_bytes is not None:
        # The explicit-width form sends no echo to the assembler (its echo
        # matching infers the width from the value); check it here instead.
        echoed = payload.startswith(body)
        if echoed:
            payload = payload[len(body) :]
            decoded = _decode(service, identifier, payload)
    return AdHocRequestResult(
        **common,
        responded=True,
        responder_id=response.responder_id,
        raw_hex=payload.hex(),
        decoded=decoded,
        identifier_echoed=echoed,
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
    "IDENTIFIER_WIDTHS",
    "MAX_TIMEOUT_SECONDS",
    "AdHocRequestBusError",
    "AdHocRequestResult",
    "encode_request_body",
    "execute_ad_hoc_request",
    "parse_data_hex",
]
