"""Measured/target AFR reconstruction for tuning capture sessions."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.config import VehicleParameters
from src.signals.derived import _append_dependency_gap, _selected_input, fuel_properties
from src.signals.vehicle_signals import (
    LoadAxisChannel,
    af_sensor_voltage_columns,
    load_axis_channels,
    wideband_lambda_columns,
)

log = logging.getLogger(__name__)

#: The target-map lookup's output and the per-bank errors built on it.
TARGET_AFR = "derived:TARGET_AFR"
AFR_ERROR_CHANNELS: tuple[str, ...] = ("derived:AFR_ERROR_B1", "derived:AFR_ERROR_B2")
#: Every channel whose value depends on the target map's load axis (P02.13).
TARGET_AFR_CHANNELS: tuple[str, ...] = (TARGET_AFR, *AFR_ERROR_CHANNELS)
#: Stable prefix of every load-axis refusal: in ``FeaturePolicy`` exclusions,
#: manifests and the lookup's own error.
LOAD_AXIS_UNVERIFIED = "load_axis_unverified"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VoltageAfrPoint(_Strict):
    voltage: float
    afr: float = Field(gt=0)


class VoltageAfrTransfer(_Strict):
    points: list[VoltageAfrPoint] = Field(min_length=2)
    censored_floor_afr: float = Field(default=10.0, gt=0)
    #: Decel fuel cut stops injection entirely, so the sensor reports nearly
    #: pure air. Those samples are not a lean *mixture* and must not reach the
    #: error surface — a cell holding sustained overrun would otherwise read as
    #: a large lean error and invite exactly the wrong correction.
    censored_ceiling_afr: float = Field(default=18.0, gt=0)

    @model_validator(mode="after")
    def _floor_below_ceiling(self) -> VoltageAfrTransfer:
        if self.censored_floor_afr >= self.censored_ceiling_afr:
            raise ValueError("censored_floor_afr must be below censored_ceiling_afr")
        return self

    @model_validator(mode="after")
    def _ordered(self) -> VoltageAfrTransfer:
        volts = [point.voltage for point in self.points]
        if any(left >= right for left, right in zip(volts, volts[1:], strict=False)):
            raise ValueError("voltage transfer points must be strictly increasing")
        return self


class TargetMapProvenance(_Strict):
    method: Literal["uprev_rom_editor_transcription", "verified_rom_decode"]
    rom_file: str
    rom_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rom_modified_at: datetime
    recorded_on: date
    map_slot: int = Field(default=1, ge=1)
    cell_decode: str
    guide_reference: str
    notes: str = ""


#: The load axis every target map declares today, and its unit. The lookup
#: resolves its load input from the vehicle's verified channel for this axis
#: (``src.signals.vehicle_signals.LOAD_AXIS_CHANNELS``), never from a column
#: name in shared code.
TargetLoadAxis = Literal["base_fuel_schedule"]
TargetLoadAxisUnit = Literal["ms"]
DEFAULT_TARGET_LOAD_AXIS: TargetLoadAxis = "base_fuel_schedule"
DEFAULT_TARGET_LOAD_AXIS_UNIT: TargetLoadAxisUnit = "ms"


class AxisEdgeClamp(_Strict):
    """Evidence that the ECM reads a point beyond one edge of the map's axis as
    that edge's row or column (P02.15).

    A declaration cannot exist without its evidence: every field but ``notes``
    is required. The check that earns one is the ECM's own target, logged
    beside the load and rpm (UpRev's TARGET AFR), matched against the edge on
    rows past it, and the same rows *failing* the obvious alternatives (linear
    extrapolation from the two edge rows or columns, the next one in), so that
    a constant matching a constant is not the evidence.
    """

    behaviour: Literal["clamp"]
    method: Literal["uprev_target_afr_log_match"]
    #: The logs (session directories) the rows came from.
    logs: list[str] = Field(min_length=1)
    #: The revision whose map the logged target was matched against.
    revision: str = Field(min_length=1)
    #: The rpm band of the rows counted.
    rpm_min: float
    rpm_max: float
    rows: int = Field(ge=1)
    rows_within_tolerance: int = Field(ge=0)
    tolerance_afr: float = Field(gt=0)
    max_error_afr: float = Field(ge=0)
    median_error_afr: float = Field(ge=0)
    #: How the alternatives scored on the same rows.
    alternatives: str = Field(min_length=1)
    verified_on: date
    notes: str = ""

    @model_validator(mode="after")
    def _counts(self) -> AxisEdgeClamp:
        if self.rows_within_tolerance > self.rows:
            raise ValueError("rows_within_tolerance cannot exceed rows")
        if self.rpm_min > self.rpm_max:
            raise ValueError("rpm_min must not exceed rpm_max")
        if self.median_error_afr > self.max_error_afr:
            raise ValueError("median_error_afr cannot exceed max_error_afr")
        return self


class AxisOutOfRange(_Strict):
    """What the ECM does with a point beyond each edge of one of the map's axes.

    An edge with no evidence record is ``None``, and rows past it get no
    target (NaN), which is also what a map without the block does.
    """

    below_first: AxisEdgeClamp | None = None
    above_last: AxisEdgeClamp | None = None


class TargetAfrMap(_Strict):
    revision: str
    load_axis_name: TargetLoadAxis = DEFAULT_TARGET_LOAD_AXIS
    load_axis_unit: TargetLoadAxisUnit = DEFAULT_TARGET_LOAD_AXIS_UNIT
    rpm_breakpoints: list[float] = Field(min_length=2)
    load_breakpoints: list[float] = Field(min_length=2)
    afr: list[list[float]]
    raw_rpm_breakpoints: list[int] | None = None
    raw_load_breakpoints: list[int] | None = None
    raw_values: list[list[int]] | None = None
    provenance: TargetMapProvenance | None = None
    #: P02.15. ``None``: a point off that axis has no target, at either edge.
    load_axis_out_of_range: AxisOutOfRange | None = None
    rpm_axis_out_of_range: AxisOutOfRange | None = None

    @property
    def clamps_below_first_load(self) -> bool:
        edges = self.load_axis_out_of_range
        return edges is not None and edges.below_first is not None

    @property
    def clamps_above_last_load(self) -> bool:
        edges = self.load_axis_out_of_range
        return edges is not None and edges.above_last is not None

    @property
    def clamps_below_first_rpm(self) -> bool:
        edges = self.rpm_axis_out_of_range
        return edges is not None and edges.below_first is not None

    @property
    def clamps_above_last_rpm(self) -> bool:
        edges = self.rpm_axis_out_of_range
        return edges is not None and edges.above_last is not None

    @model_validator(mode="after")
    def _rectangular(self) -> TargetAfrMap:
        if len(self.afr) != len(self.rpm_breakpoints) or any(
            len(row) != len(self.load_breakpoints) for row in self.afr
        ):
            raise ValueError("target AFR grid must be rpm_breakpoints x load_breakpoints")
        for values, name in (
            (self.rpm_breakpoints, "rpm"),
            (self.load_breakpoints, "load"),
        ):
            if any(left >= right for left, right in zip(values, values[1:], strict=False)):
                raise ValueError(f"{name} breakpoints must be strictly increasing")
        if self.raw_rpm_breakpoints is not None and len(self.raw_rpm_breakpoints) != len(
            self.rpm_breakpoints
        ):
            raise ValueError("raw rpm breakpoints must match decoded rpm breakpoints")
        if self.raw_load_breakpoints is not None and len(self.raw_load_breakpoints) != len(
            self.load_breakpoints
        ):
            raise ValueError("raw load breakpoints must match decoded load breakpoints")
        if self.raw_values is not None and (
            len(self.raw_values) != len(self.rpm_breakpoints)
            or any(len(row) != len(self.load_breakpoints) for row in self.raw_values)
        ):
            raise ValueError("raw target grid must be rpm_breakpoints x load_breakpoints")
        return self


#: How this ECM reports Mode-01 PID 0x44 (`COMMANDED_EQUIV_RATIO`).
#:
#: J1979 calls the field "equivalence ratio" without settling which one, and
#: φ (fuel/air) and λ (air/fuel) are reciprocals that **both read 1.0 at
#: stoich** — so the convention is invisible in closed loop and only reveals
#: itself under enrichment. Measured on the Xterra 2026-08-05: at WOT the
#: value reads 1.109-1.242, which as λ would be a commanded 16.3-18.3 AFR (no
#: ECU commands that under power, and it produced a -27 % phantom delivery
#: error). As φ it gives 11.83-13.25, matching the ROM fuel-target curve to
#: -0.4 %. See commit 78592f4 and `decode_commanded_lambda`.
#:
#: ``unverified`` is the honest default for a vehicle nobody has checked at
#: WOT: the derived commanded-AFR channel refuses rather than guessing.
EquivRatioConvention = Literal["phi", "lambda", "unverified"]


#: B05.8 — bank 1 minus bank 2 measured λ from the vehicle's A/F sensor
#: voltages, both through the profile's one verified voltage transfer (held-out
#: MAE 0.029 AFR per bank, the two banks agreeing: ``afr_calibration.yaml``).
#: A voltage difference is not a mixture difference -- the transfer is
#: non-linear (about 2 AFR/V rich of stoich, 3.9 AFR/V lean) -- so the
#: balance is taken in λ, after each bank's own conversion.
LAMBDA_BANK_BALANCE_AF_VOLTAGE = "derived:LAMBDA_BANK_BALANCE_AF_VOLTAGE"
#: The same balance from the vehicle's two wide-range lambda PIDs. Produced
#: only once ``AfrCalibration.wideband_bank_balance`` records that the bank-2
#: PID agrees with the A/F voltage path (D03.34): an unverified PID pairing
#: never becomes a channel a model could fit on. A second name, not a new
#: source behind the old one, so a model fitted on one instrument pair is
#: never served the other.
LAMBDA_BANK_BALANCE_WIDEBAND = "derived:LAMBDA_BANK_BALANCE_WIDEBAND"


class WidebandPairVerification(_Strict):
    """Known-answer evidence that the wide-range lambda PIDs may be paired (D03.34).

    The bank-2 PID (0x28 on the Xterra) is new and its scale is only known
    to be λ by the J1979 definition. Before its balance against bank 1's PID
    is a channel, a session holding both it and the A/F voltage DID must show
    they agree the way bank 1's PID and DID do: ``compare_channels`` on
    same-instant pairs through the voltage transfer, with the per-sample
    ratio read, not only the fit.
    """

    session: str = Field(min_length=1)
    #: e.g. ``compare_channels obd:O2_WR_LAMBDA_S5 -> <vehicle bank-2 A/F voltage DID>``.
    comparison: str = Field(min_length=1)
    n_pairs: int = Field(ge=30)
    ratio_median: float = Field(gt=0)
    #: Spread of the per-sample ratio (fraction, e.g. 0.005 for 0.5 %).
    ratio_spread: float = Field(ge=0)
    verified_on: date
    basis: str = Field(min_length=1)


class AfrCalibration(_Strict):
    schema_version: int = 1
    provenance: dict[str, str] = Field(default_factory=dict)
    active_revision: str | None = None
    #: Per-vehicle, because it is an ECM behaviour rather than a standard.
    commanded_equiv_ratio_convention: EquivRatioConvention = "unverified"
    voltage_transfer: VoltageAfrTransfer | None = None
    target_maps: list[TargetAfrMap] = Field(default_factory=list)
    #: B05.8 — ``None`` until the bank-2 wide-range PID is verified; until
    #: then ``LAMBDA_BANK_BALANCE_WIDEBAND`` is never produced.
    wideband_bank_balance: WidebandPairVerification | None = None

    def commanded_afr(self, value: float, *, afr_stoich: float) -> float:
        """Convert a raw PID 0x44 reading to commanded AFR.

        Raises when the convention is unverified — a wrong guess here is a
        reciprocal, i.e. an error that grows with how rich the target is,
        and it is silent at idle where most sanity-checking happens.
        """
        if self.commanded_equiv_ratio_convention == "unverified":
            raise ValueError(
                "commanded_equiv_ratio_convention is 'unverified' for this vehicle: "
                "phi and lambda are reciprocals and agree at stoich, so the convention "
                "must be checked under enrichment (WOT). Log PID 0x44 at wide-open "
                "throttle and compare 14.7/value against 14.7*value with the ROM "
                "target curve, then record the winner in afr_calibration.yaml."
            )
        if value <= 0:
            raise ValueError(f"equivalence ratio must be positive, got {value}")
        if self.commanded_equiv_ratio_convention == "phi":
            return afr_stoich / value
        return afr_stoich * value

    def commanded_afr_series(self, ratio: pd.Series, *, afr_stoich: float) -> pd.Series:
        """Convert a series of raw PID 0x44 readings, fuel cut included.

        A reading of exactly zero is decel fuel cut — the ECU is commanding no
        target at all — so it becomes ``NaN`` (no commanded AFR), never an
        error. Only :meth:`commanded_afr` on a single value refuses a
        non-positive ratio; applied row by row it raised "equivalence ratio
        must be positive, got 0.0" and cost four sessions of the 2026-09-21
        corpus run. Callers that need to *count* the cut samples test
        ``ratio <= 0`` themselves (``delivery_error`` reports them as the
        ``fuel_cut`` gate).

        Still refuses an unverified convention, once per call, probing with a
        positive value so a session that opens on fuel cut is not mistaken
        for a malformed one.
        """
        numeric = pd.to_numeric(ratio, errors="coerce")
        positive = numeric[numeric > 0].dropna()
        probe = float(positive.iloc[0]) if not positive.empty else 1.0
        self.commanded_afr(probe, afr_stoich=afr_stoich)
        valid = numeric.where(numeric > 0)
        if self.commanded_equiv_ratio_convention == "phi":
            return afr_stoich / valid
        return afr_stoich * valid

    def target_map(self, revision: str) -> TargetAfrMap | None:
        """The map recorded for exactly ``revision``, or ``None``.

        No fallback (P02.16): ``active_revision`` used to stand in for a missing
        or unknown revision, which labelled the active map as any session's.
        A caller with no revision has no map, and must say so.
        """
        return next((item for item in self.target_maps if item.revision == revision), None)


def load_afr_calibration(path: Path) -> AfrCalibration:
    return _load_afr_calibration_cached(path, path.stat().st_mtime_ns)


@lru_cache(maxsize=16)
def _load_afr_calibration_cached(path: Path, mtime_ns: int) -> AfrCalibration:
    del mtime_ns
    return AfrCalibration.model_validate(yaml.safe_load(path.read_text()))


def voltage_to_afr(voltage: pd.Series, transfer: VoltageAfrTransfer) -> pd.Series:
    points = transfer.points
    values = np.interp(
        pd.to_numeric(voltage, errors="coerce"),
        [point.voltage for point in points],
        [point.afr for point in points],
        left=np.nan,
        right=np.nan,
    )
    return pd.Series(values, index=voltage.index, dtype=float)


class LoadAxisUnavailableError(ValueError):
    """The target map's load axis has no verified channel in its unit (P02.13).

    ``reason`` starts with :data:`LOAD_AXIS_UNVERIFIED`; it is the text the
    feature policy records for :data:`TARGET_AFR_CHANNELS`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


#: The unit a wide-frame column's values are in, per the catalogs that decoded
#: the frame, or ``None`` when no catalog gives one. The catalogs (the DID
#: catalog, the Mode 01 registry) sit above this layer, so the caller resolves
#: the unit and hands the resolver in:
#: ``src.preprocessing.channel_units.channel_units_for(<the decoding DID map>)``.
ChannelUnits = Callable[[str], str | None]

#: Catalog placeholders that are not a unit.
_NO_UNIT: frozenset[str] = frozenset({"", "unknown"})


def load_axis_refusal(
    axis: str = DEFAULT_TARGET_LOAD_AXIS,
    unit: str = DEFAULT_TARGET_LOAD_AXIS_UNIT,
    *,
    channel_units: ChannelUnits | None,
) -> str | None:
    """Why no channel may feed a table lookup on ``axis`` (in ``unit``), or
    ``None`` when a vehicle declares one whose *catalog* unit is ``unit``.

    The unit is never taken from the declaration. ``channel_units`` resolves it
    from the channel's own catalog entry. A declared channel the catalog gives
    no unit (absent, ``unknown``, or no catalog passed: ``None``) is refused, and
    so is one in another unit. This is the P02.13 defect (calculated load in %
    on a base-fuel-schedule axis in ms), and a declaration alone cannot relabel
    it.
    """
    declared = load_axis_channels(axis)
    if not declared:
        return (
            f"{LOAD_AXIS_UNVERIFIED}: no channel is verified to report the target map's "
            f"load axis {axis} ({unit}) on any vehicle "
            "(src.signals.vehicle_signals LOAD_AXIS_CHANNELS; P02.13, operator decision "
            "2026-09-27). Calculated load (obd:ENGINE_LOAD, %) is not this axis"
        )
    refused: list[str] = []
    for channel in declared:
        catalog_unit = None if channel_units is None else channel_units(channel.channel)
        if catalog_unit is None or catalog_unit.strip().lower() in _NO_UNIT:
            refused.append(f"{channel.channel} (the channel catalog gives it no unit)")
        elif catalog_unit != unit:
            refused.append(f"{channel.channel} (catalog unit {catalog_unit})")
    if refused:
        return (
            f"{LOAD_AXIS_UNVERIFIED}: the {axis} axis is in {unit}, and the declared "
            f"{', '.join(refused)} is not (P02.13)"
        )
    return None


def target_afr_refusal(
    target: TargetAfrMap | None, *, channel_units: ChannelUnits | None
) -> str | None:
    """Why ``derived:TARGET_AFR`` cannot be looked up on ``target``, or ``None``."""
    if target is None:
        return "no target AFR map is recorded for this tune revision"
    return load_axis_refusal(
        target.load_axis_name, target.load_axis_unit, channel_units=channel_units
    )


def target_load_axis(
    target: TargetAfrMap, frame: pd.DataFrame, *, channel_units: ChannelUnits | None
) -> LoadAxisChannel:
    """The verified channel that carries ``target``'s load axis in ``frame``.

    The only way the lookup gets its load input. Raises
    :class:`LoadAxisUnavailableError` when no vehicle declares a channel whose
    catalog unit is the axis unit (:func:`load_axis_refusal`), and ``KeyError``
    when the declared channel is not in ``frame``. There is no argument through
    which another column could be passed in.
    """
    refusal = target_afr_refusal(target, channel_units=channel_units)
    if refusal is not None:
        raise LoadAxisUnavailableError(refusal)
    for channel in load_axis_channels(target.load_axis_name):
        if channel.channel in frame:
            return channel
    raise KeyError(
        f"no verified {target.load_axis_name} channel is in the frame "
        f"({', '.join(c.channel for c in load_axis_channels(target.load_axis_name))})"
    )


def target_afr(
    frame: pd.DataFrame,
    target: TargetAfrMap,
    *,
    channel_units: ChannelUnits | None,
    rpm_column: str = "obd:RPM",
) -> pd.Series:
    """Bilinear lookup; rows outside the exported ROM grid stay unknown.

    The load input is the vehicle's verified channel for ``target``'s load axis
    (:func:`target_load_axis`), read from ``frame`` here. A caller cannot hand
    in a load series of its own, so the map is never looked up in a unit its
    axis is not in (P02.13).

    A point past an edge of either axis is read as that edge's row or column
    only where the map declares, with evidence, that the ECM does so
    (``load_axis_out_of_range`` / ``rpm_axis_out_of_range``, P02.15).
    Otherwise the row stays unknown.
    """
    axis = target_load_axis(target, frame, channel_units=channel_units)
    rpm = frame[rpm_column]
    rpm_values = pd.to_numeric(rpm, errors="coerce").to_numpy(dtype=float)
    load_values = pd.to_numeric(frame[axis.channel], errors="coerce").to_numpy(dtype=float)
    result = np.full(len(rpm_values), np.nan, dtype=float)
    grid = np.asarray(target.afr, dtype=float)
    rpm_axis = np.asarray(target.rpm_breakpoints, dtype=float)
    load_axis = np.asarray(target.load_breakpoints, dtype=float)
    inside = (
        np.isfinite(rpm_values)
        & np.isfinite(load_values)
        & ((rpm_values >= rpm_axis[0]) | target.clamps_below_first_rpm)
        & ((rpm_values <= rpm_axis[-1]) | target.clamps_above_last_rpm)
        & ((load_values >= load_axis[0]) | target.clamps_below_first_load)
        & ((load_values <= load_axis[-1]) | target.clamps_above_last_load)
    )
    if not inside.any():
        return pd.Series(result, index=rpm.index, dtype=float)
    # Bilinear in two vectorized passes: interpolate every rpm row along the
    # load axis at once, then blend the two bracketing rows per sample. A
    # per-row Python loop here costs ~20 s on a 40-minute drive.
    # Only a declared edge lets a point past it through ``inside``; clipping
    # makes the edge row or column explicit rather than leaning on the
    # interpolation's own end behaviour.
    selected_rpm = np.clip(rpm_values[inside], rpm_axis[0], rpm_axis[-1])
    selected_load = np.clip(load_values[inside], load_axis[0], load_axis[-1])
    along_load = np.column_stack([np.interp(selected_load, load_axis, row) for row in grid])
    upper = np.clip(np.searchsorted(rpm_axis, selected_rpm, side="left"), 1, len(rpm_axis) - 1)
    lower = upper - 1
    span = rpm_axis[upper] - rpm_axis[lower]
    fraction = np.where(span > 0, (selected_rpm - rpm_axis[lower]) / span, 0.0)
    rows = np.arange(len(selected_rpm))
    result[inside] = along_load[rows, lower] * (1.0 - fraction) + along_load[rows, upper] * fraction
    return pd.Series(result, index=rpm.index, dtype=float)


class FlashHistory(Protocol):
    """A vehicle's flash history, as the target lookup reads it (P02.16).

    ``revisions_at`` maps a timestamp series to the name of the revision in
    force at each instant, ``None`` where none is recorded. The registry lives
    above this layer: :class:`src.vehicle.tune_revisions.TuneRevisionRegistry`
    implements it, and so does :class:`NoFlashHistory`.
    """

    def revisions_at(self, timestamps: pd.Series) -> pd.Series: ...


@dataclass(frozen=True)
class NoFlashHistory:
    """No flash history can be read, and why. Every row's revision is unknown."""

    reason: str

    def revisions_at(self, timestamps: pd.Series) -> pd.Series:
        return pd.Series([None] * len(timestamps), index=timestamps.index, dtype=object)


@dataclass(frozen=True)
class RecordedClockFlashHistory:
    """``base`` read at each row's *recorded* instant, for rows stamped on
    another clock (P02.16).

    A replay rebases every row to wall-clock at emit, so a replayed k106
    session would otherwise resolve to the revision flashed today. The live
    source records, per buffered row, how far its stamp is ahead of the
    instant the data was recorded (``shift_s``: emit epoch minus recorded
    epoch, from :data:`src.telemetry.REPLAY_SOURCE_AT_KEY`). A timestamp maps
    back through the shift of the newest row stamped at or before it (the
    first row's for an earlier one). Pickle-safe: the live tick carries it to
    the feature process.
    """

    base: FlashHistory
    #: Row stamps, epoch nanoseconds, non-decreasing.
    stamped_ns: tuple[int, ...]
    #: Per row, stamp minus recorded instant, in seconds.
    shift_s: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.stamped_ns) != len(self.shift_s) or not self.stamped_ns:
            raise ValueError("stamped_ns and shift_s must be equal-length and non-empty")

    def revisions_at(self, timestamps: pd.Series) -> pd.Series:
        stamps = pd.to_datetime(timestamps, utc=True, errors="coerce")
        stamped = np.asarray(self.stamped_ns, dtype=np.int64)
        present = stamps.notna().to_numpy()
        values = stamps.dt.tz_convert(None).to_numpy(dtype="datetime64[ns]").astype(np.int64)
        # The newest row stamped at or before each instant (the first row's
        # shift for an earlier one).
        position = np.clip(np.searchsorted(stamped, values, side="right") - 1, 0, len(stamped) - 1)
        shift_ns = np.rint(np.asarray(self.shift_s, dtype=float)[position] * 1e9).astype(np.int64)
        recorded = pd.Series(pd.to_datetime(values - shift_ns, utc=True), index=timestamps.index)
        return self.base.revisions_at(recorded.where(present))


