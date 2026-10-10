"""The request plan: ``PollRequest``/``PollRequestState``, building the plan from
a spec, Mode 06 and discovered-support installs, and the measured Mode 01 widths.

Part of the ``obd_scheduler`` package (R02.10).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from src.collection.can_obd_client import DEFAULT_REQUEST_ID, PID_DECODERS
from src.collection.isotp import MEMORY_READ_SERVICE
from src.collection.obd_registry import PollTier, lookup
from src.collection.obd_scheduler.spec import (
    AUTO_PIDS,
    DTC_POLL_CHANNELS,
    FREEZE_FRAME_CHANNEL_PREFIX,
    SERVICE_POLL_CHANNELS,
    SESSION_WINDOW_KINDS,
    CanObdSpec,
    _PidDiscoveryClient,
    parse_did_token,
    parse_pid_token,
)
from src.collection.request_eligibility import RequestKind
from src.decode.dids import MEMORY_SOURCE, plan_memory_reads, refuse_side_effect_dids

if TYPE_CHECKING:
    from src.collection.custom_pids import CustomPid
    from src.decode.dids import MemoryRead

log = logging.getLogger(__name__)


#: Desyncs the batch walk may attribute to one PID whose single-read width
#: agrees with the registry before that PID stops batching anyway. A width
#: contradiction excludes a PID at once; this bounds the case a single read
#: cannot explain (an ECU that answers a PID alone but mangles it in a batch),
#: so no attribution can keep ``batch_desyncs`` growing forever.
MODE01_DESYNC_BLAME_LIMIT = 3


def mode01_registry_width(pid: int) -> int | None:
    """The registry's Mode 01 payload width for ``pid``, or ``None``."""
    definition = lookup(0x01, pid)
    if definition is None or definition.byte_length <= 0:
        return None
    return definition.byte_length


