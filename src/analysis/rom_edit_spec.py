"""What the operator writes to edit a ROM for the simulator (E08.1).

An edit is reviewable data, not a script: a YAML spec names a base image, a
label and a list of edits, and resolves to :class:`src.analysis.rom_effects.
CellEdit`\\ s that :func:`~src.analysis.rom_effects.make_candidate` applies.
Nothing here writes a cell itself.

.. code-block:: yaml

    schema_version: 1
    base: {revision: ZS44A-20261004}       # or {file: x.OsirisROM, sha256: <plain>}
    label: idle 2 % richer
    edits:
      - table: fuel_compensation
        slot: 1
        region: {rpm: [600, 1000], units: {rpm: rpm}}
        scale: 1.02
        rounding: nearest

The second route, :func:`load_candidate_file`, takes an image already edited
elsewhere (saved from UpRev) and gives the same provenance shape: the changed
cells and :func:`~src.analysis.rom_effects.compare_roms`'s changed ranges.

What it makes impossible rather than documented:

- **A region is in the table's own axis units, resolved through that image's
  axes.** The rpm axis is the one whose decode is ``rpm`` -- never "the rows",
  so UpRev's swapped Fuel Comp labels (rows ``fuel_comp_engine_speed_axis``,
  columns ``fuel_comp_flow_axis`` in ``rom_map.yaml``) cannot select the
  transposed cells. ``units`` is required for every bounded axis and must
  equal that axis's decode unit (``raw`` when it has none), so a range
  written in raw counts is refused instead of read as percent.
- **A scale has no default rounding** (``rounding_undeclared``) and is exact
  decimal arithmetic on the factor as written (``1.1 x 100`` is 110, never
  float's 110.00000000000001 rounded up to 111). ``nearest`` rounds a half
  up.
- **Nothing is clamped.** A value outside the cell's dtype is
  ``make_candidate``'s ``value_out_of_range``.
- **Composition order is not expressible:** two edits selecting one cell are
  ``duplicate_edit``. An edit that leaves every selected cell at its old value
  is ``no_op_edit``, naming the cells.
- **A masked cell keeps its other bits.** The timing trims carry a flag bit
  the ECM strips before using the value (``Decode.mask`` 0x7F): ``add_raw``
  and ``scale`` act on ``raw & mask`` and keep ``raw & ~mask``, refusing a
  result outside ``0..mask`` (``value_out_of_range``, never carried into the
  flag). A ``set_raw`` that would change a bit outside the mask is
  ``masked_bits_changed``. A mask that is not contiguous low bits refuses
  arithmetic (``arithmetic_on_masked_cells``).
- **A spec cannot be applied to another base:** :func:`build_candidate`
  refuses a base image that is not the one the spec names
  (``base_mismatch``).

Every refusal is an :class:`EditSpecRefusalError` (a
:class:`~src.analysis.rom_effects.RomEffectRefusalError`) whose ``reason``
names it. Nothing here, or anything it imports, can reach the ECU: see
``tests/test_rom_edit_spec.py``'s clean-process import test.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

from src.analysis.ecu_application import ApplicationModel
from src.analysis.fuel_comp_rom import FuelCompRomRefusalError, RevisionImageResolver
from src.analysis.rom_effects import (
    Candidate,
    CellEdit,
    EffectReport,
    RomEffectRefusalError,
    compare_roms,
    make_candidate,
)
from src.analysis.rom_image import ImageFormatError, compare_images, load_image, sha256_hex
from src.analysis.rom_map import DTYPE_BYTES, RomMap, Table
from src.yaml_load import load_yaml

#: Bump when resolution or the provenance changes meaning.
TOOL_VERSION = "1"
TOOL = "rom_edit_spec"

Rounding = Literal["nearest", "floor", "ceil"]
AxisName = Literal["rpm", "load"]
Route = Literal["edit_spec", "candidate_file"]

#: The unit of an axis without a decode: its bytes as stored.
RAW_UNIT = "raw"

_ROUNDING = {"nearest": ROUND_HALF_UP, "floor": ROUND_FLOOR, "ceil": ROUND_CEILING}

#: Reasons the schema itself raises; any other validation failure is
#: ``spec_invalid``.
SCHEMA_REASONS = frozenset(
    {
        "base_ambiguous",
        "base_sha256_required",
        "no_edits",
        "selector_ambiguous",
        "operation_ambiguous",
        "rounding_undeclared",
        "rounding_without_scale",
        "region_unbounded",
        "region_inverted",
        "unit_undeclared",
    }
)


class EditSpecRefusalError(RomEffectRefusalError):
    """An edit spec, base or candidate file the simulator cannot use; ``reason`` names why."""


def _refuse(reason: str, message: str) -> PydanticCustomError:
    return PydanticCustomError(reason, message)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BaseRef(_Strict):
    """Exactly one of a ``tune_revisions.yaml`` revision (resolved by plain
    sha256 through the registry) or a file with its plain-image sha256."""

    revision: str | None = Field(default=None, min_length=1)
    #: ``.OsirisROM`` or plain image; relative to the spec's directory.
    file: str | None = Field(default=None, min_length=1)
    #: sha256 of the plain 1:1 image (the decoded ``ROMData``), not the container.
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _one(self) -> BaseRef:
        if (self.revision is None) == (self.file is None):
            raise _refuse("base_ambiguous", "base names exactly one of revision or file")
        if self.file is not None and self.sha256 is None:
            raise _refuse(
                "base_sha256_required",
                "a base file needs the sha256 of its plain image, so the spec names its bytes",
            )
        if self.revision is not None and self.sha256 is not None:
            raise _refuse(
                "base_ambiguous",
                "a revision's image hash is the registry's; give sha256 only with file",
            )
        return self


class Region(_Strict):
    """Cells whose breakpoints lie inside ``[lo, hi]`` (inclusive) on each
    bounded axis, in that axis's own units, which ``units`` restates."""

    rpm: tuple[float, float] | None = None
    load: tuple[float, float] | None = None
    #: The unit of each bounded axis, as its decode in ``rom_map.yaml`` names
    #: it (``raw`` for an axis without one). Required: a bound in the wrong
    #: unit is refused, not read in the right one.
    units: dict[AxisName, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bounds(self) -> Region:
        bounded = {name for name in ("rpm", "load") if getattr(self, name) is not None}
        if not bounded:
            raise _refuse("region_unbounded", "a region bounds rpm, load or both")
        for name in sorted(bounded):
            lo, hi = getattr(self, name)
            if lo > hi:
                raise _refuse("region_inverted", f"region {name} [{lo}, {hi}] has lo above hi")
        if set(self.units) != bounded:
            raise _refuse(
                "unit_undeclared",
                f"region units must name exactly the bounded axes {sorted(bounded)}, "
                f"got {sorted(self.units)}",
            )
        return self

    def bounds(self) -> dict[AxisName, tuple[float, float]]:
        out: dict[AxisName, tuple[float, float]] = {}
        if self.rpm is not None:
            out["rpm"] = self.rpm
        if self.load is not None:
            out["load"] = self.load
        return out


class Edit(_Strict):
    """One table, one selector, one operation."""

    #: A ``rom_map.yaml`` table id.
    table: str = Field(min_length=1)
    #: Required for a slot table, refused for an absolute one.
    slot: StrictInt | None = None
    cells: list[tuple[StrictInt, StrictInt]] | None = Field(default=None, min_length=1)
    all: Literal[True] | None = None
    region: Region | None = None
    set_raw: StrictInt | None = None
    add_raw: StrictInt | None = None
    scale: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    rounding: Rounding | None = None

    @model_validator(mode="after")
    def _one_each(self) -> Edit:
        selectors = [s for s in ("cells", "all", "region") if getattr(self, s) is not None]
        if len(selectors) != 1:
            raise _refuse(
                "selector_ambiguous",
                f"edit of {self.table} needs exactly one of cells, all or region, got {selectors}",
            )
        operations = [o for o in ("set_raw", "add_raw", "scale") if getattr(self, o) is not None]
        if len(operations) != 1:
            raise _refuse(
                "operation_ambiguous",
                f"edit of {self.table} needs exactly one of set_raw, add_raw or scale, "
                f"got {operations}",
            )
        if self.scale is not None and self.rounding is None:
            raise _refuse(
                "rounding_undeclared",
                f"a scale of {self.table} needs rounding: nearest, floor or ceil (no default)",
            )
        if self.scale is None and self.rounding is not None:
            raise _refuse("rounding_without_scale", "rounding applies to a scale only")
        return self

    @property
    def selector(self) -> str:
        return next(s for s in ("cells", "all", "region") if getattr(self, s) is not None)

    @property
    def operation(self) -> str:
        return next(o for o in ("set_raw", "add_raw", "scale") if getattr(self, o) is not None)


class EditSpec(_Strict):
    schema_version: Literal[1]
    base: BaseRef
    label: str = Field(min_length=1)
    edits: list[Edit]

    @model_validator(mode="after")
    def _some(self) -> EditSpec:
        if not self.edits:
            raise _refuse("no_edits", "a spec needs at least one edit")
        return self

    def sha256(self) -> str:
        """Over the validated content: comments and layout do not change it."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def parse_edit_spec(data: Any) -> EditSpec:
    """Validate ``data`` (a parsed YAML mapping), refusing by name."""
    if not isinstance(data, Mapping):
        raise EditSpecRefusalError("spec_invalid", "an edit spec is a mapping")
    try:
        return EditSpec.model_validate(dict(data))
    except ValidationError as exc:
        named = [e["type"] for e in exc.errors() if e["type"] in SCHEMA_REASONS]
        raise EditSpecRefusalError(named[0] if named else "spec_invalid", str(exc)) from exc


def load_edit_spec(path: Path) -> EditSpec:
    """``path``'s YAML as an :class:`EditSpec`."""
    try:
        data = load_yaml(Path(path).read_text())
    except yaml.YAMLError as exc:
        raise EditSpecRefusalError("spec_invalid", f"{path}: {exc}") from exc
    return parse_edit_spec(data)


# -- the base image -------------------------------------------------------------------------


@dataclass(frozen=True)
class BaseImage:
    """A hash-checked plain image and what it is."""

    image: bytes
    #: ``source`` (revision|file), ``revision``, ``file`` (name only),
    #: ``image_sha256``, ``image_format``, ``container_sha256``.
    identity: dict[str, Any]

    @property
    def image_sha256(self) -> str:
        return sha256_hex(self.image)

    @property
    def label(self) -> str:
        return str(self.identity["revision"] or self.identity["file"])


def _load_file(path: Path, rom_map: RomMap) -> tuple[bytes, str, str | None]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise EditSpecRefusalError("image_not_found", f"{path}: {exc}") from exc
    try:
        loaded = load_image(data, rom_map)
    except ImageFormatError as exc:
        # The UpRev backup refusal keeps its format name in the message.
        raise EditSpecRefusalError("image_unreadable", f"{path.name}: {exc}") from exc
    return loaded.image, loaded.format, loaded.container_sha256


def resolve_base(
    base: BaseRef,
    rom_map: RomMap,
    *,
    spec_dir: Path,
    resolver: RevisionImageResolver | None = None,
) -> BaseImage:
    """The image ``base`` names, hash-checked.

    A revision goes through ``resolver`` (its ``rom_file`` in the caller's ROM
    directories, or a named file by plain sha256), whose refusals keep their
    reasons (``revision_unknown``, ``no_surviving_image``, ``image_not_found``,
    ``image_hash_mismatch``, ``wrong_program`` ...). A file is read relative to
    ``spec_dir`` through :func:`load_image`; its plain sha256 must be the
    spec's (``image_hash_mismatch``)."""
    if base.revision is not None:
        if resolver is None:
            raise EditSpecRefusalError(
                "revision_resolver_missing",
                f"base revision {base.revision} needs the ROM directories to find its image",
            )
        try:
            calibration = resolver.resolve(base.revision)
            image = resolver.image(base.revision)
        except FuelCompRomRefusalError as exc:
            raise EditSpecRefusalError(exc.reason, exc.message) from exc
        return BaseImage(
            image=image,
            identity={
                "source": "revision",
                "revision": base.revision,
                "file": calibration.source_path.name,
                "image_sha256": calibration.image_sha256,
                "image_format": calibration.image_format,
                "container_sha256": None,
            },
        )
    assert base.file is not None and base.sha256 is not None
    path = Path(base.file)
    path = path if path.is_absolute() else Path(spec_dir) / path
    image, fmt, container = _load_file(path, rom_map)
    digest = sha256_hex(image)
    if digest != base.sha256:
        container_note = (
            " (that is the container's hash: give the plain image's)"
            if container == base.sha256
            else ""
        )
        raise EditSpecRefusalError(
            "image_hash_mismatch",
            f"{path.name} is plain image {digest}, not the spec's {base.sha256}{container_note}",
        )
    return BaseImage(
        image=image,
        identity={
            "source": "file",
            "revision": None,
            "file": path.name,
            "image_sha256": digest,
            "image_format": fmt,
            "container_sha256": container,
        },
    )


# -- resolving edits ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Axis:
    role: Literal["rows", "cols"]
    id: str
    values: tuple[float, ...]
    unit: str


@dataclass
class _Resolution:
    cells: list[CellEdit] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)


