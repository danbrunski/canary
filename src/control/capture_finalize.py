"""Capture finalization: boundary, drain, flush, then judge (roadmap C03.8).

The stop route used to score the trailing scheduler window and write the
sidecar *before* the writer rotated — so the verdict never knew whether the
last records made it to disk, and a flush failure at stop left a passing
sidecar beside a truncated session. The order here is the point:

1. **Boundary** — bump the capture generation. Readings issued afterwards
   carry the next generation and cannot complete a tuple in this capture.
2. **Drain** — wait, bounded, for the ingest and storage queues to be fully
   consumed, so every record the collector handed off has met the writer.
3. **Flush** — push the writer's buffer to Parquet. A failure here is
   recorded, not swallowed: the sidecar then says ``persistence_loss``.
4. **Judge** — read the rows back, count tuples per round, reconcile the
   loss-counter epochs, and only then write ``capture_quality.json``.

``live_capture_quality`` is the same arithmetic without the boundary: it
flushes and reads what is on disk *so far*, which is what the arm-time
verification and the MCP tool report. Both go through
``recompute_capture_quality`` so a live, a stop-time and a recomputed
verdict cannot disagree about what a complete tuple is.

**The judge never runs on the event loop.** On 2026-09-17 a 20-minute
``tuning-did`` capture was checked live and then stopped: each recompute read
every persisted row back and held the daemon's loop for ~60 s — HTTP went
dark, the ``canable-obd`` handoff dropped 38,600 records, inference overran 54
deadlines, and the stop-time verdict failed ``acquisition_gap`` on the ~49 s
hole the *live* check had just punched in the data. A worker thread is not
enough (the pandas work holds the GIL, the D01.4/inference-worker lesson), so
every async caller goes through :func:`recompute_quality_off_loop`, which runs
it in a spawned child process; ``recompute_capture_quality`` itself refuses to
run on a thread with a running event loop.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import multiprocessing
import threading
import time
import weakref
from collections.abc import Mapping
from concurrent.futures import BrokenExecutor, Executor, ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from src.collection import capture_generation
from src.collection.capture_losses import (
    LossEpoch,
    reconcile_losses,
    summarize_losses,
)
from src.collection.capture_quality import (
    SCOPE_LIVE,
    SCOPE_RECOMPUTE,
    SCOPE_STOP,
    CaptureQuality,
)
from src.control.capture_manifest import (
    read_capture_epoch,
    write_capture_epoch,
    write_capture_quality,
)

log = logging.getLogger(__name__)

#: How long a stop waits for the ingest and storage queues to empty. A
#: healthy daemon drains a few hundred records in well under a second; a
#: wedged sink is reported as ``drain_timed_out``, never waited on forever.
DRAIN_TIMEOUT_SECONDS = 5.0

#: Ceiling on one recompute in the child. A 20-minute ``tuning-did`` capture
#: took ~60 s on the Pi (2026-09-17); this allows captures several times that
#: long, and exists only so a wedged child cannot hold a stop forever.
RECOMPUTE_TIMEOUT_SECONDS = 600.0


@dataclass
class _RecomputeLoopState:
    """One daemon-loop coordinator for memory-heavy capture judges."""

    limit: asyncio.Semaphore
    in_flight: dict[tuple[Any, ...], asyncio.Task[CaptureQuality]]


_recompute_states_lock = threading.Lock()
_recompute_states: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _RecomputeLoopState] = (
    weakref.WeakKeyDictionary()
)


class CaptureQualityUnavailableError(RuntimeError):
    """The recompute produced no answer: the child failed to start, died, or
    timed out. Distinct from the recompute's own refusals (``ValueError``),
    which cross the process boundary unchanged."""


class _Health(Protocol):
    def capture_quality_inputs(self, *, now: float | None = None) -> Any: ...

    def loss_epoch(self, *, now: float | None = None) -> LossEpoch: ...

    async def wait_for_queues_idle(self, names: Any = ..., *, timeout: float) -> dict[str, Any]: ...


class _Writer(Protocol):
    @property
    def rows_written(self) -> int: ...

    async def flush(self) -> Path | None: ...


@dataclass(frozen=True)
class StartEpoch:
    """What the arm wrote down for the stop to reconcile against."""

    loss_epoch: LossEpoch
    generation: int
    started_wall: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generation": self.generation,
            "started_wall": round(self.started_wall, 3),
            "loss_epoch": self.loss_epoch.to_dict(),
        }


def record_capture_start(session_dir: Path, health: _Health | None) -> StartEpoch | None:
    """Snapshot the loss counters at arm and persist them beside the manifest."""
    if health is None:
        return None
    epoch = StartEpoch(
        loss_epoch=health.loss_epoch(),
        generation=capture_generation.current(),
        started_wall=time.time(),
    )
    try:
        write_capture_epoch(session_dir, epoch.to_dict())
    except OSError:
        log.exception("failed to write capture epoch sidecar for %s", session_dir)
    return epoch


def _start_epoch_from_disk(session_dir: Path) -> LossEpoch | None:
    payload = read_capture_epoch(session_dir)
    if payload is None:
        return None
    raw = payload.get("loss_epoch")
    return LossEpoch.from_dict(raw) if isinstance(raw, Mapping) else None


def losses_since(
    session_dir: Path, start: StartEpoch | None, stop: LossEpoch | None
) -> dict[str, Any]:
    """Reconcile arm-time and stop-time epochs; a missing side reads as unknown.

    The arm epoch comes from memory when this process armed the capture, else
    from ``capture_epoch.json`` — so a daemon restarted mid-capture still
    reconciles against the counters as they were at arm, or says it cannot.
    """
    start_epoch = start.loss_epoch if start is not None else _start_epoch_from_disk(session_dir)
    return summarize_losses(reconcile_losses(start_epoch, stop))


async def recompute_quality_off_loop(  # noqa: PLR0913
    session_dir: Path,
    *,
    lifetime: Mapping[str, Any] | None = None,
    losses: Mapping[str, Any] | None = None,
    persistence: Mapping[str, Any] | None = None,
    scheduler: Mapping[str, Any] | None = None,
    scope: str = SCOPE_RECOMPUTE,
    ended_at: float | None = None,
    executor: Executor | None = None,
    timeout: float = RECOMPUTE_TIMEOUT_SECONDS,
) -> CaptureQuality:
    """``recompute_capture_quality`` in a child process, awaited without
    holding the loop — the only way an async caller may score a capture.

    With no ``executor`` a single-use ``spawn`` pool is created and torn down
    per call (``spawn`` for the same reason as the inference worker: forking
    a process running asyncio plus threads inherits their locks), so a large
    capture's frame is returned to the OS with the child rather than kept
    resident in a pool on the Pi. Tests inject an executor. A child that
    cannot start, dies, times out or is cancelled is terminated and reported
    as :class:`CaptureQualityUnavailableError`; the recompute's own exceptions
    (e.g. ``ValueError`` for a non-capture directory) propagate unchanged.
    """
    kwargs: dict[str, Any] = {
        "lifetime": None if lifetime is None else dict(lifetime),
        "losses": None if losses is None else dict(losses),
        "persistence": None if persistence is None else dict(persistence),
        "scheduler": None if scheduler is None else dict(scheduler),
        "scope": scope,
        "ended_at": ended_at,
    }
    loop = asyncio.get_running_loop()
    state = _recompute_state(loop)
    key = (
        str(session_dir.resolve()),
        _freeze_recompute_value(kwargs),
        id(executor),
        float(timeout),
    )
    with _recompute_states_lock:
        task = state.in_flight.get(key)
        if task is None:
            task = loop.create_task(
                _coordinated_recompute(
                    state,
                    key,
                    session_dir,
                    kwargs=kwargs,
                    executor=executor,
                    timeout=timeout,
                )
            )
            state.in_flight[key] = task
    # One HTTP client going away must not cancel a recompute another caller is
    # awaiting. The coordinator task owns child cleanup and removes itself.
    return await asyncio.shield(task)


def _recompute_state(loop: asyncio.AbstractEventLoop) -> _RecomputeLoopState:
    """Return loop-local primitives; asyncio locks cannot cross test/daemon loops."""
    with _recompute_states_lock:
        state = _recompute_states.get(loop)
        if state is None:
            state = _RecomputeLoopState(limit=asyncio.Semaphore(1), in_flight={})
            _recompute_states[loop] = state
        return state


def _freeze_recompute_value(value: Any) -> Any:
    """Turn JSON-like judge inputs into a stable exact-work identity."""
    if isinstance(value, Mapping):
        return tuple(
            sorted((str(key), _freeze_recompute_value(item)) for key, item in value.items())
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_recompute_value(item) for item in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze_recompute_value(item) for item in value))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


async def _coordinated_recompute(
    state: _RecomputeLoopState,
    key: tuple[Any, ...],
    session_dir: Path,
    *,
    kwargs: dict[str, Any],
    executor: Executor | None,
    timeout: float,
) -> CaptureQuality:
    try:
        async with state.limit:
            return await _run_recompute(
                session_dir, kwargs=kwargs, executor=executor, timeout=timeout
            )
    finally:
        with _recompute_states_lock:
            current = state.in_flight.get(key)
            if current is asyncio.current_task():
                state.in_flight.pop(key, None)


async def _run_recompute(
    session_dir: Path,
    *,
    kwargs: dict[str, Any],
    executor: Executor | None,
    timeout: float,
) -> CaptureQuality:
    from src.analysis import capture_coverage

    call = functools.partial(capture_coverage.recompute_capture_quality, session_dir, **kwargs)
    owned = executor is None
    pool: Executor = _spawn_pool() if executor is None else executor
    finished = False
    try:
        try:
            future = asyncio.get_running_loop().run_in_executor(pool, call)
        except Exception as exc:
            raise CaptureQualityUnavailableError(
                f"capture quality recompute could not start: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            quality = await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise CaptureQualityUnavailableError(
                f"capture quality recompute for {session_dir.name} did not finish "
                f"within {timeout:.0f}s"
            ) from exc
        except BrokenExecutor as exc:
            raise CaptureQualityUnavailableError(
                f"capture quality recompute process failed: {type(exc).__name__}: {exc}"
            ) from exc
        finished = True
        return quality
    finally:
        if owned:
            _discard_pool(pool, terminate=not finished)


def _spawn_pool() -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))


def _discard_pool(pool: Executor, *, terminate: bool) -> None:
    """Release a single-use pool without waiting on the loop for its child.

    On any path that did not produce a result the child is terminated: a
    timed-out or cancelled recompute would otherwise keep a core and its
    frame until it finished on its own.
    """
    processes = []
    if terminate and isinstance(pool, ProcessPoolExecutor):
        # No public way to reach the workers before 3.14's terminate_workers().
        processes = list((pool._processes or {}).values())
    pool.shutdown(wait=False, cancel_futures=True)
    for proc in processes:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 — already gone is the goal
            log.debug("recompute child already exited", exc_info=True)


async def live_capture_quality(
    session_dir: Path,
    *,
    health: _Health,
    writer: _Writer | None,
    start: StartEpoch | None,
    executor: Executor | None = None,
) -> CaptureQuality:
    """Score the running capture on what has been persisted so far.

    Raises :class:`CaptureQualityUnavailableError` when the child cannot answer.
    """
    flush_error = await _flush(writer)
    inputs = health.capture_quality_inputs()
    losses = losses_since(session_dir, start, inputs.loss_epoch)
    persistence = {
        "flushed": flush_error is None,
        "flush_error": flush_error,
        "rows_written": None if writer is None else writer.rows_written,
        "scope": SCOPE_LIVE,
    }
    return await recompute_quality_off_loop(
        session_dir,
        lifetime=inputs.lifetime,
        losses=losses,
        persistence=persistence,
        scheduler=inputs.scheduler,
        scope=SCOPE_LIVE,
        executor=executor,
    )


async def finalize_capture(
    session_dir: Path,
    *,
    health: _Health | None,
    writer: _Writer | None,
    start: StartEpoch | None,
    drain_timeout: float = DRAIN_TIMEOUT_SECONDS,
    executor: Executor | None = None,
) -> dict[str, Any] | None:
    """Boundary, drain, flush, then write the stop-time sidecar.

    Never raises: a capture that recorded good data must not fail to stop
    because its report card could not be written. Returns the sidecar payload
    or ``None`` when nothing could be scored (no health monitor) — which the
    digest reads as unknown, never as a pass.
    """
    boundary = capture_generation.bump("capture-stop")
    if health is None:
        return None
    try:
        return await _finalize(
            session_dir,
            health=health,
            writer=writer,
            start=start,
            boundary=boundary,
            drain_timeout=drain_timeout,
            executor=executor,
        )
    except Exception:
        log.exception("failed to finalize capture quality for %s", session_dir)
        return None


async def _finalize(  # noqa: PLR0913
    session_dir: Path,
    *,
    health: _Health,
    writer: _Writer | None,
    start: StartEpoch | None,
    boundary: int,
    drain_timeout: float,
    executor: Executor | None,
) -> dict[str, Any]:
    # Lifetime is read before the drain so the interval ends at the boundary,
    # not after however long the queues took.
    inputs = health.capture_quality_inputs()
    drain = await health.wait_for_queues_idle(timeout=drain_timeout)
    flush_error = await _flush(writer)
    stop_epoch = health.loss_epoch()
    losses = losses_since(session_dir, start, stop_epoch)
    lifetime = (
        None if inputs.lifetime is None else {**inputs.lifetime, "generation_boundary": boundary}
    )
    persistence = {
        "scope": SCOPE_STOP,
        "generation_boundary": boundary,
        "drained": drain["drained"],
        "drain_timed_out": drain["timed_out"],
        "drain_unobserved": drain["unobserved"],
        "flushed": flush_error is None,
        "flush_error": flush_error,
        "rows_written": None if writer is None else writer.rows_written,
        "loss_epoch_start": None if start is None else start.loss_epoch.to_dict(),
        "loss_epoch_stop": stop_epoch.to_dict(),
    }
    quality = await recompute_quality_off_loop(
        session_dir,
        lifetime=lifetime,
        losses=losses,
        persistence=persistence,
        scheduler=inputs.scheduler,
        scope=SCOPE_STOP,
        executor=executor,
    )
    payload = quality.to_dict()
    write_capture_quality(session_dir, payload)
    if not quality.passed:
        log.warning(
            "capture %s ended with verdict %s",
            session_dir.name,
            quality.verdict,
            extra={"capture_verdict": quality.verdict, "failures": quality.failures},
        )
    return payload


async def _flush(writer: _Writer | None) -> str | None:
    if writer is None:
        return None
    try:
        await writer.flush()
    except Exception as exc:  # noqa: BLE001 — the failure is the finding
        log.exception("capture flush failed at stop")
        return f"{type(exc).__name__}: {exc}"
    return None


__all__ = [
    "DRAIN_TIMEOUT_SECONDS",
    "RECOMPUTE_TIMEOUT_SECONDS",
    "CaptureQualityUnavailableError",
    "StartEpoch",
    "finalize_capture",
    "live_capture_quality",
    "losses_since",
    "recompute_quality_off_loop",
    "record_capture_start",
]
