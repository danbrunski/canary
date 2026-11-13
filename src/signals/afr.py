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
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.columns import SESSION_COLUMN, arrived_column
from src.config import VehicleParameters
from src.refusal import ReasonedRefusal
from src.signals.bins import cell_codes
from src.signals.derived import _append_dependency_gap, _selected_input, fuel_properties
from src.signals.fuel_cut import COMMANDED_EQUIV_RATIO_COLUMN, fuel_cut_rows
from src.signals.pairing import to_seconds
from src.signals.vehicle_signals import (
    LoadAxisChannel,
    af_sensor_baro_columns,
    af_sensor_voltage_columns,
    load_axis_channels,
    wideband_lambda_columns,
)
from src.yaml_load import load_yaml

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


#: B06.7 — the name of the barometric-gain term in a transfer's ``excludes``.
#: A transfer that names it gets :func:`voltage_to_afr`'s per-sample gain from
#: ``AfrCalibration.baro_gain``; every other excluded term is a declared
#: residual source (``AfrCalibration.voltage_residual_sources``) of that name.
BARO_GAIN_TERM = "baro_gain"

#: A hex address as firmware notes write it (``0xE644``, ``0xFFFF4CA4``).
_HEX_ADDRESS = r"^0x[0-9A-Fa-f]+$"


class EcmTable1D(_Strict):
    """A 1-D ECM lookup as read from the image: raw breakpoints and values."""

    address: str = Field(pattern=_HEX_ADDRESS)
    axis_address: str = Field(pattern=_HEX_ADDRESS)
    #: Breakpoints in the ECM's input counts.
    axis_counts: list[int] = Field(min_length=2)
    #: Values in AFR x ``afr_scale_inverse`` (500 on the Xterra: lambda x 7350).
    values: list[int] = Field(min_length=2)
    afr_scale_inverse: int = Field(gt=0)

    @model_validator(mode="after")
    def _shape(self) -> EcmTable1D:
        if len(self.axis_counts) != len(self.values):
            raise ValueError("ECM table axis and values must be the same length")
        axis = self.axis_counts
        if any(left >= right for left, right in zip(axis, axis[1:], strict=False)):
            raise ValueError("ECM table axis must be strictly increasing")
        return self


class VoltageTransferProvenance(_Strict):
    """Where a vehicle's voltage transfer came from, and what it leaves out.

    Optional and per vehicle. A ``rom_table`` transfer *is* an ECM table: its
    points must equal ``table`` at ``volts_per_count`` exactly, so the curve
    and its stated source cannot drift apart. ``excludes`` names the terms of
    the ECM's own chain the curve leaves out (B06.7): :data:`BARO_GAIN_TERM`
    needs ``AfrCalibration.baro_gain``; any other name needs a residual source.
    """

    method: Literal["rom_table", "log_cross_reference", "datasheet"]
    #: The image, log or document it was taken from.
    source: str = Field(min_length=1)
    table: EcmTable1D | None = None
    #: The curve's voltage per ECM input count (Xterra: 0.0025 V; the DID
    #: decodes at 0.005 V and the table input is twice the DID word).
    volts_per_count: float | None = Field(default=None, gt=0)
    excludes: list[str] = Field(default_factory=list)
    image_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    verified_on: date
    notes: str = ""

    @model_validator(mode="after")
    def _consistent(self) -> VoltageTransferProvenance:
        if len(set(self.excludes)) != len(self.excludes):
            raise ValueError("voltage transfer excludes lists a term twice")
        if self.method == "rom_table" and (self.table is None or self.volts_per_count is None):
            raise ValueError("a rom_table transfer needs its table and volts_per_count")
        if BARO_GAIN_TERM in self.excludes and self.volts_per_count is None:
            raise ValueError(
                "a transfer that excludes the barometric gain needs volts_per_count: the "
                "gain is applied in the ECM's input counts"
            )
        return self


