"""Command-change predictions: what the reproduced target logic gives under a
proposed calibration, recorded before the change (E02.6).

For a proposed ROM change -- an exact base image and either an exact target
image or a cell edit set (the Fuel Targets editor's :class:`Patch`) -- this
runs the twin's own pure per-tuple prediction (:func:`src.analysis.target_phi.predict`,
the shared :mod:`src.decode.ecm_lookup` kernel) on both calibrations at stated
raw lookup inputs, per cell or per region, and records the result as an E01
:class:`~src.evidence.records.Prediction` carrying a
:class:`~src.evidence.records.WhatIf`.

What it makes impossible rather than documented:

* **A what-if is not a measurement.** It lives only on a prediction (never
  evidence), its quantity is :data:`WHAT_IF_QUANTITY` and its detail is
  labelled ``what_if``. Every place that takes measured output (an outcome's
  evidence, an investigation's observations, the delivery loop) is typed to
  evidence and refuses it.
* **It is conditional, and says on what.** Values hold at *equal raw lookup
  inputs* in the stated region, for the gated lookup the twin computes; the
  application model's blockers between that lookup and the final command, and
  every changed byte outside the reproduced table, are named on the record
  (``not_reproduced``). A Fuel Comp edit, which acts on the load input itself,
  therefore reads as "no change at equal inputs; the inputs are not predicted".
* **The change is exact.** The base is a resolved table (an image hash the
  twin verified); a target image must be another exact image of the same
  program, table and slot; a cell edit set must name the base's container hash
  in the flash history and each cell's current value. A moved axis is refused
  (a stated input would land on another cell).
* **It predicts; it never writes.** Nothing here (or anything it imports)
  reaches a CAN client, the OBD request path or the non-volatile write gate:
  ``tests/test_twin_command_change.py`` imports and runs it in a clean process
  and fails if any of them was loaded.
* **Order is the ledger's.** A prediction is stored with its minted record
  time. It is linked at record time only to the flash the caller names, or to
  a flash of the target image recorded after the base became current (the
  change already made: ``post_hoc``, never confirmable). An earlier flash of
  the same image (re-flashing a previous calibration) is not this change;
  :func:`command_change_order` then picks the first matching flash after the
  record time. A cell edit set names no image, so it orders only against a
  flash the caller names.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import src.analysis.target_phi as target_phi_module
import src.decode.ecm_lookup as ecm_lookup_module
from src.analysis.rom_image import compare_images, sha256_hex
from src.analysis.rom_map import RomMap
from src.analysis.rom_targets import Patch
from src.analysis.target_phi import (
    EVALUATOR_VERSION,
    ResolvedTable,
    TwinDefinition,
    predict,
)
from src.decode.ecm_lookup import KERNEL_VERSION
from src.evidence.ledger import MemoryLedger, PredictionOrder
from src.evidence.provenance import code_version
from src.evidence.records import (
    WHAT_IF_PREFIX,
    Conditions,
    Intervention,
    Prediction,
    Provenance,
    Quantity,
    VehicleIdentity,
    WhatIf,
    WhatIfPoint,
)
from src.refusal import ReasonedRefusal
from src.vehicle.tune_revisions import TuneRevisionRegistry

__all__ = [
    "WHAT_IF_QUANTITY",
    "WHAT_IF_TOOL",
    "WHAT_IF_TOOL_VERSION",
    "CommandChangeRefusalError",
    "ProposedChange",
    "Region",
    "RegionPrediction",
    "change_from_cell_edits",
    "change_from_images",
    "command_change_order",
    "default_regions",
    "predict_command_change",
    "record_command_change",
]

#: The twin's two lookup branches (``target_phi.predict``'s ``branch``).
Branch = Literal["ordinary", "override"]

WHAT_IF_TOOL = "target_phi_what_if"
#: Bump when the mapping from a computation to a prediction record changes.
WHAT_IF_TOOL_VERSION = "1"
WHAT_IF_QUANTITY = f"{WHAT_IF_PREFIX}target_equiv_ratio"
_UNIT = "1"
_RAW = "raw_count"

#: The conditions every value holds under (stated on each record).
ASSUMPTIONS = (
    "equal raw lookup inputs before and after the change: the load input, the "
    "engine-speed word and the pedal-enrichment flag are stated, not predicted",
    "the value is the gated Target AFR lookup the twin computes; it is the final "
    "command only where the application model's equals_output_when conditions hold",
)


class CommandChangeRefusalError(ReasonedRefusal, ValueError):
    """A proposed change the what-if cannot compute; ``reason`` names why."""


@dataclass(frozen=True)
class ProposedChange:
    """A base calibration's table and the table the change would leave."""

    base: ResolvedTable
    target: ResolvedTable
    #: Exactly one names the change (see :class:`~src.evidence.records.WhatIf`).
    target_image_sha256: str | None
    change_set: dict[str, Any] | None
    change_set_sha256: str | None
    #: Changed bytes outside the reproduced table and axes, or why unknown.
    not_reproduced: tuple[str, ...]

    def changed_cells(self) -> list[tuple[int, int]]:
        return [
            (r, c)
            for r, row in enumerate(self.base.cells)
            for c, value in enumerate(row)
            if self.target.cells[r][c] != value
        ]


