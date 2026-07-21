"""Tests for the operator-intent recording gate (Task 8.6)."""

from __future__ import annotations

from datetime import UTC, datetime

import pyarrow.parquet as pq
import pytest

from src.collection.record_arming import RecordArmingGate
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord


def _record(channel: str, value: object, ts: float = 1_778_000_000.0) -> TelemetryRecord:
    return TelemetryRecord.obd(ts, channel, value, "")


# ---------------------------------------------------------------------------
# RecordArmingGate (pure)
# ---------------------------------------------------------------------------


def test_gate_starts_disarmed() -> None:
    gate = RecordArmingGate()
    assert gate.armed is False


def test_gate_drops_until_arm_channel_clears_threshold() -> None:
    gate = RecordArmingGate(arm_channels=("RPM",), arm_threshold=0.0)
    assert gate.observe(_record("COOLANT_TEMP", 90.0)) is False
    assert gate.observe(_record("RPM", 0.0)) is False  # at threshold, not over
    assert gate.dropped_count == 2
    assert gate.observe(_record("RPM", 700.0)) is True
    assert gate.armed is True


def test_gate_stays_armed_for_subsequent_records_once_tripped() -> None:
    gate = RecordArmingGate(arm_channels=("RPM",))
    gate.observe(_record("RPM", 700.0))
    assert gate.observe(_record("RPM", 0.0)) is True  # armed now covers everything
    assert gate.observe(_record("COOLANT_TEMP", 20.0)) is True


def test_gate_checks_any_configured_channel() -> None:
    gate = RecordArmingGate(arm_channels=("RPM", "SPEED"))
    assert gate.observe(_record("SPEED", 12.0)) is True


def test_gate_ignores_non_numeric_and_bool_values() -> None:
    gate = RecordArmingGate(arm_channels=("RPM",))
    assert gate.observe(_record("RPM", "not a number")) is False
    assert gate.observe(_record("RPM", True)) is False  # bool is an int subclass — excluded
    assert gate.armed is False


def test_gate_reset_for_new_session_disarms() -> None:
    gate = RecordArmingGate(arm_channels=("RPM",))
    gate.observe(_record("RPM", 700.0))
    assert gate.armed is True
    gate.reset_for_new_session()
    assert gate.armed is False


def test_gate_negative_threshold_arms_on_zero() -> None:
    """``arm_threshold < 0`` is a valid way to arm on any reading (incl. 0),
    e.g. for a channel that's only ever emitted while the ECU is awake."""
    gate = RecordArmingGate(arm_channels=("RPM",), arm_threshold=-1.0)
    assert gate.observe(_record("RPM", 0.0)) is True


# ---------------------------------------------------------------------------
# ParquetTelemetryWriter wiring — additive, off by default
# ---------------------------------------------------------------------------


def test_writer_without_gate_records_everything(tmp_path) -> None:
    """No ``arming_gate`` (the default) is byte-identical to pre-8.6 behavior."""
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
        max_rows=1,
    )
    path = writer.write(_record("SPEED", 0.0))
    assert path is not None
    assert pq.read_table(path).to_pylist()[0]["channel"] == "SPEED"


def test_writer_with_gate_drops_records_until_armed(tmp_path) -> None:
    gate = RecordArmingGate(arm_channels=("RPM",))
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
        max_rows=1,
        arming_gate=gate,
    )
    assert writer.write(_record("SPEED", 0.0)) is None  # dropped pre-arm, no flush
    assert writer.write(_record("RPM", 800.0)) is not None  # arms + is itself recorded
    assert gate.dropped_count == 1


def test_writer_with_gate_records_normally_once_armed(tmp_path) -> None:
    gate = RecordArmingGate(arm_channels=("RPM",))
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
        max_rows=2,
        arming_gate=gate,
    )
    writer.write(_record("RPM", 800.0))
    path = writer.write(_record("COOLANT_TEMP", 91.0))
    assert path is not None
    channels = [row["channel"] for row in pq.read_table(path).to_pylist()]
    assert channels == ["RPM", "COOLANT_TEMP"]


def test_writer_rearms_gate_on_session_rotation(tmp_path) -> None:
    """A new key cycle (RUN_TIME reset -> rotate_session) re-disarms the gate
    — a later idle period between drives is gated again, not left armed."""
    first_started = datetime(2026, 5, 11, 12, 0, tzinfo=UTC)
    second_started = datetime(2026, 5, 11, 12, 10, tzinfo=UTC)
    gate = RecordArmingGate(arm_channels=("RPM",))
    writer = ParquetTelemetryWriter(
        tmp_path,
        session_started_at=first_started,
        max_rows=10,
        arming_gate=gate,
    )

    # Establish a RUN_TIME baseline, then arm on RPM (mirrors a real drive:
    # RUN_TIME climbs alongside RPM within one key cycle).
    writer.write(_record("RUN_TIME", 120.0, first_started.timestamp()))
    writer.write(_record("RPM", 800.0, first_started.timestamp()))
    assert gate.armed is True

    # RUN_TIME resetting below baseline signals a new key cycle -> rotation
    # -> the gate is disarmed again before this same record is observed (it
    # isn't itself an arm channel, so it's dropped, same as any other
    # pre-arm record in the new session).
    assert writer.write(_record("RUN_TIME", 1.0, second_started.timestamp())) is None
    assert gate.armed is False

    assert writer.write(_record("COOLANT_TEMP", 20.0, second_started.timestamp())) is None
    writer.write(_record("RPM", 900.0, second_started.timestamp()))
    assert gate.armed is True
    writer.close()

    files = sorted(tmp_path.glob("*/telemetry-*.parquet"))
    assert len(files) == 2
    second_rows = pq.read_table(files[1]).to_pylist()
    # RUN_TIME and COOLANT_TEMP were dropped pre-arm in the new session;
    # only the RPM record (the one that armed it) made it through.
    assert [row["channel"] for row in second_rows] == ["RPM"]


@pytest.mark.parametrize("value", [0.0, -5.0])
def test_gate_default_threshold_requires_strictly_positive_value(value: float) -> None:
    gate = RecordArmingGate(arm_channels=("RPM",))
    assert gate.observe(_record("RPM", value)) is False
