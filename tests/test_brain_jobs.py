"""The second brain's idle-time jobs (E04.4): run only while collection is
inactive, yield to a capture, survive a restart without duplicating evidence,
and leave work whose inputs are incomplete pending.

The twin job runs the real chain — W08/afr_calibration table resolution, the
target-phi evaluator, the evidence ledger — over the committed October 4
slices, in a thread instead of the spawn child (the child is the digest's own,
tested in ``tests/test_trip_end_digest*.py``) and without the address-space
cap, which would otherwise land on the test process.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from src.config import CanaryConfig
from src.control import brain_jobs
from src.control.brain_jobs import (
    BrainJobRefusedError,
    BrainJobStore,
    BrainScheduler,
    canonical_params,
    collection_activity,
    job_id_for,
    run_brain_job,
)
from src.evidence import memory_ledger_for
from src.vehicle.profiles import VehicleProfile, scope_config
from tests.configs import isolated_config
from tests.test_target_phi import EXPECT, REPO

SLUG = "nissan-xterra-2008"
SESSION = EXPECT["source_session"]


def scoped_config(tmp_path: Path) -> CanaryConfig:
    """The real vehicle profile's tracked files; every writable path under tmp."""
    base = isolated_config(tmp_path, keep=("afr_calibration",))
    return scope_config(base, VehicleProfile(slug=SLUG, vin="", name="Xterra"))


def write_session(config: CanaryConfig, *, finished: bool = True) -> Path:
    """The two committed October 4 slices as one stored capture session."""
    session = config.collection.storage_path / SESSION
    session.mkdir(parents=True)
    for i, part in enumerate(EXPECT["telemetry"]):
        shutil.copyfile(REPO / part["path"], session / f"telemetry-{i:05d}.parquet")
    if finished:
        (session / "digest_skipped.json").write_text(json.dumps({"reason": "test"}))
    return session


def in_thread(kind: str, params: dict[str, Any], config: CanaryConfig, **kw: Any) -> Any:
    kw["address_space_bytes"] = None  # never cap the test process itself
    return run_brain_job(kind, params, config, **kw)


def scheduler(
    config: CanaryConfig,
    activity: Callable[[], str | None] = lambda: None,
    runner: Callable[..., Any] = in_thread,
    **kw: Any,
) -> BrainScheduler:
    return BrainScheduler(
        config,
        activity=activity,
        runner=runner,
        executor_factory=lambda: ThreadPoolExecutor(max_workers=1),
        poll_seconds=0.02,
        **kw,
    )


@pytest.fixture
def config(tmp_path: Path) -> Iterator[CanaryConfig]:
    yield scoped_config(tmp_path)
    # A finished spawn child exits shortly after its result; until it has,
    # the registry (rightly) blocks the next job. Don't leak that across tests.
    from src.control import child_pool

    assert asyncio.run(child_pool.wait_for_child_exit(child_pool.BRAIN_CHILD, 15))


def submit_twin(config: CanaryConfig) -> str:
    params = canonical_params(config, "twin_session_evaluation", {"session": SESSION})
    job, created = BrainJobStore.for_config(config).submit("twin_session_evaluation", params)
    assert created
    return job.job_id


def test_a_twin_job_completes_on_the_pi_without_a_remote_host(config: CanaryConfig) -> None:
    write_session(config)
    job_id = submit_twin(config)
    ran = asyncio.run(scheduler(config).step())
    assert ran is not None and ran.state == "completed", ran
    assert ran.job_id == job_id and ran.attempts == 1
    assert ran.progress["stage"] == "completed"

    store = BrainJobStore.for_config(config)
    result = store.read_result(job_id)
    assert result is not None
    assert result["calibration"] == EXPECT["calibration"]["revision"]
    assert result["program_id"] == "nissan-1ZS44A-uprev-osiris"
    (evaluation,) = result["evaluations"]  # slot 1 only, the session's resolved slot
    assert evaluation["slot"] == 1
    assert evaluation["accounting"]["candidates"] == 174
    assert len(evaluation["trace"]) == 174 and evaluation["trace_omitted"] == 0
    assert evaluation["capture_requirement"]["reason"] == "no_eligible_tuples"

    ledger = memory_ledger_for(config)
    (evidence_id,) = result["evidence"]
    record = ledger.resolve_evidence(evidence_id).record
    assert record.labels["computation"] == "ecu_command"
    # Blocked qualification is unvalidated with the blockers named, never a pass.
    assert record.result.status == "unevaluable"
    assert record.result.reason == "unvalidated"
    assert record.result.residual is None
    assert record.vehicle.calibration == EXPECT["calibration"]["revision"]
    assert record.sources[0].session == SESSION
    assert ran.result is not None and ran.result["evidence"] == [evidence_id]


