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

import logging
import random
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

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
#: Individually recorded misses per request. Beyond this the *first* events
#: are kept, the overflow is counted and ``events_complete_until_s`` says how
#: far the list can be trusted, so a gap after it is "unknown" rather than
#: "nothing missed" (D03.70).
MISS_EVENT_CAPACITY = 64
#: A capture-selected request that keeps missing logs one WARNING per this
#: many seconds, carrying the count since the last line (D03.70).
MISS_LOG_INTERVAL_S = 30.0

#: Why a request's attempt did not become an observation. ``timeout`` is no
#: answer (or an answer that decoded to nothing); ``negative_response`` is a
#: formal ECU refusal and carries its code; the two ``batch`` kinds are sends a
#: batch swallowed (no per-channel evidence, so no back-off was set).
MISS_TIMEOUT = "timeout"
MISS_NEGATIVE_RESPONSE = "negative_response"
MISS_BATCH_FAILURE = "batch_failure"
MISS_BATCH_UNPARSED = "batch_unparsed"
MISS_KINDS: tuple[str, ...] = (
    MISS_TIMEOUT,
    MISS_NEGATIVE_RESPONSE,
    MISS_BATCH_FAILURE,
    MISS_BATCH_UNPARSED,
)


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


@dataclass(frozen=True)
class MissEvent:
    """One send of a request that did not become an observation."""

    at_s: float
    kind: str
    nrc: int | None
    #: Seconds the miss back-off held the request off (0 when it set none:
    #: a batch failure, or a silent liveness probe).
    backoff_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "at_s": round(self.at_s, 3),
            "kind": self.kind,
            "nrc": self.nrc,
            "backoff_s": round(self.backoff_s, 3),
        }


@dataclass(frozen=True)
class RequestLifetime:
    """One capture-selected request's sends and misses, arm to now.

    Keyed by the request's own channel as the scheduler knows it
    (``PollRequest.channel``: a Mode 01 name, ``DID_xxxx``, or for a memory
    window ``mem:<window>``); ``members`` are the decoded channels one answer
    yields (a window's ``mem:<NAME>`` readings), so a gap in a decoded channel
    maps to its request through the plan or, failing that, through here.
    """

    channel: str
    kind: str
    members: tuple[str, ...]
    attempts: int
    misses: int
    misses_by_kind: dict[str, int]
    last_nrc: int | None
    first_attempt_at: float | None
    last_attempt_at: float | None
    #: The first ``MISS_EVENT_CAPACITY`` misses, in order.
    events: tuple[MissEvent, ...]
    events_dropped: int
    #: Stretches of at least ``LONG_GAP_SECONDS`` between two consecutive
    #: *sends* of the request (``[start, end]``, the largest
    #: ``LONG_GAP_CAPACITY`` of them), tracked the way a channel's
    #: observations are: it is what tells "never sent" from "sent, no result".
    attempt_long_gaps: tuple[tuple[float, float], ...] = ()
    #: Every such stretch, kept or not.
    attempt_long_gap_count: int = 0

    @property
    def attempt_gaps_dropped(self) -> int:
        """Long inter-attempt gaps the capacity pushed out of ``attempt_long_gaps``."""
        return self.attempt_long_gap_count - len(self.attempt_long_gaps)

    @property
    def events_complete_until_s(self) -> float | None:
        """Capture-clock time up to which ``events`` lists every miss; ``None``
        when nothing was dropped (the list is complete for the whole capture)."""
        if not self.events_dropped:
            return None
        return self.events[-1].at_s if self.events else 0.0

    def to_dict(self, *, events: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "channel": self.channel,
            "kind": self.kind,
            "members": list(self.members),
            "attempts": self.attempts,
            "misses": self.misses,
            "misses_by_kind": dict(sorted(self.misses_by_kind.items())),
            "last_nrc": self.last_nrc,
            "first_attempt_at": _round(self.first_attempt_at),
            "last_attempt_at": _round(self.last_attempt_at),
            "events_recorded": len(self.events),
            "events_dropped": self.events_dropped,
            "events_complete_until_s": _round(self.events_complete_until_s),
            "attempt_long_gaps": [[round(a, 3), round(b, 3)] for a, b in self.attempt_long_gaps],
            "attempt_gaps_dropped": self.attempt_gaps_dropped,
        }
        if events:
            out["events"] = [event.to_dict() for event in self.events]
        return out


