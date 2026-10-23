"""E07.5 stage 4: the minimum torque model, as a declared structure (E09.13).

::

    brake     = indicated - friction - pumping
    indicated = air per cylinder event x (LHV / AFR_stoich) x eta_i0
                x eta(lambda) x eta(spark), over the events of one cycle
    eta(spark) = 1 - c (MBT - spark)^2
    friction  = (f0 + f1 N + f2 N^2) Vd / 4 pi      (N in krpm)
    pumping   = (baro + dp_exhaust - MAP) Vd / 4 pi

Generic physics; every number is the profile's (``torque_plant_tq0.yaml``),
loaded with a strict schema and recorded by its sha256 on every result.

The registry follows E07.3's rules: a ``known`` parameter has a value and an
independent source; every other one has a bracket, where the bracket comes
from (a ``literature_prior`` names its citation), the contract measurement that
would identify it and the unresolved effect a result resting on it carries.
Nothing is fitted (queue entry Q27): the schema has no fitted values at all, so
an ``identifiable`` parameter is refused here.

So a result is never a prediction. :func:`evaluate` runs one *bracket case*:
the caller states every unmeasured parameter (:func:`parameter_case` refuses an
unstated one and one outside its bracket), and the result names every
parameter it rests on and every effect left unresolved. MBT is one of those
parameters: retard from MBT cannot be computed from the commanded timing
alone, so an evaluation that does not state MBT is refused
(``spark_mbt_unstated``), never run at an assumed MBT.

Where the equations hold is the contract's (``torque_production.applicability``):
a sample outside it is NaN and named (``outside_applicability:<quantity>``),
never clipped; so is a sample further from MBT than the declared shape
(``spark_outside_shape``) or with MAP above ambient (``manifold_above_ambient``).

The stage's standing is derived by :func:`src.analysis.engine_dynamics.
subsystem_standing` like every stage's: built here, not validated until a
recorded pass against an available, independent torque reference (H07.12).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import ArrayLike, NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.fuel_plant import load_fuel_plant
from src.analysis.sim_contract import SimContract, SimulationRefusalError
from src.yaml_load import load_yaml

PLANT_FILE = "torque_plant_tq0.yaml"
SUBSYSTEM = "torque_production"
#: Owned quantities of other stages this stage computes for itself: none. MAP is
#: read from the manifold stage as an input, never recomputed here
#: (``engine_dynamics.check_internal_models``).
COMPUTES_INTERNALLY: tuple[str, ...] = ()

#: Every registry parameter the evaluator reads.
READS: tuple[str, ...] = (
    "displacement",
    "cylinders",
    "fuel_lower_heating_value",
    "fuel_stoich_afr",
    "indicated_efficiency_peak",
    "lambda_peak",
    "lambda_peak_gain",
    "lambda_rich_curvature",
    "spark_mbt_advance",
    "spark_efficiency_curvature",
    "friction_fmep_0",
    "friction_fmep_1",
    "friction_fmep_2",
    "exhaust_back_pressure",
)
MBT = "spark_mbt_advance"
#: The contract quantities a sample states (``engine_torque``'s inputs and the
#: stage's bounds; checked against the contract by :func:`check_registry`).
INPUTS: tuple[str, ...] = (
    "engine_speed",
    "cylinder_air_flow",
    "delivered_lambda",
    "spark_advance",
    "manifold_pressure",
    "barometric_pressure",
    "intake_air_temperature",
)

FloatArray = NDArray[np.float64]
Status = Literal["known", "identifiable", "requires_measurement"]
KnownSource = Literal["profile_fact", "datasheet", "firmware", "physics"]
#: Where an unmeasured parameter's bracket comes from. ``literature_prior``
#: names a citation; ``plant_registry`` is another plant's registry entry of the
#: same id (and must equal it); ``declared_range`` is a stated range with its
#: reasoning in the evidence.
BracketSource = Literal["literature_prior", "plant_registry", "declared_range"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TorqueParameter(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    units: str = Field(min_length=1)
    status: Status
    evidence: str = Field(min_length=1)
    #: ``known`` only.
    value: float | None = None
    source: KnownSource | None = None
    #: Every other status: the range a bracket case may state, and why.
    bracket: tuple[float, float] | None = None
    bracket_source: BracketSource | None = None
    citation: str | None = None
    #: Contract required-measurement ids (``torque_production``) that identify it.
    measured_by: tuple[str, ...] = ()
    #: The unresolved effect every result resting on the bracket names.
    effect: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")

    @model_validator(mode="after")
    def _status(self) -> TorqueParameter:
        if self.status == "identifiable":
            raise ValueError(
                f"{self.id}: identifiable needs a frozen fit, and this plant fits nothing "
                "(Q27): no log on this truck measures torque, and the fuel-energy estimate "
                "shares the stage's inputs"
            )
        unmeasured = (self.bracket, self.bracket_source, self.effect)
        if self.status == "known":
            if self.value is None or self.source is None:
                raise ValueError(f"{self.id}: a known parameter needs a value and its source")
            if any(v is not None for v in (*unmeasured, self.citation)) or self.measured_by:
                raise ValueError(f"{self.id}: a known parameter carries no bracket or measurer")
            return self
        if self.value is not None or self.source is not None:
            raise ValueError(
                f"{self.id}: requires_measurement carries no value; a bracket case states one "
                "inside its bracket"
            )
        if any(v is None for v in unmeasured) or not self.measured_by:
            raise ValueError(
                f"{self.id}: an unmeasured parameter names its bracket, the bracket's source, "
                "the measurement that identifies it and the effect it leaves unresolved"
            )
        assert self.bracket is not None
        lo, hi = self.bracket
        if not lo < hi:
            raise ValueError(f"{self.id}: bracket {self.bracket} is empty")
        if (self.bracket_source == "literature_prior") != (self.citation is not None):
            raise ValueError(
                f"{self.id}: a literature prior names its citation, and only a literature "
                "prior does (it is a range for cases, never this engine's number)"
            )
        return self


class Structure(_Strict):
    spark_efficiency: Literal["mbt_relative_quadratic"]
    lambda_efficiency: Literal["peak_quadratic_rich_fuel_limited_lean"]
    friction: Literal["quadratic_in_krpm"]
    pumping: Literal["exhaust_minus_manifold"]
    spark_shape_valid_within_deg: float = Field(gt=0)
    evidence: str = Field(min_length=1)


class Product(_Strict):
    """Parameters a measurement sees only multiplied together (a torque pull
    sees LHV x eta_i0 / AFR_stoich, never the parts). The measurement lists the
    product in ``identifies``, never a component, so a standing cannot call a
    part measured."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    components: tuple[str, ...] = Field(min_length=2)
    measured_by: tuple[str, ...] = Field(min_length=1)
    evidence: str = Field(min_length=1)


