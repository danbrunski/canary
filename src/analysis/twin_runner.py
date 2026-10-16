"""One replay runner over the application model's node graph (E09.2a).

The twin against the running engine used to be four evaluators, each with its
own entry point, its own pairing and its own idea of an empty result
(:mod:`src.analysis.target_phi`, :mod:`src.analysis.air_charge_twin`, the X
relation in ``target_phi.x_formula_check`` and the pulse chain in
:mod:`src.analysis.fuel_plant_eval`). This module runs them as one pass over a
session: the evaluators are its kernels, called as they are, never restated.

Given an ECU resolved from a ROM (:func:`resolve_ecu`, from E08.1's
:func:`src.analysis.rom_effects.load_rom`), a kit (:func:`load_twin_kit`) and
a session directory or a frame of its raw reads (a bounded window), it walks
the application model in node order (:func:`stage_order`: ``depends_on``
topologically, the fuel task's ``schedule`` breaking ties) and, per ECU output
per sample, emits predicted, observed, residual and a named state:
``eligible`` or the reason it is not (:class:`TwinRun`).

What it makes impossible rather than documented:

* **One pairing discipline for the quantities a prediction is made from.**
  Each output sample's lookup and kernel inputs are paired with it by
  :func:`src.signals.pairing.match_ordered` on the acquisition clock,
  one-to-one: BFS's MAF, rpm and load words (:func:`air_charge_twin.pair_bfs_inputs`,
  the evaluator's own pairing since its version 2), X with its BFS read,
  target phi's X, rpm and flag (``target_phi.evaluate_session``), and the
  pulse word's BFS, rpm and MAF (``fuel_plant_eval.build_samples``).
  **Named exceptions, all slow or state context, not kernel inputs paired
  as values:** the pulse stage takes ``bfs_before`` (the steadiness check's
  read 0.5 s earlier), STFT, LTFT, baro, intake temperature, coolant and
  battery voltage as the nearest read within a declared age (reuse allowed:
  ``build_samples``' ``_nearest_value``), the ``fuel_cut_reference`` pulse
  read (nearest within three ECU pairing tolerances; it sets the dead time
  when PID 0x42 is absent), the rail pressure as a window
  median of its own reads and the reference phi as a bracket of two equal
  reads; target phi's map identity is a bracket of magic reads with the
  active-map and selector reads nearest each (argmin, within the skew), and
  its coolant, mode and steadiness gates are brackets and dwell windows.
  Those are the evaluators' declared rules and are not changed here.
* **An empty result says which kind of empty it is.** Every reason a stage
  can name maps to one :data:`EMPTY_KINDS` entry -- an input never captured,
  captured but not co-sampled, a gate, the decoded logic not covering the
  sample, or no calibration -- and a reason with no mapping refuses the run
  (``reason_unclassified``) rather than being counted as something.
* **The model decides the order and the roles.** A stage whose node the
  application model no longer marks ``implemented`` (or ``observed`` for the X
  relation), or whose ``implemented_by`` names another kernel, refuses
  (``stage_role_changed``, ``kernel_mismatch``): the runner cannot run a
  kernel the model has moved on from.
* **ECU outputs only, here.** BFS, X, the target-phi word and the injector
  pulse word depend on decoded integer logic, not on the plant. The plant
  stages (E09.2b) and the paired comparison as a caller (E09.2c) are not here.

Nothing here, or anything it imports, can reach the ECU
(``tests/test_twin_runner.py``'s clean-process import test).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
import pandas as pd

from src.analysis import air_charge_twin, target_phi
from src.analysis.air_charge_twin import (
    AirChargeCalibration,
    AirChargeRefusalError,
    BfsRecord,
    SteadyBfsReport,
    bfs_series,
    pair_bfs_inputs,
    score_bfs_read,
)
from src.analysis.channels import read_channels
from src.analysis.ecu_application import (
    ApplicationModel,
    ApplicationModelRefusalError,
    load_application_model,
    observed_specs,
)
from src.analysis.fuel_plant import (
    B06Rules,
    FuelPlantProfile,
    FuelPlantRefusalError,
    load_b06,
    load_fuel_plant,
)
from src.analysis.fuel_plant_eval import (
    PULSE_CHAIN_INPUTS,
    build_samples,
    calibration_from_image,
    pulse_chain_ratios,
    pulse_chain_states,
)
from src.analysis.injector_pulse import PULSE_COUNT_MS, PulseChainCalibration
from src.analysis.rom_effects import LoadedRom
from src.analysis.rom_map import RomMap, load_rom_map
from src.analysis.rom_trace import ViewerDefinition, load_viewer_definition
from src.analysis.target_phi import (
    ResolvedTable,
    TwinDefinition,
    TwinRefusalError,
    captured_by,
    evaluate_session,
    load_twin_definition,
    resolve_table,
    x_formula_check,
    x_from_bfs,
)
from src.decode.dids import load_did_map
from src.paths import DIDS_YAML
from src.preprocessing.loader import read_session
from src.signals.pairing import PairingPolicy, match_ordered

#: Bumped when a stage, the order, a state or the record shape changes.
RUNNER_VERSION = 1

#: The kinds an unevaluable sample can be, in the order a report lists them.
EmptyKind = Literal[
    "input_not_captured",
    "not_co_sampled",
    "gated",
    "outside_model",
    "calibration_unresolved",
    "pending",
]
EMPTY_KINDS: tuple[EmptyKind, ...] = (
    "input_not_captured",
    "not_co_sampled",
    "gated",
    "outside_model",
    "calibration_unresolved",
    "pending",
)

#: The record columns, in order.
RECORD_COLUMNS: tuple[str, ...] = (
    "output",
    "observation",
    "observed_spec",
    "acquired_at",
    "observed",
    "predicted",
    "residual",
    "ratio",
    "state",
    "kind",
)


class TwinRunnerRefusalError(ValueError):
    """The run as a whole cannot proceed; ``reason`` names why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