@dataclass
class Mode01WidthLedger:
    """Measured Mode 01 payload widths, and which PIDs may therefore batch.

    The Mode 01 counterpart of Mode 22's ``did_response_widths`` rule (only a
    measured width batches), adapted to what Mode 01 has: a registry width
    checked against J1979 (``tests/test_mode01_widths.py``) that a particular
    vehicle can still contradict — a 2-bank ECU sending one byte for 0x55-0x58
    where J1979 gives two. A batched walk slices by width, so one wrong width
    desyncs every batch holding that PID and loses every member after it.

    **Policy: the registry width is trusted until contradicted.** A PID with no
    single read yet may batch, because the registry is pinned to the standard
    and requiring a measurement first would cost every PID a single request at
    each loop start for a mismatch the J1979 test already makes rare. A
    contradiction is caught either way: every single-PID response is measured
    here (its payload is bounded by the ISO-TP length, so it *is* the width),
    and a desynced batch sends its unparsed members — and the PID blamed for the
    break — back to a single read on their next slot, which measures them.

    A PID whose measured width disagrees with the registry never batches again
    for this loop (the state lives in ``CanObdLoopStats`` and restarts with it;
    nothing is persisted — a restart re-measures in one single read).
    """

    #: pid -> payload width of the most recent single-PID response.
    observed: dict[int, int] = field(default_factory=dict)
    #: pid -> (registry width, observed width); sticky once recorded.
    mismatches: dict[int, tuple[int, int]] = field(default_factory=dict)
    #: PIDs whose next slot is a single read (after a batch desync).
    remeasure: set[int] = field(default_factory=set)
    #: pid -> desyncs the walk attributed to it.
    blamed: dict[int, int] = field(default_factory=dict)
    #: Members sent back to a single read by a desync (lifetime count).
    remeasures_requested: int = 0

    def batch_width(self, pid: int) -> int | None:
        """Width to slice a batched response by; ``None`` refuses the PID."""
        if pid in self.mismatches:
            return None
        return mode01_registry_width(pid)

    def batchable(self, pid: int) -> bool:
        """Whether ``pid`` may ride (or lead) a multi-PID batch right now."""
        return (
            self.batch_width(pid) is not None
            and pid not in self.remeasure
            and self.blamed.get(pid, 0) < MODE01_DESYNC_BLAME_LIMIT
        )

    def observe_single(self, pid: int, width: int) -> None:
        """Record the payload width of a single-PID Mode 01 response."""
        self.observed[pid] = width
        self.remeasure.discard(pid)
        registry = mode01_registry_width(pid)
        if registry is None or width == registry or pid in self.mismatches:
            return
        self.mismatches[pid] = (registry, width)
        log.warning(
            "Mode 01 PID 0x%02X answers %d byte(s) but the registry says %d; "
            "polling it single-PID only for this session (a batch would desync on it)",
            pid,
            width,
            registry,
            extra={"pid": f"0x{pid:02X}", "registry_width": registry, "observed_width": width},
        )

    def on_desync(self, unparsed: Iterable[int], suspect: int | None) -> None:
        """A batch walk broke: re-measure what it could not parse, and blame."""
        pending = set(unparsed)
        if suspect is not None:
            pending.add(suspect)
            blamed = self.blamed.get(suspect, 0) + 1
            self.blamed[suspect] = blamed
            if blamed == MODE01_DESYNC_BLAME_LIMIT and suspect not in self.mismatches:
                log.warning(
                    "Mode 01 PID 0x%02X broke %d batched walks without its single-read "
                    "width contradicting the registry; polling it single-PID only for "
                    "this session",
                    suspect,
                    blamed,
                    extra={"pid": f"0x{suspect:02X}"},
                )
        self.remeasures_requested += len(pending - self.remeasure)
        self.remeasure |= pending

    def metrics(self) -> dict[str, Any]:
        """The ``/status`` batching view of the ledger."""
        return {
            "width_mismatches": {
                f"0x{pid:02X}": {"registry": registry, "observed": observed}
                for pid, (registry, observed) in sorted(self.mismatches.items())
            },
            "desync_blame": {f"0x{pid:02X}": n for pid, n in sorted(self.blamed.items())},
            "desync_excluded": [
                f"0x{pid:02X}"
                for pid, n in sorted(self.blamed.items())
                if n >= MODE01_DESYNC_BLAME_LIMIT
            ],
            "remeasure_pending": [f"0x{pid:02X}" for pid in sorted(self.remeasure)],
            "desync_remeasures": self.remeasures_requested,
            "measured_widths": {
                f"0x{pid:02X}": width for pid, width in sorted(self.observed.items())
            },
        }


@dataclass(frozen=True)
class PollRequest:
    kind: RequestKind
    service: int
    identifier: int
    request_id: int
    channel: str
    poll_tier: PollTier = "medium"
    auto_probe: bool = False
    fast_probe: bool = False
    #: First refusal on a due request slot (``CanObdSpec.priority_channels``).
    priority: bool = False
    #: Set only for ``kind="custom"`` — the definition ``_response_to_reading``
    #: decodes the response through (formula, unit). ``None`` for every other
    #: kind, which all decode via the static PID_DECODERS/DTC tables instead.
    custom_pid: CustomPid | None = None
    #: Set only for ``kind="memory"`` — the address, the read length and
    #: every channel decoded from the one reply. ``identifier`` is unused
    #: for these: a service-0x23 request has no identifier byte and its
    #: response echoes none, so there is nothing for the assembler to match.
    memory_read: MemoryRead | None = None

    @property
    def in_session_window(self) -> bool:
        """Served inside the Mode-22 session window (R02.2).

        The one window-semantics predicate: the window controller, grid
        anchoring, the cycle plan and the plan's session overhead all ask
        this, so a memory read (same session as Mode 22 on this ECM) cannot
        be a window member at one site and a Mode 01-side request at another.
        Mode 22 *batching* is a different question and still tests the kind.
        """
        return self.kind in SESSION_WINDOW_KINDS


