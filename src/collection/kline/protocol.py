"""KWP2000 over K-line (ISO 14230), at the wire: frames, services, descriptors.

Pure: no serial port, no timing, no GPIO. Built from what the CANary-HAT saw on
the 2008 Xterra's K-line on 2026-09-23, when it sniffed UpRev's session and
then held one of its own (``docs/uprev-usb-kline-research.md``, "On-wire
view"; the frames are fixtures in ``tests/fixtures/kline/``).

**Frames.** During initialisation a frame is *addressed*: a format byte
(``0x80`` physical or ``0xC0`` functional, low six bits the payload length),
target, source, payload, checksum. After StartCommunication this ECM uses
*length-byte* frames: one length byte, the payload, the checksum. The
checksum is the sum of every preceding byte of the frame, modulo 256.

**Dynamic local identifiers.** Service ``0xAC`` (this ECM's; ISO 14230's is
``0x2C``) defines a local id as an ordered list of descriptors: ``02 hh ll``
(a DID), ``83 aa aa aa aa`` (one RAM byte at a 32-bit address) or ``01 lid``
(everything another local id holds, first). ``21 lid`` then returns every
value, concatenated in descriptor order, with nothing marking where one ends.
Widths therefore come from the catalog (``data/dids.yaml``); an unknown width
is refused rather than guessed, because one wrong boundary misattributes every
later value. Duplicate descriptors are legal on the wire (UpRev defines RAM
``0xFFFF13FE`` twice); only the builders for *our* requests refuse them.

**What this client may ask for.** Reads, session control, volatile
definitions, reading stored codes and ClearDiagnosticInformation
(:data:`ALLOWED_SERVICES`; reading and clearing codes are routine diagnostic
work on any module, AGENTS.md). Every other service id is refused
here, by id, with the reason — there is no raw-request escape hatch. Writing
an ECU's non-volatile memory needs the operator's explicit permission per
operation and a parked, engine-off vehicle (AGENTS.md), and nothing in this
module can do it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

SID_START_DIAGNOSTIC_SESSION = 0x10
#: Clears a module's stored codes. Sent only as a vehicle profile records the
#: module's scan-tool clear (``KlineModule.clear_codes``).
SID_CLEAR_DIAGNOSTIC_INFORMATION = 0x14
SID_READ_ECU_IDENTIFICATION = 0x1A
#: Reads a module's stored codes: ``A2 <group>`` answered ``E2 <n> [hi lo]×n``.
#: A manufacturer-range service, seen from a scan tool reading the Xterra's
#: airbag module on 2026-10-08 (``data/analyses/2026-10-08-srs-kline-code-read.md``).
#: Sent only as a vehicle profile records a module's code read
#: (``KlineModule.read_codes``).
SID_READ_STORED_CODES = 0xA2
SID_READ_DATA_BY_LOCAL_ID = 0x21
SID_READ_DATA_BY_COMMON_ID = 0x22
SID_READ_MEMORY_BY_ADDRESS = 0x23
SID_TESTER_PRESENT = 0x3E
SID_START_COMMUNICATION = 0x81
SID_STOP_COMMUNICATION = 0x82
#: Dynamically define a local identifier — this ECM's service id.
SID_DEFINE_LOCAL_ID = 0xAC
SID_NEGATIVE_RESPONSE = 0x7F
POSITIVE_OFFSET = 0x40
#: Seen from a scan tool on 2026-09-24 (the ABS's steering-angle calibration).
#: Named for reports only: this client never sends it (not on the allowlist).
SID_IO_CONTROL_BY_LOCAL_ID = 0x30

#: Services this client may send. Everything else is refused by id.
ALLOWED_SERVICES: frozenset[int] = frozenset(
    {
        SID_START_DIAGNOSTIC_SESSION,
        SID_CLEAR_DIAGNOSTIC_INFORMATION,
        SID_READ_ECU_IDENTIFICATION,
        SID_READ_STORED_CODES,
        SID_READ_DATA_BY_LOCAL_ID,
        SID_READ_DATA_BY_COMMON_ID,
        SID_READ_MEMORY_BY_ADDRESS,
        SID_TESTER_PRESENT,
        SID_START_COMMUNICATION,
        SID_STOP_COMMUNICATION,
        SID_DEFINE_LOCAL_ID,
    }
)
#: Services a profile's ``read_codes`` recipe may name: each one's reply is
#: laid out as :func:`split_code_reply` reads it. A new code-read service needs
#: its reply layout seen on the wire first, and an entry here.
CODE_READ_SERVICES: frozenset[int] = frozenset({SID_READ_STORED_CODES})
#: Allowed services with sub-functions that are still refused: the
#: programming session exists only to flash, and nothing here flashes.
REFUSED_SUBFUNCTIONS: Mapping[int, frozenset[int]] = {
    SID_START_DIAGNOSTIC_SESSION: frozenset({0x85}),
}
#: Named non-volatile write services, for a precise refusal message.
WRITE_SERVICES: Mapping[int, str] = {
    0x27: "SecurityAccess",
    0x2E: "WriteDataByCommonIdentifier",
    0x31: "StartRoutineByLocalIdentifier",
    0x34: "RequestDownload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x3B: "WriteDataByLocalIdentifier",
    0x3D: "WriteMemoryByAddress",
}

#: Every service id a report may meet, by name. Naming is not permission: what
#: this client may *send* is :data:`ALLOWED_SERVICES`.
SERVICE_NAMES: Mapping[int, str] = {
    SID_START_DIAGNOSTIC_SESSION: "StartDiagnosticSession",
    0x11: "ECUReset",
    SID_CLEAR_DIAGNOSTIC_INFORMATION: "ClearDiagnosticInformation",
    0x17: "ReadStatusOfDiagnosticTroubleCodes",
    0x18: "ReadDiagnosticTroubleCodesByStatus",
    SID_READ_ECU_IDENTIFICATION: "ReadEcuIdentification",
    0x20: "StopDiagnosticSession",
    SID_READ_DATA_BY_LOCAL_ID: "ReadDataByLocalIdentifier",
    SID_READ_DATA_BY_COMMON_ID: "ReadDataByCommonIdentifier",
    SID_READ_MEMORY_BY_ADDRESS: "ReadMemoryByAddress",
    0x27: "SecurityAccess",
    0x2E: "WriteDataByCommonIdentifier",
    0x2F: "InputOutputControlByCommonIdentifier",
    SID_IO_CONTROL_BY_LOCAL_ID: "InputOutputControlByLocalIdentifier",
    0x31: "StartRoutineByLocalIdentifier",
    0x3B: "WriteDataByLocalIdentifier",
    SID_TESTER_PRESENT: "TesterPresent",
    SID_START_COMMUNICATION: "StartCommunication",
    SID_STOP_COMMUNICATION: "StopCommunication",
    0x83: "AccessTimingParameters",
    SID_READ_STORED_CODES: "ReadStoredCodes",
    SID_DEFINE_LOCAL_ID: "DynamicallyDefineLocalIdentifier",
}

NEGATIVE_RESPONSE_CODES: Mapping[int, str] = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported-invalidFormat",
    0x21: "busy-repeatRequest",
    0x22: "conditionsNotCorrect-requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x78: "requestCorrectlyReceived-responsePending",
}

TAG_INCLUDE = 0x01
TAG_DID = 0x02
TAG_MEMORY_BYTE = 0x83
ADDRESSED_MAX_PAYLOAD = 0x3F
LENGTH_FRAME_MAX_PAYLOAD = 0xFF


class KlineProtocolError(ValueError):
    """A malformed frame, descriptor or reply."""


class ServiceRefusedError(KlineProtocolError):
    """This client will not send that service (AGENTS.md write policy)."""


class NegativeResponseError(Exception):
    """The ECU answered ``7F <sid> <nrc>``."""

    def __init__(self, service: int, code: int) -> None:
        self.service = service
        self.code = code
        name = NEGATIVE_RESPONSE_CODES.get(code, "unknown")
        super().__init__(f"ECU refused service 0x{service:02x}: NRC 0x{code:02x} ({name})")

    def __reduce__(self) -> tuple[object, ...]:
        # Rebuilt from its constructor's arguments, so it survives a process
        # boundary (pickle); tests/test_exceptions_pickle.py holds every class to it.
        return (type(self), (self.service, self.code), self.__dict__)


# --------------------------------------------------------------------------- frames


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


@dataclass(frozen=True)
class AddressedFrame:
    target: int
    source: int
    payload: bytes
    functional: bool = False


def encode_addressed(frame: AddressedFrame) -> bytes:
    if not 1 <= len(frame.payload) <= ADDRESSED_MAX_PAYLOAD:
        raise KlineProtocolError(f"addressed payload of {len(frame.payload)} bytes (1..63)")
    fmt = (0xC0 if frame.functional else 0x80) | len(frame.payload)
    body = bytes([fmt, frame.target & 0xFF, frame.source & 0xFF]) + frame.payload
    return body + bytes([checksum(body)])


def decode_addressed(raw: bytes) -> AddressedFrame:
    if len(raw) < 5:
        raise KlineProtocolError(f"addressed frame too short: {raw.hex(' ')}")
    fmt = raw[0]
    if fmt & 0xC0 not in (0x80, 0xC0):
        raise KlineProtocolError(f"not an addressed frame (format 0x{fmt:02x})")
    length = fmt & 0x3F
    if len(raw) != length + 4:
        raise KlineProtocolError(
            f"addressed frame declares {length} payload bytes, has {len(raw) - 4}"
        )
    if checksum(raw[:-1]) != raw[-1]:
        raise KlineProtocolError(f"bad checksum: {raw.hex(' ')}")
    return AddressedFrame(raw[1], raw[2], bytes(raw[3:-1]), functional=fmt & 0xC0 == 0xC0)


def encode_length_frame(payload: bytes) -> bytes:
    if not 1 <= len(payload) <= LENGTH_FRAME_MAX_PAYLOAD:
        raise KlineProtocolError(f"payload of {len(payload)} bytes (1..255)")
    body = bytes([len(payload)]) + payload
    return body + bytes([checksum(body)])


def decode_length_frame(raw: bytes) -> bytes:
    if len(raw) < 3:
        raise KlineProtocolError(f"length frame too short: {raw.hex(' ')}")
    if raw[0] != len(raw) - 2:
        raise KlineProtocolError(
            f"length byte says {raw[0]} payload bytes, frame has {len(raw) - 2}"
        )
    if checksum(raw[:-1]) != raw[-1]:
        raise KlineProtocolError(f"bad checksum: {raw.hex(' ')}")
    return bytes(raw[1:-1])


# --------------------------------------------------------------------------- services


def check_request(payload: bytes) -> None:
    """Refuse anything but the read/session/define/clear-codes services, by id."""
    if not payload:
        raise KlineProtocolError("empty request")
    service = payload[0]
    if service in WRITE_SERVICES:
        raise ServiceRefusedError(
            f"service 0x{service:02x} ({WRITE_SERVICES[service]}) is refused: this client "
            "does not write ECU memory; non-volatile writes need the operator's explicit "
            "permission per operation and a parked, engine-off vehicle (AGENTS.md)"
        )
    if service not in ALLOWED_SERVICES:
        raise ServiceRefusedError(
            f"service 0x{service:02x} is not on this client's read-only allowlist"
        )
    refused = REFUSED_SUBFUNCTIONS.get(service, frozenset())
    if len(payload) > 1 and payload[1] in refused:
        raise ServiceRefusedError(
            f"service 0x{service:02x} sub-function 0x{payload[1]:02x} is refused "
            "(the programming session exists only to flash)"
        )


def is_response_sid(service: int) -> bool:
    """Whether a frame's service byte is a reply's (ISO 14230-3).

    A positive reply is the request's id plus ``0x40`` and a negative one is
    ``7F``; request ids live in ``0x00-0x3F`` and ``0x80-0xBF``, so bit 6 alone
    says which way a frame went — without knowing the request it answers.
    """
    return bool(service & POSITIVE_OFFSET)


def expect_positive(request_service: int, reply: bytes) -> bytes:
    """The reply's parameters after its service byte, or raise.

    ``7F <sid> <nrc>`` raises :class:`NegativeResponseError`; any other service
    byte than ``request_service + 0x40`` is a protocol error.
    """
    if not reply:
        raise KlineProtocolError("empty reply")
    if reply[0] == SID_NEGATIVE_RESPONSE:
        if len(reply) < 3:
            raise KlineProtocolError(f"short negative response: {reply.hex(' ')}")
        raise NegativeResponseError(reply[1], reply[2])
    expected = (request_service + POSITIVE_OFFSET) & 0xFF
    if reply[0] != expected:
        raise KlineProtocolError(
            f"reply service 0x{reply[0]:02x} to request 0x{request_service:02x} "
            f"(expected 0x{expected:02x})"
        )
    return bytes(reply[1:])


def start_communication(target: int, source: int) -> bytes:
    return encode_addressed(AddressedFrame(target, source, bytes([SID_START_COMMUNICATION])))


@dataclass(frozen=True)
class StartCommunicationReply:
    target: int
    source: int
    key_bytes: bytes


def parse_start_communication_reply(raw: bytes) -> StartCommunicationReply:
    frame = decode_addressed(raw)
    params = expect_positive(SID_START_COMMUNICATION, frame.payload)
    if len(params) < 2:
        raise KlineProtocolError(f"StartCommunication reply without key bytes: {raw.hex(' ')}")
    return StartCommunicationReply(frame.target, frame.source, bytes(params[:2]))


# --------------------------------------------------------------------------- descriptors


@dataclass(frozen=True)
class DidDescriptor:
    did: int


@dataclass(frozen=True)
class MemoryByteDescriptor:
    address: int


@dataclass(frozen=True)
class IncludeDescriptor:
    local_id: int


Descriptor: TypeAlias = DidDescriptor | MemoryByteDescriptor | IncludeDescriptor
ValueDescriptor: TypeAlias = DidDescriptor | MemoryByteDescriptor


def encode_descriptors(descriptors: Sequence[Descriptor]) -> bytes:
    out = bytearray()
    for descriptor in descriptors:
        if isinstance(descriptor, DidDescriptor):
            if not 0 <= descriptor.did <= 0xFFFF:
                raise KlineProtocolError(f"DID out of range: 0x{descriptor.did:x}")
            out += bytes([TAG_DID]) + descriptor.did.to_bytes(2, "big")
        elif isinstance(descriptor, MemoryByteDescriptor):
            if not 0 <= descriptor.address <= 0xFFFFFFFF:
                raise KlineProtocolError(f"address out of range: 0x{descriptor.address:x}")
            out += bytes([TAG_MEMORY_BYTE]) + descriptor.address.to_bytes(4, "big")
        else:
            out += bytes([TAG_INCLUDE, descriptor.local_id & 0xFF])
    return bytes(out)


def decode_descriptors(data: bytes) -> tuple[Descriptor, ...]:
    """Parse descriptors in order. Duplicates are kept: they are legal."""
    out: list[Descriptor] = []
    offset = 0
    while offset < len(data):
        tag = data[offset]
        if tag == TAG_DID:
            end = offset + 3
            if end > len(data):
                raise KlineProtocolError(f"truncated DID descriptor at offset {offset}")
            out.append(DidDescriptor(int.from_bytes(data[offset + 1 : end], "big")))
        elif tag == TAG_MEMORY_BYTE:
            end = offset + 5
            if end > len(data):
                raise KlineProtocolError(f"truncated memory descriptor at offset {offset}")
            out.append(MemoryByteDescriptor(int.from_bytes(data[offset + 1 : end], "big")))
        elif tag == TAG_INCLUDE:
            end = offset + 2
            if end > len(data):
                raise KlineProtocolError(f"truncated include descriptor at offset {offset}")
            out.append(IncludeDescriptor(data[offset + 1]))
        else:
            raise KlineProtocolError(f"unknown descriptor tag 0x{tag:02x} at offset {offset}")
        offset = end
    return tuple(out)


def define_local_id(local_id: int, descriptors: Sequence[Descriptor]) -> bytes:
    """Request payload defining ``local_id``. Our own definitions refuse
    duplicates: a duplicate costs bus time and says nothing new."""
    if not descriptors:
        raise KlineProtocolError("a local id needs at least one descriptor")
    if len(set(descriptors)) != len(descriptors):
        raise KlineProtocolError("definition repeats a descriptor")
    return bytes([SID_DEFINE_LOCAL_ID, local_id & 0xFF]) + encode_descriptors(descriptors)


def parse_define_local_id(payload: bytes) -> tuple[int, tuple[Descriptor, ...]]:
    if len(payload) < 3 or payload[0] != SID_DEFINE_LOCAL_ID:
        raise KlineProtocolError(f"not a local-id definition: {payload.hex(' ')}")
    return payload[1], decode_descriptors(payload[2:])


def expand_local_id(
    local_id: int, definitions: Mapping[int, Sequence[Descriptor]]
) -> tuple[ValueDescriptor, ...]:
    """Every value a ``21 local_id`` reply carries, in order, includes resolved."""
    out: list[ValueDescriptor] = []

    def visit(lid: int, stack: tuple[int, ...]) -> None:
        if lid in stack:
            raise KlineProtocolError(f"local id 0x{lid:02x} includes itself: {stack}")
        if lid not in definitions:
            raise KlineProtocolError(f"local id 0x{lid:02x} was never defined")
        for descriptor in definitions[lid]:
            if isinstance(descriptor, IncludeDescriptor):
                visit(descriptor.local_id, (*stack, lid))
            else:
                out.append(descriptor)

    visit(local_id, ())
    return tuple(out)


def value_widths(
    descriptors: Sequence[ValueDescriptor], did_widths: Mapping[int, int]
) -> tuple[int, ...]:
    """Bytes each value occupies. A RAM descriptor is one byte; a DID's width
    comes from the catalog, and an unknown one is refused."""
    widths: list[int] = []
    for descriptor in descriptors:
        if isinstance(descriptor, MemoryByteDescriptor):
            widths.append(1)
            continue
        width = did_widths.get(descriptor.did)
        if width is None or width <= 0:
            raise KlineProtocolError(f"no catalog width for DID 0x{descriptor.did:04x}")
        widths.append(width)
    return tuple(widths)


def read_local_id(local_id: int, trailer: bytes = b"") -> bytes:
    """``21 lid`` request payload; ``trailer`` is the vehicle profile's."""
    return bytes([SID_READ_DATA_BY_LOCAL_ID, local_id & 0xFF]) + trailer


