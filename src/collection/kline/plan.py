"""Turn a capture profile's ``kline:`` list into one module's read plan.

An entry is ``<MODULE>:<token>`` (``ABS:1202``, ``ABS:ENGINE_SPEED``) or a bare
token, which means the ECM (the only module before K01.5). Every entry must
name the same module: K-line is one wire and holds one session at a time.

**The ECM** (a ``local_id`` reader). Each token names a DID (catalog name or
4-digit hex, as ``mode22`` does) or a service-0x23 memory channel (catalog
name, as ``memory`` does). A DID becomes one ``02 hh ll`` descriptor and needs
a catalog ``response_length``; a memory channel becomes one ``83`` byte
descriptor per byte of the value it names (``address + byte_start``,
``byte_length`` bytes). The whole set is one ``AC`` definition, refused
before it is sent when its reply would pass the module's
``max_local_id_reply_bytes`` (the Xterra's ECM answered ``7F AC 12`` past 61).

**Any other module** (a ``common_id`` reader) reads one ``22 hh ll`` per DID,
as the scan tool did; tokens resolve against that module's profile entry
(``data/<vehicle>/kline.yaml``), whose recorded ``width`` every reply must
match.

Nothing is guessed: an unknown name or a DID with no recorded width refuses
the whole plan, naming it, because a ``21`` reply carries no boundaries and
one wrong width misattributes every value after it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from src.collection.kline.names import MEM_PREFIX, did_channel
from src.collection.kline.profile import KlineProfile, ModuleDid, load_kline_profile
from src.collection.kline.protocol import (
    DidDescriptor,
    MemoryByteDescriptor,
    ValueDescriptor,
    value_widths,
)
from src.decode.dids import DidMap, load_did_map

if TYPE_CHECKING:
    from src.config import CanaryConfig

#: A 21-reply payload is at most 255 bytes including ``61 lid``.
MAX_REPLY_DATA = 253
#: One ``AC`` frame holds at most 255 payload bytes: ``AC lid`` + descriptors.
MAX_DEFINITION_BYTES = 253


class KlinePlanError(ValueError):
    """A profile's ``kline:`` list cannot be read as asked."""


@dataclass(frozen=True)
class PlannedChannel:
    """One recorded channel: its descriptors, in order, and its value width."""

    channel: str  # the item after ``<module>:`` — ``1201`` or ``MEM_<NAME>``
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


def resolve_plan(
    entries: Sequence[str], did_map: DidMap, *, max_reply_bytes: int = MAX_REPLY_DATA
) -> KlinePlan:
    """The ECM's local-id plan: ``entries`` are bare tokens (no module)."""
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
            name = f"{MEM_PREFIX}{channel.name}"
        else:
            did = _did_for(entry, did_map)
            if did is None:
                raise KlinePlanError(
                    f"kline entry {entry!r} is neither a DID nor a memory channel in the catalog"
                )
            descriptors = (DidDescriptor(did),)
            name = f"{did:04X}"
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
    if plan.reply_bytes > max_reply_bytes:
        raise KlinePlanError(
            f"reply would carry {plan.reply_bytes} value bytes; the module answers at most "
            f"{max_reply_bytes} (a longer definition is refused with 7F AC 12)"
        )
    return plan


@dataclass(frozen=True)
class ModulePlan:
    """One K-line session's worth of reads, against one module."""

    address: int
    label: str
    service: str  # ``local_id`` or ``common_id``
    #: ``local_id``: the definition to poll.
    local: KlinePlan | None = None
    #: ``common_id``: the DIDs to read one by one, in order.
    reads: tuple[ModuleDid, ...] = ()
    #: Send TesterPresent after this long without a request (``None``: never).
    tester_present_seconds: float | None = None
    #: Each capture-profile entry, as written, and the ``<module>:<item>``
    #: channel its rows are stored under (two flags of one word share one).
    #: What lets capture quality score the profile's own entries (H05.8).
    #: Not part of equality: ``0x1201`` and ``ECM:0x1201`` are one plan.
    entry_channels: tuple[tuple[str, str], ...] = field(default=(), compare=False)

    def entries_by_channel(self) -> dict[str, tuple[str, ...]]:
        """Stored channel -> the profile entries its rows deliver."""
        out: dict[str, tuple[str, ...]] = {}
        for entry, channel in self.entry_channels:
            out[channel] = (*out.get(channel, ()), entry)
        return out


def split_module(entry: str) -> tuple[str | None, str]:
    """``ABS:1202`` -> (``ABS``, ``1202``); a bare token has no module."""
    module, sep, token = entry.strip().partition(":")
    if not sep:
        return None, module
    return module.strip().upper(), token.strip()


