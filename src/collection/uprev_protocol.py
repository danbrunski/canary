"""Pure protocol helpers for the UpRev/Cipher FTDI K-line cable.

This module deliberately contains no D2XX or USB backend. It models the
framing and diagnostic stream payloads recovered from USBPcap captures so a
Windows D2XX transport and a future Linux transport can share one parser.

The live-vehicle evidence is recorded in ``docs/uprev-usb-kline-research.md``.
None of the helpers here issue ECU programming or non-volatile writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

MAGIC = b"UPRV"
HEADER_LENGTH = 12

STREAM_DEFINITION_PREFIX = bytes.fromhex("0200ac81")
STREAM_POLL = bytes.fromhex("020021810401")
STREAM_RESPONSE_PREFIX = bytes.fromhex("66")
STREAM_DATA_PREFIX = bytes.fromhex("6181")
MEMORY_BYTE_DESCRIPTOR = 0x83

StreamDescriptor: TypeAlias = tuple[Literal["did", "memory_byte"], int]


class UprvProtocolError(ValueError):
    """Malformed or unsupported UpRev USB protocol data."""


@dataclass(frozen=True)
class UprvMessage:
    """One checksum-validated UPRV envelope."""

    payload: bytes
    reserved: bytes = b"\x00\x00\x00\x00"


def encode_message(payload: bytes, *, reserved: bytes = b"\x00\x00\x00\x00") -> bytes:
    """Wrap one cable payload in the captured 12-byte UPRV envelope."""
    if len(payload) > 0xFFFF:
        raise UprvProtocolError(f"payload exceeds 16-bit UPRV length: {len(payload)}")
    if len(reserved) != 4:
        raise UprvProtocolError(f"UPRV reserved field must be four bytes, got {len(reserved)}")
    checksum = sum(payload) & 0xFFFF
    return (
        MAGIC + len(payload).to_bytes(2, "big") + reserved + checksum.to_bytes(2, "big") + payload
    )


class UprvStreamDecoder:
    """Incrementally reassemble UPRV envelopes from an arbitrary byte stream.

    D2XX reads and USB bulk transfers do not preserve message boundaries. A
    frame may span reads, and several frames may arrive in one read. USB IN
    captures additionally have a two-byte FTDI modem-status prefix per USB
    transfer; capture tooling must remove that prefix before calling ``feed``.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.discarded_bytes = 0
        self.checksum_failures = 0

    def feed(self, data: bytes) -> list[UprvMessage]:
        self._buffer.extend(data)
        messages: list[UprvMessage] = []
        while True:
            magic_offset = self._buffer.find(MAGIC)
            if magic_offset < 0:
                keep = min(len(self._buffer), len(MAGIC) - 1)
                discard = len(self._buffer) - keep
                if discard:
                    del self._buffer[:discard]
                    self.discarded_bytes += discard
                return messages
            if magic_offset:
                del self._buffer[:magic_offset]
                self.discarded_bytes += magic_offset
            if len(self._buffer) < HEADER_LENGTH:
                return messages

            payload_length = int.from_bytes(self._buffer[4:6], "big")
            frame_length = HEADER_LENGTH + payload_length
            if len(self._buffer) < frame_length:
                return messages

            frame = bytes(self._buffer[:frame_length])
            del self._buffer[:frame_length]
            payload = frame[HEADER_LENGTH:]
            expected = int.from_bytes(frame[10:12], "big")
            actual = sum(payload) & 0xFFFF
            if actual != expected:
                # Drop the corrupt frame and keep going. Raising here would
                # discard the valid envelopes already decoded from this same
                # read and, because a long capture is a single stream, would
                # end a 15,000-sample road run over one bad frame. Corruption
                # stays visible through ``checksum_failures``, which capture
                # tooling reports alongside ``discarded_bytes``.
                self.checksum_failures += 1
                self.discarded_bytes += frame_length
                continue
            messages.append(UprvMessage(payload=payload, reserved=frame[6:10]))


