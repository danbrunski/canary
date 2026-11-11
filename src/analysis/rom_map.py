"""A vehicle's flash-image map as data: regions, tables, map slots (W08.1).

What is known about an ECU's 1:1 flash image — where the boot code, the
calibration and the application sit, which bytes are erased, where a tuner's
map slots live, which tables are where and how their cells decode — belongs in
the vehicle profile (``<profile>/rom_map.yaml``), never in shared code. Each
fact carries its confidence as a field (``verified`` / ``inferred`` /
``unknown``) and its evidence as required text, so a guess cannot be shown as
a finding: the schema refuses an entry without either.

Read-only: nothing here edits an image. Editing stays behind the known-source
hash guard in :class:`src.analysis.rom_targets.TargetEditor`.

A **program constant** is a literal in the code, not a calibration value: it
belongs to the ECU's program, so a calibration flash never moves or changes
it, while another program may hold anything there (N02.1). Each one carries
the value the decode was made against, so a reader of an image whose program
differs is refused (:func:`check_program_constants`) instead of computing with
a number nobody checked.

Two files describe the same image for this vehicle: this one, and
``rom_targets.yaml`` (the Fuel Targets editor's definition). Every verified
fact the editor relies on — the table offset and slot, both axes and the
checksum words — is checked here against that file at load, so the two cannot
drift apart (``load_rom_map`` refuses by name).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.rom_targets import Checksums, Definition
from src.yaml_load import load_yaml

#: File name of the map definition in a vehicle profile directory.
ROM_MAP_FILE = "rom_map.yaml"

Confidence = Literal["verified", "inferred", "unknown"]
RegionKind = Literal["boot", "calibration", "application", "uprev", "erased", "unknown"]
Dtype = Literal["u8", "be16"]
#: A program literal may be a whole 32-bit word (a ``mov.l`` literal-pool
#: entry); tables are bytes and words only.
ConstantDtype = Literal["u8", "be16", "be32"]

#: Where the ROM Editor's table tree files a table (W10.7). A closed set, so a
#: typo cannot fork a group: extend it when a table needs another.
TableGroup = Literal["fuel", "ignition", "airflow", "emissions"]
#: The longest label the table tree shows, about two lines of it. A longer
#: ``name`` needs a ``short_name``.
TREE_LABEL_MAX = 36

#: Bytes per cell for each dtype.
DTYPE_BYTES: dict[str, int] = {"u8": 1, "be16": 2, "be32": 4}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalibrationIdLocation(_Strict):
    """Where the image stores its calibration ID, as ASCII."""

    offset: int = Field(ge=0)
    length: int = Field(ge=1, le=64)
    evidence: str = Field(min_length=1)


class EraseSector(_Strict):
    """One unit the flash erases, as the loader that wrote this image saw it."""

    index: int = Field(ge=0)
    start: int = Field(ge=0)
    #: Inclusive: the sector's last byte.
    end: int = Field(ge=0)
    confidence: Confidence
    evidence: str = Field(min_length=1)


class Region(_Strict):
    """An absolute span of the image and what it is."""

    name: str = Field(min_length=1)
    start: int = Field(ge=0)
    #: Inclusive: the region's last byte.
    end: int = Field(ge=0)
    kind: RegionKind
    confidence: Confidence
    #: Written by the tuner's software rather than shipped in the factory
    #: image. Required, so every region states it either way.
    added_by_uprev: bool
    evidence: str = Field(min_length=1)


class SlotRegion(_Strict):
    """A span inside every map slot, addressed from the slot's base.

    For a table whose bounds or dimensions are not known yet: the changed
    bytes of a deliberate edit locate it, but rows × cols cannot be declared
    without guessing, so it is a span rather than a table.
    """

    name: str = Field(min_length=1)
    start: int = Field(ge=0)
    #: Inclusive.
    end: int = Field(ge=0)
    kind: RegionKind
    confidence: Confidence
    added_by_uprev: bool
    evidence: str = Field(min_length=1)


class MapSlots(_Strict):
    """``count`` copies of the tuner's map, ``stride`` bytes apart from ``base``.

    Slot numbers are 1-based, as the tuner names them ("Map 1").
    """

    base: int = Field(ge=0)
    stride: int = Field(ge=1)
    count: int = Field(ge=1)
    confidence: Confidence
    evidence: str = Field(min_length=1)
    regions: list[SlotRegion] = Field(default_factory=list)

    def slot_base(self, slot: int) -> int:
        if not 1 <= slot <= self.count:
            raise ValueError(f"map slot {slot} is outside 1..{self.count}")
        return self.base + (slot - 1) * self.stride

    @property
    def end(self) -> int:
        """Inclusive last byte of the last slot."""
        return self.base + self.count * self.stride - 1


class Decode(_Strict):
    """How a raw cell becomes an engineering value.

    ``linear``: ``value = raw × scale + offset``. ``reciprocal``:
    ``value = numerator / raw`` (a raw 0 has no value). A decode that has not
    been confirmed against an independent answer is not declared at all — the
    table is then shown raw.

    ``mask`` keeps only the bits that carry the value, before either formula.
    It exists because the ECM strips a flag bit before using some cells (the
    timing trims' bit 7, which marks 143 of their 256 cells): decoding such a
    cell unmasked reads a flagged cell 128 counts higher than its neighbour.
    The raw cell, flag included, is still reported beside the decoded value.
    """

    kind: Literal["linear", "reciprocal"]
    quantity: str = Field(min_length=1)
    unit: str
    scale: float | None = None
    offset: float = 0.0
    numerator: float | None = None
    mask: int | None = Field(default=None, ge=1, le=0xFFFF)
    confidence: Literal["verified", "inferred"]
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _parameters(self) -> Decode:
        if self.kind == "linear":
            if self.scale is None or self.scale == 0 or self.numerator is not None:
                raise ValueError("a linear decode needs a nonzero scale and no numerator")
        elif self.numerator is None or self.numerator == 0 or self.scale is not None:
            raise ValueError("a reciprocal decode needs a nonzero numerator and no scale")
        return self

    def apply(self, raw: int) -> float | None:
        if self.mask is not None:
            raw &= self.mask
        if self.kind == "linear":
            assert self.scale is not None
            return raw * self.scale + self.offset
        assert self.numerator is not None
        return None if raw == 0 else self.numerator / raw

    @property
    def formula(self) -> str:
        cell = "raw" if self.mask is None else f"(raw & 0x{self.mask:X})"
        if self.kind == "linear":
            offset = f" + {self.offset:g}" if self.offset else ""
            return f"{self.quantity} = {cell} × {self.scale:g}{offset}"
        return f"{self.quantity} = {self.numerator:g} / {cell}"


class TableAxes(_Strict):
    """References to the axis tables, by table ``id``."""

    rows: str | None = None
    cols: str | None = None


class EditorDisagreement(_Strict):
    """How a tuning editor shows a 2-D table's axes, where that disagrees
    with the ECM's code.

    UpRev's Fuel Compensation grid matches memory cell for cell (256/256
    row-major), but its two breakpoint lists are swapped: it labels the rpm
    rows with the load axis's bytes × 50 and the load columns with the rpm
    axis's bytes as "VFR %" (operator check 2026-10-04, d4fee9e). In an
    evidence string nothing showed that to someone reading UpRev beside the
    dashboard; as a field, every view of the table draws the same warning.
    ``positions_match`` is required so the record always says whether a cell
    may be matched between the two by position.
    """

    #: The tool whose display disagrees, e.g. ``UpRev``.
    editor: str = Field(min_length=1)
    #: What the editor labels the table's rows with.
    editor_row_labels: str = Field(min_length=1)
    #: What the editor labels the table's columns with.
    editor_col_labels: str = Field(min_length=1)
    #: Row *i*, column *j* in the editor is the same memory cell as row
    #: *i*, column *j* here.
    positions_match: bool
    #: One sentence: what a reader of the editor gets wrong, and what is safe.
    consequence: str = Field(min_length=1)
    #: How to recover the true breakpoints from the editor's labels.
    translation: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class EditorLabels(_Strict):
    """The labels a tuning editor puts on an axis the code compares as raw
    counts.

    The timing trims' and Target AFR's load axes hold X counts, which the code
    compares byte for byte with the X byte. UpRev labels each count with the
    base fuel schedule it stands for, in ms. That depends on another table: X
    is BFS x 128 / Load Point Scaler, so count 128 sits at the scaler's BFS.
    The labels are therefore declared relative to that table and computed from
    the same image, because a fixed ms scale would go wrong the moment the
    scaler is edited. They are labels, never the axis's decode: edits, traces
    and the simulator keep the code's own units.
    """

    #: The tool that shows these labels, e.g. ``UpRev``.
    editor: str = Field(min_length=1)
    quantity: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    #: ``label = raw / full_scale x`` the decoded value of this 1 x 1 table.
    relative_to: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    #: The raw count whose label is the ``relative_to`` table's value.
    full_scale: int = Field(ge=1)
    confidence: Literal["verified", "inferred"]
    evidence: str = Field(min_length=1)

    def labels(self, raw: list[int], reference: float) -> list[float]:
        return [value / self.full_scale * reference for value in raw]

    @property
    def formula(self) -> str:
        return f"{self.quantity} = raw / {self.full_scale} × {self.relative_to}"


class Table(_Strict):
    """A table at one absolute offset, or at one offset inside each map slot."""

    #: Unique within the file; axes reference tables by it.
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    #: A stable kind shared by copies of one table (``fuel_targets`` in Map 1
    #: and in Maps 2-5), when there is one.
    kind: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    #: Absolute offset; exactly one of ``offset`` and ``slot_offset``.
    offset: int | None = Field(default=None, ge=0)
    #: Offset from each map slot's base.
    slot_offset: int | None = Field(default=None, ge=0)
    #: Which slots carry this entry (default: every slot). Only with
    #: ``slot_offset``.
    slots: list[int] | None = None
    rows: int = Field(ge=1)
    cols: int = Field(ge=1)
    dtype: Dtype
    decode: Decode | None = None
    axes: TableAxes = Field(default_factory=TableAxes)
    confidence: Confidence
    evidence: str = Field(min_length=1)
    #: Where a tuning editor labels this table's axes differently from the
    #: code (:class:`EditorDisagreement`). Only on a table with both axes.
    editor_disagreement: EditorDisagreement | None = None
    #: How a tuning editor labels this axis's breakpoints
    #: (:class:`EditorLabels`). Only on a one-dimensional table.
    editor_labels: EditorLabels | None = None
    #: The ROM Editor tree's group. Required of every table no other table
    #: uses as an axis (:class:`RomMap` checks), since the tree lists them all.
    group: TableGroup | None = None
    #: The tree's label, for a ``name`` longer than :data:`TREE_LABEL_MAX`.
    short_name: str | None = Field(default=None, min_length=1, max_length=TREE_LABEL_MAX)
    #: The ROM Editor takes an edit to this table. Whether the simulator then
    #: reads the edit is the editor's own question (its slot), not the map's;
    #: an exported edit also needs ``confidence: verified``. Never on a table
    #: another uses as an axis (:class:`RomMap` checks): a moved breakpoint
    #: moves every lookup through it.
    editable: bool = False
    #: The raw values an edit may write, inclusive ``[low, high]``, where the
    #: code that reads the cell makes the rest of the dtype's range wrong: a
    #: word the ECM compares signed reads 0x8000 and up as negative, and a
    #: divisor must not be 0. ``make_candidate`` refuses a value outside it
    #: (``value_out_of_range``); nothing clamps.
    raw_range: tuple[int, int] | None = None

    @property
    def tree_label(self) -> str:
        return self.short_name or self.name

    @model_validator(mode="after")
    def _placement(self) -> Table:
        if (self.offset is None) == (self.slot_offset is None):
            raise ValueError(f"table {self.id!r}: give exactly one of offset or slot_offset")
        if self.slots is not None and self.slot_offset is None:
            raise ValueError(f"table {self.id!r}: slots only apply with slot_offset")
        if self.slots is not None and (not self.slots or len(set(self.slots)) != len(self.slots)):
            raise ValueError(f"table {self.id!r}: slots must be a nonempty list without repeats")
        mask = None if self.decode is None else self.decode.mask
        if mask is not None and mask >= 1 << (8 * DTYPE_BYTES[self.dtype]):
            raise ValueError(
                f"table {self.id!r}: mask 0x{mask:X} is wider than a {self.dtype} cell"
            )
        if self.raw_range is not None:
            low, high = self.raw_range
            if not 0 <= low <= high < 1 << (8 * DTYPE_BYTES[self.dtype]):
                raise ValueError(
                    f"table {self.id!r}: raw_range {list(self.raw_range)} must be low <= high "
                    f"inside a {self.dtype} cell"
                )
        if self.editor_disagreement is not None and (
            self.axes.rows is None or self.axes.cols is None
        ):
            raise ValueError(
                f"table {self.id!r}: editor_disagreement describes a 2-D table's row and "
                "column axes, so it needs both axes.rows and axes.cols"
            )
        if self.editor_labels is not None and not self.is_axis:
            raise ValueError(
                f"table {self.id!r}: editor_labels label an axis's breakpoints, so the "
                "table must be one-dimensional"
            )
        return self

    @property
    def size(self) -> int:
        return self.rows * self.cols * DTYPE_BYTES[self.dtype]

    @property
    def is_axis(self) -> bool:
        return self.rows == 1 or self.cols == 1


class ProgramConstant(_Strict):
    """A literal the ECU's code loads, at one absolute offset (N02.1).

    Not a table: nothing edits it and no editor shows it. It is here so a
    reconstruction that needs it reads it from the image *and* checks it is
    the value the decode was made against (``value``), which is how a
    different program is told apart from the decoded one.
    """

    #: Unique within the file, across tables too.
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    offset: int = Field(ge=0)
    dtype: ConstantDtype
    #: The raw value at ``offset`` in the program the decode was made on.
    value: int = Field(ge=0)
    #: Code addresses that load it, so the claim can be re-checked.
    loaded_at: list[int] = Field(min_length=1)
    confidence: Confidence
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _fits(self) -> ProgramConstant:
        if self.value >= 1 << (8 * DTYPE_BYTES[self.dtype]):
            raise ValueError(
                f"program constant {self.id!r}: value 0x{self.value:X} does not fit {self.dtype}"
            )
        return self

    @property
    def size(self) -> int:
        return DTYPE_BYTES[self.dtype]


class RomMap(_Strict):
    """One vehicle's map of its flash image."""

    schema_version: Literal[1]
    image_size: int = Field(gt=0)
    #: Calibration IDs this map describes. An image whose ID (read at
    #: ``calibration_id``) is not listed gets identity and block statistics
    #: but no map.
    calibration_ids: list[str] = Field(min_length=1)
    calibration_id: CalibrationIdLocation
    #: A file of this exact size and leading bytes is a known, undecodable
    #: format; uploads refuse it with ``reason``.
    refused_formats: list[RefusedFormat] = Field(default_factory=list)
    checksums: Checksums
    erase_sectors: list[EraseSector] = Field(default_factory=list)
    regions: list[Region] = Field(default_factory=list)
    map_slots: MapSlots | None = None
    tables: list[Table] = Field(default_factory=list)
    program_constants: list[ProgramConstant] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> RomMap:
        size = self.image_size

        def inside(what: str, start: int, end: int) -> None:
            if start > end:
                raise ValueError(f"{what}: start 0x{start:06X} is after end 0x{end:06X}")
            if end >= size:
                raise ValueError(
                    f"{what}: address 0x{end:06X} is beyond the {size}-byte image "
                    f"(last byte 0x{size - 1:06X})"
                )

        cal = self.calibration_id
        inside("calibration_id", cal.offset, cal.offset + cal.length - 1)
        for word in (
            self.checksums.start,
            self.checksums.sum_offset,
            self.checksums.xor_offset,
            *self.checksums.excluded,
            *self.checksums.markers,
        ):
            inside(f"checksum word 0x{word:06X}", word, word + 3)
        for sector in self.erase_sectors:
            inside(f"erase sector {sector.index}", sector.start, sector.end)
        _refuse_overlaps(
            [(f"erase sector {s.index}", s.start, s.end) for s in self.erase_sectors],
            nesting=False,
        )
        for region in self.regions:
            inside(f"region {region.name!r}", region.start, region.end)
        _refuse_overlaps([(r.name, r.start, r.end) for r in self.regions], nesting=True)

        slots = self.map_slots
        if slots is not None:
            inside("map_slots", slots.base, slots.end)
            for slot_region in slots.regions:
                if slot_region.end >= slots.stride or slot_region.start > slot_region.end:
                    raise ValueError(
                        f"slot region {slot_region.name!r}: +0x{slot_region.start:X}..."
                        f"+0x{slot_region.end:X} does not fit a 0x{slots.stride:X}-byte slot"
                    )
            _refuse_overlaps(
                [(r.name, r.start, r.end) for r in slots.regions], nesting=False, slot_relative=True
            )

        ids = [table.id for table in self.tables]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"table ids must be unique: {duplicates}")
        by_id = {table.id: table for table in self.tables}
        spans: list[tuple[str, int, int]] = []
        for table in self.tables:
            if table.slot_offset is not None:
                if slots is None:
                    raise ValueError(f"table {table.id!r} has a slot_offset but no map_slots")
                if table.slot_offset + table.size > slots.stride:
                    raise ValueError(
                        f"table {table.id!r}: +0x{table.slot_offset:X} plus {table.size} bytes "
                        f"does not fit a 0x{slots.stride:X}-byte slot"
                    )
                for slot in table.slots or range(1, slots.count + 1):
                    if not 1 <= slot <= slots.count:
                        raise ValueError(
                            f"table {table.id!r}: slot {slot} is outside 1..{slots.count}"
                        )
                    start = slots.slot_base(slot) + table.slot_offset
                    spans.append((f"{table.id} (slot {slot})", start, start + table.size - 1))
                _refuse_partial_overlap_with_slot_regions(table, slots)
            else:
                assert table.offset is not None
                start = table.offset
                inside(f"table {table.id!r}", start, start + table.size - 1)
                spans.append((table.id, start, start + table.size - 1))
            for role, ref in (("rows", table.axes.rows), ("cols", table.axes.cols)):
                if ref is None:
                    continue
                axis = by_id.get(ref)
                if axis is None:
                    raise ValueError(f"table {table.id!r}: {role} axis {ref!r} is not a table id")
                if not axis.is_axis or axis.offset is None:
                    raise ValueError(
                        f"table {table.id!r}: {role} axis {ref!r} must be a one-dimensional "
                        "table at an absolute offset"
                    )
                length = table.rows if role == "rows" else table.cols
                if axis.rows * axis.cols != length:
                    raise ValueError(
                        f"table {table.id!r}: {role} axis {ref!r} has "
                        f"{axis.rows * axis.cols} entries for {length} {role}"
                    )
            _check_editor_labels(table, by_id)
        _refuse_overlaps(spans, nesting=False, what="tables")
        self._check_tree_labels()
        axes = self.axis_table_ids()
        editable_axes = sorted(t.id for t in self.tables if t.editable and t.id in axes)
        if editable_axes:
            raise ValueError(
                f"tables {editable_axes} are axes of other tables and cannot be editable: a "
                "moved breakpoint moves every lookup through it"
            )
        constant_ids = [constant.id for constant in self.program_constants]
        clashes = sorted({i for i in constant_ids if constant_ids.count(i) > 1 or i in by_id})
        if clashes:
            raise ValueError(f"program constant ids must be unique across tables too: {clashes}")
        constant_spans: list[tuple[str, int, int]] = []
        for constant in self.program_constants:
            end = constant.offset + constant.size - 1
            inside(f"program constant {constant.id!r}", constant.offset, end)
            constant_spans.append((constant.id, constant.offset, end))
        _refuse_overlaps(spans + constant_spans, nesting=False, what="tables or constants")
        return self

    def axis_table_ids(self) -> set[str]:
        """Ids of the tables another table uses as an axis."""
        return {ref for table in self.tables for ref in (table.axes.rows, table.axes.cols) if ref}

    def _check_tree_labels(self) -> None:
        """Every table the ROM Editor lists has a group and a readable,
        unambiguous label (W10.7). The tree shows one slot's copies plus every
        single-copy table, so labels must differ within each slot."""
        axes = self.axis_table_ids()
        listed = [table for table in self.tables if table.id not in axes]
        for table in listed:
            if table.group is None:
                raise ValueError(
                    f"table {table.id!r}: give a group ({', '.join(get_args(TableGroup))}); "
                    "the ROM Editor lists every table that is not an axis"
                )
            if table.short_name is None and len(table.name) > TREE_LABEL_MAX:
                raise ValueError(
                    f"table {table.id!r}: its name is {len(table.name)} characters, longer "
                    f"than the ROM Editor tree's {TREE_LABEL_MAX}; give a short_name"
                )
        slots = range(1, self.map_slots.count + 1) if self.map_slots else [None]
        for slot in slots:
            shown = [t for t in listed if t.slot_offset is None or slot in self.table_slots(t)]
            labels = [t.tree_label for t in shown]
            clashes = sorted({label for label in labels if labels.count(label) > 1})
            if clashes:
                where = "" if slot is None else f" in Map {slot}"
                raise ValueError(
                    f"tables{where} share a ROM Editor tree label {clashes}; give one a "
                    "short_name that tells them apart"
                )

    def program_constant(self, constant_id: str) -> ProgramConstant:
        for constant in self.program_constants:
            if constant.id == constant_id:
                return constant
        raise KeyError(constant_id)

    def table(self, table_id: str) -> Table:
        for table in self.tables:
            if table.id == table_id:
                return table
        raise KeyError(table_id)

    def table_slots(self, table: Table) -> list[int | None]:
        """The slots ``table`` is placed in, or ``[None]`` for an absolute table."""
        if table.slot_offset is None:
            return [None]
        assert self.map_slots is not None
        return list(table.slots or range(1, self.map_slots.count + 1))

    def table_address(self, table: Table, slot: int | None) -> int:
        """Absolute offset of ``table``'s first cell (in ``slot`` for a slot table)."""
        if table.slot_offset is None:
            assert table.offset is not None
            return table.offset
        if self.map_slots is None or slot is None:
            raise ValueError(f"table {table.id!r} needs a map slot")
        return self.map_slots.slot_base(slot) + table.slot_offset


