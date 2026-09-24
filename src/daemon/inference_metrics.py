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

#: A01.7 — a scored sequence window whose rows cover more than this multiple
#: of their nominal duration (``(rows - 1) x tick``) lost ticks inside it. The
#: model reads one tick per row, so such a window is time-compressed.
STRETCHED_WINDOW_RATIO = 1.5


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
class WindowSpanSummary:
    """How much wall time scored sequence windows covered (A01.7).

    ``span_ratio`` is a window's wall span over its nominal duration, so a
    window of consecutive ticks reads 1.0. ``stretched`` counts windows above
    :data:`STRETCHED_WINDOW_RATIO` since start; the ratio and step
    percentiles are over the recorder's rolling window. ``unmeasured`` counts
    scored windows whose rows carried no timestamps, or scored before the
    tick period was known — never folded into ``scored``.
    """

    scored: int
    stretched: int
    unmeasured: int
    stretch_threshold: float
    span_ratio_median: float | None
    span_ratio_p95: float | None
    span_ratio_max: float | None
    max_step_seconds_p95: float | None
    max_step_seconds_max: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scored": self.scored,
            "stretched": self.stretched,
            "unmeasured": self.unmeasured,
            "stretch_threshold": self.stretch_threshold,
            "span_ratio": {
                "median": self.span_ratio_median,
                "p95": self.span_ratio_p95,
                "max": self.span_ratio_max,
            },
            "max_step_seconds": {
                "p95": self.max_step_seconds_p95,
                "max": self.max_step_seconds_max,
            },
        }


@dataclass
class EnvelopeCoverageMeter:
    """A06.2 — the novelty screen as a corpus-coverage meter.

    Counts scored sequence windows against the served artifact's training
    envelope (``SequenceInferenceResult.envelope``), since start or since the
    artifact last changed: overall, by operating mode, and by quantity. A
    window from an artifact that recorded no envelope is ``not_recorded``,
    never folded into ``inside``; one the envelope could not be compared on
    (no fresh value, no mode) is ``not_compared``. After the supercharger
    build this is where the new normal shows it lacks data. Bounded by the
    number of modes times quantities, not by time.
    """

    model_version: str | None = None
    windows: int = 0
    not_recorded: int = 0
    _by_status: Counter[str] = field(default_factory=Counter, repr=False)
    _by_mode: dict[str, Counter[str]] = field(default_factory=dict, repr=False)
    _by_quantity: dict[str, Counter[str]] = field(default_factory=dict, repr=False)
    _mode_quantity: dict[str, dict[str, Counter[str]]] = field(default_factory=dict, repr=False)

    def note(self, envelope: Mapping[str, Any] | None, *, model_version: str | None) -> None:
        if model_version != self.model_version:
            # A new artifact has a new envelope: its counts start over.
            self.model_version = model_version
            self.windows = self.not_recorded = 0
            self._by_status = Counter()
            self._by_mode = {}
            self._by_quantity = {}
            self._mode_quantity = {}
        self.windows += 1
        if envelope is None:
            self.not_recorded += 1
            return
        status = str(envelope.get("status"))
        self._by_status[status] += 1
        mode = str(envelope.get("mode"))
        self._by_mode.setdefault(mode, Counter())[status] += 1
        for name, entry in (envelope.get("quantities") or {}).items():
            quantity_status = entry.get("status")
            if quantity_status is None:
                continue
            self._by_quantity.setdefault(str(name), Counter())[str(quantity_status)] += 1
            per_mode = self._mode_quantity.setdefault(mode, {})
            per_mode.setdefault(str(name), Counter())[str(quantity_status)] += 1

    def to_dict(self) -> dict[str, Any]:
        recorded = self.windows - self.not_recorded
        outside = self._by_status["outside"] + self._by_status["mode_not_trained"]
        return {
            "model_version": self.model_version,
            # ``None`` until a window was scored: nothing is known yet.
            "envelope_recorded": None if self.windows == 0 else recorded > 0,
            "windows": self.windows,
            "not_recorded": self.not_recorded,
            "inside": self._by_status["inside"],
            "outside": outside,
            "mode_not_trained": self._by_status["mode_not_trained"],
            "not_compared": self._by_status["not_compared"],
            # Of the windows compared (inside or outside), never of every
            # scored window: ``not_compared`` / ``not_recorded`` are unknowns.
            "share_outside": _share(outside, outside + self._by_status["inside"]),
            "by_mode": {
                mode: {
                    "windows": sum(counts.values()),
                    "outside": counts["outside"] + counts["mode_not_trained"],
                    "mode_not_trained": counts["mode_not_trained"],
                    "not_compared": counts["not_compared"],
                    "share_outside": _share(
                        counts["outside"] + counts["mode_not_trained"],
                        counts["outside"] + counts["mode_not_trained"] + counts["inside"],
                    ),
                    "by_quantity": {
                        name: _quantity_block(q)
                        for name, q in sorted(self._mode_quantity.get(mode, {}).items())
                    },
                }
                for mode, counts in sorted(self._by_mode.items())
            },
            "by_quantity": {
                name: _quantity_block(counts) for name, counts in sorted(self._by_quantity.items())
            },
        }


