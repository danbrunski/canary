"""C08 — live-inference scheduling and isolation.

Three layers, each testable without the next:

* :class:`TickScheduler` under a fake clock (C08.9 / C08.13): deadlines stay
  on the ``start + k * interval`` grid regardless of processing time, an
  overrun skips to the next *future* deadline (counted, never a catch-up
  burst), stop is prompt.
* :class:`InferenceTimingRecorder` (C08.1): bounded storage, JSON-safe
  snapshot, reachable from ``build_status``.
* :class:`LiveInferencePipeline` with a gate-controlled fake model (C08.10):
  the forward pass runs in the one worker while ``LiveFeatureSource`` stays
  on the loop, a busy worker skips (never queues), a generation change
  discards the in-flight result, a worker exception is counted, shutdown
  leaves no thread.

Plus replay parity (C08.11): the committed ground-truth drive slice through
the old synchronous ``source.tick`` path and the new pipeline, same
observations and tick times, identical rows.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from src.config import CanaryConfig
from src.control.replay_control import ReplayStatus
from src.daemon.components import build_components
from src.daemon.inference import (
    INFERENCE_WORKER_THREAD_PREFIX,
    LiveInferencePipeline,
    build_live_feature_source,
    run_live_inference,
)
from src.daemon.inference_metrics import (
    MISSED_COMPUTE_BUSY,
    MISSED_OVERRUN,
    InferenceTimingRecorder,
    StageSummary,
)
from src.daemon.inference_scheduling import TickScheduler, TickSlot
from src.dashboard.status import build_status
from src.models.manager import LoadedModel
from src.models.manifest import ModelManifestCommon
from src.telemetry import TelemetryRecord

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config_yaml(tmp_path: Path) -> str:
    return f"""
vehicle:
  make: Nissan
  model: Xterra
  year: 2008
  vin: ""
hardware:
  elm327_port: /dev/ttyUSB0
  can:
    interface: virtual
    channel: vcan0
    bitrate: 500000
collection:
  obd_poll_rate_hz: 1
  storage_path: {tmp_path / "storage"}
  session_retention_days: 30
  storage_warning_mb: 500
preprocessing:
  resample_hz: 1
  rolling_window_seconds: 30
  operating_modes:
    warmup:
      rpm_min: 400
      speed_max: 0
      coolant_temp_max: 70
    idle:
      rpm_min: 400
      rpm_max: 900
      speed_max: 0
      coolant_temp_min: 70
    city:
      rpm_min: 400
      speed_min: 1
      speed_max: 60
    highway:
      rpm_min: 400
      speed_min: 60
models:
  isolation_forest:
    enabled: false
    n_estimators: 100
    contamination: 0.01
  lstm_autoencoder:
    enabled: false
    sequence_length: 60
    latent_dim: 16
    threshold_k: 3.0
dashboard:
  host: 127.0.0.1
  port: 0
daemon:
  anomalies_dir: {tmp_path / "anomalies"}
  models_dir: {tmp_path / "models"}
  model_poll_seconds: 0.05
  retention_sweep_seconds: 0.05
  broadcaster_queue_size: 64
  catalog_db: {tmp_path / "control" / "catalog.db"}
"""


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(tmp_path))
    return CanaryConfig.from_yaml(cfg_path)


class FakeClock:
    """Monotonic clock + sleep that only move when told to."""

    def __init__(self) -> None:
        self.now = 0.0
        self.oversleep = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds + self.oversleep
        await asyncio.sleep(0)


async def _drive(
    clock: FakeClock,
    interval: float,
    work: Callable[[TickSlot], float],
    ticks: int,
) -> tuple[list[TickSlot], InferenceTimingRecorder]:
    """Run the scheduler for ``ticks`` ticks; ``work`` returns how long each
    tick's processing takes on the fake clock."""
    stop = asyncio.Event()
    slots: list[TickSlot] = []
    scheduler = TickScheduler(interval, monotonic=clock.monotonic, sleep=clock.sleep)

    async def run_tick(slot: TickSlot) -> None:
        slots.append(slot)
        clock.now += work(slot)
        if len(slots) >= ticks:
            stop.set()

    await scheduler.run(run_tick, stop)
    return slots, scheduler.recorder


