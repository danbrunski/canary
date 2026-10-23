"""The `twin` capture profile is derived from the application model (E09.1).

``ecu_application.twin_observation_channels`` names every observed input and
every observable output channel of each evaluable output, grouped by the read
that has to carry them together: one Mode 01 round, one Mode 22 session
window, one service-0x23 window. The kernels' own pairings come from the
declarations the twin runner reads (``twin_runner.BFS_CHANNEL_SETS``, the
target-phi definition's gates, every fuel plant's ``specs``, the E07.4 case's
``load_input``), never from a list written here. The profile must poll each
one as its group requires, and a node gaining an observation the profile
lacks fails the accept test.

The one exception is named, not hand-picked: a memory channel the live-read
rule refuses (``tests/test_memory_catalog_knock_lambda.py``) cannot enter any
profile. Those are pinned in :data:`AWAITING_LIVE_READ`, so the set cannot grow
or shrink silently: evidence landing for one makes this test ask for it in the
profile.
"""

from __future__ import annotations

import dataclasses
import threading
from collections import Counter
from collections.abc import Iterator, Mapping
from pathlib import Path

import numpy as np
import pytest

from src.analysis.ecu_application import (
    ApplicationModel,
    ApplicationModelRefusalError,
    TwinObservations,
    load_application_model,
    twin_observation_channels,
)
from src.analysis.fuel_plant import load_fuel_plant, plant_files
from src.analysis.plant_validation import load_interventions
from src.analysis.target_phi import load_twin_definition
from src.analysis.twin_runner import BFS_CHANNEL_SETS, STAGES
from src.collection import bus_budget, tuning_profile
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_profiles import (
    CAPTURE_CONTEXT_MODE01,
    CaptureProfile,
    CaptureProfileCatalog,
)
from src.collection.hybrid_cycle import plan_cycle
from src.collection.obd_collector import ObdReading
from src.collection.obd_registry import MODE01_REGISTRY
from src.collection.obd_scheduler import (
    CanObdSpec,
    ObdSchedulerMetrics,
    did_alias_table,
    preview_polling_plan,
    profile_did_union,
)
from src.config import CanaryConfig, CanBusConfig, CollectionConfig
from src.decode.dids import DidMap, MemoryChannel, load_did_map
from src.paths import DIDS_YAML
from src.yaml_load import load_yaml
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock
from tests.test_memory_catalog_knock_lambda import LIVE_READ_METHODS

REPO = Path(__file__).resolve().parents[1]
PROFILE_DIR = REPO / "data" / "nissan-xterra-2008"
CATALOG_PATH = PROFILE_DIR / "capture_profiles.yaml"
EXAMPLE_YAML = REPO / "canary.example.yaml"
DIDS = load_did_map(DIDS_YAML)
MEMORY = {c.name: c for c in DIDS.memory}

#: The runner stage the model does not carry as a node, and the node it runs
#: after (``twin_runner.STAGES``).
DOWNSTREAM = {s.output: s.after for s in STAGES if s.role == "downstream" and s.after}
PULSE_STAGE = "injector_pulse_word"

#: Memory channels the derivation names that the live-read rule keeps out of
#: every profile, all three hypothesized in data/dids.yaml. 0x103A and the
#: selector were read through service 0x23 on the truck in
#: _capture-ram-study-2026-10-04T204557 (194 and 196 reads), but neither is
#: identified: 0x103A is one of three air-charge slots nobody has told apart,
#: and the selector read a constant 0. 0xFFFF283C has never been read. The
#: fill byte (a chain reproduction) and the X trace (the X relation, 168 of
#: 194 exact) left this set on 2026-10-16 with their known-answer reads.
FUEL_AWAITING_LIVE_READ = frozenset(
    {
        "mem:ECM_AIR_CHARGE_SLOT_103A",
        "mem:ECM_UPREV_MAP_SELECTOR",
        "mem:ECM_FUEL_TRANSIENT_FLAGS",
    }
)
#: The ignition timing chain's words (E05.4 (b)/(c), 2026-10-17): the trim
#: lookup's two bytes, every chain node's result from the trim term to the
#: timing byte, and the unidentified terms the nodes take as required
#: arguments. All catalogued from the listing and hypothesized, never read
#: (0xFFFF11CB read a constant 0 inside the spark window's reply on the
#: ram-study, which cannot identify it). A live read of each window (as on
#: 2026-10-08 for the knock bytes) is what moves one out of this set, and the
#: accept test then asks for it in a profile.
TIMING_CHAIN_AWAITING_LIVE_READ = frozenset(
    f"mem:{name}"
    for name in (
        "ECM_TIMING_TRIM_SELECTED",
        "ECM_TIMING_TRIM_A",
        "ECM_TIMING_ENRICH_RETARD",
        "ECM_TIMING_TRIM_SUBTRAHEND",
        "ECM_TIMING_TRIM_TERM",
        "ECM_TIMING_TERM_279A",
        "ECM_TIMING_TERM_27C6",
        "ECM_TIMING_TERM_1D3E",
        "ECM_TIMING_TERM_1344",
        "ECM_BASE_TIMING_SUM",
        "ECM_BASE_TIMING",
        "ECM_KNOCK_RETARD_APPLIED",
        "ECM_TIMING_OTHER_RETARD",
        "ECM_TIMING_PRELIMIT",
        "ECM_IGNITION_TIMING_FINAL",
        "ECM_IGNITION_TIMING_BYTE",
    )
)
#: The 0xFFFF27C6 chain's seven words (five windows), on ignition_timing's
#: path since its nodes were wired in (E09.7 part B, 2026-10-17):
#: hypothesized like the chain's, so the live-read rule keeps them out and the
#: profile does not grow. They would add five more reads per session window
#: beside the nine the capacity test below measures.
TIMING_27C6_AWAITING_LIVE_READ = frozenset(
    f"mem:{name}"
    for name in (
        "ECM_TIMING_CHARGE_WORD",
        "ECM_TIMING_CHARGE_FACTOR",
        "ECM_TIMING_CHARGE_TERM",
        "ECM_TIMING_LOAD_CURVE",
        "ECM_TIMING_RATE_PRODUCT",
        "ECM_TIMING_RATE_MAP",
        "ECM_TIMING_RATE_WORD",
    )
)
TIMING_AWAITING_LIVE_READ = TIMING_CHAIN_AWAITING_LIVE_READ | TIMING_27C6_AWAITING_LIVE_READ
AWAITING_LIVE_READ = FUEL_AWAITING_LIVE_READ | TIMING_AWAITING_LIVE_READ


