"""Ad-hoc read-only OBD/UDS diagnostic request (``obd_request`` tool).

Backs ``POST /control/obd_request``. Every field trip so far that needed a
one-off diagnostic read (a Mode 06 MID, a calibration-ID Mode 09 InfoType, an
unbound Mode 22 DID) meant arming passive mode, SSHing to the Pi, and
hand-writing a script against :class:`~src.collection.can_obd_client.CanObdClient`.
This module is that script, made callable.

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
from functools import lru_cache
from pathlib import Path
from typing import Any

import can

from src.collection.can_collector import bus_factory
from src.collection.can_obd_client import (
    ALLOWED_QUERY_SERVICES,
    DEFAULT_REQUEST_ID,
    PID_DECODERS,
    CanObdClient,
    DisallowedServiceError,
    parse_dtc_payload,
)
from src.config import CanBusConfig
from src.decode.dids import DidMap, apply_scaling, decode_did_raw, did_entries_by_hex, load_did_map

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 2.0
#: Cap independent of caller input — the control route is synchronous and a
#: runaway timeout would tie up an HTTP worker for no benefit; no read this
#: tool supports needs longer than this to answer or fail.
MAX_TIMEOUT_SECONDS = 10.0

#: DTC-shaped services (Mode 03/07/0A) all share the count-byte + 2-byte-per-code
#: wire format `parse_dtc_payload` decodes.
_DTC_SERVICES = frozenset({0x03, 0x07, 0x0A})

_DIDS_YAML_PATH = Path(__file__).resolve().parents[2] / "data" / "dids.yaml"


class AdHocRequestBusError(RuntimeError):
    """The bus couldn't be opened (port already exclusively held, etc.)."""


@lru_cache(maxsize=1)
def _load_did_map() -> DidMap | None:
    """Parsed ``data/dids.yaml``, or ``None`` when absent/unparseable.

    Mirrors ``obd_scheduler._load_did_map`` — same file, same "missing
    metadata degrades gracefully" contract — kept as a separate small copy
    rather than a cross-module import of that function's leading-underscore
    name.
    """
    if not _DIDS_YAML_PATH.exists():
        return None
    try:
        return load_did_map(_DIDS_YAML_PATH)
    except Exception:  # noqa: BLE001 — bad YAML shouldn't break an ad-hoc read
        return None


@dataclass(frozen=True)
class AdHocRequestResult:
    """One ``obd_request`` outcome — always returned, even on no response."""

    service: int
    identifier: int | None
    request_id: int
    responded: bool
    responder_id: int | None = None
    #: Response payload (bytes after the service/PID echo), hex-encoded.
    raw_hex: str | None = None
    #: Best-effort decode when a decoder/catalog entry exists for
    #: ``(service, identifier)``; ``None`` when nothing recognizes it — the
    #: raw bytes are still there for the caller to interpret.
    decoded: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
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
        }


def execute_ad_hoc_request(
    can_config: CanBusConfig,
    *,
    service: int,
    identifier: int | None,
    request_id: int | None = None,
    timeout_seconds: float | None = None,
) -> AdHocRequestResult:
    """Send one read-only OBD/UDS request and return whatever came back.

    Raises :class:`~src.collection.can_obd_client.DisallowedServiceError` for
    a service outside :data:`~src.collection.can_obd_client.ALLOWED_QUERY_SERVICES`
    (checked here too, before touching the bus, so a rejected request never
    opens a handle) and :class:`AdHocRequestBusError` when the bus can't be
    opened at all.
    """
    if service not in ALLOWED_QUERY_SERVICES:
        allowed = ", ".join(f"0x{s:02X}" for s in sorted(ALLOWED_QUERY_SERVICES))
        raise DisallowedServiceError(
            f"service 0x{service:02X} is not read-only allowlisted; allowed: {allowed}"
        )
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
        response = client.query(service=service, identifier=identifier, request_id=request_id)
    finally:
        bus.shutdown()

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
        did_map = _load_did_map()
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