def build_stream_definition(identifiers: Sequence[int]) -> bytes:
    """Build the observed dynamic-local-ID 0x81 definition payload.

    Every identifier is encoded as ``02 <DID-hi> <DID-lo>``. The order is
    significant: response values arrive in the same order.
    """
    if not identifiers:
        raise UprvProtocolError("stream definition needs at least one identifier")
    if len(set(identifiers)) != len(identifiers):
        raise UprvProtocolError("stream definition contains duplicate identifiers")
    descriptors = bytearray()
    for identifier in identifiers:
        if not 0 <= identifier <= 0xFFFF:
            raise UprvProtocolError(f"identifier out of range: 0x{identifier:x}")
        descriptors.append(2)
        descriptors.extend(identifier.to_bytes(2, "big"))
    return STREAM_DEFINITION_PREFIX + bytes(descriptors)


def build_memory_byte_stream_definition(addresses: Sequence[int]) -> bytes:
    """Build the captured read-only absolute-memory byte definition.

    UpRev's Target AFR stream used one ``83 <address:be32>`` descriptor per
    byte.  Keeping this separate from :func:`build_stream_definition` makes
    it impossible to accidentally reinterpret a RAM address as a DID.
    """
    if not addresses:
        raise UprvProtocolError("memory stream definition needs at least one address")
    if len(set(addresses)) != len(addresses):
        raise UprvProtocolError("memory stream definition contains duplicate addresses")
    descriptors = bytearray()
    for address in addresses:
        if not 0 <= address <= 0xFFFFFFFF:
            raise UprvProtocolError(f"memory address out of range: 0x{address:x}")
        descriptors.append(MEMORY_BYTE_DESCRIPTOR)
        descriptors.extend(address.to_bytes(4, "big"))
    return STREAM_DEFINITION_PREFIX + bytes(descriptors)


def build_mixed_stream_definition(
    identifiers: Sequence[int],
    addresses: Sequence[int],
    *,
    memory_first: bool = False,
) -> bytes:
    """Build one mixed DID/memory definition for paired observations.

    Captures prove that the cable accepts both DID-then-memory and
    memory-then-DID order. Response values follow descriptor order, so callers
    must select the order explicitly when splitting the reply.
    """
    if not identifiers or not addresses:
        raise UprvProtocolError("mixed stream definition needs DIDs and memory addresses")
    did_definition = build_stream_definition(identifiers)
    memory_definition = build_memory_byte_stream_definition(addresses)
    if memory_first:
        return memory_definition + did_definition[len(STREAM_DEFINITION_PREFIX) :]
    return did_definition + memory_definition[len(STREAM_DEFINITION_PREFIX) :]


def parse_stream_descriptors(payload: bytes) -> tuple[StreamDescriptor, ...]:
    """Parse DID and absolute-memory descriptors without losing their order."""
    if not payload.startswith(STREAM_DEFINITION_PREFIX):
        raise UprvProtocolError("not an UpRev stream-definition payload")

    encoded = payload[len(STREAM_DEFINITION_PREFIX) :]
    if not encoded:
        raise UprvProtocolError("stream definition has no descriptors")

    descriptors: list[StreamDescriptor] = []
    offset = 0
    while offset < len(encoded):
        kind = encoded[offset]
        if kind == 2:
            end = offset + 3
            if end > len(encoded):
                raise UprvProtocolError(f"truncated DID descriptor at offset {offset}")
            descriptors.append(("did", int.from_bytes(encoded[offset + 1 : end], "big")))
        elif kind == MEMORY_BYTE_DESCRIPTOR:
            end = offset + 5
            if end > len(encoded):
                raise UprvProtocolError(f"truncated memory descriptor at offset {offset}")
            descriptors.append(("memory_byte", int.from_bytes(encoded[offset + 1 : end], "big")))
        else:
            raise UprvProtocolError(
                f"unsupported stream descriptor 0x{kind:02x} at offset {offset}"
            )
        offset = end

    if len(set(descriptors)) != len(descriptors):
        raise UprvProtocolError("stream definition contains duplicate descriptors")
    return tuple(descriptors)


