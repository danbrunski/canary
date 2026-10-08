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
* ``AF_SENSOR_BARO: str`` -- the channel carrying the barometric pressure the
  ECM corrects that voltage with before its AFR table (B06.7). The voltage
  route cannot convert a sample without it.
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
* ``DID_VIEWS: tuple[DidView, ...]`` -- a Mode 22 DID read in an engineering
  unit its own catalog entry does not carry, as a separate ``derived:``
  channel (P02.14). The DID stays in raw counts, because a served model was
  trained on them; the view's scale and unit are one evidence record of the
  DID's ``dids.yaml`` entry, resolved by
  :func:`src.preprocessing.channel_units.did_view_scale`.
* ``DERIVED_CHANNEL_INPUTS`` / ``DERIVED_CHANNEL_OPTIONAL_INPUTS`` -- the
  registry entries the declarations above produce for this vehicle, written
  out as literals (B08.1). :mod:`src.signals.derived` refuses to import unless
  they equal the entries it builds (:func:`literal_derived_inputs`), and the
  artifact-lineage audit parses them at a build's revision without importing.
  ``SIGNAL_INPUTS`` is already literal, so it is not repeated there.

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


def literal_derived_inputs() -> dict[str, InputGroups]:
    """Every vehicle's literal ``DERIVED_CHANNEL_INPUTS``, merged like
    :func:`signal_inputs`: each group the union of the vehicles' alternatives,
    in module-name order (B08.1)."""
    declared: dict[str, list[InputGroups]] = {}
    for module in vehicle_modules():
        inputs: Mapping[str, InputGroups] = getattr(module, "DERIVED_CHANNEL_INPUTS", {})
        for output, groups in inputs.items():
            declared.setdefault(output, []).append(groups)
    return {output: _merge(output, declarations) for output, declarations in declared.items()}


def literal_derived_optional_inputs() -> dict[str, tuple[str, ...]]:
    """Every vehicle's literal ``DERIVED_CHANNEL_OPTIONAL_INPUTS``, each the
    union of the vehicles' columns in module-name order (B08.1)."""
    merged: dict[str, dict[str, None]] = {}
    for module in vehicle_modules():
        inputs: Mapping[str, tuple[str, ...]] = getattr(
            module, "DERIVED_CHANNEL_OPTIONAL_INPUTS", {}
        )
        for output, columns in inputs.items():
            merged.setdefault(output, {}).update(dict.fromkeys(columns))
    return {output: tuple(columns) for output, columns in merged.items()}


def af_sensor_voltage_columns(bank: int) -> tuple[str, ...]:
    """Every vehicle's A/F sensor voltage channel for ``bank``, in module order."""
    columns: dict[str, None] = {}
    for module in vehicle_modules():
        channels: Mapping[int, str] = getattr(module, "AF_SENSOR_VOLTAGE", {})
        if bank in channels:
            columns[channels[bank]] = None
    return tuple(columns)


def af_sensor_baro_columns() -> tuple[str, ...]:
    """Every vehicle's channel for the barometric pressure its ECM corrects the
    A/F sensor voltage with (B06.7), in module order.

    The voltage route reads it per sample (``afr_calibration.yaml`` ``baro_gain.
    baro_channel`` must be one of these), so the derived-channel registry names
    it as an input of every channel built on that route.
    """
    columns: dict[str, None] = {}
    for module in vehicle_modules():
        channel: str | None = getattr(module, "AF_SENSOR_BARO", None)
        if channel is not None:
            columns[channel] = None
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


@dataclass(frozen=True)
class DidView:
    """A DID read in an engineering unit, as its own ``derived:`` channel (P02.14).

    ``source`` (``did:<NAME>``) keeps decoding exactly as its catalog entry
    says. ``channel`` is ``source`` times the ``constant`` of the one evidence
    record of that entry whose ``method`` is ``scale_method``; the record's
    ``constant_unit`` (``"<unit> per count"``) is the view's unit. So the scale
    and the unit are stated once, in ``dids.yaml``, beside the evidence that
    established them, and this declaration carries neither.

    Why a second channel and not a scale on the DID: a served model was
    trained on the raw counts, so scaling the DID in place would change its
    feature values, which is a retrain. The view is refused while the source
    entry has its own ``scale``: the frame would then hold scaled values, and
    the view would scale them twice.
    """

    channel: str
    source: str
    scale_method: str

    def __post_init__(self) -> None:
        if not self.channel.startswith("derived:"):
            raise ValueError(f"DID view {self.channel!r} must be a derived: channel")
        if not self.source.startswith("did:"):
            raise ValueError(f"DID view {self.channel!r} reads {self.source!r}, not a did: channel")
        if not self.scale_method.strip():
            raise ValueError(
                f"DID view {self.channel!r} names no evidence method for its scale: a "
                "scale with no recorded evidence is the P02.13 defect"
            )


def did_views() -> tuple[DidView, ...]:
    """Every vehicle's declared DID views, in module order.

    Refuses two declarations of one ``derived:`` name: the registry gives a
    channel one input, so two vehicles reading different DIDs under one name
    would be computed from whichever came first.
    """
    views: dict[str, DidView] = {}
    for module in vehicle_modules():
        for view in getattr(module, "DID_VIEWS", ()):
            existing = views.setdefault(view.channel, view)
            if existing != view:
                raise ValueError(
                    f"{view.channel} is declared as two different DID views: {existing} and {view}"
                )
    return tuple(views.values())


__all__ = [
    "BANK_1_WIDEBAND_LAMBDA",
    "DidView",
    "InputGroups",
    "LoadAxisChannel",
    "af_sensor_baro_columns",
    "af_sensor_voltage_columns",
    "did_views",
    "load_axis_channels",
    "literal_derived_inputs",
    "literal_derived_optional_inputs",
    "load_axis_columns",
    "signal_inputs",
    "vehicle_modules",
    "wideband_lambda_columns",
]