def test_collection_defers_and_a_resumed_capture_preempts(config: CanaryConfig) -> None:
    write_session(config)
    job_id = submit_twin(config)
    state = {"activity": "collecting:engine_off"}
    release = threading.Event()
    started = threading.Event()
    calls: list[int] = []

    finished = threading.Event()

    def slow(kind: str, params: dict[str, Any], cfg: CanaryConfig, **kw: Any) -> Any:
        calls.append(1)
        started.set()
        release.wait(5)
        try:
            return in_thread(kind, params, cfg, **kw)
        finally:
            finished.set()

    sched = scheduler(config, activity=lambda: state["activity"], runner=slow)

    async def scenario() -> None:
        # A parked truck whose bus still talks is collecting: nothing runs.
        assert await sched.step() is None
        job = BrainJobStore.for_config(config).get(job_id)
        assert job is not None and job.state == "deferred"
        assert job.reason == "collecting:engine_off"
        assert not calls
        # Idle: the job starts; a capture resuming makes it yield.
        state["activity"] = None
        task = asyncio.create_task(sched.step())
        while not started.is_set():
            await asyncio.sleep(0.01)
        state["activity"] = "capture_active"
        yielded = await task
        assert yielded is not None and yielded.state == "deferred"
        assert yielded.reason == "yielded:capture_active"
        release.set()
        # A thread cannot be killed the way the spawn child is; let the yielded
        # attempt end (its evidence is the same records) before the rerun.
        while not finished.is_set():
            await asyncio.sleep(0.01)
        # The next idle window reruns it to completion.
        state["activity"] = None
        done = await sched.step()
        assert done is not None and done.state == "completed" and done.attempts == 2

    asyncio.run(scenario())


def test_a_restart_neither_loses_the_job_nor_duplicates_evidence(config: CanaryConfig) -> None:
    write_session(config)
    job_id = submit_twin(config)
    store = BrainJobStore.for_config(config)
    # The daemon died after the child minted evidence but before it finished:
    # the evidence is on the ledger and the job log still says "running".
    first = in_thread(
        "twin_session_evaluation",
        {"session": SESSION},
        config,
        job_id=job_id,
        results_dir=store.results,
    )
    job = store.get(job_id)
    assert job is not None
    store.transition(job, "running", attempts=1)
    ledger_before = memory_ledger_for(config).entries()

    # Restart: a fresh store and scheduler over the same state directory.
    restarted = scheduler(config)
    (recovered,) = restarted.recover()
    assert recovered.state == "deferred" and recovered.reason == "interrupted_by_restart"
    done = asyncio.run(restarted.step())
    assert done is not None and done.state == "completed" and done.attempts == 2
    assert done.result is not None and done.result["evidence"] == first["evidence"]
    after = memory_ledger_for(config).entries()
    assert [e.record.id for e in after] == [e.record.id for e in ledger_before]
    # Submitting the same work again is the same job, not a second one.
    again, created = store.submit("twin_session_evaluation", {"session": SESSION})
    assert not created and again.job_id == job_id and again.state == "completed"


def test_an_open_session_stays_pending_until_it_is_finished(config: CanaryConfig) -> None:
    session = write_session(config, finished=False)
    job_id = submit_twin(config)
    sched = scheduler(config)
    assert asyncio.run(sched.step()) is None
    job = BrainJobStore.for_config(config).get(job_id)
    assert job is not None and job.state == "pending" and job.reason == "session_open"
    # Still pending on the next idle window: never forced through.
    assert asyncio.run(sched.step()) is None
    assert BrainJobStore.for_config(config).get(job_id).state == "pending"  # type: ignore[union-attr]
    (session / "digest_skipped.json").write_text("{}")
    done = asyncio.run(sched.step())
    assert done is not None and done.state == "completed"


