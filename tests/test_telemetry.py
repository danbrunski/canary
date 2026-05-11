"""Tests for normalized telemetry records."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

from src.telemetry import TelemetryRecord


def test_record_normalizes_timestamp_to_utc() -> None:
    record = TelemetryRecord(
        timestamp=datetime(2026, 5, 11, 12, 0, tzinfo=timezone(timedelta(hours=-6))),
        source="obd",
        channel="RPM",
        value=700,
        unit="rpm",
    )
    assert record.timestamp == datetime(2026, 5, 11, 18, 0, tzinfo=UTC)


def test_obd_record_parquet_row_splits_numeric_value() -> None:
    record = TelemetryRecord.obd(1_778_000_000.0, "RPM", 720.0, "rpm")
    row = record.parquet_row()
    assert row["source"] == "obd"
    assert row["channel"] == "RPM"
    assert row["value_number"] == 720.0
    assert row["value_text"] is None
    assert row["unit"] == "rpm"


def test_can_record_preserves_payload_and_metadata() -> None:
    record = TelemetryRecord.can(1_778_000_000.0, 0x123, b"\x01\x02")
    row = record.parquet_row()
    assert row["source"] == "can"
    assert row["channel"] == "0x123"
    assert row["payload"] == b"\x01\x02"
    assert json.loads(str(row["metadata_json"])) == {"can_id": 0x123, "dlc": 2}
