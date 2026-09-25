"""Sustained end-to-end validation harness (roadmap task C10).

These ``slow`` tests drive whole scenarios on the **real clock**, so a host
that is otherwise busy can starve them into a false failure (observed
2026-09-08: a contended ``-n auto`` run took 2.4x as long and failed the
storage-overload case, which passed serially and in the next uncontended full
run). Re-run a failure with ``-n0`` on an idle host before treating it as a
regression.

The fast tests run a ~1 s synthetic-bus scenario through the *real*
collector thread, fanout, Parquet writer and finalize path and assert the
accounting identities that every report must satisfy (C10.14/C10.15). The
``slow`` tests run whole scenarios — the fuel-map tuple/known-answer case,
the two deliberate overloads that must fail *by name*, the inference
cadence contract (C10.16) and the drive-slice replay (C10.9).

Every run here goes through ``run_scenario_clean``: the WSL2 workstation's
wall clock steps by ~0.9 s every couple of minutes (measured while writing
this), and a step inside a run re-orders every wall-stamped row across it.
Under load it steps far more often (+2.93 s every 32.25 s beside one busy
loop per core, 2026-09-24), often enough for all three attempts to see one,
so every test here runs on ``step_free_wall_clock`` (T01.9) and the step
handling itself is tested by injecting a step.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.validation import harness
from src.validation.evidence import limiting_stage, sequence_reconciliation
from src.validation.harness import (
    BoundedSamples,
    RunArtifacts,
    harness_config,
    run_scenario_clean,
)
from src.validation.report import build_report
from src.validation.scenarios import (
    SCENARIOS,
    ScenarioSpec,
    build_profile,
    build_spec_kwargs,
    scenario,
)
from src.validation.synthetic_bus import (
    SYNTH_BROADCAST_ID,
    BroadcastLedger,
    BroadcastPlan,
    RealClock,
    SyntheticEcu,
)
from tests.waits import settle, wait_until

pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
    pytest.mark.usefixtures("step_free_wall_clock"),
]


def _mini(spec: ScenarioSpec, **overrides: Any) -> ScenarioSpec:
    """A short copy of a table scenario for the inner loop."""
    fields: dict[str, Any] = {"duration_s": 1.0, "warmup_s": 0.25}
    fields.update(overrides)
    return dataclasses.replace(spec, **fields)


async def _run(spec: ScenarioSpec, tmp_path: Path, *, inference: bool) -> RunArtifacts:
    config = harness_config(tmp_path / "scratch", models_enabled=False)
    return await run_scenario_clean(
        spec,
        label="test",
        sessions_root=tmp_path / "sessions",
        scratch=tmp_path / "scratch",
        config=config,
        enable_inference=inference,
    )


# ---------------------------------------------------------------------------
# fast: accounting identities and report shape
# ---------------------------------------------------------------------------


async def test_sequence_accounting_reconciles_exactly(tmp_path: Path) -> None:
    """C10.15 — every adapter-delivered sequence number is on disk exactly once,
    the push identity holds at every stage, and decoded rows are reconciled as
    an expansion, not as raw input."""
    artifacts = await _run(_mini(scenario("fuel-map")), tmp_path, inference=False)
    report = build_report(artifacts)
    evidence = report["evidence"]
    seq = evidence["sequence_reconciliation"]
    assert seq["status"] == "reconciled", seq
    assert seq["missing_count"] == 0 and seq["duplicated_count"] == 0 and seq["foreign_count"] == 0
    assert seq["delivered_distinct"] > 50
    losses = evidence["losses"]
    assert losses["identity_holds"] is True, losses
    assert losses["frames_tapped_identity"]["holds"] is True, losses
    assert losses["storage_dropped"] == 0
    decoded = evidence["decoded_expansion"]
    assert decoded["identity_holds"] is True, decoded
    assert decoded["decoded_rows"] == 2 * decoded["persisted_broadcast_frames"]
    assert evidence["rows"]["by_source"]["obd"] > 0


async def test_report_carries_versioned_provenance(tmp_path: Path) -> None:
    """C10.3 — the artifact names its revision, runtime, adapter, vehicle,
    profile, models, workload, warmup, duration and timestamp basis."""
    artifacts = await _run(_mini(scenario("fuel-map")), tmp_path, inference=False)
    report = build_report(artifacts)
    assert report["schema_version"] == 1
    assert report["label"] == "test" and report["git_rev"]
    prov = report["provenance"]
    for key in (
        "git",
        "runtime",
        "hardware",
        "vehicle",
        "profile_snapshot",
        "models",
        "components",
        "power_thermal",
        "workload",
        "warmup_s",
        "duration_s",
        "timestamps",
    ):
        assert key in prov, key
    assert prov["hardware"]["adapter"] == "synthetic"
    assert prov["runtime"]["dependencies"]["pyarrow"]
    assert prov["components"]["kernel_counters"].startswith("unavailable")
    assert prov["profile_snapshot"]["name"] == "fuel-map"
    assert report["headline"]["limiting_stage"] is not None
    # The capture went through the real finalize path and reopened Parquet.
    quality = report["evidence"]["quality"]
    assert quality["stop_sidecar"]["scope"] == "stop"
    assert quality["stop_sidecar"]["persistence"]["flushed"] is True
    assert (artifacts.capture_dir / "capture_quality.json").exists()  # type: ignore[operator]


async def test_heartbeat_records_an_injected_wall_clock_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T01.9 — the step detector the harness reruns on, fed a step on purpose.

    ``step_free_wall_clock`` keeps the host's steps out of every run in this
    module, so this is where a step still has to be seen: +0.5 s on the wall
    clock and nothing on the monotonic one is recorded once, with its size.
    """
    step_free = time.time
    jump = [0.0]
    monkeypatch.setattr(time, "time", lambda: step_free() + jump[0])
    stop = asyncio.Event()
    steps: list[dict[str, float]] = []
    samples = BoundedSamples()
    task = asyncio.create_task(harness._heartbeat(stop, samples, steps))
    await wait_until(lambda: samples.count >= 3, what="three heartbeats before the step")
    assert steps == []
    jump[0] = 0.5
    await wait_until(lambda: bool(steps), what="the heartbeat seeing the step")
    stop.set()
    await settle(task)
    assert len(steps) == 1 and abs(steps[0]["step_s"] - 0.5) < 0.01, steps