class Registry(_Strict):
    parameters: tuple[TorqueParameter, ...] = Field(min_length=1)
    products: tuple[Product, ...] = ()

    @model_validator(mode="after")
    def _complete(self) -> Registry:
        ids = [p.id for p in self.parameters]
        twice = sorted({i for i in ids if ids.count(i) > 1})
        if twice:
            raise ValueError(f"parameters listed twice: {twice}")
        missing = [r for r in READS if r not in ids]
        if missing:
            raise ValueError(
                f"the evaluator reads parameters the registry does not declare: {missing}"
            )
        stray = [i for i in ids if i not in READS]
        if stray:
            raise ValueError(f"declared parameters the evaluator never reads: {stray}")
        for product in self.products:
            unknown = [c for c in product.components if c not in ids]
            if unknown or product.id in ids:
                raise ValueError(
                    f"product {product.id}: components {unknown} are not parameters, or it "
                    "reuses a parameter id"
                )
        return self

    def get(self, parameter_id: str) -> TorqueParameter:
        found = next((p for p in self.parameters if p.id == parameter_id), None)
        if found is None:
            raise KeyError(parameter_id)
        return found


class TorquePlantProfile(_Strict):
    schema_version: Literal[1]
    plant_version: str = Field(min_length=1)
    hardware_configuration: str = Field(min_length=1)
    structure: Structure
    registry: Registry

    def unmeasured(self) -> tuple[str, ...]:
        return tuple(p.id for p in self.registry.parameters if p.status != "known")


