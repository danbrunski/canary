"""Firmware-derived operating context for behavior models (B08.2).

Which ECM coordinates and states a sample was taken under — the exact image
and active map slot, the Fuel Comp rpm/fill coordinates and the cell value
they read, whether either axis clamped, the timing-trim coordinate and the
table the HI DET latch selects, the pedal-enrichment flag, fuel cut, and the
retained purge / idle-air / throttle-fallback state — from the ECM's own RAM,
placed on the exact calibration's tables. ``timing_coordinate`` (B08.4) is the
timing lookup's rpm word x BFS-X tuple and HI DET latch as observed, kept
without the image so it stays visible where the trim itself cannot resolve.

One provider, shared rather than restated:

- **W08** (:mod:`src.analysis.rom_map`, :func:`src.analysis.rom_image.table_values`)
  owns where every table, axis and fill curve lives and how a cell decodes;
  :func:`resolve_context` reads them from a library image by id and refuses a
  wrong axis by name (``axis_mismatch``).
- **E02** owns which slot the ECM is reading (the twin's ``map_identity`` rules,
  :func:`src.analysis.target_phi.resolve_slot`) and the lookup arithmetic
  (:func:`src.decode.ecm_lookup.lookup_2d`).
- **E05** owns the program identity (:func:`src.analysis.ecu_application.check_image`),
  the timing-trim selection (:func:`src.analysis.ecu_state_models.selected_timing_trim`),
  and what is still assumed between the image and the RAM a lookup reads: each
  lookup's applicability is computed from its node in the application model.

**Observed, never reconstructed.** Every coordinate is the RAM the lookup
reads (or its cycle-start copy); this module never rebuilds the fill byte
from throttle (that is E05.2 / N02 work, and is not qualified).

**Relative coordinates.** The fill byte is the ECM's throttle-model fill with
no MAF, baro or temperature input: not VE, not airflow. Its two saturations
are reported separately: ``fill_at_ceiling`` (the fill curve's top, 224
counts on this calibration) and ``rpm_reciprocal_saturated`` (``k x rpm
word`` clamped at 0xFFFF, near 5,185 rpm, above which fill stops falling with
rpm).

**Every value carries its own acquisition time.** The evaluator takes reads,
never a resampled grid (a wide frame is refused: ``not_acquisition_timed``).
A read older than its input's ``max_age_s`` at the query time is
``stale:<input>``; a tuple spread wider than the lookup's skew is
``skew:<lookup>``. A memory window polled every 5 s and forward-filled cannot
pass as a fresh observation. Missing state is named, never defaulted:
``not_captured``, ``missing``, ``stale``, ``clock_unacquired``,
``wrong_units``, ``slot_unresolved:<reason>``, ``not_catalogued``.

The stored (fit) and live (serve) paths are one evaluator: a stored session
is replayed read by read through :class:`ContextStream`, and both carry the
same contract (:func:`contract`) and its fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.ecu_application import (
    ApplicationModel,
    ApplicationModelRefusalError,
    applicability,
    check_image,
)
from src.analysis.ecu_state_models import (
    TRIM_COLD_AT_OR_BELOW,
    TRIM_RPM_AXIS_SCALE,
    TRIM_VALUE_MASK,
    selected_timing_trim,
)
from src.analysis.rom_image import map_applies, read_calibration_id, sha256_hex, table_values
from src.analysis.rom_map import Decode, RomMap, check_program_constants, read_program_constant
from src.analysis.target_phi import MapIdentity, TwinDefinition, load_twin_definition, resolve_slot
from src.columns import ARRIVED_SUFFIX, GAP_SUFFIX
from src.decode.dids import DidMap, load_did_map
from src.decode.ecm_lookup import (
    WORD_MAX,
    AxisHit,
    LookupResult,
    lookup_2d,
    lookup_shift12_q16,
)
from src.decode.fuel_comp_load import BYTE_SHIFT, LOW_CURVE_MULTIPLIER, X_SPLIT
from src.decode.slices import SENTINEL_SUFFIX, linear_factors
from src.paths import DIDS_YAML
from src.signals.pairing import to_seconds
from src.yaml_load import load_yaml

#: Bumped when the evaluator's record shape, reasons or contract change.
#: 3 (B08.4): records carry ``timing_coordinate``, the image-free rpm word x
#: BFS-X tuple and HI DET latch as observed.
CONTEXT_EVALUATOR_VERSION = 3

CONTEXT_FILE = "operating_context.yaml"

#: The only clock a tuple may be judged on (the twin's ``gates.clock``).
ACQUIRED = "acquired"

#: How far a stored value may sit from a whole count once its catalog decode
#: is undone (``src.analysis.rom_trace.WHOLE_COUNT_TOLERANCE``).
_WHOLE = 1e-6

_SPEC = r"^[a-z]+:[A-Za-z0-9_]+$"

Status = Literal["proposed", "qualified"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContextRefusalError(ValueError):
    """The context cannot be resolved or evaluated at all; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message

    def __reduce__(self) -> tuple[object, ...]:
        # Rebuilt from its constructor's arguments, so it survives a process
        # boundary (pickle); tests/test_exceptions_pickle.py holds every class to it.
        return (type(self), (self.reason, self.message), self.__dict__)


# -- the profile declaration -----------------------------------------------------


class InputDecl(_Strict):
    """One RAM observation. ``relation`` says how the channel's read relates
    to what the ECM's lookup consumes: the same byte (``identity``), its
    cycle-start copy (``cycle_copy``), or a state decoded from it (``state``,
    a sentinel). There is no ``reconstructed``: a writer rebuilt from other
    inputs is not an observation."""

    channel: str = Field(pattern=_SPEC)
    ram: int = Field(ge=0xFFFF0000, le=0xFFFFFFFF)
    feeds: list[int] = Field(min_length=1)
    relation: Literal["identity", "cycle_copy", "state"]
    bits: Literal[8, 16]
    bit: int | None = Field(default=None, ge=0, le=15)
    max_age_s: float = Field(gt=0)
    status: Status
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _bit_fits(self) -> InputDecl:
        if self.bit is not None and self.bit >= self.bits:
            raise ValueError(f"{self.channel}: bit {self.bit} is outside a {self.bits}-bit read")
        if self.relation == "state" and not self.channel.endswith(SENTINEL_SUFFIX):
            raise ValueError(f"{self.channel}: a state input is a {SENTINEL_SUFFIX} channel")
        return self


class AxisDecl(_Strict):
    rom_map_axis: str = Field(min_length=1)
    input: str = Field(min_length=1)
    compare_multiplier: int = Field(ge=1)