async def test_a_run_that_saw_a_wall_clock_step_is_rerun_then_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """T01.9 — ``run_scenario_clean`` reruns a stepped run, stops at the first
    clean one, and after ``attempts`` stepped runs returns the last one, which
    still says it stepped (``build_report`` then marks it invalid)."""
    plan: list[list[dict[str, float]]] = [[{"step_s": 2.93}], [], [{"step_s": 2.93}]]
    calls: list[int] = []

    class _Run:
        def __init__(self, steps: list[dict[str, float]]) -> None:
            self.wall_clock_steps = steps
            self.attempts_used = 0

        @property
        def clock_stepped(self) -> bool:
            return bool(self.wall_clock_steps)

    async def _fake_run(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(1)
        return _Run(plan[len(calls) - 1])

    monkeypatch.setattr(harness, "run_scenario", _fake_run)
    kwargs: dict[str, Any] = {"label": "t", "sessions_root": tmp_path, "scratch": tmp_path}
    clean = await run_scenario_clean(scenario("fuel-map"), **kwargs)
    assert len(calls) == 2 and clean.attempts_used == 2 and not clean.clock_stepped

    calls.clear()
    plan[:] = [[{"step_s": 2.93}]] * 3
    stepped = await run_scenario_clean(scenario("fuel-map"), **kwargs)
    assert len(calls) == 3 and stepped.attempts_used == 3 and stepped.clock_stepped


def _frame_with_sequences(seqs: list[int]) -> pd.DataFrame:
    channel = f"0x{SYNTH_BROADCAST_ID:X}"
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-09-07", periods=len(seqs), freq="10ms", tz="UTC"),
            "source": ["can"] * len(seqs),
            "channel": [channel] * len(seqs),
            "payload": [s.to_bytes(4, "big") + bytes(4) for s in seqs],
            "session_dir": ["x"] * len(seqs),
        }
    )


