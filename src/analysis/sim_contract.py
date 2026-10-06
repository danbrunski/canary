"""Controller / plant / scenario contracts for the engine simulator (E07.1).

The simulator couples two models through a scheduler: the **ECU twin** (integer
arithmetic decoded from the firmware, run on the ECU's own task clock) and the
**engine plant** (continuous equations on a solver step). This module is the
schema both sides and every scenario are written against, and the profile's
``engine_sim_contract.yaml`` is one vehicle's instance of it. Nothing here
simulates anything; it decides what a simulation is *allowed* to be.

Every quantity is declared once, with its units, its domain (``ecu`` integers
with width and signedness; ``plant``/``actuator``/``environment``/``driver``
continuous; ``sensor`` the typed boundary from a physical quantity to an ECU
integer, with its sampling, quantisation and delay) and its dependencies, each
``same_step`` or ``previous_step``. Each mode then assigns every quantity
exactly one role -- ``measured_input``, ``prescribed_boundary``,
``inferred_state``, ``predicted_output`` or ``unused`` -- and the schema refuses
what would make a result meaningless rather than documenting it:

- **Hidden same-step cycles.** Among the quantities a mode computes, the
  same-step dependency graph must be acyclic; a loop has to be broken by an
  explicit ``previous_step`` (a declared state, which then needs an initial
  value).
- **Predicted-as-measured.** A measured input or prescribed boundary may not be
  read from a channel that observes a quantity the mode computes (aliases
  included: PID 0x44 reads the command word). The one exception is a mode
  labelled ``score_group: state_estimation`` that names each assimilated
  channel; its scores are a separate group from open-loop prediction.
- **Undeclared inputs.** A computed quantity's dependency must have a role in
  the mode; ``unused`` is not an input.
- **Mode semantics.** ECU replay computes no plant quantity; plant replay
  computes no ECU quantity; a conditional ROM comparison and a coupled rollout
  take no measured input at all (a recording was made under one calibration,
  so it cannot feed both sides of a comparison), and any non-environment
  quantity they hold fixed names the assumption it rests on.
- **Domain boundaries.** An ECU quantity reads only ECU and sensor quantities;
  a plant quantity reads no ECU integer except through an actuator. ECU integer
  arithmetic and the plant's continuous equations cannot be mixed silently.

``subsystems`` (E07.5) declare the plant's stages: every plant and actuator
quantity belongs to exactly one, and a built stage states its bounded
applicability and, when it integrates a state, its step-size refinement and
conservation checks. Every stage names the reference that could validate it
and the measurements still missing; :mod:`src.analysis.engine_dynamics`
derives each stage's standing (nothing here can state that one is validated).

:class:`Scenario` carries the provenance of one run (ECU program, image and
active slot; plant version; hardware configuration; parameter evidence) and
:meth:`Scenario.scenario_hash` is computed from its canonical form, never
supplied. :func:`check_scenario` refuses, by name, a scenario its mode cannot
evaluate -- an unresolved active slot among them.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.ecu_application import ApplicationModel

CONTRACT_FILE = "engine_sim_contract.yaml"

Kind = Literal["ecu", "sensor", "actuator", "plant", "environment", "driver"]
Role = Literal[
    "measured_input", "prescribed_boundary", "inferred_state", "predicted_output", "unused"
]
Timing = Literal["same_step", "previous_step"]
ModeKind = Literal["ecu_replay", "plant_replay", "conditional_rom_comparison", "coupled_rollout"]
ScoreGroup = Literal["open_loop", "state_estimation"]
ClockKind = Literal["ecu_time_task", "ecu_crank_event", "solver", "acquisition"]
InitMethod = Literal["observed_checkpoint", "decoded_reset", "prescribed", "steady_state"]

#: Roles whose value the mode computes.
COMPUTED: frozenset[str] = frozenset({"inferred_state", "predicted_output"})
#: Roles whose value the mode is handed.
SUPPLIED: frozenset[str] = frozenset({"measured_input", "prescribed_boundary"})
#: Kinds held as integers in the ECU's own representation.
INTEGER_KINDS: frozenset[str] = frozenset({"ecu", "sensor"})
#: What each kind may read (a dependency of another kind is refused).
ALLOWED_READS: dict[str, frozenset[str]] = {
    "ecu": frozenset({"ecu", "sensor"}),
    "sensor": frozenset({"plant", "actuator", "environment", "driver"}),
    "actuator": frozenset({"ecu", "actuator", "plant", "environment"}),
    "plant": frozenset({"plant", "actuator", "environment", "driver"}),
    "environment": frozenset({"environment"}),
    "driver": frozenset({"driver"}),
}
#: Kinds a coupled rollout or ROM comparison may hold fixed without stating why.
FREE_BOUNDARY_KINDS: frozenset[str] = frozenset({"environment", "driver"})
#: Modes in which nothing recorded under one calibration may be supplied.
SCENARIO_ONLY_MODES: frozenset[str] = frozenset({"conditional_rom_comparison", "coupled_rollout"})
FOUR_MODES: tuple[str, ...] = (
    "ecu_replay",
    "plant_replay",
    "conditional_rom_comparison",
    "coupled_rollout",
)

_ID = r"^[a-z][a-z0-9_]*$"
_CHANNEL = r"^[a-z]+:[A-Za-z0-9_]+$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SimulationRefusalError(ValueError):
    """A scenario or contract the simulator cannot evaluate; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


# -- units and clocks ---------------------------------------------------------------


