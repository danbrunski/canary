"""Canable-native OBD streaming collector.

This is the replacement path for the dead ELM327 adapter. It owns one
``python-can`` Bus, records every received CAN frame, and interleaves read-only
OBD requests on that same bus. That matters for slcan/USB Canables: opening the
same serial adapter twice is not reliable, and two independent ``recv`` loops
would race each other for diagnostic response frames.

The public surfaces mirror the legacy ELM327 collector:

* daemon path: ``collect_can_obd_from_config`` emits ``TelemetryRecord`` rows
  for both raw CAN frames and decoded OBD readings;
* recorder path: ``record_can_obd`` writes ``frames.log`` plus ``pids.jsonl``.

Mode 01 PIDs are decoded through ``can_obd_client.PID_DECODERS``. Mode 22 DIDs
are emitted as ``DID_<hex>`` with the response payload hex string, matching the
legacy ``record.py --obd-dids`` sidecar shape.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial

import can

from src.can_ids import parse_standard_can_id
from src.collection import bus_budget, capture_generation, tuning_profile, tx_gate
from src.collection.can_collector import (
    DEFAULT_RECONNECT_POLICY,
    RecordingResult,
    bus_factory,
    frame_to_record,
)
from src.collection.can_collector import _write_manifest as write_recording_manifest
from src.collection.can_obd_client import (
    PID_DECODERS,
    decode_mil_on,
    parse_dtc_payload,
)
from src.collection.custom_pids import evaluate_custom_pid
from src.collection.hybrid_cycle import CyclePlan, WakeDecision, next_wake_at, plan_cycle
from src.collection.isotp import (
    DEFAULT_REQUEST_ID,
    DEFAULT_RESPONSE_ID_MAX,
    DEFAULT_RESPONSE_ID_MIN,
    FLOW_CONTROL_CTS,
    RESPONSE_PENDING_NRC,
    IsoTpResponseAssembler,
    ObdResponse,
    build_batched_request_payload,
    build_request_payload,
    split_batched_response,
)
from src.collection.obd_collector import (
    AcquiredClock,
    JsonlReadingWriter,
    ObdReading,
    reading_to_record,
)
from src.collection.obd_registry import (
    lookup,
    misfire_count_channel,
    misfire_counts_from_results,
    parse_mode06_response,
)
from src.collection.obd_scheduler import (
    AUTO_MISS_BACKOFF_SECONDS,
    AUTO_PIDS,
    DEFAULT_DID_REQUEST_ID,
    CanObdLoopStats,
    CanObdSpec,
    DidSessionSpec,
    DidWindowController,
    ObdSchedulerMetrics,
    RequestFilter,
    RoundScheduler,
    RoundState,
    SessionControlResult,
    _bus_budget_requests_per_second,
    _count_active_requests,
    _initial_request_states,
    _next_due_at,
    _next_request_due_at,
    _next_unsuppressed_at,
    _PidDiscoveryClient,
    _PollRequest,
    _PollRequestState,
    _scheduler_metrics,
    _select_next_due_request,
    _update_response_state,
    build_poll_requests,
    parse_did_token,
    parse_pid_token,
    resolve_did_session_spec,
    round_members,
    round_period_seconds,
    select_batch_companions,
)
from src.collection.queueing import (
    CollectorHandoffStats,
    HandoffStats,
    QueueDropCounter,
    log_dropped_can_frame,
    threadsafe_enqueue,
)
from src.collection.reconnect import (
    BusReconnector,
    RebuildOutcome,
    ReconnectPolicy,
    run_with_reconnect,
)
from src.config import CanBusConfig, VehicleConfig
from src.session import SessionPaths
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)


def _capture_did_request_eligible(
    controller: DidWindowController,
    now: float,
    index: int,
    request: _PollRequest,
) -> bool:
    return controller.request_eligible(index, request, now=now) and tuning_profile.request_eligible(
        index, request
    )


AUTO_PROBE_TIMEOUT_SECONDS = 0.25
AUTO_BASELINE_PID_NAMES = tuple(name for _pid, (name, _decoder) in sorted(PID_DECODERS.items()))
SCHEDULER_METRICS_INTERVAL_SECONDS = 5.0
#: Consecutive whole-batch failures before the session narrows its batch size.
#: Small on purpose: an ECU that dislikes a given size should cost a handful
#: of request slots to discover, not a whole drive.
MAX_CONSECUTIVE_BATCH_FAILURES = 3
#: Extra wait granted each time the ECU answers `0x78 responsePending`
#: (ISO 14229 P2*). The ECU has told us it is working, so scoring this as a
#: timeout throws away an answer that was on its way.
RESPONSE_PENDING_EXTENSION_SECONDS = 0.5
#: Cap on those extensions, so a stuck ECU cannot hold the bus indefinitely.
#: Exceeding it is a real failure and is reported as NRC 0x78.
MAX_RESPONSE_PENDING_EXTENSIONS = 4
#: A zero-result Mode 06 discovery is ambiguous (passive mode, bus startup,
#: ignition state, or genuinely unsupported). Once ordinary OBD contact has
#: succeeded, retry no faster than this if discovery still returns nothing.
MODE06_DISCOVERY_RETRY_SECONDS = 30.0
#: A bus-driver frame stamp further than this from ``time.time()`` is not
#: trusted as the acquisition clock (an adapter with no clock reports 0.0;
#: a stale SocketCAN stamp after a suspend can be minutes off). The
#: userspace receipt time is used instead and the row says so (C02.7).
KERNEL_STAMP_MAX_SKEW_SECONDS = 5.0


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


def parse_can_id(value: str | int) -> int:
    return parse_standard_can_id(value)


def run_can_obd_loop(
    bus: can.BusABC,
    spec: CanObdSpec,
    *,
    on_frame: Callable[[can.Message], None],
    on_reading: Callable[[ObdReading], None],
    stop_event: threading.Event,
    deadline_monotonic: float | None = None,
    client_factory: Callable[[can.BusABC], _PidDiscoveryClient] | None = None,
    on_scheduler_metrics: Callable[[ObdSchedulerMetrics], None] | None = None,
    scheduler_metrics_interval_seconds: float = SCHEDULER_METRICS_INTERVAL_SECONDS,
) -> CanObdLoopStats:
    """Run a single-bus CAN + OBD loop until stopped.

    ``on_frame`` sees every received frame, including diagnostic responses.
    ``on_reading`` sees decoded Mode 01 PID readings and raw Mode 22 DID
    payloads. The function is synchronous by design so it can run either in the
    foreground recorder or inside ``asyncio.to_thread`` for the daemon.
    """
    from src.collection.can_obd_client import CanObdClient

    stats = CanObdLoopStats()
    # Every (re)start of the loop is a new capture generation (C02.8): a bus
    # rebuild after a fault must not let a request issued before it complete
    # a round scheduled after it.
    rounds = RoundScheduler(capture_generation.bump("collector-start"), stats)
    client = (
        CanObdClient(bus, timeout_seconds=spec.request_timeout_seconds)
        if client_factory is None
        else client_factory(bus)
    )
    requests = build_poll_requests(client, spec)
    mode06_discovery_pending = (
        spec.service_poll_interval_seconds > 0
        and spec.service_mids == "auto"
        and not any(request.kind == "service" for request in requests)
    )
    next_mode06_discovery_at = 0.0
    if mode06_discovery_pending:
        log.warning("Mode 06 discovery returned zero MIDs; will retry after successful ECU contact")
    budget_rps = _bus_budget_requests_per_second(spec)
    did_indices = [index for index, request in enumerate(requests) if request.kind == "did"]
    freeze_frame_indices = [
        index for index, request in enumerate(requests) if request.kind == "freeze_frame"
    ]
    controller: DidWindowController | None = None
    if spec.did_session is not None and did_indices:
        controller = DidWindowController(spec.did_session, did_indices, now=time.monotonic())
    if requests:
        log.info(
            "canable obd streaming started",
            extra={
                "pids": [r.channel for r in requests if r.kind == "pid"],
                "dids": [r.channel for r in requests if r.kind == "did"],
                "dtc_channels": [r.channel for r in requests if r.kind == "dtc"],
                "service_channels": [r.channel for r in requests if r.kind == "service"],
                "freeze_frame_channels": [r.channel for r in requests if r.kind == "freeze_frame"],
                "custom_pid_channels": [r.channel for r in requests if r.kind == "custom"],
                "poll_tiers": {r.channel: r.poll_tier for r in requests},
                "bus_budget_requests_per_second": budget_rps,
                "auto_miss_backoff_seconds": AUTO_MISS_BACKOFF_SECONDS,
                "did_session_windows": (
                    controller.snapshot(time.monotonic()) if controller is not None else None
                ),
            },
        )
    else:
        log.warning("canable obd streaming has no PID/DID requests; recording CAN only")

    request_states = _initial_request_states(requests, spec, time.monotonic())

    def retry_mode06_after_contact(now: float) -> None:
        nonlocal mode06_discovery_pending, next_mode06_discovery_at
        if not mode06_discovery_pending or now < next_mode06_discovery_at:
            return
        discovered = _retry_mode06_discovery(
            client,
            requests,
            request_states,
            spec=spec,
            now=now,
        )
        if discovered:
            mode06_discovery_pending = False
        else:
            next_mode06_discovery_at = now + MODE06_DISCOVERY_RETRY_SECONDS

    next_budget_at = time.monotonic()
    next_metrics_at = time.monotonic()
    # Batching is opt-in and self-disabling. An ECU that ignores or refuses
    # multi-PID requests would otherwise fail every batch forever, so a run of
    # consecutive failures falls the session back to single-PID rather than
    # stalling. This doubles as the adaptive form of the startup size probe:
    # we discover what the vehicle actually accepts by using it.
    batching_enabled = spec.batch_max_pids > 1
    active_batch_max = spec.batch_max_pids
    consecutive_batch_failures = 0
    stats.loop_started_at = time.monotonic()
    while not _should_stop(stop_event, deadline_monotonic):
        now = time.monotonic()
        generation = capture_generation.current()
        if generation != rounds.generation:
            # Profile change, pause/resume, passive toggle or replay
            # transition: rounds restart from zero under the new generation.
            rounds.reset(generation, now)
            if controller is not None:
                # ...and any session transition in flight is completed rather
                # than left as a stale phase the new profile polls through
                # (C04.7); the next window is due now, not an interval later.
                controller.on_generation_change(now)
        if (
            freeze_frame_indices
            and spec.freeze_frame_trigger is not None
            and spec.freeze_frame_trigger.take()
        ):
            # Task 3.3: a stored DTC just appeared — un-suppress every
            # freeze-frame request for exactly this one shot. They go back
            # to "on_demand" dormancy on their own once each has fired once
            # (`_request_rate_hz` returns 0 for them, see `build_poll_requests`).
            for index in freeze_frame_indices:
                request_states[index].suppressed_until = 0.0
                request_states[index].next_due_at = now
        if spec.force_poll_trigger is not None:
            forced_channels = spec.force_poll_trigger.take()
            if forced_channels:
                for index, request in enumerate(requests):
                    if request.channel.upper() in forced_channels:
                        request_states[index].suppressed_until = 0.0
                        request_states[index].next_due_at = now
        if on_scheduler_metrics is not None and now >= next_metrics_at:
            on_scheduler_metrics(
                _scheduler_metrics(
                    requests,
                    request_states,
                    spec,
                    stats,
                    now,
                    did_profile=controller.snapshot(now) if controller is not None else None,
                    rounds=rounds.snapshot(now),
                )
            )
            stats.scheduler_snapshots += 1
            next_metrics_at = now + max(0.1, scheduler_metrics_interval_seconds)

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
            wake = next_wake_at(now, deadline=deadline_monotonic)
            _drain_frames_until(bus, on_frame, stats, stop_event, wake.at, deadline_monotonic)
            continue

        if not requests:
            wake = next_wake_at(now, deadline=deadline_monotonic)
            _drain_frames_until(bus, on_frame, stats, stop_event, wake.at, deadline_monotonic)
            continue

        # The selected profile applies even for ``default``: the request plan
        # is a startup superset of every saved profile, and filtering here is
        # what keeps the seeded default byte-identical to config. Standalone
        # recorder/tests that never initialised the catalog keep the
        # scheduler's historical behaviour.
        profile_gated = spec.capture_profiles_enabled or tuning_profile.is_active()
        eligible: RequestFilter | None = None
        if controller is not None and _controller_engaged(controller, profile_gated):
            dids_selected = True
            if profile_gated:
                # The startup request plan is the union of every saved
                # profile. A DID outside the active profile must not keep
                # this window's pending set alive until its max-duration
                # timeout.
                dids_selected = False
                for did_index in did_indices:
                    if tuning_profile.request_eligible(did_index, requests[did_index]):
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
            controller.advance(request_states, now)
            if controller.phase == "mode01" and controller.cycle_mode():
                controller.set_plan(
                    _cycle_plan_for(
                        requests,
                        request_states,
                        spec,
                        controller,
                        now,
                        profile_gated=profile_gated,
                        batch_max_pids=active_batch_max if batching_enabled else 1,
                    )
                )
            budget_after = _drive_session_controller(
                bus,
                spec,
                controller,
                request_states,
                now=now,
                next_budget_at=next_budget_at,
                budget_rps=budget_rps,
                allow_entry=dids_selected,
                on_frame=on_frame,
                stop_event=stop_event,
                stats=stats,
                deadline_monotonic=deadline_monotonic,
            )
            if controller.phase != phase_before:
                # A phase change is a new sampling window: the other
                # service's round grid must not hold this one's first round
                # back by up to a period (a DID sweep waited 0.21 s after its
                # window opened under a shared 4 Hz target).
                rounds.idle(now)
            if budget_after is not None:
                next_budget_at = budget_after
                continue
            eligible = (
                partial(_capture_did_request_eligible, controller, now)
                if profile_gated
                else partial(controller.request_eligible, now=now)
            )
        elif profile_gated:
            # A profile with no DIDs deliberately suspends the session
            # controller, concentrating the budget on its Mode-01 selection.
            eligible = tuning_profile.request_eligible

        # A round ends at its deadline even while more work is due (C02.2);
        # this runs before any selection so no reading is ever stamped with
        # an expired round.
        rounds.expire(now)
        active_request_count = _count_active_requests(
            requests, request_states, now, eligible=eligible
        )
        if active_request_count == 0:
            rounds.idle(now)
            wake = _wake_for(
                now,
                controller,
                request_states,
                budget_at=next_budget_at,
                next_due=_next_unsuppressed_at(requests, request_states, now, eligible=eligible),
                deadline=deadline_monotonic,
                budget_only_for_session=True,
            )
            _drain_frames_until(bus, on_frame, stats, stop_event, wake.at, deadline_monotonic)
            continue

        if now < next_budget_at:
            wake = _wake_for(
                now,
                controller,
                request_states,
                budget_at=next_budget_at,
                next_due=None,
                deadline=deadline_monotonic,
            )
            _drain_frames_until(bus, on_frame, stats, stop_event, wake.at, deadline_monotonic)
            continue

        if rounds.current is None:
            opened, wait_until = _open_round(
                rounds, requests, request_states, spec, now, time.time(), eligible=eligible
            )
            if opened is None:
                # Never spin: with active requests but nothing to open, the
                # bus is still received until the earliest relevant deadline.
                wake = _wake_for(
                    now,
                    controller,
                    request_states,
                    budget_at=None,
                    next_due=wait_until,
                    deadline=deadline_monotonic,
                )
                _drain_frames_until(bus, on_frame, stats, stop_event, wake.at, deadline_monotonic)
                continue
        current_round = rounds.current
        assert current_round is not None
        preferred = _preferred_members(requests, current_round)
        selection = _select_next_due_request(
            requests,
            request_states,
            now,
            eligible=eligible,
            among=current_round.remaining,
            preferred=preferred,
        )
        if selection is None:
            # Members remain but none is pollable any more (the DID window
            # closed, a member got suppressed): the round cannot complete and
            # closes incomplete rather than hanging open until its deadline.
            rounds.close(now)
            continue
        request_index, request, request_state = selection
        round_timestamp = (
            current_round.wall_start if tuning_profile.round_timestamps_enabled() else None
        )

        batch_indices = select_batch_companions(
            requests,
            request_states,
            now,
            leader_index=request_index,
            max_pids=active_batch_max if batching_enabled else 1,
            eligible=eligible,
            ignore_tier=round_timestamp is not None,
            members=current_round.remaining,
            preferred=preferred,
        )
        if len(batch_indices) > 1:
            batch_started = time.monotonic()
            outcome = _issue_batched_request(
                bus,
                requests,
                request_states,
                batch_indices,
                spec=spec,
                on_frame=on_frame,
                on_reading=on_reading,
                stop_event=stop_event,
                stats=stats,
                deadline_monotonic=deadline_monotonic,
                round_timestamp=round_timestamp,
                round_state=current_round,
            )
            if controller is not None:
                controller.on_service_time("mode01", time.monotonic() - batch_started)
                if outcome.observed:
                    controller.on_response(request_index, time.monotonic(), outcome.acquired_at)
            if outcome.answered:
                consecutive_batch_failures = 0
                retry_mode06_after_contact(time.monotonic())
            else:
                consecutive_batch_failures += 1
                if consecutive_batch_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                    narrowed = _next_batch_size(active_batch_max)
                    consecutive_batch_failures = 0
                    if narrowed <= 1:
                        batching_enabled = False
                        log.warning(
                            "multi-PID batching disabled: no batch size this ECU "
                            "accepts; falling back to single-PID for this session",
                            extra={"configured_batch_max_pids": spec.batch_max_pids},
                        )
                    else:
                        log.warning(
                            "narrowing multi-PID batch size %d -> %d after %d consecutive failures",
                            active_batch_max,
                            narrowed,
                            MAX_CONSECUTIVE_BATCH_FAILURES,
                        )
                        active_batch_max = narrowed
                    stats.batch_size_narrowings += 1
            if controller is not None:
                for index in batch_indices:
                    controller.on_request_attempted(index)
            for index in batch_indices:
                request_states[index].next_due_at = _next_request_due_at(
                    requests[index], request_states[index], spec
                )
                rounds.on_attempt(index)
                observed = outcome.observed.get(index, 0)
                if observed:
                    rounds.on_observed(index, observed, outcome.acquired_at)
            _close_round_if_complete(rounds, time.monotonic())
            next_budget_at = max(
                next_budget_at + 1.0 / bus_budget.effective(budget_rps), time.monotonic()
            )
            continue

        timeout_seconds = spec.request_timeout_seconds
        if request.auto_probe and (request.fast_probe or request_state.missed_responses > 0):
            timeout_seconds = min(timeout_seconds, AUTO_PROBE_TIMEOUT_SECONDS)
        request_started = time.monotonic()
        request_state.last_requested_at = request_started
        request_state.requests_sent += 1
        try:
            result = _query_with_tap(
                bus,
                request,
                timeout_seconds=timeout_seconds,
                on_frame=on_frame,
                stop_event=stop_event,
                stats=stats,
                deadline_monotonic=deadline_monotonic,
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
        rounds.on_attempt(request_index)
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
                rounds.on_observed(request_index, len(readings), acquired_at)
                for reading in readings:
                    stats.record_read(reading.pid, time.monotonic())
                    on_reading(reading)
        if emitted:
            request_state.responses += 1
            request_state.last_response_at = time.monotonic()
            retry_mode06_after_contact(time.monotonic())
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

        request_state.next_due_at = _next_request_due_at(request, request_state, spec)
        _close_round_if_complete(rounds, time.monotonic())
        next_budget_at = max(
            next_budget_at + 1.0 / bus_budget.effective(budget_rps), time.monotonic()
        )

    if on_scheduler_metrics is not None:
        on_scheduler_metrics(
            _scheduler_metrics(
                requests,
                request_states,
                spec,
                stats,
                time.monotonic(),
                did_profile=(
                    controller.snapshot(time.monotonic()) if controller is not None else None
                ),
                rounds=rounds.snapshot(time.monotonic()),
            )
        )
        stats.scheduler_snapshots += 1
    rounds.close(time.monotonic())
    return stats


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
    states: list[_PollRequestState],
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
    states: list[_PollRequestState],
    *,
    now: float,
    next_budget_at: float,
    budget_rps: float,
    allow_entry: bool = True,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> float | None:
    """Send the exit, entry or keep-alive the controller owes, budget allowing.

    ``allow_entry=False`` (no DID selected right now) still sends an owed
    exit but never opens a window.

    Returns the advanced ``next_budget_at`` when a request went out (the
    caller restarts its tick), ``None`` when nothing was due or the budget
    slot has not arrived. The exit is tried first: handing Mode 01 the bus
    back beats waiting out the S3 timeout, and only a permanent refusal
    disables it — a busy or silent ECU is retried, so one bad moment costs
    one window rather than every window for the rest of the drive.
    """
    session = spec.did_session
    if session is None or now < next_budget_at:
        return None
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
        controller.on_session_response(ok=entry_result.ok, states=states, now=time.monotonic())
    else:
        return None
    return max(next_budget_at + 1.0 / bus_budget.effective(budget_rps), time.monotonic())


def _cycle_plan_for(
    requests: list[_PollRequest],
    states: list[_PollRequestState],
    spec: CanObdSpec,
    controller: DidWindowController,
    now: float,
    *,
    profile_gated: bool,
    batch_max_pids: int,
) -> CyclePlan | None:
    """What the next cycle must fit, from the currently selected work (C04.4)."""
    session = spec.did_session
    cycle = tuning_profile.did_cycle_override()
    if cycle is None and session is not None:
        cycle = session.cycle_seconds
    if session is None or cycle is None:
        return None
    mode01_channels = 0
    did_requests = 0
    for index, (request, state) in enumerate(zip(requests, states, strict=True)):
        if state.suppressed_until == float("inf"):
            continue
        if profile_gated and not tuning_profile.request_eligible(index, request):
            continue
        if request.kind == "did":
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
    requests: list[_PollRequest],
    states: list[_PollRequestState],
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
    requests: list[_PollRequest],
    states: list[_PollRequestState],
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


def _preferred_members(requests: list[_PollRequest], current: RoundState) -> frozenset[int]:
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


def _retry_mode06_discovery(
    client: _PidDiscoveryClient,
    requests: list[_PollRequest],
    states: list[_PollRequestState],
    *,
    spec: CanObdSpec,
    now: float,
) -> tuple[int, ...]:
    """Discover and install Mode 06 requests after the bus becomes usable."""
    discover = getattr(client, "query_supported_mode_06_mids", None)
    mids = tuple(discover()) if discover is not None else ()
    if not mids:
        log.warning(
            "Mode 06 retry returned zero MIDs; keeping discovery pending",
            extra={"retry_seconds": MODE06_DISCOVERY_RETRY_SECONDS},
        )
        return ()

    new_requests = [
        _PollRequest(
            kind="service",
            service=0x06,
            identifier=mid,
            request_id=DEFAULT_REQUEST_ID,
            channel="MONITOR_RESULTS",
            poll_tier="dtc",
        )
        for mid in mids
    ]
    requests.extend(new_requests)
    states.extend(_initial_request_states(new_requests, spec, now))
    log.info(
        "Mode 06 discovery recovered after ECU contact",
        extra={"mode06_mids": [f"0x{mid:02X}" for mid in mids]},
    )
    return mids


async def collect_can_obd_from_config(
    spec: CanObdSpec,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    factory: Callable[[CanBusConfig], can.BusABC] = bus_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
    on_scheduler_metrics: Callable[[ObdSchedulerMetrics], None] | None = None,
    on_handoff_stats: Callable[[CollectorHandoffStats], None] | None = None,
) -> None:
    """Daemon producer for the Canable-native path.

    ``on_handoff_stats`` (C03.6) receives the collector's cumulative
    thread-handoff and ingest-queue drop counters each time scheduler metrics
    are published, so loss between the poll thread and the ingest queue is
    attributable in ``/status`` (``telemetry_sinks.obd_collect``) instead of
    only in a log line.
    """
    loop = asyncio.get_running_loop()
    drop_counter = QueueDropCounter("canable-obd")
    thread_stop = threading.Event()
    #: Handoff counters from attempts that already ended; the live attempt's
    #: are added on each report so the total is cumulative across reconnects.
    finished_handoffs = HandoffStats(0, 0, 0, 0, 0, 0)
    attempts = 0

    async def _mirror_stop() -> None:
        await stop_event.wait()
        thread_stop.set()

    mirror_task = asyncio.create_task(_mirror_stop())

    def _report_handoff(enqueue_stats: HandoffStats, metrics: ObdSchedulerMetrics) -> None:
        if on_handoff_stats is None:
            return
        raw = sum(int(row.get("responses") or 0) for row in metrics.by_tier.values())
        on_handoff_stats(
            CollectorHandoffStats(
                handoff=finished_handoffs + enqueue_stats,
                queue_dropped=drop_counter.dropped,
                derived_records=metrics.readings_emitted,
                raw_responses=raw,
                reconnects=max(0, attempts - 1),
            )
        )

    async def _attempt(capture: Callable[[can.BusABC], None]) -> None:
        nonlocal finished_handoffs, attempts
        thread_stop.clear()
        attempts += 1
        # One bounded, coalescing handoff per attempt (C07.4); closed once the
        # worker has returned so a late callback can never touch the queue.
        enqueue = threadsafe_enqueue(loop, queue, on_drop=drop_counter, name="canable-obd")
        try:
            bus = factory(spec.can)
            capture(bus)

            def on_frame(msg: can.Message) -> None:
                enqueue(frame_to_record(msg))

            def on_reading(reading: ObdReading) -> None:
                enqueue(reading_to_record(reading))

            metrics_callback = None
            if on_scheduler_metrics is not None or on_handoff_stats is not None:

                def _publish(metrics: ObdSchedulerMetrics) -> None:
                    if on_scheduler_metrics is not None:
                        on_scheduler_metrics(metrics)
                    _report_handoff(enqueue.stats(), metrics)

                def metrics_callback(metrics: ObdSchedulerMetrics) -> None:
                    loop.call_soon_threadsafe(_publish, metrics)

            await asyncio.to_thread(
                run_can_obd_loop,
                bus,
                spec,
                on_frame=on_frame,
                on_reading=on_reading,
                stop_event=thread_stop,
                on_scheduler_metrics=metrics_callback,
            )
        finally:
            thread_stop.set()
            enqueue.close()
            finished_handoffs = finished_handoffs + enqueue.stats()

    try:
        await run_with_reconnect(
            _attempt,
            stop_event=stop_event,
            policy=reconnect_policy,
            teardown=lambda bus: bus.shutdown(),
            log=log,
            reconnect_message="canable obd collector failed; reconnecting",
            log_extra=lambda: {"interface": spec.can.interface, "channel": spec.can.channel},
        )
    finally:
        mirror_task.cancel()
        try:
            await mirror_task
        except asyncio.CancelledError:
            pass


def record_can_obd(
    bus: can.BusABC,
    paths: SessionPaths,
    *,
    spec: CanObdSpec,
    vehicle: VehicleConfig | None,
    duration: float | None,
    obd_writer: JsonlReadingWriter,
    vehicle_state: str | None = None,
    stop_event: threading.Event | None = None,
    factory: Callable[[CanBusConfig], can.BusABC] | None = None,
    reconnect_policy: ReconnectPolicy | None = None,
    max_reconnects: int | None = 10,
) -> RecordingResult:
    """Record ``frames.log`` and ``pids.jsonl`` from one Canable.

    When ``factory`` AND ``reconnect_policy`` are provided, a bus-level
    failure mid-capture (USB disconnect / re-enumeration, adapter reset,
    ``OSError`` / ``can.CanError`` out of ``bus.recv``/``bus.send``) triggers
    an auto-reconnect instead of ending the drive: the dead bus is shut down,
    we wait per the policy, and ``factory(spec.can)`` rebuilds it. The SAME
    ``CanutilsLogWriter`` stays open across reconnects so ``frames.log``
    remains one continuous file. Without both params, bus-level exceptions
    propagate as before (backward-compatible) — this is the behavior the
    daemon path ``collect_can_obd_from_config`` has always had, now also on
    the recorder path used by ``scripts/record.py --obd-source canable`` for
    the drive backlog.

    ``max_reconnects`` caps CONSECUTIVE failed rebuilds (default 10); a
    successful rebuild resets the counter. Once exhausted the recording stops
    with ``stopped_by = "reconnect_exhausted"``, mirroring ``record()``. This
    matters because a wedged USB adapter can leave the rebuild call in
    uninterruptible kernel I/O (the WSL2 failure mode that motivated the cap
    on the CAN-only path); the in-vehicle analogue is a brown-out on the
    Canable's USB power during a drive.

    Frames captured in a segment that ends in a bus exception are written to
    ``frames.log`` before the failure but not counted (``run_can_obd_loop``
    does not return its stats when it raises). Data integrity is preserved;
    only the manifest's ``frames_recorded`` tally is a small undercount across
    a reconnect.
    """
    started_at = datetime.now(UTC)
    deadline = time.monotonic() + duration if duration is not None else None
    local_stop = stop_event or threading.Event()
    error: BaseException | None = None
    stop_reason = "signal"

    frames_seen = 0
    parse_errors = 0

    reconnect_enabled = factory is not None and reconnect_policy is not None
    current_bus: can.BusABC | None = bus
    # Owns the bounded-backoff rebuild accounting (delay, failed_rebuilds,
    # reconnects); the wedged-adapter cap lives in BusReconnector.
    reconnector = (
        BusReconnector(reconnect_policy, max_reconnects, log)
        if reconnect_enabled and reconnect_policy is not None
        else None
    )

    log.info(
        "canable obd recording started",
        extra={
            "frames_path": str(paths.frames),
            "interface": spec.can.interface,
            "channel": spec.can.channel,
            "duration_seconds": duration,
            "auto_reconnect": reconnect_enabled,
        },
    )

    try:
        with can.io.CanutilsLogWriter(str(paths.frames)) as writer:

            def on_frame(msg: can.Message) -> None:
                writer.on_message_received(msg)

            while True:
                if local_stop.is_set():
                    stop_reason = "signal"
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    stop_reason = "deadline"
                    break

                # Lost the bus on a previous iteration: rebuild before recv.
                if current_bus is None:
                    assert reconnector is not None and factory is not None
                    outcome, current_bus = reconnector.rebuild(
                        factory,
                        spec.can,
                        stop_event=local_stop,
                        deadline_monotonic=deadline,
                        frames_recorded=frames_seen,
                    )
                    if outcome is RebuildOutcome.STOP_SIGNAL:
                        stop_reason = "signal"
                        break
                    if outcome is RebuildOutcome.STOP_DEADLINE:
                        stop_reason = "deadline"
                        break
                    if outcome is RebuildOutcome.EXHAUSTED:
                        stop_reason = "reconnect_exhausted"
                        error = reconnector.last_error
                        break
                    if outcome is RebuildOutcome.RETRY:
                        continue
                    # Only REBUILT falls through — it always returns a bus.
                    assert current_bus is not None

                try:
                    segment = run_can_obd_loop(
                        current_bus,
                        spec,
                        on_frame=on_frame,
                        on_reading=obd_writer.write,
                        stop_event=local_stop,
                        deadline_monotonic=deadline,
                    )
                except ValueError:
                    # Config error (bad PID/DID token) — not a transient bus
                    # fault. Mirrors collect_can_obd_from_config.
                    raise
                except Exception as exc:
                    if not reconnect_enabled:
                        raise
                    assert reconnector is not None
                    log.warning(
                        "canable obd loop failed; will attempt reconnect",
                        extra={
                            "error": f"{type(exc).__name__}: {exc}",
                            "delay_seconds": reconnector.delay,
                        },
                    )
                    try:
                        current_bus.shutdown()
                    except Exception:
                        # Bus may already be dead; shutdown is best-effort.
                        pass
                    current_bus = None
                    continue

                # Loop returned normally → stop_event or deadline ended it.
                frames_seen += segment.frames_seen
                parse_errors += segment.parse_errors
                if local_stop.is_set():
                    stop_reason = "signal"
                elif deadline is not None and time.monotonic() >= deadline:
                    stop_reason = "deadline"
                break
    except BaseException as exc:
        error = exc
        stop_reason = "error"
        raise
    finally:
        ended_at = datetime.now(UTC)
        # The bus may have been rebuilt; close whatever we currently hold so
        # we don't leak a USB handle. The original bus (if unchanged) is the
        # caller's to close.
        if reconnect_enabled and current_bus is not None and current_bus is not bus:
            try:
                current_bus.shutdown()
            except Exception:
                pass
        reconnects_total = reconnector.reconnects if reconnector is not None else 0
        result = RecordingResult(
            started_at=started_at,
            ended_at=ended_at,
            frames_recorded=frames_seen,
            stopped_by=stop_reason,
            parse_errors=parse_errors,
            reconnects=reconnects_total,
        )
        try:
            write_recording_manifest(
                paths.manifest,
                spec=spec.can,
                vehicle=vehicle,
                result=result,
                vehicle_state=vehicle_state,
                error=error,
            )
        except Exception:
            if error is None:
                raise
            log.exception("failed to write manifest after canable OBD recording error")
    return result


#: Mode 01 PID whose response carries both the continuous MONITOR_STATUS
#: count channel and (Task 1.4) the MIL bit `decode_monitor_status` drops.
_MONITOR_STATUS_PID = 0x01
_MIL_STATUS_CHANNEL = "MIL_STATUS"
#: Service whose "service" kind response carries per-cylinder misfire data
#: (Task 3.1/3.2) in addition to the raw sweep — see the `service` branch
#: below.
_MODE06_SERVICE = 0x06


def _response_to_reading(
    request: _PollRequest,
    response: ObdResponse,
    timestamp: float | None = None,
    *,
    arrival: FrameArrival | None = None,
    response_interval_s: float | None = None,
    round_state: RoundState | None = None,
) -> list[ObdReading]:
    """Decode a response and attach its acquisition provenance (C02.6/C02.7).

    ``arrival`` is when the (first) response frame reached us; it becomes
    ``acquired_at`` and, unless ``timestamp`` supplies a round stamp, also the
    presentation ``timestamp``. Without ``arrival`` the reading is legacy
    shaped: decode-time stamp, no acquisition metadata.

    Every reading decoded from one response shares that arrival instant. For
    a batched Mode 01 response that is the *receipt* time of one payload — it
    is not proof that the ECU sampled the PIDs it carried atomically.
    """
    if timestamp is None and arrival is not None:
        timestamp = arrival.at
    readings = _decode_response(request, response, timestamp)
    if arrival is None:
        return readings
    provenance: dict[str, object] = {
        "acquired_at": arrival.at,
        "acquired_clock": arrival.clock,
        "response_interval_s": response_interval_s,
    }
    if round_state is not None:
        provenance["generation"] = round_state.generation
        provenance["round_id"] = round_state.round_id
    return [replace(reading, **provenance) for reading in readings]  # type: ignore[arg-type]


def _decode_response(
    request: _PollRequest,
    response: ObdResponse,
    timestamp: float | None = None,
) -> list[ObdReading]:
    """Turn one raw response into zero or more decoded readings.

    Usually one reading per request; PID 0x01 emits a second (MIL_STATUS,
    Task 1.4) derived from the same payload instead of issuing another bus
    request just for the bit ``decode_monitor_status`` doesn't surface.

    ``timestamp`` overrides the decode-time clock read. A batched response
    carries several PIDs that were sampled by the ECU in *one* operation, so
    they must land on one timestamp; letting each decode call its own
    ``time.time()`` spreads a single measurement across several instants and
    forces every consumer to re-group them by proximity.
    """
    if timestamp is None:
        timestamp = time.time()
    if request.kind == "did":
        if not response.payload:
            return []
        return [ObdReading(timestamp, request.channel, response.payload.hex(), "")]

    if request.kind == "dtc":
        # Mode 03/07/0A (Task 1.3) — always emit, even with zero codes, so
        # downstream DTC state can tell "polled, none found" from "not
        # polled yet". Comma-joined string keeps the value on the same
        # TelemetryValue contract as every other reading.
        codes = parse_dtc_payload(response.payload)
        return [ObdReading(timestamp, request.channel, ",".join(codes), "")]

    if request.kind == "service":
        # Mode 06 (Task 3.1) — a whole monitor-test-result sweep is a list
        # of 9-byte records, which doesn't fit the scalar TelemetryValue
        # contract. Ship the raw payload as hex (matching the `did` branch
        # above); `src.control.monitor_state.MonitorState` parses it back
        # with `parse_mode06_response`. Always emit, even empty, so "polled,
        # nothing reported" is distinguishable from "not polled yet".
        # The ISO-TP matcher strips the echoed MID. Reattach it so the shared
        # Mode 06 parser receives its documented 9-byte
        # ``MID TID UAS value min max`` record.
        payload = response.payload
        if response.pid is not None:
            payload = bytes([response.pid]) + payload
        readings = [ObdReading(timestamp, request.channel, payload.hex(), "")]
        if request.service == _MODE06_SERVICE:
            # Task 3.2: also land per-cylinder misfire counts as ordinary
            # numeric channels (mirrors the MIL_STATUS derived-reading
            # pattern below) so they flow through storage into the wide
            # telemetry frame the session fingerprint scans — the hex sweep
            # reading above can't be aggregated numerically.
            counts = misfire_counts_from_results(parse_mode06_response(payload))
            for cylinder, count in sorted(counts.items()):
                readings.append(
                    ObdReading(timestamp, misfire_count_channel(cylinder), float(count), "count")
                )
        return readings

    if request.kind == "freeze_frame":
        # Mode 02 (Task 3.3) — same wire formula as Mode 01 for this PID
        # (`obd_registry.lookup(0x02, pid)`'s Mode-01 remap), but the
        # response carries one extra leading frame-number byte ahead of the
        # formula bytes that Mode 01 doesn't have; strip it before decoding.
        entry = PID_DECODERS.get(request.identifier)
        if entry is None or len(response.payload) < 2:
            return []
        _name, decoder = entry
        decoded = decoder(response.payload[1:])
        if decoded is None:
            return []
        value, unit = decoded
        return [ObdReading(timestamp, request.channel, value, unit, payload=response.payload)]

    if request.kind == "custom":
        # Task 2.6 — decode through the definition's sandboxed formula
        # instead of a PID_DECODERS lookup. `build_poll_requests` only ever
        # builds a "custom" request from a CustomPid, so this is set.
        assert request.custom_pid is not None
        decoded = evaluate_custom_pid(request.custom_pid, response.payload)
        if decoded is None:
            return []
        value, unit = decoded
        return [ObdReading(timestamp, request.channel, value, unit, payload=response.payload)]

    # Reuse the public PID decoder behavior without issuing another request:
    # synthesize the same DecodedPid logic locally from PID_DECODERS.
    entry = PID_DECODERS.get(request.identifier)
    if entry is None:
        return []
    name, decoder = entry
    decoded = decoder(response.payload)
    if decoded is None:
        return []
    value, unit = decoded
    readings = [ObdReading(timestamp, name, value, unit, payload=response.payload)]
    if request.identifier == _MONITOR_STATUS_PID:
        mil_on = decode_mil_on(response.payload)
        if mil_on is not None:
            readings.append(
                ObdReading(timestamp, _MIL_STATUS_CHANNEL, mil_on, "bool", payload=response.payload)
            )
    return readings


def _send_session_control(
    bus: can.BusABC,
    session_spec: DidSessionSpec,
    *,
    sub_function: int | None = None,
    request_id: int,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> SessionControlResult:
    """Send ``10 <session>`` (UDS DiagnosticSessionControl) and await ``50 <session>``.

    Opens or keeps alive the Mode-22 gating session, or — with
    ``sub_function`` — hands the bus back by returning to the default
    session. Session *control* only: no memory write, no actuation; the
    read-only posture of the collector is unchanged. Returns True on a
    positive response.
    """
    exiting = sub_function is not None
    request = _PollRequest(
        kind="session",
        service=0x10,
        identifier=sub_function if sub_function is not None else session_spec.session,
        request_id=request_id,
        channel="UDS_SESSION_EXIT" if exiting else "UDS_SESSION_CONTROL",
    )
    stats.session_controls_sent += 1
    started = time.monotonic()
    result = _query_with_tap(
        bus,
        request,
        timeout_seconds=timeout_seconds,
        on_frame=on_frame,
        stop_event=stop_event,
        stats=stats,
        deadline_monotonic=deadline_monotonic,
    )
    stats.session_control_seconds += max(0.0, time.monotonic() - started)
    if result.response is not None:
        stats.session_control_responses += 1
        return SessionControlResult(ok=True)
    # Session control bypasses the poll-request path, so without these the
    # only trace of a failure was a bare counter — a refusal and a silent bus
    # looked identical in `/status` and neither appeared in the log at all.
    # The shared counters keep every failure visible; the session-specific
    # ones keep it out of the channel-read throughput (C04.8).
    if result.negative_response_code is not None:
        stats.negative_responses += 1
        stats.session_control_negative_responses += 1
    else:
        stats.request_timeouts += 1
        stats.session_control_timeouts += 1
    return SessionControlResult(ok=False, negative_response_code=result.negative_response_code)


@dataclass(frozen=True)
class QueryResult:
    """Outcome of one `_query_with_tap` request/response cycle.

    Distinguishes a formal negative response (ECU replied with a reason,
    `0x7F <service> <NRC>`) from a true timeout (no frame at all) so
    callers can count them separately instead of folding both into
    "no response" — the gap that hid whether the Xterra's ECU is silent
    on Mode 0A/06 or promptly refusing them.
    """

    response: ObdResponse | None = None
    negative_response_code: int | None = None
    #: Arrival of the first response frame (C02.7); ``None`` without a response.
    arrival: FrameArrival | None = None
    #: Completion minus first-frame arrival for multi-frame responses, when > 0.
    response_interval_s: float | None = None


@dataclass(frozen=True)
class FrameArrival:
    """When a response frame reached the collector, and which clock says so."""

    at: float
    clock: AcquiredClock


def _frame_arrival(msg: can.Message) -> FrameArrival:
    """Acquisition instant for ``msg`` (C02.7).

    Prefers the bus driver's own stamp (``Message.timestamp`` — the kernel
    ``SO_TIMESTAMP`` on SocketCAN, the driver's receive-time read on slcan)
    over a ``time.time()`` taken after ISO-TP handling, provided it agrees
    with the wall clock to within :data:`KERNEL_STAMP_MAX_SKEW_SECONDS`.
    Adapters with no clock report 0.0 and fall back to userspace time; the
    row records which one it got.
    """
    now = time.time()
    stamp = getattr(msg, "timestamp", None)
    if isinstance(stamp, (int, float)) and abs(now - float(stamp)) <= KERNEL_STAMP_MAX_SKEW_SECONDS:
        return FrameArrival(at=float(stamp), clock="kernel")
    return FrameArrival(at=now, clock="userspace")


def _response_interval(first: FrameArrival | None, completed: FrameArrival) -> float | None:
    """Reassembly span of a multi-frame response; ``None`` for single-frame."""
    if first is None:
        return None
    interval = completed.at - first.at
    return interval if interval > 0 else None


@dataclass
class BatchedQueryResult:
    """Outcome of one multi-PID request/response cycle."""

    #: ``{pid: payload}`` for PIDs present in the response, or ``None`` when
    #: the batch as a whole failed (timeout, refusal, or a desynced walk).
    readings: dict[int, bytes] | None = None
    negative_response_code: int | None = None
    #: True when the payload walk completed without hitting an unknown PID
    #: or a truncated tail.
    clean: bool = True
    arrival: FrameArrival | None = None
    response_interval_s: float | None = None


@dataclass(frozen=True)
class BatchOutcome:
    """What one batched request produced, per member, for round accounting."""

    answered: bool
    #: Request index -> readings emitted for it.
    observed: Mapping[int, int] = field(default_factory=dict)
    #: Shared arrival instant of the batch response (receipt time, see
    #: ``_response_to_reading``); ``None`` when the batch failed.
    acquired_at: float | None = None


def _batched_pid_width(pid: int) -> int | None:
    """Mode 01 payload width for splitting a batched response."""
    definition = lookup(0x01, pid)
    if definition is None or definition.byte_length <= 0:
        return None
    return definition.byte_length


def _query_batched_with_tap(
    bus: can.BusABC,
    leader: _PollRequest,
    pids: list[int],
    *,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> BatchedQueryResult:
    """Issue one ``01 <pid> <pid> …`` request and split the response."""
    message = can.Message(
        arbitration_id=leader.request_id,
        data=build_batched_request_payload(0x01, pids),
        is_extended_id=False,
    )
    if not tx_gate.send(bus, message, what=f"batch {len(pids)} PIDs"):
        return BatchedQueryResult()
    stats.requests_sent += 1
    stats.batched_requests += 1
    stats.batched_pids_requested += len(pids)

    request_deadline = time.monotonic() + timeout_seconds
    if deadline_monotonic is not None:
        request_deadline = min(request_deadline, deadline_monotonic)
    # identifier=None: the payload is a stream of <pid> <data> pairs, so
    # there is no single echo to match on.
    assembler = IsoTpResponseAssembler(service=0x01, identifier=None)
    pending_extensions = 0
    first_frame: FrameArrival | None = None

    while not _should_stop(stop_event, request_deadline):
        remaining = max(0.001, request_deadline - time.monotonic())
        msg = _recv_and_tap(bus, on_frame, stats, timeout=min(0.05, remaining))
        if msg is None:
            continue
        if not (DEFAULT_RESPONSE_ID_MIN <= msg.arbitration_id <= DEFAULT_RESPONSE_ID_MAX):
            continue
        data = bytes(msg.data)
        if not data:
            continue
        result = assembler.accept(int(msg.arbitration_id), data)
        if result.response_pending:
            if pending_extensions >= MAX_RESPONSE_PENDING_EXTENSIONS:
                return BatchedQueryResult(negative_response_code=RESPONSE_PENDING_NRC)
            pending_extensions += 1
            request_deadline = _extend_for_response_pending(request_deadline, deadline_monotonic)
            continue
        if result.negative_response:
            return BatchedQueryResult(negative_response_code=result.negative_response_code)
        if result.flow_control_id is not None:
            first_frame = _frame_arrival(msg)
            tx_gate.send(
                bus,
                can.Message(
                    arbitration_id=result.flow_control_id,
                    data=FLOW_CONTROL_CTS,
                    is_extended_id=False,
                ),
                what="flow-control",
            )
            continue
        if result.response is not None:
            readings, clean = split_batched_response(result.response.payload, _batched_pid_width)
            completed = _frame_arrival(msg)
            return BatchedQueryResult(
                readings=readings,
                clean=clean,
                arrival=first_frame or completed,
                response_interval_s=_response_interval(first_frame, completed),
            )
    return BatchedQueryResult()


def _issue_batched_request(
    bus: can.BusABC,
    requests: list[_PollRequest],
    states: list[_PollRequestState],
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
    """
    pids = [requests[index].identifier for index in indices]
    leader = requests[indices[0]]
    started = time.monotonic()
    for index in indices:
        states[index].last_requested_at = started
        states[index].requests_sent += 1

    try:
        result = _query_batched_with_tap(
            bus,
            leader,
            pids,
            timeout_seconds=spec.request_timeout_seconds,
            on_frame=on_frame,
            stop_event=stop_event,
            stats=stats,
            deadline_monotonic=deadline_monotonic,
        )
    except Exception:  # noqa: BLE001 - propagate after recording scheduler state
        for index in indices:
            states[index].bus_errors += 1
        stats.bus_errors += 1
        raise

    if result.readings is None:
        stats.batch_failures += 1
        if result.negative_response_code is not None:
            stats.negative_responses += 1
            for index in indices:
                states[index].last_negative_response_code = result.negative_response_code
        return BatchOutcome(answered=False)
    if not result.clean:
        stats.batch_desyncs += 1

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
        payload = result.readings.get(request.identifier)
        emitted = False
        if payload is not None:
            # Synthesise the response the single-PID path would have seen, so
            # both paths decode through exactly the same code.
            readings = _response_to_reading(
                request,
                ObdResponse(
                    service=0x41,
                    pid=request.identifier,
                    payload=payload,
                    responder_id=DEFAULT_RESPONSE_ID_MIN,
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


def _query_with_tap(
    bus: can.BusABC,
    request: _PollRequest,
    *,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> QueryResult:
    # Mode 03/07/0A (Task 1.3) take no parameter byte, so their placeholder
    # identifier must not leak onto the wire. Mode 06 is different: it
    # requires a monitor ID (MID), including MID 0x00 for the supported-MID
    # query. Dropping ``service`` identifiers here turned ``06 00`` into a
    # bare ``06`` that this ECM silently ignores, and the assembler then also
    # waited for the wrong response shape.
    identifier = None if request.kind == "dtc" else request.identifier
    message = can.Message(
        arbitration_id=request.request_id,
        data=build_request_payload(request.service, identifier),
        is_extended_id=False,
    )
    if not tx_gate.send(bus, message, what=f"poll {request.channel}"):
        # Passive mode: no frame went out, so no response is coming. Return an
        # empty result WITHOUT counting a request or a timeout — the poll loop
        # normally skips issuing entirely when passive, so reaching here means
        # some other path called through; it must still not pollute the
        # scheduler's backoff state.
        return QueryResult()
    stats.requests_sent += 1

    request_deadline = time.monotonic() + timeout_seconds
    if deadline_monotonic is not None:
        request_deadline = min(request_deadline, deadline_monotonic)
    assembler = IsoTpResponseAssembler(service=request.service, identifier=identifier)
    pending_extensions = 0
    first_frame: FrameArrival | None = None

    while not _should_stop(stop_event, request_deadline):
        remaining = max(0.001, request_deadline - time.monotonic())
        msg = _recv_and_tap(bus, on_frame, stats, timeout=min(0.05, remaining))
        if msg is None:
            continue
        if not (DEFAULT_RESPONSE_ID_MIN <= msg.arbitration_id <= DEFAULT_RESPONSE_ID_MAX):
            continue
        data = bytes(msg.data)
        if not data:
            continue
        responder = int(msg.arbitration_id)
        result = assembler.accept(responder, data)
        if result.response_pending:
            if pending_extensions >= MAX_RESPONSE_PENDING_EXTENSIONS:
                return QueryResult(negative_response_code=RESPONSE_PENDING_NRC)
            pending_extensions += 1
            request_deadline = _extend_for_response_pending(request_deadline, deadline_monotonic)
            continue
        if result.negative_response:
            return QueryResult(negative_response_code=result.negative_response_code)
        if result.flow_control_id is not None:
            # Multi-frame response: the FIRST frame's arrival is the
            # acquisition instant; the reassembly span is kept separately.
            first_frame = _frame_arrival(msg)
            tx_gate.send(
                bus,
                can.Message(
                    arbitration_id=result.flow_control_id,
                    data=FLOW_CONTROL_CTS,
                    is_extended_id=False,
                ),
                what="flow-control",
            )
            continue
        if result.response is not None:
            completed = _frame_arrival(msg)
            return QueryResult(
                response=result.response,
                arrival=first_frame or completed,
                response_interval_s=_response_interval(first_frame, completed),
            )
    return QueryResult()


def _extend_for_response_pending(
    request_deadline: float,
    deadline_monotonic: float | None,
) -> float:
    """Grant one P2* extension after an ECU `0x78 responsePending`.

    Never pushes past ``deadline_monotonic`` — a shutdown or capture deadline
    still wins over an ECU asking for more time.
    """
    extended = request_deadline + RESPONSE_PENDING_EXTENSION_SECONDS
    if deadline_monotonic is not None:
        extended = min(extended, deadline_monotonic)
    return extended


def _recv_and_tap(
    bus: can.BusABC,
    on_frame: Callable[[can.Message], None],
    stats: CanObdLoopStats,
    *,
    timeout: float,
) -> can.Message | None:
    try:
        msg = bus.recv(timeout=timeout)
    except (ValueError, IndexError) as exc:
        stats.parse_errors += 1
        log_dropped_can_frame(stats.parse_errors, exc)
        return None
    if msg is None:
        return None
    stats.frames_seen += 1
    on_frame(msg)
    return msg


def _drain_frames_until(
    bus: can.BusABC,
    on_frame: Callable[[can.Message], None],
    stats: CanObdLoopStats,
    stop_event: threading.Event,
    until: float,
    deadline_monotonic: float | None,
) -> None:
    target = until if deadline_monotonic is None else min(until, deadline_monotonic)
    while not _should_stop(stop_event, target):
        remaining = max(0.001, target - time.monotonic())
        _recv_and_tap(bus, on_frame, stats, timeout=min(0.05, remaining))


def _should_stop(stop_event: threading.Event, deadline_monotonic: float | None) -> bool:
    if stop_event.is_set():
        return True
    return deadline_monotonic is not None and time.monotonic() >= deadline_monotonic


__all__ = [
    "AUTO_PIDS",
    "AUTO_BASELINE_PID_NAMES",
    "DEFAULT_DID_REQUEST_ID",
    "CanObdLoopStats",
    "CanObdSpec",
    "DidSessionSpec",
    "ObdSchedulerMetrics",
    "build_poll_requests",
    "collect_can_obd_from_config",
    "parse_can_id",
    "parse_did_token",
    "parse_pid_token",
    "record_can_obd",
    "resolve_did_session_spec",
    "run_can_obd_loop",
]
