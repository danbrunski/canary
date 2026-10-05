"""A parked truck is ``parked``, not ``no_data`` (2026-10-04).

The Xterra's bus sleeps at key-off, so hours parked had both buses silent
and the daemon reported ``no_data`` ("Pi probably disconnected") while it
answered. The HAT's 12 V sense is the evidence that tells the two apart; a
restart no longer forgets when the bus last spoke; and the startup timeline
no longer counts records the arming gate withheld as stored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.collection.record_arming import RecordArmingGate
from src.config import CanaryConfig
from src.features.live import LiveFeatureSource
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.storage.sessions import last_heard
from src.telemetry import TelemetryRecord
from src.vehicle_state import derive_from_inputs
from tests.configs import example_config

EXAMPLE_YAML = Path("canary.example.yaml")
T0 = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)


@pytest.fixture
def config() -> CanaryConfig:
    return example_config(EXAMPLE_YAML)


def _supply(at: datetime, volts: float) -> TelemetryRecord:
    return TelemetryRecord.from_epoch(
        at.timestamp(), source="sensor", channel="SUPPLY_VOLTAGE", value=volts, unit="V"
    )


def test_the_live_source_reads_the_supply_and_classifies_parked(config: CanaryConfig) -> None:
    source = LiveFeatureSource(config=config)
    source.seed_bus_last_heard(T0)
    now = T0 + timedelta(hours=3)
    source.observe(_supply(now - timedelta(seconds=0.1), 12.38))
    inputs = source.vehicle_state_inputs(now)
    assert inputs["supply_voltage"] == pytest.approx(12.38)
    assert inputs["supply_freshness_seconds"] == pytest.approx(0.1)
    # Restored from storage: "silent for 3 h", not "never heard".
    assert inputs["can_freshness_seconds"] == pytest.approx(3 * 3600)
    state = derive_from_inputs(inputs, None, now, passive=False)
    assert state.state == "parked"


def test_a_frame_seen_since_start_outranks_the_seed(config: CanaryConfig) -> None:
    source = LiveFeatureSource(config=config)
    source._last_broadcast_frame_at = T0 + timedelta(hours=2)  # a frame this run
    source.seed_bus_last_heard(T0)
    assert source.vehicle_state_inputs(T0 + timedelta(hours=3))[
        "can_freshness_seconds"
    ] == pytest.approx(3600)


def test_last_heard_finds_the_bus_behind_later_sensor_rows(tmp_path: Path) -> None:
    writer = ParquetTelemetryWriter(tmp_path, max_rows=2)
    writer.write(
        TelemetryRecord(timestamp=T0, source="can", channel="0x1F9", payload=bytes(8), value=None)
    )
    # The bus sleeps; the Pi goes on writing sensor rows for many chunks
    # (2026-10-04T233938: minutes of 100 Hz fuel pressure after the last frame).
    for k in range(120):
        writer.write(_supply(T0 + timedelta(seconds=10 + k), 12.4))
    writer.close()
    session = writer.paths.root
    assert len(list(session.glob("*.parquet"))) > 50
    assert last_heard(session, ("can",)) == T0
    assert last_heard(session, ("obd",)) is None


def test_records_the_arming_gate_withholds_are_not_reported_as_recorded(
    tmp_path: Path,
) -> None:
    gate = RecordArmingGate(arm_sources=("can", "obd"))
    writer = ParquetTelemetryWriter(tmp_path, arming_gate=gate)
    parked = [_supply(T0, 12.4)]
    writer.write_many(parked)
    assert writer.last_accepted == []
    driving = [TelemetryRecord.obd(T0.timestamp() + 1, "RPM", 750.0, "rpm"), _supply(T0, 12.4)]
    writer.write_many(driving)
    assert [r.source for r in writer.last_accepted] == ["obd", "sensor"]
