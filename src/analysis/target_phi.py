"""The deterministic target-equivalence-ratio twin, first slice (E02).

Given the exact calibration and the ECM's observed lookup inputs, what fuel
command does its decoded Target AFR logic give, and does the commanded word
agree? This predicts the ECM's *command*, never fuel delivery, and a zero
difference says nothing about engine health.

Three layers, each usable without the next:

- :func:`predict` — one tuple of raw lookup inputs to one predicted command,
  through the shared kernel :mod:`src.decode.ecm_lookup` (the same search the
  ROM Editor's ECM cell uses). Pure: the streaming adapter (E04) calls it per
  complete tuple with a preloaded :class:`ResolvedTable`.
- :func:`resolve_table` / :func:`table_from_calibration` and
  :func:`resolve_slot` — which bytes the ECM is reading, from a W08 library
  image (by plain-image hash, program, slot and verified table definition) or
  from the transcribed table in ``afr_calibration.yaml``, refusing by name.
- :func:`evaluate_session` — the stored-session adapter: every observed command
  sample becomes exactly one candidate, paired on acquisition time with each
  lookup input (``src.signals.pairing.match_ordered``, one-to-one), gated by the
  profile's declared mode, warmth, steadiness, region and map identity, and
  accounted for as ``pending``, ``unevaluable``, ``excluded`` or ``eligible``.

Everything vehicle-specific — channels, units, gates, regions, where the table
lives — is the profile's ``target_phi_twin.yaml`` (:class:`TwinDefinition`).
A learned constraint is a field there, not a comment here.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.channels import ChannelSpecError, read_channels
from src.analysis.ecu_application import (
    Applicability,
    ApplicationModel,
    ApplicationModelRefusalError,
    applicability,
    check_image,
    check_image_sha,
    observed_specs,
)
from src.analysis.rom_image import read_calibration_id, sha256_hex, table_values
from src.analysis.rom_map import RomMap
from src.analysis.rom_trace import (
    ViewerDefinition,
    mapping_refusal,
    raw_counts,
    stored_decode,
)
from src.decode.dids import DidMap, load_did_map
from src.decode.ecm_lookup import KERNEL_VERSION, LookupResult
from src.decode.target_phi_command import (
    GATE_STATE_UNKNOWN,
    Branch,
    GateConstants,
    target_command,
)
from src.decode.target_phi_command import x_from_bfs as _x_from_bfs
from src.paths import DIDS_YAML
from src.refusal import ReasonedRefusal
from src.signals.pairing import PairingPolicy, match_ordered, to_seconds
from src.signals.time_lookup import MISSING, bracketing_sample_indices
from src.timefmt import format_epoch
from src.yaml_load import load_yaml

#: Bumped when the evaluator's states, reasons or record shape change.
#: 2: provenance names the application model; blockers come from it (E05.1).
EVALUATOR_VERSION = 3

TWIN_FILE = "target_phi_twin.yaml"

State = Literal["pending", "unevaluable", "excluded", "eligible"]

#: Every reason a record can carry, with the one state it is given under. A
#: key ending in ``:`` is a template: what follows names an input (``load``,
#: ``rpm``, ``flag``, or ``tuple_spread``), a gate (``coolant``, ``mode``) or
#: a steadiness channel. Besides these, an ``excluded`` record can name a
#: fuel-system mode the mode gate does not admit (:func:`excluded_mode_reasons`).
#: Every reason is minted through :func:`_reason`, which refuses one missing
#: here, so a consumer that classifies every entry (the twin runner's
#: ``_phi_kind``) cannot meet one it never saw.
REASONS: dict[str, State] = {
    # The command sample itself.
    "unsupported_clock": "unevaluable",
    "sentinel": "unevaluable",
    # The map identity (``_identity``; ``resolve_slot``'s own two reasons).
    "awaiting_identity_read": "pending",
    "map_identity_unbracketed": "unevaluable",
    "map_uninitialised": "unevaluable",
    "slot_ambiguous": "unevaluable",
    "map_switching": "unevaluable",
    "slot_unresolved": "unevaluable",
    # The lookup inputs.
    "input_not_captured:": "unevaluable",
    "awaiting_input:": "pending",
    "input_stale:": "unevaluable",
    # The prediction (``target_phi_command.target_command``) and its region.
    GATE_STATE_UNKNOWN: "unevaluable",
    "uncovered_region": "unevaluable",
    # The gates.
    "gate_missing:": "unevaluable",
    "gate_stale:": "unevaluable",
    "cold": "excluded",
    "awaiting_mode_read": "pending",
    "mode_unknown": "unevaluable",
    "mode_transition": "excluded",
    "steadiness_unobserved:": "unevaluable",
    "transient": "excluded",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


_SPEC = r"^[a-z]+:[A-Za-z0-9_]+$"


class VerifiedImage(_Strict):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(min_length=1)


class Program(_Strict):
    calibration_id: str = Field(min_length=1)
    verified_images: list[VerifiedImage] = Field(min_length=1)
    evidence: str = Field(min_length=1)


class TableAxisRef(_Strict):
    rom_map_axis: str = Field(min_length=1)
    input: Literal["rpm", "load"]
    compare_multiplier: int = Field(ge=1)


class TableRef(_Strict):
    rom_map_kind: str = Field(min_length=1)
    rows: TableAxisRef
    columns: TableAxisRef
    cell_to_word_shift: int = Field(ge=0, le=16)
    evidence: str = Field(min_length=1)


class RawInput(_Strict):
    """A lookup input. ``raw_counts`` is the only unit the kernel accepts: a
    channel stored scaled is returned to whole counts through its own catalog
    decode, never fed at its display scale."""

    channel: str = Field(pattern=_SPEC)
    units: Literal["raw_counts"]
    bits: Literal[8, 16, 32]
    evidence: str = Field(min_length=1)


class FlagInput(RawInput):
    bit: int = Field(ge=0, le=31)
    forces: Literal["last_column"]


class Inputs(_Strict):
    load: RawInput
    rpm: RawInput
    forced_column_flag: FlagInput

    def channels(self) -> dict[str, str]:
        return {
            "load": self.load.channel,
            "rpm": self.rpm.channel,
            "flag": self.forced_column_flag.channel,
        }


class MagicRule(_Strict):
    channel: str = Field(pattern=_SPEC)
    valid: int


class ActiveMapRule(_Strict):
    channel: str = Field(pattern=_SPEC)
    committed_min: int = Field(ge=0)
    committed_max: int = Field(ge=0)


class SelectorRule(_Strict):
    channel: str = Field(pattern=_SPEC)


class DefaultSlot(_Strict):
    active_map: int
    selector: int
    slot: int = Field(ge=1)
    evidence: str = Field(min_length=1)


class MapIdentity(_Strict):
    magic: MagicRule
    active_map: ActiveMapRule
    selector: SelectorRule
    default_slot: DefaultSlot
    max_age_s: float = Field(gt=0)

    def channels(self) -> list[str]:
        return [self.magic.channel, self.active_map.channel, self.selector.channel]


class Reference(_Strict):
    channel: str = Field(pattern=_SPEC)
    sentinel_channel: str = Field(pattern=_SPEC)
    q_bits: int = Field(ge=1, le=16)


class EnrichmentGate(_Strict):
    floor_cell: int
    threshold_cell: int
    hysteresis_cells: int = Field(ge=0)
    rom_threshold_offset: int = Field(ge=0)
    rom_release_offset: int = Field(ge=0)


class ScaleCurve(_Strict):
    rom_offset: int = Field(ge=0)
    entries: int = Field(ge=1)
    identity_value: int


class FinalCommand(_Strict):
    """The decoded constants between the lookup and ``0xFFFF174B`` that the
    twin implements. Whether the path on to the final word is resolved is the
    application model's to say (``ecu_application_model.yaml``), not this
    file's: a status here is refused."""

    enrichment_gate: EnrichmentGate
    enrichment_scale_curve: ScaleCurve
    evidence: str = Field(min_length=1)


class CoolantGate(_Strict):
    sources: list[str] = Field(min_length=1)
    min_c: float
    max_age_s: float = Field(gt=0)
    status: Literal["proposed", "qualified"]


