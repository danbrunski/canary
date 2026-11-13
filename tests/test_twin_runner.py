"""E09.2a: one replay runner over the application model, ECU outputs.

Every pinned number is reproduced twice in the same test, through the runner
and through the evaluator that minted it, and the two must agree byte for
byte (the runner's ``native`` result is the evaluator's own result type, so
the comparison is equality, not a tolerance):

* E02's 174-sample October 4 accounting and its two enrichment commands
  (committed fixture; the image-backed variant skips without the ROM);
* E02's X relation, 168 of 194 exact (committed fixture);
* E05.2's 458 steady BFS reads, median 0.9991 (seven local drives);
* E07.3's pulse chain: the committed slice (n 218) and the six tuning
  captures (n 2,361, median 1.0084, p10-p90 0.958-1.025), and the negative
  control without the pressure correction (median 0.936);
* E09.2b, the plant stages on the pulse stage's own samples: the
  ``na-stock-2026-10-05.1`` identification numbers through the runner and
  through ``tools.fuel_plant_check identify --plant-version fuel-mv-1`` (and
  fuel-mv-2's ``na-stock-2026-10-08.mv2``), the held-out figures recorded in
  E07.3 (``evaluate`` on a scratch copy of the profile only: nothing is
  spent), and fuel-mv-2 against fuel-mv-1 per sample as ``compare_versions``
  reads them.

The private inputs (the ZS44A-20260920 image, ``~/canary-data`` sessions)
are not committed; each test that needs one skips by name without it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import math
import re
import shutil
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest

from src.analysis import (
    air_charge_twin,
    fuel_plant,
    fuel_plant_eval,
    injector_pulse,
    target_phi,
    twin_runner,
)
from src.analysis.fuel_plant_eval import (
    build_samples,
    pulse_chain_ratios,
    summarize_ratios,
    without_pressure_correction,
)
from src.analysis.rom_effects import load_rom
from src.analysis.sim_kit import sim_plant_version
from src.analysis.target_phi import TwinRefusalError, evaluate_session, x_formula_check
from src.analysis.twin_runner import (
    EMPTY_KINDS,
    RECORD_COLUMNS,
    ResolvedEcu,
    TwinKit,
    TwinRun,
    TwinRunnerRefusalError,
    _bfs_kind,
    _phi_kind,
    _pulse_kind,
    load_twin_kit,
    node_order,
    resolve_ecu,
    run_twin,
    stage_order,
)
from src.decode import air_charge as air_kernel
from src.decode.target_phi_command import GATE_STATE_UNKNOWN, target_command, x_from_bfs
from src.jsonable import jsonable
from src.preprocessing.loader import read_session
from src.timefmt import format_epoch
from tests.test_target_phi import EXPECT, _session, _table, fixture_frame, record_lines
from tools import fuel_plant_check

REPO = Path(__file__).resolve().parents[1]
PROFILE = REPO / "data" / "nissan-xterra-2008"
ECU_READ = Path.home() / "ecu-re" / "roms" / "ZS44A-20260920.ecuread.bin"
IMAGE_SHA = "a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50"
SESSIONS = Path.home() / "canary-data" / "sessions" / "nissan-xterra-2008"
RAM_STUDY = "_capture-ram-study-2026-10-04T204557"
PULSE_SLICE = REPO / "tests" / "fixtures" / "fuel_plant" / "_capture-tuning-2026-09-23T142926"
#: E05.2's drives (tests/test_air_charge_private_rom.py DRIVES).
BFS_DRIVES = (
    "_capture-fuel-target-research-2026-09-21T195844",
    "_capture-tuning-2026-09-21T223734",
    "_capture-tuning-2026-09-21T232153",
    "_capture-tuning-2026-09-22T170459",
    "_capture-tuning-2026-09-22T235224",
    "_capture-tuning-2026-09-23T142926",
    "_capture-tuning-2026-09-23T164007",
)
REFERENCE = "mem:ECM_TARGET_EQUIV_RATIO"
LAMBDA = "obd:O2_WR_LAMBDA_S1"
X_SPEC = "mem:ECM_FUEL_TARGET_MAP_X_TRACE"
#: E07.3's plant, whose pins these are (the simulator's is fuel-mv-2).
PLANT = "fuel-mv-1"


def _same(ours: float, theirs: float | int | None) -> bool:
    """A runner value (NaN for none) against the evaluator's (``None``)."""
    return math.isnan(ours) if theirs is None else ours == float(theirs)


#: E07.3's six tuning captures (fuel_plant.yaml split, identification + held out).
PULSE_DRIVES = BFS_DRIVES[1:]


@pytest.fixture(scope="module")
def kit() -> TwinKit:
    return load_twin_kit(PROFILE, plant_version=PLANT)


def _committed_ecu(kit: TwinKit) -> ResolvedEcu:
    """The committed target table (afr_calibration.yaml) with the fixture's
    recorded Load Point Scaler: no image."""
    table = dataclasses.replace(
        _table(kit.twin), load_point_scaler=EXPECT["calibration"]["load_point_scaler"]
    )
    return ResolvedEcu(table=table, air=None, pulse=None)


def _image() -> bytes:
    if not ECU_READ.exists():
        pytest.skip("private ROM ZS44A-20260920 (a46baa24...be50) is not on this machine")
    image = ECU_READ.read_bytes()
    if hashlib.sha256(image).hexdigest() != IMAGE_SHA:
        pytest.skip("the local ZS44A-20260920 file is not the expected plain image")
    return image


@pytest.fixture(scope="module")
def image_ecu(kit: TwinKit) -> ResolvedEcu:
    rom = load_rom(
        _image(), kit.rom_map, kit.model, kit.model_sha256, slot=1, slot_source="default_slot"
    )
    ecu = resolve_ecu(kit, rom)
    assert dict(ecu.refusals) == {}
    return ecu


def _sessions(names: tuple[str, ...]) -> list[Path]:
    missing = [n for n in names if not (SESSIONS / n).is_dir()]
    if missing:
        pytest.skip(f"local sessions absent: {missing}")
    return [SESSIONS / n for n in names]


# -- the order and the roles come from the model ----------------------------------------


def test_the_stages_run_in_the_models_node_order(kit: TwinKit) -> None:
    order = node_order(kit.model)
    assert len(order) == len(kit.model.nodes) == len(set(order))
    for node in kit.model.nodes:  # every node after everything it depends on
        assert all(order.index(dep) < order.index(node.id) for dep in node.depends_on)
    scheduled = [n for n in order if n in kit.model.schedule.order()]
    assert scheduled == kit.model.schedule.order()
    assert [s.output for s in stage_order(kit.model)] == [
        "base_fuel_schedule_word",
        "load_input_x",
        "target_equiv_ratio",
        "injector_pulse_word",
    ]


def _with_node(kit: TwinKit, node_id: str, **update: Any) -> Any:
    nodes = [n.model_copy(update=update) if n.id == node_id else n for n in kit.model.nodes]
    return kit.model.model_copy(update={"nodes": nodes})


def test_a_stage_whose_node_the_model_moved_on_from_refuses(kit: TwinKit) -> None:
    moved = _with_node(kit, "base_fuel_schedule", twin="not_modelled", implemented_by=None)
    with pytest.raises(TwinRunnerRefusalError) as refused:
        stage_order(moved)
    assert refused.value.reason == "stage_role_changed"
    other = _with_node(kit, "enrichment_gate", implemented_by="src.analysis.other.predict")
    with pytest.raises(TwinRunnerRefusalError) as refused:
        stage_order(other)
    assert refused.value.reason == "kernel_mismatch"
    computed = _with_node(kit, "load_input_x", twin="implemented", implemented_by="x.y")
    with pytest.raises(TwinRunnerRefusalError) as refused:
        stage_order(computed)
    assert refused.value.reason == "stage_role_changed"