class RequestAccumulator:
    """Mutable per-request state behind :class:`RequestLifetime`."""

    def __init__(self, channel: str, kind: str, members: tuple[str, ...]) -> None:
        self.channel = channel
        self.kind = kind
        self.members = members
        self.attempts = 0
        self.misses = 0
        self.misses_by_kind: dict[str, int] = {}
        self.last_nrc: int | None = None
        self.first_attempt: float | None = None
        self.last_attempt: float | None = None
        self.events: list[MissEvent] = []
        self.events_dropped = 0
        #: The send timeline: long gaps only, never a timestamp per send.
        self._sends = ChannelAccumulator(channel)
        #: Rate-limit state for the WARNING (:meth:`CaptureLedger.miss`).
        self.last_logged_at: float | None = None
        self.misses_since_log = 0

    def attempt(self, offset: float) -> None:
        self.attempts += 1
        if self.first_attempt is None:
            self.first_attempt = offset
        self.last_attempt = offset
        self._sends.observe(offset)

    def miss(self, event: MissEvent) -> None:
        self.misses += 1
        self.misses_by_kind[event.kind] = self.misses_by_kind.get(event.kind, 0) + 1
        if event.nrc is not None:
            self.last_nrc = event.nrc
        if len(self.events) < MISS_EVENT_CAPACITY:
            self.events.append(event)
        else:
            self.events_dropped += 1

    def snapshot(self) -> RequestLifetime:
        return RequestLifetime(
            channel=self.channel,
            kind=self.kind,
            members=self.members,
            attempts=self.attempts,
            misses=self.misses,
            misses_by_kind=dict(self.misses_by_kind),
            last_nrc=self.last_nrc,
            first_attempt_at=self.first_attempt,
            last_attempt_at=self.last_attempt,
            events=tuple(self.events),
            events_dropped=self.events_dropped,
            attempt_long_gaps=self._sends.long_gap_windows(),
            attempt_long_gap_count=self._sends.long_gap_count,
        )


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

    def long_gap_windows(self) -> tuple[tuple[float, float], ...]:
        """The recorded long gaps, in time order."""
        return tuple(sorted(self._long_gaps))

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
    #: Per capture-selected request: sends and misses (D03.70).
    requests: dict[str, RequestLifetime] = field(default_factory=dict)

    @property
    def paused_s(self) -> float:
        return sum(
            (w.end_s if w.end_s is not None else self.elapsed_s) - w.start_s
            for w in self.pause_windows
        )

    @property
    def intended_active_s(self) -> float:
        return max(0.0, self.elapsed_s - self.paused_s)

    def to_dict(self, *, events: bool = True) -> dict[str, Any]:
        """``events=False`` leaves each request's miss list out (counts stay):
        the live status polls this every second and has no use for the list."""
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
            "requests": {
                name: row.to_dict(events=events) for name, row in sorted(self.requests.items())
            },
            "request_event_capacity": MISS_EVENT_CAPACITY,
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
        self._requests: dict[str, RequestAccumulator] = {}
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
            self._requests = {}
            self._closed_pauses = []
            self._pause_reasons = set(paused_reasons)
            self._open_pause_start = 0.0 if self._pause_reasons else None
            self._open_pause_reasons = set(paused_reasons)

    def end(self) -> None:
        with self._lock:
            self._profile = None
            self._started = None
            self._channels = {}
            self._requests = {}
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

    def attempt(
        self, channel: str, now: float, *, kind: str = "", members: Iterable[str] = ()
    ) -> None:
        """One send of the capture-selected request ``channel`` at monotonic ``now``."""
        with self._lock:
            started = self._started
            if started is None or now < started:
                return
            self._request(channel, kind, members).attempt(now - started)

    def miss(  # noqa: PLR0913
        self,
        channel: str,
        now: float,
        *,
        kind: str,
        nrc: int | None = None,
        backoff_s: float = 0.0,
        request_kind: str = "",
        members: Iterable[str] = (),
    ) -> None:
        """One send of ``channel`` that did not become an observation.

        Logs one WARNING per request, rate-limited: the first miss, then at
        most one line per ``MISS_LOG_INTERVAL_S`` carrying the count since the
        last (D03.70: the daemon logged nothing for a missed window read, so a
        2.5-4.5 s hole in a memory window left no trace in the journal).
        """
        if kind not in MISS_KINDS:
            raise ValueError(f"unknown miss kind {kind!r}; one of {MISS_KINDS}")
        report: tuple[int, int, str] | None = None
        with self._lock:
            started = self._started
            if started is None or now < started:
                return
            accumulator = self._request(channel, request_kind, members)
            accumulator.miss(MissEvent(now - started, kind, nrc, max(0.0, backoff_s)))
            accumulator.misses_since_log += 1
            last = accumulator.last_logged_at
            if last is None or now - last >= MISS_LOG_INTERVAL_S:
                report = (accumulator.misses_since_log, accumulator.misses, self._profile or "")
                accumulator.last_logged_at = now
                accumulator.misses_since_log = 0
        if report is not None:
            since, total, profile = report
            log.warning(
                "capture request %s missed (%s%s), back-off %.1fs; %d miss(es) since the last "
                "report, %d this capture (%s)",
                channel,
                kind,
                "" if nrc is None else f" NRC 0x{nrc:02X}",
                max(0.0, backoff_s),
                since,
                total,
                profile,
                extra={
                    "request_channel": channel,
                    "miss_kind": kind,
                    "miss_nrc": nrc,
                    "misses_since_report": since,
                    "capture_profile": profile,
                },
            )

    def _request(self, channel: str, kind: str, members: Iterable[str]) -> RequestAccumulator:
        """The accumulator for ``channel``; the caller holds the lock."""
        accumulator = self._requests.get(channel)
        if accumulator is None:
            accumulator = RequestAccumulator(channel, kind, tuple(members))
            self._requests[channel] = accumulator
        elif members:
            # A profile switch narrows a window; the newest read's members win.
            accumulator.members = tuple(members)
        return accumulator

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
                requests={name: acc.snapshot() for name, acc in self._requests.items()},
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