class Unit(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    dimension: str = Field(min_length=1)
    meaning: str = Field(min_length=1)


class Clock(_Strict):
    id: str = Field(pattern=_ID)
    kind: ClockKind
    #: Fixed period; ``None`` for an event clock or an irregular acquisition.
    period_s: float | None = Field(default=None, gt=0)
    #: Whether ``period_s`` is established. ``unverified``: a stated figure no
    #: decode or measurement has confirmed; a result timed on it says so.
    period_status: Literal["verified", "unverified", "chosen"] | None = None
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _period(self) -> Clock:
        fixed = self.kind in ("ecu_time_task", "solver")
        if fixed != (self.period_s is not None):
            raise ValueError(
                f"clock {self.id}: a {self.kind} clock "
                f"{'needs' if fixed else 'has no'} fixed period_s"
            )
        if fixed != (self.period_status is not None):
            raise ValueError(f"clock {self.id}: period_status goes with, and only with, period_s")
        if (self.kind == "solver") != (self.period_status == "chosen"):
            raise ValueError(
                f"clock {self.id}: a solver step is chosen; an ECU period is verified or not"
            )
        return self


class EcuTask(_Strict):
    #: Index in ``EcuSchedule.task_table``.
    entry: int = Field(ge=0)
    function: int = Field(ge=0)
    name: str = Field(min_length=1)


class EcuSchedule(_Strict):
    """The order the ECU runs its fuel chain each tick, from the firmware."""

    clock: str
    #: The task table that runs ``tasks`` in this order.
    task_table: int = Field(ge=0)
    tasks: list[EcuTask] = Field(min_length=1)
    #: The task whose internal call order the application model's ``schedule``
    #: lists, and the cycle-start copy before it; both must match the model.
    application_model_task: int = Field(ge=0)
    cycle_start: int = Field(ge=0)
    #: Crank-synchronous work (the ignition chain) runs on this clock.
    event_clock: str | None = None
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _task(self) -> EcuSchedule:
        functions = [t.function for t in self.tasks]
        if len(set(functions)) != len(functions):
            raise ValueError("ecu_schedule: a task runs once per tick")
        if self.application_model_task not in functions:
            raise ValueError("ecu_schedule: application_model_task is not one of the tasks")
        return self


class Solver(_Strict):
    clock: str
    method: Literal["fixed_step_euler", "rk4"]
    #: Values cross between the twin and the plant only at ECU task boundaries.
    exchange: Literal["ecu_task_boundary"]
    evidence: str = Field(min_length=1)


# -- quantities ---------------------------------------------------------------------


class IntegerRepresentation(_Strict):
    width: Literal[8, 16, 32]
    signed: bool
    #: RAM address, when the value lives at a known one.
    ram: int | None = Field(default=None, ge=0xFFFF0000, le=0xFFFFFFFF)
    #: Engineering value per count, as text (``12.5 rpm``), or ``relative``.
    scale: str = Field(min_length=1)
    #: How the address, width and scale are known: ``observed`` (decode agrees
    #: with an independent observation), ``code`` (read from the code only) or
    #: ``inferred``.
    confidence: Literal["observed", "code", "inferred"]
    #: What a reader must know about the word (a copy it is observed through,
    #: its lag, which parts are inferred).
    note: str | None = None


class Dependency(_Strict):
    quantity: str
    timing: Timing


class SensorSpec(_Strict):
    """How a physical quantity becomes the ECU's integer."""

    measures: str
    sample_clock: str
    #: Engineering units of ``measures`` per count; ``None`` where the ECU's
    #: internal word is not decoded (never a guessed number).
    quantization: float | None = Field(default=None, gt=0)
    #: Response plus acquisition delay; ``None`` while it is not measured.
    delay_s: float | None = Field(default=None, ge=0)
    evidence: str = Field(min_length=1)


class ActuatorSpec(_Strict):
    """How an ECU command becomes a physical actuation, and its limits."""

    commanded_by: str
    minimum: float
    maximum: float
    #: Engineering units per second; ``None`` when the response is not rate-limited
    #: in this contract.
    rate_limit_per_s: float | None = Field(default=None, gt=0)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _limits(self) -> ActuatorSpec:
        if not self.minimum < self.maximum:
            raise ValueError(f"actuator limits {self.minimum}..{self.maximum} are empty")
        return self


class Quantity(_Strict):
    id: str = Field(pattern=_ID)
    kind: Kind
    units: str
    meaning: str = Field(min_length=1)
    integer: IntegerRepresentation | None = None
    depends_on: list[Dependency] = Field(default_factory=list)
    #: Channels that observe this quantity itself: what it is scored against.
    #: None may feed a mode that computes it.
    observed_as: list[str] = Field(default_factory=list)
    #: Channels that read the same or a derived word but are not this quantity
    #: (PID 0x44 reads the command word scaled by the A/F feedback target): never
    #: scored against, and equally never fed to a mode that computes it.
    leak_aliases: list[str] = Field(default_factory=list)
    sensor: SensorSpec | None = None
    actuator: ActuatorSpec | None = None
    #: The application-model node that writes it, for an ECU quantity on a
    #: traced path (E07.2 joins ROM changes to modes through this).
    manifest_node: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Quantity:
        if (self.kind in INTEGER_KINDS) != (self.integer is not None):
            raise ValueError(
                f"{self.id}: ecu and sensor quantities are integers with a width; "
                "every other kind is continuous"
            )
        if (self.kind == "sensor") != (self.sensor is not None):
            raise ValueError(f"{self.id}: sensor spec is required for, and only for, a sensor")
        if (self.kind == "actuator") != (self.actuator is not None):
            raise ValueError(f"{self.id}: actuator spec is required for, and only for, an actuator")
        if self.manifest_node is not None and self.kind != "ecu":
            raise ValueError(f"{self.id}: only an ecu quantity names a manifest node")
        for channel in (*self.observed_as, *self.leak_aliases):
            if not _matches(_CHANNEL, channel):
                raise ValueError(f"{self.id}: {channel!r} is not a channel spec")
        if set(self.observed_as) & set(self.leak_aliases):
            raise ValueError(f"{self.id}: a channel is either scored or a leak-only alias")
        deps = [d.quantity for d in self.depends_on]
        if len(set(deps)) != len(deps):
            raise ValueError(f"{self.id}: a dependency is listed twice")
        if self.sensor is not None and self.sensor.measures not in deps:
            raise ValueError(f"{self.id}: a sensor depends on what it measures")
        if self.actuator is not None and self.actuator.commanded_by not in deps:
            raise ValueError(f"{self.id}: an actuator depends on its command")
        for dep in self.depends_on:
            if dep.quantity == self.id and dep.timing == "same_step":
                raise ValueError(f"{self.id}: depends on itself in the same step")
        return self

    @property
    def is_state(self) -> bool:
        """It carries its own previous value."""
        return any(d.quantity == self.id for d in self.depends_on)


class Event(_Strict):
    id: str = Field(pattern=_ID)
    meaning: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class HardwareParameter(_Strict):
    id: str = Field(pattern=_ID)
    units: str
    #: ``known``: a hardware fact with evidence; ``identifiable``: fittable from
    #: logs already available; ``requires_measurement``: neither yet.
    status: Literal["known", "identifiable", "requires_measurement"]
    evidence: str = Field(min_length=1)


class HardwareConfiguration(_Strict):
    id: str = Field(pattern=_ID)
    meaning: str = Field(min_length=1)
    parameters: list[HardwareParameter] = Field(min_length=1)


# -- plant subsystems (E07.5) ---------------------------------------------------------

#: What a subsystem result may be checked for at every step. Each is
#: implemented by the subsystem's module, which refuses a sample that breaks it.
Invariant = Literal["manifold_at_or_below_ambient"]
#: The shortest time scale a result resolves. ``engine_cycle``: a mean-value
#: model averages over a cycle, so structure finer than one cycle (120/rpm s)
#: is solver output, not a prediction.
TimeResolution = Literal["engine_cycle", "solver_step"]


class ApplicabilityBound(_Strict):
    """A boundary quantity's range inside which the subsystem's equations are
    claimed to hold. Outside it a result is refused by name, never clipped."""

    quantity: str
    minimum: float
    maximum: float
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _range(self) -> ApplicabilityBound:
        if not self.minimum < self.maximum:
            raise ValueError(f"bound on {self.quantity}: {self.minimum}..{self.maximum} is empty")
        return self


class Applicability(_Strict):
    hardware_configurations: list[str] = Field(min_length=1)
    bounds: list[ApplicabilityBound] = Field(min_length=1)
    invariants: list[Invariant] = Field(default_factory=list)
    time_resolution: TimeResolution
    evidence: str = Field(min_length=1)


class NumericalCheck(_Strict):
    """How a built subsystem shows its numbers are converged and conserving.

    ``refinement`` divides the contract's solver step (1 = the step itself);
    the observed order of convergence must be the solver method's theoretical
    order within ``order_tolerance``, the error at the solver step at most
    ``max_relative_error``, and every stored state must follow from the one
    before under the declared equations and method, re-stepped by independent
    code, within ``equation_residual_relative`` (rounding). The subsystem's
    module runs the study; nothing
    here states that it passed."""

    refinement: list[int] = Field(min_length=3)
    order_tolerance: float = Field(gt=0)
    max_relative_error: float = Field(gt=0, lt=1)
    equation_residual_relative: float = Field(gt=0, lt=1)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ladder(self) -> NumericalCheck:
        r = self.refinement
        if r[0] != 1 or any(b <= a for a, b in zip(r, r[1:], strict=False)):
            raise ValueError("refinement starts at 1 (the solver step) and strictly increases")
        return self


class RequiredMeasurement(_Strict):
    """A measurement a subsystem needs before a parameter is identifiable or a
    result can be validated, named before anything is claimed."""

    id: str = Field(pattern=_ID)
    what: str = Field(min_length=1)
    #: Parameters or quantities it would identify or validate.
    identifies: list[str] = Field(min_length=1)
    #: Controls it varies on purpose (``spark``, ``cam``, ``lambda``). A claim
    #: about changing a control needs an available measurement that excites
    #: it: natural closed-loop logs do not.
    excites: list[Literal["spark", "cam", "lambda", "throttle"]] = Field(default_factory=list)
    status: Literal["missing", "partial", "available"]
    evidence: str = Field(min_length=1)


ReferenceKind = Literal["instrument", "derived", "ecu_computed", "external"]


class ValidationReference(_Strict):
    """A channel a subsystem's output could be checked against.

    Its lineage -- what it is computed from -- is not stated alone: the
    simulator adds the contract's own dependency graph (an ECU word is computed
    from the sensors it reads) and the derived-channel registry (a ``derived:``
    channel from its inputs). ``derived_from`` adds what neither knows (an
    external instrument's inputs)."""

    id: str = Field(pattern=_ID)
    channel: str = Field(pattern=_CHANNEL)
    measures: str
    kind: ReferenceKind
    derived_from: list[str] = Field(default_factory=list)
    #: ``available``: logged and usable now; ``requires_measurement``: the
    #: instrument or capture does not exist yet (``requires`` names it);
    #: ``not_validated``: it exists but its own validation is owed.
    status: Literal["available", "requires_measurement", "not_validated"]
    requires: str | None = None
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _requires(self) -> ValidationReference:
        if (self.status == "available") == (self.requires is not None):
            raise ValueError(
                f"reference {self.id}: an unavailable reference names the measurement it "
                "requires (and an available one names none)"
            )
        for item in self.derived_from:
            if not (_matches(_CHANNEL, item) or _matches(_ID, item)):
                raise ValueError(f"reference {self.id}: {item!r} is neither a channel nor an id")
        return self


class SharedInput(_Strict):
    """An input a reference may share with the subsystem, and why sharing it
    does not make the comparison circular (rpm as the operating-point index)."""

    quantity: str
    justification: str = Field(min_length=1)


class ValidationRecord(_Strict):
    """One scoring of a subsystem against a reference, as the scoring wrote it."""

    reference: str
    recordings: list[str] = Field(min_length=1)
    verdict: Literal["pass", "fail"]
    scored_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    evidence: str = Field(min_length=1)


class InternalModel(_Strict):
    """A quantity another stage owns that this stage's implementation computes
    for itself instead of reading from the owner. Declared, never silent: the
    two can disagree, so where they differ and who reconciles them is stated.
    The implementation module must list the same quantities in its
    ``COMPUTES_INTERNALLY`` (``engine_dynamics.check_internal_models``)."""

    quantity: str
    #: The subsystem that owns ``quantity`` in the contract.
    owner: str
    modes: list[str] = Field(min_length=1)
    model: str = Field(min_length=1)
    #: Where this model and the owner's give different answers.
    differs: str = Field(min_length=1)
    #: What reconciles them (a plant_version, a task item).
    owed_by: str = Field(min_length=1)


class SubsystemImplementation(_Strict):
    module: str = Field(pattern=r"^src\.analysis\.[a-z_]+$")
    profile_file: str = Field(pattern=r"^[a-z_]+\.yaml$")
    plant_version: str = Field(min_length=1)


class Subsystem(_Strict):
    """One stage of the plant: what it computes, where its equations hold, how
    its numerics are checked, and what independent measurement could validate
    it. Every plant and actuator quantity belongs to exactly one subsystem, so a
    quantity cannot be computed by an undeclared stage."""

    id: str = Field(pattern=_ID)
    stage: int = Field(ge=0)
    meaning: str = Field(min_length=1)
    computes: list[str] = Field(min_length=1)
    #: Named parts a single quantity hides (spark efficiency, friction).
    components: list[str] = Field(default_factory=list)
    status: Literal["unbuilt", "built"]
    implementation: SubsystemImplementation | None = None
    applicability: Applicability | None = None
    numerical: NumericalCheck | None = None
    references: list[ValidationReference] = Field(default_factory=list)
    shared_inputs: list[SharedInput] = Field(default_factory=list)
    required_measurements: list[RequiredMeasurement] = Field(default_factory=list)
    validations: list[ValidationRecord] = Field(default_factory=list)
    internal_models: list[InternalModel] = Field(default_factory=list)

    @model_validator(mode="after")
    def _declared(self) -> Subsystem:
        built = self.status == "built"
        for name, value in (
            ("implementation", self.implementation),
            ("applicability", self.applicability),
        ):
            if built != (value is not None):
                raise ValueError(
                    f"subsystem {self.id}: {name} is required for, and only for, a built "
                    "subsystem (bounded applicability comes with code)"
                )
        if not built and self.numerical is not None:
            raise ValueError(f"subsystem {self.id}: an unbuilt subsystem has no numerics")
        if not built and self.validations:
            raise ValueError(f"subsystem {self.id}: an unbuilt subsystem has nothing to score")
        _unique(f"{self.id} reference", [r.id for r in self.references])
        _unique(f"{self.id} measurement", [m.id for m in self.required_measurements])
        measurements = {m.id for m in self.required_measurements}
        for ref in self.references:
            if ref.requires is not None and ref.requires not in measurements:
                raise ValueError(
                    f"subsystem {self.id}: reference {ref.id} requires {ref.requires!r}, "
                    "which is not one of its required_measurements"
                )
        refs = {r.id for r in self.references}
        for record in self.validations:
            if record.reference not in refs:
                raise ValueError(
                    f"subsystem {self.id}: a validation scored against undeclared "
                    f"{record.reference!r}"
                )
        if not any(r.status == "available" for r in self.references) and not measurements:
            raise ValueError(
                f"subsystem {self.id}: no available reference and no required measurement; name "
                "what would validate it before anything is claimed"
            )
        return self


# -- modes ----------------------------------------------------------------------------


class StateInit(_Strict):
    method: InitMethod
    #: Events that reset the state (each declared under ``events``).
    reset_on: list[str] = Field(default_factory=list)
    note: str = Field(min_length=1)


class Assignment(_Strict):
    role: Role
    #: Where a supplied value comes from: a channel spec, or ``scenario``.
    source: str | None = None
    #: Why a held-fixed value of a non-environment quantity is acceptable.
    assumption: str | None = None
    init: StateInit | None = None

    @model_validator(mode="after")
    def _fields(self) -> Assignment:
        supplied = self.role in SUPPLIED
        if supplied != (self.source is not None):
            raise ValueError(
                f"source is required for, and only for, a supplied role (got {self.role})"
            )
        if self.role == "measured_input" and not _matches(_CHANNEL, self.source or ""):
            raise ValueError(f"a measured input is read from a channel, not {self.source!r}")
        if self.source is not None and self.source != "scenario":
            if not _matches(_CHANNEL, self.source):
                raise ValueError(f"source {self.source!r} is neither a channel nor scenario")
        if self.role == "unused" and (self.init or self.assumption):
            raise ValueError("an unused quantity carries no init or assumption")
        if self.role == "inferred_state" and self.init is None:
            raise ValueError("an inferred state needs its initialisation")
        return self


class Assimilation(_Strict):
    """A state-estimation mode's use of an observation of a computed quantity."""

    quantity: str
    channel: str = Field(pattern=_CHANNEL)
    corrects: str


class Mode(_Strict):
    id: str = Field(pattern=_ID)
    kind: ModeKind
    claim: str = Field(min_length=1)
    score_group: ScoreGroup = "open_loop"
    assimilates: list[Assimilation] = Field(default_factory=list)
    roles: dict[str, Assignment]

    @model_validator(mode="after")
    def _group(self) -> Mode:
        if (self.score_group == "state_estimation") != bool(self.assimilates):
            raise ValueError(
                f"mode {self.id}: only a state_estimation mode assimilates observations, "
                "and it must name each one"
            )
        return self

    def role(self, quantity: str) -> str:
        return self.roles[quantity].role

    def computed(self) -> list[str]:
        return [q for q, a in self.roles.items() if a.role in COMPUTED]


# -- the contract ---------------------------------------------------------------------


class SimContract(_Strict):
    schema_version: Literal[1]
    contract_version: int = Field(ge=1)
    name: Literal["engine_sim_contract"]
    plant_version: str = Field(min_length=1)
    units: list[Unit] = Field(min_length=1)
    clocks: list[Clock] = Field(min_length=1)
    ecu_schedule: EcuSchedule
    solver: Solver
    events: list[Event] = Field(default_factory=list)
    hardware_configurations: list[HardwareConfiguration] = Field(min_length=1)
    quantities: list[Quantity] = Field(min_length=1)
    modes: list[Mode] = Field(min_length=1)
    #: E07.5: the plant's stages. When declared, every plant and actuator
    #: quantity belongs to exactly one.
    subsystems: list[Subsystem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> SimContract:
        _unique("unit", [u.id for u in self.units])
        _unique("clock", [c.id for c in self.clocks])
        _unique("event", [e.id for e in self.events])
        _unique("quantity", [q.id for q in self.quantities])
        _unique("mode", [m.id for m in self.modes])
        _unique("hardware configuration", [h.id for h in self.hardware_configurations])
        clocks = {c.id: c for c in self.clocks}
        _check_clocks(self, clocks)
        units = {u.id for u in self.units}
        by_id = {q.id: q for q in self.quantities}
        for q in self.quantities:
            _check_quantity(q, by_id, units, clocks)
        for h in self.hardware_configurations:
            _unique(f"{h.id} parameter", [p.id for p in h.parameters])
            stray = [p.units for p in h.parameters if p.units not in units]
            if stray:
                raise ValueError(f"hardware {h.id}: undeclared units {stray}")
        events = {e.id for e in self.events}
        open_loop = [m.kind for m in self.modes if m.score_group == "open_loop"]
        if sorted(open_loop) != sorted(FOUR_MODES):
            raise ValueError(
                f"open-loop modes must be exactly one of each of {list(FOUR_MODES)}, "
                f"got {open_loop}"
            )
        for mode in self.modes:
            _check_mode(mode, by_id, events)
        _check_subsystems(self, by_id)
        return self

    def subsystem(self, subsystem_id: str) -> Subsystem:
        found = next((s for s in self.subsystems if s.id == subsystem_id), None)
        if found is None:
            raise SimulationRefusalError(
                "subsystem_undeclared", f"the contract declares no subsystem {subsystem_id!r}"
            )
        return found

    def subsystem_of(self, quantity_id: str) -> Subsystem | None:
        """The subsystem that computes ``quantity_id`` (``None`` for an ECU,
        sensor, environment or driver quantity)."""
        return next((s for s in self.subsystems if quantity_id in s.computes), None)

    def quantity(self, quantity_id: str) -> Quantity:
        found = next((q for q in self.quantities if q.id == quantity_id), None)
        if found is None:
            raise KeyError(quantity_id)
        return found

    def mode(self, mode_id: str) -> Mode:
        found = next((m for m in self.modes if m.id == mode_id), None)
        if found is None:
            raise SimulationRefusalError("mode_undeclared", f"the contract declares no {mode_id!r}")
        return found

    def hardware(self, config_id: str) -> HardwareConfiguration:
        found = next((h for h in self.hardware_configurations if h.id == config_id), None)
        if found is None:
            raise SimulationRefusalError(
                "hardware_undeclared",
                f"the contract declares no hardware configuration {config_id!r}",
            )
        return found

    def by_manifest_node(self) -> dict[str, Quantity]:
        return {q.manifest_node: q for q in self.quantities if q.manifest_node is not None}


def _matches(pattern: str, text: str) -> bool:
    return re.fullmatch(pattern.strip("^$"), text) is not None


def _unique(what: str, ids: list[str]) -> None:
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise ValueError(f"duplicate {what} ids: {twice}")


def _check_clocks(contract: SimContract, clocks: dict[str, Clock]) -> None:
    schedule = contract.ecu_schedule
    ecu = clocks.get(schedule.clock)
    if ecu is None or ecu.kind != "ecu_time_task":
        raise ValueError("ecu_schedule.clock must be a declared ecu_time_task clock")
    if schedule.event_clock is not None:
        event = clocks.get(schedule.event_clock)
        if event is None or event.kind != "ecu_crank_event":
            raise ValueError("ecu_schedule.event_clock must be a declared ecu_crank_event clock")
    solver = clocks.get(contract.solver.clock)
    if solver is None or solver.kind != "solver":
        raise ValueError("solver.clock must be a declared solver clock")
    assert ecu.period_s is not None and solver.period_s is not None
    ratio = ecu.period_s / solver.period_s
    if ratio < 1 or not math.isclose(ratio, round(ratio), rel_tol=0, abs_tol=1e-9):
        raise ValueError(
            f"the solver step ({solver.period_s} s) must divide the ECU task period "
            f"({ecu.period_s} s) a whole number of times: values cross only at ECU ticks"
        )


def _check_quantity(
    q: Quantity, by_id: dict[str, Quantity], units: set[str], clocks: dict[str, Clock]
) -> None:
    if q.units not in units:
        raise ValueError(f"{q.id}: units {q.units!r} are not declared")
    for dep in q.depends_on:
        target = by_id.get(dep.quantity)
        if target is None:
            raise ValueError(f"{q.id} depends on undeclared quantity {dep.quantity!r}")
        if dep.quantity != q.id and target.kind not in ALLOWED_READS[q.kind]:
            raise ValueError(
                f"{q.id} ({q.kind}) reads {target.id} ({target.kind}): a {q.kind} quantity "
                f"reads only {sorted(ALLOWED_READS[q.kind])}; cross the boundary through a "
                "declared sensor or actuator"
            )
    if q.sensor is not None:
        if q.sensor.sample_clock not in clocks:
            raise ValueError(f"{q.id}: sample clock {q.sensor.sample_clock!r} is not declared")
        if by_id[q.sensor.measures].kind in INTEGER_KINDS:
            raise ValueError(f"{q.id}: a sensor measures a physical quantity")
    if q.actuator is not None and by_id[q.actuator.commanded_by].kind != "ecu":
        raise ValueError(f"{q.id}: an actuator is commanded by an ecu quantity")


def _check_mode(mode: Mode, by_id: dict[str, Quantity], events: set[str]) -> None:
    missing = sorted(set(by_id) - set(mode.roles))
    extra = sorted(set(mode.roles) - set(by_id))
    if missing or extra:
        raise ValueError(
            f"mode {mode.id}: every quantity has exactly one role (missing {missing}, "
            f"undeclared {extra}); use unused rather than leaving one out"
        )
    computed = set(mode.computed())
    if not any(mode.role(q) == "predicted_output" for q in by_id):
        raise ValueError(f"mode {mode.id} predicts nothing")
    for q_id in computed:
        q = by_id[q_id]
        for dep in q.depends_on:
            if mode.role(dep.quantity) == "unused":
                raise ValueError(
                    f"mode {mode.id}: {q_id} reads {dep.quantity}, which has no role here "
                    "(an undeclared input)"
                )
    # A value read from the previous step must exist at the first step: every
    # computed quantity read that way (its own state included) is initialised.
    read_late = {
        d.quantity
        for q_id in computed
        for d in by_id[q_id].depends_on
        if d.timing == "previous_step" and d.quantity in computed
    }
    for q_id in sorted(read_late):
        if mode.roles[q_id].init is None:
            raise ValueError(
                f"mode {mode.id}: {q_id} is read from the previous step, so it needs an init "
                "(missing history never defaults to cold boot or steady state)"
            )
    for q_id, assignment in mode.roles.items():
        if assignment.init is not None:
            unknown = [e for e in assignment.init.reset_on if e not in events]
            if unknown:
                raise ValueError(f"mode {mode.id}: {q_id} resets on undeclared events {unknown}")
    _check_cycles(mode, by_id, computed)
    _check_leaks(mode, by_id, computed)
    _check_mode_kind(mode, by_id, computed)


def _check_subsystems(contract: SimContract, by_id: dict[str, Quantity]) -> None:
    subsystems = contract.subsystems
    if not subsystems:
        return
    _unique("subsystem", [s.id for s in subsystems])
    hardware = {h.id for h in contract.hardware_configurations}
    owner: dict[str, str] = {}
    for sub in subsystems:
        for q_id in sub.computes:
            q = by_id.get(q_id)
            if q is None or q.kind not in ("plant", "actuator"):
                raise ValueError(
                    f"subsystem {sub.id}: computes {q_id!r}, which is not a declared plant or "
                    "actuator quantity (ECU arithmetic is the twin's, not a plant stage's)"
                )
            if q_id in owner:
                raise ValueError(f"{q_id} is computed by both {owner[q_id]} and {sub.id}")
            owner[q_id] = sub.id
        stateful = any(by_id[q].is_state for q in sub.computes if q in by_id)
        if sub.status == "built" and stateful != (sub.numerical is not None):
            raise ValueError(
                f"subsystem {sub.id}: a built subsystem that integrates a state declares its "
                "step-size refinement and conservation checks (numerical), and only such a one"
            )
        named = [r.measures for r in sub.references] + [s.quantity for s in sub.shared_inputs]
        if sub.applicability is not None:
            named += [b.quantity for b in sub.applicability.bounds]
            stray_hw = sorted(set(sub.applicability.hardware_configurations) - hardware)
            if stray_hw:
                raise ValueError(f"subsystem {sub.id}: undeclared hardware {stray_hw}")
        stray = sorted({n for n in named if n not in by_id})
        if stray:
            raise ValueError(f"subsystem {sub.id}: names undeclared quantities {stray}")
    orphans = sorted(
        q.id for q in contract.quantities if q.kind in ("plant", "actuator") and q.id not in owner
    )
    if orphans:
        raise ValueError(
            f"plant/actuator quantities {orphans} belong to no subsystem: a quantity cannot be "
            "computed by an undeclared stage"
        )
    for sub in subsystems:
        modes = {m.id for m in contract.modes}
        for im in sub.internal_models:
            owner_id = next((s.id for s in subsystems if im.quantity in s.computes), None)
            if owner_id is None or owner_id == sub.id or owner_id != im.owner:
                raise ValueError(
                    f"subsystem {sub.id}: internal model of {im.quantity!r} names owner "
                    f"{im.owner!r}, but the contract's owner is {owner_id!r} (an internal model "
                    "is of another stage's quantity)"
                )
            stray_modes = sorted(set(im.modes) - modes)
            if stray_modes:
                raise ValueError(f"subsystem {sub.id}: internal model in undeclared {stray_modes}")


def _check_cycles(mode: Mode, by_id: dict[str, Quantity], computed: set[str]) -> None:
    edges = {
        q: [
            d.quantity
            for d in by_id[q].depends_on
            if d.timing == "same_step" and d.quantity in computed
        ]
        for q in computed
    }
    state: dict[str, int] = {}
    trail: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        trail.append(node)
        for nxt in edges[node]:
            if state.get(nxt) == 1:
                loop = trail[trail.index(nxt) :] + [nxt]
                raise ValueError(
                    f"mode {mode.id}: same-step cycle {' -> '.join(loop)}; break it with an "
                    "explicit previous_step state or supply one of them"
                )
            if nxt not in state:
                visit(nxt)
        trail.pop()
        state[node] = 2

    for node in sorted(computed):
        if node not in state:
            visit(node)


def leak_channels(by_id: dict[str, Quantity], computed: set[str]) -> dict[str, str]:
    """Every channel that carries (part of) a computed quantity, mapped to it:
    its scored channels and its leak-only aliases, and those of every sensor
    that measures it (the ECU's reading of a predicted lambda is still that
    lambda, whether or not the mode computes the reading)."""
    out: dict[str, str] = {}
    for q_id in sorted(computed):
        q = by_id[q_id]
        for channel in (*q.observed_as, *q.leak_aliases):
            out.setdefault(channel, q_id)
    for q in by_id.values():
        if q.sensor is not None and q.sensor.measures in computed:
            for channel in (*q.observed_as, *q.leak_aliases):
                out.setdefault(channel, q.sensor.measures)
    return out


def _check_leaks(mode: Mode, by_id: dict[str, Quantity], computed: set[str]) -> None:
    observes = leak_channels(by_id, computed)
    for q_id, assignment in mode.roles.items():
        source = assignment.source
        if assignment.role in SUPPLIED and source in observes:
            raise ValueError(
                f"mode {mode.id}: {q_id} is supplied from {source}, which observes "
                f"{observes[source]}, a quantity this mode computes (predicted-as-measured)"
            )
    for item in mode.assimilates:
        if item.quantity not in computed or item.channel not in by_id[item.quantity].observed_as:
            raise ValueError(
                f"mode {mode.id}: assimilates {item.channel} for {item.quantity}, which this "
                "mode does not compute or that channel does not observe"
            )
        if mode.roles.get(item.corrects) is None or mode.role(item.corrects) != "inferred_state":
            raise ValueError(f"mode {mode.id}: assimilation corrects {item.corrects}, not a state")


def _check_mode_kind(mode: Mode, by_id: dict[str, Quantity], computed: set[str]) -> None:
    kinds = {q: by_id[q].kind for q in by_id}
    if mode.kind == "ecu_replay":
        wrong = sorted(q for q in computed if kinds[q] != "ecu")
        if wrong:
            raise ValueError(
                f"mode {mode.id}: ECU replay computes only ECU quantities, not {wrong}"
            )
    if mode.kind == "plant_replay":
        wrong = sorted(q for q in computed if kinds[q] == "ecu")
        if wrong:
            raise ValueError(f"mode {mode.id}: plant replay computes no ECU quantity, not {wrong}")
    if mode.kind in SCENARIO_ONLY_MODES:
        measured = sorted(q for q, a in mode.roles.items() if a.role == "measured_input")
        if measured:
            raise ValueError(
                f"mode {mode.id}: a {mode.kind} takes no measured input (a recording was made "
                f"under one calibration), not {measured}"
            )
        recorded = sorted(
            q for q, a in mode.roles.items() if a.role in SUPPLIED and a.source != "scenario"
        )
        if recorded:
            raise ValueError(
                f"mode {mode.id}: a {mode.kind} supplies values from the scenario only; "
                f"{recorded} read a recorded channel (a relabelled measured input)"
            )
    for q_id, assignment in mode.roles.items():
        held = assignment.role == "prescribed_boundary" and kinds[q_id] not in FREE_BOUNDARY_KINDS
        if held != (assignment.assumption is not None):
            raise ValueError(
                f"mode {mode.id}: {q_id}: a prescribed {kinds[q_id]} quantity states the "
                "assumption it is held fixed under, and only such a quantity carries one"
            )


def load_contract(profile_dir: Path) -> tuple[SimContract, str]:
    """The profile's contract and the sha256 of its file."""
    text = (profile_dir / CONTRACT_FILE).read_bytes()
    return SimContract.model_validate(yaml.safe_load(text)), hashlib.sha256(text).hexdigest()


def check_against_application_model(contract: SimContract, model: ApplicationModel) -> list[str]:
    """Where the contract and the E05 manifest disagree; empty when they agree.

    The ECU schedule must be the manifest's task and cycle start; every ECU
    quantity's RAM word, where the manifest declares it, has the manifest's
    width and signedness; every ``manifest_node`` exists and writes that word."""
    problems: list[str] = []
    schedule = contract.ecu_schedule
    if schedule.application_model_task != model.schedule.function:
        problems.append(
            f"ecu_schedule task 0x{schedule.application_model_task:06X} is not the manifest's "
            f"0x{model.schedule.function:06X}"
        )
    if schedule.cycle_start != model.schedule.cycle_start:
        problems.append(
            f"cycle_start 0x{schedule.cycle_start:06X} is not the manifest's "
            f"0x{model.schedule.cycle_start:06X}"
        )
    ram = {v.address: v for v in model.ram}
    nodes = {n.id: n for n in model.nodes}
    for q in contract.quantities:
        rep = q.integer
        if rep is not None and rep.ram is not None and rep.ram in ram:
            declared = ram[rep.ram]
            if (declared.width, declared.signed) != (rep.width, rep.signed):
                problems.append(
                    f"{q.id}: 0x{rep.ram:08X} is {rep.width}-bit signed={rep.signed}, the manifest "
                    f"says {declared.width}-bit signed={declared.signed}"
                )
        if q.manifest_node is not None:
            node = nodes.get(q.manifest_node)
            if node is None:
                problems.append(f"{q.id}: manifest node {q.manifest_node!r} is not declared")
            elif rep is None or rep.ram is None or rep.ram not in node.writes:
                problems.append(f"{q.id}: {q.manifest_node} does not write its RAM word")
    return problems


# -- scenarios and provenance ---------------------------------------------------------


class EcuIdentity(_Strict):
    """The exact controller a run uses."""

    program_id: str = Field(min_length=1)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The active map slot; ``None`` when it could not be resolved.
    slot: int | None = Field(default=None, ge=1)
    slot_source: Literal["observed_checkpoint", "default_slot", "registry", "unresolved"]

    @model_validator(mode="after")
    def _slot(self) -> EcuIdentity:
        if (self.slot is None) != (self.slot_source == "unresolved"):
            raise ValueError("slot is None exactly when slot_source is unresolved")
        return self


class PlantIdentity(_Strict):
    version: str = Field(min_length=1)
    hardware_configuration: str = Field(min_length=1)
    #: Parameter id -> evidence id (or ``unmeasured``) for this run's values.
    parameter_evidence: dict[str, str] = Field(default_factory=dict)


class SuppliedValue(_Strict):
    """A boundary or initial value: a number in the quantity's units, or a
    channel it is read from, with where it came from."""

    value: float | None = None
    channel: str | None = Field(default=None, pattern=_CHANNEL)
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def _one(self) -> SuppliedValue:
        if (self.value is None) == (self.channel is None):
            raise ValueError("give exactly one of value and channel")
        return self


class Scenario(_Strict):
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: str
    #: One controller, or two (baseline then candidate) for a ROM comparison.
    ecu: list[EcuIdentity] = Field(default_factory=list, max_length=2)
    plant: PlantIdentity | None = None
    boundaries: dict[str, SuppliedValue] = Field(default_factory=dict)
    initial_state: dict[str, SuppliedValue] = Field(default_factory=dict)

    def canonical(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def scenario_hash(self) -> str:
        """sha256 of the canonical scenario: computed, never supplied."""
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    def provenance(self) -> dict[str, Any]:
        return {
            "scenario_sha256": self.scenario_hash(),
            "contract_sha256": self.contract_sha256,
            "mode": self.mode,
            "ecu": [e.model_dump() for e in self.ecu],
            "plant": None if self.plant is None else self.plant.model_dump(),
        }


def check_scenario(contract: SimContract, contract_sha256: str, scenario: Scenario) -> Mode:
    """The scenario's mode, or a refusal naming what it cannot evaluate."""
    if scenario.contract_sha256 != contract_sha256:
        raise SimulationRefusalError(
            "contract_mismatch", "the scenario was written against another contract file"
        )
    mode = contract.mode(scenario.mode)
    kinds = {q.id: q.kind for q in contract.quantities}
    computed = set(mode.computed())
    needs_ecu = mode.kind != "plant_replay"
    needs_plant = any(kinds[q] in ("plant", "actuator") for q in computed)
    wanted = 2 if mode.kind == "conditional_rom_comparison" else (1 if needs_ecu else 0)
    if len(scenario.ecu) != wanted:
        raise SimulationRefusalError(
            "ecu_identity_count",
            f"{mode.kind} needs {wanted} ECU identities, got {len(scenario.ecu)}",
        )
    for ecu in scenario.ecu:
        if ecu.slot is None:
            raise SimulationRefusalError(
                "active_slot_unresolved",
                f"image {ecu.image_sha256[:12]}...: the active map slot is not known, so the "
                "tables the ECU reads are not known",
            )
    if len({e.program_id for e in scenario.ecu}) > 1:
        raise SimulationRefusalError(
            "program_mismatch", "a ROM comparison runs one program's decode on both images"
        )
    if needs_plant:
        if scenario.plant is None:
            raise SimulationRefusalError("plant_identity_missing", f"{mode.id} computes the plant")
        if scenario.plant.version != contract.plant_version:
            raise SimulationRefusalError(
                "plant_version_mismatch",
                f"scenario plant {scenario.plant.version}, contract {contract.plant_version}",
            )
        hardware = contract.hardware(scenario.plant.hardware_configuration)
        declared = {p.id for p in hardware.parameters}
        stray = sorted(set(scenario.plant.parameter_evidence) - declared)
        if stray:
            raise SimulationRefusalError(
                "parameter_undeclared", f"{hardware.id} declares no parameters {stray}"
            )
    supplied = {q for q, a in mode.roles.items() if a.role in SUPPLIED}
    stray = sorted(set(scenario.boundaries) - supplied)
    if stray:
        raise SimulationRefusalError(
            "boundary_not_supplied_here", f"{mode.id} computes or ignores {stray}"
        )
    scenario_fed = sorted(
        q for q, a in mode.roles.items() if a.source == "scenario" and q not in scenario.boundaries
    )
    if scenario_fed:
        raise SimulationRefusalError(
            "boundary_missing", f"{mode.id} needs scenario values for {scenario_fed}"
        )
    if mode.kind in SCENARIO_ONLY_MODES:
        recorded = sorted(
            q
            for q, v in (*scenario.boundaries.items(), *scenario.initial_state.items())
            if v.channel is not None
        )
        if recorded:
            raise SimulationRefusalError(
                "recorded_value_refused",
                f"a {mode.kind} takes stated values only (a recording was made under one "
                f"calibration); {recorded} name a channel",
            )
    leaks = leak_channels({q.id: q for q in contract.quantities}, computed)
    for q_id, value in scenario.boundaries.items():
        source = mode.roles[q_id].source
        if value.channel is not None and value.channel in leaks:
            raise SimulationRefusalError(
                "predicted_as_measured",
                f"{q_id} is read from {value.channel}, which carries {leaks[value.channel]}, "
                f"a quantity {mode.id} computes",
            )
        if source != "scenario" and value.channel != source:
            raise SimulationRefusalError(
                "boundary_source_mismatch", f"{q_id} is read from {source} in {mode.id}"
            )
    checkpointed = sorted(
        q
        for q, a in mode.roles.items()
        if a.init is not None and a.init.method in ("observed_checkpoint", "prescribed")
    )
    missing = [q for q in checkpointed if q not in scenario.initial_state]
    if missing:
        raise SimulationRefusalError(
            "initial_state_missing",
            f"{mode.id} starts {missing} from a checkpoint or stated value; none was given "
            "(missing history never defaults to cold boot or steady state)",
        )
    stray = sorted(set(scenario.initial_state) - set(checkpointed))
    if stray:
        raise SimulationRefusalError(
            "initial_state_not_declared", f"{mode.id} does not initialise {stray} from a value"
        )
    return mode


def score_group(contract: SimContract, mode_id: str) -> ScoreGroup:
    """Which score group a result of ``mode_id`` belongs to: open-loop and
    state-estimation scores are never pooled."""
    return contract.mode(mode_id).score_group


__all__ = [
    "CONTRACT_FILE",
    "FOUR_MODES",
    "Assignment",
    "EcuIdentity",
    "Mode",
    "PlantIdentity",
    "Quantity",
    "Scenario",
    "SimContract",
    "SCENARIO_ONLY_MODES",
    "SimulationRefusalError",
    "SuppliedValue",
    "Subsystem",
    "ValidationReference",
    "check_against_application_model",
    "check_scenario",
    "leak_channels",
    "load_contract",
    "score_group",
]
