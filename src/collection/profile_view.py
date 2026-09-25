"""The capture-profile rate and selection policy, as pure functions of a profile.

:mod:`tuning_profile` holds the *active* profile as process state. Every
policy question the scheduler asks of it — is this request selected, at what
rate, with what priority, under which name — used to read that singleton
directly, so the only way to ask "what would profile P do?" was to activate
P. The capture-profile preview could not, and so re-implemented the policy
(``CaptureProfileStore.resolved_plan``, 142 lines) and drifted from it: a
different DID-window precedence, a rate cap live does not apply, and no
uniform round cadence (R02.3).

A :class:`ProfileView` is one profile plus the two pieces of process state
the policy also needs (the DID alias table and the pause flag). The
``tuning_profile`` getters are thin wrappers that build a view of the active
profile; the preview builds one of the candidate and calls the same
scheduler functions with it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.collection.capture_profiles import CaptureProfile
from src.collection.request_eligibility import Eligibility, ProfileClass, RequestKind, evaluate

if TYPE_CHECKING:
    from src.collection.obd_scheduler import DidSessionSpec, PollRequest


def classify_profile(profile: CaptureProfile) -> ProfileClass:
    """Which eligibility-matrix column a profile falls in."""
    if profile.did_continuous:
        return "continuous_did"
    if profile.name == "default":
        return "default"
    # Memory channels count: a service-0x23 read holds the same 0xC0
    # session as Mode 22, so a profile selecting only memory channels still
    # owns the session. Classifying it `focused_mode01` made every one of
    # its reads ineligible — a profile that cannot run the only requests it
    # asks for.
    if (profile.mode22 or profile.memory) and profile.mode22_enabled:
        return "mode22_owning"
    return "focused_mode01"


def request_channel_names(request: PollRequest) -> tuple[str, ...]:
    """Every catalog channel `request` would produce a reading for.

    One for most kinds. A memory WINDOW carries several and is named after
    the window, so its own channel name matches nothing in a profile's
    selection — which silently dropped every windowed read on the vehicle.
    """
    memory_read = getattr(request, "memory_read", None)
    if memory_read is not None:
        return tuple(c.name for c in memory_read.channels)
    return (str(getattr(request, "channel", "")),)


def _request_kind(request: PollRequest) -> RequestKind:
    # A request without a kind is a Mode 01 PID (the legacy shape callers
    # still pass); nothing else is guessed.
    kind: RequestKind = getattr(request, "kind", None) or "pid"
    return kind


@dataclass(frozen=True)
class ProfileView:
    """One capture profile's policy, independent of which profile is active."""

    profile: CaptureProfile
    #: Profile DID token (upper-cased) -> scheduler channel (``DID_1209``).
    aliases: Mapping[str, str] = field(default_factory=dict)
    paused: bool = False

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    @property
    def profile_class(self) -> ProfileClass:
        return classify_profile(self.profile)

    def selection_exclusion(self, request: PollRequest) -> str | None:
        """Why this profile's selection excludes `request`, or ``None``.

        Takes the request rather than ``(kind, channel)`` on purpose. A
        window's membership lives on the request, and a signature accepting a
        bare channel string lets a caller silently ask the wrong question.
        """
        profile = self.profile
        kind = _request_kind(request)
        channel = str(getattr(request, "channel", ""))
        names = request_channel_names(request)
        if kind == "memory":
            # Same session, so the same enable flag governs both.
            if not profile.mode22_enabled:
                return "mode22_disabled"
            selected = {item.upper() for item in profile.memory}
            candidates = names or (channel.split(":", 1)[-1],)
            # One read is issued if ANY member is selected; the collector
            # decodes only what the catalog names, so an unselected neighbour
            # in the same window costs nothing and is not emitted.
            if any(name.split(":", 1)[-1].upper() in selected for name in candidates):
                return None
            return "not_selected"
        if kind == "did":
            if not profile.mode22_enabled:
                return "mode22_disabled"
            requested = {item.upper() for item in profile.mode22}
            resolved = {self.aliases.get(item, item) for item in requested}
            upper = channel.upper()
            if upper in resolved or channel.removeprefix("DID_").upper() in resolved:
                return None
            return "not_selected"
        if not profile.mode01_enabled:
            return "mode01_disabled"
        if channel.upper() in profile.mode01 or "AUTO" in profile.mode01:
            return None
        return "not_selected"

    def request_verdict(self, request: PollRequest) -> Eligibility:
        """The profile gate's verdict with its reason (``paused``, ``not_selected`` …).

        A projection of the C06.7 eligibility matrix with no session
        knowledge (phase ``none``); the Mode-22 controller supplies the phase.
        """
        return evaluate(
            _request_kind(request),
            self.profile_class,
            "none",
            selection=self.selection_exclusion(request),
            paused=self.paused,
        )

    def request_eligible(self, request: PollRequest) -> bool:
        return self.request_verdict(request).eligible

    def profile_channel_for(self, channel: str) -> str | None:
        """The name this profile uses for a scheduler request channel.

        The scheduler reports DIDs by identifier (``DID_1209``) while a
        profile names them semantically (``ECM_MAF_GPS``). Returns the
        profile's own name, or ``None`` when the request is not selected.
        """
        profile = self.profile
        upper = channel.upper()
        stripped = channel.removeprefix("DID_").upper()
        # Mode 01 needs no translation: scheduler and profile use the same PID
        # channel name. "AUTO" selects every supported PID, so the request's
        # own name is the profile's name for it.
        if upper in profile.mode01 or "AUTO" in profile.mode01:
            return upper
        for item in profile.mode22:
            resolved = self.aliases.get(item.upper(), item.upper())
            if upper in {resolved, item.upper()} or stripped in {resolved, item.upper()}:
                return item
        memory_name = channel.split(":", 1)[-1].upper()
        for item in profile.memory:
            if item.upper() == memory_name:
                return item
        return None

    def required_tuple(self) -> frozenset[str]:
        """Upper-cased ``required_tuple``; empty when none."""
        return frozenset(name.upper() for name in self.profile.required_tuple)

    def priority_channel(self, channel: str, default: bool) -> bool:
        """This profile's priority decision for one request.

        ``None`` inherits the scheduler/config decision. An explicit tuple,
        including an empty one, owns the lane for the profile.
        """
        configured = self.profile.priority_channels
        if configured is None:
            return default
        candidates = {channel.upper()}
        profile_name = self.profile_channel_for(channel)
        if profile_name is not None:
            candidates.add(profile_name.upper())
        return bool(candidates & set(configured))

    # ------------------------------------------------------------------
    # Rate
    # ------------------------------------------------------------------

    def did_rate_weight(self, channel: str) -> float:
        """Relative cadence for one selected DID, resolving profile aliases.

        Profiles key weights by the name they persist (for example
        ``ECM_AF_SENSOR_V_B1``), while the scheduler uses ``DID_1225``.
        """
        upper = channel.upper()
        stripped = channel.removeprefix("DID_").upper()
        for item in self.profile.mode22:
            item_upper = item.upper()
            resolved = self.aliases.get(item_upper, item_upper)
            if upper in {resolved, item_upper} or stripped in {resolved, item_upper}:
                return self.profile.mode22_rate_weights.get(item_upper, 1.0)
        return 1.0

    def absolute_rate_hz(self, channel: str) -> float | None:
        """The profile's fixed poll rate for ``channel``, or ``None``.

        ``target_rate_hz`` times the profile's own weight for the channel, so
        one profile can say "these fast, those slow". Only for channels the
        profile lists: a target rate never accelerates something it did not
        ask for. DID names resolve through the alias table.
        """
        profile = self.profile
        target = profile.target_rate_hz
        if target is None:
            return None
        name = channel.upper()
        weight = profile.mode01.get(name)
        if weight is not None:
            return target * weight
        stripped = channel.removeprefix("DID_").upper()
        for item in profile.mode22:
            item_upper = item.upper()
            resolved = self.aliases.get(item_upper, item_upper)
            if name in {resolved, item_upper} or stripped in {resolved, item_upper}:
                return target * profile.mode22_rate_weights.get(item_upper, 1.0)
        return None

    def request_absolute_rate_hz(self, request: PollRequest) -> float | None:
        """:meth:`absolute_rate_hz` for a request rather than a channel string.

        A memory window is named after the window, so its own name finds
        nothing in the profile; membership lives on the request.
        """
        if str(getattr(request, "kind", "")) == "memory":
            target = self.profile.target_rate_hz
            if target is None:
                return None
            selected = {name.upper() for name in self.profile.memory}
            names = request_channel_names(request)
            if any(name.split(":", 1)[-1].upper() in selected for name in names):
                return target
            return None
        return self.absolute_rate_hz(str(getattr(request, "channel", "")))

    def rate_policy(self, channel: str) -> tuple[float, bool]:
        """``(rate_weight, uniform_cadence)`` for one Mode 01 channel."""
        return self.profile.mode01.get(channel.upper(), 1.0), self.profile.round_timestamps

    # ------------------------------------------------------------------
    # Mode-22 window timing: the one precedence rule
    # ------------------------------------------------------------------

    def did_cycle_seconds(self, session: DidSessionSpec) -> float | None:
        """Start-to-start cycle: the profile's, else the config's; ``None`` = none."""
        return self.profile.did_window_cycle(session)

    def did_window_interval(self, session: DidSessionSpec) -> float:
        """End-to-start window gap: the profile's, else the config's."""
        return self.profile.did_window_interval(session)

    def did_window_period(self, session: DidSessionSpec) -> float:
        """Seconds between window openings (see ``CaptureProfile.did_window_period``)."""
        return self.profile.did_window_period(session)


__all__ = ["ProfileView", "classify_profile", "request_channel_names"]