@dataclass
class PollRequestState:
    missed_responses: int = 0
    suppressed_until: float = 0.0
    next_due_at: float = 0.0
    last_requested_at: float | None = None
    last_response_at: float | None = None
    requests_sent: int = 0
    responses: int = 0
    timeouts: int = 0
    #: Sends that a *whole-batch* failure swallowed (D02.1). `_issue_batched_request`
    #: credits and penalises nobody when the batch itself timed out, was refused or
    #: desynced — we have no evidence about any individual member — but the send was
    #: already counted. Without this bucket `requests_sent - responses - timeouts` is
    #: an unnamed remainder, which read as "60 sent, 0 answered, 6 timeouts" on
    #: 2026-09-10 and made a channel's `response_rate` look like a channel problem.
    #: The invariant `requests_sent == responses + timeouts + batch_failures` is
    #: asserted by `_request_accounting_error`; keep it true.
    batch_failures: int = 0
    #: Formal ECU refusals for this request — see `CanObdLoopStats.negative_responses`.
    negative_responses: int = 0
    last_negative_response_code: int | None = None
    backoffs: int = 0
    bus_errors: int = 0
    #: When the miss that set ``suppressed_until`` happened. A back-off is a
    #: function of (misses, the policy in force), so a profile change can
    #: re-derive it under the new policy instead of serving out the old one
    #: (D05.1, :func:`replan_for_generation`).
    missed_at: float | None = None


#: Eligibility predicate the DID-window controller injects into request
#: selection: ``(request_index, request) -> bool``.
RequestFilter = Callable[[int, "PollRequest"], bool]