def _artifacts_with_ledger(delivered: list[int]) -> Any:
    ledger = BroadcastLedger(origin_monotonic=0.0, next_seq=max(delivered) + 1)
    ledger.generated = max(delivered)
    ledger.delivered = list(delivered)

    class _Stub:
        scenario = scenario("fuel-map")

    stub = _Stub()
    stub.ledger = ledger  # type: ignore[attr-defined]
    return stub


def test_injected_loss_is_named_never_hidden() -> None:
    """C10.15 — a delivered sequence absent from disk is *missing*; it is only
    reconciled when the daemon counted a drop for it, and a duplicate or a
    foreign sequence is a mismatch, never a pass."""
    delivered = list(range(1, 11))
    complete = sequence_reconciliation(
        _frame_with_sequences(delivered), _artifacts_with_ledger(delivered)
    )
    assert complete["status"] == "reconciled" and complete["present_exactly_once"] == 10

    lossy = sequence_reconciliation(
        _frame_with_sequences([1, 2, 3, 4, 5, 6, 7, 8]), _artifacts_with_ledger(delivered)
    )
    assert lossy["status"] == "UNEXPLAINED_LOSS" and lossy["missing"] == [9, 10]
    counted = sequence_reconciliation(
        _frame_with_sequences([1, 2, 3, 4, 5, 6, 7, 8]),
        _artifacts_with_ledger(delivered),
        drops_total=2,
    )
    assert counted["status"] == "reconciled_with_counted_loss"
    assert counted["identity_holds"] is True

    duplicated = sequence_reconciliation(
        _frame_with_sequences([*delivered, 5]), _artifacts_with_ledger(delivered)
    )
    assert duplicated["status"] == "MISMATCH" and duplicated["duplicated"] == [5]


def test_limiting_stage_names_loss_before_headroom() -> None:
    """C10.8 — the limiting stage is the first stage that lost data, and only
    a run that lost nothing reports headroom."""

    class _A:
        scheduler_metrics = {"bus_budget_requests_per_second": 20.0, "requests_sent": 100}
        finalize: dict[str, Any] = {"persistence": {"drain_timed_out": []}}
        writer_stalls = 0
        started_wall = 0.0
        ended_wall = 10.0

    stalled = limiting_stage(
        _A(), {"storage_dropped": 5, "buffer_dropped": 0, "queue_dropped": 0}, {}, {}
    )  # type: ignore[arg-type]
    assert stalled["stage"] == "storage_sink"
    handoff = limiting_stage(
        _A(), {"storage_dropped": 0, "buffer_dropped": 3, "queue_dropped": 0}, {}, {}
    )  # type: ignore[arg-type]
    assert handoff["stage"] == "thread_handoff"
    clean = limiting_stage(  # type: ignore[arg-type]
        _A(),
        {"storage_dropped": 0, "buffer_dropped": 0, "queue_dropped": 0},
        {"failures": []},
        {"event_loop_heartbeat_overshoot": {"p99_ms": 1.0}},
    )
    assert clean["stage"] == "none_reached" and clean["data_loss"] is False
    short = limiting_stage(  # type: ignore[arg-type]
        _A(),
        {"storage_dropped": 0, "buffer_dropped": 0, "queue_dropped": 0},
        {"failures": ["rate_shortfall"]},
        {},
    )
    assert short["stage"] in ("bus_budget_request_pacing", "ecu_response_time")


def test_profiles_and_specs_filter_to_the_trees_schema() -> None:
    """C10.8 — the same workload runs on a tree that lacks a field; the field
    is dropped and *reported*, never silently accepted or refused."""
    profile, dropped = build_profile({**scenario("tuning-did").profile, "not_a_field": 1})
    assert profile.name == "tuning-did" and dropped == ["not_a_field"]
    kwargs, dropped_spec = build_spec_kwargs({"poll_rate_hz": 2.0, "future_knob": True})
    assert kwargs == {"poll_rate_hz": 2.0} and dropped_spec == ["future_knob"]


