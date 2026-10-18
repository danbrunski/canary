"""Which lever an edit pulls, and what stops a number (E09.14a).

One function, :func:`classify_edit`, reads a baseline/candidate comparison
(:func:`src.analysis.rom_effects.compare_roms`'s report), the ROM map, the
application model and the simulation contract, and answers two questions for
an edit: *which lever is it* (Fuel Comp, Fuel Targets, K, a timing trim, the
Load Point Scaler, a fill curve; or bytes no map names, or the checksum), and
*per output, what blocks a number*. Both front ends show the same strings: the
``tools/rom_sim.py inspect`` command prints ``LeverClassification.messages``
and the ROM Editor's ``preview_edits`` (E09.14b) returns them for the page. A
caller that wrote its own wording would drift from the other.

Everything a blocker says is read from a source that changes when the work
lands, never from a table kept here:

- ``not_mapped``: the report classed changed bytes ``unknown`` (outside
  ``rom_map.yaml``) -- the MAF curve ``0xE27E`` until it is mapped (E09.8).
- ``unmodelled`` / ``observed_not_predicted`` / ``held_fixed`` /
  ``outside_decode``: the report's own per-output reasons, which come from the
  application model's node ``twin`` standing and its calibration decodes. For
  the two ECU outputs a run computes (BFS and the Target AFR word) they pass
  through :func:`paired_scenarios.twin_gate` (which applies
  :func:`paired_steady.ecu_output_gate`) and the run's ``ReasonScope``, the rule
  the run itself uses: a reason the steady fixed point covers
  (the contract's absorbed nodes) is not a blocker, one it does not cover (an
  edit to a filter node itself) stays. The report must have been made under the
  run's contract mode. Ignition outputs, which no run computes, keep every
  reason. The plant's outputs inherit what still blocks the ECU outputs.
- the torque claim's refusals, for an output whose contract quantity commands
  an actuator a claim-computing stage reads (ignition: ``ignition_timing_byte``
  carries the timing writer and commands ``spark_advance``, which
  ``torque_production`` reads; an intermediate is joined through the output it
  ``feeds``): :func:`engine_dynamics.claim_refusals` for that claim from the
  report's mode, with the actuator's control changed. Each is a blocker:
  ``no_torque_plant`` while the torque stage is ``unbuilt``; once built, its
  derived standing, ``subsystem_not_validated`` naming the measurements still
  owed (by task item: H07.10, H07.5, H07.12, H07.8, N01.2); ``<control>_
  response_unmeasured`` while no available measurement excites the control;
  ``engine_torque_not_computed`` where the mode does not compute torque. An
  output no contract quantity carries is ``no_plant_consumer``.
- the edit's own claim (E07.5 item 8): :func:`changed_controls` derives what
  the edit changes from the comparison (the contract quantities on its reached
  path, closed downstream through ECU quantities to the actuators they command:
  a timing edit is ``spark``, a Fuel Targets / Fuel Comp / K edit ``lambda``,
  and Fuel Comp and K also ``spark`` because BFS indexes the timing tables),
  and :func:`edit_claim_refusals` / :func:`require_edit_claim` refuse a torque
  or power claim for the edit by name. ``messages`` ends with that refusal.
- ``plant_not_qualified``: :func:`fuel_plant.plant_qualification`'s reason,
  passed in by the caller.
- ``closed_loop_trim_authority_undecoded``: :func:`paired_steady.
  closed_loop_trim_pending`, the function the run calls too: it holds while any
  of the three E05.3 closed-loop overrides is not ``vehicle``-qualified (they
  are read together, so E09.10 lifts it only by qualifying all three).
  :data:`paired_steady.TRIM_AUTHORITY_UNDECODED` is the code. Only this blocker
  is regime-aware: it applies when the stated regime is closed loop (or none is
  stated, since most of a drive is). Other regimes' unqualified overrides and the
  plant's per-regime qualification (``regime_not_qualified``) are not consulted.

The only presentation data here is :data:`LEVER_LABELS` (short names for the
rom map's table ``kind`` ids); a kind without a label shows its table's own
``name``, and a test pins that no label names a kind the map lacks. The
levers themselves are the map's ``kind`` (an axis table follows the table that
names it), so mapping a new table (E09.7-E09.9) makes it a lever with no change
here.

Imports nothing that transmits: see ``tests/test_rom_levers.py``'s
clean-process test.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from src.analysis.ecu_application import ApplicationModel, Output, OverrideRegime
from src.analysis.engine_dynamics import (
    CLAIM_QUANTITY,
    SubsystemStanding,
    claim_chain,
    claim_refusals,
    subsystem_standing,
)
from src.analysis.fuel_plant import Qualification
from src.analysis.paired_scenarios import OUTPUTS as RUNNER_OUTPUTS
from src.analysis.paired_scenarios import ReasonScope, twin_gate
from src.analysis.paired_steady import (
    ECU_RUNNER_OUTPUTS,
    FILL_BYTE,
    TRIM_AUTHORITY_UNDECODED,
    closed_loop_trim_pending,
    conditionable_quantities,
    steady_coverage,
)
from src.analysis.rom_effects import ChangedRange, EffectReport
from src.analysis.rom_map import RomMap, Table
from src.analysis.sim_contract import Quantity, SimContract, SimulationRefusalError, Subsystem

TOOL = "rom_levers"
#: Bump when a code, a text or the result's shape changes meaning.
TOOL_VERSION = "2"

#: Short names for the map's table ``kind`` ids. Presentation only.
LEVER_LABELS: dict[str, str] = {
    "fuel_compensation": "Fuel Comp",
    "fuel_targets": "Fuel Targets (Target AFR)",
    "k_constant": "K",
    "timing_trim_a": "Timing trim A",
    "timing_trim_b": "Timing trim B",
    "load_point_scaler": "Load Point Scaler",
}
#: Lever ids that are not table kinds.
UNMAPPED = "unmapped"
CHECKSUM = "checksum"
CONSTANT = "constant"

#: The paired runner's outputs that are not the ECU's two (``paired_steady.
#: ECU_RUNNER_OUTPUTS``, the one place those names live): the pulse, the trim
#: and the plant's.
SIMULATED_OUTPUTS: tuple[str, ...] = tuple(o for o in RUNNER_OUTPUTS if o not in ECU_RUNNER_OUTPUTS)

#: Blocker codes, in the order a message lists them.
NOT_MAPPED = "not_mapped"
MAPPED_UNMODELLED = "mapped_unmodelled"
NO_TORQUE_PLANT = "no_torque_plant"
NO_PLANT_CONSUMER = "no_plant_consumer"
PLANT_NOT_QUALIFIED = "plant_not_qualified"
UNMODELLED = "unmodelled"
OBSERVED_NOT_PREDICTED = "observed_not_predicted"
HELD_FIXED = "held_fixed"
OUTSIDE_DECODE = "outside_decode"
INACTIVE_SLOT = "inactive_slot"

TEXT_NOT_MAPPED = "not mapped"
TEXT_NO_TORQUE_PLANT = "no torque plant"
#: The claim an edit's torque/power line is about (power rests on the same
#: quantity, so its refusals are the same).
EDIT_CLAIM = "power"
TEXT_CLOSED_LOOP = "closed loop: trim authority undecoded"

_SLOT = re.compile(r"^(?P<what>.*?)(?: \(slot (?P<slot>\d+)\))?$")
Regime = OverrideRegime


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Blocker(_Strict):
    """One reason a number is withheld. ``text`` is what both front ends show."""

    code: str
    text: str
    node: str | None = None
    detail: str | None = None


class LeverEntry(_Strict):
    """One lever the edit touches, with the bytes and tables behind it."""

    lever: str
    label: str
    tables: tuple[str, ...] = ()
    slots: tuple[int, ...] = ()
    byte_class: str
    start: int
    end: int
    changed_bytes: int
    #: Cells the edit resolved to (the spec route); ``None`` for an image.
    changed_cells: int | None = None
    nodes: tuple[str, ...] = ()
    blockers: tuple[Blocker, ...] = ()


class OutputBlockers(_Strict):
    """One output: whether the edit reaches it and what blocks a number."""

    output: str
    source: Literal["application_model", "simulated"]
    #: The report's status for a model output; ``reached``, ``unknown`` or
    #: ``unchanged`` for a simulated one.
    status: str
    levers: tuple[str, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    #: Only on an output a number can be given for: the quantities every point of
    #: the run must state (the run gives ``conditioning_missing`` without them),
    #: and the other quantities the mode lets a scenario state that this edit
    #: reaches, which the run refuses as ``conditioned_on_changed_path``. So the
    #: number holds for points that state exactly ``assumes_conditioned``.
    assumes_conditioned: tuple[str, ...] = ()
    refused_if_conditioned: tuple[str, ...] = ()

    @property
    def design_dependent(self) -> bool:
        return bool(self.assumes_conditioned)

    @property
    def reached(self) -> bool:
        return self.status != "unchanged"

    @property
    def number(self) -> bool:
        """A number can be given: reached, and nothing blocks it."""
        return self.reached and not self.blockers


class LeverClassification(_Strict):
    schema_version: Literal[1] = 1
    tool: str = TOOL
    tool_version: str = TOOL_VERSION
    #: The comparison's outcome (``identical``, ``evaluable``, ``partial``,
    #: ``unevaluable``) and, for an image it could not use, the refusal.
    outcome: str
    reason: str | None = None
    regime: str | None = None
    levers: tuple[LeverEntry, ...] = ()
    outputs: tuple[OutputBlockers, ...] = ()
    #: The text both front ends show, one line per fact, in a stable order.
    messages: tuple[str, ...] = Field(default=())

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON types; the same shape for the CLI and the page."""
        plain: dict[str, Any] = self.model_dump(mode="json")
        for out, entry in zip(plain["outputs"], self.outputs, strict=True):
            out["reached"] = entry.reached
            out["number"] = entry.number
            out["design_dependent"] = entry.design_dependent
        return plain