def build_poll_requests(client: _PidDiscoveryClient | None, spec: CanObdSpec) -> list[PollRequest]:
    """Build the round-robin request plan from config and ECU support.

    With a ``client`` (standalone callers: scripts, probe tools) support is
    discovered synchronously through it. With ``None`` — what the collector
    loop passes (C06.4) — nothing is sent: ``auto`` starts as the whole
    decoded catalogue on the fast probe timeout and Mode 06 ``auto`` adds no
    requests yet; the loop installs discovery results as its budgeted probes
    answer (:func:`apply_mode01_support`, :func:`mode06_requests`).
    """
    tokens = spec.pids or (AUTO_PIDS,)
    auto = False
    explicit_pids: list[int] = []
    for token in tokens:
        parsed = parse_pid_token(token)
        if parsed is None:
            auto = True
        else:
            explicit_pids.append(parsed)

    pids: list[int] = []
    auto_probe_by_pid: dict[int, bool] = {}
    fast_probe_by_pid: dict[int, bool] = {}

    def add_pid(pid: int, *, auto_probe: bool, fast_probe: bool = False) -> None:
        if pid not in pids:
            pids.append(pid)
            fast_probe_by_pid[pid] = fast_probe
        # Explicit config wins over auto management for the same PID.
        auto_probe_by_pid[pid] = auto_probe_by_pid.get(pid, True) and auto_probe
        fast_probe_by_pid[pid] = fast_probe_by_pid.get(pid, False) and auto_probe

    if auto:
        supported = client.query_supported_mode_01_pids() if client is not None else ()
        for pid in supported:
            if pid in PID_DECODERS:
                add_pid(pid, auto_probe=True, fast_probe=False)
        # Some ECUs/adapters report sparse or incomplete supported-PID bitmasks
        # even though standard Mode-01 PIDs answer when queried directly.
        for pid in sorted(PID_DECODERS):
            add_pid(pid, auto_probe=True, fast_probe=True)

    for pid in explicit_pids:
        add_pid(pid, auto_probe=False, fast_probe=False)

    priority_channels = {channel.strip().upper() for channel in spec.priority_channels}

    requests = []
    for pid in pids:
        channel, poll_tier = _pid_channel_and_tier(pid)
        requests.append(
            PollRequest(
                kind="pid",
                service=0x01,
                identifier=pid,
                request_id=DEFAULT_REQUEST_ID,
                channel=channel,
                poll_tier=poll_tier,
                auto_probe=auto_probe_by_pid[pid],
                fast_probe=fast_probe_by_pid[pid],
                priority=channel.upper() in priority_channels,
            )
        )
    # Deduplicated by resolved identifier, the way `add_pid` already does for
    # Mode 01. Names and hex tokens both reach the same DID, and the catalogue
    # carries aliases — `ECM_MAF_GPS` and its old `ECM_BASE_FUEL_SCHEDULE_
    # CANDIDATE` name are one register. Without this the scheduler issued two
    # requests for it: the 2026-08-10 all-DID capture polled 0x1209 at 29.4 Hz
    # while every other channel got 14.7, spending a seventh of the round on a
    # duplicate. The channel name is derived from the identifier, so the second
    # request also wrote rows indistinguishable from the first.
    dids = tuple(dict.fromkeys(parse_did_token(d) for d in spec.dids))
    refuse_side_effect_dids(dids, where="the Mode 22 poll plan")
    for did in dids:
        requests.append(
            PollRequest(
                kind="did",
                service=0x22,
                identifier=did,
                request_id=spec.did_request_id,
                channel=f"DID_{did:04X}",
                poll_tier="medium",
            )
        )
    for custom in spec.custom_pids:
        # Mode 0x22 custom PIDs default to the DID request id (matches the
        # `did` branch above); 0x01/0x09 default to the Mode-01 broadcast id.
        # An explicit `header` on the definition always wins.
        default_request_id = spec.did_request_id if custom.mode == 0x22 else DEFAULT_REQUEST_ID
        requests.append(
            PollRequest(
                kind="custom",
                service=custom.mode,
                identifier=custom.pid,
                request_id=custom.header if custom.header is not None else default_request_id,
                channel=f"custom:{custom.name}",
                poll_tier=custom.poll_tier,
                custom_pid=custom,
                # Matched on the bare name, not the ``custom:`` channel prefix,
                # so a vehicle that sources rpm/speed from a custom PID gets the
                # same lane as the standard Mode 01 one.
                priority=custom.name.strip().upper() in priority_channels,
            )
        )
    for memory_read in plan_memory_reads(spec.memory):
        names = [c.name for c in memory_read.channels]
        requests.append(
            PollRequest(
                kind="memory",
                service=MEMORY_READ_SERVICE,
                # No identifier on the wire: `_query_with_tap` builds the
                # address/length body from `memory_read` instead, and the
                # assembler is given None because 0x23 echoes nothing.
                identifier=0,
                # Same endpoint and same gating session as Mode 22.
                request_id=spec.did_request_id,
                # A window carries several channels, so the request channel
                # names the window; each decoded reading gets its own
                # `mem:<NAME>` channel from the one reply.
                channel=f"{MEMORY_SOURCE}:{memory_read.name}",
                poll_tier="medium",
                memory_read=memory_read,
                priority=any(name.strip().upper() in priority_channels for name in names),
            )
        )
    if spec.dtc_poll_interval_seconds > 0:
        # Task 1.3: stored/pending/permanent DTCs on their own very-slow
        # cadence. No vehicle_state gating here — the round-robin scheduler
        # already backs off any request the ECU stops answering (the same
        # "OBD gone stale" signal `classify_vehicle_state` uses), so a dead
        # bus just accrues timeouts like any other request instead of
        # hammering it, and a live one (KOEO included — DTCs read fine with
        # the engine off) answers on schedule.
        for service, channel in DTC_POLL_CHANNELS.items():
            requests.append(
                PollRequest(
                    kind="dtc",
                    service=service,
                    identifier=0,
                    request_id=DEFAULT_REQUEST_ID,
                    channel=channel,
                    poll_tier="dtc",
                )
            )
    if spec.service_poll_interval_seconds > 0:
        # Task 3.1: Mode 06 on-board monitor results (misfire counts, margins)
        # on the same "occasional service poll" cadence model as the DTC poll
        # above — no vehicle_state gating, the round-robin scheduler already
        # backs off an ECU that stops answering.
        if spec.service_mids != "auto":
            mode06_mids: tuple[int, ...] = spec.service_mids
        elif client is None:
            mode06_mids = ()
        else:
            discover_mode06 = getattr(client, "query_supported_mode_06_mids", None)
            mode06_mids = tuple(discover_mode06()) if discover_mode06 is not None else (0x00,)
        requests.extend(mode06_requests(mode06_mids))
    if spec.freeze_frame_trigger is not None:
        # Task 3.3: dormant ("on_demand" -> rate 0) until FreezeFrameTrigger
        # fires; run_can_obd_loop un-suppresses these for one shot each time.
        for pid in spec.freeze_frame_pids:
            if pid not in PID_DECODERS:
                continue
            requests.append(
                PollRequest(
                    kind="freeze_frame",
                    service=0x02,
                    identifier=pid,
                    request_id=DEFAULT_REQUEST_ID,
                    channel=f"{FREEZE_FRAME_CHANNEL_PREFIX}{PID_DECODERS[pid][0]}",
                    poll_tier="on_demand",
                )
            )
    return requests


