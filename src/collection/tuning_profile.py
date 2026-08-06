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

from src.collection.capture_profiles import FUEL_MAP_CHANNELS, CaptureProfile

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
    log.warning("capture %s", "paused" if value else "resumed", extra={"paused": value})
    return previous


def is_paused() -> bool:
    with _lock:
        return _paused


def round_timestamps_enabled() -> bool:
    """Whether readings should carry their capture round's start time."""
    with _lock:
        return _profile.round_timestamps


def did_interval_override() -> float | None:
    """Profile's Mode-22 window cadence, or ``None`` to use the config default."""
    with _lock:
        return _profile.did_interval_seconds


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
    del index
    with _lock:
        if _paused:
            # A paused capture stops polling outright rather than discarding
            # answers: no point spending bus budget on rows nobody wants.
            return False
    kind = getattr(request, "kind", None)
    if kind == "dtc":
        return True
    channel = str(getattr(request, "channel", ""))
    with _lock:
        profile = _profile
        if kind == "did":
            if not profile.mode22_enabled:
                return False
            requested = {item.upper() for item in profile.mode22}
            resolved = {_did_channels.get(item, item) for item in requested}
            return channel.upper() in resolved or channel.removeprefix("DID_").upper() in resolved
        if not profile.mode01_enabled:
            return False
        return channel.upper() in profile.mode01 or "AUTO" in profile.mode01


def rate_weight(channel: str) -> float:
    with _lock:
        return _profile.mode01.get(channel.upper(), 1.0)


def did_continuous_enabled() -> bool:
    """Whether the active profile pins the Mode-22 session open."""
    with _lock:
        return _profile.did_continuous


def absolute_rate_hz(channel: str) -> float | None:
    """The profile's fixed poll rate for ``channel``, or ``None``.

    When set it replaces ``poll_rate_hz × tier × weight`` outright. Only
    applies to channels the profile actually lists, so a profile carrying a
    target rate never accelerates something it did not ask for.

    Covers Mode-22 DIDs as well as Mode-01 PIDs: an all-DID capture is
    exactly the case where the rate has to be sayable, and DID names are
    matched through the same alias resolution ``request_eligible`` uses.
    """
    with _lock:
        target = _profile.target_rate_hz
        if target is None:
            return None
        name = channel.upper()
        if name in _profile.mode01:
            return target
        requested = {item.upper() for item in _profile.mode22}
        resolved = {_did_channels.get(item, item) for item in requested}
        if name in resolved or channel.removeprefix("DID_").upper() in resolved:
            return target
        return None


def rate_policy(channel: str) -> tuple[float, bool]:
    """``(rate_weight, uniform_cadence)`` for one channel, under one lock.

    The poll loop asks per request, so this is a hot path; taking the lock
    once beats two round trips for two fields of the same profile.
    """
    with _lock:
        return _profile.mode01.get(channel.upper(), 1.0), _profile.round_timestamps


def snapshot(achieved_hz: dict[str, float] | None = None) -> dict[str, Any]:
    with _lock:
        payload: dict[str, Any] = {
            "active": _profile.name != "default",
            "name": _profile.name,
            "started_at": _started_at,
            "session_dir": _session_dir,
            "source": _source,
            "channels": sorted(_profile.mode01),
            "mode01": dict(_profile.mode01),
            "mode22": list(_profile.mode22),
            "mode01_enabled": _profile.mode01_enabled,
            "mode22_enabled": _profile.mode22_enabled,
            "did_interval_seconds": _profile.did_interval_seconds,
            "round_timestamps": _profile.round_timestamps,
            "target_rate_hz": _profile.target_rate_hz,
            "did_continuous": _profile.did_continuous,
            "paused": _paused,
            "broadcast_decode": _profile.broadcast_decode,
            "corpus_eligible": _profile.corpus_eligible,
            "bus_budget_override": _profile.bus_budget_override,
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
    "configure_default",
    "configure_did_aliases",
    "is_active",
    "is_configured",
    "mode22",
    "absolute_rate_hz",
    "rate_weight",
    "request_eligible",
    "set_active",
    "set_session_dir",
    "snapshot",
]
