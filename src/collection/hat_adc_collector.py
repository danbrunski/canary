"""Record the CANary-HAT rev 2.1's ADC: 12 V sense, sensor supply, fuel
pressure and the two generic sensor ports.

The chip is an ADS8688A on SPI0 CE1 (:mod:`src.collection.ads8688a`); what each
input is wired to is ``data/hat_adc_rev2_1.yaml``
(:mod:`src.decode.hat_adc_board`). Records, all ``sensor:``:

=========================== ======== ============================================
``SUPPLY_VOLTAGE``          V        CH0, the protected ``+12V`` rail: rev 2.0's
                                     channel, now from this chip. Fed to
                                     :data:`~src.collection.vehicle_power.VEHICLE_POWER`.
``SENSOR_SUPPLY_VOLTAGE``   V        CH3, ``5V_SENS``.
``FUEL_PRESSURE``           kPa      CH4, decoded exactly as the USB ADC path does.
``PORT_V1_VOLTAGE``         V        CH1, the voltage port's terminal.
``PORT_V1_RATIO``                    CH1 over CH3: what a ratiometric sensor reports.
``PORT_R1_RESISTANCE``      ohm      CH2, solved against the fitted pull-up.
=========================== ======== ============================================

A reading that is a state rather than a number goes to ``<channel>_FAULT``
instead, never to the channel: a sensor outside its valid window, a port
whose sensor supply is off or clipped, an open resistive port, an input that
hit full scale. **The clipping rule** (review item 11, for CH3 first):
``5V_SENS`` can sit at 5.10 V against a 5.12 V range, so a full-scale code on
any input anywhere in a window marks that input clipped. Its value is
recorded as ``clipped``, and no ratio is formed against a clipped supply.

**Volts are node volts.** Each input reaches U8 through a series resistor
and a matching one, and U8's 1 MΩ inputs leak towards 2.5 V. The board map
models both (:meth:`~src.decode.hat_adc_board.HatAdcBoard.node_volts`), so
``PORT_V1_VOLTAGE`` is the terminal's voltage, not U8's, and the resistance
solve includes U8's leakage.

**Bursts and rates.** A reader thread converts every mapped input once per
burst, in one bus message, at ``burst_hz``, stamping each burst on its own
(:class:`~src.collection.ads8688a.Burst`). The event loop drains them every
:data:`DRAIN_SECONDS` and averages them into fixed windows per stored rate,
each record stamped at its bursts' mean instant. Windows are wall-clock
buckets (``floor(stamp * hz)``), so every channel at one rate shares its
instants. The thread waits between bursts, and when late it skips the slots
it missed rather than catching up: the bus it shares carries the vehicle CAN
controller. While the active capture profile sets ``analog_full_rate`` (a
pulsation capture), each burst is its own fast record. It carries
``adc_seq``, the burst's slot number (gaps are skipped slots), as the USB ADC
path's full-rate samples do, and ``adc_clock: software``: these samples are
paced by a thread, not a hardware clock, and their jitter is measured at
bring-up (H08.10) before a spectrum of them is trusted.

**Configuration is checked, not assumed.** The driver verifies every register
when it configures the chip, checks one register at the end of every burst,
and verifies them all every :data:`VERIFY_SECONDS`. A chip that reset (and
so went back to ±10.24 V ranges) loses at most the burst that found it; it
is configured again on the spot, and only a chip that keeps resetting
(:data:`MAX_RECONFIGURES` in :data:`RECONFIGURE_WINDOW_SECONDS`) ends the run
for the reconnect loop.

**The sensor supply's enable** (``SENS_PWR_EN``) is driven high once the chip
has configured, for as long as the ports are collected, and low when the
collector stops, on every path it controls. A released line keeps its level
on the Pi 5 (:mod:`src.collection.gpio_lines`), so low first, then release.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import cast

from src.collection import tuning_profile
from src.collection.ads8688a import RANGES, Ads8688a, Ads8688aError, Burst
from src.collection.gpio_lines import GpioOutputs
from src.collection.hat_revision import SETTING_NAMES, HatRevision, resolve_hat_revision
from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import DEFAULT_RECONNECT_POLICY, ReconnectPolicy, run_with_reconnect
from src.collection.spi_dev import SPI_MODE_1, LinuxSpiBus, SpiBus
from src.collection.vehicle_power import VEHICLE_POWER
from src.config import HatAdcConfig, SupplyVoltageConfig
from src.decode.analog_sensors import AnalogReading, decode_counts, fault_channel
from src.decode.hat_adc_board import (
    CODES,
    SENSOR_SUPPLY_CHANNEL,
    SUPPLY_CHANNEL,
    HatAdcBoard,
    load_hat_adc_board,
    port_ratio_channel,
    port_resistance_channel,
    port_voltage_channel,
)
from src.telemetry import SENSOR_SOURCE, MetadataValue, TelemetryRecord

log = logging.getLogger(__name__)

#: ``_FAULT`` values.
CLIPPED = "clipped"
SUPPLY_CLIPPED = "sensor_supply_clipped"
SUPPLY_LOW = "sensor_supply_low"
OPEN = "open"
FULL_SCALE_CODE = CODES - 1
#: How often the event loop takes the reader's bursts.
DRAIN_SECONDS = 0.02
#: How often every register is read back (one is checked in every burst).
VERIFY_SECONDS = 1.0
#: A chip that loses its configuration this often is not recovering.
MAX_RECONFIGURES = 3
RECONFIGURE_WINDOW_SECONDS = 10.0
#: Bursts averaged for a one-shot supply reading.
ONE_SHOT_BURSTS = 8
#: With only the 12 V sense to record, bursts per stored reading.
SUPPLY_ONLY_BURSTS_PER_RECORD = 10


@dataclass(frozen=True)
class Plan:
    """What to record: the 12 V sense (``hardware.supply_voltage``), the
    sensor inputs (``hardware.hat_adc``), or both."""

    supply: bool
    sensors: bool

    def inputs(self, board: HatAdcBoard) -> list[int]:
        wanted: set[int] = set()
        if self.supply:
            wanted.add(board.supply.input)
        if self.sensors:
            wanted.add(board.sensor_supply.input)
            wanted |= {p.input for p in board.voltage_ports}
            wanted |= {p.input for p in board.resistive_ports}
            wanted |= {s.input for s in board.sensors}
        return sorted(wanted)

    def channels(self, board: HatAdcBoard) -> set[str]:
        """Every ``sensor:`` channel this plan records (without ``_FAULT``)."""
        out: set[str] = set()
        if self.supply:
            out.add(SUPPLY_CHANNEL)
        if self.sensors:
            out.add(SENSOR_SUPPLY_CHANNEL)
            for p in board.voltage_ports:
                out |= {port_voltage_channel(p.name), port_ratio_channel(p.name)}
            out |= {port_resistance_channel(p.name) for p in board.resistive_ports}
            out |= {s.name for s in board.sensors}
        return out


@dataclass
class _Window:
    """Bursts averaged into one stored instant."""

    hz: float
    key: int | None = None
    sums: dict[int, float] = field(default_factory=dict)
    stamps: float = 0.0
    count: int = 0
    #: Inputs that read full scale in any burst here.
    clipped: set[int] = field(default_factory=set)
    #: A full-rate window's one burst's slot number.
    seq: int | None = None

    def mean(self, adc_input: int) -> float:
        return self.sums[adc_input] / self.count

    @property
    def stamp(self) -> float:
        return self.stamps / self.count

    def clear(self) -> None:
        self.sums.clear()
        self.stamps = 0.0
        self.count = 0
        self.clipped.clear()
        self.seq = None


class Assembler:
    """Bursts in, records out (no I/O, so every rule here is testable)."""

    def __init__(
        self,
        board: HatAdcBoard,
        plan: Plan,
        *,
        supply_hz: float,
        record_hz: float,
        slow_hz: float,
        full_rate: Callable[[], bool] = lambda: False,
    ) -> None:
        self.board = board
        self.plan = plan
        #: Asked per burst: while it says so (a capture profile's
        #: ``analog_full_rate``), every burst is its own fast record.
        self.full_rate = full_rate
        self.catalog = board.sensor_catalog()
        self._lsb = board.full_scale_volts / CODES
        self._windows: dict[str, _Window] = {}
        if plan.supply:
            self._windows["supply"] = _Window(supply_hz)
        if plan.sensors:
            self._windows["fast"] = _Window(record_hz)
            self._windows["slow"] = _Window(slow_hz)

    def volts(self, code: float) -> float:
        """The differential U8 measured."""
        return code * self._lsb

    def burst(self, burst: Burst) -> list[TelemetryRecord]:
        out: list[TelemetryRecord] = []
        for kind, window in self._windows.items():
            key = math.floor(burst.stamp * window.hz)
            if window.key is not None and key != window.key:
                out += self._flush(kind, window)
            window.key = key
            for adc_input, code in burst.codes.items():
                window.sums[adc_input] = window.sums.get(adc_input, 0.0) + code
                if code >= FULL_SCALE_CODE:
                    window.clipped.add(adc_input)
            window.stamps += burst.stamp
            window.count += 1
            if kind == "fast" and self.full_rate():
                window.seq = burst.seq
                out += self._flush(kind, window)
                window.key = None
        return out

    def _flush(self, kind: str, window: _Window) -> list[TelemetryRecord]:
        if window.count == 0:
            return []
        if kind == "supply":
            records = self._supply_records(window)
        elif kind == "fast":
            records = self._fast_records(window)
        else:
            records = self._slow_records(window)
        window.clear()
        return records

    # -- records -----------------------------------------------------------

    def _record(
        self,
        window: _Window,
        channel: str,
        value: float | str,
        unit: str,
        metadata: Mapping[str, MetadataValue],
    ) -> TelemetryRecord:
        sampling: dict[str, MetadataValue] = (
            {"adc_samples": window.count}
            if window.seq is None
            else {"adc_seq": window.seq, "adc_clock": "software"}
        )
        return TelemetryRecord.from_epoch(
            window.stamp,
            source=SENSOR_SOURCE,
            channel=channel,
            value=value,
            unit=unit,
            metadata={**metadata, **sampling},
        )

    def _base(self, window: _Window, adc_input: int) -> dict[str, MetadataValue]:
        mean = window.mean(adc_input)
        return {
            "adc_input": adc_input,
            "adc_counts": round(mean, 2),
            "adc_volts": round(self.volts(mean), 5),
        }

    def _supply_records(self, window: _Window) -> list[TelemetryRecord]:
        supply = self.board.supply
        meta = {**self._base(window, supply.input), "calibrated": supply.calibrated}
        if supply.input in window.clipped:
            return [self._record(window, fault_channel(SUPPLY_CHANNEL), CLIPPED, "", meta)]
        volts = self.board.rail_volts(self.volts(window.mean(supply.input)))
        return [self._record(window, SUPPLY_CHANNEL, volts, "V", meta)]

    def _supply_state(self, window: _Window) -> tuple[float, str | None]:
        """Sensor-supply volts, and why no ratio may be formed against it."""
        sensor_supply = self.board.sensor_supply
        volts = self.board.sensor_supply_volts(self.volts(window.mean(sensor_supply.input)))
        if sensor_supply.input in window.clipped:
            return volts, SUPPLY_CLIPPED
        if volts < sensor_supply.min_volts:
            return volts, SUPPLY_LOW
        return volts, None

    def _fast_records(self, window: _Window) -> list[TelemetryRecord]:
        out: list[TelemetryRecord] = []
        for sensor in self.board.sensors:
            result = decode_counts(self.catalog.module, sensor, window.mean(sensor.input))
            meta = {
                **self._base(window, sensor.input),
                "sensor_volts": round(result.sensor_volts, 4),
            }
            if isinstance(result, AnalogReading):
                out.append(self._record(window, result.name, result.value, result.unit, meta))
            else:
                out.append(self._record(window, fault_channel(result.name), result.kind, "", meta))
        supply_volts, supply_fault = self._supply_state(window)
        for port in self.board.voltage_ports:
            meta = {**self._base(window, port.input), "sensor_supply_volts": round(supply_volts, 4)}
            volts_channel, ratio_channel = (
                port_voltage_channel(port.name),
                port_ratio_channel(port.name),
            )
            if port.input in window.clipped:
                out.append(self._record(window, fault_channel(volts_channel), CLIPPED, "", meta))
                out.append(self._record(window, fault_channel(ratio_channel), CLIPPED, "", meta))
                continue
            node = self.board.node_volts(
                self.volts(window.mean(port.input)),
                series_ohms=port.series_ohms,
                match_ohms=port.match_ohms,
            )
            out.append(self._record(window, volts_channel, node, "V", meta))
            if supply_fault is not None:
                out.append(
                    self._record(window, fault_channel(ratio_channel), supply_fault, "", meta)
                )
            else:
                out.append(self._record(window, ratio_channel, node / supply_volts, "", meta))
        return out

    def _slow_records(self, window: _Window) -> list[TelemetryRecord]:
        sensor_supply = self.board.sensor_supply
        supply_volts, supply_fault = self._supply_state(window)
        supply_meta = self._base(window, sensor_supply.input)
        out: list[TelemetryRecord] = []
        if sensor_supply.input in window.clipped:
            out.append(
                self._record(window, fault_channel(SENSOR_SUPPLY_CHANNEL), CLIPPED, "", supply_meta)
            )
        else:
            out.append(self._record(window, SENSOR_SUPPLY_CHANNEL, supply_volts, "V", supply_meta))
        for port in self.board.resistive_ports:
            channel = port_resistance_channel(port.name)
            meta: dict[str, MetadataValue] = {
                **self._base(window, port.input),
                "sensor_supply_volts": round(supply_volts, 4),
                "pull_up_ohms": port.pull_up_ohms,
            }
            if supply_fault is not None:
                out.append(self._record(window, fault_channel(channel), supply_fault, "", meta))
                continue
            if port.input in window.clipped:
                out.append(self._record(window, fault_channel(channel), OPEN, "", meta))
                continue
            node = self.board.node_volts(
                self.volts(window.mean(port.input)),
                series_ohms=port.series_ohms,
                match_ohms=port.match_ohms,
            )
            meta["node_volts"] = round(node, 5)
            ohms = self.board.resistance(port, node, supply_volts)
            if ohms > port.open_above_ohms:
                out.append(self._record(window, fault_channel(channel), OPEN, "", meta))
            else:
                out.append(self._record(window, channel, ohms, "ohm", meta))
        return out


# -- the collector ------------------------------------------------------------


class _Reconfigurer:
    """Configure a chip that lost its configuration again, on the same bus,
    until it does so too often to call a recovery."""

    def __init__(self, adc: Ads8688a, monotonic: Callable[[], float]) -> None:
        self.adc = adc
        self.monotonic = monotonic
        self.recent: deque[float] = deque(maxlen=MAX_RECONFIGURES)

    def __call__(self, why: Ads8688aError) -> None:
        now = self.monotonic()
        self.recent.append(now)
        if (
            len(self.recent) == MAX_RECONFIGURES
            and now - self.recent[0] <= RECONFIGURE_WINDOW_SECONDS
        ):
            raise Ads8688aError(
                f"lost its configuration {MAX_RECONFIGURES} times in "
                f"{RECONFIGURE_WINDOW_SECONDS:g} s; last: {why}"
            ) from why
        log.warning(
            "ADS8688A lost its configuration; configuring it again", extra={"error": str(why)}
        )
        self.adc.configure(self.adc.ranges)


async def collect_hat_adc(
    adc: Ads8688a,
    assembler: Assembler,
    inputs: Sequence[int],
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    burst_hz: float,
    monotonic: Callable[[], float] = time.monotonic,
    drop_counter: QueueDropCounter | None = None,
) -> None:
    """Burst on a thread, assemble and enqueue on the loop, until stopped.

    A chip that reset is configured again in place. Anything else that fails
    a burst (the bus, a chip that keeps resetting) ends the run with its
    exception, for the reconnect loop."""
    on_drop = QueueDropCounter("hat_adc") if drop_counter is None else drop_counter
    bursts: SimpleQueue[Burst] = SimpleQueue()
    halt = threading.Event()
    failures: list[BaseException] = []
    period = 1.0 / burst_hz
    reconfigure = _Reconfigurer(adc, monotonic)

    def read() -> None:
        try:
            due = monotonic()
            verify_due = due + VERIFY_SECONDS
            slot = 0
            while not halt.is_set():
                try:
                    bursts.put(replace(adc.read(inputs), seq=slot))
                except Ads8688aError as exc:
                    reconfigure(exc)
                now = monotonic()
                if now >= verify_due:
                    try:
                        adc.verify()
                    except Ads8688aError as exc:
                        reconfigure(exc)
                    verify_due = now + VERIFY_SECONDS
                due += period
                slot += 1
                behind = monotonic() - due
                if behind >= period:  # missed whole slots: skip them, no catch-up train
                    skipped = int(behind // period)
                    due += skipped * period
                    slot += skipped
                wait = due - monotonic()
                if wait > 0:
                    halt.wait(wait)
        except BaseException as exc:  # handed to the loop below
            failures.append(exc)

    thread = threading.Thread(target=read, name="hat-adc-reader", daemon=True)
    thread.start()
    try:
        while True:
            stopping = stop_event.is_set()
            for _ in range(bursts.qsize()):
                try:
                    burst = bursts.get_nowait()
                except Empty:
                    break
                for record in assembler.burst(burst):
                    if record.channel == SUPPLY_CHANNEL and isinstance(record.value, float):
                        VEHICLE_POWER.note(record.value)
                    put_nowait_or_drop(queue, record, on_drop=on_drop)
            on_drop.flush_drop_log()
            if stopping:
                return
            if failures:
                raise failures[0]
            await asyncio.sleep(DRAIN_SECONDS)
    finally:
        halt.set()
        await asyncio.to_thread(thread.join)


BusFactory = Callable[[str, int], SpiBus]
PowerFactory = Callable[[str, int], GpioOutputs]


def linux_bus_factory(device: str, speed_hz: int) -> SpiBus:
    return LinuxSpiBus(device, mode=SPI_MODE_1, speed_hz=speed_hz)


def linux_power_factory(chip: str, line: int) -> GpioOutputs:
    return GpioOutputs(chip, {line: True}, consumer="canary-hat-adc")


def spi_device_for(config: HatAdcConfig, revision: HatRevision) -> str:
    device = config.spi_device or revision.adc_spi_device
    if device is None:
        raise ValueError(
            f"CANary-HAT rev {revision.name} has no ADS8688A (hardware.hat_adc is rev 2.1's)"
        )
    return device


@dataclass
class _Held:
    """What one attempt holds, for teardown."""

    bus: SpiBus
    power: GpioOutputs | None = None
    power_line: int | None = None


def _release(held: _Held) -> None:
    if held.power is not None and held.power_line is not None:
        try:
            held.power.set({held.power_line: False})
        except OSError:
            log.warning("could not drive SENS_PWR_EN low before release", exc_info=True)
        held.power.close()
    try:
        held.bus.close()
    except OSError:
        pass


async def collect_hat_adc_from_config(
    config: HatAdcConfig,
    supply: SupplyVoltageConfig,
    plan: Plan,
    revision: HatRevision,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    bus_factory: BusFactory = linux_bus_factory,
    power_factory: PowerFactory = linux_power_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
) -> None:
    """Own the chip for the daemon queue-producer path.

    The board map is loaded and the SPI device resolved before the first
    attempt: either failing raises instead of retrying forever."""
    board = load_hat_adc_board(config.board)
    device = spi_device_for(config, revision)
    inputs = plan.inputs(board)
    ranges = dict.fromkeys(inputs, RANGES[board.range])
    power_line = revision.sensor_power_gpio if plan.sensors and config.sensor_power else None
    burst_hz = (
        config.burst_hz
        if plan.sensors
        else min(config.burst_hz, SUPPLY_ONLY_BURSTS_PER_RECORD * supply.rate_hz)
    )
    drop_counter = QueueDropCounter("hat_adc")

    async def _attempt(capture: Callable[[_Held], None]) -> None:
        held = _Held(bus_factory(device, config.spi_speed_hz))
        capture(held)
        adc = Ads8688a(held.bus)
        await asyncio.to_thread(adc.configure, ranges)
        # Only once the chip answers: an absent or miswired one would
        # otherwise power the sensor rail up and down on every retry.
        if power_line is not None:
            try:
                held.power = power_factory(config.gpio_chip, power_line)
                held.power_line = power_line
            except OSError:
                log.error(
                    "could not drive SENS_PWR_EN (GPIO%d on %s): the sensor ports will record "
                    "their supply as low",
                    power_line,
                    config.gpio_chip,
                    exc_info=True,
                )
        log.info(
            "hat adc collection started",
            extra={
                "spi_device": device,
                "inputs": inputs,
                "range": board.range,
                "burst_hz": burst_hz,
                "channels": sorted(plan.channels(board)),
                "sensor_power_gpio": power_line,
            },
        )
        assembler = Assembler(
            board,
            plan,
            supply_hz=supply.rate_hz,
            record_hz=config.record_hz,
            slow_hz=config.slow_record_hz,
            full_rate=tuning_profile.analog_full_rate,
        )
        await collect_hat_adc(
            adc, assembler, inputs, queue, stop_event, burst_hz=burst_hz, drop_counter=drop_counter
        )

    await run_with_reconnect(
        _attempt,
        stop_event=stop_event,
        policy=reconnect_policy,
        teardown=_release,
        log=log,
        reconnect_message="hat adc collector failed; reconnecting",
        log_extra=lambda: {"spi_device": device},
    )


def read_supply_once(
    config: HatAdcConfig,
    revision: HatRevision,
    *,
    bus_factory: BusFactory = linux_bus_factory,
) -> float | None:
    """One 12 V reading, for a caller that runs before the collector does
    (the startup VIN probe). Notes it in :data:`VEHICLE_POWER` too. ``None``
    when the read fails: the caller goes on as without it."""
    try:
        board = load_hat_adc_board(config.board)
        bus = bus_factory(spi_device_for(config, revision), config.spi_speed_hz)
        try:
            adc = Ads8688a(bus)
            channel = board.supply.input
            adc.configure({channel: RANGES[board.range]})
            codes = [adc.read([channel]).codes[channel] for _ in range(ONE_SHOT_BURSTS)]
        finally:
            bus.close()
    except (OSError, Ads8688aError, ValueError):
        log.debug("one-shot supply read failed", exc_info=True)
        return None
    volts = board.rail_volts(sum(codes) / len(codes) * board.full_scale_volts / CODES)
    VEHICLE_POWER.note(volts)
    return volts


# -- bring-up -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Print what every mapped input reads, once a second: the first look at
    a new board (``python -m src.collection.hat_adc_collector``). Stop the
    daemon first: configuring the chip resets it under the daemon's feet (the
    daemon's verification notices and reconfigures, but its data has a gap)."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/canary/canary.yaml"),
        help="daemon config for hardware.hat_adc (defaults when it does not exist)",
    )
    parser.add_argument(
        "--rev", choices=SETTING_NAMES, help="the board's revision (default: its ID EEPROM)"
    )
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--no-sensor-power", action="store_true", help="leave SENS_PWR_EN as it is")
    args = parser.parse_args(argv)
    try:
        return _bring_up(args.config, args.rev, args.seconds, sensor_power=not args.no_sensor_power)
    except (OSError, Ads8688aError, ValueError) as exc:
        print(f"hat_adc: {type(exc).__name__}: {exc}")
        return 1


def _bring_up(config_path: Path, rev: str | None, seconds: float, *, sensor_power: bool) -> int:
    from src.config import CanaryConfig, HatRevisionSetting

    config = HatAdcConfig()
    setting: HatRevisionSetting = "auto"
    if config_path.exists():
        hardware = CanaryConfig.from_yaml(config_path).hardware
        config, setting = hardware.hat_adc, hardware.hat_revision
    if rev is not None:
        setting = cast(HatRevisionSetting, rev)
    revision = resolve_hat_revision(setting)
    board = load_hat_adc_board(config.board)
    plan = Plan(supply=True, sensors=True)
    inputs = plan.inputs(board)
    held = _Held(linux_bus_factory(spi_device_for(config, revision), config.spi_speed_hz))
    try:
        line = revision.sensor_power_gpio
        if line is not None and sensor_power:
            try:
                held.power, held.power_line = linux_power_factory(config.gpio_chip, line), line
            except OSError as exc:
                print(f"SENS_PWR_EN (GPIO{line}) not driven: {exc}")
        adc = Ads8688a(held.bus)
        adc.configure(dict.fromkeys(inputs, RANGES[board.range]))
        assembler = Assembler(board, plan, supply_hz=1.0, record_hz=1.0, slow_hz=1.0)
        print(f"CANary-HAT rev {revision.name}, ADS8688A inputs {inputs}, range {board.range}")
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for record in assembler.burst(adc.read(inputs)):
                value = record.value
                shown = f"{value:.4f}" if isinstance(value, float) else value
                print(f"  {record.channel:28} {shown} {record.unit}")
            time.sleep(0.01)
        adc.verify()
    finally:
        _release(held)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CLIPPED",
    "MAX_RECONFIGURES",
    "OPEN",
    "SUPPLY_CLIPPED",
    "SUPPLY_LOW",
    "Assembler",
    "Plan",
    "collect_hat_adc",
    "collect_hat_adc_from_config",
    "read_supply_once",
    "spi_device_for",
]
