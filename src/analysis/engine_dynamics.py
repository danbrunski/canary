"""Dynamic plant stages for the engine simulator (E07.5), and what they may claim.

E07.5 adds the plant in stages, each a ``subsystems`` entry in the profile's
``engine_sim_contract.yaml`` (:class:`~src.analysis.sim_contract.Subsystem`):
what it computes, where its equations hold, how its numerics are checked and
which independent measurement could validate it. This module implements the
first built stage and derives every stage's standing; nothing in the contract
can *state* that a stage is validated.

**Stage 1, ``manifold_breathing`` (mean-value, one lumped manifold).** States:
the manifold's air mass ``m`` and internal energy ``E = cv m T`` (energy balance
``adiabatic``) or ``m`` alone at the intake temperature (``isothermal``)::

    dm/dt = mdot_in - mdot_cyl
    dE/dt = cp mdot_in T_in - cp mdot_cyl T_m           (adiabatic)
    mdot_cyl = VE * (m / V) * Vd * rpm / 120              (four-stroke breathing)
    p_m = m R T_m / V

In plant replay ``mdot_in`` is the recorded MAF (the meter sits upstream of the
throttle, so it measures what enters the manifold) and rpm is prescribed. The
steady state is ``mdot_cyl = mdot_in``, ``T_m = T_in`` and ``p_m`` equal to
:func:`src.analysis.fuel_plant.manifold_pressure_speed_density` at the same VE
-- one assumption with fuel-mv-1, read from its registry, never restated.
Integration is the contract's fixed-step method on its solver step, inputs
held over each step (rk4: the study below showed Euler too coarse at 1.28 ms).

What the module refuses rather than documents:

* **Outside the declared envelope.** A boundary outside the subsystem's
  ``applicability`` bounds, or hardware it was not declared for, is refused by
  name (``outside_applicability``); a sample whose manifold pressure exceeds
  ambient on a naturally aspirated engine is reported as NaN with the reason
  ``manifold_above_ambient``, never clipped to baro.
* **Unconverged or non-conserving numerics.** :func:`convergence_study` runs the
  contract's refinement ladder at every applicability corner and bracket end;
  :func:`subsystem_standing` reads its verdict, so a solver step that does not
  converge cannot be used quietly, and :func:`replay_manifold` runs only on the
  contract's solver step with a passing study for it. :func:`consistency`
  checks a trace against the declared equations with code of its own: each
  stored state must follow from the previous one under the declared method,
  and the reported cylinder flow must be the breathing law applied to the
  stored states (to the method's quadrature order). An integrator that drew
  more air than the law, or mis-booked energy, fails it.
* **Validation against a circular reference.** :func:`reference_lineage` derives
  what a reference channel is computed from -- the contract's own graph (an ECU
  word from the sensors it reads), the derived-channel registry, and any
  declared ``derived_from`` -- and :func:`check_reference` refuses one that
  shares an input with the subsystem (``reference_not_independent``). BFS
  (0x1208) is K x MAF / rpm through the ECU's own filling filter, so it cannot
  validate a manifold driven by MAF; ``derived:ESTIMATED_TORQUE_NM`` is a
  fuel-energy estimate from MAF, so it cannot calibrate torque.
* **Claims beyond the standing.** :func:`claim_refusals` refuses an
  acceleration claim from a mode that prescribes rpm
  (``prescribed_rpm_not_acceleration``), a torque/power claim from an
  unvalidated torque stage, any knock claim (no stage computes knock), and any
  claim about a changed control (spark, cam) whose response no available
  measurement excites (``<control>_response_unmeasured``).
"""

from __future__ import annotations

import hashlib
import importlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml
from numpy.typing import ArrayLike, NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.channels import read_channels
from src.analysis.fuel_plant import (
    KELVIN,
    R_AIR,
    FuelPlantProfile,
    Geometry,
    PlantParameter,
    load_fuel_plant,
    plant_qualification,
)
from src.analysis.sim_contract import (
    NumericalCheck,
    SimContract,
    SimulationRefusalError,
    Subsystem,
    ValidationReference,
    load_contract,
)
from src.signals.derived import DERIVED_CHANNEL_INPUTS, DERIVED_CHANNEL_OPTIONAL_INPUTS
from src.signals.pairing import PairingPolicy, match_ordered, to_seconds

MANIFOLD_FILE = "manifold_plant.yaml"
MANIFOLD_SUBSYSTEM = "manifold_breathing"
#: The fuel-plant registry parameter whose working assumption is the VE both
#: plants run at.
VE_PARAMETER = "manifold_pressure"
#: The parameters the manifold reads from its own profile.
MANIFOLD_READS: tuple[str, ...] = ("manifold_volume", "heat_capacity_ratio")
#: Every claim :func:`claim_refusals` understands.
CLAIMS: tuple[str, ...] = ("acceleration", "torque", "power", "knock")
#: The quantity each claim is about.
_CLAIM_QUANTITY: dict[str, str] = {
    "acceleration": "engine_speed",
    "torque": "engine_torque",
    "power": "engine_torque",
}

EnergyBalance = Literal["adiabatic", "isothermal"]
SolverMethod = Literal["fixed_step_euler", "rk4"]
#: Theoretical global order of each fixed-step method.
METHOD_ORDER: dict[str, float] = {"fixed_step_euler": 1.0, "rk4": 4.0}
#: Relative errors below this are rounding: no order is measured from them.
ROUNDING_FLOOR = 1e-9
FloatArray = NDArray[np.float64]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- the manifold profile -----------------------------------------------------------


class Study(_Strict):
    """The step-size refinement test case (see ``manifold_plant.yaml``)."""

    from_fraction_of_ambient_flow: float = Field(gt=0, lt=1)
    to_fraction_of_ambient_flow: float = Field(gt=0, le=1)
    tau_multiples: float = Field(gt=1)
    iat_c: float
    baro_kpa: float = Field(gt=0)
    evidence: str = Field(min_length=1)


class ManifoldPlantProfile(_Strict):
    schema_version: Literal[1]
    plant_version: str = Field(min_length=1)
    hardware_configuration: str = Field(min_length=1)
    energy_balance: EnergyBalance
    parameters: list[PlantParameter] = Field(min_length=1)
    study: Study

    @model_validator(mode="after")
    def _reads(self) -> ManifoldPlantProfile:
        ids = [p.id for p in self.parameters]
        missing = [p for p in MANIFOLD_READS if p not in ids]
        if missing:
            raise ValueError(f"the manifold reads parameters the profile omits: {missing}")
        for p in self.parameters:
            if p.status == "identifiable":
                raise ValueError(
                    f"{p.id}: nothing logged on this vehicle identifies a manifold parameter "
                    "(no MAP); declare it known or requires_measurement"
                )
            if p.status == "requires_measurement" and p.working_assumption is None:
                raise ValueError(f"{p.id}: requires_measurement runs under a working assumption")
        return self

    def get(self, parameter_id: str) -> PlantParameter:
        for p in self.parameters:
            if p.id == parameter_id:
                return p
        raise KeyError(parameter_id)


def load_manifold_plant(profile_dir: Path) -> tuple[ManifoldPlantProfile, str]:
    text = (profile_dir / MANIFOLD_FILE).read_bytes()
    return (
        ManifoldPlantProfile.model_validate(yaml.safe_load(text)),
        hashlib.sha256(text).hexdigest(),
    )


