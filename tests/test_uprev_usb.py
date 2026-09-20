"""USB transport tests that do not require the physical UpRev cable."""

from __future__ import annotations

from collections import deque

import pytest

from src.collection.uprev_protocol import UprvProtocolError, encode_message
from src.collection.uprev_usb import (
    UPREV_USB_ENDPOINT_IN,
    UPREV_USB_ENDPOINT_OUT,
    UpRevUsbError,
    UpRevUsbTransport,
    strip_ftdi_status,
)


class _UsbTimeoutError(Exception):
    pass


class _UsbCore:
    USBTimeoutError = _UsbTimeoutError


class _UsbUtil:
    def __init__(self) -> None:
        self.claimed: list[int] = []
        self.released: list[int] = []
        self.disposed = 0

    def claim_interface(self, _device: object, interface: int) -> None:
        self.claimed.append(interface)

    def release_interface(self, _device: object, interface: int) -> None:
        self.released.append(interface)

    def dispose_resources(self, _device: object) -> None:
        self.disposed += 1


class _Device:
    def __init__(self, reads: list[bytes | Exception] | None = None) -> None:
        self.reads: deque[bytes | Exception] = deque(reads or [])
        self.controls: list[tuple[int, int, int, int, object, int]] = []
        self.writes: list[tuple[int, bytes, int]] = []
        self.configured = False
        self.detached = False
        self.attached = False

    def is_kernel_driver_active(self, _interface: int) -> bool:
        return False

    def detach_kernel_driver(self, _interface: int) -> None:
        self.detached = True

    def attach_kernel_driver(self, _interface: int) -> None:
        self.attached = True

    def set_configuration(self) -> None:
        self.configured = True

    def ctrl_transfer(
        self,
        request_type: int,
        request: int,
        value: int,
        index: int,
        data_or_length: object,
        *,
        timeout: int,
    ) -> object:
        self.controls.append((request_type, request, value, index, data_or_length, timeout))
        return b"\x10\x00" if request_type == 0xC0 else 0

    def write(self, endpoint: int, data: bytes, *, timeout: int) -> int:
        self.writes.append((endpoint, bytes(data), timeout))
        return len(data)

    def read(self, endpoint: int, _size: int, *, timeout: int) -> bytes:
        assert endpoint == UPREV_USB_ENDPOINT_IN
        if not self.reads:
            raise _UsbTimeoutError
        result = self.reads.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def _transport(device: _Device) -> tuple[UpRevUsbTransport, _UsbUtil]:
    usb_util = _UsbUtil()
    transport = UpRevUsbTransport(device, usb_core=_UsbCore, usb_util=usb_util)
    return transport, usb_util


def test_strip_ftdi_status_from_every_packet() -> None:
    first = b"\x10\x00" + bytes(range(62))
    second = b"\x11\x00tail"
    assert strip_ftdi_status(first + second) == bytes(range(62)) + b"tail"


def test_strip_ftdi_status_rejects_one_byte_packet() -> None:
    with pytest.raises(UprvProtocolError, match="two status bytes"):
        strip_ftdi_status(b"\x10")


def test_open_uses_only_captured_ftdi_control_requests() -> None:
    device = _Device()
    transport, usb_util = _transport(device)

    transport.open()

    assert device.configured is True
    assert usb_util.claimed == [0]
    assert [(row[0], row[1], row[2], row[3], row[4]) for row in device.controls] == [
        (0x40, 0, 0, 0, None),
        (0xC0, 5, 0, 0, 2),
        (0x40, 9, 2, 0, None),
        (0x40, 0, 1, 0, None),
    ]


def test_cable_ping_matches_proven_split_write_and_reply() -> None:
    reply = encode_message(b"\x06")
    device = _Device([b"\x10\x00" + reply[:5], b"\x10\x00" + reply[5:]])
    transport, usb_util = _transport(device)

    with transport:
        transport.cable_ping()

    request = encode_message(b"\x07")
    assert device.writes == [
        (UPREV_USB_ENDPOINT_OUT, request[:12], 100),
        (UPREV_USB_ENDPOINT_OUT, request[12:], 100),
    ]
    assert usb_util.released == [0]
    assert usb_util.disposed == 1


def test_cable_ping_drains_stale_complete_reply_before_acknowledgement() -> None:
    stale = encode_message(bytes.fromhex("6605618100d88d4c"))
    acknowledgement = encode_message(b"\x06")
    device = _Device([b"\x10\x00" + stale, b"\x10\x00" + acknowledgement])
    transport, _usb_util = _transport(device)

    with transport:
        transport.cable_ping()

    assert len(device.writes) == 2


def test_receive_queues_coalesced_messages() -> None:
    wire = encode_message(b"\x06") + encode_message(b"\x7f\x01")
    device = _Device([b"\x10\x00" + wire])
    transport, _usb_util = _transport(device)
    transport.open()

    assert transport.receive().payload == b"\x06"
    assert transport.receive().payload == b"\x7f\x01"


def test_short_bulk_write_is_refused() -> None:
    device = _Device()
    transport, _usb_util = _transport(device)
    transport.open()
    device.write = lambda _endpoint, data, *, timeout: len(data) - 1  # type: ignore[method-assign]

    with pytest.raises(UpRevUsbError, match="short UpRev USB write"):
        transport.send(b"\x07")


def test_exchange_requires_an_open_transport() -> None:
    transport, _usb_util = _transport(_Device())
    with pytest.raises(UpRevUsbError, match="not open"):
        transport.exchange(b"\x07")