def _runner_spec(channel: str, dids: DidMap = DIDS) -> str:
    """A runner channel set's stored name as a spec: ``DID_1209`` is the DID
    by number, a bare name a Mode 01 PID, a spec stays a spec."""
    if ":" in channel:
        return channel
    if channel.startswith("DID_"):
        number = int(channel.removeprefix("DID_"), 16)
        return "did:" + next(d.name for d in dids.dids if d.did == number)
    return f"obd:{channel}"


def kernel_declarations(
    profile_dir: Path,
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
    """``kernel_inputs``, ``kernel_context`` and ``downstream`` for the
    derivation, from what the runner's kernels declare:

    - steady BFS: every :data:`BFS_CHANNEL_SETS` entry's MAF, rpm and load
      channels, each paired with its BFS read (0.1 / 0.25 / 0.75 s);
    - target phi: the coolant, mode and steadiness gates (as-of reads within
      their ages; the lookup inputs are the manifest's);
    - the pulse word (``build_samples``, every fuel plant version): pulse, BFS
      (0.1 s), rpm, MAF and lambda (0.3 s) and the E07.4 case's load input
      (its own tolerance) paired; trims, baro, IAT, coolant, battery, the
      reference phi, the fuel-cut reference and the rail as context."""
    twin, _ = load_twin_definition(profile_dir)
    inputs: dict[str, list[str]] = {
        "base_fuel_schedule_word": [
            _runner_spec(c) for s in BFS_CHANNEL_SETS for r, c in s.items() if r != "bfs" and c
        ],
        PULSE_STAGE: [],
    }
    # The X relation (the runner's ``load_input_x`` stage) checks the lookup's
    # X against the BFS word in the same 0x23 read (``x_formula``).
    inputs["target_equiv_ratio"] = [twin.x_formula.bfs_channel]
    gates = twin.gates
    context: dict[str, list[str]] = {
        # The coolant gate takes the first source present: its Mode 01 one
        # keeps the session window to the paired DIDs (the fill byte's 0.1 s).
        "target_equiv_ratio": [
            *(c for c in gates.coolant.sources if c.startswith("obd:")),
            gates.mode.channel,
            *(c.channel for c in gates.steadiness.channels),
        ],
        PULSE_STAGE: [],
    }
    for version in plant_files(profile_dir):
        specs = load_fuel_plant(profile_dir, version)[0].specs
        inputs[PULSE_STAGE] += [specs.pulse, specs.bfs, specs.rpm, specs.maf, specs.lambda_observed]
        context[PULSE_STAGE] += [
            specs.stft,
            specs.ltft,
            specs.baro,
            specs.intake_temp,
            specs.coolant,
            specs.battery_voltage,
            specs.reference_phi,
            specs.fuel_cut_reference,
            specs.rail_pressure,
        ]
    inputs[PULSE_STAGE] += [case.load_input for case in load_interventions(profile_dir).cases]
    return inputs, context, dict(DOWNSTREAM)


def derive(
    model: ApplicationModel, profile_dir: Path = PROFILE_DIR
) -> tuple[TwinObservations, ...]:
    inputs, context, downstream = kernel_declarations(profile_dir)
    return twin_observation_channels(
        model,
        [(c.name, c.window) for c in DIDS.memory],
        kernel_inputs=inputs,
        kernel_context=context,
        downstream=downstream,
    )


def refused_by_live_read_rule(channel: MemoryChannel) -> bool:
    live = any(e.method in LIVE_READ_METHODS for e in channel.evidence)
    return channel.confidence == "hypothesized" or not live


def profile_gaps(
    derived: tuple[TwinObservations, ...], profile: CaptureProfile
) -> tuple[list[str], set[str]]:
    """What ``profile`` lacks for ``derived`` (one line per channel or group),
    and the memory channels the live-read rule refuses, which no profile may
    poll. A ``mode22_round`` or ``memory_window`` channel must be read in every
    session window (weight 1.0), so the DIDs and windows of one output always
    share one; a ``mode01_round`` group shares one Mode 01 weight (one
    cadence, so one round)."""
    gaps: list[str] = []
    blocked: set[str] = set()

    def every_window(weight: float) -> bool:
        if profile.target_rate_hz is not None and profile.did_cycle_seconds is not None:
            return profile.target_rate_hz * weight >= 1 / profile.did_cycle_seconds
        return weight >= 1.0

    memory_weight = {n.upper(): w for n, w in profile.memory_rate_weights.items()}
    selected = {n.upper() for n in profile.memory}
    for obs in derived:
        for c in obs.channels:
            where = f"{obs.output}: {c.spec} ({c.declared_by}, {c.unit})"
            if c.service == "mode01":
                if c.name.upper() not in profile.mode01:
                    gaps.append(f"{where} not in mode01")
            elif c.service == "mode22":
                entry = next((d for d in profile.mode22 if d.upper() == c.name.upper()), None)
                if entry is None:
                    gaps.append(f"{where} not in mode22")
                elif c.unit == "mode22_round" and not every_window(
                    profile.mode22_rate_weights.get(entry, 1.0)
                ):
                    gaps.append(f"{where} not in every session window")
            elif c.service == "memory":
                if refused_by_live_read_rule(MEMORY[c.name]):
                    blocked.add(c.spec)
                    if c.name.upper() in selected:
                        gaps.append(f"{where} polled against the live-read rule")
                    continue
                if c.name.upper() not in selected:
                    gaps.append(f"{where} not in memory")
                elif c.unit == "memory_window" and not every_window(
                    memory_weight.get(c.name.upper(), 1.0)
                ):
                    gaps.append(f"{where} not in every session window")
        for group in obs.groups:
            if group.unit != "mode01_round":
                continue
            weights = {profile.mode01.get(s.split(":", 1)[1].upper()) for s in group.specs}
            if len(weights) > 1:
                gaps.append(f"{obs.output}: Mode 01 round {group.specs} at weights {weights}")
    return gaps, blocked


@pytest.fixture(scope="module")
def model() -> ApplicationModel:
    return load_application_model(PROFILE_DIR)[0]


@pytest.fixture(scope="module")
def twin_profile() -> CaptureProfile:
    catalog = CaptureProfileCatalog.model_validate(load_yaml(CATALOG_PATH.read_text()))
    profile = catalog.by_name("twin")
    assert profile is not None
    return profile


# -- the derivation ---------------------------------------------------------------------


def test_the_derivation_names_each_evaluable_output_and_the_pulse_stage(
    model: ApplicationModel,
) -> None:
    derived = {o.output: o for o in derive(model)}
    # Evaluable: a twin definition and an observation. Since 2026-10-17 that
    # includes ignition timing (the seven chain nodes, E09.7) and the trim
    # lookup (an intermediate, now with a channel at 0xFFFF1777).
    assert list(derived) == [
        "target_equiv_ratio",
        "ignition_timing",
        "ignition_trim_selected",
        "base_fuel_schedule_word",
        PULSE_STAGE,
    ]
    phi = derived["target_equiv_ratio"]
    assert phi.specs(role="output") == ("mem:ECM_TARGET_EQUIV_RATIO", "obd:COMMANDED_EQUIV_RATIO")
    # The lookup's memory windows are exactly the ones the target-phi twin
    # declares it reads (target_phi_twin.yaml acquisition.windows).
    twin, _ = load_twin_definition(PROFILE_DIR)
    windows = {g.key: set(g.specs) for g in phi.groups if g.unit == "memory_window"}
    assert windows == {k: set(v) for k, v in twin.acquisition.windows.items()}
    bfs = {g.key: g.specs for g in derived["base_fuel_schedule_word"].groups}
    assert bfs["mode22"] == ("did:ECM_AIR_CHARGE", "did:ECM_MAF_GPS")
    assert set(bfs["mode01"]) == {"obd:MAF", "obd:RPM"}
    pulse = derived[PULSE_STAGE]
    assert pulse.after == "final_command_word"
    groups = {g.key: g.specs for g in pulse.groups}
    assert groups["mode22"] == ("did:ECM_INJECTOR_PULSE_1", "did:ECM_AIR_CHARGE")
    # The fill byte is paired with the pulse read (E07.4, 0.1 s): a value,
    # in its own window, inside the pulse DID's session window.
    assert groups["fuel_comp_x"] == ("mem:ECM_FUEL_COMP_X_INPUT",)
    assert "sensor:FUEL_PRESSURE" in pulse.specs(role="context")


#: E05.4 (c): the chain's words from the trim term to the timing byte, by the
#: node that writes each (the earliest disagreement is located along these).
TIMING_CHAIN = {
    "timing_trim_terms": "mem:ECM_TIMING_TRIM_TERM",  # 0xFFFF2793
    "base_timing_sum": "mem:ECM_BASE_TIMING_SUM",  # 0xFFFF1A58
    "base_timing_select": "mem:ECM_BASE_TIMING",  # 0xFFFF27DA
    "timing_retard_combine": "mem:ECM_TIMING_PRELIMIT",  # 0xFFFF1346 (and 2332)
    "timing_rate_limit": "mem:ECM_IGNITION_TIMING_FINAL",  # 0xFFFF232E
    "timing_final_byte": "mem:ECM_IGNITION_TIMING_BYTE",  # 0xFFFF11FF
}


def test_ignition_timing_is_derived_from_the_implemented_chain(model: ApplicationModel) -> None:
    """E05.4 (b)/(c) through E09.1: the timing output's observations, every
    chain node's result, the unidentified terms the nodes read, the trim
    lookup with its HI DET byte, rpm word, X byte and coolant, each declared
    by its node, never listed here by hand."""
    derived = {o.output: o for o in derive(model)}
    timing = derived["ignition_timing"]
    assert timing.after is None
    assert timing.specs(role="output") == ("obd:TIMING_ADVANCE", "mem:ECM_IGNITION_TIMING_RAM")
    declared = {c.spec: c.declared_by for c in timing.channels}
    for node, spec in TIMING_CHAIN.items():
        assert declared[spec] == f"node:{node}", spec
    assert declared["mem:ECM_KNOCK_RETARD_APPLIED"] == "node:timing_retard_combine"
    # (b): the trim bytes, the HI DET byte, the X byte, the rpm word, coolant.
    for spec, node in {
        "mem:ECM_TIMING_TRIM_SELECTED": "timing_trim_lookup",
        "mem:ECM_TIMING_TRIM_A": "timing_trim_lookup",
        "mem:ECM_HIGH_DET_FLAGS": "high_det_latch",
        "mem:ECM_FUEL_TARGET_MAP_X_TRACE": "load_input_x",
        "mem:ECM_ENGINE_SPEED_RAM": "engine_speed_word",
        "did:ECM_COOLANT_TEMP": "coolant_byte",
    }.items():
        assert declared[spec] == f"node:{node}", spec
    # The unidentified terms are observed, so the nodes' required arguments
    # are measured (uncovered timing_final_chain).
    for spec in (
        "mem:ECM_TIMING_TERM_27C6",
        "mem:ECM_TIMING_TERM_279A",
        "mem:ECM_TIMING_ENRICH_RETARD",
        "mem:ECM_TIMING_TERM_1D3E",
        "mem:ECM_TIMING_TERM_1344",
        "mem:ECM_TIMING_TRIM_SUBTRAHEND",
        "mem:ECM_TIMING_OTHER_RETARD",
        "mem:ECM_KNOCK_RETARD_TARGET",
    ):
        assert spec in timing.specs(role="input"), spec
    windows = {g.key for g in timing.groups if g.unit == "memory_window"}
    assert {
        "timing_trim",
        "trim_subtrahend",
        "timing_trim_term",
        "base_timing",
        "base_timing_term",
        "valve_timing_term",
        "timing_terms",
        "knock_chain",
        "knock_target",
        "timing_byte",
        "spark",
        "flags_2841",
    } <= windows
    trim = derived["ignition_trim_selected"]
    assert trim.specs(role="output") == ("mem:ECM_TIMING_TRIM_SELECTED",)
    assert "mem:ECM_HIGH_DET_FLAGS" in trim.specs(role="input")
    # The trim lookup is upstream of the chain only: none of the chain's words.
    assert not set(trim.specs()) & set(TIMING_CHAIN.values())


def test_the_derivation_refuses_what_no_capture_can_read(model: ApplicationModel) -> None:
    memory = [(c.name, c.window) for c in DIDS.memory]
    with pytest.raises(ApplicationModelRefusalError, match="kernel_output_undeclared"):
        twin_observation_channels(model, memory, kernel_inputs={"not_an_output": ["obd:RPM"]})
    with pytest.raises(ApplicationModelRefusalError, match="node_undeclared"):
        twin_observation_channels(model, memory, downstream={"pulse": "no_such_node"})
    with pytest.raises(ApplicationModelRefusalError, match="observation_not_pollable"):
        twin_observation_channels(
            model, memory, kernel_context={"target_equiv_ratio": ["derived:TARGET_AFR"]}
        )
    with pytest.raises(ApplicationModelRefusalError, match="observation_not_catalogued"):
        twin_observation_channels(model, [])


def test_a_caller_cannot_demote_a_paired_input_to_context(model: ApplicationModel) -> None:
    memory = [(c.name, c.window) for c in DIDS.memory]
    (phi, *_) = twin_observation_channels(
        model, memory, kernel_context={"target_equiv_ratio": ["mem:ECM_ENGINE_SPEED_RAM"]}
    )
    (rpm,) = [c for c in phi.channels if c.spec == "mem:ECM_ENGINE_SPEED_RAM"]
    assert (rpm.role, rpm.declared_by, rpm.unit) == (
        "input",
        "node:engine_speed_word",
        "memory_window",
    )


# -- the accept test --------------------------------------------------------------------


def test_every_observation_of_every_evaluable_output_is_in_the_twin_profile(
    model: ApplicationModel, twin_profile: CaptureProfile
) -> None:
    gaps, blocked = profile_gaps(derive(model), twin_profile)
    assert gaps == []
    assert blocked == AWAITING_LIVE_READ


def _mutated_model(tmp_path: Path, old: str, new: str) -> ApplicationModel:
    """A scratch copy of the profile's model with one text edit, loaded by
    the real loader (so the schema still holds)."""
    text = (PROFILE_DIR / "ecu_application_model.yaml").read_text()
    assert text.count(old) == 1, old
    scratch = tmp_path / "profile"
    scratch.mkdir()
    (scratch / "ecu_application_model.yaml").write_text(text.replace(old, new))
    return load_application_model(scratch)[0]


ENGINE_SPEED_OBSERVATION = (
    "      - {spec: 'mem:ECM_ENGINE_SPEED_RAM', address: 0xFFFF11D8, relation: identity}\n"
)


def test_a_node_gaining_an_observation_the_profile_lacks_fails(
    tmp_path: Path, twin_profile: CaptureProfile
) -> None:
    mutated = _mutated_model(
        tmp_path,
        ENGINE_SPEED_OBSERVATION,
        ENGINE_SPEED_OBSERVATION
        + "      - {spec: 'did:ECM_RPM_COARSE', address: 0xFFFF11D8, relation: alias}\n",
    )
    gaps, blocked = profile_gaps(derive(mutated), twin_profile)
    assert blocked == AWAITING_LIVE_READ
    assert gaps == [
        f"{output}: did:ECM_RPM_COARSE (node:engine_speed_word, mode22_round) not in mode22"
        # The rpm word is on the timing trim lookup's path too (2026-10-17).
        for output in (
            "target_equiv_ratio",
            "ignition_timing",
            "ignition_trim_selected",
            "base_fuel_schedule_word",
        )
    ]


TIMING_BYTE_OBSERVATION = (
    "      - {spec: 'mem:ECM_IGNITION_TIMING_BYTE', address: 0xFFFF11FF, relation: identity,"
    " confidence: code}\n"
)


def test_a_timing_node_gaining_an_observation_the_profile_lacks_fails(
    tmp_path: Path, twin_profile: CaptureProfile
) -> None:
    """The accept test covers the timing chain too: DID 0x110A reads the
    timing byte, and declaring it on the final node asks for it in every
    session window."""
    mutated = _mutated_model(
        tmp_path,
        TIMING_BYTE_OBSERVATION,
        TIMING_BYTE_OBSERVATION
        + "      - {spec: 'did:ECM_IGNITION_TIMING', address: 0xFFFF11FF, relation: alias}\n",
    )
    gaps, blocked = profile_gaps(derive(mutated), twin_profile)
    assert blocked == AWAITING_LIVE_READ
    assert gaps == [
        "ignition_timing: did:ECM_IGNITION_TIMING (node:timing_final_byte, mode22_round) "
        "not in mode22",
    ]


def test_an_output_gaining_an_unread_memory_observation_changes_the_pinned_set(
    tmp_path: Path, twin_profile: CaptureProfile
) -> None:
    """A hypothesized channel cannot enter the profile, but it cannot slip in
    unnoticed either: the refused set no longer equals the pin."""
    anchor = (
        "      - {spec: 'mem:ECM_AIR_CHARGE_SLOT_103A', address: 0xFFFF103A, relation: identity}\n"
    )
    mutated = _mutated_model(
        tmp_path,
        anchor,
        anchor
        + "      - {spec: 'mem:ECM_AIR_CHARGE_SLOT_1038', address: 0xFFFF1038, relation: alias}\n",
    )
    gaps, blocked = profile_gaps(derive(mutated), twin_profile)
    assert gaps == []
    assert blocked - AWAITING_LIVE_READ == {"mem:ECM_AIR_CHARGE_SLOT_1038"}


# -- the profile's own contract ---------------------------------------------------------


def test_the_twin_profile_is_a_corpus_ineligible_capture(twin_profile: CaptureProfile) -> None:
    assert twin_profile.corpus_eligible is False
    assert twin_profile.storage_prefix == "_capture-twin"
    assert twin_profile.requires_tune_revision is True
    assert set(CAPTURE_CONTEXT_MODE01) <= set(twin_profile.mode01)
    assert twin_profile.mode01_enabled and twin_profile.mode22_enabled
    assert not twin_profile.did_continuous


#: The profile's in-window reads: the paired DIDs (DID 0x1101 is the coolant
#: byte the timing trim lookup reads, derived since 2026-10-17) and memory
#: windows, in the catalog's window order (knock_target is the knock retard
#: target the knock controller reads).
TWIN_DIDS = ("DID_1101", "DID_1206", "DID_1208", "DID_1209")
TWIN_WINDOWS = (
    "fuel_target",
    "spark",
    "fuel_axis",
    "uprev_state",
    "fuel_comp_x",
    "flags_2841",
    "knock_target",
)


def _plan(profile: CaptureProfile) -> dict[str, object]:
    return preview_polling_plan(CanaryConfig.from_yaml(EXAMPLE_YAML).collection, profile)


#: The cycle plan the loop reports for this profile on the Pi's collection
#: settings (``/status.obd_scheduler.did_profile.cycle.plan``, built by
#: ``hybrid_cycle.plan_cycle``): 16 PIDs and the three DTC polls in four
#: six-PID batches, 4 DIDs and 7 memory windows, entry and exit, at 90 req/s,
#: plus the 0.05 s exit grace. ``worst_case_seconds`` is that plus one request
#: timeout (1.0 s), the cycle one unanswered request costs. Before ignition
#: timing was derived (2026-10-17): 18 / 3 / 9, 0.2056 s and 1.2056 s.
CYCLE_PLAN = {
    "mode01_channels": 19,
    "mode01_requests": 4,
    "did_requests": 11,
    "session_requests": 2,
    "required_seconds": 0.2389,
    "worst_case_seconds": 1.2389,
    "feasible": True,
}
#: ``plan_cycle`` for the same work with batching refused (one PID a request);
#: 0.3722 / 1.3722 before the timing channels.
UNBATCHED_CYCLE_PLAN = {"required_seconds": 0.4056, "worst_case_seconds": 1.4056}


def _cycle_plan(batch_max_pids: int) -> dict[str, object]:
    return plan_cycle(
        cycle_seconds=0.5,
        budget_rps=90.0,
        mode01_channels=CYCLE_PLAN["mode01_channels"],  # type: ignore[arg-type]
        batch_max_pids=batch_max_pids,
        did_requests=CYCLE_PLAN["did_requests"],  # type: ignore[arg-type]
        fast_exit=True,
        exit_grace_seconds=0.05,
        lapse_seconds=5.0,
        request_timeout_seconds=1.0,
    ).to_dict()


def test_the_planned_demand_is_the_capacity_evidence(twin_profile: CaptureProfile) -> None:
    """The live plan, asked early (``preview_polling_plan``): every channel at
    the rate its weight asks, and the demand the capacity evidence states;
    and the cycle the scheduler's ``plan_cycle`` makes of that work (the
    slow fake-clock run checks the loop reports the same plan)."""
    plan = _plan(twin_profile)
    rates = {m["channel"]: m["rate_hz"] for m in plan["members"]}  # type: ignore[attr-defined]
    target = twin_profile.target_rate_hz
    assert target == 4.0
    for pid, weight in twin_profile.mode01.items():
        assert rates[pid] == pytest.approx(target * weight)
    assert set(TWIN_DIDS) == {c for c in rates if c.startswith("DID_")}
    for did in TWIN_DIDS:
        assert rates[did] == pytest.approx(2.0)
    windows = TWIN_WINDOWS
    assert sorted(c for c in rates if c.startswith("mem:")) == sorted(f"mem:{w}" for w in windows)
    for window in windows:
        assert rates[f"mem:{window}"] == pytest.approx(2.0)
    requests = plan["diagnostic_requests_per_second"]
    assert plan["channel_reads_per_second"] == pytest.approx(61.0, abs=0.05)
    assert requests["single_request_fallback"] == pytest.approx(65.0, abs=0.05)  # type: ignore[index]
    assert requests["best_case_batched"] == pytest.approx(33.0, abs=0.05)  # type: ignore[index]
    assert twin_profile.expected_total_demand_hz == 65.0
    assert plan["bus_budget_requests_per_second"] == 90.0
    assert plan["fallback_feasible"] is True
    # The cycle's work, counted as the loop counts it (_cycle_plan_for): every
    # selected Mode 01-side request once, every in-window request once.
    mode01 = [c for c in rates if not c.startswith(("DID_", "mem:"))]
    assert len(mode01) == CYCLE_PLAN["mode01_channels"]
    assert len(rates) - len(mode01) == CYCLE_PLAN["did_requests"]
    assert _cycle_plan(6).items() >= CYCLE_PLAN.items()
    assert _cycle_plan(1).items() >= UNBATCHED_CYCLE_PLAN.items()


# -- the session window on the fake clock ------------------------------------------------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeClock]:
    fake = FakeClock()
    install_clock(monkeypatch, fake)
    yield fake
    bus_budget.reset()
    tuning_profile.set_active(False, source="test")


def simulate(
    clock: FakeClock,
    profile: CaptureProfile,
    seconds: float,
    metrics: list[ObdSchedulerMetrics] | None = None,
) -> tuple[list[tuple[float, bytes]], list[ObdReading]]:
    """The real loop with the tracked catalog, ``profile`` (in place of its
    namesake) active at its own budget override, against a scripted ECM
    answering in 10 ms. Mode 06 is refused at once, so its discovery never
    waits out a timeout the truck does not have. Requests come back on the
    readings' (wall) clock; ``metrics`` collects the scheduler snapshots."""
    tracked = CaptureProfileCatalog.model_validate(load_yaml(CATALOG_PATH.read_text()))
    catalog = tracked.model_copy(
        update={"profiles": [profile if p.name == profile.name else p for p in tracked.profiles]}
    )
    default = catalog.by_name("default")
    assert default is not None
    collection = CollectionConfig(
        obd_source="canable",
        obd_pids=tuple(default.mode01),
        obd_dids=tuple(default.mode22),
        obd_batch_max_pids=6,
        obd_did_batch_max_dids=1,
        obd_request_timeout_seconds=1.0,
        obd_poll_rate_hz=1,
        storage_path=Path("unused"),
        session_retention_days=30,
        storage_warning_mb=500,
    )
    spec = dataclasses.replace(
        CanObdSpec.from_config(
            collection,
            CanBusConfig(interface="virtual", channel="twin", bitrate=500000),
            catalog=catalog.profiles,
        ),
        calibration_id=None,
    )
    tuning_profile.configure_default(default)
    tuning_profile.configure_did_aliases(
        did_alias_table(profile_did_union(collection, catalog.profiles))
    )
    tuning_profile.activate(profile, source="test")
    assert profile.bus_budget_override is not None
    bus_budget.set_override(profile.bus_budget_override, source="test", ttl_seconds=None)
    values = {pid: bytes([0x40]) * d.byte_length for pid, d in MODE01_REGISTRY.items()}
    values[0x0C] = b"\x0b\xb8"
    bus = ScriptedEcu(clock, latency=0.010, session_gated=True, values=values, mode06_nrc=0x11)
    readings: list[ObdReading] = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + seconds,
        on_scheduler_metrics=None if metrics is None else metrics.append,
    )
    wall = clock.time() - clock.monotonic()
    requests = [(at + wall, bytes(m.data)) for at, m in bus.sent if m.data[0] >> 4 != 0x3]
    return requests, readings


