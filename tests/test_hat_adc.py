"""The rev 2.1 HAT's ADC: board map, record rules, collector, wiring.

The chip itself is ``tests/ads8688a_fake.py``; the protocol is tested in
``tests/test_ads8688a.py``. Here: what the readings become.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
import yaml

from src.analysis.analog_pulsation import full_rate_series
from src.collection import hat_adc_collector, supply_sense, supply_voltage_collector
from src.collection.ads8688a import RANGES, Ads8688a, Ads8688aError, Burst
from src.collection.collectors import _hat_adc_collectors
from src.collection.hat_adc_collector import (
    Assembler,
    Plan,
    collect_hat_adc,
    collect_hat_adc_from_config,
    spi_device_for,
)
from src.collection.hat_revision import resolve_hat_revision
from src.collection.reconnect import ReconnectPolicy
from src.collection.vehicle_power import VEHICLE_POWER
from src.config import CanaryConfig, HatAdcConfig, SupplyVoltageConfig
from src.decode.analog_sensors import load_analog_sensor_catalog
from src.decode.hat_adc_board import (
    SENSOR_SUPPLY_CHANNEL,
    SUPPLY_CHANNEL,
    UNIPOLAR_RANGES,
    HatAdcBoard,
    load_hat_adc_board,
)
from src.paths import DATA_DIR
from src.telemetry import TelemetryRecord
from src.yaml_load import load_yaml
from tests.ads8688a_fake import AbsentBus, FakeAds8688a

REPO = Path(__file__).resolve().parents[1]
BOARD_PATH = DATA_DIR / "hat_adc_rev2_1.yaml"
BOARD = load_hat_adc_board(BOARD_PATH)
REV21 = resolve_hat_revision("2.1")
REV20 = resolve_hat_revision("2.0")
LSB = 5.12 / 65536

# --- the circuit, forwards: what U8 measures for a given truth -------------
# U8's inputs are 1 Mohm leaking towards 2.5 V; each channel has a series
# resistor to AIN_nP and a match on AIN_nGND (data/hat_adc_rev2_1.yaml). The
# decode runs this backwards, so feeding it these differentials must give the
# truth back.
RIN, VB = BOARD.adc_input_ohms, BOARD.adc_input_bias_volts
V1, R1, SS = BOARD.voltage_ports[0], BOARD.resistive_ports[0], BOARD.sensor_supply


def _k(ohms: float) -> float:
    return ohms / (ohms + RIN)


def measured(node: float, series: float, match: float) -> float:
    return node * (1 - _k(series)) + VB * (_k(series) - _k(match))


def rail_diff(rail: float) -> float:
    s = BOARD.supply
    return measured(rail / s.divider, s.thevenin_ohms, s.match_ohms)


def r1_diff(sensor_ohms: float, supply: float) -> float:
    """The R1 node with the pull-up, the sensor and U8's leakage all on it."""
    to_adc = 1 / (R1.series_ohms + RIN)
    conductance = 1 / R1.pull_up_ohms + 1 / sensor_ohms + to_adc
    node = (supply / R1.pull_up_ohms + VB * to_adc) / conductance
    return measured(node, R1.series_ohms, R1.match_ohms)


#: A dev-truck afternoon: 12.68 V rail, U9 at 5.00 V, the TMAP at 1.0 V on V1
#: and its NTC at 2.5 kohm on R1, the gauge at 51.0 psi.
RAIL = 12.68
NTC = 2500.0
GAUGE = (51.0 + 12.5) / 25.0
INPUTS = {
    0: rail_diff(RAIL),
    1: measured(1.0, V1.series_ohms, V1.match_ohms),
    2: r1_diff(NTC, 5.0),
    3: measured(5.0, SS.series_ohms, SS.match_ohms),
    4: GAUGE,
}


def code(volts: float) -> int:
    return min(round(volts / LSB), 0xFFFF)