class LimitDecl(_Strict):
    """A known gap between what the context observes and what the ECM's
    lookup consumed, carried in the contract's applicability by name."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    statement: str = Field(min_length=1)
    closes_with: str = Field(min_length=1)


class FuelCompDecl(_Strict):
    application_node: str = Field(min_length=1)
    rom_map_kind: str = Field(min_length=1)
    rows: AxisDecl
    columns: AxisDecl
    max_acquisition_skew_s: float = Field(gt=0)
    limits: list[LimitDecl] = Field(default_factory=list)
    evidence: str = Field(min_length=1)


class MemoryOnlyTargetDecl(_Strict):
    """A final-timing channel read in the same service-0x23 read as one of the
    timing lookup's coordinate inputs (B08.4, operator decision 2026-10-08,
    option c).

    Its tuple is memory only: the target and ``same_read_as`` share one
    acquisition, so their skew is zero by construction, and the remaining
    coordinate reads sit in the same window's back-to-back memory burst. What
    it gives up is the identical-sample comparison against a surface on Mode 01
    axes, which :mod:`src.analysis.timing_lookup` refuses by name for it."""

    channel: str = Field(pattern=r"^mem:[A-Za-z0-9_]+$")
    #: The coordinate input (``timing_trim.rows`` or ``columns``) whose channel
    #: is decoded from the same read; :func:`check_catalog` holds the two to
    #: one catalog window.
    same_read_as: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class TimingTrimDecl(_Strict):
    application_node: str = Field(min_length=1)
    application_output: str = Field(min_length=1)
    select_input: str = Field(min_length=1)
    rom_map_kind_when_clear: str = Field(min_length=1)
    rom_map_kind_when_set: str = Field(min_length=1)
    pedal_input: str = Field(min_length=1)
    coolant_input: str = Field(min_length=1)
    rows: AxisDecl
    columns: AxisDecl
    max_acquisition_skew_s: float = Field(gt=0)
    limits: list[LimitDecl] = Field(default_factory=list)
    #: B08.4 — channels that observe final ignition timing. Required, so a
    #: timing evaluation cannot take the trim lookup (an intermediate) as its
    #: target.
    final_timing_channels: list[Annotated[str, Field(pattern=_SPEC)]] = Field(min_length=1)
    #: Final-timing channels with a memory-only tuple (B08.4 option c). Each
    #: is one of ``final_timing_channels``.
    memory_only_targets: list[MemoryOnlyTargetDecl] = Field(default_factory=list)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _memory_only_targets(self) -> TimingTrimDecl:
        channels = [t.channel for t in self.memory_only_targets]
        if len(set(channels)) != len(channels):
            raise ValueError("memory_only_targets names a channel twice")
        coordinate = {self.rows.input, self.columns.input}
        for target in self.memory_only_targets:
            if target.channel not in self.final_timing_channels:
                raise ValueError(
                    f"memory_only_targets: {target.channel} is not one of final_timing_channels"
                )
            if target.same_read_as not in coordinate:
                raise ValueError(
                    f"memory_only_targets: {target.channel} same_read_as "
                    f"{target.same_read_as} is not a coordinate input ({sorted(coordinate)})"
                )
        return self


class SaturationDecl(_Strict):
    """W08 ids of what the two saturations are computed from."""

    fill_curve_low: str
    fill_curve_high: str
    rpm_multiplier: str
    reciprocal_numerator: str


class FuelCutDecl(_Strict):
    command_input: str
    sentinel_input: str


class PedalDecl(_Strict):
    input: str


class RetainedDecl(_Strict):
    """Fill-chain state kept when captured. Without ``input`` it is not in the
    memory catalog yet, and ``closes_with`` names what would capture it."""

    input: str | None = None
    ram: int = Field(ge=0xFFFF0000, le=0xFFFFFFFF)
    closes_with: str | None = None
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _one(self) -> RetainedDecl:
        if (self.input is None) == (self.closes_with is None):
            raise ValueError("retained state names an input, or says what would capture it")
        return self


class ContextDefinition(_Strict):
    schema_version: Literal[1]
    name: Literal["firmware_operating_context"]
    context_version: int = Field(ge=1)
    #: Required so no consumer can read the fill byte as VE or airflow.
    interpretation: Literal["relative_model_coordinates"]
    #: The twin definition whose ``map_identity`` decides the slot (E02).
    map_identity_from: Literal["target_phi_twin.yaml"]
    inputs: dict[str, InputDecl] = Field(min_length=1)
    fuel_comp: FuelCompDecl
    timing_trim: TimingTrimDecl
    saturation: SaturationDecl
    fuel_cut: FuelCutDecl
    pedal_enrichment: PedalDecl
    retained: dict[str, RetainedDecl] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _references(self) -> ContextDefinition:
        named = {
            "fuel_comp.rows": self.fuel_comp.rows.input,
            "fuel_comp.columns": self.fuel_comp.columns.input,
            "timing_trim.rows": self.timing_trim.rows.input,
            "timing_trim.columns": self.timing_trim.columns.input,
            "timing_trim.select_input": self.timing_trim.select_input,
            "timing_trim.pedal_input": self.timing_trim.pedal_input,
            "timing_trim.coolant_input": self.timing_trim.coolant_input,
            "fuel_cut.command_input": self.fuel_cut.command_input,
            "fuel_cut.sentinel_input": self.fuel_cut.sentinel_input,
            "pedal_enrichment.input": self.pedal_enrichment.input,
            **{
                f"retained.{name}": r.input
                for name, r in self.retained.items()
                if r.input is not None
            },
        }
        unknown = sorted(
            f"{where} -> {name}" for where, name in named.items() if name not in self.inputs
        )
        if unknown:
            raise ValueError(f"undeclared inputs: {unknown}")
        for where in (
            "timing_trim.select_input",
            "timing_trim.pedal_input",
            "pedal_enrichment.input",
        ):
            if self.inputs[named[where]].bit is None:
                raise ValueError(f"{where} is a flag and needs its `bit`")
        if self.inputs[self.fuel_cut.sentinel_input].relation != "state":
            raise ValueError("fuel_cut.sentinel_input must be the command's sentinel state")
        channels = [i.channel for i in self.inputs.values()]
        if len(set(channels)) != len(channels):
            raise ValueError("two inputs read the same channel; one input per channel")
        return self


def load_context_definition(profile_dir: Path) -> tuple[ContextDefinition, str]:
    """The profile's context definition and the sha256 of its file.
    ``FileNotFoundError`` when the vehicle declares none."""
    text = (profile_dir / CONTEXT_FILE).read_bytes()
    return ContextDefinition.model_validate(load_yaml(text)), hashlib.sha256(text).hexdigest()


# -- catalog decodes ---------------------------------------------------------------


@dataclass(frozen=True)
class StoredDecode:
    """How a channel's stored value relates to the ECM's raw count:
    ``stored = raw x scale + offset``. ``None`` scale: a state channel."""

    scale: float | None
    offset: float
    unit: str | None
    source: str

    def raw(self, stored: float) -> int | None:
        """The whole raw count, or ``None`` when it does not return to one."""
        if self.scale is None:
            return 0
        value = (float(stored) - self.offset) / self.scale
        if not math.isfinite(value) or abs(value - round(value)) > _WHOLE:
            return None
        return int(round(value))

    def engineering(self, raw: int) -> float | None:
        return None if self.scale is None else raw * self.scale + self.offset


def stored_decode(spec: str, catalog: DidMap) -> StoredDecode:
    """The catalog decode ``spec`` is stored under (``mem:`` or ``did:``).
    Refuses ``catalog_missing`` for a channel the catalog does not carry and
    ``wrong_units`` for one whose scale is unpinned (no raw count to undo to)."""
    source, _, name = spec.partition(":")
    if source == "mem" and name.endswith(SENTINEL_SUFFIX):
        base = name[: -len(SENTINEL_SUFFIX)]
        if not any(c.name == base and c.sentinel_raw is not None for c in catalog.memory):
            raise ContextRefusalError("catalog_missing", f"{spec}: no memory sentinel {base}")
        return StoredDecode(None, 0.0, None, "catalog.memory.sentinel")
    if source == "mem":
        channel = next((c for c in catalog.memory if c.name == name), None)
        if channel is None:
            raise ContextRefusalError("catalog_missing", f"{spec} is not in the memory catalog")
        scale, offset = linear_factors(channel.scale, channel.offset) or (1.0, 0.0)
        return StoredDecode(scale, offset, channel.unit, "catalog.memory")
    if source == "did":
        entry = next((d for d in catalog.dids if d.name == name), None)
        if entry is None:
            raise ContextRefusalError("catalog_missing", f"{spec} is not in the DID catalog")
        factors = linear_factors(entry.scale, entry.offset)
        if factors is None:
            raise ContextRefusalError("wrong_units", f"{spec} has no pinned scale to undo")
        return StoredDecode(factors[0], factors[1], entry.unit, "catalog.dids")
    raise ContextRefusalError("catalog_missing", f"{spec}: only mem: and did: channels are RAM")


def check_catalog(definition: ContextDefinition, catalog: DidMap) -> None:
    """Refuse (``catalog_mismatch``) a ``mem:`` input whose catalog address is
    not the RAM the definition says it reads, and a memory-only timing target
    that does not share one memory window (one service-0x23 read) with the
    coordinate input it declares ``same_read_as``."""
    wrong = []
    for name, decl in definition.inputs.items():
        source, _, channel = decl.channel.partition(":")
        if source != "mem" or decl.relation == "state":
            continue
        entry = next((c for c in catalog.memory if c.name == channel), None)
        if entry is None or entry.address != decl.ram:
            found = "absent" if entry is None else f"0x{entry.address:08X}"
            wrong.append(f"{name}: catalog {found}, declared 0x{decl.ram:08X}")
    memory = {c.name: c for c in catalog.memory}
    for target in definition.timing_trim.memory_only_targets:
        # One service-0x23 read decodes both, or the target's coordinate is not
        # "from the same read" at all (B08.4 option c).
        partner = definition.inputs[target.same_read_as].channel
        pair = [memory.get(spec.partition(":")[2]) for spec in (target.channel, partner)]
        windows = {None if channel is None else channel.window for channel in pair}
        if None in windows or len(windows) != 1:
            wrong.append(
                f"memory_only_targets: {target.channel} and {partner} are not one memory "
                f"window (catalog windows {sorted(str(w) for w in windows)})"
            )
    if wrong:
        raise ContextRefusalError("catalog_mismatch", "; ".join(wrong))


# -- the resolved tables -----------------------------------------------------------


@dataclass(frozen=True)
class ResolvedLookup:
    """One table as the ECM reads it in one slot, with its axes by W08 id."""

    kind: str
    table_id: str
    cells: tuple[tuple[int, ...], ...]
    row_axis_id: str
    column_axis_id: str
    row_axis_raw: tuple[int, ...]
    column_axis_raw: tuple[int, ...]
    row_breakpoints: tuple[int, ...]
    column_breakpoints: tuple[int, ...]
    cell_decode: Decode | None
    row_decode: Decode | None
    column_decode: Decode | None

    def lookup(self, row_value: int, column_value: int) -> LookupResult:
        return lookup_2d(
            self.cells, self.row_breakpoints, self.column_breakpoints, row_value, column_value
        )


@dataclass(frozen=True)
class ResolvedContext:
    """Everything the context reads from one image in one slot."""

    image_sha256: str
    revision: str | None
    program_id: str
    slot: int
    fuel_comp: ResolvedLookup
    trim_a: ResolvedLookup
    trim_b: ResolvedLookup
    #: The fill byte's ceiling: the fill curve at X = 0xFFFF (curves refused
    #: unless non-decreasing, so nothing above it is reachable).
    fill_ceiling: int
    #: The rpm word from which ``k x word`` clamps at 0xFFFF.
    reciprocal_saturation_word: int
    rpm_multiplier: int
    reciprocal_numerator: int

    def provenance(self) -> dict[str, Any]:
        return {
            "image_sha256": self.image_sha256,
            "revision": self.revision,
            "program": self.program_id,
            "slot": self.slot,
            "tables": {
                "fuel_comp": self.fuel_comp.table_id,
                "trim_a": self.trim_a.table_id,
                "trim_b": self.trim_b.table_id,
            },
        }


def _app(exc: ApplicationModelRefusalError) -> ContextRefusalError:
    return ContextRefusalError(exc.reason, exc.message)


def _node_axes(model: ApplicationModel, node_id: str) -> tuple[set[str], str | None, str | None]:
    try:
        node = model.node(node_id)
    except StopIteration as exc:
        raise ContextRefusalError(
            "application_model_mismatch", f"the application model has no node {node_id!r}"
        ) from exc
    tables = {r.rom_map for r in node.rom if r.rom_map is not None}
    rows = next((r.rom_map for r in node.rom if r.role == "rows"), None)
    columns = next((r.rom_map for r in node.rom if r.role == "columns"), None)
    return tables, rows, columns


def _lookup(
    entries: list[dict[str, Any]],
    rom_map: RomMap,
    model: ApplicationModel,
    *,
    kind: str,
    slot: int,
    rows: AxisDecl,
    columns: AxisDecl,
    node_id: str,
) -> ResolvedLookup:
    entry = next((t for t in entries if t["kind"] == kind and t["slot"] == slot), None)
    if entry is None:
        raise ContextRefusalError(
            "table_missing", f"rom_map.yaml places no {kind!r} in slot {slot}"
        )
    if entry["confidence"] != "verified":
        raise ContextRefusalError(
            "table_unverified", f"{entry['id']} in slot {slot} is {entry['confidence']}"
        )
    for role, declared, payload in (
        ("rows", rows, entry["axes"]["rows"]),
        ("columns", columns, entry["axes"]["cols"]),
    ):
        actual = None if payload is None else payload["id"]
        if actual != declared.rom_map_axis:
            raise ContextRefusalError(
                "axis_mismatch",
                f"{entry['id']} {role} axis is {actual!r} in rom_map.yaml; the context "
                f"declares {declared.rom_map_axis!r}",
            )
        if payload["confidence"] != "verified":
            raise ContextRefusalError(
                "axis_unverified", f"{actual} is {payload['confidence']}, not verified"
            )
    tables, node_rows, node_columns = _node_axes(model, node_id)
    if entry["id"] not in tables or (node_rows, node_columns) != (
        rows.rom_map_axis,
        columns.rom_map_axis,
    ):
        raise ContextRefusalError(
            "application_model_mismatch",
            f"node {node_id} reads {sorted(tables)} on rows {node_rows!r} x columns "
            f"{node_columns!r}; the context resolves {entry['id']} on "
            f"{rows.rom_map_axis!r} x {columns.rom_map_axis!r}",
        )
    table = rom_map.table(entry["id"])
    row_axis = rom_map.table(rows.rom_map_axis)
    column_axis = rom_map.table(columns.rom_map_axis)
    row_raw = tuple(int(v) for v in entry["axes"]["rows"]["raw"])
    column_raw = tuple(int(v) for v in entry["axes"]["cols"]["raw"])
    return ResolvedLookup(
        kind=kind,
        table_id=entry["id"],
        cells=tuple(tuple(int(v) for v in row) for row in entry["raw"]),
        row_axis_id=rows.rom_map_axis,
        column_axis_id=columns.rom_map_axis,
        row_axis_raw=row_raw,
        column_axis_raw=column_raw,
        row_breakpoints=tuple(v * rows.compare_multiplier for v in row_raw),
        column_breakpoints=tuple(v * columns.compare_multiplier for v in column_raw),
        cell_decode=table.decode,
        row_decode=row_axis.decode,
        column_decode=column_axis.decode,
    )


def _vector(entries: list[dict[str, Any]], table_id: str) -> tuple[int, ...]:
    entry = next((t for t in entries if t["id"] == table_id), None)
    if entry is None:
        raise ContextRefusalError("table_missing", f"rom_map.yaml declares no {table_id!r}")
    if entry["confidence"] != "verified":
        raise ContextRefusalError("table_unverified", f"{table_id} is {entry['confidence']}")
    return tuple(int(v) for row in entry["raw"] for v in row)


def resolve_context(
    image: bytes,
    rom_map: RomMap,
    definition: ContextDefinition,
    *,
    application_model: tuple[ApplicationModel, str],
    expected_sha256: str,
    slot: int,
) -> ResolvedContext:
    """The context's tables for one slot of a W08 library image. Refuses by
    name: ``image_hash_mismatch``, ``wrong_program`` / ``patch_missing`` /
    ``unmodelled_logic`` (the application model's identity, the calibration
    ID, the program literals), ``table_missing``, ``table_unverified``,
    ``axis_mismatch`` (a declared axis that is not the table's in W08),
    ``axis_unverified``, ``application_model_mismatch`` (the node performing
    the lookup reads other axes) and ``curves_not_monotone``."""
    model, _sha = application_model
    sha = sha256_hex(image)
    if sha != expected_sha256:
        raise ContextRefusalError(
            "image_hash_mismatch", f"the session ran plain image {expected_sha256}; this is {sha}"
        )
    try:
        check_image(image, model)
    except ApplicationModelRefusalError as exc:
        raise _app(exc) from exc
    reason = map_applies(read_calibration_id(image, rom_map), rom_map)
    if reason is not None:
        raise ContextRefusalError("wrong_program", reason)
    literals = check_program_constants(image, rom_map)
    if literals:
        raise ContextRefusalError("wrong_program", "; ".join(literals))
    entries = table_values(image, rom_map)
    fuel = definition.fuel_comp
    trim = definition.timing_trim
    if trim.rows.compare_multiplier != TRIM_RPM_AXIS_SCALE or trim.columns.compare_multiplier != 1:
        raise ContextRefusalError(
            "axis_mismatch",
            f"the trim lookup compares rpm as axis byte x {TRIM_RPM_AXIS_SCALE} and the load "
            "byte directly; the context declares otherwise",
        )
    resolved = {
        name: _lookup(
            entries,
            rom_map,
            model,
            kind=kind,
            slot=slot,
            rows=decl.rows,
            columns=decl.columns,
            node_id=decl.application_node,
        )
        for name, kind, decl in (
            ("fuel_comp", fuel.rom_map_kind, fuel),
            ("trim_a", trim.rom_map_kind_when_clear, trim),
            ("trim_b", trim.rom_map_kind_when_set, trim),
        )
    }
    sat = definition.saturation
    low = _vector(entries, sat.fill_curve_low)
    high = _vector(entries, sat.fill_curve_high)
    (multiplier,) = _vector(entries, sat.rpm_multiplier)
    try:
        numerator = read_program_constant(image, rom_map.program_constant(sat.reciprocal_numerator))
    except KeyError as exc:
        raise ContextRefusalError(
            "table_missing", f"rom_map.yaml declares no program constant {exc}"
        ) from exc
    for name, curve in (("fill_curve_low", low), ("fill_curve_high", high)):
        if any(b < a for a, b in zip(curve, curve[1:], strict=False)):
            raise ContextRefusalError(
                "curves_not_monotone", f"{name} falls: the fill byte's ceiling is not its top"
            )
    if lookup_shift12_q16(low, LOW_CURVE_MULTIPLIER * (X_SPLIT - 1)) > high[0]:
        raise ContextRefusalError(
            "curves_not_monotone", "the low fill curve ends above the high curve's start"
        )
    if multiplier == 0:
        raise ContextRefusalError(
            "unmodelled_logic", "an rpm multiplier of 0 makes the reciprocal term constant 0"
        )
    known = model.program.image(sha)
    return ResolvedContext(
        image_sha256=sha,
        revision=None if known is None else known.revision,
        program_id=model.program.id,
        slot=slot,
        fuel_comp=resolved["fuel_comp"],
        trim_a=resolved["trim_a"],
        trim_b=resolved["trim_b"],
        fill_ceiling=lookup_shift12_q16(high, WORD_MAX - X_SPLIT) >> BYTE_SHIFT,
        reciprocal_saturation_word=-(-WORD_MAX // multiplier),
        rpm_multiplier=multiplier,
        reciprocal_numerator=numerator,
    )


def image_tables(
    image: bytes,
    rom_map: RomMap,
    definition: ContextDefinition,
    *,
    application_model: tuple[ApplicationModel, str],
    expected_sha256: str,
) -> Callable[[int], ResolvedContext]:
    """``slot -> ResolvedContext`` for one library image, cached per slot
    (refusals included: one slot's refusal is not retried every sample)."""
    cache: dict[int, ResolvedContext | ContextRefusalError] = {}

    def for_slot(slot: int) -> ResolvedContext:
        if slot not in cache:
            try:
                cache[slot] = resolve_context(
                    image,
                    rom_map,
                    definition,
                    application_model=application_model,
                    expected_sha256=expected_sha256,
                    slot=slot,
                )
            except ContextRefusalError as exc:
                cache[slot] = exc
        found = cache[slot]
        if isinstance(found, ContextRefusalError):
            raise found
        return found

    return for_slot


# -- applicability and the contract ----------------------------------------------


def lookup_applicability(
    model: ApplicationModel, model_sha256: str, node_id: str, observed_ram: Iterable[int]
) -> dict[str, Any]:
    """What is still unresolved between the image and a lookup, given the RAM
    the context observes. A dependency ends its branch when the application
    model observes it or when it writes RAM the context reads directly (the
    coordinate is observed, so its writer need not be reconstructed). Every
    other ``assumed`` / ``not_modelled`` node on the path is named; a lookup
    is ``conditional`` until there are none."""
    if not any(n.id == node_id for n in model.nodes):
        raise ContextRefusalError(
            "application_model_mismatch", f"the application model has no node {node_id!r}"
        )
    observed = set(observed_ram)
    seen: set[str] = set()
    stack = [node_id]
    cut: list[str] = []
    unresolved: list[str] = []
    while stack:
        node = model.node(stack.pop())
        if node.id in seen:
            continue
        seen.add(node.id)
        if node.id != node_id and (node.twin == "observed" or observed & set(node.writes)):
            cut.append(node.id)
            continue
        if node.unresolved:
            unresolved.append(node.id)
        stack.extend(node.depends_on)
    order = {n.id: i for i, n in enumerate(model.nodes)}
    head = model.node(node_id)
    return {
        "node": node_id,
        "twin": head.twin,
        "program": model.program.id,
        "model_version": model.model_version,
        "model_sha256": model_sha256,
        "observed_by_context": sorted(cut, key=order.__getitem__),
        "unresolved": sorted(unresolved, key=order.__getitem__),
        "status": "conditional" if unresolved else "resolved",
    }


def _input_contract(decl: InputDecl, decode: StoredDecode) -> dict[str, Any]:
    return {
        "channel": decl.channel,
        "ram": f"0x{decl.ram:08X}",
        "feeds": [f"0x{a:08X}" for a in decl.feeds],
        "relation": decl.relation,
        "raw_units": "raw_counts",
        "bits": decl.bits,
        "bit": decl.bit,
        "stored_decode": {"scale": decode.scale, "offset": decode.offset, "source": decode.source},
        "engineering_unit": decode.unit,
        "freshness": {"max_age_s": decl.max_age_s, "clock": ACQUIRED, "status": decl.status},
    }


def _axis_contract(rom_map: RomMap, axis: AxisDecl) -> dict[str, Any]:
    decode = rom_map.table(axis.rom_map_axis).decode
    return {
        "rom_map_axis": axis.rom_map_axis,
        "input": axis.input,
        "compare_multiplier": axis.compare_multiplier,
        "engineering": None if decode is None else {"formula": decode.formula, "unit": decode.unit},
    }


def contract(
    definition: ContextDefinition,
    definition_sha256: str,
    *,
    twin: TwinDefinition,
    application_model: tuple[ApplicationModel, str],
    rom_map: RomMap,
    catalog: DidMap,
    role: Literal["fit", "serve"],
) -> dict[str, Any]:
    """The contract a fit records and a serve checks, from one definition.
    ``role`` is the only field that differs between the two, and the
    ``fingerprint`` excludes it, so a fit and a serve that disagree on
    freshness, skew, units or applicability cannot share a fingerprint."""
    model, model_sha = application_model
    check_catalog(definition, catalog)
    observed = {d.ram for d in definition.inputs.values()} | {
        a for d in definition.inputs.values() for a in d.feeds
    }
    identity = twin.map_identity
    fuel, trim = definition.fuel_comp, definition.timing_trim
    trim_output = applicability(model, model_sha, trim.application_output)
    body: dict[str, Any] = {
        "context_version": definition.context_version,
        "evaluator_version": CONTEXT_EVALUATOR_VERSION,
        "definition_sha256": definition_sha256,
        "interpretation": definition.interpretation,
        "clock": ACQUIRED,
        "forward_fill": (
            "refused: each value is its own acquired read; older than max_age_s is stale"
        ),
        "inputs": {
            name: _input_contract(decl, stored_decode(decl.channel, catalog))
            for name, decl in definition.inputs.items()
        },
        "map_identity": {
            "from": definition.map_identity_from,
            "channels": identity.channels(),
            "max_age_s": identity.max_age_s,
        },
        "lookups": {
            "fuel_comp": {
                "rom_map_kind": fuel.rom_map_kind,
                "rows": _axis_contract(rom_map, fuel.rows),
                "columns": _axis_contract(rom_map, fuel.columns),
                "layout": "row_major",
                "max_acquisition_skew_s": fuel.max_acquisition_skew_s,
                "applicability": {
                    **lookup_applicability(model, model_sha, fuel.application_node, observed),
                    "limits": [limit.model_dump() for limit in fuel.limits],
                },
            },
            "timing_trim": {
                "rom_map_kinds": {
                    "clear": trim.rom_map_kind_when_clear,
                    "set": trim.rom_map_kind_when_set,
                },
                "select": {
                    "input": trim.select_input,
                    "bit": definition.inputs[trim.select_input].bit,
                },
                "rows": _axis_contract(rom_map, trim.rows),
                "columns": _axis_contract(rom_map, trim.columns),
                "layout": "row_major",
                "cell_mask": f"0x{TRIM_VALUE_MASK:02X}",
                "max_acquisition_skew_s": trim.max_acquisition_skew_s,
                "final_timing_channels": list(trim.final_timing_channels),
                "memory_only_targets": {
                    t.channel: {"same_read_as": t.same_read_as} for t in trim.memory_only_targets
                },
                "applicability": {
                    **lookup_applicability(model, model_sha, trim.application_node, observed),
                    "output": trim_output.output,
                    "output_role": model.output(trim_output.output).role,
                    "output_blockers": list(trim_output.blockers),
                    "limits": [limit.model_dump() for limit in trim.limits],
                },
            },
        },
        "saturation": definition.saturation.model_dump(),
        "retained": {
            name: {
                "input": r.input,
                "ram": f"0x{r.ram:08X}",
                "catalogued": r.input is not None,
                "closes_with": r.closes_with,
            }
            for name, r in definition.retained.items()
        },
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return {"role": role, **body, "fingerprint": f"sha256:{digest}"}


# -- the evaluator -----------------------------------------------------------------


@dataclass(frozen=True)
class _Read:
    acquired_at: float
    raw: int | None
    stored: float
    basis: str


@dataclass(frozen=True)
class _Fresh:
    raw: int
    stored: float
    acquired_at: float
    age_s: float


Refusal = dict[str, Any]


def _refused(reason: str, **detail: Any) -> Refusal:
    return {"refused": reason, **detail}


def _hit(hit: AxisHit, axis_raw: Sequence[int], decode: Decode | None) -> dict[str, Any]:
    lower = axis_raw[hit.index]
    return {
        "index": hit.index,
        "fraction_q16": hit.fraction_q16,
        "position": round(hit.position, 6),
        "edge": hit.edge,
        "breakpoint_raw": lower,
        "breakpoint": None if decode is None else decode.apply(lower),
    }


@dataclass
class ContextProvider:
    """One vehicle's context: the definition, the shared rules it reads (twin
    map identity, application model, catalog, W08 map) and the tables of the
    image the data ran under (``tables``: slot -> :class:`ResolvedContext`)."""

    definition: ContextDefinition
    definition_sha256: str
    twin: TwinDefinition
    application_model: tuple[ApplicationModel, str]
    rom_map: RomMap
    catalog: DidMap
    tables: Callable[[int], ResolvedContext]
    _decodes: dict[str, StoredDecode] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        check_catalog(self.definition, self.catalog)
        for decl in self.definition.inputs.values():
            self._decodes[decl.channel] = stored_decode(decl.channel, self.catalog)
        for spec in self.twin.map_identity.channels():
            self._decodes[spec] = stored_decode(spec, self.catalog)

    @classmethod
    def from_profile(
        cls,
        profile_dir: Path,
        *,
        rom_map: RomMap,
        application_model: tuple[ApplicationModel, str],
        tables: Callable[[int], ResolvedContext],
        dids_path: Path = DIDS_YAML,
    ) -> ContextProvider:
        definition, sha = load_context_definition(profile_dir)
        twin, _twin_sha = load_twin_definition(profile_dir)
        return cls(
            definition=definition,
            definition_sha256=sha,
            twin=twin,
            application_model=application_model,
            rom_map=rom_map,
            catalog=load_did_map(dids_path),
            tables=tables,
        )

    def contract(self, role: Literal["fit", "serve"]) -> dict[str, Any]:
        return contract(
            self.definition,
            self.definition_sha256,
            twin=self.twin,
            application_model=self.application_model,
            rom_map=self.rom_map,
            catalog=self.catalog,
            role=role,
        )

    def channels(self) -> list[str]:
        """Every channel the context reads, inputs then map identity."""
        specs = [d.channel for d in self.definition.inputs.values()]
        return specs + [s for s in self.twin.map_identity.channels() if s not in specs]

    def coverage(self, captured: Iterable[str]) -> dict[str, list[str]]:
        """Which inputs a session or capture profile carries. Historical and
        default captures mostly carry none of them; that is reported, never
        assumed away."""
        present = set(captured)
        out: dict[str, list[str]] = {"captured": [], "not_captured": []}
        for name, decl in self.definition.inputs.items():
            out["captured" if decl.channel in present else "not_captured"].append(name)
        return out

    def stream(
        self, *, role: Literal["fit", "serve"], captured: Iterable[str] | None
    ) -> ContextStream:
        return ContextStream(self, role=role, captured=None if captured is None else set(captured))

    def evaluate_frame(self, long: pd.DataFrame, times: Sequence[float]) -> list[dict[str, Any]]:
        """The stored-session (fit) path: replay ``long`` read by read through
        the same stream the live path uses and take the context at each of
        ``times`` (Unix seconds). ``long`` is :func:`src.analysis.channels.
        read_channels` output with ``acquired_time=True``; a resampled wide
        frame is refused (``not_acquisition_timed``), because its cells carry
        a grid time and may be forward-filled."""
        columns = set(map(str, long.columns))
        flagged = sorted(c for c in columns if c.endswith((GAP_SUFFIX, ARRIVED_SUFFIX)))
        if not {"timestamp", "spec", "value"} <= columns or flagged:
            raise ContextRefusalError(
                "not_acquisition_timed",
                "the context takes one row per acquired read (timestamp, spec, value, "
                f"timestamp_basis), never a resampled grid{f' ({flagged[:3]})' if flagged else ''}",
            )
        if "timestamp_basis" not in columns:
            raise ContextRefusalError(
                "clock_unacquired", "reads carry no timestamp_basis; skew cannot be judged"
            )
        wanted = set(self.channels())
        rows = long[long["spec"].isin(wanted)]
        seconds = to_seconds(rows["timestamp"])
        order = np.argsort(seconds, kind="stable")
        specs = rows["spec"].astype(str).to_numpy()[order]
        values = pd.to_numeric(rows["value"], errors="coerce").to_numpy(dtype=float)[order]
        bases = rows["timestamp_basis"].astype(str).to_numpy()[order]
        seconds = seconds[order]
        stream = self.stream(role="fit", captured=set(long["spec"].astype(str)))
        records: list[dict[str, Any] | None] = [None] * len(times)
        k = 0
        for index in sorted(range(len(times)), key=lambda i: float(times[i])):
            t = float(times[index])
            while k < len(seconds) and seconds[k] <= t:
                stream.observe(specs[k], values[k], float(seconds[k]), basis=bases[k])
                k += 1
            records[index] = stream.context_at(t)
        # Returned in the caller's order, each answered from reads at or before it.
        return [r for r in records if r is not None]


class ContextStream:
    """The live (serve) path, and the engine of the stored one: the newest
    acquired read of each channel, and the context as of a query time."""

    def __init__(
        self,
        provider: ContextProvider,
        *,
        role: Literal["fit", "serve"],
        captured: set[str] | None,
    ) -> None:
        self.provider = provider
        self.role = role
        #: Channels the capture carries; ``None`` when not known (a channel
        #: never read is then ``missing``, not ``not_captured``).
        self.captured = captured
        self._latest: dict[str, _Read] = {}
        #: The newest acquisition time held: the stream keeps only the newest
        #: read per channel, so it cannot answer for an earlier instant.
        self._newest: float | None = None
        self._contract = provider.contract(role)

    @property
    def fingerprint(self) -> str:
        return str(self._contract["fingerprint"])

    def observe(self, spec: str, value: float, acquired_at: float, *, basis: str) -> None:
        """One read. Older than the newest held read of ``spec``: ignored."""
        decode = self.provider._decodes.get(spec)
        if decode is None:
            return
        held = self._latest.get(spec)
        if held is not None and acquired_at < held.acquired_at:
            return
        raw = decode.raw(value) if math.isfinite(float(value)) or decode.scale is None else None
        self._latest[spec] = _Read(acquired_at, raw, float(value), basis)
        if self._newest is None or acquired_at > self._newest:
            self._newest = acquired_at

    # -- reads ---------------------------------------------------------------

    def _fresh(self, name: str, spec: str, bits: int, max_age: float, t: float) -> _Fresh | str:
        read = self._latest.get(spec)
        if read is None:
            if self.captured is not None and spec not in self.captured:
                return f"not_captured:{name}"
            return f"missing:{name}"
        if read.basis != ACQUIRED:
            return f"clock_unacquired:{name}"
        age = t - read.acquired_at
        if age > max_age:
            return f"stale:{name}"
        if read.raw is None or not 0 <= read.raw < (1 << bits):
            return f"wrong_units:{name}"
        return _Fresh(read.raw, read.stored, read.acquired_at, round(age, 6))

    def _input(self, name: str, t: float) -> _Fresh | str:
        decl = self.provider.definition.inputs[name]
        return self._fresh(name, decl.channel, decl.bits, decl.max_age_s, t)

    def _flag(self, name: str, fresh: _Fresh) -> bool:
        bit = self.provider.definition.inputs[name].bit
        assert bit is not None
        return bool(fresh.raw >> bit & 1)

    def _engineering(self, name: str, raw: int) -> float | None:
        return self.provider._decodes[self.provider.definition.inputs[name].channel].engineering(
            raw
        )

    @staticmethod
    def _acquisition(reads: Mapping[str, _Fresh]) -> dict[str, Any]:
        times = [r.acquired_at for r in reads.values()]
        return {
            "reads": {
                n: {"acquired_at": r.acquired_at, "age_s": r.age_s} for n, r in reads.items()
            },
            "tuple_skew_s": round(max(times) - min(times), 6),
        }

    # -- identity ------------------------------------------------------------

    def _identity(self, t: float) -> ResolvedContext | Refusal:
        rules: MapIdentity = self.provider.twin.map_identity
        got: dict[str, int] = {}
        for name, spec, bits in (
            ("map_magic", rules.magic.channel, 32),
            ("map_active", rules.active_map.channel, 8),
            ("map_selector", rules.selector.channel, 8),
        ):
            fresh = self._fresh(name, spec, bits, rules.max_age_s, t)
            if isinstance(fresh, str):
                return _refused(fresh)
            got[name] = fresh.raw
        slot = resolve_slot(
            self.provider.twin,
            magic=got["map_magic"],
            active_map=got["map_active"],
            selector=got["map_selector"],
        )
        if slot.slot is None:
            return _refused(f"slot_unresolved:{slot.reason}", detail=slot.detail)
        try:
            return self.provider.tables(slot.slot)
        except ContextRefusalError as exc:
            return _refused(exc.reason, detail=exc.message)

    # -- items ---------------------------------------------------------------

    def _fuel_comp(self, resolved: ResolvedContext, t: float) -> dict[str, Any]:
        decl = self.provider.definition.fuel_comp
        reads: dict[str, _Fresh] = {}
        for name in (decl.rows.input, decl.columns.input):
            fresh = self._input(name, t)
            if isinstance(fresh, str):
                return _refused(fresh)
            reads[name] = fresh
        acquisition = self._acquisition(reads)
        if acquisition["tuple_skew_s"] > decl.max_acquisition_skew_s:
            return _refused("skew:fuel_comp", acquisition=acquisition)
        table = resolved.fuel_comp
        rpm, fill = reads[decl.rows.input].raw, reads[decl.columns.input].raw
        result = table.lookup(rpm, fill)
        value_decode = table.cell_decode
        return {
            "table": table.table_id,
            "rpm_word": rpm,
            "rpm": self._engineering(decl.rows.input, rpm),
            "fill_raw": fill,
            "fill": None if table.column_decode is None else table.column_decode.apply(fill),
            "fill_unit": None if table.column_decode is None else table.column_decode.unit,
            "row": _hit(result.row, table.row_axis_raw, table.row_decode),
            "column": _hit(result.column, table.column_axis_raw, table.column_decode),
            "cells_read": [list(c) for c in result.cells],
            "value_raw": result.value,
            "value": None if value_decode is None else value_decode.apply(result.value),
            "value_unit": None if value_decode is None else value_decode.unit,
            "clamped": {"rows": result.row.edge, "columns": result.column.edge},
            "fill_ceiling": resolved.fill_ceiling,
            "fill_at_ceiling": fill >= resolved.fill_ceiling,
            "reciprocal_saturation_word": resolved.reciprocal_saturation_word,
            "reciprocal_saturation_rpm": self._engineering(
                decl.rows.input, resolved.reciprocal_saturation_word
            ),
            "rpm_reciprocal_saturated": rpm >= resolved.reciprocal_saturation_word,
            "acquisition": acquisition,
        }

    def _timing(self, resolved: ResolvedContext, t: float) -> dict[str, Any]:
        decl = self.provider.definition.timing_trim
        reads: dict[str, _Fresh] = {}
        for name in (decl.rows.input, decl.columns.input, decl.pedal_input, decl.coolant_input):
            fresh = self._input(name, t)
            if isinstance(fresh, str):
                return _refused(fresh)
            reads[name] = fresh
        select = self._input(decl.select_input, t)
        tuple_reads = {k: v for k, v in reads.items() if k != decl.coolant_input}
        if not isinstance(select, str):
            tuple_reads[decl.select_input] = select
        acquisition = self._acquisition(tuple_reads)
        acquisition["reads"][decl.coolant_input] = {
            "acquired_at": reads[decl.coolant_input].acquired_at,
            "age_s": reads[decl.coolant_input].age_s,
        }
        if acquisition["tuple_skew_s"] > decl.max_acquisition_skew_s:
            return _refused("skew:timing_trim", acquisition=acquisition)
        pedal = self._flag(decl.pedal_input, reads[decl.pedal_input])
        high_det = None if isinstance(select, str) else self._flag(decl.select_input, select)
        rpm, x = reads[decl.rows.input].raw, reads[decl.columns.input].raw
        coolant = reads[decl.coolant_input].raw
        a, b = resolved.trim_a, resolved.trim_b
        trims = selected_timing_trim(
            table_a=a.cells,
            table_b=b.cells,
            rpm_axis=a.row_axis_raw,
            load_axis=a.column_axis_raw,
            rpm_word=rpm,
            load_byte=x,
            coolant=coolant,
            pedal_flag=pedal,
            high_det=high_det,
        )
        selected = trims.selected
        if selected.value is None:
            reason = str(selected.reason)
            if reason == "high_det_latch_unknown" and isinstance(select, str):
                reason = select
            return _refused(reason, acquisition=acquisition)
        if coolant <= TRIM_COLD_AT_OR_BELOW:
            table = a
        else:
            table = b if high_det else a
        lookup = selected.value.lookup
        return {
            "table": table.table_id,
            "high_det": high_det,
            "coolant_raw": coolant,
            "pedal_forces_last_column": pedal,
            "rpm_word": rpm,
            "rpm": self._engineering(decl.rows.input, rpm),
            "load_raw": x,
            "load_read_raw": 0xFF if pedal else x,
            "row": _hit(lookup.row, table.row_axis_raw, table.row_decode),
            "column": _hit(lookup.column, table.column_axis_raw, table.column_decode),
            "cells_read": [list(c) for c in lookup.cells],
            "trim": selected.value.trim,
            "trim_flag": selected.value.flag,
            "trim_unit": None if table.cell_decode is None else table.cell_decode.unit,
            "clamped": {"rows": lookup.row.edge, "columns": lookup.column.edge},
            "is_final_timing": False,
            "acquisition": acquisition,
        }

    def _timing_coordinate(self, t: float) -> dict[str, Any]:
        """B08.4 — the coordinate the timing-trim lookup searches (rpm word x
        BFS-X) and the HI DET latch as observed, without the image.

        The coordinate is RAM, so it needs no table, slot or coolant: it stays
        observable where :meth:`_timing` cannot resolve a trim (no map
        identity, no coolant byte, no latch). The latch is reported as read or
        by its refusal (``high_det_refused``), never assumed; when it is fresh
        its read counts toward the tuple skew, as in :meth:`_timing`. Not a
        trim and not final timing."""
        decl = self.provider.definition.timing_trim
        reads: dict[str, _Fresh] = {}
        for name in (decl.rows.input, decl.columns.input):
            fresh = self._input(name, t)
            if isinstance(fresh, str):
                return _refused(fresh)
            reads[name] = fresh
        select = self._input(decl.select_input, t)
        if not isinstance(select, str):
            reads[decl.select_input] = select
        acquisition = self._acquisition(reads)
        if acquisition["tuple_skew_s"] > decl.max_acquisition_skew_s:
            return _refused("skew:timing_coordinate", acquisition=acquisition)
        rpm, x = reads[decl.rows.input].raw, reads[decl.columns.input].raw
        return {
            "rpm_word": rpm,
            "rpm": self._engineering(decl.rows.input, rpm),
            "load_raw": x,
            "high_det": None if isinstance(select, str) else self._flag(decl.select_input, select),
            "high_det_refused": select if isinstance(select, str) else None,
            "acquisition": acquisition,
        }

    def _pedal(self, t: float) -> dict[str, Any]:
        name = self.provider.definition.pedal_enrichment.input
        fresh = self._input(name, t)
        if isinstance(fresh, str):
            return _refused(fresh)
        return {"set": self._flag(name, fresh), "raw": fresh.raw, "age_s": fresh.age_s}

    def _fuel_cut(self, t: float) -> dict[str, Any]:
        decl = self.provider.definition.fuel_cut
        inputs = self.provider.definition.inputs
        held = {
            name: self._latest.get(inputs[name].channel)
            for name in (decl.command_input, decl.sentinel_input)
        }
        present = [(read.acquired_at, name) for name, read in held.items() if read is not None]
        if not present:
            # Neither read: name the command's reason (not_captured / missing).
            return _refused(str(self._input(decl.command_input, t)))
        # The newest read of either channel decides, and only if it is itself
        # fresh and valid: an older read never stands in for a refused newer one.
        _at, newest = max(present)
        fresh = self._input(newest, t)
        if isinstance(fresh, str):
            return _refused(fresh)
        return {
            "active": newest == decl.sentinel_input,
            "age_s": fresh.age_s,
            "acquired_at": fresh.acquired_at,
        }

    def _retained(self, t: float) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, decl in self.provider.definition.retained.items():
            if decl.input is None:
                out[name] = _refused(f"not_catalogued:{name}", ram=f"0x{decl.ram:08X}")
                continue
            fresh = self._input(decl.input, t)
            if isinstance(fresh, str):
                out[name] = _refused(fresh)
            else:
                out[name] = {"raw": fresh.raw, "age_s": fresh.age_s}
        return out

    def context_at(self, t: float) -> dict[str, Any]:
        """The context as of ``t`` (Unix seconds): only reads at or before it,
        each fresh within its own age limit. Every item is a value or a named
        refusal; nothing is defaulted.

        Refuses ``query_before_reads`` for a ``t`` earlier than a read the
        stream already holds: only the newest read per channel is kept, so an
        earlier instant would be answered with reads from its future."""
        if self._newest is not None and t < self._newest:
            raise ContextRefusalError(
                "query_before_reads",
                f"context asked at {t} after a read acquired at {self._newest}; query in "
                "acquisition order (evaluate_frame does)",
            )
        identity = self._identity(t)
        record: dict[str, Any] = {
            "t": t,
            "role": self.role,
            "contract_fingerprint": self.fingerprint,
        }
        if isinstance(identity, dict):
            record["identity"] = identity
            record["fuel_comp"] = _refused(identity["refused"])
            record["timing_trim"] = _refused(identity["refused"])
        else:
            record["identity"] = identity.provenance()
            record["fuel_comp"] = self._fuel_comp(identity, t)
            record["timing_trim"] = self._timing(identity, t)
        record["timing_coordinate"] = self._timing_coordinate(t)
        record["pedal_enrichment"] = self._pedal(t)
        record["fuel_cut"] = self._fuel_cut(t)
        record["retained"] = self._retained(t)
        return record


__all__ = [
    "CONTEXT_EVALUATOR_VERSION",
    "CONTEXT_FILE",
    "ContextDefinition",
    "ContextProvider",
    "ContextRefusalError",
    "ContextStream",
    "ResolvedContext",
    "ResolvedLookup",
    "StoredDecode",
    "check_catalog",
    "contract",
    "image_tables",
    "load_context_definition",
    "lookup_applicability",
    "resolve_context",
    "stored_decode",
]
