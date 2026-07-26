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
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",), arm_threshold=0.0)
    assert gate.observe(_record("COOLANT_TEMP", 90.0)) is False
    assert gate.observe(_record("RPM", 0.0)) is False  # at threshold, not over
    assert gate.dropped_count == 2
    assert gate.observe(_record("RPM", 700.0)) is True
    assert gate.armed is True


def test_gate_stays_armed_for_subsequent_records_once_tripped() -> None:
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",))
    gate.observe(_record("RPM", 700.0))
    assert gate.observe(_record("RPM", 0.0)) is True  # armed now covers everything
    assert gate.observe(_record("COOLANT_TEMP", 20.0)) is True


def test_gate_checks_any_configured_channel() -> None:
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM", "SPEED"))
    assert gate.observe(_record("SPEED", 12.0)) is True


def test_gate_ignores_non_numeric_and_bool_values() -> None:
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",))
    assert gate.observe(_record("RPM", "not a number")) is False
    assert gate.observe(_record("RPM", True)) is False  # bool is an int subclass — excluded
    assert gate.armed is False


def test_gate_reset_for_new_session_disarms() -> None:
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",))
    gate.observe(_record("RPM", 700.0))
    assert gate.armed is True
    gate.reset_for_new_session()
    assert gate.armed is False


def test_gate_negative_threshold_arms_on_zero() -> None:
    """``arm_threshold < 0`` is a valid way to arm on any reading (incl. 0),
    e.g. for a channel that's only ever emitted while the ECU is awake."""
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",), arm_threshold=-1.0)
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
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",))
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
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",))
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
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",))
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
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",))
    assert gate.observe(_record("RPM", value)) is False


# ---------------------------------------------------------------------------
# Sustained-silence disarm (v2) — key out / adapter unplugged, Pi still up
# ---------------------------------------------------------------------------


def test_gate_disarms_after_sustained_silence() -> None:
    """The 23 h parked-GPS failure: drive ends, key out, Pi stays powered on
    the UPS, GPS keeps chattering. Rotation-only disarm never fires (it needs
    the *next* key-on), so v1 recorded all of it. With the timeout, the gate
    drops the chatter once the arm channels have been silent long enough."""
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",), disarm_after_seconds=300.0)
    t0 = 1_778_000_000.0
    assert gate.observe(_record("RPM", 800.0, t0)) is True

    # Within the window: still armed, non-arm records pass (this is what
    # keeps a stall from dropping the fault data around it).
    assert gate.observe(_record("COOLANT_TEMP", 90.0, t0 + 299.0)) is True
    assert gate.armed is True

    # Past the window: disarmed, chatter dropped from here on.
    assert gate.observe(_record("COOLANT_TEMP", 88.0, t0 + 301.0)) is False
    assert gate.armed is False
    assert gate.observe(_record("COOLANT_TEMP", 87.0, t0 + 3600.0)) is False


def test_gate_rearms_after_silence_disarm() -> None:
    """Next morning's key-on re-arms exactly like the first: the first
    RPM record over threshold passes and everything after it records."""
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",), disarm_after_seconds=300.0)
    t0 = 1_778_000_000.0
    gate.observe(_record("RPM", 800.0, t0))
    assert gate.observe(_record("COOLANT_TEMP", 60.0, t0 + 400.0)) is False  # disarmed

    assert gate.observe(_record("RPM", 250.0, t0 + 40_000.0)) is True  # cranking re-arms
    assert gate.armed is True
    assert gate.observe(_record("COOLANT_TEMP", 20.0, t0 + 40_001.0)) is True


def test_gate_active_evidence_refreshes_the_silence_window() -> None:
    """An idling engine keeps emitting RPM > 0, so a long light (or an
    hour-long idle) never trips the timeout — evidence never goes stale."""
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",), disarm_after_seconds=300.0)
    t0 = 1_778_000_000.0
    gate.observe(_record("RPM", 800.0, t0))
    for i in range(1, 20):  # 19 minutes of idle, refreshed every 60 s
        assert gate.observe(_record("RPM", 700.0, t0 + i * 60.0)) is True
    assert gate.observe(_record("COOLANT_TEMP", 90.0, t0 + 19 * 60.0 + 30.0)) is True
    assert gate.armed is True


def test_gate_without_timeout_keeps_v1_rotation_only_disarm() -> None:
    """``disarm_after_seconds=None`` (the dataclass default) is byte-for-byte
    the v1 behavior: armed stays armed however long the silence."""
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",))
    t0 = 1_778_000_000.0
    gate.observe(_record("RPM", 800.0, t0))
    assert gate.observe(_record("COOLANT_TEMP", 90.0, t0 + 1_000_000.0)) is True
    assert gate.armed is True


