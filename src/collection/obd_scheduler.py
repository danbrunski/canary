"""Adaptive OBD request scheduling for the Canable-native collector."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from src.collection import bus_budget
from src.collection.can_obd_client import DEFAULT_REQUEST_ID, PID_DECODERS
from src.collection.isotp import nrc_name
from src.collection.obd_registry import PollTier, lookup
from src.config import DEFAULT_PRIORITY_CHANNELS, CanBusConfig, CollectionConfig

if TYPE_CHECKING:
    from src.collection.custom_pids import CustomPid
    from src.decode.dids import DidAccess, DidMap

AUTO_PIDS = "auto"
DEFAULT_DID_REQUEST_ID = 0x7E0
AUTO_MISS_BACKOFF_SECONDS = 30.0
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
    return {entry.name: entry.did for entry in did_map.dids}


def known_did_names() -> tuple[str, ...]:
    """Return the vehicle DID inventory available to capture profiles."""
    return tuple(_did_ids_by_name())


def did_access() -> DidAccess | None:
    """The access block of data/dids.yaml (gating session, keep-alive)."""
    did_map = _load_did_map()
    return None if did_map is None else did_map.access


class _PidDiscoveryClient(Protocol):
    def query_supported_mode_01_pids(self) -> tuple[int, ...]: ...

    def query_supported_mode_06_mids(self) -> tuple[int, ...]: ...


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
    window_interval_seconds: float = 60.0
    window_max_seconds: float = 10.0
    lapse_seconds: float = 5.0
    #: Sub-function that hands the bus back to Mode 01 immediately (UDS
    #: default session). ``None`` = no verified fast exit on this vehicle;
    #: wait out ``lapse_seconds`` instead.
    exit_session: int | None = None
    #: Settling time after a successful fast exit, in place of the lapse.
    exit_grace_seconds: float = 0.05


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
    return DidSessionSpec(
        session=session,
        keepalive_seconds=keepalive,
        exit_session=access.exit_session if access is not None else None,
        window_interval_seconds=collection.obd_did_window_interval_seconds,
        window_max_seconds=collection.obd_did_window_max_seconds,
        lapse_seconds=collection.obd_did_session_lapse_seconds,
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
    #: ``time.monotonic()`` when the loop started, so metrics can report an
    #: achieved rate rather than a bare count.
    loop_started_at: float | None = None


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


class DidWindowController:
    """Mutually-exclusive Mode-22 window scheduler (risk R8 / Task 6.15).

    Drives three phases inside ``run_can_obd_loop``:

    * ``mode01`` — normal polling; DID requests are ineligible. At
      ``window_interval_seconds`` cadence (and only while at least one DID
      isn't backed off) the loop sends ``10 <session>`` to open a window.
    * ``window`` — only DIDs are eligible; each active DID is read once.
      ``10 <session>`` is re-sent every ``keepalive_seconds``. The window
      closes when every pending DID was attempted, on keep-alive failure,
      or at ``window_max_seconds``.
    * ``lapse`` — nothing diagnostic is sent so the ECM's session expires
      (S3 timeout) and Mode 01 answers again. Broadcast capture continues
      throughout — only the request side pauses.

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
        self._did_indices = tuple(did_indices)
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
        self._exit_due = False
        #: Latches on a refused exit so one unsupported ECU costs one attempt,
        #: not one per window.
        self._fast_exit_unsupported = False

    def advance(self, states: Sequence[_PollRequestState], now: float) -> None:
        """Apply phase transitions that don't require bus I/O."""
        if self.phase == "window":
            live = any(states[i].suppressed_until <= now for i in self._pending)
            if now >= self._window_deadline or not live:
                self._close_window(now)
        elif self.phase == "lapse" and now >= self._lapse_until:
            self.phase = "mode01"

    def _interval(self) -> float:
        """Seconds between windows — the active capture profile wins.

        DID cadence is a sampling choice, not an ECU constraint (this ECM
        answers a DID in ~1.5 ms), so a capture that wants DIDs quickly says
        so and gets them, without a restart or a config edit.
        """
        from src.collection import tuning_profile

        override = tuning_profile.did_interval_override()
        return override if override is not None else self._spec.window_interval_seconds

    def session_control_due(self, states: Sequence[_PollRequestState], now: float) -> bool:
        """Whether the loop should send ``10 <session>`` now."""
        if self.phase == "mode01":
            if now < self._next_window_at:
                return False
            if all(states[i].suppressed_until > now for i in self._did_indices):
                # Every DID is backed off; don't burn a window on them.
                self._next_window_at = now + self._interval()
                return False
            return True
        if self.phase == "window":
            return now - self._last_session_sent >= self._spec.keepalive_seconds
        return False

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
                self._next_window_at = now + self._interval()
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

    def on_exit_response(self, *, ok: bool, now: float) -> None:
        """Result of the return-to-default request."""
        self._exit_due = False
        if ok:
            self.fast_exits += 1
            self._lapse_until = min(self._lapse_until, now + self._spec.exit_grace_seconds)
        else:
            # Fall back to waiting the session out, for this window and every
            # one after it.
            self.fast_exit_failures += 1
            self._fast_exit_unsupported = True

    def request_eligible(self, index: int, request: _PollRequest, *, now: float) -> bool:
        del now  # phase state already advanced this tick
        if self.phase == "window":
            return request.kind == "did" and index in self._pending
        if self.phase == "lapse":
            return False
        # Mode 03/07/0A (Task 1.3), Mode 06 (Task 3.1), and Mode 02 freeze
        # frame (Task 3.3) are standard, non-Mode-22 requests just like Mode
        # 01 — the ECM answers them in the default session, so they share
        # Mode 01's eligibility rather than being starved for the DID
        # controller's whole lifetime. Custom PIDs (Task 2.6) get the same
        # treatment: most use Mode 01/09, which is correct here; a custom PID
        # declared on Mode 22 for a session-gated ECU would need the same
        # windowed treatment as `did` (not implemented — no such custom PID
        # exists yet, see custom_pids.example.yaml).
        return request.kind in ("pid", "dtc", "custom", "service", "freeze_frame")

    def on_request_attempted(self, index: int) -> None:
        self._pending.discard(index)

    def next_transition_at(self) -> float:
        if self.phase == "window":
            return min(
                self._window_deadline,
                self._last_session_sent + self._spec.keepalive_seconds,
            )
        if self.phase == "lapse":
            return self._lapse_until
        return self._next_window_at

    def _close_window(self, now: float) -> None:
        self.phase = "lapse"
        self._lapse_until = now + self._spec.lapse_seconds
        # Prefer handing the bus back explicitly. The lapse exists only
        # because the session has to expire on its own; when the ECU accepts a
        # return-to-default it is seconds of Mode-01 blackout for nothing.
        self._exit_due = self._spec.exit_session is not None and not self._fast_exit_unsupported
        self._next_window_at = now + self._interval()
        self.windows_completed += 1
        self._pending.clear()

    def snapshot(self, now: float) -> dict[str, Any]:
        """DID profile state for ``/status.obd_scheduler.did_profile``."""
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
            "fast_exit_supported": self._spec.exit_session is not None
            and not self._fast_exit_unsupported,
            "window_max_seconds": self._spec.window_max_seconds,
            "windows_opened": self.windows_opened,
            "windows_completed": self.windows_completed,
            "session_failures": self.session_failures,
            "keepalives_sent": self.keepalives_sent,
        }