# ---------------------------------------------------------------------------
# C08.9 / C08.13 — scheduler under a fake clock
# ---------------------------------------------------------------------------


async def test_deadlines_stay_on_grid_despite_processing_time() -> None:
    clock = FakeClock()
    slots, recorder = await _drive(clock, 1.0, lambda _slot: 0.3, ticks=20)
    assert [s.index for s in slots] == list(range(20))
    assert [s.deadline for s in slots] == [float(k) for k in range(20)]
    assert [s.started for s in slots] == [float(k) for k in range(20)]
    assert all(s.lateness_seconds == 0.0 for s in slots)
    assert recorder.missed_ticks == 0
    assert recorder.ticks_scheduled == 20


async def test_processing_time_does_not_accumulate_as_drift() -> None:
    """The pre-C08 loop was ``process(); await wait(interval)`` — its k-th
    tick started at ``k * (interval + processing)``. Reproduce that reference
    on the same fake clock and show the new grid has zero drift where the old
    one drifted by ``ticks * processing``."""
    interval, processing, ticks = 1.0, 0.04, 100

    old_clock = FakeClock()
    old_starts: list[float] = []
    for _ in range(ticks):
        old_starts.append(old_clock.now)
        old_clock.now += processing  # synchronous processing on the loop
        await old_clock.sleep(interval)  # then a full interval of waiting
    old_drift = old_starts[-1] - (ticks - 1) * interval

    new_clock = FakeClock()
    slots, recorder = await _drive(new_clock, interval, lambda _s: processing, ticks=ticks)
    new_drift = slots[-1].started - (ticks - 1) * interval

    assert old_drift == pytest.approx((ticks - 1) * processing)  # 3.96 s over 99 periods
    assert new_drift == 0.0
    assert recorder.missed_ticks == 0


async def test_overrun_skips_to_next_future_deadline_without_catchup() -> None:
    """A 3.5-period stall in tick 0: deadlines 1, 2, 3 are missed, exactly
    one tick runs next — at deadline 4 — and nothing runs in (3.5, 4)."""
    clock = FakeClock()

    def work(slot: TickSlot) -> float:
        return 3.5 if slot.index == 0 else 0.1

    slots, recorder = await _drive(clock, 1.0, work, ticks=4)
    assert [s.index for s in slots] == [0, 4, 5, 6]
    assert slots[1].deadline == 4.0
    assert slots[1].started == 4.0
    assert slots[1].skipped_before == 3
    assert slots[1].lateness_seconds == 0.0
    assert recorder.missed_ticks == 3
    assert recorder.snapshot().missed_by_reason == {MISSED_OVERRUN: 3}
    # No catch-up burst: consecutive starts are never closer than one period.
    gaps = [b.started - a.started for a, b in zip(slots, slots[1:], strict=False)]
    assert all(gap >= 1.0 for gap in gaps)


async def test_one_period_overrun_misses_exactly_one_deadline() -> None:
    clock = FakeClock()

    def work(slot: TickSlot) -> float:
        return 1.2 if slot.index == 0 else 0.0

    slots, recorder = await _drive(clock, 1.0, work, ticks=3)
    assert [s.index for s in slots] == [0, 2, 3]
    assert slots[1].started == 2.0
    assert recorder.missed_ticks == 1


async def test_exact_period_processing_is_on_time_not_missed() -> None:
    clock = FakeClock()
    slots, recorder = await _drive(clock, 1.0, lambda _s: 1.0, ticks=5)
    assert [s.index for s in slots] == [0, 1, 2, 3, 4]
    assert all(s.lateness_seconds == 0.0 for s in slots)
    assert recorder.missed_ticks == 0


async def test_late_wakeup_is_recorded_but_does_not_shift_the_grid() -> None:
    """Loop scheduling delay (a late wake-up) shows as lateness against an
    unchanged deadline — it is measured, not folded into the next period."""
    clock = FakeClock()
    clock.oversleep = 0.05
    slots, recorder = await _drive(clock, 1.0, lambda _s: 0.2, ticks=10)
    assert [s.deadline for s in slots] == [float(k) for k in range(10)]
    assert slots[0].lateness_seconds == 0.0
    assert all(s.lateness_seconds == pytest.approx(0.05) for s in slots[1:])
    assert slots[-1].started == pytest.approx(9.05)  # not 9 + 9 * 0.05
    lateness = recorder.snapshot().lateness
    assert lateness.count == 10
    assert lateness.max_ms == pytest.approx(50.0)
    assert recorder.missed_ticks == 0


