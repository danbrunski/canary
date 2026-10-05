"""Whole-ROM loading and effect coverage for the engine simulator (E07.2).

A complete flash image goes in; out comes what the modelled ECU logic reads
from it, and -- for a baseline/candidate pair -- which modelled computations
and outputs a change reaches. Three sources, none restated here:

- **W08's map** (``rom_map.yaml``) says where each table, axis and slot copy
  sits and how many bytes it has.
- **E05's application model** says which node reads which table, constant and
  calibration condition, and how nodes depend on one another.
- **E07.1's contract** (optional) says which of those nodes a simulation mode
  computes and which it holds fixed.

What it makes impossible rather than documented:

- **Unknown bytes are never no-effect.** Every changed byte is classed:
  ``consumed`` by a node on the active slot, ``inactive_for_scenario`` (a slot
  copy the ECU is not running), ``checksum`` bookkeeping, ``mapped_unmodelled``
  (a mapped table no node reads) or ``unknown``. The last two make every
  output whose change is not otherwise established ``unknown``, never
  ``unchanged``; the image bytes themselves are kept whole.
- **A change is traced, not assumed confined.** A Fuel Comp edit lands in BFS
  formation, which feeds the X byte, which both Target AFR and the timing trims
  look up: every output downstream is reached, and each reports the logic on
  its changed path that the twin does not compute, observes instead of
  predicting, or the mode holds fixed. Such an output is ``unevaluable`` by
  name, so a Fuel Comp edit cannot be scored as a fuel-only change with every
  downstream quantity frozen.
- **Wrong program, missing patch, unresolved or differing slots refuse by
  name**, as a report with an ``unevaluable`` outcome rather than a guess.
- **Simulation edits are isolated artifacts.** :func:`make_candidate` edits
  mapped table cells only, leaves the checksum words stale on purpose (a
  simulation candidate is not a flashable image) and records its provenance;
  :func:`write_candidate` refuses the checkout and the ROM library directory.
  Nothing here, or anything it imports, can reach the ECU: see
  ``tests/test_rom_effects.py``'s clean-process import test.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np

from src.analysis.ecu_application import (
    ApplicationModel,
    ApplicationModelRefusalError,
    Node,
    check_image,
)
from src.analysis.rom_image import map_applies, read_calibration_id, sha256_hex
from src.analysis.rom_map import DTYPE_BYTES, RomMap, Table
from src.analysis.sim_contract import SUPPLIED, SimContract
from src.atomic_write import atomic_write_text
from src.paths import REPO_ROOT

#: Bump when the classification or the trace changes meaning.
TOOL_VERSION = "1"
TOOL = "rom_effects"

ByteClass = Literal["consumed", "inactive_for_scenario", "checksum", "mapped_unmodelled", "unknown"]
OutputStatus = Literal["unchanged", "changed", "unevaluable", "unknown"]
Outcome = Literal["identical", "evaluable", "partial", "unevaluable"]


class RomEffectRefusalError(ValueError):
    """An image or edit the simulator cannot use; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


# -- what the modelled logic reads ------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """``[start, end]`` (inclusive) of the image and who reads it."""

    start: int
    end: int
    byte_class: ByteClass
    #: The node reading it (``consumed`` only).
    node: str | None
    #: A table id, ``constant`` or ``condition``, with any slot.
    what: str
    slot: int | None = None
    #: The bits of each byte the reader uses: a calibration condition tests
    #: only its mask, so a change to the other bits is not a change it reads.
    mask: int = 0xFF

    def contains(self, offset: int) -> bool:
        return self.start <= offset <= self.end


def _placement(rom_map: RomMap, table: Table, slot: int | None) -> tuple[int, int]:
    start = rom_map.table_address(table, slot)
    return start, start + table.size - 1


def _slot_group(rom_map: RomMap, table: Table) -> list[tuple[Table, int]]:
    """Every slot placement of a slot table's kind (``fuel_targets`` in Map 1 and
    in Maps 2-5): what the copy routine can put where the reader looks."""
    kind = table.kind or table.id
    return [
        (t, slot)
        for t in rom_map.tables
        if t.slot_offset is not None and (t.kind or t.id) == kind
        for slot in rom_map.table_slots(t)
        if slot is not None
    ]


