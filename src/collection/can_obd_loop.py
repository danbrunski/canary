"""The single-bus CAN + OBD scheduling loop (roadmap R02.9).

``run_can_obd_loop`` owns one bus: it records every received frame and
interleaves active OBD requests on it, under the request plan, capture rounds,
the Mode-22 session controller, support discovery and the bus budget. It was
a 727-line function threading ~40 locals; it is now :class:`_CanObdLoop`,
whose state is attributes and whose steps are methods, with the two
hand-mirrored batch-feedback blocks folded into one :class:`BatchGovernor`
per service. Moved out of ``can_obd_collector``, which re-exports the
public names.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Container, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal

import can

from src.collection import bus_budget, capture_generation, tuning_profile, tx_gate
from src.collection import discovery as discovery_registry
from src.collection.calibration_id import (
    CalibrationIdSchedule,
)
from src.collection.can_obd_client import (
    CanObdClient,
    FrameTap,
)
from src.collection.can_obd_io import (
    _PARSE_ERROR_OWNER,
    _drain_frames_until,
    _query_batched_with_tap,
    _query_with_tap,
    _read_calibration_id,
    _send_session_control,
    _should_stop,
)
from src.collection.discovery import (
    DiscoveryItem,
    DiscoveryState,
)
from src.collection.frame_time import FrameArrival
from src.collection.hybrid_cycle import CyclePlan, WakeDecision, next_wake_at, plan_cycle
from src.collection.isotp import (
    DEFAULT_RESPONSE_ID_MIN,
    MAX_BATCHED_DIDS,
    ObdResponse,
)
from src.collection.obd_reading import (
    ObdReading,
)
from src.collection.obd_response_decode import _response_to_reading
from src.collection.obd_scheduler import (
    AUTO_MISS_BACKOFF_SECONDS,
    CanObdLoopStats,
    CanObdSpec,
    DidWindowController,
    EngineStartLatch,
    ObdSchedulerMetrics,
    PollRequest,
    PollRequestState,
    RequestFilter,
    RoundScheduler,
    RoundState,
    _bus_budget_requests_per_second,
    _count_active_requests,
    _DiscoveryClient,
    _initial_request_states,
    _next_due_at,
    _next_request_due_at,
    _next_unsuppressed_at,
    _scheduler_metrics,
    _select_next_due_request,
    _update_response_state,
    apply_mode01_support,
    build_poll_requests,
    is_batchable,
    mode06_requests,
    rearm_on_engine_start,
    replan_for_generation,
    round_members,
    round_period_seconds,
    select_batch_companions,
    select_did_batch_companions,
)
from src.collection.obd_scheduler import discovery_items as _discovery_items
from src.collection.queueing import (
    flush_dropped_can_frame_log,
)
from src.collection.request_eligibility import Eligibility, SessionPhase, evaluate

log = logging.getLogger(__name__)


def _loop_request_verdict(
    controller: DidWindowController | None,
    profile_gated: bool,
    forced: Container[int],
    index: int,
    request: PollRequest,
) -> Eligibility:
    """Both gates in one matrix call (C06.7): the session controller's own
    verdict (phase and the window's pending set), given the active profile's
    selection and pause and the forced one-shot lift for ``trigger_poll``.
    Without a controller the phase is ``none``. The window check lives in
    ``DidWindowController.request_verdict`` only (R02.2)."""
    selection = tuning_profile.selection_exclusion(request) if profile_gated else None
    paused = tuning_profile.is_paused() if profile_gated else False
    is_forced = index in forced
    if controller is None:
        return evaluate(
            request.kind,
            tuning_profile.profile_class(),
            "none",
            selection=selection,
            paused=paused,
            forced=is_forced,
        )
    return controller.request_verdict(
        index, request, selection=selection, paused=paused, forced=is_forced
    )


def _loop_request_eligible(
    controller: DidWindowController | None,
    profile_gated: bool,
    forced: Container[int],
    index: int,
    request: PollRequest,
) -> bool:
    return _loop_request_verdict(controller, profile_gated, forced, index, request).eligible


class _FrameTap:
    """The loop's one raw-capture tap, shared with its client (C06.2).

    Counts every frame for ``CanObdLoopStats`` and, while a discovery probe
    is waiting, the frames that the old synchronous path silently consumed.
    """

    def __init__(self, on_frame: FrameTap, stats: CanObdLoopStats) -> None:
        self._on_frame = on_frame
        self._stats = stats
        self.discovering = False

    def __call__(self, msg: can.Message) -> None:
        self._stats.frames_seen += 1
        if self.discovering:
            self._stats.discovery_frames_seen += 1
        self._on_frame(msg)


AUTO_PROBE_TIMEOUT_SECONDS = 0.25


SCHEDULER_METRICS_INTERVAL_SECONDS = 5.0


#: Consecutive whole-batch failures before the session narrows its batch size.
#: Small on purpose: an ECU that dislikes a given size should cost a handful
#: of request slots to discover, not a whole drive.
MAX_CONSECUTIVE_BATCH_FAILURES = 3


#: Bound on the ``deferred`` list in the scheduler metrics (C06.7): a focused
#: capture can leave dozens of the union plan's channels unselected.
MAX_DEFERRED_REPORT = 64


#: Discovery wait reasons that clear within a slot or a round on their own.
TRANSIENT_DISCOVERY_WAITS: tuple[str, ...] = ("budget", "discovery_budget", "round_open")


def _next_batch_size(current: int) -> int:
    """Step a failing batch size down: 6 -> 4 -> 2 -> 1 (off).

    Halving rather than disabling outright, because "supports batching" is not
    binary. An ECU that answers four PIDs but refuses six would otherwise lose
    the feature entirely, when it was worth 4x. Reaching 1 disables batching
    for the session.
    """
    if current <= 2:
        return 1
    if current > 4:
        return 4
    return 2


def disable_batching(_current: int) -> int:
    """The Mode 22 narrowing strategy: one failed run disables batching.

    A UDS session either accepts multi-DID reads or refuses them outright
    (the 2008 Nissan ECM answers ``7F 22 12`` to any), so there is no
    smaller size worth trying.
    """
    return 1


class BatchGovernor:
    """Whether and how wide one service batches, learned by using it.

    Batching is opt-in and self-disabling. An ECU that ignores or refuses
    multi-identifier requests would otherwise fail every batch forever, so a
    run of consecutive failures narrows (``narrow``: the Mode 01 policy
    halves, the Mode 22 policy disables). This doubles as the adaptive form
    of the startup size probe: we discover what the vehicle accepts by using
    it.

    But a silent batch is only evidence about batching when the ECU is
    awake. The Pi boots with the key off (ECM asleep, every request times
    out), and counting that silence narrowed 6 -> 4 -> 2 -> off within
    seconds and cost the whole drive its batching (2026-09-18). So a failed
    batch counts only when the ECU answered *something* since the previous
    failure (or refused the batch with an NRC, which is itself an answer).
    Otherwise it is not counted, and the next request of that service goes
    out single as a probe: an awake ECU that ignores batches answers the
    probe, and its next batch failure then counts; an asleep ECU keeps
    producing silence and nothing narrows.

    One governor per service; this replaced a hand-mirrored ~65-line block
    over ten loop locals (R02.9).
    """

    def __init__(
        self,
        max_size: int,
        *,
        narrow: Callable[[int], int],
        kind: Literal["pid", "did"],
        stats: CanObdLoopStats,
    ) -> None:
        self.kind = kind
        self.configured = max_size
        self.enabled = max_size > 1
        self.active_max = max_size
        self.consecutive_failures = 0
        self.ecu_answered_since_failure = False
        #: The next request of this service goes out single, as the liveness
        #: probe a silent batch asked for.
        self.probe_single = False
        self._narrow = narrow
        self._stats = stats

    @property
    def effective_max(self) -> int:
        """The batch size in force: 1 once batching is disabled."""
        return self.active_max if self.enabled else 1

    def size_for_next(self) -> int:
        """Members the next request of this service may carry."""
        return self.active_max if self.enabled and not self.probe_single else 1

    def on_ecu_answer(self) -> None:
        """The ECU answered something (a reading or an NRC): it is awake."""
        self.ecu_answered_since_failure = True

    def on_single(self, answered: bool) -> bool:
        """A single request of this service completed.

        Returns True when it was the liveness probe and the ECU stayed
        silent: still asleep, so no evidence about the channel either.
        """
        was_probe = self.probe_single
        self.probe_single = False
        return was_probe and not answered

    def on_batch(self, answered: bool, negative_response_code: int | None) -> None:
        """A batch of this service completed: success, refusal or silence."""
        if answered:
            self.consecutive_failures = 0
            return
        label = "multi-DID" if self.kind == "did" else "multi-PID"
        # Silence (not an NRC) means the next request of this service must
        # re-establish that the ECU is awake before another silent batch can
        # count.
        counts = self.ecu_answered_since_failure
        self.ecu_answered_since_failure = False
        self.probe_single = negative_response_code is None
        if not counts:
            # Debug only: a key-off ECU produces one of these per budget slot
            # for as long as it sleeps.
            if self.kind == "did":
                self._stats.did_batch_failures_ecu_silent += 1
            else:
                self._stats.batch_failures_ecu_silent += 1
            log.debug(
                "%s batch failure not counted toward narrowing: no ECU "
                "answer since the previous failure; probing single",
                label,
            )
            return
        self.consecutive_failures += 1
        if self.consecutive_failures < MAX_CONSECUTIVE_BATCH_FAILURES:
            return
        self.consecutive_failures = 0
        narrowed = self._narrow(self.active_max)
        if self.kind == "did":
            self._stats.did_batch_size_narrowings += 1
        else:
            self._stats.batch_size_narrowings += 1
        if narrowed <= 1:
            self.enabled = False
            if self.kind == "did":
                log.warning(
                    "multi-DID batching disabled for this ECU/session; "
                    "falling back to healthy individual DID requests"
                )
            else:
                log.warning(
                    "multi-PID batching disabled: no batch size this ECU "
                    "accepts; falling back to single-PID for this session",
                    extra={"configured_batch_max_pids": self.configured},
                )
            return
        log.warning(
            "narrowing %s batch size %d -> %d after %d consecutive failures",
            label,
            self.active_max,
            narrowed,
            MAX_CONSECUTIVE_BATCH_FAILURES,
        )
        self.active_max = narrowed

    def metrics(self) -> dict[str, Any]:
        return {
            "configured_max": self.configured,
            "enabled": self.enabled,
            "active_max": self.effective_max,
            "consecutive_failures": self.consecutive_failures,
            "probe_single": self.probe_single,
        }


class _CanObdLoop:
    """One run of the single-bus loop; see :func:`run_can_obd_loop`."""

    def __init__(
        self,
        bus: can.BusABC,
        spec: CanObdSpec,
        *,
        on_frame: Callable[[can.Message], None],
        on_reading: Callable[[ObdReading], None],
        stop_event: threading.Event,
        deadline_monotonic: float | None,
        client_factory: Callable[[can.BusABC, FrameTap], _DiscoveryClient] | None,
        on_scheduler_metrics: Callable[[ObdSchedulerMetrics], None] | None,
        scheduler_metrics_interval_seconds: float,
    ) -> None:
        self.bus = bus
        self.spec = spec
        self.on_frame = on_frame
        self._deliver_reading = on_reading
        self.on_reading = self._observe_reading
        #: Engine starts, recognised once per real engine-off, and one not yet
        #: acted on: back-offs earned with the engine off are lifted on the
        #: next tick (``rearm_on_engine_start``), never mid-batch.
        self.engine_start = EngineStartLatch()
        self.engine_start_pending = False
        self.stop_event = stop_event
        self.deadline = deadline_monotonic
        self.on_scheduler_metrics = on_scheduler_metrics
        self.metrics_interval = scheduler_metrics_interval_seconds
        self.stats = CanObdLoopStats()
        stats = self.stats
        # Every (re)start of the loop is a new capture generation (C02.8): a bus
        # rebuild after a fault must not let a request issued before it complete
        # a round scheduled after it.
        self.rounds = RoundScheduler(capture_generation.bump("collector-start"), stats)
        self.tap = _FrameTap(on_frame, stats)
        self.client: _DiscoveryClient = (
            CanObdClient(
                bus,
                timeout_seconds=spec.request_timeout_seconds,
                time_fn=time.monotonic,
                on_frame=self.tap,
                on_parse_error=stats.count_parse_error,
                parse_error_owner=_PARSE_ERROR_OWNER,
            )
            if client_factory is None
            else client_factory(bus, self.tap)
        )
        self.requests = build_poll_requests(None, spec)
        self.request_states = _initial_request_states(self.requests, spec, time.monotonic())
        self.discovery = discovery_registry.recall(spec.discovery_key, _discovery_items(spec))
        self.installed: set[DiscoveryItem] = set()
        _install_discovery(
            self.discovery,
            self.requests,
            self.request_states,
            spec,
            time.monotonic(),
            self.installed,
        )
        self.discovery_wait: str | None = None
        #: The last *structural* reason discovery waited since its last probe
        #: (a phase, a pause, capacity). ``budget`` and ``round_open`` are
        #: transient by nature and would make the reported reason flicker.
        self.discovery_blocked: str | None = None
        #: Request indices an operator forced due (``trigger_poll``), until served.
        self.forced: set[int] = set()
        self.profile_gated = spec.capture_profiles_enabled or tuning_profile.is_active()
        self.budget_rps = _bus_budget_requests_per_second(spec)
        # Everything the Mode-22 window serves, not only Mode 22 itself:
        # service-0x23 memory reads are gated by the same session on this ECM,
        # so leaving them out would schedule every one of them in the phase
        # where the ECM is silent about them.
        self.did_indices = [
            index for index, request in enumerate(self.requests) if request.in_session_window
        ]
        self.freeze_frame_indices = [
            index for index, request in enumerate(self.requests) if request.kind == "freeze_frame"
        ]
        self.controller: DidWindowController | None = None
        if spec.did_session is not None and self.did_indices:
            self.controller = DidWindowController(
                spec.did_session, self.did_indices, now=time.monotonic()
            )
        self._log_start()
        self.next_budget_at = time.monotonic()
        self.next_discovery_at = time.monotonic()
        self.next_metrics_at = time.monotonic()
        self.pid_batching = BatchGovernor(
            spec.batch_max_pids, narrow=_next_batch_size, kind="pid", stats=stats
        )
        self.did_batching = BatchGovernor(
            min(spec.did_batch_max_dids, MAX_BATCHED_DIDS),
            narrow=disable_batching,
            kind="did",
            stats=stats,
        )
        # W02.2: every loop start is a (re)connect, so the id is owed again now.
        self.calibration_schedule = (
            CalibrationIdSchedule(spec.calibration_id, now=time.monotonic())
            if spec.calibration_id is not None
            else None
        )
        stats.loop_started_at = time.monotonic()

    # ------------------------------------------------------------------
    # The loop
    # ------------------------------------------------------------------

    def run(self) -> CanObdLoopStats:
        while not _should_stop(self.stop_event, self.deadline):
            self._tick()
        # One publisher for the in-loop and the exit snapshot: the exit call
        # used to omit the live batch sizes and report the configured ones.
        self._publish_metrics(time.monotonic())
        self.rounds.close(time.monotonic())
        discovery_registry.remember(self.discovery)
        # The loop owned the bus: a parse-error burst that ran to the end of
        # it is summarized here or never (one forced line, at a once-per-loop
        # boundary).
        flush_dropped_can_frame_log(owner=_PARSE_ERROR_OWNER, force=True)
        return self.stats

    def _tick(self) -> None:
        now = time.monotonic()
        self._on_generation_change(now)
        self._apply_triggers(now)
        self._apply_engine_start(now)
        if self.discovery.version != discovery_registry.cache_version():
            # ``reset_discovery``: every walk restarts; results already in
            # the plan stay (a re-walk only adds what it newly learns).
            self.discovery = discovery_registry.fresh(
                self.spec.discovery_key, _discovery_items(self.spec)
            )
            self.installed.clear()
        # The selected profile applies even for ``default``: the request plan
        # is a startup superset of every saved profile, and filtering here is
        # what keeps the seeded default byte-identical to config. Standalone
        # recorder/tests that never initialised the catalog keep the
        # scheduler's historical behaviour.
        self.profile_gated = self.spec.capture_profiles_enabled or tuning_profile.is_active()
        if self.on_scheduler_metrics is not None and now >= self.next_metrics_at:
            self._publish_metrics(now)
            self.next_metrics_at = now + max(0.1, self.metrics_interval)

        if tx_gate.is_passive():
            # Pure-monitoring mode. Keep receiving and tapping frames so
            # broadcast CAN logging, the anomaly pipeline and the status
            # snapshot all stay live — we simply issue nothing.
            #
            # The pause belongs HERE rather than at the send: blocking only
            # the transmit would leave every request waiting out its full
            # timeout, and `_update_response_state` would escalate each one
            # into the miss-backoff. Passive mode would then hand back a
            # scheduler with every PID suppressed once it lifted.
            self._drain_until(next_wake_at(now, deadline=self.deadline).at)
            return

        if not self.requests and not self.discovery.pending():
            self._drain_until(next_wake_at(now, deadline=self.deadline).at)
            return

        eligible, sent = self._tick_controller(now)
        if sent:
            return
        if self._maybe_read_calibration_id(now):
            return

        # A round ends at its deadline even while more work is due (C02.2);
        # this runs before any selection so no reading is ever stamped with
        # an expired round.
        self.rounds.expire(now)

        if self._maybe_probe_discovery(now, eligible):
            return
        discovery_wants_budget = self.discovery_wait == "budget"

        active_request_count = _count_active_requests(
            self.requests, self.request_states, now, eligible=eligible
        )
        if active_request_count == 0:
            self.rounds.idle(now)
            wake = _wake_for(
                now,
                self.controller,
                self.request_states,
                budget_at=self.next_budget_at,
                next_due=_next_unsuppressed_at(
                    self.requests, self.request_states, now, eligible=eligible
                ),
                deadline=self.deadline,
                budget_only_for_session=not discovery_wants_budget,
            )
            self._drain_until(wake.at)
            return

        if now < self.next_budget_at:
            wake = _wake_for(
                now,
                self.controller,
                self.request_states,
                budget_at=self.next_budget_at,
                next_due=None,
                deadline=self.deadline,
            )
            self._drain_until(wake.at)
            return

        current_round = self._ensure_round(now, eligible, discovery_wants_budget)
        if current_round is None:
            return
        preferred = _preferred_members(self.requests, current_round)
        selection = _select_next_due_request(
            self.requests,
            self.request_states,
            now,
            eligible=eligible,
            among=current_round.remaining,
            preferred=preferred,
        )
        if selection is None:
            # Members remain but none is pollable any more (the DID window
            # closed, a member got suppressed): the round cannot complete and
            # closes incomplete rather than hanging open until its deadline.
            self.rounds.close(now)
            return
        request_index, request, request_state = selection
        round_timestamp = (
            current_round.wall_start if tuning_profile.round_timestamps_enabled() else None
        )

        if request.kind == "did":
            batch_indices = select_did_batch_companions(
                self.requests,
                self.request_states,
                now,
                leader_index=request_index,
                max_dids=self.did_batching.size_for_next(),
                response_widths=self.spec.did_response_widths,
                eligible=eligible,
                members=current_round.remaining,
                preferred=preferred,
            )
        else:
            batch_indices = select_batch_companions(
                self.requests,
                self.request_states,
                now,
                leader_index=request_index,
                max_pids=self.pid_batching.size_for_next(),
                eligible=eligible,
                ignore_tier=round_timestamp is not None,
                members=current_round.remaining,
                preferred=preferred,
                widths=self.stats.mode01_widths,
            )
        if len(batch_indices) > 1:
            self._serve_batch(request_index, request, batch_indices, current_round, round_timestamp)
        else:
            self._serve_single(
                request_index, request, request_state, current_round, round_timestamp
            )

    # ------------------------------------------------------------------
    # Tick steps
    # ------------------------------------------------------------------

    def _on_generation_change(self, now: float) -> None:
        generation = capture_generation.current()
        if generation == self.rounds.generation:
            return
        # Profile change, pause/resume, passive toggle or replay
        # transition: rounds restart from zero under the new generation.
        self.rounds.reset(generation, now)
        # Every selected member starts now, under the new profile's
        # cadence and back-off policy, not the old one's (D05.1).
        gated = self.spec.capture_profiles_enabled or tuning_profile.is_active()
        replan_for_generation(
            self.requests, self.request_states, self.spec, now, profile_gated=gated
        )
        if self.controller is not None:
            # ...and any session transition in flight is completed rather
            # than left as a stale phase the new profile polls through
            # (C04.7); the next window is due now, not an interval later.
            self.controller.on_generation_change(
                now,
                profile=tuning_profile.active_profile_name(),
                members={
                    index: self.requests[index].channel
                    for index in self.did_indices
                    if not gated or tuning_profile.request_eligible(index, self.requests[index])
                },
            )

    def _apply_triggers(self, now: float) -> None:
        spec = self.spec
        if (
            self.freeze_frame_indices
            and spec.freeze_frame_trigger is not None
            and spec.freeze_frame_trigger.take()
        ):
            # Task 3.3: a stored DTC just appeared — un-suppress every
            # freeze-frame request for exactly this one shot. They go back
            # to "on_demand" dormancy on their own once each has fired once
            # (`_request_rate_hz` returns 0 for them, see `build_poll_requests`).
            for index in self.freeze_frame_indices:
                self.request_states[index].suppressed_until = 0.0
                self.request_states[index].next_due_at = now
        if spec.force_poll_trigger is not None:
            forced_channels = spec.force_poll_trigger.take()
            if forced_channels:
                for index, request in enumerate(self.requests):
                    if request.channel.upper() in forced_channels:
                        self.request_states[index].suppressed_until = 0.0
                        self.request_states[index].next_due_at = now
                        # Kept until served: the matrix lifts the profile
                        # selection for it but not the phase it needs (C06.7).
                        self.forced.add(index)

    def _observe_reading(self, reading: ObdReading) -> None:
        """Watch RPM for an engine start, then hand the reading on."""
        if (
            reading.pid == "RPM"
            and isinstance(reading.value, int | float)
            and self.engine_start.observe(float(reading.value), time.monotonic())
        ):
            self.engine_start_pending = True
        self._deliver_reading(reading)

    def _apply_engine_start(self, now: float) -> None:
        """An engine start re-probes every backed-off channel (see
        ``rearm_on_engine_start``): a PID the ECM withholds with the engine off
        must not sit out its key-on back-off once the engine runs."""
        if not self.engine_start_pending:
            return
        self.engine_start_pending = False
        moved = rearm_on_engine_start(self.requests, self.request_states, now)
        if moved:
            log.info(
                "engine started: re-probing %d backed-off request(s)",
                moved,
                extra={"rearmed": moved},
            )

    def _publish_metrics(self, now: float) -> None:
        """The one scheduler-metrics snapshot, in the loop and at its exit."""
        if self.on_scheduler_metrics is None:
            return
        controller = self.controller
        wait = self.discovery_blocked or self.discovery_wait
        self.on_scheduler_metrics(
            _scheduler_metrics(
                self.requests,
                self.request_states,
                self.spec,
                self.stats,
                now,
                did_profile=controller.snapshot(now) if controller is not None else None,
                rounds=self.rounds.snapshot(now),
                discovery=self.discovery.snapshot(now, deferred_reason=wait),
                deferred=_deferred_report(
                    self.requests,
                    self.request_states,
                    partial(_loop_request_verdict, controller, self.profile_gated, self.forced),
                    now,
                    discovery=self.discovery,
                    discovery_wait=wait,
                ),
                pid_batch_max=self.pid_batching.effective_max,
                did_batch_max=self.did_batching.effective_max,
            )
        )
        self.stats.scheduler_snapshots += 1

    def _tick_controller(self, now: float) -> tuple[RequestFilter | None, bool]:
        """Drive the Mode-22 session controller; ``(eligible, sent)``.

        ``sent`` is True when an exit, entry or keep-alive went out (the tick
        is over); ``eligible`` is the request filter for the rest of it.
        """
        controller = self.controller
        if controller is not None and _controller_engaged(controller, self.profile_gated):
            dids_selected = True
            if self.profile_gated:
                # The startup request plan is the union of every saved
                # profile. A DID outside the active profile must not keep
                # this window's pending set alive until its max-duration
                # timeout.
                dids_selected = False
                for did_index in self.did_indices:
                    if tuning_profile.request_eligible(did_index, self.requests[did_index]):
                        dids_selected = True
                    else:
                        controller.on_request_attempted(did_index)
            phase_before = controller.phase
            if not dids_selected:
                # No DID is selected right now (profile without DIDs, Mode 22
                # switched off, or the capture is paused), yet the controller
                # still holds or is leaving the session: complete the
                # transition and open nothing new — polling Mode 01 into a
                # held session is a blackout that looks like a dead bus, and
                # a paused capture must not churn entry/exit pairs (C04.7).
                controller.release(now)
            controller.advance(self.request_states, now)
            if controller.phase == "mode01" and controller.cycle_mode():
                controller.set_plan(
                    _cycle_plan_for(
                        self.requests,
                        self.request_states,
                        self.spec,
                        controller,
                        now,
                        profile_gated=self.profile_gated,
                        batch_max_pids=self.pid_batching.effective_max,
                    )
                )
            sent = _drive_session_controller(
                self.bus,
                self.spec,
                controller,
                self.request_states,
                now=now,
                next_budget_at=self.next_budget_at,
                allow_entry=dids_selected,
                on_frame=self.on_frame,
                stop_event=self.stop_event,
                stats=self.stats,
                deadline_monotonic=self.deadline,
            )
            if controller.phase != phase_before:
                # A phase change is a new sampling window: the other
                # service's round grid must not hold this one's first round
                # back by up to a period (a DID sweep waited 0.21 s after its
                # window opened under a shared 4 Hz target).
                self.rounds.idle(now)
            if sent:
                self._spend_budget_slot()
                return None, True
            eligible = partial(_loop_request_eligible, controller, self.profile_gated, self.forced)
            return eligible, False
        if self.profile_gated:
            # A profile with no DIDs deliberately suspends the session
            # controller, concentrating the budget on its Mode-01 selection.
            return partial(_loop_request_eligible, None, True, self.forced), False
        return None, False

    def _maybe_read_calibration_id(self, now: float) -> bool:
        schedule = self.calibration_schedule
        if (
            schedule is None
            or not schedule.due(now)
            or now < self.next_budget_at
            or tuning_profile.is_paused()
            or not schedule.phase_allows(
                self.controller.phase if self.controller is not None else "none"
            )
        ):
            return False
        # One identification read in this budget slot: inside a held
        # Mode-22 window when the id needs the session, bare otherwise.
        schedule.on_result(
            _read_calibration_id(
                self.bus,
                schedule.spec,
                timeout_seconds=self.spec.request_timeout_seconds,
                on_frame=self.on_frame,
                stop_event=self.stop_event,
                stats=self.stats,
                deadline_monotonic=self.deadline,
            ),
            now=time.monotonic(),
        )
        self._spend_budget_slot()
        return True

    def _maybe_probe_discovery(self, now: float, eligible: RequestFilter | None) -> bool:
        """Support discovery as scheduler work (C06.4/C06.6): one probe per
        budget slot, in a phase the matrix admits, and — under a capture
        profile — only in a slot no round member needs. Otherwise it waits
        and says why. True when a probe went out."""
        self.discovery_wait = None
        discovery_item = self.discovery.due(now)
        if discovery_item is None:
            self.discovery_blocked = None
            return False
        if now < self.next_discovery_at:
            self.discovery_wait = "discovery_budget"
        else:
            self.discovery_wait = _discovery_wait_reason(
                self.controller,
                self.rounds,
                self.requests,
                self.request_states,
                self.spec,
                now,
                eligible=eligible,
                next_budget_at=self.next_budget_at,
                budget_rps=self.budget_rps,
            )
        if self.discovery_wait not in (None, *TRANSIENT_DISCOVERY_WAITS):
            self.discovery_blocked = self.discovery_wait
        if self.discovery_wait is not None:
            return False
        self.discovery_blocked = None
        stats = self.stats
        answers_before = stats.discovery_responses + stats.discovery_negative_responses
        self.discovery = _issue_discovery_probe(
            self.client,
            self.discovery,
            discovery_item,
            tap=self.tap,
            stats=stats,
            stop_event=self.stop_event,
            deadline_monotonic=self.deadline,
        )
        _install_discovery(
            self.discovery,
            self.requests,
            self.request_states,
            self.spec,
            time.monotonic(),
            self.installed,
        )
        discovery_registry.remember(self.discovery)
        if stats.discovery_responses + stats.discovery_negative_responses > answers_before:
            self._on_ecu_answer()
        self._spend_budget_slot()
        self.next_discovery_at = time.monotonic() + (
            1.0 / self.spec.discovery_budget_requests_per_second
        )
        return True

    def _ensure_round(
        self, now: float, eligible: RequestFilter | None, discovery_wants_budget: bool
    ) -> RoundState | None:
        """The open round, opening one if due; ``None`` after draining to the
        next relevant deadline when nothing can open."""
        if self.rounds.current is None:
            opened, wait_until = _open_round(
                self.rounds,
                self.requests,
                self.request_states,
                self.spec,
                now,
                time.time(),
                eligible=eligible,
            )
            if opened is None:
                # Never spin: with active requests but nothing to open, the
                # bus is still received until the earliest relevant deadline
                # (or the budget slot a pending discovery probe can use).
                wake = _wake_for(
                    now,
                    self.controller,
                    self.request_states,
                    budget_at=self.next_budget_at if discovery_wants_budget else None,
                    next_due=wait_until,
                    deadline=self.deadline,
                )
                self._drain_until(wake.at)
                return None
        current_round = self.rounds.current
        assert current_round is not None
        return current_round

    def _serve_batch(
        self,
        request_index: int,
        request: PollRequest,
        batch_indices: list[int],
        current_round: RoundState,
        round_timestamp: float | None,
    ) -> None:
        controller = self.controller
        batch_started = time.monotonic()
        outcome = _issue_batched_request(
            self.bus,
            self.requests,
            self.request_states,
            batch_indices,
            spec=self.spec,
            on_frame=self.on_frame,
            on_reading=self.on_reading,
            stop_event=self.stop_event,
            stats=self.stats,
            deadline_monotonic=self.deadline,
            round_timestamp=round_timestamp,
            round_state=current_round,
        )
        if controller is not None:
            controller.on_service_time(
                "mode22" if request.kind == "did" else "mode01",
                time.monotonic() - batch_started,
            )
            if outcome.observed:
                controller.on_response(request_index, time.monotonic(), outcome.acquired_at)
        if outcome.answered or outcome.negative_response_code is not None:
            self._on_ecu_answer()
        if outcome.answered:
            self.discovery = self.discovery.on_contact(time.monotonic())
        governor = self.did_batching if request.kind == "did" else self.pid_batching
        governor.on_batch(outcome.answered, outcome.negative_response_code)
        if controller is not None:
            for index in batch_indices:
                controller.on_request_attempted(index)
        for index in batch_indices:
            self.request_states[index].next_due_at = _next_request_due_at(
                self.requests[index], self.request_states[index], self.spec
            )
            self.forced.discard(index)
            self.rounds.on_attempt(index)
            observed = outcome.observed.get(index, 0)
            if observed:
                self.rounds.on_observed(index, observed, outcome.acquired_at)
        _close_round_if_complete(self.rounds, time.monotonic())
        self._spend_budget_slot()

    def _serve_single(
        self,
        request_index: int,
        request: PollRequest,
        request_state: PollRequestState,
        current_round: RoundState,
        round_timestamp: float | None,
    ) -> None:
        controller = self.controller
        stats = self.stats
        timeout_seconds = self.spec.request_timeout_seconds
        if request.auto_probe and (request.fast_probe or request_state.missed_responses > 0):
            timeout_seconds = min(timeout_seconds, AUTO_PROBE_TIMEOUT_SECONDS)
        request_started = time.monotonic()
        request_state.last_requested_at = request_started
        request_state.requests_sent += 1
        try:
            result = _query_with_tap(
                self.bus,
                request,
                timeout_seconds=timeout_seconds,
                on_frame=self.on_frame,
                stop_event=self.stop_event,
                stats=stats,
                deadline_monotonic=self.deadline,
            )
        except Exception:  # noqa: BLE001 - propagate after recording scheduler state
            request_state.bus_errors += 1
            stats.bus_errors += 1
            raise
        if controller is not None:
            controller.on_service_time(
                controller.service_kind(request_index), time.monotonic() - request_started
            )
        emitted = False
        self.rounds.on_attempt(request_index)
        if result.response is not None and is_batchable(request):
            # The single-PID payload is bounded by the ISO-TP length, so its
            # size *is* this vehicle's width for the PID — the measurement the
            # batch walk otherwise takes on trust from the registry.
            stats.mode01_widths.observe_single(request.identifier, len(result.response.payload))
        if result.response is not None:
            readings = _response_to_reading(
                request,
                result.response,
                timestamp=round_timestamp,
                arrival=result.arrival,
                response_interval_s=result.response_interval_s,
                round_state=current_round,
            )
            if readings:
                emitted = True
                stats.readings_emitted += len(readings)
                acquired_at = result.arrival.at if result.arrival is not None else None
                self.rounds.on_observed(request_index, len(readings), acquired_at)
                for reading in readings:
                    stats.record_read(reading.pid, time.monotonic())
                    self.on_reading(reading)
        answered = emitted or result.negative_response_code is not None
        # This single is the liveness probe a silent batch asked for.
        governor = self.did_batching if request.kind == "did" else self.pid_batching
        silent_probe = governor.on_single(answered)
        if answered:
            self._on_ecu_answer()
        if emitted:
            request_state.responses += 1
            request_state.last_response_at = time.monotonic()
            self.discovery = self.discovery.on_contact(time.monotonic())
            if controller is not None:
                controller.on_response(
                    request_index,
                    time.monotonic(),
                    result.arrival.at if result.arrival is not None else None,
                )
        elif result.negative_response_code is not None:
            # Formal ECU refusal, not a true timeout — counted separately
            # (see `QueryResult`) so it doesn't masquerade as bus silence.
            request_state.negative_responses += 1
            request_state.last_negative_response_code = result.negative_response_code
            stats.negative_responses += 1
        else:
            request_state.timeouts += 1
            stats.request_timeouts += 1
        if not silent_probe:
            # A silent liveness probe is the ECU still asleep, like the batch
            # before it: no evidence about this channel, so no miss backoff.
            # Otherwise a key-off boot would back every channel off (doubling
            # to 15 minutes for never-answered ones) one probe at a time.
            _update_response_state(
                request_state,
                emitted=emitted,
                now=time.monotonic(),
                focused_capture=(
                    tuning_profile.is_active()
                    and tuning_profile.request_eligible(request_index, request)
                ),
            )
        if controller is not None:
            controller.on_request_attempted(request_index)
        self.forced.discard(request_index)

        request_state.next_due_at = _next_request_due_at(request, request_state, self.spec)
        _close_round_if_complete(self.rounds, time.monotonic())
        self._spend_budget_slot()

    # ------------------------------------------------------------------
    # Shared small steps
    # ------------------------------------------------------------------

    def _on_ecu_answer(self) -> None:
        """Any answer is proof of life for both services' batch governors."""
        self.pid_batching.on_ecu_answer()
        self.did_batching.on_ecu_answer()

    def _spend_budget_slot(self) -> None:
        """Advance the budget clock one slot, never into the past.

        Read per slot through ``bus_budget.effective`` so an override armed
        mid-drive takes effect on the next request, not after a restart.
        """
        self.next_budget_at = max(
            self.next_budget_at + 1.0 / bus_budget.effective(self.budget_rps), time.monotonic()
        )

    def _drain_until(self, until: float) -> None:
        _drain_frames_until(
            self.bus, self.on_frame, self.stats, self.stop_event, until, self.deadline
        )

    def _log_start(self) -> None:
        requests = self.requests
        if requests:
            log.info(
                "canable obd streaming started",
                extra={
                    "pids": [r.channel for r in requests if r.kind == "pid"],
                    "dids": [r.channel for r in requests if r.kind == "did"],
                    "dtc_channels": [r.channel for r in requests if r.kind == "dtc"],
                    "service_channels": [r.channel for r in requests if r.kind == "service"],
                    "freeze_frame_channels": [
                        r.channel for r in requests if r.kind == "freeze_frame"
                    ],
                    "custom_pid_channels": [r.channel for r in requests if r.kind == "custom"],
                    "memory_channels": [r.channel for r in requests if r.kind == "memory"],
                    "poll_tiers": {r.channel: r.poll_tier for r in requests},
                    "bus_budget_requests_per_second": self.budget_rps,
                    "auto_miss_backoff_seconds": AUTO_MISS_BACKOFF_SECONDS,
                    "did_session_windows": (
                        self.controller.snapshot(time.monotonic())
                        if self.controller is not None
                        else None
                    ),
                },
            )
        elif not self.discovery.pending():
            log.warning("canable obd streaming has no PID/DID requests; recording CAN only")


def run_can_obd_loop(
    bus: can.BusABC,
    spec: CanObdSpec,
    *,
    on_frame: Callable[[can.Message], None],
    on_reading: Callable[[ObdReading], None],
    stop_event: threading.Event,
    deadline_monotonic: float | None = None,
    client_factory: Callable[[can.BusABC, FrameTap], _DiscoveryClient] | None = None,
    on_scheduler_metrics: Callable[[ObdSchedulerMetrics], None] | None = None,
    scheduler_metrics_interval_seconds: float = SCHEDULER_METRICS_INTERVAL_SECONDS,
) -> CanObdLoopStats:
    """Run a single-bus CAN + OBD loop until stopped.

    ``on_frame`` sees every received frame, including diagnostic responses
    and the frames received while a support-discovery probe waits: the loop
    owns the bus, and every receive — its own polls, session control, drains
    and the client it hands its tap to — goes through ``receive_frame``
    (C06.2). ``on_reading`` sees decoded Mode 01 PID readings and raw Mode 22
    DID payloads. The function is synchronous by design so it can run either
    in the foreground recorder or inside ``asyncio.to_thread`` for the daemon.

    Support discovery is scheduler work (C06.4): the plan starts without it,
    and the loop issues one support-group probe per budget slot — only in a
    phase the eligibility matrix admits, and under an active capture profile
    only in slots no round member needs — installing results as they arrive.
    ``client_factory(bus, tap)`` may substitute the probe transport.
    """
    return _CanObdLoop(
        bus,
        spec,
        on_frame=on_frame,
        on_reading=on_reading,
        stop_event=stop_event,
        deadline_monotonic=deadline_monotonic,
        client_factory=client_factory,
        on_scheduler_metrics=on_scheduler_metrics,
        scheduler_metrics_interval_seconds=scheduler_metrics_interval_seconds,
    ).run()


def _controller_engaged(controller: DidWindowController, profile_gated: bool) -> bool:
    """Whether this tick must drive the session controller.

    Always when no profile gates requests. Under a profile: when it owns
    DIDs, or when the controller is mid-transition — a held window, a lapse
    or an owed exit — so no stale phase is ever polled through (C04.7).
    """
    if not profile_gated:
        return True
    return (
        bool(tuning_profile.mode22())
        or controller.phase != "mode01"
        or controller.session_exit_due()
    )


def _wake_for(
    now: float,
    controller: DidWindowController | None,
    states: list[PollRequestState],
    *,
    budget_at: float | None,
    next_due: float | None,
    deadline: float | None,
    budget_only_for_session: bool = False,
) -> WakeDecision:
    """The loop's one wake calculation, with the controller's deadlines folded in.

    ``budget_only_for_session`` drops the budget slot unless the controller
    has an exit, entry or keep-alive waiting on it: with nothing pollable the
    budget is not a deadline, the next transition is.
    """
    transition_at = keepalive_at = None
    if controller is not None:
        transition_at = controller.next_transition_at()
        keepalive_at = controller.next_keepalive_at()
        if budget_only_for_session and not controller.session_work_pending(states, now):
            budget_at = None
    elif budget_only_for_session:
        budget_at = None
    return next_wake_at(
        now,
        budget_at=budget_at,
        next_due=next_due,
        transition_at=transition_at,
        keepalive_at=keepalive_at,
        deadline=deadline,
    )


def _drive_session_controller(
    bus: can.BusABC,
    spec: CanObdSpec,
    controller: DidWindowController,
    states: list[PollRequestState],
    *,
    now: float,
    next_budget_at: float,
    allow_entry: bool = True,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> bool:
    """Send the exit, entry or keep-alive the controller owes, budget allowing.

    ``allow_entry=False`` (no DID selected right now) still sends an owed
    exit but never opens a window.

    Returns True when a request went out (the caller spends the budget slot
    and restarts its tick), False when nothing was due or the budget slot has
    not arrived. The exit is tried first: handing Mode 01 the bus
    back beats waiting out the S3 timeout, and only a permanent refusal
    disables it — a busy or silent ECU is retried, so one bad moment costs
    one window rather than every window for the rest of the drive.
    """
    session = spec.did_session
    if session is None or now < next_budget_at:
        return False
    if controller.session_exit_due():
        controller.on_exit_sent(now)
        started = time.monotonic()
        exit_result = _send_session_control(
            bus,
            session,
            sub_function=session.exit_session,
            request_id=spec.did_request_id,
            timeout_seconds=spec.request_timeout_seconds,
            on_frame=on_frame,
            stop_event=stop_event,
            stats=stats,
            deadline_monotonic=deadline_monotonic,
        )
        controller.on_service_time("session", time.monotonic() - started)
        controller.on_exit_response(result=exit_result, now=time.monotonic())
    elif allow_entry and controller.session_control_due(states, now):
        controller.on_entry_sent(now)
        started = time.monotonic()
        entry_result = _send_session_control(
            bus,
            session,
            request_id=spec.did_request_id,
            timeout_seconds=spec.request_timeout_seconds,
            on_frame=on_frame,
            stop_event=stop_event,
            stats=stats,
            deadline_monotonic=deadline_monotonic,
        )
        controller.on_service_time("session", time.monotonic() - started)
        controller.on_session_response(result=entry_result, states=states, now=time.monotonic())
    else:
        return False
    return True


def _cycle_plan_for(
    requests: list[PollRequest],
    states: list[PollRequestState],
    spec: CanObdSpec,
    controller: DidWindowController,
    now: float,
    *,
    profile_gated: bool,
    batch_max_pids: int,
) -> CyclePlan | None:
    """What the next cycle must fit, from the currently selected work (C04.4)."""
    session = spec.did_session
    if session is None:
        return None
    cycle = tuning_profile.view().did_cycle_seconds(session)
    if cycle is None:
        return None
    mode01_channels = 0
    did_requests = 0
    for index, (request, state) in enumerate(zip(requests, states, strict=True)):
        if state.suppressed_until == float("inf"):
            continue
        if profile_gated and not tuning_profile.request_eligible(index, request):
            continue
        if request.in_session_window:
            if state.suppressed_until <= now:
                did_requests += 1
        elif request.kind in ("pid", "dtc", "custom", "service"):
            mode01_channels += 1
    return plan_cycle(
        cycle_seconds=cycle,
        budget_rps=bus_budget.effective(_bus_budget_requests_per_second(spec)),
        mode01_channels=mode01_channels,
        batch_max_pids=batch_max_pids,
        did_requests=did_requests,
        fast_exit=controller.fast_exit_available(now),
        exit_grace_seconds=session.exit_grace_seconds,
        lapse_seconds=session.lapse_seconds,
        request_timeout_seconds=spec.request_timeout_seconds,
    )


def _open_round(
    rounds: RoundScheduler,
    requests: list[PollRequest],
    states: list[PollRequestState],
    spec: CanObdSpec,
    now: float,
    wall_now: float,
    *,
    eligible: RequestFilter | None,
) -> tuple[RoundState | None, float | None]:
    """Open the next round if one can start now.

    Returns ``(round, None)`` when opened, else ``(None, wait_until)``: the
    monotonic instant to drain frames until (the next round boundary, or the
    earliest due request), or ``None`` when nothing is pollable at all.
    """
    period = round_period_seconds(requests, states, spec, now, eligible=eligible)
    if period is None:
        return _open_on_demand_round(
            rounds, requests, states, spec, now, wall_now, eligible=eligible
        )
    open_at = rounds.next_open_at(now, period)
    if open_at > now:
        return None, open_at
    members = round_members(requests, states, now, deadline=open_at + period, eligible=eligible)
    if not members:
        rounds.idle(now)
        return None, _next_due_at(requests, states, now, eligible=eligible)
    return rounds.open(now, wall_now, period=period, members=members), None


#: Drain interval while only on-demand (rate 0) work is active. Nothing has a
#: cadence to anchor rounds on, but the bus must keep being received.
ON_DEMAND_ROUND_PERIOD_SECONDS = 0.2


def _open_on_demand_round(
    rounds: RoundScheduler,
    requests: list[PollRequest],
    states: list[PollRequestState],
    spec: CanObdSpec,
    now: float,
    wall_now: float,
    *,
    eligible: RequestFilter | None,
) -> tuple[RoundState | None, float | None]:
    """Rounds when every pollable request is at rate zero.

    A triggered freeze frame is un-suppressed with ``next_due_at == now`` and
    must still go out; one that already fired sits at ``inf`` forever while
    still counting as active. Neither has a cadence, so what is due opens a
    round under a nominal period and anything else waits on the bus. Returning
    ``(None, None)`` here spun the loop without a single ``recv`` while every
    finite-rate PID was backed off (found by the C02 verifier).
    """
    nominal = max(spec.request_timeout_seconds, ON_DEMAND_ROUND_PERIOD_SECONDS)
    members = round_members(requests, states, now, deadline=now + nominal, eligible=eligible)
    if not members:
        rounds.idle(now)
        return None, now + ON_DEMAND_ROUND_PERIOD_SECONDS
    return rounds.open(now, wall_now, period=nominal, members=members), None


def _preferred_members(requests: list[PollRequest], current: RoundState) -> frozenset[int]:
    """Members of the active profile's ``required_tuple`` — served first (C02.5)."""
    wanted = tuning_profile.required_tuple()
    if not wanted:
        return frozenset()
    return frozenset(
        index for index in current.members if requests[index].channel.upper() in wanted
    )


def _close_round_if_complete(rounds: RoundScheduler, now: float) -> None:
    current = rounds.current
    if current is not None and current.complete:
        rounds.close(now)


def _discovery_wait_reason(
    controller: DidWindowController | None,
    rounds: RoundScheduler,
    requests: list[PollRequest],
    states: list[PollRequestState],
    spec: CanObdSpec,
    now: float,
    *,
    eligible: RequestFilter | None,
    next_budget_at: float,
    budget_rps: float,
) -> str | None:
    """Why a due discovery probe cannot go out this tick, or ``None`` to issue it.

    The matrix gates the phase (``session_window`` / ``session_lapse`` /
    ``continuous_did_capture`` / ``paused``); the budget slot must have
    arrived; and under an active capture profile the probe may only take a
    slot no round member needs (C06.6) — between rounds, with the next round
    boundary at least one slot away — else it is ``capture_at_capacity``.
    Without a profile it goes at the head of the line between rounds, as the
    old synchronous discovery did before the first poll.
    """
    phase: SessionPhase = controller.matrix_phase if controller is not None else "none"
    verdict = evaluate(
        "discovery", tuning_profile.profile_class(), phase, paused=tuning_profile.is_paused()
    )
    if not verdict.eligible:
        return verdict.reason
    if now < next_budget_at:
        return "budget"
    if rounds.current is not None:
        return "round_open"
    if not tuning_profile.is_active():
        return None
    period = round_period_seconds(requests, states, spec, now, eligible=eligible)
    if period is None:
        return None
    slot = 1.0 / bus_budget.effective(budget_rps)
    if rounds.next_open_at(now, period) < now + slot:
        return "capture_at_capacity"
    return None


def _issue_discovery_probe(
    client: _DiscoveryClient,
    discovery: DiscoveryState,
    item: DiscoveryItem,
    *,
    tap: _FrameTap,
    stats: CanObdLoopStats,
    stop_event: threading.Event,
    deadline_monotonic: float | None,
) -> DiscoveryState:
    """One tapped, stop-aware support-group probe, charged and accounted."""
    walk = discovery.walks[item]
    group = walk.next_group
    assert group is not None
    tap.discovering = True
    started = time.monotonic()
    try:
        probe = client.probe_support(
            walk.service,
            group,
            should_stop=lambda: _should_stop(stop_event, deadline_monotonic),
        )
    except Exception:  # noqa: BLE001 - propagate after recording scheduler state
        stats.bus_errors += 1
        raise
    finally:
        tap.discovering = False
    seconds = time.monotonic() - started
    if not probe.sent:
        return discovery
    stats.requests_sent += 1
    stats.discovery_requests_sent += 1
    stats.discovery_seconds += seconds
    if probe.result == "answered":
        stats.discovery_responses += 1
    elif probe.result == "nrc":
        stats.discovery_negative_responses += 1
        stats.negative_responses += 1
    else:
        stats.discovery_timeouts += 1
        stats.request_timeouts += 1
    advanced = walk.with_probe(probe, now=time.monotonic(), seconds=seconds)
    if not advanced.complete and advanced.last_result != "answered":
        log.warning(
            "%s support discovery: group 0x%02X %s; re-probing after ECU contact or %.0fs",
            item,
            group,
            "refused" if probe.result == "nrc" else "silent",
            discovery_registry.DISCOVERY_RETRY_SECONDS,
            extra={"service": walk.service, "group": group, "result": probe.result},
        )
    return discovery.with_walk(item, advanced)


def _install_discovery(
    discovery: DiscoveryState,
    requests: list[PollRequest],
    states: list[PollRequestState],
    spec: CanObdSpec,
    now: float,
    installed: set[DiscoveryItem],
) -> None:
    """Fold every newly completed walk into the plan, in place, once."""
    for item, walk in discovery.walks.items():
        if not walk.complete or item in installed:
            continue
        installed.add(item)
        if item == "mode01":
            requests[:] = apply_mode01_support(requests, walk.supported)
            log.info(
                "Mode 01 support discovered",
                extra={
                    "supported": [f"0x{pid:02X}" for pid in walk.supported],
                    "responders": [f"0x{r:03X}" for r in walk.responders],
                    "last_result": walk.last_result,
                },
            )
        elif item == "mode06":
            present = {request.identifier for request in requests if request.kind == "service"}
            new_requests = [
                request
                for request in mode06_requests(walk.supported)
                if request.identifier not in present
            ]
            requests.extend(new_requests)
            states.extend(_initial_request_states(new_requests, spec, now))
            log.info(
                "Mode 06 discovery complete",
                extra={
                    "mode06_mids": [f"0x{mid:02X}" for mid in walk.supported],
                    "installed": len(new_requests),
                    "responders": [f"0x{r:03X}" for r in walk.responders],
                    "last_result": walk.last_result,
                },
            )


def _deferred_report(
    requests: list[PollRequest],
    states: list[PollRequestState],
    verdict: Callable[[int, PollRequest], Eligibility],
    now: float,
    *,
    discovery: DiscoveryState,
    discovery_wait: str | None,
) -> list[dict[str, Any]]:
    """Requests wanted now that the matrix defers or refuses, with reasons (C06.7)."""
    rows: list[dict[str, Any]] = []
    for item in discovery.pending():
        if discovery_wait is not None:
            rows.append(
                {
                    "channel": f"discovery:{item}",
                    "kind": "discovery",
                    "verdict": "deferred",
                    "reason": discovery_wait,
                }
            )
    for index, (request, state) in enumerate(zip(requests, states, strict=True)):
        if state.suppressed_until > now or state.next_due_at > now:
            continue
        outcome = verdict(index, request)
        if outcome.eligible:
            continue
        rows.append({"channel": request.channel, "kind": request.kind, **outcome.to_dict()})
        if len(rows) >= MAX_DEFERRED_REPORT:
            break
    rows.sort(key=lambda row: (row["verdict"] != "deferred", row["channel"]))
    return rows


@dataclass(frozen=True)
class BatchOutcome:
    """What one batched request produced, per member, for round accounting."""

    answered: bool
    #: Request index -> readings emitted for it.
    observed: Mapping[int, int] = field(default_factory=dict)
    #: Shared arrival instant of the batch response (receipt time, see
    #: ``_response_to_reading``); ``None`` when the batch failed.
    acquired_at: float | None = None
    #: Set when the whole batch was refused with a negative response. An NRC
    #: is an answer from an awake ECU, so unlike silence it is always
    #: evidence about batching.
    negative_response_code: int | None = None


def _issue_batched_request(
    bus: can.BusABC,
    requests: list[PollRequest],
    states: list[PollRequestState],
    indices: list[int],
    *,
    spec: CanObdSpec,
    on_frame: Callable[[can.Message], None],
    on_reading: Callable[[ObdReading], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
    round_timestamp: float | None = None,
    round_state: RoundState | None = None,
) -> BatchOutcome:
    """Poll several Mode 01 PIDs in one round trip; ``answered`` if the batch did.

    All PIDs in the batch share the response's arrival as ``acquired_at``.
    That is the receipt time of one payload — it is not proof that the ECU
    sampled those PIDs atomically, and nothing downstream may read it as such.

    Per-member accounting is deliberate: a PID present in the response is
    credited exactly as the single-PID path would credit it, and a PID absent
    from an otherwise-good response takes a miss on its own — so one dead
    channel backs off alone instead of dragging its five neighbours down.

    A *whole-batch* failure (timeout, refusal, desynced walk) credits and
    penalises nobody. We have no evidence about any individual PID in that
    case, and applying six misses would wrongly back off six healthy channels
    on what may be an ECU that simply dislikes batching. The caller counts
    consecutive failures and stops batching instead.

    Those sends are still counted in ``requests_sent``, so they go to
    ``batch_failures`` — otherwise the remainder is unnamed, which is how
    three channels read as "60 sent, 0 answered, 6 timeouts" on the
    2026-09-10 drive and looked like dead channels rather than 54 sends
    inside failed batches. The invariant
    ``requests_sent == responses + timeouts + batch_failures`` holds per
    request; ``_request_accounting_error`` reports any breach in ``/status``.
    """
    identifiers = [requests[index].identifier for index in indices]
    leader = requests[indices[0]]
    started = time.monotonic()
    for index in indices:
        states[index].last_requested_at = started
        states[index].requests_sent += 1

    try:
        result = _query_batched_with_tap(
            bus,
            leader,
            identifiers,
            timeout_seconds=spec.request_timeout_seconds,
            on_frame=on_frame,
            stop_event=stop_event,
            stats=stats,
            deadline_monotonic=deadline_monotonic,
            did_response_widths=spec.did_response_widths,
            mode01_widths=stats.mode01_widths,
        )
    except Exception:  # noqa: BLE001 - propagate after recording scheduler state
        for index in indices:
            states[index].bus_errors += 1
        stats.bus_errors += 1
        raise

    if result.readings is None:
        if leader.kind == "did":
            stats.did_batch_failures += 1
        else:
            stats.batch_failures += 1
        # The send was counted above for every member; nothing below will
        # credit or penalise them, by design. Name where those sends went so
        # `requests_sent == responses + timeouts + batch_failures` holds and
        # the remainder is never an unexplained bucket (D02.1).
        for index in indices:
            states[index].batch_failures += 1
        if result.negative_response_code is not None:
            stats.negative_responses += 1
            for index in indices:
                states[index].last_negative_response_code = result.negative_response_code
        return BatchOutcome(answered=False, negative_response_code=result.negative_response_code)
    # Mode 01 members the desynced walk never reached (or the blamed PID,
    # whose slice is suspect). They were not answered-and-missing — the walk
    # lost the boundary before them — so they take no timeout: that charged a
    # miss to every later member on every batch, forever (batch044). They go
    # back to a single read on their next slot instead, which measures them.
    unparsed: set[int] = set()
    if not result.clean:
        if leader.kind == "did":
            stats.did_batch_desyncs += 1
        else:
            stats.batch_desyncs += 1
            if result.suspect is not None:
                result.readings.pop(result.suspect, None)
            unparsed = {pid for pid in identifiers if pid not in result.readings}
            stats.mode01_widths.on_desync(unparsed, result.suspect)
            log.debug(
                "multi-PID batch desynced; re-measuring single on next slot",
                extra={
                    "suspect": None if result.suspect is None else f"0x{result.suspect:02X}",
                    "unparsed": [f"0x{pid:02X}" for pid in sorted(unparsed)],
                },
            )

    now = time.monotonic()
    # One response, one arrival instant, shared by every PID it carried. A
    # round stamp keeps a sample that spans several requests on one row; the
    # arrival instant is retained per reading so nothing is lost.
    arrival = (
        result.arrival if result.arrival is not None else FrameArrival(time.time(), "userspace")
    )
    observed: dict[int, int] = {}
    for index in indices:
        request = requests[index]
        state = states[index]
        if request.identifier in unparsed:
            # Same accounting as a whole-batch failure: the send is named, no
            # response state moves (C02/D02.1 invariant holds).
            state.batch_failures += 1
            continue
        payload = result.readings.get(request.identifier)
        emitted = False
        if payload is not None:
            # Synthesise the response the single-PID path would have seen, so
            # both paths decode through exactly the same code.
            readings = _response_to_reading(
                request,
                ObdResponse(
                    service=0x62 if request.kind == "did" else 0x41,
                    pid=request.identifier,
                    payload=payload,
                    responder_id=result.responder_id or DEFAULT_RESPONSE_ID_MIN,
                ),
                timestamp=round_timestamp,
                arrival=arrival,
                response_interval_s=result.response_interval_s,
                round_state=round_state,
            )
            if readings:
                emitted = True
                observed[index] = len(readings)
                stats.readings_emitted += len(readings)
                for reading in readings:
                    stats.record_read(reading.pid, time.monotonic())
                    on_reading(reading)
        if emitted:
            state.responses += 1
            state.last_response_at = now
        else:
            state.timeouts += 1
            stats.request_timeouts += 1
        _update_response_state(
            state,
            emitted=emitted,
            now=now,
            focused_capture=(
                tuning_profile.is_active() and tuning_profile.request_eligible(index, request)
            ),
        )
    return BatchOutcome(answered=True, observed=observed, acquired_at=arrival.at)