async def test_stop_returns_promptly_while_waiting_for_a_far_deadline() -> None:
    stop = asyncio.Event()
    ticks = 0

    async def run_tick(_slot: TickSlot) -> None:
        nonlocal ticks
        ticks += 1

    scheduler = TickScheduler(30.0)  # real clock; the wait must be interruptible
    task = asyncio.create_task(scheduler.run(run_tick, stop))
    await asyncio.sleep(0.05)
    assert ticks == 1
    started = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert time.monotonic() - started < 0.5


async def test_stop_set_during_a_tick_ends_the_loop_without_another_tick() -> None:
    clock = FakeClock()
    slots, _ = await _drive(clock, 1.0, lambda _s: 0.1, ticks=1)
    assert len(slots) == 1


async def test_run_tick_exception_is_logged_and_the_loop_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    stop = asyncio.Event()
    seen: list[int] = []
    scheduler = TickScheduler(1.0, monotonic=clock.monotonic, sleep=clock.sleep)

    async def run_tick(slot: TickSlot) -> None:
        seen.append(slot.index)
        if slot.index == 1:
            raise RuntimeError("boom")
        if slot.index == 3:
            stop.set()

    with caplog.at_level("ERROR"):
        await scheduler.run(run_tick, stop)
    assert seen == [0, 1, 2, 3]
    assert any("tick 1 failed" in rec.getMessage() for rec in caplog.records)


def test_scheduler_rejects_nonpositive_interval() -> None:
    with pytest.raises(ValueError):
        TickScheduler(0.0)


# ---------------------------------------------------------------------------
# C08.1 — recorder + /status exposure
# ---------------------------------------------------------------------------


def test_recorder_storage_is_bounded() -> None:
    recorder = InferenceTimingRecorder(window=5)
    for i in range(100):
        recorder.record_stage("feature", i / 1000.0)
        recorder.record_lateness(i / 1000.0)
    snap = recorder.snapshot()
    assert snap.stages["feature"].count == 5
    assert snap.stages["feature"].max_ms == pytest.approx(99.0)
    assert snap.stages["feature"].median_ms == pytest.approx(97.0)
    assert snap.lateness.count == 5


def test_stage_summary_percentiles() -> None:
    summary = StageSummary.from_samples([0.001 * i for i in range(1, 21)])
    assert summary.count == 20
    assert summary.median_ms == pytest.approx(11.0)
    assert summary.p95_ms == pytest.approx(19.0)
    assert summary.max_ms == pytest.approx(20.0)
    assert summary.last_ms == pytest.approx(20.0)
    assert StageSummary.from_samples([]).median_ms is None


def test_snapshot_is_json_safe_and_separates_clock_from_work() -> None:
    recorder = InferenceTimingRecorder()
    recorder.interval_seconds = 1.0
    recorder.record_stage("sequence", 0.5)
    recorder.record_lateness(0.002)
    recorder.note_missed(MISSED_COMPUTE_BUSY, 2)
    recorder.note_missed(MISSED_OVERRUN)
    recorder.note_stale_discarded()
    recorder.note_compute_failure()
    recorder.note_tick_scheduled()
    payload = recorder.snapshot().to_dict()
    json.dumps(payload)
    assert payload["missed_ticks"] == 3
    assert payload["missed_by_reason"] == {MISSED_COMPUTE_BUSY: 2, MISSED_OVERRUN: 1}
    assert payload["stale_results_discarded"] == 1
    assert payload["compute_failures"] == 1
    assert payload["lateness"]["max_ms"] == pytest.approx(2.0)
    assert payload["stages"]["sequence"]["max_ms"] == pytest.approx(500.0)


