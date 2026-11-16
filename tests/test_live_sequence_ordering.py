"""The live sequence detector scores windows when a score overlaps the next
tick's snapshot (2026-09-24, deploy of ``lstm_autoencoder_2026-09-24T035125``).

On the truck, with ``out_of_process_scoring: true``, the child ran the model
every tick (``stages.sequence`` 217 samples, median 310 ms) while
``sequence_windows.scored`` and ``sequence_envelope.windows`` stayed 0 and no
``lstm_autoencoder`` event was ever emitted. The window was never full.

Cause: ``LiveInferencePipeline.run_tick`` decided whether to queue a tick from
a ``busy`` flag read *before* its snapshot. A score that finished during the
snapshot (the feature build is ~200-300 ms, the score ~300 ms, so this is the
Pi's steady state) had already drained an empty queue, so the tick was parked
in the queue with nothing in flight. The next tick found the worker idle and
dispatched ahead of it, and the parked row was scored after the newer one.
Since A01.3 a row older than the window's last clears the window, so every
other row restarted it and a 30-row window never filled. Before A01.3 the same
reordering was silently stitched into the windows instead.

The tests below drive the real pipeline, the real ``InferenceWorker`` and the
child's own ``_WorkerState`` (every message pickled each way, as across the
pipe) with an ``arrived_sample`` artifact that records a training envelope,
and make every score finish while the next tick is being snapshotted.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from src.daemon import inference as inference_mod
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.daemon.inference_scheduling import TickSlot
from tests.sequence_builders import (
    FEATURES,
    RPM,
    SEQUENCE_LENGTH,
    TICKS,
    child_worker,
    drive,
    ordering_config,
    ordering_observe,
    ordering_pipeline,
    ordering_trained,
)

# --- the artifact: arrived_sample mask + a training envelope, no training ----


# --- the two places a score can run, each held until the test releases it ---


# --- the drive ---------------------------------------------------------------


def _assert_every_window_scored(recorder: InferenceTimingRecorder) -> None:
    snapshot = recorder.snapshot().to_dict()
    # Rows 0..TICKS-1 reach the engine in order, so every row from the
    # SEQUENCE_LENGTH-th on completes a window.
    expected = TICKS - SEQUENCE_LENGTH + 1
    assert snapshot["sequence_windows"]["scored"] == expected
    assert snapshot["sequence_windows"]["unmeasured"] == 0
    envelope = snapshot["sequence_envelope"]
    assert envelope["windows"] == expected
    assert envelope["envelope_recorded"] is True
    assert envelope["inside"] == expected


async def test_a_score_finishing_mid_snapshot_keeps_rows_in_order_in_the_child(
    tmp_path: Path,
) -> None:
    config = ordering_config(tmp_path)
    trained = ordering_trained()
    release = threading.Semaphore(0)
    worker, pipe = child_worker(config, trained, release)
    pipeline, source, recorder = ordering_pipeline(config, trained, worker)
    try:
        await drive(pipeline, source, release)
    finally:
        release.release(TICKS)
        await pipeline.close(timeout_seconds=1.0)

    assert pipe.scored_stamps == sorted(pipe.scored_stamps), "an older row was scored late"
    assert len(pipe.scored_stamps) == TICKS
    _assert_every_window_scored(recorder)
    # The arrival flags reached the child: every row's RPM sample arrived.
    rpm = FEATURES.index(RPM)
    assert pipe.masks and all(
        mask[:, rpm].tolist() == [1.0] * SEQUENCE_LENGTH for mask in pipe.masks
    )
    assert worker.fallbacks == {}


async def test_a_score_finishing_mid_snapshot_keeps_rows_in_order_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ordering_config(tmp_path)
    trained = ordering_trained()
    release = threading.Semaphore(0)
    scored: list[pd.Timestamp] = []
    compute = inference_mod._compute_tick

    def held_compute(request: Any) -> Any:
        assert release.acquire(timeout=5.0), "the test never released this score"
        if request.sequence_job is not None:
            scored.append(request.sequence_job.row["timestamp"].iloc[0])
        return compute(request)

    monkeypatch.setattr(inference_mod, "_compute_tick", held_compute)
    pipeline, source, recorder = ordering_pipeline(config, trained, worker=None)
    try:
        await drive(pipeline, source, release)
    finally:
        release.release(TICKS)
        await pipeline.close(timeout_seconds=1.0)

    assert scored == sorted(scored), "an older row was scored late"
    assert len(scored) == TICKS
    _assert_every_window_scored(recorder)


async def test_the_pipeline_refuses_to_score_a_tick_older_than_the_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop: whatever path hands an older tick to the worker, its row
    never reaches the engine after a newer one. It is a lost tick, counted."""
    config = ordering_config(tmp_path)
    trained = ordering_trained()
    release = threading.Semaphore(0)
    scored: list[int] = []
    compute = inference_mod._compute_tick

    def counted_compute(request: Any) -> Any:
        scored.append(request.slot.index)
        return compute(request)

    monkeypatch.setattr(inference_mod, "_compute_tick", counted_compute)
    pipeline, source, recorder = ordering_pipeline(config, trained, worker=None)
    start = datetime(2026, 9, 24, 16, 0, tzinfo=UTC)
    requests = []
    try:
        for index in (0, 1):
            now = start + timedelta(seconds=index)
            ordering_observe(source, now - timedelta(milliseconds=200))
            pipeline._wall_clock = lambda now=now: now
            requests.append(
                await pipeline._snapshot(
                    TickSlot(
                        index=index,
                        deadline=float(index),
                        started=float(index),
                        lateness_seconds=0.0,
                        skipped_before=0,
                    )
                )
            )
        pipeline._dispatch(requests[1])
        await asyncio.wait_for(pipeline.inflight, timeout=5.0)
        pipeline._dispatch(requests[0])  # older than the tick just scored
        assert pipeline.inflight is not None and pipeline.inflight.done()
    finally:
        release.release(TICKS)
        await pipeline.close(timeout_seconds=1.0)

    assert scored == [1]
    assert recorder.snapshot().to_dict()["missed_by_reason"] == {"compute_busy": 1}
