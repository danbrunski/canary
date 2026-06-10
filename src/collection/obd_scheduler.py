"""Adaptive OBD request scheduling for the Canable-native collector."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from src.collection.can_obd_client import DEFAULT_REQUEST_ID, PID_DECODERS
from src.collection.obd_registry import PollTier, lookup
from src.config import CanBusConfig, CollectionConfig

if TYPE_CHECKING:
    from src.decode.dids import DidAccess, DidMap

AUTO_PIDS = "auto"
DEFAULT_DID_REQUEST_ID = 0x7E0
AUTO_MISS_BACKOFF_SECONDS = 30.0
BUS_BUDGET_MULTIPLIER = 8.0
MIN_BUS_BUDGET_REQUESTS_PER_SECOND = 2.0
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


def did_access() -> DidAccess | None:
    """The access block of data/dids.yaml (gating session, keep-alive)."""
    did_map = _load_did_map()
    return None if did_map is None else did_map.access


class _PidDiscoveryClient(Protocol):
    def query_supported_mode_01_pids(self) -> tuple[int, ...]: ...


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
    did_request_id: int = DEFAULT_DID_REQUEST_ID
    request_timeout_seconds: float = 1.0
    #: ``None`` = poll DIDs bare, interleaved with Mode 01 (ECUs that don't
    #: gate 0x22). Set = scheduled mutually-exclusive session windows.
    did_session: DidSessionSpec | None = None


@dataclass
class CanObdLoopStats:
    frames_seen: int = 0
    parse_errors: int = 0
    requests_sent: int = 0
    readings_emitted: int = 0
    request_timeouts: int = 0
    bus_errors: int = 0
    scheduler_snapshots: int = 0
    session_controls_sent: int = 0


@dataclass(frozen=True)
class _PollRequest:
    kind: Literal["pid", "did", "session"]
    service: int
    identifier: int
    request_id: int
    channel: str
    poll_tier: PollTier = "medium"
    auto_probe: bool = False
    fast_probe: bool = False


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
        self._next_window_at = now + spec.window_interval_seconds
        self._window_deadline = 0.0
        self._lapse_until = 0.0
        self._last_session_sent = 0.0
        self._pending: set[int] = set()
        self.windows_opened = 0
        self.windows_completed = 0
        self.session_failures = 0
        self.keepalives_sent = 0

    def advance(self, states: Sequence[_PollRequestState], now: float) -> None:
        """Apply phase transitions that don't require bus I/O."""
        if self.phase == "window":
            live = any(states[i].suppressed_until <= now for i in self._pending)
            if now >= self._window_deadline or not live:
                self._close_window(now)
        elif self.phase == "lapse" and now >= self._lapse_until:
            self.phase = "mode01"

    def session_control_due(self, states: Sequence[_PollRequestState], now: float) -> bool:
        """Whether the loop should send ``10 <session>`` now."""
        if self.phase == "mode01":
            if now < self._next_window_at:
                return False
            if all(states[i].suppressed_until > now for i in self._did_indices):
                # Every DID is backed off; don't burn a window on them.
                self._next_window_at = now + self._spec.window_interval_seconds
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
                self._next_window_at = now + self._spec.window_interval_seconds
        elif self.phase == "window":
            if ok:
                self.keepalives_sent += 1
            else:
                # Session lost mid-window: let it lapse, resume Mode 01.
                self.session_failures += 1
                self._close_window(now)

    def request_eligible(self, index: int, request: _PollRequest, *, now: float) -> bool:
        del now  # phase state already advanced this tick
        if self.phase == "window":
            return request.kind == "did" and index in self._pending
        if self.phase == "lapse":
            return False
        return request.kind == "pid"

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
        self._next_window_at = now + self._spec.window_interval_seconds
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
            "window_interval_seconds": self._spec.window_interval_seconds,
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
    bus_errors: int
    response_rate: float | None
    by_tier: dict[str, dict[str, float | int | None]]
    requests: list[dict[str, Any]] = field(default_factory=list)
    #: Mode-22 session-window state, ``None`` when no session gates DIDs.
    did_profile: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured_poll_rate_hz": self.configured_poll_rate_hz,
            "bus_budget_requests_per_second": self.bus_budget_requests_per_second,
            "active_requests": self.active_requests,
            "backed_off_requests": self.backed_off_requests,
            "requests_sent": self.requests_sent,
            "readings_emitted": self.readings_emitted,
            "request_timeouts": self.request_timeouts,
            "bus_errors": self.bus_errors,
            "response_rate": self.response_rate,
            "by_tier": self.by_tier,
            "requests": self.requests,
            "did_profile": self.did_profile,
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

    requests = [
        _PollRequest(
            kind="pid",
            service=0x01,
            identifier=pid,
            request_id=DEFAULT_REQUEST_ID,
            channel=PID_DECODERS[pid][0],
            poll_tier=_poll_tier_for_pid(pid),
            auto_probe=auto_probe_by_pid[pid],
            fast_probe=fast_probe_by_pid[pid],
        )
        for pid in pids
    ]
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
    return requests


def _poll_tier_for_pid(pid: int) -> PollTier:
    definition = lookup(0x01, pid)
    if definition is None:
        return "medium"
    return definition.poll_tier


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
    state.suppressed_until = now + AUTO_MISS_BACKOFF_SECONDS
    state.backoffs += 1


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
    base = max(float(spec.poll_rate_hz), 0.001)
    return base * TIER_RATE_FACTORS.get(request.poll_tier, TIER_RATE_FACTORS["medium"])


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
    _due_at, index, request, state = min(candidates, key=lambda item: (item[0], item[1]))
    return index, request, state


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
                "configured_rate_hz": round(_request_rate_hz(request, spec), 4),
                "requests_sent": state.requests_sent,
                "responses": state.responses,
                "timeouts": state.timeouts,
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
        bus_budget_requests_per_second=round(_bus_budget_requests_per_second(spec), 4),
        active_requests=active,
        backed_off_requests=backed_off,
        requests_sent=stats.requests_sent,
        readings_emitted=stats.readings_emitted,
        request_timeouts=stats.request_timeouts,
        bus_errors=stats.bus_errors,
        response_rate=(
            round(stats.readings_emitted / stats.requests_sent, 4) if stats.requests_sent else None
        ),
        by_tier=by_tier,
        requests=request_rows,
        did_profile=did_profile,
    )


__all__ = [
    "AUTO_MISS_BACKOFF_SECONDS",
    "AUTO_PIDS",
    "DEFAULT_DID_REQUEST_ID",
    "CanObdLoopStats",
    "CanObdSpec",
    "DidSessionSpec",
    "DidWindowController",
    "ObdSchedulerMetrics",
    "build_poll_requests",
    "did_access",
    "parse_did_token",
    "parse_pid_token",
    "resolve_did_session_spec",
]