def test_build_status_exposes_inference_timing(config: CanaryConfig) -> None:
    recorder = InferenceTimingRecorder()
    recorder.note_missed(MISSED_OVERRUN, 4)
    payload = build_status(config, inference_timing=recorder).to_dict()
    assert payload["detectors"]["inference"]["missed_ticks"] == 4
    assert build_status(config).to_dict()["detectors"] is None


# ---------------------------------------------------------------------------
# C08.10 — pipeline ownership, bounded compute, generations, failure, shutdown
# ---------------------------------------------------------------------------


@dataclass
class GatedTrained:
    """Fake IF artifact whose ``score`` blocks on ``gate`` so a test can hold
    a compute job in flight and act on the loop meanwhile."""

    feature_columns: tuple[str, ...] = ("obd:RPM",)
    mode_column: str = "operating_mode"
    manifest: Any = field(default_factory=lambda: SimpleNamespace(model_version="v1"))
    gate: threading.Event = field(default_factory=threading.Event)
    calls: int = 0
    threads: list[str] = field(default_factory=list)
    fail_next: bool = False

    def score(self, frame: pd.DataFrame) -> pd.DataFrame:
        self.calls += 1
        self.threads.append(threading.current_thread().name)
        assert self.gate.wait(timeout=5.0), "test gate never released"
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("worker boom")
        out = frame.copy()
        out["isolation_forest_score"] = 0.9
        out["isolation_forest_label"] = -1
        out["isolation_forest_model_mode"] = "idle"
        out["isolation_forest_missing_features"] = False
        out["isolation_forest_calibrated_threshold"] = 0.5
        out["isolation_forest_calibrated_anomaly"] = True
        return out


class SwappableManager:
    """Manager stub whose served version a test flips mid-compute."""

    def __init__(self, trained: GatedTrained) -> None:
        self.trained = trained
        self.version = "v1"

    @property
    def current(self) -> LoadedModel[Any]:
        return LoadedModel(
            artifact=self.trained,
            manifest=ModelManifestCommon(
                artifact_format_version=1,
                model_type="isolation_forest",
                model_version=self.version,
                created_at="now",
                training_data_hash="h",
                input_rows=1,
            ),
            artifact_path=Path("/tmp/x"),
            manifest_path=Path("/tmp/x.manifest.json"),
            manifest_mtime=0.0,
        )

    def poll(self) -> None:
        return None


class FlippableReplay:
    def __init__(self) -> None:
        self.status_value = ReplayStatus(running=False)

    def status(self) -> ReplayStatus:
        return self.status_value