def test_scenario_table_covers_the_task_rows() -> None:
    rows = {spec.table_row.split(" (")[0] for spec in SCENARIOS}
    for required in (
        "Normal monitoring with discovery",
        "Fuel-map",
        "Continuous tuning DID",
        "Fuel DID research",
        "Hybrid Mode 01/22",
        "Passive / legacy ELM327 companion",
        "Fault and transition cases",
        "Overload",
        "Replay",
    ):
        assert required in rows, required
    assert scenario("overload-storage-stall").expected_failures == ("persistence_loss",)
    # Starvation has two legitimate names and the overload budget sits on the
    # boundary the C10.12 sweep measured, so the scenario declares both and
    # requires one — not one name, and not both.
    assert scenario("overload-rate").expected_failures == ()
    assert scenario("overload-rate").expected_failures_any == (
        "rate_shortfall",
        "unanswered_input",
    )
    assert all(spec.duration_s <= 60.0 for spec in SCENARIOS)


def test_synthetic_bus_serves_one_signal_on_every_service() -> None:
    """The known-answer input: the broadcast bytes, PID 0x0C and DID 0x1219
    all encode the same rpm at the same instant."""
    import can

    ledger = BroadcastLedger(origin_monotonic=0.0)
    bus = SyntheticEcu(RealClock(), ledger=ledger, broadcast=BroadcastPlan(rate_hz=0.0))
    bus.send(can.Message(arbitration_id=0x7DF, data=[0x02, 0x01, 0x0C, 0, 0, 0, 0, 0]))
    mode01 = bus.recv(0.05)
    assert mode01 is not None
    rpm_mode01 = int.from_bytes(bytes(mode01.data)[3:5], "big") / 4.0
    bus.send(can.Message(arbitration_id=0x7E0, data=[0x03, 0x22, 0x12, 0x19, 0, 0, 0, 0]))
    did = bus.recv(0.05)
    assert did is not None and bytes(did.data)[1:4] == b"\x62\x12\x19"
    rpm_did = int.from_bytes(bytes(did.data)[4:6], "big")
    # ~5 ms of ECU latency between the two sends; rpm slews <= 471 rpm/s.
    assert abs(rpm_mode01 - rpm_did) <= 10.0
    assert bus.sent and not bus.faulted


# ---------------------------------------------------------------------------
# slow: whole scenarios
# ---------------------------------------------------------------------------


@pytest.mark.slow
async def test_fuel_map_persists_tuples_and_known_answers_hold(tmp_path: Path) -> None:
    """C10.7/C10.16 — through the real writer and back: complete tuples are
    counted from the rows, the same rpm on a broadcast byte and a Mode 01 PID
    recovers slope ~1 with a tight 1:1 residual, and a deliberately wrong
    scale is *not* tautologically accepted."""
    artifacts = await _run(scenario("fuel-map"), tmp_path, inference=True)
    # The host clock stepped under load often enough to spoil all three
    # attempts (T01.9); on ``step_free_wall_clock`` the first one is clean.
    assert artifacts.attempts_used == 1, artifacts.wall_clock_steps
    report = build_report(artifacts)
    evidence = report["evidence"]
    assert evidence["valid"] is True
    tuples = evidence["quality"]["stop_sidecar"]["tuples"]
    assert tuples["source"] == "persisted_rows" and tuples["complete"] > 10, tuples
    outcomes = {f"{k['a']} ~ {k['b']}": k for k in evidence["known_answer"]}
    rpm = outcomes["can:SYNTH_RPM ~ obd:RPM"]
    assert rpm["outcome"] == "agrees_1to1", rpm
    assert abs(rpm["slope"] - 1.0) < 0.02 and abs(rpm["difference_bias"]) < 15.0
    wrong = outcomes["can:SYNTH_RPM_HALF ~ obd:RPM"]
    assert wrong["outcome"] == "differs_as_expected", wrong
    assert evidence["sequence_reconciliation"]["status"] == "reconciled"
    assert evidence["losses"]["identity_holds"] is True
    inference = evidence["timing"]["inference"]
    assert inference["ticks_scheduled"] >= 8
    assert (
        inference["ticks_completed"] + inference["missed_ticks"] >= inference["ticks_scheduled"] - 1
    )