# -- the kit and the resolved ECU ---------------------------------------------------------


class RunnerKit(Protocol):
    """What a run reads besides the session (a :class:`TwinKit`, or the
    paired simulator's kit with the twin definition's hash added)."""

    @property
    def model(self) -> ApplicationModel: ...
    @property
    def model_sha256(self) -> str: ...
    @property
    def rom_map(self) -> RomMap: ...
    @property
    def twin(self) -> TwinDefinition: ...
    @property
    def twin_sha256(self) -> str: ...
    @property
    def viewer(self) -> ViewerDefinition: ...
    @property
    def plant(self) -> FuelPlantProfile: ...
    @property
    def delay(self) -> B06Rules: ...


@dataclass(frozen=True)
class TwinKit:
    model: ApplicationModel
    model_sha256: str
    rom_map: RomMap
    twin: TwinDefinition
    twin_sha256: str
    viewer: ViewerDefinition
    plant: FuelPlantProfile
    delay: B06Rules


def load_twin_kit(profile_dir: Path, *, plant_version: str | None = None) -> TwinKit:
    """The runner's kit from one vehicle profile, by the loaders every
    evaluator uses (the fuel plant is ``fuel_plant.yaml`` unless named: the
    pulse chain and its sample rules are the E07.3 declaration's)."""
    model, model_sha = load_application_model(profile_dir)
    twin, twin_sha = load_twin_definition(profile_dir)
    plant, _ = load_fuel_plant(profile_dir, plant_version)
    return TwinKit(
        model=model,
        model_sha256=model_sha,
        rom_map=load_rom_map(profile_dir),
        twin=twin,
        twin_sha256=twin_sha,
        viewer=load_viewer_definition(profile_dir),
        plant=plant,
        delay=load_b06(profile_dir, plant.delay),
    )


@dataclass(frozen=True)
class ResolvedEcu:
    """The calibration each ECU stage reads, or why it has none.

    ``refusals`` maps a calibration (``target_table``, ``air_charge``,
    ``pulse_chain``) to the refusal that left it ``None``; a ``None`` with no
    refusal was not given (``calibration_not_given``)."""

    table: ResolvedTable | None
    air: AirChargeCalibration | None
    pulse: PulseChainCalibration | None
    refusals: Mapping[str, str] = field(default_factory=dict)
    image_sha256: str | None = None
    slot: int | None = None

    def missing(self, calibration: str) -> str:
        reason = self.refusals.get(calibration, "calibration_not_given")
        return f"calibration_unresolved:{calibration}:{reason}"


def resolve_ecu(kit: RunnerKit, rom: LoadedRom) -> ResolvedEcu:
    """Each stage's calibration from one loaded image on its active slot, by
    the evaluators' own constructors. A constructor's refusal is kept by name
    and makes that stage's samples ``calibration_unresolved``; it never stops
    the stages that did resolve."""
    refusals: dict[str, str] = {}
    table: ResolvedTable | None = None
    air: AirChargeCalibration | None = None
    pulse: PulseChainCalibration | None = None
    try:
        table = resolve_table(
            rom.image,
            kit.rom_map,
            kit.twin,
            viewer=kit.viewer,
            application_model=(kit.model, kit.model_sha256),
            expected_sha256=rom.image_sha256,
            slot=rom.slot,
        )
    except TwinRefusalError as exc:
        refusals["target_table"] = exc.reason
    try:
        air = AirChargeCalibration.from_image(
            rom.image,
            model=kit.model,
            rom_map=kit.rom_map,
            slot=rom.slot,
            image_sha256=rom.image_sha256,
        )
    except (AirChargeRefusalError, ApplicationModelRefusalError) as exc:
        refusals["air_charge"] = exc.reason
    try:
        pulse = calibration_from_image(rom.image, kit.plant.pulse_chain)
    except FuelPlantRefusalError as exc:
        refusals["pulse_chain"] = exc.reason
    return ResolvedEcu(table, air, pulse, refusals, rom.image_sha256, rom.slot)


