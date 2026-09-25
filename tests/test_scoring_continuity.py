"""A01 — live scoring never mixes ticks or windows that do not belong together.

Each test pins one defect found by the 2026-09-22 detector review
(``data/analyses/2026-09-22-anomaly-detector-review.md``):

- A01.1 — after one worker timeout, every later tick received the *previous*
  tick's result, because the late reply stayed in the pipe and nothing
  compared its index with the tick being waited on.
- A01.2/A01.3 — the sequence window survived stops, stale stretches and
  replays (live rows are always session ``"live"``), and the in-process
  fallback scored windows stitched from rows hours apart. Feeding a drive's
  tail and then the next drive 20 minutes later flagged 11 of 29 straddling
  windows.
- A01.4 — replay start/stop did not reset the window.
- A01.5 — serving could suppress columns the artifact was trained and
  calibrated with, and nothing said so.
- A01.6 — a model load in the child (2-7 s) ran against a ~3 s reply
  timeout, so every hot-swap was a timeout.
- A01.7 — nothing measured how much wall time a scored window covers.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

from src.daemon import inference as inference_mod
from src.daemon.detectors import sequence as sequence_mod
from src.daemon.inference import (
    LiveInferencePipeline,
    SequenceDetector,
    TickContext,
    _SequenceComputeJob,
    _TickResult,
)
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.daemon.inference_scheduling import GenerationToken
from src.daemon.inference_worker import (
    InferenceWorker,
    WorkerReady,
    WorkerReply,
    WorkerSwap,
    WorkerTick,
)
from src.models.inference import (
    TRAINING_SUPPRESS_SCHEMA_KEY,
    SequenceWindowBuffer,
    compare_suppression,
    training_suppress_record,
)
from src.models.sequence_dataset import WINDOW_MAX_GAP_SECONDS, build_sequence_windows
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN

# --- A01.1: a reply is applied only to the tick that asked for it ----------


class _AliveProc:
    pid = 4321

    def is_alive(self) -> bool:
        return True


def _tick(index: int) -> WorkerTick:
    return WorkerTick(
        index=index,
        row=None,
        want_if=True,
        want_sequence=False,
        if_version="v1",
        sequence_version=None,
    )


def _reply(index: int) -> WorkerReply:
    return WorkerReply(index=index, if_computed=f"result-for-{index}", durations={})


def _fast_worker(conn: Any) -> InferenceWorker:
    worker = InferenceWorker(object())
    worker._conn = conn
    worker._proc = _AliveProc()
    worker._answered_once = True  # past the startup allowance
    worker._result_timeout_seconds = 0.2
    return worker


def test_a_late_reply_is_never_applied_to_the_next_tick() -> None:
    parent, child = mp.Pipe()
    worker = _fast_worker(parent)

    # Tick 0: the child is slow, so the parent gives up and falls back.
    assert worker.compute(_tick(0)) is None
    assert worker.fallbacks == {"timeout": 1}

    # The child answers tick 0 late, then answers tick 1 promptly.
    child.send(_reply(0))
    child.send(_reply(1))

    result = worker.compute(_tick(1))

    assert isinstance(result, _TickResult)
    assert result.if_computed == "result-for-1"  # not tick 0's
    assert worker.stale_replies == 1


def test_a_late_reply_alone_is_a_timeout_not_a_result() -> None:
    parent, child = mp.Pipe()
    worker = _fast_worker(parent)
    assert worker.compute(_tick(0)) is None

    child.send(_reply(0))  # only the stale answer ever arrives

    assert worker.compute(_tick(1)) is None
    assert worker.stale_replies == 1
    assert worker.fallbacks == {"timeout": 2}


def test_discarded_late_replies_reach_the_timing_snapshot() -> None:
    recorder = InferenceTimingRecorder()
    recorder.set_worker_stale_replies(3)

    assert recorder.snapshot().to_dict()["worker_stale_replies"] == 3


# --- A01.2/A01.3: a window never spans a long gap ---------------------------


def _live_row(seconds: float, *, rpm: float = 1000.0) -> dict[str, Any]:
    stamp = pd.Timestamp("2026-09-22T12:00:00Z") + pd.Timedelta(seconds=seconds)
    return {SESSION_COLUMN: "live", TIMESTAMP_COLUMN: stamp, "rpm": rpm, "speed": 40.0}


def test_a_live_window_never_straddles_a_stop() -> None:
    """Live rows all carry session ``"live"``, so only time can separate two
    drives. The tail of one and the start of the next, 20 minutes apart, must
    not form a window."""
    buffer = SequenceWindowBuffer(feature_columns=("rpm", "speed"), sequence_length=3)
    for second in (0, 1, 2):
        ready = buffer.append(_live_row(second))
    assert ready is True

    assert buffer.append(_live_row(1200)) is False
    assert buffer.append(_live_row(1201)) is False
    assert buffer.append(_live_row(1202)) is True
    start, _end, _session = buffer.metadata()
    assert start.startswith("2026-09-22T12:20:00")


def test_a_short_gap_keeps_the_window() -> None:
    """A dropped tick or two is not a discontinuity."""
    buffer = SequenceWindowBuffer(feature_columns=("rpm", "speed"), sequence_length=3)
    buffer.append(_live_row(0))
    buffer.append(_live_row(1))

    assert buffer.append(_live_row(1 + WINDOW_MAX_GAP_SECONDS)) is True


def test_time_going_backwards_restarts_the_window() -> None:
    """A replay's rows come from an earlier drive."""
    buffer = SequenceWindowBuffer(feature_columns=("rpm", "speed"), sequence_length=2)
    buffer.append(_live_row(100))
    assert buffer.append(_live_row(101)) is True

    assert buffer.append(_live_row(5)) is False