def _same_lookup_frame(base: ResolvedTable, target: ResolvedTable) -> None:
    if (base.table_id, base.slot) != (target.table_id, target.slot):
        raise CommandChangeRefusalError(
            "table_mismatch",
            f"base reads {base.table_id} slot {base.slot}; target {target.table_id} "
            f"slot {target.slot}",
        )
    if base.application.program_id != target.application.program_id:
        raise CommandChangeRefusalError(
            "program_mismatch",
            f"base program {base.application.program_id}, target "
            f"{target.application.program_id}: another program has another decode",
        )
    if (base.row_breakpoints, base.column_breakpoints) != (
        target.row_breakpoints,
        target.column_breakpoints,
    ):
        raise CommandChangeRefusalError(
            "axis_changed",
            "the change moves the table's breakpoints: a stated raw input would land on "
            "another cell, which this per-cell prediction does not express",
        )


def _not_reproduced(
    base: ResolvedTable,
    base_image: bytes,
    target_image: bytes,
    rom_map: RomMap,
) -> tuple[str, ...]:
    """Every changed range outside the reproduced table, axes and checksum
    words, by the map's labels (or as an unmapped byte range)."""
    comparison = compare_images(base_image, target_image, rom_map)
    if not comparison["map_applied"]:
        return (
            f"unknown: the ROM map does not describe these images ({comparison['map_reason']})",
        )
    reproduced_axes = {base.row_axis_id, base.column_axis_id}
    out: list[str] = []
    for item in comparison["ranges"]:
        tables = [o for o in item["overlaps"] if o["type"] == "table"]
        outside = [
            o
            for o in tables
            if not (o["id"] == base.table_id and o["slot"] == base.slot)
            and o["id"] not in reproduced_axes
        ]
        if tables and not outside:
            continue
        if not tables and any(o["type"] == "checksum" for o in item["overlaps"]):
            continue
        labels = [o["label"] for o in outside] or [
            o["label"] for o in item["overlaps"] if o["type"] != "checksum"
        ]
        span = f"0x{item['start']:06X}-0x{item['end']:06X}"
        out.append(f"{', '.join(labels) or 'unmapped bytes'} ({span})")
    return tuple(dict.fromkeys(out))


