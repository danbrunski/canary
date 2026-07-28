"""Ad-hoc raw CAN activity capture (``can_activity`` tool).

The RE workhorse: capture N seconds of broadcast traffic and summarize, per
arbitration id, how often it appeared and which byte positions actually
varied. Pairs with the "tag physical actions, not screen selections"
convention (AGENTS.md) — call this immediately around pressing the brake,
flipping a switch, etc., and the ``changed_byte_indices`` list *is* the
answer to "which ID carries that signal," without an offline
``candump``-and-diff round trip.

Pure listen — no ``bus.send`` anywhere in this module, so unlike
``obd_request`` this never touches :mod:`~src.collection.tx_gate` and has no
passive-mode interaction. Bus access model matches
:mod:`~src.control.obd_request`: a short-lived handle opened alongside the
daemon's own (fine on SocketCAN's multi-handle model; a single-handle
transport like ``slcan`` raises :class:`CanActivityBusError` instead of
stealing bytes from the daemon's stream).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import can

from src.collection.can_collector import bus_factory
from src.config import CanBusConfig

DEFAULT_SECONDS = 5.0
#: Runs synchronously in an HTTP worker thread — capped independent of
#: caller input so a large `seconds` can't tie one up indefinitely.
MAX_SECONDS = 30.0
#: Frames captured, not bytes stored — each id's own state is already
#: bounded (a byte value is 0-255, so its per-index set tops out at 256
#: regardless of frame count). This just stops a saturated bus from running
#: the loop past its wall-clock budget on backlog draining.
DEFAULT_MAX_FRAMES = 200_000


class CanActivityBusError(RuntimeError):
    """The bus couldn't be opened (port already exclusively held, etc.)."""


@dataclass
class _IdAccumulator:
    count: int = 0
    first_hex: str = ""
    last_hex: str = ""
    #: Distinct values seen at each byte offset, grown lazily up to the
    #: longest frame this id has sent. `len(values) > 1` at index i means
    #: byte i changed during the capture.
    byte_values: list[set[int]] = field(default_factory=list)

    def observe(self, data: bytes) -> None:
        self.count += 1
        if not self.first_hex:
            self.first_hex = data.hex()
        self.last_hex = data.hex()
        while len(self.byte_values) < len(data):
            self.byte_values.append(set())
        for index, value in enumerate(data):
            self.byte_values[index].add(value)


@dataclass(frozen=True)
class CanIdActivity:
    """One arbitration id's summary over the capture window."""

    arbitration_id: int
    frame_count: int
    hz: float
    first_hex: str
    last_hex: str
    changed_byte_indices: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": f"0x{self.arbitration_id:03X}",
            "frame_count": self.frame_count,
            "hz": round(self.hz, 3),
            "first_hex": self.first_hex,
            "last_hex": self.last_hex,
            "changed_byte_indices": list(self.changed_byte_indices),
        }


@dataclass(frozen=True)
class CanActivityResult:
    duration_seconds: float
    frames_captured: int
    #: True if `max_frames` cut the capture short of its full `seconds` —
    #: per-id stats up to that point are still valid, just incomplete.
    truncated: bool
    ids: tuple[CanIdActivity, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_seconds": round(self.duration_seconds, 3),
            "frames_captured": self.frames_captured,
            "truncated": self.truncated,
            "ids": [entry.to_dict() for entry in self.ids],
        }


def capture_can_activity(
    can_config: CanBusConfig,
    *,
    seconds: float,
    ids: Sequence[int] | None = None,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> CanActivityResult:
    """Listen for ``seconds`` and summarize traffic per arbitration id.

    ``ids`` restricts the capture to those arbitration ids (default: every
    id on the bus) — useful once a prior wide capture has narrowed the
    candidates and a follow-up call wants a cleaner window on just those.
    """
    effective_seconds = min(MAX_SECONDS, max(0.05, seconds))
    id_filter = set(ids) if ids else None
    try:
        bus = bus_factory(can_config)
    except (OSError, can.CanError) as exc:
        raise CanActivityBusError(
            f"couldn't open {can_config.interface}:{can_config.channel} — {exc}"
        ) from exc

    accumulators: dict[int, _IdAccumulator] = {}
    frames_captured = 0
    truncated = False
    deadline = time.monotonic() + effective_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            frame = bus.recv(timeout=min(0.5, remaining))
            if frame is None:
                continue
            if id_filter is not None and frame.arbitration_id not in id_filter:
                continue
            frames_captured += 1
            accumulators.setdefault(frame.arbitration_id, _IdAccumulator()).observe(
                bytes(frame.data)
            )
            if frames_captured >= max_frames:
                truncated = True
                break
    finally:
        bus.shutdown()

    ids_out = tuple(
        CanIdActivity(
            arbitration_id=arb_id,
            frame_count=acc.count,
            hz=acc.count / effective_seconds if effective_seconds > 0 else 0.0,
            first_hex=acc.first_hex,
            last_hex=acc.last_hex,
            changed_byte_indices=tuple(
                index for index, values in enumerate(acc.byte_values) if len(values) > 1
            ),
        )
        for arb_id, acc in sorted(accumulators.items())
    )
    return CanActivityResult(
        duration_seconds=effective_seconds,
        frames_captured=frames_captured,
        truncated=truncated,
        ids=ids_out,
    )


__all__ = [
    "DEFAULT_MAX_FRAMES",
    "DEFAULT_SECONDS",
    "MAX_SECONDS",
    "CanActivityBusError",
    "CanActivityResult",
    "CanIdActivity",
    "capture_can_activity",
]