# -- stages and their order -----------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    """One ECU output the runner evaluates.

    ``node`` is the application-model node whose value the kernel computes
    (``implemented``) or checks by its formula (``relation``: an ``observed``
    node, read from its channel as every later stage's input). ``after``
    places a stage the model does not carry as a node (the pulse chain runs
    downstream of the final command word)."""

    output: str
    role: Literal["implemented", "relation", "downstream"]
    kernel: str
    node: str | None = None
    model_output: str | None = None
    after: str | None = None
    calibration: str = ""


STAGES: tuple[Stage, ...] = (
    Stage(
        output="target_equiv_ratio",
        role="implemented",
        kernel="src.analysis.target_phi.predict",
        node="enrichment_gate",
        model_output="target_equiv_ratio",
        calibration="target_table",
    ),
    Stage(
        output="injector_pulse_word",
        role="downstream",
        kernel="src.analysis.injector_pulse.predict_pulse",
        after="final_command_word",
        calibration="pulse_chain",
    ),
    Stage(
        output="base_fuel_schedule_word",
        role="implemented",
        kernel="src.decode.air_charge.steady_state_bfs",
        node="base_fuel_schedule",
        model_output="base_fuel_schedule_word",
        calibration="air_charge",
    ),
    Stage(
        output="load_input_x",
        role="relation",
        kernel="src.analysis.target_phi.x_from_bfs",
        node="load_input_x",
        calibration="target_table",
    ),
)


def node_order(model: ApplicationModel) -> list[str]:
    """Every model node, each after every node it ``depends_on``; among nodes
    free at once, the fuel task's ``schedule`` order decides (nodes outside
    it first: the cycle-start copies and the tasks they copy), then
    declaration order. Refuses ``graph_cycle``."""
    scheduled = {node: i for i, node in enumerate(model.schedule.order())}
    declared = {node.id: i for i, node in enumerate(model.nodes)}
    pending = {node.id: set(node.depends_on) & declared.keys() for node in model.nodes}
    order: list[str] = []
    while pending:
        free = [n for n, deps in pending.items() if not deps]
        if not free:
            raise TwinRunnerRefusalError(
                "graph_cycle", f"depends_on has a cycle among {sorted(pending)}"
            )
        chosen = min(free, key=lambda n: (scheduled.get(n, -1), declared[n]))
        order.append(chosen)
        del pending[chosen]
        for deps in pending.values():
            deps.discard(chosen)
    return order


def _check_stage(model: ApplicationModel, stage: Stage) -> None:
    anchor = stage.node or stage.after
    assert anchor is not None
    if anchor not in {n.id for n in model.nodes}:
        raise TwinRunnerRefusalError(
            "node_undeclared", f"{stage.output}: the model declares no node {anchor!r}"
        )
    if stage.role == "downstream":
        return
    assert stage.node is not None
    node = model.node(stage.node)
    expected = "implemented" if stage.role == "implemented" else "observed"
    if node.twin != expected:
        raise TwinRunnerRefusalError(
            "stage_role_changed",
            f"{stage.output}: node {node.id} is twin: {node.twin}, the runner runs it as "
            f"{expected}; bind its kernel before running it",
        )
    if stage.role == "implemented":
        named = (node.implemented_by or "").split()[0]
        if named != stage.kernel:
            raise TwinRunnerRefusalError(
                "kernel_mismatch",
                f"{stage.output}: the model implements {node.id} by {named}, the runner "
                f"calls {stage.kernel}",
            )
    if stage.model_output is not None:
        twin = model.output(stage.model_output).twin
        if twin is None or twin.predicts != stage.node:
            raise TwinRunnerRefusalError(
                "kernel_mismatch",
                f"{stage.output}: output {stage.model_output} predicts "
                f"{None if twin is None else twin.predicts}, not {stage.node}",
            )


def stage_order(model: ApplicationModel) -> tuple[Stage, ...]:
    """:data:`STAGES` in the model's node order (:func:`node_order`), each
    checked against the model's roles first."""
    for stage in STAGES:
        _check_stage(model, stage)
    rank = {node: i for i, node in enumerate(node_order(model))}

    def key(stage: Stage) -> float:
        if stage.node is not None:
            return float(rank[stage.node])
        assert stage.after is not None
        return rank[stage.after] + 0.5

    return tuple(sorted(STAGES, key=key))


# -- one stage's result ---------------------------------------------------------------------


@dataclass
class _StageResult:
    records: list[dict[str, Any]]
    #: The existing evaluator's own result for the same samples.
    native: Any
    input_reads: dict[str, int]
    provenance: dict[str, Any]
    #: The channels the output is observed through, each accounted
    #: separately (declared, so a channel with no reads still shows).
    observations: tuple[str, ...]


def _record(
    output: str,
    spec: str,
    acquired_at: float,
    observed: float | None,
    *,
    observation: str | None = None,
    predicted: float | None = None,
    ratio: float | None = None,
    state: str,
    kind: str,
) -> dict[str, Any]:
    obs = math.nan if observed is None else float(observed)
    pred = math.nan if predicted is None else float(predicted)
    return {
        "output": output,
        "observation": spec if observation is None else observation,
        "observed_spec": spec,
        "acquired_at": float(acquired_at),
        "observed": obs,
        "predicted": pred,
        "residual": obs - pred,
        "ratio": math.nan if ratio is None else float(ratio),
        "state": state,
        "kind": kind,
    }