def test_gate_out_of_order_records_do_not_roll_evidence_backwards() -> None:
    """Sources drain slightly out of order; a late-arriving old GPS row must
    not shrink the evidence window and cause a premature disarm."""
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",), disarm_after_seconds=300.0)
    t0 = 1_778_000_000.0
    gate.observe(_record("RPM", 800.0, t0 + 100.0))
    gate.observe(_record("RPM", 750.0, t0 + 50.0))  # late arrival, older ts
    # Age is measured from t0+100, not t0+50.
    assert gate.observe(_record("COOLANT_TEMP", 90.0, t0 + 380.0)) is True
    assert gate.armed is True


def test_gate_silence_disarm_counts_drops() -> None:
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",), disarm_after_seconds=300.0)
    t0 = 1_778_000_000.0
    gate.observe(_record("RPM", 800.0, t0))
    gate.observe(_record("COOLANT_TEMP", 90.0, t0 + 400.0))  # disarming drop
    gate.observe(_record("COOLANT_TEMP", 89.0, t0 + 500.0))  # ordinary disarmed drop
    assert gate.dropped_count == 2


# ---------------------------------------------------------------------------
# Source-based evidence (v3) — bus connectivity is the deciding factor
# ---------------------------------------------------------------------------


def test_gate_arms_on_raw_can_frame_by_default() -> None:
    """A raw CAN frame (no numeric value at all) proves the bus is talking —
    that alone should open recording, KOEO included."""
    gate = RecordArmingGate()
    frame = TelemetryRecord.can(1_778_000_000.0, 0x180, b"\x01\x02")
    assert gate.observe(frame) is True
    assert gate.armed is True


def test_gate_arms_on_any_obd_record_by_default() -> None:
    """KOEO: ECU responds with RPM=0. The bus is connected, so record —
    channel thresholds only matter when arm_sources is narrowed."""
    gate = RecordArmingGate()
    assert gate.observe(_record("RPM", 0.0)) is True
    assert gate.armed is True


def test_gate_never_arms_on_pi_side_chatter() -> None:
    """The 23 h failure, exactly: bench or key-out, GPS and derived keep
    chattering because the Pi has power. None of it is bus evidence."""
    gate = RecordArmingGate(disarm_after_seconds=300.0)
    t0 = 1_778_000_000.0
    assert gate.observe(TelemetryRecord.gps(t0, "speed", 0.0, "km/h")) is False
    derived = TelemetryRecord.derived(datetime.fromtimestamp(t0 + 1.0, UTC), "GRADE", 0.0, "%")
    assert gate.observe(derived) is False
    assert gate.armed is False
    assert gate.dropped_count == 2


def test_gate_disarms_when_bus_goes_silent_but_gps_keeps_flowing() -> None:
    """Post-drive key-out with the Pi on the UPS: CAN/OBD stop, GPS keeps
    emitting fixes. The GPS rows themselves must not refresh the evidence —
    the gate closes disarm_after_seconds after the last bus record."""
    gate = RecordArmingGate(disarm_after_seconds=300.0)
    t0 = 1_778_000_000.0
    gate.observe(TelemetryRecord.can(t0, 0x180, b"\x01"))
    assert gate.observe(TelemetryRecord.gps(t0 + 299.0, "speed", 0.0, "km/h")) is True
    assert gate.observe(TelemetryRecord.gps(t0 + 301.0, "speed", 0.0, "km/h")) is False
    assert gate.armed is False


def test_gate_koeo_keeps_bus_evidence_fresh() -> None:
    """A long KOEO diagnostic session (RPM=0, bus chattering) stays recorded:
    connectivity, not engine speed, is the criterion."""
    gate = RecordArmingGate(disarm_after_seconds=300.0)
    t0 = 1_778_000_000.0
    for i in range(20):  # 20 min of KOEO, a bus record every 60 s
        assert gate.observe(_record("RPM", 0.0, t0 + i * 60.0)) is True
    assert gate.armed is True


def test_gate_empty_arm_sources_falls_back_to_channel_thresholds() -> None:
    """arm_sources=[] restores value-gated arming for vehicles whose bus
    stays awake while parked (keyless-entry polling and the like)."""
    gate = RecordArmingGate(arm_sources=(), arm_channels=("RPM",))
    assert gate.observe(TelemetryRecord.can(1_778_000_000.0, 0x180, b"\x01")) is False
    assert gate.observe(_record("RPM", 0.0)) is False
    assert gate.observe(_record("RPM", 700.0)) is True