def _table(rom_map: RomMap, edit: Edit) -> Table:
    try:
        table = rom_map.table(edit.table)
    except KeyError:
        raise EditSpecRefusalError(
            "table_unknown",
            f"{edit.table!r} is not a mapped table: unmapped bytes are never edited",
        ) from None
    if (table.slot_offset is None) != (edit.slot is None) or (
        edit.slot is not None and edit.slot not in rom_map.table_slots(table)
    ):
        raise EditSpecRefusalError(
            "slot_invalid", f"{table.id} slot {edit.slot} is not a placement of it"
        )
    return table


def _read_cell(
    image: bytes, rom_map: RomMap, table: Table, slot: int | None, row: int, col: int
) -> tuple[int, int]:
    """``(address, raw)`` of one cell (a read; :func:`make_candidate` does every write)."""
    width = DTYPE_BYTES[table.dtype]
    address = rom_map.table_address(table, slot) + (row * table.cols + col) * width
    return address, int.from_bytes(image[address : address + width], "big")


def _axes(image: bytes, rom_map: RomMap, table: Table) -> dict[AxisName, _Axis]:
    """The table's rpm axis (the one whose decode quantity is ``rpm``) and its
    load axis (the other one), read from ``image``. Refuses an axis that is not
    verified, in placement or decode (``axis_unverified``)."""
    found: list[_Axis] = []
    roles: tuple[Literal["rows", "cols"], ...] = ("rows", "cols")
    for role in roles:
        ref = getattr(table.axes, role)
        if ref is None:
            continue
        axis = rom_map.table(ref)
        decode = axis.decode
        unverified_decode = decode is not None and decode.confidence != "verified"
        if axis.confidence != "verified" or unverified_decode:
            raise EditSpecRefusalError(
                "axis_unverified",
                f"{table.id}'s {role} axis {axis.id} is not verified in rom_map.yaml "
                f"(placement {axis.confidence}"
                + ("" if decode is None else f", decode {decode.confidence}")
                + "): a region cannot be resolved through it",
            )
        start = rom_map.table_address(axis, None)
        width = DTYPE_BYTES[axis.dtype]
        raw = [
            int.from_bytes(image[start + i * width : start + (i + 1) * width], "big")
            for i in range(axis.rows * axis.cols)
        ]
        values = tuple(_breakpoint(axis, r) for r in raw)
        found.append(_Axis(role, axis.id, values, RAW_UNIT if decode is None else decode.unit))
    rpm = [a for a in found if _is_rpm(rom_map, a.id)]
    out: dict[AxisName, _Axis] = {}
    if len(rpm) == 1:
        out["rpm"] = rpm[0]
        others = [a for a in found if a is not rpm[0]]
        if len(others) == 1:
            out["load"] = others[0]
    return out


