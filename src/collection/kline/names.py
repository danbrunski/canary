"""What a K-line value row is called: ``<module>:<item>``, never a bare DID.

After StartCommunication a KWP2000 session's frames carry no address, so a
``22 12 10`` means the ECM's TPS2 in one session and the ABS's yaw rate in the
next (2026-09-24). Every value row therefore names the module whose session it
came from, by the vehicle profile's name for that address
(:meth:`~src.collection.kline.profile.KlineProfile.module_label`):

- ``ABS:1303`` — DID 0x1303 read from the module named ABS;
- ``28:1303`` — the same from an address the profile does not name;
- ``unknown:1303`` — seen before any StartCommunication (the capture attached
  mid-session), so the module is not known and is not guessed;
- ``ECM:RAM_FFFF13FE`` — one RAM byte from a local-id definition;
- ``ECM:MEM_<NAME>`` — a catalog memory channel CANary read itself.

**The legacy form.** Rows recorded before K01.1 (2026-09-24) are stored as
``DID_<hex>``, ``RAM_<hex>`` and ``MEM_<NAME>``, with no module. Stored rows
are never rewritten; a reader meets them by :func:`is_legacy`, and
``src.analysis.kline_exchanges.attribute_modules`` recovers their module from
the session's own ``FRAME`` rows where the StartCommunication was captured.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Pre-K01.1 value-row prefixes, module unrecorded. Named so that a reader of
#: an old session handles them on purpose rather than by accident: the ECM's
#: DID catalog once decoded an ABS yaw-rate row as throttle position.
LEGACY_DID_PREFIX = "DID_"
LEGACY_RAM_PREFIX = "RAM_"
LEGACY_MEM_PREFIX = "MEM_"
LEGACY_PREFIXES = (LEGACY_DID_PREFIX, LEGACY_RAM_PREFIX, LEGACY_MEM_PREFIX)

#: The raw-frame channel; it carries its module in metadata, not in the name.
FRAME_CHANNEL = "FRAME"
RAM_PREFIX = "RAM_"
MEM_PREFIX = "MEM_"


def did_channel(module: str, did: int) -> str:
    return f"{module}:{did:04X}"


def ram_channel(module: str, address: int) -> str:
    return f"{module}:{RAM_PREFIX}{address:08X}"


def mem_channel(module: str, name: str) -> str:
    return f"{module}:{MEM_PREFIX}{name}"


#: Prefix of a capture-profile ``kline:`` entry's key in the capture ledger
#: (``capture_lifetime``) and in capture quality's channel table.
CAPTURE_KEY_PREFIX = "kline:"


def capture_key(entry: str) -> str:
    """``ECM_TARGET_EQUIV_RATIO`` -> ``kline:ECM_TARGET_EQUIV_RATIO``.

    Capture quality scores what the profile *asked for*, by the entry as the
    operator wrote it; the collector, which knows which stored channel each
    entry resolved to (``ModulePlan.entry_channels``), observes under this key
    (H05.8). Prefixed so a K-line entry never shares a ledger row with a Mode
    01 PID or a Mode 22 DID of the same spelling.
    """
    return f"{CAPTURE_KEY_PREFIX}{entry.strip()}"


def is_legacy(channel: str) -> bool:
    """A pre-K01.1 value row: no module in its name."""
    return ":" not in channel and channel.startswith(LEGACY_PREFIXES)


@dataclass(frozen=True)
class KlineChannel:
    module: str
    item: str

    @property
    def did(self) -> int | None:
        """The DID this row carries, when it carries one."""
        if len(self.item) == 4:
            try:
                return int(self.item, 16)
            except ValueError:
                return None
        return None


def parse_channel(channel: str) -> KlineChannel | None:
    """``ABS:1303`` -> module ``ABS``, item ``1303``; ``None`` for ``FRAME``
    and for the legacy form (see :func:`is_legacy`)."""
    module, sep, item = channel.partition(":")
    if not sep or not module or not item:
        return None
    return KlineChannel(module, item)


__all__ = [
    "CAPTURE_KEY_PREFIX",
    "FRAME_CHANNEL",
    "LEGACY_DID_PREFIX",
    "LEGACY_MEM_PREFIX",
    "LEGACY_PREFIXES",
    "LEGACY_RAM_PREFIX",
    "KlineChannel",
    "capture_key",
    "did_channel",
    "is_legacy",
    "mem_channel",
    "parse_channel",
    "ram_channel",
]