def resolve_module_plan(
    entries: Sequence[str], profile: KlineProfile, did_map: DidMap
) -> ModulePlan:
    """Resolve a capture profile's ``kline:`` list against the vehicle's
    K-line profile. Refuses (:class:`KlinePlanError`) rather than guesses."""
    if not entries:
        raise KlinePlanError("a K-line plan needs at least one channel")
    ecm_label = profile.module_label(profile.ecm_address)
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        named, token = split_module(entry)
        grouped.setdefault(named or ecm_label, []).append(token)
    if len(grouped) > 1:
        raise KlinePlanError(
            f"kline entries name {len(grouped)} modules ({', '.join(sorted(grouped))}); "
            "K-line is one wire and holds one session at a time"
        )
    ((label, tokens),) = grouped.items()
    stripped = tuple(entry.strip() for entry in entries)
    try:
        address = profile.module_address(label)
    except LookupError as exc:
        raise KlinePlanError(str(exc)) from exc
    module = profile.modules.get(address)
    if address == profile.ecm_address:
        reader = module.reader if module is not None else None
        if reader is not None and reader.read_service != "local_id":
            raise KlinePlanError(f"{label}: only a local_id reader is built for the ECM")
        cap = (
            reader.max_local_id_reply_bytes
            if reader is not None and reader.max_local_id_reply_bytes is not None
            else MAX_REPLY_DATA
        )
        local = resolve_plan(tokens, did_map, max_reply_bytes=cap)
        return ModulePlan(
            address,
            label,
            "local_id",
            local=local,
            tester_present_seconds=reader.tester_present_seconds if reader else None,
            # resolve_plan keeps one channel per token, in order, or refuses.
            entry_channels=tuple(
                (entry, f"{label}:{channel.channel}")
                for entry, channel in zip(stripped, local.channels, strict=True)
            ),
        )
    if module is None or module.reader is None:
        raise KlinePlanError(
            f"module {label} has no reader in the vehicle's K-line profile: nothing says "
            "how it may be read, so CANary does not command it"
        )
    if module.reader.read_service != "common_id":
        raise KlinePlanError(f"{label}: a local_id reader is built only for the ECM")
    reads: list[ModuleDid] = []
    entry_channels: list[tuple[str, str]] = []
    for entry, token in zip(stripped, tokens, strict=True):
        try:
            did_entry, _ = module.resolve(token)
        except LookupError as exc:
            raise KlinePlanError(str(exc)) from exc
        if did_entry not in reads:  # two flags of one word are one read
            reads.append(did_entry)
        entry_channels.append((entry, did_channel(label, did_entry.did)))
    return ModulePlan(
        address,
        label,
        "common_id",
        reads=tuple(reads),
        tester_present_seconds=module.reader.tester_present_seconds,
        entry_channels=tuple(entry_channels),
    )


@dataclass(frozen=True)
class KlineCatalog:
    """What a ``kline:`` list is resolved against, and the one loader for it.

    The K-line collector resolves a capture profile's list when it starts a
    session; the capture-profile store resolves the same list when the
    profile is saved (H05.8), so an unknown name or a DID with no recorded
    width is refused at save, with the same message the collector would log,
    instead of the capture sitting idle all drive. One method, so the two can
    never disagree about what is readable.
    """

    #: The vehicle's K-line profile (``daemon.kline_profile``).
    profile_path: Path
    #: The DID / memory catalog (``data/dids.yaml``).
    dids_path: Path
    #: ``hardware.kline.poll_interval_seconds``: a pause past P3max drops a
    #: session whose module never answered TesterPresent.
    poll_interval_seconds: float = 0.0

    @classmethod
    def from_config(cls, config: CanaryConfig) -> KlineCatalog:
        from src.paths import DIDS_YAML

        return cls(
            config.daemon.kline_profile,
            DIDS_YAML,
            config.hardware.kline.poll_interval_seconds,
        )

    def resolve(self, entries: Sequence[str]) -> tuple[KlineProfile, ModulePlan]:
        """Load both catalogs and resolve ``entries``, or refuse.

        Raises :class:`KlinePlanError` for a list that cannot be read as
        asked, ``KlineNotConfiguredError`` (a ``LookupError``) when the
        vehicle has no K-line profile, ``ValueError`` for an unreadable one.
        """
        profile = load_kline_profile(self.profile_path)
        did_map = load_did_map(self.dids_path)
        plan = resolve_module_plan(entries, profile, did_map)
        pause_ms = self.poll_interval_seconds * 1000.0
        if plan.tester_present_seconds is None and pause_ms >= profile.timing.p3_max_ms:
            raise KlinePlanError(
                f"poll_interval_seconds {self.poll_interval_seconds:g} outlasts "
                f"P3max ({profile.timing.p3_max_ms:g} ms) and {plan.label} was never seen "
                "to answer TesterPresent, so the session would drop between polls"
            )
        return profile, plan


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
    "KlineCatalog",
    "KlinePlan",
    "KlinePlanError",
    "ModulePlan",
    "PlannedChannel",
    "resolve_module_plan",
    "resolve_plan",
    "split_module",
    "split_reply",
]
