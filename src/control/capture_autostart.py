"""Start a capture profile from the vehicle's own state, not from a person.

A tuning run wants `tuning` recording every pull of the drive, and the
operator is driving: they cannot watch a terminal, cannot say "starting a
pull now", and cannot start the capture themselves. On 2026-09-22 that gap
cost the drive — the engine crossed 80 °C at 17:01, the agent's next check
was at 17:04, and three pulls over 5,000 rpm were recorded on `default` at
~1 Hz instead of on `tuning`.

So the trigger belongs to the daemon, which sees every tick:

* **Arm once, before the drive.** ``POST /control/capture/autostart`` names
  the profile and the coolant set point. Nothing starts yet.
* **The daemon starts the capture** the first tick the engine is running and
  at (or above) the set point. Warm-up therefore stays on `default`, which
  is what the cold-start models train on.
* **The daemon stops it** when the engine stops, before key-off can record
  an all-channel gap, and re-arms so the next warm running stretch of the
  same drive (a gas stop, a photo stop) captures again.

The decision is a pure state machine so the rule table is testable without a
daemon, a vehicle, or a capture: feed it one observation per tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from src.vehicle_state import ENGINE_ON_STATES

#: The states the engine must be in for a capture to run
#: (:data:`src.vehicle_state.ENGINE_ON_STATES`). `ignition_only` is
#: deliberately absent: KOEO reaches no set point and produces no pulls.
RUNNING_STATES = ENGINE_ON_STATES

#: What `update` asks the caller to do this tick.
Action = Literal["start", "stop"]


@dataclass
class CaptureAutostartRule:
    """What was armed: the profile, the set point, and the debounces."""

    profile: str
    #: Engineering °C from the classifier's own `coolant_temp`. A capture
    #: starts at or above this. `None` means "as soon as the engine runs".
    coolant_min_c: float | None = 80.0
    #: One-off overrides passed through to the capture start, exactly as the
    #: manual route takes them, so arming cannot mean something different
    #: from starting by hand.
    rate_hz: float | None = None
    bus_budget_requests_per_second: float | None = None
    tune_revision: str | None = None
    #: Consecutive qualifying ticks before starting. Two, because one stale
    #: rpm read should not open a capture and rotate the session.
    start_after_ticks: int = 2
    #: Seconds the engine must be continuously not-running before stopping.
    #: Short: the cost of stopping late is a gap recorded past key-off, and
    #: the cost of stopping early is re-arming, which is free.
    stop_after_seconds: float = 10.0

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "coolant_min_c": self.coolant_min_c,
            "rate_hz": self.rate_hz,
            "bus_budget_requests_per_second": self.bus_budget_requests_per_second,
            "tune_revision": self.tune_revision,
            "start_after_ticks": self.start_after_ticks,
            "stop_after_seconds": self.stop_after_seconds,
        }


@dataclass
class CaptureAutostartDecider:
    """Debounced engine-state → capture state machine.

    ``capture_active`` is passed in every tick rather than remembered: the
    capture can be started or stopped by hand, or by a restart, and the
    daemon's own view of it is the only one that cannot drift.
    """

    rule: CaptureAutostartRule
    _qualifying_ticks: int = field(default=0, init=False)
    _not_running_since: float | None = field(default=None, init=False)
    #: Why the last tick did not start a capture — surfaced in `/status` so
    #: "nothing happened" says which kind of nothing it is.
    reason: str = field(default="waiting for the engine to run", init=False)

    def update(
        self,
        *,
        state: str,
        coolant_temp: float | None,
        capture_active: bool,
        now: float,
    ) -> Action | None:
        """Advance the machine one tick. Returns the action to take, if any.

        :param state: the current ``VehicleState.state`` name.
        :param coolant_temp: engineering °C, or ``None`` when unknown. Unknown
            never satisfies a set point — a capture that starts on a missing
            reading is the failure this exists to prevent.
        :param capture_active: is a profile capture running right now?
        :param now: monotonic seconds.
        """
        running = state in RUNNING_STATES

        if not running:
            if self._not_running_since is None:
                self._not_running_since = now
            self._qualifying_ticks = 0
            if capture_active:
                if (now - self._not_running_since) >= self.rule.stop_after_seconds:
                    self.reason = f"engine not running ({state}); capture stopped"
                    self._not_running_since = None
                    return "stop"
                self.reason = f"engine not running ({state}); stopping shortly"
                return None
            self.reason = f"waiting for the engine to run (state {state})"
            return None

        self._not_running_since = None
        if capture_active:
            self.reason = "capture running"
            return None

        if self.rule.coolant_min_c is not None:
            if coolant_temp is None:
                self._qualifying_ticks = 0
                self.reason = "coolant temperature unknown"
                return None
            if coolant_temp < self.rule.coolant_min_c:
                self._qualifying_ticks = 0
                self.reason = (
                    f"warming up: coolant {coolant_temp:.0f} °C of {self.rule.coolant_min_c:.0f} °C"
                )
                return None

        self._qualifying_ticks += 1
        if self._qualifying_ticks < self.rule.start_after_ticks:
            self.reason = "confirming the reading"
            return None
        self._qualifying_ticks = 0
        self.reason = "set point reached; capture started"
        return "start"
