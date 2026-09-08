"""Adaptive OBD request scheduling for the Canable-native collector."""

from __future__ import annotations

import itertools
import logging
import math
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from src.collection import bus_budget, capture_lifetime
from src.collection.can_obd_client import DEFAULT_REQUEST_ID, PID_DECODERS
from src.collection.hybrid_cycle import (
    CycleAccounting,
    CyclePlan,
    ServiceKind,
    next_wake_at,
)
from src.collection.isotp import PERMANENT_REFUSAL_NRCS, nrc_name
from src.collection.obd_registry import PollTier, lookup
from src.collection.request_eligibility import Eligibility, SessionPhase, evaluate
from src.config import DEFAULT_PRIORITY_CHANNELS, CanBusConfig, CollectionConfig

if TYPE_CHECKING:
    from src.collection.custom_pids import CustomPid
    from src.collection.discovery import SupportProbe
    from src.decode.dids import DidAccess, DidMap

log = logging.getLogger(__name__)

AUTO_PIDS = "auto"
DEFAULT_DID_REQUEST_ID = 0x7E0
AUTO_MISS_BACKOFF_SECONDS = 30.0
#: Consecutive *permanent* refusals before the Mode-22 fast exit is disabled.
#: Two, not one: the ECM that motivated this answered 139 of 140 exits
#: correctly, and one stray frame must never cost the rest of the drive.
FAST_EXIT_REFUSALS_BEFORE_DISABLE = 2
#: Disabling the fast exit is always provisional. Re-probe after this long,
#: doubling to the cap, so an ECU that refused while busy recovers on its own
#: instead of needing a daemon restart mid-drive.
FAST_EXIT_RETRY_MIN_SECONDS = 30.0
FAST_EXIT_RETRY_MAX_SECONDS = 300.0
#: Clear air handed to Mode 01 when a lapse ends with the next window already
#: overdue. Applied *only* in that case, so the fast-exit path — where the
#: window is still in the future when the exit grace ends — keeps its full
#: DID rate.
MODE01_MIN_SHARE_SECONDS = 0.5
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
BUS_BUDGET_MULTIPLIER = 8.0
MIN_BUS_BUDGET_REQUESTS_PER_SECOND = 2.0
# ``DEFAULT_PRIORITY_CHANNELS`` (imported from src.config, re-exported below):
# the channels that get first refusal on a due request slot, see
# ``_select_next_due_request``. They are the signals every entry in
# ``preprocessing.operating_modes`` gates on, and the resampler holds them to
# the *fast*-channel staleness ceiling (``interval x max_gap_factor``, 2 s on a
# 1 Hz grid) rather than the generous slow-channel one — so when the bus is
# oversubscribed and they degrade along with everything else, they go NaN on
# most rows, every rpm/speed-gated mode stops matching, and the label falls
# through to whichever mode happens not to constrain the missing signal.
# Reserving their configured rate costs the other tiers a share of a contended
# bus; mislabeling every operating mode costs more. Vehicle-agnostic: these are
# standard Mode 01 PIDs, not Xterra-isms.
#
# SPEED left the lane on 2026-08-07. The lane exists to keep a mode-gating
# signal inside the 2 s fast-channel ceiling, and it was not succeeding at
# that for either channel: on 2026-08-06T162657 RPM and SPEED still ran a
# 6.6 s and 32 s p99 gap respectively, together consuming 20.3% of all Mode 01
# requests (2592 + 2503 of 25146). The segmenter now coalesces speed onto
# gps:speed and rpm onto decoded:ECM_RPM, both of which are already on the
# wire and cost no request slot, so buying SPEED a lane bought nothing the
# segmenter could not get for free. RPM keeps its lane because behavior
# models still consume `obd:RPM`. THROTTLE_POS has no broadcast/GPS fallback,
# feeds operating-mode/event classification, and the 75-request AUTO plan was
# measured at only 0.20 Hz for it on 2026-08-07 despite a configured 1 Hz.
# Giving those two due requests first refusal consumes at most 2 of the 8
# default slots per second; neither can starve the remaining plan while it is
# not due.
#: Default cadence for the Mode 03/07/0A DTC poll (Task 1.3). DTCs are a
#: minutes-scale signal (nothing polls faster than the ECM's own confirm
#: window), so ~5 min keeps it off the fast bus budget entirely.
DEFAULT_DTC_POLL_INTERVAL_SECONDS = 300.0
#: Service (mode) -> channel name for the periodic DTC poll requests.
DTC_POLL_CHANNELS: dict[int, str] = {
    0x03: "DTC_STORED",
    0x07: "DTC_PENDING",
    0x0A: "DTC_PERMANENT",
}
#: Cadence for the Mode 06 on-board monitor poll (Task 3.1) — its own knob,
#: same "occasional service poll, not a real-time PID" cadence model as the
#: DTC poll above (misfire counts/monitor margins change on a minutes scale,
#: not seconds), deliberately not folded into ``dtc_poll_interval_seconds``
#: so the two service polls can be tuned/disabled independently.
DEFAULT_SERVICE_POLL_INTERVAL_SECONDS = DEFAULT_DTC_POLL_INTERVAL_SECONDS
#: Service (mode) -> channel name for the periodic Mode 06 poll.
SERVICE_POLL_CHANNELS: dict[int, str] = {
    0x06: "MONITOR_RESULTS",
}
#: Mode 01 PIDs read via Mode 02 (freeze frame, Task 3.3) when a *stored*
#: DTC first appears — reuses the identical Mode 01 formula per
#: ``obd_registry.lookup(0x02, pid)``'s Mode-01 remap. A small, curated set
#: (not "every configured PID") since a freeze-frame read is triggered by a
#: rare event (a new stored code), not a cadence: RPM, speed, coolant temp,
#: calculated load, throttle position — the classic "what was the engine
#: doing" snapshot.
FREEZE_FRAME_PIDS: tuple[int, ...] = (0x0C, 0x0D, 0x05, 0x04, 0x11)
#: Channel prefix for one freeze-frame PID reading, e.g. ``FREEZE_FRAME_RPM``.
FREEZE_FRAME_CHANNEL_PREFIX = "FREEZE_FRAME_"