ENTRY = bytes.fromhex("0210c0")
EXIT = bytes.fromhex("021081")


def session_windows(
    requests: list[tuple[float, bytes]],
) -> list[tuple[list[tuple[float, bytes]], float | None]]:
    """Each complete session window, entry to exit, and the first Mode 01
    request after its exit."""
    out = []
    start: int | None = None
    for i, (_at, data) in enumerate(requests):
        if data.startswith(ENTRY) and start is None:
            start = i
        elif data.startswith(EXIT) and start is not None:
            after = next((at for at, d in requests[i + 1 :] if d[1] == 0x01), None)
            out.append((requests[start : i + 1], after))
            start = None
    return out


#: Simulated seconds. The first session window opens on the profile's cycle
#: (0.011 s in); every pairing is measured from its entry on.
SIMULATED_S = 120.0
#: The margin every pairing keeps inside its tolerance: one request slot at
#: the 90 req/s budget, so moving any one read a slot either way (a different
#: phase of the fake clock, one more read in the window) cannot flip the
#: verdict. A pairing within a slot of its limit is not evidence either way.
PAIRING_MARGIN_S = 1 / 90


def _acquired(readings: list[ObdReading], pid: str, after: float) -> np.ndarray:
    return np.array(
        sorted(r.acquired_at for r in readings if r.pid == pid and (r.acquired_at or 0) > after)
    )


