"""The first engine fuel-response plant: mean-value fuel and mixture (E07.3).

The engine side of E07's controller/plant split (``sim_contract``). Given the
injector command the ECM actually issued, the airflow and speed it ran at and
the fuel-system state, it computes air and fuel mass per cylinder event, the
delivered equivalence ratio, and what the wideband sensor would report. It
never reads the ECM's target: delivered lambda comes from air and fuel mass,
and a commanded phi/AFR is refused as a plant input
(:class:`FuelPlantProfile` ``command_specs``), so agreement cannot be
manufactured by copying the objective into the measurement.

Physics (continuous, SI-ish units noted per function):

* air per cylinder event ``m_a = MAF / (rpm/60 x cylinders x 2/strokes)``;
* injector differential pressure ``dP = rail_gauge + baro - MAP`` (the gauge
  reads rail minus atmosphere, whatever the regulator references);
* fuel per event ``m_f = s x (dP / dP_ref)^0.5 x max(0, Ti - Td) / AFR_s``,
  where ``s`` is grams of *stoichiometric air* per millisecond of effective
  open time at ``dP_ref`` (orifice law);
* ``lambda = m_a / (s x f(dP) x Te)``; the sensor reports it ``delay`` later.

**Identifiability, as a schema.** From MAF, pulse and lambda alone only the
*product* ``s = injector_flow x AFR_s x lambda_gain / maf_gain`` can be fitted:
a MAF that reads 5 % low and injectors that flow 5 % less give the same logs.
So the registry declares ``s`` as a lumped ``log_fit`` parameter with its
components, and refuses (:meth:`ParameterRegistry._lumped_components`) any
component that claims to be fitted from logs while another component is not
independently known — "MAF bias absorbed into injector flow, both called
measured" cannot be written down.

**Unresolved effects.** A parameter that ``requires_measurement`` carries no
value. If the plant needs it, it carries a ``working_assumption`` with a bracket
and an effect name; every result lists the effects its numbers rest on, and
:func:`sensitivity` re-runs at the bracket ends. Rail pressure is measured
where ``sensor:FUEL_PRESSURE`` was logged and an assumption elsewhere, per
sample.

**Freezing.** Identifiable values live only in a :class:`BaselineParameterSet`,
which is hashed and must be ``frozen`` before any evaluation
(:func:`resolve_parameters` refuses otherwise) — the hardware parameter set is
fixed before another ROM, or a held-out drive, is looked at.

Vehicle facts live in the profile's ``fuel_plant.yaml``; this module is generic.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.refusal import ReasonedRefusal
from src.yaml_load import load_yaml

PLANT_FILE = "fuel_plant.yaml"
#: Specific gas constant of dry air, J/(kg K).
R_AIR = 287.05
#: 0 degC in K.
KELVIN = 273.15

Status = Literal["known", "identifiable", "requires_measurement"]
#: Where a parameter's number comes from. ``log_fit`` is the only kind that
#: is identified from driving logs, and the only one a confound can hide in.
SourceKind = Literal["profile_fact", "datasheet", "firmware", "physics", "measurement", "log_fit"]
_INDEPENDENT_SOURCES: frozenset[str] = frozenset(
    {"profile_fact", "datasheet", "firmware", "physics", "measurement"}
)

#: The parameters the plant reads. A profile missing one is refused by name.
PLANT_READS: tuple[str, ...] = (
    "stoich_air_per_open_ms",
    "reference_delta_p",
    "injector_flow_exponent",
    "injector_dead_time",
    "rail_pressure",
    "manifold_pressure",
    "bank_air_split",
    "fuel_stoich_afr",
    "lambda_observation_delay",
)

#: What a ``speed_density_load_dependent`` plant reads on top (the ideal-cycle
#: residual-gas factor, :func:`part_load_breathing_factor`).
LOAD_DEPENDENT_READS: tuple[str, ...] = (
    "compression_ratio",
    "residual_expansion_exponent",
    "exhaust_pressure_ratio",
)
#: The registry parameter that is the plant's working VE (the floor of a
#: VE(rpm) curve), and the one that restates B06's frozen lambda delay.
VE_PARAMETER = "manifold_pressure"
DELAY_PARAMETER = "lambda_observation_delay"
#: The parameters only the manifold model reads: a stated (or measured) MAP
#: reads none of them, so a result on stated MAP rests on none of them.
MANIFOLD_READS: tuple[str, ...] = (VE_PARAMETER, *LOAD_DEPENDENT_READS)

FloatArray = NDArray[np.float64]

#: Quantities another plant stage owns (E07.5's contract) that this plant
#: computes for itself: MAP by steady speed density, in every plant version.
#: The contract declares it as ``fuel_delivery.internal_models`` (one entry
#: per model form, naming its plant versions); ``engine_dynamics.
#: check_internal_models`` holds the two lists equal and checks each version's
#: profile against the invariants its entry declares (:data:`MANIFOLD_MODELS`).
COMPUTES_INTERNALLY: tuple[str, ...] = ("manifold_pressure",)

#: How a plant version computes MAP, and the owner's invariants each form holds.
#:
#: * ``speed_density_capped`` (fuel-mv-1, frozen): one working VE, MAP capped
#:   at baro where the steady manifold would exceed it -- silently, so it holds
#:   no invariant of the ``manifold_breathing`` stage.
#: * ``speed_density_refusing`` (E07.5 reconciliation): VE(rpm) from the
#:   baseline's fitted curve, and a sample whose steady MAP would exceed baro is
#:   NaN with ``manifold_above_ambient`` -- the stage's own refusal, never a cap.
#: * ``speed_density_load_dependent`` (fuel-mv-3): the same fitted VE(rpm) as
#:   the full-breathing value, scaled at part load by the ideal-cycle residual
#:   gas factor (:func:`part_load_breathing_factor`), so VE falls with manifold
#:   pressure; MAP solves ``charge fraction = VE(rpm, MAP) x MAP / baro`` and
#:   refuses as ``speed_density_refusing`` does.
ManifoldModel = Literal[
    "speed_density_capped", "speed_density_refusing", "speed_density_load_dependent"
]
MANIFOLD_MODELS: dict[str, tuple[str, ...]] = {
    "speed_density_capped": (),
    "speed_density_refusing": ("manifold_at_or_below_ambient",),
    "speed_density_load_dependent": ("manifold_at_or_below_ambient",),
}
#: Forms that run a fitted VE(rpm) and refuse MAP above ambient per sample.
REFUSING_MODELS: frozenset[str] = frozenset(
    {"speed_density_refusing", "speed_density_load_dependent"}
)


def plant_reads(manifold_model: str) -> tuple[str, ...]:
    """The registry parameters one manifold form reads."""
    if manifold_model == "speed_density_load_dependent":
        return PLANT_READS + LOAD_DEPENDENT_READS
    return PLANT_READS


#: Per-sample MAP refusals of ``speed_density_refusing``, by name.
MAP_ABOVE_AMBIENT = "manifold_above_ambient"
MAP_OUTSIDE_VE_FIT = "outside_ve_fit"
#: Relative rounding allowed in MAP <= baro: the fitted envelope's own read
#: reproduces baro only to floating-point rounding.
AMBIENT_ROUNDING = 1e-9


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FuelPlantRefusalError(ReasonedRefusal, ValueError):
    """The plant cannot evaluate what it was given; ``reason`` names why."""


# -- parameter registry ------------------------------------------------------------


class WorkingAssumption(_Strict):
    """The number a ``requires_measurement`` parameter is *run* at, never claimed."""

    value: float
    bracket: tuple[float, float]
    #: The unresolved effect every result resting on this assumption names.
    effect: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    basis: str = Field(min_length=1)

    @model_validator(mode="after")
    def _inside(self) -> WorkingAssumption:
        lo, hi = self.bracket
        if not lo <= self.value <= hi:
            raise ValueError(f"working value {self.value} is outside its bracket {self.bracket}")
        return self


class PlantParameter(_Strict):
    """One hardware parameter: its status, where its number comes from, and why."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    units: str
    status: Status
    source: SourceKind | None = None
    evidence: str = Field(min_length=1)
    #: Only a ``known`` parameter holds a value here; an identified one gets
    #: its value from a frozen baseline set.
    value: float | None = None
    #: A ``log_fit`` lumped parameter names the hardware quantities whose
    #: product it is. None of them is thereby identified.
    components: list[str] = Field(default_factory=list)
    #: An independent prior for the lumped value (datasheet arithmetic), kept
    #: beside the fit as a plausibility check, never as a second measurement.
    prior: float | None = None
    working_assumption: WorkingAssumption | None = None

    @model_validator(mode="after")
    def _status(self) -> PlantParameter:
        if self.status == "known":
            if self.value is None or self.source not in _INDEPENDENT_SOURCES:
                raise ValueError(
                    f"{self.id}: a known parameter needs a value and an independent source "
                    f"({sorted(_INDEPENDENT_SOURCES)}), got source {self.source!r}"
                )
            if self.working_assumption is not None:
                raise ValueError(f"{self.id}: a known parameter needs no working assumption")
        if self.status == "requires_measurement":
            if self.value is not None:
                raise ValueError(
                    f"{self.id}: requires_measurement carries no value; run it under a "
                    "working_assumption, which names the effect it leaves unresolved"
                )
            if self.source == "log_fit":
                raise ValueError(f"{self.id}: a log fit is identifiable, not unmeasured")
        if self.status == "identifiable":
            if self.source not in ("log_fit", "measurement"):
                raise ValueError(f"{self.id}: identifiable needs source log_fit or measurement")
            if self.value is not None:
                raise ValueError(
                    f"{self.id}: an identified value belongs in a frozen baseline set, not here"
                )
        if self.components and self.source != "log_fit":
            raise ValueError(f"{self.id}: only a log_fit parameter is a lumped product")
        return self


