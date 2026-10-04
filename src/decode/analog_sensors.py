"""Analog sensors read through an ADC: the catalog, and counts to engineering units.

The catalog is data (``data/usb_adc_sensors.yaml``), and so is every
calibration number in it: the ADC's measured reference, the divider resistors
as measured, the sensor's published transfer function, and a trim. A new
sensor, a re-measured resistor or a corrected offset is a YAML edit, not a
code change.

Two legs, kept apart because they belong to different things
(``docs/aem-fuel-pressure-analog-input.md``):

- **Electrical** (:class:`AdcModule`, :class:`Divider`): counts at the pin to
  volts at the sensor. Specific to this ADC and this divider.
- **Sensor** (:class:`Transfer`, ``valid_volts``): sensor volts to a value, and
  the window outside which the sensor is reporting a fault rather than a
  measurement. A fact about the sensor, carried unchanged to any other ADC
  (the HAT rev 2.1's ADS8688A reads the same AEM gauge with no divider).

**The valid window is a gate.** Outside it :func:`decode_counts` returns an
:class:`AnalogFault`, never a number. Clamping instead would turn a broken
signal wire, sitting at 0 V, into a plausible 0.0 psig.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Recorded unit <- transfer unit, multiply by. A pair missing here is refused
#: at load rather than recorded in the wrong unit.
UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    ("psi", "kPa"): 6.894757293168361,
    ("bar", "kPa"): 100.0,
}

FaultKind = Literal["below_window", "above_window"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdcModule(_Strict):
    """The ADC itself: what one count is worth."""

    #: The hardware, for the record.
    device: str = Field(min_length=1)
    #: Volts at full scale. **Measured, not nominal**: on a board whose ADC
    #: reference is its 3.3 V regulator, the regulator's tolerance is the
    #: largest error in the chain. No default, so it cannot be forgotten.
    reference_volts: float = Field(gt=0.0)
    #: The count that ``reference_volts`` reads as (4095 for 12 bits).
    full_scale_counts: int = Field(gt=0)


class Divider(_Strict):
    """A resistor divider ahead of the ADC pin, as measured."""

    #: Sensor signal side to the ADC node.
    r_top_ohms: float = Field(gt=0.0)
    #: ADC node to ground.
    r_bottom_ohms: float = Field(gt=0.0)

    @property
    def sensor_volts_per_pin_volt(self) -> float:
        return (self.r_top_ohms + self.r_bottom_ohms) / self.r_bottom_ohms


class Transfer(_Strict):
    """``value = slope * sensor_volts + intercept``, in ``unit``."""

    slope: float
    intercept: float
    unit: str = Field(min_length=1)


class VoltWindow(_Strict):
    min: float = Field(ge=0.0)
    max: float = Field(gt=0.0)


class AnalogChannel(_Strict):
    #: Channel name, recorded as ``sensor:<name>``; faults as ``sensor:<name>_FAULT``.
    name: str = Field(min_length=1, pattern=r"^[A-Z0-9_]+$")
    #: ADC input number.
    input: int = Field(ge=0)
    #: The device and the document the transfer function came from.
    device: str = Field(min_length=1)
    source: str = Field(min_length=1)
    #: ``null`` when the sensor drives the pin directly.
    divider: Divider | None
    transfer: Transfer
    #: Sensor volts outside this window are a fault state, not a value.
    valid_volts: VoltWindow
    #: Unit the value is recorded in. Converted from ``transfer.unit`` through
    #: :data:`UNIT_CONVERSIONS`; the project's pressure unit is kPa.
    record_unit: str = Field(min_length=1)
    #: Calibration trim in ``transfer.unit``, added after the transfer function.
    offset: float = 0.0
    #: How the numbers above were obtained.
    notes: str = ""

    @property
    def sensor_volts_per_pin_volt(self) -> float:
        return 1.0 if self.divider is None else self.divider.sensor_volts_per_pin_volt

    @property
    def unit_factor(self) -> float:
        if self.transfer.unit == self.record_unit:
            return 1.0
        return UNIT_CONVERSIONS[(self.transfer.unit, self.record_unit)]

    @model_validator(mode="after")
    def _consistent(self) -> AnalogChannel:
        if self.valid_volts.min >= self.valid_volts.max:
            raise ValueError(f"{self.name}: valid_volts.min must be below valid_volts.max")
        if (
            self.transfer.unit != self.record_unit
            and (self.transfer.unit, self.record_unit) not in UNIT_CONVERSIONS
        ):
            raise ValueError(
                f"{self.name}: no conversion from {self.transfer.unit!r} to "
                f"{self.record_unit!r}; known: {sorted(UNIT_CONVERSIONS)}"
            )
        return self


class AnalogSensorCatalog(_Strict):
    module: AdcModule
    channels: list[AnalogChannel]

    @model_validator(mode="after")
    def _measurable(self) -> AnalogSensorCatalog:
        names = [c.name for c in self.channels]
        if len(names) != len(set(names)):
            raise ValueError("analog channel names must be unique")
        inputs = [c.input for c in self.channels]
        if len(inputs) != len(set(inputs)):
            raise ValueError("each ADC input may feed only one channel")
        for channel in self.channels:
            # The top of the window must read below full scale, or a saturated
            # pin (a short to a supply, an over-range sensor) is
            # indistinguishable from a valid maximum.
            pin_at_max = channel.valid_volts.max / channel.sensor_volts_per_pin_volt
            if pin_at_max >= self.module.reference_volts:
                raise ValueError(
                    f"{channel.name}: valid_volts.max {channel.valid_volts.max} V reaches the "
                    f"pin as {pin_at_max:.3f} V, at or above the ADC's "
                    f"{self.module.reference_volts} V full scale; the divider must bring it "
                    "under, or saturation reads as a valid value"
                )
        return self

    def channel_for_input(self, adc_input: int) -> AnalogChannel | None:
        for channel in self.channels:
            if channel.input == adc_input:
                return channel
        return None


def load_analog_sensor_catalog(path: Path) -> AnalogSensorCatalog:
    return AnalogSensorCatalog.model_validate(yaml.safe_load(path.read_text()))


@dataclass(frozen=True)
class AnalogReading:
    name: str
    value: float
    unit: str
    sensor_volts: float


@dataclass(frozen=True)
class AnalogFault:
    """The sensor's output is outside its valid window: a state, not a value."""

    name: str
    kind: FaultKind
    sensor_volts: float


