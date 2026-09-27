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
from typing import TYPE_CHECKING, Any

from src.collection import capture_generation, capture_lifetime
from src.collection.capture_profiles import CaptureProfile
from src.collection.profile_view import (
    ProfileView,
    classify_profile,
    request_channel_names,
)
from src.collection.request_eligibility import Eligibility, ProfileClass
from src.vehicle.tune_revisions import calibration_matches

if TYPE_CHECKING:
    from src.collection.obd_scheduler import PollRequest

log = logging.getLogger(__name__)
#: Mode 01 channels the legacy boolean ``POST /control/tuning-mode`` polls when
#: the caller names none. An API default for that endpoint only; capture
#: profiles proper are data (``data/capture_profiles.yaml``), not code.
DEFAULT_TUNING_CHANNELS: frozenset[str] = frozenset(
    {
        "RPM",
        "ENGINE_LOAD",
        "MAF",
        "TIMING_ADVANCE",
        "THROTTLE_POS",
        "O2_WR_LAMBDA_S1",
        "O2_WR_LAMBDA_S2",
        "COMMANDED_EQUIV_RATIO",
        "FUEL_STATUS",
        "BAROMETRIC_PRESSURE",
        "INTAKE_TEMP",
        "SPEED",
        "COOLANT_TEMP",
        "SHORT_FUEL_TRIM_1",
        "SHORT_FUEL_TRIM_2",
        "LONG_FUEL_TRIM_1",
        "LONG_FUEL_TRIM_2",
        "EVAPORATIVE_PURGE",
    }
)


def _startup_profile() -> CaptureProfile:
    """The built-in ``default`` profile the module starts with."""
    return CaptureProfile(
        name="default",
        description="Normal collection",
        mode01={name: 1.0 for name in DEFAULT_TUNING_CHANNELS},
        corpus_eligible=True,
    )


_lock = threading.Lock()
_profile = _startup_profile()
_started_at: str | None = None
_started_monotonic: float | None = None
_session_dir: str | None = None
_source = "default"
_did_channels: dict[str, str] = {}
_configured = False
#: The calibration the tune-revision registry resolved as running at collector
#: start-up, reported in the capture payload. Provenance only: memory channels
#: are not gated on it (RAM addresses survive reflashes on this program).
_running_tune_revision: str | None = None
_ecu_calibration_id: str | None = None
_ecu_calibration_read_at: str | None = None
_ecu_calibration_changed = False
#: The running revision's recorded ECU id (``TuneRevision.calibration_id``).
_registry_calibration_id: str | None = None
#: The (ecu, registry) pair a mismatch WARNING was last logged for.
_ecu_calibration_mismatch_logged: tuple[str, str | None] | None = None
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


def _reset_for_tests() -> None:
    """Every module global back to its import-time value (R07.4).

    Only :func:`src.collection.process_state.reset_process_state_for_tests`
    calls this; the daemon resets through :func:`configure_default`.
    """
    global _profile, _started_at, _started_monotonic, _session_dir, _source, _did_channels
    global _configured, _running_tune_revision, _ecu_calibration_id, _ecu_calibration_read_at
    global _ecu_calibration_changed, _registry_calibration_id, _ecu_calibration_mismatch_logged
    global _paused
    with _lock:
        _profile = _startup_profile()
        _started_at = None
        _started_monotonic = None
        _session_dir = None
        _source = "default"
        _did_channels = {}
        _configured = False
        _running_tune_revision = None
        _ecu_calibration_id = None
        _ecu_calibration_read_at = None
        _ecu_calibration_changed = False
        _registry_calibration_id = None
        _ecu_calibration_mismatch_logged = None
        _paused = False


def configure_running_tune_revision(
    running_tune_revision: str | None, *, calibration_id: str | None = None
) -> None:
    """Record which calibration the registry says is running.

    ``calibration_id`` is that revision's recorded ECU id, the thing an
    ECU-reported id is compared against (W02.3).
    """
    global _running_tune_revision, _registry_calibration_id
    with _lock:
        _running_tune_revision = running_tune_revision
        _registry_calibration_id = calibration_id


def record_ecu_calibration_id(calibration_id: str | None) -> None:
    """Cache one local-identifier read and flag a change or mismatch loudly (W02.3)."""
    global _ecu_calibration_id, _ecu_calibration_read_at, _ecu_calibration_changed
    global _ecu_calibration_mismatch_logged
    if not calibration_id:
        return
    with _lock:
        if _ecu_calibration_id is not None and _ecu_calibration_id != calibration_id:
            _ecu_calibration_changed = True
            log.warning(
                "ECU calibration id changed during this daemon lifetime",
                extra={"previous": _ecu_calibration_id, "current": calibration_id},
            )
        _ecu_calibration_id = calibration_id
        _ecu_calibration_read_at = datetime.now(UTC).isoformat()
        registry_id = _registry_calibration_id
        revision = _running_tune_revision
        if calibration_matches(calibration_id, registry_id) is False:
            # Once per distinct pair: the id is re-read every few minutes,
            # and a line per read would bury the one that matters.
            if _ecu_calibration_mismatch_logged != (calibration_id, registry_id):
                _ecu_calibration_mismatch_logged = (calibration_id, registry_id)
                log.warning(
                    "ECU reports a calibration the flash history does not contain; "
                    "record the flash before trusting any RAM address",
                    extra={
                        "ecu_calibration_id": calibration_id,
                        "registry_calibration_id": registry_id,
                        "running_tune_revision": revision,
                    },
                )
        else:
            _ecu_calibration_mismatch_logged = None