def nearest_gaps(readings: list[ObdReading], anchor: str, other: str, after: float) -> np.ndarray:
    """For each ``anchor`` read, seconds to the nearest ``other`` read."""
    a, b = _acquired(readings, anchor, after), _acquired(readings, other, after)
    assert len(a) and len(b), (anchor, other)
    idx = np.searchsorted(b, a).clip(1, len(b) - 1)
    return np.minimum(np.abs(a - b[idx - 1]), np.abs(b[idx] - a))


def _first_entry(requests: list[tuple[float, bytes]]) -> float:
    """Just before the first session entry: the cut every pairing is measured
    from, so no window is split by it."""
    return session_windows(requests)[0][0][0][0] - 1e-6


def _within(gaps: np.ndarray, limit: float) -> bool:
    return bool(gaps.max() <= limit - PAIRING_MARGIN_S)


@pytest.mark.slow
def test_one_session_window_carries_every_did_and_window_the_twin_pairs(
    clock: FakeClock, twin_profile: CaptureProfile
) -> None:
    """On the fake clock (10 ms answers, 90 req/s), every 0.5 s window from
    the first reads the four paired DIDs and the seven memory windows, memory
    last and back to back (B08.4), every channel runs at its planned rate, and
    the loop reports the cycle plan :data:`CYCLE_PLAN` states. Each kernel
    pairing sits inside its tolerance by :data:`PAIRING_MARGIN_S` on every
    read: pulse to BFS DID 0.011 s (0.1), BFS to MAF DID 0.022 s (0.1), BFS
    DID to Mode 01 rpm 0.184 s (0.25), pulse to Mode 01 rpm and MAF 0.173 s
    (0.3), the lookup's windows within 0.052 s (0.25), and the fill byte
    0.083 s after the pulse DID (E07.4, 0.1). Before ignition timing was
    derived (DID 0x1101, PID 0x0E, the knock_target window; 2026-10-17) those
    were 0.160, 0.149 and 0.069 s."""
    metrics: list[ObdSchedulerMetrics] = []
    requests, readings = simulate(clock, twin_profile, SIMULATED_S, metrics)
    after = _first_entry(requests)
    windows = session_windows(requests)
    assert len(windows) >= 0.95 * SIMULATED_S / 0.5
    for window, _ in windows:
        dids = sorted(f"DID_{d[2:4].hex().upper()}" for _at, d in window if d[1] == 0x22)
        assert dids == sorted(TWIN_DIDS), dids
        assert sum(1 for _at, d in window if d[1] == 0x23) == len(TWIN_WINDOWS)
    span = max(r.acquired_at or 0 for r in readings) - after
    counts = Counter(r.pid for r in readings if (r.acquired_at or 0) > after)
    plan = {m["channel"]: m["rate_hz"] for m in _plan(twin_profile)["members"]}  # type: ignore[attr-defined]
    for pid in [*twin_profile.mode01, *TWIN_DIDS]:
        assert counts[pid] / span == pytest.approx(plan[pid], rel=0.05), pid
    limits = {
        ("DID_1206", "DID_1208"): 0.1,
        ("DID_1208", "DID_1209"): 0.1,
        ("DID_1208", "RPM"): 0.25,
        ("DID_1206", "RPM"): 0.3,
        ("DID_1206", "MAF"): 0.3,
        ("DID_1206", "mem:ECM_FUEL_COMP_X_INPUT"): 0.1,
        ("mem:ECM_TARGET_EQUIV_RATIO", "mem:ECM_ENGINE_SPEED_RAM"): 0.25,
        ("mem:ECM_TARGET_EQUIV_RATIO", "mem:ECM_PEDAL_ENRICH_FLAGS"): 0.25,
        ("mem:ECM_TARGET_EQUIV_RATIO", "mem:ECM_UPREV_MAGIC"): 0.25,
        ("mem:ECM_TARGET_EQUIV_RATIO", "mem:ECM_FUEL_TARGET_MAP_X_TRACE"): 0.25,
    }
    for (anchor, other), limit in limits.items():
        assert _within(nearest_gaps(readings, anchor, other, after), limit), (anchor, other)
    # E07.4's comparison on the ram-study (0 of 192 fill-byte reads had PID
    # 0x0C within 0.1 s) is not asserted here any more: with DID 0x1101 in
    # the window the fake clock puts it at 0.090 s, within one request slot
    # of 0.1 s, which by PAIRING_MARGIN_S decides nothing (0.080 s before).
    # No kernel pairs the two: E07.4 pairs the fill byte with the pulse read,
    # asserted above.
    reported = [m.did_profile["cycle"]["plan"] for m in metrics if m.did_profile]
    assert reported and reported[-1].items() >= CYCLE_PLAN.items()
    assert reported[-1] == _cycle_plan(6)