def test_a_reason_no_kind_names_refuses_rather_than_counting() -> None:
    calls: list[Callable[[], object]] = [
        lambda: _bfs_kind("a_new_gate", {}),
        lambda: _phi_kind("unevaluable", ["a_new_reason"], identity_captured=True),
        lambda: _pulse_kind(
            "a_new_check", {}, load_twin_kit(PROFILE, plant_version=PLANT).plant, fuel_cut_reads=0
        ),
    ]
    for call in calls:
        with pytest.raises(TwinRunnerRefusalError) as refused:
            call()
        assert refused.value.reason == "reason_unclassified"
    # A fuel-system mode the gate does not admit is a gate, by state.
    assert _phi_kind("excluded", ["open_loop_fault"], identity_captured=True) == "gated"
    # Several reasons: the decoded logic not covering the sample wins, then
    # an input never read over one read at the wrong time.
    both = ["input_stale:load", "sentinel"]
    assert _phi_kind("unevaluable", both, identity_captured=True) == "outside_model"
    stale_and_missing = ["input_stale:load", "input_not_captured:flag"]
    assert (
        _phi_kind("unevaluable", stale_and_missing, identity_captured=True) == "input_not_captured"
    )
    # An unbracketed map identity is a capture question first.
    unbracketed = ["map_identity_unbracketed"]
    assert _phi_kind("unevaluable", unbracketed, identity_captured=True) == "not_co_sampled"
    assert _phi_kind("unevaluable", unbracketed, identity_captured=False) == "input_not_captured"


def _target_phi_reasons(definition: Any) -> list[tuple[str, str]]:
    """Every ``(state, reason)`` target_phi can put on a record, read from its
    registry (``target_phi.REASONS``: a template stands as ``<prefix>x``) and
    the excluded fuel-system mode names its mode gate declares. The registry
    is the source: ``_reason`` refuses a reason it lacks, so a split tuple, a
    list or a variable cannot carry one past it."""
    found: list[tuple[str, str]] = [
        (state, reason + "x" if reason.endswith(":") else reason)
        for reason, state in target_phi.REASONS.items()
    ]
    found += [("excluded", name) for name in target_phi.excluded_mode_reasons(definition)]
    return found


def test_every_reason_target_phi_can_emit_classifies(kit: TwinKit) -> None:
    reasons = _target_phi_reasons(kit.twin)
    names = {r for _, r in reasons}
    # The enumeration reaches the three the first runner missed, and the rest.
    assert {"map_uninitialised", "slot_ambiguous", "map_switching"} <= names
    assert {"sentinel", "map_identity_unbracketed", "input_stale:x", "awaiting_input:x"} <= names
    assert GATE_STATE_UNKNOWN in names
    assert len(reasons) >= 25
    for state, reason in reasons:
        for captured in (True, False):
            assert _phi_kind(state, [reason], identity_captured=captured) in EMPTY_KINDS
    assert _phi_kind("unevaluable", ["map_uninitialised"], identity_captured=True) == (
        "outside_model"
    )


def test_target_phi_mints_every_reason_through_its_registry() -> None:
    """No reason tuple is written outside ``_reason`` / ``_excluded_mode``,
    and either refuses what the registry does not hold (or holds under
    another state)."""
    source = inspect.getsource(target_phi)
    raw = re.findall(r'(?<!_reason)\(\s*"(?:unevaluable|excluded|pending)"\s*,', source)
    assert raw == []
    assert target_phi._reason("unevaluable", "input_stale:load") == (
        "unevaluable",
        "input_stale:load",
    )
    for state, reason in (
        ("unevaluable", "not_a_reason"),
        ("excluded", "sentinel"),
        ("unevaluable", "input_stale:"),
        ("unevaluable", None),
    ):
        with pytest.raises(TwinRefusalError) as refused:
            target_phi._reason(state, reason)  # type: ignore[arg-type]
        assert refused.value.reason == "reason_unregistered"
    with pytest.raises(TwinRefusalError):
        target_phi._excluded_mode({1: "closed_loop"}, "made_up_mode")


def test_x_reads_are_the_x_formula_checks_reads(kit: TwinKit) -> None:
    """The runner's X stage and ``x_formula_check`` read X through one public
    reader: its reads, paired, are the check's pairs."""
    frame = fixture_frame()
    reads = target_phi.x_reads(frame, kit.twin)
    check = x_formula_check(frame, kit.twin, scaler=1)
    assert (reads.x_spec, reads.bfs_spec) == (X_SPEC, kit.twin.x_formula.bfs_channel)
    assert len(reads.x_times) == len(reads.x_raw) == 194
    pairs = zip(reads.x_times, reads.x_raw, strict=True)
    by_time = {format_epoch(float(t), nanoseconds=True): int(v) for t, v in pairs}
    assert all(by_time[p["acquired_at"]] == p["x"] for p in check["pairs"])


def test_an_uninitialised_map_magic_is_counted_not_refused(kit: TwinKit) -> None:
    """The Oct 4 fixture with every UpRev magic read zeroed: resolve_slot
    names ``map_uninitialised``; the runner counts it and keeps every stage."""
    frame = fixture_frame()
    magic = frame["channel"] == "mem:ECM_UPREV_MAGIC"
    assert magic.any()
    frame.loc[magic, "value_number"] = 0.0
    run = run_twin(frame, kit, _committed_ecu(kit), name=RAM_STUDY, plant_version=PLANT)
    states = run.accounting()["target_equiv_ratio"][REFERENCE]
    assert sum(states.values()) == 174
    assert states.get("map_uninitialised", 0) > 0
    assert run.accounting()["load_input_x"][X_SPEC] == {"eligible": 194}


def test_a_session_without_identity_reads_names_them_not_captured(kit: TwinKit) -> None:
    frame = _session()
    identity = set(kit.twin.map_identity.channels())
    stripped = frame[~frame["channel"].isin(identity)]
    run = run_twin(stripped, kit, _committed_ecu(kit), name="synthetic", plant_version=PLANT)
    assert run.accounting()["target_equiv_ratio"][REFERENCE] == {"map_identity_unbracketed": 1}
    assert run.empty()["target_equiv_ratio"][REFERENCE] == {"input_not_captured": 1}
    # With them, the same sample is eligible.
    whole = run_twin(frame, kit, _committed_ecu(kit), name="synthetic", plant_version=PLANT)
    assert whole.empty()["target_equiv_ratio"][REFERENCE] is None


# -- E02: the October 4 fixture, committed --------------------------------------------------


def _check_october_4(run: TwinRun, native_expected: dict[str, Any]) -> None:
    phi = run.native["target_equiv_ratio"]
    assert phi == native_expected  # the evaluator's own result, byte for byte
    accounting = run.accounting()["target_equiv_ratio"][REFERENCE]
    expected = EXPECT["accounting"]
    assert sum(accounting.values()) == expected["candidates"] == 174
    assert accounting == expected["reasons"]["unevaluable"]
    assert record_lines(phi) == EXPECT["records"]
    rows = run.records[run.records["output"] == "target_equiv_ratio"]
    assert len(rows) == 174 and not (rows["state"] == "eligible").any()
    # 0 eligible, and why: 153 command samples whose inputs were read, but
    # never within the skew of the sample; 21 fuel-cut sentinels.
    assert run.empty()["target_equiv_ratio"] == {
        REFERENCE: {"not_co_sampled": 153, "outside_model": 21}
    }
    # Per record, the runner's observed and predicted words are the evaluator's.
    assert len(rows) == len(phi["records"])
    for row, record in zip(cast(Any, rows.itertuples()), phi["records"], strict=True):
        sample = record["sample"]
        assert row.observed_spec == sample["spec"]
        assert row.acquired_at == pd.Timestamp(sample["acquired_at"]).timestamp()
        assert _same(row.observed, sample["observed_word"])
        assert _same(row.predicted, record["predicted_word"])
        assert row.state == (record["reason"] if record["state"] != "eligible" else "eligible")
    # The two enrichment commands: 0x4A80 at the recorded instants, neither a pass.
    at_override = rows[rows["observed"] == 0x4A80]
    stamps = [
        pd.Timestamp(t, unit="s", tz="UTC").strftime("%H:%M:%S.%f")
        for t in at_override["acquired_at"]
    ]
    assert stamps == [case["command_at"] for case in EXPECT["known_override"]]
    assert set(at_override["kind"]) == {"not_co_sampled"}


