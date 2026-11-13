"""When the broadcast bus was last heard, for ``vehicle_state`` (P02.10, R27.6).

Freshness reads the raw frame stream: every CAN frame whose id has a binding
or a DBC message is the bus being alive, whether or not the storage change
filter keeps it. The decoded channels' observation ages follow the
change-only stream, where an unchanged id is observed once per re-write
interval, so they cannot answer this alone.

Pure: no I/O and no clock of its own. The live source replaces it with a
fresh instance on ``reset`` (the replay-isolation boundary).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from src.can_ids import parse_can_channel
from src.telemetry import TelemetryRecord


def _utc(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


@dataclass
class BusFreshness:
    """The newest decodable CAN frame's stamp.

    ``decodable_can_ids`` is fixed by the source's bindings and DBC database
    at construction; ``last_frame_at`` is the state.
    """

    decodable_can_ids: frozenset[int] = frozenset()
    last_frame_at: datetime | None = None

    def decodes(self, record: TelemetryRecord) -> bool:
        """Whether a raw CAN frame's id has a binding or a DBC message."""
        if not self.decodable_can_ids:
            return False
        can_id = parse_can_channel(record.channel)
        return can_id is not None and can_id in self.decodable_can_ids

    def observe(self, record: TelemetryRecord) -> None:
        """Stamp a raw CAN frame that decodes, kept by the change filter or not."""
        if self.decodes(record):
            self.last_frame_at = _utc(record.timestamp)

    def seed(self, heard_at: datetime) -> None:
        """When the bus was last heard before this process started.

        Freshness only ever compares this against the 10 s threshold, so a
        stored stamp cannot make a sleeping bus look alive; it makes a
        restart report "silent for 3 h" instead of "never heard". A frame
        seen since start always wins.
        """
        if self.last_frame_at is None:
            self.last_frame_at = _utc(heard_at)

    def frame_age_seconds(self, now: datetime) -> float | None:
        """Seconds since the newest decodable frame at ``now``, or ``None``."""
        if self.last_frame_at is None:
            return None
        return (_utc(now) - self.last_frame_at).total_seconds()


__all__ = ["BusFreshness"]