@pytest.mark.slow
async def test_overloaded_storage_yields_persistence_loss_never_pass(tmp_path: Path) -> None:
    """C10.14/C10.15 — a wedged writer under a flooded bus loses records; the
    loss is counted where it happened, the push identity still holds, and
    the verdict names ``persistence_loss``."""
    artifacts = await _run(scenario("overload-storage-stall"), tmp_path, inference=False)
    report = build_report(artifacts)
    evidence = report["evidence"]
    losses = evidence["losses"]
    assert losses["storage_dropped"] > 0, losses
    assert losses["identity_holds"] is True, losses
    seq = evidence["sequence_reconciliation"]
    assert seq["status"] == "reconciled_with_counted_loss", seq
    assert 0 < seq["missing_count"] <= seq["counted_drops"]
    quality = evidence["quality"]
    assert quality["verdict"] != "pass"
    assert "persistence_loss" in quality["failures"], quality["failures"]
    assert evidence["limiting_stage"]["stage"] == "storage_sink"
    assert report["headline"]["expected_failures_met"] is True
    # The queue that dropped the records must say how deep it got: the last
    # depth is 0 once the stall clears, so only the high-water mark shows
    # the saturation that explains the loss (C10.5).
    storage_queue = losses["queue_high_water"]["storage"]
    assert storage_queue["queue_high_water"] > 0, storage_queue
    assert storage_queue["queue_high_water"] <= storage_queue["queue_max_size"], storage_queue


#: Starvation has two legitimate names depending on how deep it is: a channel
#: that answers too slowly yields ``rate_shortfall``, one that never reaches
#: the wire at all yields ``unanswered_input``. The C10.12 sweep measured the
#: boundary — ``fuel-map`` reports ``unanswered_input`` at 4 req/s and
#: ``rate_shortfall`` at 6 — and the overload scenario's 4 req/s budget sits
#: on it, so asserting one name made the test depend on host timing. What is
#: invariant, and what the item actually asks for, is that starvation is
#: named and attributed to pacing rather than to the ECU.
_STARVATION_FAILURES = ("rate_shortfall", "unanswered_input")


@pytest.mark.slow
async def test_rate_overload_names_starvation_and_blames_pacing(tmp_path: Path) -> None:
    """C10.14 — sixteen channels at 4 Hz on a 4 req/s budget with batching
    off cannot meet the contract; the verdict says so by name, the constraint
    is pacing (never the ECU), and nothing is silently dropped to make the
    numbers look better."""
    artifacts = await _run(scenario("overload-rate"), tmp_path, inference=False)
    report = build_report(artifacts)
    evidence = report["evidence"]
    quality = evidence["quality"]
    assert quality["verdict"] == "fail"
    named = [name for name in quality["failures"] if name in _STARVATION_FAILURES]
    assert named, quality["failures"]
    assert report["headline"]["expected_failures_met"] is True
    assert evidence["losses"]["storage_dropped"] == 0
    assert evidence["sequence_reconciliation"]["status"] == "reconciled"
    assert evidence["limiting_stage"]["stage"] == "bus_budget_request_pacing"


@pytest.mark.slow
async def test_inference_keeps_cadence_or_reports_missed_deadlines(tmp_path: Path) -> None:
    """C10.16 — at a 4 Hz model cadence under the fuel-map load every
    scheduled tick is either completed or reported missed, lateness is
    recorded per tick, and the feature stage's loop occupancy is measured."""
    spec = dataclasses.replace(scenario("fuel-map"), duration_s=5.0, inference_tick_s=0.25)
    artifacts = await _run(spec, tmp_path, inference=True)
    report = build_report(artifacts)
    inference = report["evidence"]["timing"]["inference"]
    assert inference["interval_seconds"] == 0.25
    assert inference["ticks_scheduled"] >= 15
    assert inference["lateness"]["count"] == inference["ticks_scheduled"]
    assert inference["missed_ticks"] == sum(inference["missed_by_reason"].values())
    assert inference["stages"]["feature"]["count"] >= 15
    assert report["evidence"]["timing"]["event_loop_heartbeat_overshoot"]["count"] > 100


