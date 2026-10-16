"""The ECM application model and coverage manifest (E05.1).

For each ECM output the twin claims, the profile's
``ecu_application_model.yaml`` traces the firmware path backwards: the writer
of the output, the functions, RAM state, flags and ROM constants it reads, the
order the fuel-command task runs them in, and for each piece whether the twin
*implements* it, *observes* it, *assumes* it or does not model it. This module
is the schema, the loader, the program/patch/calibration identity checks, and
the read model the dashboard reports from.

Two rules are enforced by the schema rather than stated:

- **Every unresolved dependency blocks.** A node on an output's path that the
  twin neither implements nor observes (``assumed``, ``not_modelled``) must be
  named by one of that output's ``blockers``, and a blocker may name nothing
  else. A blocker cannot outlive its cause, and an unresolved node cannot be
  dropped silently. Consumers (``src.analysis.target_phi``) take the blockers
  from here instead of restating them.
- **Compatibility is the program plus the full image, never the calibration
  ID.** The calibration ID sits in the reflashed calibration block, so five
  calibrations share it. :func:`check_image` hashes the program spans, checks
  the patch markers and every node's ``calibration_conditions`` on the bytes,
  and refuses by name.
- **A traced override is never a verified one** (E05.3). Each warm-up,
  closed-loop, transient, fuel-cut or start override records how it was read
  (``listing`` / ``decompile_text`` / ``inferred``) and how far it was checked
  (``traced`` / ``fixture`` / ``vehicle``). Above ``traced`` needs the named
  evidence, and an override on an output that has not qualified on the
  vehicle must be held out by one of that output's regions.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

MODEL_FILE = "ecu_application_model.yaml"

Confidence = Literal["observed", "code", "inferred", "unknown"]
TwinRole = Literal["implemented", "observed", "assumed", "not_modelled", "calibration_dead"]
#: ``synthetic``: vectors derived from the code; ``fixture``: a committed real
#: capture where its inputs exist; ``vehicle``: a held-out drive.
ReferenceValidation = Literal["synthetic", "fixture", "vehicle"]

#: Twin roles that leave a node unresolved on an output's path.
UNRESOLVED: frozenset[str] = frozenset({"assumed", "not_modelled"})

_SPEC = r"^[a-z]+:[A-Za-z0-9_]+$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApplicationModelRefusalError(ValueError):
    """An image or artifact the model does not apply to; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message

    def __reduce__(self) -> tuple[object, ...]:
        # Rebuilt from its constructor's arguments, so it survives a process
        # boundary (pickle); tests/test_exceptions_pickle.py holds every class to it.
        return (type(self), (self.reason, self.message), self.__dict__)


# -- program identity -----------------------------------------------------------


class ProgramHash(_Strict):
    #: ``[start, end)`` spans hashed in order.
    regions: list[tuple[int, int]] = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> ProgramHash:
        previous = -1
        for start, end in self.regions:
            if not previous <= start < end:
                raise ValueError(
                    f"program regions must be ordered, non-empty spans: {self.regions}"
                )
            previous = end
        return self


class PatchMarker(_Strict):
    offset: int = Field(ge=0)
    bytes: str = Field(pattern=r"^([0-9a-f]{2})+$")
    meaning: str = Field(min_length=1)


class Patch(_Strict):
    name: str = Field(min_length=1)
    markers: list[PatchMarker] = Field(min_length=1)
    evidence: str = Field(min_length=1)


class KnownImage(_Strict):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(min_length=1)
    #: ``decoded``: the listing was read from this image. ``program_checked``:
    #: its program spans hash to the identity (its calibration conditions are
    #: still checked whenever its bytes are present).
    role: Literal["decoded", "program_checked"]


class Program(_Strict):
    id: str = Field(min_length=1)
    calibration_id: str = Field(min_length=1)
    identity: ProgramHash
    patch: Patch
    images: list[KnownImage] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_decoded(self) -> Program:
        if not any(i.role == "decoded" for i in self.images):
            raise ValueError("program.images needs the image the listing was decoded from")
        return self

    def image(self, sha256: str) -> KnownImage | None:
        return next((i for i in self.images if i.sha256 == sha256), None)


# -- RAM, nodes, outputs ----------------------------------------------------------


