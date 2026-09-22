"""Add-on sensor CAN: the AEM gauge layout, the decoder's refusals, the collector."""

from __future__ import annotations

import asyncio
from pathlib import Path

import can
import pytest
from pydantic import ValidationError

from src.analysis.channels import parse_spec
from src.collection.collectors import _sensor_collectors
from src.collection.reconnect import ReconnectPolicy
from src.collection.sensor_can_collector import collect_sensor_can, frame_to_records
from src.config import CanaryConfig, SensorCanConfig
from src.decode.sensor_can import (
    SensorCanCatalog,
    SensorCanSignal,
    decode_frame,
    load_sensor_can_catalog,
)
from src.telemetry import TelemetryRecord

REPO = Path(__file__).resolve().parents[1]
CATALOG = load_sensor_can_catalog(REPO / "data/sensor_can_signals.yaml")
AEM_ID = 0x000A0301


def _aem_frame(kpa: int) -> bytes:
    return kpa.to_bytes(2, "big") + bytes(6)


def test_aem_gauge_known_answer() -> None:
    """51 psig = 351.6 kPa; the gauge sends 0.01 bar (= 1 kPa) per bit, big-endian."""
    readings = decode_frame(CATALOG, AEM_ID, True, _aem_frame(352))
    assert [(r.name, r.value, r.unit) for r in readings] == [("FUEL_PRESSURE", 352.0, "kPa")]
    assert readings[0].value / 6.894757 == pytest.approx(51.05, abs=0.01)


def test_wrong_endianness_would_be_dropped_not_recorded() -> None:
    """Little-endian 0x01 0x60 is 24,577 kPa: outside the sensor's range, so nothing."""
    little = SensorCanCatalog(
        signals=[CATALOG.signals[0].model_copy(update={"endianness": "little"})]
    )
    assert decode_frame(little, AEM_ID, True, _aem_frame(352)) == []


@pytest.mark.parametrize(
    ("arbitration_id", "extended", "data"),
    [
        (0x0A0302, True, _aem_frame(352)),  # another frame
        (0x301, False, _aem_frame(352)),  # an 11-bit ID is a different frame
        (AEM_ID, True, b"\x01"),  # too short for the signal's two bytes
    ],
)
def test_frames_that_are_not_the_signal_yield_nothing(
    arbitration_id: int, extended: bool, data: bytes
) -> None:
    assert decode_frame(CATALOG, arbitration_id, extended, data) == []


def test_layouts_that_cannot_be_right_refuse_to_load() -> None:
    base = CATALOG.signals[0].model_dump()
    with pytest.raises(ValidationError, match="exceed 8"):
        SensorCanSignal.model_validate({**base, "byte_start": 7, "byte_length": 2})
    with pytest.raises(ValidationError, match="11-bit"):
        SensorCanSignal.model_validate({**base, "extended": False})
    with pytest.raises(ValidationError, match="Field required"):
        SensorCanSignal.model_validate({k: v for k, v in base.items() if k != "endianness"})


def test_collector_records_sensor_channel_from_a_virtual_bus() -> None:
    async def run() -> list[TelemetryRecord]:
        rx = can.Bus(interface="virtual", channel="sensor-test")
        tx = can.Bus(interface="virtual", channel="sensor-test")
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(collect_sensor_can(rx, CATALOG, queue, stop))
        tx.send(can.Message(arbitration_id=AEM_ID, is_extended_id=True, data=_aem_frame(340)))
        record = await asyncio.wait_for(queue.get(), timeout=5)
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        tx.shutdown()
        rx.shutdown()
        return [record]

    (record,) = asyncio.run(run())
    assert (record.source, record.channel, record.value, record.unit) == (
        "sensor",
        "FUEL_PRESSURE",
        340.0,
        "kPa",
    )
    assert record.payload == _aem_frame(340)
    assert record.metadata["arbitration_id"] == "0x000a0301"


def test_frame_time_is_the_receive_stamp() -> None:
    msg = can.Message(
        timestamp=1_790_000_000.25, arbitration_id=AEM_ID, is_extended_id=True, data=_aem_frame(1)
    )
    (record,) = frame_to_records(CATALOG, msg)
    assert record.timestamp.timestamp() == pytest.approx(1_790_000_000.25)


def test_sensor_bus_is_off_unless_enabled() -> None:
    config = CanaryConfig.from_yaml(REPO / "canary.example.yaml")
    assert config.hardware.sensor_can.enabled is False
    assert _sensor_collectors(config, ReconnectPolicy()) == []
    enabled = config.model_copy(
        update={
            "hardware": config.hardware.model_copy(
                update={"sensor_can": SensorCanConfig(enabled=True)}
            )
        }
    )
    assert len(_sensor_collectors(enabled, ReconnectPolicy())) == 1


def test_sensor_namespace_is_readable_by_analysis() -> None:
    spec = parse_spec("sensor:FUEL_PRESSURE")
    assert (spec.source, spec.name) == ("sensor", "FUEL_PRESSURE")
