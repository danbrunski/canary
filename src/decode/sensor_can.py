"""Frame layouts for add-on CAN sensors, and decoding them.

The catalog is data (``data/sensor_can_signals.yaml``): one entry per signal,
naming the frame, the bytes, and the scale. A new gauge is a YAML entry, not a
code change. Every field a decode depends on is required, so a layout copied
half-remembered from a manual fails to load instead of decoding plausibly —
endianness and scale are exactly the errors that produce believable numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.decode.slices import in_range, slice_raw


class SensorCanSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Channel name, recorded as ``sensor:<name>``.
    name: str = Field(min_length=1, pattern=r"^[A-Z0-9_]+$")
    #: The device and the document the layout came from.
    device: str = Field(min_length=1)
    source: str = Field(min_length=1)
    arbitration_id: int = Field(ge=0, le=0x1FFFFFFF)
    extended: bool
    byte_start: int = Field(ge=0, le=7)
    byte_length: int = Field(ge=1, le=4)
    endianness: Literal["big", "little"]
    signed: bool
    scale: float
    offset: float = 0.0
    unit: str = Field(min_length=1)
    #: Whether the layout has been checked against the device itself (a
    #: ``candump`` beside the gauge's own display). A layout read off a
    #: manual is ``false`` until then; the collector says so at start-up.
    verified: bool
    #: Physical range the sensor can report; a decode outside it is a sensor
    #: error or a wrong layout, and is dropped rather than recorded.
    valid_min: float
    valid_max: float

    @model_validator(mode="after")
    def _fits(self) -> SensorCanSignal:
        if self.byte_start + self.byte_length > 8:
            raise ValueError(f"{self.name}: bytes {self.byte_start}+{self.byte_length} exceed 8")
        if not self.extended and self.arbitration_id > 0x7FF:
            raise ValueError(f"{self.name}: 11-bit id {self.arbitration_id:#x} out of range")
        if self.valid_min >= self.valid_max:
            raise ValueError(f"{self.name}: valid_min must be below valid_max")
        return self


class SensorCanCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signals: list[SensorCanSignal]

    @model_validator(mode="after")
    def _unique(self) -> SensorCanCatalog:
        names = [s.name for s in self.signals]
        if len(names) != len(set(names)):
            raise ValueError("sensor signal names must be unique")
        return self


def load_sensor_can_catalog(path: Path) -> SensorCanCatalog:
    return SensorCanCatalog.model_validate(yaml.safe_load(path.read_text()))


@dataclass(frozen=True)
class SensorReading:
    name: str
    value: float
    unit: str


def decode_frame(
    catalog: SensorCanCatalog, arbitration_id: int, extended: bool, data: bytes
) -> list[SensorReading]:
    """Every catalogued signal this frame carries, in engineering units.

    A frame too short for a signal's bytes, or a value outside the signal's
    valid range, yields nothing for that signal — never a clamped number.
    """
    readings: list[SensorReading] = []
    for signal in catalog.signals:
        if signal.arbitration_id != arbitration_id or signal.extended != extended:
            continue
        raw = slice_raw(
            data, signal.byte_start, signal.byte_length, signal.endianness, signed=signal.signed
        )
        if raw is None:
            continue
        value = raw * signal.scale + signal.offset
        if in_range(value, signal.valid_min, signal.valid_max):
            readings.append(SensorReading(signal.name, value, signal.unit))
    return readings


__all__ = [
    "SensorCanCatalog",
    "SensorCanSignal",
    "SensorReading",
    "decode_frame",
    "load_sensor_can_catalog",
]