def _node_spans(node: Node, rom_map: RomMap, slot: int) -> list[Span]:
    by_id = {t.id: t for t in rom_map.tables}
    spans: list[Span] = []
    for ref in node.rom:
        if ref.rom_map is not None:
            table = by_id[ref.rom_map]
            if table.slot_offset is None:
                start, end = _placement(rom_map, table, None)
                spans.append(Span(start, end, "consumed", node.id, table.id))
            else:
                for placed, placed_slot in _slot_group(rom_map, table):
                    start, end = _placement(rom_map, placed, placed_slot)
                    active = placed_slot == slot
                    spans.append(
                        Span(
                            start,
                            end,
                            "consumed" if active else "inactive_for_scenario",
                            node.id if active else None,
                            placed.id,
                            placed_slot,
                        )
                    )
            for axis_id in (table.axes.rows, table.axes.cols):
                if axis_id is not None:
                    start, end = _placement(rom_map, by_id[axis_id], None)
                    spans.append(Span(start, end, "consumed", node.id, axis_id))
        else:
            assert ref.offset is not None and ref.width is not None
            end = ref.offset + ref.width // 8 - 1
            spans.append(Span(ref.offset, end, "consumed", node.id, "constant"))
    for condition in node.calibration_conditions:
        end = condition.offset + condition.count - 1
        spans.append(
            Span(condition.offset, end, "consumed", node.id, "condition", mask=condition.mask)
        )
    return spans


def coverage_spans(model: ApplicationModel, rom_map: RomMap, slot: int) -> list[Span]:
    """Every byte range the model's nodes read on ``slot`` (and the other
    slots' copies, inactive), the checksum words, and the mapped tables no
    node reads. Bytes in none of these are unknown."""
    spans = [s for node in model.nodes for s in _node_spans(node, rom_map, slot)]
    checks = rom_map.checksums
    for word in (checks.sum_offset, checks.xor_offset):
        spans.append(Span(word, word + 3, "checksum", None, "checksum"))
    cited = {(s.what, s.slot) for s in spans}
    for table in rom_map.tables:
        for table_slot in rom_map.table_slots(table):
            if (table.id, table_slot) in cited:
                continue
            start, end = _placement(rom_map, table, table_slot)
            spans.append(Span(start, end, "mapped_unmodelled", None, table.id, table_slot))
    return spans


#: Most specific first: a byte read on the active slot is consumed even if
#: another view would call it something else.
_PRECEDENCE: tuple[ByteClass, ...] = (
    "consumed",
    "checksum",
    "inactive_for_scenario",
    "mapped_unmodelled",
)


def classify(
    offset: int, spans: Sequence[Span], changed_bits: int = 0xFF
) -> tuple[ByteClass, list[Span]]:
    """The class of a change at ``offset`` to ``changed_bits``. A reader whose
    mask misses every changed bit does not read the change: with no other
    reader the change is ``unknown`` (an undecoded bit), never inert."""
    hits = [s for s in spans if s.contains(offset) and s.mask & changed_bits]
    for byte_class in _PRECEDENCE:
        chosen = [s for s in hits if s.byte_class == byte_class]
        if chosen:
            return byte_class, chosen
    return "unknown", []


# -- loading a whole image --------------------------------------------------------------