#: Stable prefixes of the reasons a row gets no target from its revision (P02.16).
TUNE_REVISION_UNKNOWN = "tune_revision_unknown"
TARGET_MAP_UNRECORDED = "target_map_unrecorded"


@dataclass(frozen=True)
class TargetRevisionRefusal:
    """Rows left without a target because of their tune revision."""

    #: The revision those rows ran on; ``None`` when none resolves.
    revision: str | None
    rows: int
    reason: str


@dataclass(frozen=True)
class TargetAfrLookup:
    """``derived:TARGET_AFR`` per row, each on its own revision's map."""

    values: pd.Series
    #: The revisions that produced a value on at least one row.
    revisions_used: tuple[str, ...]
    refusals: tuple[TargetRevisionRefusal, ...]
    #: Rows a map would have given a value but the ECM was in fuel cut
    #: (:func:`fuel_cut_rows`), so they have none.
    fuel_cut_rows: int = 0


#: J1979 PID 0x44, commanded equivalence ratio. Exactly zero is the ECM
#: commanding no fuel at all: decel fuel cut (``AfrCalibration.commanded_afr_series``
#: reads it the same way), whatever the vehicle's phi/lambda convention.
COMMANDED_EQUIV_RATIO_COLUMN = "obd:COMMANDED_EQUIV_RATIO"