def _unclassified(output: str, reason: str) -> TwinRunnerRefusalError:
    return TwinRunnerRefusalError(
        "reason_unclassified",
        f"{output}: the runner has no empty kind for {reason!r}; classify it in twin_runner",
    )


# -- base fuel schedule (E05.2) -------------------------------------------------------------

#: The BFS observation channels, each with the input channels it is scored
#: from: the DID round of the tuning captures, and the RAM study's memory
#: window. Every set whose BFS channel has reads is evaluated.
BFS_CHANNEL_SETS: tuple[dict[str, str | None], ...] = (
    {"bfs": "DID_1208", "maf": "DID_1209", "rpm": "RPM", "load": None},
    {
        "bfs": "mem:ECM_AIR_CHARGE_SLOT_103A",
        "maf": "MAF",
        "rpm": "RPM",
        "load": "mem:ECM_FUEL_COMP_X_INPUT",
    },
)
_BFS_UNPAIRED = {"maf_unpaired": "maf", "rpm_unpaired": "rpm", "load_unpaired": "load"}
_BFS_GATED = {
    "below_min_rpm",
    "maf_window_sparse",
    "maf_not_steady",
    "rpm_not_steady",
}
_BFS_OUTSIDE = {"maf_word_unreachable", "predicted_zero"}


def _bfs_kind(reason: str, reads: Mapping[str, int]) -> EmptyKind:
    if reason in _BFS_UNPAIRED:
        return "input_not_captured" if reads[_BFS_UNPAIRED[reason]] == 0 else "not_co_sampled"
    if reason in _BFS_GATED:
        return "gated"
    if reason in _BFS_OUTSIDE:
        return "outside_model"
    raise _unclassified("base_fuel_schedule_word", reason)


#: The stored columns the BFS words are recovered from.
_BFS_COLUMNS = ("timestamp", "channel", "value_number", "value_text", "payload", "metadata_json")


def _bfs_rows(session: Path | pd.DataFrame, channels: list[str]) -> pd.DataFrame:
    """The raw rows of ``channels``. A frame lacking a stored column (a
    synthetic window) reads it as absent: a word that cannot be recovered
    from what was stored is dropped, as :func:`air_charge_twin.raw_series`
    drops it."""
    if isinstance(session, pd.DataFrame):
        rows = session[session["channel"].isin(channels)]
        missing = [c for c in _BFS_COLUMNS if c not in rows.columns]
        return rows.assign(**dict.fromkeys(missing)) if missing else rows
    return read_session(session, filters=[("channel", "in", channels)])


def _bfs_set(
    rows: pd.DataFrame, chosen: Mapping[str, str | None], ecu: ResolvedEcu
) -> tuple[list[dict[str, Any]], SteadyBfsReport | None, dict[str, int]]:
    """One BFS observation channel with its own input channels: per-read
    records, the evaluator's report type for the same reads, and reads per role."""
    output = "base_fuel_schedule_word"
    bfs_channel, maf_channel, rpm_channel = chosen["bfs"], chosen["maf"], chosen["rpm"]
    assert bfs_channel is not None and maf_channel is not None and rpm_channel is not None
    series = bfs_series(
        rows,
        bfs_channel=bfs_channel,
        maf_channel=maf_channel,
        rpm_channel=rpm_channel,
        load_channel=chosen["load"],
    )
    reads = {
        "bfs": len(series.bfs_t),
        "maf": len(series.maf_t),
        "rpm": len(series.rpm_t),
        "load": len(series.load_t),
    }
    records: list[dict[str, Any]] = []
    if ecu.air is None:
        reason = ecu.missing("air_charge")
        for at, word in zip(series.bfs_t, series.bfs_w, strict=True):
            records.append(
                _record(output, bfs_channel, at, word, state=reason, kind="calibration_unresolved")
            )
        return records, None, reads
    gates = air_charge_twin.FROZEN_GATES
    scored: list[BfsRecord] = []
    excluded: dict[str, int] = {}
    pairs = pair_bfs_inputs(series, gates)
    for index, (at, word) in enumerate(zip(series.bfs_t, series.bfs_w, strict=True)):
        result = score_bfs_read(
            series,
            index,
            ecu.air,
            maf_index=pairs.maf.get(index),
            rpm_index=pairs.rpm.get(index),
            load_index=pairs.load.get(index),
            gates=gates,
        )
        if isinstance(result, str):
            excluded[result] = excluded.get(result, 0) + 1
            records.append(
                _record(output, bfs_channel, at, word, state=result, kind=_bfs_kind(result, reads))
            )
            continue
        scored.append(result)
        records.append(
            _record(
                output,
                bfs_channel,
                at,
                word,
                predicted=result.prediction.mid,
                ratio=result.ratio,
                state="eligible",
                kind="eligible",
            )
        )
    return records, SteadyBfsReport(records=tuple(scored), excluded=excluded, gates=gates), reads


