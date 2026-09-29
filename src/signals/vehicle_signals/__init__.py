"""Which vehicle channels feed the shared derived signals (R05.4).

A few ``derived:`` anomaly signals have a shared *formula* but
vehicle-specific *inputs*: the bank-to-bank A/F sensor voltage balance reads
whatever identifiers a vehicle's ECM exposes for those sensors. The formulas
stay in :mod:`src.signals.derived`; each vehicle declares its channels in a
module here named for its profile (:func:`src.vehicle.profiles.profile_module_stem`),
the same name its recipes use under :mod:`src.analysis.recipes`.

A module defines either or both of:

* ``SIGNAL_INPUTS: Mapping[str, tuple[tuple[str, ...], ...]]`` -- for an
  output in :data:`src.signals.derived.VEHICLE_INPUT_SIGNALS`, its ordered
  input groups (one group = "any one of these").
* ``AF_SENSOR_VOLTAGE: Mapping[int, str]`` -- the wideband A/F sensor voltage
  channel per bank, the measured-AFR fallback when the lambda PID is absent
  (:func:`src.signals.afr.append_afr_channels`).
* ``WIDEBAND_LAMBDA: Mapping[int, str]`` -- the upstream wide-range O2 lambda
  PID channel per bank. Which J1979 sensor number is bank 2 depends on the
  ECM's O2 sensor layout (PID 0x13: sensor 5; PID 0x1D: sensor 3), so only
  the vehicle can say. Sensor 1 is bank 1 under both layouts, so bank 1 needs
  no declaration (:func:`wideband_lambda_columns`).
* ``LOAD_AXIS_CHANNELS: tuple[LoadAxisChannel, ...]`` -- the channel verified
  to report a ROM table's load axis (P02.13); its unit is read from its own
  catalog entry and must be the axis's. The target
  AFR lookup reads its load input from here and nowhere else
  (:func:`src.signals.afr.target_load_axis`), so a vehicle with no verified
  channel for an axis gets no ``derived:TARGET_AFR``, rather than one looked
  up with whatever load-like channel happens to be in the frame.

The derived-channel registry is module-level (trainability and capture-profile
validation read it without a config in hand), so it is built from **every**
vehicle module, merged: each input group becomes the union of the vehicles'
alternatives, in module-name order. A signal is computed only where its
inputs are present, and a vehicle's identifiers appear only in its own
sessions, so each vehicle's frames resolve to its own channels. With one
vehicle module the registry and every output column are exactly what they
were when the names sat in shared code.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from types import ModuleType

InputGroups = tuple[tuple[str, ...], ...]


@cache
def vehicle_modules() -> tuple[ModuleType, ...]:
    """Every vehicle's signal module, in module-name order."""
    names = sorted(info.name for info in pkgutil.iter_modules(__path__))
    return tuple(importlib.import_module(f"{__name__}.{name}") for name in names)


def _merge(output: str, declarations: list[InputGroups]) -> InputGroups:
    arity = {len(groups) for groups in declarations}
    if len(arity) != 1:
        raise ValueError(f"vehicle modules declare {output} with different input arity: {arity}")
    return tuple(
        tuple(dict.fromkeys(column for groups in declarations for column in groups[index]))
        for index in range(arity.pop())
    )


def signal_inputs(known_outputs: tuple[str, ...]) -> dict[str, InputGroups]:
    """``{output: merged input groups}`` for the outputs some vehicle declares.

    Refuses an output that is not one of ``known_outputs``: a vehicle can
    supply inputs to a shared formula, not invent a signal nobody computes.
    """
    declared: dict[str, list[InputGroups]] = {}
    for module in vehicle_modules():
        inputs: Mapping[str, InputGroups] = getattr(module, "SIGNAL_INPUTS", {})
        unknown = set(inputs) - set(known_outputs)
        if unknown:
            raise ValueError(
                f"{module.__name__} declares inputs for {sorted(unknown)}, which no "
                f"shared formula computes (known: {list(known_outputs)})"
            )
        for output, groups in inputs.items():
            declared.setdefault(output, []).append(groups)
    return {
        output: _merge(output, declared[output]) for output in known_outputs if output in declared
    }


