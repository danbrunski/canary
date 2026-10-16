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
- ``no_torque_plant``: an application-model output no simulation-contract
  quantity carries (ignition: the contract names no quantity whose
  ``manifest_node`` is the timing writer) while the contract's torque subsystem
  is ``unbuilt``. When a plant is built for it the blocker is not produced.
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

from src.analysis.ecu_application import ApplicationModel, OverrideRegime
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
from src.analysis.sim_contract import SimContract

TOOL = "rom_levers"
#: Bump when a code, a text or the result's shape changes meaning.
TOOL_VERSION = "1"

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


def _torque_unbuilt(contract: SimContract) -> bool:
    subsystem = contract.subsystem_of("engine_torque")
    return subsystem is not None and subsystem.status == "unbuilt"


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
    consumers = set(contract.by_manifest_node())
    torque_unbuilt = _torque_unbuilt(contract)

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
        if reached and spec.written_by not in consumers:
            blockers.append(
                Blocker(
                    code=NO_TORQUE_PLANT if torque_unbuilt else NO_PLANT_CONSUMER,
                    text=TEXT_NO_TORQUE_PLANT
                    if torque_unbuilt
                    else f"no plant consumes {effect.output}",
                    node=spec.written_by,
                    detail=(
                        "no simulation-contract quantity carries this output and the torque "
                        "subsystem is unbuilt (E07.5)"
                        if torque_unbuilt
                        else None
                    ),
                )
            )
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
    return LeverClassification(
        outcome=report.outcome,
        regime=regime,
        levers=tuple(entries),
        outputs=tuple(outputs),
        messages=_messages(entries, outputs),
    )


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
    "LeverClassification",
    "LeverEntry",
    "OutputBlockers",
    "classify_edit",
]
