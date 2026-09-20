"""Canable-native OBD streaming collector.

This is the replacement path for the dead ELM327 adapter. It owns one
``python-can`` Bus, records every received CAN frame, and interleaves active
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
from collections.abc import Callable, Container, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any

import can

from src.can_ids import parse_standard_can_id
from src.collection import bus_budget, capture_generation, tuning_profile, tx_gate
from src.collection import discovery as discovery_registry
from src.collection.can_collector import (
    DEFAULT_RECONNECT_POLICY,
    RecordingResult,
    bus_factory,
    frame_to_record,
)
from src.collection.can_collector import _write_manifest as write_recording_manifest
from src.collection.can_obd_client import (
    PID_DECODERS,
    CanObdClient,
    FrameTap,
    decode_mil_on,
    parse_dtc_payload,
    receive_frame,
)
from src.collection.custom_pids import evaluate_custom_pid
from src.collection.discovery import (
    MODE01_SUPPORT_GROUPS,
    MODE06_SUPPORT_GROUPS,
    DiscoveryItem,
    DiscoveryState,
)
from src.collection.hybrid_cycle import CyclePlan, WakeDecision, next_wake_at, plan_cycle
from src.collection.isotp import (
    DEFAULT_RESPONSE_ID_MAX,
    DEFAULT_RESPONSE_ID_MIN,
    FLOW_CONTROL_CTS,
    RESPONSE_PENDING_NRC,
    IsoTpResponseAssembler,
    ObdResponse,
    build_batched_did_request_payload,
    build_batched_request_payload,
    build_memory_read_payload,
    build_request_payload,
    split_batched_did_response,
    walk_batched_response,
)
from src.collection.obd_collector import (
    AcquiredClock,
    JsonlReadingWriter,
    ObdReading,
    reading_to_record,
)
from src.collection.obd_registry import (
    misfire_count_channel,
    misfire_counts_from_results,
    parse_mode06_response,
)
from src.collection.obd_scheduler import (
    AUTO_MISS_BACKOFF_SECONDS,
    AUTO_PIDS,
    DEFAULT_DID_REQUEST_ID,
    SESSION_WINDOW_KINDS,
    CanObdLoopStats,
    CanObdSpec,
    DidSessionSpec,
    DidWindowController,
    Mode01WidthLedger,
    ObdSchedulerMetrics,
    RequestFilter,
    RoundScheduler,
    RoundState,
    SessionControlResult,
    _bus_budget_requests_per_second,
    _count_active_requests,
    _DiscoveryClient,
    _initial_request_states,
    _next_due_at,
    _next_request_due_at,
    _next_unsuppressed_at,
    _PollRequest,
    _PollRequestState,
    _scheduler_metrics,
    _select_next_due_request,
    _update_response_state,
    apply_mode01_support,
    build_poll_requests,
    is_batchable,
    mode01_registry_width,
    mode06_requests,
    parse_did_token,
    parse_pid_token,
    resolve_did_session_spec,
    round_members,
    round_period_seconds,
    select_batch_companions,
    select_did_batch_companions,
)
from src.collection.queueing import (
    CollectorHandoffStats,
    HandoffStats,
    QueueDropCounter,
    flush_dropped_can_frame_log,
    log_dropped_can_frame,
    threadsafe_enqueue,
)
from src.collection.reconnect import (
    BusReconnector,
    RebuildOutcome,
    ReconnectPolicy,
    run_with_reconnect,
)
from src.collection.request_eligibility import Eligibility, SessionPhase, evaluate
from src.config import CanBusConfig, VehicleConfig
from src.decode.dids import (
    apply_memory_scaling,
    decode_memory_raw,
    is_memory_sentinel,
)
from src.session import SessionPaths
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)


def _loop_request_verdict(
    controller: DidWindowController | None,
    profile_gated: bool,
    forced: Container[int],
    index: int,
    request: _PollRequest,
) -> Eligibility:
    """Both gates in one matrix call (C06.7): phase from the session
    controller, selection and pause from the active profile, and the forced
    one-shot lift for ``trigger_poll``. The DID window's own pending set is
    the one fact the matrix cannot know."""
    phase: SessionPhase = controller.matrix_phase if controller is not None else "none"
    verdict = evaluate(
        request.kind,
        tuning_profile.profile_class(),
        phase,
        selection=(
            tuning_profile.selection_exclusion(request.kind, request.channel)
            if profile_gated
            else None
        ),
        paused=tuning_profile.is_paused() if profile_gated else False,
        forced=index in forced,
    )
    if (
        verdict.eligible
        and controller is not None
        and request.kind == "did"
        and not controller.did_pending(index)
    ):
        return Eligibility("deferred", "attempted_this_window")
    return verdict


def _loop_request_eligible(
    controller: DidWindowController | None,
    profile_gated: bool,
    forced: Container[int],
    index: int,
    request: _PollRequest,
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
#: Bound on the ``deferred`` list in the scheduler metrics (C06.7): a focused
#: capture can leave dozens of the union plan's channels unselected.
MAX_DEFERRED_REPORT = 64
#: Discovery wait reasons that clear within a slot or a round on their own.
TRANSIENT_DISCOVERY_WAITS: tuple[str, ...] = ("budget", "discovery_budget", "round_open")
#: A bus-driver frame stamp further than this from ``time.time()`` is not
#: trusted as the acquisition clock (an adapter with no clock reports 0.0;
#: a stale SocketCAN stamp after a suspend can be minutes off). The
#: userspace receipt time is used instead and the row says so (C02.7).
KERNEL_STAMP_MAX_SKEW_SECONDS = 5.0
#: Identity of this loop's parse-error log throttle (one line per
#: ``DROP_LOG_INTERVAL_SECONDS``). Distinct from the passive sniffer's owners
#: so a storm on one live bus never suppresses the other's warning lines.
_PARSE_ERROR_OWNER = "canable-obd"


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
    stats = CanObdLoopStats()
    # Every (re)start of the loop is a new capture generation (C02.8): a bus
    # rebuild after a fault must not let a request issued before it complete
    # a round scheduled after it.
    rounds = RoundScheduler(capture_generation.bump("collector-start"), stats)
    tap = _FrameTap(on_frame, stats)
    client: _DiscoveryClient = (
        CanObdClient(
            bus,
            timeout_seconds=spec.request_timeout_seconds,
            time_fn=time.monotonic,
            on_frame=tap,
        )
        if client_factory is None
        else client_factory(bus, tap)
    )
    requests = build_poll_requests(None, spec)
    request_states = _initial_request_states(requests, spec, time.monotonic())
    discovery = discovery_registry.recall(spec.discovery_key, _discovery_items(spec))
    installed: set[DiscoveryItem] = set()
    _install_discovery(discovery, requests, request_states, spec, time.monotonic(), installed)
    discovery_wait: str | None = None
    #: The last *structural* reason discovery waited since its last probe
    #: (a phase, a pause, capacity). ``budget`` and ``round_open`` are
    #: transient by nature and would make the reported reason flicker.
    discovery_blocked: str | None = None
    #: Request indices an operator forced due (``trigger_poll``), until served.
    forced: set[int] = set()
    profile_gated = spec.capture_profiles_enabled or tuning_profile.is_active()
    budget_rps = _bus_budget_requests_per_second(spec)
    # Everything the Mode-22 window serves, not only Mode 22 itself:
    # service-0x23 memory reads are gated by the same session on this ECM,
    # so leaving them out would schedule every one of them in the phase
    # where the ECM is silent about them.
    did_indices = [
        index for index, request in enumerate(requests) if request.kind in SESSION_WINDOW_KINDS
    ]
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
                "memory_channels": [r.channel for r in requests if r.kind == "memory"],
                "poll_tiers": {r.channel: r.poll_tier for r in requests},
                "bus_budget_requests_per_second": budget_rps,
                "auto_miss_backoff_seconds": AUTO_MISS_BACKOFF_SECONDS,
                "did_session_windows": (
                    controller.snapshot(time.monotonic()) if controller is not None else None
                ),
            },
        )
    elif not discovery.pending():
        log.warning("canable obd streaming has no PID/DID requests; recording CAN only")

    next_budget_at = time.monotonic()
    next_discovery_at = time.monotonic()
    next_metrics_at = time.monotonic()
    # Batching is opt-in and self-disabling. An ECU that ignores or refuses
    # multi-PID requests would otherwise fail every batch forever, so a run of
    # consecutive failures falls the session back to single-PID rather than
    # stalling. This doubles as the adaptive form of the startup size probe:
    # we discover what the vehicle actually accepts by using it.
    #
    # But a silent batch is only evidence about batching when the ECU is
    # awake. The Pi boots with the key off (ECM asleep, every request times
    # out), and counting that silence narrowed 6 -> 4 -> 2 -> off within
    # seconds and cost the whole drive its batching (2026-09-18). So a failed
    # batch counts only when the ECU answered *something* since the previous
    # failure (or refused the batch with an NRC, which is itself an answer).
    # Otherwise it is not counted, and the next request of that service goes
    # out single as a probe: an awake ECU that ignores batches answers the
    # probe, and its next batch failure then counts; an asleep ECU keeps
    # producing silence and nothing narrows.
    batching_enabled = spec.batch_max_pids > 1
    active_batch_max = spec.batch_max_pids
    consecutive_batch_failures = 0
    did_batching_enabled = spec.did_batch_max_dids > 1
    active_did_batch_max = min(spec.did_batch_max_dids, 3)
    consecutive_did_batch_failures = 0
    ecu_answered_since_pid_batch_failure = False
    ecu_answered_since_did_batch_failure = False
    probe_single_pid = False
    probe_single_did = False
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
                        # Kept until served: the matrix lifts the profile
                        # selection for it but not the phase it needs (C06.7).
                        forced.add(index)
        if discovery.version != discovery_registry.cache_version():
            # ``reset_discovery``: every walk restarts; results already in
            # the plan stay (a re-walk only adds what it newly learns).
            discovery = discovery_registry.fresh(spec.discovery_key, _discovery_items(spec))
            installed.clear()
        # The selected profile applies even for ``default``: the request plan
        # is a startup superset of every saved profile, and filtering here is
        # what keeps the seeded default byte-identical to config. Standalone
        # recorder/tests that never initialised the catalog keep the
        # scheduler's historical behaviour.
        profile_gated = spec.capture_profiles_enabled or tuning_profile.is_active()
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
                    discovery=discovery.snapshot(
                        now, deferred_reason=discovery_blocked or discovery_wait
                    ),
                    deferred=_deferred_report(
                        requests,
                        request_states,
                        partial(_loop_request_verdict, controller, profile_gated, forced),
                        now,
                        discovery=discovery,
                        discovery_wait=discovery_blocked or discovery_wait,
                    ),
                    pid_batch_max=active_batch_max if batching_enabled else 1,
                    did_batch_max=active_did_batch_max if did_batching_enabled else 1,
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

        if not requests and not discovery.pending():
            wake = next_wake_at(now, deadline=deadline_monotonic)
            _drain_frames_until(bus, on_frame, stats, stop_event, wake.at, deadline_monotonic)
            continue

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
            eligible = partial(_loop_request_eligible, controller, profile_gated, forced)
        elif profile_gated:
            # A profile with no DIDs deliberately suspends the session
            # controller, concentrating the budget on its Mode-01 selection.
            eligible = partial(_loop_request_eligible, None, True, forced)

        # A round ends at its deadline even while more work is due (C02.2);
        # this runs before any selection so no reading is ever stamped with
        # an expired round.
        rounds.expire(now)

        # Support discovery as scheduler work (C06.4/C06.6): one probe per
        # budget slot, in a phase the matrix admits, and — under a capture
        # profile — only in a slot no round member needs. Otherwise it waits
        # and says why.
        discovery_wait = None
        discovery_item = discovery.due(now)
        if discovery_item is None:
            discovery_blocked = None
        else:
            if now < next_discovery_at:
                discovery_wait = "discovery_budget"
            else:
                discovery_wait = _discovery_wait_reason(
                    controller,
                    rounds,
                    requests,
                    request_states,
                    spec,
                    now,
                    eligible=eligible,
                    next_budget_at=next_budget_at,
                    budget_rps=budget_rps,
                )
            if discovery_wait not in (None, *TRANSIENT_DISCOVERY_WAITS):
                discovery_blocked = discovery_wait
            if discovery_wait is None:
                discovery_blocked = None
                discovery_answers_before = (
                    stats.discovery_responses + stats.discovery_negative_responses
                )
                discovery = _issue_discovery_probe(
                    client,
                    discovery,
                    discovery_item,
                    tap=tap,
                    stats=stats,
                    stop_event=stop_event,
                    deadline_monotonic=deadline_monotonic,
                )
                _install_discovery(
                    discovery, requests, request_states, spec, time.monotonic(), installed
                )
                discovery_registry.remember(discovery)
                if (
                    stats.discovery_responses + stats.discovery_negative_responses
                    > discovery_answers_before
                ):
                    ecu_answered_since_pid_batch_failure = True
                    ecu_answered_since_did_batch_failure = True
                next_budget_at = max(
                    next_budget_at + 1.0 / bus_budget.effective(budget_rps), time.monotonic()
                )
                next_discovery_at = time.monotonic() + (
                    1.0 / spec.discovery_budget_requests_per_second
                )
                continue
        discovery_wants_budget = discovery_wait == "budget"

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
                budget_only_for_session=not discovery_wants_budget,
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
                # bus is still received until the earliest relevant deadline
                # (or the budget slot a pending discovery probe can use).
                wake = _wake_for(
                    now,
                    controller,
                    request_states,
                    budget_at=next_budget_at if discovery_wants_budget else None,
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

        if request.kind == "did":
            batch_indices = select_did_batch_companions(
                requests,
                request_states,
                now,
                leader_index=request_index,
                max_dids=(
                    active_did_batch_max if did_batching_enabled and not probe_single_did else 1
                ),
                response_widths=spec.did_response_widths,
                eligible=eligible,
                members=current_round.remaining,
                preferred=preferred,
            )
        else:
            batch_indices = select_batch_companions(
                requests,
                request_states,
                now,
                leader_index=request_index,
                max_pids=active_batch_max if batching_enabled and not probe_single_pid else 1,
                eligible=eligible,
                ignore_tier=round_timestamp is not None,
                members=current_round.remaining,
                preferred=preferred,
                widths=stats.mode01_widths,
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
                controller.on_service_time(
                    "mode22" if request.kind == "did" else "mode01",
                    time.monotonic() - batch_started,
                )
                if outcome.observed:
                    controller.on_response(request_index, time.monotonic(), outcome.acquired_at)
            if outcome.answered or outcome.negative_response_code is not None:
                ecu_answered_since_pid_batch_failure = True
                ecu_answered_since_did_batch_failure = True
            if outcome.answered:
                if request.kind == "did":
                    consecutive_did_batch_failures = 0
                else:
                    consecutive_batch_failures = 0
                discovery = discovery.on_contact(time.monotonic())
            else:
                # Silence (not an NRC) means the next request of this service
                # must re-establish that the ECU is awake before another
                # silent batch can count.
                silent = outcome.negative_response_code is None
                if request.kind == "did":
                    counts = ecu_answered_since_did_batch_failure
                    ecu_answered_since_did_batch_failure = False
                    probe_single_did = silent
                else:
                    counts = ecu_answered_since_pid_batch_failure
                    ecu_answered_since_pid_batch_failure = False
                    probe_single_pid = silent
                if not counts:
                    # Debug only: a key-off ECU produces one of these per
                    # budget slot for as long as it sleeps.
                    if request.kind == "did":
                        stats.did_batch_failures_ecu_silent += 1
                    else:
                        stats.batch_failures_ecu_silent += 1
                    log.debug(
                        "%s batch failure not counted toward narrowing: no ECU "
                        "answer since the previous failure; probing single",
                        "multi-DID" if request.kind == "did" else "multi-PID",
                    )
                elif request.kind == "did":
                    consecutive_did_batch_failures += 1
                    if consecutive_did_batch_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                        did_batching_enabled = False
                        consecutive_did_batch_failures = 0
                        stats.did_batch_size_narrowings += 1
                        log.warning(
                            "multi-DID batching disabled for this ECU/session; "
                            "falling back to healthy individual DID requests"
                        )
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
                                "narrowing multi-PID batch size %d -> %d after %d "
                                "consecutive failures",
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
                forced.discard(index)
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
        # This single is the liveness probe a silent batch asked for.
        if request.kind == "did":
            liveness_probe = probe_single_did
            probe_single_did = False
        else:
            liveness_probe = probe_single_pid
            probe_single_pid = False
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
                rounds.on_observed(request_index, len(readings), acquired_at)
                for reading in readings:
                    stats.record_read(reading.pid, time.monotonic())
                    on_reading(reading)
        if emitted or result.negative_response_code is not None:
            ecu_answered_since_pid_batch_failure = True
            ecu_answered_since_did_batch_failure = True
        if emitted:
            request_state.responses += 1
            request_state.last_response_at = time.monotonic()
            discovery = discovery.on_contact(time.monotonic())
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
        if not (liveness_probe and not emitted and result.negative_response_code is None):
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
        forced.discard(request_index)

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
                discovery=discovery.snapshot(
                    time.monotonic(), deferred_reason=discovery_blocked or discovery_wait
                ),
                deferred=_deferred_report(
                    requests,
                    request_states,
                    partial(_loop_request_verdict, controller, profile_gated, forced),
                    time.monotonic(),
                    discovery=discovery,
                    discovery_wait=discovery_blocked or discovery_wait,
                ),
            )
        )
        stats.scheduler_snapshots += 1
    rounds.close(time.monotonic())
    discovery_registry.remember(discovery)
    # The loop owned the bus: a parse-error burst that ran to the end of it is
    # summarized here or never (one forced line, at a once-per-loop boundary).
    flush_dropped_can_frame_log(owner=_PARSE_ERROR_OWNER, force=True)
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
        controller.on_session_response(result=entry_result, states=states, now=time.monotonic())
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


def _discovery_items(spec: CanObdSpec) -> dict[DiscoveryItem, tuple[int, tuple[int, ...]]]:
    """Which support walks this spec owes: ``item -> (service, groups)``."""
    wanted: dict[DiscoveryItem, tuple[int, tuple[int, ...]]] = {}
    if any(parse_pid_token(token) is None for token in (spec.pids or (AUTO_PIDS,))):
        wanted["mode01"] = (0x01, MODE01_SUPPORT_GROUPS)
    if spec.service_poll_interval_seconds > 0 and spec.service_mids == "auto":
        wanted["mode06"] = (0x06, MODE06_SUPPORT_GROUPS)
    return wanted


def _discovery_wait_reason(
    controller: DidWindowController | None,
    rounds: RoundScheduler,
    requests: list[_PollRequest],
    states: list[_PollRequestState],
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
    requests: list[_PollRequest],
    states: list[_PollRequestState],
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
    requests: list[_PollRequest],
    states: list[_PollRequestState],
    verdict: Callable[[int, _PollRequest], Eligibility],
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
        "responder_id": response.responder_id,
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

    if request.kind == "memory":
        assert request.memory_channel is not None
        memory_channel = request.memory_channel
        raw = decode_memory_raw(memory_channel, response.payload)
        if raw is None:
            return []
        if is_memory_sentinel(memory_channel, raw):
            # "Polled, and the ECU is commanding nothing" is an answer about
            # the engine; emitting no row at all would be indistinguishable
            # from "not polled", and scaling the sentinel would put a
            # fabricated value into every mean that follows.
            return [
                ObdReading(
                    timestamp,
                    f"{request.channel}_SENTINEL",
                    memory_channel.sentinel_meaning or "sentinel",
                    "",
                    payload=response.payload,
                )
            ]
        scaled = apply_memory_scaling(memory_channel, raw)
        if scaled is None:
            return []
        return [
            ObdReading(
                timestamp,
                request.channel,
                scaled,
                memory_channel.unit,
                payload=response.payload,
            )
        ]

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
    session. This helper performs session control only—no memory write or
    actuation—and returns True on a positive response.
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
    responder_id: int | None = None
    #: Mode 01 only: the PID the walk blames for a desync (``BatchWalk.suspect``).
    suspect: int | None = None


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


def _batched_pid_width(pid: int, widths: Mode01WidthLedger | None = None) -> int | None:
    """Mode 01 payload width for splitting a batched response.

    The registry width, unless ``widths`` has measured this PID answering with
    a different one — then ``None``, so a walk that meets it stops rather than
    slicing by a width the vehicle has contradicted. (Such a PID is never put
    in a batch; this is the walk's own refusal, not the batching rule.)
    """
    if widths is not None:
        return widths.batch_width(pid)
    return mode01_registry_width(pid)


def _query_batched_with_tap(
    bus: can.BusABC,
    leader: _PollRequest,
    identifiers: list[int],
    *,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
    did_response_widths: Mapping[int, int] | None = None,
    mode01_widths: Mode01WidthLedger | None = None,
) -> BatchedQueryResult:
    """Issue one Mode-01 or Mode-22 batch through the owned receive tap."""
    is_did = leader.kind == "did"
    service = 0x22 if is_did else 0x01
    payload = (
        build_batched_did_request_payload(service, identifiers)
        if is_did
        else build_batched_request_payload(service, identifiers)
    )
    message = can.Message(
        arbitration_id=leader.request_id,
        data=payload,
        is_extended_id=False,
    )
    if not tx_gate.send(bus, message, what=f"batch {len(identifiers)} identifiers"):
        return BatchedQueryResult()
    stats.requests_sent += 1
    if is_did:
        stats.did_batched_requests += 1
        stats.did_batched_members_requested += len(identifiers)
    else:
        stats.batched_requests += 1
        stats.batched_pids_requested += len(identifiers)

    request_deadline = time.monotonic() + timeout_seconds
    if deadline_monotonic is not None:
        request_deadline = min(request_deadline, deadline_monotonic)
    # identifier=None: the payload is a stream of <pid> <data> pairs, so
    # there is no single echo to match on.
    assembler = IsoTpResponseAssembler(service=service, identifier=None)
    pending_extensions = 0
    first_frame: FrameArrival | None = None

    while not _should_stop(stop_event, request_deadline):
        remaining = max(0.001, request_deadline - time.monotonic())
        msg = _recv_and_tap(bus, on_frame, stats, timeout=min(0.05, remaining))
        if msg is None:
            continue
        if not (DEFAULT_RESPONSE_ID_MIN <= msg.arbitration_id <= DEFAULT_RESPONSE_ID_MAX):
            continue
        # A physical request has one owning responder. Functional 0x7DF may
        # legitimately receive 0x7E8..0x7EF, but accepting 0x7E9 for a 0x7E0
        # DID batch would attach another module's bytes to the ECM plan.
        if leader.request_id != 0x7DF and msg.arbitration_id != leader.request_id + 8:
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
            suspect: int | None = None
            if is_did:
                readings, clean = split_batched_did_response(
                    result.response.payload,
                    lambda did: (did_response_widths or {}).get(did),
                )
            else:
                walk = walk_batched_response(
                    result.response.payload,
                    lambda pid: _batched_pid_width(pid, mode01_widths),
                    expected=frozenset(identifiers),
                )
                readings, clean, suspect = walk.readings, walk.clean, walk.suspect
            completed = _frame_arrival(msg)
            return BatchedQueryResult(
                readings=readings,
                clean=clean,
                arrival=first_frame or completed,
                response_interval_s=_response_interval(first_frame, completed),
                responder_id=result.response.responder_id,
                suspect=suspect,
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
    # A service-0x23 read carries an address and a length instead of an
    # identifier, and its positive response echoes neither -- so `identifier`
    # must be None for the assembler too, or it would look for an echo that
    # is never sent and drop every reply.
    if request.kind == "memory":
        assert request.memory_channel is not None
        identifier = None
        request_data = build_memory_read_payload(
            request.memory_channel.address, request.memory_channel.length
        )
    else:
        identifier = None if request.kind == "dtc" else request.identifier
        request_data = build_request_payload(request.service, identifier)
    message = can.Message(
        arbitration_id=request.request_id,
        data=request_data,
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
        if request.request_id != 0x7DF and msg.arbitration_id != request.request_id + 8:
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
    """The collector's receive: ``receive_frame`` plus the loop's accounting.

    Every receive the loop makes — polls, batches, session control, drains —
    comes through here; the client it hands its tap to uses the same
    ``receive_frame``. Nothing else on the loop's thread calls ``bus.recv``.
    """
    try:
        msg = receive_frame(bus, timeout=timeout, on_frame=on_frame)
    except (ValueError, IndexError) as exc:
        stats.parse_errors += 1
        log_dropped_can_frame(exc, owner=_PARSE_ERROR_OWNER)
        return None
    if msg is None:
        return None
    flush_dropped_can_frame_log(owner=_PARSE_ERROR_OWNER)
    stats.frames_seen += 1
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
