"""Which vehicle channels feed the shared derived signals (R05.4).

A few ``derived:`` anomaly signals have a shared *formula* but
vehicle-specific *inputs*: the bank-to-bank A/F sensor voltage balance reads
whatever identifiers a vehicle's ECM exposes for those sensors. The formulas
stay in :mod:`src.analysis.derived`; each vehicle declares its channels in a
module here named for its profile (:func:`src.profiles.profile_module_stem`),
the same name its recipes use under :mod:`src.analysis.recipes`.

A module defines either or both of:

* ``SIGNAL_INPUTS: Mapping[str, tuple[tuple[str, ...], ...]]`` -- for an
  output in :data:`src.analysis.derived.VEHICLE_INPUT_SIGNALS`, its ordered
  input groups (one group = "any one of these").
* ``AF_SENSOR_VOLTAGE: Mapping[int, str]`` -- the wideband A/F sensor voltage
  channel per bank, the measured-AFR fallback when the lambda PID is absent
  (:func:`src.analysis.afr.append_afr_channels`).

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


__all__ = ["InputGroups", "af_sensor_voltage_columns", "signal_inputs", "vehicle_modules"]
