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

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import numpy as np
import yaml
from numpy.typing import ArrayLike, NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

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

FloatArray = NDArray[np.float64]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FuelPlantRefusalError(ValueError):
    """The plant cannot evaluate what it was given; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


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

    @model_validator(mode="after")
    def _intervals(self) -> BaselineParameterSet:
        if set(self.values) != set(self.intervals):
            raise ValueError("every identified value needs its interval and nothing else")
        for key, (lo, hi) in self.intervals.items():
            if not lo <= self.values[key] <= hi:
                raise ValueError(f"{key}: value outside its interval")
        return self

    def sha256(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True)
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
    held_out: list[str] = Field(min_length=1)
    declared_on: date
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _disjoint(self) -> Split:
        both = sorted(set(self.identification) & set(self.held_out))
        if both:
            raise ValueError(f"recordings in both identification and held-out: {both}")
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
        if self.baseline is not None:
            if self.baseline.plant_version != self.plant_version:
                raise ValueError("baseline was identified on another plant_version")
            if self.baseline.hardware_configuration != self.hardware_configuration:
                raise ValueError("baseline belongs to another hardware configuration")
            identifiable = {
                p.id
                for p in self.registry.parameters
                if p.status == "identifiable"
                and p.id in PLANT_READS
                and p.working_assumption is None
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
        return self

    def spent(self) -> dict[str, SpentRecord]:
        return {r.recording: r for r in self.spent_held_out}


def load_fuel_plant(profile_dir: Path) -> tuple[FuelPlantProfile, str]:
    """The profile's plant declaration and the sha256 of its file."""
    text = (profile_dir / PLANT_FILE).read_bytes()
    return FuelPlantProfile.model_validate(yaml.safe_load(text)), hashlib.sha256(text).hexdigest()


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

    raw = yaml.safe_load((profile_dir / ref.file).read_text())
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
    """
    if not volumetric_efficiency > 0:
        raise FuelPlantRefusalError("ve_nonpositive", "volumetric efficiency must be > 0")
    v_m3 = geometry.cylinder_volume_l / 1000.0
    p_pa = _f(air_g) / 1000.0 * R_AIR * (_f(iat_c) + KELVIN) / (volumetric_efficiency * v_m3)
    return np.minimum(p_pa / 1000.0, _f(baro_kpa))


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


def resolve_parameters(
    profile: FuelPlantProfile,
    delay: B06Rules,
    *,
    overrides: Mapping[str, float] | None = None,
    for_identification: bool = False,
) -> ResolvedParameters:
    """Every number :func:`predict` reads, from a *frozen* baseline.

    ``overrides`` moves a working assumption inside its bracket (sensitivity);
    an override outside it, or of a known/identified value, is refused.
    ``for_identification`` runs without a baseline, with every identifiable
    value at 1.0 so the fit can read its per-sample ratio; nothing it returns
    is a prediction.
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
    for pid in PLANT_READS:
        p = profile.registry.get(pid)
        if pid == "lambda_observation_delay":
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
    return ResolvedParameters(values, tuple(effects), rail_effect, sha)


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


def predict(inputs: PlantInputs, params: ResolvedParameters, geometry: Geometry) -> PlantOutput:
    """Plant replay: delivered fuel and lambda for one bank, per sample."""
    v = params.values
    # Injector latency: the working assumption is the ECM's compensation
    # *offset* by the declared value (0 = the injector is as the ECM believes).
    dead = inputs.ecu_dead_time_ms + v["injector_dead_time"]
    te = effective_open_ms(inputs.pulse_ms, dead)
    # Equal air per cylinder is bank_air_split = 1/banks; a bank's cylinder
    # gets split x banks of the mean.
    air = air_per_event_g(inputs.maf_gps, inputs.rpm, geometry) * (
        v["bank_air_split"] * geometry.banks
    )
    map_kpa = (
        manifold_pressure_speed_density(
            air, inputs.iat_c, inputs.baro_kpa, geometry, v["manifold_pressure"]
        )
        if inputs.map_kpa is None
        else _f(inputs.map_kpa)
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
    verdicts = {mine[name].verdict for name in profile.split.held_out}
    if "fail" in verdicts:
        return refused(f"baseline {baseline.id} failed its predeclared held-out bounds", sha)
    regimes = (
        ("closed_loop",) if "pass_closed_loop_only" in verdicts else ("closed_loop", "enrichment")
    )
    return Qualification(True, regimes, None, sha, profile.plant_version, profile.synthetic)


def sensitivity_cases(profile: FuelPlantProfile, delay: B06Rules) -> dict[str, dict[str, float]]:
    """One override set per bracket end of every working assumption."""
    cases: dict[str, dict[str, float]] = {}
    for pid in PLANT_READS:
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