def _breakpoint(axis: Table, raw: int) -> float:
    """The axis entry in its own units; a reciprocal of raw 0 has none, so it
    is NaN and lies inside no bound."""
    if axis.decode is None:
        return float(raw)
    value = axis.decode.apply(raw)
    return float("nan") if value is None else value


def _is_rpm(rom_map: RomMap, axis_id: str) -> bool:
    decode = rom_map.table(axis_id).decode
    return decode is not None and decode.quantity == "rpm"


def _inside(value: float, lo: float, hi: float) -> bool:
    """Inclusive, with a relative tolerance so a breakpoint written exactly
    (``25`` % for a raw 56 at ``100/224``) is not lost to float rounding."""
    tolerance = 1e-9 * max(1.0, abs(value))
    return lo - tolerance <= value <= hi + tolerance


def _region_cells(
    image: bytes, rom_map: RomMap, table: Table, region: Region
) -> tuple[list[tuple[int, int]], dict[str, Any]]:
    axes = _axes(image, rom_map, table)
    rows = list(range(table.rows))
    cols = list(range(table.cols))
    resolved: dict[str, Any] = {}
    for name, (lo, hi) in region.bounds().items():
        axis = axes.get(name)
        if axis is None:
            raise EditSpecRefusalError(
                "axis_not_found",
                f"{table.id} has no {name} axis to bound (axes {table.axes.rows} x "
                f"{table.axes.cols}; the rpm axis is the one whose decode is rpm, load the other)",
            )
        if region.units[name] != axis.unit:
            raise EditSpecRefusalError(
                "unit_mismatch",
                f"region {name} is in {region.units[name]!r}; {axis.id} is in {axis.unit!r}",
            )
        picked = [i for i, v in enumerate(axis.values) if _inside(v, lo, hi)]
        if axis.role == "rows":
            rows = [r for r in rows if r in picked]
        else:
            cols = [c for c in cols if c in picked]
        resolved[name] = {
            "axis": axis.id,
            "role": axis.role,
            "unit": axis.unit,
            "bounds": [lo, hi],
            "indices": picked,
            "breakpoints": [axis.values[i] for i in picked],
        }
    cells = [(r, c) for r in rows for c in cols]
    if not cells:
        raise EditSpecRefusalError(
            "region_empty",
            f"region on {table.id} selects no cell: "
            + "; ".join(
                f"{name} [{v['bounds'][0]}, {v['bounds'][1]}] {v['unit']} holds "
                f"{len(v['indices'])} breakpoint(s)"
                for name, v in resolved.items()
            ),
        )
    return cells, resolved