def bursts(volts: dict[int, float], start: float, hz: float, n: int) -> list[Burst]:
    codes = {i: code(v) for i, v in volts.items()}
    return [Burst(stamp=start + k / hz, codes=dict(codes)) for k in range(n)]


def run(assembler: Assembler, items: list[Burst]) -> list[TelemetryRecord]:
    out: list[TelemetryRecord] = []
    for b in items:
        out += assembler.burst(b)
    return out


def by_channel(records: list[TelemetryRecord]) -> dict[str, list[TelemetryRecord]]:
    out: dict[str, list[TelemetryRecord]] = {}
    for r in records:
        out.setdefault(r.channel, []).append(r)
    return out


def full_assembler(**rates: float) -> Assembler:
    return Assembler(
        BOARD,
        Plan(supply=True, sensors=True),
        supply_hz=rates.get("supply_hz", 10.0),
        record_hz=rates.get("record_hz", 100.0),
        slow_hz=rates.get("slow_hz", 10.0),
    )


# --------------------------------------------------------------------------
# The board map
# --------------------------------------------------------------------------


def test_the_rev_2_1_map_accounts_for_every_input() -> None:
    claimed = sorted([*BOARD.roles().values(), *BOARD.tied_to_ground])
    assert claimed == list(range(8))
    assert BOARD.tied_to_ground == [5, 6, 7]  # section 13.5.13


def test_an_unmapped_or_doubly_mapped_input_is_refused() -> None:
    raw = load_yaml(BOARD_PATH.read_text())
    raw["tied_to_ground"] = [5, 6]
    with pytest.raises(ValueError, match=r"inputs \[7\] are not mapped"):
        HatAdcBoard.model_validate(raw)
    raw["tied_to_ground"] = [4, 5, 6, 7]
    with pytest.raises(ValueError, match="input 4 is both"):
        HatAdcBoard.model_validate(raw)


def test_range_names_are_the_drivers() -> None:
    for name, full_scale in UNIPOLAR_RANGES.items():
        assert RANGES[name].low == 0.0
        assert RANGES[name].high == full_scale


def test_the_fuel_gauge_decodes_the_same_on_both_adcs() -> None:
    """Section 13.5.8: the volts-to-psi conversion and the gate are written
    once. The electrical legs differ; the gauge's leg may not."""
    usb = load_analog_sensor_catalog(DATA_DIR / "usb_adc_sensors.yaml").channel_for_input(0)
    hat = next(s for s in BOARD.sensors if s.name == "FUEL_PRESSURE")
    assert usb is not None and usb.name == hat.name
    for leg in ("device", "source", "transfer", "valid_volts", "record_unit", "offset"):
        assert getattr(hat, leg) == getattr(usb, leg), leg
    assert hat.divider is None  # no divider ahead of U8


def test_the_12v_divider_corrects_for_the_adc_loading_it() -> None:
    diff = rail_diff(RAIL)
    assert BOARD.rail_volts(diff) == pytest.approx(RAIL, rel=1e-9)
    # Uncorrected, the divider alone would read the 18 k source 1.8 % low.
    assert diff * BOARD.supply.divider / RAIL == pytest.approx(1 / 1.018, rel=1e-3)


def test_the_resistance_solve_includes_the_adcs_leakage() -> None:
    """The reviewer's numbers: across a cold NTC's tens of kilohms U8's 1 Mohm
    is a few per cent. Ignoring it, 50 kohm read 2.2 % low."""
    for ohms in (500.0, 2500.0, 10_000.0, 50_000.0, 100_000.0):
        diff = r1_diff(ohms, 5.0)
        node = BOARD.node_volts(diff, series_ohms=R1.series_ohms, match_ohms=R1.match_ohms)
        assert BOARD.resistance(R1, node, 5.0) == pytest.approx(ohms, rel=1e-9)
    naive_ratio = r1_diff(50_000.0, 5.0) / 5.0
    naive = R1.pull_up_ohms * naive_ratio / (1 - naive_ratio)
    assert naive / 50_000.0 < 0.98