class RefusedFormat(_Strict):
    """A file shape that is recognised and refused by name."""

    name: str = Field(min_length=1)
    size: int = Field(gt=0)
    #: Leading bytes, as hex.
    prefix_hex: str = Field(pattern=r"^([0-9a-f]{2})+$")
    reason: str = Field(min_length=1)

    def matches(self, data: bytes) -> bool:
        return len(data) == self.size and data.startswith(bytes.fromhex(self.prefix_hex))


RomMap.model_rebuild()


def _check_editor_labels(table: Table, by_id: dict[str, Table]) -> None:
    """``editor_labels`` name a 1 x 1 table at an absolute offset whose decode
    is in the labels' unit: the one value they are computed from."""
    labels = table.editor_labels
    if labels is None:
        return
    reference = by_id.get(labels.relative_to)
    if reference is None:
        raise ValueError(
            f"table {table.id!r}: editor_labels.relative_to {labels.relative_to!r} "
            "is not a table id"
        )
    if reference.rows * reference.cols != 1 or reference.offset is None:
        raise ValueError(
            f"table {table.id!r}: editor_labels.relative_to {labels.relative_to!r} must be a "
            "1 x 1 table at an absolute offset"
        )
    if reference.decode is None or reference.decode.unit != labels.unit:
        found = "no decode" if reference.decode is None else repr(reference.decode.unit)
        raise ValueError(
            f"table {table.id!r}: editor_labels are in {labels.unit!r}, but "
            f"{labels.relative_to!r} has {found}"
        )