@dataclass(frozen=True)
class ObdSchedulerMetrics:
    """Live state of the adaptive OBD poll scheduler."""

    configured_poll_rate_hz: float
    bus_budget_requests_per_second: float
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
    #: Mode-22 session-window state, ``None`` when no session gates DIDs.
    did_profile: dict[str, Any] | None = None
    #: Multi-PID batching (roadmap/multi-pid-polling.md). ``None`` when
    #: batching is off. ``mean_pids_per_batch`` is the round-trip saving, and
    #: ``achieved_hz`` is what an operator actually needs to know before
    #: trusting a tuning capture — a per-cell verdict is only as good as the
    #: sample count behind it.
    batching: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured_poll_rate_hz": self.configured_poll_rate_hz,
            "bus_budget_requests_per_second": self.bus_budget_requests_per_second,
            "active_requests": self.active_requests,
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


def build_poll_requests(client: _PidDiscoveryClient, spec: CanObdSpec) -> list[_PollRequest]:
    """Build the round-robin request plan from config and ECU support."""
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
        supported = client.query_supported_mode_01_pids()
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
    for did in [parse_did_token(d) for d in spec.dids]:
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
        if spec.service_mids == "auto":
            discover_mode06 = getattr(client, "query_supported_mode_06_mids", None)
            mode06_mids = tuple(discover_mode06()) if discover_mode06 is not None else (0x00,)
        else:
            mode06_mids = spec.service_mids
        for service, channel in SERVICE_POLL_CHANNELS.items():
            for mid in mode06_mids:
                requests.append(
                    _PollRequest(
                        kind="service",
                        service=service,
                        identifier=mid,
                        request_id=DEFAULT_REQUEST_ID,
                        channel=channel,
                        poll_tier="dtc",
                    )
                )
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
) -> None:
    if emitted:
        state.missed_responses = 0
        state.suppressed_until = 0.0
        return
    state.missed_responses += 1
    state.suppressed_until = now + _miss_backoff_seconds(state)
    state.backoffs += 1