# -- levers ---------------------------------------------------------------------------------


def _lever_of(table: Table, rom_map: RomMap) -> str:
    """The table's kind; an axis follows the table that names it."""
    if table.kind:
        return table.kind
    for other in rom_map.tables:
        if table.id in (other.axes.rows, other.axes.cols):
            return other.kind or other.id
    return table.id


def _label(lever: str, rom_map: RomMap) -> str:
    if lever in LEVER_LABELS:
        return LEVER_LABELS[lever]
    for table in rom_map.tables:
        if table.id == lever:
            return table.name
    return lever


def _region_name(rom_map: RomMap, start: int) -> str | None:
    holding = [r for r in rom_map.regions if r.start <= start <= r.end]
    return min(holding, key=lambda r: r.end - r.start).name if holding else None


def _split(what: str) -> tuple[str, int | None]:
    match = _SLOT.match(what)
    assert match is not None
    slot = match.group("slot")
    return match.group("what"), None if slot is None else int(slot)


def _hex_span(start: int, end: int) -> str:
    return f"0x{start:X}" if start == end else f"0x{start:X}-0x{end:X}"


def _cell_counts(cells: Sequence[Mapping[str, Any]] | None) -> dict[str, int] | None:
    if cells is None:
        return None
    counts: dict[str, int] = {}
    for cell in cells:
        counts[str(cell["table"])] = counts.get(str(cell["table"]), 0) + 1
    return counts