class FreezeFrameTrigger:
    """Thread-safe one-shot flag bridging the DTC sink (asyncio) and the
    collector loop (its own thread / ``asyncio.to_thread``) (Task 3.3).

    ``DtcState`` calls :meth:`request` when a *stored* DTC first appears;
    ``run_can_obd_loop`` calls :meth:`take` once per tick and, on a hit,
    un-suppresses every ``kind="freeze_frame"`` request so the normal
    round-robin scheduler reads them once (they go back to ``on_demand``
    dormancy immediately after — see ``_request_rate_hz``).
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def take(self) -> bool:
        """Atomically consume a pending request; ``False`` if none pending."""
        if self._event.is_set():
            self._event.clear()
            return True
        return False


class ForcePollTrigger:
    """Thread-safe bridge for the ``trigger_poll`` tool: force specific
    channels due on the collector's *very next* tick instead of waiting out
    their tier cadence (or the DTC/Mode 06 poll's 5-minute interval).

    Same one-shot-flag shape as :class:`FreezeFrameTrigger`, generalized to a
    caller-chosen channel set instead of the fixed freeze-frame list —
    ``request()`` is called from the control route (asyncio), ``take()``
    once per tick from ``run_can_obd_loop`` (its own thread). Channel names
    are matched case-insensitively against ``_PollRequest.channel``
    (``RPM``, ``DTC_STORED``, ``MONITOR_RESULTS``, ``DID_1101``,
    ``custom:injector_pw``, ...) — see ``GET /status``'s
    ``obd_scheduler.requests[].channel`` for what's currently active. An
    unknown name is simply never matched; nothing tells the caller that, by
    design symmetry with how a backed-off or unsupported channel already
    behaves in this scheduler.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: set[str] = set()

    def request(self, channels: Iterable[str]) -> None:
        normalized = {c.strip().upper() for c in channels if c.strip()}
        with self._lock:
            self._pending |= normalized

    def take(self) -> frozenset[str]:
        """Atomically consume and return the pending channel set (uppercased);
        empty when nothing is pending."""
        with self._lock:
            pending, self._pending = self._pending, set()
        return frozenset(pending)


#: Trailing window for the per-channel achieved rate, in seconds. Long enough
#: that a slow tier still lands a sample, short enough that arming a capture
#: and re-reading /status a moment later shows the new rate rather than an
#: average dominated by however long the daemon has been up.
ACHIEVED_HZ_WINDOW = 60.0

TIER_RATE_FACTORS: dict[PollTier, float] = {
    "fast": 1.0,
    "medium": 1.0 / 3.0,
    "slow": 1.0 / 15.0,
    "on_demand": 0.0,
}

_PID_IDS_BY_NAME: dict[str, int] = {name: pid for pid, (name, _decoder) in PID_DECODERS.items()}

# DID name→identifier lives in data/dids.yaml (not a static table like PIDs), so
# resolve it lazily and cache. Path is repo-root-relative so cwd doesn't matter.
_DIDS_YAML_PATH = Path(__file__).resolve().parents[2] / "data" / "dids.yaml"


@lru_cache(maxsize=1)
def _load_did_map() -> DidMap | None:
    """Parsed data/dids.yaml, or ``None`` when absent/unparseable.

    ``None`` keeps the collector running without DID metadata — callers fall
    back to hex-only token parsing and session-less polling.
    """
    from src.decode.dids import load_did_map

    if not _DIDS_YAML_PATH.exists():
        return None
    try:
        return load_did_map(_DIDS_YAML_PATH)
    except Exception:  # noqa: BLE001 — bad YAML shouldn't crash the collector
        return None


@lru_cache(maxsize=1)
def _did_ids_by_name() -> dict[str, int]:
    """Symbolic DID name → identifier from data/dids.yaml.

    Empty when the file is absent or unparseable — callers then accept only
    4-char hex, matching the pre-resolution behavior. Mirrors how
    ``parse_pid_token`` resolves PID names against ``_PID_IDS_BY_NAME``.
    """
    did_map = _load_did_map()
    if did_map is None:
        return {}
    # Aliases resolve too, so renaming a DID cannot break a saved capture
    # profile that still references its old name. Current names are inserted
    # last so they win any collision with a stale alias.
    resolved: dict[str, int] = {}
    for entry in did_map.dids:
        for alias in entry.aliases:
            resolved[alias] = entry.did
    for entry in did_map.dids:
        resolved[entry.name] = entry.did
    return resolved


def known_did_names() -> tuple[str, ...]:
    """Return the vehicle DID inventory available to capture profiles."""
    return tuple(_did_ids_by_name())


def did_access() -> DidAccess | None:
    """The access block of data/dids.yaml (gating session, keep-alive)."""
    did_map = _load_did_map()
    return None if did_map is None else did_map.access


class _PidDiscoveryClient(Protocol):
    """Synchronous discovery, for standalone ``build_poll_requests`` callers."""

    def query_supported_mode_01_pids(self) -> tuple[int, ...]: ...

    def query_supported_mode_06_mids(self) -> tuple[int, ...]: ...


class _DiscoveryClient(Protocol):
    """One support-group probe at a time — what the collector loop drives (C06.4)."""

    def probe_support(
        self, service: int, group: int, *, should_stop: Callable[[], bool] | None = None
    ) -> SupportProbe: ...


@dataclass(frozen=True)
class DidSessionSpec:
    """Mode-22 session-window policy for ECUs that gate DID reads.

    Some ECMs (the 2008 Nissan ECM among them — see data/dids.yaml access
    notes) refuse service 0x22 outside a non-default UDS session entered via
    ``10 <session>``, and stop answering standard Mode 01 while that session
    is held. The two are mutually exclusive on the bus, so DIDs are read in
    short scheduled *windows*: enter the session, read every active DID once
    (re-sending ``10 <session>`` every ``keepalive_seconds`` — TesterPresent
    is rejected on this ECM), then go silent for ``lapse_seconds`` so the
    session expires and Mode 01 answers again.
    """

    session: int
    keepalive_seconds: float = 2.0
    #: **End-to-start** gap: seconds from one window closing to the next
    #: opening (the historic meaning, unchanged). Sweep duration, the exit,
    #: its grace and Mode 01 all sit *outside* this number, so it is not a
    #: cycle period — a 0.25 s interval measured as a 0.95 s cycle on the
    #: fake bus (docs/hybrid-scheduling.md). Ignored when ``cycle_seconds``
    #: (or the profile's ``did_cycle_seconds``) is set.
    window_interval_seconds: float = 60.0
    window_max_seconds: float = 10.0
    lapse_seconds: float = 5.0
    #: Sub-function that hands the bus back to Mode 01 immediately (UDS
    #: default session). ``None`` = no verified fast exit on this vehicle;
    #: wait out ``lapse_seconds`` instead.
    exit_session: int | None = None
    #: Settling time after a successful fast exit, in place of the lapse.
    #: Vehicle-scoped: resolved from the dids.yaml access block when it
    #: states one (the Xterra measured recovery on the *next* request).
    exit_grace_seconds: float = 0.05
    #: **Start-to-start** period of one Mode 01 + Mode 22 cycle (C04.3):
    #: windows open at ``anchor + k * cycle_seconds`` on monotonic deadlines,
    #: with the sweep, exit, grace and the Mode 01 phase all inside the
    #: period. ``None`` keeps the end-to-start ``window_interval_seconds``
    #: schedule, so existing config and saved profiles are not reinterpreted.
    cycle_seconds: float | None = None


@dataclass(frozen=True)
class SessionControlResult:
    """Outcome of one ``10 <session>`` DiagnosticSessionControl request.

    Carries the NRC, not just a pass/fail bit, so callers can separate "this
    ECU will never do this" (`serviceNotSupported`, `subFunctionNotSupported`)
    from "not right now" (busy, conditions-not-correct, or no answer at all).
    Collapsing the two into a bool is what let a single transient failure
    permanently disable the Mode-22 fast exit mid-drive on 2026-08-07.
    """

    ok: bool
    negative_response_code: int | None = None

    @property
    def permanently_refused(self) -> bool:
        """True only for NRCs that no amount of retrying will change."""
        return self.negative_response_code in PERMANENT_REFUSAL_NRCS


def resolve_did_session_spec(collection: CollectionConfig) -> DidSessionSpec | None:
    """Resolve the Mode-22 session policy from config + data/dids.yaml.

    ``obd_did_session: "auto"`` (the default) takes the gating session from
    the dids.yaml ``access`` block — the vehicle protocol map is the source
    of truth for *how* to reach Mode 22; canary.yaml only carries the
    operational cadence (window interval/length). Returns ``None`` when no
    DIDs are configured or no session gates them (DIDs are then polled
    bare, interleaved with Mode 01 — correct for ECUs that don't gate 0x22).
    """
    if not collection.obd_dids:
        return None
    access = did_access()
    session: int | None
    if collection.obd_did_session == "auto":
        session = access.session if access is not None else None
    else:
        session = collection.obd_did_session
    if session is None:
        return None
    keepalive = collection.obd_did_session_keepalive_seconds
    if keepalive is None:
        keepalive = (
            access.keepalive_seconds
            if access is not None and access.keepalive_seconds
            else DidSessionSpec.keepalive_seconds
        )
    exit_grace = (
        access.exit_grace_seconds
        if access is not None and access.exit_grace_seconds is not None
        else DidSessionSpec.exit_grace_seconds
    )
    return DidSessionSpec(
        session=session,
        keepalive_seconds=keepalive,
        exit_session=access.exit_session if access is not None else None,
        exit_grace_seconds=exit_grace,
        window_interval_seconds=collection.obd_did_window_interval_seconds,
        window_max_seconds=collection.obd_did_window_max_seconds,
        lapse_seconds=collection.obd_did_session_lapse_seconds,
        cycle_seconds=collection.obd_did_cycle_seconds,
    )


@dataclass(frozen=True)
class CanObdSpec:
    """Streaming OBD settings for the Canable-native path."""

    can: CanBusConfig
    poll_rate_hz: float = 1.0
    pids: tuple[str, ...] = (AUTO_PIDS,)
    dids: tuple[str, ...] = ()
    #: Request plan is the union of persisted capture profiles and therefore
    #: must be filtered through the process-wide active-profile gate.
    capture_profiles_enabled: bool = False
    did_request_id: int = DEFAULT_DID_REQUEST_ID
    request_timeout_seconds: float = 1.0
    #: ``None`` = poll DIDs bare, interleaved with Mode 01 (ECUs that don't
    #: gate 0x22). Set = scheduled mutually-exclusive session windows.
    did_session: DidSessionSpec | None = None
    #: Cadence for the Mode 03/07/0A DTC poll (Task 1.3) — deliberately its
    #: own knob, not a ``PollTier`` scaled off ``poll_rate_hz``, since DTCs
    #: change on the order of minutes, not seconds. ``<= 0`` disables the
    #: DTC poll entirely (no requests added, nothing to skip at runtime).
    dtc_poll_interval_seconds: float = DEFAULT_DTC_POLL_INTERVAL_SECONDS
    #: Cadence for the Mode 06 on-board monitor poll (Task 3.1) — same
    #: cadence model as ``dtc_poll_interval_seconds`` above (own knob, not
    #: `poll_rate_hz`/tier scaled). ``<= 0`` disables the Mode 06 poll.
    service_poll_interval_seconds: float = DEFAULT_SERVICE_POLL_INTERVAL_SECONDS
    #: ``"auto"`` discovers supported Mode 06 MIDs at connect time. The
    #: low-level default keeps the historical MID-00 request and avoids adding
    #: discovery traffic to callers that construct a spec directly.
    service_mids: Literal["auto"] | tuple[int, ...] = (0x00,)
    #: Shared flag a ``DtcState`` sets when a stored DTC first appears (Task
    #: 3.3); ``None`` (the default) disables the Mode 02 freeze-frame read
    #: entirely — no ``freeze_frame`` requests are built at all.
    freeze_frame_trigger: FreezeFrameTrigger | None = None
    #: PIDs read once via Mode 02 when ``freeze_frame_trigger`` fires.
    freeze_frame_pids: tuple[int, ...] = FREEZE_FRAME_PIDS
    #: Bridges the ``trigger_poll`` control route into the collector loop
    #: (Task: force-poll on demand). ``None`` (the default) disables it —
    #: no per-tick check, no behavior change for callers that construct a
    #: spec directly without one.
    force_poll_trigger: ForcePollTrigger | None = None
    #: Max Mode 01 PIDs to pack into one request (roadmap/multi-pid-polling.md).
    #: SAE J1979 permits up to 6; on this ECM that was measured at 67.7
    #: requests/sec returning 6 channels each — 33.85 Hz per channel against
    #: 0.06-0.5 Hz today, because the cost here is the round trip, not the
    #: bytes.
    #:
    #: Defaults to 1 (disabled) deliberately. Batching is verified on the
    #: 2008 Xterra ECM but is not universal, and a vehicle that answers
    #: single-PID requests happily may ignore or refuse a batched one. Opt in
    #: per install, or let ``probe_batch_size`` establish it at startup.
    batch_max_pids: int = 1
    #: User-defined custom PIDs (Task 2.6) loaded once at daemon startup via
    #: ``custom_pids.load_custom_pids`` and threaded straight through — the
    #: scheduler never re-reads the YAML itself. Empty by default (no
    #: ``data/custom_pids.yaml`` is the common case).
    custom_pids: tuple[CustomPid, ...] = ()
    #: Channels that win a contended request slot while due, so they hold
    #: their tier rate instead of degrading with the rest of the plan. See
    #: ``DEFAULT_PRIORITY_CHANNELS``; empty disables the priority lane.
    priority_channels: tuple[str, ...] = DEFAULT_PRIORITY_CHANNELS
    #: Vehicle identity the discovery cache is keyed by (C06.6): the VIN, else
    #: make-model-year (``discovery.vehicle_key``). ``None`` — the default,
    #: and every standalone recorder/test spec — caches nothing, so a walk
    #: restarts with each loop.
    discovery_key: str | None = None


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
    #: Support discovery as scheduler work (C06.8). ``requests_sent`` still
    #: counts every frame on the wire; these separate the probes from channel
    #: reads, and count the raw frames tapped *while* a probe was waiting —
    #: the frames the old synchronous path silently consumed.
    discovery_requests_sent: int = 0
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
class _PollRequest:
    kind: Literal["pid", "did", "session", "dtc", "custom", "service", "freeze_frame"]
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


@dataclass
class _PollRequestState:
    missed_responses: int = 0
    suppressed_until: float = 0.0
    next_due_at: float = 0.0
    last_requested_at: float | None = None
    last_response_at: float | None = None
    requests_sent: int = 0
    responses: int = 0
    timeouts: int = 0
    #: Formal ECU refusals for this request — see `CanObdLoopStats.negative_responses`.
    negative_responses: int = 0
    last_negative_response_code: int | None = None
    backoffs: int = 0
    bus_errors: int = 0


#: Eligibility predicate the DID-window controller injects into request
#: selection: ``(request_index, request) -> bool``.
RequestFilter = Callable[[int, "_PollRequest"], bool]


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
    requests: Sequence[_PollRequest],
    states: Sequence[_PollRequestState],
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
    requests: Sequence[_PollRequest],
    states: Sequence[_PollRequestState],
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


class DidWindowController:
    """Mutually-exclusive Mode-22 window scheduler (risk R8 / Task 6.15).

    Drives three phases inside ``run_can_obd_loop``:

    * ``mode01`` — normal polling; DID requests are ineligible. When the
      next window is due (and at least one DID isn't backed off) the loop
      sends ``10 <session>`` to open a window.
    * ``window`` — only DIDs are eligible; each active DID is read once.
      ``10 <session>`` is re-sent every ``keepalive_seconds``. The window
      closes when every pending DID was attempted, on keep-alive failure,
      or at ``window_max_seconds``.
    * ``lapse`` — nothing diagnostic is sent so the ECM's session expires
      (S3 timeout) and Mode 01 answers again — or, with a fast exit, the
      short post-exit grace. Broadcast capture continues throughout — only
      the request side pauses.

    Two timing policies (C04.3): with a ``cycle_seconds`` (spec or profile)
    windows open **start-to-start** at ``anchor + k * cycle`` and the Mode 01
    phase is reserved a share of each cycle; without one the historic
    **end-to-start** ``window_interval_seconds`` applies unchanged.

    Mode 01 misses are *prevented* (requests deferred, not timed out), so
    the 30 s auto-miss backoff never punishes channels for a DID window.
    """

    def __init__(
        self,
        spec: DidSessionSpec,
        did_indices: Sequence[int],
        *,
        now: float,
    ) -> None:
        self._spec = spec
        self._did_indices = frozenset(did_indices)
        self.phase: Literal["mode01", "window", "lapse"] = "mode01"
        self._next_window_at = now + self._interval()
        self._window_deadline = 0.0
        self._lapse_until = 0.0
        self._last_session_sent = 0.0
        self._pending: set[int] = set()
        self.windows_opened = 0
        self.windows_completed = 0
        self.session_failures = 0
        self.keepalives_sent = 0
        self.fast_exits = 0
        self.fast_exit_failures = 0
        #: A (re)connect cannot know whether the ECU is still holding the
        #: session a previous loop opened — the Mode 01 blackout outlives an
        #: S3 lapse by >30 s on the Xterra — so a vehicle with a verified fast
        #: exit is returned to the default session first (C04.7 reconnect).
        self._exit_due = spec.exit_session is not None
        #: Consecutive *permanent* refusals (serviceNotSupported and friends).
        #: Any success resets it — a busy ECU must not accumulate toward being
        #: declared incapable.
        self._exit_refusals = 0
        #: Monotonic time the fast exit may next be attempted. 0.0 = now.
        #: This replaces the old boolean latch, which never cleared and so
        #: turned one refused exit into a Mode-01 blackout for the whole drive.
        self._exit_retry_at = 0.0
        self._exit_retry_backoff = FAST_EXIT_RETRY_MIN_SECONDS
        #: NRC of the most recent failed exit, for `/status` and the log.
        self.last_exit_nrc: int | None = None
        #: Cycle schedule (C04.3/C04.5): windows open at ``anchor + k*cycle``.
        self._cycle_anchor: float | None = None
        self._cycle_k = 0
        self._next_cycle_k = 0
        self._plan: CyclePlan | None = None
        self._infeasible_logged: CyclePlan | None = None
        self.cycle = CycleAccounting()

    def advance(self, states: Sequence[_PollRequestState], now: float) -> None:
        """Apply phase transitions that don't require bus I/O."""
        if self.phase == "window":
            if self._continuous():
                # Nothing to hand the bus back to, so the window never closes:
                # re-arm every DID that has come due instead. Closing would
                # cost a session re-entry and, worse, a lapse during which the
                # capture records nothing.
                self._pending = {i for i in self._did_indices if states[i].suppressed_until <= now}
                return
            live = any(states[i].suppressed_until <= now for i in self._pending)
            if now >= self._window_deadline or not live:
                self._close_window(now)
        elif self.phase == "lapse" and now >= self._lapse_until:
            self.phase = "mode01"
            if self._cycle() is not None:
                # Mode 01 gets its planned share of the cycle before the next
                # window may open, however overdue that window is (C04.5).
                share = self._plan.mode01_share_seconds if self._plan is not None else 0.0
                self._schedule_next_window(now, earliest=now + share)
            elif self._next_window_at <= now:
                # The lapse ate the whole inter-window gap, so the next window
                # is already overdue and would reopen before a single Mode 01
                # request could be issued — starving Mode 01 to *exactly* zero
                # rather than merely slowing it. Any `window_interval` below
                # `lapse_seconds` guarantees this, which is how the 0.25 s
                # hybrid profile lost Mode 01 entirely on 2026-08-07. Hand
                # Mode 01 a slice before reopening.
                self._next_window_at = now + MODE01_MIN_SHARE_SECONDS

    @staticmethod
    def _continuous() -> bool:
        from src.collection import tuning_profile

        return tuning_profile.did_continuous_enabled()

    def _interval(self) -> float:
        """Seconds between windows — the active capture profile wins.

        DID cadence is a sampling choice, not an ECU constraint (this ECM
        answers a DID in ~1.5 ms), so a capture that wants DIDs quickly says
        so and gets them, without a restart or a config edit.
        """
        from src.collection import tuning_profile

        override = tuning_profile.did_interval_override()
        return override if override is not None else self._spec.window_interval_seconds

    def _cycle(self) -> float | None:
        """Start-to-start cycle period, profile first; ``None`` = legacy interval."""
        from src.collection import tuning_profile

        override = tuning_profile.did_cycle_override()
        return override if override is not None else self._spec.cycle_seconds

    def cycle_mode(self) -> bool:
        return self._cycle() is not None

    def set_plan(self, plan: CyclePlan | None) -> None:
        """Record what the next cycle must fit; log an infeasible plan once."""
        self._plan = plan
        if plan is None or plan.feasible or plan == self._infeasible_logged:
            return
        self._infeasible_logged = plan
        log.warning(
            "hybrid cycle of %.3fs is infeasible: %d Mode 01 + %d DID + %d session requests "
            "at %.1f req/s plus %.3fs grace need %.3fs (short by %.3fs); reporting "
            "cycle_feasible=false rather than promising the target",
            plan.cycle_seconds,
            plan.mode01_requests,
            plan.did_requests,
            plan.session_requests,
            plan.budget_rps,
            plan.grace_seconds,
            plan.required_seconds,
            plan.shortfall_seconds,
            extra={"cycle_plan": plan.to_dict()},
        )

    def _schedule_next_window(self, now: float, *, earliest: float) -> None:
        """Next window at the first cycle boundary at or after ``earliest``.

        Boundaries that cannot be met are skipped and counted as missed
        cycles — never issued late as a catch-up burst (C04.5).
        """
        cycle = self._cycle()
        if cycle is None or self._cycle_anchor is None:
            self._next_window_at = max(now + self._interval(), earliest)
            return
        k_next = max(self._next_cycle_k, self._cycle_k + 1)
        boundary = self._cycle_anchor + k_next * cycle
        if boundary < earliest:
            k_needed = math.ceil((earliest - self._cycle_anchor) / cycle - 1e-9)
            k_needed = max(k_needed, k_next)
            self.cycle.on_missed(k_needed - k_next)
            k_next = k_needed
            boundary = self._cycle_anchor + k_next * cycle
        self._next_cycle_k = k_next
        self._next_window_at = boundary

    def session_control_due(self, states: Sequence[_PollRequestState], now: float) -> bool:
        """Whether the loop should send ``10 <session>`` now."""
        if self.phase == "mode01":
            if now < self._next_window_at:
                return False
            if all(states[i].suppressed_until > now for i in self._did_indices):
                # Every DID is backed off; don't burn a window on them. With
                # nothing to schedule the cycle re-anchors on the next open
                # rather than reporting idle time as missed cycles.
                self._cycle_anchor = None
                self._next_window_at = now + (self._cycle() or self._interval())
                return False
            return True
        if self.phase == "window":
            return now - self._last_session_sent >= self._spec.keepalive_seconds
        return False

    def session_work_pending(self, states: Sequence[_PollRequestState], now: float) -> bool:
        """Side-effect-free: is an exit, entry or keep-alive waiting on the budget?"""
        if self._exit_due:
            return True
        if self.phase == "mode01":
            return now >= self._next_window_at and any(
                states[i].suppressed_until <= now for i in self._did_indices
            )
        if self.phase == "window":
            return now - self._last_session_sent >= self._spec.keepalive_seconds
        return False

    def on_entry_sent(self, now: float) -> None:
        """The loop is about to send ``10 <session>``: a cycle begins (or a keep-alive)."""
        if self.phase != "mode01":
            return
        cycle = self._cycle()
        missed = 0
        if cycle is None or self._cycle_anchor is None:
            self._cycle_anchor, self._cycle_k, self._next_cycle_k = now, 0, 0
        else:
            k_actual = max(self._next_cycle_k, int((now - self._cycle_anchor) / cycle + 1e-9))
            missed = k_actual - self._next_cycle_k
            self._cycle_k = k_actual
        self.cycle.begin_cycle(now, self._plan, missed=missed)
        self.cycle.on_entry_sent(now)

    def on_exit_sent(self, now: float) -> None:
        self.cycle.on_exit_sent(now)

    def on_session_response(
        self,
        *,
        ok: bool,
        states: Sequence[_PollRequestState],
        now: float,
    ) -> None:
        self._last_session_sent = now
        if self.phase == "mode01":
            if ok:
                self.phase = "window"
                self.windows_opened += 1
                self._window_deadline = now + self._spec.window_max_seconds
                self._pending = {i for i in self._did_indices if states[i].suppressed_until <= now}
                # Every pending DID is due "this window" regardless of tier.
                for i in self._pending:
                    states[i].next_due_at = now
            else:
                self.session_failures += 1
                self._schedule_next_window(now, earliest=now)
        elif self.phase == "window":
            if ok:
                self.keepalives_sent += 1
            else:
                # Session lost mid-window: let it lapse, resume Mode 01.
                self.session_failures += 1
                self._close_window(now)

    def session_exit_due(self) -> bool:
        """Whether the loop should send ``10 <exit_session>`` now."""
        return self._exit_due

    def fast_exit_available(self, now: float) -> bool:
        """Whether a fast exit should be attempted when the next window closes."""
        return self._spec.exit_session is not None and now >= self._exit_retry_at

    def on_exit_response(self, *, result: SessionControlResult, now: float) -> None:
        """Result of the return-to-default request.

        Only an NRC that means "never" disables the fast exit, and only after
        `FAST_EXIT_REFUSALS_BEFORE_DISABLE` of them in a row. Everything else —
        busy, conditions-not-correct, or silence — costs this window's lapse
        and is retried on the next one. Even a disable is provisional: it
        expires, so the capture heals mid-drive without a restart.
        """
        self._exit_due = False
        if result.ok:
            self.fast_exits += 1
            if self._exit_retry_at:
                log.info(
                    "Mode-22 fast exit recovered; Mode 01 back at full rate",
                    extra={"fast_exits": self.fast_exits, "failures": self.fast_exit_failures},
                )
            self._exit_refusals = 0
            self._exit_retry_at = 0.0
            self._exit_retry_backoff = FAST_EXIT_RETRY_MIN_SECONDS
            self.last_exit_nrc = None
            self._lapse_until = min(self._lapse_until, now + self._spec.exit_grace_seconds)
            return

        self.fast_exit_failures += 1
        self.last_exit_nrc = result.negative_response_code
        if not result.permanently_refused:
            # Transient: wait this window's lapse out and try again next time.
            return
        self._exit_refusals += 1
        if self._exit_refusals < FAST_EXIT_REFUSALS_BEFORE_DISABLE:
            return
        self._exit_retry_at = now + self._exit_retry_backoff
        log.warning(
            "Mode-22 fast exit refused %d times in a row (%s); falling back to the "
            "%.1fs session lapse and re-probing in %.0fs. Mode 01 sample rate drops "
            "until then.",
            self._exit_refusals,
            nrc_name(result.negative_response_code)
            if result.negative_response_code is not None
            else "no response",
            self._spec.lapse_seconds,
            self._exit_retry_backoff,
            extra={
                "nrc": result.negative_response_code,
                "retry_in_seconds": self._exit_retry_backoff,
            },
        )
        self._exit_retry_backoff = min(self._exit_retry_backoff * 2, FAST_EXIT_RETRY_MAX_SECONDS)

    def request_eligible(self, index: int, request: _PollRequest, *, now: float) -> bool:
        del now  # phase state already advanced this tick
        return self.request_verdict(index, request).eligible

    def request_verdict(self, index: int, request: _PollRequest) -> Eligibility:
        """The session gate: the C06.7 matrix row for this phase, selection aside.

        Mode 03/07/0A (Task 1.3), Mode 06 (Task 3.1), Mode 02 freeze frames
        (Task 3.3) and custom PIDs (Task 2.6) are Mode 01-side services the
        ECM answers in the default session, so they share Mode 01's phase
        rather than being starved for the controller's whole lifetime; a
        custom PID declared on Mode 22 for a session-gated ECU would need the
        ``did`` treatment (not implemented — no such custom PID exists yet).
        Inside a window a DID already attempted this window is deferred to
        the next one.
        """
        from src.collection import tuning_profile

        verdict = evaluate(request.kind, tuning_profile.profile_class(), self.phase)
        if verdict.eligible and request.kind == "did" and index not in self._pending:
            return Eligibility("deferred", "attempted_this_window")
        return verdict

    @property
    def matrix_phase(self) -> SessionPhase:
        return self.phase

    def did_pending(self, index: int) -> bool:
        """Whether this DID is still owed in the open window."""
        return index in self._pending

    def service_kind(self, index: int) -> ServiceKind:
        return "mode22" if index in self._did_indices else "mode01"

    def on_request_attempted(self, index: int) -> None:
        if index in self._did_indices:
            if index in self._pending:
                self.cycle.on_attempt("mode22")
            self._pending.discard(index)
        else:
            self.cycle.on_attempt("mode01")

    def on_service_time(self, kind: ServiceKind, seconds: float) -> None:
        self.cycle.on_service_time(kind, seconds)

    def on_response(self, index: int, now: float, acquired_at: float | None) -> None:
        """A request answered with readings — the only recovery evidence there is."""
        kind = self.service_kind(index)
        self.cycle.on_acquired(kind, acquired_at)
        if kind == "mode22":
            self.cycle.on_did_answered(now)
        else:
            self.cycle.on_mode01_answered(now)

    def next_transition_at(self) -> float:
        if self.phase == "window":
            if self._continuous():
                # The window deadline is meaningless here and, once passed,
                # would pin this to a time in the past and spin the loop.
                # Keep-alive is the only scheduled event left.
                return self._last_session_sent + self._spec.keepalive_seconds
            return min(
                self._window_deadline,
                self._last_session_sent + self._spec.keepalive_seconds,
            )
        if self.phase == "lapse":
            return self._lapse_until
        return self._next_window_at

    def next_keepalive_at(self) -> float | None:
        """When the held session next needs ``10 <session>`` re-sent."""
        if self.phase != "window":
            return None
        return self._last_session_sent + self._spec.keepalive_seconds

    def release(self, now: float) -> None:
        """Close an open window so the session transition completes (C04.7).

        Used when the profile drops its DIDs, on a generation change, and
        when passive TX gating lifts: a window left in ``window`` phase would
        keep the ECU in the diagnostic session (Mode 01 dead) while the loop
        polls as if it were not.
        """
        if self.phase == "window":
            self._close_window(now)

    def on_generation_change(self, now: float) -> None:
        """New capture generation: finish any transition and re-plan promptly.

        The next window is due now (not ``interval`` after the previous
        close), so a DID profile armed mid-drive starts within a budget slot
        instead of up to a config interval later — the 26.56 s first-DID
        delay recorded on 2026-08-07.
        """
        self.release(now)
        self.cycle.abandon(now)
        self.cycle.reset()
        self._plan = None
        self._infeasible_logged = None
        self._cycle_anchor = None
        if self.phase == "mode01":
            self._next_window_at = now

    def _close_window(self, now: float) -> None:
        self.cycle.on_window_closed(unattempted=len(self._pending))
        self.phase = "lapse"
        self._lapse_until = now + self._spec.lapse_seconds
        # Prefer handing the bus back explicitly. The lapse exists only
        # because the session has to expire on its own; when the ECU accepts a
        # return-to-default it is seconds of Mode-01 blackout for nothing.
        self._exit_due = self.fast_exit_available(now)
        if not self._exit_due:
            self.cycle.on_lapse_started()
        self._schedule_next_window(now, earliest=now)
        self.windows_completed += 1
        self._pending.clear()

    def snapshot(self, now: float) -> dict[str, Any]:
        """DID profile state for ``/status.obd_scheduler.did_profile``."""
        cycle = self._cycle()
        return {
            "phase": self.phase,
            "session": f"0x{self._spec.session:02X}",
            "did_request_count": len(self._did_indices),
            "next_window_in_seconds": (
                round(max(0.0, self._next_window_at - now), 3) if self.phase == "mode01" else 0.0
            ),
            "window_interval_seconds": self._interval(),
            "fast_exits": self.fast_exits,
            "fast_exit_failures": self.fast_exit_failures,
            "fast_exit_supported": self.fast_exit_available(now),
            #: Seconds until the fast exit is re-probed; 0.0 when it is live.
            #: Non-zero means Mode 01 is running at the reduced lapse-path
            #: rate *and will recover on its own* — the state the old boolean
            #: could not express.
            "fast_exit_retry_in_seconds": (
                round(max(0.0, self._exit_retry_at - now), 1) if self._exit_retry_at else 0.0
            ),
            "last_exit_nrc": self.last_exit_nrc,
            "last_exit_nrc_name": (
                nrc_name(self.last_exit_nrc) if self.last_exit_nrc is not None else None
            ),
            "window_max_seconds": self._spec.window_max_seconds,
            "windows_opened": self.windows_opened,
            "windows_completed": self.windows_completed,
            "session_failures": self.session_failures,
            "keepalives_sent": self.keepalives_sent,
            # Hybrid cycle (C04.8): the target and the achieved values share
            # one definition — start-to-start between session entries.
            "cycle": {
                "timing_policy": "cycle" if cycle is not None else "interval",
                "cycle_seconds": cycle,
                "cycle_feasible": self._plan.feasible if self._plan is not None else None,
                "plan": self._plan.to_dict() if self._plan is not None else None,
                **self.cycle.snapshot(now),
            },
        }


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
    #: Multi-PID batching (roadmap/multi-pid-polling.md). ``None`` when
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "session_control": self.session_control,
            "discovery": self.discovery,
            "deferred": self.deferred,
            "configured_poll_rate_hz": self.configured_poll_rate_hz,
            "bus_budget_requests_per_second": self.bus_budget_requests_per_second,
            "active_requests": self.active_requests,
            "active_requests_unfiltered": self.active_requests,
            "achieved_hz_60s": self.achieved_hz_60s,
            "achieved_hz_capture": self.achieved_hz_capture,
            "capture_observed_seconds": self.capture_observed_seconds,
            "capture_samples": self.capture_samples,
            "capture_max_gap_seconds": self.capture_max_gap_seconds,
            "backed_off_requests": self.backed_off_requests,
            "requests_sent": self.requests_sent,
            "readings_emitted": self.readings_emitted,
            "request_timeouts": self.request_timeouts,
            "negative_responses": self.negative_responses,
            "bus_errors": self.bus_errors,
            "response_rate": self.response_rate,
            "by_tier": self.by_tier,
            "requests": self.requests,
            "did_profile": self.did_profile,
            "batching": self.batching,
        }


