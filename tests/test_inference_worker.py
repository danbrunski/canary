"""Out-of-process scoring (:mod:`src.daemon.inference_worker`).

The property that matters is not "the worker runs" but "a slow model can no
longer stall the daemon". Two things have to hold for that: what crosses the
pipe must be small and picklable (a model going back and forth every tick
would cost more than it saves), and a worker that is missing, wedged or
broken must degrade to in-process scoring instead of dropping the tick.
"""

from __future__ import annotations

import multiprocessing as mp
import pickle
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pandas as pd
import pytest

from src.daemon.components import DaemonComponents
from src.daemon.detectors.isolation_forest import IsolationForestDetector
from src.daemon.detectors.sequence import SequenceDetector
from src.daemon.inference import (
    LiveInferencePipeline,
    _IFComputed,
    _TickRequest,
    _TickResult,
)
from src.daemon.inference_worker import InferenceWorker, WorkerReply, WorkerTick


def test_worker_tick_is_picklable_and_carries_no_model() -> None:
    """The request is a row and two version strings — nothing heavier.

    If a model ever ends up on this message it will still *work*, which is
    exactly why it needs a test: it would silently pickle megabytes per tick
    and give back the latency the worker exists to remove.
    """
    row = pd.DataFrame([{"a": 1.0, "b": 2.0}])
    tick = WorkerTick(
        index=7,
        row=row,
        want_if=True,
        want_sequence=True,
        if_version="if_v1",
        sequence_version="seq_v1",
    )

    payload = pickle.dumps(tick)
    restored = pickle.loads(payload)

    assert restored.index == 7
    assert restored.if_version == "if_v1"
    assert restored.sequence_version == "seq_v1"
    pd.testing.assert_frame_equal(restored.row, row)
    # A one-row frame plus two short strings. The IF artifact alone is ~16 MB.
    assert len(payload) < 100_000


def test_if_result_survives_the_process_boundary() -> None:
    """``_IFComputed`` used to carry the engine, which cannot be sent back
    every tick. It is pure data now, and this pins that."""
    computed = _IFComputed(
        model_version="if_v1",
        report=cast(Any, None),
        scored=pd.Series({"score": 0.5}),
        latency_ms=1.5,
        timestamp="2026-09-09T00:00:00Z",
        missing=False,
        mode_str="city",
        calibrated_threshold=0.4,
        scale=1.0,
        score_val=0.5,
        feature_contributors={"obd:RPM": 0.9},
        affected_channels=("obd:RPM",),
    )

    restored = pickle.loads(pickle.dumps(computed))

    assert restored.model_version == "if_v1"
    assert restored.score_val == 0.5
    assert not hasattr(restored, "engine")


def _echo_worker(conn: Any, _config_path: str) -> None:
    """Stand-in child: answers every tick with a fixed, marked result."""
    while True:
        msg = conn.recv()
        if msg is None:
            break
        conn.send(
            WorkerReply(
                index=msg.index,
                if_computed="scored-in-child",
                sequence_result=None,
                durations={"compute": 0.01},
            )
        )
    conn.close()


@dataclass
class _FakeProc:
    _alive: bool = True

    def is_alive(self) -> bool:
        return self._alive

    def join(self, _timeout: float | None = None) -> None:
        self._alive = False

    def terminate(self) -> None:
        self._alive = False

    pid = 1234


def _detached_worker(*, conn: Any, proc: Any) -> InferenceWorker:
    """A worker built through the real constructor, with a substituted pipe
    and process.

    Deliberately not ``__new__``: these tests used to hand-set the private
    attributes, so every new field on the class (D02.3 added three) left a
    half-built object that raised ``AttributeError`` deep inside ``compute``.
    Going through ``__init__`` means a field added later is always initialised.
    """
    worker = InferenceWorker(object())
    worker._conn = conn
    worker._proc = proc
    return worker


