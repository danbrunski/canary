"""The brain scheduler: one job at a time, only while collection is inactive (E04.4, R29.13).

Split out of :mod:`src.control.brain_jobs`, which still exports every name here.
:func:`collection_activity` names why heavy work must wait; :class:`BrainScheduler`
picks the oldest runnable job whose :func:`~src.control.brain_jobs_runners.precheck`
says run, runs it in the brain's own child and yields it the moment collection,
a digest or a workbench job needs the Pi.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from concurrent.futures import BrokenExecutor, Executor
from datetime import datetime
from typing import Any, TypeVar

from src.analysis.digest import digest_address_space_bytes
from src.collection import tuning_profile
from src.config import CanaryConfig
from src.control import child_pool
from src.control.brain_jobs_runners import precheck, run_brain_job
from src.control.brain_jobs_store import (
    _RUNNABLE,
    MAX_BREAKS,
    BrainJob,
    BrainJobStore,
    JobState,
    _breaks,
    _now,
)
from src.control.brain_state import reconcile_identity
from src.control.trip_end import DIGEST_TIMEOUT_SECONDS, any_digest_in_flight
from src.control.workbench_jobs import any_workbench_job_in_flight
from src.vehicle_state import (
    QUIET_STATES,
    ReplayStatusSource,
    derive_from_live_source,
    running_replay_id,
)

log = logging.getLogger(__name__)

_T = TypeVar("_T")


# -- scheduling ---------------------------------------------------------------


def collection_activity(
    *, live_source: Any, replay_controller: ReplayStatusSource | None
) -> str | None:
    """Why heavy work must wait now, or ``None`` when collection is inactive.

    Only a quiet verdict (``QUIET_STATES``: ``parked``, ``bench`` or ``no_data``, neither
    bus fresh) is inactive. ``engine_off``
    (CAN fresh, key out), ``ignition_only``, ``passive`` and ``unknown`` are all
    still collecting: a parked vehicle whose bus talks is being recorded.
    """
    if tuning_profile.is_active():
        return "capture_active"
    if running_replay_id(replay_controller) is not None:
        return "replay_active"
    if live_source is None:
        return "collection_state_unknown"
    state = derive_from_live_source(live_source, replay_controller=replay_controller).state
    return None if state in QUIET_STATES else f"collecting:{state}"


#: Shortest scheduler tick: longer than the reaper's grace, so a yielded child
#: is gone (SIGTERM, then SIGKILL) before the next window can start another.
#: ``blocked`` also refuses while the previous child is alive; this keeps the
#: idle poll from spinning on that refusal.
MIN_TICK_SECONDS = child_pool.CHILD_EXIT_BOUND_SECONDS


class BrainScheduler:
    """Runs brain jobs one at a time, only while collection is inactive.

    Nothing that touches the disk runs on the event loop: the job log, the
    precheck (session listing, Parquet footers), identity reconcile and every
    transition go through :func:`asyncio.to_thread`. On the loop stay only the
    in-memory checks (:meth:`blocked`)."""

    def __init__(
        self,
        config: CanaryConfig,
        *,
        activity: Callable[[], str | None],
        store: BrainJobStore | None = None,
        runner: Callable[..., dict[str, Any]] = run_brain_job,
        executor_factory: Callable[[], Executor] | None = None,
        poll_seconds: float = 0.5,
        tick_seconds: float = 15.0,
        timeout: float = DIGEST_TIMEOUT_SECONDS,
        started_at: datetime | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        if tick_seconds <= MIN_TICK_SECONDS:
            raise ValueError(
                f"tick_seconds={tick_seconds} must exceed {MIN_TICK_SECONDS}s, the bound on a "
                "yielded child's exit, or two analysis children could overlap"
            )
        self.config = config
        self.store = store or BrainJobStore.for_config(config)
        self._activity = activity
        self._runner = runner
        self._executor_factory = executor_factory
        self.poll_seconds = poll_seconds
        self.tick_seconds = tick_seconds
        self.timeout = timeout
        self.started_at = started_at or _now()
        self.running: str | None = None
        #: The daemon's stop signal; set, a dead child is a shutdown, not a failure.
        self._stop_event = stop_event

    def blocked(self) -> str | None:
        """Why no brain child may run now. In-memory only: safe on the loop."""
        reason = self._activity()
        if reason is not None:
            return reason
        if any_digest_in_flight():
            return "digest_running"
        if any_workbench_job_in_flight():
            return "workbench_running"
        holder = child_pool.analysis_holder()
        if holder is not None:
            # Whoever holds the analysis slot (a capture-quality recompute, a
            # digest refresh): one analysis child at a time, and the brain yields.
            return f"{holder}_running"
        if self.running is None and child_pool.child_alive(child_pool.BRAIN_CHILD):
            return "brain_child_exiting"
        return None

    def _yield_reason(self) -> str | None:
        """While a job runs: why it must stop (in-memory only)."""
        reason = self._activity()
        if reason is not None:
            return reason
        if any_digest_in_flight():
            return "digest_running"
        if any_workbench_job_in_flight():
            return "workbench_running"
        holder = child_pool.analysis_holder()
        if holder is not None:
            return f"{holder}_running"
        if child_pool.yield_requested(child_pool.BRAIN_CHILD):
            return "analysis_requested"
        return None

    def recover(self) -> list[BrainJob]:
        """Reopen: defer interrupted jobs and record the identity now in force.
        Blocking; :meth:`run_forever` calls it off the loop."""
        recovered = self.store.recover()
        self._reconcile()
        return recovered

    def _reconcile(self) -> None:
        try:
            reconcile_identity(self.config, write=True)
        except Exception:  # noqa: BLE001 - identity is reported, never fatal
            log.exception("brain identity reconcile failed")

    def _set(self, job: BrainJob, state: JobState, **changes: Any) -> BrainJob:
        """Record a transition, skipping one that changes nothing visible."""
        same = job.state == state and all(getattr(job, k) == v for k, v in changes.items())
        return job if same else self.store.transition(job, state, **changes)

    async def _io(self, call: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        return await asyncio.to_thread(call, *args, **kwargs)

    def _runnable(self) -> list[BrainJob]:
        return [j for j in self.store.ordered() if j.state in _RUNNABLE]

    def _defer_all(self, candidates: list[BrainJob], reason: str) -> None:
        for job in candidates:
            if job.state in ("queued", "deferred"):
                self._set(job, "deferred", reason=reason, detail="waiting for an idle window")

    def _pick(self, candidates: list[BrainJob]) -> BrainJob | None:
        """The first job whose precheck says run; records pending/failed ones."""
        for job in candidates:
            verdict, why, detail = precheck(self.config, job)
            if verdict == "pending":
                self._set(job, "pending", reason=why, detail=detail)
                continue
            if verdict == "failed":
                self._set(job, "failed", reason=why, detail=detail, finished_at=_now().isoformat())
                continue
            return job
        return None

    async def step(self) -> BrainJob | None:
        """Run the oldest runnable job if collection is inactive; return it as
        it ended (completed, failed, deferred), or ``None`` when nothing ran."""
        candidates: list[BrainJob] = await self._io(self._runnable)
        if not candidates:
            return None
        reason = self.blocked()
        if reason is not None:
            await self._io(self._defer_all, candidates, reason)
            return None
        job = await self._io(self._pick, candidates)
        if job is None or self.blocked() is not None:
            return None
        return await self._run(job)

    def _start(self, job: BrainJob) -> BrainJob:
        self._reconcile()
        return self.store.transition(
            job,
            "running",
            attempts=job.attempts + 1,
            reason=None,
            detail=None,
            started_at=_now().isoformat(),
            progress={
                "stage": "child_running",
                "attempt": job.attempts + 1,
                "breaks": _breaks(job),
            },
        )

    def shutting_down(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    def _broken(self, job: BrainJob, exc: BaseException) -> BrainJob:
        """The child died abruptly. During shutdown that is the shutdown; the
        first time otherwise it is an outside signal and the job reruns; only a
        second break in a row is the job's own failure."""
        detail = f"{type(exc).__name__}: {exc}"
        if self.shutting_down():
            return self.store.transition(
                job,
                "deferred",
                reason="daemon_shutdown",
                detail=f"the child died as the daemon stopped ({detail}); it reruns after restart",
                progress={**job.progress, "stage": "interrupted"},
            )
        breaks = _breaks(job) + 1
        if breaks < MAX_BREAKS:
            return self.store.transition(
                job,
                "deferred",
                reason="child_interrupted",
                detail=f"the child was killed abruptly ({detail}); it reruns once in the next "
                "idle window",
                progress={**job.progress, "stage": "interrupted", "breaks": breaks},
            )
        return self.store.transition(
            job,
            "failed",
            reason="child_failed",
            detail=f"the child died abruptly twice in a row; last: {detail}",
            finished_at=_now().isoformat(),
            progress={**job.progress, "stage": "failed", "breaks": breaks},
        )

    async def _run(self, job: BrainJob) -> BrainJob:
        """Run one job in the brain's own child, polling so it can yield.

        Deliberately not :func:`child_pool.run_in_child`: that waits on the
        child once, under the analysis slot, and the brain is the tenant that
        gives way to the slot's holders. It polls the future every
        ``poll_seconds`` for a reason to yield (collection started, a digest or
        workbench job or recompute holds the analysis slot, an operator
        request), cancels the future and records ``deferred`` when it does, and
        times the job against its own clock. Forcing it through
        ``run_in_child`` would take the analysis slot away from the work it
        exists to step aside for. It does share the pool lifecycle and the
        registry (``spawn_pool``, ``discard_pool``, ``claim_child`` under
        ``BRAIN_CHILD``).
        """
        child_pool.claim_child(child_pool.BRAIN_CHILD)
        self.running = job.job_id
        owned = self._executor_factory is None
        pool: Executor | None = None
        finished = False
        try:
            running: BrainJob = await self._io(self._start, job)
            call = functools.partial(
                self._runner,
                running.kind,
                running.params,
                self.config,
                job_id=running.job_id,
                results_dir=self.store.results,
                address_space_bytes=digest_address_space_bytes(self.config),
            )
            pool = (
                child_pool.spawn_pool()
                if self._executor_factory is None
                else self._executor_factory()
            )
            loop = asyncio.get_running_loop()
            started = loop.time()
            future = loop.run_in_executor(pool, call)
            child_pool.attach_child(child_pool.BRAIN_CHILD, pool)
            try:
                while True:
                    done, _ = await asyncio.wait({future}, timeout=self.poll_seconds)
                    if done:
                        break
                    reason = self._yield_reason()
                    if reason is not None:
                        future.cancel()
                        return await self._io(
                            self.store.transition,
                            running,
                            "deferred",
                            reason=f"yielded:{reason}",
                            detail="the child was stopped so collection or a higher-priority "
                            "analysis has the Pi; the job reruns in the next idle window",
                            progress={"stage": "yielded", "attempt": running.attempts},
                        )
                    if loop.time() - started > self.timeout:
                        future.cancel()
                        return await self._io(
                            self._failed,
                            running,
                            "child_timed_out",
                            f"did not finish within {self.timeout:.0f}s",
                        )
                try:
                    result = future.result()
                except BrokenExecutor as exc:
                    return await self._io(self._broken, running, exc)
                except MemoryError:
                    ceiling = digest_address_space_bytes(self.config) // (1024 * 1024)
                    return await self._io(
                        self._failed,
                        running,
                        "child_out_of_memory",
                        f"exceeded its {ceiling} MiB address-space ceiling",
                    )
                except Exception as exc:  # noqa: BLE001 - a job's failure is its state
                    return await self._io(
                        self._failed, running, "error", f"{type(exc).__name__}: {exc}"
                    )
                finished = True
                if isinstance(result, dict) and "pending" in result:
                    return await self._io(
                        self.store.transition,
                        running,
                        "pending",
                        reason=str(result["pending"].get("reason", "pending")),
                        detail=str(result["pending"].get("detail", "")),
                        result=result,
                        progress={
                            "stage": "pending",
                            "attempt": running.attempts,
                            "sessions_seen": result.get("sessions_seen"),
                        },
                    )
                return await self._io(
                    self.store.transition,
                    running,
                    "completed",
                    result=result,
                    finished_at=_now().isoformat(),
                    progress={"stage": "completed", "attempt": running.attempts},
                )
            except asyncio.CancelledError:
                # Shutdown: record it off the loop, shielded so a second
                # cancel cannot drop the record.
                await asyncio.shield(
                    asyncio.to_thread(
                        self.store.transition,
                        running,
                        "deferred",
                        reason="daemon_shutdown",
                        detail="the daemon stopped while the job ran; it reruns after restart",
                        progress={"stage": "interrupted", "attempt": running.attempts},
                    )
                )
                raise
        finally:
            self.running = None
            if pool is not None:
                if owned:
                    child_pool.discard_pool(pool, terminate=not finished)
                elif not finished:
                    pool.shutdown(wait=False, cancel_futures=True)
            child_pool.release_child(child_pool.BRAIN_CHILD)

    def _failed(self, job: BrainJob, reason: str, detail: str) -> BrainJob:
        return self.store.transition(
            job, "failed", reason=reason, detail=detail, finished_at=_now().isoformat()
        )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Recover, then run work in each idle window until ``stop_event``."""
        self._stop_event = stop_event
        await self._io(self.recover)
        while not stop_event.is_set():
            ran: BrainJob | None = None
            try:
                ran = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop outlives one bad job
                log.exception("brain scheduler step failed")
            if ran is not None and ran.state == "completed":
                continue  # back-to-back while the window stays idle
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                pass
