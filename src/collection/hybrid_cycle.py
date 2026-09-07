"""Hybrid Mode 01 / Mode 22 cycle scheduling (roadmap task C04).

Three things live here, all used by ``DidWindowController`` and
``run_can_obd_loop``:

* :func:`next_wake_at` — the **one** next-wake calculation. Every wait in the
  collector loop goes through it, so a controller transition (the 50 ms
  post-exit grace, a keep-alive, a window deadline) is serviced when it is
  due rather than after whatever flat drain the branch happened to use.
* :class:`CyclePlan` / :func:`plan_cycle` — what one hybrid cycle has to fit:
  the Mode 01 requests, the DID sweep, session entry/exit and the exit grace,
  against the effective bus budget. An infeasible cycle is *reported*, never
  promised.
* :class:`CycleAccounting` — bounded, per-cycle evidence: achieved cycle
  duration, per-service occupancy, transition latency, missed cycles, per
  service shortfall and cross-mode acquisition separation.

Vehicle facts (session ids, exit sub-function, grace) stay in the
``DidSessionSpec`` resolved from config; nothing here hard-codes them.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

#: Longest the loop may go without re-running its tick (metrics, triggers,
#: generation changes are all checked per tick). Also the fallback wake when
#: nothing else is scheduled — the loop never sleeps blind past this.
MAX_IDLE_TICK_SECONDS = 0.2
#: Per-cycle history kept for the achieved-value summaries. At a 4 Hz cycle
#: this is one minute; bounded so a day-long daemon does not grow.
CYCLE_HISTORY = 240

WakeReason = Literal["budget", "due", "transition", "keepalive", "deadline", "idle"]
ServiceKind = Literal["mode01", "mode22", "session"]


@dataclass(frozen=True)
class WakeDecision:
    """The instant the loop should next act, and which deadline chose it."""

    at: float
    reason: WakeReason


def next_wake_at(
    now: float,
    *,
    budget_at: float | None = None,
    next_due: float | None = None,
    transition_at: float | None = None,
    keepalive_at: float | None = None,
    deadline: float | None = None,
    max_idle_seconds: float = MAX_IDLE_TICK_SECONDS,
) -> WakeDecision:
    """Earliest relevant instant strictly after ``now``.

    Candidates already in the past are dropped rather than returned: they
    mean "act on the next tick", and the caller only reaches a wait when the
    tick found nothing actionable — so waking on a past instant would spin.
    The idle tick guarantees the result is in the future and bounded, so raw
    CAN keeps being received during every wait.
    """
    idle = max(max_idle_seconds, 0.001)
    # Order breaks ties: a keep-alive that coincides with the controller's
    # transition instant (continuous mode) is reported as the keep-alive.
    candidates: list[tuple[float, WakeReason]] = [
        (budget_at, "budget"),  # type: ignore[list-item]
        (next_due, "due"),  # type: ignore[list-item]
        (keepalive_at, "keepalive"),  # type: ignore[list-item]
        (transition_at, "transition"),  # type: ignore[list-item]
        (deadline, "deadline"),  # type: ignore[list-item]
        (now + idle, "idle"),
    ]
    future = [
        (at, reason)
        for at, reason in candidates
        if at is not None and at > now and at != float("inf")
    ]
    at, reason = min(future, key=lambda item: item[0])
    return WakeDecision(at=at, reason=reason)


@dataclass(frozen=True)
class CyclePlan:
    """What one Mode 01 + Mode 22 cycle must fit, at the effective budget.

    Request costs are the budget pacing floor (``1 / budget_rps`` each); the
    ECU's real latency and any timeout only make a cycle *longer*, which the
    accounting reports as missed cycles rather than this plan hiding.
    """

    cycle_seconds: float
    budget_rps: float
    #: Mode 01-side channels (PIDs, DTC/service polls, custom PIDs) selected.
    mode01_channels: int
    #: Requests those channels cost once batching is applied.
    mode01_requests: int
    did_requests: int
    #: Entry plus exit when a fast exit is available, entry alone otherwise.
    session_requests: int
    #: Exit grace with a fast exit; the full session lapse without one.
    grace_seconds: float
    request_timeout_seconds: float

    @property
    def request_seconds(self) -> float:
        total = self.mode01_requests + self.did_requests + self.session_requests
        return total / self.budget_rps

    @property
    def required_seconds(self) -> float:
        return self.request_seconds + self.grace_seconds

    @property
    def feasible(self) -> bool:
        return self.required_seconds <= self.cycle_seconds + 1e-9

    @property
    def shortfall_seconds(self) -> float:
        return max(0.0, self.required_seconds - self.cycle_seconds)

    @property
    def mode01_share_seconds(self) -> float:
        """Bus time reserved for Mode 01 before the next window may open."""
        return self.mode01_requests / self.budget_rps

    @property
    def worst_case_seconds(self) -> float:
        """One timed-out request on top of the nominal cycle — informational."""
        return self.required_seconds + self.request_timeout_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_seconds": self.cycle_seconds,
            "budget_rps": round(self.budget_rps, 3),
            "mode01_channels": self.mode01_channels,
            "mode01_requests": self.mode01_requests,
            "did_requests": self.did_requests,
            "session_requests": self.session_requests,
            "grace_seconds": self.grace_seconds,
            "required_seconds": round(self.required_seconds, 4),
            "worst_case_seconds": round(self.worst_case_seconds, 4),
            "feasible": self.feasible,
            "shortfall_seconds": round(self.shortfall_seconds, 4),
            "mode01_share_seconds": round(self.mode01_share_seconds, 4),
        }


def plan_cycle(
    *,
    cycle_seconds: float,
    budget_rps: float,
    mode01_channels: int,
    batch_max_pids: int,
    did_requests: int,
    fast_exit: bool,
    exit_grace_seconds: float,
    lapse_seconds: float,
    request_timeout_seconds: float,
) -> CyclePlan:
    """Allocate one cycle's work. ``batch_max_pids <= 1`` means no batching."""
    batch = max(1, batch_max_pids)
    return CyclePlan(
        cycle_seconds=cycle_seconds,
        budget_rps=max(budget_rps, 0.001),
        mode01_channels=mode01_channels,
        mode01_requests=math.ceil(mode01_channels / batch),
        did_requests=did_requests,
        session_requests=2 if fast_exit else 1,
        grace_seconds=exit_grace_seconds if fast_exit else lapse_seconds,
        request_timeout_seconds=request_timeout_seconds,
    )