def change_from_images(
    base: ResolvedTable,
    target: ResolvedTable,
    *,
    base_image: bytes | None = None,
    target_image: bytes | None = None,
    rom_map: RomMap | None = None,
) -> ProposedChange:
    """Base and target as resolved tables (each refused or verified by the
    twin's own resolution). With both images' bytes, every changed range
    outside the reproduced table is named; without them that is unknown."""
    if base.image_sha256 == target.image_sha256:
        raise CommandChangeRefusalError(
            "no_change", f"base and target are the same image {base.image_sha256}"
        )
    _same_lookup_frame(base, target)
    if base_image is not None and target_image is not None and rom_map is not None:
        for image, table in ((base_image, base), (target_image, target)):
            if sha256_hex(image) != table.image_sha256:
                raise CommandChangeRefusalError(
                    "image_hash_mismatch",
                    f"bytes hash to {sha256_hex(image)}, the table names {table.image_sha256}",
                )
        not_reproduced = _not_reproduced(base, base_image, target_image, rom_map)
    else:
        not_reproduced = (
            "unknown: the two images' bytes were not compared, so changes outside the "
            "reproduced table are not known",
        )
    return ProposedChange(
        base=base,
        target=target,
        target_image_sha256=target.image_sha256,
        change_set=None,
        change_set_sha256=None,
        not_reproduced=not_reproduced,
    )


def _container_hash(registry: TuneRevisionRegistry, image_sha256: str) -> str | None:
    for revision in registry.revisions:
        if revision.image_sha256 == image_sha256:
            return revision.rom_sha256
    for reference in registry.reference_images:
        if reference.image_sha256 == image_sha256:
            return reference.rom_sha256
    return None


def change_from_cell_edits(
    base: ResolvedTable,
    patch: Patch,
    *,
    registry: TuneRevisionRegistry,
    definition: TwinDefinition,
) -> ProposedChange:
    """A cell edit set (the Fuel Targets editor's patch) applied to ``base``.

    The patch names the base by its container hash; the flash history must
    record that container for the base's exact plain image
    (``patch_source_mismatch``). Each edit must name the cell's current value
    (``old_value_mismatch``), lie inside the table (``cell_outside_table``),
    change something (``no_op_edit``) and appear once (``duplicate_edit``)."""
    if patch.table != definition.table.rom_map_kind:
        raise CommandChangeRefusalError(
            "table_mismatch",
            f"the patch edits {patch.table}; the twin reads {definition.table.rom_map_kind}",
        )
    if patch.map_slot != base.slot:
        raise CommandChangeRefusalError(
            "table_mismatch", f"the patch edits slot {patch.map_slot}; the base is slot {base.slot}"
        )
    container = _container_hash(registry, base.image_sha256)
    if container is None or container != patch.source_sha256:
        raise CommandChangeRefusalError(
            "patch_source_mismatch",
            f"the patch names container {patch.source_sha256}; the flash history records "
            f"{container or 'no container'} for base image {base.image_sha256}",
        )
    cells = [list(row) for row in base.cells]
    seen: set[tuple[int, int]] = set()
    for edit in patch.changes:
        key = (edit.row, edit.column)
        if edit.row >= len(cells) or edit.column >= len(cells[0]):
            raise CommandChangeRefusalError(
                "cell_outside_table", f"row {edit.row} column {edit.column} is outside the table"
            )
        if key in seen:
            raise CommandChangeRefusalError(
                "duplicate_edit", f"row {edit.row} column {edit.column} edited twice"
            )
        seen.add(key)
        if cells[edit.row][edit.column] != edit.old_raw:
            raise CommandChangeRefusalError(
                "old_value_mismatch",
                f"row {edit.row} column {edit.column} holds {cells[edit.row][edit.column]}, "
                f"the edit expects {edit.old_raw}",
            )
        if edit.old_raw == edit.new_raw:
            raise CommandChangeRefusalError(
                "no_op_edit", f"row {edit.row} column {edit.column} is unchanged"
            )
        cells[edit.row][edit.column] = edit.new_raw
    canonical = patch.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    target = ResolvedTable(
        **{**base.__dict__, "cells": tuple(tuple(row) for row in cells)},
    )
    return ProposedChange(
        base=base,
        target=target,
        target_image_sha256=None,
        change_set=canonical,
        change_set_sha256=digest,
        not_reproduced=(),
    )


# -- regions and points -----------------------------------------------------------