@pytest.mark.slow
async def test_replay_of_the_drive_slice_reproduces_the_known_answer(tmp_path: Path) -> None:
    """C10.9 — the committed drive slice through the isolated replay path
    (queue -> fanout -> writer -> live source -> detectors), then the C01
    ground-truth pair re-read from the *replayed* Parquet."""
    # 30 s at 2x = 60 s of slice: obd:RPM is ~0.57 Hz there, so ~34 pairs (>= MIN_PAIRS 30).
    spec = dataclasses.replace(scenario("replay-drive-slice"), duration_s=30.0)
    artifacts = await _run(spec, tmp_path, inference=True)
    report = build_report(artifacts)
    evidence = report["evidence"]
    assert artifacts.session_dirs and artifacts.session_dirs[-1].name.startswith("_replay-")
    assert evidence["rows"]["by_source"]["can"] > 1000
    assert evidence["rows"]["by_source"]["obd"] > 20
    pairs = {f"{k['a']} ~ {k['b']}": k for k in evidence["known_answer"]}
    rpm = pairs["can:ECM_RPM ~ obd:RPM"]
    assert rpm["outcome"] == "agrees_1to1", rpm
    inference = evidence["timing"]["inference"]
    assert inference["ticks_completed"] >= 20
    assert report["provenance"]["hardware"]["adapter"] == "replay (none)"


def test_models_dir_override_is_profile_scoped_like_the_daemon(tmp_path: Path) -> None:
    """C08.1 — only manifests are committed under ``models/``, so a workstation
    run installs the declared IF stub. ``--models-dir`` points the harness at
    real artifacts, and must be scoped by vehicle profile exactly as the
    daemon scopes its own ``models_dir`` — otherwise the override silently
    looks one directory above the artifact and the stub comes back."""
    from src.validation.harness import harness_config

    default = harness_config(tmp_path / "a")
    overridden = harness_config(tmp_path / "b", models_dir=tmp_path / "artifacts")

    assert overridden.daemon.models_dir != default.daemon.models_dir
    assert overridden.daemon.models_dir.is_relative_to(tmp_path / "artifacts")
    # The profile slug the daemon scopes with is appended, not dropped.
    assert overridden.daemon.models_dir.name == default.daemon.models_dir.name


def _sweep_report(*, failures: list[str], limiting: str, cadence_worst: float) -> dict[str, Any]:
    return {
        "headline": {
            "quality_failures": failures,
            "quality_verdict": "fail" if failures else "pass",
            "quality_verdict_with_adapter_ledger": "fail" if failures else "pass",
            "limiting_stage": limiting,
            "tuples_complete": 40,
            "tuples_attempted": 40,
            "tuple_skew_max_s": 0.0,
            "storage_dropped": 0,
            "sequence_missing": 0,
            "loss_identity_holds": True,
            "batch_occupancy": None,
        },
        "evidence": {
            "limiting_stage": {"stage": limiting, "evidence": {}},
            "transport": {
                "channel_cadence": [
                    {
                        "channel": "0X6A0",
                        "kind": "broadcast",
                        "requested_hz": 100,
                        "achieved_hz": 1,
                    },
                    {
                        "channel": "RPM",
                        "kind": "pid",
                        "requested_hz": 4.0,
                        "achieved_hz": 4.0 * cadence_worst,
                    },
                ]
            },
        },
    }


def test_budget_sweep_changes_only_the_budget_between_rungs() -> None:
    """C10.12 — a ladder whose rungs differ in more than one input measures
    nothing, so ``spec_at_budget`` must carry every other field through."""
    from dataclasses import asdict

    from src.validation.sweep import spec_at_budget

    base = scenario("fuel-map")
    raised = spec_at_budget(base, 32.0)

    assert raised.profile["bus_budget_override"] == 32.0
    assert base.profile["bus_budget_override"] != 32.0, "the base spec was mutated"
    before, after = asdict(base), asdict(raised)
    before_profile = before.pop("profile")
    after_profile = after.pop("profile")
    assert before == after, "a field other than the profile changed between rungs"
    assert {
        key: value for key, value in before_profile.items() if key != "bus_budget_override"
    } == {key: value for key, value in after_profile.items() if key != "bus_budget_override"}