def test_compute_returns_a_result_from_a_real_child_process() -> None:
    """End to end over a real pipe and a real second process."""
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe()
    proc = ctx.Process(target=_echo_worker, args=(child_conn, ""), daemon=True)
    proc.start()
    child_conn.close()

    worker = _detached_worker(conn=parent_conn, proc=proc)

    try:
        result = worker.compute(
            WorkerTick(
                index=1,
                row=pd.DataFrame([{"a": 1.0}]),
                want_if=True,
                want_sequence=False,
                if_version="v1",
                sequence_version=None,
            )
        )
        assert isinstance(result, _TickResult)
        assert cast(Any, result.if_computed) == "scored-in-child"
        assert result.durations["compute"] == pytest.approx(0.01)
    finally:
        worker.stop()

    assert not proc.is_alive()


def test_compute_returns_none_when_the_child_is_dead() -> None:
    """A dead worker must hand the tick back, not raise — the pipeline
    scores it in-process instead."""
    worker = _detached_worker(conn=None, proc=_FakeProc(_alive=False))

    assert (
        worker.compute(
            WorkerTick(
                index=1,
                row=None,
                want_if=False,
                want_sequence=False,
                if_version=None,
                sequence_version=None,
            )
        )
        is None
    )


def test_compute_returns_none_when_the_pipe_is_broken() -> None:
    """A closed pipe is a fallback, not a crash."""
    ctx = mp.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe()
    parent_conn.close()
    child_conn.close()

    worker = _detached_worker(conn=parent_conn, proc=_FakeProc(_alive=True))

    assert (
        worker.compute(
            WorkerTick(
                index=1,
                row=None,
                want_if=True,
                want_sequence=False,
                if_version="v1",
                sequence_version=None,
            )
        )
        is None
    )


def test_stop_is_safe_without_a_started_worker() -> None:
    worker = _detached_worker(conn=None, proc=None)
    worker._failures = 0

    worker.stop()  # must not raise


class _StubWorker:
    """A worker that is up but always hands the tick back."""

    def __init__(self) -> None:
        self.calls = 0

    def ready_for(self, _tick: WorkerTick) -> bool:
        return True

    def compute(self, _tick: WorkerTick) -> None:
        self.calls += 1
        return None

    def stop(self) -> None:  # pragma: no cover - not exercised here
        pass


def test_pipeline_scores_in_process_when_the_worker_declines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is the whole safety story: a worker that returns ``None``
    must produce the same result the thread-only path produced, not a
    dropped tick."""
    from src.daemon import inference as inference_mod

    sentinel = _TickResult(
        if_computed=cast(Any, "scored-in-parent"), sequence_result=None, durations={}
    )
    monkeypatch.setattr(inference_mod, "_compute_tick", lambda _request: sentinel)

    pipeline = LiveInferencePipeline.__new__(LiveInferencePipeline)
    worker = _StubWorker()
    pipeline._worker = cast(InferenceWorker, worker)

    class _Detector:
        model_version = "v1"

    pipeline._if = cast(IsolationForestDetector, _Detector())
    pipeline._sequence = cast(SequenceDetector, _Detector())

    request = cast(
        _TickRequest,
        SimpleNamespace(
            slot=SimpleNamespace(index=3),
            if_job=SimpleNamespace(row=pd.DataFrame([{"a": 1.0}])),
            sequence_job=None,
        ),
    )

    result = pipeline._compute(request)

    assert worker.calls == 1
    assert result is sentinel


def test_pipeline_uses_the_worker_result_when_it_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.daemon import inference as inference_mod

    monkeypatch.setattr(
        inference_mod,
        "_compute_tick",
        lambda _request: pytest.fail("must not score in-process when the worker answered"),
    )

    from_child = _TickResult(
        if_computed=cast(Any, "scored-in-child"), sequence_result=None, durations={}
    )

    class _AnsweringWorker:
        def ready_for(self, _tick: WorkerTick) -> bool:
            return True

        def compute(self, _tick: WorkerTick) -> _TickResult:
            return from_child

    pipeline = LiveInferencePipeline.__new__(LiveInferencePipeline)
    pipeline._worker = cast(InferenceWorker, _AnsweringWorker())

    class _Detector:
        model_version = "v1"

    pipeline._if = cast(IsolationForestDetector, _Detector())
    pipeline._sequence = cast(SequenceDetector, _Detector())

    request = cast(
        _TickRequest,
        SimpleNamespace(
            slot=SimpleNamespace(index=4),
            if_job=SimpleNamespace(row=pd.DataFrame([{"a": 1.0}])),
            sequence_job=None,
        ),
    )

    assert pipeline._compute(request) is from_child


def test_worker_is_handed_the_live_scoped_config_not_a_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profiles rewrite every vehicle-specific path under the active slug.

    A worker given a config *path* would re-read the unscoped YAML and look
    for artifacts in ``models/`` instead of ``models/<slug>/`` — loading
    nothing, scoring nothing, and reporting no error. Pin that it receives
    the parent's already-scoped config object.
    """
    from src.daemon import inference as inference_mod

    scoped = SimpleNamespace(
        models=SimpleNamespace(out_of_process_scoring=True),
        daemon=SimpleNamespace(models_dir="models/nissan-xterra-2008"),
    )
    components = cast(DaemonComponents, SimpleNamespace(config=scoped))
    captured: dict[str, Any] = {}

    class _Recorder:
        def __init__(self, config: Any, **kwargs: Any) -> None:
            captured["config"] = config
            captured["kwargs"] = kwargs

        def start(self) -> bool:
            return True

    monkeypatch.setattr(inference_mod, "InferenceWorker", _Recorder)

    worker = inference_mod._start_inference_worker(components)

    assert worker is not None
    assert captured["config"] is scoped
    assert captured["config"].daemon.models_dir == "models/nissan-xterra-2008"