def _selected(
    image: bytes, rom_map: RomMap, table: Table, edit: Edit
) -> tuple[list[tuple[int, int]], dict[str, Any] | None]:
    if edit.all is not None:
        return [(r, c) for r in range(table.rows) for c in range(table.cols)], None
    if edit.region is not None:
        return _region_cells(image, rom_map, table, edit.region)
    assert edit.cells is not None
    cells = [tuple(c) for c in edit.cells]
    for row, col in cells:
        if not (0 <= row < table.rows and 0 <= col < table.cols):
            raise EditSpecRefusalError(
                "cell_outside_table", f"{table.id} has no row {row} column {col}"
            )
    repeated = sorted({c for c in cells if cells.count(c) > 1})
    if repeated:
        raise EditSpecRefusalError(
            "duplicate_edit", f"{table.id} cells {repeated} listed twice in one edit"
        )
    return [(int(r), int(c)) for r, c in cells], None


def _arithmetic(edit: Edit, value: int) -> int:
    if edit.add_raw is not None:
        return value + edit.add_raw
    assert edit.scale is not None and edit.rounding is not None
    # The factor as written (repr is the shortest round-trip string), times
    # an integer, is exact in decimal: rounding sees no float error.
    exact = Decimal(value) * Decimal(repr(edit.scale))
    return int(exact.to_integral_value(rounding=_ROUNDING[edit.rounding]))


