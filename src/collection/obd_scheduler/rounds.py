"""Capture rounds (C02): scheduled sampling windows with explicit members.

Part of the ``obd_scheduler`` package (R02.10).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from src.collection.obd_scheduler.cadence import (
    _is_request_suppressed,
    _request_interval_seconds,
)
from src.collection.obd_scheduler.metrics import (
    CanObdLoopStats,
)
from src.collection.obd_scheduler.requests import (
    PollRequest,
    PollRequestState,
    RequestFilter,
)
from src.collection.obd_scheduler.spec import (
    CanObdSpec,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoundState:
    """One capture round: a scheduled sampling window with explicit members.

    Immutable; the scheduler replaces it on every change so a reading issued
    under one snapshot can never be re-tagged by a later one. A round ENDS on
    membership completion (every member attempted once) or at ``deadline``,
    whichever is first — never "when the loop runs out of work", which is the
    condition that let one stamp cover 875 ms of readings (C02.1).
    """

    generation: int
    round_id: int
    #: Monotonic window. ``deadline = scheduled_start + period_s``.
    scheduled_start: float
    deadline: float
    period_s: float
    #: Wall clock at ``scheduled_start`` — the presentation stamp when the
    #: active profile asks for ``round_timestamps``.
    wall_start: float
    #: Request indices due inside this window when it opened.
    members: tuple[int, ...]
    attempted: frozenset[int] = frozenset()
    #: Member -> readings emitted. Never mutated; replaced via ``with_observed``.
    observations: Mapping[int, int] = field(default_factory=dict)
    first_acquired_at: float | None = None
    last_acquired_at: float | None = None

    @property
    def remaining(self) -> tuple[int, ...]:
        return tuple(index for index in self.members if index not in self.attempted)

    @property
    def complete(self) -> bool:
        return not self.remaining

    @property
    def succeeded(self) -> frozenset[int]:
        return frozenset(index for index, count in self.observations.items() if count > 0)

    @property
    def skew_s(self) -> float:
        """Measured acquisition spread across this round's readings."""
        if self.first_acquired_at is None or self.last_acquired_at is None:
            return 0.0
        return max(0.0, self.last_acquired_at - self.first_acquired_at)

    def with_attempt(self, index: int) -> RoundState:
        return replace(self, attempted=self.attempted | {index})

    def with_observed(self, index: int, count: int, acquired_at: float | None) -> RoundState:
        observations = {**self.observations, index: self.observations.get(index, 0) + count}
        first, last = self.first_acquired_at, self.last_acquired_at
        if acquired_at is not None:
            first = acquired_at if first is None else min(first, acquired_at)
            last = acquired_at if last is None else max(last, acquired_at)
        return replace(
            self,
            observations=observations,
            first_acquired_at=first,
            last_acquired_at=last,
        )