class ModeGate(_Strict):
    channel: str = Field(pattern=_SPEC)
    states: dict[int, str] = Field(min_length=1)
    eligible: list[str] = Field(min_length=1)
    max_age_s: float = Field(gt=0)
    status: Literal["proposed", "qualified"]

    @model_validator(mode="after")
    def _eligible_named(self) -> ModeGate:
        unknown = set(self.eligible) - set(self.states.values())
        if unknown:
            raise ValueError(f"mode gate: eligible names no declared state: {sorted(unknown)}")
        return self


class SteadyChannel(_Strict):
    channel: str = Field(pattern=_SPEC)
    max_span: float | None = Field(default=None, gt=0)
    max_relative_span: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _one_limit(self) -> SteadyChannel:
        if (self.max_span is None) == (self.max_relative_span is None):
            raise ValueError(f"{self.channel}: give exactly one of max_span, max_relative_span")
        return self


class SteadinessGate(_Strict):
    dwell_s: float = Field(gt=0)
    min_samples: int = Field(ge=2)
    channels: list[SteadyChannel] = Field(min_length=1)
    status: Literal["proposed", "qualified"]


class Gates(_Strict):
    max_acquisition_skew_s: float = Field(gt=0)
    clock: Literal["acquired"]
    coolant: CoolantGate
    mode: ModeGate
    steadiness: SteadinessGate


class Region(_Strict):
    rpm_raw: tuple[int, int]
    load_raw: tuple[int, int] | None = None
    status: Literal["proposed", "qualified"]


class Regions(_Strict):
    ordinary: Region
    override: Region

    @model_validator(mode="after")
    def _ordinary_bounds_load(self) -> Regions:
        if self.ordinary.load_raw is None:
            raise ValueError("the ordinary region needs load_raw bounds")
        return self


class XFormula(_Strict):
    bfs_channel: str = Field(pattern=_SPEC)
    scaler_rom_map_table: str = Field(min_length=1)
    shift: int = Field(ge=0)
    cap: int = Field(ge=1)


class Acquisition(_Strict):
    """Which memory windows the twin needs, and which must be read together.

    A tuple is only as simultaneous as its reads: the command and its three
    lookup inputs live in four windows, and on October 4 their nearest reads
    sat a median 0.9-1.5 s from each command sample (the fixture's
    ``accounting.input_pairing``), so no tuple fitted the skew. ``back_to_back`` names the
    windows a capture must read consecutively, within ``max_span_s``."""

    #: Window name (``data/dids.yaml`` ``window:``) -> the channels it carries.
    windows: dict[str, list[str]] = Field(min_length=1)
    back_to_back: list[str] = Field(min_length=2)
    max_span_s: float = Field(gt=0)
    evidence: str = Field(min_length=1)

    @model_validator(mode="after")
    def _back_to_back_declared(self) -> Acquisition:
        unknown = [w for w in self.back_to_back if w not in self.windows]
        if unknown:
            raise ValueError(f"acquisition.back_to_back names undeclared windows: {unknown}")
        return self

    def window_of(self, channel: str) -> str | None:
        return next((w for w, specs in self.windows.items() if channel in specs), None)


class TwinDefinition(_Strict):
    schema_version: Literal[1]
    name: Literal["target_equiv_ratio"]
    #: The application-model output this twin predicts (E05.1). Its blockers
    #: decide whether any residual is qualified.
    application_output: str = Field(min_length=1)
    program: Program
    table: TableRef
    inputs: Inputs
    map_identity: MapIdentity
    reference: Reference
    final_command: FinalCommand
    gates: Gates
    regions: Regions
    x_formula: XFormula
    acquisition: Acquisition

    @model_validator(mode="after")
    def _windows_cover_the_tuple(self) -> TwinDefinition:
        acquisition = self.acquisition
        needed = [
            *self.inputs.channels().values(),
            self.reference.channel,
            *self.map_identity.channels(),
            self.x_formula.bfs_channel,
        ]
        undeclared = [c for c in needed if acquisition.window_of(c) is None]
        if undeclared:
            raise ValueError(f"acquisition.windows declares no window for {undeclared}")
        tuple_windows = {
            acquisition.window_of(c)
            for c in [*self.inputs.channels().values(), self.reference.channel]
        }
        missing = sorted(
            w for w in tuple_windows if w is not None and w not in acquisition.back_to_back
        )
        if missing:
            raise ValueError(
                f"acquisition.back_to_back must include every tuple window; missing {missing}"
            )
        if acquisition.max_span_s > self.gates.max_acquisition_skew_s:
            raise ValueError(
                "acquisition.max_span_s cannot exceed gates.max_acquisition_skew_s: a "
                "back-to-back read slower than the skew can never yield a tuple"
            )
        return self

    def verified_revision(self, image_sha256: str) -> str | None:
        return next(
            (v.revision for v in self.program.verified_images if v.sha256 == image_sha256), None
        )


def check_windows(definition: TwinDefinition, catalog: DidMap) -> None:
    """Refuse (``window_mismatch``) unless every declared channel sits in the
    declared window of the memory catalog the session is decoded with."""
    by_name = {f"mem:{c.name}": c.window or c.name for c in catalog.memory}
    wrong = []
    for window, specs in definition.acquisition.windows.items():
        for spec in specs:
            actual = by_name.get(spec)
            if actual != window:
                wrong.append(f"{spec} (catalog window {actual!r}, declared {window!r})")
    if wrong:
        raise TwinRefusalError("window_mismatch", "; ".join(wrong))


def load_twin_definition(profile_dir: Path) -> tuple[TwinDefinition, str]:
    """The profile's twin definition and the sha256 of its file (the
    decoder version every prediction carries). ``FileNotFoundError`` when the
    vehicle declares none."""
    path = profile_dir / TWIN_FILE
    text = path.read_bytes()
    return (
        TwinDefinition.model_validate(load_yaml(text)),
        hashlib.sha256(text).hexdigest(),
    )


# -- refusals -----------------------------------------------------------------


class TwinRefusalError(ReasonedRefusal, ValueError):
    """The twin cannot run at all; ``reason`` names why."""


# -- the resolved table -------------------------------------------------------


@dataclass(frozen=True)
class ResolvedTable:
    """The exact bytes a prediction reads, and where they came from."""

    cells: tuple[tuple[int, ...], ...]
    #: Breakpoints in each input's comparison domain (axis byte x multiplier).
    row_breakpoints: tuple[int, ...]
    column_breakpoints: tuple[int, ...]
    #: The axis bytes as stored, for the record.
    row_axis_raw: tuple[int, ...]
    column_axis_raw: tuple[int, ...]
    image_sha256: str
    revision: str | None
    slot: int
    table_id: str
    row_axis_id: str
    column_axis_id: str
    #: ``rom_library`` (read from the image) or ``afr_calibration`` (the
    #: transcribed table, pinned equal to the image by the private-ROM test).
    source: Literal["rom_library", "afr_calibration"]
    #: Load Point Scaler, when read from the image.
    load_point_scaler: int | None
    #: Whether the final-command constants were checked against the image.
    final_command_constants_checked: bool
    #: The application model's standing for the twin's output: its blockers
    #: decide qualification (E05.1).
    application: Applicability

    def provenance(self) -> dict[str, Any]:
        return {
            "image_sha256": self.image_sha256,
            "revision": self.revision,
            "slot": self.slot,
            "table": self.table_id,
            "axes": {"rows": self.row_axis_id, "columns": self.column_axis_id},
            "table_source": self.source,
        }


def _check_final_command_constants(image: bytes, definition: TwinDefinition) -> None:
    final = definition.final_command
    gate = final.enrichment_gate
    threshold = image[gate.rom_threshold_offset]
    release = image[gate.rom_release_offset]
    if threshold != gate.threshold_cell or threshold - release != gate.hysteresis_cells:
        raise TwinRefusalError(
            "unmodelled_logic",
            f"this image's enrichment gate is {threshold} with release {release}; the twin "
            f"models threshold {gate.threshold_cell}, hysteresis {gate.hysteresis_cells}",
        )
    curve = final.enrichment_scale_curve
    values = list(image[curve.rom_offset : curve.rom_offset + curve.entries])
    if any(value != curve.identity_value for value in values):
        raise TwinRefusalError(
            "unmodelled_logic",
            f"enrichment scale curve at 0x{curve.rom_offset:X} is {values}, not the identity "
            f"{curve.identity_value}; the twin does not model that scaling",
        )