def split_local_id_reply(local_id: int, params: bytes, widths: Sequence[int]) -> tuple[bytes, ...]:
    """Split a positive ``61 lid ...`` reply's parameters into value bytes."""
    if not params or params[0] != local_id:
        raise KlineProtocolError(
            f"reply for local id 0x{params[0] if params else -1:02x}, asked 0x{local_id:02x}"
        )
    data = params[1:]
    if len(data) != sum(widths):
        raise KlineProtocolError(
            f"local id 0x{local_id:02x} carried {len(data)} bytes; the definition "
            f"needs {sum(widths)}"
        )
    values: list[bytes] = []
    offset = 0
    for width in widths:
        values.append(bytes(data[offset : offset + width]))
        offset += width
    return tuple(values)


def read_did(did: int, trailer: bytes = b"") -> bytes:
    return bytes([SID_READ_DATA_BY_COMMON_ID]) + did.to_bytes(2, "big") + trailer


def split_did_reply(did: int, params: bytes) -> bytes:
    """A positive ``62 hh ll value...`` reply's value bytes."""
    if len(params) < 3 or int.from_bytes(params[:2], "big") != did:
        raise KlineProtocolError(f"reply is not for DID 0x{did:04x}: {params.hex(' ')}")
    return bytes(params[2:])


