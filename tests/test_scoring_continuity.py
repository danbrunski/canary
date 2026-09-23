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
"""

from __future__ import annotations

import multiprocessing as mp
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

from src.daemon import inference as inference_mod
from src.daemon.inference import (
    LiveInferencePipeline,
    SequenceDetector,
    TickContext,
    _SequenceComputeJob,
    _TickResult,
)
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.daemon.inference_scheduling import GenerationToken
from src.daemon.inference_worker import InferenceWorker, WorkerReply, WorkerTick
from src.models.inference import SequenceWindowBuffer
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
    monkeypatch.setattr(inference_mod, "_build_sequence_engine", lambda *_a, **_k: engine)
    monkeypatch.setattr(inference_mod, "_suppress_channels_for", lambda *_a, **_k: ())
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
    monkeypatch.setattr(inference_mod, "_sequence_observe", lambda e, r: observed.append(r))

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