def _quantity_block(counts: Counter[str]) -> dict[str, Any]:
    """Outside share of the windows a quantity was actually compared on."""
    compared = counts["inside"] + counts["outside"]
    return {
        "compared": compared,
        "outside": counts["outside"],
        "no_data": counts["no_data"],
        "not_trained": counts["not_trained"],
        "share_outside": _share(counts["outside"], compared),
    }


def _share(part: int, whole: int) -> float | None:
    return None if whole <= 0 else round(part / whole, 4)


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
    #: Late worker replies to earlier ticks, discarded instead of applied to
    #: the tick being waited on (A01.1). Non-zero means the child answered
    #: after a timeout at least once.
    worker_stale_replies: int
    #: Model loads in the scoring child — startup and hot-swaps — and the
    #: ticks the sequence detector was not ready for while they ran (A01.6).
    #: Empty when no worker is configured.
    worker_swaps: Mapping[str, Any]
    #: Wall span of scored sequence windows against their nominal length
    #: (A01.7).
    sequence_windows: WindowSpanSummary
    #: Ticks whose compute ran past the tick period (D02.3). Not a miss — the
    #: result is still applied — but the loop cannot start the next tick, so
    #: this is the count behind `missed_by_reason.compute_busy`.
    slow_computes: int
    lateness: StageSummary
    stages: Mapping[str, StageSummary]
    window: int
    #: A05.9 — the plausibility evaluator's live block (counters, open
    #: pending/confirmed detections, coverage, per-tick cost), or ``None``
    #: when ``plausibility.enabled`` is off. Its per-tick cost is also the
    #: ``stages["plausibility"]`` occupancy.
    plausibility: Mapping[str, Any] | None = None
    #: A06.2 — scored sequence windows against the served artifact's training
    #: envelope (:class:`EnvelopeCoverageMeter`).
    sequence_envelope: Mapping[str, Any] | None = None

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
            "worker_stale_replies": self.worker_stale_replies,
            "worker_swaps": dict(self.worker_swaps),
            "sequence_windows": self.sequence_windows.to_dict(),
            "slow_computes": self.slow_computes,
            "lateness": self.lateness.to_dict(),
            "stages": {name: summary.to_dict() for name, summary in self.stages.items()},
            "window": self.window,
            "plausibility": dict(self.plausibility) if self.plausibility is not None else None,
            "sequence_envelope": (
                dict(self.sequence_envelope) if self.sequence_envelope is not None else None
            ),
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
    _worker_stale_replies: int = field(default=0, repr=False)
    _worker_swaps: Mapping[str, Any] = field(default_factory=dict, repr=False)
    _window_ratios: deque[float] = field(default_factory=deque, repr=False)
    _window_max_steps: deque[float] = field(default_factory=deque, repr=False)
    _windows_scored: int = field(default=0, repr=False)
    _windows_stretched: int = field(default=0, repr=False)
    _windows_unmeasured: int = field(default=0, repr=False)
    _plausibility: Mapping[str, Any] | None = field(default=None, repr=False)
    _envelope: EnvelopeCoverageMeter = field(default_factory=EnvelopeCoverageMeter, repr=False)

    def __post_init__(self) -> None:
        if self.window < 1:
            raise ValueError(f"window must be >= 1, got {self.window}")
        self._lateness = deque(maxlen=self.window)
        self._window_ratios = deque(maxlen=self.window)
        self._window_max_steps = deque(maxlen=self.window)

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

    def set_worker_stale_replies(self, count: int) -> None:
        """Publish how many late worker replies were discarded (A01.1)."""
        self._worker_stale_replies = int(count)

    def set_worker_swaps(self, swaps: Mapping[str, Any]) -> None:
        """Publish the scoring worker's model-load record (A01.6)."""
        self._worker_swaps = dict(swaps)

    def set_plausibility(self, block: Mapping[str, Any] | None) -> None:
        """Publish the plausibility evaluator's live block (A05.9)."""
        self._plausibility = dict(block) if block is not None else None

    def note_sequence_window(
        self,
        *,
        rows: int | None,
        span_seconds: float | None,
        max_step_seconds: float | None,
    ) -> None:
        """Record one scored sequence window's wall span (A01.7).

        The nominal duration is ``(rows - 1)`` tick periods. A window that
        cannot be measured — no timestamps, or no tick period yet — is counted
        as ``unmeasured`` rather than as a clean one.
        """
        interval = self.interval_seconds
        if not rows or rows < 2 or span_seconds is None or not interval or interval <= 0:
            self._windows_unmeasured += 1
            return
        ratio = span_seconds / ((rows - 1) * interval)
        self._windows_scored += 1
        if ratio > STRETCHED_WINDOW_RATIO:
            self._windows_stretched += 1
        self._window_ratios.append(ratio)
        if max_step_seconds is not None:
            self._window_max_steps.append(max_step_seconds)

    def note_sequence_envelope(
        self, envelope: Mapping[str, Any] | None, *, model_version: str | None
    ) -> None:
        """Count one scored sequence window against the training envelope
        (A06.2). ``envelope`` is ``None`` when the artifact recorded none."""
        self._envelope.note(envelope, model_version=model_version)

    def _window_span_summary(self) -> WindowSpanSummary:
        ratios = sorted(self._window_ratios)
        steps = sorted(self._window_max_steps)
        return WindowSpanSummary(
            scored=self._windows_scored,
            stretched=self._windows_stretched,
            unmeasured=self._windows_unmeasured,
            stretch_threshold=STRETCHED_WINDOW_RATIO,
            span_ratio_median=_round(_percentile(ratios, 0.5)) if ratios else None,
            span_ratio_p95=_round(_percentile(ratios, 0.95)) if ratios else None,
            span_ratio_max=_round(ratios[-1]) if ratios else None,
            max_step_seconds_p95=_round(_percentile(steps, 0.95)) if steps else None,
            max_step_seconds_max=_round(steps[-1]) if steps else None,
        )

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
            worker_stale_replies=self._worker_stale_replies,
            worker_swaps=dict(self._worker_swaps),
            sequence_windows=self._window_span_summary(),
            slow_computes=self.slow_computes,
            lateness=StageSummary.from_samples(tuple(self._lateness)),
            stages={
                name: StageSummary.from_samples(tuple(samples))
                for name, samples in sorted(self._stages.items())
            },
            window=self.window,
            plausibility=self._plausibility,
            sequence_envelope=self._envelope.to_dict(),
        )


def _percentile(ordered: Sequence[float], q: float) -> float:
    """Nearest-rank percentile over an already-sorted sequence."""
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 3)


def _round(value: float) -> float:
    return round(value, 3)


__all__ = [
    "DEFAULT_WINDOW",
    "EnvelopeCoverageMeter",
    "MISSED_COMPUTE_BUSY",
    "MISSED_OVERRUN",
    "STRETCHED_WINDOW_RATIO",
    "InferenceTiming",
    "InferenceTimingRecorder",
    "StageSummary",
    "WindowSpanSummary",
]