def _levers(
    ranges: Sequence[ChangedRange], rom_map: RomMap, cells: Sequence[Mapping[str, Any]] | None
) -> list[LeverEntry]:
    """Changed ranges, grouped by lever, in address order of the first byte."""
    by_table = {t.id: t for t in rom_map.tables}
    cell_counts = _cell_counts(cells)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for rng in ranges:
        pieces: list[tuple[str, str, str | None, int | None]] = []  # lever, label, table, slot
        if rng.byte_class == "unknown":
            region = _region_name(rom_map, rng.start)
            where = f" in {region}" if region else ""
            pieces.append((UNMAPPED, f"unmapped bytes{where}", None, None))
        elif rng.byte_class == "checksum":
            pieces.append((CHECKSUM, "checksum words", None, None))
        else:
            for what in rng.what:
                name, slot = _split(what)
                table = by_table.get(name)
                if table is None:
                    node = ",".join(rng.nodes) or "no node"
                    pieces.append((CONSTANT, f"{name} read by {node}", None, slot))
                else:
                    lever = _lever_of(table, rom_map)
                    pieces.append((lever, _label(lever, rom_map), table.id, slot))
        for lever, label, table_id, slot in pieces:
            g = groups.setdefault(
                (lever, label),
                {
                    "classes": set(),
                    "tables": [],
                    "slots": set(),
                    "start": rng.start,
                    "end": rng.end,
                    "bytes": 0,
                    "nodes": set(),
                    "ranges": [],
                },
            )
            g["classes"].add(rng.byte_class)
            if table_id is not None and table_id not in g["tables"]:
                g["tables"].append(table_id)
            if slot is not None:
                g["slots"].add(slot)
            g["start"], g["end"] = min(g["start"], rng.start), max(g["end"], rng.end)
            g["bytes"] += rng.changed
            g["nodes"].update(rng.nodes)
            g["ranges"].append(rng)
    entries: list[LeverEntry] = []
    for (lever, label), g in sorted(groups.items(), key=lambda kv: kv[1]["start"]):
        classes = sorted(g["classes"])
        cells_changed = (
            None
            if cell_counts is None or lever in (UNMAPPED, CHECKSUM, CONSTANT)
            else sum(cell_counts.get(t, 0) for t in g["tables"])
        )
        entries.append(
            LeverEntry(
                lever=lever,
                label=label,
                tables=tuple(g["tables"]),
                slots=tuple(sorted(g["slots"])),
                byte_class="+".join(classes),
                start=g["start"],
                end=g["end"],
                changed_bytes=g["bytes"],
                changed_cells=cells_changed,
                nodes=tuple(sorted(g["nodes"])),
                blockers=_lever_blockers(lever, classes, g, label),
            )
        )
    return entries


def _lever_blockers(
    lever: str, classes: list[str], g: Mapping[str, Any], label: str
) -> tuple[Blocker, ...]:
    out: list[Blocker] = []
    span = _hex_span(g["start"], g["end"])
    if lever == UNMAPPED:
        out.append(
            Blocker(
                code=NOT_MAPPED,
                text=TEXT_NOT_MAPPED,
                detail=f"bytes {span} are outside rom_map.yaml: no node reads them",
            )
        )
    if "mapped_unmodelled" in classes:
        out.append(
            Blocker(
                code=MAPPED_UNMODELLED,
                text="mapped but unmodelled",
                detail=f"{label} is in rom_map.yaml but no application-model node reads it",
            )
        )
    if "inactive_for_scenario" in classes and "consumed" not in classes:
        out.append(
            Blocker(
                code=INACTIVE_SLOT,
                text="not the running map",
                detail=f"{label} {span}: a slot copy the ECU is not running",
            )
        )
    return tuple(out)