@dataclass(frozen=True)
class Region:
    """Table cells by index, inclusive, on one branch. On ``override`` the
    pedal flag forces the last column, so only the rows matter."""

    rows: tuple[int, int]
    columns: tuple[int, int]
    branch: Branch

    def key(self) -> str:
        (r0, r1), (c0, c1) = self.rows, self.columns
        if self.branch == "override":
            return f"override:r{r0}-{r1}"
        return f"ordinary:r{r0}-{r1}:c{c0}-{c1}"


@dataclass(frozen=True)
class RegionPrediction:
    region: Region
    points: tuple[WhatIfPoint, ...]
    direction: Literal["increase", "decrease", "no_change"]
    magnitude: Quantity | None
    bounds: dict[str, Quantity]


def default_regions(change: ProposedChange) -> list[Region]:
    """Each changed cell on its own (ordinary), plus the override row of each
    changed last-column cell; the whole table on both branches when no cell
    changes (a change that acts elsewhere predicts no change *at equal
    inputs*)."""
    cells = change.changed_cells()
    rows = len(change.base.cells)
    last = len(change.base.cells[0]) - 1
    if not cells:
        return [
            Region((0, rows - 1), (0, last), "ordinary"),
            Region((0, rows - 1), (last, last), "override"),
        ]
    regions = [Region((r, r), (c, c), "ordinary") for r, c in cells]
    regions += [Region((r, r), (last, last), "override") for r, c in cells if c == last]
    return regions


def _point(
    change: ProposedChange,
    definition: TwinDefinition,
    *,
    row: int,
    column: int,
    branch: Branch,
) -> WhatIfPoint:
    table = change.base
    rpm = table.row_breakpoints[row]
    flag = (1 << definition.inputs.forced_column_flag.bit) if branch == "override" else 0
    # Under the flag the firmware substitutes the load byte's top value, which
    # clamps to the last column (target_phi.predict does the same).
    load = (
        (1 << definition.inputs.load.bits) - 1
        if branch == "override"
        else table.column_breakpoints[column]
    )
    before = predict(change.base, definition, load_raw=load, rpm_raw=rpm, flag_raw=flag)
    after = predict(change.target, definition, load_raw=load, rpm_raw=rpm, flag_raw=flag)
    reason = before.reason or after.reason
    key = f"override:r{row}" if branch == "override" else f"r{row}c{column}"
    return WhatIfPoint(
        key=key,
        inputs={
            "rpm_word": Quantity(value=rpm, unit=_RAW),
            "load_input": Quantity(value=load, unit=_RAW),
            "pedal_flag": Quantity(value=flag, unit=_RAW),
        },
        before=None if reason or before.phi is None else Quantity(value=before.phi, unit=_UNIT),
        after=None if reason or after.phi is None else Quantity(value=after.phi, unit=_UNIT),
        reason=reason,
    )