def _refuse_overlaps(
    spans: list[tuple[str, int, int]],
    *,
    nesting: bool,
    what: str | None = None,
    slot_relative: bool = False,
) -> None:
    """Refuse two spans that share a byte. ``nesting`` allows one inside the other."""
    ordered = sorted(spans, key=lambda span: (span[1], -span[2]))
    for index, (name, start, end) in enumerate(ordered):
        for other, other_start, other_end in ordered[index + 1 :]:
            if other_start > end:
                break
            if nesting and other_end <= end:
                continue  # wholly inside: a sub-region
            label = what or ("slot regions" if slot_relative else "spans")
            raise ValueError(
                f"overlapping {label}: {name!r} (0x{start:06X}-0x{end:06X}) and {other!r} "
                f"(0x{other_start:06X}-0x{other_end:06X})"
            )


def _refuse_partial_overlap_with_slot_regions(table: Table, slots: MapSlots) -> None:
    """A slot table and a slot span are disjoint, or one contains the other."""
    assert table.slot_offset is not None
    start, end = table.slot_offset, table.slot_offset + table.size - 1
    for region in slots.regions:
        if region.end < start or region.start > end:
            continue
        if (region.start <= start and end <= region.end) or (
            start <= region.start and region.end <= end
        ):
            continue
        raise ValueError(
            f"table {table.id!r} (+0x{start:X}-+0x{end:X}) partly overlaps slot region "
            f"{region.name!r} (+0x{region.start:X}-+0x{region.end:X})"
        )