class ParameterRegistry(_Strict):
    parameters: list[PlantParameter] = Field(min_length=1)

    @model_validator(mode="after")
    def _registry(self) -> ParameterRegistry:
        ids = [p.id for p in self.parameters]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"parameters listed twice: {dupes}")
        missing = [p for p in PLANT_READS if p not in ids]
        if missing:
            raise ValueError(f"the plant reads parameters the registry does not declare: {missing}")
        self._lumped_components()
        return self

    def _lumped_components(self) -> None:
        """Refuse splitting a lumped log fit into 'independently identified' parts.

        A component of a lumped log-fit parameter may itself be identified from
        logs only when every *other* component is independently known;
        otherwise the fit's whole residual — a MAF bias, say — would be
        assigned to that one component and called a measurement of it.
        """
        by_id = {p.id: p for p in self.parameters}
        for lumped in self.parameters:
            if not lumped.components:
                continue
            unknown = [c for c in lumped.components if c not in by_id]
            if unknown:
                raise ValueError(f"{lumped.id}: components not declared: {unknown}")
            for name in lumped.components:
                part = by_id[name]
                if part.source != "log_fit" and part.status != "identifiable":
                    continue
                others = [
                    by_id[o]
                    for o in lumped.components
                    if o != name
                    and not (by_id[o].status == "known" and by_id[o].source in _INDEPENDENT_SOURCES)
                ]
                if others:
                    raise ValueError(
                        f"{name} is a component of the lumped log fit {lumped.id} and is declared "
                        f"identifiable from logs while {[o.id for o in others]} are not "
                        "independently known: the fit cannot tell them apart (a MAF bias would be "
                        "absorbed into injector flow and both called measured)"
                    )

    def get(self, parameter_id: str) -> PlantParameter:
        for p in self.parameters:
            if p.id == parameter_id:
                return p
        raise KeyError(parameter_id)


class VeCurve(_Strict):
    """VE by rpm band, as fitted (``fuel_plant_eval.identify_ve``) and frozen
    in a baseline.

    ``envelope`` is the largest steady warm charge fraction (MAF over the
    ambient fill) any identification read reached in the band: on a naturally
    aspirated engine ``VE >= charge fraction`` on every read, so the envelope
    is the smallest VE the identification drives allow, and equals VE where
    they reached full breathing. ``None``: fewer reads than the rule's
    ``min_reads``. Run at ``max(envelope, floor)``, the floor being the
    registry's working VE, so a band the drives never filled runs at the
    declared assumption, not at a part-throttle read.
    """

    rpm_edges: list[float] = Field(min_length=2)
    envelope: list[float | None]
    n_reads: list[int]
    #: The baro and intake-temperature ranges of the reads the fit used. The
    #: curve is not extrapolated: a read outside either is ``outside_ve_fit``.
    #: Baro, because the intake's pressure losses (inside a VE referenced to
    #: ambient) scale with the pressure ratio and the corpus spans 63-101 kPa.
    #: IAT, because on this kind of intake the sensor tracks airflow (it is
    #: stagnation-heated, not the charge temperature), so the ideal-gas fill
    #: does not divide the charge density out and the fit holds only where
    #: its reads were.
    baro_kpa: tuple[float, float]
    iat_c: tuple[float, float]

    @model_validator(mode="after")
    def _bands(self) -> VeCurve:
        e = self.rpm_edges
        if any(b <= a for a, b in zip(e, e[1:], strict=False)) or e[0] <= 0:
            raise ValueError("rpm_edges are positive and strictly increasing")
        if not len(self.envelope) == len(self.n_reads) == len(e) - 1:
            raise ValueError("one envelope value and one read count per rpm band")
        for lo, hi in (self.baro_kpa, self.iat_c):
            if not lo <= hi:
                raise ValueError(f"empty range [{lo}, {hi}]")
        for v in self.envelope:
            if v is not None and not 0.0 < v <= 1.0:
                raise ValueError(
                    f"envelope {v} is not a naturally aspirated VE: a steady charge fraction "
                    "above 1 says the airflow boundary is wrong, not that VE is"
                )
        return self

    def bounds(self) -> dict[str, tuple[float, float]]:
        """Where the fit holds, keyed by contract quantity (what the contract's
        ``parameter_fits`` must declare, checked by
        ``engine_dynamics.check_internal_models``)."""
        return {
            "engine_speed": (self.rpm_edges[0], self.rpm_edges[-1]),
            "barometric_pressure": (float(self.baro_kpa[0]), float(self.baro_kpa[1])),
            "intake_air_temperature": (float(self.iat_c[0]), float(self.iat_c[1])),
        }

    def ve(self, rpm: ArrayLike, floor: float, *, fitted_at: float) -> FloatArray:
        """VE per sample; NaN outside the fitted rpm range (never extrapolated).

        ``fitted_at`` is the registry's working VE, which decides which bands
        are the floor's (no envelope, or one below it) and which are fitted.
        A fitted band runs at its envelope whatever ``floor`` is, so a floor
        moved inside its bracket (a sensitivity case, a Monte Carlo draw)
        moves only the floor's bands; it never lifts the frozen fit (E08.11).
        Required: a caller that has only a floor must say that the floor is
        also where the bands split."""
        r = _f(rpm)
        edges = np.asarray(self.rpm_edges, dtype=float)
        band = np.clip(np.searchsorted(edges, r, side="right") - 1, 0, len(edges) - 2)
        per_band = np.array(self.band_values(floor, fitted_at=fitted_at))
        inside = (r >= edges[0]) & (r <= edges[-1])
        return np.where(inside, per_band[band], np.nan)

    def floor_bands(self, fitted_at: float) -> list[bool]:
        """Per band, whether it is the floor's: no envelope, or an envelope
        below the registry's working VE ``fitted_at`` (the drives never filled
        it above that). The others are the fit."""
        return [v is None or v < fitted_at for v in self.envelope]

    def band_values(self, floor: float, *, fitted_at: float) -> list[float]:
        """The VE each band runs at: a fitted band at its envelope, a floor
        band at ``floor`` but never below its own envelope (charge fraction <=
        VE on every read of an NA engine)."""
        return [
            floor if v is None else (max(v, floor) if is_floor else v)
            for v, is_floor in zip(self.envelope, self.floor_bands(fitted_at), strict=True)
        ]

    def describe(self, floor: float, *, fitted_at: float) -> str:
        """The bands and the VE each runs at, e.g. ``[500, 1000) 0.7 floor,
        ..., [5000, 6500] 0.872824 fit`` (one string form, for every result
        that names the curve)."""
        e = self.rpm_edges
        parts = []
        for k, (value, is_floor) in enumerate(
            zip(
                self.band_values(floor, fitted_at=fitted_at),
                self.floor_bands(fitted_at),
                strict=True,
            )
        ):
            close = "]" if k == len(e) - 2 else ")"
            parts.append(
                f"[{e[k]:g}, {e[k + 1]:g}{close} {value!r} {'floor' if is_floor else 'fit'}"
            )
        return ", ".join(parts)