def _region_prediction(
    change: ProposedChange, definition: TwinDefinition, region: Region
) -> RegionPrediction:
    table = change.base
    n_rows, n_cols = len(table.cells), len(table.cells[0])
    (r0, r1), (c0, c1) = region.rows, region.columns
    if not (0 <= r0 <= r1 < n_rows and 0 <= c0 <= c1 < n_cols):
        raise CommandChangeRefusalError(
            "region_outside_table", f"{region.key()} is outside the {n_rows}x{n_cols} table"
        )
    if region.branch == "override" and (c0, c1) != (n_cols - 1, n_cols - 1):
        raise CommandChangeRefusalError(
            "region_outside_branch",
            "the override branch reads only the last column; state columns as the last",
        )
    points = tuple(
        _point(change, definition, row=r, column=c, branch=region.branch)
        for r in range(r0, r1 + 1)
        for c in (range(c0, c1 + 1) if region.branch == "ordinary" else (c1,))
    )
    changes = [p.change for p in points if p.change is not None]
    if not changes:
        raise CommandChangeRefusalError(
            "no_evaluable_point",
            f"every point of {region.key()} is ambiguous under the decoded logic "
            f"({sorted({p.reason for p in points if p.reason})})",
        )
    signs = {(c > 0) - (c < 0) for c in changes}
    if signs == {0}:
        direction: Literal["increase", "decrease", "no_change"] = "no_change"
    elif signs <= {0, 1}:
        direction = "increase"
    elif signs <= {0, -1}:
        direction = "decrease"
    else:
        raise CommandChangeRefusalError(
            "mixed_direction",
            f"{region.key()} rises at some points and falls at others; split the region",
        )
    # One magnitude only when every evaluable point changes by the same amount
    # (zeros included): a region whose cells move differently has none.
    distinct = set(changes)
    magnitude = Quantity(value=distinct.pop(), unit=_UNIT) if len(distinct) == 1 else None
    rpm = [table.row_breakpoints[r] for r in range(r0, r1 + 1)]
    bounds = {
        "rpm_word_min": Quantity(value=min(rpm), unit=_RAW),
        "rpm_word_max": Quantity(value=max(rpm), unit=_RAW),
    }
    if region.branch == "ordinary":
        load = [table.column_breakpoints[c] for c in range(c0, c1 + 1)]
        bounds["load_input_min"] = Quantity(value=min(load), unit=_RAW)
        bounds["load_input_max"] = Quantity(value=max(load), unit=_RAW)
    return RegionPrediction(region, points, direction, magnitude, bounds)


def predict_command_change(
    change: ProposedChange,
    definition: TwinDefinition,
    regions: Sequence[Region] | None = None,
) -> list[RegionPrediction]:
    """Per region, the commanded phi before and after at each breakpoint
    point (bilinear between breakpoints, so the extremes are on them).
    Refuses ``mixed_direction`` (split the region) and ``no_evaluable_point``."""
    chosen = list(regions) if regions is not None else default_regions(change)
    return [_region_prediction(change, definition, region) for region in chosen]


# -- recording ----------------------------------------------------------------------


def _regime(change: ProposedChange, region: Region) -> str:
    table = change.base
    (r0, r1), (c0, c1) = region.rows, region.columns
    where = (
        f"rows {r0}-{r1}, pedal-enrichment override (last column)"
        if region.branch == "override"
        else f"rows {r0}-{r1}, columns {c0}-{c1}, ordinary lookup"
    )
    return (
        f"what-if: ECM commanded target equivalence ratio at equal raw lookup inputs, "
        f"{table.table_id} slot {table.slot}, {where}"
    )


def _flashes(ledger: MemoryLedger, image_sha256: str | None) -> list[Intervention]:
    """Calibration interventions installing ``image_sha256``, by effect time."""
    if image_sha256 is None or not ledger.path.exists():
        return []
    found = [
        entry.record
        for entry in ledger.query(kinds=("intervention",))
        if isinstance(entry.record, Intervention)
        and entry.record.intervention_kind == "calibration"
        and entry.record.image_sha256 == image_sha256
    ]
    return sorted(found, key=lambda r: r.valid_from)


def _flash_since_base(ledger: MemoryLedger, change: ProposedChange) -> Intervention | None:
    """The flash of the target image that happened *after* the base became
    current: only that one is this change already made. A target image
    flashed before the base (re-flashing a previous calibration, a restore) is
    another occurrence, never this prediction's intervention. Without a
    recorded flash of the base there is no "since", so nothing is linked."""
    base_flashes = _flashes(ledger, change.base.image_sha256)
    if not base_flashes:
        return None
    since = base_flashes[-1].valid_from
    later = [r for r in _flashes(ledger, change.target_image_sha256) if r.valid_from > since]
    return later[0] if later else None


def _named_intervention(
    ledger: MemoryLedger, change: ProposedChange, intervention_id: str
) -> Intervention:
    record = ledger.get(intervention_id)
    if not isinstance(record, Intervention) or record.intervention_kind != "calibration":
        raise CommandChangeRefusalError(
            "not_a_flash", f"{intervention_id} is not a calibration intervention"
        )
    target = change.target_image_sha256
    if target is not None and record.image_sha256 != target:
        raise CommandChangeRefusalError(
            "intervention_mismatch",
            f"{intervention_id} installs image {record.image_sha256}; the what-if's target "
            f"is {target}",
        )
    return record