def fuel_cut_rows(frame: pd.DataFrame) -> pd.Series:
    """Rows on which the ECM reports decel fuel cut: its commanded
    equivalence ratio (PID 0x44) reads exactly zero (P02.15).

    A sentinel is a state, not a number: the ECM has no target then (UpRev
    displays its TARGET AFR as 30.00), so the target map has nothing to say.
    PID 0x03 ``FUEL_STATUS`` cannot stand in. Its fuel-cut code (0x04) is
    "open loop for load *or* decel fuel cut" by J1979. On the Xterra's two
    default drives it reads 0x04 on 154 rows that also carry phi: 114 at
    phi 0, 35 near stoich, 3 under enrichment (phi 1.15-1.16), so it would
    withhold real targets. Phi 0 agrees with it the other way (114 of 117
    phi-0 rows read 0x04). A row with no commanded ratio is not in fuel cut as
    far as this knows: absent, the state is unknown, not "cut".
    """
    if COMMANDED_EQUIV_RATIO_COLUMN not in frame:
        return pd.Series(False, index=frame.index, dtype=bool)
    ratio = pd.to_numeric(frame[COMMANDED_EQUIV_RATIO_COLUMN], errors="coerce")
    return ratio.eq(0.0).fillna(False).astype(bool)


def target_map_refusal(calibration: AfrCalibration, revision: str | None) -> str | None:
    """Why rows on ``revision`` get no target map, or ``None`` when one is recorded.

    Exact match only: a revision with no map of its own is refused, never
    given ``active_revision``'s or a neighbour's (P02.16).
    """
    if revision is None:
        return f"{TUNE_REVISION_UNKNOWN}: the flash history records no revision for these rows"
    if any(item.revision == revision for item in calibration.target_maps):
        return None
    recorded = ", ".join(item.revision for item in calibration.target_maps) or "none"
    return (
        f"{TARGET_MAP_UNRECORDED}: tune revision {revision} has no target AFR map in the "
        f"vehicle's afr_calibration.yaml (recorded: {recorded}). Its rows get no target "
        "rather than another revision's map (P02.16)"
    )