def _run_bfs(session: Path | pd.DataFrame, ecu: ResolvedEcu) -> _StageResult:
    """Every :data:`BFS_CHANNEL_SETS` entry whose BFS channel has reads, each
    with its own inputs; ``native`` is keyed by the BFS channel."""
    all_channels = sorted({c for s in BFS_CHANNEL_SETS for c in s.values() if c is not None})
    rows = _bfs_rows(session, all_channels)
    present = set(rows["channel"].unique()) if len(rows) else set()
    records: list[dict[str, Any]] = []
    native: dict[str, SteadyBfsReport | None] = {}
    reads: dict[str, int] = {}
    evaluated = []
    for chosen in BFS_CHANNEL_SETS:
        if chosen["bfs"] not in present:
            continue
        bfs_channel = str(chosen["bfs"])
        set_records, report, set_reads = _bfs_set(rows, chosen, ecu)
        records += set_records
        native[bfs_channel] = report
        for role, n in set_reads.items():
            channel = chosen[role]
            if channel is not None:
                reads[channel] = n
        evaluated.append(dict(chosen))
    return _StageResult(
        records,
        native,
        reads,
        {"channel_sets": evaluated, "pairing": "air_charge_twin.pair_bfs_inputs"},
        tuple(str(c["bfs"]) for c in BFS_CHANNEL_SETS),
    )


# -- load input X (the observed node's formula) -----------------------------------------


def _x_reads(
    session: Path | pd.DataFrame, kit: RunnerKit
) -> tuple[Any, np.ndarray, Any, np.ndarray]:
    """X and BFS reads in raw counts, read exactly as ``x_formula_check``
    reads them (its own series and catalog-decode helpers)."""
    load_spec = kit.twin.inputs.load.channel
    bfs_spec = kit.twin.x_formula.bfs_channel
    long = read_channels(session, [load_spec, bfs_spec], dids_path=DIDS_YAML, acquired_time=True)
    x = target_phi._series(long, load_spec)
    bfs = target_phi._series(long, bfs_spec)
    catalog = load_did_map(DIDS_YAML)
    return (
        x,
        target_phi._raw(x, load_spec, DIDS_YAML, catalog),
        bfs,
        target_phi._raw(bfs, bfs_spec, DIDS_YAML, catalog),
    )


def _run_x(session: Path | pd.DataFrame, kit: RunnerKit, ecu: ResolvedEcu) -> _StageResult:
    """Every X read, paired one-to-one with a BFS read by ``match_ordered``
    (the X relation's policy), predicted by ``x_from_bfs``; an unpaired read
    keeps its time and value and says why."""
    output = "load_input_x"
    load_spec = kit.twin.inputs.load.channel
    bfs_spec = kit.twin.x_formula.bfs_channel
    x, x_raw, bfs, bfs_raw = _x_reads(session, kit)
    reads = {load_spec: len(x.times), bfs_spec: len(bfs.times)}
    if ecu.table is None or ecu.table.load_point_scaler is None:
        reason = (
            ecu.missing("target_table")
            if ecu.table is None
            else "calibration_unresolved:load_point_scaler"
        )
        records = [
            _record(output, load_spec, t, v, state=reason, kind="calibration_unresolved")
            for t, v in zip(x.times, x_raw, strict=True)
        ]
        return _StageResult(records, None, reads, {}, (load_spec,))
    scaler = ecu.table.load_point_scaler
    matched = match_ordered(
        x.times,
        bfs.times,
        PairingPolicy.within(pd.Timedelta(seconds=kit.twin.gates.max_acquisition_skew_s)),
    )
    partner = dict(zip(matched.index_a.tolist(), matched.index_b.tolist(), strict=True))
    unpaired_kind: EmptyKind = "input_not_captured" if not len(bfs.times) else "not_co_sampled"
    records = []
    for i, (t, observed) in enumerate(zip(x.times, x_raw, strict=True)):
        j = partner.get(i)
        if j is None:
            records.append(
                _record(output, load_spec, t, observed, state="bfs_unpaired", kind=unpaired_kind)
            )
            continue
        predicted = x_from_bfs(int(bfs_raw[j]), scaler, kit.twin)
        records.append(
            _record(
                output,
                load_spec,
                t,
                observed,
                predicted=predicted,
                ratio=float(observed) / predicted if predicted else None,
                state="eligible",
                kind="eligible",
            )
        )
    native = x_formula_check(session, kit.twin, scaler=scaler)
    return _StageResult(
        records,
        native,
        reads,
        {"specs": {"x": load_spec, "bfs": bfs_spec}, "scaler": scaler},
        (load_spec,),
    )


# -- the target-phi word (E02) --------------------------------------------------------------

