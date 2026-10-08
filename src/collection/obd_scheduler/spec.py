"""What the OBD scheduler is configured with: ``CanObdSpec`` (and its one config
builder), the Mode-22 session spec, the triggers, and the catalog lookups.

Part of the ``obd_scheduler`` package (R02.10).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from src.collection.calibration_id import CalibrationIdSpec, resolve_calibration_id_spec
from src.collection.can_obd_client import PID_DECODERS
from src.collection.custom_pids import load_custom_pids
from src.collection.discovery import (
    MODE01_SUPPORT_GROUPS,
    MODE06_SUPPORT_GROUPS,
    DiscoveryItem,
    vehicle_key,
)
from src.collection.isotp import PERMANENT_REFUSAL_NRCS
from src.config import DEFAULT_PRIORITY_CHANNELS, CanBusConfig, CollectionConfig, VehicleConfig
from src.decode.dids import (
    _did_ids_by_name,
    did_access,
    did_response_widths,
    known_did_names,
    memory_channels,
    select_memory_channels,
)

if TYPE_CHECKING:
    from src.collection.capture_profiles import CaptureProfile
    from src.collection.custom_pids import CustomPid
    from src.collection.discovery import SupportProbe
    from src.decode.dids import MemoryChannel


log = logging.getLogger(__name__)


AUTO_PIDS = "auto"


#: Request kinds the ECM answers only inside the Mode-22 session window.
#: One name for the set so the window controller, the request plan and
#: the eligibility matrix cannot disagree about who is in it.
SESSION_WINDOW_KINDS: frozenset[str] = frozenset({"did", "memory"})


DEFAULT_DID_REQUEST_ID = 0x7E0


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
    are matched case-insensitively against ``PollRequest.channel``
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


_PID_IDS_BY_NAME: dict[str, int] = {name: pid for pid, (name, _decoder) in PID_DECODERS.items()}


# The DID-catalog accessors live in ``src.decode.dids`` (R01.2); imported here
# for the spec builder and re-exported from the package for its callers.


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

    @property
    def silent(self) -> bool:
        """No reply at all — unlike an NRC, the ECU's session state is unknown.

        A lost or late ``50 <session>`` looks exactly like this while the ECU
        *did* switch sessions (D03.18), so a silent entry must not be read as
        "still in the default session".
        """
        return not self.ok and self.negative_response_code is None


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
    #: Max Mode 01 PIDs to pack into one request (roadmap/archive/multi-pid-polling.md).
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
    #: Mode-22 batching is independently scoped: support for Mode 01 batches
    #: says nothing about a UDS session. 1 disables it.
    did_batch_max_dids: int = 1
    did_response_widths: Mapping[int, int] = field(default_factory=dict)
    did_response_width_sources: Mapping[int, str] = field(default_factory=dict)
    #: User-defined custom PIDs (Task 2.6) loaded once at daemon startup via
    #: ``custom_pids.load_custom_pids`` and threaded straight through — the
    #: scheduler never re-reads the YAML itself. Empty by default (no
    #: ``data/custom_pids.yaml`` is the common case).
    custom_pids: tuple[CustomPid, ...] = ()
    #: Service-0x23 channels, already filtered to those a capture profile
    #: names by the caller. Threaded through
    #: like ``custom_pids``; the scheduler never reads the catalog itself.
    memory: tuple[MemoryChannel, ...] = ()
    #: Channels that win a contended request slot while due, so they hold
    #: their tier rate instead of degrading with the rest of the plan. See
    #: ``DEFAULT_PRIORITY_CHANNELS``; empty disables the priority lane.
    priority_channels: tuple[str, ...] = DEFAULT_PRIORITY_CHANNELS
    discovery_budget_requests_per_second: float = 32.0
    #: Vehicle identity the discovery cache is keyed by (C06.6): the VIN, else
    #: make-model-year (``discovery.vehicle_key``). ``None`` — the default,
    #: and every standalone recorder/test spec — caches nothing, so a walk
    #: restarts with each loop.
    discovery_key: str | None = None
    #: The ECU calibration-id read (W02.2), resolved from the dids.yaml
    #: access block. ``None`` — the default, and every standalone spec —
    #: sends no identification request at all.
    calibration_id: CalibrationIdSpec | None = None

    @classmethod
    def from_config(
        cls,
        collection: CollectionConfig,
        can: CanBusConfig,
        *,
        catalog: Sequence[CaptureProfile] | None = None,
        pids: Sequence[str] | None = None,
        dids: Sequence[str] | None = None,
        did_request_id: int | None = None,
        custom_pids_path: Path | None = None,
        vehicle: VehicleConfig | None = None,
        freeze_frame_trigger: FreezeFrameTrigger | None = None,
        force_poll_trigger: ForcePollTrigger | None = None,
    ) -> CanObdSpec:
        """The one config -> spec builder (R02.7): the daemon, the recorder
        and the capture-profile preview all build their spec here.

        ``catalog`` is the saved capture profiles. With one, the request plan
        is their union — every profile's Mode 01 channels (unless ``auto``
        already covers them), DIDs plus the catalogued inventory, and memory
        channels — and the process-wide active-profile gate filters it
        (``capture_profiles_enabled``). Without one (the recorder) the plan is
        the config's own selection, polled ungated.

        ``pids``/``dids``/``did_request_id`` override the config's selection
        (the recorder's CLI flags). Everything else — batch sizes, DID
        response widths, priority lane, DTC/monitor cadence, monitor MIDs,
        custom PIDs, the calibration-id read, the discovery cache key — comes
        from config and the vehicle's data files, so no caller can forget one.
        """
        profiles = tuple(catalog or ())
        request_pids = tuple(collection.obd_pids if pids is None else pids)
        request_dids: tuple[str, ...] = tuple(collection.obd_dids if dids is None else dids)
        memory: tuple[MemoryChannel, ...] = ()
        if catalog is not None:
            if not any(token.strip().lower() == AUTO_PIDS for token in request_pids):
                # ``auto`` already discovers every supported Mode-01 channel;
                # an explicit plan is expanded to the saved-profile union.
                request_pids = tuple(
                    dict.fromkeys(
                        [
                            *request_pids,
                            *(channel for profile in profiles for channel in profile.mode01),
                        ]
                    )
                )
            request_dids = tuple(
                dict.fromkeys([*request_dids, *profile_did_union(collection, profiles)])
            )
            # RAM addresses belong to the ECU's program code, which the
            # flashes made on this vehicle do not change (operator,
            # 2026-09-21), so channels are not gated on the running tune
            # revision; the known-answer verify is the moved-address check.
            memory = profile_memory_union(profiles)
        widths, width_sources = did_response_widths()
        custom: tuple[CustomPid, ...] = ()
        if custom_pids_path is not None:
            custom = tuple(load_custom_pids(custom_pids_path).custom_pids)
        return cls(
            can=can,
            poll_rate_hz=collection.obd_poll_rate_hz,
            pids=request_pids,
            dids=request_dids,
            capture_profiles_enabled=catalog is not None,
            batch_max_pids=collection.obd_batch_max_pids,
            did_batch_max_dids=collection.obd_did_batch_max_dids,
            did_response_widths=widths,
            did_response_width_sources=width_sources,
            did_request_id=(
                collection.obd_did_request_id if did_request_id is None else did_request_id
            ),
            request_timeout_seconds=collection.obd_request_timeout_seconds,
            did_session=resolve_did_session_spec(collection),
            dtc_poll_interval_seconds=collection.obd_dtc_poll_interval_seconds,
            service_poll_interval_seconds=collection.obd_monitor_poll_interval_seconds,
            service_mids=collection.obd_monitor_mids,
            freeze_frame_trigger=freeze_frame_trigger,
            force_poll_trigger=force_poll_trigger,
            custom_pids=custom,
            memory=memory,
            priority_channels=collection.obd_priority_channels,
            discovery_budget_requests_per_second=(
                collection.obd_discovery_budget_requests_per_second
            ),
            # Support maps cached per vehicle across reconnects (C06.6).
            discovery_key=(
                None
                if vehicle is None
                else vehicle_key(vehicle.vin, vehicle.make, vehicle.model, vehicle.year)
            ),
            # W02.2: the ECU's own calibration id, read per the vehicle's
            # dids.yaml access block (which session it needs is data).
            calibration_id=resolve_calibration_id_spec(did_access()),
        )


def profile_did_union(
    collection: CollectionConfig, profiles: Sequence[CaptureProfile]
) -> tuple[str, ...]:
    """Every DID token a capture-profile-gated plan carries, in order.

    The config's DIDs, the catalogued inventory and every saved profile's
    Mode 22 selection. Computed once here: ``default_collectors`` used to
    spell it twice (the request plan and the alias table).
    """
    return tuple(
        dict.fromkeys(
            [
                *collection.obd_dids,
                *known_did_names(),
                *(did for profile in profiles for did in profile.mode22),
            ]
        )
    )


def profile_memory_union(profiles: Sequence[CaptureProfile]) -> tuple[MemoryChannel, ...]:
    """Every catalog memory channel a saved profile selects, in catalog order.

    Refuses a profile naming a channel the catalog does not hold, the way
    ``parse_did_token`` refuses an unknown DID, so the daemon start, the
    capture-profile preview and capture start all fail on it by name. This
    used to filter the catalog by the selection, which dropped a stale name
    without a word: the profile ran with that channel never polled.
    """
    selected: set[str] = set()
    for profile in profiles:
        try:
            chosen = select_memory_channels(profile.memory)
        except ValueError as exc:
            raise ValueError(f"capture profile {profile.name!r}: {exc}") from exc
        selected.update(channel.name for channel in chosen)
    return tuple(channel for channel in memory_channels() if channel.name in selected)


def did_alias_table(tokens: Iterable[str]) -> dict[str, str]:
    """Profile DID token -> scheduler channel (``DID_1209``), for the profile gate."""
    return {token.upper(): f"DID_{parse_did_token(token):04X}" for token in dict.fromkeys(tokens)}


def discovery_items(spec: CanObdSpec) -> dict[DiscoveryItem, tuple[int, tuple[int, ...]]]:
    """Which support walks this spec owes: ``item -> (service, groups)``."""
    wanted: dict[DiscoveryItem, tuple[int, tuple[int, ...]]] = {}
    if any(parse_pid_token(token) is None for token in (spec.pids or (AUTO_PIDS,))):
        wanted["mode01"] = (0x01, MODE01_SUPPORT_GROUPS)
    if spec.service_poll_interval_seconds > 0 and spec.service_mids == "auto":
        wanted["mode06"] = (0x06, MODE06_SUPPORT_GROUPS)
    return wanted


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


def _bus_budget_requests_per_second(spec: CanObdSpec) -> float:
    base = max(float(spec.poll_rate_hz), 0.001)
    return max(MIN_BUS_BUDGET_REQUESTS_PER_SECOND, base * BUS_BUDGET_MULTIPLIER)