@dataclass(frozen=True)
class ManifoldParameters:
    """The numbers one manifold run uses and the unresolved effects they rest on."""

    volume_l: float
    volumetric_efficiency: float
    gamma: float
    displacement_l: float
    energy_balance: EnergyBalance
    effects: tuple[str, ...]

    def tau_s(self, rpm: ArrayLike) -> FloatArray:
        """The filling time constant 120 V / (VE Vd rpm), s."""
        return 120.0 * self.volume_l / (self.volumetric_efficiency * self.displacement_l * _f(rpm))


def _bracket(p: PlantParameter) -> tuple[float, float, float, str]:
    if p.working_assumption is not None:
        wa = p.working_assumption
        return wa.value, wa.bracket[0], wa.bracket[1], wa.effect
    assert p.value is not None
    return p.value, p.value, p.value, ""


def resolve_manifold(
    profile: ManifoldPlantProfile,
    fuel: FuelPlantProfile,
    *,
    overrides: Mapping[str, float] | None = None,
    energy_balance: EnergyBalance | None = None,
) -> ManifoldParameters:
    """The manifold's numbers: its own profile's, VE from the fuel plant's
    registry, ``overrides`` only inside a declared bracket."""
    if profile.hardware_configuration != fuel.hardware_configuration:
        raise SimulationRefusalError(
            "hardware_mismatch",
            f"manifold {profile.hardware_configuration}, fuel plant {fuel.hardware_configuration}",
        )
    overrides = dict(overrides or {})
    sources = {
        "manifold_volume": profile.get("manifold_volume"),
        "heat_capacity_ratio": profile.get("heat_capacity_ratio"),
        "volumetric_efficiency": fuel.registry.get(VE_PARAMETER),
    }
    values: dict[str, float] = {}
    effects: list[str] = []
    for key, param in sources.items():
        value, lo, hi, effect = _bracket(param)
        if key in overrides:
            new = overrides.pop(key)
            if lo == hi or not lo <= new <= hi:
                raise SimulationRefusalError(
                    "override_outside_bracket", f"{key}={new} outside [{lo}, {hi}]"
                )
            value = new
        values[key] = float(value)
        if effect:
            effects.append(effect)
    if overrides:
        raise SimulationRefusalError("unknown_override", f"not manifold parameters: {overrides}")
    form = energy_balance or profile.energy_balance
    effects += ["manifold_heat_transfer_unmodelled", "maf_response_unmeasured"]
    return ManifoldParameters(
        volume_l=values["manifold_volume"],
        volumetric_efficiency=values["volumetric_efficiency"],
        gamma=values["heat_capacity_ratio"],
        displacement_l=fuel.geometry.displacement_l,
        energy_balance=form,
        effects=tuple(effects),
    )


def bracket_cases(
    profile: ManifoldPlantProfile, fuel: FuelPlantProfile
) -> dict[str, ManifoldParameters]:
    """The working case and every bracket-end combination of volume and VE,
    under both energy balances: the cases the convergence study and a
    sensitivity run over."""
    _, v_lo, v_hi, _ = _bracket(profile.get("manifold_volume"))
    _, e_lo, e_hi, _ = _bracket(fuel.registry.get(VE_PARAMETER))
    cases = {"working": resolve_manifold(profile, fuel)}
    for form in ("adiabatic", "isothermal"):
        for v in (v_lo, v_hi):
            for ve in (e_lo, e_hi):
                cases[f"{form}:V={v}:VE={ve}"] = resolve_manifold(
                    profile,
                    fuel,
                    overrides={"manifold_volume": v, "volumetric_efficiency": ve},
                    energy_balance=form,
                )
    return cases


# -- physics ------------------------------------------------------------------------


def _f(values: ArrayLike) -> FloatArray:
    return np.asarray(values, dtype=float)


def _cv(gamma: float) -> float:
    return R_AIR / (gamma - 1.0)


def steady_mass_g(maf_gps: ArrayLike, rpm: ArrayLike, params: ManifoldParameters) -> FloatArray:
    """Manifold air mass at which the cylinders draw exactly ``maf_gps``."""
    return _f(maf_gps) * params.tau_s(rpm)


def pressure_kpa(mass_g: ArrayLike, temp_k: ArrayLike, volume_l: ArrayLike) -> FloatArray:
    """Ideal gas: p = m R T / V, kPa (m in g, V in L)."""
    return _f(mass_g) * R_AIR * _f(temp_k) / (_f(volume_l) * 1000.0)


@dataclass(frozen=True)
class ManifoldTrace:
    """One manifold run, per solver step ``k`` over ``[t_k, t_k + h)``.

    ``mass_g``/``energy_j``/``temp_k``/``map_kpa`` are the states at ``t_k``;
    ``cylinder_gps`` and ``enthalpy_out_w`` are what left the manifold *during*
    step ``k`` (the integrator's own weighted stage average). Those are the
    values the fuel and torque stages read, and the ones :func:`consistency`
    holds to the breathing law."""

    t_s: FloatArray
    step_s: float
    method: SolverMethod
    inflow_gps: FloatArray
    rpm: FloatArray
    iat_k: FloatArray
    mass_g: FloatArray
    energy_j: FloatArray
    temp_k: FloatArray
    map_kpa: FloatArray
    cylinder_gps: FloatArray
    enthalpy_out_w: FloatArray
    #: The state after the last step.
    final_mass_g: FloatArray
    final_energy_j: FloatArray
    params: tuple[ManifoldParameters, ...]


def integrate(
    inflow_gps: ArrayLike,
    rpm: ArrayLike,
    iat_c: ArrayLike,
    params: Sequence[ManifoldParameters],
    *,
    step_s: float,
    method: SolverMethod,
    initial_mass_g: ArrayLike | None = None,
    initial_temp_k: ArrayLike | None = None,
) -> ManifoldTrace:
    """Integrate ``(m, E)`` with the contract's fixed-step ``method``; inputs are
    ``(n_steps, n_cases)`` (or ``(n_steps,)`` for one case), held over each step.

    The state starts settled at the first inputs unless given. Each case may
    have its own parameters (vectorised for the refinement study). Isothermal
    cases hold ``T_m`` at the intake temperature: the walls exchange whatever
    heat that takes, so ``E`` is tracked at ``T_in``."""
    if not step_s > 0:
        raise SimulationRefusalError("step_nonpositive", "the solver step must be > 0")
    q_in = _f(inflow_gps)
    if q_in.ndim == 1:
        q_in = np.repeat(q_in[:, None], len(params), axis=1)
    n, c = q_in.shape
    if len(params) != c:
        raise ValueError(f"{len(params)} parameter sets for {c} cases")
    speed = np.broadcast_to(np.atleast_2d(_f(rpm).T).T, (n, c))
    t_in = np.broadcast_to(np.atleast_2d(_f(iat_c).T).T, (n, c)) + KELVIN
    vol = np.array([p.volume_l for p in params])
    ve = np.array([p.volumetric_efficiency for p in params])
    disp = np.array([p.displacement_l for p in params])
    gamma = np.array([p.gamma for p in params])
    adiabatic = np.array([p.energy_balance == "adiabatic" for p in params])
    cv = R_AIR / (gamma - 1.0) / 1000.0  # J/(g K)
    cp = gamma * cv
    breathe = ve * disp / (120.0 * vol)  # mdot_cyl = m * breathe * rpm
    m = (
        q_in[0] / (breathe * speed[0])
        if initial_mass_g is None
        else np.broadcast_to(_f(initial_mass_g), (c,)).astype(float)
    )
    temp0 = t_in[0] if initial_temp_k is None else np.broadcast_to(_f(initial_temp_k), (c,))
    e = cv * m * temp0

    def rates(
        m_: FloatArray, e_: FloatArray, q: FloatArray, w: FloatArray, tin: FloatArray
    ) -> tuple[FloatArray, FloatArray]:
        t_m = np.where(adiabatic, e_ / (cv * m_), tin)
        out = m_ * breathe * w
        return q - out, cp * (q * tin - out * t_m)

    mass = np.empty((n, c))
    energy = np.empty((n, c))
    temp = np.empty((n, c))
    cyl = np.empty((n, c))
    h_out = np.empty((n, c))
    for k in range(n):
        q, w, tin = q_in[k], speed[k], t_in[k]
        mass[k], energy[k] = m, e
        temp[k] = np.where(adiabatic, e / (cv * m), tin)
        if method == "fixed_step_euler":
            dm, de = rates(m, e, q, w, tin)
        else:
            k1 = rates(m, e, q, w, tin)
            k2 = rates(m + step_s / 2 * k1[0], e + step_s / 2 * k1[1], q, w, tin)
            k3 = rates(m + step_s / 2 * k2[0], e + step_s / 2 * k2[1], q, w, tin)
            k4 = rates(m + step_s * k3[0], e + step_s * k3[1], q, w, tin)
            dm = (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0]) / 6.0
            de = (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1]) / 6.0
        # What left during the step is what entered less what stayed.
        cyl[k] = q - dm
        h_out[k] = cp * q * tin - de
        m = m + step_s * dm
        e = np.where(adiabatic, e + step_s * de, cv * m * t_in[min(k + 1, n - 1)])
    return ManifoldTrace(
        t_s=np.arange(n, dtype=float) * step_s,
        step_s=step_s,
        method=method,
        inflow_gps=q_in,
        rpm=np.asarray(speed, dtype=float),
        iat_k=np.asarray(t_in, dtype=float),
        mass_g=mass,
        energy_j=energy,
        temp_k=temp,
        map_kpa=pressure_kpa(mass, temp, vol),
        cylinder_gps=cyl,
        enthalpy_out_w=h_out,
        final_mass_g=m,
        final_energy_j=e,
        params=tuple(params),
    )