async def test_budget_sweep_reports_a_ceiling_only_above_a_rung_that_held(
    monkeypatch: Any,
) -> None:
    """A failure below the workload's demand is a floor, not a ceiling. Only a
    failure that appears *after* the contract has already held at a lower
    budget is the thing C10.12 is looking for."""
    from src.validation import sweep as sweep_mod

    unknown = ["unknown_loss_counters"]
    reports = {
        # 8 is starved (below demand) — a floor, not the ceiling.
        8.0: _sweep_report(
            failures=[*unknown, "rate_shortfall"],
            limiting="bus_budget_request_pacing",
            cadence_worst=0.4,
        ),
        12.0: _sweep_report(failures=unknown, limiting="none_reached", cadence_worst=1.0),
        16.0: _sweep_report(failures=unknown, limiting="none_reached", cadence_worst=1.0),
        # 20 breaks again, this time after the contract held — a real ceiling.
        20.0: _sweep_report(
            failures=[*unknown, "persistence_loss"],
            limiting="storage_sink",
            cadence_worst=1.0,
        ),
        24.0: _sweep_report(failures=unknown, limiting="none_reached", cadence_worst=1.0),
    }
    seen: list[float] = []

    async def fake_run(spec: Any, **kwargs: Any) -> Any:
        seen.append(float(spec.profile["bus_budget_override"]))
        return spec

    monkeypatch.setattr(sweep_mod, "run_scenario_clean", fake_run)
    monkeypatch.setattr(
        sweep_mod, "build_report", lambda spec: reports[float(spec.profile["bus_budget_override"])]
    )

    result = await sweep_mod.run_budget_sweep(
        scenario("fuel-map"),
        [8.0, 12.0, 16.0, 20.0, 24.0],
        label="t",
        sessions_root=Path("unused"),
        scratch=Path("unused"),
    )

    # 24 runs first as the max-headroom baseline and is never run twice.
    assert seen == [24.0, 8.0, 12.0, 16.0, 20.0]
    assert result.baseline_budget_req_s == 24.0
    assert result.baseline_failures == []
    assert result.lowest_holding == 12.0, "the starved 8 req/s rung must not count as holding"
    assert result.ceiling_at == 20.0, "the ceiling is the failure above a rung that held"
    assert result.constraint == "storage_sink"
    assert result.constraint_failures == ["persistence_loss"]
    text = sweep_mod.summarize_sweep(result)
    assert "storage_sink" in text
    assert "No queue was widened and no threshold relaxed" in text


async def test_budget_sweep_never_blames_the_budget_for_a_baseline_failure(
    monkeypatch: Any,
) -> None:
    """The real fuel-map sweep stopped at its opening rung because
    ``acquisition_gap`` fires at *every* budget — a capture-start artefact
    (members keep the previous profile's due time across activation), nothing
    to do with pacing. Reporting "8 req/s is the ceiling" about that is the
    shape of every retracted finding in this project. A failure the lowest
    rung already has is baseline, and the ladder keeps climbing."""
    from src.validation import sweep as sweep_mod

    # acquisition_gap fires at every budget (capture-start artefact);
    # rate_shortfall only where the budget is genuinely below demand.
    gap = ["acquisition_gap", "acquisition_gap", "unknown_loss_counters"]
    reports = {
        8.0: _sweep_report(
            failures=[*gap, "rate_shortfall"],
            limiting="bus_budget_request_pacing",
            cadence_worst=0.4,
        ),
        12.0: _sweep_report(failures=gap, limiting="feature_stage", cadence_worst=1.0),
        16.0: _sweep_report(failures=gap, limiting="feature_stage", cadence_worst=1.0),
    }

    async def fake_run(spec: Any, **kwargs: Any) -> Any:
        return spec

    monkeypatch.setattr(sweep_mod, "run_scenario_clean", fake_run)
    monkeypatch.setattr(
        sweep_mod, "build_report", lambda spec: reports[float(spec.profile["bus_budget_override"])]
    )

    result = await sweep_mod.run_budget_sweep(
        scenario("fuel-map"),
        [8.0, 12.0, 16.0],
        label="t",
        sessions_root=Path("unused"),
        scratch=Path("unused"),
    )

    assert result.baseline_failures == ["acquisition_gap", "acquisition_gap"]
    assert result.baseline_budget_req_s == 16.0, "the baseline must be the most headroom"
    # 8 is starved; 12 and 16 hold *despite* carrying the same two
    # acquisition_gaps, because those are baseline and not about pacing.
    assert [step.contract_held for step in result.steps] == [False, True, True]
    assert result.steps[0].new_failures == ["rate_shortfall"]
    assert result.lowest_holding == 12.0
    assert result.ceiling_at is None, "a failure below every holding rung is a floor"
    text = sweep_mod.summarize_sweep(result)
    assert "the most headroom any rung" in text
    assert "ran out of rungs" in text
    assert "license no" in text or "licenses no" in text