_PHI_OUTSIDE = {
    "sentinel",
    "unsupported_clock",
    "uncovered_region",
    "slot_unresolved",
    "mode_unknown",
    "enrichment_gate_state_unknown",
    # The map identity resolves to no slot, or to two (target_phi.resolve_slot,
    # target_phi._identity): the table the ECU looked up is not known.
    "map_uninitialised",
    "slot_ambiguous",
    "map_switching",
}
_PHI_GATED = {"cold", "mode_transition", "transient"}
#: ``steadiness_unobserved``: too few reads inside the dwell before the
#: sample (the channel may be read all drive, just not there).
_PHI_NOT_CO_SAMPLED_PREFIXES = ("input_stale:", "gate_stale:", "steadiness_unobserved:")
#: The evaluator's own "never read by this sample's time" reasons.
_PHI_NOT_CAPTURED_PREFIXES = ("input_not_captured:", "gate_missing:")
#: No bracketing identity read: never captured when any of the three identity
#: channels (all needed to resolve a slot) had no read by the sample's
#: decision time, else read but not around the sample.
_PHI_IDENTITY_UNBRACKETED = "map_identity_unbracketed"


#: When a record names several reasons, the kind it is counted under: a
#: sample the decoded logic does not cover is that whatever was captured; an
#: input never read outranks one read at the wrong time, which outranks a gate.
_KIND_PRECEDENCE: tuple[EmptyKind, ...] = (
    "calibration_unresolved",
    "outside_model",
    "input_not_captured",
    "not_co_sampled",
    "gated",
    "pending",
)


def _phi_reason_kind(reason: str, *, identity_captured: bool) -> EmptyKind | None:
    if reason == _PHI_IDENTITY_UNBRACKETED:
        return "not_co_sampled" if identity_captured else "input_not_captured"
    if reason.startswith("awaiting_"):
        return "pending"
    if reason.startswith(_PHI_NOT_CAPTURED_PREFIXES):
        return "input_not_captured"
    if reason.startswith(_PHI_NOT_CO_SAMPLED_PREFIXES):
        return "not_co_sampled"
    if reason in _PHI_GATED:
        return "gated"
    if reason in _PHI_OUTSIDE:
        return "outside_model"
    return None


def _phi_kind(state: str, reasons: list[str], *, identity_captured: bool) -> EmptyKind:
    """The kind of one unevaluable record from all its reasons; the first
    (primary) reason must be classified, or the run refuses. An excluded
    record's unnamed reason is a fuel-system mode the gate does not admit.
    ``identity_captured``: every map-identity channel had a read by the
    sample's decision time."""
    primary = _phi_reason_kind(reasons[0], identity_captured=identity_captured)
    if primary is None and state == "excluded":
        primary = "gated"
    if primary is None:
        raise _unclassified("target_equiv_ratio", f"{state}:{reasons[0]}")
    kinds = {primary} | {
        k
        for r in reasons[1:]
        if (k := _phi_reason_kind(r, identity_captured=identity_captured)) is not None
    }
    return next(k for k in _KIND_PRECEDENCE if k in kinds)


def _run_phi(
    session: Path | pd.DataFrame,
    kit: RunnerKit,
    ecu: ResolvedEcu,
    *,
    session_complete: bool,
) -> _StageResult:
    output = "target_equiv_ratio"
    definition = kit.twin
    reference = definition.reference
    # A fuel-cut sentinel is a state of the same command word: one observation.
    observation = reference.channel
    if ecu.table is None:
        reason = ecu.missing("target_table")
        long = read_channels(
            session, [reference.channel, reference.sentinel_channel], acquired_time=True
        )
        records = [
            _record(
                output,
                spec,
                ts.timestamp(),
                None,
                observation=observation,
                state=reason,
                kind="calibration_unresolved",
            )
            for spec, ts in zip(long["spec"], long["timestamp"], strict=True)
        ]
        return _StageResult(records, None, {}, {}, (observation,))
    native = evaluate_session(
        session,
        definition,
        ecu.table,
        definition_sha256=kit.twin_sha256,
        session_complete=session_complete,
    )
    inputs = list(definition.inputs.channels().values())
    identity = definition.map_identity.channels()
    long = read_channels(session, [*inputs, *identity], acquired_time=True)
    counts = long["spec"].value_counts()
    first = long.groupby("spec")["timestamp"].min()
    first_s = {str(k): pd.Timestamp(v).timestamp() for k, v in first.items()}

    def first_read(spec: str) -> float:
        return first_s.get(spec, math.inf)

    records = []
    for record in native["records"]:
        sample = record["sample"]
        state = record["state"]
        t = pd.Timestamp(sample["acquired_at"]).timestamp()
        if state == "eligible":
            kind: str = "eligible"
        else:
            kind = _phi_kind(
                state,
                record["reasons"],
                identity_captured=all(
                    captured_by(definition, first_read, spec, t) for spec in identity
                ),
            )
        records.append(
            _record(
                output,
                sample["spec"],
                t,
                sample["observed_word"],
                observation=observation,
                predicted=record["predicted_word"],
                ratio=record["ratio"],
                state="eligible" if state == "eligible" else record["reason"],
                kind=kind,
            )
        )
    reads = {spec: int(counts.get(spec, 0)) for spec in [*inputs, *identity]}
    return _StageResult(
        records,
        native,
        reads,
        {"pairing": native["pairing"], "input_pairing": native["accounting"]["input_pairing"]},
        (observation,),
    )