def load_torque_plant(profile_dir: Path) -> tuple[TorquePlantProfile, str]:
    """The profile's torque plant declaration and the sha256 of its file."""
    text = (profile_dir / PLANT_FILE).read_bytes()
    profile = TorquePlantProfile.model_validate(load_yaml(text))
    return profile, hashlib.sha256(text).hexdigest()


def check_registry(
    profile: TorquePlantProfile, contract: SimContract, profile_dir: Path
) -> list[str]:
    """Where the registry and the contract (or another plant's registry)
    disagree; empty when they agree.

    The contract's ``torque_production`` must run this module, file and
    version on the declared hardware; every unmeasured parameter's
    ``measured_by`` must be one of the stage's required measurements, and that
    measurement must list the parameter in ``identifies`` (so an owed
    measurement in a standing is the one a parameter waits on); a parameter a
    fuel plant also declares must say the same thing there (a
    ``plant_registry`` bracket must have such an entry); and the samples the
    evaluator reads must be the quantities torque depends on and is bounded by."""
    problems: list[str] = []
    sub = contract.subsystem(SUBSYSTEM)
    impl = sub.implementation
    if impl is None or sub.applicability is None:
        return [f"{SUBSYSTEM}: the contract declares the stage unbuilt"]
    if impl.module != __name__ or impl.profile_file != PLANT_FILE:
        problems.append(f"{SUBSYSTEM}: runs {impl.module}/{impl.profile_file}, not this plant")
    if profile.plant_version not in impl.plant_versions:
        problems.append(f"{SUBSYSTEM}: declares {impl.plant_versions}, not {profile.plant_version}")
    if profile.hardware_configuration not in sub.applicability.hardware_configurations:
        problems.append(f"{profile.hardware_configuration} is outside the stage's hardware")
    measurements = {m.id: m for m in sub.required_measurements}
    products = profile.registry.products
    for prod in products:
        for m_id in prod.measured_by:
            m = measurements.get(m_id)
            if m is None:
                problems.append(f"{prod.id}: measured_by {m_id!r} is not a required measurement")
            elif prod.id not in m.identifies:
                problems.append(f"{prod.id}: {m_id} does not list it in identifies")
    for p in profile.registry.parameters:
        for m_id in p.measured_by:
            m = measurements.get(m_id)
            via = [q.id for q in products if p.id in q.components and m_id in q.measured_by]
            if m is None:
                problems.append(f"{p.id}: measured_by {m_id!r} is not a required measurement")
            elif via and p.id in m.identifies:
                problems.append(
                    f"{p.id}: {m_id} sees it only in {via}, so it lists the product, not the part"
                )
            elif not via and p.id not in m.identifies:
                problems.append(f"{p.id}: {m_id} does not list it in identifies")
    problems += _shared_with_fuel_plants(profile, contract, profile_dir)
    torque = contract.quantity("engine_torque")
    wanted = {d.quantity for d in torque.depends_on} | {
        b.quantity for b in sub.applicability.bounds
    }
    if wanted != set(INPUTS):
        problems.append(
            f"evaluator inputs {sorted(INPUTS)} are not engine_torque's inputs and bounds "
            f"{sorted(wanted)}"
        )
    return problems