def bind_application(
    application_model: tuple[ApplicationModel, str], definition: TwinDefinition
) -> Applicability:
    """The application model's standing for this twin, after checking the two
    describe the same thing: the output names this twin's file, the node it
    predicts is implemented, and the observed inputs on that node's path are
    exactly the channels the twin reads. Refuses ``application_model_mismatch``
    (or ``output_undeclared``)."""
    model, sha = application_model
    try:
        output = model.output(definition.application_output)
    except ApplicationModelRefusalError as exc:
        raise TwinRefusalError(exc.reason, exc.message) from exc
    if output.twin is None or output.twin.definition != TWIN_FILE:
        raise TwinRefusalError(
            "application_model_mismatch",
            f"output {output.id} does not name {TWIN_FILE} as its twin",
        )
    predicts = model.node(output.twin.predicts)
    if predicts.twin != "implemented":
        raise TwinRefusalError(
            "application_model_mismatch",
            f"the model says {predicts.id} is {predicts.twin}; the twin computes it",
        )
    needed = set(observed_specs(model, predicts.id))
    reads = {*definition.inputs.channels().values(), *definition.map_identity.channels()}
    if needed != reads:
        raise TwinRefusalError(
            "application_model_mismatch",
            f"the model's observed inputs to {predicts.id} are {sorted(needed)}; the twin "
            f"reads {sorted(reads)}",
        )
    return applicability(model, sha, output.id)


def check_trace_mapping(viewer: ViewerDefinition, definition: TwinDefinition) -> None:
    """Refuse unless W05's verified trace mapping for this table names the
    twin's own inputs, compared the way the twin compares them.

    ``rom_viewer.yaml`` is where the ECM coordinates' landing on the axes was
    settled (W05.1); the twin consumes that verdict rather than restating it.
    Refuses ``mapping_unverified`` (no trace, or an unverified axis) and
    ``mapping_mismatch`` (another channel, or a mapping that is not the raw
    bytes for the load axis and rpm for the rpm axis)."""
    info = viewer.table(definition.table.rom_map_kind)
    refusal = mapping_refusal(None if info is None else info.trace)
    if refusal is not None:
        raise TwinRefusalError("mapping_unverified", refusal["message"])
    assert info is not None and info.trace is not None
    trace = info.trace
    inputs = definition.inputs
    expected = {
        "x": (inputs.load.channel, "raw", 1.0, 0.0),
        "y": (inputs.rpm.channel, "display", 1.0, 0.0),
    }
    for name, axis in (("x", trace.x), ("y", trace.y)):
        actual = (axis.channel, axis.axis_units, axis.scale, axis.offset)
        if actual != expected[name]:
            raise TwinRefusalError(
                "mapping_mismatch",
                f"rom_viewer.yaml maps the {name.upper()} trace as {actual}; the twin "
                f"reads {expected[name]}",
            )
    if not trace.clamps:
        raise TwinRefusalError(
            "mapping_unverified", "rom_viewer.yaml declares no clamp past the axis ends"
        )


def resolve_table(
    image: bytes,
    rom_map: RomMap,
    definition: TwinDefinition,
    *,
    viewer: ViewerDefinition,
    application_model: tuple[ApplicationModel, str],
    expected_sha256: str,
    slot: int,
) -> ResolvedTable:
    """The table the ECM reads, from a W08 library image. Refuses by name:
    ``mapping_unverified``/``mapping_mismatch`` (W05's trace mapping),
    ``application_model_mismatch``, ``image_hash_mismatch``, ``wrong_program``
    (the application model's program hash, then the calibration ID),
    ``patch_missing``, ``table_missing``, ``table_unverified``,
    ``axis_unverified``, ``unmodelled_logic`` (the twin's constants or the
    model's calibration conditions)."""
    check_trace_mapping(viewer, definition)
    applied = bind_application(application_model, definition)
    sha = sha256_hex(image)
    if sha != expected_sha256:
        raise TwinRefusalError(
            "image_hash_mismatch",
            f"the session ran plain image {expected_sha256}; this image is {sha}",
        )
    try:
        check_image(image, application_model[0])
    except ApplicationModelRefusalError as exc:
        raise TwinRefusalError(exc.reason, exc.message) from exc
    calibration_id = read_calibration_id(image, rom_map)
    if calibration_id != definition.program.calibration_id:
        raise TwinRefusalError(
            "wrong_program",
            f"image calibration ID {calibration_id!r} is not the decoded program "
            f"{definition.program.calibration_id!r}",
        )
    ref = definition.table
    entries = [t for t in table_values(image, rom_map) if t["kind"] == ref.rom_map_kind]
    entry = next((t for t in entries if t["slot"] == slot), None)
    if entry is None:
        raise TwinRefusalError(
            "table_missing",
            f"rom_map.yaml places no {ref.rom_map_kind!r} table in slot {slot}",
        )
    if entry["confidence"] != "verified":
        raise TwinRefusalError(
            "table_unverified",
            f"{entry['id']} in slot {slot} is {entry['confidence']}, not verified",
        )
    axes: dict[str, dict[str, Any]] = {}
    for name, axis_ref, payload in (
        ("rows", ref.rows, entry["axes"]["rows"]),
        ("columns", ref.columns, entry["axes"]["cols"]),
    ):
        if payload is None or payload["id"] != axis_ref.rom_map_axis:
            raise TwinRefusalError(
                "table_missing",
                f"{entry['id']} has no {name} axis {axis_ref.rom_map_axis!r}",
            )
        if payload["confidence"] != "verified":
            raise TwinRefusalError(
                "axis_unverified",
                f"{axis_ref.rom_map_axis} is {payload['confidence']}, not verified",
            )
        axes[name] = payload
    _check_final_command_constants(image, definition)
    scaler = next(
        (
            t["raw"][0][0]
            for t in table_values(image, rom_map)
            if t["id"] == definition.x_formula.scaler_rom_map_table
        ),
        None,
    )
    return ResolvedTable(
        cells=tuple(tuple(int(v) for v in row) for row in entry["raw"]),
        row_breakpoints=tuple(v * ref.rows.compare_multiplier for v in axes["rows"]["raw"]),
        column_breakpoints=tuple(
            v * ref.columns.compare_multiplier for v in axes["columns"]["raw"]
        ),
        row_axis_raw=tuple(axes["rows"]["raw"]),
        column_axis_raw=tuple(axes["columns"]["raw"]),
        image_sha256=sha,
        revision=definition.verified_revision(sha),
        slot=slot,
        table_id=entry["id"],
        row_axis_id=ref.rows.rom_map_axis,
        column_axis_id=ref.columns.rom_map_axis,
        source="rom_library",
        load_point_scaler=None if scaler is None else int(scaler),
        final_command_constants_checked=True,
        application=applied,
    )