def af_sensor_voltage_columns(bank: int) -> tuple[str, ...]:
    """Every vehicle's A/F sensor voltage channel for ``bank``, in module order."""
    columns: dict[str, None] = {}
    for module in vehicle_modules():
        channels: Mapping[int, str] = getattr(module, "AF_SENSOR_VOLTAGE", {})
        if bank in channels:
            columns[channels[bank]] = None
    return tuple(columns)


#: The one wide-range lambda PID whose bank does not depend on the layout:
#: J1979 sensor 1 is bank 1 sensor 1 under both PID 0x13 and PID 0x1D.
BANK_1_WIDEBAND_LAMBDA = "obd:O2_WR_LAMBDA_S1"


def wideband_lambda_columns(bank: int) -> tuple[str, ...]:
    """Every vehicle's upstream wide-range lambda channel for ``bank``.

    Vehicle declarations first, in module order; bank 1 always ends with
    ``obd:O2_WR_LAMBDA_S1``. There is no generic bank-2 channel. Sensor 2
    (``O2_WR_LAMBDA_S2``, PID 0x25) is bank 1 *sensor 2* under both layouts,
    and it had been read as bank 2 (D03.34, 2026-09-26).
    """
    columns: dict[str, None] = {}
    for module in vehicle_modules():
        channels: Mapping[int, str] = getattr(module, "WIDEBAND_LAMBDA", {})
        if bank in channels:
            columns[channels[bank]] = None
    if bank == 1:
        columns.setdefault(BANK_1_WIDEBAND_LAMBDA, None)
    return tuple(columns)


@dataclass(frozen=True)
class LoadAxisChannel:
    """A channel verified to report a ROM table's load axis (P02.13).

    ``axis`` is the table's axis name (``TargetAfrMap.load_axis_name``).
    There is deliberately no unit field. The lookup takes the channel's unit
    from its own catalog entry (``dids.yaml``, the Mode 01 registry) and refuses
    the declaration unless that is the map's ``load_axis_unit``. A declaration
    therefore cannot relabel calculated load (%) as milliseconds. ``evidence``
    is where the verification is recorded: a same-instant
    ``compare_channels`` against an instrument that reads the axis directly,
    never an aggregate match. The load axis is where a wrong channel does the
    most harm. ``derived:TARGET_AFR`` was looked up with calculated load (%)
    on a map whose axis is base fuel schedule (ms) from B05 until P02.13, and
    every value was dimensionally wrong.
    """

    axis: str
    channel: str
    evidence: str

    def __post_init__(self) -> None:
        if ":" not in self.channel:
            raise ValueError(f"load-axis channel {self.channel!r} names no source namespace")
        if not self.axis.strip():
            raise ValueError(f"load-axis channel {self.channel!r} has an empty axis")
        if not self.evidence.strip():
            raise ValueError(
                f"load-axis channel {self.channel!r} names no verification evidence: an "
                "unverified channel on a table axis is the P02.13 defect"
            )


def load_axis_channels(axis: str) -> tuple[LoadAxisChannel, ...]:
    """Every vehicle's declared channel for the table load axis ``axis``, in
    module order. Empty when no vehicle has verified one."""
    declared: list[LoadAxisChannel] = []
    for module in vehicle_modules():
        channels: tuple[LoadAxisChannel, ...] = getattr(module, "LOAD_AXIS_CHANNELS", ())
        declared.extend(channel for channel in channels if channel.axis == axis)
    return tuple(declared)


def load_axis_columns(axis: str) -> tuple[str, ...]:
    """The wide-frame columns of :func:`load_axis_channels`, deduplicated."""
    return tuple(dict.fromkeys(channel.channel for channel in load_axis_channels(axis)))


__all__ = [
    "BANK_1_WIDEBAND_LAMBDA",
    "InputGroups",
    "LoadAxisChannel",
    "af_sensor_voltage_columns",
    "load_axis_channels",
    "load_axis_columns",
    "signal_inputs",
    "vehicle_modules",
    "wideband_lambda_columns",
]