def test_digest_and_workbench_children_are_shared_not_doubled(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_session(config)
    job_id = submit_twin(config)
    monkeypatch.setattr(brain_jobs, "any_digest_in_flight", lambda: True)
    assert asyncio.run(scheduler(config).step()) is None
    assert BrainJobStore.for_config(config).get(job_id).reason == "digest_running"  # type: ignore[union-attr]
    monkeypatch.setattr(brain_jobs, "any_digest_in_flight", lambda: False)
    monkeypatch.setattr(brain_jobs, "any_workbench_job_in_flight", lambda: True)
    assert asyncio.run(scheduler(config).step()) is None
    assert BrainJobStore.for_config(config).get(job_id).reason == "workbench_running"  # type: ignore[union-attr]


def test_submission_is_identified_validated_and_rerunnable(config: CanaryConfig) -> None:
    write_session(config)
    store = BrainJobStore.for_config(config)
    with pytest.raises(BrainJobRefusedError) as missing:
        canonical_params(config, "twin_session_evaluation", {"session": "2020-01-01T000000"})
    assert missing.value.status == 404
    with pytest.raises(BrainJobRefusedError):
        store.submit("retrain_everything", {})
    a, created_a = store.submit("memory_consolidation", {})
    b, created_b = store.submit("memory_consolidation", {})
    assert created_a and not created_b and a.job_id == b.job_id
    assert a.job_id == job_id_for("memory_consolidation", {})
    done = asyncio.run(scheduler(config).step())
    assert done is not None and done.state == "completed"
    again, created = store.submit("memory_consolidation", {}, rerun=True)
    assert created and again.state == "queued"


def test_memory_consolidation_summarises_without_appending(config: CanaryConfig) -> None:
    write_session(config)
    submit_twin(config)
    asyncio.run(scheduler(config).step())
    before = len(memory_ledger_for(config).entries())
    job, _ = BrainJobStore.for_config(config).submit("memory_consolidation", {})
    done = asyncio.run(scheduler(config).step())
    assert done is not None and done.state == "completed"
    result = BrainJobStore.for_config(config).read_result(job.job_id)
    assert result is not None and result["records"] == before
    assert result["by_kind"] == {"evidence": before}
    assert len(memory_ledger_for(config).entries()) == before


def test_a_failing_job_is_failed_with_its_reason(config: CanaryConfig) -> None:
    def broken(*_args: Any, **_kw: Any) -> Any:
        raise RuntimeError("table bytes unreadable")

    job, _ = BrainJobStore.for_config(config).submit("memory_consolidation", {})
    done = asyncio.run(scheduler(config, runner=broken).step())
    assert done is not None and done.state == "failed"
    assert done.reason == "error" and "table bytes unreadable" in (done.detail or "")
    # A failed job is queued again on resubmission.
    again, created = BrainJobStore.for_config(config).submit("memory_consolidation", {})
    assert created and again.state == "queued" and again.job_id == job.job_id


class _Live:
    def __init__(self, inputs: dict[str, float | None]) -> None:
        self.inputs = inputs

    def vehicle_state_inputs(self, now: Any) -> dict[str, float | None]:
        return self.inputs


class _Replay:
    def __init__(self, running: bool) -> None:
        self.running = running

    def status(self) -> Any:
        class _S:
            replay_id = "r1" if self.running else None
            running = self.running

        return _S()


def test_only_no_data_counts_as_collection_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.collection import tuning_profile

    monkeypatch.setattr(tuning_profile, "is_active", lambda: False)
    parked = _Live({"can_freshness_seconds": 0.5, "obd_freshness_seconds": None})
    silent = _Live({"can_freshness_seconds": None, "obd_freshness_seconds": None})
    assert collection_activity(live_source=None, replay_controller=None) == (
        "collection_state_unknown"
    )
    assert collection_activity(live_source=parked, replay_controller=None) == (
        "collecting:engine_off"
    )
    assert collection_activity(live_source=silent, replay_controller=None) is None
    assert (
        collection_activity(live_source=silent, replay_controller=_Replay(True)) == "replay_active"
    )
    monkeypatch.setattr(tuning_profile, "is_active", lambda: True)
    assert collection_activity(live_source=silent, replay_controller=None) == "capture_active"


def test_the_real_spawn_child_runs_a_job(config: CanaryConfig) -> None:
    """The production path: the digest's single-use spawn pool and its
    address-space cap (in the child only), with a picklable config."""
    job, _ = BrainJobStore.for_config(config).submit("memory_consolidation", {})
    sched = BrainScheduler(config, activity=lambda: None, poll_seconds=0.05)
    done = asyncio.run(sched.step())
    assert done is not None and done.state == "completed", done
    assert BrainJobStore.for_config(config).read_result(job.job_id) is not None
    # The slot stays alive until the process itself has exited, then clears.
    from src.control import child_pool

    assert asyncio.run(
        child_pool.wait_for_child_exit(child_pool.BRAIN_CHILD, child_pool.CHILD_EXIT_BOUND_SECONDS)
    )
    assert not child_pool.child_alive(child_pool.BRAIN_CHILD)


# -- the event loop never waits on the brain's disk work -------------------------


def test_step_keeps_the_loop_responsive_under_a_slow_precheck(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The precheck lists sessions and reads Parquet footers (0.4-1.6 s on the
    laptop); with it, the job log, identity and transitions all off the loop,
    a heartbeat on the loop keeps ticking while step() waits."""
    write_session(config)
    submit_twin(config)

    def slow_precheck(cfg: CanaryConfig, job: Any) -> Any:
        time.sleep(0.6)
        return "pending", "session_open", "slow"

    monkeypatch.setattr(brain_jobs, "precheck", slow_precheck)
    sched = scheduler(config)

    async def scenario() -> float:
        gaps: list[float] = []
        stop = asyncio.Event()

        async def heartbeat() -> None:
            loop = asyncio.get_running_loop()
            last = loop.time()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = loop.time()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        started = asyncio.get_running_loop().time()
        assert await sched.step() is None
        elapsed = asyncio.get_running_loop().time() - started
        stop.set()
        await beat
        assert elapsed >= 0.6
        return max(gaps)

    assert asyncio.run(scenario()) < 0.2


def test_the_job_log_is_read_incrementally(config: CanaryConfig) -> None:
    store = BrainJobStore.for_config(config)
    first, _ = store.submit("memory_consolidation", {})
    other = BrainJobStore.for_config(config)  # another instance, same file
    assert other.get(first.job_id) is not None
    index = brain_jobs._INDEXES[store.path.resolve()]
    offset = index.offset
    other.transition(first, "deferred", reason="collecting:engine_off")
    job = store.get(first.job_id)
    assert job is not None and job.state == "deferred"
    assert index.offset > offset  # only the appended bytes were parsed
    # A torn tail (power cut mid-line) is neither parsed nor swallows the next line.
    with store.path.open("ab") as handle:
        handle.write(b'{"job_id": "torn')
    assert store.get(first.job_id).state == "deferred"  # type: ignore[union-attr]
    store.transition(job, "queued")
    assert store.get(first.job_id).state == "queued"  # type: ignore[union-attr]
    # A replaced file is re-read whole.
    store.path.write_text("")
    assert store.jobs() == {}


# -- one analysis child at a time ---------------------------------------------------


def test_the_tick_must_exceed_a_yielded_childs_exit_bound(config: CanaryConfig) -> None:
    from src.control.child_pool import CHILD_EXIT_BOUND_SECONDS, TERMINATE_GRACE_SECONDS

    assert brain_jobs.MIN_TICK_SECONDS == CHILD_EXIT_BOUND_SECONDS > TERMINATE_GRACE_SECONDS
    with pytest.raises(ValueError, match="must exceed"):
        BrainScheduler(config, activity=lambda: None, tick_seconds=TERMINATE_GRACE_SECONDS)


def test_a_workbench_request_makes_a_running_job_yield(config: CanaryConfig) -> None:
    from src.control import child_pool

    job, _ = BrainJobStore.for_config(config).submit("memory_consolidation", {})
    release = threading.Event()

    def slow(kind: str, params: dict[str, Any], cfg: CanaryConfig, **kw: Any) -> Any:
        release.wait(5)
        return in_thread(kind, params, cfg, **kw)

    sched = scheduler(config, runner=slow)

    async def scenario() -> Any:
        task = asyncio.create_task(sched.step())
        while not child_pool.child_alive(child_pool.BRAIN_CHILD):
            await asyncio.sleep(0.01)
        child_pool.request_yield(child_pool.BRAIN_CHILD)  # what run_off_loop does
        try:
            return await task
        finally:
            release.set()

    yielded = asyncio.run(scenario())
    assert yielded.state == "deferred" and yielded.reason == "yielded:analysis_requested"
    assert not child_pool.child_alive(child_pool.BRAIN_CHILD)


def test_twin_evaluation_is_queued_after_a_digest_only_for_sessions_with_its_command(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from src.daemon import runtime
    from src.storage.capture_manifest import CAPTURE_MANIFEST_FILENAME

    session = write_session(config)

    async def digest(*_args: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(runtime, "handle_trip_end_off_loop", digest)
    components = SimpleNamespace(emitter=None, notifier=None)

    def after_digest() -> None:
        asyncio.run(runtime._trip_end_then_brain(session, config, components, None))  # type: ignore[arg-type]

    # A plain drive (no capture manifest): nothing queued.
    after_digest()
    assert BrainJobStore.for_config(config).jobs() == {}
    manifest = {
        "profile": "ram-study",
        "started_at": "2026-10-04T20:45:57+00:00",
        "profile_snapshot": {"memory": ["ECM_ENGINE_SPEED_RAM"]},
    }
    (session / CAPTURE_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    after_digest()  # memory channels, but not the twin's command word
    assert BrainJobStore.for_config(config).jobs() == {}
    manifest["profile_snapshot"] = {"memory": ["ECM_TARGET_EQUIV_RATIO", "ECM_ENGINE_SPEED_RAM"]}
    (session / CAPTURE_MANIFEST_FILENAME).write_text(json.dumps(manifest))
    after_digest()
    (job,) = BrainJobStore.for_config(config).jobs().values()
    assert job.kind == "twin_session_evaluation" and job.params == {"session": SESSION}
    assert job.state == "queued"
    after_digest()  # the same drive again is the same job
    assert len(BrainJobStore.for_config(config).jobs()) == 1
