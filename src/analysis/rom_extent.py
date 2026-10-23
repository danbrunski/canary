"""Does an ECU image hold the whole flash? Read from the image alone.

A backup reads the range its recipe names (``rom_backup.yaml``). An ECM with
more flash than that range would be read short, and nothing on the bus says so.
The program in the image can: a vector table entry or a call whose target lies
past the image's end, yet below where RAM begins, points at flash the read did
not reach. Everything here is offline, from bytes already read; it never asks
the ECU for anything.

Two kinds of evidence, of different strength:

- **Vector tables** the recipe names (``extent.vector_tables``). Entry 0 is the
  reset address, so it must be an even address inside the image, and every
  entry must be a code address (even), a RAM or I/O address (a stack), or
  erased. A table that is not all of those is not a table here: it is reported
  ``valid: false`` with the entry that broke it, and says nothing. One valid
  table entry past the image is the verdict ``larger``.
- **Call targets** (``extent.call_scan: sh2``): the 32-bit literal an SH-2
  ``mov.l @(disp,PC),Rn`` loads, when ``jsr @Rn`` or ``jmp @Rn`` follows within
  three instructions. Counted across the image and reported, never the
  verdict: data can look like such a pair.

``no_sign`` is not proof. A program that never points at its upper flash leaves
nothing to see, so a backup without a ROM map stays unverified on size either
way. Known answer: the 2013 Xterra's ECU read (sha256 ``437ba37e…3eaf``, an
SH7058 whose boot code names the part) reads ``no_sign``, with all 256 entries
of its application vector table valid and 4,159 call targets, none past
``0x0FFFFF`` (tests/test_rom_extent.py, skipped where the image is absent).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

ExtentVerdict = Literal["larger", "no_sign", "unknown"]

#: An erased flash word.
ERASED_WORD = 0xFFFFFFFF
#: How many instructions after the ``mov.l`` a ``jsr``/``jmp`` may sit.
SH2_CALL_WINDOW = 3
#: Addresses listed in a payload or summary; the counts are always whole.
LISTED_ADDRESSES = 8


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VectorTable(_Strict):
    """A table of 32-bit big-endian addresses whose entry 0 is the reset PC."""

    offset: int = Field(ge=0)
    entries: int = Field(ge=4, le=1024)
    evidence: str = Field(min_length=1)


class ExtentSpec(_Strict):
    """What a vehicle's image says about its own size (``rom_backup.yaml``)."""

    #: From here up, addresses are RAM and I/O, never flash.
    ram_from: int = Field(ge=1, le=0xFFFFFFFF)
    vector_tables: list[VectorTable] = Field(min_length=1)
    #: The instruction set whose call pairs are counted; ``None`` counts none.
    call_scan: Literal["sh2"] | None = None

    @property
    def tables_end(self) -> int:
        """Bytes from 0 the vector tables need: the early check's threshold."""
        return max(t.offset + 4 * t.entries for t in self.vector_tables)


@dataclass(frozen=True)
class TableReading:
    offset: int
    entries: int
    valid: bool
    #: Why the table is not one; ``None`` when valid.
    reason: str | None
    inside: int
    ram: int
    erased: int
    #: Every distinct entry value past the image, ascending.
    beyond: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "offset": f"0x{self.offset:06X}",
            "entries": self.entries,
            "valid": self.valid,
            "reason": self.reason,
            "inside": self.inside,
            "ram": self.ram,
            "erased": self.erased,
            "beyond": [f"0x{v:08X}" for v in self.beyond[:LISTED_ADDRESSES]],
            "beyond_count": len(self.beyond),
        }


