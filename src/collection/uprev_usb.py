"""Linux USB transport for the UpRev/Cipher vehicle interface.

The cable is an FTDI device with a custom product ID, but the captured USB
traffic shows that it carries the proprietary UPRV envelope rather than a
transparent serial stream.  This module therefore uses PyUSB/libusb directly
and reproduces only the FTDI control requests observed during a successful
Windows D2XX session.

ECU/K-line session setup does not belong here; it lives in
:mod:`src.collection.uprev_session`. In particular, this transport contains no
flash or ECU non-volatile-memory operation.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

from src.collection.uprev_protocol import (
    HEADER_LENGTH,
    UprvMessage,
    UprvProtocolError,
    UprvStreamDecoder,
    encode_message,
)

UPREV_USB_VENDOR_ID = 0x0403
UPREV_USB_PRODUCT_ID = 0xCCB8
UPREV_USB_INTERFACE = 0
UPREV_USB_ENDPOINT_IN = 0x81
UPREV_USB_ENDPOINT_OUT = 0x02
UPREV_USB_PACKET_SIZE = 64

# FTDI SIO vendor requests observed in the D2XX capture.
_FTDI_REQUEST_OUT = 0x40
_FTDI_REQUEST_IN = 0xC0
_FTDI_SIO_RESET = 0
_FTDI_SIO_GET_MODEM_STATUS = 5
_FTDI_SIO_SET_LATENCY_TIMER = 9
_FTDI_RESET_SIO = 0
_FTDI_PURGE_RX = 1


class UpRevUsbError(RuntimeError):
    """The UpRev cable could not be discovered or addressed over USB."""


@dataclass(frozen=True)
class UpRevUsbDeviceInfo:
    """Stable identifying information for one attached cable."""

    bus: int | None
    address: int | None
    vendor_id: int
    product_id: int
    product: str | None
    serial_number: str | None
    _device: Any = field(repr=False, compare=False)


def strip_ftdi_status(data: bytes, *, packet_size: int = UPREV_USB_PACKET_SIZE) -> bytes:
    """Remove the two FTDI status bytes from every USB bulk-IN packet.

    A libusb read may contain more than one 64-byte USB packet.  Removing only
    the first two bytes corrupts every packet boundary after the first, which
    in turn can produce plausible but invalid UPRV framing.
    """
    if packet_size < 3:
        raise ValueError(f"FTDI packet size must be at least 3 bytes, got {packet_size}")
    payload = bytearray()
    for offset in range(0, len(data), packet_size):
        packet = data[offset : offset + packet_size]
        if len(packet) < 2:
            raise UpRevUsbError(
                f"FTDI bulk-IN packet has {len(packet)} byte; two status bytes are required"
            )
        payload.extend(packet[2:])
    return bytes(payload)


def _load_pyusb() -> tuple[Any, Any]:
    try:
        import usb.core
        import usb.util
    except ImportError as exc:  # pragma: no cover - depends on optional environment
        raise UpRevUsbError(
            "PyUSB is not installed; run `.venv/bin/python -m pip install -e '.[uprev-usb]'`"
        ) from exc
    return usb.core, usb.util


def discover_uprev_usb_devices() -> tuple[UpRevUsbDeviceInfo, ...]:
    """Enumerate attached UpRev/Cipher interfaces without claiming one."""
    usb_core, _usb_util = _load_pyusb()
    try:
        devices = tuple(
            usb_core.find(
                find_all=True,
                idVendor=UPREV_USB_VENDOR_ID,
                idProduct=UPREV_USB_PRODUCT_ID,
            )
            or ()
        )
        return tuple(
            UpRevUsbDeviceInfo(
                bus=getattr(device, "bus", None),
                address=getattr(device, "address", None),
                vendor_id=int(device.idVendor),
                product_id=int(device.idProduct),
                product=getattr(device, "product", None),
                serial_number=getattr(device, "serial_number", None),
                _device=device,
            )
            for device in devices
        )
    except Exception as exc:
        raise UpRevUsbError(f"failed to enumerate UpRev USB cable: {exc}") from exc


class UpRevUsbTransport:
    """Claim one cable and exchange checksum-validated UPRV messages."""

    def __init__(
        self,
        device: Any,
        *,
        usb_core: Any,
        usb_util: Any,
        transfer_timeout_ms: int = 100,
    ) -> None:
        if transfer_timeout_ms <= 0:
            raise ValueError("transfer_timeout_ms must be positive")
        self._device = device
        self._usb_core = usb_core
        self._usb_util = usb_util
        self._transfer_timeout_ms = transfer_timeout_ms
        self._decoder = UprvStreamDecoder()
        self._pending: deque[UprvMessage] = deque()
        self._opened = False
        self._detached_kernel_driver = False

    @classmethod
    def connect(
        cls,
        *,
        serial_number: str | None = None,
        transfer_timeout_ms: int = 100,
    ) -> Self:
        """Find and construct a transport for one attached cable.

        Multiple matching cables are refused unless ``serial_number`` chooses
        one.  Silently taking the first USB device would make physical-device
        identity depend on enumeration order.
        """
        usb_core, usb_util = _load_pyusb()
        infos = discover_uprev_usb_devices()
        if serial_number is not None:
            infos = tuple(info for info in infos if info.serial_number == serial_number)
        if not infos:
            target = f" with serial {serial_number!r}" if serial_number else ""
            raise UpRevUsbError(
                f"no attached UpRev USB cable{target}; attach 0403:ccb8 to WSL with usbipd first"
            )
        if len(infos) > 1:
            serials = ", ".join(repr(info.serial_number) for info in infos)
            raise UpRevUsbError(f"multiple UpRev USB cables found ({serials}); select --serial")
        return cls(
            infos[0]._device,
            usb_core=usb_core,
            usb_util=usb_util,
            transfer_timeout_ms=transfer_timeout_ms,
        )

    def open(self) -> None:
        """Claim interface 0 and apply the minimal captured D2XX setup."""
        if self._opened:
            return
        try:
            if self._device.is_kernel_driver_active(UPREV_USB_INTERFACE):
                self._device.detach_kernel_driver(UPREV_USB_INTERFACE)
                self._detached_kernel_driver = True
            self._device.set_configuration()
            self._usb_util.claim_interface(self._device, UPREV_USB_INTERFACE)
            self._control_out(_FTDI_SIO_RESET, _FTDI_RESET_SIO)
            status = bytes(
                self._device.ctrl_transfer(
                    _FTDI_REQUEST_IN,
                    _FTDI_SIO_GET_MODEM_STATUS,
                    0,
                    0,
                    2,
                    timeout=self._transfer_timeout_ms,
                )
            )
            if len(status) != 2:
                raise UpRevUsbError(
                    f"FTDI modem-status request returned {len(status)} bytes, expected 2"
                )
            self._control_out(_FTDI_SIO_SET_LATENCY_TIMER, 2)
            self._control_out(_FTDI_SIO_RESET, _FTDI_PURGE_RX)
            self._opened = True
        except Exception as exc:
            self._cleanup_after_failed_open()
            if isinstance(exc, UpRevUsbError):
                raise
            raise UpRevUsbError(f"failed to initialize UpRev USB cable: {exc}") from exc

    def _control_out(self, request: int, value: int) -> None:
        transferred = self._device.ctrl_transfer(
            _FTDI_REQUEST_OUT,
            request,
            value,
            0,
            None,
            timeout=self._transfer_timeout_ms,
        )
        if transferred not in (None, 0):
            raise UpRevUsbError(
                f"FTDI control request {request} unexpectedly transferred {transferred} bytes"
            )

    def purge_rx(self) -> None:
        """Discard bytes queued by the cable, matching FTDI ``FT_PURGE_RX``."""
        self._require_open()
        self._control_out(_FTDI_SIO_RESET, _FTDI_PURGE_RX)
        self._decoder = UprvStreamDecoder()
        self._pending.clear()

    def send(self, payload: bytes) -> None:
        """Write one UPRV message using the two writes proven by the D2XX probe."""
        self._require_open()
        message = encode_message(payload)
        self._bulk_write(message[:HEADER_LENGTH])
        self._bulk_write(message[HEADER_LENGTH:])

    def _bulk_write(self, data: bytes) -> None:
        if not data:
            return
        try:
            written = int(
                self._device.write(
                    UPREV_USB_ENDPOINT_OUT,
                    data,
                    timeout=self._transfer_timeout_ms,
                )
            )
        except Exception as exc:
            raise UpRevUsbError(f"UpRev USB bulk write failed: {exc}") from exc
        if written != len(data):
            raise UpRevUsbError(f"short UpRev USB write: {written}/{len(data)} bytes")

    def receive(self, *, timeout_seconds: float = 1.0) -> UprvMessage:
        """Read and reassemble one UPRV response before the deadline."""
        self._require_open()
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self._pending:
            return self._pending.popleft()
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"UpRev USB response timed out after {timeout_seconds:.3f}s")
            read_timeout_ms = max(1, min(self._transfer_timeout_ms, int(remaining * 1000)))
            try:
                raw = bytes(
                    self._device.read(
                        UPREV_USB_ENDPOINT_IN,
                        4096,
                        timeout=read_timeout_ms,
                    )
                )
            except Exception as exc:
                timeout_type = getattr(self._usb_core, "USBTimeoutError", ())
                if timeout_type and isinstance(exc, timeout_type):
                    continue
                raise UpRevUsbError(f"UpRev USB bulk read failed: {exc}") from exc
            application_data = strip_ftdi_status(raw)
            try:
                self._pending.extend(self._decoder.feed(application_data))
            except UprvProtocolError as exc:
                # UprvProtocolError is a ValueError, so without this it would
                # sail past every `except (TimeoutError, UpRevUsbError)` in
                # the session bootstrap and the probe retry loop and abort a
                # capture with a bare traceback.
                raise UpRevUsbError(f"UpRev USB framing failed: {exc}") from exc
            if self._pending:
                return self._pending.popleft()

    def exchange(self, payload: bytes, *, timeout_seconds: float = 1.0) -> UprvMessage:
        """Send one UPRV request and return the next response."""
        self.send(payload)
        return self.receive(timeout_seconds=timeout_seconds)

    def cable_ping(self, *, timeout_seconds: float = 1.0) -> None:
        """Perform the proven cable-local ``07 -> 06`` handshake."""
        self.send(b"\x07")
        deadline = time.monotonic() + timeout_seconds
        unexpected: list[str] = []
        while time.monotonic() < deadline:
            try:
                reply = self.receive(timeout_seconds=max(0.001, deadline - time.monotonic()))
            except TimeoutError:
                break
            if reply.payload == b"\x06":
                return
            # A failed stream can leave its final reply queued behind the
            # FTDI purge and ahead of the ping acknowledgement. The hardware
            # reproduced this on 2026-09-20: the first ping saw one complete
            # 0x61 stream reply and the second saw 0x06. Drain whole validated
            # messages until the acknowledgement rather than treating an old
            # reply as the new request's response.
            unexpected.append(reply.payload.hex())
        rendered = ", ".join(unexpected) if unexpected else "timeout"
        raise UpRevUsbError(f"cable ping did not return 06; preceding replies: {rendered}")

    def close(self) -> None:
        """Release the libusb handle and restore a detached kernel driver."""
        if not self._opened and not self._detached_kernel_driver:
            return
        error: Exception | None = None
        try:
            self._usb_util.release_interface(self._device, UPREV_USB_INTERFACE)
        except Exception as exc:  # pragma: no cover - hardware cleanup failure
            error = exc
        if self._detached_kernel_driver:
            try:
                self._device.attach_kernel_driver(UPREV_USB_INTERFACE)
            except Exception as exc:  # pragma: no cover - depends on host driver
                error = error or exc
        try:
            self._usb_util.dispose_resources(self._device)
        except Exception as exc:  # pragma: no cover - hardware cleanup failure
            error = error or exc
        self._opened = False
        self._detached_kernel_driver = False
        if error is not None:
            raise UpRevUsbError(f"failed to release UpRev USB cable: {error}") from error

    def _cleanup_after_failed_open(self) -> None:
        try:
            self._usb_util.release_interface(self._device, UPREV_USB_INTERFACE)
        except Exception:
            pass
        try:
            self._usb_util.dispose_resources(self._device)
        except Exception:
            pass
        if self._detached_kernel_driver:
            try:
                self._device.attach_kernel_driver(UPREV_USB_INTERFACE)
            except Exception:
                pass
        self._detached_kernel_driver = False
        self._opened = False

    def _require_open(self) -> None:
        if not self._opened:
            raise UpRevUsbError("UpRev USB transport is not open")

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
