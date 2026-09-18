"""The capture judge never runs on the daemon's event loop (2026-09-17).

A live ``GET /control/capture/quality`` and the stop that followed each held
the loop ~60 s while ``recompute_capture_quality`` read a 20-minute
``tuning-did`` capture back from Parquet: HTTP went dark, the OBD handoff
dropped 38,600 records, and the stop-time verdict failed on the gap the live
check had caused. These tests pin the fix: every async caller awaits a child
process, the recompute refuses to run inline on a loop, and a child that
cannot answer is a reported failure, never a hang.
"""

from __future__ import annotations

import asyncio
import json
import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool, ProcessPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.analysis import capture_coverage
from src.collection.capture_losses import LossEpoch
from src.control import capture_finalize
from src.control.capture_finalize import (
    CaptureQualityUnavailableError,
    finalize_capture,
    live_capture_quality,
    recompute_quality_off_loop,
)
from src.storage.parquet_writer import write_records_to_parquet
from tests.test_capture_coverage import T0, _rounds, _session


class _Health:
    def capture_quality_inputs(self, *, now: float | None = None) -> Any:
        return SimpleNamespace(lifetime=None, scheduler={}, loss_epoch=self.loss_epoch())

    def loss_epoch(self, *, now: float | None = None) -> LossEpoch:
        return LossEpoch(taken_at_monotonic=1.0, taken_at_wall=T0, counters={"obd.dropped": 0})

    async def wait_for_queues_idle(self, names: Any = None, *, timeout: float) -> dict[str, Any]:
        return {"drained": [], "timed_out": [], "unobserved": []}


class _Writer:
    rows_written = 480

    async def flush(self) -> Path | None:
        return None


class _BrokenExecutor(Executor):
    """A pool whose child died mid-task, as an OOM kill would leave it."""

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        future.set_exception(BrokenProcessPool("child terminated abruptly"))
        return future


def _slow_recompute(seconds: float, calls: list[bool]) -> Callable[..., Any]:
    def fake(session_dir: Path, **_: Any) -> Any:
        try:
            asyncio.get_running_loop()
            calls.append(True)
        except RuntimeError:
            calls.append(False)
        time.sleep(seconds)
        return SimpleNamespace(
            to_dict=lambda: {"verdict": "pass"}, passed=True, verdict="pass", failures=[]
        )

    return fake


async def _ticks_during(awaitable: Any) -> tuple[Any, int]:
    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    try:
        result = await awaitable
    finally:
        done.set()
        await task
    return result, ticks


async def test_stop_finalize_keeps_the_loop_running_while_it_judges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(capture_coverage, "recompute_capture_quality", _slow_recompute(1.0, calls))
    with ThreadPoolExecutor(max_workers=1) as pool:
        payload, ticks = await _ticks_during(
            finalize_capture(
                tmp_path, health=_Health(), writer=_Writer(), start=None, executor=pool
            )
        )
    assert payload == {"verdict": "pass"}
    assert calls == [False], "the recompute ran on the event loop's thread"
    # Inline, the loop would have ticked once or not at all during the 1 s.
    assert ticks >= 20
    assert json.loads((tmp_path / "capture_quality.json").read_text()) == {"verdict": "pass"}


async def test_live_quality_keeps_the_loop_running_while_it_judges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(capture_coverage, "recompute_capture_quality", _slow_recompute(1.0, calls))
    with ThreadPoolExecutor(max_workers=1) as pool:
        quality, ticks = await _ticks_during(
            live_capture_quality(
                tmp_path, health=_Health(), writer=_Writer(), start=None, executor=pool
            )
        )
    assert quality.verdict == "pass"
    assert calls == [False]
    assert ticks >= 20


async def test_equivalent_recomputes_share_one_in_flight_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(capture_coverage, "recompute_capture_quality", _slow_recompute(0.1, calls))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = await asyncio.gather(
            recompute_quality_off_loop(tmp_path, executor=pool),
            recompute_quality_off_loop(tmp_path, executor=pool),
        )
    assert first is second
    assert calls == [False]