@dataclass(frozen=True)
class CallTargets:
    #: ``mov.l``/``jsr``-or-``jmp`` pairs whose literal lies inside the image.
    pairs: int
    inside: int
    ram: int
    #: Distinct targets past the image, ascending.
    beyond: tuple[int, ...]
    #: Pairs whose target is past the image (a target may repeat).
    beyond_pairs: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "pairs": self.pairs,
            "inside": self.inside,
            "ram": self.ram,
            "beyond": [f"0x{v:08X}" for v in self.beyond[:LISTED_ADDRESSES]],
            "beyond_count": len(self.beyond),
            "beyond_pairs": self.beyond_pairs,
        }


@dataclass(frozen=True)
class ExtentReading:
    verdict: ExtentVerdict
    image_size: int
    ram_from: int
    tables: tuple[TableReading, ...]
    calls: CallTargets | None

    def summary(self) -> str:
        """One plain sentence naming what was seen, and where."""
        end = f"0x{self.image_size - 1:06X}"
        valid = [t for t in self.tables if t.valid]
        if self.verdict == "larger":
            pointing = [t for t in valid if t.beyond]
            points = ", ".join(
                f"the vector table at 0x{t.offset:06X} to {_addresses(t.beyond)}" for t in pointing
            )
            return (
                f"the image points past its end ({end}): {points}, so the flash is larger "
                f"than the {self.image_size:,} bytes read"
            )
        if self.verdict == "unknown":
            reasons = "; ".join(f"0x{t.offset:06X}: {t.reason}" for t in self.tables)
            return f"no vector table could be read ({reasons})"
        entries = sum(t.entries for t in valid)
        where = ", ".join(f"0x{t.offset:06X}" for t in valid)
        text = f"nothing points past {end}: {entries:,} vector-table entries (tables at {where})"
        calls = self.calls
        if calls is None:
            return text + " land inside the image, in RAM or erased"
        text += f" and {calls.inside + calls.ram:,} call targets land inside the image or in RAM"
        if calls.beyond_pairs:
            text += (
                f"; {calls.beyond_pairs:,} call-like pair(s) target {_addresses(calls.beyond)}, "
                "past it, which no vector table confirms"
            )
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "summary": self.summary(),
            "image_size": self.image_size,
            "ram_from": f"0x{self.ram_from:08X}",
            "tables": [t.to_dict() for t in self.tables],
            "calls": None if self.calls is None else self.calls.to_dict(),
        }


def _addresses(values: tuple[int, ...]) -> str:
    shown = ", ".join(f"0x{v:08X}" for v in values[:LISTED_ADDRESSES])
    more = len(values) - LISTED_ADDRESSES
    return shown + (f" and {more:,} more" if more > 0 else "")


def read_vector_table(
    data: bytes, table: VectorTable, *, image_size: int, ram_from: int
) -> TableReading:
    """One table, read from ``data`` (the image or a prefix of it) and judged
    against the whole image's ``image_size``."""
    end = table.offset + 4 * table.entries
    if end > image_size:
        return _invalid(table, f"the table runs to 0x{end:06X}, past the image's end")
    if end > len(data):
        return _invalid(table, f"only 0x{len(data):06X} bytes were given, short of 0x{end:06X}")
    words = np.frombuffer(data, dtype=">u4", count=table.entries, offset=table.offset)
    values = words.astype(np.int64)
    reset = int(values[0])
    if reset % 2 or reset >= image_size:
        return _invalid(table, f"entry 0 is 0x{reset:08X}, not a reset address inside the image")
    erased = values == ERASED_WORD
    ram = (values >= ram_from) & ~erased
    even = values % 2 == 0
    bad = ~(erased | ram | even)
    if bad.any():
        index = int(np.flatnonzero(bad)[0])
        return _invalid(
            table, f"entry {index} is 0x{int(values[index]):08X}, an odd address below RAM"
        )
    inside = even & (values < image_size)
    beyond = even & (values >= image_size) & ~ram & ~erased
    return TableReading(
        offset=table.offset,
        entries=table.entries,
        valid=True,
        reason=None,
        inside=int(inside.sum()),
        ram=int(ram.sum()),
        erased=int(erased.sum()),
        beyond=tuple(sorted({int(v) for v in values[beyond]})),
    )


