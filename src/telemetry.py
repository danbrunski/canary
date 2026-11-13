"""Shared telemetry record contract for live collection and storage."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from types import MappingProxyType
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
#: imu:ACCEL_LON/LAT). Phase 2a (docs/roadmap/archive/imu-gps-integration.md): a
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
#: that computes them is the only place the value ever exists — so they ride a
#: real ``TelemetryRecord`` and get persisted. (``DECODED_SOURCE`` rows ride
#: one too, but are never persisted since P02.8: their raw frame is.)
DERIVED_SOURCE: Final = "derived"
#: Imported UpRev Osiris K-line datalog rows (``tools/import_uprev_log.py``).
#: Never produced by the live daemon — only ever lands in a quarantined
#: ``_uprev/<session>`` directory (excluded from training discovery /
#: ``query_telemetry``'s unscoped sweep by the leading-underscore
#: convention, same as ``_archive``), so a foreign log format riding this
#: source can never reach the training corpus or a live capture.
UPREV_SOURCE: Final = "uprev"
#: Add-on sensors that are not the vehicle's own: analog senders read through
#: a USB ADC (``sensor:FUEL_PRESSURE`` from the AEM X-Series gauge's analog
#: output; ``data/usb_adc_sensors.yaml``), CAN-output sensors on a second,
#: private CAN interface (``data/sensor_can_signals.yaml``), and later boost
#: MAP. A first-class source like GPS: it rides a ``TelemetryRecord`` and is
#: persisted into every session while the hardware is configured. Layouts and
#: calibrations are data, not code.
SENSOR_SOURCE: Final = "sensor"
#: ECU data CANary reads itself over K-line, through the CANary-HAT's own
#: transceiver (``src.collection.kline``): module-scoped values
#: (``kline:<module>:<hex>``, ``kline:ECM:MEM_<NAME>``; K01.1, named by
#: ``src.collection.kline.names``), and raw ``kline:FRAME`` rows carrying the
#: wire bytes so any session can be re-decoded later. Rows stored before K01.1
#: are ``kline:DID_<hex>``/``kline:MEM_<NAME>`` with no module. A different instrument
#: from ``uprev`` (the UpRev cable's logs, import-only) and from ``obd`` (the
#: same DIDs over CAN): lineage decides which instrument a value came from
#: (all-did-tuning-hardening 2.7, 8.5). Not trainable until the operator
#: decides otherwise (H00: it exists only while a K-line session runs).
KLINE_SOURCE: Final = "kline"

#: Metadata key naming a CAN frame that is not a data frame (P02.10):
#: ``"error"`` (a controller error frame, e.g. RX overflow) or ``"remote"``.
CAN_FRAME_KIND_KEY: Final = "frame_kind"
CanFrameKind = Literal["error", "remote"]

#: Metadata key on every record a replay emits: the epoch seconds the row was
#: *recorded* at, before its timestamp was rebased to wall-clock (P02.16). A
#: consumer that must know when the data was recorded, not when it arrived
#: (which calibration was flashed then), reads it from the record itself, so
#: no replay entry point has to tell it a replay is running.
REPLAY_SOURCE_AT_KEY: Final = "replay_source_at"
#: The recorded ``acquired_at`` of a replayed read, unshifted (E04.2). Replay
#: rebases ``acquired_at`` by each row's own emit offset, which changes the
#: spacing between reads by the pacing jitter; a consumer that pairs reads on
#: the acquisition clock (the live twin) reads the original instant here so a
#: replay decides exactly what the stored session decides.
REPLAY_SOURCE_ACQUIRED_KEY: Final = "replay_source_acquired_at"

TelemetrySource = Literal[
    "obd", "can", "decoded", "gps", "derived", "imu", "uprev", "sensor", "kline"
]
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
    SENSOR_SOURCE,
    KLINE_SOURCE,
}

#: Sources the live pipeline **re-derives from a raw row** rather than
#: receiving from a collector: ``decoded`` comes from a raw ``can`` frame
#: (:meth:`src.features.live.LiveFeatureSource._apply_can_decode` for the
#: detectors; the session readers decode it on read, and nothing stores it
#: since P02.8) and ``derived`` from the inference tick's computed channels
#: (:mod:`src.daemon.inference`). Anything that re-injects stored rows into the
#: live queue (replay) must skip these or the detectors read whichever copy
#: arrived last (and, before P02.8, every decoded signal was persisted twice)
#: — see ``data/analyses/2026-09-17-d029-replay-amplification.md``.
RE_DERIVED_SOURCES: Final[frozenset[TelemetrySource]] = frozenset({DECODED_SOURCE, DERIVED_SOURCE})
#: Sources that only ever originate at a collector/importer: nothing
#: downstream can reconstruct them, so they are always safe to re-emit.
CAPTURED_SOURCES: Final[frozenset[TelemetrySource]] = frozenset(
    {OBD_SOURCE, CAN_SOURCE, GPS_SOURCE, IMU_SOURCE, UPREV_SOURCE, SENSOR_SOURCE, KLINE_SOURCE}
)
# Every source must be classified as one or the other. A new entry in
# ``TelemetrySource`` therefore fails here at import time until someone
# decides whether it is re-derived — the alternative is a future source
# silently doubling itself through replay, which is the defect this pair
# exists to close.
assert RE_DERIVED_SOURCES | CAPTURED_SOURCES == set(get_args(TelemetrySource))
assert not (RE_DERIVED_SOURCES & CAPTURED_SOURCES)

#: H01.1 — sources whose channels may train and score the anomaly detectors
#: (Isolation Forest, sequence detector) and behavior models. An **allowlist**
#: of ECU-origin sources: a source is eligible because it is listed here, and a
#: source nobody classified is not, so a new source cannot become a feature by
#: being forgotten. ``can`` raw frames never become features themselves; their
#: engineering values arrive as ``decoded``. This is the *source* gate, over
#: channels. Which *capture profile's* sessions the detectors may train,
#: calibrate, evaluate or score on (tuning captures: none of these, operator
#: 2026-09-24, R03.12) is a separate, profile-level gate, over sessions.
TRAINABLE_SOURCES: Final[frozenset[TelemetrySource]] = frozenset({OBD_SOURCE, DECODED_SOURCE})
#: Sources that are not origins: a ``derived`` channel is trainable only when
#: every input it is computed from is (``src.trainability``, reading
#: ``src.signals.derived.DERIVED_CHANNEL_INPUTS``). "Derived" says how a value
#: was made, not where it came from.
INPUT_GATED_SOURCES: Final[frozenset[TelemetrySource]] = frozenset({DERIVED_SOURCE})
#: Every other source, with the reason it never trains. Surfaced verbatim in
#: the ``source_not_trainable`` exclusion reason and in manifests.
NON_TRAINABLE_SOURCES: Final[Mapping[TelemetrySource, str]] = MappingProxyType(
    {
        CAN_SOURCE: "raw CAN frames; their values train as `decoded`",
        GPS_SOURCE: "external sensor (H00 operator decision 1, 2026-09-23)",
        IMU_SOURCE: "external sensor (H00 operator decision 1, 2026-09-23)",
        SENSOR_SOURCE: "external sensor (H00 operator decision 1, 2026-09-23)",
        UPREV_SOURCE: "imported UpRev log, a foreign instrument kept out of the corpus",
        KLINE_SOURCE: (
            "exists only while a K-line session runs; not trainable until the "
            "operator decides (H00 K-line defaults)"
        ),
    }
)
# The allowlist names only real sources (H01.1).
assert TRAINABLE_SOURCES <= set(get_args(TelemetrySource))


def require_trainability_classified(
    sources: Collection[str],
    trainable: Collection[str],
    input_gated: Collection[str],
    non_trainable: Collection[str],
) -> None:
    """Refuse unless every source is classified for training exactly once.

    Called below at import time over ``TelemetrySource``, so a source added to
    the Literal without a trainability decision fails the first import of this
    module — the alternative is the IMU arriving in the next Isolation Forest
    fit and leaving with the hardware (the D02.5 failure shape in reverse).
    A raise, not an ``assert``, so ``python -O`` cannot skip it.
    """
    classes = (set(trainable), set(input_gated), set(non_trainable))
    unclassified = set(sources) - set().union(*classes)
    if unclassified:
        raise TypeError(
            f"telemetry sources {sorted(unclassified)} have no trainability classification: "
            "add each to TRAINABLE_SOURCES, INPUT_GATED_SOURCES or NON_TRAINABLE_SOURCES "
            "(src/telemetry.py, H01.1)"
        )
    unknown = set().union(*classes) - set(sources)
    if unknown:
        raise TypeError(f"trainability classification names non-sources {sorted(unknown)}")
    overlap = sorted(
        (classes[0] & classes[1]) | (classes[0] & classes[2]) | (classes[1] & classes[2])
    )
    if overlap:
        raise TypeError(f"telemetry sources {overlap} are classified more than once")


require_trainability_classified(
    get_args(TelemetrySource), TRAINABLE_SOURCES, INPUT_GATED_SOURCES, NON_TRAINABLE_SOURCES
)

TelemetryValue = bool | int | float | str | None
MetadataValue = bool | int | float | str | None

#: Entries the metadata-JSON memo keeps (least recently used out). A CAN row's
#: metadata is ``{"can_id", "dlc"}`` plus an optional ``frame_kind``: a few dozen
#: distinct values per drive, so this is room for every one.
METADATA_JSON_CACHE_SIZE: Final = 4096

#: Value types whose equality is JSON equality, the only ones the memo may key
#: on. ``True == 1`` and ``1.0 == 1`` in a Python key, yet ``true``, ``1`` and
#: ``1.0`` are three different strings, so a memo that let a ``bool`` or a
#: ``float`` through would hand one row another row's ``metadata_json``. A
#: float is also what makes a row's metadata unique (``acquired_at``), which no
#: memo helps. Anything else is serialized directly, so an unhashable value
#: (a list in a hand-built record) is an ordinary miss, never an error.
_MEMOIZABLE_METADATA_TYPES: Final = frozenset({str, int, type(None)})


@lru_cache(maxsize=METADATA_JSON_CACHE_SIZE)
def _memoized_metadata_json(items: tuple[tuple[str, MetadataValue], ...]) -> str:
    return json.dumps(dict(items), sort_keys=True)


def metadata_json(metadata: Mapping[str, MetadataValue]) -> str:
    """``metadata`` as the ``metadata_json`` column spells it:
    ``json.dumps(metadata, sort_keys=True)``, byte for byte.

    The one place that spelling lives (R23.1): :meth:`TelemetryRecord.parquet_row`
    and the chunk writer's column build both call it, so a row stored singly
    and a row stored in a chunk cannot differ. The dumps was three quarters of
    a CAN row's ``parquet_row`` (2.15 of 2.84 microseconds, laptop, 2026-11-12),
    and nearly every CAN row repeats a previous row's metadata, so the result is
    memoized, keyed by the sorted items, for the metadata that is safe to key
    (see :data:`_MEMOIZABLE_METADATA_TYPES`).
    """
    if not metadata:
        return "{}"
    for key, value in metadata.items():
        if type(key) is not str or type(value) not in _MEMOIZABLE_METADATA_TYPES:
            return json.dumps(metadata, sort_keys=True)
    return _memoized_metadata_json(tuple(sorted(metadata.items())))


def split_value(value: TelemetryValue) -> tuple[float | None, str | None, bool | None]:
    """A record's value as the ``(value_number, value_text, value_bool)`` columns:
    exactly one is set for a value, none for ``None``. ``bool`` is tested first
    (it is an ``int``)."""
    if isinstance(value, bool):
        return None, None, value
    if isinstance(value, (int, float)):
        return float(value), None, None
    if value is not None:
        return None, str(value), None
    return None, None, None


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


@dataclass(frozen=True, slots=True)
class TelemetryRecord:
    """One normalized telemetry sample from any collection source.

    ``slots=True``: a record is built per frame and per reading (tens of
    thousands a second on a busy bus) and never takes another attribute, so
    it carries no ``__dict__`` (R23.6).
    """

    timestamp: datetime
    source: TelemetrySource
    channel: str
    value: TelemetryValue = None
    unit: str = ""
    payload: bytes | None = None
    metadata: dict[str, MetadataValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ts = self.timestamp
        tzinfo = ts.tzinfo
        if tzinfo is UTC:
            return  # already normalised: nothing to set
        if tzinfo is None:
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
        frame_kind: CanFrameKind | None = None,
    ) -> TelemetryRecord:
        """``frame_kind`` marks a controller error frame or a remote request
        (:data:`CAN_FRAME_KIND_KEY` in the metadata): they share the id
        namespace (SocketCAN's ``CAN_ERR_CRTL`` arrives as id ``0x4``) but are
        events, never a broadcast value, so change-only storage keeps every
        one (P02.10). Absent for an ordinary data frame."""
        metadata: dict[str, MetadataValue] = {"can_id": can_id, "dlc": len(payload)}
        if frame_kind is not None:
            metadata[CAN_FRAME_KIND_KEY] = frame_kind
        return cls.from_epoch(
            timestamp,
            source="can",
            channel=f"0x{can_id:X}" if channel is None else channel,
            payload=payload,
            metadata=metadata,
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
        """Return a stable, columnar row representation for Parquet storage.

        A one-row view for callers that hold a single record. The chunk writer
        builds whole columns instead (``storage.parquet_writer.records_to_table``)
        from the same :func:`split_value` and :func:`metadata_json`, so the two
        agree by construction (and by a parity test).
        """
        value_number, value_text, value_bool = split_value(self.value)
        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "channel": self.channel,
            "value_number": value_number,
            "value_text": value_text,
            "value_bool": value_bool,
            "unit": self.unit,
            "payload": self.payload,
            "metadata_json": metadata_json(self.metadata),
        }