def target_afr_by_revision(
    frame: pd.DataFrame,
    calibration: AfrCalibration,
    flash_history: FlashHistory,
    *,
    channel_units: ChannelUnits | None,
    rpm_column: str = "obd:RPM",
    timestamp_column: str = "timestamp",
) -> TargetAfrLookup:
    """Look every row up on the map of the revision flashed when it was recorded.

    The revision is resolved per row, from the row's own timestamp, so a frame
    that concatenates sessions, or one session that spans a flash, gets each
    side's map. There is no argument through which a single revision could be
    applied to the whole frame. A row whose revision has no map, or that
    resolves to no revision, stays NaN and is named in
    :attr:`TargetAfrLookup.refusals`. A row in decel fuel cut
    (:func:`fuel_cut_rows`) has no target either, and is counted in
    :attr:`TargetAfrLookup.fuel_cut_rows`.

    Raises like :func:`target_afr` when the load axis is refused or its channel
    is not in ``frame``.
    """
    values = pd.Series(np.nan, index=frame.index, dtype=float)
    if timestamp_column not in frame:
        refusal = TargetRevisionRefusal(
            None,
            len(frame),
            f"{TUNE_REVISION_UNKNOWN}: the frame has no {timestamp_column!r} column, so no "
            "row's tune revision can be resolved",
        )
        return TargetAfrLookup(values, (), (refusal,) if len(frame) else ())
    resolved = flash_history.revisions_at(frame[timestamp_column])
    labels = pd.Series(resolved.to_numpy(dtype=object), index=frame.index, dtype=object)
    known = labels.notna()
    used: list[str] = []
    refusals: list[TargetRevisionRefusal] = []
    if not known.all():
        # A replay wraps the history in RecordedClockFlashHistory; the cause of
        # an unreadable registry is on the history it wraps.
        underlying = (
            flash_history.base
            if isinstance(flash_history, RecordedClockFlashHistory)
            else flash_history
        )
        reason = (
            f"{TUNE_REVISION_UNKNOWN}: {underlying.reason}"
            if isinstance(underlying, NoFlashHistory)
            else target_map_refusal(calibration, None)
        )
        refusals.append(TargetRevisionRefusal(None, int((~known).sum()), str(reason)))
    maps = {item.revision: item for item in calibration.target_maps}
    for revision in dict.fromkeys(str(label) for label in labels[known]):
        rows = labels == revision
        why = target_map_refusal(calibration, revision)
        if why is not None:
            refusals.append(TargetRevisionRefusal(revision, int(rows.sum()), why))
            continue
        values.loc[rows] = target_afr(
            frame.loc[rows], maps[revision], channel_units=channel_units, rpm_column=rpm_column
        )
        used.append(revision)
    cut = fuel_cut_rows(frame) & values.notna()
    values = values.where(~cut)
    return TargetAfrLookup(values, tuple(used), tuple(refusals), int(cut.sum()))