# -- outputs --------------------------------------------------------------------------------


def _ecu_output_reasons(
    report: EffectReport, model: ApplicationModel, contract: SimContract
) -> dict[str, list[str]]:
    """What the run withholds from each ECU output it computes, as the run does
    it: the gate reasons :func:`paired_scenarios.twin_gate` forms (BFS through
    :func:`paired_steady.ecu_output_gate`, the steady coverage read from the
    contract's mode), each kept only for the outputs downstream of the node it
    names (:class:`paired_scenarios.ReasonScope`). Reasons that are only the
    gate's own verdict word (``..._unevaluable``) are dropped: they restate the
    status. Scenario-specific gates (a point that conditions a quantity the edit
    reaches) are not known here."""
    assert report.mode is not None
    coverage = steady_coverage(contract, report.mode, report.changed_nodes)
    gated = twin_gate(report, coverage, [], model)
    if any("unknown_bytes_changed" in o.reasons for o in report.outputs):
        gated.append(("rom:unknown_bytes_changed", None))
    scope = ReasonScope.for_model(model, gated)
    names = sorted({r for r, _ in gated})
    out: dict[str, list[str]] = {}
    for runner_output, model_output in ECU_RUNNER_OUTPUTS.items():
        kept = scope.keep(runner_output, names)
        out[model_output] = [
            r.removeprefix("rom:")
            for r in kept
            if ":" in r.removeprefix("rom:") or r == "rom:unknown_bytes_changed"
        ]
    return out


def _carrier(
    model: ApplicationModel, spec: Output, carriers: Mapping[str, Quantity]
) -> Quantity | None:
    """The contract quantity that carries an output: its writer's, or for an
    intermediate the final output's it feeds."""
    found = carriers.get(spec.written_by)
    if found is None and spec.feeds is not None:
        found = carriers.get(model.output(spec.feeds).written_by)
    return found


def _actuators(contract: SimContract, quantity_id: str) -> list[Quantity]:
    return [
        q
        for q in contract.quantities
        if q.actuator is not None and q.actuator.commanded_by == quantity_id
    ]


def _readers(contract: SimContract, quantity_id: str) -> list[Subsystem]:
    """The stages computing a quantity that reads ``quantity_id``."""
    out: list[Subsystem] = []
    for q in contract.quantities:
        if any(d.quantity == quantity_id for d in q.depends_on):
            sub = contract.subsystem_of(q.id)
            if sub is not None and sub not in out:
                out.append(sub)
    return out


def chain_standing(
    contract: SimContract, mode: str, claim: str, qualification: Qualification
) -> dict[str, SubsystemStanding]:
    """The derived standing of each stage the claim rests on, as far as it can
    be derived without the profile: the fuel stage from ``qualification`` (for
    its plant version), a stage whose standing reads the profile (a convergence
    study) is left out and so counts as not validated."""
    out: dict[str, SubsystemStanding] = {}
    for sub in claim_chain(contract, mode, claim):
        impl = sub.implementation
        fuel = impl is not None and qualification.plant_version in impl.plant_versions
        try:
            out[sub.id] = subsystem_standing(
                contract,
                sub,
                plant_version=qualification.plant_version if fuel else None,
                qualification=qualification if fuel else None,
            )
        except SimulationRefusalError as refused:
            if refused.reason != "profile_required":
                raise
    return out


def _owed(sub: Subsystem) -> str:
    """The measurements a stage still waits on, by task item where one is named."""
    missing = [m for m in sub.required_measurements if m.status != "available"]
    tasks = list(dict.fromkeys(m.task for m in missing if m.task))
    rest = len([m for m in missing if not m.task])
    named = ", ".join(tasks)
    if rest:
        more = f"{rest} more" if tasks else f"{rest} measurements"
        named = f"{named} and {more}" if named else more
    return named