def table_from_calibration(
    calibration: Any,
    definition: TwinDefinition,
    *,
    viewer: ViewerDefinition,
    application_model: tuple[ApplicationModel, str],
    revision: str,
    image_sha256: str,
    slot: int,
) -> ResolvedTable:
    """The table transcribed for ``revision`` in ``afr_calibration.yaml``.

    For checks that must run without the private ROM. The image hash still
    names which bytes these are (the registry's ``image_sha256`` for the
    revision); the private-ROM test pins them equal to that image's slot.
    Without the bytes, the application model must list that exact image as
    checked against its program (``program_unverified``), and the calibration
    conditions stay unchecked (``final_command_constants_unchecked``)."""
    check_trace_mapping(viewer, definition)
    applied = bind_application(application_model, definition)
    try:
        check_image_sha(image_sha256, application_model[0])
    except ApplicationModelRefusalError as exc:
        raise TwinRefusalError(exc.reason, exc.message) from exc
    target = calibration.target_map(revision)
    if target is None or target.raw_values is None:
        raise TwinRefusalError("table_missing", f"no raw target table recorded for {revision!r}")
    if target.raw_rpm_breakpoints is None or target.raw_load_breakpoints is None:
        raise TwinRefusalError("table_missing", f"no raw axes recorded for {revision!r}")
    provenance = getattr(target, "provenance", None)
    recorded_slot = getattr(provenance, "map_slot", None)
    if recorded_slot is not None and recorded_slot != slot:
        raise TwinRefusalError(
            "slot_unresolved", f"{revision!r} was transcribed from slot {recorded_slot}, not {slot}"
        )
    ref = definition.table
    return ResolvedTable(
        cells=tuple(tuple(int(v) for v in row) for row in target.raw_values),
        row_breakpoints=tuple(v * ref.rows.compare_multiplier for v in target.raw_rpm_breakpoints),
        column_breakpoints=tuple(
            v * ref.columns.compare_multiplier for v in target.raw_load_breakpoints
        ),
        row_axis_raw=tuple(target.raw_rpm_breakpoints),
        column_axis_raw=tuple(target.raw_load_breakpoints),
        image_sha256=image_sha256,
        revision=revision,
        slot=slot,
        table_id=ref.rom_map_kind,
        row_axis_id=ref.rows.rom_map_axis,
        column_axis_id=ref.columns.rom_map_axis,
        source="afr_calibration",
        load_point_scaler=None,
        final_command_constants_checked=False,
        application=applied,
    )


# -- map identity -------------------------------------------------------------


@dataclass(frozen=True)
class SlotResolution:
    slot: int | None
    reason: str | None
    detail: str


def resolve_slot(
    definition: TwinDefinition, *, magic: int, active_map: int, selector: int
) -> SlotResolution:
    """The active slot from one identity read, or the named reason it is not
    resolved: ``map_uninitialised`` (magic absent: UpRev's init has not run,
    the RAM copy is unknown) or ``slot_ambiguous`` (a value the rules do not
    cover, e.g. a selector stepped but not yet committed)."""
    rules = definition.map_identity
    if magic != rules.magic.valid:
        return SlotResolution(None, "map_uninitialised", f"magic 0x{magic:08X}")
    if rules.active_map.committed_min <= active_map <= rules.active_map.committed_max:
        return SlotResolution(active_map, None, f"committed slot {active_map}")
    default = rules.default_slot
    if active_map == default.active_map and selector == default.selector:
        return SlotResolution(default.slot, None, f"default slot {default.slot} (no commit)")
    return SlotResolution(
        None, "slot_ambiguous", f"active map {active_map} with selector {selector}"
    )


# -- the pure prediction ------------------------------------------------------


@dataclass(frozen=True)
class Prediction:
    branch: Branch
    lookup: LookupResult
    #: Lookup value (a table cell, or an interpolation of cells).
    cell: int
    #: ``None`` when the lookup lands in the enrichment gate's hysteresis band,
    #: whose state is not captured.
    word: int | None
    phi: float | None
    reason: str | None


def predict(
    table: ResolvedTable,
    definition: TwinDefinition,
    *,
    load_raw: int,
    rpm_raw: int,
    flag_raw: int,
) -> Prediction:
    """One tuple of raw lookup inputs to the command the decoded logic gives.

    The forced-column flag replaces the load input with the axis's last
    column (the firmware's 0xFF, which clamps there). The enrichment gate is
    applied as decoded; a lookup inside its hysteresis band is ambiguous and
    carries a reason instead of a word.

    The arithmetic is :func:`src.decode.target_phi_command.target_command`
    (the E06.1 contract's ``target_command``); this unpacks the definition
    into its values and adds the display ratio."""
    gate = definition.final_command.enrichment_gate
    command = target_command(
        table.cells,
        table.row_breakpoints,
        table.column_breakpoints,
        rpm_raw=rpm_raw,
        load_raw=load_raw,
        flag_raw=flag_raw,
        flag_bit=definition.inputs.forced_column_flag.bit,
        load_bits=definition.inputs.load.bits,
        gate=GateConstants(gate.floor_cell, gate.threshold_cell, gate.hysteresis_cells),
        cell_to_word_shift=definition.table.cell_to_word_shift,
    )
    word = command.word
    phi = None if word is None else word / (1 << definition.reference.q_bits)
    return Prediction(command.branch, command.lookup, command.cell, word, phi, command.reason)


def x_from_bfs(bfs: int, scaler: int, definition: TwinDefinition) -> int:
    """The load input from the BFS word, as the ECM computes it."""
    formula = definition.x_formula
    return _x_from_bfs(bfs, scaler, shift=formula.shift, cap=formula.cap)


# -- the stored-session adapter -------------------------------------------------


@dataclass
class _Series:
    times: np.ndarray
    values: np.ndarray
    basis: list[str]


def _series(long: pd.DataFrame, spec: str) -> _Series:
    rows = long[long["spec"] == spec]
    basis = (
        rows["timestamp_basis"].astype(str).tolist()
        if "timestamp_basis" in rows
        else ["unknown"] * len(rows)
    )
    return _Series(
        times=to_seconds(rows["timestamp"]),
        values=pd.to_numeric(rows["value"], errors="coerce").to_numpy(dtype=float),
        basis=basis,
    )


def _raw(series: _Series, spec: str, dids_path: Path, catalog: DidMap | None) -> np.ndarray:
    """``series`` back in raw counts, or a ``wrong_units`` refusal."""
    if not len(series.values):
        return series.values
    decode = stored_decode(spec, dids_path, catalog=catalog)
    if decode is None:
        raise TwinRefusalError(
            "wrong_units", f"{spec} is not a catalog memory channel; its raw counts are unknown"
        )
    raw = raw_counts(series.values, decode)
    if raw is None:
        raise TwinRefusalError(
            "wrong_units",
            f"{spec} values do not return to whole raw counts under the catalog decode "
            f"(scale {decode[0]}, offset {decode[1]}): stored under another decode",
        )
    return raw


def _bracket(series: _Series, t: float, max_age: float) -> tuple[int | None, int | None]:
    """Indices of the latest reading at/before ``t`` and the next reading
    after that one, each within ``max_age`` of ``t`` (``None`` when missing).

    The two are always distinct reads: a reading stamped exactly at ``t`` is
    the prior, and the bracket still needs a later one (see
    :func:`~src.signals.time_lookup.bracketing_sample_indices`). Returning the
    same read on both sides would let a change between it and the next read (a
    map switch, a mode change) pass as a stable bracket."""
    prior, nxt = bracketing_sample_indices(series.times, np.array([t]), max_age_s=max_age)
    return (
        None if prior[0] == MISSING else int(prior[0]),
        None if nxt[0] == MISSING else int(nxt[0]),
    )


@dataclass
class _Candidate:
    index: int
    time: float
    sentinel: bool
    word: int | None
    record: dict[str, Any]
    reasons: list[tuple[State, str]]
    prediction: Prediction | None = None


def _registered(reason: str) -> State | None:
    """The state :data:`REASONS` gives ``reason`` (by its template's prefix
    for a templated one), or None when it is not registered."""
    if reason in REASONS and not reason.endswith(":"):
        return REASONS[reason]
    prefix, sep, rest = reason.partition(":")
    return REASONS.get(prefix + sep) if sep and rest else None


def _reason(state: State, reason: str | None) -> tuple[State, str]:
    """One record reason, refused unless :data:`REASONS` registers it under
    ``state`` (``reason_unregistered``)."""
    if reason is None or _registered(reason) != state:
        raise TwinRefusalError(
            "reason_unregistered",
            f"({state}, {reason}) is not in target_phi.REASONS; register it before emitting it",
        )
    return state, reason


def _excluded_mode(states: dict[int, str], name: str) -> tuple[State, str]:
    """An ``excluded`` reason naming a fuel-system mode the gate does not
    admit: one of the mode gate's declared state names, never another string."""
    if name not in states.values() or name in REASONS:
        raise TwinRefusalError(
            "reason_unregistered", f"{name} is not a declared fuel-system mode name"
        )
    return "excluded", name


def excluded_mode_reasons(definition: TwinDefinition) -> list[str]:
    """The fuel-system mode names an ``excluded`` record can carry: every
    declared mode state the gate does not admit."""
    mode = definition.gates.mode
    return sorted({name for name in mode.states.values() if name not in mode.eligible})


