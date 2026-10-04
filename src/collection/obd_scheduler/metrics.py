"""Loop statistics and the scheduler-metrics snapshot, including the resolved
plan the capture-profile preview shares with the live metrics.

Part of the ``obd_scheduler`` package (R02.10).
"""

from __future__ import annotations

import itertools
import logging
import statistics
from collections import deque
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.collection import bus_budget, capture_lifetime, tuning_profile
from src.collection.isotp import nrc_name
from src.collection.obd_registry import lookup
from src.collection.obd_scheduler.cadence import (
    _is_request_backed_off,
    _is_request_suppressed,
    _request_rate_hz,
)
from src.collection.obd_scheduler.requests import (
    Mode01WidthLedger,
    PollRequest,
    PollRequestState,
    _request_accounting_error,
    _request_sends_judged,
    build_poll_requests,
)
from src.collection.obd_scheduler.selection import (
    _count_active_requests,
)
from src.collection.obd_scheduler.spec import (
    CanObdSpec,
    _bus_budget_requests_per_second,
    did_alias_table,
    profile_did_union,
)
from src.collection.polling_plan import PlanMember, ResolvedPollingPlan
from src.config import CanBusConfig, CollectionConfig

if TYPE_CHECKING:
    from src.collection.capture_profiles import CaptureProfile
    from src.collection.profile_view import ProfileView

log = logging.getLogger(__name__)


#: Trailing window for the per-channel achieved rate, in seconds. Long enough
#: that a slow tier still lands a sample, short enough that arming a capture
#: and re-reading /status a moment later shows the new rate rather than an
#: average dominated by however long the daemon has been up.
ACHIEVED_HZ_WINDOW = 60.0


def preview_discovery_active(profile: CaptureProfile) -> bool:
    """Whether a preview charges support-discovery demand to ``profile``.

    Live, discovery is runtime state: walks are owed at loop start and retire
    within seconds, cached per vehicle. A preview describes the profile's
    steady state, so it charges discovery only to a profile whose selection
    *is* the discovered set (``AUTO``).
    """
    return any(token.strip().upper() == "AUTO" for token in profile.mode01)


def preview_polling_plan(
    collection: CollectionConfig,
    profile: CaptureProfile,
    *,
    custom_pids_path: Path | None = None,
) -> dict[str, Any]:
    """The plan ``profile`` would run, before it is armed (R02.3).

    Not a model of the scheduler: the scheduler itself, asked about a
    candidate. The spec comes from ``CanObdSpec.from_config`` with the
    candidate as the catalog, the requests from ``build_poll_requests``, and
    the plan from ``_resolved_polling_plan`` with a :class:`ProfileView` of
    the candidate in place of the active profile. The live metrics call the
    same function with the active profile, so the two cannot drift.

    What only the running loop knows is an input, not policy: the configured
    batch sizes (live may narrow them), whether support discovery is still
    walking (:func:`preview_discovery_active`), and the budget the capture
    route arms (the profile's ``bus_budget_override``, else the configured
    default).
    """
    from src.collection.profile_view import ProfileView

    spec = CanObdSpec.from_config(
        collection,
        # Never opened: the preview sends nothing.
        CanBusConfig(interface="virtual", channel="capture-profile-preview", bitrate=500000),
        catalog=(profile,),
        custom_pids_path=custom_pids_path,
    )
    view = ProfileView(profile, did_alias_table(profile_did_union(collection, (profile,))))
    return _resolved_polling_plan(
        build_poll_requests(None, spec),
        spec,
        pid_batch_max=spec.batch_max_pids,
        did_batch_max=spec.did_batch_max_dids,
        discovery_active=preview_discovery_active(profile),
        view=view,
        budget_rps=profile.bus_budget_override or _bus_budget_requests_per_second(spec),
    )


@dataclass(frozen=True)
class ChannelRate:
    """What a capture achieved for one channel: rate and coverage, separately.

    Keeping these apart is the whole point. A single "achieved Hz" derived
    from ``samples / wall-clock`` answers neither question honestly — a
    perfectly-paced channel looks slow if the capture spans time it wasn't
    being polled, which is exactly when an operator reaches for the number.
    """

    #: Sampling rate while actually sampling, from the median inter-sample gap.
    hz: float
    #: Samples observed within the capture epoch.
    samples: int
    #: Longest stretch with no sample, bracketed by the capture epoch and now.
    #: Large relative to ``1 / hz`` means a coverage problem, not a rate one.
    max_gap_seconds: float


