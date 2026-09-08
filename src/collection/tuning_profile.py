"""Process-wide runtime gate for the active capture profile.

The historic name remains as a compatibility import.  State is deliberately
ephemeral: daemon startup always initializes ``default`` and capture state is
never read back from a session sidecar.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

from src.collection import capture_generation, capture_lifetime
from src.collection.capture_profiles import FUEL_MAP_CHANNELS, CaptureProfile
from src.collection.request_eligibility import Eligibility, ProfileClass, evaluate

log = logging.getLogger(__name__)
DEFAULT_TUNING_CHANNELS: frozenset[str] = frozenset(FUEL_MAP_CHANNELS)

_lock = threading.Lock()
_profile = CaptureProfile(
    name="default",
    description="Normal collection",
    mode01={name: 1.0 for name in DEFAULT_TUNING_CHANNELS},
    corpus_eligible=True,
)
_started_at: str | None = None
_started_monotonic: float | None = None
_session_dir: str | None = None
_source = "default"
_did_channels: dict[str, str] = {}
_configured = False
#: Polling suspended while a capture stays open. Process state, like the
#: active profile: a restart resumes collecting rather than coming back up
#: silently paused.
_paused = False


def configure_default(profile: CaptureProfile) -> None:
    """Initialize process state; always called as a non-persistent reset."""
    if profile.name != "default":
        raise ValueError("configure_default requires the default capture profile")
    global _configured, _profile, _started_at, _started_monotonic, _session_dir, _source, _paused
    with _lock:
        _profile = profile
        _started_at = None
        _started_monotonic = None
        _session_dir = None
        _source = "startup"
        _configured = True
        _paused = False
    capture_lifetime.end()


def is_configured() -> bool:
    with _lock:
        return _configured


def configure_did_aliases(aliases: dict[str, str]) -> None:
    global _did_channels
    with _lock:
        _did_channels = {name.upper(): channel.upper() for name, channel in aliases.items()}


def active_profile() -> CaptureProfile:
    with _lock:
        return _profile.model_copy(deep=True)


def activate(
    profile: CaptureProfile,
    *,
    session_dir: str | None = None,
    source: str = "control",
) -> CaptureProfile:
    global _profile, _started_at, _started_monotonic, _session_dir, _source, _paused
    with _lock:
        previous = _profile
        _profile = profile
        _paused = False
        _started_at = None if profile.name == "default" else datetime.now(UTC).isoformat()
        _started_monotonic = None if profile.name == "default" else time.monotonic()
        _session_dir = session_dir
        _source = source
    # A new profile is a new capture generation (C02.8): rounds restart, and
    # a request issued under the old profile keeps the old tag.
    generation = capture_generation.bump(f"profile:{profile.name}")
    # The capture-lifetime ledger (C03.2) starts and ends with the profile.
    if _started_monotonic is None:
        capture_lifetime.end()
    else:
        capture_lifetime.begin(
            _started_monotonic,
            profile=profile.name,
            generation=generation,
            started_wall=time.time(),
        )
    log.warning(
        "capture profile activated: %s",
        profile.name,
        extra={"capture_profile": profile.name, "source": source},
    )
    return previous


def set_session_dir(path: str | None) -> None:
    global _session_dir
    with _lock:
        _session_dir = path


def is_active() -> bool:
    """Legacy meaning: a non-default focused profile is active."""
    with _lock:
        return _profile.name != "default"


def capture_started_monotonic() -> float | None:
    """Monotonic start of the active bounded capture, if any.

    Scheduler reads use monotonic time, so this is the epoch that can exclude
    pre-arm samples without a wall-clock conversion or directory-name guess.
    """
    with _lock:
        return _started_monotonic


def channels() -> frozenset[str]:
    with _lock:
        return frozenset(_profile.mode01) if _profile.mode01_enabled else frozenset()


def mode22() -> frozenset[str]:
    """The DIDs to poll — empty while Mode 22 is switched off.

    Callers use this to decide whether to run the diagnostic-session window
    at all, so a disabled service must read as "no DIDs" rather than as a
    populated-but-inert list.
    """
    with _lock:
        return frozenset(_profile.mode22) if _profile.mode22_enabled else frozenset()


def set_paused(value: bool) -> bool:
    """Suspend/resume polling without ending the capture. Returns the previous state."""
    global _paused
    with _lock:
        previous = _paused
        _paused = value
    if previous != value:
        capture_generation.bump("capture-paused" if value else "capture-resumed")
        # Recorded as a pause window (C03.4) so coverage can exclude it by
        # name rather than by guessing from a gap.
        capture_lifetime.mark_paused(value, reason="operator", now=time.monotonic())
    log.warning("capture %s", "paused" if value else "resumed", extra={"paused": value})
    return previous


def is_paused() -> bool:
    with _lock:
        return _paused


def round_timestamps_enabled() -> bool:
    """Whether readings should carry their capture round's start time."""
    with _lock:
        return _profile.round_timestamps