def test_every_recorded_name_is_one_roles() -> None:
    assert SUPPLY_CHANNEL == supply_voltage_collector.CHANNEL  # rev 2.0's channel
    raw = load_yaml(BOARD_PATH.read_text())
    raw["sensors"][0]["name"] = "PORT_V1_RATIO"
    with pytest.raises(ValueError, match=r"\['PORT_V1_RATIO'\] would be recorded by two roles"):
        HatAdcBoard.model_validate(raw)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


def test_a_steady_board_records_every_channel_in_engineering_units() -> None:
    assembler = full_assembler()
    got = by_channel(run(assembler, bursts(INPUTS, 1000.0, 500.0, 251)))
    assert got["SUPPLY_VOLTAGE"][0].value == pytest.approx(RAIL, abs=0.002)
    assert got[SENSOR_SUPPLY_CHANNEL][0].value == pytest.approx(5.0, abs=1e-3)
    assert got["FUEL_PRESSURE"][0].value == pytest.approx(51.0 * 6.894757, abs=0.05)
    assert got["FUEL_PRESSURE"][0].unit == "kPa"
    assert got["PORT_V1_VOLTAGE"][0].value == pytest.approx(1.0, abs=1e-3)
    assert got["PORT_V1_RATIO"][0].value == pytest.approx(0.2, abs=1e-4)
    assert got["PORT_R1_RESISTANCE"][0].value == pytest.approx(NTC, rel=2e-4)
    assert got["PORT_R1_RESISTANCE"][0].unit == "ohm"


def test_fast_channels_are_stored_at_record_hz_and_slow_ones_at_slow_hz() -> None:
    got = by_channel(run(full_assembler(), bursts(INPUTS, 1000.0, 500.0, 501)))  # one second
    assert len(got["FUEL_PRESSURE"]) == 100
    assert len(got["PORT_V1_RATIO"]) == 100
    assert len(got["PORT_R1_RESISTANCE"]) == 10
    assert len(got["SUPPLY_VOLTAGE"]) == 10
    first = got["FUEL_PRESSURE"][0]
    assert first.metadata["adc_samples"] == 5
    # Stamped at the mean of the five bursts it averages.
    assert first.timestamp.timestamp() == pytest.approx(1000.0 + 2 / 500.0, abs=1e-6)


def test_a_full_rate_capture_stores_every_burst_of_the_fast_channels() -> None:
    """A pulsation capture (the profile's analog_full_rate) wants every
    sample, as the USB path stores every sample; the slow channels stay slow."""
    assembler = Assembler(
        BOARD,
        Plan(supply=True, sensors=True),
        supply_hz=10.0,
        record_hz=100.0,
        slow_hz=10.0,
        full_rate=lambda: True,
    )
    items = [replace(b, seq=k) for k, b in enumerate(bursts(INPUTS, 1000.0, 500.0, 501))]
    got = by_channel(run(assembler, items))
    assert len(got["FUEL_PRESSURE"]) == 501
    first = got["FUEL_PRESSURE"][7].metadata
    assert (first["adc_seq"], first["adc_clock"], "adc_samples" in first) == (7, "software", False)
    assert len(got["PORT_R1_RESISTANCE"]) == 10
    assert "adc_samples" in got["PORT_R1_RESISTANCE"][0].metadata  # slow stays averaged
    # The pulsation analysis takes them by sequence, as it takes the USB path's.
    frame = pd.DataFrame(
        {
            "source": [r.source for r in got["FUEL_PRESSURE"]],
            "channel": [r.channel for r in got["FUEL_PRESSURE"]],
            "timestamp": [r.timestamp for r in got["FUEL_PRESSURE"]],
            "value": [r.value for r in got["FUEL_PRESSURE"]],
            "metadata_json": [json.dumps(r.metadata) for r in got["FUEL_PRESSURE"]],
        }
    )
    series = full_rate_series(frame)
    assert list(series["seq"][:3]) == [0, 1, 2] and len(series) == 501


