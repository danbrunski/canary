"""Shared telemetry record contract for live collection and storage."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

TelemetrySource = Literal["obd", "can"]
TelemetryValue = bool | int | float | str | None
MetadataValue = bool | int | float | str | None


@dataclass(frozen=True)
class TelemetryRecord:
    """One normalized telemetry sample from any collection source."""

    timestamp: datetime
    source: TelemetrySource
    channel: str
    value: TelemetryValue = None
    unit: str = ""
    payload: bytes | None = None
    metadata: dict[str, MetadataValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ts = self.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)
        object.__setattr__(self, "timestamp", ts)

    @classmethod
    def from_epoch(
        cls,
        timestamp: float,
        *,
        source: TelemetrySource,
        channel: str,
        value: TelemetryValue = None,
        unit: str = "",
        payload: bytes | None = None,
        metadata: dict[str, MetadataValue] | None = None,
    ) -> TelemetryRecord:
        return cls(
            timestamp=datetime.fromtimestamp(timestamp, tz=UTC),
            source=source,
            channel=channel,
            value=value,
            unit=unit,
            payload=payload,
            metadata={} if metadata is None else metadata,
        )

    @classmethod
    def obd(
        cls,
        timestamp: float,
        pid: str,
        value: TelemetryValue,
        unit: str,
    ) -> TelemetryRecord:
        return cls.from_epoch(
            timestamp,
            source="obd",
            channel=pid,
            value=value,
            unit=unit,
        )

    @classmethod
    def can(
        cls,
        timestamp: float,
        can_id: int,
        payload: bytes,
        *,
        channel: str | None = None,
    ) -> TelemetryRecord:
        return cls.from_epoch(
            timestamp,
            source="can",
            channel=f"0x{can_id:X}" if channel is None else channel,
            payload=payload,
            metadata={"can_id": can_id, "dlc": len(payload)},
        )

    def parquet_row(self) -> dict[str, object]:
        """Return a stable, columnar row representation for Parquet storage."""
        value_number: float | None = None
        value_text: str | None = None
        value_bool: bool | None = None
        if isinstance(self.value, bool):
            value_bool = self.value
        elif isinstance(self.value, (int, float)):
            value_number = float(self.value)
        elif self.value is not None:
            value_text = str(self.value)

        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "channel": self.channel,
            "value_number": value_number,
            "value_text": value_text,
            "value_bool": value_bool,
            "unit": self.unit,
            "payload": self.payload,
            "metadata_json": json.dumps(self.metadata, sort_keys=True),
        }