def mode06_requests(mids: Iterable[int]) -> list[PollRequest]:
    """Mode 06 monitor-result requests, one per MID, on the service-poll cadence.

    Task 3.1: the same "occasional service poll" cadence model as the DTC
    poll — no vehicle_state gating, the round-robin scheduler already backs
    off an ECU that stops answering. The MID stays on the wire as the
    request identifier (``06 <mid>``) and the response's responder id rides
    every reading, so per-ECU provenance is never lost (C06.11).
    """
    return [
        PollRequest(
            kind="service",
            service=service,
            identifier=mid,
            request_id=DEFAULT_REQUEST_ID,
            channel=channel,
            poll_tier="dtc",
        )
        for service, channel in SERVICE_POLL_CHANNELS.items()
        for mid in mids
    ]


def apply_mode01_support(
    requests: list[PollRequest], supported: Iterable[int]
) -> list[PollRequest]:
    """A new plan with the discovered Mode 01 PIDs off the fast probe timeout.

    This is the whole effect Mode 01 support discovery has on the plan: an
    auto-probed PID the ECU claims gets the full request timeout on its
    first miss instead of ``AUTO_PROBE_TIMEOUT_SECONDS``. The sparse-bitmap
    fallback (every decoded PID is probed regardless) is unchanged.
    """
    claimed = set(supported)
    return [
        replace(request, fast_probe=False)
        if request.kind == "pid" and request.auto_probe and request.identifier in claimed
        else request
        for request in requests
    ]


def _request_sends_judged(state: PollRequestState) -> int:
    """Sends this request has evidence about: answered or missed on its own.

    A send swallowed by a whole-batch failure is excluded — the batch failed,
    not the member, and `_issue_batched_request` deliberately credits and
    penalises nobody in that case (D02.1).
    """
    return state.responses + state.timeouts


def _request_accounting_error(state: PollRequestState) -> int:
    """`requests_sent` minus everything that claims to explain it.

    Non-zero means a send went out and no counter owns it — the defect D02.1
    exists to make impossible. Surfaced per request in the status payload
    rather than asserted, because a scheduler must not crash a live collector
    over a counter; a non-zero value here is the alarm.
    """
    return state.requests_sent - (state.responses + state.timeouts + state.batch_failures)


def _pid_channel_and_tier(pid: int) -> tuple[str, PollTier]:
    """Channel name + poll tier for a Mode 01 PID, both from ``MODE01_REGISTRY``.

    Task 1.1: previously the channel name came from ``PID_DECODERS`` while the
    tier came from ``MODE01_REGISTRY`` (via ``lookup``) — two different
    tables keyed by the same pid, with nothing tying them together. Both now
    come from a single ``lookup()`` call so they can never split. Falls back
    to ``PID_DECODERS``'s name / a "medium" tier only if the registry somehow
    lacks the pid (`assert_pid_tables_consistent` makes that unreachable for
    any pid `build_poll_requests` can produce, since it only builds requests
    for pids already in `PID_DECODERS`).
    """
    definition = lookup(0x01, pid)
    if definition is None:
        return PID_DECODERS[pid][0], "medium"
    return definition.channel, definition.poll_tier


#: The names these had before R02.10 made them public.
_PollRequest = PollRequest
_PollRequestState = PollRequestState