class RoundScheduler:
    """Opens, tracks and closes capture rounds on monotonic deadlines (C02.2/C02.4).

    Rounds are scheduled at ``anchor + k * period``. When the loop falls
    behind, the periods it missed are counted as ``rounds_skipped`` and the
    next round opens at the next *future* boundary — no catch-up burst. When
    the plan has nothing due at all (passive mode, a DID lapse, everything
    backed off) the schedule is re-anchored on the next open instead, so an
    idle stretch is not reported as skipped rounds.

    Counters are written straight onto the loop's :class:`CanObdLoopStats`
    so ``/status`` and the stop-time sidecar see them without extra plumbing.
    """

    def __init__(self, generation: int, stats: CanObdLoopStats) -> None:
        self.generation = generation
        self.stats = stats
        self.current: RoundState | None = None
        self._next_round_id = 0
        self._anchor: float | None = None
        self._period: float | None = None
        self._last_k: int | None = None
        self.last_round_skew_s = 0.0
        self.closed: int = 0

    def reset(self, generation: int, now: float) -> None:
        """Start a new generation: close any open round and restart numbering."""
        self.close(now)
        self.generation = generation
        self._next_round_id = 0
        self._anchor = None
        self._last_k = None

    def next_open_at(self, now: float, period: float) -> float:
        """Earliest instant the next round may open, so two rounds never share a slot."""
        if self._anchor is None or self._last_k is None or self._period != period:
            return now
        return max(now, self._anchor + (self._last_k + 1) * period)

    def open(
        self,
        now: float,
        wall_now: float,
        *,
        period: float,
        members: Sequence[int],
    ) -> RoundState:
        if self.current is not None:
            raise RuntimeError("a round is already open; close it first")
        if self._anchor is None or self._last_k is None or self._period != period:
            self._anchor, self._period, k = now, period, 0
        else:
            # ``next_open_at`` guarantees ``now`` is at or past the next
            # boundary; the max() keeps a float rounding error at exactly
            # that boundary from re-using the previous round's slot.
            k = max(int((now - self._anchor) / period), self._last_k + 1)
            missed = k - (self._last_k + 1)
            if missed > 0:
                self.stats.rounds_skipped += missed
        start = self._anchor + k * period
        state = RoundState(
            generation=self.generation,
            round_id=self._next_round_id,
            scheduled_start=start,
            deadline=start + period,
            period_s=period,
            wall_start=wall_now - (now - start),
            members=tuple(members),
        )
        self._next_round_id += 1
        self._last_k = k
        self.current = state
        self.stats.rounds_started += 1
        return state

    def expire(self, now: float) -> RoundState | None:
        """Close the open round if its deadline has passed."""
        if self.current is not None and now >= self.current.deadline:
            return self.close(now)
        return None

    def idle(self, now: float) -> None:
        """Nothing is due: close any round and re-anchor on the next open."""
        self.close(now)
        self._last_k = None

    def on_attempt(self, index: int) -> None:
        if self.current is not None:
            self.current = self.current.with_attempt(index)

    def on_observed(self, index: int, count: int, acquired_at: float | None) -> None:
        if self.current is not None:
            self.current = self.current.with_observed(index, count, acquired_at)

    def close(self, now: float) -> RoundState | None:
        del now  # the close instant is not part of the round's identity
        state = self.current
        if state is None:
            return None
        self.current = None
        self.closed += 1
        if state.complete:
            self.stats.rounds_complete += 1
        else:
            self.stats.rounds_incomplete += 1
        self.last_round_skew_s = state.skew_s
        self.stats.round_skew_max_s = max(self.stats.round_skew_max_s, state.skew_s)
        return state

    def snapshot(self, now: float) -> dict[str, Any]:
        """Round accounting for ``/status.obd_scheduler.rounds``."""
        current = self.current
        return {
            "generation": self.generation,
            "period_s": self._period,
            "started": self.stats.rounds_started,
            "complete": self.stats.rounds_complete,
            "incomplete": self.stats.rounds_incomplete,
            "skipped": self.stats.rounds_skipped,
            "skew_max_s": round(self.stats.round_skew_max_s, 6),
            "last_skew_s": round(self.last_round_skew_s, 6),
            "open": None
            if current is None
            else {
                "round_id": current.round_id,
                "members": len(current.members),
                "attempted": len(current.attempted),
                "succeeded": len(current.succeeded),
                "deadline_in_s": round(max(0.0, current.deadline - now), 4),
            },
        }


def round_period_seconds(
    requests: Sequence[PollRequest],
    states: Sequence[PollRequestState],
    spec: CanObdSpec,
    now: float,
    *,
    eligible: RequestFilter | None = None,
) -> float | None:
    """The round period: the fastest configured cadence among pollable requests.

    ``None`` when nothing is pollable (every eligible request is at rate zero
    or suppressed). Slower requests join every Nth round (C02.3).
    """
    intervals = [
        _request_interval_seconds(request, spec)
        for index, (request, state) in enumerate(zip(requests, states, strict=True))
        if not _is_request_suppressed(state, now) and (eligible is None or eligible(index, request))
    ]
    finite = [interval for interval in intervals if interval != float("inf")]
    return min(finite) if finite else None


def round_members(
    requests: Sequence[PollRequest],
    states: Sequence[PollRequestState],
    now: float,
    *,
    deadline: float,
    eligible: RequestFilter | None = None,
) -> tuple[int, ...]:
    """Requests due before ``deadline`` — the membership of the round opening now."""
    return tuple(
        index
        for index, (request, state) in enumerate(zip(requests, states, strict=True))
        if not _is_request_suppressed(state, now)
        and state.next_due_at < deadline
        and (eligible is None or eligible(index, request))
    )