def test_a_fallback_engine_idle_since_long_ago_scores_nothing() -> None:
    """A01.2: the parent engine only sees fallback ticks. After one timeout,
    its last row is from the previous fallback run, so it must restart rather
    than stitch a window across the gap."""
    buffer = SequenceWindowBuffer(feature_columns=("rpm", "speed"), sequence_length=3)
    for second in (0, 1, 2):
        buffer.append(_live_row(second))

    # One timeout an hour later: a single row, no window.
    assert buffer.append(_live_row(3600)) is False


def test_offline_windows_and_live_delta_restart_at_the_same_gap() -> None:
    """Offline windows split where the live buffer clears, and Δt restarts
    there on both sides, so a model is calibrated on the windows it serves."""
    seconds = [0, 1, 2, 3, 20, 21, 22, 23]
    stale = [False, True, True, False, True, False, True, True]
    frame = pd.DataFrame(
        {
            SESSION_COLUMN: ["s"] * len(seconds),
            TIMESTAMP_COLUMN: [
                pd.Timestamp("2026-09-22T12:00:00Z") + pd.Timedelta(seconds=s) for s in seconds
            ],
            "rpm": [800.0 + s for s in seconds],
            "rpm__gap": stale,
        }
    )

    built = build_sequence_windows(frame, sequence_length=3, feature_columns=("rpm",))

    spans = [(w.start_row, w.end_row) for w in built.windows]
    assert spans == [(0, 3), (1, 4), (4, 7), (5, 8)]  # none crosses rows 3→4

    buffer = SequenceWindowBuffer(feature_columns=("rpm",), sequence_length=3)
    live_windows = []
    for position, row in frame.iterrows():
        if buffer.append(row.to_dict()):
            live_windows.append((int(position), buffer.delta_array(), buffer.mask_array()))

    assert [end + 1 for end, _, _ in live_windows] == [end for _, end in spans]
    for (end, live_delta, live_mask), (start, stop) in zip(live_windows, spans, strict=True):
        assert end + 1 == stop
        np.testing.assert_allclose(live_delta[:, 0], built.delta[start:stop, 0])
        np.testing.assert_allclose(live_mask[:, 0], built.mask[start:stop, 0])


# --- A01.3/A01.4: a generation change resets the window ---------------------


class _FakeEngine:
    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1


def _detector(monkeypatch: Any, engine: _FakeEngine) -> SequenceDetector:
    monkeypatch.setattr(sequence_mod, "_build_sequence_engine", lambda *_a, **_k: engine)
    monkeypatch.setattr(sequence_mod, "_suppress_channels_for", lambda *_a, **_k: ())
    loaded = SimpleNamespace(serving_version="seq-v1")
    components = SimpleNamespace(
        sequence_model_type="lstm_autoencoder",
        model_managers={"lstm_autoencoder": SimpleNamespace(current=loaded)},
    )
    return SequenceDetector(components)  # type: ignore[arg-type]