#: Refusals already logged in this process: the live path featurizes every
#: tick, and one revision's refusal is news once.
_logged_refusals: set[str] = set()


def append_afr_channels(
    frame: pd.DataFrame,
    vehicle: VehicleParameters,
    calibration: AfrCalibration | None = None,
    *,
    channel_units: ChannelUnits | None,
    flash_history: FlashHistory,
) -> pd.DataFrame:
    """Append measured AFR, censor flags, target AFR and per-bank error.

    ``derived:TARGET_AFR`` and ``derived:AFR_ERROR_B*`` exist only where the
    map's load axis has a declared channel whose catalog unit (resolved by
    ``channel_units``) is the axis unit (:func:`target_afr_refusal` names why
    not, P02.13). They are absent otherwise, never computed from another
    load-like column. ``channel_units=None`` means no catalog, so no target.

    P02.16 — each row is looked up on the map of the revision ``flash_history``
    says was flashed at that row's timestamp (:func:`target_afr_by_revision`),
    on the live path and offline alike. It has no default: a calibration's
    ``active_revision`` applied to every session gave pre-flash sessions the
    wrong map. Rows whose revision has no map stay NaN, and the reason is
    logged once per process; the channels are absent when no row resolves one.
    """
    out = frame.copy()
    stoich = fuel_properties(vehicle).afr_stoich
    transfer = calibration.voltage_transfer if calibration else None
    floor = transfer.censored_floor_afr if transfer else 10.0
    ceiling = transfer.censored_ceiling_afr if transfer else 18.0
    for bank in (1, 2):
        measured: pd.Series | None = None
        # The bank's upstream wide-range lambda PID, which only the vehicle
        # can name for bank 2 (its O2 sensor layout decides the sensor number).
        for lambda_column in wideband_lambda_columns(bank):
            if measured is not None or lambda_column not in out:
                continue
            candidate = pd.to_numeric(out[lambda_column], errors="coerce") * stoich
            # Presence is not the same as data. Bank-2 lambda is unsupported on
            # some ECUs (it stays in poll sets because that is a fact about one
            # vehicle), leaving an all-NaN column that must not shadow a
            # working voltage DID.
            measured = candidate if candidate.notna().any() else None
        # The vehicle's A/F sensor voltage channel (R05.4: named by the
        # vehicle in src.signals.vehicle_signals, never here).
        for voltage_column in af_sensor_voltage_columns(bank):
            if measured is not None or not transfer or voltage_column not in out:
                continue
            candidate = voltage_to_afr(out[voltage_column], transfer)
            measured = candidate if candidate.notna().any() else None
        if measured is not None:
            out[f"derived:AFR_B{bank}"] = measured
            out[f"derived:AFR_CENSORED_B{bank}"] = measured.le(floor) | measured.ge(ceiling)

    _append_lambda_bank_balances(out, calibration, stoich=stoich, floor=floor, ceiling=ceiling)

    if calibration is None or not calibration.target_maps or "obd:RPM" not in out:
        return out
    # Every recorded map shares one load axis today; a refused axis refuses
    # them all, before any revision is resolved.
    if any(
        target_afr_refusal(target, channel_units=channel_units) is not None
        for target in calibration.target_maps
    ):
        return out
    try:
        lookup = target_afr_by_revision(
            out, calibration, flash_history, channel_units=channel_units
        )
    except KeyError:
        # The verified axis channel was not captured on this frame.
        return out
    for refusal in lookup.refusals:
        if refusal.reason not in _logged_refusals:
            _logged_refusals.add(refusal.reason)
            log.warning("derived:TARGET_AFR withheld on %d rows: %s", refusal.rows, refusal.reason)
    if not lookup.revisions_used:
        return out
    out[TARGET_AFR] = lookup.values
    for bank, error_column in zip((1, 2), AFR_ERROR_CHANNELS, strict=True):
        measured_column = f"derived:AFR_B{bank}"
        if measured_column in out:
            out[error_column] = (out[measured_column] - out[TARGET_AFR]).where(
                ~out[f"derived:AFR_CENSORED_B{bank}"]
            )
    return out