def _provenance() -> Provenance:
    return Provenance(
        producer=WHAT_IF_TOOL,
        producer_kind="tool",
        version=f"{WHAT_IF_TOOL_VERSION}+evaluator{EVALUATOR_VERSION}+kernel{KERNEL_VERSION}",
        code_version=code_version(sys.modules[__name__], target_phi_module, ecm_lookup_module),
    )


def record_command_change(
    ledger: MemoryLedger,
    change: ProposedChange,
    definition: TwinDefinition,
    *,
    vehicle: VehicleIdentity,
    regions: Sequence[Region] | None = None,
    investigation: str | None = None,
    intervention: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Record one what-if prediction per region; returns their ids.

    ``vehicle.calibration`` must be the base's revision (the calibration the
    prediction is made from) and ``vehicle.program_id`` its program.

    ``intervention`` names the flash this predicts (a calibration
    intervention installing the target image, ``intervention_mismatch``
    otherwise). Unnamed, the prediction is linked only to a flash of the target
    image recorded *after the base became current* (the change already made:
    the ledger then derives ``post_hoc``). An earlier flash of the same image
    -- re-flashing a previous calibration -- is never linked; the prediction
    stays unlinked and :func:`command_change_order` finds the first flash
    after it was recorded."""
    base = change.base
    if vehicle.calibration != base.revision:
        raise CommandChangeRefusalError(
            "calibration_mismatch",
            f"the prediction is made from {base.revision!r}; the vehicle identity says "
            f"{vehicle.calibration!r}",
        )
    if vehicle.program_id != base.application.program_id:
        raise CommandChangeRefusalError(
            "program_mismatch",
            f"the base table is program {base.application.program_id!r}; the vehicle "
            f"identity says {vehicle.program_id!r}",
        )
    stamp = now if now is not None else datetime.now(UTC)
    linked = (
        _named_intervention(ledger, change, intervention)
        if intervention is not None
        else _flash_since_base(ledger, change)
    )
    ids: list[str] = []
    for found in predict_command_change(change, definition, regions):
        what_if = WhatIf(
            output=base.application.output,
            base_image_sha256=base.image_sha256,
            target_image_sha256=change.target_image_sha256,
            change_set_sha256=change.change_set_sha256,
            assumptions=ASSUMPTIONS,
            blockers=base.application.blockers,
            not_reproduced=change.not_reproduced,
            points=found.points,
        )
        prediction = Prediction(
            vehicle=vehicle,
            provenance=_provenance(),
            conditions=Conditions(
                regime=_regime(change, found.region),
                vehicle_state="engine_running",
                bounds=found.bounds,
            ),
            valid_from=stamp,
            investigation=investigation,
            intervention=None if linked is None else linked.id,
            quantity=WHAT_IF_QUANTITY,
            direction=found.direction,
            magnitude=found.magnitude,
            noise_floor=None,
            what_if=what_if,
        )
        ids.append(ledger.append(prediction, now=now).id)
    return ids


def command_change_order(ledger: MemoryLedger, prediction_id: str) -> PredictionOrder | None:
    """Whether a what-if was recorded before the flash installing its target.

    The intervention it names, else the first calibration intervention
    installing its target image that took effect after the prediction was
    recorded (so an earlier flash of the same image, a restore's source, is
    never its outcome). ``None`` while no such flash is recorded, and always
    for a cell edit set unlinked at record time: it names no image to match a
    flash by."""
    prediction = ledger.get(prediction_id)
    if not isinstance(prediction, Prediction) or prediction.what_if is None:
        raise CommandChangeRefusalError(
            "not_a_what_if", f"{prediction_id} is not a what-if prediction"
        )
    target = ledger.what_if_intervention(prediction_id)
    if target is None:
        return None
    return ledger.prediction_order(prediction_id, intervention=target)