def _summary(values: deque[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "n": len(ordered),
        "median": round(statistics.median(ordered), 4),
        "p95": round(ordered[max(0, p95_index)], 4),
        "max": round(ordered[-1], 4),
    }


@dataclass
class _OpenCycle:
    """Mutable state of the cycle currently running; replaced every cycle."""

    started_at: float
    plan: CyclePlan | None
    occupancy: dict[str, float] = field(
        default_factory=lambda: {"mode01": 0.0, "mode22": 0.0, "session": 0.0}
    )
    mode01_attempts: int = 0
    did_attempts: int = 0
    did_unattempted_at_close: int = 0
    first_acquired: dict[str, float] = field(default_factory=dict)
    last_acquired: dict[str, float] = field(default_factory=dict)

    @property
    def cross_mode_separation_s(self) -> float | None:
        """Widest acquisition gap between a Mode 01 and a Mode 22 reading."""
        if "mode01" not in self.first_acquired or "mode22" not in self.first_acquired:
            return None
        return max(
            abs(self.last_acquired["mode01"] - self.first_acquired["mode22"]),
            abs(self.last_acquired["mode22"] - self.first_acquired["mode01"]),
        )


class CycleAccounting:
    """Bounded per-cycle evidence for ``/status.obd_scheduler.did_profile.cycle``.

    A cycle starts when the session-entry request goes out and ends when the
    next one does — the same definition the target ``cycle_seconds`` uses, so
    the two are comparable. Everything is measured; nothing here infers that
    Mode 01 recovered because a wait elapsed.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.cycles_started = 0
        self.cycles_missed = 0
        self.mode01_shortfall = 0
        self.did_shortfall = 0
        self._durations: deque[float] = deque(maxlen=CYCLE_HISTORY)
        self._occupancy: deque[dict[str, float]] = deque(maxlen=CYCLE_HISTORY)
        self._separation: deque[float] = deque(maxlen=CYCLE_HISTORY)
        self._exit_latency: deque[float] = deque(maxlen=CYCLE_HISTORY)
        self._entry_latency: deque[float] = deque(maxlen=CYCLE_HISTORY)
        self._current: _OpenCycle | None = None
        self._exit_sent_at: float | None = None
        self._entry_sent_at: float | None = None
        #: Monotonic instant of the last *observed* Mode 01 success following
        #: a session exit or lapse. ``None`` until one is seen.
        self.mode01_recovered_at: float | None = None
        self.mode01_recovery_pending = False

    # -- cycle boundaries -------------------------------------------------

    def begin_cycle(self, now: float, plan: CyclePlan | None, *, missed: int = 0) -> None:
        self._finish_current(now)
        self.cycles_started += 1
        self.cycles_missed += missed
        self._current = _OpenCycle(started_at=now, plan=plan)

    def _finish_current(self, now: float) -> None:
        current = self._current
        if current is None:
            return
        self._durations.append(now - current.started_at)
        self._occupancy.append(dict(current.occupancy))
        separation = current.cross_mode_separation_s
        if separation is not None:
            self._separation.append(separation)
        if current.plan is not None:
            self.mode01_shortfall += max(0, current.plan.mode01_channels - current.mode01_attempts)
            self.did_shortfall += current.did_unattempted_at_close
        self._current = None

    def abandon(self, now: float) -> None:
        """Close the open cycle without starting another (generation change)."""
        self._finish_current(now)

    def on_missed(self, count: int) -> None:
        self.cycles_missed += count

    def on_window_closed(self, *, unattempted: int) -> None:
        if self._current is not None:
            self._current.did_unattempted_at_close += unattempted

    # -- per-request evidence -------------------------------------------

    def on_attempt(self, kind: ServiceKind) -> None:
        if self._current is None:
            return
        if kind == "mode22":
            self._current.did_attempts += 1
        elif kind == "mode01":
            self._current.mode01_attempts += 1

    def on_service_time(self, kind: ServiceKind, seconds: float) -> None:
        if self._current is not None:
            self._current.occupancy[kind] += max(0.0, seconds)

    def on_acquired(self, kind: ServiceKind, acquired_at: float | None) -> None:
        if self._current is None or acquired_at is None or kind == "session":
            return
        self._current.first_acquired.setdefault(kind, acquired_at)
        self._current.last_acquired[kind] = max(
            self._current.last_acquired.get(kind, acquired_at), acquired_at
        )

    # -- transitions ------------------------------------------------------

    def on_entry_sent(self, now: float) -> None:
        self._entry_sent_at = now

    def on_exit_sent(self, now: float) -> None:
        self._exit_sent_at = now
        self.mode01_recovery_pending = True

    def on_lapse_started(self) -> None:
        """Window closed without a fast exit: recovery is unobserved until seen."""
        self.mode01_recovery_pending = True

    def on_did_answered(self, now: float) -> None:
        if self._entry_sent_at is not None:
            self._entry_latency.append(max(0.0, now - self._entry_sent_at))
            self._entry_sent_at = None

    def on_mode01_answered(self, now: float) -> None:
        if not self.mode01_recovery_pending:
            return
        if self._exit_sent_at is not None:
            self._exit_latency.append(max(0.0, now - self._exit_sent_at))
            self._exit_sent_at = None
        self.mode01_recovered_at = now
        self.mode01_recovery_pending = False

    # -- reporting --------------------------------------------------------

    def snapshot(self, now: float) -> dict[str, Any]:
        last_occupancy = self._occupancy[-1] if self._occupancy else None
        return {
            "cycles_started": self.cycles_started,
            "cycles_missed": self.cycles_missed,
            "mode01_shortfall": self.mode01_shortfall,
            "did_shortfall": self.did_shortfall,
            "cycle_duration_s": _summary(self._durations),
            "occupancy_s": None
            if last_occupancy is None
            else {kind: round(seconds, 4) for kind, seconds in last_occupancy.items()},
            "transition_latency_s": {
                "exit_to_mode01": _summary(self._exit_latency),
                "entry_to_did": _summary(self._entry_latency),
            },
            "cross_mode_separation_s": _summary(self._separation),
            "mode01_recovery_pending": self.mode01_recovery_pending,
            "mode01_recovered_age_s": (
                round(max(0.0, now - self.mode01_recovered_at), 3)
                if self.mode01_recovered_at is not None
                else None
            ),
        }


__all__ = [
    "CYCLE_HISTORY",
    "MAX_IDLE_TICK_SECONDS",
    "CycleAccounting",
    "CyclePlan",
    "WakeDecision",
    "next_wake_at",
    "plan_cycle",
]