class VoltageAfrTransfer(_Strict):
    points: list[VoltageAfrPoint] = Field(min_length=2)
    censored_floor_afr: float = Field(default=10.0, gt=0)
    #: Decel fuel cut stops injection entirely, so the sensor reports nearly
    #: pure air. Those samples are not a lean *mixture* and must not reach the
    #: error surface — a cell holding sustained overrun would otherwise read as
    #: a large lean error and invite exactly the wrong correction.
    censored_ceiling_afr: float = Field(default=18.0, gt=0)
    #: B06.7 — optional; required by a vehicle whose ECM applies terms the
    #: curve leaves out (it names them in ``excludes``).
    provenance: VoltageTransferProvenance | None = None

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

    @model_validator(mode="after")
    def _is_its_table(self) -> VoltageAfrTransfer:
        """A ``rom_table`` transfer's points are the table, exactly."""
        provenance = self.provenance
        if provenance is None or provenance.method != "rom_table":
            return self
        table, per_count = provenance.table, provenance.volts_per_count
        assert table is not None and per_count is not None
        expected = [
            (count * per_count, value / table.afr_scale_inverse)
            for count, value in zip(table.axis_counts, table.values, strict=True)
        ]
        actual = [(point.voltage, point.afr) for point in self.points]
        if len(actual) != len(expected) or any(
            abs(av - ev) > 1e-9 or abs(aa - ea) > 1e-9
            for (av, aa), (ev, ea) in zip(actual, expected, strict=True)
        ):
            raise ValueError(
                f"a rom_table transfer must equal ECM table {table.address} at "
                f"{per_count} V per count exactly; its points differ"
            )
        return self

    def curve_afr(self, voltage: np.ndarray) -> np.ndarray:
        """The curve at ``voltage``, clamped to its end points (an ECM's table
        lookup clamps both ends)."""
        volts = np.asarray([point.voltage for point in self.points], dtype=float)
        afr = np.asarray([point.afr for point in self.points], dtype=float)
        return np.asarray(np.interp(np.asarray(voltage, dtype=float), volts, afr), dtype=float)

    @property
    def excludes(self) -> list[str]:
        return [] if self.provenance is None else list(self.provenance.excludes)

    @property
    def volts_per_count(self) -> float | None:
        return None if self.provenance is None else self.provenance.volts_per_count


class BaroWordScale(_Strict):
    """``kPa = word * scale + offset`` for the ECM's raw baro word ``0xFFFF4C30``.

    The arithmetic Mode 01 PID 0x33 reports (``(word * 58 / 128 + 27724) /
    512``), so a table axis in raw counts and the PID read one scale.
    """

    scale: float = Field(gt=0)
    offset: float
    basis: str = Field(min_length=1)

    def kpa(self, word: float) -> float:
        return word * self.scale + self.offset


class BaroGainSkip(_Strict):
    """The raw baro window inside which the ECM leaves the word uncorrected."""

    word_min: int = Field(ge=0, le=0xFFFF)
    word_max: int = Field(ge=0, le=0xFFFF)
    basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> BaroGainSkip:
        if self.word_min > self.word_max:
            raise ValueError("skip word_min must not exceed word_max")
        return self


class BaroGainProvenance(_Strict):
    method: Literal["rom_decompile", "datasheet", "measured"]
    revision: str = Field(min_length=1)
    image_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    #: Routines and words the arithmetic was read from.
    code: list[str] = Field(min_length=1)
    verified_on: date
    #: The check on logged data: the gain a fit selects against PID 0x24,
    #: beside the gain this table predicts from the session's baro.
    checked_on_data: str = Field(min_length=1)