def required_tuple() -> frozenset[str]:
    """Upper-cased ``required_tuple`` of the active profile; empty when none.

    The scheduler serves these members of a round first and batches them
    together (C02.5), so the tuple completes before the budget runs out.
    """
    with _lock:
        return frozenset(name.upper() for name in _profile.required_tuple)


def did_interval_override() -> float | None:
    """Profile's Mode-22 window cadence, or ``None`` to use the config default."""
    with _lock:
        return _profile.did_interval_seconds


def did_cycle_override() -> float | None:
    """Profile's start-to-start hybrid cycle (C04.3), or ``None`` for the config policy."""
    with _lock:
        return _profile.did_cycle_seconds


def broadcast_decode_enabled() -> bool:
    with _lock:
        return _profile.broadcast_decode


def set_active(
    enabled: bool,
    *,
    channel_names: frozenset[str] | None = None,
    rate_hz: float | None = None,
    source: str = "control",
) -> bool:
    """Compatibility shim for the old boolean tuning-mode endpoint.

    ``rate_hz`` pins every selected channel to that absolute rate. Without it
    the profile keeps the relative model, which caps each channel at
    ``poll_rate_hz`` — 1 Hz as configured, regardless of bus budget.
    """
    previous = is_active()
    if enabled:
        activate(
            CaptureProfile(
                name="fuel-map",
                description="Legacy tuning-mode activation",
                mode01={name: 1.0 for name in (channel_names or DEFAULT_TUNING_CHANNELS)},
                target_rate_hz=rate_hz,
                corpus_eligible=False,
                storage_prefix="_capture-fuel-map",
            ),
            source=source,
        )
    else:
        activate(
            CaptureProfile(
                name="default",
                description="Normal collection",
                mode01={name: 1.0 for name in DEFAULT_TUNING_CHANNELS},
                corpus_eligible=True,
            ),
            source=source,
        )
    return previous


def request_eligible(index: int, request: Any) -> bool:
    """The profile gate: is this request selected by the active profile right now?

    A projection of the C06.7 eligibility matrix with no session knowledge
    (phase ``none``); the Mode-22 controller supplies the phase from its
    side and the loop combines both. Policy lives in
    ``request_eligibility``, not here.
    """
    del index
    return request_verdict(request).eligible


def request_verdict(request: Any) -> Eligibility:
    """The profile gate's verdict with its reason (``paused``, ``not_selected`` …)."""
    # A request without a kind is a Mode 01 PID (the legacy shape callers
    # still pass); nothing else is guessed.
    kind = str(getattr(request, "kind", None) or "pid")
    channel = str(getattr(request, "channel", ""))
    with _lock:
        profile = _profile
        paused = _paused
        aliases = _did_channels
    return evaluate(
        kind,
        classify_profile(profile),
        "none",
        selection=_selection_exclusion(profile, aliases, kind, channel),
        paused=paused,
    )