def _ctx() -> TickContext:
    return TickContext(
        now=pd.Timestamp("2026-09-22T12:00:00Z").to_pydatetime(),
        operating_mode="city",
        timestamp_str="2026-09-22T12:00:00Z",
        scoring_allowed=True,
    )


def _token(*, replay_id: str | None = None) -> GenerationToken:
    return GenerationToken(
        replay_id=replay_id,
        capture_id=None,
        model_versions=(("lstm_autoencoder", "seq-v1"),),
    )


def test_a_generation_change_asks_for_a_window_reset(monkeypatch) -> None:
    detector = _detector(monkeypatch, _FakeEngine())
    row = pd.DataFrame([{"rpm": 1.0}])

    first = detector.prepare(row, _ctx(), generation=_token())
    same = detector.prepare(row, _ctx(), generation=_token())
    replay = detector.prepare(row, _ctx(), generation=_token(replay_id="replay-1"))
    back = detector.prepare(row, _ctx(), generation=_token())

    assert first is not None and first.reset is False
    assert same is not None and same.reset is False
    assert replay is not None and replay.reset is True  # replay started
    assert back is not None and back.reset is True  # replay stopped


def test_in_process_compute_clears_the_window_when_asked(monkeypatch) -> None:
    engine = _FakeEngine()
    observed: list[Any] = []
    monkeypatch.setattr(sequence_mod, "_sequence_observe", lambda e, r: observed.append(r))

    SequenceDetector.compute(_SequenceComputeJob(engine=engine, row="row-a", reset=False))
    SequenceDetector.compute(_SequenceComputeJob(engine=engine, row="row-b", reset=True))

    assert engine.resets == 1
    assert observed == ["row-a", "row-b"]


def test_the_reset_rides_to_the_scoring_child(monkeypatch) -> None:
    pipeline = LiveInferencePipeline.__new__(LiveInferencePipeline)
    pipeline._if = SimpleNamespace(model_version="if-v1")
    pipeline._sequence = SimpleNamespace(model_version="seq-v1")
    request = SimpleNamespace(
        slot=SimpleNamespace(index=9),
        if_job=None,
        sequence_job=_SequenceComputeJob(engine=None, row="row", reset=True),
    )

    tick = pipeline._worker_tick(request)  # type: ignore[arg-type]

    assert tick.want_sequence is True
    assert tick.reset_sequence is True


# --- A01.5: the suppress list training ran with is recorded and compared ----


def _trained_with_schema(schema: dict[str, Any]) -> Any:
    manifest = SimpleNamespace(model_version="seq-v1", schema=schema)
    return SimpleNamespace(manifest=manifest)


def test_an_unrecorded_suppress_list_still_names_the_trained_columns_serving_drops() -> None:
    """The 2026-09-20 artifact predates the record, but training drops every
    column it suppresses, so a suppressed column still in the schema was
    trained and calibrated with — serving leaving it out is the difference."""
    trained = _trained_with_schema({"feature_columns": ["rpm", "obd:FUEL_LEVEL"]})

    comparison = compare_suppression(
        trained, serving=("obd:FUEL_LEVEL",), suppressed_columns=("obd:FUEL_LEVEL",)
    )

    assert comparison.training is None
    assert comparison.differs is True
    payload = comparison.to_dict()
    assert payload["recorded_at_training"] is False
    assert payload["added_at_serve"] is None  # unknown, not "nothing"
    assert payload["trained_columns_suppressed_at_serve"] == ["obd:FUEL_LEVEL"]


def test_a_recorded_suppress_list_names_what_serving_added_and_dropped() -> None:
    trained = _trained_with_schema(
        {
            "feature_columns": ["rpm", "obd:HVAC"],
            TRAINING_SUPPRESS_SCHEMA_KEY: training_suppress_record(
                ("obd:FUEL_LEVEL",), ("obd:FUEL_LEVEL", "obd:FUEL_LEVEL__mean")
            ),
        }
    )

    same = compare_suppression(trained, serving=("obd:FUEL_LEVEL",), suppressed_columns=())
    changed = compare_suppression(trained, serving=("obd:HVAC",), suppressed_columns=("obd:HVAC",))

    assert same.differs is False
    assert changed.differs is True
    assert changed.added_at_serve == ("obd:HVAC",)
    assert changed.dropped_at_serve == ("obd:FUEL_LEVEL",)
    assert changed.trained_columns_suppressed_at_serve == ("obd:HVAC",)


