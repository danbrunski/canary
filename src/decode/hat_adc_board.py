"""The CANary-HAT rev 2.1's ADS8688A channel map: what each input is wired to.

Data, not code (``data/hat_adc_rev2_1.yaml``): the 12 V divider, the sensor
supply, the two generic sensor ports with their fitted resistors, the analog
sensors decoded through :mod:`src.decode.analog_sensors`, and the inputs the
board ties to ground. Design record: ``docs/CANary-HAT-Rev2.1.md`` §13.5.3
(front end), §13.5.11 (ports), §13.5.13 (CH5-CH7 grounded).

**Every input is accounted for.** The map must name each of the eight inputs
exactly once, as a role or as tied to ground, so an input nobody mapped cannot
be read by accident (open item 33): the driver refuses any channel not mapped
here. Every recorded channel name is unique too, across all roles.

**Ports are hardware, not sensors** (§13.5.11). ``V1`` and ``R1`` are named
after the terminal, never after what the dev truck plugs into them. Which
sensor is on a port, and its transfer function, is per-vehicle data that has
not been pinned yet. Until it is, a port is recorded as what the board can
measure: volts, a ratio against the sensor supply, a resistance.

**The ADC loads every input** (SBAS680, electrical characteristics: 1 MΩ
±15 %, leaking ``(VIN - 2.50 V) / RIN`` on the unipolar ranges). Each channel
reaches U8 through a series resistor on ``AIN_nP`` and a matching one on
``AIN_nGND``, and :meth:`HatAdcBoard.node_volts` turns the differential U8
measures back into the voltage at the board node. The resistive port solves
for the sensor with that leakage in the node's current balance, since across a
cold NTC's tens of kilohms it is a few per cent.

**A resistive reading needs the pull-up's real value**, so the map carries the
board's population. A board reworked for a different sensor is an edit here,
until the population record moves into the HAT's ID EEPROM (§13.5.8).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.decode.analog_sensors import AdcModule, AnalogChannel, AnalogSensorCatalog

INPUTS = 8
#: An ADS8688A code is 16 bits, straight binary: ``volts = code * full_scale / CODES``.
CODES = 65536

#: The unipolar ranges (SBAS680 Table 15, internal 4.096 V reference): every
#: board signal is ground-referenced and positive. Their names are the
#: driver's (:data:`src.collection.ads8688a.RANGES`); a test holds them equal.
UNIPOLAR_RANGES: dict[str, float] = {
    "0-10.24": 10.24,
    "0-5.12": 5.12,
    "0-2.56": 2.56,
    "0-1.28": 1.28,
}
RangeName = Literal["0-10.24", "0-5.12", "0-2.56", "0-1.28"]

#: ``sensor:`` channel names. ``SUPPLY_VOLTAGE`` is rev 2.0's 12 V channel too
#: (``supply_voltage_collector.CHANNEL``; a test holds them equal).
SUPPLY_CHANNEL = "SUPPLY_VOLTAGE"
SENSOR_SUPPLY_CHANNEL = "SENSOR_SUPPLY_VOLTAGE"


def port_voltage_channel(port: str) -> str:
    return f"PORT_{port}_VOLTAGE"


def port_ratio_channel(port: str) -> str:
    return f"PORT_{port}_RATIO"


def port_resistance_channel(port: str) -> str:
    return f"PORT_{port}_RESISTANCE"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupplySense(_Strict):
    """The protected ``+12V`` rail through a divider: ``sensor:SUPPLY_VOLTAGE``."""

    input: int = Field(ge=0, lt=INPUTS)
    #: Rail side to the ADC node, and node to ground.
    r_top_ohms: float = Field(gt=0.0)
    r_bottom_ohms: float = Field(gt=0.0)
    #: On ``AIN_nGND``, sized to the divider's Thevenin resistance.
    match_ohms: float = Field(ge=0.0)
    #: Rail volts measured over rail volts read, from a meter at bring-up.
    #: Required, and ``null`` until measured: an unmeasured sense still reads
    #: well enough to tell parked from bench, but a non-volatile write's
    #: battery floor will not trust it
    #: (:func:`src.control.nonvolatile_write.supply_voltage_sources`).
    gain_trim: float | None = Field(gt=0.5, lt=1.5)
    notes: str = ""

    @property
    def calibrated(self) -> bool:
        return self.gain_trim is not None

    @property
    def divider(self) -> float:
        return (self.r_top_ohms + self.r_bottom_ohms) / self.r_bottom_ohms

    @property
    def thevenin_ohms(self) -> float:
        return self.r_top_ohms * self.r_bottom_ohms / (self.r_top_ohms + self.r_bottom_ohms)


class SensorSupply(_Strict):
    """The 5 V rail the sensor ports are pulled to and powered from (U9)."""

    input: int = Field(ge=0, lt=INPUTS)
    series_ohms: float = Field(ge=0.0)
    match_ohms: float = Field(ge=0.0)
    #: Below this the regulator is off or in dropout, and a ratio against it
    #: describes the regulator, not the sensor.
    min_volts: float = Field(gt=0.0)
    notes: str = ""


class VoltagePort(_Strict):
    """A port for a driven voltage output."""

    name: str = Field(pattern=r"^[A-Z][A-Z0-9]*$")
    input: int = Field(ge=0, lt=INPUTS)
    series_ohms: float = Field(ge=0.0)
    match_ohms: float = Field(ge=0.0)
    #: Fitted pull resistors, ``null`` where the pad is empty. For the record:
    #: the port reports the voltage its sensor drives onto them.
    pull_down_ohms: float | None = Field(gt=0.0)
    pull_up_ohms: float | None = Field(gt=0.0)
    notes: str = ""


class ResistivePort(_Strict):
    """A port for a two-wire resistive sensor, pulled up to the sensor supply."""

    name: str = Field(pattern=r"^[A-Z][A-Z0-9]*$")
    input: int = Field(ge=0, lt=INPUTS)
    series_ohms: float = Field(ge=0.0)
    match_ohms: float = Field(ge=0.0)
    pull_up_ohms: float = Field(gt=0.0)
    #: Above this is an open circuit or an unplugged sensor, not a reading.
    open_above_ohms: float = Field(gt=0.0)
    notes: str = ""


class HatAdcBoard(_Strict):
    device: str = Field(min_length=1)
    #: Every mapped input's range.
    range: RangeName
    #: U8's input resistance, and the voltage its inputs leak towards on the
    #: unipolar ranges (SBAS680: ``(VIN - 2.50 V) / RIN``).
    adc_input_ohms: float = Field(gt=0.0)
    adc_input_bias_volts: float
    supply: SupplySense
    sensor_supply: SensorSupply
    voltage_ports: list[VoltagePort]
    resistive_ports: list[ResistivePort]
    #: Decoded exactly as the USB ADC path decodes them.
    sensors: list[AnalogChannel]
    #: Inputs the board grounds. Never read.
    tied_to_ground: list[int]

    @property
    def full_scale_volts(self) -> float:
        return UNIPOLAR_RANGES[self.range]

    @property
    def module(self) -> AdcModule:
        """What one count is worth, for :func:`src.decode.analog_sensors.decode_counts`."""
        return AdcModule(
            device=self.device, reference_volts=self.full_scale_volts, full_scale_counts=CODES
        )

    def sensor_catalog(self) -> AnalogSensorCatalog:
        return AnalogSensorCatalog(module=self.module, channels=self.sensors)

    # -- the analog front end ------------------------------------------------

    def _loading(self, ohms: float) -> float:
        """The fraction of the way an input sits from its source to the bias."""
        return ohms / (ohms + self.adc_input_ohms)

    def node_volts(self, differential: float, *, series_ohms: float, match_ohms: float) -> float:
        """The board node's voltage, from the differential U8 measured.

        ``AIN_nP`` sits ``k_s`` of the way from the node to the bias through
        the series resistor, and ``AIN_nGND`` sits ``k_m`` of the way from
        ground to it through the match resistor, so
        ``differential = node * (1 - k_s) + bias * (k_s - k_m)``."""
        k_s, k_m = self._loading(series_ohms), self._loading(match_ohms)
        return (differential - self.adc_input_bias_volts * (k_s - k_m)) / (1.0 - k_s)

    def rail_volts(self, differential: float) -> float:
        """The 12 V rail, from CH0's differential."""
        supply = self.supply
        open_circuit = self.node_volts(
            differential, series_ohms=supply.thevenin_ohms, match_ohms=supply.match_ohms
        )
        trim = 1.0 if supply.gain_trim is None else supply.gain_trim
        return open_circuit * supply.divider * trim

    def sensor_supply_volts(self, differential: float) -> float:
        s = self.sensor_supply
        return self.node_volts(differential, series_ohms=s.series_ohms, match_ohms=s.match_ohms)

    def resistance(self, port: ResistivePort, node: float, supply: float) -> float:
        """The sensor's resistance from its node and supply voltages; ``inf``
        when nothing in the node's current balance is left for it (open).

        The pull-up's current feeds the sensor and U8's input, which leaks to
        its bias through the series resistor."""
        to_adc = (node - self.adc_input_bias_volts) / (port.series_ohms + self.adc_input_ohms)
        to_sensor = (supply - node) / port.pull_up_ohms - to_adc
        return node / to_sensor if to_sensor > 0.0 else math.inf

    # -- validation ------------------------------------------------------------

    def roles(self) -> dict[str, int]:
        """Every mapped role -> its input."""
        out = {"supply": self.supply.input, "sensor_supply": self.sensor_supply.input}
        out |= {f"port {p.name}": p.input for p in self.voltage_ports}
        out |= {f"port {p.name}": p.input for p in self.resistive_ports}
        out |= {f"sensor {s.name}": s.input for s in self.sensors}
        return out

    def channel_names(self) -> list[str]:
        """Every channel the map can record (without ``_FAULT``), duplicates kept."""
        names = [SUPPLY_CHANNEL, SENSOR_SUPPLY_CHANNEL]
        for p in self.voltage_ports:
            names += [port_voltage_channel(p.name), port_ratio_channel(p.name)]
        names += [port_resistance_channel(p.name) for p in self.resistive_ports]
        names += [s.name for s in self.sensors]
        return names

    @model_validator(mode="after")
    def _every_input_and_name_once(self) -> HatAdcBoard:
        seen: dict[int, str] = {}
        claims = [*self.roles().items(), *(("tied to ground", i) for i in self.tied_to_ground)]
        for role, adc_input in claims:
            if not 0 <= adc_input < INPUTS:
                raise ValueError(f"{role}: input {adc_input} outside 0..{INPUTS - 1}")
            if adc_input in seen:
                raise ValueError(f"input {adc_input} is both {seen[adc_input]} and {role}")
            seen[adc_input] = role
        missing = sorted(set(range(INPUTS)) - set(seen))
        if missing:
            raise ValueError(
                f"inputs {missing} are not mapped: name each as a role or under tied_to_ground"
            )
        names = self.channel_names()
        doubled = sorted({n for n in names if names.count(n) > 1})
        if doubled:
            raise ValueError(f"channel names {doubled} would be recorded by two roles")
        # The sensors' own checks (window under full scale, units).
        self.sensor_catalog()
        return self


def load_hat_adc_board(path: Path) -> HatAdcBoard:
    return HatAdcBoard.model_validate(yaml.safe_load(path.read_text()))


__all__ = [
    "CODES",
    "INPUTS",
    "SENSOR_SUPPLY_CHANNEL",
    "SUPPLY_CHANNEL",
    "UNIPOLAR_RANGES",
    "HatAdcBoard",
    "ResistivePort",
    "SensorSupply",
    "SupplySense",
    "VoltagePort",
    "load_hat_adc_board",
    "port_ratio_channel",
    "port_resistance_channel",
    "port_voltage_channel",
]