class RamVar(_Strict):
    address: int = Field(ge=0xFFFF0000, le=0xFFFFFFFF)
    width: Literal[8, 16, 32]
    signed: bool
    count: int = Field(default=1, ge=1)
    name: str = Field(min_length=1)
    units: str = Field(min_length=1)
    confidence: Confidence
    meaning: str = Field(min_length=1)


class Observation(_Strict):
    spec: str = Field(pattern=_SPEC)
    address: int
    relation: Literal["identity", "alias", "high_byte", "bit_0"]
    confidence: Confidence = "observed"
    note: str | None = None


class RomRef(_Strict):
    """A ROM table by its ``rom_map.yaml`` id, or a constant by offset."""

    rom_map: str | None = None
    offset: int | None = Field(default=None, ge=0)
    width: Literal[8, 16] | None = None
    value: int | None = None
    role: str = Field(min_length=1)

    @model_validator(mode="after")
    def _one_kind(self) -> RomRef:
        if (self.rom_map is None) == (self.offset is None):
            raise ValueError(f"rom ref {self.role!r}: give exactly one of rom_map, offset")
        if self.offset is not None and (self.width is None or self.value is None):
            raise ValueError(f"rom constant at 0x{self.offset:X} needs width and value")
        return self


class CalibrationCondition(_Strict):
    """Bytes that must hold for the node's modelled behaviour (masked compare;
    ``count`` consecutive bytes)."""

    offset: int = Field(ge=0)
    mask: int = Field(ge=1, le=0xFF)
    equals: int = Field(ge=0, le=0xFF)
    count: int = Field(default=1, ge=1)
    why: str = Field(min_length=1)

    @model_validator(mode="after")
    def _within_mask(self) -> CalibrationCondition:
        if self.equals & ~self.mask:
            raise ValueError(f"condition at 0x{self.offset:X}: equals has bits outside mask")
        return self

    def failures(self, image: bytes) -> list[int]:
        return [
            self.offset + k
            for k in range(self.count)
            if self.offset + k >= len(image) or image[self.offset + k] & self.mask != self.equals
        ]


class Branch(_Strict):
    when: str = Field(min_length=1)
    then: str = Field(min_length=1)