def read_program_constant(image: bytes, constant: ProgramConstant) -> int:
    """The raw value at ``constant``'s offset in ``image`` (big-endian)."""
    end = constant.offset + constant.size
    if end > len(image):
        raise ValueError(f"program constant {constant.id!r} lies past the end of the image")
    return int.from_bytes(image[constant.offset : end], "big")


def check_program_constants(image: bytes, rom_map: RomMap) -> list[str]:
    """Each program constant whose value in ``image`` is not the decoded one.

    Empty when the image holds every declared literal; otherwise one line per
    mismatch naming the constant, its offset and both values. A mismatch means
    another program, whose memory map and arithmetic nobody has checked.
    """
    problems = []
    for constant in rom_map.program_constants:
        found = read_program_constant(image, constant)
        if found != constant.value:
            problems.append(
                f"{constant.id} at 0x{constant.offset:06X} holds 0x{found:X}, not the decoded "
                f"0x{constant.value:X}"
            )
    return problems


def check_against_targets(rom_map: RomMap, targets: Definition) -> None:
    """Refuse a map that disagrees with the Fuel Targets editor's definition.

    The editor's offsets are verified facts (``rom_targets.yaml``): the table
    at ``table_offset`` for ``map_slot``, both axes, the image size and the
    checksum words. The map must place a ``fuel_targets`` table, of the same
    dimensions the editor reads, exactly there.
    """
    problems: list[str] = []
    if rom_map.image_size != targets.image_size:
        problems.append(f"image_size {rom_map.image_size} != {targets.image_size}")
    if rom_map.checksums != targets.checksums:
        problems.append("checksums differ")
    placed = [
        (table, slot)
        for table in rom_map.tables
        if table.kind == targets.table
        for slot in rom_map.table_slots(table)
        if slot == targets.map_slot
    ]
    if len(placed) != 1:
        problems.append(
            f"expected one {targets.table!r} table in slot {targets.map_slot}, found {len(placed)}"
        )
    else:
        table, slot = placed[0]
        address = rom_map.table_address(table, slot)
        if address != targets.table_offset:
            problems.append(
                f"{table.id} slot {slot} at 0x{address:06X}, rom_targets.yaml says "
                f"0x{targets.table_offset:06X}"
            )
        if table.dtype != "u8":
            problems.append(f"{table.id} dtype {table.dtype}, the editor reads u8 cells")
        for role, expected in (
            ("rows", targets.rpm_axis_offset),
            ("cols", targets.load_axis_offset),
        ):
            ref = getattr(table.axes, role)
            axis = None if ref is None else rom_map.table(ref)
            if axis is None or axis.offset != expected:
                got = "none" if axis is None or axis.offset is None else f"0x{axis.offset:06X}"
                problems.append(
                    f"{table.id} {role} axis at {got}, rom_targets.yaml says 0x{expected:06X}"
                )
        decode = table.decode
        if (
            decode is None
            or decode.kind != "reciprocal"
            or decode.numerator != targets.afr_numerator
        ):
            problems.append(f"{table.id} decode must be {targets.afr_numerator} / raw")
    if problems:
        raise ValueError(f"{ROM_MAP_FILE} disagrees with rom_targets.yaml: " + "; ".join(problems))


