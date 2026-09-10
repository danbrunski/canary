"""Per-tick timing and overload counters for the live-inference loop (C08.1).

Two distinct questions get separate numbers here, on purpose:

* **How long does a tick's work take?** — the per-stage occupancy
  (``feature``, ``isolation_forest``, ``sequence``, ``apply``, ...). This is
  model/feature cost.
* **Is the scheduler keeping its clock?** — ``lateness`` (actual tick start
  minus the scheduled deadline) and ``missed_ticks`` (deadlines that were
  skipped because the previous tick overran, or the worker was still busy).
  This is event-loop / overload behaviour, independent of how expensive a
  model is.

Storage is bounded: every stage keeps a rolling window of the last
``window`` samples, so a daemon that runs for a week holds the same memory as
one that ran for five minutes. The recorder is **loop-owned** — the worker
thread never touches it; it returns its own durations inside the compute
result and the loop records them on apply (see
``docs/inference-scheduling.md``).

Pure stdlib by design: ``src.dashboard.status`` names these types and must
not pull ``src.daemon``'s package ``__init__`` (which imports the dashboard)
into its import graph.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: A tick the scheduler skipped because the loop-side work of the previous
#: tick ran past one or more deadlines.
MISSED_OVERRUN = "overrun"
#: A tick the scheduler skipped because the single worker was still scoring
#: the previous tick when the deadline arrived (C08.6 — skip, never queue).
MISSED_COMPUTE_BUSY = "compute_busy"

#: Default rolling-window length per stage. At 1 Hz this is five minutes.
DEFAULT_WINDOW = 300


@dataclass(frozen=True)
class StageSummary:
    """Median / p95 / max over the rolling window of one stage, in ms."""

    count: int
    median_ms: float | None
    p95_ms: float | None
    max_ms: float | None
    last_ms: float | None

    @classmethod
    def from_samples(cls, samples: Sequence[float]) -> StageSummary:
        """Summarise ``samples`` given in **seconds**."""
        if not samples:
            return cls(count=0, median_ms=None, p95_ms=None, max_ms=None, last_ms=None)
        ordered = sorted(samples)
        return cls(
            count=len(ordered),
            median_ms=_ms(_percentile(ordered, 0.5)),
            p95_ms=_ms(_percentile(ordered, 0.95)),
            max_ms=_ms(ordered[-1]),
            last_ms=_ms(samples[-1]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "median_ms": self.median_ms,
            "p95_ms": self.p95_ms,
            "max_ms": self.max_ms,
            "last_ms": self.last_ms,
        }


@dataclass(frozen=True)
class InferenceTiming:
    """Point-in-time snapshot of the recorder, JSON-safe via :meth:`to_dict`.

    ``lateness`` and ``missed_ticks`` describe the *clock*; ``stages``
    describes the *work*. Read them separately — a big ``stages['sequence']``
    with zero lateness means the worker is slow but the loop is healthy; a
    big lateness with small stages means something else on the loop is
    hogging it (C07's receive path is the usual suspect).
    """

    interval_seconds: float | None
    ticks_scheduled: int
    ticks_completed: int
    missed_ticks: int
    missed_by_reason: Mapping[str, int]
    stale_results_discarded: int
    compute_failures: int
    #: Out-of-process scoring fallbacks by reason, cumulative (D02.3). Empty
    #: when no worker is configured; a non-empty `timeout` or `pipe_error`
    #: means the child is wedged and every tick since has scored in-process.
    worker_fallbacks: Mapping[str, int]
    #: Ticks whose compute ran past the tick period (D02.3). Not a miss — the
    #: result is still applied — but the loop cannot start the next tick, so
    #: this is the count behind `missed_by_reason.compute_busy`.
    slow_computes: int
    lateness: StageSummary
    stages: Mapping[str, StageSummary]
    window: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "interval_seconds": self.interval_seconds,
            "ticks_scheduled": self.ticks_scheduled,
            "ticks_completed": self.ticks_completed,
            "missed_ticks": self.missed_ticks,
            "missed_by_reason": dict(self.missed_by_reason),
            "stale_results_discarded": self.stale_results_discarded,
            "compute_failures": self.compute_failures,
            "worker_fallbacks": dict(self.worker_fallbacks),
            "slow_computes": self.slow_computes,
            "lateness": self.lateness.to_dict(),
            "stages": {name: summary.to_dict() for name, summary in self.stages.items()},
            "window": self.window,
        }


@dataclass
class InferenceTimingRecorder:
    """Bounded, loop-owned accumulator behind :class:`InferenceTiming`.

    Not thread-safe by contract — only the event loop calls it. Durations
    measured inside the worker thread travel back in the compute result and
    are recorded by the loop when that result is applied.
    """

    window: int = DEFAULT_WINDOW
    interval_seconds: float | None = None
    _stages: dict[str, deque[float]] = field(default_factory=dict, repr=False)
    _lateness: deque[float] = field(default_factory=deque, repr=False)
    _missed: Counter[str] = field(default_factory=Counter, repr=False)
    ticks_scheduled: int = 0
    ticks_completed: int = 0
    stale_results_discarded: int = 0
    compute_failures: int = 0
    slow_computes: int = 0
    _worker_fallbacks: Mapping[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError(f"window must be >= 1, got {self.window}")
        self._lateness = deque(maxlen=self.window)

    def record_stage(self, name: str, seconds: float) -> None:
        """Record one occupancy sample for ``name`` (seconds)."""
        bucket = self._stages.get(name)
        if bucket is None:
            bucket = deque(maxlen=self.window)
            self._stages[name] = bucket
        bucket.append(max(0.0, seconds))

    def record_lateness(self, seconds: float) -> None:
        """Record how late a tick started against its scheduled deadline."""
        self._lateness.append(max(0.0, seconds))

    def note_tick_scheduled(self) -> None:
        self.ticks_scheduled += 1

    def note_tick_completed(self) -> None:
        self.ticks_completed += 1

    def note_missed(self, reason: str, count: int = 1) -> None:
        """Count ``count`` skipped deadlines under ``reason``."""
        if count <= 0:
            return
        self._missed[reason] += count

    def note_stale_discarded(self) -> None:
        self.stale_results_discarded += 1

    def note_compute_failure(self) -> None:
        self.compute_failures += 1

    def note_slow_compute(self) -> None:
        """A tick whose compute outran the tick period (D02.3)."""
        self.slow_computes += 1

    def set_worker_fallbacks(self, fallbacks: Mapping[str, int]) -> None:
        """Publish the scoring worker's cumulative fallback reasons.

        The worker owns the counts; the recorder only carries them into the
        snapshot, so `/status` shows why scoring left the child.
        """
        self._worker_fallbacks = dict(fallbacks)

    @property
    def missed_ticks(self) -> int:
        return sum(self._missed.values())

    def snapshot(self) -> InferenceTiming:
        return InferenceTiming(
            interval_seconds=self.interval_seconds,
            ticks_scheduled=self.ticks_scheduled,
            ticks_completed=self.ticks_completed,
            missed_ticks=self.missed_ticks,
            missed_by_reason=dict(sorted(self._missed.items())),
            stale_results_discarded=self.stale_results_discarded,
            compute_failures=self.compute_failures,
            worker_fallbacks=dict(self._worker_fallbacks),
            slow_computes=self.slow_computes,
            lateness=StageSummary.from_samples(tuple(self._lateness)),
            stages={
                name: StageSummary.from_samples(tuple(samples))
                for name, samples in sorted(self._stages.items())
            },
            window=self.window,
        )


def _percentile(ordered: Sequence[float], q: float) -> float:
    """Nearest-rank percentile over an already-sorted sequence."""
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 3)


__all__ = [
    "DEFAULT_WINDOW",
    "MISSED_COMPUTE_BUSY",
    "MISSED_OVERRUN",
    "InferenceTiming",
    "InferenceTimingRecorder",
    "StageSummary",
]
