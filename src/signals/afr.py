"""Measured/target AFR reconstruction for tuning capture sessions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

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

    def target_map(self, revision: str | None = None) -> TargetAfrMap | None:
        selected = revision or self.active_revision
        return next((item for item in self.target_maps if item.revision == selected), None)


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


#: The unit a wide-frame column's values are in, per the repo's channel
#: catalogs, or ``None`` when no catalog gives one. The catalogs (``dids.yaml``,
#: the Mode 01 registry) sit above this layer, so the caller resolves the unit
#: and hands the resolver in: ``src.preprocessing.channel_units.catalog_channel_unit``.
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
        & (rpm_values >= rpm_axis[0])
        & (rpm_values <= rpm_axis[-1])
        & (load_values >= load_axis[0])
        & (load_values <= load_axis[-1])
    )
    if not inside.any():
        return pd.Series(result, index=rpm.index, dtype=float)
    # Bilinear in two vectorized passes: interpolate every rpm row along the
    # load axis at once, then blend the two bracketing rows per sample. A
    # per-row Python loop here costs ~20 s on a 40-minute drive.
    selected_rpm = rpm_values[inside]
    selected_load = load_values[inside]
    along_load = np.column_stack([np.interp(selected_load, load_axis, row) for row in grid])
    upper = np.clip(np.searchsorted(rpm_axis, selected_rpm, side="left"), 1, len(rpm_axis) - 1)
    lower = upper - 1
    span = rpm_axis[upper] - rpm_axis[lower]
    fraction = np.where(span > 0, (selected_rpm - rpm_axis[lower]) / span, 0.0)
    rows = np.arange(len(selected_rpm))
    result[inside] = along_load[rows, lower] * (1.0 - fraction) + along_load[rows, upper] * fraction
    return pd.Series(result, index=rpm.index, dtype=float)


def append_afr_channels(
    frame: pd.DataFrame,
    vehicle: VehicleParameters,
    calibration: AfrCalibration | None = None,
    *,
    channel_units: ChannelUnits | None,
    tune_revision: str | None = None,
) -> pd.DataFrame:
    """Append measured AFR, censor flags, target AFR and per-bank error.

    ``derived:TARGET_AFR`` and ``derived:AFR_ERROR_B*`` exist only where the
    map's load axis has a declared channel whose catalog unit (resolved by
    ``channel_units``) is the axis unit (:func:`target_afr_refusal` names why
    not, P02.13). They are absent otherwise, never computed from another
    load-like column. ``channel_units=None`` means no catalog, so no target.
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

    target = calibration.target_map(tune_revision) if calibration else None
    if (
        target is None
        or target_afr_refusal(target, channel_units=channel_units) is not None
        or "obd:RPM" not in out
    ):
        return out
    try:
        out[TARGET_AFR] = target_afr(out, target, channel_units=channel_units)
    except KeyError:
        # The verified axis channel was not captured on this frame.
        return out
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
    "DEFAULT_TARGET_LOAD_AXIS",
    "DEFAULT_TARGET_LOAD_AXIS_UNIT",
    "LAMBDA_BANK_BALANCE_AF_VOLTAGE",
    "LAMBDA_BANK_BALANCE_WIDEBAND",
    "LOAD_AXIS_UNVERIFIED",
    "TARGET_AFR",
    "TARGET_AFR_CHANNELS",
    "AfrCalibration",
    "ChannelUnits",
    "LoadAxisUnavailableError",
    "TargetAfrMap",
    "TargetMapProvenance",
    "VoltageAfrTransfer",
    "WidebandPairVerification",
    "append_afr_channels",
    "afr_error_surface",
    "load_afr_calibration",
    "load_axis_refusal",
    "target_afr",
    "target_afr_refusal",
    "target_load_axis",
    "voltage_to_afr",
]