class AfSensorBaroGain(_Strict):
    """The ECM's barometric gain on the A/F sensor word (B06.7).

    Before its AFR table the ECM rescales the doubled voltage word about
    stoich: ``x2' = stoich + (x2 - stoich) * k_unity / k``, with ``k`` looked
    up on its raw barometric pressure. PID 0x24/0x28 carry the result; a
    transfer recorded without it reads lean of them on the rich side at
    altitude (+1.5 % at lambda 0.85-0.90 at ~81 kPa, +3.9 % at 0.70-0.80).
    :func:`voltage_to_afr` applies it per sample, from the barometric
    pressure as of that sample, and refuses a sample with none rather than
    assume sea level.
    """

    #: The channel reporting the ECM's own barometric pressure.
    baro_channel: str = Field(min_length=1)
    #: A sample takes the newest baro read at or before it, no older than
    #: this; older is ``baro_stale``, none is ``baro_missing``.
    max_age_s: float = Field(gt=0)
    max_age_basis: str = Field(min_length=1)
    #: The channel's resolution and how the ECM rounds to it (PID 0x33 is
    #: floored, so a read is the bottom of a 1 kPa interval and the gain is
    #: looked up at its middle).
    pid_resolution_kpa: float = Field(gt=0)
    pid_rounding: Literal["floor", "nearest"]
    stoich_x2_counts: int = Field(gt=0)
    stoich_address: str = Field(pattern=_HEX_ADDRESS)
    #: ``k`` at which the gain is 1.
    k_unity: int = Field(gt=0)
    #: ``k`` is a byte in the ECM: whole counts, rounding not decoded.
    k_resolution: int = Field(ge=0)
    table_address: str = Field(pattern=_HEX_ADDRESS)
    axis_address: str = Field(pattern=_HEX_ADDRESS)
    #: The lookup's axis: the raw baro word's high byte (``word >> 8``) ...
    axis_raw: list[int] = Field(min_length=2)
    #: ... and the same breakpoints in kPa (checked against ``word_scale``).
    axis_kpa: list[float] = Field(min_length=2)
    k: list[int] = Field(min_length=2)
    word_scale: BaroWordScale
    skip: BaroGainSkip
    provenance: BaroGainProvenance

    @model_validator(mode="after")
    def _table(self) -> AfSensorBaroGain:
        if not len(self.axis_raw) == len(self.axis_kpa) == len(self.k):
            raise ValueError("baro gain axis_raw, axis_kpa and k must be the same length")
        if any(left > right for left, right in zip(self.axis_raw, self.axis_raw[1:], strict=False)):
            raise ValueError("baro gain axis_raw must be non-decreasing")
        if any(value <= 0 for value in self.k):
            raise ValueError("baro gain k must be positive (the ECM divides by it)")
        for raw, kpa in zip(self.axis_raw, self.axis_kpa, strict=True):
            expected = self.word_scale.kpa(raw * 256)
            if abs(expected - kpa) > 0.01:
                raise ValueError(
                    f"baro gain axis_kpa {kpa} disagrees with raw {raw} through word_scale "
                    f"({expected:.2f} kPa)"
                )
        if len(self._strict_axis()[0]) < 2:
            raise ValueError("baro gain axis needs two distinct breakpoints")
        if self.baro_channel not in af_sensor_baro_columns():
            raise ValueError(
                f"baro gain channel {self.baro_channel!r} is not declared as any vehicle's "
                "A/F sensor baro input (src.signals.vehicle_signals AF_SENSOR_BARO), so the "
                "derived-channel registry would not name it as an input of the voltage route"
            )
        return self

    def _strict_axis(self) -> tuple[np.ndarray, np.ndarray]:
        """The axis without the ROM's repeated end padding (``255, 255, ...``)."""
        keep = [0]
        for index in range(1, len(self.axis_raw)):
            if self.axis_raw[index] > self.axis_raw[keep[-1]]:
                keep.append(index)
        return (
            np.asarray([self.axis_kpa[i] for i in keep], dtype=float),
            np.asarray([self.k[i] for i in keep], dtype=float),
        )

    @property
    def skip_kpa(self) -> tuple[float, float]:
        return (self.word_scale.kpa(self.skip.word_min), self.word_scale.kpa(self.skip.word_max))

    def estimated_kpa(self, reported_kpa: np.ndarray) -> np.ndarray:
        """The ECM's baro from the channel's reading: the middle of the
        interval a floor-rounded reading stands for."""
        reported = np.asarray(reported_kpa, dtype=float)
        if self.pid_rounding == "floor":
            return reported + self.pid_resolution_kpa / 2.0
        return reported

    def k_at(self, reported_kpa: np.ndarray) -> np.ndarray:
        """The table's ``k`` at each reading (continuous; clamped at the axis ends)."""
        axis, k = self._strict_axis()
        return np.asarray(np.interp(self.estimated_kpa(reported_kpa), axis, k), dtype=float)

    def gain(self, reported_kpa: np.ndarray) -> np.ndarray:
        """``k_unity / k`` per reading, 1 inside the ECM's skip window."""
        kpa = self.estimated_kpa(reported_kpa)
        low, high = self.skip_kpa
        return np.where((kpa >= low) & (kpa <= high), 1.0, self.k_unity / self.k_at(reported_kpa))