@pytest.mark.slow
def test_the_awaited_channels_add_no_window_and_bound_the_ram_maf_pairing(
    clock: FakeClock, twin_profile: CaptureProfile
) -> None:
    """The profile as it becomes once :data:`FUEL_AWAITING_LIVE_READ` (bar
    the never-read 0x283C) may be polled. 0x103A widens the fuel_axis read the
    X trace already makes and the selector the uprev_state read, so no window
    is added and the fill byte stays 0.083 s after the pulse DID on every read,
    inside the operator's 0.1 s (2026-10-13) by the margin. (DIDs 0x120F and
    0x1110 in the window as well would put it past the margin, so neither is in
    the profile.)

    The steady-BFS RAM channel set pairs Mode 01 MAF within 0.1 s of the
    0x103A read (``SteadyGates.max_maf_skew_s``, frozen). What holds whatever
    the phase: Mode 01 is silent while the session is held, so no MAF read
    lands inside a window and a memory read is at least its distance to the
    window's edges from the nearest one. Where that puts 0x103A depends on its
    place in the window: 0.100 s here, within a request slot of the limit, so
    the fake clock decides nothing. On the truck DID 0x1208 had MAF within
    0.1 s on 372 of 3,089 reads (_capture-tuning-2026-09-23T142926)."""
    awaited = sorted(s.split(":", 1)[1] for s in FUEL_AWAITING_LIVE_READ)
    awaited.remove("ECM_FUEL_TRANSIENT_FLAGS")
    profile = twin_profile.model_copy(update={"memory": twin_profile.memory + tuple(awaited)})
    requests, readings = simulate(clock, profile, SIMULATED_S)
    after = _first_entry(requests)
    windows = session_windows(requests)
    for window, _ in windows:
        assert sum(1 for _at, d in window if d[1] == 0x23) == len(TWIN_WINDOWS)
        assert not any(d[1] == 0x01 for _at, d in window)
    fill = nearest_gaps(readings, "DID_1206", "mem:ECM_FUEL_COMP_X_INPUT", after)
    assert _within(fill, 0.1)
    bfs = "mem:ECM_AIR_CHARGE_SLOT_103A"
    assert _within(nearest_gaps(readings, bfs, "RPM", after), 0.25)
    assert _within(nearest_gaps(readings, bfs, "mem:ECM_FUEL_COMP_X_INPUT", after), 0.75)
    edges = np.array([(w[0][0], w[-1][0]) for w, _ in windows])
    maf = _acquired(readings, "MAF", after)
    inside = (maf[:, None] > edges[:, 0]) & (maf[:, None] < edges[:, 1])
    assert not inside.any()
    reads = _acquired(readings, bfs, after)
    which = np.searchsorted(edges[:, 0], reads) - 1
    to_edge = np.minimum(reads - edges[which, 0], edges[which, 1] - reads)
    assert (to_edge > 0).all()
    assert (nearest_gaps(readings, bfs, "MAF", after) >= to_edge).all()