def _shared_with_fuel_plants(
    profile: TorquePlantProfile, contract: SimContract, profile_dir: Path
) -> list[str]:
    fuel = contract.subsystem("fuel_delivery").implementation
    versions = [] if fuel is None else fuel.plant_versions
    problems: list[str] = []
    for p in profile.registry.parameters:
        found = False
        for version in versions:
            plant, _ = load_fuel_plant(profile_dir, version)
            try:
                other = plant.registry.get(p.id)
            except KeyError:
                continue
            found = True
            if p.status == "known":
                if other.status != "known" or other.value != p.value:
                    problems.append(f"{p.id}: {p.value} here, {other.value} in {version}")
                continue
            wa = other.working_assumption
            theirs = None if wa is None else tuple(wa.bracket)
            if theirs != p.bracket:
                problems.append(f"{p.id}: bracket {p.bracket} here, {theirs} in {version}")
        if p.bracket_source == "plant_registry" and not found:
            problems.append(f"{p.id}: a plant_registry bracket no fuel plant declares")
    return problems


# -- the structure ---------------------------------------------------------------------


def _f(x: ArrayLike) -> FloatArray:
    return np.asarray(x, dtype=np.float64)


def retard_from_mbt(spark_btdc: ArrayLike, mbt_btdc: ArrayLike | None) -> FloatArray:
    """Degrees of retard from MBT (negative: advanced past it). Refused without
    MBT: the commanded timing is where the calibration put spark, not MBT."""
    if mbt_btdc is None:
        raise SimulationRefusalError(
            "spark_mbt_unstated",
            "MBT is a requires_measurement parameter (controlled_spark_sweep); retard from MBT "
            "cannot be computed from the commanded timing alone",
        )
    return _f(mbt_btdc) - _f(spark_btdc)


def spark_efficiency(retard_deg: ArrayLike, curvature_per_deg2: float) -> FloatArray:
    """``1 - c x retard^2``: 1 at MBT, symmetric about it, flat at it."""
    r = _f(retard_deg)
    return 1.0 - curvature_per_deg2 * r * r


def lambda_efficiency(
    lam: ArrayLike, peak: float, peak_gain: float, rich_curvature: float
) -> FloatArray:
    """Torque at fixed air relative to stoich: ``1 + G`` at ``peak``, 1 at
    stoich (the quadratic through both between them), ``1 + G - k (lambda -
    peak)^2`` richer than the peak, and fuel-limited ``1 / lambda`` lean."""
    if not 0.0 < peak < 1.0:
        raise ValueError(f"lambda peak {peak} must lie rich of stoich")
    x = _f(lam)
    between = 1.0 + peak_gain - peak_gain * ((x - peak) / (1.0 - peak)) ** 2
    rich = 1.0 + peak_gain - rich_curvature * (x - peak) ** 2
    with np.errstate(divide="ignore", invalid="ignore"):
        lean = 1.0 / x
    return np.where(x > 1.0, lean, np.where(x >= peak, between, rich))