def _append_lambda_bank_balances(
    out: pd.DataFrame,
    calibration: AfrCalibration | None,
    *,
    stoich: float,
    floor: float,
    ceiling: float,
) -> None:
    """Append the per-instrument-pair λ bank balances (B05.8), in place.

    Each bank's reading becomes λ on its own before the difference, and a
    censored reading (at the transfer's floor or the fuel-cut ceiling) is no
    measurement, so the balance is missing there rather than a rail-to-value
    difference. Both inputs' freshness feeds the output's ``__gap``.
    """
    transfer = calibration.voltage_transfer if calibration else None
    pairs: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    if transfer is not None:
        pairs.append(
            (
                LAMBDA_BANK_BALANCE_AF_VOLTAGE,
                af_sensor_voltage_columns(1),
                af_sensor_voltage_columns(2),
            )
        )
    if calibration is not None and calibration.wideband_bank_balance is not None:
        pairs.append(
            (LAMBDA_BANK_BALANCE_WIDEBAND, wideband_lambda_columns(1), wideband_lambda_columns(2))
        )
    for output, bank_1, bank_2 in pairs:
        columns = (_selected_input(out, bank_1), _selected_input(out, bank_2))
        if columns[0] is None or columns[1] is None:
            continue
        lambdas = []
        for column in columns:
            if output == LAMBDA_BANK_BALANCE_AF_VOLTAGE:
                assert transfer is not None
                afr = voltage_to_afr(out[column], transfer)
            else:
                afr = pd.to_numeric(out[column], errors="coerce") * stoich
            lambdas.append((afr / stoich).where((afr > floor) & (afr < ceiling)))
        out[output] = (lambdas[0] - lambdas[1]).astype(float)
        _append_dependency_gap(out, output, bank_1, bank_2)