def _law_rates(
    m: FloatArray,
    e: FloatArray,
    q: FloatArray,
    rpm: FloatArray,
    tin_k: FloatArray,
    params: Sequence[ManifoldParameters],
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """The declared equations, written apart from :func:`integrate` so a fault
    in its rate code is not shared: ``(dm/dt, dE/dt, mdot_cyl)``."""
    vol = np.array([p.volume_l for p in params])
    ve = np.array([p.volumetric_efficiency for p in params])
    disp = np.array([p.displacement_l for p in params])
    gamma = np.array([p.gamma for p in params])
    adiabatic = np.array([p.energy_balance == "adiabatic" for p in params])
    cv = R_AIR / (gamma - 1.0) / 1000.0
    out = ve * (m / vol) * disp * rpm / 120.0  # the breathing law
    with np.errstate(divide="ignore", invalid="ignore"):
        t_m = np.where(adiabatic, e / (cv * m), tin_k)
    return q - out, gamma * cv * (q * tin_k - out * t_m), out


@dataclass(frozen=True)
class Consistency:
    """How far a trace is from the declared equations, per case.

    ``equation_residual_rel``: the largest difference between a stored state
    and one step of the declared method from the state before it (relative to
    the state's range of magnitude); rounding for a correct integrator.
    ``breathing_excess``: the largest ratio of |reported cylinder flow - the
    breathing law over the step| to the method's quadrature bound; at most 1.
    ``nonphysical``: per step, a state that is non-finite or not positive."""

    equation_residual_rel: FloatArray
    breathing_excess: FloatArray
    nonphysical: NDArray[np.bool_]


def consistency(trace: ManifoldTrace) -> Consistency:
    """Check ``trace`` against the declared equations with independent code.

    The stored states must follow one from the next under ``trace.method``
    (re-stepped with :func:`_law_rates`), and the reported cylinder flow must
    be the breathing law ``VE m/V Vd rpm/120`` over each step: exactly the
    start-of-step value for Euler, and for rk4 the trapezoid of the two stored
    endpoints within its error bound ``(h/tau)^2/12`` of the step's distance
    from steady flow (doubled), plus rounding.

    Residuals are taken over the steps whose start and end states are both
    physical, so a nonphysical sample is named (``nonphysical``) and can never
    hide an inconsistency elsewhere in the trace."""
    h, params = trace.step_s, trace.params
    n = len(trace.t_s)
    m = trace.mass_g
    e = trace.energy_j
    m_next = np.vstack([m[1:], trace.final_mass_g[None, :]])
    e_next = np.vstack([e[1:], trace.final_energy_j[None, :]])
    q = trace.inflow_gps
    tin = trace.iat_k
    rpm = trace.rpm
    adiabatic = np.array([p.energy_balance == "adiabatic" for p in params])
    nonphysical = ~(np.isfinite(m) & np.isfinite(e) & (m > 0) & (trace.temp_k > 0))
    final_ok = (
        np.isfinite(trace.final_mass_g)
        & np.isfinite(trace.final_energy_j)
        & (trace.final_mass_g > 0)
    )
    # A step is checked when it starts and ends on a physical state.
    physical_step = ~nonphysical & ~np.vstack([nonphysical[1:], ~final_ok[None, :]])
    with np.errstate(all="ignore"):
        if trace.method == "fixed_step_euler":
            dm, de, _ = _law_rates(m, e, q, rpm, tin, params)
        else:
            k1 = _law_rates(m, e, q, rpm, tin, params)
            k2 = _law_rates(m + h / 2 * k1[0], e + h / 2 * k1[1], q, rpm, tin, params)
            k3 = _law_rates(m + h / 2 * k2[0], e + h / 2 * k2[1], q, rpm, tin, params)
            k4 = _law_rates(m + h * k3[0], e + h * k3[1], q, rpm, tin, params)
            dm = (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0]) / 6.0
            de = (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1]) / 6.0
        stepped_m = m + h * dm
        # Isothermal: the walls hold T at the next intake temperature.
        tin_next = np.vstack([tin[1:], tin[-1:]])
        cv = np.array([R_AIR / (p.gamma - 1.0) / 1000.0 for p in params])
        stepped_e = np.where(adiabatic, e + h * de, cv * m_next * tin_next)
        m_scale = _masked_max(np.abs(m), ~nonphysical)
        e_scale = _masked_max(np.abs(e), ~nonphysical)
        eq = np.maximum(
            _masked_max(np.abs(stepped_m - m_next), physical_step) / m_scale,
            _masked_max(np.abs(stepped_e - e_next), physical_step) / e_scale,
        )
        law_k = _law_rates(m, e, q, rpm, tin, params)[2]
        law_next = _law_rates(m_next, e_next, q, rpm, tin, params)[2]
        rounding = 1e-9 * np.max(np.abs(q), axis=0) + 1e-12
        if trace.method == "fixed_step_euler":
            quad, bound = law_k, np.broadcast_to(rounding, law_k.shape)
        else:
            quad = (law_k + law_next) / 2.0
            tau = np.array([[p.tau_s(r) for p, r in zip(params, row, strict=True)] for row in rpm])
            bound = 2.0 * (h / tau) ** 2 / 12.0 * np.abs(law_k - q) + rounding
        excess = _masked_max(np.abs(trace.cylinder_gps - quad) / bound, physical_step)
    del n
    return Consistency(
        equation_residual_rel=np.where(np.isnan(eq), 0.0, np.where(np.isfinite(eq), eq, np.inf)),
        breathing_excess=np.where(
            np.isnan(excess), 0.0, np.where(np.isfinite(excess), excess, np.inf)
        ),
        nonphysical=nonphysical,
    )


