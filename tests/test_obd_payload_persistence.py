"""Raw OBD response bytes must survive into Parquet.

The LONG_FUEL_TRIM flat-0.0 investigation dead-ended on "raw PID bytes
aren't stored" — a suspect decoder could not be re-run over history. These
tests pin the fix: the wire payload rides the reading into the shared
``payload`` Parquet column.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.collection.can_obd_collector import _response_to_reading
from src.collection.isotp import ObdResponse
from src.collection.obd_collector import ObdReading, reading_to_record
from src.collection.obd_scheduler import _PollRequest
from src.storage.parquet_writer import ParquetTelemetryWriter


def test_pid_reading_carries_response_payload() -> None:
    request = _PollRequest(
        kind="pid", service=0x01, identifier=0x0C, request_id=0x7E0, channel="RPM"
    )
    # RPM = (0x0C * 256 + 0x80) / 4; the raw bytes are what must survive.
    response = ObdResponse(service=0x01, pid=0x0C, payload=b"\x0c\x80", responder_id=0x7E8)
    (reading,) = _response_to_reading(request, response)
    assert reading.value == (0x0C * 256 + 0x80) / 4.0
    assert reading.payload == b"\x0c\x80"


def test_reading_to_record_and_parquet_roundtrip(tmp_path: Path) -> None:
    reading = ObdReading(
        timestamp=1_753_800_000.0,
        pid="LONG_FUEL_TRIM_1",
        value=0.0,
        unit="%",
        payload=b"\x80",  # the 0x80 sentinel the LTFT investigation needed
    )
    record = reading_to_record(reading)
    assert record.payload == b"\x80"

    writer = ParquetTelemetryWriter(tmp_path, max_rows=1)
    writer.write(record)
    writer.close()
    frames = [pd.read_parquet(p) for p in tmp_path.rglob("*.parquet")]
    stored = pd.concat(frames, ignore_index=True)
    row = stored[stored["channel"] == "LONG_FUEL_TRIM_1"].iloc[0]
    assert bytes(row["payload"]) == b"\x80"


def test_payload_defaults_to_none_for_legacy_paths() -> None:
    reading = ObdReading(timestamp=1.0, pid="RPM", value=800.0, unit="rpm")
    assert reading.payload is None
    assert reading_to_record(reading).payload is None
    # JSONL debug sidecar stays byte-free and serializable.
    assert "payload" not in reading.to_json_line()