def test_the_october_4_accounting_through_the_runner_and_the_evaluator(kit: TwinKit) -> None:
    frame = fixture_frame()
    ecu = _committed_ecu(kit)
    run = run_twin(frame, kit, ecu, name=RAM_STUDY, plant_version=PLANT)
    assert list(run.records.columns) == list(RECORD_COLUMNS)
    assert ecu.table is not None
    old = evaluate_session(frame, kit.twin, ecu.table, definition_sha256=kit.twin_sha256)
    _check_october_4(run, old)
    # No image: the BFS and pulse stages say so per sample, by name.
    bfs = run.records[run.records["output"] == "base_fuel_schedule_word"]
    assert len(bfs) == 194
    assert set(bfs["state"]) == {"calibration_unresolved:air_charge:calibration_not_given"}
    assert run.empty()["base_fuel_schedule_word"] == {
        "DID_1208": "output_not_captured",
        "mem:ECM_AIR_CHARGE_SLOT_103A": {"calibration_unresolved": 194},
    }
    # The capture polled no pulse DID: an output never read, not a gate.
    assert run.empty()["injector_pulse_word"] == {"did:ECM_INJECTOR_PULSE_1": "output_not_captured"}
    # ... so the plant, anchored on it, has no sample, and names the anchor.
    anchor = "input_not_captured:did:ECM_INJECTOR_PULSE_1"
    assert run.empty()["delivered_lambda"] == {LAMBDA: anchor}
    assert run.empty()["manifold_pressure"] == {twin_runner.UNOBSERVED: anchor}


def test_the_x_relation_through_the_runner_and_the_evaluator(kit: TwinKit) -> None:
    frame = fixture_frame()
    run = run_twin(frame, kit, _committed_ecu(kit), name=RAM_STUDY, plant_version=PLANT)
    scaler = EXPECT["calibration"]["load_point_scaler"]
    old = x_formula_check(frame, kit.twin, scaler=scaler)
    assert run.native["load_input_x"] == old
    expected = EXPECT["x_relation"]
    rows = run.eligible("load_input_x")
    assert len(rows) == expected["n_pairs"] == 194
    assert int((rows["residual"] == 0).sum()) == expected["exact"] == 168
    misses = rows["residual"][rows["residual"] != 0].astype(int).value_counts().to_dict()
    assert misses == expected["differences"]
    # Per pair: the runner's own pairing and kernel call give the evaluator's
    # pairs (time, observed X, X from BFS), and every X read is a record.
    ours = sorted(
        (format_epoch(t, nanoseconds=True), o, x)
        for t, o, x in zip(rows["acquired_at"], rows["observed"], rows["predicted"], strict=True)
    )
    theirs = sorted((p["acquired_at"], float(p["x"]), float(p["x_from_bfs"])) for p in old["pairs"])
    assert ours == theirs
    every = run.records[run.records["output"] == "load_input_x"]
    assert len(every) == old["n_pairs"] + old["unmatched"]["x"]
    assert every["acquired_at"].notna().all() and every["observed"].notna().all()


def test_an_unpaired_x_read_keeps_its_time_and_value(kit: TwinKit) -> None:
    frame = fixture_frame()
    bfs = frame.index[frame["channel"] == kit.twin.x_formula.bfs_channel]
    trimmed = frame.drop(bfs[:10])
    run = run_twin(trimmed, kit, _committed_ecu(kit), name=RAM_STUDY, plant_version=PLANT)
    old = x_formula_check(trimmed, kit.twin, scaler=EXPECT["calibration"]["load_point_scaler"])
    rows = run.records[run.records["output"] == "load_input_x"]
    unpaired = rows[rows["state"] == "bfs_unpaired"]
    assert len(unpaired) == old["unmatched"]["x"] >= 10
    assert unpaired["acquired_at"].notna().all() and unpaired["observed"].notna().all()
    assert set(unpaired["kind"]) == {"not_co_sampled"}