def test_the_suppress_comparison_reaches_status_when_the_engine_loads(monkeypatch) -> None:
    """Named at load on ``/status``, not only in a log line."""
    from src.models.health import HealthMonitor

    engine = _FakeEngine()
    trained = _trained_with_schema({"feature_columns": ["rpm", "obd:FUEL_LEVEL"]})
    engine.suppression = compare_suppression(  # type: ignore[attr-defined]
        trained, serving=("obd:FUEL_LEVEL",), suppressed_columns=("obd:FUEL_LEVEL",)
    )
    detector = _detector(monkeypatch, engine)
    health = HealthMonitor()
    detector._components.health_monitor = health  # type: ignore[attr-defined]

    detector.prepare(pd.DataFrame([{"rpm": 1.0}]), _ctx(), generation=_token())

    block = health.snapshot()["sequence_suppression"]
    assert block["model_version"] == "seq-v1"
    assert block["differs"] is True
    assert block["trained_columns_suppressed_at_serve"] == ["obd:FUEL_LEVEL"]


# --- A01.6: a model load in the child is "not ready", never a timeout -------


def _versioned_tick(index: int, *, sequence_version: str) -> WorkerTick:
    return WorkerTick(
        index=index,
        row=None,
        want_if=True,
        want_sequence=True,
        if_version="if-v1",
        sequence_version=sequence_version,
    )


def _tracked_worker(conn: Any) -> InferenceWorker:
    worker = _fast_worker(conn)
    worker._child_versions = ("if-v1", "seq-v1")
    return worker


def test_a_hot_swap_holds_ticks_back_instead_of_timing_out() -> None:
    parent, child = mp.Pipe()
    worker = _tracked_worker(parent)

    assert worker.ready_for(_versioned_tick(0, sequence_version="seq-v1")) is True

    # The parent now serves seq-v2: the child is asked to load it, not handed a
    # tick it would need 2-7 s to answer.
    assert worker.ready_for(_versioned_tick(1, sequence_version="seq-v2")) is False
    request = child.recv()
    assert isinstance(request, WorkerSwap)
    assert request.sequence_version == "seq-v2"
    # Still loading: not ready, and asked only once.
    assert worker.ready_for(_versioned_tick(2, sequence_version="seq-v2")) is False
    assert child.poll(0) is False

    child.send(
        WorkerReady(
            request_id=request.request_id,
            if_version="if-v1",
            sequence_version="seq-v2",
            load_seconds=4.2,
        )
    )
    assert worker.ready_for(_versioned_tick(3, sequence_version="seq-v2")) is True

    child.send(_reply(3))
    result = worker.compute(_versioned_tick(3, sequence_version="seq-v2"))

    assert isinstance(result, _TickResult)
    assert result.if_computed == "result-for-3"
    assert worker.fallbacks == {}  # the swap window was never a timeout
    assert worker.stale_replies == 0
    swaps = worker.swaps
    assert swaps["in_progress"] is False
    assert swaps["completed"] == 1
    assert swaps["not_ready_ticks"] == 2
    assert swaps["last_load_seconds"] == 4.2
    assert swaps["last_swap_seconds"] is not None


def test_a_child_that_loaded_another_version_is_asked_again_later() -> None:
    parent, child = mp.Pipe()
    worker = _tracked_worker(parent)
    assert worker.ready_for(_versioned_tick(0, sequence_version="seq-v2")) is False
    request = child.recv()

    # A manifest landed between the two processes' polls: it holds seq-v3.
    child.send(WorkerReady(request.request_id, "if-v1", "seq-v3", load_seconds=3.0))
    assert worker.ready_for(_versioned_tick(1, sequence_version="seq-v2")) is False
    assert worker.swaps["version_mismatches"] == 1
    assert child.poll(0) is False  # not re-asked every tick

    worker._swap_retry_after = 0.0  # the retry interval has passed
    assert worker.ready_for(_versioned_tick(2, sequence_version="seq-v2")) is False
    assert isinstance(child.recv(), WorkerSwap)