def _within(series: _Series, start: float | None, end: float | None) -> _Series:
    keep = np.ones(len(series.times), dtype=bool)
    if start is not None:
        keep &= series.times >= start
    if end is not None:
        keep &= series.times < end
    index = np.flatnonzero(keep)
    return _Series(series.times[index], series.values[index], [series.basis[i] for i in index])


def _sorted(series: _Series) -> _Series:
    order = np.argsort(series.times, kind="stable")
    return _Series(series.times[order], series.values[order], [series.basis[i] for i in order])


def evaluated_specs(definition: TwinDefinition) -> list[str]:
    """Every channel the evaluator reads: reference and sentinel, the lookup
    inputs, the map identity and each gate's source, in that order."""
    gates = definition.gates
    return list(
        dict.fromkeys(
            [
                definition.reference.channel,
                definition.reference.sentinel_channel,
                *definition.inputs.channels().values(),
                *definition.map_identity.channels(),
                *gates.coolant.sources,
                gates.mode.channel,
                *(c.channel for c in gates.steadiness.channels),
            ]
        )
    )


def raw_specs(definition: TwinDefinition) -> list[str]:
    """The channels read as raw counts (catalog decode undone)."""
    return list(
        dict.fromkeys(
            [
                *definition.inputs.channels().values(),
                *definition.map_identity.channels(),
                definition.reference.channel,
            ]
        )
    )


def decision_horizon_s(definition: TwinDefinition) -> float:
    """How far past a command sample the reads deciding it can lie: the
    lookup inputs' skew, the after-side identity read (and its partners'
    skew), and the after-side mode and coolant reads. A sample is final once
    every stream has been read this far past it."""
    gates = definition.gates
    skew = gates.max_acquisition_skew_s
    return max(
        skew,
        definition.map_identity.max_age_s + skew,
        gates.mode.max_age_s,
        gates.coolant.max_age_s,
    )


def lookback_s(definition: TwinDefinition) -> float:
    """How far before a command sample the reads deciding it can lie: the
    steadiness dwell, the before-side identity, mode and coolant reads, and
    the inputs' skew."""
    gates = definition.gates
    skew = gates.max_acquisition_skew_s
    return max(
        skew,
        gates.steadiness.dwell_s,
        definition.map_identity.max_age_s + skew,
        gates.mode.max_age_s,
        gates.coolant.max_age_s,
    )


def new_candidate(
    definition: TwinDefinition,
    *,
    index: int,
    time: float,
    sentinel: bool,
    row: int,
    word: int | None,
    basis: str,
) -> _Candidate:
    """One command sample (a value or a fuel-cut state) as an undecided
    candidate. ``row`` is the sample's row in its own channel's series."""
    reference = definition.reference
    candidate = _Candidate(
        index=index,
        time=time,
        sentinel=sentinel,
        word=word,
        record={
            "sample": {
                "spec": reference.sentinel_channel if sentinel else reference.channel,
                "row": row,
                "acquired_at": format_epoch(time, nanoseconds=True),
                "timestamp_basis": basis,
                "observed_word": word,
                "observed_phi": None if word is None else word / (1 << reference.q_bits),
            }
        },
        reasons=[],
    )
    if basis != definition.gates.clock:
        candidate.reasons.append(_reason("unevaluable", "unsupported_clock"))
    if sentinel:
        candidate.reasons.append(_reason("unevaluable", "sentinel"))
    return candidate


def evaluate_session(
    session: Path | pd.DataFrame,
    definition: TwinDefinition,
    table: ResolvedTable,
    *,
    definition_sha256: str,
    dids_path: Path = DIDS_YAML,
    session_complete: bool = True,
    tolerance_words: int = 1,
    window: tuple[float | None, float | None] | None = None,
) -> dict[str, Any]:
    """Every command sample in ``session``, predicted where the declared
    applicability allows and accounted for where it does not.

    ``window`` (``[start, end)`` acquisition seconds, either side open) keeps
    only the reads inside it: one identity segment of a session that spans a
    flash, evaluated as on its own -- what a live stream rebuilt at the flash
    instant decides.

    ``session_complete=False`` (a capture still recording, the streaming case)
    leaves a sample whose decision needs later reads ``pending``; a finished
    session names what was missing instead. ``tolerance_words`` is the
    acceptance target in reference counts (one Q14 count), never widened to
    absorb a miss. Raises :class:`TwinRefusalError` only when the session as a
    whole cannot be evaluated (a channel stored under another decode)."""
    gates = definition.gates
    skew = gates.max_acquisition_skew_s
    inputs = definition.inputs.channels()
    reference = definition.reference
    specs = evaluated_specs(definition)
    try:
        long = read_channels(session, specs, dids_path=dids_path, acquired_time=True)
    except (ChannelSpecError, KeyError) as exc:
        raise TwinRefusalError("gate_unreadable", str(exc)) from exc

    series = {spec: _sorted(_series(long, spec)) for spec in specs}
    if window is not None:
        series = {spec: _within(each, *window) for spec, each in series.items()}
    session_end = max(
        (float(s.times[-1]) for s in series.values() if len(s.times)), default=float("nan")
    )
    catalog = load_did_map(dids_path)
    check_windows(definition, catalog)
    raw = {spec: _raw(series[spec], spec, dids_path, catalog) for spec in raw_specs(definition)}

    # One candidate per observed command sample: a value or a fuel-cut state.
    target = series[reference.channel]
    sentinel = series[reference.sentinel_channel]
    stamps = [(float(t), False, i) for i, t in enumerate(target.times)]
    stamps += [(float(t), True, i) for i, t in enumerate(sentinel.times)]
    stamps.sort()
    candidates = [
        new_candidate(
            definition,
            index=position,
            time=t,
            sentinel=is_sentinel,
            row=i,
            word=None if is_sentinel else int(raw[reference.channel][i]),
            basis=(sentinel if is_sentinel else target).basis[i],
        )
        for position, (t, is_sentinel, i) in enumerate(stamps)
    ]

    # Pair each lookup input one-to-one with the command samples.
    candidate_times = np.array([c.time for c in candidates], dtype=float)
    policy = PairingPolicy.within(pd.Timedelta(seconds=skew), reference="a")
    pairs: dict[str, dict[int, int]] = {}
    for name, spec in inputs.items():
        matched = match_ordered(candidate_times, series[spec].times, policy)
        pairs[name] = dict(zip(matched.index_a.tolist(), matched.index_b.tolist(), strict=True))

    first_read = first_reads(series)
    for candidate in candidates:
        _assess(
            candidate,
            definition=definition,
            table=table,
            series=series,
            raw=raw,
            pairs=pairs,
            first_read=first_read,
            session_end=session_end,
            session_complete=session_complete,
            tolerance_words=tolerance_words,
        )

    result = _summarise(
        candidates,
        definition=definition,
        table=table,
        definition_sha256=definition_sha256,
        tolerance_words=tolerance_words,
    )
    result["accounting"]["input_pairing"] = _input_pairing(candidates, inputs, series)
    return result


def _input_refs(
    candidate: _Candidate,
    *,
    inputs: dict[str, str],
    series: dict[str, _Series],
    raw: dict[str, np.ndarray],
    pairs: dict[str, dict[int, int]],
) -> dict[str, dict[str, Any] | None]:
    refs: dict[str, dict[str, Any] | None] = {}
    for name, spec in inputs.items():
        j = pairs[name].get(candidate.index)
        if j is None:
            refs[name] = None
            continue
        t = float(series[spec].times[j])
        refs[name] = {
            "spec": spec,
            "row": j,
            "acquired_at": format_epoch(t, nanoseconds=True),
            "skew_s": round(t - candidate.time, 6),
            # One window read stamps its channels together; the ECM may still
            # have updated them in different loop iterations.
            "same_read": t == candidate.time,
            "raw": int(raw[spec][j]),
        }
    return refs