def _claim_blocker(
    reason: str, contract: SimContract, standing: Mapping[str, SubsystemStanding], mode: str
) -> Blocker:
    code, _, rest = reason.partition(":")
    rest = rest.strip()
    if code == "subsystem_not_validated":
        sub = contract.subsystem(rest)
        label = sub.id.replace("_", " ")
        if sub.status == "unbuilt":
            torque = CLAIM_QUANTITY["torque"] in sub.computes
            return Blocker(
                code=NO_TORQUE_PLANT if torque else "subsystem_unbuilt",
                text=TEXT_NO_TORQUE_PLANT if torque else f"{label} unbuilt",
                detail=f"the {sub.id} stage is unbuilt (E07.5)",
            )
        st = standing.get(sub.id)
        why = [r for r in (st.reasons if st else ()) if not r.startswith("requires_measurement:")]
        # A stage judged by its own held-out verdicts (the fuel plant) says
        # that; any other names the measurements it waits on.
        verdict = next((r for r in why if r.startswith("plant_not_qualified")), None)
        owed = "plant not qualified" if verdict else _owed(sub)
        missing = [m.id for m in sub.required_measurements if m.status != "available"]
        detail = "; ".join([*why, *([f"owed: {', '.join(missing)}"] if missing else [])]) or (
            "standing not derivable without the profile" if st is None else None
        )
        return Blocker(
            code=code,
            text=f"{label} built, not validated"
            + (f" ({owed})" if verdict else f" (owed {owed})" if owed else ""),
            detail=detail,
        )
    if code.endswith("_response_unmeasured"):
        return Blocker(code=code, text=code.replace("_", " "), detail=rest or None)
    if code.endswith("_not_computed"):
        quantity = code.removesuffix("_not_computed").replace("_", " ")
        return Blocker(code=code, text=f"{quantity} not computed in {mode}", detail=rest or None)
    if code == "controls_unresolved":
        return Blocker(code=code, text="changed controls unresolved", detail=rest or None)
    return Blocker(code=code, text=reason)


def _actuated_blockers(
    contract: SimContract, carrier: Quantity, mode: str, qualification: Qualification
) -> list[Blocker]:
    """What blocks a claim through the actuators ``carrier`` commands: per
    claim-computing stage that reads one, the claim's refusals with that
    actuator's control changed."""
    out: list[Blocker] = []
    claims = {q: c for c, q in reversed(list(CLAIM_QUANTITY.items()))}
    for actuator in _actuators(contract, carrier.id):
        assert actuator.actuator is not None
        for sub in _readers(contract, actuator.id):
            for q in sub.computes:
                claim = claims.get(q)
                if claim is None:
                    continue
                standing = chain_standing(contract, mode, claim, qualification)
                for reason in claim_refusals(
                    contract,
                    standing,
                    mode,
                    claim,
                    changed_controls=[actuator.actuator.control],
                ):
                    out.append(_claim_blocker(reason, contract, standing, mode))
    return out


# -- the edit's controls and its claim (E07.5 item 8) ------------------------------------------


class EditControls(_Strict):
    """What an edit changes on the plant side: the controls of the actuators
    its reached contract quantities command (closed downstream through ECU
    quantities), and why the set may be incomplete."""

    controls: tuple[str, ...] = ()
    #: Non-empty when the comparison cannot say what the edit moves: bytes no
    #: node reads, or a reached output no contract quantity carries.
    unresolved: tuple[str, ...] = ()


def changed_controls(
    report: EffectReport, model: ApplicationModel, contract: SimContract
) -> EditControls:
    """The controls an edit changes, from the comparison: the contract
    quantities whose manifest node the change reaches, followed downstream
    through ECU quantities to the actuators they command. A timing-table edit
    reaches the timing byte (``spark``); a fuel edit the pulse (``lambda``)."""
    if report.reason is not None or report.outcome == "identical":
        return EditControls()
    carriers = contract.by_manifest_node()
    unresolved: list[str] = []
    if any("unknown_bytes_changed" in o.reasons for o in report.outputs):
        unresolved.append("unknown_bytes_changed")
    for effect in report.outputs:
        if (
            effect.status != "unchanged"
            and _carrier(model, model.output(effect.output), carriers) is None
        ):
            unresolved.append(f"output_not_in_contract:{effect.output}")
    reached = set(report.changed_nodes) | {n for o in report.outputs for n in o.path}
    stack = [q.id for node, q in carriers.items() if node in reached]
    dependents: dict[str, list[Quantity]] = {}
    for q in contract.quantities:
        for d in q.depends_on:
            if d.quantity != q.id:
                dependents.setdefault(d.quantity, []).append(q)
    seen: set[str] = set()
    controls: set[str] = set()
    while stack:
        q_id = stack.pop()
        if q_id in seen:
            continue
        seen.add(q_id)
        for dep in dependents.get(q_id, ()):
            if dep.actuator is not None and dep.actuator.commanded_by == q_id:
                controls.add(dep.actuator.control)
            elif dep.kind == "ecu":
                stack.append(dep.id)
    if not controls and not unresolved:
        # The edit changed bytes but reaches no contract quantity, so nothing
        # says what it moves: refused, never read as "changes nothing".
        unresolved.append("no_contract_quantity_reached")
    return EditControls(controls=tuple(sorted(controls)), unresolved=tuple(unresolved))


def edit_claim_refusals(
    report: EffectReport,
    *,
    model: ApplicationModel,
    contract: SimContract,
    qualification: Qualification,
    claim: str = EDIT_CLAIM,
    standing: Mapping[str, SubsystemStanding] | None = None,
) -> list[str]:
    """Why ``claim`` (torque, power, knock, acceleration) cannot be made for
    this edit; empty when it can. The controls are :func:`changed_controls`'s,
    never the caller's; an edit whose controls cannot be resolved is refused
    (``controls_unresolved``). ``standing`` defaults to
    :func:`chain_standing`."""
    if report.reason is not None:
        return [f"comparison_refused: {report.reason}"]
    if report.mode is None:
        raise ValueError("a claim is made under a contract mode: compare_roms(..., mode=mode)")
    edit = changed_controls(report, model, contract)
    reasons: list[str] = []
    if edit.unresolved:
        reasons.append(f"controls_unresolved: {', '.join(edit.unresolved)}")
    if standing is None:
        standing = chain_standing(contract, report.mode, claim, qualification)
    reasons += claim_refusals(
        contract, standing, report.mode, claim, changed_controls=edit.controls
    )
    return reasons