def test_a_late_load_acknowledgement_is_not_a_bad_reply() -> None:
    parent, child = mp.Pipe()
    worker = _tracked_worker(parent)
    child.send(WorkerReady(99, "if-v1", "seq-v1", load_seconds=1.0))
    child.send(_reply(5))

    result = worker.compute(_versioned_tick(5, sequence_version="seq-v1"))

    assert isinstance(result, _TickResult)
    assert result.if_computed == "result-for-5"
    assert worker.fallbacks == {}


def test_the_pipeline_scores_the_forest_but_not_the_sequence_while_the_child_loads(
    monkeypatch,
) -> None:
    captured: list[Any] = []
    monkeypatch.setattr(
        inference_mod,
        "_compute_tick",
        lambda request: captured.append(request) or _TickResult(None, None, {}),
    )

    class _LoadingWorker:
        def ready_for(self, _tick: WorkerTick) -> bool:
            return False

        def compute(self, _tick: WorkerTick) -> None:  # pragma: no cover - must not run
            raise AssertionError("a loading child must not be sent a tick")

    engine = _FakeEngine()
    pipeline = LiveInferencePipeline.__new__(LiveInferencePipeline)
    pipeline._worker = _LoadingWorker()  # type: ignore[assignment]
    pipeline._if = SimpleNamespace(model_version="if-v1")  # type: ignore[assignment]
    pipeline._sequence = SimpleNamespace(model_version="seq-v2")  # type: ignore[assignment]
    request = inference_mod._TickRequest(
        slot=SimpleNamespace(index=4),  # type: ignore[arg-type]
        ctx=_ctx(),
        token=_token(),
        if_job=inference_mod._IFComputeJob(loaded=None, row="row"),
        sequence_job=_SequenceComputeJob(engine=engine, row="row", reset=True),
        behavior_residuals=(),
        snapshot_started=0.0,
        snapshot_seconds=0.0,
        snapshot_loop_seconds=0.0,
    )

    pipeline._compute(request)

    assert len(captured) == 1
    assert captured[0].if_job is request.if_job  # the forest still scores here
    assert captured[0].sequence_job is None  # the sequence detector is not ready
    assert engine.resets == 1  # and a window never spans the rows it missed


class _FakeManager:
    def __init__(self, *versions: str) -> None:
        self._versions = list(versions)
        self.polls = 0
        self.current = SimpleNamespace(serving_version=self._versions.pop(0))

    def poll(self) -> None:
        self.polls += 1
        if self._versions:
            self.current = SimpleNamespace(serving_version=self._versions.pop(0))


def test_the_child_loads_then_acknowledges_a_swap(monkeypatch) -> None:
    from src.daemon.inference_worker import _WorkerState

    built: list[_FakeEngine] = []

    def build(*_a: Any, **_k: Any) -> _FakeEngine:
        built.append(_FakeEngine())
        return built[-1]

    monkeypatch.setattr(sequence_mod, "_build_sequence_engine", build)
    monkeypatch.setattr(sequence_mod, "_suppress_channels_for", lambda *_a, **_k: ())
    forest = _FakeManager("if-v1")
    sequence = _FakeManager("seq-v1", "seq-v2")
    state = _WorkerState({"isolation_forest": forest, "lstm_autoencoder": sequence}, object())

    startup = state.ready(0, load_seconds=2.5)
    ready = state.swap(WorkerSwap(request_id=7, if_version="if-v1", sequence_version="seq-v2"))

    assert (startup.if_version, startup.sequence_version) == ("if-v1", "seq-v1")
    assert ready.request_id == 7
    assert (ready.if_version, ready.sequence_version) == ("if-v1", "seq-v2")
    assert ready.error is None
    assert forest.polls == 0 and sequence.polls == 1
    assert len(built) == 2  # the new version got its own engine before the ack

    # A swap of the forest alone still clears the window: the parent sent no
    # sequence rows while the child was loading.
    before = built[-1].resets
    state.swap(WorkerSwap(request_id=8, if_version="if-v1", sequence_version="seq-v2"))
    assert len(built) == 2  # same sequence model: no rebuild
    assert built[-1].resets == before + 1