def afr_error_surface(
    frame: pd.DataFrame,
    target: TargetAfrMap,
    *,
    bank: int,
    channel_units: ChannelUnits | None,
    min_samples: int = 5,
) -> list[dict[str, Any]]:
    """RPM x load error cells; censored observations never enter statistics.

    Cells are binned on the map's own load axis, read from its verified
    channel (:func:`target_load_axis`). Raises :class:`LoadAxisUnavailableError`
    when there is none, rather than return an empty surface that reads as
    "no errors".
    """
    error_column = f"derived:AFR_ERROR_B{bank}"
    censored_column = f"derived:AFR_CENSORED_B{bank}"
    refusal = target_afr_refusal(target, channel_units=channel_units)
    if refusal is not None:
        raise LoadAxisUnavailableError(refusal)
    present = [c for c in load_axis_channels(target.load_axis_name) if c.channel in frame]
    if not present:
        return []
    load_column = present[0].channel
    required = ["obd:RPM", load_column, error_column]
    if any(column not in frame for column in required):
        return []
    columns = [*required, *([censored_column] if censored_column in frame else [])]
    clean = frame.loc[:, columns].copy()
    if censored_column in clean:
        clean = clean.loc[~clean[censored_column].fillna(False).astype(bool)]
    clean = clean.dropna(subset=required)
    clean["rpm_bin"] = pd.cut(
        clean["obd:RPM"], target.rpm_breakpoints, include_lowest=True, right=True
    )
    clean["load_bin"] = pd.cut(
        clean[load_column], target.load_breakpoints, include_lowest=True, right=True
    )
    rows: list[dict[str, Any]] = []
    grouped = clean.groupby(["rpm_bin", "load_bin"], observed=True)[error_column]
    for (rpm_bin, load_bin), values in grouped:
        rpm_interval = cast(pd.Interval, rpm_bin)
        load_interval = cast(pd.Interval, load_bin)
        numeric = pd.to_numeric(values, errors="coerce").dropna()
        count = int(len(numeric))
        rows.append(
            {
                "bank": bank,
                "rpm_low": float(rpm_interval.left),
                "rpm_high": float(rpm_interval.right),
                "load_low": float(load_interval.left),
                "load_high": float(load_interval.right),
                "median_error_afr": float(numeric.median()),
                "iqr_error_afr": float(numeric.quantile(0.75) - numeric.quantile(0.25)),
                "count": count,
                "sparse": count < min_samples,
            }
        )
    return rows