@dataclass
class CanObdLoopStats:
    frames_seen: int = 0
    parse_errors: int = 0
    requests_sent: int = 0
    readings_emitted: int = 0
    request_timeouts: int = 0
    #: D03.18: multi-frame responses whose continuation never completed
    #: within N_Cr (isotp.CONSECUTIVE_FRAME_TIMEOUT_SECONDS), and how often a
    #: re-sent flow control was tried and then rescued the response.
    continuation_losses: int = 0
    flow_control_resends: int = 0
    flow_control_rescues: int = 0
    #: Formal ECU refusals (0x7F <service> <NRC>) — distinct from
    #: `request_timeouts` (no frame at all). Folding these together made a
    #: silent ECU indistinguishable from one promptly saying "no" with a
    #: reason (found investigating Mode 0A/06 on the Xterra).
    negative_responses: int = 0
    bus_errors: int = 0
    scheduler_snapshots: int = 0
    session_controls_sent: int = 0
    #: Session-control (UDS 0x10) traffic, kept apart from channel-read
    #: throughput (C04.8). ``requests_sent`` still counts every frame on the
    #: wire; ``ObdSchedulerMetrics.session_control`` reports these and the
    #: channel-only remainder side by side.
    session_control_responses: int = 0
    session_control_timeouts: int = 0
    session_control_negative_responses: int = 0
    session_control_seconds: float = 0.0
    #: Multi-PID requests issued, and the PIDs they carried. The ratio is the
    #: round-trip saving, which is the whole point of batching.
    batched_requests: int = 0
    batched_pids_requested: int = 0
    #: Batches that produced nothing usable — timeout, refusal, or a walk that
    #: desynced. Consecutive failures disable batching for the session.
    batch_failures: int = 0
    #: Batches that answered but whose payload walk stopped early. The values
    #: collected before the stop are still good; this counts the events so a
    #: partially-understood PID set is visible rather than silent.
    batch_desyncs: int = 0
    #: Times the session narrowed its batch size after repeated failures.
    batch_size_narrowings: int = 0
    #: Of ``batch_failures``, those not counted toward narrowing because the
    #: ECU had shown no sign of life since the previous failure (asleep, key
    #: off). Silence from a sleeping ECU says nothing about batching.
    batch_failures_ecu_silent: int = 0
    did_batched_requests: int = 0
    did_batched_members_requested: int = 0
    did_batch_failures: int = 0
    did_batch_desyncs: int = 0
    did_batch_size_narrowings: int = 0
    did_batch_failures_ecu_silent: int = 0
    #: Support discovery as scheduler work (C06.8). ``requests_sent`` still
    #: counts every frame on the wire; these separate the probes from channel
    #: reads, and count the raw frames tapped *while* a probe was waiting —
    #: the frames the old synchronous path silently consumed.
    discovery_requests_sent: int = 0
    #: ECU identification reads (the W02.2 calibration id): on the wire, so in
    #: ``requests_sent``, but not channel reads.
    identification_requests_sent: int = 0
    discovery_responses: int = 0
    discovery_negative_responses: int = 0
    discovery_timeouts: int = 0
    discovery_seconds: float = 0.0
    discovery_frames_seen: int = 0
    #: Capture rounds (C02). A round is one scheduled sampling window: it
    #: ends when every member has been attempted once or at its deadline,
    #: whichever comes first, so a stamp can never span more than one period.
    #: ``rounds_incomplete`` counts deadlines reached with members still
    #: unattempted (budget or timeouts could not serve the window);
    #: ``rounds_skipped`` counts whole periods that passed with no round
    #: opened while work was pending (the loop fell behind and skipped to the
    #: next future deadline rather than bursting catch-up samples).
    rounds_started: int = 0
    rounds_complete: int = 0
    rounds_incomplete: int = 0
    rounds_skipped: int = 0
    #: Largest measured acquisition spread inside one round — the honest
    #: "how simultaneous were these" number. Round identity is a scheduling
    #: fact; this is the evidence.
    round_skew_max_s: float = 0.0
    #: ``time.monotonic()`` when the loop started, so metrics can report an
    #: achieved rate rather than a bare count.
    loop_started_at: float | None = None
    #: Recent reading times per channel, for a *trailing-window* rate.
    #:
    #: The lifetime average (`batching.channel_reads_per_second`) cannot answer
    #: "is the capture I just armed working?" — it is diluted by however long
    #: the daemon has been up, and on 2026-08-04 it stayed near its old value
    #: through a rate change, which read as "the change did nothing". Deques
    #: bounded by ACHIEVED_HZ_WINDOW keep this O(reads in the window).
    recent_reads: dict[str, deque[float]] = field(default_factory=dict)
    #: Measured Mode 01 widths and which PIDs may batch (see the ledger).
    mode01_widths: Mode01WidthLedger = field(default_factory=Mode01WidthLedger)

    def count_parse_error(self, _exc: Exception) -> None:
        """``receive_frame``'s ``on_parse_error``: one malformed frame dropped."""
        self.parse_errors += 1

    def record_read(self, channel: str, now: float) -> None:
        """Note one reading of ``channel`` for the trailing-window rate.

        Assumes ``now`` is non-decreasing per channel, which holds because
        this is called as readings arrive. That is what lets eviction be a
        left-pop rather than a scan.

        The same read also feeds the capture-lifetime ledger (C03.2): the
        trailing window here is what the dashboard shows, the ledger is what
        the capture verdict is scored on, and they must see identical events.
        """
        capture_lifetime.observe(channel, now)
        window = self.recent_reads.get(channel)
        if window is None:
            window = deque()
            self.recent_reads[channel] = window
        window.append(now)
        cutoff = now - ACHIEVED_HZ_WINDOW
        while window and window[0] < cutoff:
            window.popleft()

    def achieved_hz(self, now: float) -> dict[str, float]:
        """Per-channel reads/s over the trailing window."""
        cutoff = now - ACHIEVED_HZ_WINDOW
        out: dict[str, float] = {}
        for channel, window in self.recent_reads.items():
            while window and window[0] < cutoff:
                window.popleft()
            if window:
                out[channel] = round(len(window) / ACHIEVED_HZ_WINDOW, 2)
        return out

    def capture_rates(self, now: float, started_at: float) -> tuple[dict[str, ChannelRate], float]:
        """Per-channel sampling rate and stall exposure over the **trailing** window.

        **Trailing, not lifetime.** This looks back at most ACHIEVED_HZ_WINDOW
        seconds and is the live "is it working right now" number the dashboard
        shows. It must never be the capture's verdict: with samples only in the
        last minute of a five-minute capture it reports 60 observed seconds and
        a 0.25 s maximum gap (roadmap C03). ``capture_lifetime`` keeps the
        arm-to-stop statistics that ``capture_quality`` scores.

        Rate comes from the *median inter-sample interval*, not from dividing
        the sample count by wall-clock.  Dividing by wall-clock conflates rate
        with duty cycle, and the two fail in opposite directions: on
        2026-08-06 a capture polling at a solid 14.93 Hz reported 11.25 Hz
        purely because the Mode 22 window opened ~11 s after the arm, and the
        same artifact made a 97.7 Hz broadcast channel read as 52.9 Hz across
        a session that spent half its span with the engine off.  Both read as
        rate shortfalls that did not exist.

        ``max_gap_seconds`` keeps the coverage question answerable without
        contaminating the rate: it brackets the samples with the capture epoch
        and ``now``, so a channel that started late, stalled mid-capture, or
        has gone quiet right now all surface as a gap.
        """
        observed_seconds = min(ACHIEVED_HZ_WINDOW, max(0.0, now - started_at))
        if observed_seconds <= 0:
            return {}, 0.0
        epoch = max(started_at, now - ACHIEVED_HZ_WINDOW)
        out: dict[str, ChannelRate] = {}
        for channel, window in self.recent_reads.items():
            while window and window[0] < now - ACHIEVED_HZ_WINDOW:
                window.popleft()
            samples = [read_at for read_at in window if read_at >= started_at]
            if not samples:
                continue
            bounds = [epoch, *samples, now]
            gaps = [later - earlier for earlier, later in itertools.pairwise(bounds)]
            between = gaps[1:-1]
            median_interval = statistics.median(between) if between else 0.0
            out[channel] = ChannelRate(
                hz=round(1.0 / median_interval, 2) if median_interval > 0 else 0.0,
                samples=len(samples),
                max_gap_seconds=round(max(gaps), 3),
            )
        return out, round(observed_seconds, 3)