def parse_stream_definition(payload: bytes) -> tuple[int, ...]:
    """Return identifiers from a captured stream-definition payload."""
    if not payload.startswith(STREAM_DEFINITION_PREFIX):
        raise UprvProtocolError("not an UpRev stream-definition payload")
    descriptors = payload[len(STREAM_DEFINITION_PREFIX) :]
    if not descriptors or len(descriptors) % 3:
        raise UprvProtocolError("malformed stream-definition descriptors")
    identifiers: list[int] = []
    for offset in range(0, len(descriptors), 3):
        if descriptors[offset] != 2:
            # 0x02 is the addressing-mode tag, not a value width: DID 0x1101
            # rides an 02 descriptor and answers with a single byte. Widths
            # come from the catalog, which is why parse_stream_response needs
            # them passed in.
            raise UprvProtocolError(
                f"unsupported descriptor tag 0x{descriptors[offset]:02x} at offset {offset}"
            )
        identifiers.append(int.from_bytes(descriptors[offset + 1 : offset + 3], "big"))
    if len(set(identifiers)) != len(identifiers):
        raise UprvProtocolError("stream definition contains duplicate identifiers")
    return tuple(identifiers)


def parse_memory_byte_stream_definition(payload: bytes) -> tuple[int, ...]:
    """Return absolute byte addresses from a captured memory definition."""
    if not payload.startswith(STREAM_DEFINITION_PREFIX):
        raise UprvProtocolError("not an UpRev stream-definition payload")
    descriptors = payload[len(STREAM_DEFINITION_PREFIX) :]
    if not descriptors or len(descriptors) % 5:
        raise UprvProtocolError("malformed memory-stream descriptors")
    addresses: list[int] = []
    for offset in range(0, len(descriptors), 5):
        descriptor = descriptors[offset]
        if descriptor != MEMORY_BYTE_DESCRIPTOR:
            raise UprvProtocolError(
                f"unsupported memory descriptor 0x{descriptor:02x} at offset {offset}"
            )
        addresses.append(int.from_bytes(descriptors[offset + 1 : offset + 5], "big"))
    if len(set(addresses)) != len(addresses):
        raise UprvProtocolError("memory stream definition contains duplicate addresses")
    return tuple(addresses)


def parse_stream_data(payload: bytes, widths: Sequence[int]) -> tuple[bytes, ...]:
    """Validate a local-ID ``0x81`` reply and split its ordered value bytes."""
    if len(payload) < 5 or not payload.startswith(STREAM_RESPONSE_PREFIX):
        raise UprvProtocolError("not an UpRev cable response")
    inner_length = payload[1]
    if len(payload) != inner_length + 3:
        raise UprvProtocolError(
            f"inner length mismatch: declared {inner_length}, payload has {len(payload)} bytes"
        )
    if payload[2:4] != STREAM_DATA_PREFIX:
        raise UprvProtocolError(f"not a local-ID 0x81 response: {payload.hex()}")
    checksum = sum(payload[1:-1]) & 0xFF
    if checksum != payload[-1]:
        raise UprvProtocolError(
            f"inner checksum mismatch: calculated 0x{checksum:02x}, received 0x{payload[-1]:02x}"
        )
    if not widths or any(width <= 0 for width in widths):
        raise UprvProtocolError("response widths must be non-empty and positive")

    data = payload[4:-1]
    expected_length = sum(widths)
    if len(data) != expected_length:
        raise UprvProtocolError(
            f"stream data length mismatch: expected {expected_length}, got {len(data)}"
        )

    readings: list[bytes] = []
    offset = 0
    for width in widths:
        readings.append(data[offset : offset + width])
        offset += width
    return tuple(readings)


def parse_stream_response(
    payload: bytes,
    identifiers: Sequence[int],
    widths: Mapping[int, int],
) -> dict[int, bytes]:
    """Split one checksum-validated local-ID 0x81 reply into raw DID values.

    The reply does not carry per-value lengths, so every requested DID's width
    must already be known. Refusing unknown widths prevents a single mistaken
    boundary from silently misattributing every later value.
    """
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise UprvProtocolError("response identifiers must be non-empty and unique")

    resolved_widths: list[int] = []
    for identifier in identifiers:
        width = widths.get(identifier)
        if width is None or width <= 0:
            raise UprvProtocolError(f"unknown response width for DID 0x{identifier:04x}")
        resolved_widths.append(width)

    values = parse_stream_data(payload, resolved_widths)
    return dict(zip(identifiers, values, strict=True))