def _identity(
    candidate: _Candidate,
    definition: TwinDefinition,
    series: dict[str, _Series],
    raw: dict[str, np.ndarray],
    *,
    session_end: float,
    session_complete: bool,
) -> tuple[SlotResolution | None, tuple[State, str] | None, dict[str, Any]]:
    rules = definition.map_identity
    magic, active, selector = (
        series[rules.magic.channel],
        series[rules.active_map.channel],
        series[rules.selector.channel],
    )
    reads: list[SlotResolution] = []
    detail: dict[str, Any] = {}
    for side, pick in (("before", 0), ("after", 1)):
        index = _bracket(magic, candidate.time, rules.max_age_s)[pick]
        if index is None:
            detail[side] = None
            continue
        t = float(magic.times[index])
        others = []
        for other in (active, selector):
            k = int(np.argmin(np.abs(other.times - t))) if len(other.times) else None
            others.append(
                None
                if k is None
                or abs(float(other.times[k]) - t) > definition.gates.max_acquisition_skew_s
                else k
            )
        if others[0] is None or others[1] is None:
            detail[side] = None
            continue
        resolution = resolve_slot(
            definition,
            magic=int(raw[rules.magic.channel][index]),
            active_map=int(raw[rules.active_map.channel][others[0]]),
            selector=int(raw[rules.selector.channel][others[1]]),
        )
        detail[side] = {
            "acquired_at": format_epoch(t, nanoseconds=True),
            "slot": resolution.slot,
            "reason": resolution.reason,
            "detail": resolution.detail,
        }
        reads.append(resolution)
    if detail.get("after") is None and detail.get("before") is not None:
        if not session_complete and session_end - candidate.time < rules.max_age_s:
            return None, _reason("pending", "awaiting_identity_read"), detail
    if len(reads) < 2:
        return None, _reason("unevaluable", "map_identity_unbracketed"), detail
    for resolution in reads:
        if resolution.reason is not None:
            return None, _reason("unevaluable", resolution.reason), detail
    if reads[0].slot != reads[1].slot:
        return None, _reason("unevaluable", "map_switching"), detail
    return reads[0], None, detail


def _assess(
    candidate: _Candidate,
    *,
    definition: TwinDefinition,
    table: ResolvedTable,
    series: dict[str, _Series],
    raw: dict[str, np.ndarray],
    pairs: dict[str, dict[int, int]],
    first_read: Callable[[str], float],
    session_end: float,
    session_complete: bool,
    tolerance_words: int,
) -> None:
    """Decide one candidate. ``first_read(spec)`` is the acquisition time of
    the run's earliest read of ``spec`` (``inf`` when none): every "was it
    captured" and "which coolant source" question is asked of it per sample
    and causally (:func:`captured_by`, :func:`coolant_source`), so a stored
    session and a stream deciding the same sample give the same answer."""
    gates = definition.gates
    skew = gates.max_acquisition_skew_s
    t = candidate.time
    record = candidate.record
    reasons = candidate.reasons
    inputs = definition.inputs.channels()

    # Map identity: bracketing reads that resolve to the table's slot.
    slot, identity_reason, identity_detail = _identity(
        candidate,
        definition,
        series,
        raw,
        session_end=session_end,
        session_complete=session_complete,
    )
    record["map_identity"] = identity_detail
    if identity_reason is not None:
        reasons.append(identity_reason)
    elif slot is not None and slot.slot != table.slot:
        reasons.append(_reason("unevaluable", "slot_unresolved"))

    # Lookup inputs, each within the skew of the command and of each other.
    refs = _input_refs(candidate, inputs=inputs, series=series, raw=raw, pairs=pairs)
    record["inputs"] = refs
    present = [ref for ref in refs.values() if ref is not None]
    for name, spec in inputs.items():
        if refs[name] is not None:
            continue
        if not captured_by(definition, first_read, spec, t):
            reasons.append(_reason("unevaluable", f"input_not_captured:{name}"))
        elif not session_complete and session_end - t < skew:
            reasons.append(_reason("pending", f"awaiting_input:{name}"))
        else:
            reasons.append(_reason("unevaluable", f"input_stale:{name}"))
    tuple_times = [t, *(t + ref["skew_s"] for ref in present)]
    spread = max(tuple_times) - min(tuple_times)
    record["tuple_skew_s"] = round(spread, 6)
    if len(present) == len(inputs) and spread > skew:
        reasons.append(_reason("unevaluable", "input_stale:tuple_spread"))
    for ref in present:
        if ref["spec"] and series[ref["spec"]].basis[ref["row"]] != gates.clock:
            reasons.append(_reason("unevaluable", "unsupported_clock"))

    # The prediction itself, whenever its inputs exist (a diagnostic
    # difference is kept for excluded and transient samples too).
    if len(present) == len(inputs) and slot is not None and slot.slot == table.slot:
        assert refs["load"] is not None and refs["rpm"] is not None and refs["flag"] is not None
        prediction = predict(
            table,
            definition,
            load_raw=refs["load"]["raw"],
            rpm_raw=refs["rpm"]["raw"],
            flag_raw=refs["flag"]["raw"],
        )
        candidate.prediction = prediction
        if prediction.reason is not None:
            reasons.append(_reason("unevaluable", prediction.reason))
        region = getattr(definition.regions, prediction.branch)
        rpm_raw = refs["rpm"]["raw"]
        load_raw = refs["load"]["raw"]
        inside = region.rpm_raw[0] <= rpm_raw <= region.rpm_raw[1] and (
            region.load_raw is None or region.load_raw[0] <= load_raw <= region.load_raw[1]
        )
        if not inside:
            reasons.append(_reason("unevaluable", "uncovered_region"))

    # Gates: warmth, mode, steadiness — from inputs and mode, never the residual.
    record["gates"] = _gates(
        candidate, definition, series, first_read, session_end, session_complete
    )
    for state, reason in record["gates"].pop("_reasons"):
        reasons.append((state, reason))

    _finish(candidate, definition, tolerance_words)


def first_reads(series: dict[str, _Series]) -> Callable[[str], float]:
    """``first_read`` over whole sorted series (the stored adapter)."""
    return lambda spec: float(series[spec].times[0]) if len(series[spec].times) else math.inf


def captured_by(
    definition: TwinDefinition, first_read: Callable[[str], float], spec: str, t: float
) -> bool:
    """Whether ``spec`` had been read by the time a sample at ``t`` is decided:
    a read at or before ``t + decision_horizon_s``. Asked per sample, never of
    the whole session, so a channel first read later in a session is *not
    captured* for the samples before it on every path."""
    return first_read(spec) <= t + decision_horizon_s(definition)


def coolant_source(
    definition: TwinDefinition, first_read: Callable[[str], float], t: float
) -> str | None:
    """The coolant source for a sample at ``t``: the highest-priority declared
    source with a read at or before ``t``, or inside the gate's after-side
    bracket (``t + max_age_s``). Per sample and causal, so a higher-priority
    source that first appears mid-session is used from then on, on every
    path."""
    coolant = definition.gates.coolant
    return next((s for s in coolant.sources if first_read(s) <= t + coolant.max_age_s), None)


