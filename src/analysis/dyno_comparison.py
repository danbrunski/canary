"""Run-to-run repeatability before claims: the noise floor and run comparison (H07.8).

A dyno result is only a difference if it exceeds the same-day, same-ROM spread
of pull pairs. :class:`RunConditions` is the set of conditions every dyno
result carries; :class:`DynoCurve` one quantity against rpm;
:func:`record_dyno_floor` measures a :class:`DynoNoiseFloor` and
:func:`compare_runs` refuses to call two runs different inside it.

Depends only on :mod:`src.analysis.dyno_contract` and the noise-floor sigma
threshold. Moved verbatim out of ``dyno.py`` (R16); ``_grid`` is the public
``rpm_grid`` because the power computation resamples on the same grid.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
from pydantic import Field, model_validator

from src.analysis.dyno_contract import DynoRefusal, FloatArray, StrictModel, as_float_array
from src.analysis.noise_floor import SIGMA_THRESHOLD

#: A noise floor needs at least this many pull pairs (H07.8).
MIN_PULL_PAIRS = 3

#: Rpm grid step for resampling dyno curves before comparing them.
CURVE_GRID_STEP_RPM = 50.0


Quantity = Literal["wheel_power_kw", "wheel_torque_nm"]
QUANTITY_UNITS: dict[str, str] = {"wheel_power_kw": "kW", "wheel_torque_nm": "N m"}

#: The fields a floor must share with a comparison for it to apply: the same
#: gear and road, and the same induction (an NA floor says nothing about a
#: boosted engine's spread).
FLOOR_MATCH_FIELDS: tuple[str, ...] = ("gear", "road", "induction")

#: Additionally shared by every pull pair a floor is recorded from.
FLOOR_SAME_DAY_FIELDS: tuple[str, ...] = (*FLOOR_MATCH_FIELDS, "tune_revision", "date")


class RunConditions(StrictModel):
    """The conditions every dyno result carries (H07 rules). Required, all of them."""

    gear: int = Field(ge=1)
    #: ``pair`` is the average of both directions on one road; ``single`` is not.
    direction: Literal["pair", "single"]
    tune_revision: str = Field(min_length=1)
    induction: Literal["na", "boosted"]
    baro_kpa: float = Field(gt=0.0)
    intake_temp_c: float
    ambient_temp_c: float
    road: str = Field(min_length=1)
    date: dt.date
    payload_kg: float = Field(ge=0.0)
    sessions: list[str] = Field(min_length=1)


@dataclass(frozen=True)
class DynoCurve:
    """One quantity against engine rpm for one run (a pull pair, normally)."""

    quantity: Quantity
    rpm: FloatArray
    value: FloatArray
    conditions: RunConditions

    def __post_init__(self) -> None:
        rpm = as_float_array("rpm", self.rpm)
        value = as_float_array("value", self.value)
        if len(rpm) != len(value):
            raise ValueError(f"rpm has {len(rpm)} points, value {len(value)}")
        if len(rpm) < 2 or np.any(np.diff(rpm) <= 0):
            raise ValueError("a dyno curve needs >= 2 points in strictly increasing rpm")
        object.__setattr__(self, "rpm", rpm)
        object.__setattr__(self, "value", value)


class FloorConditions(StrictModel):
    """The regime a floor applies to, and what it was measured under."""

    gear: int = Field(ge=1)
    road: str = Field(min_length=1)
    induction: Literal["na", "boosted"]
    tune_revision: str = Field(min_length=1)
    date: dt.date
    baro_kpa_range: tuple[float, float]
    intake_temp_c_range: tuple[float, float]
    ambient_temp_c_range: tuple[float, float]


class FloorBand(StrictModel):
    rpm_min: float
    rpm_max: float
    #: Spread of one pull pair about the same-day mean, per rpm point, pooled
    #: over the band (RMS of the per-point sample sd).
    sd: float = Field(gt=0.0)
    n_points: int = Field(ge=2)

    @model_validator(mode="after")
    def _ordered(self) -> FloorBand:
        if self.rpm_max <= self.rpm_min:
            raise ValueError(f"band {self.rpm_min}-{self.rpm_max} rpm is empty")
        return self

    @property
    def sd_of_difference(self) -> float:
        """Spread of the difference between two runs, each a single pair."""
        return math.sqrt(2.0) * self.sd


class DynoNoiseFloor(StrictModel):
    """Same-day, same-ROM run-to-run spread per rpm band (H07.8)."""

    quantity: Quantity
    unit: str = Field(min_length=1)
    conditions: FloorConditions
    n_pull_pairs: int = Field(ge=MIN_PULL_PAIRS)
    bands: list[FloorBand] = Field(min_length=1)
    note: str = ""

    def matches(self, quantity: str, conditions: RunConditions) -> bool:
        return self.quantity == quantity and all(
            getattr(self.conditions, key) == getattr(conditions, key) for key in FLOOR_MATCH_FIELDS
        )


def rpm_grid(lo: float, hi: float, step: float) -> FloatArray:
    start = math.ceil(lo / step) * step
    return np.arange(start, hi + 1e-9, step, dtype=np.float64)


def _span(values: Sequence[float]) -> tuple[float, float]:
    return (float(min(values)), float(max(values)))


def record_dyno_floor(
    pairs: Sequence[DynoCurve],
    *,
    band_edges_rpm: Sequence[float],
    grid_step_rpm: float = CURVE_GRID_STEP_RPM,
    note: str = "",
) -> DynoNoiseFloor | DynoRefusal:
    """Measure the noise floor from same-day, same-ROM pull pairs (H07.8).

    Each curve is resampled onto one rpm grid over the band all of them
    cover; the per-point sample sd across pairs is pooled per band. The
    stored ``sd`` is one pair's spread; :attr:`FloorBand.sd_of_difference`
    is what a comparison of two pairs is judged against.
    """
    name = "noise_floor"
    if len(pairs) < MIN_PULL_PAIRS:
        return DynoRefusal(
            name,
            "too_few_pull_pairs",
            f"{len(pairs)} pull pairs; a floor needs at least {MIN_PULL_PAIRS}",
        )
    singles = [i for i, p in enumerate(pairs) if p.conditions.direction != "pair"]
    if singles:
        return DynoRefusal(
            name,
            "direction_mismatch",
            f"runs {singles} are single-direction; a floor is measured on pull pairs",
        )
    quantities = {p.quantity for p in pairs}
    if len(quantities) != 1:
        return DynoRefusal(name, "invalid_input", f"mixed quantities {sorted(quantities)}")
    first = pairs[0].conditions
    for key in FLOOR_SAME_DAY_FIELDS:
        values = {str(getattr(p.conditions, key)) for p in pairs}
        if len(values) != 1:
            return DynoRefusal(
                name,
                "not_same_day_same_rom",
                f"the pull pairs differ in {key}: {sorted(values)}; a noise floor is one day, "
                "one ROM, one gear, one road",
            )
    edges = sorted(float(e) for e in band_edges_rpm)
    if len(edges) < 2:
        return DynoRefusal(name, "invalid_input", "band_edges_rpm needs at least two edges")
    lo = max(float(p.rpm[0]) for p in pairs)
    hi = min(float(p.rpm[-1]) for p in pairs)
    grid = rpm_grid(lo, hi, grid_step_rpm) if hi > lo else np.empty(0)
    matrix = np.vstack([np.interp(grid, p.rpm, p.value) for p in pairs])
    point_var = matrix.var(axis=0, ddof=1) if len(grid) else np.empty(0)
    bands: list[FloorBand] = []
    for band_lo, band_hi in zip(edges[:-1], edges[1:], strict=True):
        inside = (grid >= band_lo) & (grid < band_hi)
        if int(inside.sum()) < 2:
            continue
        sd = math.sqrt(float(point_var[inside].mean()))
        scale = float(np.abs(matrix[:, inside]).max())
        if sd <= 1e-9 * max(scale, 1.0):
            return DynoRefusal(
                name,
                "no_spread",
                f"the pull pairs are identical over {band_lo:.0f}-{band_hi:.0f} rpm; "
                "the same pull recorded twice is not a null run",
            )
        bands.append(FloorBand(rpm_min=band_lo, rpm_max=band_hi, sd=sd, n_points=int(inside.sum())))
    if not bands:
        return DynoRefusal(
            name,
            "no_overlap",
            f"the pull pairs share {lo:.0f}-{hi:.0f} rpm, which fills no band of "
            f"{edges} with two grid points",
        )
    quantity = pairs[0].quantity
    return DynoNoiseFloor(
        quantity=quantity,
        unit=QUANTITY_UNITS[quantity],
        conditions=FloorConditions(
            gear=first.gear,
            road=first.road,
            induction=first.induction,
            tune_revision=first.tune_revision,
            date=first.date,
            baro_kpa_range=_span([p.conditions.baro_kpa for p in pairs]),
            intake_temp_c_range=_span([p.conditions.intake_temp_c for p in pairs]),
            ambient_temp_c_range=_span([p.conditions.ambient_temp_c for p in pairs]),
        ),
        n_pull_pairs=len(pairs),
        bands=bands,
        note=note,
    )


BandVerdictKind = Literal["differs", "within_floor", "not_covered"]


@dataclass(frozen=True)
class BandVerdict:
    rpm_min: float
    rpm_max: float
    verdict: BandVerdictKind
    #: Mean of the per-rpm-point differences (b − a) in the band.
    delta: float | None = None
    sd_of_difference: float | None = None
    sigma: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rpm_min": self.rpm_min,
            "rpm_max": self.rpm_max,
            "verdict": self.verdict,
            "delta": self.delta,
            "sd_of_difference": self.sd_of_difference,
            "sigma": self.sigma,
        }


@dataclass(frozen=True)
class RunComparison:
    quantity: Quantity
    unit: str
    floor_conditions: FloorConditions
    sigma_threshold: float
    bands: tuple[BandVerdict, ...] = field(default_factory=tuple)

    @property
    def differs(self) -> bool:
        """True only when at least one band clears the floor."""
        return any(band.verdict == "differs" for band in self.bands)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": True,
            "quantity": self.quantity,
            "unit": self.unit,
            "floor_conditions": self.floor_conditions.model_dump(mode="json"),
            "sigma_threshold": self.sigma_threshold,
            "differs": self.differs,
            "bands": [band.as_dict() for band in self.bands],
        }


def compare_runs(
    run_a: DynoCurve,
    run_b: DynoCurve,
    floor: DynoNoiseFloor | None,
    *,
    sigma_threshold: float = SIGMA_THRESHOLD,
    grid_step_rpm: float = CURVE_GRID_STEP_RPM,
) -> RunComparison | DynoRefusal:
    """Say, band by band, whether ``run_b`` differs from ``run_a`` beyond the floor.

    The two curves are paired at the same rpm on one grid; a band's delta is
    the mean of those per-point differences, judged against the floor's
    per-point spread of a difference (conservative: averaging correlated
    points does not shrink the noise much). A band ``differs`` only at
    ``|delta| >= sigma_threshold × sd_of_difference``; below it the verdict is
    ``within_floor``, never "smaller effect". Refuses without a floor, when
    the floor was measured in another gear, road, induction or quantity, and
    when either run is single-direction.
    """
    name = "compare_runs"
    if floor is None:
        return DynoRefusal(
            name,
            "no_floor",
            "no dyno noise floor has been measured for this regime; record three same-day "
            "pull pairs first (H07.8)",
        )
    for label, run in (("run_a", run_a), ("run_b", run_b)):
        if run.quantity != floor.quantity:
            return DynoRefusal(
                name,
                "conditions_mismatch",
                f"{label} is {run.quantity}; the floor is {floor.quantity}",
            )
        for key in FLOOR_MATCH_FIELDS:
            ours, theirs = getattr(run.conditions, key), getattr(floor.conditions, key)
            if ours != theirs:
                return DynoRefusal(
                    name,
                    "conditions_mismatch",
                    f"{label} has {key} {ours!r}; the floor was measured with {key} "
                    f"{theirs!r}. A floor is only valid for its own regime.",
                )
        if run.conditions.direction != "pair":
            return DynoRefusal(
                name,
                "direction_mismatch",
                f"{label} is single-direction; the floor is the spread of pull pairs, and "
                "one direction carries grade and wind error it does not include",
            )
    lo = max(float(run_a.rpm[0]), float(run_b.rpm[0]))
    hi = min(float(run_a.rpm[-1]), float(run_b.rpm[-1]))
    grid = rpm_grid(lo, hi, grid_step_rpm) if hi > lo else np.empty(0)
    delta = np.interp(grid, run_b.rpm, run_b.value) - np.interp(grid, run_a.rpm, run_a.value)
    verdicts: list[BandVerdict] = []
    for band in floor.bands:
        inside = (grid >= band.rpm_min) & (grid < band.rpm_max)
        if not inside.any():
            verdicts.append(BandVerdict(band.rpm_min, band.rpm_max, "not_covered"))
            continue
        band_delta = float(delta[inside].mean())
        sigma = band_delta / band.sd_of_difference
        verdicts.append(
            BandVerdict(
                band.rpm_min,
                band.rpm_max,
                "differs" if abs(sigma) >= sigma_threshold else "within_floor",
                delta=band_delta,
                sd_of_difference=band.sd_of_difference,
                sigma=sigma,
            )
        )
    if all(v.verdict == "not_covered" for v in verdicts):
        return DynoRefusal(
            name,
            "no_overlap",
            f"the runs share {lo:.0f}-{hi:.0f} rpm, which touches no band of the floor",
        )
    return RunComparison(
        quantity=floor.quantity,
        unit=floor.unit,
        floor_conditions=floor.conditions,
        sigma_threshold=sigma_threshold,
        bands=tuple(verdicts),
    )