def air_per_event_g(cylinder_air_gps: ArrayLike, rpm: ArrayLike, cylinders: float) -> FloatArray:
    """Air each intake event draws: four-stroke, one event per cylinder per two
    revolutions."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return _f(cylinder_air_gps) * 120.0 / (_f(rpm) * cylinders)


def mep_torque_nm(mep_kpa: ArrayLike, displacement_l: float) -> FloatArray:
    """A mean effective pressure as crank torque: ``mep x Vd / 4 pi`` (kPa x L = J)."""
    return _f(mep_kpa) * displacement_l / (4.0 * math.pi)


def friction_mep_kpa(rpm: ArrayLike, f0: float, f1: float, f2: float) -> FloatArray:
    n = _f(rpm) / 1000.0
    return f0 + f1 * n + f2 * n * n


# -- a bracket case --------------------------------------------------------------------


@dataclass(frozen=True)
class TorqueParameters:
    """One point in the registry's space: the known values and the values the
    caller stated for every unmeasured parameter.

    Only :func:`parameter_case` makes one (it checks every value against the
    registry): built by hand it is refused (``parameters_not_from_registry``),
    so a case cannot skip the brackets, the MBT refusal or the named effects."""

    values: Mapping[str, float]
    #: The unmeasured parameters, every one stated by the caller.
    stated: tuple[str, ...]
    plant_version: str
    _issued_by: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issued_by is not _ISSUER:
            raise SimulationRefusalError(
                "parameters_not_from_registry",
                "a parameter case is made by parameter_case(profile, stated), which checks "
                "every value against the registry",
            )

    def __getitem__(self, key: str) -> float:
        return self.values[key]


_ISSUER = object()


def parameter_case(profile: TorquePlantProfile, stated: Mapping[str, float]) -> TorqueParameters:
    """The known values plus ``stated``, refused unless every unmeasured
    parameter is stated (MBT first: ``spark_mbt_unstated``), each inside its
    bracket (``outside_bracket``), and no known one is restated."""
    reg = profile.registry
    stray = sorted(set(stated) - {p.id for p in reg.parameters})
    if stray:
        raise SimulationRefusalError("parameter_undeclared", f"no registry parameters {stray}")
    restated = sorted(k for k in stated if reg.get(k).status == "known")
    if restated:
        raise SimulationRefusalError(
            "parameter_known", f"{restated} are known; their values are the registry's"
        )
    missing = [p for p in profile.unmeasured() if p not in stated]
    if MBT in missing:
        retard_from_mbt(0.0, None)
    if missing:
        raise SimulationRefusalError(
            "parameter_unstated",
            f"{missing} require measurement and carry no value; a case states each inside its "
            "bracket",
        )
    values: dict[str, float] = {}
    for p in reg.parameters:
        if p.status == "known":
            assert p.value is not None
            values[p.id] = p.value
            continue
        v = float(stated[p.id])
        assert p.bracket is not None
        lo, hi = p.bracket
        if not (math.isfinite(v) and lo <= v <= hi):
            raise SimulationRefusalError(
                "outside_bracket", f"{p.id} = {v} is outside its bracket {p.bracket}"
            )
        values[p.id] = v
    return TorqueParameters(values, profile.unmeasured(), profile.plant_version, _ISSUER)


@dataclass(frozen=True)
class TorqueResult:
    """One bracket case over the given samples. Never a prediction: ``kind``
    says so, and every parameter it rests on and every effect it leaves
    unresolved is named."""

    plant_version: str
    plant_sha256: str
    kind: Literal["bracket_case"]
    parameters: Mapping[str, float]
    unmeasured: tuple[str, ...]
    effects: tuple[str, ...]
    air_per_event_g: FloatArray
    eta_spark: FloatArray
    eta_lambda: FloatArray
    indicated_nm: FloatArray
    friction_nm: FloatArray
    pumping_nm: FloatArray
    brake_nm: FloatArray
    #: Per refusal reason, the samples it applies to (NaN in every array).
    refused: Mapping[str, NDArray[np.bool_]] = field(default_factory=dict)


def evaluate(
    profile: TorquePlantProfile,
    plant_sha256: str,
    contract: SimContract,
    stated: Mapping[str, float],
    samples: Mapping[str, ArrayLike],
) -> TorqueResult:
    """Brake torque and its parts for each sample, at one stated parameter case.

    ``stated`` is the caller's value for every unmeasured parameter; the case
    is made here by :func:`parameter_case` (an unstated MBT is
    ``spark_mbt_unstated``, a value off its bracket ``outside_bracket``), so no
    route reaches the equations around the registry. ``samples`` maps each of
    :data:`INPUTS` (contract quantity ids, in the contract's units) to
    equal-length values. Outside the stage's applicability,
    further from MBT than the declared shape, or with MAP above ambient, a
    sample is NaN and named; nothing is clipped."""
    if not isinstance(stated, Mapping):
        raise SimulationRefusalError(
            "parameters_not_stated",
            f"evaluate takes the stated values (a mapping), not {type(stated).__name__}",
        )
    params = parameter_case(profile, stated)
    missing = [q for q in INPUTS if q not in samples]
    if missing:
        raise SimulationRefusalError("input_missing", f"samples lack {missing}")
    sub = contract.subsystem(SUBSYSTEM)
    if sub.status != "built" or sub.applicability is None:
        raise SimulationRefusalError("subsystem_unbuilt", f"{SUBSYSTEM} is not built")
    if profile.hardware_configuration not in sub.applicability.hardware_configurations:
        raise SimulationRefusalError(
            "outside_applicability", f"{profile.hardware_configuration} is another plant"
        )
    x = {q: _f(samples[q]) for q in INPUTS}
    n = {v.shape for v in x.values()}
    if len(n) != 1:
        raise SimulationRefusalError("input_shape", f"samples differ in shape: {sorted(n)}")
    p = params
    refused: dict[str, NDArray[np.bool_]] = {}
    for b in sub.applicability.bounds:
        v = x[b.quantity]
        out = ~((v >= b.minimum) & (v <= b.maximum))
        if out.any():
            refused[f"outside_applicability:{b.quantity}"] = out
    above = x["manifold_pressure"] > x["barometric_pressure"]
    if above.any():
        refused["manifold_above_ambient"] = above
    retard = retard_from_mbt(x["spark_advance"], p[MBT])
    far = np.abs(retard) > profile.structure.spark_shape_valid_within_deg
    if far.any():
        refused["spark_outside_shape"] = far

    eta_s = spark_efficiency(retard, p["spark_efficiency_curvature"])
    eta_l = lambda_efficiency(
        x["delivered_lambda"],
        p["lambda_peak"],
        p["lambda_peak_gain"],
        p["lambda_rich_curvature"],
    )
    air = air_per_event_g(x["cylinder_air_flow"], x["engine_speed"], p["cylinders"])
    # J per event of fuel energy at stoich: g x MJ/kg = kJ.
    event_j = air * p["fuel_lower_heating_value"] * 1000.0 / p["fuel_stoich_afr"]
    cycle_j = p["cylinders"] * event_j * p["indicated_efficiency_peak"] * eta_l * eta_s
    indicated = cycle_j / (4.0 * math.pi)
    vd = p["displacement"]
    friction = mep_torque_nm(
        friction_mep_kpa(
            x["engine_speed"], p["friction_fmep_0"], p["friction_fmep_1"], p["friction_fmep_2"]
        ),
        vd,
    )
    pumping = mep_torque_nm(
        x["barometric_pressure"] + p["exhaust_back_pressure"] - x["manifold_pressure"], vd
    )
    brake = indicated - friction - pumping
    bad = np.zeros(next(iter(n)), dtype=bool)
    for mask in refused.values():
        bad |= mask
    arrays = [air, eta_s, eta_l, indicated, friction, pumping, brake]
    air, eta_s, eta_l, indicated, friction, pumping, brake = (
        np.where(bad, np.nan, a) for a in arrays
    )
    effects = tuple(sorted({str(profile.registry.get(k).effect) for k in params.stated}))
    return TorqueResult(
        plant_version=profile.plant_version,
        plant_sha256=plant_sha256,
        kind="bracket_case",
        parameters=dict(params.values),
        unmeasured=params.stated,
        effects=effects,
        air_per_event_g=air,
        eta_spark=eta_s,
        eta_lambda=eta_l,
        indicated_nm=indicated,
        friction_nm=friction,
        pumping_nm=pumping,
        brake_nm=brake,
        refused=refused,
    )


__all__ = [
    "COMPUTES_INTERNALLY",
    "INPUTS",
    "PLANT_FILE",
    "READS",
    "SUBSYSTEM",
    "Product",
    "TorqueParameter",
    "TorqueParameters",
    "TorquePlantProfile",
    "TorqueResult",
    "air_per_event_g",
    "check_registry",
    "evaluate",
    "friction_mep_kpa",
    "lambda_efficiency",
    "load_torque_plant",
    "mep_torque_nm",
    "parameter_case",
    "retard_from_mbt",
    "spark_efficiency",
]
