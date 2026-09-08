"""Bounded support discovery as scheduler work (roadmap task C06).

Support discovery — the SAE J1979 walk over Mode 01 PID groups
``0x00, 0x20, …`` and Mode 06 MID groups — used to run synchronously through
``CanObdClient`` at loop start and again after ECU contact. Here it is a
state machine the collector loop advances **one probe per budget slot**,
through the tapped client, only when the eligibility matrix admits Mode
01-side traffic (``request_eligibility``), so it can neither consume raw
capture frames untapped nor poll into a held Mode-22 session.

The walk is pure data (:class:`SupportWalk`), replaced on every probe. It
keeps every responder's bitmap apart and merges them for the plan, follows
the continuation bit group by group, and separates *answered* from
*refused* (NRC) from *silent* (timeout): only an answer or a refusal that
means "never" completes a walk. A timeout is re-probed after
:data:`DISCOVERY_RETRY_SECONDS`, or as soon as ordinary ECU contact is
observed, and is never recorded as proof that a service is unsupported.

State survives a reconnect in a process-wide registry keyed by vehicle
identity (VIN, else make-model-year), so a bus rebuild resumes the walk
where it was instead of re-probing groups that already answered.
``reset_discovery`` bumps the cache version and clears it; the loop restarts
every walk when the version it started under is gone.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from src.collection.isotp import PERMANENT_REFUSAL_NRCS, ObdResponse, nrc_name

MODE01_SUPPORT_GROUPS: tuple[int, ...] = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0)
MODE06_SUPPORT_GROUPS: tuple[int, ...] = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0)
#: Re-probe a silent group no sooner than this (the historic Mode 06 retry
#: cadence). ECU contact re-arms it immediately.
DISCOVERY_RETRY_SECONDS = 30.0
SUPPORT_BITMAP_BYTES = 4
#: Lowest bitmask bit: "more identifiers in the next group".
CONTINUATION_BIT = 0x01
#: NRCs that end a walk at the current group: the service, or this group, is
#: not offered. Anything else (busy, conditions-not-correct) is "not now".
WALK_ENDING_NRCS: frozenset[int] = PERMANENT_REFUSAL_NRCS | {0x31}

ProbeResult = Literal["answered", "nrc", "timeout"]
DiscoveryItem = Literal["mode01", "mode06"]


@dataclass(frozen=True)
class SupportProbe:
    """What one support-group request produced, across all responders."""

    responses: tuple[ObdResponse, ...] = ()
    negative_response_code: int | None = None
    #: False when the request never left (TX gate passive): nothing was
    #: learned and the walk must not move.
    sent: bool = True

    @property
    def result(self) -> ProbeResult:
        if self.responses:
            return "answered"
        if self.negative_response_code is not None:
            return "nrc"
        return "timeout"

    def bitmasks(self) -> dict[int, int]:
        """``responder -> 32-bit bitmask`` for every well-formed response."""
        out: dict[int, int] = {}
        for response in self.responses:
            bits = support_bitmask(response)
            if bits is not None:
                out[response.responder_id] = bits
        return out


def support_bitmask(response: ObdResponse) -> int | None:
    if len(response.payload) < SUPPORT_BITMAP_BYTES:
        return None
    return int.from_bytes(response.payload[:SUPPORT_BITMAP_BYTES], byteorder="big")


def bitmask_identifiers(base: int, bits: int) -> frozenset[int]:
    """Identifiers a support bitmask claims, MSB first (``base + 1 …``)."""
    return frozenset(base + 1 + index for index in range(32) if bits & (1 << (31 - index)))


@dataclass(frozen=True)
class SupportWalk:
    """One service's walk over its support groups; replaced on every probe."""

    service: int
    groups: tuple[int, ...]
    position: int = 0
    supported_by_responder: Mapping[int, frozenset[int]] = field(default_factory=dict)
    attempts: int = 0
    seconds_spent: float = 0.0
    last_result: ProbeResult | None = None
    last_nrc: int | None = None
    last_probed_at: float | None = None
    complete: bool = False
    next_probe_at: float = 0.0

    @classmethod
    def start(cls, service: int, groups: tuple[int, ...]) -> SupportWalk:
        return cls(service=service, groups=tuple(groups))

    @property
    def next_group(self) -> int | None:
        if self.complete or self.position >= len(self.groups):
            return None
        return self.groups[self.position]

    @property
    def supported(self) -> tuple[int, ...]:
        """Merged across responders, the probe groups themselves dropped."""
        merged: set[int] = set()
        for identifiers in self.supported_by_responder.values():
            merged |= identifiers
        merged.difference_update(self.groups)
        return tuple(sorted(merged))

    @property
    def responders(self) -> tuple[int, ...]:
        return tuple(sorted(self.supported_by_responder))

    def due(self, now: float) -> bool:
        return not self.complete and self.next_group is not None and now >= self.next_probe_at

    def with_probe(self, probe: SupportProbe, *, now: float, seconds: float = 0.0) -> SupportWalk:
        """Advance on one probe's outcome (pure)."""
        if not probe.sent:
            return self
        base = replace(
            self,
            attempts=self.attempts + 1,
            seconds_spent=self.seconds_spent + max(0.0, seconds),
            last_result=probe.result,
            last_nrc=probe.negative_response_code,
            last_probed_at=now,
        )
        group = self.next_group
        if group is None:
            return replace(base, complete=True)
        if probe.result == "answered":
            bitmasks = probe.bitmasks()
            merged = dict(self.supported_by_responder)
            for responder, bits in bitmasks.items():
                merged[responder] = merged.get(responder, frozenset()) | bitmask_identifiers(
                    group, bits
                )
            more = any(bits & CONTINUATION_BIT for bits in bitmasks.values())
            position = self.position + 1
            return replace(
                base,
                supported_by_responder=merged,
                position=position,
                complete=not more or position >= len(self.groups),
                next_probe_at=now,
            )
        if probe.result == "nrc" and probe.negative_response_code in WALK_ENDING_NRCS:
            # A definitive refusal ends the walk here; groups beyond are not
            # probed (the continuation is conditional on the previous group).
            return replace(base, complete=True)
        # Silent, or refused "not now": try the same group again later. Never
        # persisted as "unsupported" — ``complete`` stays False.
        return replace(base, next_probe_at=now + DISCOVERY_RETRY_SECONDS)

    def on_contact(self, now: float) -> SupportWalk:
        """Ordinary ECU contact re-arms a parked walk.

        Parked means silent (timeout) or refused "not now" (a non-ending NRC
        such as busy / response-pending). Either way an answered poll shows
        the ECU is talking again, so the 30 s park is cut short. A walk ended
        by a definitive NRC is complete and never re-armed.
        """
        if self.complete or self.last_result == "answered" or self.next_probe_at <= now:
            return self
        return replace(self, next_probe_at=now)

    def snapshot(self, now: float) -> dict[str, Any]:
        return {
            "service": f"0x{self.service:02X}",
            "complete": self.complete,
            "pending_group": (f"0x{self.next_group:02X}" if self.next_group is not None else None),
            "groups_answered": self.position,
            "attempts": self.attempts,
            "requests_spent": self.attempts,
            "seconds_spent": round(self.seconds_spent, 4),
            "last_result": self.last_result,
            "last_nrc": (
                f"0x{self.last_nrc:02X} ({nrc_name(self.last_nrc)})"
                if self.last_nrc is not None
                else None
            ),
            "next_probe_in_s": (
                round(max(0.0, self.next_probe_at - now), 3) if not self.complete else None
            ),
            "supported_count": len(self.supported),
            "responders": [f"0x{responder:03X}" for responder in self.responders],
        }