# -- the injector pulse word (E07.3) -------------------------------------------------------

#: The fuel-plant spec each pulse-chain input column is read from.
_PULSE_INPUT_SPECS: dict[str, Callable[[FuelPlantProfile], list[str]]] = {
    "bfs": lambda p: [p.specs.bfs],
    "bfs_before": lambda p: [p.specs.bfs],
    "rpm": lambda p: [p.specs.rpm],
    "reference_phi": lambda p: [p.specs.reference_phi],
    "stft_pct": lambda p: [p.specs.stft],
    "baro_kpa": lambda p: [p.specs.baro],
    "iat_c": lambda p: [p.specs.intake_temp],
    # PID 0x42 where logged, else the session's own fuel-cut reads.
    "ecu_dead_time_counts": lambda p: [p.specs.battery_voltage, p.specs.fuel_cut_reference],
}
_PULSE_GATED = {"not_steady", "bfs_not_steady"}
_PULSE_OUTSIDE = {"not_fuelled", "reference_phi_not_positive"}


def _pulse_kind(reason: str, reads: Mapping[str, int], plant: FuelPlantProfile) -> EmptyKind:
    if reason.startswith("input_missing:"):
        column = reason.split(":", 1)[1]
        specs = _PULSE_INPUT_SPECS[column](plant)
        return "input_not_captured" if not any(reads[s] for s in specs) else "not_co_sampled"
    if reason in _PULSE_GATED:
        return "gated"
    if reason in _PULSE_OUTSIDE:
        return "outside_model"
    raise _unclassified("injector_pulse_word", reason)


def _session_name(session: Path | pd.DataFrame, name: str | None) -> str:
    if name is not None:
        return name
    if isinstance(session, Path):
        return session.name
    names: list[Any] = session["session"].dropna().unique().tolist() if "session" in session else []
    if len(names) != 1:
        raise TwinRunnerRefusalError(
            "session_unnamed", "a frame of reads needs its session name (pass name=)"
        )
    return str(names[0])


def _run_pulse(
    session: Path | pd.DataFrame, kit: RunnerKit, ecu: ResolvedEcu, name: str
) -> _StageResult:
    output = "injector_pulse_word"
    plant = kit.plant
    spec = plant.specs.pulse
    specs = sorted({s for column in PULSE_CHAIN_INPUTS for s in _PULSE_INPUT_SPECS[column](plant)})
    long = read_channels(session, [spec, *specs])
    counts = long["spec"].value_counts()
    reads = {s: int(counts.get(s, 0)) for s in [spec, *specs]}
    frame = build_samples(session, plant, kit.delay, name=name)
    if not len(frame):
        return _StageResult([], None, reads, {"spec": spec}, (spec,))
    measured = np.rint(frame["pulse_ms"].to_numpy(dtype=float) / PULSE_COUNT_MS)
    if ecu.pulse is None:
        reason = ecu.missing("pulse_chain")
        records = [
            _record(output, spec, t, m, state=reason, kind="calibration_unresolved")
            for t, m in zip(frame["t"], measured, strict=True)
        ]
        return _StageResult(records, None, reads, {"spec": spec}, (spec,))
    chain = plant.pulse_chain
    states = pulse_chain_states(frame, chain)
    native = pulse_chain_ratios(frame, ecu.pulse, chain)
    records = []
    for index, t, m, state in zip(frame.index, frame["t"], measured, states, strict=True):
        if state != "eligible":
            records.append(
                _record(output, spec, t, m, state=str(state), kind=_pulse_kind(state, reads, plant))
            )
            continue
        row = native.loc[index]
        ts = float(row["ecu_dead_time_counts"])
        records.append(
            _record(
                output,
                spec,
                t,
                m,
                predicted=float(row["predicted_effective"]) + ts,
                ratio=float(row["ratio"]),
                state="eligible",
                kind="eligible",
            )
        )
    return _StageResult(
        records,
        native,
        reads,
        {"spec": spec, "dead_time_source": sorted(set(frame["dead_time_source"]))},
        (spec,),
    )


# -- the run --------------------------------------------------------------------------------