class VoltageResidualSource(_Strict):
    """A term PID 0x24/0x28 carry and the voltage route does not (B06.7).

    Neither is modelled: one needs a memory read, the other the ECM's rpm and
    base fuel schedule at the same instant. Each is declared with its bounds
    per bank, in AFR, as *PID minus route*, so a residual between the two can
    say what it may contain and a tolerance can be built from the bounds.
    """

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    #: Where the term lives, per bank (RAM word or calibration table).
    addresses: dict[int, str]
    #: ``[low, high]`` AFR per bank.
    bounds_afr: dict[int, tuple[float, float]]
    #: How the term could be read: a memory read, from the operating point it
    #: is looked up on, or not at all.
    observable: Literal["memory_read", "operating_point", "not_observable"]
    basis: str = Field(min_length=1)
    #: For a table-driven term: the table per bank (rows x columns) in AFR x
    #: ``table_afr_scale_inverse``. Its bounds must be the table's range.
    table_afr_x500: dict[int, list[list[int]]] | None = None
    table_afr_scale_inverse: int = Field(default=500, gt=0)

    @model_validator(mode="after")
    def _bounds(self) -> VoltageResidualSource:
        if set(self.addresses) != set(self.bounds_afr):
            raise ValueError(f"{self.name}: addresses and bounds_afr must name the same banks")
        for bank, (low, high) in self.bounds_afr.items():
            if low > high:
                raise ValueError(f"{self.name}: bank {bank} bound low exceeds high")
        if self.table_afr_x500 is not None:
            for bank, rows in self.table_afr_x500.items():
                values = [value for row in rows for value in row]
                scale = float(self.table_afr_scale_inverse)
                expected = (min(values) / scale, max(values) / scale)
                declared = self.bounds_afr.get(bank)
                if declared is None or any(
                    abs(a - b) > 1e-9 for a, b in zip(declared, expected, strict=True)
                ):
                    raise ValueError(
                        f"{self.name}: bank {bank} bounds {declared} are not its table's "
                        f"range {expected}"
                    )
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
#: B06.7 renamed it (was ``derived:LAMBDA_BANK_BALANCE_AF_VOLTAGE``) when each
#: bank's conversion gained the ECM's barometric gain: away from stoich the
#: balance scales by ~1.13 at 80 kPa and a row without fresh baro has none, so
#: a model fitted on the gain-1 values must not be served these. The new name
#: changes the ``afr_bank_balance_robust`` recipe's contract fingerprint, which
#: flags the deployed fit ``contract_changed:recipe`` until it is refitted.
LAMBDA_BANK_BALANCE_AF_VOLTAGE = "derived:LAMBDA_BANK_BALANCE_AF_VOLTAGE_BARO"
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
    #: M01.3(a) — the AFR this ECM states at λ = 1, required: the constant its
    #: author chose for every AFR it holds or produces (the A/F voltage table,
    #: the target maps, the residual-source bounds), and so the scale of every
    #: AFR derived here from λ or φ. A property of the ECM's program, not of
    #: the fuel: the Xterra's tables are λ × 7350 = AFR × 500, i.e. λ × 14.7,
    #: whatever is in the tank. Converting these AFRs with the configured
    #: fuel's stoich read every voltage-route λ 4.3 % lean the day the profile
    #: said E10. Fuel-mass arithmetic uses the fuel's stoich
    #: (:func:`src.signals.derived.fuel_properties`); nothing here does.
    afr_scale: float = Field(gt=0)
    #: Per-vehicle, because it is an ECM behaviour rather than a standard.
    commanded_equiv_ratio_convention: EquivRatioConvention = "unverified"
    voltage_transfer: VoltageAfrTransfer | None = None
    #: B06.7 — the ECM's barometric gain on the A/F voltage word, declared by
    #: a vehicle whose ECM applies one (its transfer ``excludes: [baro_gain]``):
    #: :func:`voltage_to_afr` then applies it and refuses a sample without baro.
    baro_gain: AfSensorBaroGain | None = None
    #: B06.7 — every other term of the ECM's chain the transfer excludes, with
    #: its bounds: what a voltage-route residual against PID 0x24 may contain.
    voltage_residual_sources: list[VoltageResidualSource] = Field(default_factory=list)
    target_maps: list[TargetAfrMap] = Field(default_factory=list)
    #: B05.8 — ``None`` until the bank-2 wide-range PID is verified; until
    #: then ``LAMBDA_BANK_BALANCE_WIDEBAND`` is never produced.
    wideband_bank_balance: WidebandPairVerification | None = None

    @model_validator(mode="after")
    def _voltage_chain_accounted(self) -> AfrCalibration:
        """Every term the transfer leaves out is applied or declared (B06.7)."""
        transfer = self.voltage_transfer
        declared = [source.name for source in self.voltage_residual_sources]
        if len(set(declared)) != len(declared):
            raise ValueError("voltage_residual_sources names a term twice")
        if transfer is None:
            if self.baro_gain is not None or declared:
                raise ValueError(
                    "baro_gain and voltage_residual_sources describe a voltage transfer, "
                    "and this calibration has none"
                )
            return self
        excluded = set(transfer.excludes)
        if BARO_GAIN_TERM in excluded and self.baro_gain is None:
            raise ValueError(
                "the voltage transfer excludes the ECM's barometric gain, so baro_gain is "
                "required: without it every voltage-route AFR is the gain-1 reading (on the "
                "Xterra lean of PID 0x24 by ~1.5-3.9 % rich of stoich at 81 kPa; B06.7)"
            )
        if BARO_GAIN_TERM not in excluded and self.baro_gain is not None:
            raise ValueError(
                "baro_gain is declared, but the voltage transfer does not name baro_gain in "
                "its provenance excludes: say what the curve leaves out"
            )
        missing = sorted(excluded - {BARO_GAIN_TERM} - set(declared))
        extra = sorted(set(declared) - excluded)
        if missing or extra:
            raise ValueError(
                "voltage_residual_sources must declare exactly the terms the transfer "
                f"excludes besides baro_gain: missing {missing}, not excluded {extra}"
            )
        return self

    def residual_source(self, name: str) -> VoltageResidualSource:
        for source in self.voltage_residual_sources:
            if source.name == name:
                return source
        raise KeyError(f"no voltage residual source named {name!r}")

    def commanded_afr(self, value: float) -> float:
        """Convert a raw PID 0x44 reading to commanded AFR, in :attr:`afr_scale`.

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
            return self.afr_scale / value
        return self.afr_scale * value

    def commanded_afr_series(self, ratio: pd.Series) -> pd.Series:
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
        self.commanded_afr(probe)
        valid = numeric.where(numeric > 0)
        if self.commanded_equiv_ratio_convention == "phi":
            return self.afr_scale / valid
        return self.afr_scale * valid

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
    return AfrCalibration.model_validate(load_yaml(path.read_text()))


#: B06.7 — the voltage route's two barometric refusals, as row states and as
#: the stable prefixes of their reasons. ``baro_missing``: no barometric read at
#: or before the sample. ``baro_stale``: the newest one is older than the
#: declared ``baro_gain.max_age_s`` (or its age cannot be shown). Neither
#: falls back to gain 1: that is sea level, and the truck lives at ~80 kPa.
BARO_MISSING = "baro_missing"
BARO_STALE = "baro_stale"


@dataclass(frozen=True)
class BaroSamples:
    """Barometric pressure as of each voltage sample: the newest read at or
    before it, and how old that read is (B06.7).

    Built by its constructors, which never look ahead: ``kpa`` is the
    channel's reading as reported (the conversion applies its rounding),
    ``NaN`` where nothing was read at or before the sample; ``age_s`` is
    ``NaN`` where the age cannot be shown.
    """

    kpa: np.ndarray
    age_s: np.ndarray

    def __post_init__(self) -> None:
        if len(self.kpa) != len(self.age_s):
            raise ValueError("BaroSamples kpa and age_s must be the same length")

    def __len__(self) -> int:
        return len(self.kpa)

    @classmethod
    def unobserved(cls, n: int) -> BaroSamples:
        """No barometric read at all: every sample is ``baro_missing``."""
        return cls(np.full(n, np.nan), np.full(n, np.nan))

    @classmethod
    def as_of(
        cls, sample_times_s: np.ndarray, baro_times_s: np.ndarray, baro_kpa: np.ndarray
    ) -> BaroSamples:
        """Each sample's newest baro read at or before it (float seconds, one clock)."""
        samples = np.asarray(sample_times_s, dtype=float)
        times = np.asarray(baro_times_s, dtype=float)
        values = np.asarray(baro_kpa, dtype=float)
        keep = np.isfinite(times) & np.isfinite(values)
        times, values = times[keep], values[keep]
        if not len(times):
            return cls.unobserved(len(samples))
        order = np.argsort(times, kind="stable")
        times, values = times[order], values[order]
        index = np.searchsorted(times, samples, side="right") - 1
        found = (index >= 0) & np.isfinite(samples)
        kpa = np.full(len(samples), np.nan)
        age = np.full(len(samples), np.nan)
        kpa[found] = values[index[found]]
        age[found] = samples[found] - times[index[found]]
        return cls(kpa, age)

    @classmethod
    def from_long(cls, sample_times_s: np.ndarray, baro: pd.DataFrame | None) -> BaroSamples:
        """:meth:`as_of` from a long frame (``timestamp``, ``value``)."""
        if baro is None or baro.empty:
            return cls.unobserved(len(sample_times_s))
        times = to_seconds(pd.to_datetime(baro["timestamp"], utc=True))
        values = pd.to_numeric(baro["value"], errors="coerce").to_numpy(dtype=float)
        return cls.as_of(sample_times_s, times, values)

    @classmethod
    def from_wide(cls, frame: pd.DataFrame, column: str) -> BaroSamples:
        """Per row of a wide (resampled or live) frame.

        The cell holds the bounded forward-fill; its age is the time since the
        row on which a read last *arrived* (``<column>__arrived``, A03.1), per
        session. A cell with no arrival before it in the frame, or a frame
        without the flag, has no provable age: ``baro_stale``, not a guess.
        """
        n = len(frame)
        if column not in frame or "timestamp" not in frame:
            return cls.unobserved(n)
        kpa = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
        flag = arrived_column(column)
        if flag not in frame:
            return cls(kpa, np.full(n, np.nan))
        times = to_seconds(pd.to_datetime(frame["timestamp"], utc=True))
        arrived = frame[flag].fillna(False).astype(bool).to_numpy() & np.isfinite(kpa)
        arrival = pd.Series(np.where(arrived, times, np.nan), index=frame.index)
        if SESSION_COLUMN in frame:
            last = arrival.groupby(frame[SESSION_COLUMN], sort=False).ffill()
        else:
            last = arrival.ffill()
        return cls(kpa, times - last.to_numpy(dtype=float))