def test_a_clipped_sensor_supply_forms_no_ratio() -> None:
    """Review item 11: 5V_SENS can sit above the 5.12 V range's top code.
    One full-scale code in a window marks the whole window clipped."""
    items = bursts(INPUTS, 1000.0, 500.0, 51)
    items[3] = Burst(items[3].stamp, {**items[3].codes, 3: 0xFFFF})
    got = by_channel(run(full_assembler(), items))
    assert got["SENSOR_SUPPLY_VOLTAGE_FAULT"][0].value == "clipped"
    assert got["PORT_V1_RATIO_FAULT"][0].value == "sensor_supply_clipped"
    assert got["PORT_R1_RESISTANCE_FAULT"][0].value == "sensor_supply_clipped"
    assert "PORT_V1_RATIO" in got  # later fast windows are clean again
    assert got["PORT_V1_VOLTAGE"]  # volts need no supply
    assert got["FUEL_PRESSURE"]  # the gauge has its own supply


def test_any_input_at_full_scale_is_clipped_not_a_number() -> None:
    """A full-scale CH0 code would read as a valid 28.9 V; a clipped V1 as a
    plain 5.12 V."""
    items = bursts(INPUTS, 1000.0, 500.0, 101)
    items[2] = Burst(items[2].stamp, {**items[2].codes, 0: 0xFFFF, 1: 0xFFFF})
    got = by_channel(run(full_assembler(), items))
    assert got["SUPPLY_VOLTAGE_FAULT"][0].value == "clipped"
    assert got["PORT_V1_VOLTAGE_FAULT"][0].value == "clipped"
    assert got["PORT_V1_RATIO_FAULT"][0].value == "clipped"
    assert got["SUPPLY_VOLTAGE"]  # the next windows are clean


def test_sensor_supply_off_is_a_state_not_a_ratio() -> None:
    got = by_channel(run(full_assembler(), bursts({**INPUTS, 3: 0.02}, 1000.0, 500.0, 51)))
    assert {r.value for r in got["PORT_V1_RATIO_FAULT"]} == {"sensor_supply_low"}
    assert {r.value for r in got["PORT_R1_RESISTANCE_FAULT"]} == {"sensor_supply_low"}
    assert "PORT_V1_RATIO" not in got


def test_an_unplugged_resistive_sensor_reads_open() -> None:
    open_port = r1_diff(math.inf, 5.0)
    got = by_channel(run(full_assembler(), bursts({**INPUTS, 2: open_port}, 1000.0, 500.0, 51)))
    assert got["PORT_R1_RESISTANCE_FAULT"][0].value == "open"
    assert "PORT_R1_RESISTANCE" not in got


def test_a_gauge_outside_its_window_is_a_fault_state() -> None:
    got = by_channel(run(full_assembler(), bursts({**INPUTS, 4: 0.23}, 1000.0, 500.0, 11)))
    assert got["FUEL_PRESSURE_FAULT"][0].value == "below_window"
    assert "FUEL_PRESSURE" not in got


def test_the_supply_only_plan_reads_and_records_only_the_rail() -> None:
    plan = Plan(supply=True, sensors=False)
    assert plan.inputs(BOARD) == [0]
    assert plan.channels(BOARD) == {"SUPPLY_VOLTAGE"}
    assembler = Assembler(BOARD, plan, supply_hz=10.0, record_hz=100.0, slow_hz=10.0)
    got = by_channel(run(assembler, bursts({0: INPUTS[0]}, 1000.0, 100.0, 21)))
    assert set(got) == {"SUPPLY_VOLTAGE"}


# --------------------------------------------------------------------------
# The collector
# --------------------------------------------------------------------------


def _configured(fake: FakeAds8688a, inputs: list[int]) -> Ads8688a:
    adc = Ads8688a(fake)
    adc.configure(dict.fromkeys(inputs, RANGES["0-5.12"]))
    return adc