def test_a_mutated_lookup_kernel_moves_both_front_ends(
    kit: TwinKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One eligible override sample: the runner and the evaluator agree, then
    a one-count error in the shared kernel shows in both, identically."""
    frame = _session()
    ecu = _committed_ecu(kit)
    assert ecu.table is not None
    table = ecu.table

    def both() -> tuple[float, float]:
        run = run_twin(frame, kit, ecu, name="synthetic", plant_version=PLANT)
        (row,) = cast(Any, run.eligible("target_equiv_ratio").itertuples())
        old = evaluate_session(frame, kit.twin, table, definition_sha256=kit.twin_sha256)
        (record,) = old["records"]
        return float(row.residual), float(record["difference_words"])

    assert both() == (0.0, 0.0)
    real = target_command

    def off_by_one(*args: Any, **kwargs: Any) -> Any:
        command = real(*args, **kwargs)
        assert command.word is not None
        return dataclasses.replace(command, word=command.word + 1)

    monkeypatch.setattr(target_phi, "target_command", off_by_one)
    assert both() == (-1.0, -1.0)


def test_a_mutated_lookup_kernel_moves_the_replay_and_the_paired_comparison(
    kit: TwinKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E09.2c: ``run_paired`` runs the runner's own ECU stages on stated
    points (``twin_runner.stated_ecu``), so one count of error in the shared
    lookup kernel moves a replay residual and a paired phi word alike."""
    from src.analysis.paired_scenarios import run_paired
    from tests.test_paired_scenarios import WOT, _scenario
    from tests.test_sim_montecarlo import build_kit_and_image

    sim_kit, image = build_kit_and_image()
    frame = _session()
    ecu = _committed_ecu(kit)
    q = 1 << kit.twin.reference.q_bits

    def both() -> tuple[float, int]:
        run = run_twin(frame, kit, ecu, name="synthetic", plant_version=PLANT)
        (row,) = cast(Any, run.eligible("target_equiv_ratio").itertuples())
        paired = run_paired(sim_kit, _scenario(sim_kit, image, image, WOT), image, image)
        phi = paired.points[0]["outputs"]["target_phi"]["base"]
        assert phi is not None
        return float(row.residual), round(phi * q)

    residual, word = both()
    real = target_command

    def off_by_one(*args: Any, **kwargs: Any) -> Any:
        command = real(*args, **kwargs)
        word = None if command.word is None else command.word + 1
        return dataclasses.replace(command, word=word)

    monkeypatch.setattr(target_phi, "target_command", off_by_one)
    assert both() == (residual - 1.0, word + 1)


def test_a_zero_load_point_scaler_is_a_named_calibration_refusal(kit: TwinKit) -> None:
    """X divides by the scaler: a zero one is ``calibration_unresolved``,
    named, on every X read (it raised ZeroDivisionError before E09.2c)."""
    ecu = _committed_ecu(kit)
    assert ecu.table is not None
    zero = dataclasses.replace(ecu, table=dataclasses.replace(ecu.table, load_point_scaler=0))
    run = run_twin(fixture_frame(), kit, zero, name=RAM_STUDY, plant_version=PLANT)
    assert run.accounting()["load_input_x"][X_SPEC] == {
        "calibration_unresolved:load_point_scaler_invalid": 194
    }
    assert run.native["load_input_x"] is None


def test_a_mutated_x_kernel_moves_both_front_ends(
    kit: TwinKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = fixture_frame()
    scaler = EXPECT["calibration"]["load_point_scaler"]
    real = x_from_bfs
    monkeypatch.setattr(target_phi, "_x_from_bfs", lambda bfs, sc, **kw: real(bfs + 400, sc, **kw))
    run = run_twin(frame, kit, _committed_ecu(kit), name=RAM_STUDY, plant_version=PLANT)
    old = x_formula_check(frame, kit.twin, scaler=scaler)
    assert run.native["load_input_x"] == old
    exact = int((run.eligible("load_input_x")["residual"] == 0).sum())
    assert exact == old["exact"] < 168


def test_the_runner_imports_nothing_that_reaches_the_ecu() -> None:
    script = textwrap.dedent(
        f"""
        import json, sys
        from src.analysis.twin_runner import load_twin_kit, run_twin, ResolvedEcu
        from tests.test_target_phi import _table, fixture_frame
        from pathlib import Path
        kit = load_twin_kit(Path({str(PROFILE)!r}), plant_version="fuel-mv-1")
        run = run_twin(fixture_frame(), kit, ResolvedEcu(_table(kit.twin), None, None),
                       name={RAM_STUDY!r}, plant_version="fuel-mv-1")
        states = run.accounting()["target_equiv_ratio"]["mem:ECM_TARGET_EQUIV_RATIO"]
        assert sum(states.values()) == 174
        print(json.dumps(sorted(sys.modules)))
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=True,
        timeout=300,
    )
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    write_paths = ("src.control", "src.daemon", "src.dashboard", "src.mcp_server", "can", "serial")
    allowed = {"src.collection", "src.collection.kline", "src.collection.kline.names"}
    reached = [
        m
        for m in loaded
        if any(m == p or m.startswith(p + ".") for p in write_paths)
        or (m.startswith("src.collection") and m not in allowed)
    ]
    assert reached == []


def test_every_kind_a_run_reports_is_declared(kit: TwinKit) -> None:
    run = run_twin(fixture_frame(), kit, _committed_ecu(kit), name=RAM_STUDY, plant_version=PLANT)
    assert set(run.records["kind"]) <= {"eligible", *EMPTY_KINDS}


# -- E02 from the image (private) -----------------------------------------------------------


def test_the_october_4_accounting_from_the_image(kit: TwinKit, image_ecu: ResolvedEcu) -> None:
    frame = fixture_frame()
    run = run_twin(frame, kit, image_ecu, name=RAM_STUDY, plant_version=PLANT)
    assert image_ecu.table is not None
    old = evaluate_session(frame, kit.twin, image_ecu.table, definition_sha256=kit.twin_sha256)
    _check_october_4(run, old)
    # The BFS stage now has a calibration: the 194 RAM-window reads are
    # unscored because PID 0x10 was never read within 0.1 s of one.
    assert run.accounting()["base_fuel_schedule_word"] == {
        "DID_1208": {},
        "mem:ECM_AIR_CHARGE_SLOT_103A": {"maf_unpaired": 194},
    }
    assert run.empty()["base_fuel_schedule_word"]["mem:ECM_AIR_CHARGE_SLOT_103A"] == {
        "not_co_sampled": 194
    }
    # ... which is E05.2's own October 4 answer, through its evaluator.
    assert image_ecu.air is not None
    steady_report = air_charge_twin.evaluate_steady_bfs(
        frame,
        image_ecu.air,
        bfs_channel="mem:ECM_AIR_CHARGE_SLOT_103A",
        maf_channel="MAF",
        load_channel="mem:ECM_FUEL_COMP_X_INPUT",
    )
    assert run.native["base_fuel_schedule_word"] == {"mem:ECM_AIR_CHARGE_SLOT_103A": steady_report}
    assert steady_report.excluded == {"maf_unpaired": 194}


# -- E05.2: steady BFS (private) ------------------------------------------------------------


def _raw_rows(session: Path) -> pd.DataFrame:
    """The rows E05.2's own test hands the evaluator (``_read`` there)."""
    from tests.test_air_charge_private_rom import _read

    return _read([str(session / "telemetry-*.parquet")])


@pytest.fixture(scope="module")
def drive_runs(kit: TwinKit, image_ecu: ResolvedEcu) -> dict[str, TwinRun]:
    return {
        path.name: run_twin(path, kit, image_ecu, plant_version=PLANT)
        for path in _sessions(BFS_DRIVES)
    }


@pytest.mark.slow
def test_steady_bfs_through_the_runner_and_the_evaluator(
    image_ecu: ResolvedEcu, drive_runs: dict[str, TwinRun]
) -> None:
    assert image_ecu.air is not None
    runner_records: list[air_charge_twin.BfsRecord] = []
    old_records: list[air_charge_twin.BfsRecord] = []
    runner_excluded: dict[str, int] = {}
    old_excluded: dict[str, int] = {}
    for name in BFS_DRIVES:
        native = drive_runs[name].native["base_fuel_schedule_word"]["DID_1208"]
        old = air_charge_twin.evaluate_steady_bfs(_raw_rows(SESSIONS / name), image_ecu.air)
        # Both now pair through air_charge_twin.pair_bfs_inputs (match_ordered),
        # so this pins the runner's routing, not the pairing itself; the
        # evidence that the one-to-one pairing changed nothing on these drives
        # is E09.2a's HEAD comparison (every field, the 7 drives + RAM study).
        assert native == old, name
        runner_records += native.records
        old_records += old.records
        for target, report in ((runner_excluded, native), (old_excluded, old)):
            for reason, n in report.excluded.items():
                target[reason] = target.get(reason, 0) + n
    runner = air_charge_twin.SteadyBfsReport(tuple(runner_records), runner_excluded)
    old_report = air_charge_twin.SteadyBfsReport(tuple(old_records), old_excluded)
    assert runner == old_report
    summary = runner.summary()
    assert summary["scored"] == 458
    assert round(summary["median_ratio"], 4) == 0.9991
    assert summary == old_report.summary()
    assert runner.total == 14_843
    # The runner's records carry the same per-sample ratios.
    eligible = [drive_runs[n].eligible("base_fuel_schedule_word") for n in BFS_DRIVES]
    ratios = np.concatenate(
        [e[e["observed_spec"] == "DID_1208"]["ratio"].to_numpy() for e in eligible]
    )
    assert ratios.tolist() == [r.ratio for r in runner_records]


@pytest.mark.slow
def test_a_mutated_bfs_kernel_moves_both_front_ends(
    kit: TwinKit, image_ecu: ResolvedEcu, monkeypatch: pytest.MonkeyPatch
) -> None:
    (session,) = _sessions((BFS_DRIVES[1],))
    assert image_ecu.air is not None
    real = air_kernel.steady_state_bfs
    monkeypatch.setattr(
        air_kernel, "steady_state_bfs", lambda filtered, ceiling: real(filtered, ceiling) * 2
    )
    run = run_twin(
        session, kit, dataclasses.replace(image_ecu, table=None, pulse=None), plant_version=PLANT
    )
    old = air_charge_twin.evaluate_steady_bfs(_raw_rows(session), image_ecu.air)
    assert run.native["base_fuel_schedule_word"]["DID_1208"] == old
    median = old.summary()["median_ratio"]
    assert median == pytest.approx(0.5, abs=0.01)


# -- E07.3: the pulse chain (private) -------------------------------------------------------


def _old_pulse(session: Path, kit: TwinKit, cal: injector_pulse.PulseChainCalibration) -> Any:
    frame = build_samples(session, kit.plant, kit.delay)
    return pulse_chain_ratios(frame, cal, kit.plant.pulse_chain)


def test_the_pulse_chain_slice_through_the_runner_and_the_evaluator(
    kit: TwinKit, image_ecu: ResolvedEcu
) -> None:
    assert image_ecu.pulse is not None
    run = run_twin(
        PULSE_SLICE, kit, dataclasses.replace(image_ecu, table=None, air=None), plant_version=PLANT
    )
    old = _old_pulse(PULSE_SLICE, kit, image_ecu.pulse)
    pd.testing.assert_frame_equal(run.native["injector_pulse_word"], old)
    stats = summarize_ratios(run.eligible("injector_pulse_word")["ratio"].to_numpy(dtype=float))
    assert stats == summarize_ratios(old["ratio"].to_numpy(dtype=float))
    assert stats["n"] == 218
    assert stats["median"] == pytest.approx(1.0084, abs=0.0005)
    # Every pulse read is accounted for, once.
    frame = build_samples(PULSE_SLICE, kit.plant, kit.delay)
    states = run.accounting()["injector_pulse_word"]["did:ECM_INJECTOR_PULSE_1"]
    assert sum(states.values()) == len(frame)
    # The reads the ratios score are exactly the ones named eligible.
    named = fuel_plant_eval.pulse_chain_states(frame, kit.plant.pulse_chain)
    assert frame.index[named == "eligible"].equals(old.index)
    # Per record: the runner's observed and predicted pulse words are the
    # evaluator's measured and predicted words (counts, dead time included).
    rows = run.eligible("injector_pulse_word")
    ts = old["ecu_dead_time_counts"].to_numpy(dtype=float)
    np.testing.assert_array_equal(rows["acquired_at"].to_numpy(), old["t"].to_numpy())
    np.testing.assert_array_equal(
        rows["observed"].to_numpy(), old["measured_effective"].to_numpy(dtype=float) + ts
    )
    np.testing.assert_array_equal(
        rows["predicted"].to_numpy(), old["predicted_effective"].to_numpy(dtype=float) + ts
    )
    np.testing.assert_array_equal(rows["ratio"].to_numpy(), old["ratio"].to_numpy(dtype=float))


def test_a_mutated_pulse_kernel_moves_both_front_ends(
    kit: TwinKit, image_ecu: ResolvedEcu, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert image_ecu.pulse is not None
    real = injector_pulse.pressure_correction_word
    monkeypatch.setattr(
        injector_pulse,
        "pressure_correction_word",
        lambda vac512, cal: real(vac512, cal) * 9 // 10,
    )
    run = run_twin(
        PULSE_SLICE, kit, dataclasses.replace(image_ecu, table=None, air=None), plant_version=PLANT
    )
    old = _old_pulse(PULSE_SLICE, kit, image_ecu.pulse)
    pd.testing.assert_frame_equal(run.native["injector_pulse_word"], old)
    assert not 0.98 <= float(np.median(old["ratio"])) <= 1.04


@pytest.mark.slow
def test_the_pulse_chain_on_six_captures_and_its_negative_control(
    kit: TwinKit, image_ecu: ResolvedEcu, drive_runs: dict[str, TwinRun]
) -> None:
    assert image_ecu.pulse is not None
    control = without_pressure_correction(image_ecu.pulse)
    control_ecu = ResolvedEcu(table=None, air=None, pulse=control)
    runner, old, runner_neg, old_neg = [], [], [], []
    for session in _sessions(PULSE_DRIVES):
        native = drive_runs[session.name].native["injector_pulse_word"]
        frame = build_samples(session, kit.plant, kit.delay)
        expected = pulse_chain_ratios(frame, image_ecu.pulse, kit.plant.pulse_chain)
        pd.testing.assert_frame_equal(native, expected)
        runner.append(drive_runs[session.name].eligible("injector_pulse_word")["ratio"].to_numpy())
        old.append(expected["ratio"].to_numpy(dtype=float))
        negative = run_twin(session, kit, control_ecu, plant_version=PLANT)
        expected_neg = pulse_chain_ratios(frame, control, kit.plant.pulse_chain)
        pd.testing.assert_frame_equal(negative.native["injector_pulse_word"], expected_neg)
        runner_neg.append(negative.eligible("injector_pulse_word")["ratio"].to_numpy())
        old_neg.append(expected_neg["ratio"].to_numpy(dtype=float))
    stats = summarize_ratios(np.concatenate(runner))
    assert stats == summarize_ratios(np.concatenate(old))
    assert stats["n"] == 2361
    assert round(stats["median"], 4) == 1.0084
    assert (round(stats["p10"], 3), round(stats["p90"], 3)) == (0.958, 1.025)
    neg = summarize_ratios(np.concatenate(runner_neg))
    assert neg == summarize_ratios(np.concatenate(old_neg))
    assert neg["n"] == 2361
    assert round(neg["median"], 3) == 0.936


@pytest.mark.slow
def test_the_target_phi_twin_on_the_tuning_captures_is_empty_and_says_why(
    drive_runs: dict[str, TwinRun],
) -> None:
    """The tuning captures polled the command word but never the lookup's
    load input X: no target-phi sample can be scored, and the runner names
    the kinds rather than reporting a bare zero."""
    for name in PULSE_DRIVES:
        run = drive_runs[name]
        assert run.input_reads["target_equiv_ratio"]["mem:ECM_FUEL_TARGET_MAP_X_TRACE"] == 0
        empty = run.empty()["target_equiv_ratio"][REFERENCE]
        assert isinstance(empty, dict) and empty, name
        assert "input_not_captured" in empty, name
        assert run.empty()["load_input_x"] == {X_SPEC: "output_not_captured"}
        # BFS: the DID round scores; the RAM window was never read, and the
        # first channel's eligible reads do not hide that.
        assert run.empty()["base_fuel_schedule_word"] == {
            "DID_1208": None,
            "mem:ECM_AIR_CHARGE_SLOT_103A": "output_not_captured",
        }


def test_fuel_plant_eval_seams_leave_the_evaluator_unchanged() -> None:
    """``build_samples`` on a frame of reads, named, equals it on the
    directory; ``pulse_chain_states`` names exactly the reads the ratios score."""

    plant = load_twin_kit(PROFILE, plant_version=PLANT).plant
    delay = load_twin_kit(PROFILE, plant_version=PLANT).delay
    on_path = build_samples(PULSE_SLICE, plant, delay)
    reads = read_session(PULSE_SLICE)
    with pytest.raises(ValueError):
        build_samples(reads, plant, delay)
    on_frame = build_samples(reads, plant, delay, name=PULSE_SLICE.name)
    pd.testing.assert_frame_equal(on_frame, on_path)
    states = fuel_plant_eval.pulse_chain_states(on_path, plant.pulse_chain)
    assert len(states) == len(on_path)
    assert set(states) <= {
        "eligible",
        "not_fuelled",
        "not_steady",
        "bfs_not_steady",
        "reference_phi_not_positive",
        *(f"input_missing:{c}" for c in fuel_plant_eval.PULSE_CHAIN_INPUTS),
    }


def test_two_bfs_reads_10_ms_apart_share_no_maf_read() -> None:
    """Verifier case (2026-10-16): two BFS reads 10 ms apart, one MAF read
    between them and no other within the 0.1 s skew. Nearest-with-reuse (the
    evaluator before version 2) scored both from that one read; one-to-one
    pairing gives it to the first and names the second ``maf_unpaired``. The
    evaluator and the runner now agree, record for record."""
    from tests.test_air_charge import _calibration

    cal = _calibration()
    bfs = air_charge_twin.predict_steady_bfs(cal, maf_word=1500, rpm_word=160).mid
    rpm_payload = bytes([(160 * 50) >> 8, (160 * 50) & 0xFF])

    def row(channel: str, at: float, text: str | None = None) -> tuple[Any, ...]:
        meta = json.dumps({"acquired_at": at})
        if channel == "RPM":
            return (channel, 2000.0, None, rpm_payload, meta)
        return (channel, None, text, None, meta)

    maf_times = [99.4, 99.55, 99.7, 99.85, 100.005, 100.16, 100.3, 100.45, 100.6]
    rows = [row("DID_1209", t, f"{1500:04x}") for t in maf_times]
    rows += [row("RPM", t) for t in (99.6, 99.8, 100.001, 100.011, 100.2, 100.4)]
    rows += [row("DID_1208", t, f"{bfs:04x}") for t in (100.0, 100.010)]
    frame = pd.DataFrame(
        rows, columns=["channel", "value_number", "value_text", "payload", "metadata_json"]
    )
    frame["timestamp"] = pd.Timestamp("2026-10-05", tz="UTC")
    old = air_charge_twin.evaluate_steady_bfs(frame, cal)
    assert [r.acquired_at for r in old.records] == [100.0]
    assert old.excluded == {"maf_unpaired": 1}
    stage = twin_runner._run_bfs(frame, ResolvedEcu(table=None, air=cal, pulse=None))
    assert stage.native == {"DID_1208": old}
    assert [(r["state"], r["kind"]) for r in stage.records] == [
        ("eligible", "eligible"),
        ("maf_unpaired", "not_co_sampled"),
    ]


# -- E09.2b: the plant stages ------------------------------------------------------------

PLANT_OUTPUTS = ("delivered_lambda", "fuel_per_event", "manifold_pressure")
MV2 = "fuel-mv-2"


def _plant_rows(run: TwinRun, output: str) -> pd.DataFrame:
    return run.records[run.records["output"] == output].reset_index(drop=True)


def _errors_of(rows: pd.DataFrame) -> np.ndarray:
    """The plant's lambda error per record, as the evaluator computes it."""
    err: np.ndarray = 100.0 * (
        rows["predicted"].to_numpy(dtype=float) / rows["observed"].to_numpy(dtype=float) - 1.0
    )
    return err


def test_every_state_the_plant_names_classifies() -> None:
    """Enumerated from ``fuel_plant_eval.STATES``: a state added there without a
    kind here fails, and so does a check ``build_samples`` names that STATES
    does not declare."""
    kinds = twin_runner.plant_state_kinds()
    assert set(kinds) == set(fuel_plant_eval.STATES) - {"eligible"}
    for state, possible in kinds.items():
        assert possible and set(possible) <= set(EMPTY_KINDS), state
    assert kinds["not_co_observed"] == ("not_co_sampled", "input_not_captured")
    assert kinds["missing_input"] == ("not_co_sampled", "input_not_captured")
    assert {k for k, v in kinds.items() if v == ("gated",)} == {
        "cold",
        "post_fuel_cut",
        "transient",
    }
    assert {k for k, v in kinds.items() if v == ("outside_model",)} == {
        "fuel_cut",
        "lambda_rail",
        "short_pulse",
        fuel_plant.MAP_OUTSIDE_VE_FIT,
        fuel_plant.MAP_ABOVE_AMBIENT,
    }
    with pytest.raises(TwinRunnerRefusalError) as refused:
        twin_runner._plant_kind("a_new_check", input_captured=True)
    assert refused.value.reason == "reason_unclassified"
    source = inspect.getsource(fuel_plant_eval.build_samples)
    block = source[source.index("checks: list") : source.index("for name, mask in checks")]
    named = set(re.findall(r'\(\s*"([a-z_]+)",', block))
    assert len(named) == 8 and named <= set(fuel_plant_eval.STATES)
    # The manifold refusals are states too, and the plant's input columns are
    # the evaluator's own list.
    plant_source = inspect.getsource(fuel_plant)
    refusals = set(re.findall(r"reason\[[a-z_]+\] = (MAP_[A-Z_]+)", plant_source))
    assert {getattr(fuel_plant, r) for r in refusals} <= set(fuel_plant_eval.STATES)
    assert len(refusals) == 2
    assert set(twin_runner._PLANT_INPUT_SPECS) == set(fuel_plant_eval.PLANT_SAMPLE_INPUTS)


def test_the_plant_version_is_named_never_defaulted(kit: TwinKit) -> None:
    for function in (load_twin_kit, run_twin):
        parameter = inspect.signature(function).parameters["plant_version"]
        assert parameter.default is inspect.Parameter.empty, function.__name__
    with pytest.raises(TwinRunnerRefusalError) as refused:
        load_twin_kit(PROFILE, plant_version=None)
    assert refused.value.reason == "plant_version_missing"
    frame = fixture_frame()
    for version, reason in ((None, "plant_version_missing"), (MV2, "plant_version_mismatch")):
        with pytest.raises(TwinRunnerRefusalError) as refused:
            run_twin(frame, kit, _committed_ecu(kit), name=RAM_STUDY, plant_version=version)
        assert refused.value.reason == reason
    # The simulator's plant is its declaration's, passed by name.
    simulator = sim_plant_version(PROFILE)
    assert simulator == MV2
    assert load_twin_kit(PROFILE, plant_version=simulator).plant.plant_version == MV2


def test_the_plant_runs_on_the_pulse_stages_own_samples(kit: TwinKit) -> None:
    """The committed slice, no image: one sample frame for the pulse word and
    the plant; per record the plant's states, predictions and errors are the
    evaluator's (``apply_manifold_refusals``, ``predict``, ``_errors``)."""
    run = run_twin(PULSE_SLICE, kit, ResolvedEcu(None, None, None), plant_version=PLANT)
    assert run.order[-3:] == PLANT_OUTPUTS
    expected = build_samples(PULSE_SLICE, kit.plant, kit.delay)
    pd.testing.assert_frame_equal(run.samples, expected)
    pulse = _plant_rows(run, "injector_pulse_word")
    for output in PLANT_OUTPUTS:
        rows = _plant_rows(run, output)
        np.testing.assert_array_equal(rows["acquired_at"].to_numpy(), pulse["acquired_at"])
        np.testing.assert_array_equal(rows["acquired_at"].to_numpy(), expected["t"].to_numpy())
    params = fuel_plant.resolve_parameters(kit.plant, kit.delay)
    scored = fuel_plant_eval.apply_manifold_refusals(expected, kit.plant, params)
    lam = _plant_rows(run, "delivered_lambda")
    assert lam["state"].tolist() == scored["state"].tolist()
    assert run.accounting()["delivered_lambda"][LAMBDA] == {
        "eligible": 152,
        "fuel_cut": 106,
        "lambda_rail": 16,
        "missing_input": 1,
        "post_fuel_cut": 462,
        "transient": 859,
    }
    eligible = scored[scored["state"] == "eligible"]
    out = fuel_plant.predict(fuel_plant_eval.plant_inputs(eligible), params, kit.plant.geometry)
    got = lam[lam["state"] == "eligible"]
    np.testing.assert_array_equal(got["predicted"].to_numpy(), out.lambda_delivered)
    np.testing.assert_array_equal(got["observed"].to_numpy(), eligible["lambda_obs"].to_numpy())
    np.testing.assert_array_equal(
        _errors_of(got), fuel_plant_eval._errors(eligible, kit.plant, kit.delay)
    )
    np.testing.assert_array_equal(
        got["ratio"].to_numpy(), got["observed"].to_numpy() / got["predicted"].to_numpy()
    )
    # Fuel and MAP: predicted, never observed, never "eligible".
    for output, column in (("fuel_per_event", "fuel_g"), ("manifold_pressure", "map_kpa")):
        rows = _plant_rows(run, output)
        assert not (rows["state"] == "eligible").any()
        unobserved = rows[rows["state"] == twin_runner.UNOBSERVED]
        assert set(unobserved["kind"]) == {"unobserved"}
        assert unobserved["observed"].isna().all() and unobserved["residual"].isna().all()
        np.testing.assert_array_equal(unobserved["predicted"].to_numpy(), getattr(out, column))
        assert (rows["state"] != twin_runner.UNOBSERVED).tolist() == (
            lam["state"] != "eligible"
        ).tolist()
    assert run.empty()["delivered_lambda"] == {LAMBDA: None}
    assert run.empty()["manifold_pressure"] == {
        twin_runner.UNOBSERVED: {
            "not_co_sampled": 1,
            "gated": 1321,
            "outside_model": 122,
            "unobserved": 152,
        }
    }
    assert set(run.records["kind"]) <= {"eligible", *EMPTY_KINDS}
    assert run.provenance["stage_details"]["delivered_lambda"]["baseline_id"] == (
        "na-stock-2026-10-05.1"
    )


def test_a_lambda_never_read_is_not_captured_not_co_sampled(kit: TwinKit) -> None:
    reads = read_session(PULSE_SLICE)
    stripped = reads[reads["channel"] != "O2_WR_LAMBDA_S1"]
    run = run_twin(
        stripped, kit, ResolvedEcu(None, None, None), name=PULSE_SLICE.name, plant_version=PLANT
    )
    lam = _plant_rows(run, "delivered_lambda")
    assert set(lam["state"]) <= {"missing_input", "not_co_observed"}
    assert set(lam["kind"]) == {"input_not_captured"}
    assert run.input_reads["delivered_lambda"][LAMBDA] == 0
    # With the reads, the one sample whose lambda read fell outside the
    # pairing is captured but not co-sampled.
    whole = run_twin(PULSE_SLICE, kit, ResolvedEcu(None, None, None), plant_version=PLANT)
    missing = _plant_rows(whole, "delivered_lambda")
    assert missing[missing["state"] == "missing_input"]["kind"].tolist() == ["not_co_sampled"]


def test_a_dead_time_nothing_could_derive_is_not_captured(kit: TwinKit) -> None:
    """Verifier case (2026-10-16): PID 0x42 never polled and no fuel-cut read
    (the slice's ``COMMANDED_EQUIV_RATIO == 0`` reads dropped). No sample can
    have a dead time, so reading the fuel-cut reference at other values does
    not make it ``not_co_sampled``: pulse and plant both say not captured."""
    reads = read_session(PULSE_SLICE)
    cut = (reads["channel"] == "COMMANDED_EQUIV_RATIO") & (reads["value_number"] == 0)
    assert int(cut.sum()) == 196
    run = run_twin(
        reads[~cut],
        kit,
        dataclasses.replace(_committed_ecu(kit), pulse=_any_pulse_calibration()),
        name=PULSE_SLICE.name,
        plant_version=PLANT,
    )
    assert run.samples["dead_time_source"].value_counts().to_dict() == {"none": 1596}
    assert run.input_reads["delivered_lambda"][kit.plant.specs.fuel_cut_reference] > 0
    lam = _plant_rows(run, "delivered_lambda")
    assert lam.groupby(["state", "kind"]).size().to_dict() == {
        ("missing_input", "input_not_captured"): 1596
    }
    pulse = _plant_rows(run, "injector_pulse_word")
    dead = pulse[pulse["state"] == "input_missing:ecu_dead_time_counts"]
    assert len(dead) > 0 and set(dead["kind"]) == {"input_not_captured"}
    assert run.provenance["stage_details"]["delivered_lambda"]["dead_time_source"] == ["none"]
    # With the fuel-cut reads, the dead time comes from them.
    whole = run_twin(PULSE_SLICE, kit, ResolvedEcu(None, None, None), plant_version=PLANT)
    assert set(whole.samples["dead_time_source"]) == {"fuel_cut_reads"}


def _any_pulse_calibration() -> injector_pulse.PulseChainCalibration:
    """A pulse calibration of the declared shape (the kinds, not the ratios,
    are under test): the profile's declared words and flat curves."""
    chain = load_twin_kit(PROFILE, plant_version=PLANT).plant.pulse_chain
    return injector_pulse.PulseChainCalibration(
        dead_time_base=chain.dead_time_base.value,
        dead_time_slope=chain.dead_time_slope.value,
        dead_time_vref=chain.dead_time_vref,
        rail_reference=chain.rail_reference.value,
        flow_scaler=chain.flow_scaler.value,
        vacuum_load_curve=(128,) * 16,
        vacuum_rpm_curve=(128,) * 16,
        minimum_g=chain.minimum_g,
    )


def test_a_plant_without_a_frozen_baseline_is_unresolved_per_sample(kit: TwinKit) -> None:
    unfrozen = dataclasses.replace(kit, plant=kit.plant.model_copy(update={"baseline": None}))
    run = run_twin(PULSE_SLICE, unfrozen, ResolvedEcu(None, None, None), plant_version=PLANT)
    for output in PLANT_OUTPUTS:
        rows = _plant_rows(run, output)
        assert set(rows["state"]) == {"calibration_unresolved:plant:baseline_not_frozen"}
        assert set(rows["kind"]) == {"calibration_unresolved"}
    # The ECU stages do not depend on the plant's baseline.
    frozen = run_twin(PULSE_SLICE, kit, ResolvedEcu(None, None, None), plant_version=PLANT)
    pd.testing.assert_frame_equal(
        _plant_rows(run, "injector_pulse_word"), _plant_rows(frozen, "injector_pulse_word")
    )


def test_a_mutated_plant_kernel_moves_both_front_ends(
    kit: TwinKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    def errors() -> tuple[np.ndarray, np.ndarray]:
        run = run_twin(PULSE_SLICE, kit, ResolvedEcu(None, None, None), plant_version=PLANT)
        lam = _plant_rows(run, "delivered_lambda")
        eligible = run.samples[(lam["state"] == "eligible").to_numpy()]
        return _errors_of(lam[lam["state"] == "eligible"]), fuel_plant_eval._errors(
            eligible, kit.plant, kit.delay
        )

    ours, theirs = errors()
    np.testing.assert_array_equal(ours, theirs)
    real = fuel_plant.flow_factor
    monkeypatch.setattr(fuel_plant, "flow_factor", lambda dp, ref, exp: real(dp, ref, exp) * 1.1)
    mutated, mutated_theirs = errors()
    np.testing.assert_array_equal(mutated, mutated_theirs)
    assert float(np.median(mutated - ours)) < -5.0


# -- E09.2b on the recorded drives (private) -----------------------------------------------

#: E07.3 / E07.5's identification numbers, as frozen into each baseline.
IDENTIFICATION: dict[str, dict[str, Any]] = {
    PLANT: {
        "value": 0.051063,
        "interval": [0.050591, 0.051501],
        "n_samples": 766,
        "n_regions": 89,
        "abs_p90_pct": 12.17,
        "region_pass_fraction": 0.584,
        "null_abs_p90_pct": 3.35,
        "refused": {},
    },
    MV2: {
        "value": 0.050792,
        "interval": [0.050357, 0.051198],
        "n_samples": 764,
        "n_regions": 89,
        "abs_p90_pct": 11.61,
        "region_pass_fraction": 0.618,
        "null_abs_p90_pct": None,
        "refused": {"manifold_above_ambient": 2},
    },
}


@pytest.fixture(scope="module")
def mv1_runs(
    kit: TwinKit, image_ecu: ResolvedEcu, drive_runs: dict[str, TwinRun]
) -> dict[str, TwinRun]:
    (ram,) = _sessions((RAM_STUDY,))
    return {**drive_runs, RAM_STUDY: run_twin(ram, kit, image_ecu, plant_version=PLANT)}


@pytest.fixture(scope="module")
def mv2_runs() -> dict[str, TwinRun]:
    kit2 = load_twin_kit(PROFILE, plant_version=MV2)
    none = ResolvedEcu(None, None, None)
    return {
        path.name: run_twin(path, kit2, none, plant_version=MV2)
        for path in _sessions((*PULSE_DRIVES, RAM_STUDY))
    }


@pytest.mark.slow
@pytest.mark.parametrize("version", [PLANT, MV2])
def test_the_identification_numbers_through_the_runner_and_the_cli(
    version: str, request: pytest.FixtureRequest, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = request.getfixturevalue("mv1_runs" if version == PLANT else "mv2_runs")
    kit_v = load_twin_kit(PROFILE, plant_version=version)
    plant = kit_v.plant
    assert plant.baseline is not None
    ident = [runs[n] for n in plant.split.identification]
    # Handed in any order: the runner concatenates in the split's order.
    result = twin_runner.identify_plant(ident[::-1], kit_v, sessions_root=SESSIONS)
    ours = json.dumps(jsonable(result, ndigits=6), indent=2)
    argv = ["identify", "--plant-version", version, "--sessions-root", str(SESSIONS)]
    assert fuel_plant_check.main(argv) == 0
    assert ours == capsys.readouterr().out.rstrip("\n")  # byte for byte
    pinned, expected = json.loads(ours), IDENTIFICATION[version]
    assert pinned["value"] == expected["value"] == plant.baseline.values["stoich_air_per_open_ms"]
    assert pinned["interval"] == expected["interval"]
    assert (pinned["n_samples"], pinned["n_regions"]) == (
        expected["n_samples"],
        expected["n_regions"],
    )
    assert round(pinned["in_sample"]["abs_p90_pct"], 2) == expected["abs_p90_pct"]
    assert round(pinned["in_sample"]["region_pass_fraction"], 3) == expected["region_pass_fraction"]
    if expected["null_abs_p90_pct"] is not None:
        null = pinned["in_sample_null_lambda_one"]["abs_p90_pct"]
        assert round(null, 2) == expected["null_abs_p90_pct"]
    for state, n in expected["refused"].items():
        assert pinned["states"][state] == n
    # Per record: under the frozen baseline the runner scores exactly the
    # reads identification fitted on, and predicts each as predict does.
    lam = pd.concat([_plant_rows(run, "delivered_lambda") for run in ident[::-1]])
    lam = lam[lam["state"] == "eligible"]
    assert len(lam) == expected["n_samples"]
    data = pd.concat([run.samples for run in ident[::-1]], ignore_index=True)
    params = fuel_plant.resolve_parameters(plant, kit_v.delay)
    eligible = fuel_plant_eval.apply_manifold_refusals(data, plant, params)
    eligible = eligible[eligible["state"] == "eligible"]
    assert eligible["region_id"].nunique() == expected["n_regions"]
    np.testing.assert_array_equal(lam["acquired_at"].to_numpy(), eligible["t"].to_numpy())
    np.testing.assert_array_equal(lam["observed"].to_numpy(), eligible["lambda_obs"].to_numpy())
    out = fuel_plant.predict(fuel_plant_eval.plant_inputs(eligible), params, plant.geometry)
    np.testing.assert_array_equal(lam["predicted"].to_numpy(), out.lambda_delivered)
    # At the frozen (rounded) scale the in-sample figure is the fit's, to
    # the precision recorded.
    regions = eligible["region_id"].to_numpy()
    in_sample = fuel_plant_eval._stats(_errors_of(lam), regions, plant)
    assert round(in_sample["abs_p90_pct"], 2) == expected["abs_p90_pct"]


#: E07.3's held-out figures (fuel-mv-1), median error by MAF band: (reads,
#: median %, the decimals recorded).
HELD_OUT_BANDS = {
    "[0.0, 5.0)": (614, 1.70, 2),
    "[5.0, 10.0)": (24, 1.04, 2),
    "[10.0, 20.0)": (14, 22.2, 1),
    "[20.0, 40.0)": (25, 7.0, 1),
    "[40.0, 80.0)": (157, 0.85, 2),
    "[80.0, inf)": (11, -0.55, 2),
}


def _sha_of_plant_files() -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(PROFILE.glob("fuel_plant*.yaml"))
    }


@pytest.mark.slow
def test_the_held_out_figures_through_the_runner_spending_nothing(
    kit: TwinKit, mv1_runs: dict[str, TwinRun], tmp_path: Path
) -> None:
    """The runner's per-record errors on E07.3's four held-out recordings give
    the figures ``evaluate`` recorded. ``evaluate`` runs only on a scratch
    copy of the profile, and the real plant files are unchanged after."""
    before = _sha_of_plant_files()
    plant = kit.plant
    held = list(plant.split.held_out)
    assert len(held) == 4 and RAM_STUDY in held
    rows, samples = [], []
    for name in held:
        lam = _plant_rows(mv1_runs[name], "delivered_lambda")
        mask = (lam["state"] == "eligible").to_numpy()
        rows.append(lam[mask])
        samples.append(mv1_runs[name].samples[mask])
    lam = pd.concat(rows, ignore_index=True)
    eligible = pd.concat(samples, ignore_index=True)
    err = _errors_of(lam)
    regions = eligible["region_id"].to_numpy()
    pooled = fuel_plant_eval._stats(err, regions, plant)
    maf_band = pd.cut(eligible["maf_gps"], list(fuel_plant_eval.MAF_BANDS), right=False)
    bands = (
        pd.Series(err)
        .groupby(maf_band.astype(str).to_numpy())
        .agg(["size", "median"])
        .to_dict(orient="index")
    )
    # The evaluator's own figures, on a scratch copy: nothing here can spend.
    scratch = tmp_path / "profile"
    shutil.copytree(PROFILE, scratch)
    profile, _ = fuel_plant.load_fuel_plant(scratch, PLANT)
    delay = fuel_plant.load_b06(scratch, profile.delay)
    frames = [build_samples(SESSIONS / n, profile, delay) for n in held]
    result = fuel_plant_eval.evaluate(
        frames, profile, delay, sessions_root=SESSIONS, profile_dir=scratch
    )
    assert pooled == result["lambda"]
    assert bands == result["coverage"]["median_error_by_maf_band_pct"]
    # Per record: the runner's error is the evaluator's, read for read.
    data = pd.concat(frames, ignore_index=True)
    theirs = data[data["state"] == "eligible"].reset_index(drop=True)
    pd.testing.assert_frame_equal(eligible, theirs)
    np.testing.assert_array_equal(err, fuel_plant_eval._errors(theirs, profile, delay))
    # The figures recorded in E07.3.
    assert (pooled["n_samples"], pooled["n_regions"]) == (845, 137)
    assert round(pooled["median_pct"], 2) == 1.69
    assert round(pooled["abs_p90_pct"], 1) == 11.7
    assert round(pooled["region_pass_fraction"], 2) == 0.62
    null = fuel_plant_eval._stats(100.0 * (1.0 / lam["observed"].to_numpy() - 1.0), regions, plant)
    assert round(null["abs_p90_pct"], 2) == 2.33
    per_drive = {
        n: round(float(np.median(err[(eligible["session"] == n).to_numpy()])), 2) for n in held[:3]
    }
    assert sorted(per_drive.values()) == [0.69, 1.79, 4.96]
    for band, (n, median, decimals) in HELD_OUT_BANDS.items():
        assert bands[band]["size"] == n, band
        assert round(bands[band]["median"], decimals) == median, band
    # The 10-04 RAM study contributed nothing, and says why.
    ram = mv1_runs[RAM_STUDY].empty()["delivered_lambda"][LAMBDA]
    assert isinstance(ram, dict) and set(ram) <= {"not_co_sampled", "gated", "outside_model"}
    assert ram.get("not_co_sampled", 0) > 0
    assert _sha_of_plant_files() == before


def _change_summary(change: np.ndarray) -> dict[str, Any]:
    """``compare_versions``' per-sample change summary, restated."""
    return {
        "n": len(change),
        "median": float(np.median(change)) if len(change) else math.nan,
        "p10": float(np.quantile(change, 0.1)) if len(change) else math.nan,
        "p90": float(np.quantile(change, 0.9)) if len(change) else math.nan,
        "fraction_better": float(np.mean(change < 0)) if len(change) else math.nan,
        "fraction_worse": float(np.mean(change > 0)) if len(change) else math.nan,
    }


@pytest.mark.slow
def test_fuel_mv_2_against_fuel_mv_1_per_sample_through_the_runner(
    mv1_runs: dict[str, TwinRun], mv2_runs: dict[str, TwinRun]
) -> None:
    """Both plants' records on the identical pulse reads, scored per sample on
    the reads both call eligible: ``compare_versions``' figures (in-sample and
    the spent fuel-mv-1 held-out drives), which spend nothing."""
    before = _sha_of_plant_files()
    profiles = [fuel_plant.load_fuel_plant(PROFILE, v)[0] for v in (PLANT, MV2)]
    delay = fuel_plant.load_b06(PROFILE, profiles[0].delay)
    names = list(dict.fromkeys(n for p in profiles for n in [*p.split.identification, *p.spent()]))
    frames = [build_samples(SESSIONS / n, profiles[0], delay) for n in names]
    theirs = fuel_plant_eval.compare_versions(frames, profiles, delay)
    errors: dict[str, list[np.ndarray]] = {PLANT: [], MV2: []}
    regions, groups, bands = [], [], []
    refused: dict[str, dict[str, int]] = {PLANT: {}, MV2: {}}
    for name in names:
        one, two = mv1_runs[name], mv2_runs[name]
        pd.testing.assert_frame_equal(one.samples, two.samples)  # one sample source
        lam = {
            PLANT: _plant_rows(one, "delivered_lambda"),
            MV2: _plant_rows(two, "delivered_lambda"),
        }
        np.testing.assert_array_equal(lam[PLANT]["acquired_at"], lam[MV2]["acquired_at"])
        base = (one.samples["state"] == "eligible").to_numpy()
        common = base.copy()
        for version, rows in lam.items():
            state = rows["state"].to_numpy()
            common &= state == "eligible"
            for s in state[base & (state != "eligible")]:
                refused[version][s] = refused[version].get(s, 0) + 1
        for version, rows in lam.items():
            errors[version].append(_errors_of(rows[common]))
        regions.append(one.samples["region_id"].to_numpy()[common])
        label = theirs["sessions"][name]
        groups.append(np.full(int(common.sum()), label))
        maf = pd.cut(one.samples["maf_gps"][common], list(fuel_plant_eval.MAF_BANDS), right=False)
        bands.append(maf.astype(str).to_numpy())
    err = {v: np.concatenate(e) for v, e in errors.items()}
    region, group, band = np.concatenate(regions), np.concatenate(groups), np.concatenate(bands)
    assert len(region) == theirs["common_samples"] == 1490
    assert refused == theirs["refused_by_version"]
    assert refused[MV2] == {"outside_ve_fit": 117, "manifold_above_ambient": 4}
    # Per record: each version's error on the common reads is predict's on
    # compare_versions' own rows.
    data = pd.concat(frames, ignore_index=True)
    common_rows = (data["state"] == "eligible").to_numpy().copy()
    for p in profiles:
        params = fuel_plant.resolve_parameters(p, delay)
        states = fuel_plant_eval.apply_manifold_refusals(data, p, params)["state"].to_numpy()
        common_rows &= states == "eligible"
    rows = data[common_rows].reset_index(drop=True)
    for p in profiles:
        out = fuel_plant.predict(
            fuel_plant_eval.plant_inputs(rows), fuel_plant.resolve_parameters(p, delay), p.geometry
        )
        expected = 100.0 * (out.lambda_delivered / rows["lambda_obs"].to_numpy(dtype=float) - 1.0)
        np.testing.assert_array_equal(err[p.plant_version], expected)
    key = f"abs_error_change_{MV2}_minus_{PLANT}_pct"
    for name in ("in_sample", "spent_held_out"):
        mask = group == name
        ours: dict[str, Any] = {
            "versions": {
                v: fuel_plant_eval._stats(err[v][mask], region[mask], profiles[0]) for v in err
            }
        }
        change = np.abs(err[MV2][mask]) - np.abs(err[PLANT][mask])
        ours[key] = _change_summary(change)
        ours[f"{key}_by_maf_band"] = {
            str(b): _change_summary(change[band[mask] == b]) for b in sorted(set(band[mask]))
        }
        assert ours == theirs[name], name
    # The figures recorded in E07.3 (fuel-mv-2, 2026-10-08).
    recorded = {
        "in_sample": (764, 89, (12.01, 11.61), (0.573, 0.618)),
        "spent_held_out": (726, 101, (11.74, 12.21), (0.564, 0.554)),
    }
    for name, (n, n_regions, p90, region_pass) in recorded.items():
        versions = theirs[name]["versions"]
        assert versions[PLANT]["n_samples"] == versions[MV2]["n_samples"] == n
        assert versions[PLANT]["n_regions"] == n_regions
        assert tuple(round(versions[v]["abs_p90_pct"], 2) for v in (PLANT, MV2)) == p90
        assert (
            tuple(round(versions[v]["region_pass_fraction"], 3) for v in (PLANT, MV2))
            == region_pass
        )
    assert _sha_of_plant_files() == before