@dataclass
class Harness:
    components: Any
    source: Any
    trained: GatedTrained
    manager: SwappableManager
    pipeline: LiveInferencePipeline
    recorder: InferenceTimingRecorder
    scores: Any
    wall: list[datetime]
    tick_threads: list[str] = field(default_factory=list)

    def slot(self, index: int) -> TickSlot:
        return TickSlot(
            index=index,
            deadline=float(index),
            started=float(index),
            lateness_seconds=0.0,
            skipped_before=0,
        )

    async def tick(self, index: int) -> None:
        await self.pipeline.run_tick(self.slot(index))

    async def finish(self) -> None:
        inflight = self.pipeline.inflight
        if inflight is not None:
            await asyncio.wait_for(inflight, timeout=5.0)

    async def worker_started(self, calls: int) -> None:
        """Yield to the loop until the worker has begun its ``calls``-th
        forward pass (the compute task only starts once the loop runs)."""
        deadline = time.monotonic() + 5.0
        while self.trained.calls < calls:
            assert time.monotonic() < deadline, "worker never started"
            await asyncio.sleep(0.005)

    def isolation_forest_scores(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        while not self.scores.queue.empty():
            msg = self.scores.queue.get_nowait()
            if msg.get("source") == "isolation_forest":
                out.append(msg)
        return out


def _make_harness(config: CanaryConfig, *, released: bool = True) -> Harness:
    components = build_components(config)
    trained = GatedTrained()
    if released:
        trained.gate.set()
    manager = SwappableManager(trained)
    components.model_managers["isolation_forest"] = manager  # type: ignore[assignment]
    source = build_live_feature_source(components)
    base = datetime.now(UTC)
    source.observe(TelemetryRecord.obd(base.timestamp(), "RPM", 700.0, "rpm"))
    wall = [base]
    harness_threads: list[str] = []
    original_tick = source.tick

    def recording_tick(now: datetime) -> Any:
        harness_threads.append(threading.current_thread().name)
        return original_tick(now)

    source.tick = recording_tick  # type: ignore[method-assign]
    recorder = InferenceTimingRecorder()
    pipeline = LiveInferencePipeline(
        source,
        components,
        interval_seconds=1.0,
        recorder=recorder,
        wall_clock=lambda: wall[0],
    )
    return Harness(
        components=components,
        source=source,
        trained=trained,
        manager=manager,
        pipeline=pipeline,
        recorder=recorder,
        scores=components.scores_broadcaster.subscribe(),
        wall=wall,
        tick_threads=harness_threads,
    )


async def test_forward_pass_runs_in_worker_while_source_stays_on_loop(
    config: CanaryConfig,
) -> None:
    h = _make_harness(config, released=False)
    try:
        await h.tick(0)
        inflight = h.pipeline.inflight
        assert inflight is not None and not inflight.done()
        await h.worker_started(1)
        # The loop is free while the worker is blocked: observe() interleaves.
        h.source.observe(TelemetryRecord.obd(h.wall[0].timestamp(), "SPEED", 12.0, "kph"))
        await asyncio.sleep(0.05)
        assert not inflight.done()
        assert h.trained.calls == 1
        h.trained.gate.set()
        await h.finish()
    finally:
        h.trained.gate.set()
        await h.pipeline.close(timeout_seconds=1.0)
    assert h.trained.threads == [h.trained.threads[0]]
    assert h.trained.threads[0].startswith(INFERENCE_WORKER_THREAD_PREFIX)
    assert h.tick_threads == [threading.main_thread().name]
    assert h.recorder.ticks_completed == 1
    assert len(h.isolation_forest_scores()) == 1
    stages = h.recorder.snapshot().stages
    assert {"feature", "snapshot", "isolation_forest", "compute", "apply", "total"} <= set(stages)


async def test_busy_worker_queues_one_tick_and_drops_the_rest(config: CanaryConfig) -> None:
    """D01.9 — a tick arriving mid-score is snapshotted and queued, not
    skipped whole. The queue is depth 1 and the newest row wins, so only the
    *displaced* tick counts as a `compute_busy` miss.

    Before D01.9 both ticks 1 and 2 were skipped and neither was ever scored;
    that cost the pipeline the overlap it exists for, and on the truck every
    miss recorded was this one reason.
    """
    h = _make_harness(config, released=False)
    try:
        await h.tick(0)
        await h.worker_started(1)
        await h.tick(1)  # queued behind tick 0
        await h.tick(2)  # displaces tick 1; tick 1 is the only miss
        assert h.trained.calls == 1
        assert h.recorder.snapshot().missed_by_reason == {MISSED_COMPUTE_BUSY: 1}
        h.trained.gate.set()
        await h.finish()  # tick 0 applies, then the queued tick 2 dispatches
        await h.finish()
    finally:
        h.trained.gate.set()
        await h.pipeline.close(timeout_seconds=1.0)
    assert h.trained.calls == 2, "the queued tick must still be scored"
    assert h.recorder.ticks_completed == 2
    assert h.recorder.missed_ticks == 1


async def test_only_one_compute_runs_at_a_time(config: CanaryConfig) -> None:
    """The half that must NOT be pipelined. The sequence engine's window is
    advanced inside `compute`, so two concurrent scores would interleave it."""
    h = _make_harness(config, released=False)
    try:
        await h.tick(0)
        await h.worker_started(1)
        await h.tick(1)
        await h.tick(2)

        # Three ticks in, exactly one compute has been entered.
        assert h.trained.calls == 1
    finally:
        h.trained.gate.set()
        await h.pipeline.close(timeout_seconds=1.0)


async def test_snapshot_still_runs_while_the_worker_is_busy(config: CanaryConfig) -> None:
    """The point of D01.9: phase 1 of the next tick overlaps phase 2 of this
    one, so the feature build is no longer serialised behind the score."""
    h = _make_harness(config, released=False)
    try:
        await h.tick(0)
        await h.worker_started(1)
        before = h.recorder.snapshot().stages.get("feature")
        before_n = before.count if before is not None else 0

        await h.tick(1)  # worker still blocked

        after = h.recorder.snapshot().stages.get("feature")
        assert after is not None
        assert after.count > before_n, "the snapshot must run even while scoring"
    finally:
        h.trained.gate.set()
        await h.pipeline.close(timeout_seconds=1.0)


async def test_close_does_not_dispatch_the_queued_tick(config: CanaryConfig) -> None:
    """Shutdown must not start a fresh compute on its way out."""
    h = _make_harness(config, released=False)
    try:
        await h.tick(0)
        await h.worker_started(1)
        await h.tick(1)  # queued
        h.trained.gate.set()
    finally:
        await h.pipeline.close(timeout_seconds=1.0)

    assert h.pipeline._pending is None
    assert h.trained.calls == 1, "the queued tick must not be dispatched by close"


async def test_model_hot_swap_mid_compute_discards_the_stale_result(
    config: CanaryConfig,
) -> None:
    h = _make_harness(config, released=False)
    try:
        await h.tick(0)
        await h.worker_started(1)
        h.manager.version = "v2"  # hot swap while the worker is scoring v1
        h.trained.gate.set()
        await h.finish()
        assert h.recorder.stale_results_discarded == 1
        assert h.recorder.ticks_completed == 0
        assert h.isolation_forest_scores() == []
        await h.tick(1)  # next tick scores under v2 and applies
        await h.finish()
    finally:
        h.trained.gate.set()
        await h.pipeline.close(timeout_seconds=1.0)
    assert h.recorder.ticks_completed == 1
    assert h.recorder.stale_results_discarded == 1
    assert len(h.isolation_forest_scores()) == 1


async def test_replay_transition_mid_compute_discards_the_stale_result(
    config: CanaryConfig,
) -> None:
    h = _make_harness(config, released=False)
    replay = FlippableReplay()
    h.components.replay_controller = replay  # type: ignore[assignment]
    try:
        await h.tick(0)
        await h.worker_started(1)
        replay.status_value = ReplayStatus(running=True, replay_id="r1")
        h.trained.gate.set()
        await h.finish()
        assert h.recorder.stale_results_discarded == 1
        await h.tick(1)
        await h.finish()
        replay.status_value = ReplayStatus(running=False)
        await h.tick(2)  # replay stopped *before* this snapshot → applies normally
        await h.finish()
    finally:
        h.trained.gate.set()
        await h.pipeline.close(timeout_seconds=1.0)
    assert h.recorder.ticks_completed == 2
    assert h.recorder.stale_results_discarded == 1


async def test_worker_exception_is_counted_and_the_loop_continues(
    config: CanaryConfig, caplog: pytest.LogCaptureFixture
) -> None:
    h = _make_harness(config)
    h.trained.fail_next = True
    try:
        with caplog.at_level("ERROR"):
            await h.tick(0)
            await h.finish()
        assert h.recorder.compute_failures == 1
        assert h.recorder.ticks_completed == 0
        assert any("compute failed for tick 0" in r.getMessage() for r in caplog.records)
        await h.tick(1)
        await h.finish()
    finally:
        await h.pipeline.close(timeout_seconds=1.0)
    assert h.recorder.ticks_completed == 1
    assert h.recorder.compute_failures == 1


async def test_each_accepted_result_applies_exactly_once_in_order(
    config: CanaryConfig,
) -> None:
    h = _make_harness(config)
    base = h.wall[0]
    try:
        for k in range(5):
            h.wall[0] = base + timedelta(seconds=k)
            # Keep the fast channel fresh so every tick has a row to score.
            h.source.observe(TelemetryRecord.obd(h.wall[0].timestamp(), "RPM", 700.0, "rpm"))
            await h.tick(k)
            await h.finish()
    finally:
        await h.pipeline.close(timeout_seconds=1.0)
    published = [m["timestamp"] for m in h.isolation_forest_scores()]
    expected = [
        (base + timedelta(seconds=k)).strftime("%Y-%m-%dT%H:%M:%S.%fZ").replace(".000000Z", "Z")
        for k in range(5)
    ]
    assert len(published) == 5
    assert published == sorted(published)
    assert [p[:19] for p in published] == [e[:19] for e in expected]
    assert h.recorder.ticks_completed == 5
    assert h.trained.calls == 5


async def test_close_joins_the_worker_and_leaves_no_thread(config: CanaryConfig) -> None:
    h = _make_harness(config)
    await h.tick(0)
    await h.finish()
    assert any(t.name.startswith(INFERENCE_WORKER_THREAD_PREFIX) for t in threading.enumerate())
    await h.pipeline.close(timeout_seconds=1.0)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and any(
        t.name.startswith(INFERENCE_WORKER_THREAD_PREFIX) for t in threading.enumerate()
    ):
        await asyncio.sleep(0.01)
    assert not any(t.name.startswith(INFERENCE_WORKER_THREAD_PREFIX) for t in threading.enumerate())


async def test_ticks_with_nothing_to_score_apply_inline_without_the_worker(
    config: CanaryConfig,
) -> None:
    components = build_components(config)  # no IF manager at all
    source = build_live_feature_source(components)
    source.observe(TelemetryRecord.obd(datetime.now(UTC).timestamp(), "RPM", 700.0, "rpm"))
    recorder = InferenceTimingRecorder()
    pipeline = LiveInferencePipeline(source, components, interval_seconds=1.0, recorder=recorder)
    try:
        await pipeline.run_tick(
            TickSlot(index=0, deadline=0.0, started=0.0, lateness_seconds=0.0, skipped_before=0)
        )
    finally:
        await pipeline.close(timeout_seconds=1.0)
    assert pipeline.inflight is None
    assert recorder.ticks_completed == 1
    assert "compute" not in recorder.snapshot().stages


async def test_run_live_inference_records_timing_and_shuts_down(config: CanaryConfig) -> None:
    """End to end through the public entry: the scheduler + pipeline + owned
    executor, with ``tick_seconds`` as the model cadence (C08.8)."""
    components = build_components(config)
    trained = GatedTrained()
    trained.gate.set()
    components.model_managers["isolation_forest"] = SwappableManager(trained)  # type: ignore[assignment]
    source = build_live_feature_source(components)
    source.observe(TelemetryRecord.obd(datetime.now(UTC).timestamp(), "RPM", 700.0, "rpm"))
    stop = asyncio.Event()
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
    await asyncio.sleep(0.4)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    snap = components.inference_timing.snapshot()
    assert snap.interval_seconds == 0.05
    assert snap.ticks_scheduled >= 3
    assert snap.ticks_completed <= snap.ticks_scheduled
    assert snap.ticks_completed >= 1
    assert snap.lateness.count == snap.ticks_scheduled
    assert {"feature", "isolation_forest", "compute", "apply", "total"} <= set(snap.stages)
    assert not any(t.name.startswith(INFERENCE_WORKER_THREAD_PREFIX) for t in threading.enumerate())


# ---------------------------------------------------------------------------
# C08.11 — replay parity on the committed ground-truth slice
# ---------------------------------------------------------------------------

FIXTURE_SESSION = Path("tests/fixtures/ground_truth/drive-slice")
BINDINGS_PATH = Path("data/bindings.yaml")


def _fixture_records(seconds: float) -> list[TelemetryRecord]:
    """The first ``seconds`` of the committed drive slice as records with
    their *recorded* timestamps (replay re-stamps to wall time, which is the
    one thing a parity test must not do)."""
    frames = [pd.read_parquet(path) for path in sorted(FIXTURE_SESSION.glob("*.parquet"))]
    df = pd.concat(frames).sort_values("timestamp", kind="stable")
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    first = df["timestamp"].iloc[0]
    df = df[df["timestamp"] - first <= pd.Timedelta(seconds=seconds)]
    records: list[TelemetryRecord] = []
    for row in df.itertuples(index=False):
        number = getattr(row, "value_number", None)
        text = getattr(row, "value_text", None)
        value: float | str | None
        if number is not None and not pd.isna(number):
            value = float(number)
        elif isinstance(text, str):
            value = text
        else:
            value = None
        payload = getattr(row, "payload", None)
        unit = getattr(row, "unit", None)
        records.append(
            TelemetryRecord(
                timestamp=row.timestamp.to_pydatetime(),
                source=str(row.source),  # type: ignore[arg-type]
                channel=str(row.channel),
                value=value,
                unit="" if not isinstance(unit, str) else unit,
                payload=bytes(payload) if isinstance(payload, (bytes, bytearray)) else None,
            )
        )
    return records


@pytest.mark.slow
async def test_replay_parity_between_synchronous_path_and_scheduled_pipeline(
    config: CanaryConfig,
) -> None:
    """Same observation stream, same tick times: the pre-C08 synchronous
    ``source.tick(now)`` path and the new pipeline must produce identical
    feature values, gap flags, operating modes and sequence inputs."""
    span_seconds = 90.0
    records = _fixture_records(span_seconds)
    assert len(records) > 1000
    start = records[0].timestamp
    tick_times = [start + timedelta(seconds=k) for k in range(1, int(span_seconds) + 1)]

    old_components = build_components(config)
    old_source = build_live_feature_source(old_components, bindings_path=BINDINGS_PATH)
    new_components = build_components(config)
    new_source = build_live_feature_source(new_components, bindings_path=BINDINGS_PATH)

    new_rows: list[pd.DataFrame | None] = []
    original_tick = new_source.tick

    def capturing_tick(now: datetime) -> Any:
        row = original_tick(now)
        new_rows.append(None if row is None else row.copy())
        return row

    new_source.tick = capturing_tick  # type: ignore[method-assign]
    wall: list[datetime] = [start]
    pipeline = LiveInferencePipeline(
        new_source,
        new_components,
        interval_seconds=1.0,
        recorder=InferenceTimingRecorder(),
        wall_clock=lambda: wall[0],
    )

    old_rows: list[pd.DataFrame | None] = []
    old_points: list[dict[str, Any] | None] = []
    new_points: list[dict[str, Any] | None] = []
    cursor = 0
    try:
        for k, now in enumerate(tick_times):
            while cursor < len(records) and records[cursor].timestamp <= now:
                old_source.observe(records[cursor])
                new_source.observe(records[cursor])
                cursor += 1
            row = old_source.tick(now)
            old_rows.append(None if row is None else row.copy())
            old_points.append(old_source.latest_operating_point)
            wall[0] = now
            await pipeline.run_tick(
                TickSlot(
                    index=k,
                    deadline=float(k),
                    started=float(k),
                    lateness_seconds=0.0,
                    skipped_before=0,
                )
            )
            new_points.append(new_source.latest_operating_point)
    finally:
        await pipeline.close(timeout_seconds=1.0)

    assert len(old_rows) == len(new_rows) == len(tick_times)
    produced = [r for r in old_rows if r is not None]
    assert len(produced) >= 30, "the slice must actually produce feature rows"
    for old, new in zip(old_rows, new_rows, strict=True):
        assert (old is None) == (new is None)
        if old is None or new is None:
            continue
        pd.testing.assert_frame_equal(old, new)
        gap_cols = [c for c in old.columns if str(c).endswith("__gap")]
        assert gap_cols, "gap flags must be present"
        assert old[gap_cols].iloc[0].to_dict() == new[gap_cols].iloc[0].to_dict()
        assert old["operating_mode"].iloc[0] == new["operating_mode"].iloc[0]
        value_cols = [c for c in old.columns if c not in ("timestamp", "session")]
        pd.testing.assert_series_equal(old[value_cols].iloc[0], new[value_cols].iloc[0])
    modes = {r["operating_mode"].iloc[0] for r in produced}
    assert modes, "the segmenter must have classified the slice"
    for old_point, new_point in zip(old_points, new_points, strict=True):
        assert (old_point is None) == (new_point is None)
        if old_point is not None and new_point is not None:
            assert old_point.keys() == new_point.keys()
            for key in old_point:
                a, b = old_point[key], new_point[key]
                if isinstance(a, float) and pd.isna(a):
                    assert pd.isna(b)
                else:
                    assert a == b