@dataclass(frozen=True)
class ObdSchedulerMetrics:
    """Live state of the adaptive OBD poll scheduler."""

    configured_poll_rate_hz: float
    bus_budget_requests_per_second: float
    #: Requests due and not backed off — counted WITHOUT the active capture
    #: profile's eligibility filter, so during a narrowed capture this still
    #: reports the full startup plan. It read as "the capture did not narrow"
    #: on 2026-08-04 when the capture had in fact narrowed correctly; the
    #: session's own parquet is the answer to what was polled.
    active_requests: int
    backed_off_requests: int
    requests_sent: int
    readings_emitted: int
    request_timeouts: int
    negative_responses: int
    bus_errors: int
    response_rate: float | None
    by_tier: dict[str, dict[str, float | int | None]]
    requests: list[dict[str, Any]] = field(default_factory=list)
    #: Per-channel reads/s over the trailing ACHIEVED_HZ_WINDOW seconds — the
    #: "is this capture actually running at the rate I asked for?" number.
    #: Distinct from `batching.channel_reads_per_second_lifetime`, which is
    #: diluted by daemon uptime and stayed near its old value through a real
    #: rate change on 2026-08-04.
    achieved_hz_60s: dict[str, float] = field(default_factory=dict)
    #: Capture-only rate, measured while sampling (see :class:`ChannelRate`).
    #: Empty when normal collection is active.
    achieved_hz_capture: dict[str, float] = field(default_factory=dict)
    #: Wall-clock span of the capture observation (up to 60 s). This is *not*
    #: the denominator of ``achieved_hz_capture`` — it is the window those
    #: samples were drawn from. Pair it with ``capture_max_gap_seconds`` to
    #: judge coverage; using it to re-derive a rate reintroduces the
    #: duty-cycle conflation this split exists to prevent.
    capture_observed_seconds: float | None = None
    #: Capture-only sample count by requested channel.
    capture_samples: dict[str, int] = field(default_factory=dict)
    #: Longest no-sample stretch per channel within the capture epoch.
    capture_max_gap_seconds: dict[str, float] = field(default_factory=dict)
    #: Mode-22 session-window state, ``None`` when no session gates DIDs.
    did_profile: dict[str, Any] | None = None
    #: Multi-PID batching (roadmap/archive/multi-pid-polling.md). ``None`` when
    #: batching is off. ``mean_pids_per_batch`` is the round-trip saving, and
    #: ``achieved_hz`` is what an operator actually needs to know before
    #: trusting a tuning capture — a per-cell verdict is only as good as the
    #: sample count behind it.
    batching: dict[str, Any] | None = None
    #: Capture-round accounting (C02): generation, period, started /
    #: complete / incomplete / skipped counts and the measured in-round
    #: acquisition skew. ``None`` only for a metrics object built without a
    #: round scheduler.
    rounds: dict[str, Any] | None = None
    #: Session-control (UDS 0x10) traffic beside the channel-read remainder
    #: (C04.8), so a hybrid capture's throughput is not padded by entries,
    #: exits and keep-alives.
    session_control: dict[str, Any] | None = None
    #: Support discovery (C06.6): pending items, requests and time spent,
    #: last result per item, and why it is waiting if it is. ``None`` for a
    #: metrics object built without a loop.
    discovery: dict[str, Any] | None = None
    #: Requests wanted right now that the eligibility matrix defers or
    #: refuses, each with its reason (C06.7). Bounded.
    deferred: list[dict[str, Any]] = field(default_factory=list)
    #: The selected, alias-deduplicated plan and its demand in distinct units.
    resolved_plan: dict[str, Any] | None = None
    #: Raw receive accounting for the collector loop (C06.12): every frame the
    #: loop tapped, the frames tapped while a discovery probe was outstanding,
    #: and parse failures. These were counted in :class:`CanObdLoopStats` from
    #: C06 on but never serialized, so the hardware procedure's step 3
    #: ("record ``discovery_frames_seen``") and step 7 ("compare
    #: ``frames_seen`` against ``can0.rx_packets``") could not be run at all on
    #: the active canable path -- where the broadcast collector, and with it
    #: the ``can_receive`` sink, is never registered. ``None`` for a metrics
    #: object built without a loop.
    receive: dict[str, Any] | None = None
    #: Mode-22 window members withheld at a profile start, by reason, since the
    #: loop started (D05.1). A scalar map so the compact ``/status`` can carry
    #: it; the per-start detail is ``did_profile.profile_start``.
    profile_start_withheld: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Every field, plus the one alias.

        Built from ``dataclasses.fields`` rather than a hand list: a
        27-key enumeration here once silently dropped a field (``receive``,
        C06.12), and a field nobody serializes is a counter nobody can read.
        """
        payload = {item.name: getattr(self, item.name) for item in fields(self)}
        # Kept for consumers that predate the name: the count is taken
        # without the capture profile's filter (see ``active_requests``).
        payload["active_requests_unfiltered"] = self.active_requests
        return payload


def _batching_metrics(
    spec: CanObdSpec,
    stats: CanObdLoopStats,
    now: float,
) -> dict[str, Any] | None:
    """Multi-PID batching summary, or ``None`` when batching is off.

    ``achieved_hz`` is the number that decides whether a tuning capture is
    worth analysing: a per-cell timing verdict is only as trustworthy as the
    sample count behind it, and at the default 8 req/s a whole drive yields
    roughly ten samples per rpm x load cell.
    """
    if (
        spec.batch_max_pids <= 1
        and stats.batched_requests == 0
        and spec.did_batch_max_dids <= 1
        and stats.did_batched_requests == 0
    ):
        return None
    mean_pids = (
        round(stats.batched_pids_requested / stats.batched_requests, 2)
        if stats.batched_requests
        else None
    )
    elapsed = None if stats.loop_started_at is None else now - stats.loop_started_at
    achieved_hz = (
        round(stats.batched_pids_requested / elapsed, 2) if elapsed and elapsed > 0 else None
    )
    return {
        "max_pids": spec.batch_max_pids,
        **_mode01_batching_counters(stats),
        "mean_pids_per_batch": mean_pids,
        # Measured-width guard: PIDs whose single-read width contradicts the
        # registry (never batched), desync attribution, and pending re-reads.
        **stats.mode01_widths.metrics(),
        # Lifetime average since the loop started. Explicitly named: read as
        # a current rate it is misleading, and was — on 2026-08-04 it barely
        # moved through a real rate change because hours of history dominated.
        # `obd_scheduler.achieved_hz_60s` is the trailing-window answer.
        "channel_reads_per_second_lifetime": achieved_hz,
        "channel_reads_per_second": achieved_hz,
        # Identity of the counters above: every counter here restarts with
        # the loop, so a capture comparing an arm-time reading against a
        # later one needs to know whether it is the same loop's (a restart
        # followed by regrowth would otherwise read as a small delta).
        "loop_started_monotonic": (
            None if stats.loop_started_at is None else round(stats.loop_started_at, 3)
        ),
        "mode22": {
            "max_dids": spec.did_batch_max_dids,
            "known_response_widths": {
                f"0x{did:04X}": {
                    "bytes": width,
                    "source": spec.did_response_width_sources.get(did),
                }
                for did, width in sorted(spec.did_response_widths.items())
            },
            **_mode22_batching_counters(stats),
            "mean_dids_per_batch": (
                round(stats.did_batched_members_requested / stats.did_batched_requests, 2)
                if stats.did_batched_requests
                else None
            ),
        },
    }


def _mode01_batching_counters(stats: CanObdLoopStats) -> dict[str, int]:
    """The cumulative Mode 01 counters in ``batching`` (R02.11)."""
    return {
        "batched_requests": stats.batched_requests,
        "batched_pids_requested": stats.batched_pids_requested,
        "failures": stats.batch_failures,
        "desyncs": stats.batch_desyncs,
        "size_narrowings": stats.batch_size_narrowings,
        "failures_while_ecu_silent": stats.batch_failures_ecu_silent,
    }


def _mode22_batching_counters(stats: CanObdLoopStats) -> dict[str, int]:
    """The cumulative Mode 22 counters in ``batching.mode22`` (R02.11)."""
    return {
        "batched_requests": stats.did_batched_requests,
        "members_requested": stats.did_batched_members_requested,
        "failures": stats.did_batch_failures,
        "desyncs": stats.did_batch_desyncs,
        "fallbacks": stats.did_batch_size_narrowings,
        "failures_while_ecu_silent": stats.did_batch_failures_ecu_silent,
    }


#: The cumulative counters ``batching`` carries, derived from the producer so
#: the consumer (``capture_quality`` scopes them to a capture) can never ask
#: for a key that is not there: a renamed key used to read as a verified zero.
BATCHING_COUNTERS: tuple[str, ...] = tuple(_mode01_batching_counters(CanObdLoopStats()))


#: The same for ``batching.mode22``.
MODE22_BATCHING_COUNTERS: tuple[str, ...] = tuple(_mode22_batching_counters(CanObdLoopStats()))


def _resolved_polling_plan(
    requests: list[PollRequest],
    spec: CanObdSpec,
    *,
    pid_batch_max: int,
    did_batch_max: int,
    discovery_active: bool,
    view: ProfileView | None = None,
    budget_rps: float | None = None,
) -> dict[str, Any]:
    """Resolve a profile's selection of ``requests`` into transport demand.

    Pure in its arguments (R02.3): ``view`` is the profile whose policy
    applies (the active one when omitted) and ``budget_rps`` the pacing
    budget (the live effective budget when omitted). The live metrics and
    the capture-profile preview both call this; there is no second copy.
    """
    if view is None:
        view = tuning_profile.view()
    if budget_rps is None:
        budget_rps = bus_budget.effective(_bus_budget_requests_per_second(spec))
    required = view.required_tuple()
    priority_names = view.profile.priority_channels
    members: list[PlanMember] = []
    selected_dids = False
    for request in requests:
        if not view.request_eligible(request):
            continue
        profile_channel = view.profile_channel_for(request.channel)
        effective_priority = view.priority_channel(request.channel, request.priority)
        width: int | None = None
        definition = lookup(request.service, request.identifier)
        if definition is not None and definition.byte_length > 0:
            width = definition.byte_length
        if request.in_session_window:
            selected_dids = True
        if request.kind == "did":
            width = spec.did_response_widths.get(request.identifier)
        # `_request_rate_hz` already caps a windowed member at one read per
        # window, so the plan does not re-apply it.
        rate_hz = _request_rate_hz(request, spec, view)
        name = (profile_channel or request.channel).upper()
        members.append(
            PlanMember(
                kind=request.kind,
                service=request.service,
                request_id=request.request_id,
                identifier=request.identifier,
                channel=request.channel,
                profile_channel=profile_channel,
                rate_hz=rate_hz,
                priority=effective_priority,
                required=name in required,
                response_payload_bytes=width,
                decoded_rows_per_read=(
                    2 if request.kind == "pid" and request.identifier == 0x01 else 1
                ),
            )
        )

    session_overhead = 0.0
    if selected_dids and spec.did_session is not None and not view.profile.did_continuous:
        period = view.did_window_period(spec.did_session)
        if period > 0:
            session_overhead = (1 + int(spec.did_session.exit_session is not None)) / period
    plan = ResolvedPollingPlan.build(
        members,
        bus_budget_requests_per_second=budget_rps,
        pid_batch_max=pid_batch_max,
        did_batch_max=did_batch_max,
        session_control_requests_per_second=session_overhead,
        discovery_requests_per_second=(
            min(spec.discovery_budget_requests_per_second, budget_rps) if discovery_active else 0.0
        ),
    ).to_dict()
    plan["priority_source"] = "profile" if priority_names is not None else "collection"
    return plan


def _scheduler_metrics(
    requests: list[PollRequest],
    states: list[PollRequestState],
    spec: CanObdSpec,
    stats: CanObdLoopStats,
    now: float,
    *,
    did_profile: dict[str, Any] | None = None,
    rounds: dict[str, Any] | None = None,
    discovery: dict[str, Any] | None = None,
    deferred: list[dict[str, Any]] | None = None,
    pid_batch_max: int | None = None,
    did_batch_max: int | None = None,
) -> ObdSchedulerMetrics:
    active = _count_active_requests(requests, states, now)
    backed_off = sum(
        1
        for _request, state in zip(requests, states, strict=True)
        if _is_request_backed_off(state, now)
    )
    by_tier: dict[str, dict[str, float | int | None]] = {}
    for tier in ("fast", "medium", "slow", "on_demand"):
        tier_requests = [
            (request, state)
            for request, state in zip(requests, states, strict=True)
            if request.poll_tier == tier
        ]
        sent = sum(state.requests_sent for _request, state in tier_requests)
        responses = sum(state.responses for _request, state in tier_requests)
        timeouts = sum(state.timeouts for _request, state in tier_requests)
        negative_responses = sum(state.negative_responses for _request, state in tier_requests)
        tier_active = sum(
            1 for _request, state in tier_requests if not _is_request_suppressed(state, now)
        )
        tier_backed_off = sum(
            1 for _request, state in tier_requests if _is_request_backed_off(state, now)
        )
        sample_request = tier_requests[0][0] if tier_requests else None
        by_tier[tier] = {
            "configured_rate_hz": (
                round(_request_rate_hz(sample_request, spec), 4)
                if sample_request is not None
                else 0.0
            ),
            "request_count": len(tier_requests),
            "active_count": tier_active,
            "backed_off_count": tier_backed_off,
            "requests_sent": sent,
            "responses": responses,
            "timeouts": timeouts,
            "negative_responses": negative_responses,
            "response_rate": round(responses / sent, 4) if sent else None,
        }

    request_rows: list[dict[str, Any]] = []
    for index, (request, state) in enumerate(zip(requests, states, strict=True)):
        backed = _is_request_backed_off(state, now)
        request_rows.append(
            {
                "kind": request.kind,
                "service": f"0x{request.service:02X}",
                "identifier": f"0x{request.identifier:04X}"
                if request.identifier > 0xFF
                else f"0x{request.identifier:02X}",
                "channel": request.channel,
                # The active profile's own name for this request. DIDs are
                # reported here by identifier (`DID_1209`) while a profile names
                # them semantically (`ECM_MAF_GPS`); carrying the resolved name
                # means a consumer matching against a profile's channel list
                # never has to re-derive the alias map, which is what made the
                # capture contract call four answering DIDs "never answered".
                "profile_channel": tuning_profile.profile_channel_for(request.channel),
                "capture_selected": tuning_profile.request_eligible(index, request),
                "poll_tier": request.poll_tier,
                "priority": tuning_profile.priority_channel(request.channel, request.priority),
                "configured_rate_hz": round(_request_rate_hz(request, spec), 4),
                "requests_sent": state.requests_sent,
                "responses": state.responses,
                "timeouts": state.timeouts,
                "batch_failures": state.batch_failures,
                "accounting_error": _request_accounting_error(state),
                "negative_responses": state.negative_responses,
                "last_negative_response_code": (
                    f"0x{state.last_negative_response_code:02X} "
                    f"({nrc_name(state.last_negative_response_code)})"
                    if state.last_negative_response_code is not None
                    else None
                ),
                # Rate over sends we have evidence about: a send a whole-batch
                # failure swallowed says nothing about this channel, so counting
                # it as a non-response understates a healthy PID (D02.1).
                "response_rate": (
                    round(state.responses / _request_sends_judged(state), 4)
                    if _request_sends_judged(state)
                    else None
                ),
                "missed_responses": state.missed_responses,
                "backed_off": backed,
                "backoff_remaining_seconds": (
                    round(max(0.0, state.suppressed_until - now), 3) if backed else 0.0
                ),
                "last_response_age_seconds": (
                    round(max(0.0, now - state.last_response_at), 3)
                    if state.last_response_at is not None
                    else None
                ),
                "bus_errors": state.bus_errors,
            }
        )

    capture_started_at = tuning_profile.capture_started_monotonic()
    capture_rates: dict[str, ChannelRate] = {}
    capture_observed_seconds: float | None = None
    if capture_started_at is not None:
        capture_rates, capture_observed_seconds = stats.capture_rates(now, capture_started_at)
    capture_hz = {channel: rate.hz for channel, rate in capture_rates.items()}
    capture_samples = {channel: rate.samples for channel, rate in capture_rates.items()}
    capture_max_gaps = {channel: rate.max_gap_seconds for channel, rate in capture_rates.items()}
    return ObdSchedulerMetrics(
        configured_poll_rate_hz=float(spec.poll_rate_hz),
        # The *effective* rate, not the configured one. The collector paces at
        # `bus_budget.effective(...)`, so reporting the config value here made
        # /status contradict the bus whenever an override was armed: it read
        # 8.0 while requests actually went out at 25/s. That defeats the one
        # property the override is documented to have — that an unexpectedly
        # high achieved rate is distinguishable from a fault.
        bus_budget_requests_per_second=round(
            bus_budget.effective(_bus_budget_requests_per_second(spec)), 4
        ),
        active_requests=active,
        backed_off_requests=backed_off,
        requests_sent=stats.requests_sent,
        readings_emitted=stats.readings_emitted,
        achieved_hz_60s=stats.achieved_hz(now),
        achieved_hz_capture=capture_hz,
        capture_observed_seconds=capture_observed_seconds,
        capture_samples=capture_samples,
        capture_max_gap_seconds=capture_max_gaps,
        request_timeouts=stats.request_timeouts,
        negative_responses=stats.negative_responses,
        bus_errors=stats.bus_errors,
        response_rate=(
            round(stats.readings_emitted / stats.requests_sent, 4) if stats.requests_sent else None
        ),
        by_tier=by_tier,
        requests=request_rows,
        did_profile=did_profile,
        batching=_batching_metrics(spec, stats, now),
        rounds=rounds,
        session_control=_session_control_metrics(stats),
        receive=_receive_metrics(stats),
        discovery=discovery,
        deferred=list(deferred or []),
        profile_start_withheld=dict(
            ((did_profile or {}).get("profile_start") or {}).get("withheld_total") or {}
        ),
        resolved_plan=_resolved_polling_plan(
            requests,
            spec,
            pid_batch_max=spec.batch_max_pids if pid_batch_max is None else pid_batch_max,
            did_batch_max=spec.did_batch_max_dids if did_batch_max is None else did_batch_max,
            discovery_active=bool((discovery or {}).get("pending")),
        ),
    )


def _receive_metrics(stats: CanObdLoopStats) -> dict[str, Any]:
    """Frames the collector loop tapped, for upstream-continuity comparison.

    ``frames_seen`` is compared against the kernel's ``can0.rx_packets`` over
    the same window, with the ``rx_dropped`` / ``rx_over_errors`` deltas: a
    received count alone never proves nothing was lost upstream, which is the
    whole point of step 7 in ``docs/diagnostic-discovery.md``.
    ``discovery_frames_seen`` is the subset tapped while a support probe was
    outstanding -- the frames the old synchronous probe path silently ate.
    """
    return {
        "frames_seen": stats.frames_seen,
        "parse_errors": stats.parse_errors,
        "discovery_frames_seen": stats.discovery_frames_seen,
        # D03.18: a lost ISO-TP continuation ends at N_Cr, not the request
        # timeout; these say how often, and how often re-sent FC saved it.
        "continuation_losses": stats.continuation_losses,
        "flow_control_resends": stats.flow_control_resends,
        "flow_control_rescues": stats.flow_control_rescues,
    }


def _session_control_metrics(stats: CanObdLoopStats) -> dict[str, Any]:
    channel_requests = max(
        0,
        stats.requests_sent
        - stats.session_controls_sent
        - stats.discovery_requests_sent
        - stats.identification_requests_sent,
    )
    return {
        "sent": stats.session_controls_sent,
        "responses": stats.session_control_responses,
        "timeouts": stats.session_control_timeouts,
        "negative_responses": stats.session_control_negative_responses,
        "seconds": round(stats.session_control_seconds, 4),
        "channel_requests_sent": channel_requests,
        "channel_response_rate": (
            round(stats.readings_emitted / channel_requests, 4) if channel_requests else None
        ),
    }