def calibration_snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "ecu_calibration_id": _ecu_calibration_id,
            "ecu_calibration_read_at": _ecu_calibration_read_at,
            "ecu_calibration_changed": _ecu_calibration_changed,
            "running_tune_revision": _running_tune_revision,
            "registry_calibration_id": _registry_calibration_id,
        }


def reset_ecu_calibration() -> None:
    """Forget every ECU calibration read — for tests; the daemon never calls it."""
    global _ecu_calibration_id, _ecu_calibration_read_at, _ecu_calibration_changed
    global _ecu_calibration_mismatch_logged
    with _lock:
        _ecu_calibration_id = None
        _ecu_calibration_read_at = None
        _ecu_calibration_changed = False
        _ecu_calibration_mismatch_logged = None


def configure_did_aliases(aliases: dict[str, str]) -> None:
    global _did_channels
    with _lock:
        _did_channels = {name.upper(): channel.upper() for name, channel in aliases.items()}


def active_profile() -> CaptureProfile:
    with _lock:
        return _profile.model_copy(deep=True)


def active_profile_name() -> str:
    """The active profile's name, without :func:`active_profile`'s deep copy."""
    with _lock:
        return _profile.name


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


def mode01_batch_compatible(first: str, second: str) -> bool:
    """Whether two Mode-01 channels may share the active profile's batch.

    With no explicit partitions every pair remains compatible. Once a profile
    declares groups, two channels may batch only when both appear in the same
    group. Listed-vs-unlisted and different-group pairs stay in separate ECU
    responses, which gives timing experiments distinct acquisition instants.
    """
    with _lock:
        groups = _profile.mode01_batch_groups
    if not groups:
        return True
    a, b = first.upper(), second.upper()
    for group in groups:
        normalized = {name.upper() for name in group}
        if a in normalized and b in normalized:
            return True
    return False


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


def view() -> ProfileView:
    """The active profile's policy as a :class:`ProfileView`, under one lock.

    Every getter below is a thin wrapper over it, so the live gate and the
    capture-profile preview (which builds a view of a *candidate* profile)
    run the same policy code (R02.3). The profile object is replaced, never
    mutated, on activation, so the view holds it without a copy.
    """
    with _lock:
        return ProfileView(_profile, _did_channels, _paused)


def request_eligible(index: int, request: PollRequest) -> bool:
    """The profile gate: is this request selected by the active profile right now?

    A projection of the C06.7 eligibility matrix with no session knowledge
    (phase ``none``); the Mode-22 controller supplies the phase from its
    side and the loop combines both. Policy lives in
    ``request_eligibility``, not here.
    """
    del index
    return view().request_eligible(request)


def request_verdict(request: PollRequest) -> Eligibility:
    """The profile gate's verdict with its reason (``paused``, ``not_selected`` …)."""
    return view().request_verdict(request)


def profile_class() -> ProfileClass:
    with _lock:
        return classify_profile(_profile)


def selection_exclusion(request: PollRequest) -> str | None:
    """Why the active profile's selection excludes `request`, or ``None``.

    Takes the request rather than ``(kind, channel)`` on purpose. A window's
    membership lives on the request, and a signature accepting a bare
    channel string lets a caller silently ask the wrong question — this
    function had two callers and only one was fixed when windows landed,
    which cost a drive.
    """
    return view().selection_exclusion(request)


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
    return view().profile_channel_for(channel)


def rate_weight(channel: str) -> float:
    with _lock:
        return _profile.mode01.get(channel.upper(), 1.0)


def priority_channel(channel: str, default: bool) -> bool:
    """Return the active profile's priority decision for one request.

    ``None`` inherits the scheduler/config decision. An explicit tuple,
    including an empty one, owns the lane for the active profile.
    """
    return view().priority_channel(channel, default)


def did_rate_weight(channel: str) -> float:
    """Relative cadence for one selected DID, resolving profile aliases.

    Profiles key weights by the name they persist (for example
    ``ECM_AF_SENSOR_V_B1``), while the scheduler uses ``DID_1225``. Resolve
    through the same alias table as selection and profile-channel reporting so
    a renamed DID cannot silently lose its configured cadence.
    """
    return view().did_rate_weight(channel)


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
    return view().absolute_rate_hz(channel)


def request_absolute_rate_hz(request: PollRequest) -> float | None:
    """:func:`absolute_rate_hz` for a request rather than a channel string.

    A memory window is named after the window, so looking its own name up in
    the profile finds nothing and the read silently falls back to the ~1 Hz
    tier rate inside a capture that asked for 10. Same shape as the selection
    gate bug: the membership lives on the request, so ask the request.
    """
    return view().request_absolute_rate_hz(request)


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
            "running_tune_revision": _running_tune_revision,
        }
    if achieved_hz is not None:
        payload["achieved_hz"] = achieved_hz
    return payload


__all__ = [
    "DEFAULT_TUNING_CHANNELS",
    "activate",
    "active_profile",
    "channels",
    "capture_started_monotonic",
    "did_continuous_enabled",
    "did_rate_weight",
    "configure_default",
    "configure_did_aliases",
    "configure_running_tune_revision",
    "calibration_snapshot",
    "record_ecu_calibration_id",
    "reset_ecu_calibration",
    "is_active",
    "is_configured",
    "is_paused",
    "mode22",
    "mode01_batch_compatible",
    "absolute_rate_hz",
    "classify_profile",
    "profile_class",
    "priority_channel",
    "rate_weight",
    "request_channel_names",
    "request_eligible",
    "request_verdict",
    "selection_exclusion",
    "required_tuple",
    "set_active",
    "set_session_dir",
    "snapshot",
    "view",
]