def require_edit_claim(
    report: EffectReport,
    *,
    model: ApplicationModel,
    contract: SimContract,
    qualification: Qualification,
    claim: str = EDIT_CLAIM,
    standing: Mapping[str, SubsystemStanding] | None = None,
) -> None:
    """Raise :class:`SimulationRefusalError` naming every reason ``claim``
    cannot be made for this edit (the first reason's code is the error's); a
    stage that is not validated is followed by the measurements it still owes,
    by task item."""
    reasons = edit_claim_refusals(
        report,
        model=model,
        contract=contract,
        qualification=qualification,
        claim=claim,
        standing=standing,
    )
    if reasons:
        raise SimulationRefusalError(
            reasons[0].split(":")[0], "; ".join(_with_owed(r, contract) for r in reasons)
        )


def _with_owed(reason: str, contract: SimContract) -> str:
    code, _, sub_id = reason.partition(":")
    if code != "subsystem_not_validated":
        return reason
    missing = [
        f"{m.id} ({m.task})" if m.task else m.id
        for m in contract.subsystem(sub_id).required_measurements
        if m.status != "available"
    ]
    return f"{reason} (owed: {', '.join(missing)})" if missing else reason


def _unknown_blockers(entries: Sequence[LeverEntry]) -> list[Blocker]:
    """What the run's ``unknown_bytes_changed`` means for this edit: it is one
    reason for two causes. Bytes outside the map are ``not mapped``; bytes in a
    mapped table that no node reads are a mapped table no node reads."""
    out: list[Blocker] = []
    for e in entries:
        if e.lever == UNMAPPED:
            out.append(
                Blocker(
                    code=NOT_MAPPED,
                    text=TEXT_NOT_MAPPED,
                    detail="changed bytes outside rom_map.yaml: every output is unknown",
                )
            )
        if "mapped_unmodelled" in e.byte_class.split("+"):
            out.append(
                Blocker(
                    code=MAPPED_UNMODELLED,
                    text="unknown bytes changed: a mapped table no node reads"
                    f" ({', '.join(e.tables)})",
                    detail="the table is in rom_map.yaml but no application-model node reads it",
                )
            )
    return out


def _reason_blockers(reasons: Sequence[str], entries: Sequence[LeverEntry]) -> list[Blocker]:
    out: list[Blocker] = []
    for reason in reasons:
        if reason == "unknown_bytes_changed":
            out.extend(_unknown_blockers(entries))
        else:
            out.append(_reason_blocker(reason))
    return out


def _reason_blocker(reason: str) -> Blocker:
    code, _, node = reason.partition(":")
    if code == UNMODELLED:
        return Blocker(code=code, text=f"unmodelled: {node}", node=node)
    if code == OBSERVED_NOT_PREDICTED:
        return Blocker(code=code, text=f"observed, not predicted: {node}", node=node)
    if code == HELD_FIXED:
        return Blocker(code=code, text=f"held fixed by the mode: {node}", node=node)
    if code == OUTSIDE_DECODE:
        return Blocker(code=code, text=f"outside decode: {node}", node=node)
    return Blocker(code=code, text=reason, node=node or None)


def _plant_text(qualification: Qualification) -> str:
    """``plant_not_qualified: <why>`` as the reader sees it."""
    why = (qualification.reason or "").partition(":")[2].strip()
    return f"plant not qualified: {why}" if why else "plant not qualified"


def _dedupe(blockers: Sequence[Blocker]) -> tuple[Blocker, ...]:
    seen: set[tuple[str, str | None, str]] = set()
    out: list[Blocker] = []
    for b in blockers:
        key = (b.code, b.node, b.text)
        if key not in seen:
            seen.add(key)
            out.append(b)
    return tuple(out)


def _levers_reaching(
    path: Sequence[str], entries: Sequence[LeverEntry], unknown: bool
) -> tuple[str, ...]:
    labels: list[str] = []
    for entry in entries:
        unknown_here = entry.lever == UNMAPPED or "mapped_unmodelled" in entry.byte_class.split("+")
        hit = (unknown and unknown_here) or bool(set(entry.nodes) & set(path))
        if hit and entry.label not in labels:
            labels.append(entry.label)
    return tuple(labels)


