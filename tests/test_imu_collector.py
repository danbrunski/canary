"""IMU collector: records, units, lifecycle, reconnect, daemon wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.collection.collectors import _imu_collectors
from src.collection.imu_collector import (
    ImuHealth,
    batch_to_records,
    collect_imu,
    collect_imu_from_config,
)
from src.collection.lsm6dsox import FifoBatch, ImuSample, TemperatureSample
from src.collection.reconnect import ReconnectPolicy
from src.config import CanaryConfig, ImuConfig
from src.telemetry import TelemetryRecord
from tests.fakes import FakeLsmBus, lsm_fifo_instants

REPO = Path(__file__).resolve().parents[1]
HAT_IMU = ImuConfig(source="lsm6dsox", sample_rate_hz=104.0, fifo_poll_seconds=0.01)


def test_batch_to_records_emits_six_axes_and_temperature_with_units() -> None:
    batch = FifoBatch(
        samples=[ImuSample(1000.0, (0.01, -0.02, 1.0), (0.5, 0.0, -0.5))],
        temperatures=[TemperatureSample(1000.2, 31.5)],
    )
    records = batch_to_records(batch)
    by_channel = {r.channel: r for r in records}
    assert set(by_channel) == {
        "ACCEL_X",
        "ACCEL_Y",
        "ACCEL_Z",
        "GYRO_X",
        "GYRO_Y",
        "GYRO_Z",
        "TEMP",
    }
    assert all(r.source == "imu" for r in records)
    assert by_channel["ACCEL_Z"].value == 1.0 and by_channel["ACCEL_Z"].unit == "g"
    assert by_channel["GYRO_X"].unit == "deg/s"
    assert by_channel["TEMP"].unit == "degC"
    # Stamped at the sample instant, not the read instant.
    assert by_channel["ACCEL_X"].timestamp.timestamp() == pytest.approx(1000.0)


def test_a_sample_missing_one_vector_emits_only_the_other() -> None:
    records = batch_to_records(FifoBatch(samples=[ImuSample(5.0, None, (1.0, 2.0, 3.0))]))
    assert [r.channel for r in records] == ["GYRO_X", "GYRO_Y", "GYRO_Z"]


class FakeDevice:
    def __init__(self, batches: list[FifoBatch]) -> None:
        self.batches = list(batches)
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def read_fifo(self) -> FifoBatch:
        return self.batches.pop(0) if self.batches else FifoBatch()


async def _drain_until(queue: asyncio.Queue[TelemetryRecord], count: int) -> list[TelemetryRecord]:
    out: list[TelemetryRecord] = []
    while len(out) < count:
        out.append(await asyncio.wait_for(queue.get(), timeout=5))
    return out


async def test_collect_imu_enqueues_records_counts_overruns_and_powers_down() -> None:
    device = FakeDevice(
        [
            FifoBatch(samples=[ImuSample(1.0, (0, 0, 1), (0, 0, 0))], settling=3),
            FifoBatch(overrun=True),
            FifoBatch(samples=[ImuSample(2.0, (0, 0, 1), (0, 0, 0))]),
        ]
    )
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    health = ImuHealth()
    task = asyncio.create_task(collect_imu(device, queue, stop, poll_seconds=0.001, health=health))
    records = await _drain_until(queue, 12)
    stop.set()
    await task

    assert device.started and device.stopped
    assert [r.timestamp.timestamp() for r in records[::6]] == [1.0, 2.0]
    assert health.samples == 2 and health.overruns == 1 and health.last_sample_at == 2.0


async def test_the_device_is_powered_down_even_when_reading_fails() -> None:
    class Broken(FakeDevice):
        def read_fifo(self) -> FifoBatch:
            raise OSError("bus error")

    device = Broken([])
    with pytest.raises(OSError):
        await collect_imu(device, asyncio.Queue(), asyncio.Event(), poll_seconds=0.001)
    assert device.stopped


async def test_from_config_reconnects_after_a_missing_device() -> None:
    attempts: list[int] = []

    def factory(config: ImuConfig) -> FakeLsmBus:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("no such device")
        return FakeLsmBus(lsm_fifo_instants(0, 60, ticks0=0, ticks_per=384.0))

    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    policy = ReconnectPolicy(initial_delay_seconds=0.001, max_delay_seconds=0.001)
    task = asyncio.create_task(
        collect_imu_from_config(HAT_IMU, queue, stop, bus_factory=factory, reconnect_policy=policy)
    )
    first = await asyncio.wait_for(queue.get(), timeout=5)
    stop.set()
    await task
    assert len(attempts) == 2
    assert first.source == "imu"


async def test_from_config_refuses_a_chip_without_a_collector() -> None:
    with pytest.raises(ValueError, match="no collector"):
        await collect_imu_from_config(ImuConfig(source="bno055"), asyncio.Queue(), asyncio.Event())


def test_config_refuses_a_rate_the_lsm6dsox_cannot_do() -> None:
    with pytest.raises(ValueError, match="ODR 100"):
        ImuConfig(source="lsm6dsox")  # the pre-HAT default of 100 Hz
    with pytest.raises(ValueError, match="accel range"):
        ImuConfig(source="lsm6dsox", sample_rate_hz=104, accel_range_g=6)
    assert ImuConfig(source="lsm6dsox", sample_rate_hz=208).sample_rate_hz == 208


def _with_imu(config: CanaryConfig, imu: ImuConfig) -> CanaryConfig:
    return config.model_copy(update={"hardware": config.hardware.model_copy(update={"imu": imu})})


def test_imu_collector_runs_only_when_configured() -> None:
    config = CanaryConfig.from_yaml(REPO / "canary.example.yaml")
    assert _imu_collectors(config, ReconnectPolicy()) == []
    assert len(_imu_collectors(_with_imu(config, HAT_IMU), ReconnectPolicy())) == 1


def test_a_pre_hat_chip_is_refused_at_startup_not_silently_skipped() -> None:
    config = _with_imu(
        CanaryConfig.from_yaml(REPO / "canary.example.yaml"), ImuConfig(source="bno055")
    )
    with pytest.raises(ValueError, match="lsm6dsox"):
        _imu_collectors(config, ReconnectPolicy())