#: An ``rpm_quantile`` VE fit's quantile lies above this (a *high* quantile:
#: the envelope estimates where the band's reads stop, M01.16).
MIN_VE_QUANTILE = 0.5


class VeFitRule(_Strict):
    """How a ``speed_density_refusing`` plant fits VE(rpm), declared before the fit.

    * ``rpm_quantile``: per band, the ``quantile`` (Hyndman-Fan type 9,
      ``normal_unbiased``) of the steady warm charge fractions **above the
      registry's working VE** (only those can lift a
      band: :meth:`VeCurve.band_values` runs a band at ``max(envelope,
      floor)``). A fixed quantile of noisy full-breathing reads is biased by a
      fixed multiple of the noise, whatever the count; the maximum of ``n`` is
      biased by about ``sd * sqrt(2 ln n)``, which put a false shape into a curve
      whose bands held 805, 286, 24 and 5 reads (M01.16). A band with fewer
      than ``min_reads`` such reads is refused, with its count. By design a
      q-quantile envelope sits below about ``1 - q`` of the full-breathing
      reads, so it refuses about that share of held-out full-throttle reads
      as ``manifold_above_ambient``.
    * ``rpm_envelope``: the per-band maximum over every steady warm read. Kept
      only to reproduce the curves fuel-mv-2 and fuel-mv-3 froze with it: a
      plant declaring it must carry a frozen baseline (:class:`FuelPlantProfile`),
      so no new fit can be made on the biased rule.
    """

    form: Literal["rpm_envelope", "rpm_quantile"]
    rpm_edges: list[float] = Field(min_length=2)
    #: Steady warm reads a band needs before its envelope counts (for
    #: ``rpm_quantile``: reads above the working VE, at least ``1 / (1 - q)``).
    min_reads: int = Field(ge=1)
    #: ``rpm_quantile`` only: the quantile, in ``(0.5, 1)``.
    quantile: float | None = None
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _quantile_form(self) -> VeFitRule:
        if self.form == "rpm_envelope":
            if self.quantile is not None:
                raise ValueError("rpm_envelope is the band maximum; it takes no quantile")
            return self
        q = self.quantile
        if q is None or not MIN_VE_QUANTILE < q < 1.0:
            raise ValueError(f"rpm_quantile needs a quantile in ({MIN_VE_QUANTILE}, 1), got {q}")
        need = math.ceil(1.0 / (1.0 - q) - 1e-9)
        if self.min_reads < need:
            raise ValueError(
                f"rpm_quantile {q} needs min_reads >= {need} (1 / (1 - q)): with fewer reads "
                "the quantile is set by the top one or two reads, so the read count still "
                "biases it"
            )
        return self


