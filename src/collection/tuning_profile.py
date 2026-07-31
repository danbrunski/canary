"""Process-wide runtime gate for the active capture profile.

The historic name remains as a compatibility import.  State is deliberately
ephemeral: daemon startup always initializes ``default`` and capture state is
never read back from a session sidecar.
"""

from __future__ import annotations

import logging
import threading
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
_session_dir: str | None = None
_source = "default"
_did_channels: dict[str, str] = {}
_configured = False


def configure_default(profile: CaptureProfile) -> None:
    """Initialize process state; always called as a non-persistent reset."""
    if profile.name != "default":
        raise ValueError("configure_default requires the default capture profile")
    global _configured, _profile, _started_at, _session_dir, _source
    with _lock:
        _profile = profile
        _started_at = None
        _session_dir = None
        _source = "startup"
        _configured = True


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
    global _profile, _started_at, _session_dir, _source
    with _lock:
        previous = _profile
        _profile = profile
        _started_at = None if profile.name == "default" else datetime.now(UTC).isoformat()
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


def channels() -> frozenset[str]:
    with _lock:
        return frozenset(_profile.mode01)


def mode22() -> frozenset[str]:
    with _lock:
        return frozenset(_profile.mode22)


def broadcast_decode_enabled() -> bool:
    with _lock:
        return _profile.broadcast_decode


def set_active(
    enabled: bool,
    *,
    channel_names: frozenset[str] | None = None,
    source: str = "control",
) -> bool:
    """Compatibility shim for the old boolean tuning-mode endpoint."""
    previous = is_active()
    if enabled:
        activate(
            CaptureProfile(
                name="fuel-map",
                description="Legacy tuning-mode activation",
                mode01={name: 1.0 for name in (channel_names or DEFAULT_TUNING_CHANNELS)},
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
    kind = getattr(request, "kind", None)
    if kind == "dtc":
        return True
    channel = str(getattr(request, "channel", ""))
    with _lock:
        profile = _profile
        if kind == "did":
            requested = {item.upper() for item in profile.mode22}
            resolved = {_did_channels.get(item, item) for item in requested}
            return channel.upper() in resolved or channel.removeprefix("DID_").upper() in resolved
        return channel.upper() in profile.mode01 or "AUTO" in profile.mode01


def rate_weight(channel: str) -> float:
    with _lock:
        return _profile.mode01.get(channel.upper(), 1.0)


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
    "configure_default",
    "configure_did_aliases",
    "is_active",
    "is_configured",
    "mode22",
    "rate_weight",
    "request_eligible",
    "set_active",
    "set_session_dir",
    "snapshot",
]