def _masked_max(values: FloatArray, mask: NDArray[np.bool_]) -> FloatArray:
    """Column-wise max over ``mask``; NaN for a column with nothing selected,
    ``inf`` where a selected value is not finite."""
    v = np.where(mask, np.where(np.isfinite(values), values, np.inf), -np.inf)
    out = np.max(v, axis=0) if len(v) else np.full(values.shape[1:], -np.inf)
    return np.where(np.isneginf(out), np.nan, out)


# -- applicability ------------------------------------------------------------------


def check_applicability(
    subsystem: Subsystem, hardware_configuration: str, values: Mapping[str, ArrayLike]
) -> None:
    """Refuse a run whose hardware or boundary values leave the declared
    envelope. Every bounded quantity must be supplied; nothing is clipped."""
    app = subsystem.applicability
    if app is None:
        raise SimulationRefusalError("subsystem_unbuilt", f"{subsystem.id} declares no envelope")
    if hardware_configuration not in app.hardware_configurations:
        raise SimulationRefusalError(
            "outside_applicability",
            f"{subsystem.id} holds for {app.hardware_configurations}, not "
            f"{hardware_configuration} (a new hardware configuration is a new plant)",
        )
    for bound in app.bounds:
        if bound.quantity not in values:
            raise SimulationRefusalError(
                "outside_applicability", f"{bound.quantity} is bounded and was not supplied"
            )
        arr = _f(values[bound.quantity])
        if not np.all(np.isfinite(arr)):
            raise SimulationRefusalError(
                "outside_applicability", f"{bound.quantity} has missing values"
            )
        lo, hi = float(arr.min()), float(arr.max())
        if lo < bound.minimum or hi > bound.maximum:
            raise SimulationRefusalError(
                "outside_applicability",
                f"{bound.quantity} spans {lo:g}..{hi:g}, outside {bound.minimum:g}.."
                f"{bound.maximum:g} ({subsystem.id})",
            )


# -- plant replay -------------------------------------------------------------------


def hold_on_grid(times_s: ArrayLike, values: ArrayLike, grid_s: ArrayLike) -> FloatArray:
    """Zero-order hold: the last read at or before each grid time (NaN before
    the first). A boundary is held between acquisitions, never interpolated."""
    t, v, g = _f(times_s), _f(values), _f(grid_s)
    order = np.argsort(t, kind="stable")
    t, v = t[order], v[order]
    i = np.searchsorted(t, g, side="right") - 1
    out = np.full(len(g), np.nan)
    ok = i >= 0
    out[ok] = v[i[ok]]
    return out


@dataclass(frozen=True)
class ManifoldReplay:
    """Stage 1 under the plant-replay mode: recorded MAF in, prescribed rpm.

    Not a prediction of speed or acceleration: rpm is a boundary here.
    ``refused`` marks samples whose outputs are NaN, for the reasons in
    ``refusal_reasons``: ``manifold_above_ambient`` (the working VE contradicts
    the airflow there) or ``state_nonphysical`` (a non-finite or non-positive
    state)."""

    t_s: FloatArray
    map_kpa: FloatArray
    temp_c: FloatArray
    cylinder_gps: FloatArray
    refused: NDArray[np.bool_]
    above_ambient: NDArray[np.bool_]
    nonphysical: NDArray[np.bool_]
    #: The shortest structure a result resolves at each step: one engine
    #: cycle, 120 / rpm s (the contract's ``time_resolution``).
    resolved_period_s: FloatArray
    consistency: Consistency
    step_s: float
    effects: tuple[str, ...]
    prescribed: tuple[str, ...] = ("engine_speed", "mass_air_flow")
    labels: dict[str, str] = field(
        default_factory=lambda: {
            "engine_speed": "prescribed boundary: not a prediction of speed or acceleration"
        }
    )

    def refusal_reasons(self) -> dict[str, int]:
        counts = {
            "manifold_above_ambient": int(self.above_ambient.sum()),
            "state_nonphysical": int(self.nonphysical.sum()),
        }
        return {k: v for k, v in counts.items() if v}


def replay_manifold(
    contract: SimContract,
    hardware_configuration: str,
    params: ManifoldParameters,
    *,
    times_s: Mapping[str, ArrayLike],
    values: Mapping[str, ArrayLike],
    convergence: ConvergenceReport,
) -> ManifoldReplay:
    """Replay stage 1 over one in-envelope stretch, on the contract's solver.

    The step and method are the contract's, never the caller's, and
    ``convergence`` must be a passing study of exactly that step, method and
    declared check (``numerics_unverified`` otherwise): a coarse step is how a
    stable-looking replay returns megapascals of negative pressure.

    ``times_s`` / ``values`` hold each boundary's own acquisition times and
    reads, keyed by contract quantity (``mass_air_flow``, ``engine_speed``,
    ``intake_air_temperature``, ``barometric_pressure``); each is held onto the
    solver grid from the latest first read to the earliest last read."""
    sub = contract.subsystem(MANIFOLD_SUBSYSTEM)
    step_s = contract_step_s(contract)
    method = contract.solver.method
    refusal = numerics_refusal(contract, convergence)
    if refusal is not None:
        raise SimulationRefusalError(*refusal)
    needed = ("mass_air_flow", "engine_speed", "intake_air_temperature", "barometric_pressure")
    missing = [q for q in needed if q not in values or not len(_f(values[q]))]
    if missing:
        raise SimulationRefusalError("boundary_missing", f"no reads for {missing}")
    start = max(float(_f(times_s[q]).min()) for q in needed)
    stop = min(float(_f(times_s[q]).max()) for q in needed)
    if not stop > start:
        raise SimulationRefusalError("boundary_missing", "the boundaries do not overlap in time")
    grid = start + np.arange(int((stop - start) / step_s) + 1, dtype=float) * step_s
    held = {q: hold_on_grid(times_s[q], values[q], grid) for q in needed}
    check_applicability(sub, hardware_configuration, held)
    trace = integrate(
        held["mass_air_flow"],
        held["engine_speed"],
        held["intake_air_temperature"],
        [params],
        step_s=step_s,
        method=method,
    )
    check = consistency(trace)
    assert sub.numerical is not None
    if (
        check.equation_residual_rel[0] > sub.numerical.equation_residual_relative
        or check.breathing_excess[0] > 1.0
    ):
        raise SimulationRefusalError(
            "integration_inconsistent",
            f"the trace departs from the declared equations (state residual "
            f"{check.equation_residual_rel[0]:.2e}, breathing excess "
            f"{check.breathing_excess[0]:.2f})",
        )
    p = trace.map_kpa[:, 0]
    nonphysical = check.nonphysical[:, 0] | ~np.isfinite(p)
    with np.errstate(invalid="ignore"):
        above = ~nonphysical & (p > held["barometric_pressure"])
    refused = above | nonphysical
    nan = np.where(refused, np.nan, 1.0)
    return ManifoldReplay(
        t_s=grid,
        map_kpa=p * nan,
        temp_c=(trace.temp_k[:, 0] - KELVIN) * nan,
        cylinder_gps=trace.cylinder_gps[:, 0] * nan,
        refused=refused,
        above_ambient=above,
        nonphysical=nonphysical,
        resolved_period_s=120.0 / held["engine_speed"],
        consistency=check,
        step_s=step_s,
        effects=params.effects,
    )


# -- step-size refinement -----------------------------------------------------------


