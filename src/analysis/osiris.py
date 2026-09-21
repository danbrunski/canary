"""Offline Osiris v1 codec. No hardware or ECU write path.

Derived from the independently verified Windows research codec. Unknown
sections and the entire original prefix survive serialization unchanged.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

SIGNATURE = b"UpRev Osiris ROM File Ver 1.0000"
HEADER = struct.Struct("<8sIIII")


def mask(seed: int, length: int) -> bytes:
    return b"".join(
        struct.pack(">I", zlib.crc32(struct.pack(">I", (seed + 5 * j) & 0xFFFFFFFF) * 4))
        for j in range((length + 3) // 4)
    )[:length]


def _xor(data: bytes, seed: int) -> bytes:
    return bytes(a ^ b for a, b in zip(data, mask(seed, len(data)), strict=True))


def _rol(value: int, count: int) -> int:
    return ((value << count) | (value >> (16 - count))) & 0xFFFF


def transform(data: bytes, key: int, *, encode: bool = False) -> bytes:
    if not data or len(data) % 4:
        raise ValueError("ROMData must contain complete BE32 words")
    result = bytearray()
    for (word,) in struct.iter_unpack(">I", data):
        hi, lo = word >> 16, word & 0xFFFF
        if encode:
            v = (lo + (key >> 16)) & 0xFFFF
            w = ((_rol(v, 2) + v - 1) & 0xFFFF) ^ hi
            v = ((key & 0xFFFF) + w) & 0xFFFF
            v = (_rol(v, 1) + v - 1) & 0xFFFF
            hi, lo = v ^ _rol(v, 4) ^ lo, w
        else:
            v = ((key & 0xFFFF) + lo) & 0xFFFF
            v = (v + _rol(v, 1) - 1) & 0xFFFF
            v = v ^ _rol(v, 4) ^ hi
            w = (v + (key >> 16)) & 0xFFFF
            hi, lo = ((w + _rol(w, 2) - 1) & 0xFFFF) ^ lo, v
        result.extend(struct.pack(">I", (hi << 16) | lo))
    return bytes(result)


@dataclass(frozen=True)
class Section:
    label: bytes
    option: int
    data: bytes = field(repr=False)

    def serialize(self) -> bytes:
        if len(self.label) != 8 or self.option not in (0, 1):
            raise ValueError("Unsupported section header")
        crc = zlib.crc32(self.data)
        raw = _xor(self.data, crc) if self.option else self.data
        return HEADER.pack(self.label, len(raw), len(raw) + HEADER.size, self.option, crc) + raw


@dataclass(frozen=True)
class Container:
    prefix: bytes = field(repr=False)
    sections: tuple[Section, ...] = field(repr=False)

    @classmethod
    def parse(cls, data: bytes) -> Container:
        if len(data) < 96 or not data.startswith(SIGNATURE):
            raise ValueError("Unsupported Osiris signature or truncated prefix")
        offset = 96
        sections = []
        names = set()
        while offset < len(data):
            if len(data) - offset < HEADER.size:
                raise ValueError("Truncated section header")
            label, length, total, option, crc = HEADER.unpack_from(data, offset)
            if total != length + HEADER.size or total > len(data) - offset:
                raise ValueError("Invalid section bounds")
            if option not in (0, 1):
                raise ValueError("Unsupported section encoding")
            if label in names:
                raise ValueError("Duplicate section")
            names.add(label)
            raw = data[offset + HEADER.size : offset + total]
            clear = _xor(raw, crc) if option else raw
            if zlib.crc32(clear) != crc:
                raise ValueError(f"CRC mismatch for section {label!r}")
            sections.append(Section(label, option, clear))
            offset += total
        container = cls(data[:96], tuple(sections))
        if container.serialize() != data:
            raise ValueError("Container roundtrip mismatch")
        return container

    def section(self, label: bytes) -> Section:
        for section in self.sections:
            if section.label == label:
                return section
        raise ValueError(f"Missing section {label!r}")

    def _key(self) -> int:
        keys = self.section(b"Keys    ").data
        if len(keys) != 12:
            raise ValueError("Unsupported Keys section length")
        return int(struct.unpack_from(">I", keys, 8)[0])

    def decoded(self) -> bytes:
        raw = self.section(b"ROMData ").data
        decoded = transform(raw, self._key())
        if transform(decoded, self._key(), encode=True) != raw:
            raise ValueError("ROM transform roundtrip mismatch")
        return decoded

    def with_rom(self, decoded: bytes) -> Container:
        if len(decoded) != len(self.section(b"ROMData ").data):
            raise ValueError("ROM length cannot change")
        encoded = transform(decoded, self._key(), encode=True)
        return Container(
            self.prefix,
            tuple(
                Section(s.label, s.option, encoded) if s.label == b"ROMData " else s
                for s in self.sections
            ),
        )

    def serialize(self) -> bytes:
        return self.prefix + b"".join(s.serialize() for s in self.sections)