@dataclass(frozen=True)
class VoltageAfr:
    """The voltage route's AFR per sample, and why a sample has none (B06.7)."""

    #: AFR with the ECM's barometric gain applied; ``NaN`` on a refused sample
    #: and on a voltage outside the transfer's domain.
    afr: pd.Series
    #: The gain applied (1.0 inside the ECM's skip window); ``NaN`` where refused.
    gain: pd.Series
    baro_missing: pd.Series
    baro_stale: pd.Series

    @property
    def refused(self) -> pd.Series:
        return self.baro_missing | self.baro_stale

    def refusals(self) -> dict[str, int]:
        """Rows per barometric refusal (zeros included)."""
        return {
            BARO_MISSING: int(self.baro_missing.sum()),
            BARO_STALE: int(self.baro_stale.sum()),
        }


def voltage_to_afr(
    voltage: pd.Series, calibration: AfrCalibration, *, baro: BaroSamples | None
) -> VoltageAfr:
    """A/F sensor voltage to AFR through the vehicle's declared chain (B06.7).

    Where the vehicle declares an ECM barometric gain (``calibration.baro_gain``,
    its transfer ``excludes: [baro_gain]``): ``x = voltage / volts_per_count``,
    the gain about stoich from ``baro`` as of each sample, then the transfer
    curve. ``baro`` is keyword-required: with a declared gain, ``None`` raises,
    and a sample whose baro is missing or older than ``baro_gain.max_age_s``
    gets no AFR and is counted under its refusal, never converted at gain 1.
    Without a declared gain the curve applies directly and ``baro`` must be
    ``None``. The curve clamps at its end points, as an ECM's table lookup does.

    What the ECM's own lambda still carries and this does not is
    ``calibration.voltage_residual_sources``.
    """
    transfer = calibration.voltage_transfer
    if transfer is None:
        raise ValueError("voltage_to_afr needs the calibration's voltage_transfer")
    gain_table = calibration.baro_gain
    volts = pd.to_numeric(voltage, errors="coerce").to_numpy(dtype=float)
    index = voltage.index
    valid = np.isfinite(volts) & (volts >= 0.0)
    if gain_table is None:
        if baro is not None:
            raise ValueError("this vehicle declares no barometric gain; pass baro=None")
        afr = np.where(valid, transfer.curve_afr(np.nan_to_num(volts)), np.nan)
        none = pd.Series(False, index=index, dtype=bool)
        return VoltageAfr(
            afr=pd.Series(afr, index=index, dtype=float),
            gain=pd.Series(np.where(valid, 1.0, np.nan), index=index, dtype=float),
            baro_missing=none,
            baro_stale=none.copy(),
        )
    if baro is None:
        raise ValueError(
            "this vehicle's ECM applies a barometric gain to the A/F voltage; "
            "voltage_to_afr needs baro (BaroSamples) for every sample"
        )
    if len(baro) != len(voltage):
        raise ValueError(f"baro has {len(baro)} samples for {len(voltage)} voltage samples")
    missing = ~np.isfinite(baro.kpa)
    with np.errstate(invalid="ignore"):
        fresh = baro.age_s <= gain_table.max_age_s
    stale = ~missing & ~fresh
    usable = ~missing & ~stale
    gain = np.full(len(volts), np.nan)
    gain[usable] = gain_table.gain(baro.kpa[usable])
    per_count = transfer.volts_per_count
    assert per_count is not None  # the provenance validator requires it with the gain
    stoich = float(gain_table.stoich_x2_counts)
    corrected = np.maximum(0.0, stoich + (volts / per_count - stoich) * gain) * per_count
    afr = np.where(usable & valid, transfer.curve_afr(np.nan_to_num(corrected)), np.nan)
    return VoltageAfr(
        afr=pd.Series(afr, index=index, dtype=float),
        gain=pd.Series(gain, index=index, dtype=float),
        baro_missing=pd.Series(missing, index=index, dtype=bool),
        baro_stale=pd.Series(stale, index=index, dtype=bool),
    )