def parse_pid_token(token: str) -> int | None:
    """Parse a PID selector."""
    normalized = token.strip().upper()
    if not normalized:
        raise ValueError("empty OBD PID token")
    if normalized == AUTO_PIDS.upper():
        return None
    by_name = _PID_IDS_BY_NAME.get(normalized)
    if by_name is not None:
        return by_name
    try:
        pid = int(normalized, 16)
    except ValueError as exc:
        raise ValueError(f"unknown Canable OBD PID {token!r}") from exc
    if pid not in PID_DECODERS:
        raise ValueError(f"no Canable OBD decoder registered for PID 0x{pid:02X}")
    return pid


def parse_did_token(token: str) -> int:
    stripped = token.strip()
    normalized = stripped.upper()
    if len(normalized) == 4 and all(c in "0123456789ABCDEF" for c in normalized):
        return int(normalized, 16)
    by_name = _did_ids_by_name().get(stripped)
    if by_name is not None:
        return by_name
    raise ValueError(
        f"DID must be a 4-character hex string or a name from data/dids.yaml, got {token!r}"
    )


def build_poll_requests(client: _PidDiscoveryClient | None, spec: CanObdSpec) -> list[_PollRequest]:
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
            _PollRequest(
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
    for did in dict.fromkeys(parse_did_token(d) for d in spec.dids):
        requests.append(
            _PollRequest(
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
            _PollRequest(
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
                _PollRequest(
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
                _PollRequest(
                    kind="freeze_frame",
                    service=0x02,
                    identifier=pid,
                    request_id=DEFAULT_REQUEST_ID,
                    channel=f"{FREEZE_FRAME_CHANNEL_PREFIX}{PID_DECODERS[pid][0]}",
                    poll_tier="on_demand",
                )
            )
    return requests


def mode06_requests(mids: Iterable[int]) -> list[_PollRequest]:
    """Mode 06 monitor-result requests, one per MID, on the service-poll cadence.

    Task 3.1: the same "occasional service poll" cadence model as the DTC
    poll — no vehicle_state gating, the round-robin scheduler already backs
    off an ECU that stops answering. The MID stays on the wire as the
    request identifier (``06 <mid>``) and the response's responder id rides
    every reading, so per-ECU provenance is never lost (C06.11).
    """
    return [
        _PollRequest(
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
    requests: list[_PollRequest], supported: Iterable[int]
) -> list[_PollRequest]:
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


def _update_response_state(
    state: _PollRequestState,
    *,
    emitted: bool,
    now: float,
    focused_capture: bool = False,
) -> None:
    if emitted:
        state.missed_responses = 0
        state.suppressed_until = 0.0
        return
    state.missed_responses += 1
    state.suppressed_until = now + _miss_backoff_seconds(state, focused_capture=focused_capture)
    state.backoffs += 1


def _miss_backoff_seconds(state: _PollRequestState, *, focused_capture: bool = False) -> float:
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


def _initial_request_states(
    requests: list[_PollRequest],
    spec: CanObdSpec,
    now: float,
) -> list[_PollRequestState]:
    if not requests:
        return []
    spacing = 1.0 / _bus_budget_requests_per_second(spec)
    states: list[_PollRequestState] = []
    for index, request in enumerate(requests):
        states.append(_PollRequestState(next_due_at=now + index * spacing))
        if _request_rate_hz(request, spec) <= 0.0:
            states[-1].suppressed_until = float("inf")
    return states


def _bus_budget_requests_per_second(spec: CanObdSpec) -> float:
    base = max(float(spec.poll_rate_hz), 0.001)
    return max(MIN_BUS_BUDGET_REQUESTS_PER_SECOND, base * BUS_BUDGET_MULTIPLIER)


def _request_rate_hz(request: _PollRequest, spec: CanObdSpec) -> float:
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
    base = max(float(spec.poll_rate_hz), 0.001)
    rate = base * TIER_RATE_FACTORS.get(request.poll_tier, TIER_RATE_FACTORS["medium"])
    if request.kind == "did":
        # An all-DID capture needs its rate sayable too. Outside a capture
        # this returns None and DID cadence stays the window controller's
        # business, as before.
        from src.collection import tuning_profile

        absolute = tuning_profile.absolute_rate_hz(request.channel)
        if absolute is not None:
            return absolute
    if request.kind == "pid":
        # A weight is a relative share within the selected profile; 1.0 is
        # byte-for-byte the historic tier cadence.
        from src.collection import tuning_profile

        # A capture profile may pin an absolute rate instead. The relative
        # model tops out at `poll_rate_hz` (1.0) per channel however much bus
        # budget exists, which is not a rate a tuning capture can be expressed
        # in — see CaptureProfile.target_rate_hz.
        absolute = tuning_profile.absolute_rate_hz(request.channel)
        if absolute is not None:
            return absolute

        weight, uniform_cadence = tuning_profile.rate_policy(request.channel)
        if uniform_cadence:
            # Tiers exist so a minute-scale signal doesn't burn budget at a
            # fast channel's cadence — a monitoring optimization. A capture
            # round is a *sample*: the operator picked these channels to see
            # them together, so they all run at the base cadence or they can
            # never share a round.
            rate = base
        rate *= weight
    return rate


def _request_interval_seconds(request: _PollRequest, spec: CanObdSpec) -> float:
    rate = _request_rate_hz(request, spec)
    if rate <= 0.0:
        return float("inf")
    return 1.0 / rate


def _next_request_due_at(
    request: _PollRequest,
    state: _PollRequestState,
    spec: CanObdSpec,
) -> float:
    interval = _request_interval_seconds(request, spec)
    if interval == float("inf"):
        return float("inf")
    anchor = state.last_requested_at if state.last_requested_at is not None else time.monotonic()
    next_due = anchor + interval
    if state.suppressed_until > next_due:
        return state.suppressed_until
    return next_due


def _count_active_requests(
    requests: list[_PollRequest],
    states: list[_PollRequestState],
    now: float,
    *,
    eligible: RequestFilter | None = None,
) -> int:
    return sum(
        1
        for index, (request, state) in enumerate(zip(requests, states, strict=True))
        if not _is_request_suppressed(state, now) and (eligible is None or eligible(index, request))
    )


def _select_next_due_request(
    requests: list[_PollRequest],
    states: list[_PollRequestState],
    now: float,
    *,
    eligible: RequestFilter | None = None,
    among: Container[int] | None = None,
    preferred: Container[int] = frozenset(),
) -> tuple[int, _PollRequest, _PollRequestState] | None:
    """The due request to issue next.

    ``among`` restricts the choice to the open round's unattempted members
    (C02.3: one attempt per member per round). A member is due inside the
    round's window by construction, so it may be issued as soon as the round
    is open rather than at its own due instant — a one-time phase advance of
    under one period that lets a round's members converge onto one cadence
    (and one batch) instead of staying staggered forever; the configured
    rate is unchanged because the next due time is still ``issued + interval``.
    ``preferred`` — the profile's ``required_tuple`` — ranks just below the
    priority lane, so when the budget cannot serve a whole round the tuple
    completes first (C02.5); it never makes a request due, so it cannot
    starve the others.
    """
    candidates = [
        (state.next_due_at, index, request, state)
        for index, (request, state) in enumerate(zip(requests, states, strict=True))
        if not _is_request_suppressed(state, now)
        and (among is not None or state.next_due_at <= now)
        and (among is None or index in among)
        and (eligible is None or eligible(index, request))
    ]
    if not candidates:
        return None
    # Priority requests (``CanObdSpec.priority_channels``) win over any other
    # due request, however overdue that one is. Without this the plan degrades
    # *uniformly* when demand exceeds what the bus can actually serve — every
    # request, mode-critical or not, ends up at the same fraction of its
    # configured rate. Starvation isn't a risk in the other direction: the
    # priority set is small and each member still only becomes due at its own
    # tier rate, so it can never claim more than that slice of the budget.
    _due_at, index, request, state = min(
        candidates,
        key=lambda item: (not item[2].priority, item[1] not in preferred, item[0], item[1]),
    )
    return index, request, state


def is_batchable(request: _PollRequest) -> bool:
    """Whether a request can ride along in a multi-PID Mode 01 batch.

    Only plain Mode 01 PIDs. Custom PIDs are excluded even when they sit on
    Mode 01: they decode through their own formula against a payload this
    module doesn't know the width of, so they can't be split back out of a
    batched response reliably.
    """
    return request.kind == "pid" and request.service == 0x01 and 0 <= request.identifier <= 0xFF


def select_batch_companions(  # noqa: PLR0913
    requests: list[_PollRequest],
    states: list[_PollRequestState],
    now: float,
    *,
    leader_index: int,
    max_pids: int,
    eligible: RequestFilter | None = None,
    ignore_tier: bool = False,
    members: Container[int] | None = None,
    preferred: Container[int] = frozenset(),
) -> list[int]:
    """Indices to issue alongside ``leader_index`` as one batched request.

    Returns the leader plus up to ``max_pids - 1`` companions, or just the
    leader when batching is off or nothing else qualifies.

    Companions must be due, unsuppressed, eligible, batchable, and **in the
    same poll tier** as the leader. The tier rule is what stops a slow-tier
    PID from being dragged to fast-tier cadence (wasting budget on data that
    changes on the order of minutes) or a fast-tier one being held back.
    ``ignore_tier`` lifts it for capture rounds, where the operator's channel
    selection *is* the sample and every member is expected on the same row.

    ``members`` confines companions to the open round's unattempted members
    (C02.5: same-round companions travel together; a member is never issued
    twice in one round). A member is due *inside this round's window* by
    construction, so it may ride the leader's batch a little before its own
    due instant: that is a one-time phase advance of under one period, after
    which the batch shares one cadence — not a rate increase. ``preferred``
    — the profile's ``required_tuple`` — fills the batch first, so the
    tuple's members share one response when the ceiling would otherwise
    split them across requests.
    """
    leader = requests[leader_index]
    if max_pids <= 1 or not is_batchable(leader):
        return [leader_index]

    chosen = [leader_index]
    seen_pids = {leader.identifier}
    order = sorted(range(len(requests)), key=lambda index: (index not in preferred, index))
    for index in order:
        request, state = requests[index], states[index]
        if len(chosen) >= max_pids:
            break
        if index == leader_index or not is_batchable(request):
            continue
        if members is not None and index not in members:
            continue
        if not ignore_tier and request.poll_tier != leader.poll_tier:
            continue
        if request.identifier in seen_pids:
            continue
        if _is_request_suppressed(state, now):
            continue
        if members is None and state.next_due_at > now:
            continue
        if eligible is not None and not eligible(index, request):
            continue
        chosen.append(index)
        seen_pids.add(request.identifier)
    return chosen


def _next_due_at(
    requests: list[_PollRequest],
    states: list[_PollRequestState],
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
    requests: list[_PollRequest],
    states: list[_PollRequestState],
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


def _is_request_suppressed(state: _PollRequestState, now: float) -> bool:
    return state.suppressed_until > now


def _is_request_backed_off(state: _PollRequestState, now: float) -> bool:
    return _is_request_suppressed(state, now) and state.suppressed_until != float("inf")


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
    if spec.batch_max_pids <= 1 and stats.batched_requests == 0:
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
        "batched_requests": stats.batched_requests,
        "batched_pids_requested": stats.batched_pids_requested,
        "mean_pids_per_batch": mean_pids,
        "failures": stats.batch_failures,
        "desyncs": stats.batch_desyncs,
        "size_narrowings": stats.batch_size_narrowings,
        # Lifetime average since the loop started. Explicitly named: read as
        # a current rate it is misleading, and was — on 2026-08-04 it barely
        # moved through a real rate change because hours of history dominated.
        # `obd_scheduler.achieved_hz_60s` is the trailing-window answer.
        "channel_reads_per_second_lifetime": achieved_hz,
        "channel_reads_per_second": achieved_hz,
    }


def _scheduler_metrics(
    requests: list[_PollRequest],
    states: list[_PollRequestState],
    spec: CanObdSpec,
    stats: CanObdLoopStats,
    now: float,
    *,
    did_profile: dict[str, Any] | None = None,
    rounds: dict[str, Any] | None = None,
    discovery: dict[str, Any] | None = None,
    deferred: list[dict[str, Any]] | None = None,
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

    from src.collection import tuning_profile

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
                "priority": request.priority,
                "configured_rate_hz": round(_request_rate_hz(request, spec), 4),
                "requests_sent": state.requests_sent,
                "responses": state.responses,
                "timeouts": state.timeouts,
                "negative_responses": state.negative_responses,
                "last_negative_response_code": (
                    f"0x{state.last_negative_response_code:02X} "
                    f"({nrc_name(state.last_negative_response_code)})"
                    if state.last_negative_response_code is not None
                    else None
                ),
                "response_rate": (
                    round(state.responses / state.requests_sent, 4) if state.requests_sent else None
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
        discovery=discovery,
        deferred=list(deferred or []),
    )


def _session_control_metrics(stats: CanObdLoopStats) -> dict[str, Any]:
    channel_requests = max(
        0, stats.requests_sent - stats.session_controls_sent - stats.discovery_requests_sent
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


__all__ = [
    "apply_mode01_support",
    "mode06_requests",
    "select_batch_companions",
    "is_batchable",
    "RoundScheduler",
    "RoundState",
    "round_members",
    "round_period_seconds",
    "AUTO_MISS_BACKOFF_SECONDS",
    "CAPTURE_MISS_BACKOFF_SECONDS",
    "AUTO_PIDS",
    "DEFAULT_DID_REQUEST_ID",
    "DEFAULT_PRIORITY_CHANNELS",
    "MAX_MISS_BACKOFF_SECONDS",
    "next_wake_at",
    "CanObdLoopStats",
    "CanObdSpec",
    "DidSessionSpec",
    "DidWindowController",
    "ForcePollTrigger",
    "FreezeFrameTrigger",
    "ObdSchedulerMetrics",
    "build_poll_requests",
    "did_access",
    "parse_did_token",
    "parse_pid_token",
    "resolve_did_session_spec",
]