#: The 4 Hz Mode 01 group the fuel kernels pair (weight 1.0 at 4 Hz).
FOUR_HZ_GROUP = ("RPM", "MAF", "THROTTLE_POS", "O2_WR_LAMBDA_S1", "COMMANDED_EQUIV_RATIO")


@pytest.mark.slow
def test_the_awaited_timing_windows_do_not_fit_beside_the_fuel_pairings(
    clock: FakeClock, twin_profile: CaptureProfile
) -> None:
    """The measured reason :data:`TIMING_CHAIN_AWAITING_LIVE_READ` cannot simply
    join this profile when its live reads land (E09.1 Remaining). The chain
    adds eight windows and knock_chain, nine reads in every session window:
    the window grows from 0.13 s to 0.25 s, the 4 Hz Mode 01 group falls to
    about 1.96 Hz, DID 0x1208 to PID 0x0C reaches 0.298 s (the steady-BFS
    pairing allows 0.25), and the fill byte trails the pulse DID by 0.093 s,
    inside 0.1 s by less than a request slot. ``plan_cycle`` still calls the
    cycle feasible (0.3389 s of 0.5 s): it counts requests, not the Mode 01
    cadence the window leaves room for. So the chain needs a second,
    timing-only profile, or a scheduler change. If a scheduler change makes
    them fit, this test fails and says so."""
    timing = sorted(s.split(":", 1)[1] for s in TIMING_CHAIN_AWAITING_LIVE_READ)
    profile = twin_profile.model_copy(update={"memory": twin_profile.memory + tuple(timing)})
    plan = plan_cycle(
        cycle_seconds=0.5,
        budget_rps=90.0,
        mode01_channels=CYCLE_PLAN["mode01_channels"],  # type: ignore[arg-type]
        batch_max_pids=6,
        did_requests=CYCLE_PLAN["did_requests"] + 9,  # type: ignore[operator]
        fast_exit=True,
        exit_grace_seconds=0.05,
        lapse_seconds=5.0,
        request_timeout_seconds=1.0,
    ).to_dict()
    assert plan["feasible"] is True
    assert plan["required_seconds"] == pytest.approx(0.3389, abs=1e-4)
    requests, readings = simulate(clock, profile, SIMULATED_S)
    after = _first_entry(requests)
    windows = session_windows(requests)
    for window, _ in windows:
        assert sum(1 for _at, d in window if d[1] == 0x23) == len(TWIN_WINDOWS) + 9
    span = max(r.acquired_at or 0 for r in readings) - after
    counts = Counter(r.pid for r in readings if (r.acquired_at or 0) > after)
    for pid in FOUR_HZ_GROUP:
        assert counts[pid] / span < 0.6 * 4.0, pid
    assert nearest_gaps(readings, "DID_1208", "RPM", after).max() > 0.25
    assert not _within(nearest_gaps(readings, "DID_1206", "mem:ECM_FUEL_COMP_X_INPUT", after), 0.1)