@dataclass(frozen=True)
class LoadedRom:
    """A complete image, identified, with every modelled read resolved.

    ``image`` is the whole image, unknown bytes included; nothing is dropped."""

    image: bytes
    image_sha256: str
    program_id: str
    model_sha256: str
    model_version: int
    slot: int
    slot_source: str
    #: ``(node, what, slot)`` -> raw bytes as hex, for every modelled table,
    #: axis, constant and condition read.
    reads: dict[str, str]
    #: Constants whose image value differs from the decoded one, and failing
    #: calibration conditions: the node's decode does not describe this image.
    outside_decode: tuple[str, ...]
    coverage: dict[str, int]

    def digest(self) -> str:
        """sha256 of everything resolved: equal images and states give equal digests."""
        payload = {
            "image": self.image_sha256,
            "program": self.program_id,
            "model": self.model_sha256,
            "slot": self.slot,
            "reads": self.reads,
            "outside_decode": list(self.outside_decode),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def _outside_decode(image: bytes, model: ApplicationModel) -> list[str]:
    out: list[str] = []
    for node in model.nodes:
        for ref in node.rom:
            if ref.offset is None:
                continue
            assert ref.width is not None and ref.value is not None
            raw = int.from_bytes(image[ref.offset : ref.offset + ref.width // 8], "big")
            if raw != ref.value:
                out.append(f"{node.id}@0x{ref.offset:X}: {raw} not the decoded {ref.value}")
        for condition in node.calibration_conditions:
            failed = condition.failures(image)
            if failed:
                out.append(
                    f"{node.id}@{','.join(f'0x{o:X}' for o in failed)}: condition fails "
                    f"({condition.why})"
                )
    return out


def load_rom(
    image: bytes,
    rom_map: RomMap,
    model: ApplicationModel,
    model_sha256: str,
    *,
    slot: int | None,
    slot_source: str,
) -> LoadedRom:
    """Identify ``image`` and resolve every table, constant and condition the
    model's nodes read on the active ``slot``.

    Refuses by name: ``image_size_mismatch``, ``map_does_not_apply`` (the
    calibration ID is not one the map describes), ``wrong_program`` /
    ``patch_missing`` (the application model's identity checks),
    ``active_slot_unresolved`` and ``slot_out_of_range``. A failing
    calibration condition is not a refusal of the image: it is recorded in
    ``outside_decode`` and makes every output it reaches unevaluable."""
    if len(image) != rom_map.image_size:
        raise RomEffectRefusalError(
            "image_size_mismatch", f"{len(image):,} bytes, the map describes {rom_map.image_size:,}"
        )
    reason = map_applies(read_calibration_id(image, rom_map), rom_map)
    if reason is not None:
        raise RomEffectRefusalError("map_does_not_apply", reason)
    try:
        check_image(image, model)
    except ApplicationModelRefusalError as refused:
        if refused.reason != "unmodelled_logic":
            raise RomEffectRefusalError(refused.reason, refused.message) from refused
    if slot is None:
        raise RomEffectRefusalError(
            "active_slot_unresolved",
            "the active map slot is not known, so which table copies the ECU reads is not known",
        )
    slots = rom_map.map_slots
    if slots is None or not 1 <= slot <= slots.count:
        raise RomEffectRefusalError("slot_out_of_range", f"slot {slot} is not a map slot here")
    spans = coverage_spans(model, rom_map, slot)
    reads = {
        f"{s.node}|{s.what}|{s.slot}|0x{s.start:06X}": image[s.start : s.end + 1].hex()
        for s in spans
        if s.byte_class == "consumed"
    }
    return LoadedRom(
        image=image,
        image_sha256=sha256_hex(image),
        program_id=model.program.id,
        model_sha256=model_sha256,
        model_version=model.model_version,
        slot=slot,
        slot_source=slot_source,
        reads=reads,
        outside_decode=tuple(_outside_decode(image, model)),
        coverage=_coverage(model, rom_map, spans),
    )


def _coverage(model: ApplicationModel, rom_map: RomMap, spans: Sequence[Span]) -> dict[str, int]:
    """Bytes per class outside the program spans (what a reflash can change)."""
    calibration = np.ones(rom_map.image_size, dtype=bool)
    for start, end in model.program.identity.regions:
        calibration[start:end] = False
    labels = np.full(rom_map.image_size, len(_PRECEDENCE), dtype=np.int8)
    for rank in reversed(range(len(_PRECEDENCE))):
        for s in spans:
            if s.byte_class == _PRECEDENCE[rank]:
                labels[s.start : s.end + 1] = rank
    counts: dict[str, int] = {
        name: int(((labels == rank) & calibration).sum()) for rank, name in enumerate(_PRECEDENCE)
    }
    unknown = (labels == len(_PRECEDENCE)) & calibration
    erased = np.zeros(rom_map.image_size, dtype=bool)
    for region in rom_map.regions:
        if region.kind == "erased":
            erased[region.start : region.end + 1] = True
    # Erased bytes are known to be empty, not known to be unread: a change there
    # is still an unknown-effect change.
    counts["unknown_erased"] = int((unknown & erased).sum())
    counts["unknown"] = int((unknown & ~erased).sum())
    counts["program"] = int((~calibration).sum())
    return counts


# -- tracing a change -------------------------------------------------------------------


@dataclass(frozen=True)
class ChangedRange:
    start: int
    #: Inclusive.
    end: int
    byte_class: ByteClass
    nodes: tuple[str, ...]
    what: tuple[str, ...]
    #: Changed bytes in the range.
    changed: int


@dataclass(frozen=True)
class OutputEffect:
    output: str
    status: OutputStatus
    #: Nodes on a path from a changed node to the output's writer.
    path: tuple[str, ...]
    reasons: tuple[str, ...]
    #: The manifest's own standing of the output (``conditional`` etc.).
    model_status: str
    #: The twin's predicted node, when the output has one: reached by the
    #: change, and computed at equal inputs by implemented nodes only.
    twin_node: dict[str, Any] | None = None


@dataclass(frozen=True)
class EffectReport:
    outcome: Outcome
    reason: str | None
    base_sha256: str | None
    candidate_sha256: str | None
    slot: int | None
    mode: str | None
    changed_bytes: int = 0
    ranges: tuple[ChangedRange, ...] = ()
    changed_nodes: tuple[str, ...] = ()
    outside_decode: tuple[str, ...] = ()
    #: Nodes reached by the change that look up tables: they are read at a
    #: moved operating point (e.g. Target AFR and timing trims after BFS moves).
    moved_lookups: tuple[dict[str, Any], ...] = ()
    outputs: tuple[OutputEffect, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    def output(self, output_id: str) -> OutputEffect:
        return next(o for o in self.outputs if o.output == output_id)

    def to_dict(self) -> dict[str, Any]:
        plain: dict[str, Any] = json.loads(json.dumps(self, default=_plain))
        return plain


def _plain(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {k: getattr(value, k) for k in value.__dataclass_fields__}
    raise TypeError(type(value))


def _children(model: ApplicationModel) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {n.id: set() for n in model.nodes}
    for node in model.nodes:
        for dep in node.depends_on:
            out[dep].add(node.id)
    return out


def _closure(start: Iterable[str], edges: dict[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    stack = list(start)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(edges.get(current, ()))
    return seen


def _ancestors(model: ApplicationModel, node: str) -> set[str]:
    """``node`` and everything it depends on, through observed nodes too: a
    ROM change does not stop at a quantity the twin happens to measure."""
    parents = {n.id: set(n.depends_on) for n in model.nodes}
    return _closure([node], parents)


def _path_reasons(
    path: Iterable[str],
    model: ApplicationModel,
    quantities: dict[str, str],
    mode_roles: dict[str, str] | None,
) -> list[str]:
    reasons: list[str] = []
    for node_id in path:
        node = model.node(node_id)
        quantity = quantities.get(node_id)
        role = None if mode_roles is None or quantity is None else mode_roles[quantity]
        if role in SUPPLIED:
            reasons.append(f"held_fixed:{quantity}")
        elif node.twin == "observed":
            reasons.append(f"observed_not_predicted:{node_id}")
        elif node.twin in ("assumed", "not_modelled"):
            reasons.append(f"unmodelled:{node_id}")
    return reasons


def _refused(
    reason: str, base: bytes | None, candidate: bytes | None, mode: str | None
) -> EffectReport:
    return EffectReport(
        outcome="unevaluable",
        reason=reason,
        base_sha256=None if base is None else sha256_hex(base),
        candidate_sha256=None if candidate is None else sha256_hex(candidate),
        slot=None,
        mode=mode,
        provenance=_provenance(),
    )


def _provenance() -> dict[str, Any]:
    return {"tool": TOOL, "tool_version": TOOL_VERSION}


def compare_roms(
    base_image: bytes,
    candidate_image: bytes,
    rom_map: RomMap,
    model: ApplicationModel,
    model_sha256: str,
    *,
    base_slot: int | None,
    candidate_slot: int | None,
    slot_source: str,
    contract: SimContract | None = None,
    mode: str | None = None,
) -> EffectReport:
    """Trace ``base_image`` -> ``candidate_image`` through the application
    model. Never raises for an image it cannot use: the report's outcome is
    ``unevaluable`` with the refusal's ``reason``.

    With ``contract`` and ``mode``, a node the mode supplies (a measured input
    or a prescribed boundary) on a changed path makes the output unevaluable
    (``held_fixed:<quantity>``): the mode would freeze what the change moves."""
    if (contract is None) != (mode is None):
        raise ValueError("give both contract and mode, or neither")
    mode_roles: dict[str, str] | None = None
    quantities: dict[str, str] = {}
    if contract is not None and mode is not None:
        roles = contract.mode(mode).roles
        mode_roles = {q: str(a.role) for q, a in roles.items()}
        quantities = {n: q.id for n, q in contract.by_manifest_node().items()}
    try:
        base = load_rom(
            base_image, rom_map, model, model_sha256, slot=base_slot, slot_source=slot_source
        )
        candidate = load_rom(
            candidate_image,
            rom_map,
            model,
            model_sha256,
            slot=candidate_slot,
            slot_source=slot_source,
        )
    except RomEffectRefusalError as refused:
        return _refused(refused.reason, base_image, candidate_image, mode)
    if base.slot != candidate.slot:
        return _refused("slot_mismatch", base_image, candidate_image, mode)
    provenance = {
        **_provenance(),
        "program": model.program.id,
        "application_model": {"sha256": model_sha256, "model_version": model.model_version},
        "slot_source": slot_source,
        "base_digest": base.digest(),
        "candidate_digest": candidate.digest(),
    }
    if base.image_sha256 == candidate.image_sha256:
        return EffectReport(
            outcome="identical",
            reason=None,
            outputs=tuple(OutputEffect(o.id, "unchanged", (), (), o.status) for o in model.outputs),
            provenance=provenance,
            base_sha256=base.image_sha256,
            candidate_sha256=candidate.image_sha256,
            slot=base.slot,
            mode=mode,
        )
    spans = coverage_spans(model, rom_map, base.slot)
    a = np.frombuffer(base.image, dtype=np.uint8)
    b = np.frombuffer(candidate.image, dtype=np.uint8)
    diff = np.flatnonzero(a != b).tolist()
    ranges = _ranges(diff, spans, a, b)
    changed_nodes = sorted({n for r in ranges if r.byte_class == "consumed" for n in r.nodes})
    # A node whose constant or condition is off its decode in either image.
    decode_nodes = {
        entry.split("@", 1)[0] for entry in (*base.outside_decode, *candidate.outside_decode)
    }
    unknown_changed = any(r.byte_class in ("unknown", "mapped_unmodelled") for r in ranges)
    children = _children(model)
    reach = _closure(changed_nodes, children)
    outputs = []
    for output in model.outputs:
        on_path = reach & _ancestors(model, output.written_by)
        path = tuple(n.id for n in model.nodes if n.id in on_path)
        reasons = _path_reasons(path, model, quantities, mode_roles)
        reasons += [f"outside_decode:{n}" for n in path if n in decode_nodes]
        twin_node = None
        if output.twin is not None:
            predicts = output.twin.predicts
            twin_path = tuple(
                n.id for n in model.nodes if n.id in reach & _ancestors(model, predicts)
            )
            twin_reasons = _path_reasons(twin_path, model, quantities, mode_roles)
            twin_reasons += [f"outside_decode:{n}" for n in twin_path if n in decode_nodes]
            twin_node = {
                "node": predicts,
                "reached": bool(twin_path),
                "evaluable_at_equal_inputs": bool(twin_path)
                and not twin_reasons
                and not unknown_changed,
                "reasons": twin_reasons,
            }
        if path:
            status: OutputStatus = "unevaluable" if reasons or unknown_changed else "changed"
        else:
            status = "unknown" if unknown_changed else "unchanged"
        if unknown_changed:
            reasons.append("unknown_bytes_changed")
        outputs.append(
            OutputEffect(output.id, status, path, tuple(reasons), output.status, twin_node)
        )
    moved = tuple(
        {
            "node": n.id,
            "tables": [r.rom_map for r in n.rom if r.rom_map is not None],
        }
        for n in model.nodes
        if n.id in reach and n.id not in changed_nodes and any(r.rom_map for r in n.rom)
    )
    statuses = {o.status for o in outputs}
    if statuses <= {"unchanged", "changed"}:
        outcome: Outcome = "evaluable"
    elif statuses & {"unchanged", "changed"}:
        outcome = "partial"
    else:
        outcome = "unevaluable"
    return EffectReport(
        outcome=outcome,
        reason=None,
        changed_bytes=len(diff),
        ranges=tuple(ranges),
        changed_nodes=tuple(changed_nodes),
        outside_decode=candidate.outside_decode,
        moved_lookups=moved,
        outputs=tuple(outputs),
        provenance=provenance,
        base_sha256=base.image_sha256,
        candidate_sha256=candidate.image_sha256,
        slot=base.slot,
        mode=mode,
    )


def _ranges(
    diff: Sequence[int], spans: Sequence[Span], a: np.ndarray, b: np.ndarray
) -> list[ChangedRange]:
    """Changed bytes grouped by class and readers: consecutive changed bytes
    with the same classification form one range (unchanged bytes between them
    included in ``start``..``end``; ``changed`` counts only changed ones)."""
    out: list[ChangedRange] = []
    for offset in diff:
        byte_class, hits = classify(offset, spans, int(a[offset]) ^ int(b[offset]))
        nodes = tuple(sorted({s.node for s in hits if s.node is not None}))
        what = tuple(
            sorted({s.what if s.slot is None else f"{s.what} (slot {s.slot})" for s in hits})
        )
        last = out[-1] if out else None
        if last is not None and (last.byte_class, last.nodes, last.what) == (
            byte_class,
            nodes,
            what,
        ):
            out[-1] = ChangedRange(last.start, offset, byte_class, nodes, what, last.changed + 1)
        else:
            out.append(ChangedRange(offset, offset, byte_class, nodes, what, 1))
    return out


# -- isolated simulation candidates -------------------------------------------------------


@dataclass(frozen=True)
class CellEdit:
    """One mapped table cell set to a raw value."""

    table: str
    row: int
    col: int
    raw: int
    #: Required for a slot table, refused for an absolute one.
    slot: int | None = None


@dataclass(frozen=True)
class Candidate:
    image: bytes
    provenance: dict[str, Any]

    @property
    def image_sha256(self) -> str:
        return sha256_hex(self.image)


def make_candidate(
    base_image: bytes, rom_map: RomMap, edits: Sequence[CellEdit], *, base_label: str
) -> Candidate:
    """``base_image`` with ``edits`` applied to mapped table cells only.

    Every other byte, unknown ones included, is the base's. The checksum words
    are left as they were: a simulation candidate is not a flashable image.
    Refuses by name: ``no_edits``, ``table_unknown``, ``slot_invalid``,
    ``cell_outside_table``, ``value_out_of_range``, ``duplicate_edit``,
    ``no_op_edit``."""
    if not edits:
        raise RomEffectRefusalError("no_edits", "a candidate needs at least one edit")
    by_id = {t.id: t for t in rom_map.tables}
    out = bytearray(base_image)
    seen: set[tuple[str, int | None, int, int]] = set()
    applied = []
    for edit in edits:
        table = by_id.get(edit.table)
        if table is None:
            raise RomEffectRefusalError(
                "table_unknown",
                f"{edit.table!r} is not a mapped table: unmapped bytes are never edited",
            )
        if (table.slot_offset is None) != (edit.slot is None) or (
            edit.slot is not None and edit.slot not in rom_map.table_slots(table)
        ):
            raise RomEffectRefusalError(
                "slot_invalid", f"{table.id} slot {edit.slot} is not a placement of it"
            )
        if not (0 <= edit.row < table.rows and 0 <= edit.col < table.cols):
            raise RomEffectRefusalError(
                "cell_outside_table", f"{table.id} has no row {edit.row} column {edit.col}"
            )
        width = DTYPE_BYTES[table.dtype]
        if not 0 <= edit.raw < 1 << (8 * width):
            raise RomEffectRefusalError(
                "value_out_of_range", f"{edit.raw} does not fit a {table.dtype} cell"
            )
        key = (table.id, edit.slot, edit.row, edit.col)
        if key in seen:
            raise RomEffectRefusalError("duplicate_edit", f"{key} edited twice")
        seen.add(key)
        address = (
            rom_map.table_address(table, edit.slot) + (edit.row * table.cols + edit.col) * width
        )
        old = int.from_bytes(out[address : address + width], "big")
        if old == edit.raw:
            raise RomEffectRefusalError("no_op_edit", f"{key} already holds {old}")
        out[address : address + width] = edit.raw.to_bytes(width, "big")
        applied.append(
            {
                "table": table.id,
                "slot": edit.slot,
                "row": edit.row,
                "col": edit.col,
                "address": address,
                "old": old,
                "new": edit.raw,
            }
        )
    edit_set = json.dumps(applied, sort_keys=True, separators=(",", ":"))
    image = bytes(out)
    return Candidate(
        image=image,
        provenance={
            **_provenance(),
            "kind": "simulation_candidate",
            "flashable": False,
            "checksums": "stale on purpose: a simulation candidate is not a flashable image",
            "base_sha256": sha256_hex(base_image),
            "base_label": base_label,
            "candidate_sha256": sha256_hex(image),
            "edit_set_sha256": hashlib.sha256(edit_set.encode()).hexdigest(),
            "edits": applied,
        },
    )


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def write_candidate(candidate: Candidate, root: Path, *, rom_library_dir: Path) -> Path:
    """Store ``candidate`` as ``<root>/<sha256>/image.sim`` beside its
    ``provenance.json``. ``root`` is a runtime directory (scratch or under the
    state dir): refused inside the checkout (``inside_checkout``: images stay
    out of Git) and inside or around the ROM library (``inside_rom_library``:
    a candidate never sits where real images are stored and uploaded from)."""
    resolved = Path(root).resolve()
    library = Path(rom_library_dir).resolve()
    if _inside(resolved, REPO_ROOT) or _inside(REPO_ROOT, resolved):
        raise RomEffectRefusalError(
            "inside_checkout", f"{resolved} is in the checkout; images never go into Git"
        )
    if _inside(resolved, library) or _inside(library, resolved):
        raise RomEffectRefusalError(
            "inside_rom_library",
            f"{resolved} overlaps the ROM library {library}; simulation candidates stay apart",
        )
    directory = resolved / candidate.image_sha256
    directory.mkdir(parents=True, exist_ok=True)
    image_path = directory / "image.sim"
    tmp = directory / "image.sim.tmp"
    tmp.write_bytes(candidate.image)
    tmp.replace(image_path)
    record = {**candidate.provenance, "written_at": datetime.now(UTC).isoformat()}
    atomic_write_text(directory / "provenance.json", json.dumps(record, indent=2, sort_keys=True))
    return directory


__all__ = [
    "TOOL_VERSION",
    "Candidate",
    "CellEdit",
    "ChangedRange",
    "EffectReport",
    "LoadedRom",
    "OutputEffect",
    "RomEffectRefusalError",
    "Span",
    "classify",
    "compare_roms",
    "coverage_spans",
    "load_rom",
    "make_candidate",
    "write_candidate",
]
