"""Full-duplex SPI through Linux ``/dev/spidevB.C``, no dependency.

The CANary-HAT rev 2.1's ADC (ADS8688A, U8) sits on SPI0 CE1 beside the
vehicle CAN controller on CE0, which the kernel's ``mcp251xfd`` driver owns.
The overlay disables only CE0's spidev node, so CE1 comes up as
``/dev/spidev0.1`` for a userspace driver, the way the IMU and rev 2.0's ADC
are read over I²C (:mod:`src.collection.i2c_dev`).

Drivers talk through :class:`SpiBus`. One :meth:`SpiBus.transfer` call is one
``SPI_IOC_MESSAGE`` ioctl: every frame in it is its own chip-select cycle (the
ADS8688A starts a conversion on each falling edge), and the kernel holds the
controller for the whole message. A burst is therefore one short, atomic use
of the bus that the CAN controller's traffic queues behind, rather than a
string of separate transactions interleaved with it.

Structure layouts are ``linux/spi/spidev.h``; the size is asserted so a
mistake fails at import, not on the bus.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import struct
from collections.abc import Sequence
from typing import Protocol

SPI_IOC_MAGIC = ord("k")
#: ``SPI_MODE_1``: clock idles low, data sampled on the falling edge.
SPI_MODE_1 = 0x01
#: Frames in one message. The kernel's limit is far higher (spidev's default
#: ``bufsiz`` of 4096 bytes is the real cap); a burst here is under ten.
MAX_FRAMES = 64
MAX_FRAME_BYTES = 64


class SpiBus(Protocol):
    """One SPI device: clock frames out and return what came back."""

    def transfer(self, frames: Sequence[bytes]) -> list[bytes]:
        """Send each frame in its own chip-select cycle, all in one message;
        return the bytes clocked in during each, same lengths."""
        ...

    def close(self) -> None: ...


class _Transfer(ctypes.Structure):
    _fields_ = [
        ("tx_buf", ctypes.c_uint64),
        ("rx_buf", ctypes.c_uint64),
        ("len", ctypes.c_uint32),
        ("speed_hz", ctypes.c_uint32),
        ("delay_usecs", ctypes.c_uint16),
        ("bits_per_word", ctypes.c_uint8),
        ("cs_change", ctypes.c_uint8),
        ("tx_nbits", ctypes.c_uint8),
        ("rx_nbits", ctypes.c_uint8),
        ("word_delay_usecs", ctypes.c_uint8),
        ("pad", ctypes.c_uint8),
    ]


assert ctypes.sizeof(_Transfer) == 32, ctypes.sizeof(_Transfer)
# Every field where spidev.h puts it, so a swap of two same-sized fields fails
# here too.
assert [getattr(_Transfer, spec[0]).offset for spec in _Transfer._fields_] == [
    0,
    8,
    16,
    20,
    24,
    26,
    27,
    28,
    29,
    30,
    31,
]


def _iow(nr: int, size: int) -> int:
    return (1 << 30) | (size << 16) | (SPI_IOC_MAGIC << 8) | nr


SPI_IOC_WR_MODE = _iow(1, 1)
SPI_IOC_WR_BITS_PER_WORD = _iow(3, 1)
SPI_IOC_WR_MAX_SPEED_HZ = _iow(4, 4)


def spi_ioc_message(frames: int) -> int:
    """``SPI_IOC_MESSAGE(frames)``."""
    return _iow(0, frames * ctypes.sizeof(_Transfer))


class LinuxSpiBus:
    """A :class:`SpiBus` on ``/dev/spidev<bus>.<chip select>``."""

    def __init__(self, device: str, *, mode: int, speed_hz: int) -> None:
        if speed_hz <= 0:
            raise ValueError(f"SPI speed {speed_hz} Hz must be positive")
        self.device = device
        self.speed_hz = speed_hz
        self._fd: int | None = os.open(device, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.ioctl(self._fd, SPI_IOC_WR_MODE, struct.pack("B", mode))
            fcntl.ioctl(self._fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
            fcntl.ioctl(self._fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", speed_hz))
        except OSError:
            self.close()
            raise

    def transfer(self, frames: Sequence[bytes]) -> list[bytes]:
        if self._fd is None:
            raise OSError(f"SPI device {self.device} is closed")
        if not 1 <= len(frames) <= MAX_FRAMES:
            raise ValueError(f"{len(frames)} frames in one message (1..{MAX_FRAMES})")
        array = (_Transfer * len(frames))()
        buffers: list[tuple[ctypes.Array[ctypes.c_uint8], ctypes.Array[ctypes.c_uint8]]] = []
        for index, (slot, frame) in enumerate(zip(array, frames, strict=True)):
            if not 1 <= len(frame) <= MAX_FRAME_BYTES:
                raise ValueError(f"frame of {len(frame)} bytes (1..{MAX_FRAME_BYTES})")
            tx = (ctypes.c_uint8 * len(frame))(*frame)
            rx = (ctypes.c_uint8 * len(frame))()
            buffers.append((tx, rx))  # alive until the ioctl returns
            slot.tx_buf = ctypes.addressof(tx)
            slot.rx_buf = ctypes.addressof(rx)
            slot.len = len(frame)
            slot.speed_hz = self.speed_hz
            slot.bits_per_word = 8
            # Deselect between frames; on the last one cs_change would leave
            # the device selected after the message instead.
            slot.cs_change = 1 if index < len(frames) - 1 else 0
        fcntl.ioctl(self._fd, spi_ioc_message(len(frames)), array)
        return [bytes(rx) for _, rx in buffers]

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


__all__ = ["SPI_MODE_1", "LinuxSpiBus", "SpiBus", "spi_ioc_message"]
