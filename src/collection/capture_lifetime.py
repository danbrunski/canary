"""Capture-lifetime observation ledger (roadmap task C03.2 / C03.4).

The trailing-window statistics in ``CanObdLoopStats`` answer "is the capture
working *right now*" and are what the dashboard shows. They cannot answer
"did the capture deliver": ``capture_rates()`` looks back at most 60 s, so a
five-minute capture with samples only in its last minute reported 60 observed
seconds, a 0.25 s maximum gap and ``pass``. This module keeps the other set of
numbers — per capture member, from arm to stop, on the capture clock:

* first / last observation and the count of successful observations;
* the missing initial interval, the missing final interval and the longest
  gap, so a late start, a mid-capture stall and a missing tail are three
  different findings instead of one trailing number;
* a **bounded** cadence summary: median and p95 interarrival from a
  reservoir sample of interarrivals, never an unbounded list;
* pause windows, so a metric whose contract says "active only" can exclude
  an intentional pause explicitly, and one that promised continuous
  acquisition cannot silently exclude an engine-off gap.

A memory channel's fuel-cut sentinel (``mem:<NAME>_SENTINEL``) is an
*answer* from the ECU, not silence, so it also observes ``mem:<NAME>``
(:func:`observed_channels` / :func:`row_channels_observing`, the one mapping
both the live ledger and the rebuild from rows use).

Like ``capture_generation`` this is a process-wide ledger with no imports from
the rest of ``src``: ``tuning_profile.activate`` / ``set_paused`` and
``tx_gate.set_passive`` call it, and the collector's ``record_read`` feeds it,
without an import cycle. Everything here is monotonic seconds; snapshots are
frozen and report offsets from the capture start ("the capture clock").
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from typing import Any

#: Prefix of a decoded memory reading's channel: ``mem:<NAME>``.
#: Suffix of the state channel a memory sentinel decodes to instead of a value.
#: Spelled here, not imported from ``src.decode``: ``src.decode.dids`` loads
#: pandas, and this module is on the recording path (``tx_gate`` imports it), so
#: importing it there took ``import src.main`` from 0.15 s to 0.44 s (S01.3,
#: verifier 2026-11-12). ``tests/test_capture_lifetime.py`` pins both to the
#: decode layer's ``MEMORY_SOURCE`` and ``SENTINEL_SUFFIX``.
MEMORY_READING_PREFIX = "mem:"
MEMORY_SENTINEL_SUFFIX = "_SENTINEL"

#: Interarrivals kept per channel for the cadence quantiles. A reservoir
#: sample (Algorithm R): O(1) memory however long the capture runs.
RESERVOIR_CAPACITY = 1024
#: Gaps at or above this are recorded individually as coverage evidence.
#: Matches ``capture_quality.MIN_STALL_SECONDS``: below it a gap is scheduling
#: jitter at any rate, above it the operator wants to know *where*.
LONG_GAP_SECONDS = 2.0
#: Individually recorded long gaps per channel. Beyond this the largest are
#: kept and the overflow is counted — the sum stays exact either way.
LONG_GAP_CAPACITY = 64


def observed_channels(channel: str) -> tuple[str, ...]:
    """The ledger channels one read of ``channel`` is an observation of.

    A memory read whose word is the declared sentinel decodes to
    ``mem:<NAME>_SENTINEL`` instead of ``mem:<NAME>`` (a state, not a number),
    and the ECU did answer, so it also observes ``mem:<NAME>``: a decel fuel cut
    must not read as the target word falling silent. A window read yields
    either the value or the sentinel for a channel, never both, so this never
    double-counts. Only memory channels map; any other name ending in
    ``_SENTINEL`` is itself.
    """
    base = _sentinel_base(channel)
    return (channel,) if base is None else (channel, base)


def row_channels_observing(channel: str) -> tuple[str, ...]:
    """The stored row channels (any case) whose rows observe ``channel``.

    The inverse of :func:`observed_channels`, for the rebuild from rows: the
    channel's own rows, plus its sentinel's when it is a memory value channel.
    """
    if not channel.upper().startswith(MEMORY_READING_PREFIX.upper()) or (
        _sentinel_base(channel) is not None
    ):
        return (channel,)
    return (channel, f"{channel}{MEMORY_SENTINEL_SUFFIX}")


def _sentinel_base(channel: str) -> str | None:
    """``mem:<NAME>`` for ``mem:<NAME>_SENTINEL``; ``None`` for anything else."""
    if not channel.startswith(MEMORY_READING_PREFIX):
        return None
    base = channel.removesuffix(MEMORY_SENTINEL_SUFFIX)
    if base == channel or base == MEMORY_READING_PREFIX:
        return None
    return base


class InterarrivalReservoir:
    """Bounded uniform sample of a stream of intervals (Vitter's Algorithm R).

    Deterministic under a fixed seed so a test can reproduce its quantiles;
    the seed is per channel, never per process.
    """

    def __init__(self, capacity: int = RESERVOIR_CAPACITY, *, seed: int = 0x5EED) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._rng = random.Random(seed)
        self._values: list[float] = []
        self._seen = 0

    @property
    def seen(self) -> int:
        return self._seen

    @property
    def capacity(self) -> int:
        return self._capacity

    def add(self, value: float) -> None:
        self._seen += 1
        if len(self._values) < self._capacity:
            self._values.append(value)
            return
        slot = self._rng.randrange(self._seen)
        if slot < self._capacity:
            self._values[slot] = value

    def quantile(self, q: float) -> float | None:
        if not self._values:
            return None
        ordered = sorted(self._values)
        index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return ordered[index]


@dataclass(frozen=True)
class ChannelLifetime:
    """One capture member from arm to now, on the capture clock (seconds)."""

    channel: str
    observations: int
    first_observed_at: float | None
    last_observed_at: float | None
    #: Gap between arm and the first observation, and between the last
    #: observation and now. Kept apart from ``max_interarrival_s`` because
    #: "started late", "stalled" and "went quiet" want different responses.
    missing_initial_s: float
    missing_final_s: float
    #: Longest stretch between two consecutive observations.
    max_interarrival_s: float | None
    #: The longest of initial gap, any interarrival and final gap.
    max_gap_s: float
    #: Cadence, from the interarrival distribution — never a count divided
    #: by wall time (that conflates rate with duty cycle).
    median_interarrival_s: float | None
    p95_interarrival_s: float | None
    interarrival_samples: int
    #: Interarrivals at or above ``LONG_GAP_SECONDS``: ``[start, end]`` on
    #: the capture clock, the largest ``LONG_GAP_CAPACITY`` of them.
    long_gaps: tuple[tuple[float, float], ...]
    long_gap_count: int
    long_gap_total_s: float
    #: Observations whose clock read earlier than the previous one. They are
    #: counted, not silently folded into a negative gap.
    clock_regressions: int = 0

    @property
    def cadence_hz(self) -> float | None:
        median = self.median_interarrival_s
        return None if not median or median <= 0 else 1.0 / median

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "observations": self.observations,
            "first_observed_at": _round(self.first_observed_at),
            "last_observed_at": _round(self.last_observed_at),
            "missing_initial_s": round(self.missing_initial_s, 3),
            "missing_final_s": round(self.missing_final_s, 3),
            "max_interarrival_s": _round(self.max_interarrival_s),
            "max_gap_s": round(self.max_gap_s, 3),
            "median_interarrival_s": _round(self.median_interarrival_s, 6),
            "p95_interarrival_s": _round(self.p95_interarrival_s, 6),
            "cadence_hz": _round(self.cadence_hz),
            "interarrival_samples": self.interarrival_samples,
            "long_gaps": [[round(a, 3), round(b, 3)] for a, b in self.long_gaps],
            "long_gap_count": self.long_gap_count,
            "long_gap_total_s": round(self.long_gap_total_s, 3),
            "clock_regressions": self.clock_regressions,
        }


class ChannelAccumulator:
    """Mutable per-channel state behind :class:`ChannelLifetime`.

    ``observe(offset)`` takes seconds since the capture started and must be
    called in arrival order; a regression is counted rather than applied.
    """

    def __init__(self, channel: str, *, seed: int = 0x5EED) -> None:
        self.channel = channel
        self.observations = 0
        self.first: float | None = None
        self.last: float | None = None
        self.max_interarrival: float | None = None
        self.clock_regressions = 0
        self.long_gap_count = 0
        self.long_gap_total = 0.0
        self._long_gaps: list[tuple[float, float]] = []
        self._reservoir = InterarrivalReservoir(seed=seed)

    def observe(self, offset: float) -> None:
        if self.last is not None and offset < self.last:
            self.clock_regressions += 1
            self.observations += 1
            return
        if self.first is None:
            self.first = offset
        elif self.last is not None:
            self._observe_interarrival(self.last, offset)
        self.last = offset
        self.observations += 1

    def _observe_interarrival(self, previous: float, current: float) -> None:
        gap = current - previous
        self._reservoir.add(gap)
        if self.max_interarrival is None or gap > self.max_interarrival:
            self.max_interarrival = gap
        if gap >= LONG_GAP_SECONDS:
            self.long_gap_count += 1
            self.long_gap_total += gap
            self._long_gaps.append((previous, current))
            if len(self._long_gaps) > LONG_GAP_CAPACITY:
                self._long_gaps.sort(key=lambda window: window[1] - window[0], reverse=True)
                del self._long_gaps[LONG_GAP_CAPACITY:]

    def snapshot(self, elapsed: float) -> ChannelLifetime:
        first, last = self.first, self.last
        missing_initial = elapsed if first is None else max(0.0, first)
        missing_final = 0.0 if last is None else max(0.0, elapsed - last)
        max_gap = max(missing_initial, missing_final, self.max_interarrival or 0.0)
        return ChannelLifetime(
            channel=self.channel,
            observations=self.observations,
            first_observed_at=first,
            last_observed_at=last,
            missing_initial_s=missing_initial,
            missing_final_s=missing_final,
            max_interarrival_s=self.max_interarrival,
            max_gap_s=max_gap,
            median_interarrival_s=self._reservoir.quantile(0.5),
            p95_interarrival_s=self._reservoir.quantile(0.95),
            interarrival_samples=self._reservoir.seen,
            long_gaps=tuple(sorted(self._long_gaps)),
            long_gap_count=self.long_gap_count,
            long_gap_total_s=self.long_gap_total,
            clock_regressions=self.clock_regressions,
        )


@dataclass(frozen=True)
class PauseWindow:
    """One stretch during which polling was intentionally suspended."""

    start_s: float
    end_s: float | None
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_s": round(self.start_s, 3),
            "end_s": _round(self.end_s),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class CaptureLifetime:
    """Everything the ledger knows about the active capture, frozen."""

    profile: str
    generation_at_start: int
    started_monotonic: float
    started_wall: float
    elapsed_s: float
    pause_windows: tuple[PauseWindow, ...]
    channels: dict[str, ChannelLifetime] = field(default_factory=dict)
    source: str = "live"

    @property
    def paused_s(self) -> float:
        return sum(
            (w.end_s if w.end_s is not None else self.elapsed_s) - w.start_s
            for w in self.pause_windows
        )

    @property
    def intended_active_s(self) -> float:
        return max(0.0, self.elapsed_s - self.paused_s)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "profile": self.profile,
            "generation_at_start": self.generation_at_start,
            "started_monotonic": round(self.started_monotonic, 3),
            "started_wall": round(self.started_wall, 3),
            "elapsed_s": round(self.elapsed_s, 3),
            "paused_s": round(self.paused_s, 3),
            "intended_active_s": round(self.intended_active_s, 3),
            "pause_windows": [
                [w.start_s, w.end_s if w.end_s is not None else self.elapsed_s]
                for w in self.pause_windows
            ],
            "pause_reasons": [w.to_dict() for w in self.pause_windows],
            "channels": {name: row.to_dict() for name, row in sorted(self.channels.items())},
        }


class CaptureLedger:
    """Thread-safe ledger for one capture at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._profile: str | None = None
        self._generation = 0
        self._started: float | None = None
        self._started_wall = 0.0
        self._channels: dict[str, ChannelAccumulator] = {}
        self._closed_pauses: list[PauseWindow] = []
        self._open_pause_start: float | None = None
        self._pause_reasons: set[str] = set()
        #: Every reason seen while the open window was open, so a window that
        #: began as "passive" and ended as "operator" names both.
        self._open_pause_reasons: set[str] = set()

    def begin(
        self,
        started_monotonic: float,
        *,
        profile: str,
        generation: int,
        started_wall: float = 0.0,
        paused_reasons: tuple[str, ...] = (),
    ) -> None:
        """Reset for a new capture (a generation boundary). Pause reasons in
        force at arm time (passive mode already on) open a window at 0."""
        with self._lock:
            self._profile = profile
            self._generation = generation
            self._started = started_monotonic
            self._started_wall = started_wall
            self._channels = {}
            self._closed_pauses = []
            self._pause_reasons = set(paused_reasons)
            self._open_pause_start = 0.0 if self._pause_reasons else None
            self._open_pause_reasons = set(paused_reasons)

    def end(self) -> None:
        with self._lock:
            self._profile = None
            self._started = None
            self._channels = {}
            self._closed_pauses = []
            self._open_pause_start = None
            self._pause_reasons = set()
            self._open_pause_reasons = set()

    def is_active(self) -> bool:
        with self._lock:
            return self._started is not None

    def observe(self, channel: str, now: float) -> None:
        """One successful observation of ``channel`` at monotonic ``now``.

        Also an observation of every channel :func:`observed_channels` names
        (a memory sentinel answers for its value channel).
        """
        with self._lock:
            started = self._started
            if started is None or now < started:
                return
            for name in observed_channels(channel):
                accumulator = self._channels.get(name)
                if accumulator is None:
                    accumulator = ChannelAccumulator(name, seed=_seed_for(name))
                    self._channels[name] = accumulator
                accumulator.observe(now - started)

    def mark_paused(self, paused: bool, *, reason: str, now: float) -> None:
        """Open or close a pause window; several reasons may overlap."""
        with self._lock:
            started = self._started
            if started is None:
                # Not capturing: remember the reason so a capture armed while
                # passive mode is on starts paused rather than looking live.
                if paused:
                    self._pause_reasons.add(reason)
                else:
                    self._pause_reasons.discard(reason)
                return
            offset = max(0.0, now - started)
            if paused:
                self._pause_reasons.add(reason)
                self._open_pause_reasons.add(reason)
                if self._open_pause_start is None:
                    self._open_pause_start = offset
                return
            self._pause_reasons.discard(reason)
            if not self._pause_reasons and self._open_pause_start is not None:
                self._closed_pauses.append(
                    PauseWindow(
                        self._open_pause_start, offset, tuple(sorted(self._open_pause_reasons))
                    )
                )
                self._open_pause_start = None
                self._open_pause_reasons = set()

    def snapshot(self, now: float) -> CaptureLifetime | None:
        with self._lock:
            started = self._started
            if started is None:
                return None
            elapsed = max(0.0, now - started)
            windows = list(self._closed_pauses)
            if self._open_pause_start is not None:
                windows.append(
                    PauseWindow(
                        self._open_pause_start, None, tuple(sorted(self._open_pause_reasons))
                    )
                )
            return CaptureLifetime(
                profile=self._profile or "unknown",
                generation_at_start=self._generation,
                started_monotonic=started,
                started_wall=self._started_wall,
                elapsed_s=elapsed,
                pause_windows=tuple(windows),
                channels={name: acc.snapshot(elapsed) for name, acc in self._channels.items()},
            )


def _seed_for(channel: str) -> int:
    # Stable across processes (``hash(str)`` is salted per process).
    return sum((index + 1) * ord(char) for index, char in enumerate(channel)) & 0xFFFF


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def subtract_windows(
    start: float, end: float, windows: list[tuple[float, float]] | tuple[tuple[float, float], ...]
) -> float:
    """Length of ``[start, end]`` not covered by any of ``windows``.

    The pause-exclusion primitive: a gap that overlaps an intentional pause is
    only a coverage failure for the part outside the pause.
    """
    if end <= start:
        return 0.0
    covered = 0.0
    for w_start, w_end in windows:
        overlap = min(end, w_end) - max(start, w_start)
        if overlap > 0:
            covered += overlap
    return max(0.0, (end - start) - covered)


# --- process-wide ledger -----------------------------------------------------

_ledger = CaptureLedger()


def begin(
    started_monotonic: float,
    *,
    profile: str,
    generation: int,
    started_wall: float = 0.0,
) -> None:
    """Start the ledger for a capture. Pause reasons already in force
    (passive mode on) carry over as a window opening at 0."""
    with _ledger._lock:
        active_reasons = tuple(sorted(_ledger._pause_reasons))
    _ledger.begin(
        started_monotonic,
        profile=profile,
        generation=generation,
        started_wall=started_wall,
        paused_reasons=active_reasons,
    )


def end() -> None:
    _ledger.end()


def is_active() -> bool:
    return _ledger.is_active()


def observe(channel: str, now: float) -> None:
    _ledger.observe(channel, now)


def mark_paused(paused: bool, *, reason: str, now: float) -> None:
    _ledger.mark_paused(paused, reason=reason, now=now)


def snapshot(now: float) -> CaptureLifetime | None:
    return _ledger.snapshot(now)


def _reset_for_tests() -> None:
    global _ledger
    _ledger = CaptureLedger()


__all__ = [
    "LONG_GAP_CAPACITY",
    "LONG_GAP_SECONDS",
    "MEMORY_READING_PREFIX",
    "MEMORY_SENTINEL_SUFFIX",
    "RESERVOIR_CAPACITY",
    "CaptureLedger",
    "CaptureLifetime",
    "ChannelAccumulator",
    "ChannelLifetime",
    "InterarrivalReservoir",
    "PauseWindow",
    "begin",
    "end",
    "is_active",
    "mark_paused",
    "observe",
    "observed_channels",
    "row_channels_observing",
    "snapshot",
    "subtract_windows",
]