def classify_profile(profile: CaptureProfile) -> ProfileClass:
    """Which eligibility-matrix column a profile falls in."""
    if profile.did_continuous:
        return "continuous_did"
    if profile.name == "default":
        return "default"
    if profile.mode22 and profile.mode22_enabled:
        return "mode22_owning"
    return "focused_mode01"


def profile_class() -> ProfileClass:
    with _lock:
        return classify_profile(_profile)


def selection_exclusion(kind: str, channel: str) -> str | None:
    """Why the active profile's selection excludes this channel, or ``None``."""
    with _lock:
        return _selection_exclusion(_profile, _did_channels, kind, channel)


def _selection_exclusion(
    profile: CaptureProfile, aliases: dict[str, str], kind: str, channel: str
) -> str | None:
    if kind == "did":
        if not profile.mode22_enabled:
            return "mode22_disabled"
        requested = {item.upper() for item in profile.mode22}
        resolved = {aliases.get(item, item) for item in requested}
        upper = channel.upper()
        if upper in resolved or channel.removeprefix("DID_").upper() in resolved:
            return None
        return "not_selected"
    if not profile.mode01_enabled:
        return "mode01_disabled"
    if channel.upper() in profile.mode01 or "AUTO" in profile.mode01:
        return None
    return "not_selected"


def profile_channel_for(channel: str) -> str | None:
    """The name the *active profile* uses for a scheduler request channel.

    The scheduler reports DIDs by identifier (``DID_1209``) while a profile
    names them semantically (``ECM_MAF_GPS``), and ``request_eligible`` already
    resolves between the two. Anything else that has to match a profile's
    channel list against live scheduler rows needs the same mapping — and
    re-deriving it is how the capture contract came to report four DIDs as
    "never answered" while they were visibly answering.

    Returns the profile's own name, or ``None`` when this request is not part
    of the active profile's selection.
    """
    upper = channel.upper()
    stripped = channel.removeprefix("DID_").upper()
    with _lock:
        # Mode 01 needs no translation: scheduler and profile use the same PID
        # channel name. "AUTO" selects every supported PID, so the request's own
        # name is the profile's name for it.
        if upper in _profile.mode01 or "AUTO" in _profile.mode01:
            return upper
        for item in _profile.mode22:
            resolved = _did_channels.get(item.upper(), item.upper())
            if upper in {resolved, item.upper()} or stripped in {resolved, item.upper()}:
                return item
    return None


def rate_weight(channel: str) -> float:
    with _lock:
        return _profile.mode01.get(channel.upper(), 1.0)


def priority_channel(channel: str, default: bool) -> bool:
    """Return the active profile's priority decision for one request.

    ``None`` inherits the scheduler/config decision. An explicit tuple,
    including an empty one, owns the lane for the active profile.
    """
    profile_name = profile_channel_for(channel)
    with _lock:
        configured = _profile.priority_channels
        if configured is None:
            return default
        candidates = {channel.upper()}
        if profile_name is not None:
            candidates.add(profile_name.upper())
        return bool(candidates & set(configured))


def did_rate_weight(channel: str) -> float:
    """Relative cadence for one selected DID, resolving profile aliases.

    Profiles key weights by the name they persist (for example
    ``ECM_AF_SENSOR_V_B1``), while the scheduler uses ``DID_1225``. Resolve
    through the same alias table as selection and profile-channel reporting so
    a renamed DID cannot silently lose its configured cadence.
    """
    upper = channel.upper()
    stripped = channel.removeprefix("DID_").upper()
    with _lock:
        for item in _profile.mode22:
            item_upper = item.upper()
            resolved = _did_channels.get(item_upper, item_upper)
            if upper in {resolved, item_upper} or stripped in {resolved, item_upper}:
                return _profile.mode22_rate_weights.get(item_upper, 1.0)
    return 1.0