def voltage_route_refusal(calibration: AfrCalibration | None) -> str | None:
    """Why no voltage-route AFR can be produced for this calibration, or ``None``."""
    if calibration is None or calibration.voltage_transfer is None:
        return "voltage-to-AFR transfer is unavailable"
    return None


class LoadAxisUnavailableError(ReasonedRefusal, ValueError):
    """The target map's load axis has no verified channel in its unit (P02.13).

    ``reason`` starts with :data:`LOAD_AXIS_UNVERIFIED`; it is the text the
    feature policy records for :data:`TARGET_AFR_CHANNELS`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason, reason)

    @staticmethod
    def render(reason: str, detail: str) -> str:
        return reason


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

    B06.7 — where measured AFR falls back to the A/F sensor voltage, each row
    is converted at the ECM's barometric gain from the baro read as of that
    row (:meth:`BaroSamples.from_wide`). A row with no fresh baro has no AFR
    (logged once per process by refusal), never the gain-1 value.
    """
    out = frame.copy()
    # M01.3(a): every AFR here is compared with the calibration's (the voltage
    # route, the target maps), so a wideband's λ becomes AFR at the ECM's
    # scale, never the fuel's stoich (14.08 for E10 against the tables' 14.7).
    # With no calibration there is no ECM scale and nothing in that scale to
    # compare against, so the wideband-only AFR stays the fuel's mass ratio.
    scale = (
        calibration.afr_scale if calibration is not None else fuel_properties(vehicle).afr_stoich
    )
    transfer = calibration.voltage_transfer if calibration else None
    floor = transfer.censored_floor_afr if transfer else 10.0
    ceiling = transfer.censored_ceiling_afr if transfer else 18.0
    # B06.7: the voltage route converts each row at the ECM's barometric gain,
    # from the baro read as of that row; rows without one are refused.
    baro = (
        BaroSamples.from_wide(out, calibration.baro_gain.baro_channel)
        if calibration is not None and calibration.baro_gain is not None
        else None
    )
    for bank in (1, 2):
        measured: pd.Series | None = None
        # The bank's upstream wide-range lambda PID, which only the vehicle
        # can name for bank 2 (its O2 sensor layout decides the sensor number).
        for lambda_column in wideband_lambda_columns(bank):
            if measured is not None or lambda_column not in out:
                continue
            candidate = pd.to_numeric(out[lambda_column], errors="coerce") * scale
            # Presence is not the same as data. Bank-2 lambda is unsupported on
            # some ECUs (it stays in poll sets because that is a fact about one
            # vehicle), leaving an all-NaN column that must not shadow a
            # working voltage DID.
            measured = candidate if candidate.notna().any() else None
        # The vehicle's A/F sensor voltage channel (R05.4: named by the
        # vehicle in src.signals.vehicle_signals, never here).
        for voltage_column in af_sensor_voltage_columns(bank):
            if measured is not None or transfer is None or voltage_column not in out:
                continue
            assert calibration is not None
            converted = voltage_to_afr(out[voltage_column], calibration, baro=baro)
            _log_baro_refusals(f"derived:AFR_B{bank}", converted, calibration)
            measured = converted.afr if converted.afr.notna().any() else None
        if measured is not None:
            out[f"derived:AFR_B{bank}"] = measured
            out[f"derived:AFR_CENSORED_B{bank}"] = measured.le(floor) | measured.ge(ceiling)

    _append_lambda_bank_balances(out, calibration, baro, floor=floor, ceiling=ceiling)

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