class Node(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    function: int = Field(ge=0)
    span: tuple[int, int] | None = None
    kind: Literal["computation", "state", "copy"]
    writes: list[int]
    reads: list[int]
    depends_on: list[str] = Field(default_factory=list)
    arithmetic: str = Field(min_length=1)
    rounding: str = Field(min_length=1)
    saturation: str = Field(min_length=1)
    branches: list[Branch] = Field(default_factory=list)
    rom: list[RomRef] = Field(default_factory=list)
    calibration_conditions: list[CalibrationCondition] = Field(default_factory=list)
    outside_schedule: str | None = None
    confidence: Confidence
    twin: TwinRole
    implemented_by: str | None = None
    observed_as: list[Observation] = Field(default_factory=list)
    assumption: str | None = None
    #: An executable reference of this node's decode (E05.3) that no output
    #: consumes yet: the node keeps the twin role its outputs use until the
    #: reference qualifies, so adding one never changes a prediction.
    reference_model: str | None = None
    #: What the reference has been checked against so far.
    reference_validation: list[ReferenceValidation] = Field(default_factory=list)
    evidence: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _role_fields(self) -> Node:
        if (self.reference_model is None) != (not self.reference_validation):
            raise ValueError(f"{self.id}: reference_model and reference_validation go together")
        if self.reference_model is not None and self.twin == "implemented":
            raise ValueError(
                f"{self.id}: an implemented node names implemented_by, not a reference_model"
            )
        if (self.twin == "implemented") != (self.implemented_by is not None):
            raise ValueError(
                f"{self.id}: implemented_by is required for, and only for, twin: implemented"
            )
        if self.twin == "implemented" and self.confidence not in ("observed", "code"):
            raise ValueError(f"{self.id}: an {self.confidence} decode cannot be implemented")
        if self.twin == "observed" and not any(o.relation != "alias" for o in self.observed_as):
            raise ValueError(f"{self.id}: twin: observed needs an observed_as channel")
        if (self.twin == "assumed") != (self.assumption is not None):
            raise ValueError(f"{self.id}: assumption is required for, and only for, twin: assumed")
        if self.twin == "calibration_dead" and not self.calibration_conditions:
            raise ValueError(
                f"{self.id}: calibration_dead needs the calibration_conditions that kill it"
            )
        return self

    @property
    def unresolved(self) -> bool:
        return self.twin in UNRESOLVED


class ScheduleCall(_Strict):
    function: int = Field(ge=0)
    nodes: list[str]


class Schedule(_Strict):
    function: int = Field(ge=0)
    cycle_start: int = Field(ge=0)
    calls: list[ScheduleCall] = Field(min_length=1)

    def order(self) -> list[str]:
        return [n for call in self.calls for n in call.nodes]


class OutputTwin(_Strict):
    definition: str = Field(min_length=1)
    predicts: str = Field(min_length=1)


class OutputCondition(_Strict):
    id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    settled_by: str = Field(min_length=1)


class Blocker(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    nodes: list[str] = Field(min_length=1)
    closes_with: str = Field(min_length=1)


class Region(_Strict):
    id: str = Field(min_length=1)
    status: Literal["conditional", "unevaluable"]
    when: str = Field(min_length=1)
    nodes: list[str] = Field(min_length=1)
    #: Command overrides (E05.3) whose samples this region holds out: every
    #: override on this output that has not qualified on the vehicle must be
    #: named by one of the output's regions.
    overrides: list[str] = Field(default_factory=list)


class Output(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    ram: int
    written_by: str
    units: str = Field(min_length=1)
    #: ``intermediate``: a value on another output's path (E05.4: the trim
    #: lookup is not ignition timing). It names the output it ``feeds``, and
    #: may lack an observation channel, which keeps it from qualifying.
    role: Literal["final", "intermediate"] = "final"
    feeds: str | None = None
    observed_as: list[Observation] = Field(default_factory=list)
    twin: OutputTwin | None = None
    equals_output_when: list[OutputCondition] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)
    regions: list[Region] = Field(default_factory=list)

    @model_validator(mode="after")
    def _role(self) -> Output:
        if (self.role == "intermediate") != (self.feeds is not None):
            raise ValueError(
                f"output {self.id}: feeds is required for, and only for, an intermediate"
            )
        if self.role == "final" and not self.observed_as:
            raise ValueError(f"output {self.id}: a final output needs an observation channel")
        return self

    @property
    def status(self) -> str:
        """``qualified`` only with no blockers and a channel to compare with."""
        if self.blockers:
            return "conditional"
        return "qualified" if self.observed_as else "unobserved"


class Uncovered(_Strict):
    id: str = Field(min_length=1)
    where: str = Field(min_length=1)
    blocks: str = Field(min_length=1)
    next_evidence: str = Field(min_length=1)


#: The operating regimes whose command overrides E05.3 identifies one by one.
OverrideRegime = Literal["start", "warm_up", "closed_loop", "transient", "fuel_cut"]
#: How the override was read: ``listing`` (the instructions read through),
#: ``decompile_text`` (the decompiler's output, not line-checked against the
#: listing), ``inferred`` (from use or naming, not read through).
TraceSource = Literal["listing", "decompile_text", "inferred"]
#: ``traced``: read from the code only, never checked. ``fixture``: condition
#: and effect both read in a committed real capture and the effect followed.
#: ``vehicle``: the same on a held-out drive.
OverrideQualification = Literal["traced", "fixture", "vehicle"]

_ADDRESS = re.compile(r"0xFFFF[0-9A-Fa-f]{4}\b")


class Override(_Strict):
    """One identified condition under which the firmware replaces or reshapes
    a command (E05.3): where it is, what it reads and writes, how the trace
    was made, and how far it has been checked. A traced override is
    representable as exactly that; it qualifies only with named evidence."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    regime: OverrideRegime
    #: Model outputs whose value it overrides. Each must hold its samples out
    #: through a region until the override qualifies on the vehicle.
    acts_on: list[str] = Field(default_factory=list)
    #: Declared RAM it writes that no output models (the injector pulse terms).
    acts_on_ram: list[int] = Field(default_factory=list)
    #: The traced nodes it lives in; every one is in ``routine``.
    nodes: list[str] = Field(default_factory=list)
    routine: int | None = Field(default=None, ge=0)
    span: tuple[int, int] | None = None
    condition: str = Field(min_length=1)
    effect: str = Field(min_length=1)
    reads: list[int] = Field(default_factory=list)
    writes: list[int] = Field(default_factory=list)
    rom: list[RomRef] = Field(default_factory=list)
    established_by: TraceSource
    #: Of the condition-to-effect reading itself; ``observed`` only once it
    #: has qualified.
    confidence: Confidence
    evidence: list[str] = Field(min_length=1)
    qualification: OverrideQualification
    #: Tests or sessions where condition and effect were both read.
    qualified_by: list[str] = Field(default_factory=list)
    #: Observations that agree but do not qualify it (the condition was not
    #: read with the effect, or the regime was not exercised).
    consistent_with: list[str] = Field(default_factory=list)
    #: What the trace did not reach.
    gaps: list[str] = Field(default_factory=list)
    verify_with: str = Field(min_length=1)

    @model_validator(mode="after")
    def _honest(self) -> Override:
        if (self.qualification == "traced") != (not self.qualified_by):
            raise ValueError(
                f"override {self.id}: qualified_by is required for, and only for, a "
                f"qualification above traced (got {self.qualification})"
            )
        if self.qualification != "traced" and self.established_by == "inferred":
            raise ValueError(
                f"override {self.id}: an inferred trace cannot qualify; read it through first"
            )
        if (self.confidence == "observed") != (self.qualification != "traced"):
            raise ValueError(
                f"override {self.id}: confidence observed goes with, and only with, a "
                "qualified override"
            )
        if self.established_by == "inferred" and self.confidence == "code":
            raise ValueError(f"override {self.id}: an inferred trace is not confidence code")
        if self.routine is None and not self.gaps:
            raise ValueError(f"override {self.id}: an unrecorded routine is a gap; name it")
        if self.nodes and self.routine is None:
            raise ValueError(f"override {self.id}: nodes need the routine they share")
        if self.span is not None and (self.routine is None or self.span[0] > self.span[1]):
            raise ValueError(f"override {self.id}: span needs a routine and start <= end")
        if not (self.acts_on or self.acts_on_ram or self.gaps):
            raise ValueError(f"override {self.id}: name what it acts on, or the gap that hides it")
        return self

    def cited(self) -> set[int]:
        """RAM addresses its condition and effect text cite."""
        return {int(m, 16) for m in _ADDRESS.findall(f"{self.condition} {self.effect}")}


class ApplicationModel(_Strict):
    schema_version: Literal[1]
    model_version: int = Field(ge=1)
    name: Literal["ecm_application_model"]
    program: Program
    ram: list[RamVar] = Field(min_length=1)
    schedule: Schedule
    nodes: list[Node] = Field(min_length=1)
    outputs: list[Output] = Field(min_length=1)
    uncovered: list[Uncovered] = Field(default_factory=list)
    #: E05.3: warm-up, closed-loop, transient, fuel-cut and start overrides of
    #: the commands, each with its trace and qualification.
    overrides: list[Override] = Field(default_factory=list)

    @model_validator(mode="after")
    def _graph(self) -> ApplicationModel:
        ids = [n.id for n in self.nodes]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate node ids: {duplicates}")
        by_id = {n.id: n for n in self.nodes}
        declared = [v.address for v in self.ram]
        twice = sorted({a for a in declared if declared.count(a) > 1})
        if twice:
            raise ValueError(
                f"RAM declared more than once: {[f'0x{a:08X}' for a in twice]}; one entry "
                "per address, or width and signedness can disagree"
            )
        addresses = set(declared)
        observed = [(n.id, o) for n in self.nodes for o in n.observed_as]
        observed += [(out.id, o) for out in self.outputs for o in out.observed_as]
        stray = [f"{owner}: {o.spec} at 0x{o.address:08X}" for owner, o in observed]
        stray = [s for (_, o), s in zip(observed, stray, strict=True) if o.address not in addresses]
        if stray:
            raise ValueError(f"observations read RAM not declared under `ram`: {stray}")
        for node in self.nodes:
            unknown = [d for d in node.depends_on if d not in by_id]
            if unknown:
                raise ValueError(f"{node.id} depends on undeclared nodes {unknown}")
            undeclared = [a for a in (*node.reads, *node.writes) if a not in addresses]
            if undeclared:
                raise ValueError(
                    f"{node.id} touches RAM not declared under `ram` (width/signedness "
                    f"unknown): {[f'0x{a:08X}' for a in undeclared]}"
                )
        order = self.schedule.order()
        for node in self.nodes:
            listed = order.count(node.id)
            if listed > 1 or (listed == 1) == (node.outside_schedule is not None):
                raise ValueError(
                    f"{node.id}: a node is either called once by the schedule or names "
                    "outside_schedule, not both or neither"
                )
        unknown_scheduled = [n for n in order if n not in by_id]
        if unknown_scheduled:
            raise ValueError(f"schedule names undeclared nodes {unknown_scheduled}")
        for output in self.outputs:
            _check_output(output, by_id, addresses)
        _check_intermediates(self.outputs, by_id)
        _check_overrides(self.overrides, self.outputs, by_id, addresses)
        return self

    def overrides_on(self, output_id: str) -> list[Override]:
        """The overrides acting on one output, in declared order."""
        return [o for o in self.overrides if output_id in o.acts_on]

    def node(self, node_id: str) -> Node:
        return next(n for n in self.nodes if n.id == node_id)

    def output(self, output_id: str) -> Output:
        found = next((o for o in self.outputs if o.id == output_id), None)
        if found is None:
            raise ApplicationModelRefusalError(
                "output_undeclared", f"the application model declares no output {output_id!r}"
            )
        return found

    def path(self, start: str) -> list[Node]:
        """Nodes ``start`` depends on, transitively, in declaration order.
        An observed node ends its branch: nothing upstream of a measurement is
        needed to use it."""
        seen: set[str] = set()
        stack = [start]
        while stack:
            node = self.node(stack.pop())
            if node.id in seen:
                continue
            seen.add(node.id)
            if node.twin != "observed":
                stack.extend(node.depends_on)
        return [n for n in self.nodes if n.id in seen]


def _check_output(output: Output, by_id: dict[str, Node], addresses: set[int]) -> None:
    if output.ram not in addresses:
        raise ValueError(f"output {output.id}: RAM 0x{output.ram:08X} is not declared")
    if output.written_by not in by_id:
        raise ValueError(f"output {output.id}: written_by {output.written_by!r} is not a node")
    if output.ram not in by_id[output.written_by].writes:
        raise ValueError(
            f"output {output.id}: {output.written_by} does not write 0x{output.ram:08X}"
        )
    reachable = _reachable(output.written_by, by_id)
    if output.twin is not None and output.twin.predicts not in reachable:
        raise ValueError(
            f"output {output.id}: predicts {output.twin.predicts!r} is not on its path"
        )
    unresolved = {n for n in reachable if by_id[n].unresolved}
    named: dict[str, str] = {}
    for blocker in output.blockers:
        for node_id in blocker.nodes:
            if node_id in named:
                raise ValueError(f"output {output.id}: {node_id} is under two blockers")
            named[node_id] = blocker.name
            if node_id not in unresolved:
                raise ValueError(
                    f"output {output.id}: blocker {blocker.name} names {node_id}, which is "
                    "not an unresolved node on this output's path; remove it"
                )
    silent = sorted(unresolved - named.keys())
    if silent:
        raise ValueError(
            f"output {output.id}: unresolved nodes {silent} block nothing; every undecoded "
            "dependency must make the output conditional"
        )
    names = [b.name for b in output.blockers]
    if len(set(names)) != len(names):
        raise ValueError(f"output {output.id}: duplicate blocker names")
    for region in output.regions:
        unknown = [n for n in region.nodes if n not in reachable]
        if unknown:
            raise ValueError(f"output {output.id} region {region.id}: {unknown} not on its path")


def _check_intermediates(outputs: list[Output], by_id: dict[str, Node]) -> None:
    """An intermediate is a value on the path of the final output it feeds,
    at a different address: it can never be presented as that output."""
    finals = {o.id: o for o in outputs if o.role == "final"}
    for output in outputs:
        if output.feeds is None:
            continue
        fed = finals.get(output.feeds)
        if fed is None:
            raise ValueError(
                f"output {output.id}: feeds {output.feeds!r}, which is not a final output"
            )
        if output.ram == fed.ram:
            raise ValueError(f"output {output.id}: an intermediate cannot be {fed.id}'s word")
        if output.written_by not in _reachable(fed.written_by, by_id):
            raise ValueError(f"output {output.id}: {output.written_by} is not on {fed.id}'s path")


def _check_overrides(
    overrides: list[Override],
    outputs: list[Output],
    by_id: dict[str, Node],
    addresses: set[int],
) -> None:
    """Each override sits where its nodes are, touches only declared RAM,
    cites only what it reads or writes, and acts on an output only from that
    output's path. An override that has not qualified on the vehicle is held
    out by a region of every output it acts on, so a traced override can never
    leave its samples counted as if the twin covered them."""
    ids = [o.id for o in overrides]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise ValueError(f"duplicate override ids: {twice}")
    by_output = {o.id: o for o in outputs}
    for override in overrides:
        name = f"override {override.id}"
        for node_id in override.nodes:
            node = by_id.get(node_id)
            if node is None:
                raise ValueError(f"{name}: node {node_id!r} is not declared")
            if node.function != override.routine:
                raise ValueError(
                    f"{name}: node {node_id} is in 0x{node.function:06X}, not routine "
                    f"0x{override.routine or 0:06X}"
                )
        touched = [*override.reads, *override.writes, *override.acts_on_ram]
        undeclared = [a for a in touched if a not in addresses]
        if undeclared:
            raise ValueError(
                f"{name} touches RAM not declared under `ram`: {[f'0x{a:08X}' for a in undeclared]}"
            )
        loose = override.cited() - set(touched)
        if loose:
            raise ValueError(
                f"{name}: its condition or effect cites RAM it neither reads nor writes: "
                f"{sorted(f'0x{a:08X}' for a in loose)}"
            )
        for output_id in override.acts_on:
            output = by_output.get(output_id)
            if output is None:
                raise ValueError(f"{name}: acts on undeclared output {output_id!r}")
            reachable = _reachable(output.written_by, by_id)
            off_path = [n for n in override.nodes if n not in reachable]
            if not override.nodes or off_path:
                raise ValueError(
                    f"{name}: acts on {output_id} but its nodes {off_path or '[]'} are not "
                    "on that output's path"
                )
            held_out = {i for r in output.regions for i in r.overrides}
            if override.qualification != "vehicle" and override.id not in held_out:
                raise ValueError(
                    f"{name} is {override.qualification}, not vehicle-qualified: a region of "
                    f"{output_id} must hold its samples out"
                )
    for output in outputs:
        for region in output.regions:
            stray = [i for i in region.overrides if output.id not in _acts_on(overrides, i)]
            if stray:
                raise ValueError(
                    f"output {output.id} region {region.id}: {stray} are not overrides "
                    "acting on this output"
                )


def _acts_on(overrides: list[Override], override_id: str) -> list[str]:
    found = next((o for o in overrides if o.id == override_id), None)
    return [] if found is None else found.acts_on


def _reachable(start: str, by_id: dict[str, Node]) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        if by_id[current].twin != "observed":
            stack.extend(by_id[current].depends_on)
    return seen


def load_application_model(profile_dir: Path) -> tuple[ApplicationModel, str]:
    """The profile's application model and the sha256 of its file.
    ``FileNotFoundError`` when the vehicle declares none."""
    text = (profile_dir / MODEL_FILE).read_bytes()
    return ApplicationModel.model_validate(yaml.safe_load(text)), hashlib.sha256(text).hexdigest()


# -- identity checks ----------------------------------------------------------------


def program_sha256(image: bytes, model: ApplicationModel) -> str:
    """sha256 over the program spans, in order."""
    digest = hashlib.sha256()
    for start, end in model.program.identity.regions:
        digest.update(image[start:end])
    return digest.hexdigest()


def check_image(image: bytes, model: ApplicationModel) -> None:
    """Refuse an image this model does not describe, by name:
    ``wrong_program`` (the program spans hash differently), ``patch_missing``
    (a patch marker's bytes differ) or ``unmodelled_logic`` (a node's
    calibration condition fails, so logic the model treats as unreachable or
    fixed is live in this calibration)."""
    identity = model.program.identity
    actual = program_sha256(image, model)
    if actual != identity.sha256:
        raise ApplicationModelRefusalError(
            "wrong_program",
            f"program spans hash to {actual}, not {identity.sha256} ({model.program.id}); "
            "a different program needs its own decode",
        )
    for marker in model.program.patch.markers:
        expected = bytes.fromhex(marker.bytes)
        found = image[marker.offset : marker.offset + len(expected)]
        if found != expected:
            raise ApplicationModelRefusalError(
                "patch_missing",
                f"{model.program.patch.name}: 0x{marker.offset:06X} holds {found.hex()}, "
                f"not {marker.bytes}",
            )
    for node in model.nodes:
        for condition in node.calibration_conditions:
            failed = condition.failures(image)
            if failed:
                raise ApplicationModelRefusalError(
                    "unmodelled_logic",
                    f"{node.id}: calibration bytes {[f'0x{o:X}' for o in failed]} fail "
                    f"(mask 0x{condition.mask:02X} = 0x{condition.equals:02X}): {condition.why}",
                )


def check_image_sha(image_sha256: str, model: ApplicationModel) -> None:
    """Without the image's bytes: refuse ``program_unverified`` unless the
    model lists this exact image as checked against its program."""
    if model.program.image(image_sha256) is None:
        raise ApplicationModelRefusalError(
            "program_unverified",
            f"image {image_sha256} is not one the application model's program "
            f"({model.program.id}) was checked on; supply the image bytes",
        )


# -- what a consumer applies --------------------------------------------------------


@dataclass(frozen=True)
class Applicability:
    """One output's standing under the model, as a consumer applies it."""

    output: str
    model_version: int
    model_sha256: str
    program_id: str
    #: Blocker names in declared order; empty means the path is resolved.
    blockers: tuple[str, ...]
    #: The unresolved nodes those blockers name, in declared order: the logic
    #: between the reproduced computation and the output that the twin omits.
    blocked_nodes: tuple[str, ...] = ()
    #: The model's ``uncovered`` entries (logic the trace reached and did not
    #: resolve, model-wide), by id.
    uncovered: tuple[str, ...] = ()

    def provenance(self) -> dict[str, Any]:
        return {
            "file": MODEL_FILE,
            "sha256": self.model_sha256,
            "model_version": self.model_version,
            "program": self.program_id,
            "output": self.output,
        }


def applicability(model: ApplicationModel, model_sha256: str, output_id: str) -> Applicability:
    output = model.output(output_id)
    return Applicability(
        output=output.id,
        model_version=model.model_version,
        model_sha256=model_sha256,
        program_id=model.program.id,
        blockers=tuple(b.name for b in output.blockers),
        blocked_nodes=tuple(dict.fromkeys(n for b in output.blockers for n in b.nodes)),
        uncovered=tuple(u.id for u in model.uncovered),
    )


def observed_specs(model: ApplicationModel, start: str) -> list[str]:
    """Channels of the observed nodes on ``start``'s path (excluding aliases),
    in declaration order: the measured inputs a twin of ``start`` needs."""
    return [
        o.spec
        for node in model.path(start)
        if node.twin == "observed"
        for o in node.observed_as
        if o.relation != "alias"
    ]


def check_observations(model: ApplicationModel, memory: Iterable[tuple[str, int]]) -> list[str]:
    """Each ``mem:`` observation whose catalog address differs from the RAM
    the model says it reads (``memory`` is ``(name, address)`` from the memory
    catalog). Empty when every one agrees."""
    catalog = {f"mem:{name}": address for name, address in memory}
    wrong = []
    observations = [o for n in model.nodes for o in n.observed_as]
    observations += [o for out in model.outputs for o in out.observed_as]
    for o in observations:
        if o.spec.startswith("mem:") and catalog.get(o.spec) != o.address:
            found = catalog.get(o.spec)
            wrong.append(
                f"{o.spec}: catalog {'absent' if found is None else f'0x{found:08X}'}, "
                f"model 0x{o.address:08X}"
            )
    return wrong


# -- the read model -------------------------------------------------------------------


def _hex(value: int, digits: int = 6) -> str:
    return f"0x{value:0{digits}X}"


def _node_summary(node: Node) -> dict[str, Any]:
    return {
        "id": node.id,
        "function": _hex(node.function),
        "kind": node.kind,
        "twin": node.twin,
        "confidence": node.confidence,
        "implemented_by": node.implemented_by,
        "observed_as": [o.spec for o in node.observed_as],
        "assumption": node.assumption,
        "reference_model": node.reference_model,
        "reference_validation": list(node.reference_validation),
    }


def _override_summary(override: Override) -> dict[str, Any]:
    return {
        "id": override.id,
        "regime": override.regime,
        "acts_on": list(override.acts_on),
        "acts_on_ram": [_hex(a, 8) for a in override.acts_on_ram],
        "nodes": list(override.nodes),
        "routine": None if override.routine is None else _hex(override.routine),
        "span": None if override.span is None else [_hex(a) for a in override.span],
        "condition": override.condition,
        "effect": override.effect,
        "established_by": override.established_by,
        "confidence": override.confidence,
        "qualification": override.qualification,
        "qualified_by": list(override.qualified_by),
        "evidence": list(override.evidence),
        "consistent_with": list(override.consistent_with),
        "gaps": list(override.gaps),
        "verify_with": override.verify_with,
    }


def coverage_report(
    model: ApplicationModel, model_sha256: str, *, image_sha256: str | None = None
) -> dict[str, Any]:
    """What the dashboard reports: each output's status (``qualified`` only
    with no blockers), what the twin implements, observes and assumes on its
    path, the blockers with their nodes, the conditions and regions, the
    logic the trace reached without resolving, and the command overrides
    (E05.3) with each one's qualification.

    ``image_sha256`` (the running image, when known) adds whether the model's
    program was checked on it; the bytes themselves are checked by
    :func:`check_image` where they are read."""
    outputs = []
    for output in model.outputs:
        path = model.path(output.written_by)
        by_role: dict[str, list[str]] = {}
        for node in path:
            by_role.setdefault(node.twin, []).append(node.id)
        outputs.append(
            {
                "id": output.id,
                "ram": _hex(output.ram, 8),
                "units": output.units,
                "role": output.role,
                "feeds": output.feeds,
                "status": output.status,
                "observed_as": [o.model_dump(exclude_none=True) for o in output.observed_as],
                "twin": None if output.twin is None else output.twin.model_dump(),
                "path": [_node_summary(n) for n in path],
                "by_role": by_role,
                "blockers": [b.model_dump() for b in output.blockers],
                "equals_output_when": [c.model_dump() for c in output.equals_output_when],
                "regions": [r.model_dump() for r in output.regions],
                "overrides": [o.id for o in model.overrides_on(output.id)],
            }
        )
    image = None
    if image_sha256 is not None:
        known = model.program.image(image_sha256)
        image = {
            "sha256": image_sha256,
            "known": known is not None,
            "revision": None if known is None else known.revision,
            "role": None if known is None else known.role,
        }
    return {
        "model": {
            "name": model.name,
            "schema_version": model.schema_version,
            "model_version": model.model_version,
            "sha256": model_sha256,
        },
        "program": {
            "id": model.program.id,
            "calibration_id": model.program.calibration_id,
            "identity_sha256": model.program.identity.sha256,
            "regions": [[_hex(s), _hex(e)] for s, e in model.program.identity.regions],
            "patch": model.program.patch.name,
            "images": [i.model_dump() for i in model.program.images],
        },
        "image": image,
        "schedule": {
            "function": _hex(model.schedule.function),
            "cycle_start": _hex(model.schedule.cycle_start),
            "order": model.schedule.order(),
        },
        "outputs": outputs,
        "uncovered": [u.model_dump() for u in model.uncovered],
        "overrides": [_override_summary(o) for o in model.overrides],
    }


__all__ = [
    "MODEL_FILE",
    "Applicability",
    "ApplicationModel",
    "ApplicationModelRefusalError",
    "Override",
    "applicability",
    "check_image",
    "check_image_sha",
    "check_observations",
    "coverage_report",
    "load_application_model",
    "observed_specs",
    "program_sha256",
]