@dataclass(frozen=True)
class CaseConvergence:
    case: str
    rpm: float
    tau_s: float
    #: Max |error| / step amplitude at each refinement, cylinder flow against
    #: the exact solution.
    flow_error: tuple[float, ...]
    #: Max |error| / amplitude of manifold pressure (and, adiabatic, temperature
    #: excursion) against the Richardson extrapolation of the two finest steps.
    pressure_error: tuple[float, ...]
    temperature_error: tuple[float, ...]
    #: NaN when the errors reached rounding before two refinements could
    #: measure it (converged, not unmeasured).
    observed_order: float
    #: :func:`consistency` on the base-step run.
    equation_residual_rel: float
    breathing_excess: float
    settled_flow_rel: float
    passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ConvergenceReport:
    step_s: float
    method: SolverMethod
    refinement: tuple[int, ...]
    check: NumericalCheck
    cases: tuple[CaseConvergence, ...]

    @property
    def passed(self) -> bool:
        return bool(self.cases) and all(c.passed for c in self.cases)

    @property
    def worst(self) -> CaseConvergence:
        return max(self.cases, key=lambda c: c.flow_error[0])

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_s": self.step_s,
            "method": self.method,
            "refinement": list(self.refinement),
            "passed": self.passed,
            "n_cases": len(self.cases),
            "worst_case": self.worst.case,
            "worst_rpm": self.worst.rpm,
            "worst_tau_s": self.worst.tau_s,
            "worst_flow_error": list(self.worst.flow_error),
            "max_pressure_error_at_step": max(c.pressure_error[0] for c in self.cases),
            "max_temperature_error_at_step": max(
                (c.temperature_error[0] for c in self.cases if c.temperature_error), default=0.0
            ),
            "observed_order_range": [
                min(
                    (c.observed_order for c in self.cases if math.isfinite(c.observed_order)),
                    default=math.nan,
                ),
                max(
                    (c.observed_order for c in self.cases if math.isfinite(c.observed_order)),
                    default=math.nan,
                ),
            ],
            "order_at_rounding": sum(not math.isfinite(c.observed_order) for c in self.cases),
            "max_equation_residual_rel": max(c.equation_residual_rel for c in self.cases),
            "max_breathing_excess": max(c.breathing_excess for c in self.cases),
            "failed": {c.case: list(c.reasons) for c in self.cases if not c.passed},
        }


def _max_rel(a: FloatArray, b: FloatArray, scale: float) -> float:
    return float(np.max(np.abs(a - b)) / scale) if scale > 0 else 0.0


def numerics_refusal(
    contract: SimContract, convergence: ConvergenceReport
) -> tuple[str, str] | None:
    """Why ``convergence`` does not verify the contract's numerics for the
    manifold stage, as ``(reason, message)``; ``None`` when it does.

    The report must be of the contract's own solver step and method under the
    stage's declared check (``numerics_unverified``) and must have passed
    with at least one case (``numerics_unconverged``). Every consumer of a
    report -- replay and standing -- asks this one function."""
    sub = contract.subsystem(MANIFOLD_SUBSYSTEM)
    step_s = contract_step_s(contract)
    method = contract.solver.method
    if (
        not math.isclose(convergence.step_s, step_s, rel_tol=0, abs_tol=1e-12)
        or convergence.method != method
        or convergence.check != sub.numerical
    ):
        return (
            "numerics_unverified",
            f"the study given ran {convergence.method} at {convergence.step_s} s; the contract "
            f"solves with {method} at {step_s} s under its declared check",
        )
    if not convergence.passed:
        return (
            "numerics_unconverged",
            f"{method} at {step_s} s failed the refinement study: "
            f"{[c.case for c in convergence.cases if not c.passed] or 'no cases run'}",
        )
    return None


def contract_step_s(contract: SimContract) -> float:
    """The contract's solver step: the only step a plant stage runs at."""
    solver = next(c for c in contract.clocks if c.id == contract.solver.clock)
    assert solver.period_s is not None
    return solver.period_s


def convergence_study(
    contract: SimContract,
    profile: ManifoldPlantProfile,
    fuel: FuelPlantProfile,
    *,
    step_s: float | None = None,
    method: SolverMethod | None = None,
) -> ConvergenceReport:
    """The contract's refinement ladder at every applicability corner.

    Each case is a tip-in from a settled manifold (``profile.study``) at the
    lowest and highest bounded rpm, under every bracket-end combination of
    volume and VE and both energy balances. The cylinder flow has an exact
    solution (``m`` obeys a linear ODE: dm/dt = in - m/tau); pressure and
    temperature are checked against the Richardson extrapolation of the two
    finest steps. ``step_s`` and ``method`` default to the contract's solver;
    another method is for showing why the contract's was chosen.

    Flows are compared as what left the manifold over each base step (a
    refined run's sub-steps are summed back onto the base step), so every
    rung of the ladder answers the same question."""
    sub = contract.subsystem(MANIFOLD_SUBSYSTEM)
    check = sub.numerical
    app = sub.applicability
    if check is None or app is None:
        raise SimulationRefusalError("subsystem_unbuilt", f"{sub.id} declares no numerics")
    if step_s is None:
        step_s = contract_step_s(contract)
    method = method or contract.solver.method
    order_expected = METHOD_ORDER[method]
    rpm_bound = next(b for b in app.bounds if b.quantity == "engine_speed")
    study = profile.study
    cases: list[tuple[str, float, ManifoldParameters]] = [
        (name, rpm, params)
        for name, params in bracket_cases(profile, fuel).items()
        for rpm in (rpm_bound.minimum, rpm_bound.maximum)
    ]
    out: list[CaseConvergence] = []
    ladder = tuple(check.refinement)
    temp_k = study.iat_c + KELVIN
    # One vectorised run per rpm corner: every case at that rpm shares the
    # grid, run for the slowest case's horizon.
    for rpm in (rpm_bound.minimum, rpm_bound.maximum):
        group = [(name, params) for name, r_, params in cases if r_ == rpm]
        tau = np.array([float(params.tau_s(rpm)) for _, params in group])
        # The flow that would settle each manifold at ambient pressure.
        vol = np.array([params.volume_l for _, params in group])
        q_amb = study.baro_kpa * vol * 1000.0 / (R_AIR * temp_k) / tau
        q1 = study.from_fraction_of_ambient_flow * q_amb
        q2 = study.to_fraction_of_ambient_flow * q_amb
        n = int(math.ceil(study.tau_multiples * float(tau.max()) / step_s)) + 1
        runs = []
        for r in ladder:
            trace = integrate(
                np.broadcast_to(q2, (n * r, len(group))),
                rpm,
                study.iat_c,
                [params for _, params in group],
                step_s=step_s / r,
                method=method,
                initial_mass_g=q1 * tau,
                initial_temp_k=temp_k,
            )
            runs.append((trace, r))
        t = (np.arange(n) * step_s)[:, None]
        # Exact mass leaving over each base step [t, t + h), divided by h.
        exact_flow = q2 + (q1 - q2) * tau / step_s * (
            np.exp(-t / tau) - np.exp(-(t + step_s) / tau)
        )
        amp = np.abs(q2 - q1)
        gain = 2.0**order_expected
        (fine, r_f), (prev, r_p) = runs[-1], runs[-2]
        p_ref = (gain * fine.map_kpa[::r_f] - prev.map_kpa[::r_p]) / (gain - 1.0)
        t_ref = (gain * fine.temp_k[::r_f] - prev.temp_k[::r_p]) / (gain - 1.0)
        books = consistency(runs[0][0])
        for j, (name, params) in enumerate(group):
            flow_err = tuple(
                _max_rel(tr.cylinder_gps[:, j].reshape(n, r).mean(axis=1), exact_flow[:, j], amp[j])
                for tr, r in runs
            )
            p_amp = float(np.max(np.abs(p_ref[:, j] - p_ref[0, j])))
            t_amp = float(np.max(np.abs(t_ref[:, j] - t_ref[0, j])))
            p_err = tuple(_max_rel(tr.map_kpa[::r, j], p_ref[:, j], p_amp) for tr, r in runs[:-2])
            adiabatic = params.energy_balance == "adiabatic"
            t_err = (
                tuple(_max_rel(tr.temp_k[::r, j], t_ref[:, j], t_amp) for tr, r in runs[:-2])
                if adiabatic and t_amp > 0
                else ()
            )
            order = math.nan
            for i in range(len(ladder) - 1):
                if flow_err[i + 1] > ROUNDING_FLOOR:
                    order = math.log(flow_err[i] / flow_err[i + 1], ladder[i + 1] / ladder[i])
                    break
            settled = float(abs(runs[0][0].cylinder_gps[-1, j] - q2[j]) / amp[j])
            eq_rel = float(books.equation_residual_rel[j])
            excess = float(books.breathing_excess[j])
            reasons = []
            tol = check.max_relative_error
            if flow_err[0] > tol:
                reasons.append(f"flow_error {flow_err[0]:.4f} > {tol}")
            if p_err and p_err[0] > tol:
                reasons.append(f"pressure_error {p_err[0]:.4f} > {tol}")
            if t_err and t_err[0] > tol:
                reasons.append(f"temperature_error {t_err[0]:.4f} > {tol}")
            if math.isfinite(order) and abs(order - order_expected) > check.order_tolerance:
                reasons.append(f"observed_order {order:.3f} != {order_expected}")
            if not math.isfinite(order) and flow_err[0] > tol:
                reasons.append("order_unmeasured")
            if eq_rel > check.equation_residual_relative:
                reasons.append(f"equation_residual {eq_rel:.2e}")
            if excess > 1.0:
                reasons.append(f"breathing_excess {excess:.2f}")
            if books.nonphysical[:, j].any():
                reasons.append("state_nonphysical")
            if settled > math.exp(-study.tau_multiples) * 1.5:
                reasons.append(f"not_settled {settled:.4f}")
            out.append(
                CaseConvergence(
                    case=f"{name}:rpm={rpm:g}",
                    rpm=rpm,
                    tau_s=float(tau[j]),
                    flow_error=flow_err,
                    pressure_error=p_err,
                    temperature_error=t_err,
                    observed_order=order,
                    equation_residual_rel=eq_rel,
                    breathing_excess=excess,
                    settled_flow_rel=settled,
                    passed=not reasons,
                    reasons=tuple(reasons),
                )
            )
    return ConvergenceReport(step_s, method, ladder, check, tuple(out))


