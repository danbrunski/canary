"""One resolver from channel to its plausibility limits (A05.1).

Every limit a check uses comes from data, never from shared code:

* ``plausible_min``/``plausible_max`` on broadcast bindings
  (``data/bindings.yaml``), Mode 22 DIDs and service-0x23 memory channels
  (``data/dids.yaml``);
* the encoding range of each SAE J1979 Mode 01 PID, computed from the
  registry's own decoder (``src.collection.obd_registry``) — a value outside
  it cannot have come off the wire;
* the curated decode-level impossibilities in
  :data:`src.analysis.fsm.PHYSICAL_LIMITS` (J1979 facts, not vehicle ones);
* the vehicle's ``plausibility.yaml`` in its profile data directory: tighter
  per-channel limits, slew limits, stuck rules, enabled operating modes, poll
  tier intervals and the confirmation policy — the Xterra's numbers live in
  ``data/nissan-xterra-2008/plausibility.yaml``, not here.

Where several sources bound the same side, the tightest wins and the record
names which source supplied it. Nothing is loosened by a later source: a
catalog bound that is wrong is fixed in the catalog.

**Units.** A check compares a value in the units it *arrives* in. ``did:`` and
``mem:`` values arrive scaled (raw counts when their scale is unpinned, and
their bounds are then stated in counts too, as the catalog already does);
``decoded:`` values arrive as the binding's **raw integer** (see
``src.preprocessing.live``), so their engineering bounds are converted to raw
here, and ``plausibility.yaml`` states decoded limits in engineering units
too. A decoded channel with no pinned scale cannot take an engineering limit
and is refused rather than guessed.

**Unchecked is an answer.** :meth:`ChannelLimits.coverage` names, per check,
either ``"checked"`` or why not — a channel with no stated limit reports
``unchecked: no stated limit``, never ``ok``.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from src.config import CanaryConfig
    from src.decode.bindings import Bindings
    from src.decode.dids import DidMap

#: The profile data file this module reads, beside ``noise_floor.yaml`` and
#: ``tune_revisions.yaml`` in the vehicle's profile directory.
PLAUSIBILITY_FILENAME = "plausibility.yaml"

CheckName = Literal["range", "stuck", "freshness", "slew"]
CHECKS: tuple[CheckName, ...] = ("range", "stuck", "freshness", "slew")

#: Answer groups: channels that arrive through the same request path. A
#: channel is only "stale" while its group is otherwise answering — a whole
#: group going quiet is either a Mode 22 window (Mode 01 is suspended while
#: one is open) or the source going stale, which ``vehicle_state`` reports.
GROUP_OBD = "obd"
GROUP_DID = "did"
GROUP_MEMORY = "memory"
GROUP_BROADCAST = "broadcast"

#: Default tier per group for channels that carry no J1979 ``poll_tier``.
#: A tier only yields an expected interval when ``plausibility.yaml`` states
#: one for it.
GROUP_DEFAULT_TIER: dict[str, str] = {
    GROUP_DID: "did",
    GROUP_MEMORY: "memory",
    GROUP_BROADCAST: "broadcast",
}

#: J1979 units whose "encoding range" is not a physical range at all.
_NON_PHYSICAL_UNITS = frozenset({"bitmask", "enum", "count", ""})
_NON_PHYSICAL_CATEGORIES = frozenset({"status", "info", "dtc", "monitor"})


def plausibility_path(config: CanaryConfig) -> Path:
    """The active profile's ``plausibility.yaml``.

    Same directory as ``src.control.calibration.profile_dir`` (the scoped
    ``afr_calibration`` parent), so it follows the VIN profile.
    """
    return config.daemon.afr_calibration.parent / PLAUSIBILITY_FILENAME


def channel_group(channel: str) -> str:
    """The answer group of a wide-frame channel key."""
    source, _, rest = channel.partition(":")
    if source == "obd":
        return GROUP_MEMORY if rest.startswith("mem:") else GROUP_OBD
    if source == "did":
        return GROUP_DID
    if source == "decoded":
        return GROUP_BROADCAST
    return source


# ---------------------------------------------------------------------------
# Profile file schema
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TierSpec(_Strict):
    """Expected arrival interval of one poll tier on this vehicle."""

    interval_seconds: float = Field(gt=0)
    #: Where the number came from (a measurement, a config fact). Required: an
    #: interval with no provenance is a guess that nothing can re-check.
    reason: str = Field(min_length=1)


class GroupSpec(_Strict):
    """How long an answer group may go quiet before it has *stopped*.

    Gaps between any two arrivals in the group shorter than this are routine
    polling; a longer one is the group being suspended (a Mode 22 window
    suspends Mode 01) and every member's freshness clock restarts when it
    resumes. Unstated, the group's smallest freshness threshold is used,
    which never restarts a clock inside a drive.
    """

    silence_seconds: float = Field(gt=0)
    reason: str = Field(min_length=1)


class ConfirmSpec(_Strict):
    """When a repeated detection confirms (A05.6).

    ``repeats`` detections of the same check on the same channel, the later
    ones at least ``min_separation_seconds`` after the first, all inside
    ``window_seconds``. ``across_drives`` keeps a pending detection alive
    into the next drive, where the first repeat confirms it — for checks too
    slow to repeat inside one window.
    """

    repeats: int = Field(default=2, ge=2)
    window_seconds: float = Field(gt=0)
    min_separation_seconds: float = Field(default=0.0, ge=0)
    across_drives: bool = False

    @model_validator(mode="after")
    def _separation_fits(self) -> ConfirmSpec:
        if self.min_separation_seconds >= self.window_seconds:
            raise ValueError("min_separation_seconds must be shorter than window_seconds")
        return self


class RangeRule(_Strict):
    """A vehicle limit tighter than any catalog bound, and/or the modes the
    range check runs in, with its reason. ``modes`` alone gates the catalog's
    own bound (e.g. a key-on sentinel the ECM broadcasts before its first
    read)."""

    min: float | None = None
    max: float | None = None
    reason: str = Field(min_length=1)
    #: Operating modes the range check runs in; omitted means every mode.
    modes: list[str] | None = None

    @model_validator(mode="after")
    def _a_bound(self) -> RangeRule:
        if self.min is None and self.max is None and self.modes is None:
            raise ValueError("a range rule needs min, max or modes")
        if self.min is not None and self.max is not None and self.min >= self.max:
            raise ValueError("range min must be below max")
        return self


class SlewRule(_Strict):
    """Largest physically possible change per second: a step is flagged when
    ``|delta| > per_second * max(dt, update_interval_seconds) + allowance``.

    ``allowance`` is the value's quantization step. ``update_interval_seconds``
    is how often the source refreshes the value: two reads closer together
    than that can straddle one refresh and see a whole refresh's worth of
    change in a few milliseconds, which is acquisition, not physics.
    Engineering units for every channel (decoded ones are converted through
    the binding's scale)."""

    per_second: float = Field(gt=0)
    allowance: float = Field(default=0.0, ge=0)
    update_interval_seconds: float = Field(default=0.0, ge=0)
    reason: str = Field(min_length=1)
    modes: list[str] | None = None


class StuckRule(_Strict):
    """``samples`` consecutive identical arrived samples spanning at least
    ``min_seconds``, in a mode where the channel is expected to move.

    ``modes`` is required: a channel that may legitimately hold (coolant at
    warm idle) simply names no such mode. ``min_seconds`` exists because a
    sample count alone depends on the poll rate: the same physical hold is
    5 samples at 1 Hz and 20 at 4 Hz.
    """

    samples: int = Field(ge=2)
    min_seconds: float = Field(default=0.0, ge=0)
    modes: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class FreshnessRule(_Strict):
    """Per-channel freshness overrides: its tier, or opting it out."""

    tier: str | None = None
    enabled: bool = True
    reason: str | None = None
    modes: list[str] | None = None


class ChannelRules(_Strict):
    range: RangeRule | None = None
    slew: SlewRule | None = None
    stuck: StuckRule | None = None
    freshness: FreshnessRule | None = None
    #: Per-channel confirmation override, by check.
    confirm: dict[CheckName, ConfirmSpec] = Field(default_factory=dict)


class PlausibilityProfile(_Strict):
    """A vehicle's ``plausibility.yaml``."""

    schema_version: Literal[1]
    #: Expected interval per poll tier — J1979's ``fast``/``medium``/``slow``
    #: plus ``did``/``memory``/``broadcast`` for the non-Mode-01 groups.
    tiers: dict[str, TierSpec] = Field(default_factory=dict)
    #: A channel is stale past ``interval_seconds * freshness_k``.
    freshness_k: float = Field(gt=1)
    #: Per answer group (``obd``, ``did``, ``memory``, ``broadcast``).
    groups: dict[str, GroupSpec] = Field(default_factory=dict)
    #: Confirmation policy per check. Every check must have one: a check with
    #: no stated policy would either never alert or alert on one sample.
    confirm: dict[CheckName, ConfirmSpec]
    channels: dict[str, ChannelRules] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _complete(self) -> PlausibilityProfile:
        missing = [check for check in CHECKS if check not in self.confirm]
        if missing:
            raise ValueError(f"confirm policy missing for checks {missing}")
        unprefixed = sorted(name for name in self.channels if ":" not in name)
        if unprefixed:
            raise ValueError(f"channel keys must be source-prefixed (e.g. obd:RPM): {unprefixed}")
        return self

    def mode_names(self) -> set[str]:
        """Every operating-mode name this file references."""
        names: set[str] = set()
        for rules in self.channels.values():
            for rule in (rules.range, rules.slew, rules.stuck, rules.freshness):
                modes = getattr(rule, "modes", None) if rule is not None else None
                if modes:
                    names.update(modes)
        return names


def load_plausibility_profile(path: Path) -> PlausibilityProfile:
    """Parse and validate a ``plausibility.yaml``. Raises on a bad file."""
    raw = yaml.safe_load(path.read_text())
    return PlausibilityProfile.model_validate(raw)


# ---------------------------------------------------------------------------
# Resolved limits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bound:
    """One side of a range, in the channel's arriving units, with its source."""

    value: float
    source: str


@dataclass(frozen=True)
class SlewLimit:
    """``|delta| > per_second * max(dt, update_interval) + allowance`` flags,
    in arriving units."""

    per_second: float
    allowance: float
    source: str
    update_interval_seconds: float = 0.0


@dataclass(frozen=True)
class StuckLimit:
    samples: int
    source: str
    min_seconds: float = 0.0


@dataclass(frozen=True)
class ChannelLimits:
    """Everything the evaluator needs about one channel.

    ``modes[check]`` is ``None`` for "every mode", else the modes it runs in.
    ``engineering`` is ``(scale, offset)`` for a ``decoded:`` channel whose
    values arrive raw, so evidence can quote both.
    """

    channel: str
    unit: str
    group: str
    minimum: Bound | None = None
    maximum: Bound | None = None
    slew: SlewLimit | None = None
    stuck: StuckLimit | None = None
    tier: str | None = None
    expected_interval_seconds: float | None = None
    freshness_threshold_seconds: float | None = None
    freshness_disabled_reason: str | None = None
    modes: Mapping[CheckName, frozenset[str] | None] = field(default_factory=dict)
    engineering: tuple[float, float] | None = None
    #: Why the channel was not polled with a tier interval, when it wasn't.
    tier_missing_reason: str | None = None

    def enabled_in(self, check: CheckName, mode: str | None) -> bool:
        """Whether ``check`` runs in ``mode``. ``None`` modes = every mode;
        an unknown mode (no tick yet) only runs every-mode checks."""
        modes = self.modes.get(check)
        if modes is None:
            return True
        return mode is not None and mode in modes

    def coverage(self) -> dict[CheckName, str]:
        """Per check: ``"checked"`` or ``"unchecked: <why>"``."""
        out: dict[CheckName, str] = {}
        out["range"] = (
            "checked"
            if self.minimum is not None or self.maximum is not None
            else "unchecked: no stated limit"
        )
        out["slew"] = "checked" if self.slew is not None else "unchecked: no slew limit"
        out["stuck"] = (
            "checked"
            if self.stuck is not None
            else "unchecked: no stuck rule (not expected to move in any stated mode)"
        )
        if self.freshness_threshold_seconds is not None:
            out["freshness"] = "checked"
        elif self.freshness_disabled_reason is not None:
            out["freshness"] = f"unchecked: {self.freshness_disabled_reason}"
        else:
            out["freshness"] = f"unchecked: {self.tier_missing_reason or 'no poll tier'}"
        return out

    def is_checked(self) -> bool:
        return any(state == "checked" for state in self.coverage().values())

    def to_engineering(self, value: float) -> float:
        if self.engineering is None:
            return value
        scale, offset = self.engineering
        return value * scale + offset


@dataclass(frozen=True)
class LimitsCatalog:
    """The resolver's output: channel → :class:`ChannelLimits`."""

    channels: Mapping[str, ChannelLimits]
    profile: PlausibilityProfile | None
    #: Where the vehicle rules came from, for the status/evidence trail.
    profile_path: str | None = None

    def get(self, channel: str) -> ChannelLimits:
        """Limits for ``channel``; an all-unchecked record when none are known."""
        found = self.channels.get(channel)
        if found is not None:
            return found
        return ChannelLimits(
            channel=channel,
            unit="",
            group=channel_group(channel),
            tier_missing_reason="channel not in any catalog",
        )

    def group_silence(self, group: str) -> float | None:
        """The stated silence that means ``group`` stopped, if any."""
        if self.profile is None:
            return None
        spec = self.profile.groups.get(group)
        return spec.silence_seconds if spec is not None else None

    def confirm_policy(self, channel: str, check: CheckName) -> ConfirmSpec:
        if self.profile is None:
            raise ValueError("no plausibility profile: confirmation policy is unstated")
        rules = self.profile.channels.get(channel)
        if rules is not None and check in rules.confirm:
            return rules.confirm[check]
        return self.profile.confirm[check]

    def coverage(self, channels: Iterable[str] | None = None) -> dict[str, dict[CheckName, str]]:
        names = sorted(self.channels) if channels is None else sorted(set(channels))
        return {name: self.get(name).coverage() for name in names}


@dataclass
class _Builder:
    channel: str
    unit: str = ""
    minimum: Bound | None = None
    maximum: Bound | None = None
    tier: str | None = None
    engineering: tuple[float, float] | None = None
    #: A decoded channel without a pinned scale: engineering rules refused.
    unscaled_decoded: bool = False

    def tighten(self, low: float | None, high: float | None, source: str) -> None:
        if low is not None and math.isfinite(low):
            if self.minimum is None or low > self.minimum.value:
                self.minimum = Bound(float(low), source)
        if high is not None and math.isfinite(high):
            if self.maximum is None or high < self.maximum.value:
                self.maximum = Bound(float(high), source)


def _to_raw(builder: _Builder, value: float) -> float:
    """Engineering → the raw units a decoded channel arrives in."""
    if builder.engineering is None:
        return value
    scale, offset = builder.engineering
    return (value - offset) / scale


def _raw_bounds(builder: _Builder, low: float | None, high: float | None) -> tuple[Any, Any]:
    if builder.engineering is None:
        return low, high
    raw_low = _to_raw(builder, low) if low is not None else None
    raw_high = _to_raw(builder, high) if high is not None else None
    if builder.engineering[0] < 0:  # a negative scale swaps the sides
        raw_low, raw_high = raw_high, raw_low
    return raw_low, raw_high


def j1979_encoding_ranges() -> dict[str, tuple[float, float, str, str, int]]:
    """``obd:<CHANNEL>`` → ``(min, max, unit, tier, pid)`` over every payload
    the registry's decoder can produce.

    Each of the first two payload bytes is swept through 0..255 with the
    others held at 0x00 and at 0xFF; every J1979 formula is monotonic in each
    byte, so that reaches both ends. Status/bitmask/enum PIDs have no physical
    range and are left out (their tier still resolves via
    :func:`j1979_poll_tiers`).
    """
    from src.collection.obd_registry import MODE01_REGISTRY

    out: dict[str, tuple[float, float, str, str, int]] = {}
    for pid, definition in MODE01_REGISTRY.items():
        if definition.decoder is None or definition.byte_length <= 0:
            continue
        if definition.unit in _NON_PHYSICAL_UNITS:
            continue
        if definition.category in _NON_PHYSICAL_CATEGORIES:
            continue
        values: list[float] = []
        for payload in _encoding_payloads(definition.byte_length):
            try:
                decoded = definition.decoder(payload)
            except (ValueError, IndexError, ZeroDivisionError):
                continue
            if decoded is None or isinstance(decoded[0], str):
                continue
            value = float(decoded[0])
            if math.isfinite(value):
                values.append(value)
        if values:
            out[f"obd:{definition.channel}"] = (
                min(values),
                max(values),
                definition.unit,
                definition.poll_tier,
                pid,
            )
    return out


def j1979_poll_tiers() -> dict[str, str]:
    """``obd:<CHANNEL>`` → its J1979 registry ``poll_tier``."""
    from src.collection.obd_registry import MODE01_REGISTRY

    return {
        f"obd:{definition.channel}": definition.poll_tier
        for definition in MODE01_REGISTRY.values()
        if definition.poll_tier in ("fast", "medium", "slow")
    }


def _encoding_payloads(length: int) -> Iterable[bytes]:
    swept = min(length, 2)
    for fill in (0x00, 0xFF):
        for position in range(swept):
            for byte in range(256):
                payload = bytearray([fill] * length)
                payload[position] = byte
                yield bytes(payload)


def resolve_limits(
    profile: PlausibilityProfile | None,
    *,
    bindings: Bindings | None = None,
    did_map: DidMap | None = None,
    operating_modes: Iterable[str] | None = None,
    include_j1979: bool = True,
    include_physical_limits: bool = True,
    profile_path: Path | None = None,
    priority_channels: Iterable[str] | None = None,
) -> LimitsCatalog:
    """Resolve every known channel's limits; see the module docstring.

    ``operating_modes`` (the config's mode names) makes a mode named in the
    profile that the segmenter can never produce a load-time error rather
    than a check that silently never runs.

    ``priority_channels`` (``collection.obd_priority_channels``) are the
    Mode 01 PIDs whose tier rate the scheduler holds when the bus budget is
    short; every other PID degrades around them, and was measured absent for
    three minutes and more while its peers answered on clean drives. Only a
    guaranteed rate makes a missing sample evidence, so when given, a Mode 01
    channel outside it reports freshness ``unchecked`` with that reason.
    """
    builders: dict[str, _Builder] = {}

    def builder(channel: str) -> _Builder:
        found = builders.get(channel)
        if found is None:
            found = builders[channel] = _Builder(channel)
        return found

    if include_j1979:
        for channel, poll_tier in j1979_poll_tiers().items():
            builder(channel).tier = poll_tier
        for channel, (low, high, unit, pid_tier, pid) in j1979_encoding_ranges().items():
            entry = builder(channel)
            entry.unit = unit
            entry.tier = pid_tier
            entry.tighten(low, high, f"J1979 PID 0x{pid:02X} encoding range")
    if include_physical_limits:
        from src.analysis.fsm import PHYSICAL_LIMITS

        for channel, limit in PHYSICAL_LIMITS.items():
            entry = builder(channel)
            entry.unit = entry.unit or limit.unit
            entry.tighten(limit.min, limit.max, f"fsm.PHYSICAL_LIMITS: {limit.reason}")
    if bindings is not None:
        for signal in bindings.signals:
            entry = builder(f"decoded:{signal.name}")
            entry.tier = GROUP_DEFAULT_TIER[GROUP_BROADCAST]
            entry.unit = signal.display_unit
            if signal.scale is not None and signal.scale != 0:
                entry.engineering = (signal.scale, signal.offset or 0.0)
            else:
                entry.unscaled_decoded = True
            if signal.plausible_min is None and signal.plausible_max is None:
                continue
            low, high = _raw_bounds(entry, signal.plausible_min, signal.plausible_max)
            entry.tighten(low, high, f"bindings.yaml {signal.name} plausible range")
    if did_map is not None:
        for did in did_map.dids:
            entry = builder(f"did:{did.name}")
            entry.tier = GROUP_DEFAULT_TIER[GROUP_DID]
            entry.unit = did.unit if did.scale is not None else "raw"
            entry.tighten(
                did.plausible_min, did.plausible_max, f"dids.yaml {did.name} plausible range"
            )
        for memory in did_map.memory:
            entry = builder(f"obd:mem:{memory.name}")
            entry.tier = GROUP_DEFAULT_TIER[GROUP_MEMORY]
            entry.unit = memory.unit if memory.scale is not None else "counts"
            entry.tighten(
                memory.plausible_min,
                memory.plausible_max,
                f"dids.yaml memory {memory.name} plausible range",
            )

    if profile is not None and operating_modes is not None:
        known = set(operating_modes)
        unknown = sorted(profile.mode_names() - known)
        if unknown:
            raise ValueError(
                f"plausibility.yaml names operating modes the segmenter cannot produce: "
                f"{unknown}; known: {sorted(known)}"
            )

    priority = (
        {name.strip().upper() for name in priority_channels}
        if priority_channels is not None
        else None
    )
    source_label = f"{profile_path.name if profile_path else PLAUSIBILITY_FILENAME}"
    rules_by_channel = profile.channels if profile is not None else {}
    for channel in rules_by_channel:
        builder(channel)

    channels: dict[str, ChannelLimits] = {}
    for channel, entry in builders.items():
        rules = rules_by_channel.get(channel)
        modes: dict[CheckName, frozenset[str] | None] = dict.fromkeys(CHECKS)
        slew: SlewLimit | None = None
        stuck: StuckLimit | None = None
        if rules is not None:
            states_value = rules.range is not None and (
                rules.range.min is not None or rules.range.max is not None
            )
            if entry.unscaled_decoded and (states_value or rules.slew is not None):
                raise ValueError(
                    f"{channel}: engineering-unit rules need a pinned binding scale; "
                    "this binding has none"
                )
            if rules.range is not None:
                low, high = _raw_bounds(entry, rules.range.min, rules.range.max)
                entry.tighten(low, high, f"{source_label}: {rules.range.reason}")
                modes["range"] = _modes(rules.range.modes)
            if rules.slew is not None:
                factor = abs(entry.engineering[0]) if entry.engineering is not None else 1.0
                slew = SlewLimit(
                    per_second=rules.slew.per_second / factor,
                    allowance=rules.slew.allowance / factor,
                    source=f"{source_label}: {rules.slew.reason}",
                    update_interval_seconds=rules.slew.update_interval_seconds,
                )
                modes["slew"] = _modes(rules.slew.modes)
            if rules.stuck is not None:
                stuck = StuckLimit(
                    rules.stuck.samples,
                    f"{source_label}: {rules.stuck.reason}",
                    rules.stuck.min_seconds,
                )
                modes["stuck"] = frozenset(rules.stuck.modes)
            if rules.freshness is not None:
                if rules.freshness.tier is not None:
                    entry.tier = rules.freshness.tier
                modes["freshness"] = _modes(rules.freshness.modes)
        group = channel_group(channel)
        tier = entry.tier or GROUP_DEFAULT_TIER.get(group)
        interval: float | None = None
        threshold: float | None = None
        disabled: str | None = None
        tier_missing: str | None = None
        if rules is not None and rules.freshness is not None and not rules.freshness.enabled:
            disabled = f"freshness disabled in {source_label}" + (
                f": {rules.freshness.reason}" if rules.freshness.reason else ""
            )
        elif (
            priority is not None
            and group == GROUP_OBD
            and channel.partition(":")[2].upper() not in priority
        ):
            disabled = (
                "not a priority channel: the scheduler degrades it under bus-budget "
                "pressure (collection.obd_priority_channels), so its absence is not evidence"
            )
        elif tier is None:
            tier_missing = "no poll tier"
        elif profile is None:
            tier_missing = f"no {PLAUSIBILITY_FILENAME} for this vehicle"
        elif tier not in profile.tiers:
            tier_missing = f"tier {tier!r} has no interval in {source_label}"
        else:
            interval = profile.tiers[tier].interval_seconds
            threshold = interval * profile.freshness_k
        channels[channel] = ChannelLimits(
            channel=channel,
            unit=entry.unit,
            group=group,
            minimum=entry.minimum,
            maximum=entry.maximum,
            slew=slew,
            stuck=stuck,
            tier=tier,
            expected_interval_seconds=interval,
            freshness_threshold_seconds=threshold,
            freshness_disabled_reason=disabled,
            modes=modes,
            engineering=entry.engineering,
            tier_missing_reason=tier_missing,
        )
    return LimitsCatalog(
        channels=channels,
        profile=profile,
        profile_path=str(profile_path) if profile_path is not None else None,
    )


def _modes(modes: list[str] | None) -> frozenset[str] | None:
    return frozenset(modes) if modes is not None else None


def resolve_limits_for_config(
    config: CanaryConfig,
    *,
    bindings: Bindings | None = None,
    did_map: DidMap | None = None,
) -> LimitsCatalog:
    """Resolve against the active profile's ``plausibility.yaml``.

    Refuses (``FileNotFoundError``) when the vehicle has no such file: the
    confirmation policy and tier intervals are vehicle facts, so running
    without them would be running on guesses.
    """
    path = plausibility_path(config)
    if not path.exists():
        raise FileNotFoundError(
            f"no {PLAUSIBILITY_FILENAME} for this vehicle at {path}; plausibility checks "
            "need its tier intervals and confirmation policy"
        )
    profile = load_plausibility_profile(path)
    return resolve_limits(
        profile,
        bindings=bindings,
        did_map=did_map,
        operating_modes=config.preprocessing.operating_modes,
        profile_path=path,
        priority_channels=config.collection.obd_priority_channels,
    )


__all__ = [
    "CHECKS",
    "PLAUSIBILITY_FILENAME",
    "Bound",
    "ChannelLimits",
    "ChannelRules",
    "CheckName",
    "ConfirmSpec",
    "LimitsCatalog",
    "PlausibilityProfile",
    "SlewLimit",
    "StuckLimit",
    "channel_group",
    "j1979_encoding_ranges",
    "j1979_poll_tiers",
    "load_plausibility_profile",
    "plausibility_path",
    "resolve_limits",
    "resolve_limits_for_config",
]