def _apply(edit: Edit, table: Table, old: int, where: str) -> int:
    """The cell's new raw value. On a masked table (``Decode.mask``, e.g. the
    timing trims' bit 7 flag) arithmetic moves only the value bits and keeps
    every other bit; ``set_raw`` must leave those bits as they are."""
    mask = None if table.decode is None else table.decode.mask
    if mask is None:
        return edit.set_raw if edit.set_raw is not None else _arithmetic(edit, old)
    flags = old & ~mask
    if edit.set_raw is not None:
        if edit.set_raw & ~mask != flags:
            raise EditSpecRefusalError(
                "masked_bits_changed",
                f"set_raw {edit.set_raw} (0x{edit.set_raw:X}) on {where} changes bits outside "
                f"{table.id}'s value mask 0x{mask:X}: the cell holds 0x{old:X}, whose "
                f"bits 0x{flags:X} the ECM reads separately. Use add_raw or scale, which move "
                "the value bits only",
            )
        return edit.set_raw
    value = _arithmetic(edit, old & mask)
    if not 0 <= value <= mask:
        raise EditSpecRefusalError(
            "value_out_of_range",
            f"{edit.operation} on {where} takes its masked value {old & mask} to {value}, "
            f"outside 0..{mask} (mask 0x{mask:X}): never clamped, and never carried into the "
            "flag bits",
        )
    return flags | value


