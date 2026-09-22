"""Measured/target AFR reconstruction for tuning capture sessions."""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.derived import fuel_properties
from src.config import VehicleParameters


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


class TargetAfrMap(_Strict):
    revision: str
    load_axis_name: Literal["base_fuel_schedule"] = "base_fuel_schedule"
    load_axis_unit: Literal["ms"] = "ms"
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


class AfrCalibration(_Strict):
    schema_version: int = 1
    provenance: dict[str, str] = Field(default_factory=dict)
    active_revision: str | None = None
    #: Per-vehicle, because it is an ECM behaviour rather than a standard.
    commanded_equiv_ratio_convention: EquivRatioConvention = "unverified"
    voltage_transfer: VoltageAfrTransfer | None = None
    target_maps: list[TargetAfrMap] = Field(default_factory=list)

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


def target_afr(rpm: pd.Series, load: pd.Series, target: TargetAfrMap) -> pd.Series:
    """Bilinear lookup; rows outside the exported ROM grid stay unknown."""
    rpm_values = pd.to_numeric(rpm, errors="coerce").to_numpy(dtype=float)
    load_values = pd.to_numeric(load, errors="coerce").to_numpy(dtype=float)
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
    tune_revision: str | None = None,
) -> pd.DataFrame:
    """Append measured AFR, censor flags, target AFR and per-bank error."""
    out = frame.copy()
    stoich = fuel_properties(vehicle).afr_stoich
    transfer = calibration.voltage_transfer if calibration else None
    floor = transfer.censored_floor_afr if transfer else 10.0
    ceiling = transfer.censored_ceiling_afr if transfer else 18.0
    for bank in (1, 2):
        measured: pd.Series | None = None
        lambda_column = f"obd:O2_WR_LAMBDA_S{bank}"
        voltage_column = f"did:ECM_AF_SENSOR_V_B{bank}"
        if lambda_column in out:
            candidate = pd.to_numeric(out[lambda_column], errors="coerce") * stoich
            # Presence is not the same as data. Bank-2 lambda is unsupported on
            # some ECUs (it stays in poll sets because that is a fact about one
            # vehicle), leaving an all-NaN column that must not shadow a
            # working voltage DID.
            measured = candidate if candidate.notna().any() else None
        if measured is None and transfer and voltage_column in out:
            candidate = voltage_to_afr(out[voltage_column], transfer)
            measured = candidate if candidate.notna().any() else None
        if measured is not None:
            out[f"derived:AFR_B{bank}"] = measured
            out[f"derived:AFR_CENSORED_B{bank}"] = measured.le(floor) | measured.ge(ceiling)

    target = calibration.target_map(tune_revision) if calibration else None
    if target and "obd:RPM" in out and "obd:ENGINE_LOAD" in out:
        out["derived:TARGET_AFR"] = target_afr(out["obd:RPM"], out["obd:ENGINE_LOAD"], target)
        for bank in (1, 2):
            measured_column = f"derived:AFR_B{bank}"
            if measured_column in out:
                out[f"derived:AFR_ERROR_B{bank}"] = (
                    out[measured_column] - out["derived:TARGET_AFR"]
                ).where(~out[f"derived:AFR_CENSORED_B{bank}"])
    return out


def afr_error_surface(
    frame: pd.DataFrame,
    target: TargetAfrMap,
    *,
    bank: int,
    min_samples: int = 5,
) -> list[dict[str, Any]]:
    """RPM x load error cells; censored observations never enter statistics."""
    error_column = f"derived:AFR_ERROR_B{bank}"
    censored_column = f"derived:AFR_CENSORED_B{bank}"
    required = ["obd:RPM", "obd:ENGINE_LOAD", error_column]
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
        clean["obd:ENGINE_LOAD"], target.load_breakpoints, include_lowest=True, right=True
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
    "AfrCalibration",
    "TargetAfrMap",
    "TargetMapProvenance",
    "VoltageAfrTransfer",
    "append_afr_channels",
    "afr_error_surface",
    "load_afr_calibration",
    "target_afr",
    "voltage_to_afr",
]
