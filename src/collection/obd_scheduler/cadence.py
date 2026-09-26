"""When each request is due: the rate policy (pure in request, spec and profile
view), due times on the window grid, miss back-off and generation replans.

Part of the ``obd_scheduler`` package (R02.10).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Container, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.collection import tuning_profile
from src.collection.obd_registry import PollTier
from src.collection.obd_scheduler.requests import (
    PollRequest,
    PollRequestState,
    RequestFilter,
)
from src.collection.obd_scheduler.spec import (
    CanObdSpec,
    _bus_budget_requests_per_second,
)
from src.operating import ENGINE_RUNNING_MIN_RPM

if TYPE_CHECKING:
    from src.collection.profile_view import ProfileView

log = logging.getLogger(__name__)


AUTO_MISS_BACKOFF_SECONDS = 30.0


#: A focused high-rate capture has already established that every selected
#: channel answers.  One transient miss must not erase 30 seconds of that
#: channel (roughly 450 samples at 15 Hz), so retry quickly and escalate only
#: if the channel keeps missing.  Normal collection retains the conservative
#: 30-second retry below.
CAPTURE_MISS_BACKOFF_SECONDS = 1.0


#: Ceiling for the escalating backoff applied to a request the ECU has *never*
#: answered (see ``_miss_backoff_seconds``). The ``auto`` PID set probes every
#: decoder in the catalog, so a dozen-odd PIDs this ECU simply doesn't support
#: is normal — at the flat 30 s backoff they each re-probe twice a minute
#: forever, and every probe costs a full request slot plus its timeout. That
#: is bus time stolen from PIDs that do answer.
MAX_MISS_BACKOFF_SECONDS = 900.0


TIER_RATE_FACTORS: dict[PollTier, float] = {
    "fast": 1.0,
    "medium": 1.0 / 3.0,
    "slow": 1.0 / 15.0,
    "on_demand": 0.0,
}


def _update_response_state(
    state: PollRequestState,
    *,
    emitted: bool,
    now: float,
    focused_capture: bool = False,
) -> None:
    if emitted:
        state.missed_responses = 0
        state.suppressed_until = 0.0
        state.missed_at = None
        return
    state.missed_responses += 1
    state.missed_at = now
    state.suppressed_until = now + _miss_backoff_seconds(state, focused_capture=focused_capture)
    state.backoffs += 1


def _miss_backoff_seconds(state: PollRequestState, *, focused_capture: bool = False) -> float:
    """Backoff to apply after a missed response.

    A request that has answered at least once normally keeps the flat
    ``AUTO_MISS_BACKOFF_SECONDS``.  During a focused high-rate capture that
    would turn one transient timeout into a 30-second hole, so capture-owned
    channels retry after one second and exponentially approach the normal
    ceiling only when misses continue.

    A request that has *never* answered is a different thing: on the ``auto``
    PID set the catalog is probed wholesale, so unsupported PIDs are expected
    and permanent. Doubling their backoff up to ``MAX_MISS_BACKOFF_SECONDS``
    stops them re-probing twice a minute forever and hands the reclaimed
    request slots (and their timeout stalls) back to channels that answer.
    They are never retired outright — a PID gated behind a warm engine or an
    ECU that only answers in a particular state still gets a periodic probe.
    """
    if state.last_response_at is not None:
        if focused_capture:
            exponent = min(max(state.missed_responses - 1, 0), 20)
            return min(
                CAPTURE_MISS_BACKOFF_SECONDS * (2.0**exponent),
                AUTO_MISS_BACKOFF_SECONDS,
            )
        return AUTO_MISS_BACKOFF_SECONDS
    # Clamped before the exponentiation: float ** big raises OverflowError.
    exponent = min(max(state.missed_responses - 1, 0), 20)
    return min(AUTO_MISS_BACKOFF_SECONDS * (2.0**exponent), MAX_MISS_BACKOFF_SECONDS)


#: Request kinds whose cadence and miss back-off the active capture profile
#: sets (``_request_rate_hz``). DTC and Mode 06 polls run on fixed intervals of
#: their own; freeze frames and identification are one-shots.
PROFILE_CADENCE_KINDS: frozenset[str] = frozenset({"pid", "did", "memory"})


def replan_for_generation(
    requests: Sequence[PollRequest],
    states: Sequence[PollRequestState],
    spec: CanObdSpec,
    now: float,
    *,
    profile_gated: bool,
) -> int:
    """Re-plan every selected request under the profile now in force (D05.1).

    A request's ``next_due_at`` and ``suppressed_until`` were computed under the
    *previous* generation's profile. Carried across a switch, they hold a
    member back by a schedule nobody asked for any more: on 2026-09-23 six of
    the eight `tuning` DIDs waited 37.5 s, because `default` reads a DID once
    per 60 s window and their next `default` window was still 37.5 s off. A
    DID `default` does not poll (0x1209) had no stale due time and started at
    once. Reproduced on the fake clock in ``tests/test_capture_start_latency.py``.

    So a new generation starts every selected member now, exactly as a loop
    start does, and re-derives a miss back-off under the new policy (a
    `default` 30 s bench becomes a capture's 1 s retry). Neither is ever
    lengthened. A back-off still standing on its own evidence (a channel
    that has never answered) is kept, and a request whose rate was zero when
    planned (``suppressed_until`` is infinite) is not revived here.

    "Selected" is the active profile's selection when ``profile_gated`` (the
    same gate the loop applies), else every request.

    Returns how many requests were brought forward.
    """
    moved = 0
    for index, (request, state) in enumerate(zip(requests, states, strict=True)):
        if request.kind not in PROFILE_CADENCE_KINDS:
            continue
        member = tuning_profile.request_eligible(index, request)
        if profile_gated and not member:
            continue
        if state.suppressed_until == float("inf") or _request_rate_hz(request, spec) <= 0.0:
            continue
        before = (state.next_due_at, state.suppressed_until)
        state.next_due_at = min(state.next_due_at, now)
        if state.suppressed_until > now and state.missed_at is not None:
            # The same policy the loop applies to the next miss.
            focused = tuning_profile.is_active() and member
            rederived = state.missed_at + _miss_backoff_seconds(state, focused_capture=focused)
            state.suppressed_until = min(state.suppressed_until, rederived)
        if (state.next_due_at, state.suppressed_until) != before:
            moved += 1
    return moved


def rearm_on_engine_start(
    requests: Sequence[PollRequest],
    states: Sequence[PollRequestState],
    now: float,
) -> int:
    """Lift every miss back-off when the engine starts; returns how many moved.

    A back-off is evidence about a channel *in the state it was probed in*.
    Some channels are state-gated: on 2026-09-20 the Xterra ECM left
    TIMING_ADVANCE (PID 0x0E) out of every batched answer through 35 minutes
    of key-on-engine-off. The miss back-off for a request that has never
    answered doubles to ``MAX_MISS_BACKOFF_SECONDS``, so after the engine
    started TIMING_ADVANCE stayed silent for another 9.9-10.9 minutes. The
    first running RPM row came 593 s before the first TIMING_ADVANCE row, and
    the start itself fell inside a 61.7 s gap before that. It read 0.47/s over
    a drive whose other fast channels read 0.78/s (both counts over the whole
    session). On the fake clock the same 35-minute KOEO left it silent
    10.6 minutes past the start. The regression test pins a shorter case
    (``tests/test_default_profile_core_rate.py``).

    Which reading is a start is :class:`EngineStartLatch`'s decision: once per
    real engine-off, never on RPM flapping across the running floor.

    An engine start is new evidence for every backed-off channel, so each one
    is due now. ``missed_responses`` is kept: a channel that is still silent
    with the engine running goes straight back to the back-off it had
    earned. The cost is one probe per channel per engine start, and a probe
    that rides a Mode 01 batch costs nothing more than its identifier byte.

    Requests parked at ``inf`` (rate zero) are not revived, and neither are
    DTC, service or freeze-frame polls, which run on their own intervals.
    """
    moved = 0
    for request, state in zip(requests, states, strict=True):
        if request.kind not in PROFILE_CADENCE_KINDS or state.missed_at is None:
            continue
        if not now < state.suppressed_until < float("inf"):
            continue
        state.suppressed_until = now
        state.next_due_at = min(state.next_due_at, now)
        moved += 1
    return moved


#: How long the engine must read *off* before the next start counts as one.
#: "Off" is the project's KOEO definition: RPM exactly 0 with OBD answering
#: (``vehicle_state``'s ``ignition_only``). Readings between 0 and
#: ``ENGINE_RUNNING_MIN_RPM`` (cranking, a stumble) are neither off nor
#: running. The hold is the first miss back-off step: an engine-off shorter
#: than that cannot have earned a back-off longer than it, so re-probing
#: after it gains nothing.
ENGINE_OFF_HOLD_SECONDS = AUTO_MISS_BACKOFF_SECONDS


@dataclass
class EngineStartLatch:
    """Recognises an engine start from RPM readings, once per real engine-off.

    A start is a running reading (RPM >= ``ENGINE_RUNNING_MIN_RPM``) after
    RPM has read 0 for at least ``ENGINE_OFF_HOLD_SECONDS`` since the last
    running reading. Without the hold, every upward crossing of the running
    floor was a start. RPM flapping 390/410 on alternate reads re-probed a
    never-answering PID 150 times in 300 s instead of 4. At a batch size of 1
    the probes took RPM from 297 reads to 200 (verifier, 2026-09-26).

    A loop that starts with the engine running has seen no engine-off, so its
    first running reading is not a start.
    """

    #: First RPM == 0 reading since the last running reading, else ``None``.
    off_since: float | None = None
    armed: bool = False

    def observe(self, rpm: float, now: float) -> bool:
        """Feed one RPM reading; ``True`` when it is an engine start."""
        if rpm >= ENGINE_RUNNING_MIN_RPM:
            started = self.armed
            self.armed = False
            self.off_since = None
            return started
        if rpm == 0:
            if self.off_since is None:
                self.off_since = now
            if now - self.off_since >= ENGINE_OFF_HOLD_SECONDS:
                self.armed = True
        return False


def _initial_request_states(
    requests: list[PollRequest],
    spec: CanObdSpec,
    now: float,
) -> list[PollRequestState]:
    if not requests:
        return []
    spacing = 1.0 / _bus_budget_requests_per_second(spec)
    states: list[PollRequestState] = []
    for index, request in enumerate(requests):
        states.append(PollRequestState(next_due_at=now + index * spacing))
        if _request_rate_hz(request, spec) <= 0.0:
            states[-1].suppressed_until = float("inf")
    return states


def _request_rate_hz(
    request: PollRequest, spec: CanObdSpec, view: ProfileView | None = None
) -> float:
    """The rate policy: pure in ``(request, spec, view)`` (R02.3).

    ``view`` is the capture profile whose policy applies — the active one
    (``tuning_profile.view()``) when omitted, a candidate for the preview.
    """
    if request.kind == "dtc":
        # Fixed cadence (Task 1.3), independent of `poll_rate_hz`/tier
        # scaling — the DTC poll doesn't get faster just because the fast
        # telemetry tier does.
        interval = spec.dtc_poll_interval_seconds
        return 0.0 if interval <= 0 else 1.0 / interval
    if request.kind == "service":
        # Same fixed-cadence model as `dtc` above (Task 3.1), own knob.
        interval = spec.service_poll_interval_seconds
        return 0.0 if interval <= 0 else 1.0 / interval
    if view is None:
        view = tuning_profile.view()
    base = max(float(spec.poll_rate_hz), 0.001)
    rate = base * TIER_RATE_FACTORS.get(request.poll_tier, TIER_RATE_FACTORS["medium"])
    if request.in_session_window:
        # An all-DID capture needs its rate sayable too. Outside a capture
        # this returns None and DID cadence stays the window controller's
        # business, as before.
        absolute = view.request_absolute_rate_hz(request)
        weight = view.did_rate_weight(request.channel)
        if spec.did_session is not None and not view.profile.did_continuous:
            # A window is the only opportunity to sample this DID. Weight 1
            # therefore means every window (not the ordinary medium tier),
            # and a fractional weight skips boundaries. More than 1 cannot
            # create a second read inside one window.
            window_period = view.did_window_period(spec.did_session)
            wanted = absolute if absolute is not None else weight / window_period
            return min(wanted, 1.0 / window_period)
        if absolute is not None:
            return absolute
        rate *= weight
    if request.kind == "pid":
        # A weight is a relative share within the selected profile; 1.0 is
        # byte-for-byte the historic tier cadence.
        #
        # A capture profile may pin an absolute rate instead. The relative
        # model tops out at `poll_rate_hz` (1.0) per channel however much bus
        # budget exists, which is not a rate a tuning capture can be expressed
        # in — see CaptureProfile.target_rate_hz.
        absolute = view.absolute_rate_hz(request.channel)
        if absolute is not None:
            return absolute

        weight, uniform_cadence = view.rate_policy(request.channel)
        if uniform_cadence:
            # Tiers exist so a minute-scale signal doesn't burn budget at a
            # fast channel's cadence — a monitoring optimization. A capture
            # round is a *sample*: the operator picked these channels to see
            # them together, so they all run at the base cadence or they can
            # never share a round.
            rate = base
        rate *= weight
    return rate


def _request_interval_seconds(
    request: PollRequest, spec: CanObdSpec, view: ProfileView | None = None
) -> float:
    rate = _request_rate_hz(request, spec, view)
    if rate <= 0.0:
        return float("inf")
    return 1.0 / rate


def _next_request_due_at(
    request: PollRequest,
    state: PollRequestState,
    spec: CanObdSpec,
) -> float:
    view = tuning_profile.view()
    interval = _request_interval_seconds(request, spec, view)
    if interval == float("inf"):
        return float("inf")
    now = time.monotonic()
    # Every cadence is a grid: advance from the due time just served and skip
    # whole missed periods (never a catch-up burst). Anchoring on the request
    # instant instead adds every delay to every later period, and it did so
    # twice over (D03.34, 2026-09-26):
    #
    # - A windowed DID was found a few milliseconds late at each boundary, so
    #   a 4 Hz plan silently skipped every other window.
    # - A round member may be issued up to one period *before* its due time
    #   (``_select_next_due_request``). Anchored on that instant, the advance
    #   recurred every cycle, so a slower request's interval collapsed to a
    #   whole number of round periods. Medium PIDs planned at 3.0 s ran every
    #   2.4 s under a 0.8 s round: +22 % on the fake clock. A request delayed
    #   instead (a DID window, a timed-out single, a busy budget slot) lost
    #   the delay from every later period.
    next_due = state.next_due_at + interval
    if next_due <= now:
        next_due += (math.floor((now - next_due) / interval) + 1) * interval
    return max(next_due, state.suppressed_until)


def _next_due_at(
    requests: list[PollRequest],
    states: list[PollRequestState],
    now: float,
    *,
    eligible: RequestFilter | None = None,
    among: Container[int] | None = None,
) -> float | None:
    due_times = [
        state.next_due_at
        for index, (request, state) in enumerate(zip(requests, states, strict=True))
        if not _is_request_suppressed(state, now)
        and (among is None or index in among)
        and (eligible is None or eligible(index, request))
    ]
    return min(due_times) if due_times else None


def _next_unsuppressed_at(
    requests: list[PollRequest],
    states: list[PollRequestState],
    now: float,
    *,
    eligible: RequestFilter | None = None,
) -> float | None:
    """Earliest instant a currently backed-off eligible request re-arms.

    The wake deadline when nothing is pollable *yet*: without it the loop
    idled on its flat tick and noticed a lifted backoff up to a tick late.
    Requests parked at ``inf`` (rate zero) never count.
    """
    ends = [
        state.suppressed_until
        for index, (request, state) in enumerate(zip(requests, states, strict=True))
        if _is_request_backed_off(state, now) and (eligible is None or eligible(index, request))
    ]
    return min(ends) if ends else None


def _is_request_suppressed(state: PollRequestState, now: float) -> bool:
    return state.suppressed_until > now


def _is_request_backed_off(state: PollRequestState, now: float) -> bool:
    return _is_request_suppressed(state, now) and state.suppressed_until != float("inf")
