"""Absolute-deadline tick scheduler for the live-inference loop (C08.2/C08.3).

The pre-C08 loop did ``process(); await wait(interval)`` — so every tick's
period was ``processing + interval`` and the feature stage's ~40 ms
(``data/analyses/2026-09-06-benchmark-live-pipeline-baseline.md``) accumulated
as cadence drift, tick after tick. :class:`TickScheduler` instead keeps a grid
of deadlines ``start + k * interval`` on the monotonic clock and sleeps *to*
the next deadline, so processing time never enters the period.

Overrun semantics (C08.3): when the loop gets back to the scheduler and one
or more deadlines have already passed, those deadlines are **skipped** (counted
as ``missed_ticks`` under :data:`~src.daemon.inference_metrics.MISSED_OVERRUN`)
and the next tick runs at the next *future* deadline. There is never a burst of
back-to-back catch-up ticks: a 3.5-period stall yields exactly three missed
deadlines and one tick at the fourth. Chronology stays truthful because every
tick observes the real wall clock — a skipped period shows up as a real gap in
``LiveFeatureSource`` (it derives gap flags and Δt from the ``now`` it is
handed), not as a synthesized fill.

``monotonic`` and ``sleep`` are injectable so the whole thing runs under a
fake clock in tests (C08.9).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .inference_metrics import MISSED_OVERRUN, InferenceTimingRecorder

log = logging.getLogger(__name__)

#: A deadline is "passed" only when the clock is more than this far beyond
#: it — so a tick landing exactly on its deadline (fake clocks do this) or a
#: microsecond after it is on time, not a missed period.
_DEADLINE_SLACK_SECONDS = 1e-6

SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class TickSlot:
    """One scheduled tick, handed to ``run_tick``.

    ``deadline`` and ``started`` are monotonic-clock seconds; the observation
    itself is timestamped by the pipeline with the wall clock it reads when
    ``run_tick`` begins, so the two clocks are never mixed in stored data.
    """

    index: int
    deadline: float
    started: float
    lateness_seconds: float
    #: Deadlines skipped immediately before this slot because the previous
    #: tick's loop-side work overran them (``0`` on a healthy loop).
    skipped_before: int


@dataclass(frozen=True)
class GenerationToken:
    """What the world looked like when a tick was snapshotted (C08.5).

    A compute result is applied only if the token it was tagged with still
    equals the current one — a model hot-swap, a replay start/stop, or a
    capture start/stop between snapshot and apply makes the result stale and
    it is discarded (counted as ``stale_results_discarded``).
    """

    replay_id: str | None
    capture_id: str | None
    #: ``(manager name, serving_version)`` for every registered manager,
    #: sorted by name; ``None`` while a manager has nothing loaded.
    model_versions: tuple[tuple[str, str | None], ...]


class TickScheduler:
    """Drive ``run_tick`` on an absolute monotonic deadline grid."""

    def __init__(
        self,
        interval_seconds: float,
        *,
        recorder: InferenceTimingRecorder | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: SleepFn | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be > 0, got {interval_seconds}")
        self.interval = interval_seconds
        self._recorder = recorder if recorder is not None else InferenceTimingRecorder()
        self._recorder.interval_seconds = interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep

    @property
    def recorder(self) -> InferenceTimingRecorder:
        return self._recorder

    async def run(
        self,
        run_tick: Callable[[TickSlot], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None:
        """Tick until ``stop_event`` is set. Returns promptly on stop — the
        wait between ticks is interruptible, so stop latency is bounded by
        the running tick, never by the interval.

        ``run_tick`` exceptions are logged and never end the loop (the
        pipeline is best-effort, matching the pre-C08 behaviour).
        """
        # One clock read seeds both the grid origin and the first
        # overrun check, so tick 0 can never be "late" against itself.
        now = self._monotonic()
        start = now
        index = 0
        while not stop_event.is_set():
            deadline = start + index * self.interval
            skipped = self._skip_passed_deadlines(start, index, now)
            if skipped:
                index += skipped
                deadline = start + index * self.interval
            if now < deadline and await self._wait_until(deadline, stop_event):
                return
            if stop_event.is_set():
                return
            started = self._monotonic()
            lateness = max(0.0, started - deadline)
            self._recorder.record_lateness(lateness)
            self._recorder.note_tick_scheduled()
            slot = TickSlot(
                index=index,
                deadline=deadline,
                started=started,
                lateness_seconds=lateness,
                skipped_before=skipped,
            )
            try:
                await run_tick(slot)
            except Exception:  # noqa: BLE001 — live inference is best-effort
                log.exception("live inference tick %d failed", index)
            index += 1
            now = self._monotonic()

    def _skip_passed_deadlines(self, start: float, index: int, now: float) -> int:
        """Number of deadlines at or after ``index`` that ``now`` has already
        passed — the C08.3 overrun rule. ``0`` when deadline ``index`` is
        still in the future (or exactly now)."""
        next_index = math.ceil((now - start - _DEADLINE_SLACK_SECONDS) / self.interval)
        skipped = max(0, next_index - index)
        if skipped:
            self._recorder.note_missed(MISSED_OVERRUN, skipped)
            log.warning(
                "live inference overran %d deadline(s); skipping to the next future tick",
                skipped,
            )
        return skipped

    async def _wait_until(self, deadline: float, stop_event: asyncio.Event) -> bool:
        """Sleep until ``deadline``. Returns ``True`` if ``stop_event`` fired."""
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            return stop_event.is_set()
        if self._sleep is not None:
            await self._sleep(remaining)
            return stop_event.is_set()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=remaining)
        except TimeoutError:
            return False
        return True


__all__ = ["GenerationToken", "TickScheduler", "TickSlot"]