def walk_support(
    probe_group: Callable[[int], SupportProbe],
    *,
    service: int,
    groups: tuple[int, ...],
) -> tuple[int, ...]:
    """Synchronous walk for standalone callers (scripts, probe tools).

    The same transition function as the loop-driven walk, so there is one
    support-map parser; the walk stops at the first silent or refused group
    and returns what it has.
    """
    walk = SupportWalk.start(service, groups)
    while (group := walk.next_group) is not None:
        walk = walk.with_probe(probe_group(group), now=0.0)
        if walk.last_result != "answered":
            break
    return walk.supported


@dataclass(frozen=True)
class DiscoveryState:
    """Every discovery item the loop owes, plus the cache version it runs under."""

    key: str | None
    version: int
    walks: Mapping[DiscoveryItem, SupportWalk] = field(default_factory=dict)

    def pending(self) -> tuple[DiscoveryItem, ...]:
        return tuple(item for item, walk in self.walks.items() if not walk.complete)

    def due(self, now: float) -> DiscoveryItem | None:
        for item, walk in self.walks.items():
            if walk.due(now):
                return item
        return None

    def with_walk(self, item: DiscoveryItem, walk: SupportWalk) -> DiscoveryState:
        return replace(self, walks={**self.walks, item: walk})

    def on_contact(self, now: float) -> DiscoveryState:
        return replace(
            self, walks={item: walk.on_contact(now) for item, walk in self.walks.items()}
        )

    def snapshot(self, now: float, *, deferred_reason: str | None = None) -> dict[str, Any]:
        pending = self.pending()
        return {
            "key": self.key,
            "cache_version": self.version,
            "pending": list(pending),
            "deferred_reason": deferred_reason if pending else None,
            "requests_spent": sum(walk.attempts for walk in self.walks.values()),
            "seconds_spent": round(sum(walk.seconds_spent for walk in self.walks.values()), 4),
            "items": {item: walk.snapshot(now) for item, walk in self.walks.items()},
        }


