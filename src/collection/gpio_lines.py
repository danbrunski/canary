"""Drive GPIO output lines through the Linux GPIO character device, no dependency.

The K-line transport owns two lines on the CANary-HAT: the TLIN1027's EN
(GPIO26) and the commander pull-up FET's gate (GPIO24). This requests them as
outputs through the kernel's v2 uAPI (``GPIO_V2_GET_LINE_IOCTL``, the same call
libgpiod makes) and sets them with ``GPIO_V2_LINE_SET_VALUES_IOCTL``. Holding
the request keeps the lines ours: another process asking for them gets EBUSY
(checked on the HAT 2026-09-23).

**Releasing a line does not reset it.** On the Pi 5 a released line keeps the
level it was last driven to (measured the same day), so a process killed while
holding EN or the pull-up high leaves them high. Callers drive their lines to
the safe level before :meth:`GpioOutputs.close` on every path they control
(``src.collection.kline.transport`` does), and the board's own pull-down on EN
(R4) only helps once the line is an input again, at the next boot.

Structure layouts are ``linux/gpio.h`` (v2, since Linux 5.10); sizes are
asserted so a mistake fails at import, not on the bus.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
from collections.abc import Mapping, Sequence

GPIO_V2_LINES_MAX = 64
GPIO_MAX_NAME_SIZE = 32
GPIO_V2_LINE_NUM_ATTRS_MAX = 10
GPIO_V2_LINE_FLAG_OUTPUT = 1 << 3
GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES = 2


class _AttrUnion(ctypes.Union):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("values", ctypes.c_uint64),
        ("debounce_period_us", ctypes.c_uint32),
    ]


class _LineAttribute(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint32), ("padding", ctypes.c_uint32), ("u", _AttrUnion)]


class _LineConfigAttribute(ctypes.Structure):
    _fields_ = [("attr", _LineAttribute), ("mask", ctypes.c_uint64)]


class _LineConfig(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint64),
        ("num_attrs", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 5),
        ("attrs", _LineConfigAttribute * GPIO_V2_LINE_NUM_ATTRS_MAX),
    ]


class _LineRequest(ctypes.Structure):
    _fields_ = [
        ("offsets", ctypes.c_uint32 * GPIO_V2_LINES_MAX),
        ("consumer", ctypes.c_char * GPIO_MAX_NAME_SIZE),
        ("config", _LineConfig),
        ("num_lines", ctypes.c_uint32),
        ("event_buffer_size", ctypes.c_uint32),
        ("padding", ctypes.c_uint32 * 5),
        ("fd", ctypes.c_int32),
    ]


class _LineValues(ctypes.Structure):
    _fields_ = [("bits", ctypes.c_uint64), ("mask", ctypes.c_uint64)]


assert ctypes.sizeof(_LineRequest) == 592, ctypes.sizeof(_LineRequest)
assert ctypes.sizeof(_LineValues) == 16


def _iowr(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (0xB4 << 8) | nr


GPIO_V2_GET_LINE_IOCTL = _iowr(0x07, ctypes.sizeof(_LineRequest))
GPIO_V2_LINE_SET_VALUES_IOCTL = _iowr(0x0F, ctypes.sizeof(_LineValues))


class GpioOutputs:
    """Output lines requested together, with their initial values.

    ``initial`` maps line offset -> level; the lines come up at those levels
    in the same ioctl that claims them, so there is no glitch between
    claiming and setting.
    """

    def __init__(
        self,
        chip: str,
        initial: Mapping[int, bool],
        *,
        consumer: str = "canary",
    ) -> None:
        if not 1 <= len(initial) <= GPIO_V2_LINES_MAX:
            raise ValueError(f"{len(initial)} lines requested (1..{GPIO_V2_LINES_MAX})")
        self.offsets: tuple[int, ...] = tuple(initial)
        request = _LineRequest()
        for index, offset in enumerate(self.offsets):
            request.offsets[index] = offset
        request.consumer = consumer.encode()[: GPIO_MAX_NAME_SIZE - 1]
        request.num_lines = len(self.offsets)
        request.config.flags = GPIO_V2_LINE_FLAG_OUTPUT
        request.config.num_attrs = 1
        attr = request.config.attrs[0]
        attr.attr.id = GPIO_V2_LINE_ATTR_ID_OUTPUT_VALUES
        attr.attr.u.values = self._bits(initial)
        attr.mask = (1 << len(self.offsets)) - 1
        chip_fd = os.open(chip, os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.ioctl(chip_fd, GPIO_V2_GET_LINE_IOCTL, request)
        finally:
            os.close(chip_fd)
        self._fd: int | None = request.fd

    def _bits(self, levels: Mapping[int, bool]) -> int:
        bits = 0
        for index, offset in enumerate(self.offsets):
            if levels.get(offset):
                bits |= 1 << index
        return bits

    def set(self, levels: Mapping[int, bool]) -> None:
        """Set the given lines; lines not named keep their level."""
        if self._fd is None:
            raise OSError("GPIO lines already released")
        unknown = set(levels) - set(self.offsets)
        if unknown:
            raise ValueError(f"lines {sorted(unknown)} were not requested")
        values = _LineValues()
        values.bits = self._bits(levels)
        values.mask = self._bits(dict.fromkeys(levels, True))
        fcntl.ioctl(self._fd, GPIO_V2_LINE_SET_VALUES_IOCTL, values)

    def close(self) -> None:
        """Release the lines. They keep their current level: set them first."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def request_outputs(
    chip: str, lines: Sequence[int], *, level: bool = False, consumer: str = "canary"
) -> GpioOutputs:
    return GpioOutputs(chip, dict.fromkeys(lines, level), consumer=consumer)


__all__ = ["GpioOutputs", "request_outputs"]