def _gates(
    candidate: _Candidate,
    definition: TwinDefinition,
    series: dict[str, _Series],
    first_read: Callable[[str], float],
    session_end: float,
    session_complete: bool,
) -> dict[str, Any]:
    gates = definition.gates
    t = candidate.time
    out: dict[str, Any] = {}
    reasons: list[tuple[State, str]] = []

    coolant = gates.coolant
    coolant_spec = coolant_source(definition, first_read, t)
    if coolant_spec is None:
        reasons.append(_reason("unevaluable", "gate_missing:coolant"))
        out["coolant"] = None
    else:
        s = series[coolant_spec]
        prior, nxt = _bracket(s, t, coolant.max_age_s)
        values = [float(s.values[i]) for i in (prior, nxt) if i is not None]
        out["coolant"] = {"spec": coolant_spec, "values": values}
        if not values:
            reasons.append(_reason("unevaluable", "gate_stale:coolant"))
        elif min(values) < coolant.min_c:
            reasons.append(_reason("excluded", "cold"))

    mode = gates.mode
    s = series[mode.channel]
    if not captured_by(definition, first_read, mode.channel, t):
        reasons.append(_reason("unevaluable", "gate_missing:mode"))
        out["mode"] = None
    else:
        prior, nxt = _bracket(s, t, mode.max_age_s)
        names = [
            mode.states.get(int(s.values[i]), f"unknown_{s.values[i]:g}")
            for i in (prior, nxt)
            if i is not None and math.isfinite(s.values[i])
        ]
        out["mode"] = {"spec": mode.channel, "states": names}
        if prior is None or nxt is None:
            if nxt is None and not session_complete and session_end - t < mode.max_age_s:
                reasons.append(_reason("pending", "awaiting_mode_read"))
            else:
                reasons.append(_reason("unevaluable", "gate_stale:mode"))
        elif len(names) < 2 or any(name.startswith("unknown_") for name in names):
            reasons.append(_reason("unevaluable", "mode_unknown"))
        elif names[0] != names[1]:
            reasons.append(_reason("excluded", "mode_transition"))
        elif names[0] not in mode.eligible:
            reasons.append(_excluded_mode(mode.states, names[0]))

    steady = gates.steadiness
    steady_out: dict[str, dict[str, Any]] = {}
    for channel in steady.channels:
        s = series[channel.channel]
        window = (s.times >= t - steady.dwell_s) & (s.times <= t)
        dwell = s.values[window]
        dwell = dwell[np.isfinite(dwell)]
        if len(dwell) < steady.min_samples:
            steady_out[channel.channel] = {"n": int(len(dwell)), "span": None}
            reasons.append(_reason("unevaluable", f"steadiness_unobserved:{channel.channel}"))
            continue
        span = float(dwell.max() - dwell.min())
        steady_out[channel.channel] = {"n": int(len(dwell)), "span": round(span, 6)}
        if channel.max_span is not None:
            limit_exceeded = span > channel.max_span
        else:
            assert channel.max_relative_span is not None
            mid = float(np.median(np.abs(dwell)))
            limit_exceeded = mid == 0 or span / mid > channel.max_relative_span
        if limit_exceeded:
            reasons.append(_reason("excluded", "transient"))
    out["steadiness"] = steady_out
    out["_reasons"] = reasons
    return out


_PRIORITY: dict[State, int] = {"unevaluable": 0, "pending": 1, "excluded": 2, "eligible": 3}


def _finish(candidate: _Candidate, definition: TwinDefinition, tolerance_words: int) -> None:
    record = candidate.record
    reasons = list(dict.fromkeys(candidate.reasons))
    # A pending decision outranks a stale one only while reads can still come;
    # otherwise the worst state wins: unevaluable, pending, excluded, eligible.
    state: State = "eligible"
    if reasons:
        state = min((s for s, _ in reasons), key=lambda s: _PRIORITY[s])
    record["state"] = state
    record["reasons"] = [reason for s, reason in reasons if s == state] + [
        reason for s, reason in reasons if s != state
    ]
    record["reason"] = record["reasons"][0] if record["reasons"] else None
    prediction = candidate.prediction
    observed = candidate.word
    if prediction is None:
        record.update(branch=None, lookup=None, predicted_word=None, predicted_phi=None)
    else:
        lookup = prediction.lookup
        record.update(
            branch=prediction.branch,
            lookup={
                "row": lookup.row.index,
                "row_fraction_q16": lookup.row_fraction,
                "row_edge": lookup.row.edge,
                "column": lookup.column.index,
                "column_fraction_q16": lookup.column_fraction,
                "column_edge": lookup.column.edge,
                "cells": [list(cell) for cell in lookup.cells],
                "cell_value": prediction.cell,
            },
            predicted_word=prediction.word,
            predicted_phi=prediction.phi,
        )
    q = 1 << definition.reference.q_bits
    if prediction is not None and prediction.word is not None and observed is not None:
        difference = observed - prediction.word
        record["difference_words"] = difference
        record["difference_phi"] = difference / q
        record["ratio"] = observed / prediction.word if prediction.word else None
        record["within_tolerance"] = abs(difference) <= tolerance_words
    else:
        record["difference_words"] = None
        record["difference_phi"] = None
        record["ratio"] = None
        record["within_tolerance"] = None
    # Filled by _summarise once run-level qualification is known.
    record["qualified_residual_phi"] = None


def duplicate_use(records: Sequence[dict[str, Any]]) -> int:
    """How many observations serve more than one candidate: a command sample
    counted twice, or one input read placed in two tuples. Read back from
    the records themselves, so it checks the pairing rather than restating
    its contract."""
    used: Counter[tuple[str, int]] = Counter()
    for record in records:
        used.update(_observations(record))
    return sum(1 for n in used.values() if n > 1)


def _observations(record: dict[str, Any]) -> list[tuple[str, int]]:
    """Every observation a record cites: its command sample and input reads."""
    keys = [(record["sample"]["spec"], record["sample"]["row"])]
    for ref in (record.get("inputs") or {}).values():
        if ref is not None:
            keys.append((ref["spec"], ref["row"]))
    return keys


def _input_pairing(
    candidates: list[_Candidate], inputs: dict[str, str], series: dict[str, _Series]
) -> dict[str, dict[str, Any]]:
    """Per lookup input: how many candidates it paired within the skew, and
    how far away its nearest read was (median over candidates, seconds) --
    the measurement that says why a capture yields no tuples."""
    out: dict[str, dict[str, Any]] = {}
    for name, spec in inputs.items():
        times = series[spec].times
        paired = sum(1 for c in candidates if c.record["inputs"].get(name) is not None)
        nearest = [float(np.min(np.abs(times - c.time))) for c in candidates] if len(times) else []
        out[name] = {
            "spec": spec,
            "paired": paired,
            "candidates": len(candidates),
            "median_nearest_read_s": round(float(np.median(nearest)), 3) if nearest else None,
        }
    return out


def _summarise(
    candidates: list[_Candidate],
    *,
    definition: TwinDefinition,
    table: ResolvedTable,
    definition_sha256: str,
    tolerance_words: int,
) -> dict[str, Any]:
    records = [c.record for c in candidates]
    qualification = Qualification.of(table, definition, definition_sha256)
    accounting = Accounting()
    for record in records:
        qualification.apply(record)
        accounting.add(record)
    return {
        "provenance": qualification.provenance,
        "pairing": pairing_block(definition),
        "tolerance_words": tolerance_words,
        "qualification_blockers": list(qualification.blockers),
        "coolant_sources": accounting.coolant_sources(definition),
        "accounting": accounting.counts(),
        "coverage": accounting.coverage(),
        "capture_requirement": capture_requirement(accounting.eligible, definition),
        "records": records,
    }


def pairing_block(definition: TwinDefinition) -> dict[str, Any]:
    return {
        "max_acquisition_skew_s": definition.gates.max_acquisition_skew_s,
        "clock": definition.gates.clock,
        "method": "match_ordered, one-to-one per input, command samples as reference",
    }


@dataclass(frozen=True)
class Qualification:
    """What may turn an eligible difference into a qualified residual: the
    application model's blockers (unresolved logic between the lookup and the
    final word, an unverified RAM copy), then the table's own. One per table,
    applied to every record the table predicted -- stored or streamed."""

    blockers: tuple[str, ...]
    provenance: dict[str, Any]

    @classmethod
    def of(
        cls, table: ResolvedTable, definition: TwinDefinition, definition_sha256: str
    ) -> Qualification:
        blockers: list[str] = list(table.application.blockers)
        if not table.final_command_constants_checked:
            blockers.append("final_command_constants_unchecked")
        provenance = {
            **table.provenance(),
            "program": definition.program.calibration_id,
            "decoder": {"profile_file": TWIN_FILE, "sha256": definition_sha256},
            "application_model": table.application.provenance(),
            "evaluator_version": EVALUATOR_VERSION,
            "kernel_version": KERNEL_VERSION,
        }
        return cls(tuple(blockers), provenance)

    def apply(self, record: dict[str, Any]) -> None:
        if record["state"] == "eligible" and not self.blockers:
            record["qualified_residual_phi"] = record["difference_phi"]
        if record["predicted_word"] is not None:
            record["provenance"] = self.provenance


