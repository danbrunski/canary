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
  control without the pressure correction (median 0.936).

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
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis import (
    air_charge_twin,
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
from src.analysis.target_phi import evaluate_session, x_formula_check
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
from src.decode.target_phi_command import GATE_STATE_UNKNOWN
from tests.test_target_phi import EXPECT, _session, _table, fixture_frame, record_lines

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
X_SPEC = "mem:ECM_FUEL_TARGET_MAP_X_TRACE"


def _same(ours: float, theirs: float | int | None) -> bool:
    """A runner value (NaN for none) against the evaluator's (``None``)."""
    return math.isnan(ours) if theirs is None else ours == float(theirs)


#: E07.3's six tuning captures (fuel_plant.yaml split, identification + held out).
PULSE_DRIVES = BFS_DRIVES[1:]


@pytest.fixture(scope="module")
def kit() -> TwinKit:
    return load_twin_kit(PROFILE)


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
    for call in (
        lambda: _bfs_kind("a_new_gate", {}),
        lambda: _phi_kind("unevaluable", ["a_new_reason"], identity_captured=True),
        lambda: _pulse_kind("a_new_check", {}, load_twin_kit(PROFILE).plant),
    ):
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


#: The reason expressions target_phi emits that are not string literals,
#: each with every value it can take (verifier, 2026-10-16: three were missed).
_PHI_VARIABLE_REASONS = {
    "names[0]": None,  # an excluded fuel-system mode name: classified by state
    "prediction.reason": [GATE_STATE_UNKNOWN],
    "resolution.reason": "resolve_slot",
    "identity_reason": None,  # the _identity tuple, enumerated from its returns
    "(state, reason)": None,  # the gates' list, enumerated from _gates' appends
}


def _target_phi_reasons() -> list[tuple[str, str]]:
    """Every ``(state, reason)`` target_phi can put on a record, read from its
    source: literal tuples, f-string templates (with a placeholder) and the
    slot resolver's reasons. A reason expression of another shape fails here."""
    source = inspect.getsource(target_phi)
    found: list[tuple[str, str]] = []
    pattern = re.compile(r'\("(unevaluable|excluded|pending)", ([^)]*)\)')
    for state, expression in pattern.findall(source):
        literal = re.fullmatch(r'"([a-z_:]+)"', expression)
        template = re.fullmatch(r'f"([a-z_]+:)\{[^}]+\}"', expression)
        if literal:
            found.append((state, literal.group(1)))
        elif template:
            found.append((state, template.group(1) + "x"))
        else:
            assert expression in _PHI_VARIABLE_REASONS, f"unenumerated reason {expression}"
            values = _PHI_VARIABLE_REASONS[expression]
            if values == "resolve_slot":
                slot_source = inspect.getsource(target_phi.resolve_slot)
                reasons = re.findall(r'SlotResolution\(\s*None,\s*"([a-z_]+)"', slot_source)
                assert reasons, "resolve_slot names no reason"
                found += [(state, r) for r in reasons]
            elif isinstance(values, list):
                found += [(state, v) for v in values]
    return found


def test_every_reason_target_phi_can_emit_classifies() -> None:
    reasons = _target_phi_reasons()
    names = {r for _, r in reasons}
    # The enumeration reaches the three the first runner missed, and the rest.
    assert {"map_uninitialised", "slot_ambiguous", "map_switching"} <= names
    assert {"sentinel", "map_identity_unbracketed", "input_stale:x", "awaiting_input:x"} <= names
    assert len(reasons) >= 25
    for state, reason in reasons:
        for captured in (True, False):
            assert _phi_kind(state, [reason], identity_captured=captured) in EMPTY_KINDS
    assert _phi_kind("unevaluable", ["map_uninitialised"], identity_captured=True) == (
        "outside_model"
    )


def test_an_uninitialised_map_magic_is_counted_not_refused(kit: TwinKit) -> None:
    """The Oct 4 fixture with every UpRev magic read zeroed: resolve_slot
    names ``map_uninitialised``; the runner counts it and keeps every stage."""
    frame = fixture_frame()
    magic = frame["channel"] == "mem:ECM_UPREV_MAGIC"
    assert magic.any()
    frame.loc[magic, "value_number"] = 0.0
    run = run_twin(frame, kit, _committed_ecu(kit), name=RAM_STUDY)
    states = run.accounting()["target_equiv_ratio"][REFERENCE]
    assert sum(states.values()) == 174
    assert states.get("map_uninitialised", 0) > 0
    assert run.accounting()["load_input_x"][X_SPEC] == {"eligible": 194}


def test_a_session_without_identity_reads_names_them_not_captured(kit: TwinKit) -> None:
    frame = _session()
    identity = set(kit.twin.map_identity.channels())
    stripped = frame[~frame["channel"].isin(identity)]
    run = run_twin(stripped, kit, _committed_ecu(kit), name="synthetic")
    assert run.accounting()["target_equiv_ratio"][REFERENCE] == {"map_identity_unbracketed": 1}
    assert run.empty()["target_equiv_ratio"][REFERENCE] == {"input_not_captured": 1}
    # With them, the same sample is eligible.
    whole = run_twin(frame, kit, _committed_ecu(kit), name="synthetic")
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
    for row, record in zip(rows.itertuples(), phi["records"], strict=True):
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
    run = run_twin(frame, kit, ecu, name=RAM_STUDY)
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


def test_the_x_relation_through_the_runner_and_the_evaluator(kit: TwinKit) -> None:
    frame = fixture_frame()
    run = run_twin(frame, kit, _committed_ecu(kit), name=RAM_STUDY)
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
        (target_phi._iso(t), o, x)
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
    run = run_twin(trimmed, kit, _committed_ecu(kit), name=RAM_STUDY)
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

    def both() -> tuple[float, float]:
        run = run_twin(frame, kit, ecu, name="synthetic")
        (row,) = run.eligible("target_equiv_ratio").itertuples()
        old = evaluate_session(frame, kit.twin, ecu.table, definition_sha256=kit.twin_sha256)
        (record,) = old["records"]
        return float(row.residual), float(record["difference_words"])

    assert both() == (0.0, 0.0)
    real = target_phi.target_command

    def off_by_one(*args: Any, **kwargs: Any) -> Any:
        command = real(*args, **kwargs)
        return dataclasses.replace(command, word=command.word + 1)

    monkeypatch.setattr(target_phi, "target_command", off_by_one)
    assert both() == (-1.0, -1.0)


def test_a_mutated_x_kernel_moves_both_front_ends(
    kit: TwinKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = fixture_frame()
    scaler = EXPECT["calibration"]["load_point_scaler"]
    real = target_phi._x_from_bfs
    monkeypatch.setattr(target_phi, "_x_from_bfs", lambda bfs, sc, **kw: real(bfs + 400, sc, **kw))
    run = run_twin(frame, kit, _committed_ecu(kit), name=RAM_STUDY)
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
        kit = load_twin_kit(Path({str(PROFILE)!r}))
        run = run_twin(fixture_frame(), kit, ResolvedEcu(_table(kit.twin), None, None),
                       name={RAM_STUDY!r})
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
    run = run_twin(fixture_frame(), kit, _committed_ecu(kit), name=RAM_STUDY)
    assert set(run.records["kind"]) <= {"eligible", *EMPTY_KINDS}


# -- E02 from the image (private) -----------------------------------------------------------


def test_the_october_4_accounting_from_the_image(kit: TwinKit, image_ecu: ResolvedEcu) -> None:
    frame = fixture_frame()
    run = run_twin(frame, kit, image_ecu, name=RAM_STUDY)
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
    old = air_charge_twin.evaluate_steady_bfs(
        frame,
        image_ecu.air,
        bfs_channel="mem:ECM_AIR_CHARGE_SLOT_103A",
        maf_channel="MAF",
        load_channel="mem:ECM_FUEL_COMP_X_INPUT",
    )
    assert run.native["base_fuel_schedule_word"] == {"mem:ECM_AIR_CHARGE_SLOT_103A": old}
    assert old.excluded == {"maf_unpaired": 194}


# -- E05.2: steady BFS (private) ------------------------------------------------------------


def _raw_rows(session: Path) -> pd.DataFrame:
    """The rows E05.2's own test hands the evaluator (``_read`` there)."""
    from tests.test_air_charge_private_rom import _read

    return _read([str(session / "telemetry-*.parquet")])


@pytest.fixture(scope="module")
def drive_runs(kit: TwinKit, image_ecu: ResolvedEcu) -> dict[str, TwinRun]:
    return {path.name: run_twin(path, kit, image_ecu) for path in _sessions(BFS_DRIVES)}


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
    run = run_twin(session, kit, dataclasses.replace(image_ecu, table=None, pulse=None))
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
    run = run_twin(PULSE_SLICE, kit, dataclasses.replace(image_ecu, table=None, air=None))
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
    run = run_twin(PULSE_SLICE, kit, dataclasses.replace(image_ecu, table=None, air=None))
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
        negative = run_twin(session, kit, control_ecu)
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
    from src.preprocessing.loader import read_session

    plant = load_twin_kit(PROFILE).plant
    delay = load_twin_kit(PROFILE).delay
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