def _miss_backoff_seconds(state: _PollRequestState) -> float:
    """Backoff to apply after a missed response.

    A request that has answered at least once keeps the flat
    ``AUTO_MISS_BACKOFF_SECONDS`` — it's a live channel that dropped a
    response (bus dropout, DID window, momentary ECU silence) and must come
    back promptly once the bus recovers, so this path is deliberately not
    escalated.

    A request that has *never* answered is a different thing: on the ``auto``
    PID set the catalog is probed wholesale, so unsupported PIDs are expected
    and permanent. Doubling their backoff up to ``MAX_MISS_BACKOFF_SECONDS``
    stops them re-probing twice a minute forever and hands the reclaimed
    request slots (and their timeout stalls) back to channels that answer.
    They are never retired outright — a PID gated behind a warm engine or an
    ECU that only answers in a particular state still gets a periodic probe.
    """
    if state.last_response_at is not None:
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
    if request.kind == "pid":
        # A weight is a relative share within the selected profile; 1.0 is
        # byte-for-byte the historic tier cadence.
        from src.collection import tuning_profile

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
) -> tuple[int, _PollRequest, _PollRequestState] | None:
    candidates = [
        (state.next_due_at, index, request, state)
        for index, (request, state) in enumerate(zip(requests, states, strict=True))
        if not _is_request_suppressed(state, now)
        and state.next_due_at <= now
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
        candidates, key=lambda item: (not item[2].priority, item[0], item[1])
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
    """
    leader = requests[leader_index]
    if max_pids <= 1 or not is_batchable(leader):
        return [leader_index]

    chosen = [leader_index]
    seen_pids = {leader.identifier}
    for index, (request, state) in enumerate(zip(requests, states, strict=True)):
        if len(chosen) >= max_pids:
            break
        if index == leader_index or not is_batchable(request):
            continue
        if not ignore_tier and request.poll_tier != leader.poll_tier:
            continue
        if request.identifier in seen_pids:
            continue
        if _is_request_suppressed(state, now) or state.next_due_at > now:
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
) -> float | None:
    due_times = [
        state.next_due_at
        for index, (request, state) in enumerate(zip(requests, states, strict=True))
        if not _is_request_suppressed(state, now) and (eligible is None or eligible(index, request))
    ]
    return min(due_times) if due_times else None


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
    for request, state in zip(requests, states, strict=True):
        backed = _is_request_backed_off(state, now)
        request_rows.append(
            {
                "kind": request.kind,
                "service": f"0x{request.service:02X}",
                "identifier": f"0x{request.identifier:04X}"
                if request.identifier > 0xFF
                else f"0x{request.identifier:02X}",
                "channel": request.channel,
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
    )


__all__ = [
    "select_batch_companions",
    "is_batchable",
    "AUTO_MISS_BACKOFF_SECONDS",
    "AUTO_PIDS",
    "DEFAULT_DID_REQUEST_ID",
    "DEFAULT_PRIORITY_CHANNELS",
    "MAX_MISS_BACKOFF_SECONDS",
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