async def _run_for(seconds: float, coro_factory) -> asyncio.Queue[TelemetryRecord]:  # type: ignore[no-untyped-def]
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(coro_factory(queue, stop))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    return queue


def test_the_collector_records_and_notes_the_rail() -> None:
    VEHICLE_POWER.reset()
    inputs = Plan(supply=True, sensors=True).inputs(BOARD)
    adc = _configured(FakeAds8688a(INPUTS), inputs)
    assembler = full_assembler(supply_hz=20.0, slow_hz=20.0)

    queue = asyncio.run(
        _run_for(
            0.35,
            lambda q, s: collect_hat_adc(adc, assembler, inputs, q, s, burst_hz=200.0),
        )
    )
    channels = set()
    while not queue.empty():
        channels.add(queue.get_nowait().channel)
    assert {"SUPPLY_VOLTAGE", "FUEL_PRESSURE", "PORT_V1_RATIO"} <= channels
    assert VEHICLE_POWER.latest() == pytest.approx(RAIL, abs=0.01)
    VEHICLE_POWER.reset()


def test_a_chip_that_resets_mid_run_is_configured_again_in_place(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reset costs the burst that found it, not a reconnect: the sensor
    supply stays on and the channels keep coming (review finding 2)."""
    caplog.set_level(logging.WARNING, logger="src.collection.hat_adc_collector")
    fake = FakeAds8688a(INPUTS)
    inputs = [0, 3]
    adc = _configured(fake, inputs)
    assembler = Assembler(
        BOARD, Plan(supply=True, sensors=False), supply_hz=20.0, record_hz=100.0, slow_hz=20.0
    )

    async def scenario() -> list[TelemetryRecord]:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(
            collect_hat_adc(adc, assembler, inputs, queue, stop, burst_hz=200.0)
        )
        await asyncio.sleep(0.15)
        fake.transfer([bytes([0x85, 0x00, 0x00, 0x00])])  # a brown-out resets it
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=5)  # still running: no exception
        out = []
        while not queue.empty():
            out.append(queue.get_nowait())
        return out

    records = asyncio.run(scenario())
    assert any("configuring it again" in r.getMessage() for r in caplog.records)
    assert fake.registers[0x05] == RANGES["0-5.12"].code  # configured again
    late = [r for r in records if r.channel == "SUPPLY_VOLTAGE"][-1]
    assert late.value == pytest.approx(RAIL, abs=0.01)  # and never misscaled
    rails = [r.value for r in records if r.channel == "SUPPLY_VOLTAGE"]
    assert all(isinstance(v, float) and abs(v - RAIL) < 0.01 for v in rails)


def test_a_chip_that_keeps_resetting_ends_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    class ResetsEveryBurst(FakeAds8688a):
        def transfer(self, frames: Sequence[bytes]) -> list[bytes]:
            if len(frames[0]) == 4 and frames[0][0] & 0xC0 == 0xC0:  # a burst
                super().transfer([bytes([0x85, 0x00, 0x00, 0x00])])
            return super().transfer(frames)

    fake = ResetsEveryBurst(INPUTS)
    adc = _configured(fake, [0])
    assembler = Assembler(
        BOARD, Plan(supply=True, sensors=False), supply_hz=10.0, record_hz=100.0, slow_hz=10.0
    )

    async def scenario() -> None:
        stop = asyncio.Event()
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        with pytest.raises(Ads8688aError, match="3 times in 10 s"):
            await asyncio.wait_for(
                collect_hat_adc(adc, assembler, [0], queue, stop, burst_hz=100.0), timeout=5
            )
        assert queue.empty()  # not one misscaled reading

    asyncio.run(scenario())


class _FakeLines:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self.log = log

    def set(self, levels: dict[int, bool]) -> None:
        self.log.append(("set", dict(levels)))

    def close(self) -> None:
        self.log.append(("close", None))


def _from_config(
    plan: Plan,
    *,
    power_log: list[tuple[str, object]],
    power_error: bool = False,
    seconds: float = 0.25,
) -> FakeAds8688a:
    fake = FakeAds8688a(INPUTS)

    def power(chip: str, line: int) -> _FakeLines:
        if power_error:
            raise OSError("EBUSY")
        power_log.append(("claim", (chip, line)))
        return _FakeLines(power_log)

    config = HatAdcConfig(enabled=True, burst_hz=200.0, record_hz=50.0, slow_record_hz=10.0)
    asyncio.run(
        _run_for(
            seconds,
            lambda q, s: collect_hat_adc_from_config(
                config,
                SupplyVoltageConfig(enabled=True),
                plan,
                REV21,
                q,
                s,
                bus_factory=lambda device, speed: fake,
                power_factory=power,  # type: ignore[arg-type]
                reconnect_policy=None,
            ),
        )
    )
    return fake


def test_the_sensor_supply_is_switched_on_for_the_run_and_off_after() -> None:
    log: list[tuple[str, object]] = []
    fake = _from_config(Plan(supply=True, sensors=True), power_log=log)
    assert log[0] == ("claim", ("/dev/gpiochip0", 19))
    assert log[-2:] == [("set", {19: False}), ("close", None)]  # low, then released
    assert fake.closed


def test_an_absent_chip_never_powers_the_sensor_rail() -> None:
    """Review finding 5: the enable waits for the chip to answer."""
    log: list[tuple[str, object]] = []
    absent = AbsentBus()

    def power(chip: str, line: int) -> _FakeLines:
        log.append(("claim", (chip, line)))
        return _FakeLines(log)

    with pytest.raises(Ads8688aError):
        asyncio.run(
            collect_hat_adc_from_config(
                HatAdcConfig(enabled=True),
                SupplyVoltageConfig(enabled=True),
                Plan(supply=True, sensors=True),
                REV21,
                asyncio.Queue(),
                asyncio.Event(),
                bus_factory=lambda d, s: absent,
                power_factory=power,  # type: ignore[arg-type]
                reconnect_policy=None,
            )
        )
    assert log == []
    assert absent.closed


def test_a_bus_failing_mid_run_still_drops_the_enable_and_closes_the_bus() -> None:
    class DiesLater(FakeAds8688a):
        calls = 0

        def transfer(self, frames: Sequence[bytes]) -> list[bytes]:
            DiesLater.calls += 1
            if DiesLater.calls > 20:
                raise OSError("EIO")
            return super().transfer(frames)

    fake = DiesLater(INPUTS)
    log: list[tuple[str, object]] = []
    with pytest.raises(OSError, match="EIO"):
        asyncio.run(
            collect_hat_adc_from_config(
                HatAdcConfig(enabled=True, burst_hz=200.0, record_hz=50.0),
                SupplyVoltageConfig(enabled=True),
                Plan(supply=True, sensors=True),
                REV21,
                asyncio.Queue(),
                asyncio.Event(),
                bus_factory=lambda d, s: fake,
                power_factory=lambda c, line: _FakeLines(log),  # type: ignore[arg-type,return-value]
                reconnect_policy=None,
            )
        )
    assert log[-2:] == [("set", {19: False}), ("close", None)]
    assert fake.closed
    assert not any(th.name == "hat-adc-reader" for th in threading.enumerate())


def test_reading_only_the_rail_leaves_the_sensor_supply_alone() -> None:
    log: list[tuple[str, object]] = []
    _from_config(Plan(supply=True, sensors=False), power_log=log)
    assert log == []


def test_a_busy_enable_line_is_logged_and_the_rail_still_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="src.collection.hat_adc_collector")
    _from_config(Plan(supply=True, sensors=True), power_log=[], power_error=True)
    assert any("SENS_PWR_EN" in r.getMessage() for r in caplog.records)


def test_rev_2_0_has_no_spi_device_for_this_adc() -> None:
    with pytest.raises(ValueError, match="has no ADS8688A"):
        spi_device_for(HatAdcConfig(), REV20)
    assert spi_device_for(HatAdcConfig(), REV21) == "/dev/spidev0.1"
    assert spi_device_for(HatAdcConfig(spi_device="/dev/spidev1.0"), REV21) == "/dev/spidev1.0"


def test_one_shot_supply_reading_for_the_vin_probe() -> None:
    VEHICLE_POWER.reset()
    volts = hat_adc_collector.read_supply_once(
        HatAdcConfig(), REV21, bus_factory=lambda d, s: FakeAds8688a(INPUTS)
    )
    assert volts == pytest.approx(RAIL, abs=0.002)
    assert VEHICLE_POWER.latest() == pytest.approx(RAIL, abs=0.002)
    assert (
        hat_adc_collector.read_supply_once(
            HatAdcConfig(), REV21, bus_factory=lambda d, s: AbsentBus()
        )
        is None
    )
    VEHICLE_POWER.reset()


def test_the_vin_probe_asks_the_fitted_revisions_adc(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        hat_adc_collector, "read_supply_once", lambda c, r: asked.append("ads8688a")
    )
    monkeypatch.setattr(
        supply_voltage_collector, "read_supply_once", lambda c: asked.append("ads1015")
    )
    config = CanaryConfig.from_yaml(REPO / "canary.example.yaml").hardware
    on = SupplyVoltageConfig(enabled=True)
    supply_sense.read_supply_once(
        config.model_copy(update={"supply_voltage": on, "hat_revision": "2.1"})
    )
    supply_sense.read_supply_once(
        config.model_copy(update={"supply_voltage": on, "hat_revision": "2.0"})
    )
    assert asked == ["ads8688a", "ads1015"]


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def _hardware(**update: object) -> CanaryConfig:
    config = CanaryConfig.from_yaml(REPO / "canary.example.yaml")
    return config.model_copy(update={"hardware": config.hardware.model_copy(update=update)})


def test_hat_adc_on_a_rev_2_0_board_is_refused_with_a_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="src.collection.collectors")
    config = _hardware(hat_adc=HatAdcConfig(enabled=True), hat_revision="2.0")
    assert _hat_adc_collectors(config, ReconnectPolicy()) == []
    assert any("CANary-HAT rev 2.0 has none" in r.getMessage() for r in caplog.records)


def test_two_instruments_may_not_write_one_channel() -> None:
    base = CanaryConfig.from_yaml(REPO / "canary.example.yaml").hardware
    usb = base.usb_adc.model_copy(update={"enabled": True})
    config = _hardware(hat_adc=HatAdcConfig(enabled=True), hat_revision="2.1", usb_adc=usb)
    with pytest.raises(ValueError, match=r"\['FUEL_PRESSURE'\].*hardware.usb_adc"):
        _hat_adc_collectors(config, ReconnectPolicy())


def test_rev_2_0_supply_settings_on_a_rev_2_1_board_are_named_as_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="src.collection.collectors")
    config = _hardware(
        hat_revision="2.1", supply_voltage=SupplyVoltageConfig(enabled=True, divider_ratio=5.6)
    )
    assert len(_hat_adc_collectors(config, ReconnectPolicy())) == 1
    assert any("divider_ratio" in r.getMessage() for r in caplog.records)


def test_rates_must_nest() -> None:
    with pytest.raises(ValueError, match="slow_record_hz <= record_hz <= burst_hz"):
        HatAdcConfig(record_hz=1000.0, burst_hz=500.0)


# --------------------------------------------------------------------------
# Bring-up CLI (checkout_hat.sh runs it on a new board)
# --------------------------------------------------------------------------


def test_bring_up_prints_every_channel_and_releases_everything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake = FakeAds8688a(INPUTS)
    lines: list[tuple[str, object]] = []
    monkeypatch.setattr(hat_adc_collector, "linux_bus_factory", lambda d, s: fake)
    monkeypatch.setattr(
        hat_adc_collector, "linux_power_factory", lambda chip, line: _FakeLines(lines)
    )
    code_ = hat_adc_collector.main(
        ["--rev", "2.1", "--seconds", "1.2", "--config", str(tmp_path / "absent.yaml")]
    )
    out = capsys.readouterr().out
    assert code_ == 0
    for channel in (
        "SUPPLY_VOLTAGE",
        "SENSOR_SUPPLY_VOLTAGE",
        "FUEL_PRESSURE",
        "PORT_R1_RESISTANCE",
    ):
        assert channel in out
    assert lines[-2:] == [("set", {19: False}), ("close", None)]
    assert fake.closed


def test_bring_up_says_why_in_one_line(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert hat_adc_collector.main(["--rev", "2.0", "--config", str(tmp_path / "x.yaml")]) == 1
    assert "has no ADS8688A" in capsys.readouterr().out


# --------------------------------------------------------------------------
# The reflash battery floor never trusts an uncalibrated rev 2.1 sense
# --------------------------------------------------------------------------


def _gate_hardware(rev: str, *, gain_trim: float | None, tmp_path: Path):  # type: ignore[no-untyped-def]
    raw = load_yaml(BOARD_PATH.read_text())
    raw["supply"]["gain_trim"] = gain_trim
    board = tmp_path / "board.yaml"
    board.write_text(yaml.safe_dump(raw))
    base = CanaryConfig.from_yaml(REPO / "canary.example.yaml").hardware
    return base.model_copy(
        update={
            "hat_revision": rev,
            "supply_voltage": SupplyVoltageConfig(enabled=True),
            "hat_adc": HatAdcConfig(board=board),
        }
    )


def test_an_uncalibrated_rev_2_1_sense_is_withheld_from_the_write_gate(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from src.control.nonvolatile_write import (
        supply_voltage_from_latest_rows,
        supply_voltage_sources,
    )

    assert BOARD.supply.gain_trim is None  # the shipped map: not measured yet
    sources = supply_voltage_sources(_gate_hardware("2.1", gain_trim=None, tmp_path=tmp_path))
    assert ("sensor", "SUPPLY_VOLTAGE") not in sources.order
    now = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
    sense = {"source": "sensor", "channel": "SUPPLY_VOLTAGE", "value": 12.0, "timestamp": now}
    ecm = {"source": "obd", "channel": "CONTROL_MODULE_VOLTAGE", "value": 11.8, "timestamp": now}
    # A fresh sense reading 12.0 V is not believed; the ECM's 11.8 V is.
    reading = supply_voltage_from_latest_rows(
        [sense, ecm], now=now, max_age_seconds=5.0, sources=sources
    )
    assert (reading.volts, reading.source) == (11.8, "obd:CONTROL_MODULE_VOLTAGE")
    # With no ECM reading the write is refused, and the refusal says why.
    unknown = supply_voltage_from_latest_rows(
        [sense], now=now, max_age_seconds=5.0, sources=sources
    )
    assert unknown.volts is None
    assert unknown.reason is not None and "uncalibrated" in unknown.reason


def test_a_calibrated_sense_and_rev_2_0_s_are_trusted(tmp_path: Path) -> None:
    from src.control.nonvolatile_write import SUPPLY_VOLTAGE_SOURCES, supply_voltage_sources

    calibrated = supply_voltage_sources(_gate_hardware("2.1", gain_trim=1.004, tmp_path=tmp_path))
    rev20 = supply_voltage_sources(_gate_hardware("2.0", gain_trim=None, tmp_path=tmp_path))
    assert calibrated.order == rev20.order == SUPPLY_VOLTAGE_SOURCES
    assert calibrated.withheld == rev20.withheld == ()


def test_supply_records_say_whether_they_are_calibrated() -> None:
    got = by_channel(run(full_assembler(), bursts(INPUTS, 1000.0, 500.0, 101)))
    assert got["SUPPLY_VOLTAGE"][0].metadata["calibrated"] is False