# -- reference independence ----------------------------------------------------------


def _observers(contract: SimContract) -> dict[str, list[str]]:
    """Channel -> the contract quantities it observes (scored or alias)."""
    out: dict[str, list[str]] = {}
    for q in contract.quantities:
        for channel in (*q.observed_as, *q.leak_aliases):
            out.setdefault(channel, []).append(q.id)
    return out


def _computed_from(contract: SimContract, quantity_id: str) -> set[str]:
    """The physical quantities an observation of ``quantity_id`` carries: an
    ECU word is computed from the sensors it reads, a sensor carries what it
    measures, a physical quantity is itself."""
    by_id = {q.id: q for q in contract.quantities}
    seen: set[str] = set()
    out: set[str] = set()
    stack = [quantity_id]
    while stack:
        q_id = stack.pop()
        if q_id in seen:
            continue
        seen.add(q_id)
        q = by_id[q_id]
        if q.kind == "ecu":
            stack.extend(d.quantity for d in q.depends_on)
        elif q.kind == "sensor":
            assert q.sensor is not None
            out.add(q.sensor.measures)
        else:
            out.add(q_id)
    return out


def _derived_leaves(channel: str) -> tuple[set[str], bool]:
    """Every raw channel a ``derived:`` channel is computed from (required and
    optional inputs, transitively), and whether the channel is derived at all."""
    if channel not in DERIVED_CHANNEL_INPUTS:
        return {channel}, False
    leaves: set[str] = set()
    stack, seen = [channel], set()
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        if c in DERIVED_CHANNEL_INPUTS:
            stack.extend(x for group in DERIVED_CHANNEL_INPUTS[c] for x in group)
            stack.extend(DERIVED_CHANNEL_OPTIONAL_INPUTS.get(c, ()))
        else:
            leaves.add(c)
    return leaves, True


@dataclass(frozen=True)
class Lineage:
    quantities: frozenset[str]
    #: ``False`` when nothing says what the reference is computed from.
    known: bool


def reference_lineage(contract: SimContract, ref: ValidationReference) -> Lineage:
    """The contract quantities ``ref`` is computed from (its own measured
    quantity included), from the contract graph, the derived-channel registry
    and its declared ``derived_from``."""
    observers = _observers(contract)
    ids = {q.id for q in contract.quantities}
    found: set[str] = {ref.measures} if ref.kind == "instrument" else set()
    known = ref.kind == "instrument" or bool(ref.derived_from)
    leaves, derived = _derived_leaves(ref.channel)
    known = known or derived or ref.channel in observers
    for item in (*leaves, *ref.derived_from):
        if item in ids:
            found.add(item)
            continue
        sub_leaves, _ = _derived_leaves(item)
        for leaf in sub_leaves:
            for q_id in observers.get(leaf, ()):
                found |= _computed_from(contract, q_id)
    return Lineage(frozenset(found), known)


def subsystem_inputs(contract: SimContract, sub: Subsystem) -> frozenset[str]:
    """The physical quantities upstream of what ``sub`` computes: its
    dependencies through the plant, stopping at ECU quantities and at the
    inputs the subsystem declares it may share."""
    by_id = {q.id: q for q in contract.quantities}
    shared = {s.quantity for s in sub.shared_inputs}
    seen: set[str] = set()
    out: set[str] = set()
    stack = [d.quantity for q in sub.computes for d in by_id[q].depends_on]
    while stack:
        q_id = stack.pop()
        if q_id in seen:
            continue
        seen.add(q_id)
        q = by_id[q_id]
        if q.kind in ("ecu", "sensor"):
            continue
        if q_id not in sub.computes:
            out.add(q_id)
        if q_id in shared:
            continue
        stack.extend(d.quantity for d in q.depends_on)
    return frozenset(out - shared)


def check_reference(contract: SimContract, sub: Subsystem, ref: ValidationReference) -> list[str]:
    """Why ``ref`` cannot validate ``sub``; empty when it can.

    Refused: an unavailable reference (naming what it requires), one whose
    lineage is unknown, and one computed from any input of the subsystem
    (``reference_not_independent``) -- agreement with it would be the shared
    input agreeing with itself."""
    reasons: list[str] = []
    if ref.measures not in sub.computes:
        reasons.append(f"reference_measures_other:{ref.id}:{ref.measures}")
    if ref.status != "available":
        reasons.append(f"reference_{ref.status}:{ref.id}:{ref.requires}")
    lineage = reference_lineage(contract, ref)
    if not lineage.known:
        reasons.append(f"reference_lineage_unknown:{ref.id}")
    shared = sorted(lineage.quantities & subsystem_inputs(contract, sub))
    if shared:
        reasons.append(f"reference_not_independent:{ref.id}:{','.join(shared)}")
    return reasons


# -- standing and claims -------------------------------------------------------------


