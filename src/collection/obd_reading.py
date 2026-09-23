"""The OBD reading shape and its conversion to a telemetry record.

Shared by both OBD transports: the Canable collector
(:mod:`src.collection.can_obd_collector`) and the ELM327 one
(:mod:`src.collection.obd_collector`). Split out of the latter because it
imports python-OBD, and through it pint: about 400 ms warm (``obd``'s units
table alone 535 ms self-time), which a Canable-only daemon paid on the main
thread before any collector ran (S01.3). Nothing here imports either.
:mod:`src.collection.obd_collector` re-exports every name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.telemetry import MetadataValue, TelemetryRecord

#: Version of the timing metadata ``reading_to_record`` persists (C02.6).
TS_SCHEMA_VERSION = 2
#: Clock source behind ``ObdReading.acquired_at``.
AcquiredClock = Literal["kernel", "userspace"]


@dataclass(frozen=True)
class ObdReading:
    """One Mode 01 PID reading at a wall-clock timestamp.

    `timestamp` uses `time.time()` so it aligns directly with the
    candump-format `frames.log` from the CAN recorder.
    `value` is decoded — float for numeric PIDs (after pint stripping),
    str for textual ones (VIN), list for DTCs.
    """

    timestamp: float
    pid: str
    value: Any
    unit: str
    #: Raw response data bytes the decoder consumed (None on paths that
    #: never see the wire bytes, e.g. python-obd's pint-quantity readings).
    #: Persisted to Parquet so a wrong decoder can be re-run over history.
    payload: bytes | None = None
    #: Wire-arrival instant of the (first) response frame — the measurement
    #: clock. The Canable collector always sets it (``ts_schema`` 2, C02.6);
    #: ``timestamp`` is then the *presentation* stamp: the capture round's
    #: scheduled start when the active profile asks for ``round_timestamps``,
    #: otherwise this same instant. ``None`` only on paths that never see the
    #: wire (the legacy ELM327 collector), whose rows stay ``stored``-basis.
    acquired_at: float | None = None
    #: Where ``acquired_at`` came from: ``"kernel"`` when the bus driver
    #: stamped the frame (SocketCAN ``SO_TIMESTAMP``; python-can
    #: ``Message.timestamp``) and it agreed with the wall clock, else
    #: ``"userspace"`` — a ``time.time()`` read after receipt.
    acquired_clock: AcquiredClock | None = None
    #: Multi-frame ISO-TP responses use the FIRST frame's arrival as
    #: ``acquired_at``; this is completion minus first frame when > 0, so the
    #: reassembly span is kept rather than folded into the stamp (C02.7).
    response_interval_s: float | None = None
    #: Capture generation and round this reading was acquired in (C02.2/C02.8).
    #: Round identity says which window scheduled the request; it is NOT
    #: evidence that members were sampled together — compare ``acquired_at``.
    generation: int | None = None
    round_id: int | None = None
    #: CAN id of the module that answered (``0x7E8`` engine, ``0x7E9`` …).
    #: Functional requests can be answered by several ECUs; a reading keeps
    #: which one it came from (C06.11). ``None`` on paths without the wire.
    responder_id: int | None = None

    def to_json_line(self) -> str:
        # payload intentionally omitted: the JSONL sidecar is a human-facing
        # debug log; the bytes ride the Parquet ``payload`` column instead.
        return json.dumps(
            {"t": self.timestamp, "pid": self.pid, "value": self.value, "unit": self.unit}
        )


class JsonlReadingWriter:
    """Append OBD readings to a JSONL file, flushed per write for crash safety.

    Mirrors how the tag sidecar works — durable per line so a crash mid-
    session loses at most one in-flight reading, and external tools can
    tail the file live during a recording.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fp = path.open("a")

    def write(self, reading: ObdReading) -> None:
        self._fp.write(reading.to_json_line() + "\n")
        self._fp.flush()

    def close(self) -> None:
        self._fp.close()


def reading_to_record(reading: ObdReading) -> TelemetryRecord:
    """Convert an OBD reading into the shared telemetry schema.

    Timing provenance travels in ``metadata`` (Parquet ``metadata_json``)
    under :data:`TS_SCHEMA_VERSION`; see :func:`acquisition_metadata`.
    """
    value = reading.value
    if not isinstance(value, (bool, int, float, str, type(None))):
        value = str(value)
    return TelemetryRecord.obd(
        reading.timestamp,
        reading.pid,
        value,
        reading.unit,
        payload=reading.payload,
        metadata=acquisition_metadata(reading),
    )


def acquisition_metadata(reading: ObdReading) -> dict[str, MetadataValue] | None:
    """The persisted timing fields for one reading, or ``None`` for legacy paths.

    Schema versions, as readers must interpret them (``docs/capture-rounds.md``):

    * absent ``acquired_at`` — legacy: the stored ``timestamp`` is a
      decode-time clock read, or a round stamp if the session manifest says
      ``round_timestamps`` (``timestamp_basis`` ``stored`` / ``round``);
    * ``acquired_at`` without ``ts_schema`` — v1: written only when the row
      was round-stamped;
    * ``ts_schema: 2`` — ``acquired_at`` is always present with its clock
      source, plus ``generation`` / ``round_id`` when a round scheduled it.
    """
    if reading.acquired_at is None:
        return None
    metadata: dict[str, MetadataValue] = {
        "ts_schema": TS_SCHEMA_VERSION,
        "acquired_at": reading.acquired_at,
        "acquired_clock": reading.acquired_clock or "userspace",
    }
    if reading.response_interval_s is not None:
        metadata["response_interval_s"] = reading.response_interval_s
    if reading.generation is not None:
        metadata["generation"] = reading.generation
    if reading.round_id is not None:
        metadata["round_id"] = reading.round_id
    if reading.responder_id is not None:
        metadata["responder_id"] = reading.responder_id
    return metadata


__all__ = [
    "TS_SCHEMA_VERSION",
    "AcquiredClock",
    "JsonlReadingWriter",
    "ObdReading",
    "acquisition_metadata",
    "reading_to_record",
]