class BaselineParameterSet(_Strict):
    """The identified hardware values, frozen before evaluation.

    ``fitted_on`` names the recordings the values came from; an evaluation on
    any of them is refused as leakage. ``sha256`` is computed, never supplied.
    """

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    frozen: bool
    frozen_on: date
    plant_version: str
    hardware_configuration: str
    fitted_on: list[str] = Field(min_length=1)
    revisions: list[str] = Field(min_length=1)
    values: dict[str, float]
    #: Central interval per value (bootstrap over steady regions).
    intervals: dict[str, tuple[float, float]]
    method: str = Field(min_length=1)
    #: What the fit reported, for the record (counts, spreads).
    fit_summary: dict[str, float | int | str] = Field(default_factory=dict)
    #: The fitted VE(rpm) of a ``speed_density_refusing`` plant. Absent (and
    #: left out of the hash) for every other plant, so fuel-mv-1's sha holds.
    ve_curve: VeCurve | None = None

    @model_validator(mode="after")
    def _intervals(self) -> BaselineParameterSet:
        if set(self.values) != set(self.intervals):
            raise ValueError("every identified value needs its interval and nothing else")
        for key, (lo, hi) in self.intervals.items():
            if not lo <= self.values[key] <= hi:
                raise ValueError(f"{key}: value outside its interval")
        return self

    def sha256(self) -> str:
        data = self.model_dump(mode="json")
        if self.ve_curve is None:
            del data["ve_curve"]
        canonical = json.dumps(data, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()


# -- profile -----------------------------------------------------------------------


class Geometry(_Strict):
    cylinders: int = Field(gt=0)
    displacement_l: float = Field(gt=0)
    strokes: Literal[2, 4] = 4
    banks: int = Field(gt=0)

    @property
    def cylinder_volume_l(self) -> float:
        return self.displacement_l / self.cylinders


class Specs(_Strict):
    """Channel specs the plant replay reads, and the one it is scored against."""

    pulse: str
    rpm: str
    maf: str
    lambda_observed: str
    baro: str
    intake_temp: str
    coolant: str
    battery_voltage: str
    rail_pressure: str
    #: ECU quantities read only for the ground-truth pulse-chain check
    #: (never plant inputs): BFS, short/long trim.
    bfs: str
    stft: str
    ltft: str
    #: A reference the *regime label* reads (closed loop vs enrichment). Never
    #: a plant input.
    reference_phi: str
    #: A fuel-cut indicator for the dead-time-from-cut fallback and holdoff.
    fuel_cut_reference: str


class SteadyRule(_Strict):
    """Which pulse reads are steady enough to score a mean-value plant on."""

    window_s: float = Field(gt=0)
    rpm_span: float = Field(gt=0)
    maf_span: float = Field(gt=0)
    pulse_span: float = Field(gt=0)
    min_rpm_reads: int = Field(ge=1)
    min_maf_reads: int = Field(ge=1)
    min_pulse_reads: int = Field(ge=1)
    min_coolant_c: float
    min_effective_pulse_ms: float = Field(ge=0)
    lambda_rail: float = Field(gt=1)
    fuel_cut_margin_counts: int = Field(ge=0)
    region_gap_s: float = Field(gt=0)
    #: Pairing tolerances, seconds.
    pair_tolerance_s: float = Field(gt=0)
    ecu_pair_tolerance_s: float = Field(gt=0)
    reference_phi_max_age_s: float = Field(gt=0)
    slow_max_age_s: float = Field(gt=0)
    battery_max_age_s: float = Field(gt=0)
    rail_window_s: float = Field(gt=0)
    evidence: str = Field(min_length=1)


class Split(_Strict):
    """Recordings for identification and held-out evaluation, declared first."""

    identification: list[str] = Field(min_length=1)
    held_out: list[str] = Field(default_factory=list)
    declared_on: date
    evidence: str = Field(min_length=1)
    #: Why no held-out recording is declared yet (every candidate is spent, say).
    #: Required exactly when ``held_out`` is empty; such a plant is never
    #: qualified (:func:`plant_qualification`).
    held_out_pending: str | None = None

    @model_validator(mode="after")
    def _disjoint(self) -> Split:
        both = sorted(set(self.identification) & set(self.held_out))
        if both:
            raise ValueError(f"recordings in both identification and held-out: {both}")
        if (not self.held_out) != (self.held_out_pending is not None):
            raise ValueError(
                "held_out_pending names why no held-out recording is declared, and is "
                "given only then"
            )
        return self


class Acceptance(_Strict):
    """Held-out error bounds, declared before the held-out drives were scored."""

    declared_on: date
    #: |median| of per-sample lambda error over all held-out steady samples, %.
    pooled_median_abs_pct: float = Field(gt=0)
    #: 90th percentile of per-sample |lambda error|, %.
    sample_abs_p90_pct: float = Field(gt=0)
    #: A region passes when |its median error| is within this, %...
    region_abs_pct: float = Field(gt=0)
    #: ...and at least this fraction of regions must pass.
    region_pass_fraction: float = Field(gt=0, le=1)
    #: |median| over enrichment samples, %, when enough enrichment regions exist.
    enrichment_median_abs_pct: float = Field(gt=0)
    min_regions: int = Field(ge=1)
    min_enrichment_regions: int = Field(ge=1)
    bootstrap_resamples: int = Field(ge=100)
    bootstrap_level: float = Field(gt=0, lt=1)
    seed: int
    evidence: str = Field(min_length=1)


class DelayReference(_Strict):
    """B06's declaration: where the lambda delay was frozen, and the regime
    rules (closed-loop band, post-fuel-cut holdoffs) the plant's steady rule
    takes from it rather than copying them."""

    file: str
    regime: str
    source: str


Verdict = Literal["pass", "pass_closed_loop_only", "fail"]


class SpentRecord(_Strict):
    """One held-out recording, scored once, against one frozen baseline, and
    the verdict that scoring returned (what :func:`plant_qualification` reads)."""

    recording: str
    baseline_id: str
    baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plant_version: str
    scored_on: date
    verdict: Verdict


class CalibrationWord(_Strict):
    """A calibration constant at its flash address, with the value the firmware
    analysis published for it (checked against an image when one is given)."""

    address: int = Field(ge=0)
    width: Literal[1, 2]
    value: int


class PulseChain(_Strict):
    """Where the pulse chain's calibration lives in the image.

    The dead-time words and scalars are published in
    ``docs/ecu-firmware-analysis.md``, so their values are declared here and the
    plant can use the ECM's dead time without an image; the vacuum curves are
    read from an image only (no ROM bytes are committed).
    """

    dead_time_base: CalibrationWord
    dead_time_slope: CalibrationWord
    rail_reference: CalibrationWord
    flow_scaler: CalibrationWord
    vacuum_load_curve: int = Field(ge=0)
    vacuum_rpm_curve: int = Field(ge=0)
    #: Code immediates, not calibration bytes.
    dead_time_vref: int
    minimum_g: int
    #: The known-answer check's own steadiness: BFS now against BFS this long
    #: ago (the firmware session's rule: within 3 % over 0.5 s).
    check_bfs_lag_s: float = Field(gt=0)
    check_bfs_span: float = Field(gt=0)
    evidence: str = Field(min_length=1)


class FuelPlantProfile(_Strict):
    schema_version: Literal[1]
    plant_version: str = Field(min_length=1)
    #: How this version computes MAP (:data:`MANIFOLD_MODELS`).
    manifold_model: ManifoldModel = "speed_density_capped"
    #: The VE(rpm) fit rule; required by, and only by, a refusing form
    #: (:data:`REFUSING_MODELS`).
    volumetric_efficiency: VeFitRule | None = None
    #: A labelled synthetic plant (tests and demonstrations): it is identified
    #: and scored only on ``_sim-*`` traces, and a real plant refuses them
    #: (``fuel_plant_eval``'s ``synthetic_as_observed``).
    synthetic: bool = False
    hardware_configuration: str
    geometry: Geometry
    specs: Specs
    #: ECU command channels: refused as plant inputs and as the observation.
    command_specs: list[str] = Field(min_length=1)
    delay: DelayReference
    pulse_chain: PulseChain
    registry: ParameterRegistry
    steady: SteadyRule
    split: Split
    acceptance: Acceptance
    baseline: BaselineParameterSet | None = None
    #: Held-out recordings already scored, and under which baseline. A spent
    #: recording is never held-out evidence for another baseline or plant.
    spent_held_out: list[SpentRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_target_as_input(self) -> FuelPlantProfile:
        inputs = {
            "pulse": self.specs.pulse,
            "rpm": self.specs.rpm,
            "maf": self.specs.maf,
            "lambda_observed": self.specs.lambda_observed,
            "baro": self.specs.baro,
            "intake_temp": self.specs.intake_temp,
            "coolant": self.specs.coolant,
            "battery_voltage": self.specs.battery_voltage,
            "rail_pressure": self.specs.rail_pressure,
        }
        commands = set(self.command_specs)
        leaked = {k: v for k, v in inputs.items() if v in commands}
        if leaked:
            raise ValueError(
                f"target_as_input: {leaked} read an ECU command; a plant fed the target "
                "reproduces it instead of predicting delivered mixture"
            )
        if self.specs.reference_phi not in commands:
            raise ValueError("reference_phi must be one of the declared command_specs")
        refusing = self.manifold_model in REFUSING_MODELS
        if refusing != (self.volumetric_efficiency is not None):
            raise ValueError(
                "volumetric_efficiency (the VE fit rule) is declared by, and only by, a "
                "speed_density_refusing plant"
            )
        declared = {p.id: p for p in self.registry.parameters}
        missing = [p for p in plant_reads(self.manifold_model) if p not in declared]
        if missing:
            raise ValueError(
                f"{self.manifold_model} reads parameters the registry does not declare: {missing}"
            )
        if self.manifold_model == "speed_density_load_dependent":
            _check_breathing_form(declared)
        rule = self.volumetric_efficiency
        if (
            rule is not None
            and rule.form == "rpm_envelope"
            and not (self.baseline is not None and self.baseline.frozen)
        ):
            raise ValueError(
                "rpm_envelope (the band maximum, biased by the read count: M01.16) only "
                "reproduces a frozen baseline's ve_curve; a plant without one declares "
                "rpm_quantile"
            )
        if self.baseline is not None:
            curve = self.baseline.ve_curve
            if refusing and (
                curve is None
                or self.volumetric_efficiency is None
                or curve.rpm_edges != self.volumetric_efficiency.rpm_edges
            ):
                raise ValueError(
                    "a speed_density_refusing baseline freezes its ve_curve on the declared "
                    "rpm_edges"
                )
            if not refusing and curve is not None:
                raise ValueError(f"{self.manifold_model} runs one working VE; no ve_curve")
            if self.baseline.plant_version != self.plant_version:
                raise ValueError("baseline was identified on another plant_version")
            if self.baseline.hardware_configuration != self.hardware_configuration:
                raise ValueError("baseline belongs to another hardware configuration")
            reads = plant_reads(self.manifold_model)
            identifiable = {
                p.id
                for p in self.registry.parameters
                if p.status == "identifiable" and p.id in reads and p.working_assumption is None
            }
            if set(self.baseline.values) != identifiable:
                raise ValueError(
                    f"baseline values {sorted(self.baseline.values)} must be exactly the "
                    f"identifiable parameters the plant reads {sorted(identifiable)}"
                )
            leaked_fit = sorted(set(self.baseline.fitted_on) & set(self.split.held_out))
            if leaked_fit:
                raise ValueError(f"baseline was fitted on held-out recordings: {leaked_fit}")
        current = (
            (self.baseline.sha256(), self.plant_version) if self.baseline is not None else None
        )
        spent_elsewhere = sorted(
            r.recording
            for r in self.spent_held_out
            if (r.baseline_sha256, r.plant_version) != current
            and r.recording in self.split.held_out
        )
        if spent_elsewhere:
            raise ValueError(
                f"spent_held_out: {spent_elsewhere} were already scored under another baseline "
                "or plant_version; a revised plant needs never-scored held-out drives"
            )
        # This baseline's own scorings are of its declared held-out drives only:
        # a verdict recorded on anything else cannot stand in for them.
        stray = sorted(
            r.recording
            for r in self.spent_held_out
            if (r.baseline_sha256, r.plant_version) == current
            and r.recording not in self.split.held_out
        )
        if stray:
            raise ValueError(
                f"spent_held_out: {stray} are recorded against this baseline but are not in "
                "split.held_out"
            )
        return self

    def spent(self) -> dict[str, SpentRecord]:
        return {r.recording: r for r in self.spent_held_out}


def _check_breathing_form(declared: Mapping[str, PlantParameter]) -> None:
    """The residual-gas factor's inputs: geometry and physics are known (they
    carry no fitted error), the exhaust back-pressure is an assumption with a
    bracket, and the form is defined at every bracket end."""
    for pid in ("compression_ratio", "residual_expansion_exponent"):
        if declared[pid].status != "known":
            raise ValueError(f"{pid} is a geometry or physics fact here: status known")
    rc = declared["compression_ratio"].value
    n = declared["residual_expansion_exponent"].value
    assert rc is not None and n is not None
    if not (rc > 1.0 and n > 0.0):
        raise ValueError(f"compression ratio {rc} must exceed 1 and exponent {n} be positive")
    pe = declared["exhaust_pressure_ratio"]
    if pe.working_assumption is None:
        raise ValueError("exhaust_pressure_ratio runs under a bracketed working assumption")
    for end in pe.working_assumption.bracket:
        if not 0.0 < end < rc**n:
            raise ValueError(
                f"exhaust_pressure_ratio {end}: the residual gas would fill the cylinder at "
                "ambient intake pressure"
            )


@functools.lru_cache(maxsize=32)
def _parsed(text: bytes) -> Any:
    """One parse per distinct file content (every load reads its siblings'
    ledgers too). Read-only: callers validate, never mutate."""
    return load_yaml(text)


def plant_files(profile_dir: Path) -> dict[str, Path]:
    """Every fuel-plant declaration in the profile (``fuel_plant*.yaml``), by
    the ``plant_version`` it declares. Two files declaring one version are
    refused: a version is one frozen thing."""
    out: dict[str, Path] = {}
    for path in sorted(profile_dir.glob("fuel_plant*.yaml")):
        raw = _parsed(path.read_bytes())
        version = raw.get("plant_version") if isinstance(raw, dict) else None
        if not isinstance(version, str):
            continue
        if version in out:
            raise FuelPlantRefusalError(
                "plant_version_twice", f"{out[version].name} and {path.name} both declare {version}"
            )
        out[version] = path
    return out


def load_fuel_plant(
    profile_dir: Path, plant_version: str | None = None
) -> tuple[FuelPlantProfile, str]:
    """A plant declaration and the sha256 of its file.

    ``None`` reads ``fuel_plant.yaml``, the version the contract runs; a named
    version is found among :func:`plant_files` and must declare that version."""
    if plant_version is None:
        path = profile_dir / PLANT_FILE
    else:
        files = plant_files(profile_dir)
        if plant_version not in files:
            raise FuelPlantRefusalError(
                "plant_version_undeclared",
                f"{profile_dir} declares no fuel plant {plant_version!r} (has {sorted(files)})",
            )
        path = files[plant_version]
    text = path.read_bytes()
    profile = FuelPlantProfile.model_validate(_parsed(text))
    if plant_version is not None and profile.plant_version != plant_version:
        raise FuelPlantRefusalError("plant_version_mismatch", f"{path.name} is not {plant_version}")
    refuse_spent_elsewhere(profile_dir, profile, profile.split.held_out)
    return profile, hashlib.sha256(text).hexdigest()


def refuse_spent_elsewhere(
    profile_dir: Path, profile: FuelPlantProfile, recordings: Sequence[str]
) -> None:
    """Refuse any of ``recordings`` that *any* plant declaration in the
    profile has spent under another baseline or plant version.

    Each file's validator sees only its own ledger, so every ledger on disk is
    read here; :func:`load_fuel_plant` calls it on the split, and
    ``fuel_plant_eval.evaluate`` on what it is about to score, so a profile
    built in memory cannot score a sibling's spent drive either. A real
    (non-synthetic) plant must be declared in ``profile_dir``, so an empty
    directory cannot stand in for the profile."""
    files = plant_files(profile_dir)
    if not profile.synthetic and profile.plant_version not in files:
        raise FuelPlantRefusalError(
            "plant_version_undeclared",
            f"{profile_dir} declares no fuel plant {profile.plant_version!r}: its spent "
            "ledgers cannot be checked there",
        )
    names = set(recordings)
    if not names:
        return
    current = (
        (profile.baseline.sha256(), profile.plant_version) if profile.baseline is not None else None
    )
    for path in files.values():
        raw = _parsed(path.read_bytes())
        records = raw.get("spent_held_out", []) if isinstance(raw, dict) else []
        spent = sorted(
            r.recording
            for r in (SpentRecord.model_validate(x) for x in records or [])
            if r.recording in names and (r.baseline_sha256, r.plant_version) != current
        )
        if spent:
            raise FuelPlantRefusalError(
                "spent_held_out",
                f"{spent} were scored by {path.name}; a revised plant needs never-scored "
                "held-out drives",
            )


def internal_model_invariants(profile_dir: Path, plant_version: str) -> dict[str, tuple[str, ...]]:
    """The owner invariants each internally computed quantity holds in one
    plant version (what ``engine_dynamics.check_internal_models`` reads)."""
    profile, _ = load_fuel_plant(profile_dir, plant_version)
    return {"manifold_pressure": MANIFOLD_MODELS[profile.manifold_model]}


#: The parameter a ``speed_density_refusing`` plant fits from logs, as the
#: contract's ``parameter_fits`` names it (the registry carries its floor as
#: ``manifold_pressure``'s working VE).
VE_FIT_PARAMETER = "volumetric_efficiency"


def parameter_fit_bounds(
    profile_dir: Path, plant_version: str
) -> dict[str, dict[str, tuple[float, float]]]:
    """Each log-fitted parameter of one plant version and the bounds its
    frozen fit holds in (empty for a plant that fits none)."""
    profile, _ = load_fuel_plant(profile_dir, plant_version)
    curve = profile.baseline.ve_curve if profile.baseline is not None else None
    return {} if curve is None else {VE_FIT_PARAMETER: curve.bounds()}


@dataclass(frozen=True)
class B06Rules:
    """What the plant takes from B06's declaration, through B06's own schema."""

    #: The frozen lambda delay (enrichment, RAM-target route) and its bracket.
    seconds: float
    bracket: tuple[float, float]
    fitted_on: tuple[str, ...]
    #: ``|phi - 1| <= closed_loop_band`` is closed loop.
    closed_loop_band: float
    holdoff_closed_loop_s: float
    holdoff_enrichment_s: float
    #: Every recording that chose any of B06's constants (both regimes): never
    #: held-out evidence for a plant that uses them.
    informed_by: tuple[str, ...]


def load_b06(profile_dir: Path, ref: DelayReference) -> B06Rules:
    """B06's frozen delay and regime rules, validated by B06's own schema.

    Only the ``eligibility`` block is validated, with
    :class:`src.analysis.lambda_delivery.Eligibility` (unchanged since HEAD);
    the whole-file loader is not used here. The delay is read, never refitted:
    a delay that is not frozen is refused.
    """
    from src.analysis.lambda_delivery import Eligibility

    raw = load_yaml((profile_dir / ref.file).read_text())
    try:
        elig = Eligibility.model_validate(raw["eligibility"])
        entry = elig.regimes.for_regime(ref.regime).transport_delays[ref.source]
    except (KeyError, TypeError) as exc:
        raise FuelPlantRefusalError(
            "delay_not_declared", f"{ref.file} has no {ref.regime}/{ref.source} delay"
        ) from exc
    if entry.status != "frozen" or entry.seconds is None or entry.bracket_s is None:
        raise FuelPlantRefusalError(
            "delay_not_frozen", f"{ref.file} {ref.regime}/{ref.source} is {entry.status!r}"
        )
    lo, hi = entry.bracket_s
    return B06Rules(
        seconds=float(entry.seconds),
        bracket=(float(lo), float(hi)),
        fitted_on=tuple(entry.fitted_on),
        closed_loop_band=elig.closed_loop_band,
        holdoff_closed_loop_s=elig.regimes.closed_loop.post_fuel_cut_holdoff_s,
        holdoff_enrichment_s=elig.regimes.enrichment.post_fuel_cut_holdoff_s,
        informed_by=tuple(elig.informed_by),
    )


def check_against_contract(
    profile: FuelPlantProfile, contract_parameters: Mapping[str, str], contract_plant_version: str
) -> list[str]:
    """Where the plant and the E07.1 contract disagree; empty when they agree.

    ``contract_parameters`` maps the contract hardware configuration's
    parameter ids to their status. Every contract parameter must be in the
    registry with the same status; the registry may declare more.
    """
    out: list[str] = []
    if contract_plant_version != profile.plant_version:
        out.append(
            f"plant_version: contract {contract_plant_version!r}, plant {profile.plant_version!r}"
        )
    ids = {p.id: p for p in profile.registry.parameters}
    for pid, status in sorted(contract_parameters.items()):
        if pid not in ids:
            out.append(f"parameter {pid}: in the contract, not in the plant registry")
        elif ids[pid].status != status:
            out.append(f"parameter {pid}: contract {status}, plant {ids[pid].status}")
    return out


# -- physics -----------------------------------------------------------------------


def _f(values: ArrayLike) -> FloatArray:
    return np.asarray(values, dtype=float)


def events_per_second(rpm: ArrayLike, geometry: Geometry) -> FloatArray:
    """Cylinder firing events per second (all cylinders)."""
    return _f(rpm) / 60.0 * geometry.cylinders * 2.0 / geometry.strokes


def air_per_event_g(maf_gps: ArrayLike, rpm: ArrayLike, geometry: Geometry) -> FloatArray:
    """Air mass per cylinder event, g. ``rpm <= 0`` has no event: NaN."""
    ev = events_per_second(rpm, geometry)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(ev > 0, _f(maf_gps) / ev, np.nan)


def manifold_pressure_speed_density(
    air_g: ArrayLike,
    iat_c: ArrayLike,
    baro_kpa: ArrayLike,
    geometry: Geometry,
    volumetric_efficiency: float,
) -> FloatArray:
    """MAP that would hold ``air_g`` per cylinder at ``volumetric_efficiency``, kPa.

    Capped at baro: a naturally aspirated manifold cannot exceed ambient.
    fuel-mv-1's form (``speed_density_capped``), frozen; a revised plant uses
    :func:`manifold_pressure_ve_curve`, which refuses instead of capping.
    """
    if not volumetric_efficiency > 0:
        raise FuelPlantRefusalError("ve_nonpositive", "volumetric efficiency must be > 0")
    v_m3 = geometry.cylinder_volume_l / 1000.0
    p_pa = _f(air_g) / 1000.0 * R_AIR * (_f(iat_c) + KELVIN) / (volumetric_efficiency * v_m3)
    return np.minimum(p_pa / 1000.0, _f(baro_kpa))


def manifold_pressure_ve_curve(
    air_g: ArrayLike,
    rpm: ArrayLike,
    iat_c: ArrayLike,
    baro_kpa: ArrayLike,
    geometry: Geometry,
    curve: VeCurve,
    floor: float,
    *,
    fitted_at: float | None = None,
) -> tuple[FloatArray, NDArray[np.str_]]:
    """Steady speed-density MAP at VE(rpm), kPa, and a per-sample refusal.

    Never capped: where the steady manifold at the fitted VE would exceed
    ambient the sample is NaN with :data:`MAP_ABOVE_AMBIENT`, as
    ``manifold_breathing`` refuses it; an rpm, baro or intake temperature
    outside the fit's range is NaN with :data:`MAP_OUTSIDE_VE_FIT`. ``""`` is
    a sample with a MAP."""
    if not floor > 0:
        raise FuelPlantRefusalError("ve_nonpositive", "volumetric efficiency must be > 0")
    ve = curve.ve(rpm, floor, fitted_at=floor if fitted_at is None else fitted_at)
    v_m3 = geometry.cylinder_volume_l / 1000.0
    with np.errstate(invalid="ignore"):
        p_kpa = _f(air_g) / 1000.0 * R_AIR * (_f(iat_c) + KELVIN) / (ve * v_m3) / 1000.0
    baro = _f(baro_kpa)
    iat = _f(iat_c)
    outside = _outside_ve_fit(ve, baro, iat, curve)
    above = ~outside & (p_kpa > baro * (1.0 + AMBIENT_ROUNDING))
    reason = np.full(p_kpa.shape, "", dtype="<U32")
    reason[above] = MAP_ABOVE_AMBIENT
    reason[outside] = MAP_OUTSIDE_VE_FIT
    return np.where(above | outside, np.nan, p_kpa), reason


def _outside_ve_fit(
    ve: FloatArray, baro: FloatArray, iat: FloatArray, curve: VeCurve
) -> NDArray[np.bool_]:
    out: NDArray[np.bool_] = (
        ~np.isfinite(ve)
        | ~((baro >= curve.baro_kpa[0]) & (baro <= curve.baro_kpa[1]))
        | ~((iat >= curve.iat_c[0]) & (iat <= curve.iat_c[1]))
    )
    return out


def part_load_breathing_factor(
    pressure_ratio: ArrayLike,
    *,
    compression_ratio: float,
    exhaust_ratio: float,
    exponent: float,
) -> FloatArray:
    """VE at intake pressure ``x = MAP / baro`` relative to VE at ``x = 1``.

    The ideal-cycle residual-gas form: at TDC the clearance volume ``V_c``
    holds exhaust at ``p_e``; it expands to the intake pressure ``p_i`` and
    occupies ``V_c (p_e / p_i)^(1/n)``, so the fresh charge per displacement
    is ``eta = (r_c - (p_e / p_i)^(1/n)) / (r_c - 1)``. Here ``p_e / p_i =
    exhaust_ratio / x`` (exhaust pressure as a ratio of baro) and the result is
    normalised to ``eta`` at ``x = 1``, so the fitted full-breathing VE(rpm)
    is unchanged there. It falls as the manifold empties (the residual takes
    more of the cylinder) and is 0 where the residual alone fills it. A
    geometry/physics form, not a fit: overlap backflow and throttling losses
    beyond it are not modelled."""
    x = _f(pressure_ratio)
    rc, pe, n = compression_ratio, exhaust_ratio, exponent

    def eta(r: FloatArray) -> FloatArray:
        with np.errstate(divide="ignore", invalid="ignore"):
            out: FloatArray = (rc - (pe / r) ** (1.0 / n)) / (rc - 1.0)
        return out

    full = float(eta(np.array([1.0]))[0])
    if not full > 0:
        raise FuelPlantRefusalError(
            "breathing_form_invalid", "the residual gas fills the cylinder at ambient pressure"
        )
    with np.errstate(invalid="ignore"):
        return np.where(x > 0, np.maximum(eta(np.where(x > 0, x, 1.0)), 0.0) / full, 0.0)


#: Bisection steps of :func:`manifold_pressure_load_dependent` (2^-80 of the
#: bracket: far below float resolution).
_BISECTION_STEPS = 80


def manifold_pressure_load_dependent(
    air_g: ArrayLike,
    rpm: ArrayLike,
    iat_c: ArrayLike,
    baro_kpa: ArrayLike,
    geometry: Geometry,
    curve: VeCurve,
    floor: float,
    *,
    compression_ratio: float,
    exhaust_ratio: float,
    exponent: float,
    fitted_at: float | None = None,
) -> tuple[FloatArray, NDArray[np.str_]]:
    """Steady speed-density MAP with a load-dependent VE, kPa, and a per-sample refusal.

    Solves ``charge fraction = VE(rpm) x f(x) x x`` for ``x = MAP / baro``,
    ``f`` the :func:`part_load_breathing_factor`. The right side rises
    monotonically in ``x`` (both factors do), so the root is unique; it is
    bracketed between where ``f`` reaches 0 and ``x = 1``. A charge fraction
    above VE(rpm) needs ``x > 1``: NaN with :data:`MAP_ABOVE_AMBIENT`, as
    :func:`manifold_pressure_ve_curve` refuses it; an rpm, baro or IAT outside
    the fit is :data:`MAP_OUTSIDE_VE_FIT`. With an infinite compression ratio
    ``f = 1`` and this is :func:`manifold_pressure_ve_curve`."""
    if not floor > 0:
        raise FuelPlantRefusalError("ve_nonpositive", "volumetric efficiency must be > 0")
    form = dict(compression_ratio=compression_ratio, exhaust_ratio=exhaust_ratio, exponent=exponent)
    part_load_breathing_factor([1.0], **form)  # refuses an undefined form
    ve = curve.ve(rpm, floor, fitted_at=floor if fitted_at is None else fitted_at)
    v_m3 = geometry.cylinder_volume_l / 1000.0
    baro = _f(baro_kpa)
    iat = _f(iat_c)
    p_full = _f(air_g) / 1000.0 * R_AIR * (iat + KELVIN) / v_m3 / 1000.0
    with np.errstate(invalid="ignore", divide="ignore"):
        target = p_full / baro / ve
    outside = _outside_ve_fit(ve, baro, iat, curve)
    above = ~outside & (target > 1.0 + AMBIENT_ROUNDING)
    solve = ~outside & ~above & np.isfinite(target)
    x_zero = exhaust_ratio / compression_ratio**exponent
    lo = np.full(target.shape, x_zero)
    hi = np.ones(target.shape)
    t = np.where(solve, np.minimum(target, 1.0), 0.0)
    for _ in range(_BISECTION_STEPS):
        mid = (lo + hi) / 2.0
        under = part_load_breathing_factor(mid, **form) * mid < t
        lo = np.where(under, mid, lo)
        hi = np.where(under, hi, mid)
    p_kpa = np.where(solve, (lo + hi) / 2.0 * baro, np.nan)
    reason = np.full(p_kpa.shape, "", dtype="<U32")
    reason[above] = MAP_ABOVE_AMBIENT
    reason[outside] = MAP_OUTSIDE_VE_FIT
    return p_kpa, reason


def injector_delta_p_kpa(
    rail_gauge_kpa: ArrayLike, baro_kpa: ArrayLike, map_kpa: ArrayLike
) -> FloatArray:
    """Pressure across the injector, kPa: rail absolute minus manifold."""
    return _f(rail_gauge_kpa) + _f(baro_kpa) - _f(map_kpa)


def flow_factor(delta_p_kpa: ArrayLike, reference_kpa: float, exponent: float) -> FloatArray:
    """Injector flow relative to its reference differential pressure."""
    dp = _f(delta_p_kpa)
    with np.errstate(invalid="ignore"):
        return np.where(dp > 0, (dp / reference_kpa) ** exponent, np.nan)


def effective_open_ms(pulse_ms: ArrayLike, dead_time_ms: ArrayLike) -> FloatArray:
    """The part of a pulse that meters fuel, ms (never negative)."""
    return np.maximum(_f(pulse_ms) - _f(dead_time_ms), 0.0)


def delivered_lambda(air_g: ArrayLike, stoich_air_equiv_g: ArrayLike) -> FloatArray:
    """lambda from air mass and the stoichiometric-air equivalent of the fuel.

    No fuel has no lambda (NaN), never infinity or a rail value.
    """
    fuel = _f(stoich_air_equiv_g)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(fuel > 0, _f(air_g) / fuel, np.nan)


@dataclass(frozen=True)
class ResolvedParameters:
    """Numbers one plant run uses, and the unresolved effects they rest on."""

    values: dict[str, float]
    #: Effects the run rests on regardless of data (assumed parameters).
    effects: tuple[str, ...]
    #: The effect named when the per-sample rail pressure is not measured.
    rail_effect: str
    baseline_sha256: str
    manifold_model: ManifoldModel = "speed_density_capped"
    #: The frozen VE(rpm) of a ``speed_density_refusing`` plant; its floor is
    #: ``values["manifold_pressure"]`` (the registry's working VE).
    ve_curve: VeCurve | None = None
    #: The registry's working VE, which splits the curve's bands into the
    #: floor's and the fitted ones (:meth:`VeCurve.ve`): an override of the
    #: floor moves only the floor's bands.
    ve_fitted_at: float | None = None


def resolve_parameters(
    profile: FuelPlantProfile,
    delay: B06Rules,
    *,
    overrides: Mapping[str, float] | None = None,
    for_identification: bool = False,
    ve_curve: VeCurve | None = None,
) -> ResolvedParameters:
    """Every number :func:`predict` reads, from a *frozen* baseline.

    ``overrides`` moves a working assumption inside its bracket (sensitivity);
    an override outside it, or of a known/identified value, is refused.
    ``for_identification`` runs without a baseline, with every identifiable
    value at 1.0 so the fit can read its per-sample ratio; nothing it returns
    is a prediction. A ``speed_density_refusing`` plant reads its VE(rpm) from
    the frozen baseline; only identification may hand one in (``ve_curve``,
    fitted on the identification drives first).
    """
    baseline = profile.baseline
    if not for_identification and (baseline is None or not baseline.frozen):
        raise FuelPlantRefusalError(
            "baseline_not_frozen",
            "identify, then freeze the hardware parameter set before evaluating anything",
        )
    overrides = dict(overrides or {})
    values: dict[str, float] = {}
    effects: list[str] = []
    rail_effect = ""
    for pid in plant_reads(profile.manifold_model):
        p = profile.registry.get(pid)
        if pid == DELAY_PARAMETER:
            wa = p.working_assumption
            if wa is None or (wa.value, tuple(wa.bracket)) != (delay.seconds, delay.bracket):
                raise FuelPlantRefusalError(
                    "delay_mismatch",
                    f"{pid} must restate B06's frozen {delay.seconds} s, bracket {delay.bracket}; "
                    "a lag is never re-chosen here",
                )
            value, bracket, effect = delay.seconds, delay.bracket, wa.effect
        elif p.status == "known":
            assert p.value is not None
            value, bracket, effect = p.value, (p.value, p.value), ""
        elif p.status == "identifiable" and p.working_assumption is None:
            value = 1.0 if for_identification or baseline is None else baseline.values[pid]
            bracket, effect = (value, value), ""
        elif p.working_assumption is not None:
            wa = p.working_assumption
            value, bracket, effect = wa.value, wa.bracket, wa.effect
        else:
            raise FuelPlantRefusalError(
                "unresolved_without_assumption",
                f"{pid} is {p.status} with no value and no working assumption",
            )
        if pid in overrides:
            new = overrides.pop(pid)
            if bracket[0] == bracket[1] or not bracket[0] <= new <= bracket[1]:
                raise FuelPlantRefusalError(
                    "override_outside_bracket", f"{pid}={new} outside {bracket}"
                )
            value = new
        values[pid] = float(value)
        if pid == "rail_pressure":
            rail_effect = effect
        elif effect:
            effects.append(effect)
    if overrides:
        raise FuelPlantRefusalError(
            "unknown_override", f"not plant parameters: {sorted(overrides)}"
        )
    sha = "" if for_identification or baseline is None else baseline.sha256()
    curve: VeCurve | None = None
    if profile.manifold_model in REFUSING_MODELS:
        if ve_curve is not None and not for_identification:
            raise FuelPlantRefusalError(
                "ve_curve_not_frozen", "outside identification VE(rpm) comes from the baseline"
            )
        curve = ve_curve if ve_curve is not None else (baseline.ve_curve if baseline else None)
        if curve is None:
            raise FuelPlantRefusalError(
                "ve_curve_missing", "a speed_density_refusing plant runs on a fitted VE(rpm)"
            )
    elif ve_curve is not None:
        raise FuelPlantRefusalError("ve_curve_unused", f"{profile.manifold_model} has no VE curve")
    fitted_at: float | None = None
    if curve is not None:
        wa = profile.registry.get(VE_PARAMETER).working_assumption
        assert wa is not None  # the registry check requires it of a VE-reading plant
        fitted_at = float(wa.value)
    return ResolvedParameters(
        values, tuple(effects), rail_effect, sha, profile.manifold_model, curve, fitted_at
    )


@dataclass(frozen=True)
class PlantInputs:
    """One bank's plant-replay inputs, aligned per sample. No target here."""

    pulse_ms: FloatArray
    #: The ECM's own dead time at the sample (its compensation), ms.
    ecu_dead_time_ms: FloatArray
    rpm: FloatArray
    maf_gps: FloatArray
    baro_kpa: FloatArray
    iat_c: FloatArray
    #: Measured rail gauge pressure, kPa; NaN where not logged.
    rail_kpa: FloatArray
    #: Manifold pressure stated per sample, kPa (a scenario's prescribed
    #: boundary, E07.6). ``None``: speed density at the working VE.
    map_kpa: FloatArray | None = None


@dataclass(frozen=True)
class PlantOutput:
    air_g: FloatArray
    effective_ms: FloatArray
    map_kpa: FloatArray
    delta_p_kpa: FloatArray
    flow_factor: FloatArray
    stoich_air_equiv_g: FloatArray
    fuel_g: FloatArray
    lambda_delivered: FloatArray
    rail_measured: NDArray[np.bool_]
    #: Effects every sample rests on; ``rail_effect`` too where not measured.
    effects: tuple[str, ...]
    rail_effect: str = ""
    extra: dict[str, FloatArray] = field(default_factory=dict)
    #: Per-sample MAP refusal (``""`` = none); only a refusing plant sets one.
    manifold_refusal: NDArray[np.str_] | None = None


def predict(inputs: PlantInputs, params: ResolvedParameters, geometry: Geometry) -> PlantOutput:
    """Plant replay: delivered fuel and lambda for one bank, per sample."""
    v = params.values
    # Injector latency: the working assumption is the ECM's compensation
    # *offset* by the declared value (0 = the injector is as the ECM believes).
    dead = inputs.ecu_dead_time_ms + v["injector_dead_time"]
    te = effective_open_ms(inputs.pulse_ms, dead)
    # One plenum feeds both banks (E08.11, operator 2026-10-16; the contract's
    # "one manifold for both banks"): MAP is set by the engine's total air, the
    # mean cylinder charge, whatever the split. The split is a per-bank
    # fuelling fact: a bank's cylinder gets split x banks of the mean, which
    # is what its injector meters against and its lambda sees. At the working
    # split 1/banks the factor is exactly 1.0, so the two are the same array.
    mean_air = air_per_event_g(inputs.maf_gps, inputs.rpm, geometry)
    air = mean_air * (v["bank_air_split"] * geometry.banks)
    refusal: NDArray[np.str_] | None = None
    if inputs.map_kpa is not None:
        map_kpa = _f(inputs.map_kpa)
    elif params.manifold_model == "speed_density_load_dependent":
        assert params.ve_curve is not None
        map_kpa, refusal = manifold_pressure_load_dependent(
            mean_air,
            inputs.rpm,
            inputs.iat_c,
            inputs.baro_kpa,
            geometry,
            params.ve_curve,
            v["manifold_pressure"],
            compression_ratio=v["compression_ratio"],
            exhaust_ratio=v["exhaust_pressure_ratio"],
            exponent=v["residual_expansion_exponent"],
            fitted_at=params.ve_fitted_at,
        )
    elif params.manifold_model == "speed_density_refusing":
        assert params.ve_curve is not None
        map_kpa, refusal = manifold_pressure_ve_curve(
            mean_air,
            inputs.rpm,
            inputs.iat_c,
            inputs.baro_kpa,
            geometry,
            params.ve_curve,
            v["manifold_pressure"],
            fitted_at=params.ve_fitted_at,
        )
    else:
        map_kpa = manifold_pressure_speed_density(
            mean_air, inputs.iat_c, inputs.baro_kpa, geometry, v["manifold_pressure"]
        )
    measured = np.isfinite(inputs.rail_kpa)
    rail = np.where(measured, inputs.rail_kpa, v["rail_pressure"])
    dp = injector_delta_p_kpa(rail, inputs.baro_kpa, map_kpa)
    ff = flow_factor(dp, v["reference_delta_p"], v["injector_flow_exponent"])
    equiv = v["stoich_air_per_open_ms"] * ff * te
    lam = delivered_lambda(air, equiv)
    return PlantOutput(
        air_g=air,
        effective_ms=te,
        map_kpa=map_kpa,
        delta_p_kpa=dp,
        flow_factor=ff,
        stoich_air_equiv_g=equiv,
        fuel_g=equiv / v["fuel_stoich_afr"],
        lambda_delivered=lam,
        rail_measured=measured,
        effects=params.effects,
        rail_effect=params.rail_effect,
        manifold_refusal=refusal,
    )


@dataclass(frozen=True)
class Qualification:
    """Whether the frozen plant passed its own predeclared held-out test.

    Derived from ``spent_held_out`` (the verdict each scoring recorded), never
    stated: a tuning or intervention claim reads this and cannot be handed a
    "qualified" flag. ``regimes`` is empty unless ``qualified``."""

    qualified: bool
    regimes: tuple[str, ...]
    #: ``None`` when qualified; else ``plant_not_qualified: <why>``.
    reason: str | None
    baseline_sha256: str
    plant_version: str
    synthetic: bool


def plant_qualification(profile: FuelPlantProfile) -> Qualification:
    """The plant's standing for prediction: qualified only when every declared
    held-out recording was scored under *this* frozen baseline and plant
    version, and every one of those scorings passed."""
    baseline = profile.baseline

    def refused(why: str, sha: str = "") -> Qualification:
        return Qualification(
            False, (), f"plant_not_qualified: {why}", sha, profile.plant_version, profile.synthetic
        )

    if baseline is None or not baseline.frozen:
        return refused("no frozen baseline")
    sha = baseline.sha256()
    if not profile.split.held_out:
        return refused(f"baseline {baseline.id} has no held-out recording (held_out_pending)", sha)
    mine = {
        r.recording: r
        for r in profile.spent_held_out
        if (r.baseline_sha256, r.plant_version) == (sha, profile.plant_version)
    }
    unscored = sorted(set(profile.split.held_out) - set(mine))
    if not mine:
        return refused(f"baseline {baseline.id} was never scored held out", sha)
    if unscored:
        return refused(f"held-out recordings {unscored} were not scored", sha)
    stray = sorted(set(mine) - set(profile.split.held_out))
    if stray:
        return refused(f"verdicts recorded on undeclared recordings {stray}", sha)
    verdicts = {r.verdict for r in mine.values()}
    if "fail" in verdicts:
        return refused(f"baseline {baseline.id} failed its predeclared held-out bounds", sha)
    regimes = (
        ("closed_loop",) if "pass_closed_loop_only" in verdicts else ("closed_loop", "enrichment")
    )
    return Qualification(True, regimes, None, sha, profile.plant_version, profile.synthetic)


def sensitivity_cases(profile: FuelPlantProfile, delay: B06Rules) -> dict[str, dict[str, float]]:
    """One override set per bracket end of every working assumption."""
    cases: dict[str, dict[str, float]] = {}
    for pid in plant_reads(profile.manifold_model):
        p = profile.registry.get(pid)
        if pid == "lambda_observation_delay":
            lo, hi = delay.bracket
        elif p.working_assumption is not None:
            lo, hi = p.working_assumption.bracket
        else:
            continue
        if lo != hi:
            cases[f"{pid}=lo"] = {pid: lo}
            cases[f"{pid}=hi"] = {pid: hi}
    return cases


def mass_balance_residual(air_g: ArrayLike, fuel_g: ArrayLike, exhaust_g: ArrayLike) -> FloatArray:
    """Exhaust minus (air + fuel), g: zero for a conserving plant."""
    return _f(exhaust_g) - (_f(air_g) + _f(fuel_g))


def bootstrap_median(
    groups: Sequence[FloatArray], *, resamples: int, level: float, seed: int
) -> tuple[float, float, float]:
    """Median of group medians, with a bootstrap interval resampling *groups*.

    Samples inside one steady region are not independent, so the region is
    the resampling unit.
    """
    medians = np.array([float(np.median(g)) for g in groups if len(g)])
    if not len(medians):
        return math.nan, math.nan, math.nan
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(medians), size=(resamples, len(medians)))
    boot = np.median(medians[idx], axis=1)
    tail = (1.0 - level) / 2.0
    lo, hi = np.quantile(boot, [tail, 1.0 - tail])
    return float(np.median(medians)), float(lo), float(hi)