def attempt(channel: str, now: float, *, kind: str = "", members: Iterable[str] = ()) -> None:
    _ledger.attempt(channel, now, kind=kind, members=members)


def miss(  # noqa: PLR0913
    channel: str,
    now: float,
    *,
    kind: str,
    nrc: int | None = None,
    backoff_s: float = 0.0,
    request_kind: str = "",
    members: Iterable[str] = (),
) -> None:
    _ledger.miss(
        channel,
        now,
        kind=kind,
        nrc=nrc,
        backoff_s=backoff_s,
        request_kind=request_kind,
        members=members,
    )


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
    "MISS_BATCH_FAILURE",
    "MISS_BATCH_UNPARSED",
    "MISS_EVENT_CAPACITY",
    "MISS_KINDS",
    "MISS_LOG_INTERVAL_S",
    "MISS_NEGATIVE_RESPONSE",
    "MISS_TIMEOUT",
    "RESERVOIR_CAPACITY",
    "CaptureLedger",
    "CaptureLifetime",
    "ChannelAccumulator",
    "ChannelLifetime",
    "InterarrivalReservoir",
    "MissEvent",
    "PauseWindow",
    "RequestAccumulator",
    "RequestLifetime",
    "attempt",
    "begin",
    "end",
    "is_active",
    "mark_paused",
    "miss",
    "observe",
    "observed_channels",
    "row_channels_observing",
    "snapshot",
    "subtract_windows",
]