def _resolve(spec: EditSpec, rom_map: RomMap, image: bytes) -> _Resolution:
    out = _Resolution()
    owner: dict[int, int] = {}
    for index, edit in enumerate(spec.edits):
        table = _table(rom_map, edit)
        mask = None if table.decode is None else table.decode.mask
        if mask is not None and edit.set_raw is None and mask & (mask + 1):
            # Only a low contiguous mask (0x7F) is a plain number in the low
            # bits; arithmetic across a gapped mask has no meaning.
            raise EditSpecRefusalError(
                "arithmetic_on_masked_cells",
                f"{table.id}'s value mask 0x{mask:X} is not contiguous low bits, so "
                f"{edit.operation} has no defined result; state each cell with set_raw",
            )
        cells, region = _selected(image, rom_map, table, edit)
        changed: list[dict[str, Any]] = []
        unchanged: list[dict[str, Any]] = []
        for row, col in cells:
            address, old = _read_cell(image, rom_map, table, edit.slot, row, col)
            if address in owner:
                raise EditSpecRefusalError(
                    "duplicate_edit",
                    f"{table.id} row {row} column {col} is selected by edits {owner[address]} "
                    f"and {index}: composition order is not something a spec can express",
                )
            owner[address] = index
            new = _apply(edit, table, old, f"{table.id} [{row}, {col}]")
            entry = {"row": row, "col": col, "old": old, "new": new}
            (unchanged if new == old else changed).append(entry)
        if not changed:
            listed = ", ".join(f"[{c['row']}, {c['col']}]={c['old']}" for c in unchanged)
            raise EditSpecRefusalError(
                "no_op_edit",
                f"edit {index} ({edit.operation} on {table.id}) leaves every selected cell at "
                f"its old value: {listed}",
            )
        out.cells.extend(
            CellEdit(table.id, c["row"], c["col"], c["new"], slot=edit.slot) for c in changed
        )
        out.records.append(
            {
                "index": index,
                "table": table.id,
                "slot": edit.slot,
                "selector": edit.selector,
                "operation": {
                    k: v
                    for k, v in edit.model_dump(mode="json").items()
                    if k in ("set_raw", "add_raw", "scale", "rounding") and v is not None
                },
                "region": region,
                "mask": mask,
                "selected": len(cells),
                "changed": changed,
                "unchanged": unchanged,
            }
        )
    return out


def resolve_edits(spec: EditSpec, rom_map: RomMap, image: bytes) -> list[CellEdit]:
    """``spec``'s edits as cell edits on ``image``: only the cells whose value
    changes, each already accepted by :func:`make_candidate` (so a value
    outside its dtype is refused here too, ``value_out_of_range``)."""
    resolution = _resolve(spec, rom_map, image)
    make_candidate(image, rom_map, resolution.cells, base_label=spec.label)
    return resolution.cells


# -- candidates and their provenance --------------------------------------------------------


def _cell_record(cell: Mapping[str, Any]) -> dict[str, Any]:
    return {k: cell[k] for k in ("table", "slot", "row", "col", "address", "old", "new")}


def _traced(
    base: BaseImage,
    image: bytes,
    rom_map: RomMap,
    model: ApplicationModel,
    model_sha256: str,
    slot: int | None,
    slot_source: str,
) -> EffectReport:
    report = compare_roms(
        base.image,
        image,
        rom_map,
        model,
        model_sha256,
        base_slot=slot,
        candidate_slot=slot,
        slot_source=slot_source,
    )
    if report.reason is not None:
        raise EditSpecRefusalError(
            report.reason,
            "compare_roms cannot trace this candidate against its base, so its changes have "
            "no known reach",
        )
    return report