def classify_edit(
    report: EffectReport,
    *,
    rom_map: RomMap,
    model: ApplicationModel,
    contract: SimContract,
    qualification: Qualification,
    regime: Regime | None = None,
    cells: Sequence[Mapping[str, Any]] | None = None,
) -> LeverClassification:
    """Classify an edit by lever and name what blocks a number, per output.

    ``report`` is ``compare_roms(base, candidate, ...)``: the edit's resolved
    cells (E08.1 provenance) are not needed to find the levers, since the
    comparison's changed ranges say the same for an edit spec and for an image
    saved elsewhere. ``cells`` (``provenance['cells']``) only adds a changed-cell
    count per lever. The report must be made under the run's contract mode
    (``compare_roms(..., contract=contract, mode=mode)``): the steady coverage
    that decides which reasons the run absorbs is that mode's, so a report
    without one is refused (``ValueError``) rather than classified on a rule the
    run does not use.

    ``qualification`` is ``fuel_plant.plant_qualification(plant)``. ``regime``
    is the operating regime the edit is run in; ``None`` is "not stated", which
    lists the closed-loop blocker because most of a drive is closed loop.

    Never raises for an image the comparison refused: the result carries the
    refusal ``reason`` and one message saying so."""
    if regime is not None and regime not in get_args(Regime):
        raise ValueError(f"regime {regime!r} is not one of {list(get_args(Regime))}")
    if report.reason is not None:
        return LeverClassification(
            outcome=report.outcome,
            reason=report.reason,
            regime=regime,
            messages=(f"cannot classify the edit: the comparison refused ({report.reason})",),
        )
    if report.outcome == "identical":
        return LeverClassification(
            outcome=report.outcome,
            regime=regime,
            outputs=tuple(
                OutputBlockers(output=o.id, source="application_model", status="unchanged")
                for o in model.outputs
            ),
            messages=("no change: the candidate is the baseline image",),
        )
    if report.mode is None:
        raise ValueError(
            "classify_edit needs a report made under the run's contract mode: "
            "compare_roms(..., contract=contract, mode=mode)"
        )
    entries = _levers(report.ranges, rom_map, cells)
    ecu_reasons = _ecu_output_reasons(report, model, contract)
    carriers = contract.by_manifest_node()

    outputs: list[OutputBlockers] = []
    by_name: dict[str, OutputBlockers] = {}
    for effect in report.outputs:
        spec = model.output(effect.output)
        reached = effect.status != "unchanged"
        row_status: str = effect.status
        if effect.output in ecu_reasons:
            # The run's own rule for the outputs it computes.
            reasons = ecu_reasons[effect.output]
            if reached and not reasons:
                row_status = "changed"
        else:
            reasons = list(effect.reasons)
        blockers = _reason_blockers(reasons, entries)
        carrier = _carrier(model, spec, carriers) if reached else None
        if reached and carrier is None:
            blockers.append(
                Blocker(
                    code=NO_PLANT_CONSUMER,
                    text=f"no plant consumes {effect.output}",
                    node=spec.written_by,
                    detail="no simulation-contract quantity carries this output",
                )
            )
        elif carrier is not None:
            blockers.extend(_actuated_blockers(contract, carrier, report.mode, qualification))
        row = OutputBlockers(
            output=effect.output,
            source="application_model",
            status=row_status,
            levers=_levers_reaching(effect.path, entries, row_status == "unknown")
            if reached
            else (),
            blockers=_dedupe(blockers) if reached else (),
        )
        outputs.append(row)
        by_name[effect.output] = row

    # The plant's outputs are downstream of the two ECU outputs the runner
    # computes; they are reached when those are.
    fuel_chain = [by_name[m] for m in ECU_RUNNER_OUTPUTS.values() if m in by_name]
    fuel_reached = any(o.status in ("changed", "unevaluable") for o in fuel_chain)
    fuel_unknown = any(o.status == "unknown" for o in fuel_chain)
    # What the run withholds from the ECU outputs it withholds from everything
    # downstream (the gate reasons reach every output below the node they name).
    inherited = [b for o in fuel_chain for b in o.blockers]
    pending = closed_loop_trim_pending(model)
    for name in SIMULATED_OUTPUTS:
        status = "reached" if fuel_reached else ("unknown" if fuel_unknown else "unchanged")
        blockers = list(inherited) if status != "unchanged" else []
        if status == "unknown":
            blockers.extend(_unknown_blockers(entries))
        if status == "reached":
            if not qualification.qualified:
                blockers.append(
                    Blocker(
                        code=PLANT_NOT_QUALIFIED,
                        text=_plant_text(qualification),
                        detail=f"plant {qualification.plant_version}",
                    )
                )
            if pending and regime in (None, "closed_loop"):
                blockers.append(
                    Blocker(
                        code=TRIM_AUTHORITY_UNDECODED,
                        text=TEXT_CLOSED_LOOP,
                        detail="closed-loop overrides not qualified on the vehicle: "
                        + ", ".join(pending),
                    )
                )
        levers = tuple(dict.fromkeys(label for o in fuel_chain for label in o.levers))
        row = OutputBlockers(
            output=name,
            source="simulated",
            status=status,
            levers=levers if status != "unchanged" else (),
            blockers=_dedupe(blockers),
        )
        outputs.append(row)

    # A number is the run's for a point that states the fill byte and nothing
    # else the edit reaches (what ``run_paired`` refuses or leaves missing).
    conditionable = conditionable_quantities(contract, report.mode)
    if FILL_BYTE not in conditionable:
        raise ValueError(
            f"the contract's mode {report.mode} does not let a point state {FILL_BYTE}"
        )
    reached_nodes = set(report.changed_nodes) | {n for o in report.outputs for n in o.path}
    stated = {
        f"conditioned_on_changed_path:{q}": (q, node)
        for q, node in conditionable.items()
        if node in reached_nodes
    }
    scope = ReasonScope.for_model(model, [(reason, node) for reason, (_, node) in stated.items()])
    runner_of = {m: r for r, m in ECU_RUNNER_OUTPUTS.items()}
    outputs = [
        o.model_copy(
            update={
                "assumes_conditioned": (FILL_BYTE,),
                # Only the outputs downstream of a stated quantity's node are refused.
                "refused_if_conditioned": tuple(
                    stated[r][0] for r in scope.keep(runner_of[o.output], stated)
                ),
            }
        )
        if o.number and o.output in runner_of
        else o
        for o in outputs
    ]
    claim = _claim_line(report, model, contract, qualification)
    return LeverClassification(
        outcome=report.outcome,
        regime=regime,
        levers=tuple(entries),
        outputs=tuple(outputs),
        messages=(*_messages(entries, outputs), *claim),
    )