def test_starved_channels_are_not_reported_as_unsupported_by_the_ecu() -> None:
    """C10.12 — at a budget far below demand the scheduler never gets some
    channels onto the wire at all. The classifier used to call that
    ``ecu_unsupported_channel``, which on the truck reads as "the ECM does not
    serve this PID" about a PID it serves fine one rung up. A saturated budget
    with zero timeouts means starved, not unsupported."""
    from types import SimpleNamespace

    from src.validation.evidence import limiting_stage

    quality = {"failures": ["unanswered_input", "acquisition_gap"]}

    def classify(budget: float) -> dict[str, Any]:
        artifacts = SimpleNamespace(
            started_wall=0.0,
            ended_wall=10.0,
            writer_stalls=0,
            finalize={},
            scheduler_metrics={
                "bus_budget_requests_per_second": budget,
                "requests_sent": 39,
                "request_timeouts": 0,
            },
        )
        return limiting_stage(artifacts, {}, quality, {})

    # 39 requests in 10 s against a 4 req/s budget is saturated.
    starved = classify(4.0)
    assert starved["stage"] == "bus_budget_request_pacing", starved
    assert "starved, not unsupported" in starved["evidence"]["note"]

    # With headroom the same failure really is the ECU's answer.
    supported = classify(40.0)
    assert supported["stage"] == "ecu_unsupported_channel", supported


def test_expected_failures_any_accepts_either_legitimate_name() -> None:
    """A scenario whose outcome has two legitimate names must be able to say
    so. Declaring one made ``expected_failures_met`` depend on host timing;
    declaring both under ``expected_failures`` would have demanded *both*."""
    from src.validation.report import headline

    def met(failures: list[str]) -> bool:
        return bool(
            headline(
                (),
                {"quality": {"failures": failures}},
                expected_any=("rate_shortfall", "unanswered_input"),
            )["expected_failures_met"]
        )

    assert met(["rate_shortfall"]) is True
    assert met(["unanswered_input"]) is True
    assert met(["rate_shortfall", "unanswered_input"]) is True
    assert met(["persistence_loss"]) is False
    # An empty "any" set must not make every run vacuously met-or-unmet.
    assert headline((), {"quality": {"failures": []}})["expected_failures_met"] is True


def test_an_idle_bus_missing_its_rate_blames_the_schedule_not_the_ecu() -> None:
    """C04.12 — the interval policy missed 4.0 Hz by 26 % with the bus 27 %
    used and zero timeouts, where the cycle policy hit it exactly under the
    identical workload. Neither the budget nor the ECM was the constraint; the
    schedule was. Reporting ``ecu_response_time`` there blames the ECM for an
    end-anchored schedule."""
    from types import SimpleNamespace

    from src.validation.evidence import limiting_stage

    def classify(sent: int, timeouts: int) -> dict[str, Any]:
        artifacts = SimpleNamespace(
            started_wall=0.0,
            ended_wall=10.0,
            writer_stalls=0,
            finalize={},
            scheduler_metrics={
                "bus_budget_requests_per_second": 90.0,
                "requests_sent": sent,
                "request_timeouts": timeouts,
            },
        )
        return limiting_stage(artifacts, {}, {"failures": ["rate_shortfall"]}, {})

    idle = classify(sent=239, timeouts=0)  # utilisation 0.266
    assert idle["stage"] == "scheduling_policy_idle_bus", idle
    assert idle["evidence"]["utilisation"] < 0.5

    # Timeouts on an idle bus really are the ECU taking too long.
    slow_ecu = classify(sent=239, timeouts=12)
    assert slow_ecu["stage"] == "ecu_response_time", slow_ecu

    # A saturated bus is still pacing.
    saturated = classify(sent=860, timeouts=0)
    assert saturated["stage"] == "bus_budget_request_pacing", saturated