def _invalid(table: VectorTable, reason: str) -> TableReading:
    return TableReading(
        offset=table.offset,
        entries=table.entries,
        valid=False,
        reason=reason,
        inside=0,
        ram=0,
        erased=0,
        beyond=(),
    )


def scan_sh2_calls(image: bytes, *, ram_from: int) -> CallTargets:
    """Targets of ``mov.l @(disp,PC),Rn`` followed by ``jsr``/``jmp @Rn``."""
    usable = len(image) - len(image) % 4
    halves = np.frombuffer(image, dtype=">u2", count=usable // 2).astype(np.int64)
    words = np.frombuffer(image, dtype=">u4", count=usable // 4).astype(np.int64)
    loads = np.flatnonzero((halves & 0xF000) == 0xD000)
    register = (halves[loads] >> 8) & 0xF
    called = np.zeros(len(loads), dtype=bool)
    for step in range(1, SH2_CALL_WINDOW + 1):
        later = loads + step
        ok = later < len(halves)
        nxt = np.full(len(loads), -1, dtype=np.int64)
        nxt[ok] = halves[later[ok]]
        called |= (nxt == (0x400B | (register << 8))) | (nxt == (0x402B | (register << 8)))
    pcs = loads[called] * 2
    literal = (pcs & ~3) + 4 + (halves[loads[called]] & 0xFF) * 4
    literal = literal[literal + 4 <= usable]
    targets = words[literal // 4]
    erased = targets == ERASED_WORD
    ram = (targets >= ram_from) & ~erased
    inside = targets < len(image)
    beyond = ~inside & ~ram & ~erased
    return CallTargets(
        pairs=len(targets),
        inside=int(inside.sum()),
        ram=int(ram.sum()),
        beyond=tuple(sorted({int(v) for v in targets[beyond]})),
        beyond_pairs=int(beyond.sum()),
    )


def read_vector_tables(
    data: bytes, spec: ExtentSpec, *, image_size: int
) -> tuple[TableReading, ...]:
    """Every table the spec names, from ``data`` (a prefix is enough once it
    reaches :attr:`ExtentSpec.tables_end`)."""
    return tuple(
        read_vector_table(data, t, image_size=image_size, ram_from=spec.ram_from)
        for t in spec.vector_tables
    )


def tables_verdict(tables: tuple[TableReading, ...]) -> ExtentVerdict:
    valid = [t for t in tables if t.valid]
    if any(t.beyond for t in valid):
        return "larger"
    return "no_sign" if valid else "unknown"


def read_extent(image: bytes, spec: ExtentSpec) -> ExtentReading:
    """The whole image's reading: its vector tables, then its call targets."""
    tables = read_vector_tables(image, spec, image_size=len(image))
    calls = scan_sh2_calls(image, ram_from=spec.ram_from) if spec.call_scan == "sh2" else None
    return ExtentReading(
        verdict=tables_verdict(tables),
        image_size=len(image),
        ram_from=spec.ram_from,
        tables=tables,
        calls=calls,
    )


def early_extent(prefix: bytes, spec: ExtentSpec, *, image_size: int) -> ExtentReading:
    """The tables alone, from the first bytes of a read still in progress."""
    tables = read_vector_tables(prefix, spec, image_size=image_size)
    return ExtentReading(
        verdict=tables_verdict(tables),
        image_size=image_size,
        ram_from=spec.ram_from,
        tables=tables,
        calls=None,
    )


__all__ = [
    "CallTargets",
    "ExtentReading",
    "ExtentSpec",
    "ExtentVerdict",
    "TableReading",
    "VectorTable",
    "early_extent",
    "read_extent",
    "read_vector_table",
    "read_vector_tables",
    "scan_sh2_calls",
    "tables_verdict",
]