def test_the_profile_lists_only_what_the_derivation_or_a_named_study_needs(
    model: ApplicationModel, twin_profile: CaptureProfile
) -> None:
    """Beyond the derivation, the profile carries the Fuel Comp fit's
    sources (``fuel_comp_fit.yaml``): throttle as PID 0x11 and purge as PID
    0x2E, its first purge source; and bank 2's trims. The HI DET byte left
    this list on 2026-10-17: the timing trim lookup's high_det_latch node now
    declares it. DIDs 0x120F and 0x1110 stay out of this profile (the tuning
    profile reads them): each DID in the pulse DID's window moves the fill
    byte a request slot later, and DID 0x1101 (derived) already took it from
    0.069 s to 0.083 s on the fake clock."""
    derived = {s for obs in derive(model) for s in obs.specs()}
    polled = {f"obd:{p}" for p in twin_profile.mode01}
    polled |= {f"did:{d}" for d in twin_profile.mode22}
    polled |= {f"mem:{m}" for m in twin_profile.memory}
    extra: Mapping[str, str] = {
        "obd:THROTTLE_POS": "fuel_comp_fit throttle_pid",
        "obd:EVAPORATIVE_PURGE": "fuel_comp_fit purge",
        "obd:SHORT_FUEL_TRIM_2": "bank 2",
        "obd:LONG_FUEL_TRIM_2": "bank 2",
    }
    assert polled - derived == set(extra)
