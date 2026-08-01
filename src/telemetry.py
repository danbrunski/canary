"""Shared telemetry record contract for live collection and storage."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal, get_args

#: Separator between a telemetry source namespace and its channel in a
#: wide-column name, e.g. ``obd:RPM``. Single source of truth: everything
#: that builds or parses a ``<source>:<channel>`` key goes through the
#: helpers below rather than spelling the ``:`` (or the source token)
#: inline, so a namespace rename is a one-line change here.
SOURCE_SEP: Final = ":"

#: Canonical telemetry source namespaces. A row carrying one of these as its
#: ``source`` pivots (via :func:`src.preprocessing.loader.to_wide`) into the
#: wide column ``<source>:<channel>``.
OBD_SOURCE: Final = "obd"
CAN_SOURCE: Final = "can"
DECODED_SOURCE: Final = "decoded"
#: Serial NMEA GPS readings (gps:speed, gps:latitude, ...). A first-class
#: source like OBD/CAN: it rides a ``TelemetryRecord`` and pivots to the wide
#: column ``gps:<channel>``. ``gps:speed`` (km/h) coalesces into the operating-
#: mode segmenter's speed signal; position/quality channels are logged but
#: excluded from model features.
GPS_SOURCE: Final = "gps"
#: 3-axis IMU readings (imu:ACCEL_X/Y/Z, imu:GYRO_X/Y/Z, and — post
#: ``src.preprocessing.imu_frame`` gravity/heading resolution —
#: imu:ACCEL_LON/LAT). Phase 2a (docs/roadmap/imu-gps-integration.md): a
#: first-class source like GPS/OBD, no hardware required to exist in the
#: Literal — ``ImuConfig.source: disabled`` is the default until Phase 2b.
IMU_SOURCE: Final = "imu"
#: DBC-decoded values share the wide-column namespace contract but never ride
#: a ``TelemetryRecord`` (``decode_can_rows_dbc`` writes ``source_channel``
#: directly), so ``DBC_SOURCE`` is deliberately *not* a ``TelemetrySource``.
DBC_SOURCE: Final = "dbc"
#: Computed performance channels (fuel rate, economy, trip integrals,
#: boost/vacuum, estimated power/torque — Task 1.1). Unlike ``DBC_SOURCE``/
#: ``did`` (pseudo-sources that only ever exist as wide-column prefixes),
#: derived channels have no raw record to decode from later — the live tick
#: that computes them is the only place the value ever exists — so, like
#: ``DECODED_SOURCE``, they ride a real ``TelemetryRecord`` and get persisted.
DERIVED_SOURCE: Final = "derived"
#: Imported UpRev Osiris K-line datalog rows (``tools/import_uprev_log.py``).
#: Never produced by the live daemon — only ever lands in a quarantined
#: ``_uprev/<session>`` directory (excluded from training discovery /
#: ``query_telemetry``'s unscoped sweep by the leading-underscore
#: convention, same as ``_archive``), so a foreign log format riding this
#: source can never reach the training corpus or a live capture.
UPREV_SOURCE: Final = "uprev"

TelemetrySource = Literal["obd", "can", "decoded", "gps", "derived", "imu", "uprev"]
# PEP 586 forbids spelling a Literal from variables, so the line above is the
# one unavoidable duplication. Guard it against silently drifting from the
# constants it must mirror.
assert set(get_args(TelemetrySource)) == {
    OBD_SOURCE,
    CAN_SOURCE,
    DECODED_SOURCE,
    GPS_SOURCE,
    DERIVED_SOURCE,
    IMU_SOURCE,
    UPREV_SOURCE,
}

TelemetryValue = bool | int | float | str | None
MetadataValue = bool | int | float | str | None


def channel_key(source: str, channel: str) -> str:
    """Return the canonical wide-column name for a telemetry channel."""
    return f"{source}{SOURCE_SEP}{channel}"


def source_of(column: str) -> str | None:
    """Return a wide column's source namespace, or ``None`` if it is unprefixed.

    ``source_of("obd:RPM") == "obd"``; ``source_of("derived_thing") is None``.
    """
    source, sep, _ = column.partition(SOURCE_SEP)
    return source if sep else None


def channel_of(column: str) -> str:
    """Return a wide column's channel, dropping any source namespace.

    The inverse of :func:`channel_key`: ``channel_of("obd:RPM") == "RPM"``;
    an unprefixed name passes through (``channel_of("RPM") == "RPM"``).
    """
    _, sep, channel = column.partition(SOURCE_SEP)
    return channel if sep else column


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
        *,
        payload: bytes | None = None,
        metadata: dict[str, MetadataValue] | None = None,
    ) -> TelemetryRecord:
        """`payload` is the raw response data the decoder consumed. Persisting
        it makes decoder bugs retroactively fixable — the LONG_FUEL_TRIM
        flat-0.0 investigation dead-ended precisely because the raw bytes
        behind a suspect decode were gone."""
        return cls.from_epoch(
            timestamp,
            source="obd",
            channel=pid,
            value=value,
            unit=unit,
            payload=payload,
            metadata=metadata,
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

    @classmethod
    def decoded(
        cls,
        timestamp: datetime,
        channel: str,
        value: TelemetryValue,
        unit: str = "",
        *,
        metadata: dict[str, MetadataValue] | None = None,
    ) -> TelemetryRecord:
        return cls(
            timestamp=timestamp,
            source="decoded",
            channel=channel,
            value=value,
            unit=unit,
            metadata={} if metadata is None else metadata,
        )

    @classmethod
    def derived(
        cls,
        timestamp: datetime,
        channel: str,
        value: TelemetryValue,
        unit: str = "",
        *,
        metadata: dict[str, MetadataValue] | None = None,
    ) -> TelemetryRecord:
        """A computed performance channel (fuel rate, trip integrals, ...)."""
        return cls(
            timestamp=timestamp,
            source="derived",
            channel=channel,
            value=value,
            unit=unit,
            metadata={} if metadata is None else metadata,
        )

    @classmethod
    def gps(
        cls,
        timestamp: float,
        channel: str,
        value: TelemetryValue,
        unit: str = "",
        *,
        metadata: dict[str, MetadataValue] | None = None,
    ) -> TelemetryRecord:
        """A GPS reading (``channel`` like ``speed``/``latitude``) at an epoch ts."""
        return cls.from_epoch(
            timestamp,
            source="gps",
            channel=channel,
            value=value,
            unit=unit,
            metadata=metadata,
        )

    @classmethod
    def imu(
        cls,
        timestamp: float,
        channel: str,
        value: TelemetryValue,
        unit: str = "",
        *,
        metadata: dict[str, MetadataValue] | None = None,
    ) -> TelemetryRecord:
        """An IMU reading (``channel`` like ``ACCEL_X``/``GYRO_Z``) at an epoch ts."""
        return cls.from_epoch(
            timestamp,
            source="imu",
            channel=channel,
            value=value,
            unit=unit,
            metadata=metadata,
        )

    @classmethod
    def uprev(
        cls,
        timestamp: float,
        channel: str,
        value: TelemetryValue,
        unit: str = "",
        *,
        metadata: dict[str, MetadataValue] | None = None,
    ) -> TelemetryRecord:
        """One imported UpRev log row (``channel`` like ``TIMING_ADVANCE``,
        ``COOLANT_TEMP``) at an epoch ts. See ``tools/import_uprev_log.py``."""
        return cls.from_epoch(
            timestamp,
            source="uprev",
            channel=channel,
            value=value,
            unit=unit,
            metadata=metadata,
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