def test_a_started_child_acknowledges_readiness(tmp_path) -> None:
    """End to end over a real spawned child: the startup load is announced,
    and until it is the parent sends the child nothing."""
    import time

    from src.config import CanaryConfig

    config = CanaryConfig.from_yaml(Path(__file__).resolve().parents[1] / "canary.example.yaml")
    config.daemon.models_dir = tmp_path  # nothing to load: a fast, honest start
    worker = InferenceWorker(config)
    assert worker.start() is True
    tick = WorkerTick(
        index=0,
        row=None,
        want_if=False,
        want_sequence=False,
        if_version=None,
        sequence_version=None,
    )
    try:
        deadline = time.monotonic() + 120.0
        while not worker.ready_for(tick):
            assert time.monotonic() < deadline, "the child never acknowledged its startup"
            time.sleep(0.05)
        swaps = worker.swaps
        assert swaps["in_progress"] is False
        assert swaps["startup_seconds"] is not None
        assert swaps["last_load_seconds"] is not None
        assert worker.fallbacks == {}
    finally:
        worker.stop()


def test_swap_counters_reach_the_timing_snapshot() -> None:
    recorder = InferenceTimingRecorder()
    recorder.set_worker_swaps({"completed": 1, "last_swap_seconds": 4.2})

    assert recorder.snapshot().to_dict()["worker_swaps"]["last_swap_seconds"] == 4.2


# --- A01.7: a scored window says how much wall time its rows cover -----------


def test_a_window_with_a_dropped_tick_reports_its_real_span() -> None:
    buffer = SequenceWindowBuffer(feature_columns=("rpm", "speed"), sequence_length=4)
    for second in (0, 1, 3, 4):  # tick 2 dropped under load
        buffer.append(_live_row(second))

    assert buffer.span() == (4.0, 2.0)


def test_stretched_windows_are_counted_against_the_tick_period() -> None:
    recorder = InferenceTimingRecorder()
    recorder.interval_seconds = 1.0

    recorder.note_sequence_window(rows=30, span_seconds=29.0, max_step_seconds=1.0)
    recorder.note_sequence_window(rows=30, span_seconds=31.0, max_step_seconds=3.0)
    recorder.note_sequence_window(rows=30, span_seconds=48.0, max_step_seconds=4.0)
    recorder.note_sequence_window(rows=30, span_seconds=None, max_step_seconds=None)

    windows = recorder.snapshot().to_dict()["sequence_windows"]
    assert windows["scored"] == 3
    assert windows["stretched"] == 1  # 48 s over a nominal 29 s
    assert windows["unmeasured"] == 1  # no timestamps is not a clean window
    assert windows["stretch_threshold"] == 1.5
    assert windows["span_ratio"]["max"] == round(48.0 / 29.0, 3)
    assert windows["max_step_seconds"]["max"] == 4.0


def test_applying_a_sequence_result_records_its_window_span() -> None:
    recorder = InferenceTimingRecorder()
    recorder.interval_seconds = 1.0
    pipeline = LiveInferencePipeline.__new__(LiveInferencePipeline)
    pipeline._recorder = recorder
    pipeline._components = SimpleNamespace(  # type: ignore[assignment]
        replay_controller=None,
        capture_controller=None,
        model_managers={"lstm_autoencoder": SimpleNamespace(current=None)},
    )
    pipeline._if = SimpleNamespace(apply=lambda *_a: [])  # type: ignore[assignment]
    pipeline._sequence = SimpleNamespace(apply=lambda *_a: [])  # type: ignore[assignment]
    pipeline._behavior = SimpleNamespace(apply=lambda *_a: [])  # type: ignore[assignment]
    pipeline._fuse = lambda *_a: None  # type: ignore[method-assign]
    request = SimpleNamespace(
        token=GenerationToken(
            replay_id=None, capture_id=None, model_versions=(("lstm_autoencoder", None),)
        ),
        ctx=_ctx(),
        behavior_residuals=(),
        snapshot_loop_seconds=0.0,
        snapshot_started=0.0,
    )
    result = _TickResult(
        if_computed=None,
        sequence_result=SimpleNamespace(
            window_rows=30, window_span_seconds=58.0, window_max_step_seconds=5.0
        ),
        durations={},
    )

    pipeline._apply(request, result)  # type: ignore[arg-type]

    windows = recorder.snapshot().to_dict()["sequence_windows"]
    assert windows["scored"] == 1
    assert windows["stretched"] == 1