def _provenance(
    *,
    route: Route,
    label: str,
    base: BaseImage,
    image: bytes,
    cells: Sequence[Mapping[str, Any]],
    report: EffectReport,
    slot_source: str,
    spec: EditSpec | None = None,
    spec_edits: Sequence[dict[str, Any]] = (),
    edit_set_sha256: str | None = None,
    candidate_file: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One shape for both routes: the changed cells (old -> new raw) and the
    changed ranges with their reach, whichever route made the bytes."""
    plain = report.to_dict()
    return {
        "tool": TOOL,
        "tool_version": TOOL_VERSION,
        "kind": "simulation_candidate",
        "flashable": False,
        "checksums": (
            "stale on purpose: a simulation candidate is not a flashable image"
            if route == "edit_spec"
            else "as saved in the file, not checked: a simulation input"
        ),
        "route": route,
        "label": label,
        "base": dict(base.identity),
        "base_sha256": base.image_sha256,
        "candidate_sha256": sha256_hex(image),
        "spec_sha256": None if spec is None else spec.sha256(),
        "spec_edits": list(spec_edits),
        "edit_set_sha256": edit_set_sha256,
        "candidate_file": candidate_file,
        "cells": sorted((_cell_record(c) for c in cells), key=lambda c: c["address"]),
        "ranges": plain["ranges"],
        "effect": {
            "outcome": report.outcome,
            "slot": report.slot,
            "slot_source": slot_source,
            "changed_bytes": report.changed_bytes,
            "changed_nodes": list(report.changed_nodes),
            "moved_lookups": plain["moved_lookups"],
            "rom_effects": report.provenance,
        },
    }


def _check_base(spec: EditSpec, base: BaseImage) -> None:
    named = spec.base
    if named.revision is not None:
        ok = base.identity.get("source") == "revision" and base.identity["revision"] == (
            named.revision
        )
        what = f"revision {named.revision}"
    else:
        ok = base.image_sha256 == named.sha256
        what = f"image {named.sha256}"
    if not ok:
        raise EditSpecRefusalError(
            "base_mismatch",
            f"the spec edits {what}; the base given is {base.label} ({base.image_sha256})",
        )


def build_candidate(
    spec: EditSpec,
    rom_map: RomMap,
    base: BaseImage,
    *,
    model: ApplicationModel,
    model_sha256: str,
    slot: int | None,
    slot_source: str,
) -> Candidate:
    """``spec`` applied to ``base`` through :func:`make_candidate`, traced
    by :func:`compare_roms` on the active ``slot``. ``base`` must be the image
    the spec names (``base_mismatch``)."""
    _check_base(spec, base)
    resolution = _resolve(spec, rom_map, base.image)
    made = make_candidate(base.image, rom_map, resolution.cells, base_label=base.label)
    report = _traced(base, made.image, rom_map, model, model_sha256, slot, slot_source)
    return Candidate(
        image=made.image,
        provenance=_provenance(
            route="edit_spec",
            label=spec.label,
            base=base,
            image=made.image,
            cells=made.provenance["edits"],
            report=report,
            slot_source=slot_source,
            spec=spec,
            spec_edits=resolution.records,
            edit_set_sha256=made.provenance["edit_set_sha256"],
        ),
    )


def load_candidate_file(
    path: Path,
    rom_map: RomMap,
    *,
    base: BaseImage,
    model: ApplicationModel,
    model_sha256: str,
    slot: int | None,
    slot_source: str,
    label: str | None = None,
) -> Candidate:
    """An image edited elsewhere (e.g. saved from UpRev), read through
    :func:`load_image` (the UpRev backup ``.bin`` is refused by its format
    name, ``image_unreadable``), with the changed mapped cells and
    :func:`compare_roms`'s changed ranges as its edits. An identical file is
    a valid candidate (the negative control), with no cells and no ranges."""
    path = Path(path)
    image, fmt, container = _load_file(path, rom_map)
    report = _traced(base, image, rom_map, model, model_sha256, slot, slot_source)
    compared = compare_images(base.image, image, rom_map)
    cells = [cell for entry in compared["ranges"] for cell in entry["cells"]]
    return Candidate(
        image=image,
        provenance=_provenance(
            route="candidate_file",
            label=label or path.name,
            base=base,
            image=image,
            cells=cells,
            report=report,
            slot_source=slot_source,
            candidate_file={"file": path.name, "format": fmt, "container_sha256": container},
        ),
    )


__all__ = [
    "RAW_UNIT",
    "SCHEMA_REASONS",
    "TOOL_VERSION",
    "BaseImage",
    "BaseRef",
    "Edit",
    "EditSpec",
    "EditSpecRefusalError",
    "Region",
    "build_candidate",
    "load_candidate_file",
    "load_edit_spec",
    "parse_edit_spec",
    "resolve_base",
    "resolve_edits",
]