def sensor_volts(module: AdcModule, channel: AnalogChannel, counts: int) -> float:
    pin_volts = counts * module.reference_volts / module.full_scale_counts
    return pin_volts * channel.sensor_volts_per_pin_volt


def decode_counts(
    module: AdcModule, channel: AnalogChannel, counts: int
) -> AnalogReading | AnalogFault:
    """ADC counts -> a value in ``channel.record_unit``, or the fault it is."""
    volts = sensor_volts(module, channel, counts)
    if volts < channel.valid_volts.min:
        return AnalogFault(channel.name, "below_window", volts)
    if volts > channel.valid_volts.max:
        return AnalogFault(channel.name, "above_window", volts)
    native = channel.transfer.slope * volts + channel.transfer.intercept + channel.offset
    return AnalogReading(channel.name, native * channel.unit_factor, channel.record_unit, volts)


def fault_channel(name: str) -> str:
    """The state channel that carries ``name``'s out-of-window readings."""
    return f"{name}_FAULT"


__all__ = [
    "UNIT_CONVERSIONS",
    "AdcModule",
    "AnalogChannel",
    "AnalogFault",
    "AnalogReading",
    "AnalogSensorCatalog",
    "Divider",
    "Transfer",
    "VoltWindow",
    "decode_counts",
    "fault_channel",
    "load_analog_sensor_catalog",
    "sensor_volts",
]