def did_continuous_enabled() -> bool:
    """Whether the active profile pins the Mode-22 session open."""
    with _lock:
        return _profile.did_continuous


def absolute_rate_hz(channel: str) -> float | None:
    """The profile's fixed poll rate for ``channel``, or ``None``.

    When set it replaces ``poll_rate_hz × tier`` outright, but **keeps the
    profile's own weight** as a per-channel multiplier: the target is the rate
    for a weight-1.0 channel, and a weight of 0.25 asks for a quarter of it.

    That the weight survives is the point. Without it a target rate flattened
    every channel to one cadence, so a capture wanting a WOT transient at 4 Hz
    also dragged coolant, trims and purge along at 4 Hz — spending most of the
    budget on channels that move over seconds to minutes, and forcing the
    "split it into two profiles" workaround. A single profile can now say
    "these fast, those slow" in the structure it already had, where the weights
    were previously accepted and then silently ignored.

    Only applies to channels the profile actually lists, so a profile carrying
    a target rate never accelerates something it did not ask for.

    Covers Mode-22 DIDs as well as Mode-01 PIDs: an all-DID capture is
    exactly the case where the rate has to be sayable, and DID names are
    matched through the same alias resolution ``request_eligible`` uses.
    """
    with _lock:
        target = _profile.target_rate_hz
        if target is None:
            return None
        name = channel.upper()
        weight = _profile.mode01.get(name)
        if weight is not None:
            return target * weight
        stripped = channel.removeprefix("DID_").upper()
        for item in _profile.mode22:
            item_upper = item.upper()
            resolved = _did_channels.get(item_upper, item_upper)
            if name in {resolved, item_upper} or stripped in {resolved, item_upper}:
                return target * _profile.mode22_rate_weights.get(item_upper, 1.0)
        return None


def rate_policy(channel: str) -> tuple[float, bool]:
    """``(rate_weight, uniform_cadence)`` for one channel, under one lock.

    The poll loop asks per request, so this is a hot path; taking the lock
    once beats two round trips for two fields of the same profile.
    """
    with _lock:
        return _profile.mode01.get(channel.upper(), 1.0), _profile.round_timestamps


def snapshot(achieved_hz: dict[str, float] | None = None) -> dict[str, Any]:
    """The active profile plus its runtime state, as a plain payload.

    Every profile field is emitted by dumping the model rather than by naming
    fields one at a time. That enumeration was the bug it replaces: the payload
    is what ``capture_quality.evaluate_capture_quality`` scores a capture
    against and what the manifest stores, so a field nobody remembered to add
    here was not a cosmetic omission — it silently switched off the check that
    depended on it. Adding a field to :class:`CaptureProfile` now carries it
    through automatically.
    """
    with _lock:
        payload: dict[str, Any] = {
            **_profile.model_dump(mode="json"),
            "active": _profile.name != "default",
            "started_at": _started_at,
            "session_dir": _session_dir,
            "source": _source,
            # Kept alongside `mode01` for consumers that only want the names.
            "channels": sorted(_profile.mode01),
            "paused": _paused,
        }
    if achieved_hz is not None:
        payload["achieved_hz"] = achieved_hz
    return payload


__all__ = [
    "DEFAULT_TUNING_CHANNELS",
    "activate",
    "active_profile",
    "broadcast_decode_enabled",
    "channels",
    "capture_started_monotonic",
    "did_continuous_enabled",
    "did_cycle_override",
    "did_rate_weight",
    "configure_default",
    "configure_did_aliases",
    "is_active",
    "is_configured",
    "is_paused",
    "mode22",
    "absolute_rate_hz",
    "classify_profile",
    "profile_class",
    "priority_channel",
    "rate_weight",
    "request_eligible",
    "request_verdict",
    "selection_exclusion",
    "required_tuple",
    "set_active",
    "set_session_dir",
    "snapshot",
]
