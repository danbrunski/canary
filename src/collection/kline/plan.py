"""Turn a capture profile's ``kline:`` list into one K-line definition.

Each entry names a DID (catalog name or 4-digit hex, as ``mode22`` does) or a
service-0x23 memory channel (catalog name, as ``memory`` does). A DID becomes
one ``02 hh ll`` descriptor and needs a catalog ``response_length``; a memory
channel becomes one ``83`` byte descriptor per byte of the value it names
(``address + byte_start``, ``byte_length`` bytes). Nothing is guessed: an
unknown name or a DID with no recorded width refuses the whole plan, naming
it, because the reply carries no boundaries and one wrong width misattributes
every value after it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.collection.kline.protocol import (
    DidDescriptor,
    MemoryByteDescriptor,
    ValueDescriptor,
    value_widths,
)
from src.decode.dids import DidMap

#: A 21-reply payload is at most 255 bytes including ``61 lid``.
MAX_REPLY_DATA = 253
#: One ``AC`` frame holds at most 255 payload bytes: ``AC lid`` + descriptors.
MAX_DEFINITION_BYTES = 253


class KlinePlanError(ValueError):
    """A profile's ``kline:`` list cannot be read as asked."""


@dataclass(frozen=True)
class PlannedChannel:
    """One recorded channel: its descriptors, in order, and its value width."""

    channel: str  # ``DID_1201`` or ``MEM_<NAME>``
    descriptors: tuple[ValueDescriptor, ...]
    width: int


@dataclass(frozen=True)
class KlinePlan:
    channels: tuple[PlannedChannel, ...]

    @property
    def descriptors(self) -> tuple[ValueDescriptor, ...]:
        return tuple(d for channel in self.channels for d in channel.descriptors)

    @property
    def reply_bytes(self) -> int:
        return sum(channel.width for channel in self.channels)

    @property
    def descriptor_widths(self) -> tuple[int, ...]:
        """Width of each descriptor's value, in reply order (a RAM byte is 1)."""
        return tuple(
            1 if isinstance(d, MemoryByteDescriptor) else channel.width
            for channel in self.channels
            for d in channel.descriptors
        )


def _did_for(token: str, did_map: DidMap) -> int | None:
    stripped = token.strip()
    normalized = stripped.upper()
    if len(normalized) == 4 and all(c in "0123456789ABCDEF" for c in normalized):
        return int(normalized, 16)
    if normalized.startswith("0X") and len(normalized) == 6:
        return int(normalized, 16)
    for entry in did_map.dids:
        if entry.name == stripped:
            return entry.did
    return None


def resolve_plan(entries: Sequence[str], did_map: DidMap) -> KlinePlan:
    if not entries:
        raise KlinePlanError("a K-line plan needs at least one channel")
    memory = {channel.name.upper(): channel for channel in did_map.memory}
    widths: dict[int, int] = {
        entry.did: entry.response_length
        for entry in did_map.dids
        if entry.response_length is not None
    }
    planned: list[PlannedChannel] = []
    seen: set[str] = set()
    for entry in entries:
        key = entry.strip().upper()
        if key in memory:
            channel = memory[key]
            start = channel.address + channel.byte_start
            descriptors: tuple[ValueDescriptor, ...] = tuple(
                MemoryByteDescriptor(start + offset) for offset in range(channel.byte_length)
            )
            name = f"MEM_{channel.name}"
        else:
            did = _did_for(entry, did_map)
            if did is None:
                raise KlinePlanError(
                    f"kline entry {entry!r} is neither a DID nor a memory channel in the catalog"
                )
            descriptors = (DidDescriptor(did),)
            name = f"DID_{did:04X}"
        if name in seen:
            raise KlinePlanError(f"kline entry {entry!r} repeats {name}")
        seen.add(name)
        try:
            width = sum(value_widths(descriptors, widths))
        except ValueError as exc:
            raise KlinePlanError(
                f"kline entry {entry!r}: {exc}; read it once with 22 to learn its width, "
                "then record response_length in data/dids.yaml"
            ) from exc
        planned.append(PlannedChannel(name, descriptors, width))
    plan = KlinePlan(tuple(planned))
    definition = sum(3 if isinstance(d, DidDescriptor) else 5 for d in plan.descriptors)
    if definition > MAX_DEFINITION_BYTES:
        raise KlinePlanError(
            f"definition needs {definition} descriptor bytes; one frame carries "
            f"{MAX_DEFINITION_BYTES}"
        )
    if plan.reply_bytes > MAX_REPLY_DATA:
        raise KlinePlanError(
            f"reply would carry {plan.reply_bytes} value bytes; one frame carries {MAX_REPLY_DATA}"
        )
    return plan


def split_reply(plan: KlinePlan, values: Sequence[bytes]) -> list[tuple[str, bytes]]:
    """Group per-descriptor value bytes back into channels, in plan order."""
    out: list[tuple[str, bytes]] = []
    index = 0
    for channel in plan.channels:
        count = len(channel.descriptors)
        out.append((channel.channel, b"".join(values[index : index + count])))
        index += count
    if index != len(values):
        raise KlinePlanError(f"reply had {len(values)} values, the plan {index}")
    return out


__all__ = [
    "KlinePlan",
    "KlinePlanError",
    "PlannedChannel",
    "resolve_plan",
    "split_reply",
]
