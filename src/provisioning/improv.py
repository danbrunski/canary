"""The Improv WiFi BLE wire format (https://www.improv-wifi.com/ble/, v2.2).

Pure: bytes in, bytes out. Nothing here knows about BlueZ or NetworkManager,
so every framing rule is testable on a laptop.

Packets on the RPC Command and RPC Result characteristics share one frame::

    [command] [data length] [data ...] [checksum]

where the checksum is the low byte of the sum of every preceding byte. Result
data is a list of length-prefixed UTF-8 strings. The data length is one byte,
so a frame never carries more than :data:`MAX_DATA_LEN` bytes of data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum, IntFlag

SERVICE_UUID = "00467768-6228-2272-4663-277478268000"
#: 16-bit UUID 0x4677, carried as service data in the advertisement (v2.0).
#: Written short on purpose: BlueZ keeps a full 128-bit string as a 128-bit
#: UUID, whose service-data entry no longer fits beside the service UUID in
#: one 31-byte packet.
SERVICE_DATA_UUID = "4677"
CHAR_STATE_UUID = "00467768-6228-2272-4663-277478268001"
CHAR_ERROR_UUID = "00467768-6228-2272-4663-277478268002"
CHAR_RPC_COMMAND_UUID = "00467768-6228-2272-4663-277478268003"
CHAR_RPC_RESULT_UUID = "00467768-6228-2272-4663-277478268004"
CHAR_CAPABILITIES_UUID = "00467768-6228-2272-4663-277478268005"

#: The frame's length field is one byte.
MAX_DATA_LEN = 255
#: Command, length, and checksum around the data.
FRAME_OVERHEAD = 3


class State(IntEnum):
    AUTHORIZATION_REQUIRED = 0x01
    AUTHORIZED = 0x02
    PROVISIONING = 0x03
    PROVISIONED = 0x04


class ImprovError(IntEnum):
    NONE = 0x00
    INVALID_RPC_PACKET = 0x01
    UNKNOWN_RPC_COMMAND = 0x02
    UNABLE_TO_CONNECT = 0x03
    NOT_AUTHORIZED = 0x04
    BAD_HOSTNAME = 0x05
    UNKNOWN = 0xFF


class Command(IntEnum):
    WIFI_SETTINGS = 0x01
    IDENTIFY = 0x02
    DEVICE_INFO = 0x03
    SCAN_WIFI = 0x04
    HOSTNAME = 0x05
    DEVICE_NAME = 0x06


class Capability(IntFlag):
    NONE = 0
    IDENTIFY = 1 << 0
    DEVICE_INFO = 1 << 1
    SCAN_WIFI = 1 << 2
    HOSTNAME = 1 << 3


class ImprovPacketError(ValueError):
    """A write that cannot be acted on, carrying the error to report."""

    def __init__(self, error: ImprovError, detail: str) -> None:
        super().__init__(detail)
        self.error = error


@dataclass(frozen=True)
class RpcCommand:
    command: int
    data: bytes


@dataclass(frozen=True)
class WifiCredentials:
    ssid: str
    password: str

    def __repr__(self) -> str:
        # Never let a passphrase reach a log line through a stray %r.
        return f"WifiCredentials(ssid={self.ssid!r}, password=<{len(self.password)} chars>)"


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def parse_command(packet: bytes) -> RpcCommand:
    """Validate one complete frame and split it into command and data."""
    if len(packet) < FRAME_OVERHEAD:
        raise ImprovPacketError(ImprovError.INVALID_RPC_PACKET, f"{len(packet)}-byte frame")
    if len(packet) != packet[1] + FRAME_OVERHEAD:
        raise ImprovPacketError(
            ImprovError.INVALID_RPC_PACKET,
            f"length field says {packet[1]} data bytes, frame has {len(packet) - 3}",
        )
    if packet[-1] != checksum(packet[:-1]):
        raise ImprovPacketError(ImprovError.INVALID_RPC_PACKET, "bad checksum")
    return RpcCommand(command=packet[0], data=bytes(packet[2:-1]))


def parse_strings(data: bytes) -> list[bytes]:
    """Split RPC data into its length-prefixed strings; refuse any overrun."""
    strings: list[bytes] = []
    pos = 0
    while pos < len(data):
        length = data[pos]
        end = pos + 1 + length
        if end > len(data):
            raise ImprovPacketError(
                ImprovError.INVALID_RPC_PACKET, f"string at offset {pos} overruns the data"
            )
        strings.append(bytes(data[pos + 1 : end]))
        pos = end
    return strings


def parse_wifi_settings(data: bytes) -> WifiCredentials:
    strings = parse_strings(data)
    if len(strings) != 2:
        raise ImprovPacketError(
            ImprovError.INVALID_RPC_PACKET, f"WiFi settings carry {len(strings)} strings, not 2"
        )
    try:
        ssid, password = (s.decode("utf-8") for s in strings)
    except UnicodeDecodeError as exc:
        raise ImprovPacketError(ImprovError.INVALID_RPC_PACKET, "not UTF-8") from exc
    if not ssid:
        raise ImprovPacketError(ImprovError.INVALID_RPC_PACKET, "empty SSID")
    return WifiCredentials(ssid=ssid, password=password)


def _string_data(strings: Sequence[str]) -> bytes:
    out = bytearray()
    for text in strings:
        raw = text.encode("utf-8")
        if len(raw) > MAX_DATA_LEN:
            raise ValueError(f"result string of {len(raw)} bytes cannot be length-prefixed")
        out.append(len(raw))
        out += raw
    return bytes(out)


def encode_frame(command: int, data: bytes) -> bytes:
    if len(data) > MAX_DATA_LEN:
        raise ValueError(f"{len(data)} data bytes exceed the one-byte length field")
    frame = bytes([command, len(data)]) + data
    return frame + bytes([checksum(frame)])


def encode_result(command: int, strings: Sequence[str]) -> bytes:
    return encode_frame(command, _string_data(strings))


def encode_scan_result(networks: Sequence[tuple[str, int, str]], max_frame: int) -> bytes:
    """Encode ``(ssid, rssi_dbm, auth)`` triples, as many as fit one frame.

    Clients read a scan as a single RPC result (Home Assistant's client parses
    exactly one), so the list is cut, never split: pass it strongest first and
    the weakest networks are the ones dropped. ``max_frame`` is what one
    notification can carry (ATT MTU - 3).
    """
    budget = min(MAX_DATA_LEN, max_frame - FRAME_OVERHEAD)
    data = b""
    for ssid, rssi, auth in networks:
        try:
            entry = _string_data([ssid, str(rssi), auth])
        except ValueError:
            continue
        if len(data) + len(entry) > budget:
            break
        data += entry
    return encode_frame(Command.SCAN_WIFI, data)


def service_data(state: State, capabilities: Capability) -> bytes:
    """The 6-byte advertisement service data: state, capabilities, 4 reserved."""
    return bytes([state, capabilities, 0, 0, 0, 0])


class CommandAssembler:
    """Rebuild a frame a client wrote in several chunks.

    The spec lets a client split a frame over consecutive writes when it
    exceeds the 20-byte default payload. The frame's own length field says
    when it is complete, so nothing else has to delimit writes.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> bytes | None:
        """Add a write; return the complete frame once there is one."""
        self._buffer += chunk
        if len(self._buffer) < 2:
            return None
        expected = self._buffer[1] + FRAME_OVERHEAD
        if len(self._buffer) < expected:
            return None
        frame = bytes(self._buffer)
        self._buffer.clear()
        # More bytes than the length field allows: hand over the whole run so
        # parse_command reports it, rather than guessing where a frame starts.
        return frame

    def reset(self) -> None:
        self._buffer.clear()
