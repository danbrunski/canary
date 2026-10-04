"""USB ADC analog sensors: the calibration catalog, the window gate, the collector.

Known answers are the AEM fuel gauge's bring-up on 2026-10-03, read through the
committed catalog (measured 3.282 V reference, 9.82 k / 19.64 k divider): 476
counts at ACC with the gauge at 0.574 V on a DMM, and 2114 counts running with
the factory rail pressure of 350 kPa.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from src.collection.collectors import _usb_adc_collectors
from src.collection.reconnect import ReconnectPolicy
from src.collection.usb_adc_collector import (
    UsbAdcStalledError,
    UsbAdcStats,
    collect_usb_adc,
    collect_usb_adc_from_config,
    counts_to_records,
    parse_line,
)
from src.config import CanaryConfig, SensorCanConfig, UsbAdcConfig
from src.decode.analog_sensors import (
    AnalogFault,
    AnalogReading,
    AnalogSensorCatalog,
    decode_counts,
    load_analog_sensor_catalog,
    sensor_volts,
)
from src.telemetry import TelemetryRecord

REPO = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO / "data/usb_adc_sensors.yaml"
CATALOG = load_analog_sensor_catalog(CATALOG_PATH)
FUEL = CATALOG.channels[0]
PSI_TO_KPA = 6.894757293168361


def _raw_catalog() -> dict[str, Any]:
    loaded = yaml.safe_load(CATALOG_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _catalog_with(**channel_updates: Any) -> AnalogSensorCatalog:
    raw = _raw_catalog()
    raw["channels"][0].update(channel_updates)
    return AnalogSensorCatalog.model_validate(raw)


def test_committed_catalog_is_the_fuel_gauge_on_input_0() -> None:
    assert (FUEL.name, FUEL.input, FUEL.record_unit) == ("FUEL_PRESSURE", 0, "kPa")
    assert FUEL.sensor_volts_per_pin_volt == pytest.approx(1.5, abs=1e-4)


def test_zero_pressure_known_answer_matches_the_dmm() -> None:
    """476 counts was 0.3825 V at the pins and 0.574 V at the gauge on the DMM."""
    assert sensor_volts(CATALOG.module, FUEL, 476) == pytest.approx(0.574, abs=0.002)
    reading = decode_counts(CATALOG.module, FUEL, 476)
    assert isinstance(reading, AnalogReading)
    # The gauge face read 0 here; the analog output reads +1.8 psi (offset left 0).
    assert reading.value / PSI_TO_KPA == pytest.approx(1.8, abs=0.05)


def test_running_known_answer_is_the_factory_rail_pressure() -> None:
    reading = decode_counts(CATALOG.module, FUEL, 2114)
    assert isinstance(reading, AnalogReading)
    assert reading.unit == "kPa"
    assert reading.value == pytest.approx(351.9, abs=0.5)  # 51.04 psi; factory 350 kPa


@pytest.mark.parametrize(
    ("counts", "kind"),
    [
        (0, "below_window"),  # open WHITE, or WHITE shorted to BROWN
        (300, "below_window"),
        (3800, "above_window"),
        (4095, "above_window"),  # saturated: WHITE on a supply
    ],
)
def test_outside_the_window_is_a_fault_never_a_number(counts: int, kind: str) -> None:
    result = decode_counts(CATALOG.module, FUEL, counts)
    assert isinstance(result, AnalogFault)
    assert result.kind == kind


def test_window_edges_decode_to_the_sensors_published_range() -> None:
    low = decode_counts(CATALOG.module, FUEL, 417)
    high = decode_counts(CATALOG.module, FUEL, 3743)
    assert isinstance(low, AnalogReading) and isinstance(high, AnalogReading)
    assert low.value / PSI_TO_KPA == pytest.approx(0.0, abs=0.1)
    assert high.value / PSI_TO_KPA == pytest.approx(100.0, abs=0.1)


def test_offset_trims_in_the_transfer_unit() -> None:
    catalog = _catalog_with(offset=-1.8)
    trimmed = decode_counts(catalog.module, catalog.channels[0], 2114)
    plain = decode_counts(CATALOG.module, FUEL, 2114)
    assert isinstance(trimmed, AnalogReading) and isinstance(plain, AnalogReading)
    assert plain.value - trimmed.value == pytest.approx(1.8 * PSI_TO_KPA)


def test_a_window_the_adc_cannot_resolve_is_refused() -> None:
    """Without the divider, 4.50 V saturates a 3.282 V ADC: max reads as valid."""
    with pytest.raises(ValidationError, match="full scale"):
        _catalog_with(divider=None)


def test_unknown_unit_conversion_is_refused() -> None:
    with pytest.raises(ValidationError, match="no conversion"):
        _catalog_with(record_unit="mmHg")


def test_reference_volts_is_required() -> None:
    raw = _raw_catalog()
    del raw["module"]["reference_volts"]
    with pytest.raises(ValidationError, match="reference_volts"):
        AnalogSensorCatalog.model_validate(raw)


def test_two_channels_on_one_input_are_refused() -> None:
    raw = _raw_catalog()
    raw["channels"].append({**raw["channels"][0], "name": "OIL_PRESSURE"})
    with pytest.raises(ValidationError, match="only one channel"):
        AnalogSensorCatalog.model_validate(raw)


def test_parse_line() -> None:
    assert parse_line(b"CH0:0477\t0.382V") == (0, 477)
    assert parse_line(b"CH9:2212\t1.782V") == (9, 2212)
    assert parse_line(b"CH0:2114") == (0, 2114)  # firmware/usb-adc: no volts column
    assert parse_line(b"0477\t0.382V") is None  # the tail of a line cut by the open
    assert parse_line(b"") is None
    assert parse_line(b"CH0:") is None


def test_records_carry_counts_volts_and_the_raw_line() -> None:
    [record] = counts_to_records(CATALOG, 0, 2114, 100.0, b"CH0:2114\t1.694V")
    assert (record.source, record.channel, record.unit) == ("sensor", "FUEL_PRESSURE", "kPa")
    assert record.payload == b"CH0:2114\t1.694V"
    assert record.metadata["adc_counts"] == 2114
    assert record.metadata["sensor_volts"] == pytest.approx(2.5415, abs=1e-3)


def test_fault_records_are_a_state_channel() -> None:
    [record] = counts_to_records(CATALOG, 0, 0, 100.0, b"CH0:0000\t0.000V")
    assert (record.channel, record.value, record.unit) == (
        "FUEL_PRESSURE_FAULT",
        "below_window",
        "",
    )


def test_unconfigured_inputs_record_nothing() -> None:
    assert counts_to_records(CATALOG, 3, 2193, 100.0, b"CH3:2193\t1.767V") == []


def _frame(ch0: int) -> list[bytes]:
    return [f"CH{n}:{ch0 if n == 0 else 2000:04d}\t1.000V\r\n".encode() for n in range(10)] + [
        b"\r\n"
    ]


class FakeSerial:
    """Hands out ``lines``, then sets ``stop`` and times out forever."""

    def __init__(self, lines: Iterable[bytes], stop: asyncio.Event | None = None) -> None:
        self.lines = list(lines)
        self.stop = stop
        self.flushed = False
        self.closed = False

    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        if self.stop is not None:
            self.stop.set()
        return b""

    def reset_input_buffer(self) -> None:
        self.flushed = True

    def close(self) -> None:
        self.closed = True


async def _drain(queue: asyncio.Queue[TelemetryRecord]) -> list[TelemetryRecord]:
    out = []
    while not queue.empty():
        out.append(await queue.get())
    return out


async def test_collector_discards_the_first_frame_then_records_each_reading() -> None:
    stop = asyncio.Event()
    # Opened mid-frame: a cut line, the rest of a stale frame, then two frames.
    stale = [b"0476\t0.381V\r\n", *_frame(476)[1:]]
    reader = FakeSerial([*stale, *_frame(2114), *_frame(0)], stop)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stats = UsbAdcStats()
    stamps = iter(float(n) for n in range(1000))
    await collect_usb_adc(
        reader, CATALOG, queue, stop, stall_seconds=60.0, now_fn=lambda: next(stamps), stats=stats
    )
    records = await _drain(queue)
    assert [(r.channel, r.value) for r in records] == [
        ("FUEL_PRESSURE", pytest.approx(351.9, abs=0.5)),
        ("FUEL_PRESSURE_FAULT", "below_window"),
    ]
    assert records[0].timestamp < records[1].timestamp
    assert (stats.lines, stats.faults, stats.parse_errors) == (20, 1, 0)


async def test_collector_counts_garbage_lines_after_sync() -> None:
    stop = asyncio.Event()
    reader = FakeSerial([b"\r\n", b"\x00\xffCH", *_frame(2114)], stop)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stats = UsbAdcStats()
    await collect_usb_adc(reader, CATALOG, queue, stop, stall_seconds=60.0, stats=stats)
    assert stats.parse_errors == 1
    assert [r.channel for r in await _drain(queue)] == ["FUEL_PRESSURE"]


async def test_a_silent_module_raises_so_the_port_is_reopened() -> None:
    clock = iter(float(n) for n in range(1000))
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    with pytest.raises(UsbAdcStalledError):
        await collect_usb_adc(
            FakeSerial([]),
            CATALOG,
            queue,
            asyncio.Event(),
            stall_seconds=5.0,
            monotonic=lambda: next(clock),
        )


async def test_from_config_flushes_the_port_and_closes_it() -> None:
    stop = asyncio.Event()
    reader = FakeSerial([b"\r\n", *_frame(2114)], stop)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    config = UsbAdcConfig(enabled=True, sensors=CATALOG_PATH)
    await collect_usb_adc_from_config(config, queue, stop, factory=lambda _c: reader)
    assert reader.flushed and reader.closed
    assert [r.channel for r in await _drain(queue)] == ["FUEL_PRESSURE"]


async def test_from_config_refuses_a_bad_catalog_without_retrying(tmp_path: Path) -> None:
    bad = tmp_path / "sensors.yaml"
    bad.write_text("module: {device: x, full_scale_counts: 4095}\nchannels: []\n")
    config = UsbAdcConfig(enabled=True, sensors=bad)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    with pytest.raises(ValidationError):
        await collect_usb_adc_from_config(config, queue, asyncio.Event())


def _with_hardware(config: CanaryConfig, **updates: Any) -> CanaryConfig:
    return config.model_copy(update={"hardware": config.hardware.model_copy(update=updates)})


def test_collector_runs_only_when_enabled() -> None:
    config = CanaryConfig.from_yaml(REPO / "canary.example.yaml")
    assert _usb_adc_collectors(config, ReconnectPolicy()) == []
    enabled = _with_hardware(config, usb_adc=UsbAdcConfig(enabled=True))
    assert len(_usb_adc_collectors(enabled, ReconnectPolicy())) == 1


def test_two_instruments_may_not_write_one_channel() -> None:
    """The sensor CAN catalog also defines FUEL_PRESSURE (the AEMnet layout)."""
    config = _with_hardware(
        CanaryConfig.from_yaml(REPO / "canary.example.yaml"),
        usb_adc=UsbAdcConfig(enabled=True),
        sensor_can=SensorCanConfig(enabled=True),
    )
    with pytest.raises(ValueError, match="FUEL_PRESSURE"):
        _usb_adc_collectors(config, ReconnectPolicy())