def vehicle_key(vin: str | None, make: str = "", model: str = "", year: int | None = None) -> str:
    """Cache key for one vehicle: the VIN, else ``make-model-year``."""
    if vin and vin.strip():
        return vin.strip().upper()
    parts = [part for part in (make, model, str(year) if year is not None else "") if part]
    return "-".join(part.strip().lower().replace(" ", "-") for part in parts) or "unknown"


_lock = threading.Lock()
_version = 1
_remembered: dict[str, DiscoveryState] = {}


def cache_version() -> int:
    with _lock:
        return _version


def fresh(
    key: str | None, wanted: Mapping[DiscoveryItem, tuple[int, tuple[int, ...]]]
) -> DiscoveryState:
    """A state with every wanted walk at its first group."""
    with _lock:
        version = _version
    return DiscoveryState(
        key=key,
        version=version,
        walks={
            item: SupportWalk.start(service, groups) for item, (service, groups) in wanted.items()
        },
    )


def recall(
    key: str | None, wanted: Mapping[DiscoveryItem, tuple[int, tuple[int, ...]]]
) -> DiscoveryState:
    """The remembered state for ``key`` under the current version, else fresh.

    A remembered walk is reused only for items still wanted; items the spec
    no longer asks for are dropped and newly wanted ones start fresh. A
    reconnect therefore resumes from the last answered group.
    """
    start = fresh(key, wanted)
    if key is None:
        return start
    with _lock:
        remembered = _remembered.get(key)
    if remembered is None or remembered.version != start.version:
        return start
    walks = {
        item: remembered.walks.get(item, walk)
        if remembered.walks.get(item, walk).service == walk.service
        else walk
        for item, walk in start.walks.items()
    }
    return replace(start, walks=walks)


def remember(state: DiscoveryState) -> None:
    if state.key is None:
        return
    with _lock:
        if state.version == _version:
            _remembered[state.key] = state


def reset_discovery(key: str | None = None) -> int:
    """Invalidate cached capability: one vehicle, or everything. Returns the new version."""
    global _version
    with _lock:
        _version += 1
        if key is None:
            _remembered.clear()
        else:
            _remembered.pop(key, None)
        return _version


def registry_snapshot(now: float) -> dict[str, Any]:
    with _lock:
        return {
            "cache_version": _version,
            "vehicles": {key: state.snapshot(now) for key, state in _remembered.items()},
        }


__all__ = [
    "DISCOVERY_RETRY_SECONDS",
    "MODE01_SUPPORT_GROUPS",
    "MODE06_SUPPORT_GROUPS",
    "WALK_ENDING_NRCS",
    "DiscoveryItem",
    "DiscoveryState",
    "ProbeResult",
    "SupportProbe",
    "SupportWalk",
    "bitmask_identifiers",
    "cache_version",
    "fresh",
    "recall",
    "registry_snapshot",
    "remember",
    "reset_discovery",
    "support_bitmask",
    "vehicle_key",
    "walk_support",
]