@dataclass(frozen=True)
class TwinRun:
    """One pass of the runner over one session."""

    session: str
    #: The stages' outputs, in the order they ran.
    order: tuple[str, ...]
    #: One row per output per sample (:data:`RECORD_COLUMNS`).
    records: pd.DataFrame
    #: The existing evaluator's own result per output, for the same samples
    #: (``None`` where the stage had no calibration).
    native: Mapping[str, Any]
    #: Per output: reads of each input, as the stage counts them.
    input_reads: Mapping[str, Mapping[str, int]]
    provenance: Mapping[str, Any]
    #: Per output: the channels it is observed through, each accounted
    #: separately (BFS has two: the DID round and the RAM window).
    observations: Mapping[str, tuple[str, ...]]

    def accounting(self) -> dict[str, dict[str, dict[str, int]]]:
        """Per output, per observation channel, samples by state; every
        sample counted once. A declared channel with no reads is ``{}``."""
        out: dict[str, dict[str, dict[str, int]]] = {
            name: {channel: {} for channel in self.observations[name]} for name in self.order
        }
        for output, channel, state in zip(
            self.records["output"], self.records["observation"], self.records["state"], strict=True
        ):
            states = out[str(output)].setdefault(str(channel), {})
            states[str(state)] = states.get(str(state), 0) + 1
        return {
            name: {channel: dict(sorted(states.items())) for channel, states in per.items()}
            for name, per in out.items()
        }

    def empty(self) -> dict[str, dict[str, str | dict[str, int] | None]]:
        """Per output, per observation channel: ``None`` when any of that
        channel's samples is eligible; ``"output_not_captured"`` when the
        channel was never read; else its samples by :data:`EMPTY_KINDS`. One
        channel's eligible reads never hide another channel's empty."""
        out: dict[str, dict[str, str | dict[str, int] | None]] = {}
        records = self.records
        for name in self.order:
            per: dict[str, str | dict[str, int] | None] = {}
            for channel in self.observations[name]:
                rows = records[(records["output"] == name) & (records["observation"] == channel)]
                if not len(rows):
                    per[channel] = "output_not_captured"
                elif (rows["kind"] == "eligible").any():
                    per[channel] = None
                else:
                    counts = rows["kind"].value_counts()
                    per[channel] = {k: int(counts[k]) for k in EMPTY_KINDS if k in counts}
            out[name] = per
        return out

    def eligible(self, output: str) -> pd.DataFrame:
        rows = self.records
        return rows[(rows["output"] == output) & (rows["state"] == "eligible")]


def _provenance(kit: RunnerKit, ecu: ResolvedEcu, stages: tuple[Stage, ...]) -> dict[str, Any]:
    return {
        "runner_version": RUNNER_VERSION,
        "application_model_sha256": kit.model_sha256,
        "twin_definition_sha256": kit.twin_sha256,
        "plant_version": kit.plant.plant_version,
        "image_sha256": ecu.image_sha256,
        "slot": ecu.slot,
        "calibration_refusals": dict(ecu.refusals),
        "stages": [
            {
                "output": s.output,
                "role": s.role,
                "node": s.node,
                "after": s.after,
                "kernel": s.kernel,
                "model_observed_inputs": observed_specs(kit.model, s.node) if s.node else [],
            }
            for s in stages
        ],
    }


def run_twin(
    session: Path | pd.DataFrame,
    kit: RunnerKit,
    ecu: ResolvedEcu,
    *,
    name: str | None = None,
    session_complete: bool = True,
) -> TwinRun:
    """Every ECU output of ``session``, in the model's node order.

    ``session`` is a session directory or a frame of its raw reads: a bounded
    window is a frame, handed whole to every stage, so no stage can be
    bounded differently from another. ``session_complete=False`` (a capture
    still recording) leaves target-phi samples awaiting later reads
    ``pending``."""
    stages = stage_order(kit.model)
    label = _session_name(session, name)
    results: dict[str, _StageResult] = {}
    for stage in stages:
        if stage.output == "base_fuel_schedule_word":
            results[stage.output] = _run_bfs(session, ecu)
        elif stage.output == "load_input_x":
            results[stage.output] = _run_x(session, kit, ecu)
        elif stage.output == "target_equiv_ratio":
            results[stage.output] = _run_phi(session, kit, ecu, session_complete=session_complete)
        elif stage.output == "injector_pulse_word":
            results[stage.output] = _run_pulse(session, kit, ecu, label)
        else:  # pragma: no cover - STAGES and this dispatch are one list
            raise TwinRunnerRefusalError("stage_unbound", stage.output)
    order = tuple(s.output for s in stages)
    rows = [r for output in order for r in results[output].records]
    records = pd.DataFrame(rows, columns=list(RECORD_COLUMNS))
    provenance = _provenance(kit, ecu, stages)
    provenance["stage_details"] = {o: results[o].provenance for o in order}
    return TwinRun(
        session=label,
        order=order,
        records=records,
        native={o: results[o].native for o in order},
        input_reads={o: results[o].input_reads for o in order},
        provenance=provenance,
        observations={o: results[o].observations for o in order},
    )


def run_digest(run: TwinRun) -> str:
    """sha256 of the records and the accounting: equal runs give equal digests."""
    payload = {
        "records": run.records.astype(object)
        .where(run.records.notna(), None)
        .to_dict(orient="records"),
        "accounting": run.accounting(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


__all__ = [
    "BFS_CHANNEL_SETS",
    "EMPTY_KINDS",
    "RECORD_COLUMNS",
    "RUNNER_VERSION",
    "STAGES",
    "ResolvedEcu",
    "RunnerKit",
    "Stage",
    "TwinKit",
    "TwinRun",
    "TwinRunnerRefusalError",
    "load_twin_kit",
    "node_order",
    "resolve_ecu",
    "run_digest",
    "run_twin",
    "stage_order",
]