def rom_map_sha256(profile_dir: Path) -> str:
    """sha256 of ``<profile_dir>/rom_map.yaml`` as stored: which map a result
    was made with, so a later edit to the map is visible against it."""
    return hashlib.sha256((profile_dir / ROM_MAP_FILE).read_bytes()).hexdigest()


def load_rom_map(profile_dir: Path) -> RomMap:
    """``<profile_dir>/rom_map.yaml``, validated, and cross-checked against
    ``rom_targets.yaml`` when that file is beside it.

    Raises ``FileNotFoundError`` when the vehicle has no map definition and
    ``ValueError`` (naming the problem) when it is invalid.
    """
    path = profile_dir / ROM_MAP_FILE
    rom_map = RomMap.model_validate(load_yaml(path.read_text()))
    targets_path = profile_dir / "rom_targets.yaml"
    if targets_path.exists():
        targets = Definition.model_validate(load_yaml(targets_path.read_text()))
        check_against_targets(rom_map, targets)
    return rom_map


__all__ = [
    "DTYPE_BYTES",
    "ROM_MAP_FILE",
    "CalibrationIdLocation",
    "Confidence",
    "ConstantDtype",
    "Decode",
    "EditorDisagreement",
    "EditorLabels",
    "EraseSector",
    "MapSlots",
    "ProgramConstant",
    "RefusedFormat",
    "Region",
    "RegionKind",
    "RomMap",
    "SlotRegion",
    "Table",
    "TableAxes",
    "check_against_targets",
    "check_program_constants",
    "load_rom_map",
    "read_program_constant",
    "rom_map_sha256",
]