@dataclass(frozen=True)
class SubsystemStanding:
    subsystem: str
    built: bool
    #: ``None`` for an unbuilt or stateless subsystem (nothing to integrate).
    numerically_verified: bool | None
    validated: bool
    #: Empty exactly when validated.
    reasons: tuple[str, ...]


def _is_stateful(contract: SimContract, sub: Subsystem) -> bool:
    return any(contract.quantity(q).is_state for q in sub.computes)


def check_internal_models(contract: SimContract) -> list[str]:
    """Where a built stage's code and the contract disagree about which owned
    quantities it computes for itself; empty when they agree.

    Each implementation module lists them in ``COMPUTES_INTERNALLY`` (none:
    empty); the contract's ``internal_models`` must name exactly those, so a
    second live model of a quantity cannot exist undeclared."""
    problems: list[str] = []
    for sub in contract.subsystems:
        if sub.implementation is None:
            continue
        module = importlib.import_module(sub.implementation.module)
        in_code = set(getattr(module, "COMPUTES_INTERNALLY", ()))
        mine = set(sub.computes)
        in_code -= mine
        declared = {im.quantity for im in sub.internal_models}
        for q in sorted(in_code - declared):
            problems.append(f"{sub.id}: {sub.implementation.module} computes {q} undeclared")
        for q in sorted(declared - in_code):
            problems.append(f"{sub.id}: declares an internal {q} its module does not compute")
    return problems


def subsystem_standing(
    contract: SimContract,
    sub: Subsystem,
    *,
    profile_dir: Path,
    convergence: ConvergenceReport | None = None,
) -> SubsystemStanding:
    """A subsystem's standing, derived: built, its numerics verified by running
    the study (or the given report), and validated only by a recorded passing
    scoring against an available, independent reference -- for the fuel stage,
    its own profile's held-out verdicts (:func:`plant_qualification`)."""
    reasons: list[str] = []
    missing = [m.id for m in sub.required_measurements if m.status != "available"]
    if sub.status == "unbuilt":
        reasons.append(f"subsystem_unbuilt:{sub.id}")
        reasons += [f"requires_measurement:{m}" for m in missing]
        return SubsystemStanding(sub.id, False, None, False, tuple(reasons))
    assert sub.implementation is not None
    numerics: bool | None = None
    if _is_stateful(contract, sub):
        if sub.id != MANIFOLD_SUBSYSTEM:
            raise SimulationRefusalError(
                "numerics_unimplemented", f"no convergence study is implemented for {sub.id}"
            )
        if convergence is None:
            manifold, _ = load_manifold_plant(profile_dir)
            fuel, _ = load_fuel_plant(profile_dir)
            convergence = convergence_study(contract, manifold, fuel)
        refusal = numerics_refusal(contract, convergence)
        numerics = refusal is None
        if refusal is not None:
            reasons.append(f"{refusal[0]}:{sub.id}")
    reasons += [
        f"internal_model:{im.quantity}:owed_by_next_plant_version" for im in sub.internal_models
    ]
    usable = [r for r in sub.references if not check_reference(contract, sub, r)]
    for ref in sub.references:
        reasons += check_reference(contract, sub, ref)
    if sub.implementation.profile_file == "fuel_plant.yaml":
        fuel, _ = load_fuel_plant(profile_dir)
        q = plant_qualification(fuel)
        if not q.qualified:
            reasons.append(str(q.reason))
        passed = q.qualified
    else:
        usable_ids = {r.id for r in usable}
        passed = any(v.verdict == "pass" and v.reference in usable_ids for v in sub.validations)
        if not passed:
            reasons.append(f"no_passing_validation:{sub.id}")
    if passed and not usable:
        reasons.append(f"no_independent_reference:{sub.id}")
    reasons += [f"requires_measurement:{m}" for m in missing if not passed]
    validated = passed and bool(usable) and numerics is not False
    if validated:
        reasons = []
    return SubsystemStanding(sub.id, True, numerics, validated, tuple(dict.fromkeys(reasons)))


def standings(
    contract: SimContract, profile_dir: Path, *, convergence: ConvergenceReport | None = None
) -> dict[str, SubsystemStanding]:
    return {
        s.id: subsystem_standing(contract, s, profile_dir=profile_dir, convergence=convergence)
        for s in contract.subsystems
    }


def _upstream_subsystems(contract: SimContract, mode_id: str, quantity_id: str) -> list[Subsystem]:
    """The subsystems computing ``quantity_id`` and every physical quantity
    the mode computes on the way to it, nearest first. A quantity the mode
    supplies (a prescribed rpm) is a stated value, so the stage that would
    compute it is not needed there."""
    mode = contract.mode(mode_id)
    computed = set(mode.computed())
    by_id = {q.id: q for q in contract.quantities}
    order: list[Subsystem] = []
    seen: set[str] = set()
    stack = [quantity_id]
    while stack:
        q_id = stack.pop(0)
        if q_id in seen:
            continue
        seen.add(q_id)
        q = by_id[q_id]
        if q.kind in ("ecu", "sensor") or (q_id != quantity_id and q_id not in computed):
            continue
        sub = contract.subsystem_of(q_id)
        if sub is not None and sub not in order:
            order.append(sub)
        stack.extend(d.quantity for d in q.depends_on if d.quantity != q_id)
    return order


def claim_refusals(
    contract: SimContract,
    standing: Mapping[str, SubsystemStanding],
    mode_id: str,
    claim: str,
    *,
    changed_controls: Iterable[str] = (),
) -> list[str]:
    """Why ``claim`` cannot be made from a result of ``mode_id``; empty when it
    can. ``changed_controls`` names what the scenario varies (``spark``,
    ``cam``, ``lambda``): a claim about its effect needs an available
    measurement that excites it, which natural closed-loop logs do not."""
    if claim not in CLAIMS:
        raise SimulationRefusalError("claim_undeclared", f"{claim!r} is not one of {CLAIMS}")
    if claim == "knock":
        return ["knock_unmodelled: no subsystem computes knock"]
    mode = contract.mode(mode_id)
    quantity = _CLAIM_QUANTITY[claim]
    reasons: list[str] = []
    role = mode.role(quantity)
    if claim == "acceleration" and role == "prescribed_boundary":
        reasons.append(
            f"prescribed_rpm_not_acceleration: {mode.id} imposes engine speed; its results "
            "are at a stated speed, never a prediction of how fast it changes"
        )
    elif role not in ("predicted_output", "inferred_state"):
        reasons.append(f"{quantity}_not_computed: {mode.id} gives {quantity} the role {role}")
    chain = _upstream_subsystems(contract, mode_id, quantity)
    for sub in chain:
        st = standing.get(sub.id)
        if st is None or not st.validated:
            reasons.append(f"subsystem_not_validated:{sub.id}")
    for control in changed_controls:
        excited = [
            m
            for sub in chain
            for m in sub.required_measurements
            if control in m.excites and m.status == "available"
        ]
        if not excited:
            named = sorted(
                {m.id for sub in chain for m in sub.required_measurements if control in m.excites}
            )
            reasons.append(
                f"{control}_response_unmeasured: no available measurement excites {control} "
                f"(needed: {named or 'none declared'})"
            )
    return reasons


def require_claim(
    contract: SimContract,
    standing: Mapping[str, SubsystemStanding],
    mode_id: str,
    claim: str,
    *,
    changed_controls: Iterable[str] = (),
) -> None:
    reasons = claim_refusals(contract, standing, mode_id, claim, changed_controls=changed_controls)
    if reasons:
        raise SimulationRefusalError(reasons[0].split(":")[0], "; ".join(reasons))