class Accounting:
    """The run's counts, folded one decided record at a time, so a stored
    session and a stream that decides records in batches give the same
    numbers. Bounded: per-cell counts and ranges, never the records."""

    def __init__(self) -> None:
        self.candidates = 0
        self.states: Counter[str] = Counter()
        self.by_reason: dict[str, Counter[str]] = {}
        self.eligible: dict[str, dict[str, int]] = {
            "ordinary": {"pass": 0, "miss": 0},
            "override": {"pass": 0, "miss": 0},
        }
        self.transient: Counter[str] = Counter()
        self._cells: dict[str, Counter[tuple[int, int]]] = {}
        self._ranges: dict[str, dict[str, list[int]]] = {}
        self._used: Counter[tuple[str, int]] = Counter()
        self.duplicate_use = 0
        self._coolant: set[str] = set()

    def add(self, r: dict[str, Any]) -> None:
        self.candidates += 1
        coolant = (r.get("gates") or {}).get("coolant")
        if coolant:
            self._coolant.add(coolant["spec"])
        self.states[r["state"]] += 1
        if r["state"] != "eligible":
            self.by_reason.setdefault(r["state"], Counter())[r["reason"]] += 1
        elif r["branch"] is not None:
            branch = r["branch"]
            self.eligible[branch]["pass" if r["within_tolerance"] else "miss"] += 1
            self._cells.setdefault(branch, Counter())[
                (r["lookup"]["row"], r["lookup"]["column"])
            ] += 1
            ranges = self._ranges.setdefault(branch, {})
            for name in ("rpm", "load"):
                value = r["inputs"][name]["raw"]
                bounds = ranges.setdefault(f"{name}_raw", [value, value])
                bounds[0], bounds[1] = min(bounds[0], value), max(bounds[1], value)
        if "transient" in r["reasons"] and r["within_tolerance"] is not None:
            self.transient[
                "within_tolerance" if r["within_tolerance"] else "outside_tolerance"
            ] += 1
        # duplicate_use, folded: an observation cited a second time counts once.
        for key in _observations(r):
            self._used[key] += 1
            if self._used[key] == 2:
                self.duplicate_use += 1

    def forget_before(self, rows: dict[str, int]) -> None:
        """Drop the citation memory of rows below ``rows[spec]``: a stream
        that has evicted them can never cite them again."""
        self._used = Counter(
            {key: n for key, n in self._used.items() if key[1] >= rows.get(key[0], 0)}
        )

    def coolant_sources(self, definition: TwinDefinition) -> list[str]:
        """The coolant sources the records used, in declared priority."""
        return [s for s in definition.gates.coolant.sources if s in self._coolant]

    def counts(self) -> dict[str, Any]:
        return {
            "candidates": self.candidates,
            "states": {s: int(self.states.get(s, 0)) for s in _PRIORITY},
            "reasons": {s: dict(sorted(c.items())) for s, c in sorted(self.by_reason.items())},
            "eligible": {branch: dict(v) for branch, v in self.eligible.items()},
            "transient_differences": dict(self.transient),
            "duplicate_use": self.duplicate_use,
        }

    def coverage(self) -> dict[str, dict[str, Any]]:
        return {
            branch: {
                "cells": sorted([list(cell), n] for cell, n in cells.items()),
                **{name: list(bounds) for name, bounds in self._ranges[branch].items()},
            }
            for branch, cells in self._cells.items()
        }


def capture_requirement(
    eligible: dict[str, dict[str, int]], definition: TwinDefinition
) -> dict[str, Any] | None:
    """The measurement that would supply a branch with no eligible tuple."""
    missing = [b for b in ("ordinary", "override") if not sum(eligible[b].values())]
    if not missing:
        return None
    return {
        "reason": "no_eligible_tuples",
        "branches": missing,
        "message": (
            "No eligible steady tuple for "
            + " and ".join(missing)
            + ": capture the command word with the X trace, rpm word and pedal flag "
            f"in one read cycle (all within {definition.gates.max_acquisition_skew_s * 1000:g}"
            " ms), plus fuel status, coolant and steady rpm/MAF, at warm open-loop "
            "points across the table's cells and through sustained pedal enrichment "
            "(E02.7)."
        ),
    }


# -- the independent X-formula check ---------------------------------------------


@dataclass(frozen=True)
class XReads:
    """The load input X and the BFS word it is formed from, each read on the
    acquisition clock and returned to raw counts by the catalog decode."""

    x_spec: str
    bfs_spec: str
    x_times: np.ndarray
    x_raw: np.ndarray
    bfs_times: np.ndarray
    bfs_raw: np.ndarray


def x_reads(
    session: Path | pd.DataFrame, definition: TwinDefinition, *, dids_path: Path = DIDS_YAML
) -> XReads:
    """Every X and BFS read of ``session`` in raw counts: the reads
    :func:`x_formula_check` pairs, and the twin runner's X stage too (one
    reader, so the two cannot read X differently). A channel stored under
    another decode refuses ``wrong_units``."""
    load_spec = definition.inputs.load.channel
    bfs_spec = definition.x_formula.bfs_channel
    long = read_channels(session, [load_spec, bfs_spec], dids_path=dids_path, acquired_time=True)
    x = _series(long, load_spec)
    bfs = _series(long, bfs_spec)
    catalog = load_did_map(dids_path)
    return XReads(
        x_spec=load_spec,
        bfs_spec=bfs_spec,
        x_times=x.times,
        x_raw=_raw(x, load_spec, dids_path, catalog),
        bfs_times=bfs.times,
        bfs_raw=_raw(bfs, bfs_spec, dids_path, catalog),
    )


def x_formula_check(
    session: Path | pd.DataFrame,
    definition: TwinDefinition,
    *,
    scaler: int,
    dids_path: Path = DIDS_YAML,
) -> dict[str, Any]:
    """Every same-instant X/BFS pair against ``X = min(cap, floor(BFS << shift
    / scaler))``, with each pair's difference in raw X counts.

    A regression check of the load input's formula, not a tolerance for the
    lookup: misses are counted and kept, never absorbed."""
    reads = x_reads(session, definition, dids_path=dids_path)
    x_raw, bfs_raw = reads.x_raw, reads.bfs_raw
    matched = match_ordered(
        reads.x_times,
        reads.bfs_times,
        PairingPolicy.within(pd.Timedelta(seconds=definition.gates.max_acquisition_skew_s)),
    )
    pairs: list[dict[str, Any]] = []
    differences: Counter[int] = Counter()
    for a, b, separation in zip(
        matched.index_a.tolist(), matched.index_b.tolist(), matched.separation_s, strict=True
    ):
        observed = int(x_raw[a])
        expected = x_from_bfs(int(bfs_raw[b]), scaler, definition)
        pairs.append(
            {
                "acquired_at": format_epoch(float(reads.x_times[a]), nanoseconds=True),
                "separation_s": float(separation),
                "bfs": int(bfs_raw[b]),
                "x": observed,
                "x_from_bfs": expected,
                "difference": observed - expected,
            }
        )
        differences[observed - expected] += 1
    return {
        "scaler": scaler,
        "n_pairs": len(pairs),
        "exact": differences.get(0, 0),
        "differences": {k: v for k, v in sorted(differences.items()) if k != 0},
        "unmatched": {"x": matched.unmatched_a, "bfs": matched.unmatched_b},
        "pairs": pairs,
    }


def records_digest(result: dict[str, Any], keys: Sequence[str]) -> list[dict[str, Any]]:
    """The listed fields of every record, for pinning minted expectations."""
    return [{key: record.get(key) for key in keys} for record in result["records"]]


__all__ = [
    "EVALUATOR_VERSION",
    "TWIN_FILE",
    "Accounting",
    "Prediction",
    "Qualification",
    "REASONS",
    "ResolvedTable",
    "SlotResolution",
    "TwinDefinition",
    "TwinRefusalError",
    "bind_application",
    "capture_requirement",
    "captured_by",
    "check_trace_mapping",
    "check_windows",
    "decision_horizon_s",
    "duplicate_use",
    "evaluate_session",
    "evaluated_specs",
    "excluded_mode_reasons",
    "first_reads",
    "coolant_source",
    "load_twin_definition",
    "lookback_s",
    "new_candidate",
    "pairing_block",
    "predict",
    "raw_specs",
    "records_digest",
    "resolve_slot",
    "resolve_table",
    "table_from_calibration",
    "XReads",
    "x_formula_check",
    "x_from_bfs",
    "x_reads",
]
