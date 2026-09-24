"""Register access to I²C devices through Linux ``/dev/i2c-N``, no dependency.

The HAT's IMU (LSM6DSOX, 0x6B) and 12 V sense ADC (ADS1015, 0x48) are plain
register devices on I²C1. Drivers talk to them through :class:`RegisterBus`,
a two-method protocol, so every test drives a fake and a later transport (the
rev 3.0 supervisor MCU owns these sensors on its own bus,
``docs/CANary-HAT-Revisions.md`` §13.3) can stand in without touching the chip
drivers.

:class:`LinuxI2cBus` uses the kernel's ``I2C_RDWR`` ioctl, which sends the
register write and the read as one combined transaction (a repeated start,
no stop between them). That is what these chips' datasheets specify, and it
is what ``smbus2`` does internally; a few lines of ``ctypes`` avoid adding it
as a runtime dependency (H01.3, ``docs/roadmap/tasks/H01-source-policy.md``).
Reads are not limited to SMBus's 32-byte blocks, so a FIFO can be drained in
one transaction.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
from typing import Protocol

#: ``linux/i2c-dev.h`` / ``linux/i2c.h``.
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001
#: The kernel caps one I2C_RDWR message at 8192 bytes; stay well inside it.
MAX_READ = 4096


class RegisterBus(Protocol):
    """One device's registers: read ``length`` bytes from ``register`` onward,
    write one byte, or write several (a 16-bit register such as the ADS1015's,
    MSB first). Auto-increment across registers is the device's job (e.g. the
    LSM6DSOX's ``IF_INC``)."""

    def read(self, register: int, length: int) -> bytes: ...

    def write(self, register: int, value: int) -> None: ...

    def write_block(self, register: int, data: bytes) -> None: ...

    def close(self) -> None: ...


class _I2cMsg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.POINTER(ctypes.c_uint8)),
    ]


class _I2cRdwrData(ctypes.Structure):
    _fields_ = [
        ("msgs", ctypes.POINTER(_I2cMsg)),
        ("nmsgs", ctypes.c_uint32),
    ]


class LinuxI2cBus:
    """A :class:`RegisterBus` for one device address on ``/dev/i2c-<bus>``."""

    def __init__(self, bus: int, address: int, *, device_dir: str = "/dev") -> None:
        if not 0x03 <= address <= 0x77:
            raise ValueError(f"I2C address {address:#04x} is outside 0x03..0x77")
        self.bus = bus
        self.address = address
        self._fd: int | None = os.open(f"{device_dir}/i2c-{bus}", os.O_RDWR)

    def _transfer(self, *msgs: tuple[int, ctypes.Array[ctypes.c_uint8]]) -> None:
        if self._fd is None:
            raise OSError(f"I2C bus {self.bus} device {self.address:#04x} is closed")
        array = (_I2cMsg * len(msgs))()
        for slot, (flags, buf) in zip(array, msgs, strict=True):
            slot.addr = self.address
            slot.flags = flags
            slot.len = len(buf)
            slot.buf = ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8))
        data = _I2cRdwrData(ctypes.cast(array, ctypes.POINTER(_I2cMsg)), len(msgs))
        fcntl.ioctl(self._fd, I2C_RDWR, data)

    def read(self, register: int, length: int) -> bytes:
        if not 1 <= length <= MAX_READ:
            raise ValueError(f"read length {length} outside 1..{MAX_READ}")
        pointer = (ctypes.c_uint8 * 1)(register & 0xFF)
        out = (ctypes.c_uint8 * length)()
        self._transfer((0, pointer), (I2C_M_RD, out))
        return bytes(out)

    def write(self, register: int, value: int) -> None:
        self.write_block(register, bytes([value & 0xFF]))

    def write_block(self, register: int, data: bytes) -> None:
        """Write ``data`` starting at ``register`` (e.g. a 16-bit register, MSB first)."""
        if not 1 <= len(data) <= 64:
            raise ValueError(f"write length {len(data)} outside 1..64")
        buf = (ctypes.c_uint8 * (1 + len(data)))(register & 0xFF, *data)
        self._transfer((0, buf))

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


__all__ = ["LinuxI2cBus", "RegisterBus"]