def rollout_blockers(contract: SimContract, standing: Mapping[str, SubsystemStanding]) -> list[str]:
    """The subsystems a coupled rollout computes through that are not validated."""
    mode = next(m for m in contract.modes if m.kind == "coupled_rollout")
    subs = {sub.id for q in mode.computed() if (sub := contract.subsystem_of(q)) is not None}
    return sorted(s for s in subs if not standing[s].validated)


# -- the corpus: a bound the boundary data must respect ---------------------------------


def charge_fraction(
    maf_gps: ArrayLike, rpm: ArrayLike, iat_c: ArrayLike, baro_kpa: ArrayLike, geometry: Geometry
) -> FloatArray:
    """MAF over the ideal-gas fill of the displacement at ambient pressure and
    intake temperature: ``VE x p_m / p_ambient`` at steady state.

    The one breathing quantity MAF identifies. It bounds VE from below
    (``p_m <= p_ambient`` on an NA engine), and a value above any physical VE
    says the airflow boundary itself is wrong."""
    fill_g_per_s = (
        _f(baro_kpa)
        * 1000.0
        / (R_AIR * (_f(iat_c) + KELVIN))
        * geometry.displacement_l
        * _f(rpm)
        / 120.0
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(fill_g_per_s > 0, _f(maf_gps) / fill_g_per_s, np.nan)


def _channel(long: pd.DataFrame, spec: str) -> tuple[FloatArray, FloatArray]:
    rows = long[long["spec"] == spec].sort_values("timestamp", kind="stable")
    vals = rows["value"].to_numpy(dtype=float)
    ok = np.isfinite(vals)
    return to_seconds(rows["timestamp"])[ok], vals[ok]


def breathing_samples(session: Path, fuel: FuelPlantProfile) -> pd.DataFrame:
    """Per MAF read: rpm paired one-to-one on the acquisition clock, baro and
    intake temperature as-of (slow), and the charge fraction. Nothing is
    averaged across signals."""
    s = fuel.specs
    long = read_channels(
        session, [s.maf, s.rpm, s.baro, s.intake_temp, s.coolant], acquired_time=True
    )
    t_maf, maf = _channel(long, s.maf)
    t_rpm, rpm = _channel(long, s.rpm)
    out = pd.DataFrame({"t": t_maf, "maf_gps": maf})
    paired = np.full(len(t_maf), np.nan)
    if len(t_maf) and len(t_rpm):
        policy = PairingPolicy.within(f"{fuel.steady.pair_tolerance_s}s", reference="a")
        m = match_ordered(t_maf, t_rpm, policy)
        paired[m.index_a] = rpm[m.index_b]
    out["rpm"] = paired
    for name, spec in (("baro_kpa", s.baro), ("iat_c", s.intake_temp), ("coolant_c", s.coolant)):
        t, v = _channel(long, spec)
        out[name] = _as_of(t, v, t_maf, fuel.steady.slow_max_age_s)
    out["charge_fraction"] = charge_fraction(
        out["maf_gps"], out["rpm"], out["iat_c"], out["baro_kpa"], fuel.geometry
    )
    out["session"] = session.name
    return out


def _as_of(t: FloatArray, v: FloatArray, at: FloatArray, max_age_s: float) -> FloatArray:
    out = np.full(len(at), np.nan)
    if not len(t):
        return out
    i = np.searchsorted(t, at)
    lo, hi = np.clip(i - 1, 0, len(t) - 1), np.clip(i, 0, len(t) - 1)
    d_lo, d_hi = np.abs(at - t[lo]), np.abs(t[hi] - at)
    j = np.where(d_lo <= d_hi, lo, hi)
    ok = np.minimum(d_lo, d_hi) <= max_age_s
    out[ok] = v[j[ok]]
    return out


#: rpm bands the breathing bound is reported in.
RPM_BANDS: tuple[float, ...] = (500.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0, 6500.0)


def breathing_bound(
    samples: pd.DataFrame, fuel: FuelPlantProfile, *, min_coolant_c: float | None = None
) -> dict[str, Any]:
    """Where the boundary data contradicts the shared VE assumption.

    Per warm sample with rpm in the manifold's envelope: the charge fraction,
    and how many samples exceed the working VE (there the steady manifold
    would sit above ambient: ``manifold_above_ambient``), the bracket's top,
    and 1.0. A per-sample bound, not a comparison of two signals."""
    wa = fuel.registry.get(VE_PARAMETER).working_assumption
    assert wa is not None
    floor = fuel.steady.min_coolant_c if min_coolant_c is None else min_coolant_c
    ok = (
        np.isfinite(samples["charge_fraction"])
        & (samples["rpm"] >= RPM_BANDS[0])
        & (samples["rpm"] <= RPM_BANDS[-1])
        & (samples["coolant_c"] >= floor)
    )
    warm = samples[ok]
    cf = warm["charge_fraction"].to_numpy(dtype=float)
    states = {
        "rpm_unpaired": int(samples["rpm"].isna().sum()),
        "environment_missing": int((samples["baro_kpa"].isna() | samples["iat_c"].isna()).sum()),
        "cold_or_out_of_band": int((np.isfinite(samples["charge_fraction"]) & ~ok).sum()),
        "eligible": int(ok.sum()),
    }

    def summary(values: FloatArray) -> dict[str, Any]:
        if not len(values):
            return {"n": 0}
        q = np.quantile(values, [0.5, 0.9, 0.99, 1.0])
        return {
            "n": len(values),
            "median": float(q[0]),
            "p90": float(q[1]),
            "p99": float(q[2]),
            "max": float(q[3]),
            "above_working_ve": int((values > wa.value).sum()),
            "above_bracket_top": int((values > wa.bracket[1]).sum()),
            "above_one": int((values > 1.0).sum()),
        }

    bands = {}
    rpm = warm["rpm"].to_numpy(dtype=float)
    for lo, hi in zip(RPM_BANDS, RPM_BANDS[1:], strict=False):
        sel = (rpm >= lo) & (rpm < hi) if hi < RPM_BANDS[-1] else (rpm >= lo) & (rpm <= hi)
        bands[f"{lo:g}-{hi:g}"] = summary(cf[sel])
    return {
        "working_ve": wa.value,
        "ve_bracket": list(wa.bracket),
        "states": states,
        "all": summary(cf),
        "by_rpm_band": bands,
        "sessions": sorted(set(warm["session"])),
    }


def load_kit(profile_dir: Path) -> tuple[SimContract, ManifoldPlantProfile, FuelPlantProfile]:
    contract, _ = load_contract(profile_dir)
    manifold, _ = load_manifold_plant(profile_dir)
    fuel, _ = load_fuel_plant(profile_dir)
    return contract, manifold, fuel


__all__ = [
    "CLAIMS",
    "MANIFOLD_FILE",
    "MANIFOLD_SUBSYSTEM",
    "ConvergenceReport",
    "ManifoldParameters",
    "ManifoldPlantProfile",
    "ManifoldReplay",
    "ManifoldTrace",
    "SubsystemStanding",
    "bracket_cases",
    "breathing_bound",
    "breathing_samples",
    "charge_fraction",
    "check_applicability",
    "check_reference",
    "check_internal_models",
    "claim_refusals",
    "convergence_study",
    "hold_on_grid",
    "integrate",
    "consistency",
    "contract_step_s",
    "numerics_refusal",
    "load_kit",
    "load_manifold_plant",
    "reference_lineage",
    "replay_manifold",
    "require_claim",
    "resolve_manifold",
    "rollout_blockers",
    "standings",
    "steady_mass_g",
    "subsystem_inputs",
    "subsystem_standing",
]
