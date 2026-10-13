"""``read_latest_per_channel`` returns each channel's literal newest row.

It used ``groupby(...).last()``, which takes the last non-null value of each
column separately: a newest row with no number (``NO DATA``, a NaN) came back
with its own fresh timestamp and an older row's ``value_number``. Found
verifying H03.2 (2026-09-26): a stale 12.6 V read as a fresh one, which the
non-volatile write gate's voltage check would have accepted.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

from src.control.nonvolatile_write import (
    SUPPLY_VOLTAGE_SOURCES,
    SupplyVoltageSources,
    supply_voltage_from_latest_rows,
)
from src.dashboard.readers import read_latest_per_channel
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

T0 = datetime(2026, 9, 26, 11, 59, 0, tzinfo=UTC).timestamp()


def _session(tmp_path: Path, records: list[TelemetryRecord]) -> Path:
    writer = ParquetTelemetryWriter(
        tmp_path / "sessions", session_started_at=datetime(2026, 9, 26, 11, 58, tzinfo=UTC)
    )
    writer.write_many(records)
    writer.close()
    return writer.paths.root


def _voltage_rows(tmp_path: Path, newest_value: object) -> list[dict[str, object]]:
    session = _session(
        tmp_path,
        [
            TelemetryRecord.obd(T0, "CONTROL_MODULE_VOLTAGE", 12.6, "V"),
            TelemetryRecord.obd(T0 + 60.0, "CONTROL_MODULE_VOLTAGE", newest_value, "V"),  # type: ignore[arg-type]
            TelemetryRecord.obd(T0 + 60.0, "RPM", 0.0, "rpm"),
        ],
    )
    return read_latest_per_channel(session.parent, session_dir=session)


def test_a_nan_newest_row_is_not_dressed_in_an_older_value(tmp_path: Path) -> None:
    rows = _voltage_rows(tmp_path, float("nan"))
    (voltage,) = [r for r in rows if r["channel"] == "CONTROL_MODULE_VOLTAGE"]
    assert voltage["timestamp"] == "2026-09-26T12:00:00+00:00"
    assert voltage["value"] is None or (
        isinstance(voltage["value"], float) and math.isnan(voltage["value"])
    )
    # The repro: the voltage gate reads it as unknown, not as a fresh 12.6 V.
    reading = supply_voltage_from_latest_rows(
        rows,
        now=datetime(2026, 9, 26, 12, 0, 1, tzinfo=UTC),
        max_age_seconds=5.0,
        sources=SupplyVoltageSources(SUPPLY_VOLTAGE_SOURCES),
    )
    assert reading.volts is None
    assert reading.reason is not None and "obd:CONTROL_MODULE_VOLTAGE" in reading.reason


def test_a_no_data_text_newest_row_reads_as_its_text(tmp_path: Path) -> None:
    rows = _voltage_rows(tmp_path, "NO DATA")
    (voltage,) = [r for r in rows if r["channel"] == "CONTROL_MODULE_VOLTAGE"]
    assert voltage["value"] == "NO DATA"
    assert voltage["timestamp"] == "2026-09-26T12:00:00+00:00"


def test_one_row_per_channel_sorted_by_source_and_channel(tmp_path: Path) -> None:
    rows = _voltage_rows(tmp_path, 12.4)
    assert [(r["source"], r["channel"]) for r in rows] == [
        ("obd", "CONTROL_MODULE_VOLTAGE"),
        ("obd", "RPM"),
    ]
    assert rows[0]["value"] == 12.4