def _claim_line(
    report: EffectReport,
    model: ApplicationModel,
    contract: SimContract,
    qualification: Qualification,
) -> tuple[str, ...]:
    """The edit's torque/power claim, refused by name (E07.5 item 8)."""
    assert report.mode is not None
    reasons = edit_claim_refusals(
        report, model=model, contract=contract, qualification=qualification
    )
    if not reasons:
        return ()
    edit = changed_controls(report, model, contract)
    standing = chain_standing(contract, report.mode, EDIT_CLAIM, qualification)
    blockers = _dedupe([_claim_blocker(r, contract, standing, report.mode) for r in reasons])
    controls = ", ".join(edit.controls) or "no control resolved"
    return (f"torque/power claim for this edit ({controls}): refused; {_blocker_text(blockers)}",)


# -- the text -------------------------------------------------------------------------------


#: How a stated quantity is named in a message; any other shows its id.
QUANTITY_LABELS: dict[str, str] = {FILL_BYTE: "the fill byte"}


def _caveat(output: OutputBlockers) -> str:
    """``(for points that state only the fill byte)``: the number is the run's
    for a point that states exactly these quantities, and no other."""
    if not output.assumes_conditioned:
        return ""
    named = ", ".join(QUANTITY_LABELS.get(q, q) for q in output.assumes_conditioned)
    return f" (for points that state only {named})"


def _blocker_text(blockers: Sequence[Blocker]) -> str:
    """One output's blockers on one line; blockers that name a node share their
    prefix (``unmodelled: a, b, c``)."""
    parts: dict[str, list[str]] = {}
    for b in blockers:
        if b.node is not None and b.text.endswith(b.node):
            prefix = b.text[: -len(b.node)].rstrip()
            parts.setdefault(prefix, []).append(b.node)
        else:
            parts.setdefault(b.text, [])
    return "; ".join(f"{k} {', '.join(v)}" if v else k for k, v in parts.items())


def _messages(entries: Sequence[LeverEntry], outputs: Sequence[OutputBlockers]) -> tuple[str, ...]:
    lines: list[str] = []
    for e in entries:
        span = _hex_span(e.start, e.end)
        if e.lever == UNMAPPED:
            lines.append(f"{e.label} {span}: {TEXT_NOT_MAPPED} (outside rom_map.yaml)")
        elif e.lever == CHECKSUM:
            lines.append(f"{e.label} {span}: bookkeeping; no output reads them")
        for b in e.blockers:
            if b.code not in (NOT_MAPPED,):
                lines.append(f"{e.label}: {b.text}" + (f" ({b.detail})" if b.detail else ""))
    for o in outputs:
        if not o.reached:
            continue
        who = " + ".join(o.levers) if o.levers else "edit"
        if not o.blockers:
            lines.append(f"{who} -> {o.output}: a number can be given{_caveat(o)}")
        else:
            lines.append(f"{who} -> {o.output}: {_blocker_text(o.blockers)}")
    if not lines:
        lines.append("the edit reaches no output")
    return tuple(dict.fromkeys(lines))


__all__ = [
    "LEVER_LABELS",
    "SIMULATED_OUTPUTS",
    "TEXT_CLOSED_LOOP",
    "TEXT_NOT_MAPPED",
    "TEXT_NO_TORQUE_PLANT",
    "Blocker",
    "EditControls",
    "LeverClassification",
    "LeverEntry",
    "OutputBlockers",
    "chain_standing",
    "changed_controls",
    "classify_edit",
    "edit_claim_refusals",
    "require_edit_claim",
]