async def test_distinct_recomputes_do_not_compete_for_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = 0
    high_water = 0
    lock = threading.Lock()

    def tracked(_session_dir: Path, **_: Any) -> Any:
        nonlocal active, high_water
        with lock:
            active += 1
            high_water = max(high_water, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return SimpleNamespace(to_dict=dict, passed=True, verdict="pass", failures=[])

    monkeypatch.setattr(capture_coverage, "recompute_capture_quality", tracked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        await asyncio.gather(
            recompute_quality_off_loop(tmp_path / "one", executor=pool),
            recompute_quality_off_loop(tmp_path / "two", executor=pool),
        )
    assert high_water == 1


async def test_the_recompute_refuses_to_run_inline_on_a_loop(tmp_path: Path) -> None:
    session = _session(tmp_path, _rounds(8))
    with pytest.raises(RuntimeError, match="must not run on an event loop"):
        capture_coverage.recompute_capture_quality(session)


async def test_the_real_child_process_scores_like_the_inline_recompute(tmp_path: Path) -> None:
    session = _session(tmp_path, _rounds(240))
    expected = await asyncio.to_thread(
        capture_coverage.recompute_capture_quality, session, ended_at=T0 + 60
    )
    quality = await recompute_quality_off_loop(session, ended_at=T0 + 60)
    assert quality.to_dict() == expected.to_dict()
    assert quality.tuples["complete"] == 240
    assert quality.verdict == expected.verdict and quality.failures == expected.failures


async def test_finalize_through_the_real_child_writes_the_sidecar(tmp_path: Path) -> None:
    session = _session(tmp_path, _rounds(240))
    payload = await finalize_capture(session, health=_Health(), writer=_Writer(), start=None)
    assert payload is not None
    assert payload["scope"] == "stop"
    assert payload["tuples"]["complete"] == 240
    assert json.loads((session / "capture_quality.json").read_text()) == json.loads(
        json.dumps(payload)
    )


async def test_the_recompute_refusal_crosses_the_process_boundary(tmp_path: Path) -> None:
    plain = tmp_path / "2026-08-05T142720"
    write_records_to_parquet(_rounds(4), plain)
    with pytest.raises(ValueError, match="not a capture session"):
        await recompute_quality_off_loop(plain)


async def test_a_dead_child_is_reported_not_hung(tmp_path: Path) -> None:
    with pytest.raises(CaptureQualityUnavailableError, match="process failed"):
        await recompute_quality_off_loop(tmp_path, executor=_BrokenExecutor())
    # The stop path turns it into "no verdict", never an exception.
    assert (
        await finalize_capture(
            tmp_path, health=_Health(), writer=_Writer(), start=None, executor=_BrokenExecutor()
        )
        is None
    )


async def test_a_wedged_child_times_out_and_is_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path, _rounds(240))
    pools: list[ProcessPoolExecutor] = []
    spawn = capture_finalize._spawn_pool

    def tracked() -> ProcessPoolExecutor:
        pool = spawn()
        pools.append(pool)
        return pool

    monkeypatch.setattr(capture_finalize, "_spawn_pool", tracked)
    processes: list[Any] = []

    async def watch() -> None:
        while not processes:
            if pools and pools[0]._processes:
                processes.extend(pools[0]._processes.values())
            await asyncio.sleep(0.001)

    watcher = asyncio.create_task(watch())
    with pytest.raises(CaptureQualityUnavailableError, match="did not finish"):
        await recompute_quality_off_loop(session, timeout=0.05)
    watcher.cancel()
    assert processes, "no child was spawned"
    for proc in processes:
        proc.join(timeout=10)
        # Killed, not left to finish on its own (a clean worker exit is 0).
        assert proc.exitcode == -signal.SIGTERM