def _log_baro_refusals(output: str, converted: VoltageAfr, calibration: AfrCalibration) -> None:
    """Log once per process that a voltage-route channel lost rows to baro (B06.7)."""
    if calibration.baro_gain is None:
        return
    for state, rows in converted.refusals().items():
        if not rows:
            continue
        reason = (
            f"{state}: {output} needs {calibration.baro_gain.baro_channel} as of each row "
            f"(within {calibration.baro_gain.max_age_s:g} s) for the ECM's barometric gain"
        )
        if reason not in _logged_refusals:
            _logged_refusals.add(reason)
            log.warning("%s withheld on %d rows: %s", output, rows, reason)


def _append_lambda_bank_balances(
    out: pd.DataFrame,
    calibration: AfrCalibration | None,
    baro: BaroSamples | None,
    *,
    floor: float,
    ceiling: float,
) -> None:
    """Append the per-instrument-pair λ bank balances (B05.8), in place.

    Each bank's reading becomes λ on its own before the difference, and a
    censored reading (at the transfer's floor or the fuel-cut ceiling) is no
    measurement, so the balance is missing there rather than a rail-to-value
    difference. Every input's freshness feeds the output's ``__gap``: for the
    A/F voltage pair that includes the barometric pressure both banks are
    corrected with (B06.7); without a baro column there is no balance.
    """
    pairs: list[tuple[str, tuple[tuple[str, ...], ...]]] = []
    if calibration is not None and calibration.voltage_transfer is not None:
        baro_group = (
            ((calibration.baro_gain.baro_channel,),) if calibration.baro_gain is not None else ()
        )
        pairs.append(
            (
                LAMBDA_BANK_BALANCE_AF_VOLTAGE,
                (af_sensor_voltage_columns(1), af_sensor_voltage_columns(2), *baro_group),
            )
        )
    if calibration is not None and calibration.wideband_bank_balance is not None:
        pairs.append(
            (LAMBDA_BANK_BALANCE_WIDEBAND, (wideband_lambda_columns(1), wideband_lambda_columns(2)))
        )
    if not pairs:
        return
    assert calibration is not None
    # M01.3(a): the voltage route's AFR is in the ECM's scale, so it divides
    # back to λ by that scale; the configured fuel's stoich is not it.
    scale = calibration.afr_scale
    for output, groups in pairs:
        selected = [_selected_input(out, group) for group in groups]
        if any(column is None for column in selected):
            continue
        columns = [str(column) for column in selected[:2]]
        lambdas = []
        for column in columns:
            if output == LAMBDA_BANK_BALANCE_AF_VOLTAGE:
                assert calibration is not None
                converted = voltage_to_afr(out[column], calibration, baro=baro)
                _log_baro_refusals(output, converted, calibration)
                afr = converted.afr
            else:
                afr = pd.to_numeric(out[column], errors="coerce") * scale
            lambdas.append((afr / scale).where((afr > floor) & (afr < ceiling)))
        out[output] = (lambdas[0] - lambdas[1]).astype(float)
        _append_dependency_gap(out, output, *groups)


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
    # The one bin rule (R24.6): ``[lo, hi)`` cells, the last closed. This used
    # ``pd.cut(include_lowest=True)``, under which a sample on an interior
    # breakpoint fell in the cell below it, unlike every other surface.
    rpm_edges = [float(v) for v in target.rpm_breakpoints]
    load_edges = [float(v) for v in target.load_breakpoints]
    clean["rpm_bin"] = cell_codes(clean["obd:RPM"], rpm_edges, out_of_range="drop")
    clean["load_bin"] = cell_codes(clean[load_column], load_edges, out_of_range="drop")
    clean = clean.loc[(clean["rpm_bin"] >= 0) & (clean["load_bin"] >= 0)]
    rows: list[dict[str, Any]] = []
    grouped = clean.groupby(["rpm_bin", "load_bin"], sort=True)[error_column]
    for (rpm_bin, load_bin), values in grouped:
        r, c = int(cast(int, rpm_bin)), int(cast(int, load_bin))
        numeric = pd.to_numeric(values, errors="coerce").dropna()
        count = int(len(numeric))
        rows.append(
            {
                "bank": bank,
                "rpm_low": rpm_edges[r],
                "rpm_high": rpm_edges[r + 1],
                "load_low": load_edges[c],
                "load_high": load_edges[c + 1],
                "median_error_afr": float(numeric.median()),
                "iqr_error_afr": float(numeric.quantile(0.75) - numeric.quantile(0.25)),
                "count": count,
                "sparse": count < min_samples,
            }
        )
    return rows


__all__ = [
    "AFR_ERROR_CHANNELS",
    "BARO_MISSING",
    "BARO_STALE",
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
    "BARO_GAIN_TERM",
    "AfSensorBaroGain",
    "AfrCalibration",
    "AxisEdgeClamp",
    "AxisOutOfRange",
    "BaroSamples",
    "ChannelUnits",
    "EcmTable1D",
    "FlashHistory",
    "LoadAxisUnavailableError",
    "NoFlashHistory",
    "RecordedClockFlashHistory",
    "TargetAfrLookup",
    "TargetAfrMap",
    "TargetMapProvenance",
    "TargetRevisionRefusal",
    "VoltageAfr",
    "VoltageAfrTransfer",
    "VoltageResidualSource",
    "VoltageTransferProvenance",
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
    "voltage_route_refusal",
    "voltage_to_afr",
]
