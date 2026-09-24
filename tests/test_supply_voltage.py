"""The CANary-HAT's 12 V sense: ADS1015 driver and the supply-voltage collector.

Known answers from the HAT's bring-up (2026-09-23): the config word the
checkout used (``0xC383``) and ADC code 1143 reading 12.68 V key-off.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.collection import ads1015
from src.collection.ads1015 import Ads1015, Ads1015Error, code_to_volts, config_word
from src.collection.collectors import _supply_voltage_collectors
from src.collection.reconnect import ReconnectPolicy
from src.collection.supply_voltage_collector import (
    collect_supply_voltage_from_config,
    supply_record,
)
from src.config import CanaryConfig, SupplyVoltageConfig
from src.telemetry import TelemetryRecord

REPO = Path(__file__).resolve().parents[1]
HAT = SupplyVoltageConfig(enabled=True, rate_hz=100.0)


class FakeAdc:
    """Config/conversion registers; the conversion finishes after ``busy_reads``."""

    def __init__(self, code: int, *, busy_reads: int = 1) -> None:
        self.code = code
        self.busy_reads = busy_reads
        self.config_writes: list[int] = []
        self.closed = False

    def read(self, register: int, length: int) -> bytes:
        if register == ads1015.REG_CONFIG:
            if self.busy_reads > 0:
                self.busy_reads -= 1
                return (0).to_bytes(2, "big")
            return ads1015.CONFIG_OS_START.to_bytes(2, "big")
        return ((self.code << 4) & 0xFFFF).to_bytes(2, "big")

    def write(self, register: int, value: int) -> None:
        raise AssertionError("the ADS1015 has only 16-bit registers")

    def write_block(self, register: int, data: bytes) -> None:
        assert register == ads1015.REG_CONFIG
        self.config_writes.append(int.from_bytes(data, "big"))

    def close(self) -> None:
        self.closed = True


def test_config_word_matches_the_one_measured_on_the_hat() -> None:
    assert config_word(0, 4.096) == 0xC383


def test_config_word_refuses_inputs_and_ranges_the_chip_lacks() -> None:
    with pytest.raises(ValueError, match="input 4"):
        config_word(4, 4.096)
    with pytest.raises(ValueError, match="full scale 5.0"):
        config_word(0, 5.0)


def test_code_1143_is_12_68_volts_through_the_hat_divider() -> None:
    pin = code_to_volts((1143 << 4).to_bytes(2, "big"), 4.096)
    assert pin == pytest.approx(2.286)
    assert pin * (122 / 22) == pytest.approx(12.68, abs=0.005)


def test_negative_codes_are_signed() -> None:
    assert code_to_volts(((-16 << 4) & 0xFFFF).to_bytes(2, "big"), 4.096) == pytest.approx(-0.032)


def test_read_waits_for_the_conversion_and_stamps_its_midpoint() -> None:
    times = iter([10.0, 10.0004, 10.001])
    adc = Ads1015(FakeAdc(1143, busy_reads=1), clock=lambda: next(times), sleep=lambda _s: None)
    stamp, volts = adc.read_volts(0)
    assert volts == pytest.approx(2.286)
    assert stamp == pytest.approx(10.0005)


def test_a_conversion_that_never_finishes_is_an_error() -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 0.01
        return now[0]

    adc = Ads1015(FakeAdc(0, busy_reads=10**6), clock=clock, sleep=lambda _s: None)
    with pytest.raises(Ads1015Error, match="AIN0"):
        adc.read_volts(0)


def test_record_is_sensor_supply_voltage_in_volts() -> None:
    record = supply_record(HAT, 50.0, 2.286)
    assert (record.source, record.channel, record.unit) == ("sensor", "SUPPLY_VOLTAGE", "V")
    assert record.value == pytest.approx(12.68, abs=0.005)


async def test_collector_records_scaled_volts_until_stopped() -> None:
    bus = FakeAdc(1143, busy_reads=0)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(
        collect_supply_voltage_from_config(HAT, queue, stop, bus_factory=lambda _c: bus)
    )
    first = await asyncio.wait_for(queue.get(), timeout=5)
    second = await asyncio.wait_for(queue.get(), timeout=5)
    stop.set()
    await task
    assert first.value == pytest.approx(12.68, abs=0.005)
    assert second.timestamp > first.timestamp
    assert bus.config_writes[0] == 0xC383
    assert bus.closed


def test_divider_ratio_must_divide() -> None:
    with pytest.raises(ValueError):
        SupplyVoltageConfig(divider_ratio=0.5)


def test_supply_voltage_collector_runs_only_when_enabled() -> None:
    config = CanaryConfig.from_yaml(REPO / "canary.example.yaml")
    assert _supply_voltage_collectors(config, ReconnectPolicy()) == []
    enabled = config.model_copy(
        update={"hardware": config.hardware.model_copy(update={"supply_voltage": HAT})}
    )
    assert len(_supply_voltage_collectors(enabled, ReconnectPolicy())) == 1