__all__ = [
    "AFR_ERROR_CHANNELS",
    "COMMANDED_EQUIV_RATIO_COLUMN",
    "DEFAULT_TARGET_LOAD_AXIS",
    "DEFAULT_TARGET_LOAD_AXIS_UNIT",
    "LAMBDA_BANK_BALANCE_AF_VOLTAGE",
    "LAMBDA_BANK_BALANCE_WIDEBAND",
    "LOAD_AXIS_UNVERIFIED",
    "TARGET_AFR",
    "TARGET_AFR_CHANNELS",
    "TARGET_MAP_UNRECORDED",
    "TUNE_REVISION_UNKNOWN",
    "AfrCalibration",
    "AxisEdgeClamp",
    "AxisOutOfRange",
    "ChannelUnits",
    "FlashHistory",
    "LoadAxisUnavailableError",
    "NoFlashHistory",
    "RecordedClockFlashHistory",
    "TargetAfrLookup",
    "TargetAfrMap",
    "TargetMapProvenance",
    "TargetRevisionRefusal",
    "VoltageAfrTransfer",
    "WidebandPairVerification",
    "append_afr_channels",
    "afr_error_surface",
    "fuel_cut_rows",
    "load_afr_calibration",
    "load_axis_refusal",
    "target_afr",
    "target_afr_by_revision",
    "target_afr_refusal",
    "target_load_axis",
    "target_map_refusal",
    "voltage_to_afr",
]
