"""Paired tuning scenarios: one baseline ROM, one candidate, nothing else different (E07.6).

A :class:`PairedScenario` is *one* object holding the hardware/plant identity,
the prescribed operating points (the driver scenario and environment) and the
declared learned state; both ROMs run from it, so the two sides cannot differ
in anything but the image and the events the change itself implies. Each point
becomes a contract :class:`~src.analysis.sim_contract.Scenario` and passes
:func:`~src.analysis.sim_contract.check_scenario`, so a recorded channel, a
supplied value for a quantity the mode computes, or a mismatched contract is
refused by the contract's own names.

What it computes, per point and per ROM, in this slice (warm, steady,
prescribed rpm and airflow):

* BFS at its steady fixed point from the prescribed airflow, rpm and the
  stated throttle-model fill byte, through each image's own K, Fuel Comp and
  axes (:mod:`src.analysis.paired_steady`, the manifest's implemented
  ``base_fuel_schedule``), so a Fuel Comp, K or axis edit is computed rather
  than conditioned on;
* the Target AFR lookup (the twin's decoded kernel, :mod:`src.analysis.target_phi`)
  at the load input X computed from that BFS by the ECM's own formula;
* the injector pulse word (the decoded chain, :mod:`src.analysis.injector_pulse`)
  with each image's own pulse-chain calibration, at the stated trims in open
  loop and at the trim's fixed point in closed loop (the candidate holds the
  base's metered open time while its target phi is unchanged);
* delivered lambda and fuel per event from the frozen E07.3 plant.

What it makes impossible rather than documented:

* **No recorded post-change value feeds a prediction.** Comparisons and
  rollouts take stated values only (the contract's ``recorded_value_refused``);
  a stated value whose ``source`` names a recording is refused here too.
  Quantities the mode computes -- BFS, the trims in a rollout, airflow and rpm
  in a rollout -- cannot be supplied (``boundary_not_supplied_here``).
* **Conditioning is not holding fixed.** The fill byte ``0xFFFF118A`` (throttle
  area, idle air and purge) is not computed, so the comparison is *conditional
  on* a stated byte, shared by both ROMs. That is allowed only where the change
  does not reach the quantity (:func:`src.analysis.rom_effects.compare_roms`);
  a change that reaches it (a fill-curve edit) leaves every output downstream
  unresolved, named ``conditioned_on_changed_path:<quantity>``. A quantity the
  slice computes (BFS, X) cannot be stated at all (``conditioning_not_allowed``).
* **Learned state is declared, with its resets.** Both runs start from the
  same stated trims; an event the scenario declares for the candidate (the
  flash, ``ecu_reflash``) resets what the contract says it resets, to the
  trim's neutral value. Relearning is not modelled.
* **Unsupported is not unchanged.** Each output is ``supported`` per point or
  carries its reasons (an unresolved ROM dependency, a closed-loop or cold
  regime, an undecoded short pulse); :func:`rank_as_improvement` refuses any
  result with an unsupported output or point, an unqualified plant or an
  intervention test that has not passed, and :func:`optimise` is refused
  outright until effect predictions qualify.
* **Synthetic output stays synthetic.** A result is labelled ``simulation``;
  its traces carry a ``_sim-*`` session name, which plant identification and
  evaluation refuse (``synthetic_as_observed``) and the memory ledger refuses
  as evidence (``SyntheticIsolationError``).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.analysis.air_charge_twin import AirChargeCalibration, AirChargeRefusalError
from src.analysis.ecu_application import (
    ApplicationModel,
    ApplicationModelRefusalError,
    load_application_model,
)
from src.analysis.fuel_plant import (
    DELAY_PARAMETER,
    MANIFOLD_READS,
    MAP_OUTSIDE_VE_FIT,
    PLANT_FILE,
    VE_PARAMETER,
    B06Rules,
    FuelPlantProfile,
    FuelPlantRefusalError,
    PlantInputs,
    ResolvedParameters,
    VeCurve,
    load_b06,
    load_fuel_plant,
    plant_qualification,
    plant_reads,
    predict,
    resolve_parameters,
    sensitivity_cases,
)
from src.analysis.fuel_plant_eval import calibration_from_image
from src.analysis.injector_pulse import (
    PULSE_COUNT_MS,
    PulseChainCalibration,
    predict_pulse,
    trim_word,
    vb_counts_from_volts,
)
from src.analysis.paired_steady import (
    BFS_OUTPUT,
    COMPUTED_HERE,
    ECU_RUNNER_OUTPUTS,
    FILL_BYTE,
    SETPOINT_MOVED,
    TRIM_AUTHORITY_UNDECODED,
    TWIN_OUTPUT,
    SteadyCoverage,
    closed_loop_k,
    closed_loop_trim_pending,
    conditionable_node,
    ecu_output_gate,
    steady_bfs,
    steady_coverage,
    total_trim_pct,
    trim_k,
)
from src.analysis.plant_validation import InterventionVerdict
from src.analysis.rom_effects import EffectReport, _ancestors, compare_roms
from src.analysis.rom_image import sha256_hex
from src.analysis.rom_map import DTYPE_BYTES, RomMap, load_rom_map
from src.analysis.rom_trace import ViewerDefinition, load_viewer_definition
from src.analysis.sim_contract import (
    SCENARIO_ONLY_MODES,
    EcuIdentity,
    PlantIdentity,
    Scenario,
    SimContract,
    SimulationRefusalError,
    SuppliedValue,
    check_scenario,
    load_contract,
)
from src.analysis.target_phi import (
    ResolvedTable,
    TwinDefinition,
    TwinRefusalError,
    load_twin_definition,
    resolve_table,
    x_from_bfs,
)
from src.analysis.target_phi import predict as predict_phi
from src.session import SYNTHETIC_SESSION_PREFIX

#: Bump when what a result means changes. 2: BFS is computed per ROM at its
#: steady fixed point, and closed-loop points run at the trim's fixed point.
#: 3: a reason reaches only the outputs downstream of the node it names, and a
#: gate withholds the candidate per output (E08.10).
TOOL_VERSION = "3"

#: Outputs the slice computes per point, in order.
OUTPUTS: tuple[str, ...] = (
    "base_fuel_schedule",
    "target_phi",
    "injector_pulse_ms",
    "fuel_trim_total_pct",
    "delivered_lambda",
    "fuel_per_event_g",
)
#: ECU-side outputs: no plant regime or trim fixed point gates them.
_ECU_OUTPUTS: frozenset[str] = frozenset(ECU_RUNNER_OUTPUTS)
#: The application-model output each computed one is gated on.
_TWIN_OUTPUT = TWIN_OUTPUT
#: The application-model output the steady BFS computes.
_BFS_OUTPUT = BFS_OUTPUT
#: Application-model outputs this slice computes (the rest are traced only).
_COMPUTED_TWIN_OUTPUTS: frozenset[str] = frozenset({_TWIN_OUTPUT, _BFS_OUTPUT})
#: The stated fill byte BFS's Fuel Comp lookup reads.
_LOAD_BYTE = FILL_BYTE
#: The value a trim is reset to by an event that resets it: its neutral value.
NEUTRAL_TRIM_PCT = 0.0
#: Learned ECU states this slice carries, by contract quantity id.
LEARNED_TRIMS: tuple[str, ...] = ("stft_b1", "ltft_b1")

# A session-directory stamp inside a value's stated source: a number copied
# out of a recording.
_RECORDING = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}")


@dataclass(frozen=True)
class SimKit:
    """Everything a paired run reads, loaded once."""

    contract: SimContract
    contract_sha256: str
    model: ApplicationModel
    model_sha256: str
    rom_map: RomMap
    twin: TwinDefinition
    viewer: ViewerDefinition
    plant: FuelPlantProfile
    delay: B06Rules
    #: The plant declaration's file, ``<profile>/<file>``: where each working
    #: assumption a result names comes from (E08.10).
    plant_file: str = PLANT_FILE


def load_kit(profile_dir: Path) -> SimKit:
    contract, contract_sha = load_contract(profile_dir)
    model, model_sha = load_application_model(profile_dir)
    twin, _ = load_twin_definition(profile_dir)
    plant, _ = load_fuel_plant(profile_dir)
    return SimKit(
        contract=contract,
        contract_sha256=contract_sha,
        model=model,
        model_sha256=model_sha,
        rom_map=load_rom_map(profile_dir),
        twin=twin,
        viewer=load_viewer_definition(profile_dir),
        plant=plant,
        delay=load_b06(profile_dir, plant.delay),
        plant_file=f"{profile_dir.name}/{PLANT_FILE}",
    )


MapSource = Literal["stated", "measured", "plant_model"]
RailSource = Literal["stated", "measured", "working_assumption"]
BatterySource = Literal["stated", "measured", "unmeasured"]
#: The reason the absolute pulse carries at a point with no voltage read.
BATTERY_UNMEASURED = "battery_voltage_unmeasured"
#: The reason a result run on drawn plant parameters carries in ranking.
OVERRIDES_NOT_FROZEN = "plant_overrides_not_frozen"


@dataclass(frozen=True)
class OperatingPoint:
    """One prescribed steady point. Every value is stated, none read from a recording."""

    rpm: float
    maf_gps: float
    baro_kpa: float
    iat_c: float
    coolant_c: float
    battery_v: float
    rail_kpa: float
    map_kpa: float
    throttle_pct: float
    #: The pedal-enrichment flag byte (its inputs are not traced: stated).
    pedal_flag: int
    #: Shared values of computed-but-unmodelled ECU quantities, by contract
    #: quantity id (the fill byte ``fuel_comp_load_byte``, raw counts). Both
    #: ROMs see the same value, so it is refused for any quantity the change
    #: reaches, and for a quantity this slice computes (BFS, X).
    conditioned: Mapping[str, float]
    #: Where the numbers come from (a design, a grid): never a recording.
    source: str
    #: How ``map_kpa`` is had. ``stated``/``measured``: a boundary, held for
    #: every plant parameter set. ``plant_model``: not a boundary at all -- the
    #: plant's own manifold model computes it from the airflow on each run, so
    #: a VE draw moves it (``map_kpa`` is then unused; the contract's
    #: ``unstated: internal_model`` lets the scenario leave it out).
    map_kpa_source: MapSource = "stated"
    #: How ``rail_kpa`` is had. ``working_assumption``: no rail reading, so
    #: the plant runs its rail working assumption (drawn in a Monte Carlo) and
    #: ``rail_kpa`` is only the boundary's stated working value.
    rail_kpa_source: RailSource = "stated"
    #: ``unmeasured``: no module-voltage read; ``battery_v`` is a stated
    #: stand-in. Only the absolute pulse (its dead time) reads it, so that
    #: output alone is unsupported (``battery_voltage_unmeasured``).
    battery_v_source: BatterySource = "stated"

    @property
    def sources_stated(self) -> bool:
        """Every source is the default (the point predates them, E07.6)."""
        return (self.map_kpa_source, self.rail_kpa_source, self.battery_v_source) == (
            "stated",
            "stated",
            "stated",
        )


@dataclass(frozen=True)
class LearnedState:
    """The declared initial learned/adaptive state, shared by both runs."""

    stft_pct: float
    ltft_pct: float
    throttle_closed_learned: float
    idle_air_learned: float
    evap_purge_area: float
    source: str


#: Contract boundary quantity -> how a point or the learned state supplies it.
_POINT_FIELDS: dict[str, str] = {
    "barometric_pressure": "baro_kpa",
    "intake_air_temperature": "iat_c",
    "battery_voltage": "battery_v",
    "engine_speed": "rpm",
    "coolant_temperature": "coolant_c",
    "manifold_pressure": "map_kpa",
    "mass_air_flow": "maf_gps",
    "fuel_rail_pressure": "rail_kpa",
    "throttle_position_actual": "throttle_pct",
    "pedal_flag": "pedal_flag",
}
_LEARNED_FIELDS: dict[str, str] = {
    "stft_b1": "stft_pct",
    "ltft_b1": "ltft_pct",
    "throttle_closed_learned": "throttle_closed_learned",
    "idle_air_learned": "idle_air_learned",
    "evap_purge_area": "evap_purge_area",
}


@dataclass(frozen=True)
class PairedScenario:
    """Baseline and candidate ROMs under one hardware set, one driver scenario,
    one environment and one declared initial state.

    ``candidate_events`` are what the change itself implies for the candidate
    run (``ecu_reflash``: the flash resets what the contract resets on it)."""

    mode: str
    base: EcuIdentity
    candidate: EcuIdentity
    plant: PlantIdentity
    points: tuple[OperatingPoint, ...]
    learned: LearnedState
    candidate_events: tuple[str, ...] = ()
    #: Extra stated boundaries by contract quantity id (a rollout's driver and
    #: road inputs). A value for a quantity the mode computes is refused.
    extra_boundaries: Mapping[str, SuppliedValue] = field(default_factory=dict)
    initial_state: Mapping[str, SuppliedValue] = field(default_factory=dict)

    def contract_scenarios(self, contract: SimContract, contract_sha256: str) -> list[Scenario]:
        """The contract scenarios for every point: one holding both ECU identities
        in a ROM comparison, one per ROM in a rollout (which runs one ECU)."""
        if contract.mode(self.mode).kind == "conditional_rom_comparison":
            sides = [[self.base, self.candidate]]
        else:
            sides = [[self.base], [self.candidate]]
        out = []
        for point, ecus in ((p, e) for p in self.points for e in sides):
            boundaries: dict[str, SuppliedValue] = {
                q: SuppliedValue(value=float(getattr(point, f)), source=point.source)
                for q, f in _POINT_FIELDS.items()
                if not (q == "manifold_pressure" and point.map_kpa_source == "plant_model")
            }
            boundaries.update(
                {
                    q: SuppliedValue(
                        value=float(getattr(self.learned, f)), source=self.learned.source
                    )
                    for q, f in _LEARNED_FIELDS.items()
                }
            )
            boundaries.update(self.extra_boundaries)
            out.append(
                Scenario(
                    contract_sha256=contract_sha256,
                    mode=self.mode,
                    ecu=ecus,
                    plant=self.plant,
                    boundaries=boundaries,
                    initial_state=dict(self.initial_state),
                )
            )
        return out

    def scenario_hash(self, contract: SimContract, contract_sha256: str) -> str:
        """sha256 over every point's contract scenario, the conditioning and the events."""
        payload = {
            "scenarios": [
                s.scenario_hash() for s in self.contract_scenarios(contract, contract_sha256)
            ],
            "conditioned": [dict(sorted(p.conditioned.items())) for p in self.points],
            "candidate_events": list(self.candidate_events),
        }
        if not all(p.sources_stated for p in self.points):
            payload["boundary_sources"] = [
                [p.map_kpa_source, p.rail_kpa_source, p.battery_v_source] for p in self.points
            ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _with_contract_mode(
    contract: SimContract, scenario: PairedScenario, contract_sha256: str
) -> None:
    """The contract's refusals, per point, then the mode this slice runs.

    A point prescribes rpm, airflow and the trims; in a coupled rollout those
    are computed, so the contract refuses the point itself
    (``boundary_not_supplied_here``): recorded or stated, they cannot be fed
    back into a rollout's prediction of them."""
    mode = contract.mode(scenario.mode)
    for sc in scenario.contract_scenarios(contract, contract_sha256):
        check_scenario(contract, contract_sha256, sc)
    if mode.kind not in SCENARIO_ONLY_MODES:
        raise SimulationRefusalError(
            "not_a_paired_mode", f"{mode.id} replays a recording; a paired run states its inputs"
        )
    if mode.kind == "coupled_rollout":
        raise SimulationRefusalError(
            "dynamic_plant_unbuilt",
            "a coupled rollout recomputes airflow, rpm, BFS and the trims from driver demand; "
            "that needs E07.5's dynamic plant, so this slice runs the steady comparison only",
        )


def _refuse_recorded_sources(scenario: PairedScenario) -> None:
    named = [p.source for p in scenario.points if _RECORDING.search(p.source)]
    if _RECORDING.search(scenario.learned.source):
        named.append(scenario.learned.source)
    for value in (*scenario.extra_boundaries.values(), *scenario.initial_state.values()):
        if _RECORDING.search(value.source):
            named.append(value.source)
    if named:
        raise SimulationRefusalError(
            "recorded_value_refused",
            f"stated values whose source is a recording {sorted(set(named))}: a number copied "
            "from a drive recorded under one calibration is that recording, not a scenario",
        )


def _resets(contract: SimContract, events: Sequence[str]) -> dict[str, str]:
    """Learned trims each event resets, from the contract's declared inits."""
    declared = {e.id for e in contract.events}
    unknown = [e for e in events if e not in declared]
    if unknown:
        raise SimulationRefusalError("event_undeclared", f"the contract declares no {unknown}")
    out: dict[str, str] = {}
    for mode in contract.modes:
        for q, assignment in mode.roles.items():
            if assignment.init is None:
                continue
            for event in assignment.init.reset_on:
                if event in events:
                    if q not in LEARNED_TRIMS:
                        raise SimulationRefusalError(
                            "reset_unmodelled",
                            f"{event} resets {q}, which this slice does not carry",
                        )
                    out[q] = event
    return out


# -- affected cells -----------------------------------------------------------------------


def affected_cells(base: bytes, candidate: bytes, rom_map: RomMap) -> list[dict[str, Any]]:
    """Every mapped table cell whose bytes differ, with its raw old and new value."""
    out: list[dict[str, Any]] = []
    for table in rom_map.tables:
        width = DTYPE_BYTES[table.dtype]
        for slot in rom_map.table_slots(table):
            start = rom_map.table_address(table, slot)
            for k in range(table.rows * table.cols):
                at = start + k * width
                old, new = base[at : at + width], candidate[at : at + width]
                if old != new:
                    out.append(
                        {
                            "table": table.id,
                            "slot": slot,
                            "row": k // table.cols,
                            "col": k % table.cols,
                            "old": int.from_bytes(old, "big"),
                            "new": int.from_bytes(new, "big"),
                        }
                    )
    return out


# -- plant parameters -------------------------------------------------------------------------


def plant_parameters(
    kit: SimKit, overrides: Mapping[str, float] | None = None
) -> ResolvedParameters:
    """The frozen plant's parameters with ``overrides`` applied (E08.3's hook).

    A working assumption moves inside its bracket through
    :func:`~src.analysis.fuel_plant.resolve_parameters`, which refuses
    anything else. The lumped identified scale (``stoich_air_per_open_ms``)
    has no bracket: it moves inside the frozen baseline's own bootstrap
    interval, the measured spread a Monte Carlo draws it from. A name the
    plant does not read is refused (``override_unknown``), as is a value
    outside its bracket or interval (``override_outside_bracket``). No
    overrides is exactly ``resolve_parameters`` with none."""
    if not overrides:
        return resolve_parameters(kit.plant, kit.delay)
    unknown = sorted(set(overrides) - set(plant_reads(kit.plant.manifold_model)))
    if unknown:
        raise SimulationRefusalError(
            "override_unknown",
            f"{unknown} are not parameters plant {kit.plant.plant_version} reads",
        )
    baseline = kit.plant.baseline
    measured = baseline.intervals if baseline is not None else {}
    bracketed = {k: float(v) for k, v in overrides.items() if k not in measured}
    try:
        params = resolve_parameters(kit.plant, kit.delay, overrides=bracketed)
    except FuelPlantRefusalError as exc:
        if exc.reason == "override_outside_bracket":
            raise SimulationRefusalError(exc.reason, str(exc)) from exc
        raise
    identified: dict[str, float] = {}
    for pid in sorted(set(overrides) & set(measured)):
        lo, hi = measured[pid]
        value = float(overrides[pid])
        if not lo <= value <= hi:
            raise SimulationRefusalError(
                "override_outside_bracket",
                f"{pid}={value} outside the frozen baseline's interval ({lo}, {hi})",
            )
        identified[pid] = value
    if identified:
        params = replace(params, values={**params.values, **identified})
    return params


# -- running ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Side:
    table: ResolvedTable | None
    cal: PulseChainCalibration | None
    air: AirChargeCalibration | None
    reasons: tuple[str, ...]
    ltft_pct: float


def _side(kit: SimKit, image: bytes, identity: EcuIdentity, ltft_pct: float, label: str) -> _Side:
    reasons: list[str] = []
    table: ResolvedTable | None = None
    cal: PulseChainCalibration | None = None
    air: AirChargeCalibration | None = None
    assert identity.slot is not None
    try:
        table = resolve_table(
            image,
            kit.rom_map,
            kit.twin,
            viewer=kit.viewer,
            application_model=(kit.model, kit.model_sha256),
            expected_sha256=identity.image_sha256,
            slot=identity.slot,
        )
    except TwinRefusalError as exc:
        reasons.append(f"{label}_target_table:{exc.reason}")
    try:
        cal = calibration_from_image(image, kit.plant.pulse_chain)
    except FuelPlantRefusalError as exc:
        reasons.append(f"{label}_pulse_chain:{exc.reason}")
    try:
        air = AirChargeCalibration.from_image(
            image,
            model=kit.model,
            rom_map=kit.rom_map,
            slot=identity.slot,
            image_sha256=identity.image_sha256,
        )
    except (AirChargeRefusalError, ApplicationModelRefusalError) as exc:
        reasons.append(f"{label}_air_charge:{exc.reason}")
    return _Side(table, cal, air, tuple(reasons), ltft_pct)


def _rpm_quantum(contract: SimContract) -> float:
    q = contract.quantity("rpm_word")
    assert q.sensor is not None and q.sensor.quantization is not None
    return q.sensor.quantization


def rpm_word(rpm: float, contract: SimContract) -> int:
    """The ECM's engine-speed word for a stated rpm: ``round(rpm / quantum)``,
    the contract's ``rpm_word`` quantization. The one conversion the paired
    run and anything placing its points (E08.4's cells) share."""
    return int(round(rpm / _rpm_quantum(contract)))


def _ecu_chain(kit: SimKit, side: _Side, point: OperatingPoint) -> dict[str, Any]:
    """One ROM at one point, ECU side: steady BFS, X and the phi word, or why not."""
    out: dict[str, Any] = {name: None for name in OUTPUTS}
    out["reasons"] = list(side.reasons)
    out["target_phi_word"] = None
    load = point.conditioned.get(_LOAD_BYTE)
    if load is None:
        out["reasons"].append(f"conditioning_missing:{_LOAD_BYTE}")
        return out
    if side.table is None or side.cal is None or side.air is None:
        return out
    if side.table.load_point_scaler is None:
        out["reasons"].append("load_point_scaler_unresolved")
        return out
    rpm_raw = rpm_word(point.rpm, kit.contract)
    try:
        bfs = steady_bfs(side.air, maf_gps=point.maf_gps, rpm_word=rpm_raw, load_byte=int(load))
    except AirChargeRefusalError as exc:
        out["reasons"].append(f"steady_bfs:{exc.reason}")
        return out
    out["base_fuel_schedule"] = bfs.word
    out["bfs_interval"] = (bfs.lo, bfs.hi)
    out["fuel_comp"] = bfs.fuel_comp
    x = x_from_bfs(bfs.word, side.table.load_point_scaler, kit.twin)
    phi = predict_phi(side.table, kit.twin, load_raw=x, rpm_raw=rpm_raw, flag_raw=point.pedal_flag)
    out["load_input_x"] = x
    if phi.word is None:
        out["reasons"].append(f"target_lookup:{phi.reason}")
        return out
    out["target_phi"] = phi.phi
    out["target_phi_word"] = phi.word
    return out


def _fuel_chain(
    kit: SimKit,
    side: _Side,
    point: OperatingPoint,
    ecu: dict[str, Any],
    *,
    stft_pct: float,
    overrides: Mapping[str, float],
    hold: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Pulse, total trim, lambda and fuel for one ROM at one point (a copy of
    ``ecu`` with them filled in).

    Open loop (``hold`` None): the stated trims. Closed loop: ``hold`` is the
    base's ``(G, k)``; the trim settles where this ROM meters the same open
    time, ``k = G_b x k_b / G`` (:func:`~src.analysis.paired_steady.closed_loop_k`),
    so the pulse is the base's metered part plus this ROM's own dead time."""
    out = dict(ecu)
    out["reasons"] = list(ecu["reasons"])
    if ecu["target_phi_word"] is None or side.cal is None:
        return out
    pulse = predict_pulse(
        bfs=[ecu["base_fuel_schedule"]],
        phi_q14=[ecu["target_phi_word"]],
        stft_word=trim_word([stft_pct]),
        ltft_word=trim_word([side.ltft_pct]),
        vb_counts=vb_counts_from_volts([point.battery_v]),
        rpm=[point.rpm],
        baro_kpa=[point.baro_kpa],
        iat_c=[point.iat_c],
        cal=side.cal,
    )
    assert pulse.g is not None
    g = int(pulse.g[0])
    dead = int(pulse.dead_time[0])
    if hold is None:
        k: float = trim_k(stft_pct, side.ltft_pct)
        counts = int(pulse.pulse[0])
    else:
        g_base, k_base = hold
        k = closed_loop_k(g_base, k_base, g)
        counts = ((g_base * k_base) >> 16) + dead
    out["g_word"] = g
    out["k_word"] = k
    out["fuel_trim_total_pct"] = total_trim_pct(k)
    pulse_ms = counts * PULSE_COUNT_MS
    dead_ms = dead * PULSE_COUNT_MS
    out["injector_pulse_ms"] = pulse_ms
    out["effective_ms"] = pulse_ms - dead_ms
    params = plant_parameters(kit, overrides)
    plant = predict(
        PlantInputs(
            pulse_ms=np.array([pulse_ms]),
            ecu_dead_time_ms=np.array([dead_ms]),
            rpm=np.array([point.rpm]),
            maf_gps=np.array([point.maf_gps]),
            baro_kpa=np.array([point.baro_kpa]),
            iat_c=np.array([point.iat_c]),
            rail_kpa=np.array([plant_rail_kpa(point)]),
            map_kpa=None if point.map_kpa_source == "plant_model" else np.array([point.map_kpa]),
        ),
        params,
        kit.plant.geometry,
    )
    out["delivered_lambda"] = float(plant.lambda_delivered[0])
    out["fuel_per_event_g"] = float(plant.fuel_g[0])
    if plant.manifold_refusal is not None and plant.manifold_refusal[0]:
        out["reasons"].append(f"plant:{plant.manifold_refusal[0]}")
    return out


def plant_rail_kpa(point: OperatingPoint) -> float:
    """The rail reading the plant gets: NaN where none was measured, so its
    rail working assumption (a drawn parameter) applies."""
    return math.nan if point.rail_kpa_source == "working_assumption" else float(point.rail_kpa)


@dataclass(frozen=True)
class _PairPoint:
    base: dict[str, Any]
    candidate: dict[str, Any]
    #: ``closed_loop`` (fixed point), ``closed_loop_setpoint_moved`` or None.
    loop: str | None


def _pair_point(
    kit: SimKit,
    sides: tuple[_Side, _Side],
    point: OperatingPoint,
    stfts: tuple[float, float],
    overrides: Mapping[str, float],
) -> _PairPoint:
    """Both ROMs at one point. A closed-loop point holds the base's metered
    open time on the candidate while the target phi word is the same on both
    sides; a moved word moves the setpoint, whose relation to phi is not
    decoded, so the candidate's fuel side is not computed."""
    base_side, cand_side = sides
    b_ecu = _ecu_chain(kit, base_side, point)
    c_ecu = _ecu_chain(kit, cand_side, point)
    b = _fuel_chain(kit, base_side, point, b_ecu, stft_pct=stfts[0], overrides=overrides)
    closed = any(_plant_regime(e["target_phi"], kit) == "closed_loop" for e in (b_ecu, c_ecu))
    if not closed:
        c = _fuel_chain(kit, cand_side, point, c_ecu, stft_pct=stfts[1], overrides=overrides)
        return _PairPoint(b, c, None)
    if b_ecu["target_phi_word"] is None or b_ecu["target_phi_word"] != c_ecu["target_phi_word"]:
        return _PairPoint(b, c_ecu, SETPOINT_MOVED)
    hold = (int(b["g_word"]), int(b["k_word"])) if b.get("g_word") is not None else None
    if hold is None:
        return _PairPoint(b, c_ecu, "closed_loop")
    c = _fuel_chain(kit, cand_side, point, c_ecu, stft_pct=stfts[1], overrides=overrides, hold=hold)
    return _PairPoint(b, c, "closed_loop")


Status = Literal["supported", "unsupported"]


@dataclass
class PairedResult:
    """Baseline/candidate traces, deltas and what they rest on. Always synthetic."""

    label: Literal["simulation"]
    synthetic: Literal[True]
    scenario_sha256: str
    base_image_sha256: str
    candidate_image_sha256: str
    outcome: str
    rom_report: dict[str, Any]
    learned_state: dict[str, Any]
    points: list[dict[str, Any]]
    coverage: dict[str, float]
    affected_cells: list[dict[str, Any]]
    unresolved: list[str]
    plant: dict[str, Any]
    #: Outputs the application model traces but this slice does not compute:
    #: their ROM-effect status only (``unchanged`` is a supported zero delta).
    traced_outputs: dict[str, dict[str, Any]]
    provenance: dict[str, Any]
    #: Stated conditions the supported values rest on.
    assumptions: list[str] = field(default_factory=list)

    @property
    def session(self) -> str:
        return f"{SYNTHETIC_SESSION_PREFIX}{self.scenario_sha256[:12]}"

    def traces(self) -> pd.DataFrame:
        """Long frame of every computed value, under a ``_sim-*`` session name
        that no identification, evaluation or memory path accepts."""
        rows = []
        for p in self.points:
            for name in OUTPUTS:
                o = p["outputs"][name]
                rows.append(
                    {
                        "session": self.session,
                        "point": p["index"],
                        "output": name,
                        "base": o["base"],
                        "candidate": o["candidate"],
                        "delta": o["delta"],
                        "status": o["status"],
                    }
                )
        frame = pd.DataFrame(rows)
        frame.attrs["synthetic"] = True
        frame.attrs["scenario_sha256"] = self.scenario_sha256
        return frame

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _plant_regime(phi: float | None, kit: SimKit) -> str | None:
    """The plant's regime name for a commanded phi (as E07.3 labels reads)."""
    if phi is None:
        return None
    band = kit.delay.closed_loop_band
    if abs(phi - 1.0) <= band:
        return "closed_loop"
    return "enrichment" if phi > 1.0 + band else "lean_target"


#: Outputs computed by the plant: gated on the regimes it qualified in.
_PLANT_OUTPUTS: frozenset[str] = frozenset({"delivered_lambda", "fuel_per_event_g"})


def _regime(point: OperatingPoint, kit: SimKit) -> str | None:
    """A regime no part of the slice models (cold: wall wetting and the cold
    enrichment states). Closed loop is modelled at the trim's fixed point."""
    if point.coolant_c < kit.plant.steady.min_coolant_c:
        return "regime_unsupported:cold"
    return None


def _steady_assumptions(coverage: SteadyCoverage) -> list[str]:
    return [
        "steady BFS: the airflow and fill filters have settled and the transient term is 0 "
        f"(absorbing {sorted(coverage.absorbed)}); the engine runs past the start branch, "
        "with no MAF fault and the normal (adding) fill-filter form",
        "the ECU's MAF reading is the prescribed airflow (PID 0x10 word); a MAF transfer "
        "error stays lumped in the plant's identified scale, as in identification",
    ]


#: A gate reason and the application-model node it names (None: no node, so
#: it reaches every output).
GateReason = tuple[str, str | None]


def computed_node(model: ApplicationModel, output_id: str, *, written: bool = False) -> str | None:
    """The application-model node this slice computes for a model output:
    the node its twin predicts, else the node that writes it (``written``:
    always the writer, the word the output is). None when the model does not
    declare the output."""
    found = next((o for o in model.outputs if o.id == output_id), None)
    if found is None:
        return None
    if found.twin is None or written:
        return found.written_by
    return found.twin.predicts


def twin_gate(
    report: EffectReport,
    coverage: SteadyCoverage,
    assumptions: list[str],
    model: ApplicationModel,
) -> list[GateReason]:
    """Gate reasons from the two application-model outputs this slice
    computes, each with the node it names; a covered dependency becomes a
    stated assumption instead."""
    gate: list[GateReason] = []
    bfs_node = computed_node(model, _BFS_OUTPUT, written=True)
    twin_node = computed_node(model, _TWIN_OUTPUT, written=True)
    verdicts = ecu_output_gate(report, coverage)
    bfs = next((o for o in report.outputs if o.output == _BFS_OUTPUT), None)
    if bfs is None:
        gate.append((f"rom:{_BFS_OUTPUT}_not_traced", bfs_node))
    elif bfs.status != "unchanged":
        rest, _ = verdicts[_BFS_OUTPUT]
        if rest:
            gate.extend((f"rom:{r}", bfs_node) for r in rest)
            gate.append((f"rom:{_BFS_OUTPUT}_{bfs.status}", bfs_node))
        else:
            assumptions.append(
                f"steady: the change reaches BFS through {list(bfs.path)}; computed at the "
                f"fixed point, rom_effects reasons {list(bfs.reasons)} absorbed"
            )
    twin = next((o for o in report.outputs if o.output == _TWIN_OUTPUT), None)
    if twin is None:
        gate.append((f"rom:{_TWIN_OUTPUT}_not_traced", twin_node))
    elif twin.status != "unchanged":
        held, evaluable = verdicts[_TWIN_OUTPUT]
        if not evaluable:
            gate.extend((f"rom:{r}", twin_node) for r in held)
            gate.append((f"rom:{_TWIN_OUTPUT}_{twin.status}", twin_node))
        else:
            # The twin computes the gated Target AFR lookup; the final word adds a
            # slew and a cap (not modelled). At a steady point neither binds (the
            # contract's steady_state init for target_phi_word): stated, not hidden.
            assumptions.append(
                "steady: the final command equals the gated lookup (slew and cap not binding); "
                f"rom_effects reasons {list(twin.reasons)}"
            )
    return gate


# -- which outputs a reason reaches (E08.10) ---------------------------------------------------

#: Per-point reasons that name a computed node, by prefix: the model output
#: whose computed node (:func:`computed_node`) the reason is about. A
#: ``target_lookup:*`` reason is the Target AFR lookup's (the twin's gated
#: lookup), which steady BFS is computed before; ``steady_bfs:*`` is BFS's.
#: Any other reason names no node and reaches every output.
_REASON_OUTPUT_BY_PREFIX: dict[str, str] = {
    "target_lookup": _TWIN_OUTPUT,
    "steady_bfs": _BFS_OUTPUT,
}


def _output_sinks(model: ApplicationModel) -> dict[str, frozenset[str] | None]:
    """The application-model nodes each computed output is: BFS the BFS
    word's writer, target phi the final command word's (the twin computes the
    gated lookup upstream of it; the corrections between are what its gate
    reasons name), and every fuel-side output both (the pulse chain takes the
    BFS word and the phi word, ``predict_pulse(bfs=, phi_q14=)``). None where
    the model lacks one: every reason then reaches that output."""
    bfs = computed_node(model, _BFS_OUTPUT, written=True)
    phi = computed_node(model, _TWIN_OUTPUT, written=True)
    own: dict[str, str | None] = {"base_fuel_schedule": bfs, "target_phi": phi}
    out: dict[str, frozenset[str] | None] = {}
    for name in OUTPUTS:
        nodes = (own[name],) if name in own else (bfs, phi)
        out[name] = None if any(n is None for n in nodes) else frozenset(n for n in nodes if n)
    return out


@dataclass(frozen=True)
class ReasonScope:
    """Which outputs each reason reaches: those downstream of the node it
    names, through the application model's ``depends_on``
    (:func:`~src.analysis.rom_effects._ancestors`). A reason tied to no node
    reaches every output, conservatively. ``run_paired`` and the Monte Carlo
    fast path both filter every output's reasons, and decide whether a gate
    withholds the candidate, through this one object."""

    #: Per output, every node it rests on (its sinks and their ancestors);
    #: None: the output rests on everything.
    rests_on: Mapping[str, frozenset[str] | None]
    #: Run-level gate reasons and the nodes each is tied to (None: no node).
    gate: Mapping[str, frozenset[str | None]]
    #: Per-point reason prefixes and the node each names (None: no node).
    prefixes: Mapping[str, str | None]
    #: Every application-model node id.
    model_nodes: frozenset[str]

    @classmethod
    def of(cls, kit: SimKit, gate: Iterable[GateReason]) -> ReasonScope:
        return cls.for_model(kit.model, gate)

    @classmethod
    def for_model(cls, model: ApplicationModel, gate: Iterable[GateReason]) -> ReasonScope:
        """The scope for a model alone (``rom_levers`` has no kit)."""
        rests_on: dict[str, frozenset[str] | None] = {}
        for name, sinks in _output_sinks(model).items():
            rests_on[name] = (
                None if sinks is None else frozenset(n for s in sinks for n in _ancestors(model, s))
            )
        nodes: dict[str, set[str | None]] = {}
        for reason, node in gate:
            nodes.setdefault(reason, set()).add(node)
        return cls(
            rests_on=rests_on,
            gate={r: frozenset(n) for r, n in nodes.items()},
            prefixes={
                prefix: computed_node(model, output)
                for prefix, output in _REASON_OUTPUT_BY_PREFIX.items()
            },
            model_nodes=frozenset(n.id for n in model.nodes),
        )

    def _nodes(self, reason: str) -> frozenset[str | None]:
        if reason in self.gate:
            # A gate reason that names a model node itself (rom_effects'
            # ``unmodelled:<node>``, ``observed_not_predicted:<node>``) is that
            # node's; otherwise it is the node of the output that raised it.
            named = reason.rsplit(":", 1)[-1]
            if named in self.model_nodes:
                return frozenset({named})
            return self.gate[reason]
        prefix = reason.split(":", 1)[0]
        if ":" in reason and prefix in self.prefixes:
            return frozenset({self.prefixes[prefix]})
        return frozenset({None})

    def reaches(self, reason: str, output: str) -> bool:
        rests_on = self.rests_on[output]
        if rests_on is None:
            return True
        return any(n is None or n in rests_on for n in self._nodes(reason))

    def keep(self, output: str, reasons: Iterable[str]) -> list[str]:
        """``reasons`` that reach ``output``, in order."""
        return [r for r in reasons if self.reaches(r, output)]


# -- the plant working assumptions a result rests on (E08.10) --------------------------------

#: Every result names each plant working assumption it rests on with this
#: prefix, one string per parameter (:func:`format_plant_assumption`).
PLANT_ASSUMPTION_PREFIX = "plant working assumption: "
#: The registry parameters whose working value the plant reads only at some
#: points: the rail where no rail reading was given, VE where the plant's own
#: manifold model sets MAP (a stated or measured MAP does not read it).
_RAIL_PARAMETER = "rail_pressure"
_VE_PARAMETER = VE_PARAMETER


@dataclass(frozen=True)
class PlantAssumption:
    """One registry working assumption a run resolved and rests on."""

    parameter: str
    units: str
    value: float
    bracket: tuple[float, float]
    #: The declaration it comes from (``<profile>/<file>``, plant version).
    source: str
    effect: str
    #: True: the run varies it inside its bracket; False: held at ``value``.
    drawn: bool
    #: What the number is, where the plant uses it other than as itself.
    role: str = ""


def format_plant_assumption(a: PlantAssumption) -> str:
    """The one string form of a plant working assumption (``assumptions`` is
    a list of strings end to end: the CLI, the report and the GUI)."""
    lo, hi = a.bracket
    how = "drawn inside its bracket in this run" if a.drawn else "held at that value"
    role = f", {a.role}" if a.role else ""
    return (
        f"{PLANT_ASSUMPTION_PREFIX}{a.parameter} ({a.units}) = {a.value!r}{role}, bracket "
        f"[{lo!r}, {hi!r}], {how} ({a.source}; unresolved effect {a.effect})"
    )


def plant_assumptions(
    kit: SimKit, points: Sequence[OperatingPoint], *, drawn: Collection[str]
) -> list[PlantAssumption]:
    """Every working assumption :func:`~src.analysis.fuel_plant.resolve_parameters`
    resolves for ``kit``'s plant and some point reads, from the registry
    itself (never a hand list): a parameter not ``known`` whose resolved number
    is its registry ``working_assumption.value`` (B06's restated delay among
    them: the registry restates it, and resolution checks it equal). The rail
    counts only when a point has no rail reading; the manifold model's
    parameters (:data:`~src.analysis.fuel_plant.MANIFOLD_READS`: VE, and
    fuel-mv-3's residual-gas form) only when a point's MAP is the plant's own
    model, since a stated MAP reads none of them. ``drawn`` names the
    parameters the run varies."""
    resolved = resolve_parameters(kit.plant, kit.delay)
    source = f"{kit.plant_file} registry, plant {kit.plant.plant_version}"
    reads_rail = any(pt.rail_kpa_source == "working_assumption" for pt in points)
    reads_model = any(pt.map_kpa_source == "plant_model" for pt in points)
    out: list[PlantAssumption] = []
    for pid, value in resolved.values.items():
        p = kit.plant.registry.get(pid)
        wa = p.working_assumption
        if wa is None or p.status == "known" or value != float(wa.value):
            continue
        if pid == _RAIL_PARAMETER and not reads_rail:
            continue
        if pid in MANIFOLD_READS and not reads_model:
            continue
        where = source
        if pid == DELAY_PARAMETER:
            where = f"{source}, restating {kit.plant.delay.file}"
        role = ""
        if pid == _VE_PARAMETER and resolved.ve_curve is not None:
            assert resolved.ve_fitted_at is not None
            floors = _floor_band_names(resolved.ve_curve, resolved.ve_fitted_at)
            role = (
                f"the floor under the frozen baseline's VE(rpm) fit: the VE of rpm bands "
                f"{floors}, each at least its own envelope (the fitted bands are held at the fit)"
            )
        out.append(
            PlantAssumption(
                parameter=pid,
                units=p.units,
                value=float(value),
                bracket=(float(wa.bracket[0]), float(wa.bracket[1])),
                source=where,
                effect=wa.effect,
                drawn=pid in drawn,
                role=role,
            )
        )
    return out


def _floor_band_names(curve: VeCurve, fitted_at: float) -> str:
    e = curve.rpm_edges
    return ", ".join(
        f"[{e[k]:g}, {e[k + 1]:g}{']' if k == len(e) - 2 else ')'}"
        for k, is_floor in enumerate(curve.floor_bands(fitted_at))
        if is_floor
    )


def held_ve_fit(kit: SimKit, points: Sequence[OperatingPoint]) -> str | None:
    """The frozen VE(rpm) fit a result rests on, as one string (E08.11), or
    ``None`` on a plant with one working VE or where no point's MAP is the
    plant's own model. The fit is a ``log_fit`` (the contract's
    ``parameter_fits``), not a bracket: it is held in every run, never drawn,
    and named with its bands, the VE each band runs at, and its baseline."""
    resolved = resolve_parameters(kit.plant, kit.delay)
    curve, baseline = resolved.ve_curve, kit.plant.baseline
    if curve is None or baseline is None:
        return None
    if not any(pt.map_kpa_source == "plant_model" for pt in points):
        return None
    assert resolved.ve_fitted_at is not None
    floor = resolved.values[_VE_PARAMETER]
    lo_b, hi_b = curve.baro_kpa
    lo_t, hi_t = curve.iat_c
    return (
        f"{PLANT_ASSUMPTION_PREFIX}volumetric_efficiency(rpm) is the frozen fit of baseline "
        f"{baseline.id} (sha256 {resolved.baseline_sha256[:12]}), held at the fit in this run, "
        f"never drawn: {curve.describe(floor, fitted_at=resolved.ve_fitted_at)}; a floor band "
        f"runs at {_VE_PARAMETER} (named above); part-load VE equals full-breathing VE at the "
        f"same rpm by assumption; outside rpm {curve.rpm_edges[0]:g}-{curve.rpm_edges[-1]:g}, "
        f"baro {lo_b:g}-{hi_b:g} kPa or IAT {lo_t:g}-{hi_t:g} C a MAP is "
        f"{MAP_OUTSIDE_VE_FIT} ({kit.plant_file}, plant {kit.plant.plant_version})"
    )


def plant_assumption_lines(
    kit: SimKit, points: Sequence[OperatingPoint], *, drawn: Collection[str]
) -> list[str]:
    """Every plant assumption a result over ``points`` names, as strings: each
    working assumption (:func:`plant_assumptions`), then a VE(rpm) plant's
    held fit (:func:`held_ve_fit`). The one list ``run_paired`` and the Monte
    Carlo both state."""
    lines = [format_plant_assumption(a) for a in plant_assumptions(kit, points, drawn=drawn)]
    fit = held_ve_fit(kit, points)
    if fit is not None:
        lines.append(fit)
    return lines


def run_paired(
    kit: SimKit,
    scenario: PairedScenario,
    base_image: bytes,
    candidate_image: bytes,
    *,
    plant_overrides: Mapping[str, float] | None = None,
) -> PairedResult:
    """Run both ROMs over the scenario's points; refuse what the contract or the
    slice cannot evaluate, and name what each output rests on.

    ``plant_overrides`` runs both ROMs on one plant parameter vector other
    than the frozen working values (:func:`plant_parameters`: a Monte Carlo
    draw, E08.3). Both sides always share it."""
    overrides = dict(plant_overrides or {})
    if overrides:
        plant_parameters(kit, overrides)
    _refuse_recorded_sources(scenario)
    _with_contract_mode(kit.contract, scenario, kit.contract_sha256)
    for label, image, identity in (
        ("base", base_image, scenario.base),
        ("candidate", candidate_image, scenario.candidate),
    ):
        if sha256_hex(image) != identity.image_sha256:
            raise SimulationRefusalError(
                "image_hash_mismatch", f"{label} bytes are not image {identity.image_sha256}"
            )
    if (scenario.plant.version, scenario.plant.hardware_configuration) != (
        kit.plant.plant_version,
        kit.plant.hardware_configuration,
    ):
        raise SimulationRefusalError(
            "plant_mismatch",
            f"the scenario names plant {scenario.plant.version}/"
            f"{scenario.plant.hardware_configuration}; the kit's is {kit.plant.plant_version}/"
            f"{kit.plant.hardware_configuration}",
        )
    resets = _resets(kit.contract, scenario.candidate_events)
    report: EffectReport = compare_roms(
        base_image,
        candidate_image,
        kit.rom_map,
        kit.model,
        kit.model_sha256,
        base_slot=scenario.base.slot,
        candidate_slot=scenario.candidate.slot,
        slot_source=scenario.base.slot_source,
        contract=kit.contract,
        mode=scenario.mode,
    )
    qualification = plant_qualification(kit.plant)
    base_ltft = scenario.learned.ltft_pct
    cand_ltft = NEUTRAL_TRIM_PCT if "ltft_b1" in resets else base_ltft
    stft = NEUTRAL_TRIM_PCT if "stft_b1" in resets else scenario.learned.stft_pct
    learned = {
        "base": {"stft_pct": scenario.learned.stft_pct, "ltft_pct": base_ltft},
        "candidate": {"stft_pct": stft, "ltft_pct": cand_ltft},
        "resets": resets,
        "relearning": "not modelled: the candidate's trims stay at their reset values",
        "source": scenario.learned.source,
    }
    scenario_sha = scenario.scenario_hash(kit.contract, kit.contract_sha256)
    if overrides:
        # A run on drawn parameters is a different run: its own hash and session.
        scenario_sha = hashlib.sha256(
            json.dumps(
                {"scenario": scenario_sha, "plant_overrides": dict(sorted(overrides.items()))},
                sort_keys=True,
            ).encode()
        ).hexdigest()
    provenance = {
        "tool": "paired_scenarios",
        "tool_version": TOOL_VERSION,
        "contract_sha256": kit.contract_sha256,
        "application_model_sha256": kit.model_sha256,
        "plant_version": kit.plant.plant_version,
        "baseline_sha256": qualification.baseline_sha256,
        "rom_effects": report.provenance,
    }
    if overrides:
        provenance["plant_overrides"] = dict(sorted(overrides.items()))
    base_report = {
        "outcome": report.outcome,
        "reason": report.reason,
        "changed_nodes": list(report.changed_nodes),
        "outputs": {
            o.output: {"status": o.status, "reasons": list(o.reasons)} for o in report.outputs
        },
    }
    if report.outcome == "unevaluable" and report.reason is not None:
        return PairedResult(
            label="simulation",
            synthetic=True,
            scenario_sha256=scenario_sha,
            base_image_sha256=scenario.base.image_sha256,
            candidate_image_sha256=scenario.candidate.image_sha256,
            outcome="unevaluable",
            rom_report=base_report,
            learned_state=learned,
            points=[],
            coverage={name: 0.0 for name in OUTPUTS},
            affected_cells=[],
            unresolved=[f"rom:{report.reason}"],
            plant=asdict(qualification),
            traced_outputs={},
            provenance=provenance,
        )
    reached = set(report.changed_nodes) | {n for o in report.outputs for n in o.path}
    unknown_changed = any(r.byte_class in ("unknown", "mapped_unmodelled") for r in report.ranges)
    coverage = steady_coverage(kit.contract, scenario.mode, report.changed_nodes)
    gated: list[GateReason] = []
    for point in scenario.points:
        for quantity in point.conditioned:
            if quantity in COMPUTED_HERE:
                raise SimulationRefusalError(
                    "conditioning_not_allowed",
                    f"{quantity} is computed here, per ROM, at its steady fixed point; a stated "
                    "value cannot stand in for it",
                )
            node = conditionable_node(kit.contract, scenario.mode, quantity)
            if node is None:
                raise SimulationRefusalError(
                    "conditioning_not_allowed",
                    f"{quantity} is not a computed ECU quantity on the manifest; supply it as "
                    "a boundary if the mode takes it, or not at all",
                )
            if node in reached:
                gated.append((f"conditioned_on_changed_path:{quantity}", node))
    assumptions: list[str] = [
        "LTFT acts in open loop: the decoded pulse formula adds STFT and LTFT into k_b with "
        "no open-loop gate (docs/ecu-firmware-analysis.md:629)",
        "STFT is neutral (the stated value) in open loop: assumed, not decoded",
        *_steady_assumptions(coverage),
    ]
    gated.extend(twin_gate(report, coverage, assumptions, kit.model))
    if unknown_changed:
        gated.append(("rom:unknown_bytes_changed", None))
    gate = sorted({r for r, _ in gated})
    # Each reason reaches only the outputs downstream of the node it names
    # (E08.10): a Target AFR lookup gate does not withhold steady BFS.
    scope = ReasonScope.of(kit, gated)
    withheld = {name: bool(scope.keep(name, gate)) for name in OUTPUTS}

    base_side = _side(kit, base_image, scenario.base, base_ltft, "base")
    cand_side = _side(kit, candidate_image, scenario.candidate, cand_ltft, "candidate")
    sides = (base_side, cand_side)
    stfts = (scenario.learned.stft_pct, stft)
    s_val = s_lo = s_hi = math.nan
    baseline = kit.plant.baseline
    if baseline is not None:
        s_val = overrides.get("stoich_air_per_open_ms", baseline.values["stoich_air_per_open_ms"])
        s_lo, s_hi = baseline.intervals["stoich_air_per_open_ms"]
    cases = {
        name: case
        for name, case in sensitivity_cases(kit.plant, kit.delay).items()
        if "lambda_observation_delay" not in case
    }
    points_out: list[dict[str, Any]] = []
    supported_counts = dict.fromkeys(OUTPUTS, 0)
    loops: set[str] = set()
    for index, point in enumerate(scenario.points):
        pair = _pair_point(kit, sides, point, stfts, overrides)
        b, c = pair.base, pair.candidate
        if pair.loop is not None:
            loops.add(pair.loop)
        regime = _regime(point, kit)
        plant_regimes = {
            r
            for r in (_plant_regime(b["target_phi"], kit), _plant_regime(c["target_phi"], kit))
            if r
        }
        # A qualified plant answers only in the regimes it passed in: a
        # closed-loop-only pass says nothing about WOT deltas.
        unqualified_regimes = (
            sorted(plant_regimes - set(qualification.regimes)) if qualification.qualified else []
        )
        outputs: dict[str, dict[str, Any]] = {}
        for name in OUTPUTS:
            reasons = scope.keep(name, [*gate, *b["reasons"], *c["reasons"]])
            if name not in _ECU_OUTPUTS:
                if regime is not None:
                    reasons.append(regime)
                if pair.loop == SETPOINT_MOVED:
                    reasons.append(SETPOINT_MOVED)
                elif pair.loop == "closed_loop" and closed_loop_trim_pending(kit.model):
                    reasons.append(TRIM_AUTHORITY_UNDECODED)
                if name in _PLANT_OUTPUTS:
                    reasons.extend(f"regime_not_qualified:{r}" for r in unqualified_regimes)
                eff = [s["effective_ms"] for s in (b, c) if s.get("effective_ms") is not None]
                if any(e < kit.plant.steady.min_effective_pulse_ms for e in eff):
                    reasons.append("short_pulse_undecoded")
            if name == "injector_pulse_ms" and point.battery_v_source == "unmeasured":
                # The dead time reads the voltage; the metered part does not,
                # so lambda, fuel and their deltas stand without it.
                reasons.append(BATTERY_UNMEASURED)
            if b[name] is None or c[name] is None:
                reasons.append("not_computed")
            reasons = sorted(set(reasons))
            status: Status = "unsupported" if reasons else "supported"
            if status == "supported":
                supported_counts[name] += 1
            # The change reaches what this point is conditioned on (or an unknown
            # byte): the candidate's value at the shared condition is not what
            # it would compute, so neither it nor a delta is given.
            candidate = None if withheld[name] else c[name]
            delta = (
                None if b[name] is None or candidate is None else float(candidate) - float(b[name])
            )
            outputs[name] = {
                "base": b[name],
                "candidate": candidate,
                "delta": delta,
                "status": status,
                "reasons": reasons,
            }
        bfs = outputs["base_fuel_schedule"]
        bfs["interval"] = {"base": b.get("bfs_interval"), "candidate": c.get("bfs_interval")}
        lam = outputs["delivered_lambda"]
        if lam["delta"] is not None and baseline is not None:
            # The lumped scale's interval moves lambda as 1/s on both sides.
            lam["parameter_band"] = (lam["base"] * s_val / s_hi, lam["base"] * s_val / s_lo)
            spread = []
            for case in cases.values():
                cp = _pair_point(kit, sides, point, stfts, {**overrides, **case})
                cb, cc = cp.base["delivered_lambda"], cp.candidate["delivered_lambda"]
                if cb is not None and cc is not None:
                    spread.append(cc / cb - 1.0)
            ratio = lam["candidate"] / lam["base"] - 1.0
            lam["relative_delta"] = ratio
            lam["relative_delta_range_over_assumptions"] = (
                (min([ratio, *spread]), max([ratio, *spread])) if spread else (ratio, ratio)
            )
        record: dict[str, Any] = {
            "index": index,
            "load_input_x": {"base": b.get("load_input_x"), "candidate": c.get("load_input_x")},
            "loop": pair.loop or "open_loop",
            "outputs": outputs,
        }
        if (
            pair.loop == "closed_loop"
            and c.get("fuel_trim_total_pct") is not None
            and not withheld["fuel_trim_total_pct"]
        ):
            # The fixed point fixes the total; with the LTFT the candidate
            # starts from (reset by a flash), the STFT carries the rest until
            # the long-term trim relearns (not modelled).
            record["candidate_stft_at_its_ltft_pct"] = c["fuel_trim_total_pct"] - cand_ltft
        points_out.append(record)
    if "closed_loop" in loops:
        assumptions.append(
            "closed loop: the stated trims are the base ROM's settled trims at the point; the "
            "candidate's trim settles where it meters the base's open time (same setpoint, air, "
            "rail and manifold), so delivered lambda and fuel are unchanged and the total trim "
            "carries the change; trim authority, the STFT/LTFT split and settling are not "
            "modelled"
        )
    assumptions.extend(plant_assumption_lines(kit, scenario.points, drawn=overrides))
    n = max(len(scenario.points), 1)
    unresolved = set(gate)
    for p in points_out:
        for o in p["outputs"].values():
            unresolved.update(o["reasons"])
    resolved = resolve_parameters(kit.plant, kit.delay)
    unresolved.update(f"plant:{e}" for e in resolved.effects)
    if any(p.rail_kpa_source == "working_assumption" for p in scenario.points):
        # No rail reading at the point: the plant ran its rail working value.
        unresolved.add(f"plant:{resolved.rail_effect}")
    if not qualification.qualified:
        assert qualification.reason is not None
        unresolved.add(qualification.reason)
    traced = {
        o.output: {"status": o.status, "reasons": list(o.reasons)}
        for o in report.outputs
        if o.output not in _COMPUTED_TWIN_OUTPUTS
    }
    return PairedResult(
        label="simulation",
        synthetic=True,
        scenario_sha256=scenario_sha,
        base_image_sha256=scenario.base.image_sha256,
        candidate_image_sha256=scenario.candidate.image_sha256,
        outcome=report.outcome,
        rom_report=base_report,
        learned_state=learned,
        points=points_out,
        coverage={name: supported_counts[name] / n for name in OUTPUTS},
        affected_cells=affected_cells(base_image, candidate_image, kit.rom_map),
        unresolved=sorted(unresolved),
        plant=asdict(qualification),
        traced_outputs=traced,
        provenance=provenance,
        assumptions=assumptions,
    )


# -- what a result may be used for ---------------------------------------------------------


def rank_as_improvement(
    result: PairedResult, *, intervention: InterventionVerdict | None
) -> dict[str, Any]:
    """Whether ``result`` may be ranked as a reliable improvement.

    ``intervention`` is the sealed verdict :func:`evaluate_intervention`
    returned (``report["verdict_record"]``), never a caller's string. It must
    verify, pass, be on recorded data, name the same two images as ``result``
    and the same frozen plant baseline. ``result`` itself must come from a
    recorded (not synthetic) plant that qualified, with every output at every
    point supported. Every failing condition is named."""
    reasons: list[str] = []
    if result.plant.get("synthetic"):
        reasons.append("synthetic_plant: a labelled synthetic plant never ranks a tuning effect")
    if not result.plant.get("qualified"):
        reasons.append(str(result.plant.get("reason") or "plant_not_qualified"))
    if result.provenance.get("plant_overrides"):
        reasons.append(
            f"{OVERRIDES_NOT_FROZEN}: the plant ran on overridden parameters "
            f"{sorted(result.provenance['plant_overrides'])}, not its frozen set"
        )
    if intervention is None:
        reasons.append("intervention_test_not_passed:none")
    elif not intervention.verify():
        reasons.append("intervention_verdict_unsealed: not minted by evaluate_intervention")
    else:
        if intervention.verdict != "pass":
            reasons.append(f"intervention_test_not_passed:{intervention.verdict}")
        if intervention.synthetic:
            reasons.append("intervention_test_synthetic")
        if (intervention.base_image_sha256, intervention.candidate_image_sha256) != (
            result.base_image_sha256,
            result.candidate_image_sha256,
        ):
            reasons.append("intervention_images_differ: the test was of another change")
        if intervention.baseline_sha256 != result.plant.get("baseline_sha256"):
            reasons.append("intervention_plant_differs: the test ran another baseline")
    for p in result.points:
        for name, o in p["outputs"].items():
            if o["status"] != "supported":
                reasons.append(f"unsupported:{name}@point{p['index']}:{','.join(o['reasons'])}")
    changed_untraced = [k for k, v in result.traced_outputs.items() if v["status"] != "unchanged"]
    for name in changed_untraced:
        reasons.append(f"unsupported:{name}:{result.traced_outputs[name]['status']}")
    if not result.points:
        reasons.append("no_points")
    return {"rankable": not reasons, "reasons": reasons}


def optimise(*_: Any, **__: Any) -> None:
    """Refused: optimising against the simulator waits for qualified effect
    predictions (E07.4 and E07.6 acceptance) and still needs later physical
    verification of whatever it proposes."""
    raise SimulationRefusalError(
        "optimisation_deferred",
        "the simulator's effect predictions have not qualified (the plant fails its held-out "
        "bounds and no intervention test has passed); a proposal from it would need physical "
        "verification anyway",
    )


__all__ = [
    "BATTERY_UNMEASURED",
    "ECU_RUNNER_OUTPUTS",
    "OUTPUTS",
    "OVERRIDES_NOT_FROZEN",
    "PLANT_ASSUMPTION_PREFIX",
    "GateReason",
    "LearnedState",
    "OperatingPoint",
    "PairedResult",
    "PairedScenario",
    "PlantAssumption",
    "ReasonScope",
    "SimKit",
    "affected_cells",
    "computed_node",
    "format_plant_assumption",
    "held_ve_fit",
    "load_kit",
    "plant_assumption_lines",
    "plant_assumptions",
    "optimise",
    "plant_parameters",
    "plant_rail_kpa",
    "rank_as_improvement",
    "rpm_word",
    "run_paired",
    "twin_gate",
]
