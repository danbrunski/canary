"""Tests for normalized telemetry records."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from typing import get_args

from src.telemetry import (
    CAN_SOURCE,
    DBC_SOURCE,
    DECODED_SOURCE,
    DERIVED_SOURCE,
    GPS_SOURCE,
    IMU_SOURCE,
    OBD_SOURCE,
    SENSOR_SOURCE,
    UPREV_SOURCE,
    TelemetryRecord,
    TelemetrySource,
    channel_key,
    channel_of,
    source_of,
)


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


def test_channel_key_round_trips_through_source_and_channel() -> None:
    key = channel_key(OBD_SOURCE, "RPM")
    assert key == "obd:RPM"
    assert source_of(key) == OBD_SOURCE
    assert channel_of(key) == "RPM"


def test_source_and_channel_helpers_pass_through_unprefixed_names() -> None:
    assert source_of("derived_thing") is None
    assert channel_of("derived_thing") == "derived_thing"


def test_namespace_helpers_agree_with_the_decode_path_wrappers() -> None:
    from src.decode.dbc import dbc_channel
    from src.preprocessing.decode import decoded_channel

    assert decoded_channel("ECM_RPM") == channel_key(DECODED_SOURCE, "ECM_RPM")
    assert dbc_channel("EngineSpeed") == channel_key(DBC_SOURCE, "EngineSpeed")


def test_telemetry_source_literal_stays_in_sync_with_constants() -> None:
    # DBC_SOURCE is deliberately excluded — DBC values never ride a record.
    assert set(get_args(TelemetrySource)) == {
        OBD_SOURCE,
        CAN_SOURCE,
        DECODED_SOURCE,
        GPS_SOURCE,
        DERIVED_SOURCE,
        IMU_SOURCE,
        UPREV_SOURCE,
        SENSOR_SOURCE,
    }