# --------------------------------------------------------------------------- codes

#: The letter an SAE J2012 code word's top two bits give.
CODE_LETTERS = "PCBU"


def decode_code_word(word: int) -> str:
    """A two-byte code word as its SAE J2012 code: the top two bits are the
    letter (``00`` P, ``01`` C, ``10`` B, ``11`` U), the remaining fourteen
    bits four hex digits. ``0x9023`` is ``B1023``."""
    if not 0 <= word <= 0xFFFF:
        raise KlineProtocolError(f"code word 0x{word:x} is not two bytes")
    return f"{CODE_LETTERS[word >> 14]}{(word >> 12) & 0x3:X}{word & 0x0FFF:03X}"


def split_code_reply(params: bytes) -> tuple[int, ...]:
    """The code words of a positive code-read reply, after its service byte:
    ``<n> [hi lo]×n``. An empty tuple is a module that read and holds none.

    A reply whose length does not match its own count is refused: one missing
    byte would shift every later code into a different, plausible-looking one.
    """
    if not params:
        raise KlineProtocolError("code reply without a count byte")
    count = params[0]
    words = params[1:]
    if len(words) != 2 * count:
        raise KlineProtocolError(
            f"code reply says {count} code(s) ({2 * count} bytes) but carries "
            f"{len(words)}: {params.hex(' ')}"
        )
    return tuple(int.from_bytes(words[i : i + 2], "big") for i in range(0, len(words), 2))


__all__ = [
    "ALLOWED_SERVICES",
    "CODE_LETTERS",
    "CODE_READ_SERVICES",
    "SERVICE_NAMES",
    "AddressedFrame",
    "Descriptor",
    "DidDescriptor",
    "IncludeDescriptor",
    "KlineProtocolError",
    "MemoryByteDescriptor",
    "NegativeResponseError",
    "ServiceRefusedError",
    "StartCommunicationReply",
    "ValueDescriptor",
    "check_request",
    "checksum",
    "decode_addressed",
    "decode_code_word",
    "decode_descriptors",
    "decode_length_frame",
    "define_local_id",
    "encode_addressed",
    "encode_descriptors",
    "encode_length_frame",
    "expand_local_id",
    "expect_positive",
    "is_response_sid",
    "parse_define_local_id",
    "parse_start_communication_reply",
    "read_did",
    "read_local_id",
    "split_code_reply",
    "split_did_reply",
    "split_local_id_reply",
    "start_communication",
    "value_widths",
]