def test_no_worker_is_started_when_the_flag_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.daemon import inference as inference_mod

    monkeypatch.setattr(
        inference_mod,
        "InferenceWorker",
        lambda _config: pytest.fail("must not construct a worker when disabled"),
    )
    components = cast(
        DaemonComponents,
        SimpleNamespace(
            config=SimpleNamespace(models=SimpleNamespace(out_of_process_scoring=False))
        ),
    )

    assert inference_mod._start_inference_worker(components) is None


def test_sequence_result_stays_small_and_plain_across_the_pipe() -> None:
    """The sequence result travels back from the child every scoring tick.

    It is all floats, strings and bounded tuples today. A tensor field added
    later would still pickle — and would silently put torch data on the wire
    every tick — so bound it here rather than trusting review to catch it.
    """
    from src.models.inference import SequenceInferenceResult

    result = SequenceInferenceResult(
        model_version="lstm_v1",
        timestamp="2026-09-09T00:00:00Z",
        session="s",
        window_start_timestamp="2026-09-09T00:00:00Z",
        window_end_timestamp="2026-09-09T00:00:30Z",
        global_score=0.7,
        global_threshold=0.5,
        channel_scores={"obd:RPM": 0.7},
        channel_thresholds={"obd:RPM": 0.5},
        anomalous_channels=("obd:RPM",),
        is_anomaly=True,
        latency_ms=12.0,
        top_channels=("obd:RPM",),
        reconstruction_series={"obd:RPM": (0.1, 0.2, 0.3)},
    )

    payload = pickle.dumps(result)
    restored = pickle.loads(payload)

    assert restored.global_score == 0.7
    assert restored.anomalous_channels == ("obd:RPM",)
    assert len(payload) < 50_000


def test_out_of_process_scoring_ships_off() -> None:
    """The child only sees models loaded from the config's artifact paths.

    A parent holding a model that did not come from disk — an injected fake,
    or any caller populating ``model_managers`` itself — would have the child
    score against something else and report nothing wrong. Defaulting this on
    made every ``run_live_inference`` end-to-end test complete zero ticks.
    Deployments opt in; the default stays off.
    """
    from src.config import ModelsConfig

    assert ModelsConfig.model_fields["out_of_process_scoring"].default is False
