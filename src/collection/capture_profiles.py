"""Strict, vehicle-scoped capture profile persistence.

Capture profiles describe *collection*, not vehicle identity.  The catalog is
stored beside the active vehicle's other scoped artifacts and is deliberately
small enough to reload on every API operation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.atomic_write import atomic_write_text
from src.collection.bus_budget import (
    MAX_BUDGET_REQUESTS_PER_SECOND,
    MIN_BUDGET_REQUESTS_PER_SECOND,
)
from src.config import CollectionConfig, DaemonConfig
from src.decode.dids import memory_channels, select_memory_channels
from src.signals.derived import DERIVED_CHANNEL_INPUTS

if TYPE_CHECKING:
    from src.collection.kline.plan import KlineCatalog
    from src.collection.obd_scheduler import DidSessionSpec

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_PROFILE_NAME = "default"
#: The catalog a vehicle starts from when it has no tracked one. Only
#: ``default`` is built in code, because it mirrors the running collection
#: config; every other profile is data. It stands in, in memory, for the missing
#: tracked catalog; an operator's edits and deletions are this machine's
#: overrides layered over it (S03.4), so they are final until reverted.
STARTER_CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "capture_profiles.yaml"
#: Keys older catalogs carry from the retired built-in upgrade machinery
#: (``builtin_revision`` / ``operator_modified``, removed 2026-09-21). Dropped
#: on load so those files still validate under ``extra="forbid"``.
_RETIRED_PROFILE_KEYS = ("builtin_revision", "operator_modified")
#: ``broadcast_decode`` chose whether a capture stored ``decoded`` rows beside
#: its raw frames. Retired by P02.8 (operator 2026-09-26): no capture stores
#: them, and every reader decodes the frames on read, so the field has nothing
#: left to switch. Saved catalogs on the Pi carry it and nothing re-seeds a
#: saved catalog, so it is accepted and dropped with a named warning (once per
#: process), never an error; the next save of the catalog writes it out.
_RETIRED_BROADCAST_DECODE = "broadcast_decode"
_broadcast_decode_warned = False

#: Request rate above which the storage sink has a *measured* problem: 25 req/s
#: backed the writer up until it dropped 2491 records on 2026-07-29, and the
#: writer-side root cause was never established. Exceeding it is allowed, but
#: only with ``capacity_evidence`` — see ``_raised_budget_needs_evidence``.
STORAGE_SAFE_REQUESTS_PER_SECOND = 24.0
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
#: Ceiling on a profile's absolute per-channel poll rate. Matches the bus
#: budget's own ceiling: demand above what the budget can ever issue is a
#: configuration error, not a request. Measured headroom on the Xterra ECM is
#: 478 channel-reads/s batched with zero errors, so this is not the binding
#: limit — the budget is, deliberately.
MAX_TARGET_RATE_HZ = 200.0
#: Session directories are discovered by name. A capture that does not match
#: ``src.session.classify_session_name`` as a capture still records telemetry but becomes
#: unreachable through the sessions API — no listing, no export, no delete — so
#: the prefix is constrained here rather than left to the operator.
_SAFE_PREFIX = re.compile(r"^_capture-[a-z0-9][a-z0-9-]*$")

#: Channels that must land in the *same* poll round for a delivery-error
#: sample to exist at all. Declared rather than described: a fuel capture whose
#: per-channel rates all look healthy can still yield nothing, because delivery
#: error needs these five co-observed and the scarcest one sets the real yield.
#: The 2026-08-10 capture polled every one of them and produced two usable
#: sample-instants in 24 minutes; per-channel rates never said so.
FUEL_DELIVERY_TUPLE: tuple[str, ...] = (
    "RPM",
    "ENGINE_LOAD",
    "FUEL_STATUS",
    "COMMANDED_EQUIV_RATIO",
    "O2_WR_LAMBDA_S1",
)

#: Air-density and thermal context every tuning capture must carry: ambient
#: pressure, intake-air temperature and coolant temperature.
#:
#: Without them a capture cannot be normalised. Airflow, load and VE only
#: compare across days as VE or ``MAP / measured baro`` -- this vehicle lives
#: at ~80 kPa and drives to 63 kPa, and a sea-level or even a fixed 80 kPa
#: assumption fakes a restriction -- and warm-up gating needs coolant. The
#: 2026-09-21 `fuel-target-research` drive polled none of the three, so its VE
#: had to be computed from a GPS-altitude baro estimate and an assumed 25 C
#: intake: an answer with two guesses in it. Every non-default Xterra profile
#: missed at least one.
#:
#: Tests hold every shipped capture profile to this, via Mode 01 where the
#: profile runs Mode 01. A profile that holds the Mode-22 session continuously
#: cannot poll Mode 01 at all; it must carry the IAT and coolant DIDs instead,
#: and baro has no known DID on this ECM, so those captures take it from the
#: adjacent default session.
CAPTURE_CONTEXT_MODE01: tuple[str, ...] = ("BAROMETRIC_PRESSURE", "INTAKE_TEMP", "COOLANT_TEMP")


def _memory_window_members() -> dict[str, tuple[str, ...]]:
    """Catalog memory window name -> its member channel names, in catalog order."""
    windows: dict[str, list[str]] = {}
    for channel in memory_channels():
        if channel.window:
            windows.setdefault(channel.window, []).append(channel.name)
    return {name: tuple(members) for name, members in windows.items()}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptureProfile(_Strict):
    @model_validator(mode="before")
    @classmethod
    def _drop_retired_keys(cls, data: Any) -> Any:
        if isinstance(data, dict) and any(key in data for key in _RETIRED_PROFILE_KEYS):
            data = {k: v for k, v in data.items() if k not in _RETIRED_PROFILE_KEYS}
        if isinstance(data, dict) and _RETIRED_BROADCAST_DECODE in data:
            data = {k: v for k, v in data.items() if k != _RETIRED_BROADCAST_DECODE}
            _warn_broadcast_decode_retired()
        return data

    name: str
    description: str = ""
    #: Why this profile is the way it is — the reasoning a future editor needs
    #: before changing it. ``description`` is the one-line label a UI shows;
    #: this is the rationale.
    #:
    #: It is a *field* rather than a YAML comment because the catalog is
    #: written back through ``yaml.safe_dump`` on every save, which drops
    #: comments silently. It did: a rewrite once deleted 64 comment blocks
    #: across the two shipped catalogs, including why PID 0x25 must stay out
    #: of this ECM's profile.
    notes: str = ""
    #: Research profiles are intentionally operator-visible but have not yet
    #: earned the same trust as routine captures.  Keep that status in the
    #: schema so every UI/API consumer can render it; parsing words such as
    #: "experimental" out of a description would lose the warning on rename.
    experimental: bool = False
    mode01: dict[str, float] = Field(default_factory=dict)
    #: Explicit Mode-01 batch partitions. Members in different groups can
    #: never share one multi-PID request; a listed member also cannot batch
    #: with an unlisted member. This preserves batching throughput while
    #: giving timing experiments independent acquisition instants (for
    #: example commanded phi in one response and measured lambda in another).
    #: Empty keeps the historical unrestricted batching policy.
    mode01_batch_groups: tuple[tuple[str, ...], ...] = ()
    mode22: tuple[str, ...] = ()
    #: Relative cadence for selected Mode-22 members. Missing entries are 1.0,
    #: preserving every pre-C09 profile byte-for-byte at load. In a continuous
    #: DID session the weight scales the member's ordinary/absolute scheduler
    #: rate. In a windowed profile it controls which cycle windows the member
    #: is due in; it never changes the window cadence itself.
    mode22_rate_weights: dict[str, float] = Field(default_factory=dict)
    #: Service-0x23 memory channels this profile polls, by catalog name.
    #: Separate from ``mode22`` because they are a different kind of claim:
    #: a DID is an ECU-defined identifier, a RAM address is a location in the
    #: program's memory map (stable across calibration flashes, verified by
    #: its ``known_answer``). Gated by ``mode22_enabled`` -- they need the
    #: same diagnostic session.
    memory: tuple[str, ...] = ()
    #: Relative cadence for selected memory channels (D03.38), keyed by the
    #: channel name ``memory:`` lists. Missing entries are 1.0, so every
    #: profile saved before the field existed polls exactly as it did.
    #:
    #: The unit polled is the *window*: one service-0x23 read returns every
    #: channel that shares a catalog ``window``, so a window cannot be read at
    #: two rates. It runs at the **largest** weight among this profile's
    #: selected members. The fastest member's demand has to be met, and a
    #: slower neighbour then gets extra samples at no extra bus cost: the same
    #: bytes arrive in the same reply. Keyed by channel rather than by window
    #: because a window is catalog layout (widened, split or regrouped without
    #: renaming its channels), while a profile names channels everywhere
    #: else. A key naming a window is refused with its members.
    #:
    #: Same meaning as ``mode22_rate_weights``: in a windowed profile the
    #: weight chooses which session windows the read is due in (1.0 = every
    #: window; it cannot add a second read to one window), in a continuous
    #: one it scales the scheduler rate, and under ``target_rate_hz`` it
    #: multiplies the target.
    memory_rate_weights: dict[str, float] = Field(default_factory=dict)
    #: Channels to read over **K-line** through the CANary-HAT
    #: (``src.collection.kline_collector``), all from one module: bare entries
    #: (catalog name or DID hex) are the ECM's; ``<MODULE>:<token>``
    #: (``ABS:ENGINE_SPEED``, ``ABS:1102``) names another module from the
    #: vehicle's K-line profile. Non-empty means that while this profile is
    #: active the K-line collector holds that module's session. Only for the
    #: ECM does that pause CAN diagnostic polling (``tx_gate``: the ECM's
    #: session is shared); another module shares only the wire. Broadcast CAN
    #: capture carries on either way. Empty (the default) means K-line is never
    #: commanded (operator decision 2026-09-23). Every entry needs a recorded
    #: width (catalog or K-line profile); see ``src.collection.kline.plan``.
    kline: tuple[str, ...] = ()
    #: Whether each service is polled at all. Disabling keeps the selection
    #: intact so re-enabling doesn't mean rebuilding it — "off for this
    #: capture" and "not of interest" are different statements.
    mode01_enabled: bool = True
    mode22_enabled: bool = True
    #: Seconds between Mode-22 session windows, overriding the daemon default.
    #: DID cadence is a *sampling choice*, not an ECU limit: the ECU answers a
    #: DID in ~1.5 ms, so a slow default belongs in config, never in the
    #: scheduler.
    did_interval_seconds: float | None = None
    #: Start-to-start period of one Mode 01 + Mode 22 cycle (C04.3): the
    #: session entry, DID sweep, exit, its grace and the Mode 01 phase all
    #: fit inside it, and the next window opens at ``anchor + k * cycle``.
    #: ``did_interval_seconds`` is the *end-to-start* gap between windows
    #: (its historic meaning, kept) and cannot express a cycle rate — a
    #: 0.25 s interval ran as a ~0.95 s cycle. Setting this field is the
    #: migration; ``None`` leaves an existing profile on the interval policy
    #: exactly as saved. Ignored by ``did_continuous`` (no cycle exists).
    did_cycle_seconds: float | None = None
    #: Stamp every channel polled in one round with the round's start time, so
    #: a sample spanning several requests reads as one row. The true arrival
    #: instant is kept per reading (``acquired_at`` metadata), so this changes
    #: how a sample is *presented*, not what was measured. Off for `default`:
    #: it alters the arrival pattern the resampler and __roc derivatives see,
    #: which wants validating (multi-pid-polling.md 4.3) before the training
    #: corpus depends on it.
    round_timestamps: bool = False
    #: Poll every channel in this profile at exactly this rate, bypassing
    #: ``poll_rate_hz × tier × weight`` entirely.
    #:
    #: Weights express a *relative share* of the historic tier cadence, and
    #: with `poll_rate_hz` at 1.0 that caps any channel at 1 Hz no matter how
    #: much bus budget is available — measured 2026-08-04, when raising the
    #: budget 8 → 30 req/s moved the achieved rate not at all because demand
    #: never reached the old ceiling. A capture wants an *absolute* rate
    #: ("give me lambda at 20 Hz"), which no combination of the relative knobs
    #: can express. The bus is nowhere near the constraint: the same session
    #: measured 478 channel-reads/s over 6 batched PIDs with zero timeouts,
    #: zero desyncs and zero `rx_dropped`, at 4.9 ms p50.
    #:
    #: ``None`` keeps the relative model. The bus budget still gates what is
    #: actually issued, so this raises *demand* — it cannot exceed the budget.
    target_rate_hz: float | None = None
    #: Hold the Mode-22 session open for the whole capture instead of polling
    #: DIDs in short windows.
    #:
    #: The windowed model exists so Mode 01 gets the bus back — the two are
    #: mutually exclusive on this ECM. A tuning capture that wants only DIDs
    #: has nothing to hand the bus back *to*, and windowing then costs it
    #: everything: measured 2026-08-05, each brief session cost ~40 s of the
    #: Mode-01 channels it interrupted, because they earn backoff while the
    #: session refuses them. One mode for the whole capture has no such edge.
    #:
    #: Safe because the reads themselves keep the session alive (bench
    #: 2026-08-04: 658 DID reads/s for 8 s, zero failures, no explicit
    #: keepalive needed). Requires ``mode01_enabled: false`` — a profile that
    #: pins the session open while still expecting Mode 01 answers would
    #: starve every PID it lists.
    did_continuous: bool = False
    bus_budget_override: float | None = None
    #: Channels that keep their requested cadence when this profile contends
    #: for bus budget. ``None`` inherits the daemon-wide monitoring lane;
    #: an empty tuple deliberately disables priority for this profile.
    priority_channels: tuple[str, ...] | None = None
    #: Evidence behind the requested rate/budget, kept beside the profile so
    #: a vehicle-specific envelope is not mistaken for a platform constant.
    expected_total_demand_hz: float | None = Field(default=None, gt=0)
    capacity_evidence: str = ""
    #: Channels that must be observed together for this profile's headline
    #: analysis to yield a sample. Verified as a *tuple rate*, because the
    #: per-channel rates can all pass while the tuple almost never completes.
    required_tuple: tuple[str, ...] = ()
    #: Per-channel floor the capture contract enforces. ``None`` means the
    #: profile makes no rate promise and the contract only reports.
    min_achieved_rate_hz: float | None = Field(default=None, gt=0)
    #: Largest acquisition spread (seconds, first to last member arrival) a
    #: round may show and still count as one complete tuple (C03.7). A shared
    #: round stamp says the members were *asked for* together; this is the
    #: bound on how far apart they actually *arrived*. ``None`` derives half a
    #: target-rate period, or ``capture_quality.DEFAULT_MAX_TUPLE_SKEW_S``
    #: without a target rate. Must fit inside one round period.
    max_tuple_skew_s: float | None = Field(default=None, gt=0)
    #: Prompt for a named calibration, without requiring one. **Advisory by
    #: operator decision 2026-08-10** — an earlier version refused to arm and
    #: that got in the way more than it helped. Nothing is silently lost: the
    #: manifest still records ``tune_revision: null`` and the digest still
    #: reports the source as ``unknown``, so a comparison across a flash can
    #: tell the calibration was unrecorded. Do not restore the refusal.
    requires_tune_revision: bool = False
    corpus_eligible: bool = False
    #: Narrow exception to corpus isolation: these captures may be evidence
    #: for only the named behavior-surface recipes. They remain excluded from
    #: IF/LSTM discovery and from every unlisted behavior model.
    behavior_model_recipes: tuple[str, ...] = ()
    storage_prefix: str = ""
    #: P02.10 (operator 2026-09-26): store every broadcast CAN frame in this
    #: capture's session. Normal collection stores a frame only when its
    #: payload changes (an unchanged id re-written once per
    #: ``collection.broadcast_stale_timeout_seconds``), which keeps every value
    #: but not sub-second frame timing or per-id frame counts; a capture that
    #: needs those (receive-loss accounting, broadcast byte discovery, frame
    #: rates) sets this. The ``default`` profile may not: it is the corpus's.
    store_every_frame: bool = False
    #: Store every sample of the USB ADC's analog inputs (fuel pressure) at
    #: the firmware's full rate (firmware/usb-adc v2: 503.9 Hz, anti-alias
    #: filtered to 180 Hz) instead of the mean of each ~10 ms, about 100 Hz.
    #: For studying the rail's injector pulsation; 5x the sensor rows.
    analog_full_rate: bool = False

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SAFE_NAME.fullmatch(normalized):
            raise ValueError("name must be a lowercase slug (letters, digits, hyphens)")
        return normalized

    @field_validator("mode01")
    @classmethod
    def _valid_weights(cls, value: dict[str, float]) -> dict[str, float]:
        cleaned: dict[str, float] = {}
        for channel, weight in value.items():
            name = channel.strip().upper()
            if not name:
                raise ValueError("Mode-01 channel names must not be blank")
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError(f"Mode-01 rate weight for {name} must be finite and positive")
            cleaned[name] = float(weight)
        return cleaned

    @field_validator("mode22_rate_weights")
    @classmethod
    def _valid_did_weights(cls, value: dict[str, float]) -> dict[str, float]:
        cleaned: dict[str, float] = {}
        for channel, weight in value.items():
            name = channel.strip().upper()
            if not name:
                raise ValueError("Mode-22 rate-weight keys must not be blank")
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError(f"Mode-22 rate weight for {name} must be finite and positive")
            cleaned[name] = float(weight)
        return cleaned

    @field_validator("memory_rate_weights")
    @classmethod
    def _valid_memory_weights(cls, value: dict[str, float]) -> dict[str, float]:
        cleaned: dict[str, float] = {}
        for channel, weight in value.items():
            name = channel.strip()
            if not name:
                raise ValueError("memory rate-weight keys must not be blank")
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError(f"memory rate weight for {name} must be finite and positive")
            if name in cleaned:
                raise ValueError(f"memory rate weight for {name} is given twice")
            cleaned[name] = float(weight)
        return cleaned

    @model_validator(mode="after")
    def _memory_weights_are_selected(self) -> CaptureProfile:
        """A memory weight must name a channel ``memory:`` selects.

        A window name is refused by name: weighting it would read as a
        per-window rate, but the rate a window runs at is derived from its
        members (see ``memory_rate_weights``), so the key would do nothing.
        Catalog membership itself is checked where ``memory:`` is (the store's
        save and the daemon start, ``select_memory_channels``).
        """
        selected = {item.strip().upper() for item in self.memory}
        unknown = sorted(name for name in self.memory_rate_weights if name.upper() not in selected)
        if not unknown:
            return self
        windows = _memory_window_members()
        hints = [
            f"{name} is a memory window, not a channel; weight its member channels "
            f"({', '.join(windows[name])}): a window runs at its fastest selected member's weight"
            for name in unknown
            if name in windows
        ]
        plain = [name for name in unknown if name not in windows]
        parts = []
        if plain:
            parts.append(
                "memory_rate_weights names channels not selected by memory: " + ", ".join(plain)
            )
        parts.extend(hints)
        raise ValueError("; ".join(parts))

    @field_validator("mode22")
    @classmethod
    def _valid_dids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(cleaned) != len(value):
            raise ValueError("Mode-22 DIDs must be non-blank and unique")
        return cleaned

    @field_validator("kline")
    @classmethod
    def _valid_kline(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(cleaned) != len(value):
            raise ValueError("K-line channels must be non-blank and unique")
        return cleaned

    @field_validator("priority_channels")
    @classmethod
    def _valid_priority_channels(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        cleaned = tuple(dict.fromkeys(item.strip().upper() for item in value if item.strip()))
        if len(cleaned) != len(value):
            raise ValueError("priority channels must be non-blank and unique")
        return cleaned

    @model_validator(mode="after")
    def _did_weights_are_selected(self) -> CaptureProfile:
        selected = {item.upper() for item in self.mode22}
        unknown = sorted(set(self.mode22_rate_weights) - selected)
        if unknown:
            raise ValueError(
                "mode22_rate_weights names DIDs not selected by mode22: " + ", ".join(unknown)
            )
        return self

    @field_validator("behavior_model_recipes")
    @classmethod
    def _valid_behavior_recipes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(cleaned) != len(value):
            raise ValueError("behavior-model recipe names must be non-blank and unique")
        return cleaned

    @field_validator("did_interval_seconds")
    @classmethod
    def _valid_did_interval(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("did_interval_seconds must be positive")
        return value

    @field_validator("did_cycle_seconds")
    @classmethod
    def _valid_did_cycle(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("did_cycle_seconds must be positive")
        return value

    @model_validator(mode="after")
    def _cycle_needs_windows(self) -> CaptureProfile:
        if self.did_cycle_seconds is not None and self.did_continuous:
            raise ValueError(
                "did_cycle_seconds schedules Mode-22 windows; did_continuous holds the "
                "session open and has no cycle"
            )
        return self

    @model_validator(mode="after")
    def _continuous_requires_did_only(self) -> CaptureProfile:
        """A pinned session and Mode-01 expectations are incompatible.

        Caught here rather than at run time: the failure mode is silent — the
        PIDs simply never answer — and it would look like a dead channel set
        rather than a configuration error.
        """
        if self.did_continuous:
            if self.mode01_enabled and self.mode01:
                raise ValueError(
                    "did_continuous holds the Mode-22 session open, which blocks Mode 01; "
                    "set mode01_enabled: false or clear mode01"
                )
            if not self.mode22:
                raise ValueError("did_continuous requires at least one Mode-22 DID")
        return self

    @field_validator("target_rate_hz")
    @classmethod
    def _valid_target_rate(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if value <= 0:
            raise ValueError("target_rate_hz must be positive")
        if value > MAX_TARGET_RATE_HZ:
            raise ValueError(f"target_rate_hz must not exceed {MAX_TARGET_RATE_HZ}")
        return float(value)

    @field_validator("bus_budget_override")
    @classmethod
    def _valid_budget(cls, value: float | None) -> float | None:
        if value is not None and not (
            MIN_BUDGET_REQUESTS_PER_SECOND <= value <= MAX_BUDGET_REQUESTS_PER_SECOND
        ):
            raise ValueError(
                f"bus budget must be between {MIN_BUDGET_REQUESTS_PER_SECOND:g} and "
                f"{MAX_BUDGET_REQUESTS_PER_SECOND:g} requests/s"
            )
        return value

    @model_validator(mode="after")
    def _tuple_skew_fits_a_round(self) -> CaptureProfile:
        """A skew bound wider than the round period admits readings from two
        different rounds' worth of time and means nothing as a tuple check."""
        if self.max_tuple_skew_s is None or self.target_rate_hz is None:
            return self
        period = 1.0 / self.target_rate_hz
        if self.max_tuple_skew_s > period:
            raise ValueError(
                f"max_tuple_skew_s ({self.max_tuple_skew_s}s) exceeds the round period "
                f"({period:.4g}s at {self.target_rate_hz} Hz)"
            )
        return self

    @model_validator(mode="after")
    def _mode01_batch_groups_are_a_disjoint_subset(self) -> CaptureProfile:
        """Batch partitions must name selected PIDs exactly once."""
        if not self.mode01_batch_groups:
            return self
        selected = {name.upper() for name in self.mode01}
        if "AUTO" in selected:
            raise ValueError("mode01_batch_groups requires explicit mode01 channels, not AUTO")
        seen: set[str] = set()
        duplicates: set[str] = set()
        unknown: set[str] = set()
        for group in self.mode01_batch_groups:
            if not group:
                raise ValueError("mode01_batch_groups cannot contain an empty group")
            for name in group:
                upper = name.upper()
                if upper in seen:
                    duplicates.add(upper)
                seen.add(upper)
                if upper not in selected:
                    unknown.add(upper)
        if duplicates:
            raise ValueError(
                "mode01_batch_groups repeats channels: " + ", ".join(sorted(duplicates))
            )
        if unknown:
            raise ValueError(
                "mode01_batch_groups names channels outside mode01: " + ", ".join(sorted(unknown))
            )
        return self

    @model_validator(mode="after")
    def _required_tuple_is_polled(self) -> CaptureProfile:
        """Every tuple member must actually be requested by this profile.

        A tuple naming a channel the profile never polls would verify as
        permanently 0 Hz and read as a vehicle fault. Catching it here makes
        the contract's failures mean "the capture underperformed", never "the
        contract was written wrong".
        """
        if not self.required_tuple:
            return self
        polled = {name.upper() for name in self.mode01} | {name.upper() for name in self.mode22}
        if "AUTO" in polled:
            return self
        unpolled = sorted({name.upper() for name in self.required_tuple} - polled)
        if unpolled:
            raise ValueError(
                "required_tuple names channels this profile does not poll: " + ", ".join(unpolled)
            )
        return self

    @model_validator(mode="after")
    def _required_tuple_weights_meet_floor(self) -> CaptureProfile:
        """Context weights cannot silently weaken a required tuple's floor."""
        if self.target_rate_hz is None or self.min_achieved_rate_hz is None:
            return self
        below: list[str] = []
        for channel in self.required_tuple:
            upper = channel.upper()
            weight = self.mode01.get(upper, self.mode22_rate_weights.get(upper, 1.0))
            if self.target_rate_hz * weight < self.min_achieved_rate_hz:
                below.append(upper)
        if below:
            raise ValueError(
                "required_tuple weighted demand is below min_achieved_rate_hz: "
                + ", ".join(sorted(below))
            )
        return self

    @model_validator(mode="after")
    def _raised_budget_needs_evidence(self) -> CaptureProfile:
        """A budget above the storage sink's known-safe line must cite why.

        25 req/s backed the storage writer up until it dropped 2491 records on
        2026-07-29 and the writer-side root cause was never established. That
        made the number a measured constraint, and a measured constraint
        belongs in a validator rather than in the comment where it has been
        sitting — the comment did not stop anything.
        """
        budget = self.bus_budget_override
        if budget is not None and budget > STORAGE_SAFE_REQUESTS_PER_SECOND:
            if not self.capacity_evidence.strip():
                raise ValueError(
                    f"bus_budget_override above {STORAGE_SAFE_REQUESTS_PER_SECOND:g} req/s "
                    "requires capacity_evidence: 25 req/s dropped 2491 records on 2026-07-29 "
                    "and the writer-side cause was never found, so exceeding it is a claim "
                    "that needs a measurement behind it"
                )
        return self

    @field_validator("storage_prefix")
    @classmethod
    def _valid_prefix(cls, value: str) -> str:
        cleaned = value.strip().rstrip("-")
        if cleaned and not _SAFE_PREFIX.fullmatch(cleaned):
            raise ValueError(
                "storage_prefix must look like '_capture-<slug>' so the session stays "
                "listable and resolvable through /control/sessions"
            )
        return cleaned

    @model_validator(mode="after")
    def _storage_policy(self) -> CaptureProfile:
        if self.name == DEFAULT_PROFILE_NAME:
            if self.storage_prefix:
                raise ValueError("the default profile uses normal timestamped sessions")
            if not self.corpus_eligible:
                raise ValueError("the default profile must remain corpus eligible")
        elif self.corpus_eligible:
            # Operator decision 2026-09-25 (R03.12a): a capture polls another
            # channel set at other rates, so it never represents the default
            # profile the detectors learned; it may not join the corpus.
            raise ValueError(
                f"capture profile {self.name!r} may not be corpus_eligible: only the "
                "default profile trains and scores the detectors (operator decision "
                "2026-09-25, R03.12)"
            )
        elif not self.storage_prefix:
            raise ValueError("non-corpus profiles require a '_capture-<slug>' storage_prefix")
        if self.name == DEFAULT_PROFILE_NAME and self.store_every_frame:
            raise ValueError(
                "the default profile stores CAN frames change-only (P02.10, operator "
                "2026-09-26); every-frame storage belongs to a capture profile"
            )
        return self

    # ------------------------------------------------------------------
    # Mode-22 window timing: the one precedence rule (R02.3)
    # ------------------------------------------------------------------

    def did_window_cycle(self, session: DidSessionSpec) -> float | None:
        """Start-to-start cycle period: this profile's, else the config's.

        ``None`` when neither sets one: the end-to-start interval policy.
        """
        if self.did_cycle_seconds is not None:
            return self.did_cycle_seconds
        return session.cycle_seconds

    def did_window_interval(self, session: DidSessionSpec) -> float:
        """End-to-start gap between windows: this profile's, else the config's."""
        if self.did_interval_seconds is not None:
            return self.did_interval_seconds
        return session.window_interval_seconds

    def did_window_period(self, session: DidSessionSpec) -> float:
        """Seconds from one Mode-22 window opening to the next.

        The one precedence rule, *profile cycle → config cycle → profile
        interval → config interval*: a cycle, from either side, is the
        schedule whenever one exists, and only without one does an interval
        apply. The live scheduler, the window controller and the capture
        preview all read it here. The preview used to spell it *profile
        cycle → profile interval → config cycle*, so a profile interval beat a
        configured cycle there and nowhere else (R02.3).
        """
        cycle = self.did_window_cycle(session)
        return cycle if cycle is not None else self.did_window_interval(session)


class CaptureProfileCatalog(_Strict):
    schema_version: int = SCHEMA_VERSION
    profiles: list[CaptureProfile]

    @model_validator(mode="after")
    def _unique_and_default(self) -> CaptureProfileCatalog:
        names = [profile.name for profile in self.profiles]
        if len(names) != len(set(names)):
            raise ValueError("capture profile names must be unique")
        if DEFAULT_PROFILE_NAME not in names:
            raise ValueError("capture profile catalog requires the reserved default profile")
        return self

    def by_name(self, name: str) -> CaptureProfile | None:
        normalized = name.strip().lower()
        return next((profile for profile in self.profiles if profile.name == normalized), None)


def _reset_for_tests() -> None:
    """Forget that the ``broadcast_decode`` retirement and any shadowed
    override were already reported (``src.collection.process_state``)."""
    global _broadcast_decode_warned
    _broadcast_decode_warned = False
    _SHADOW_WARNED.clear()


def _warn_broadcast_decode_retired() -> None:
    global _broadcast_decode_warned
    if _broadcast_decode_warned:
        return
    _broadcast_decode_warned = True
    log.warning(
        "capture profile field broadcast_decode is retired and ignored (P02.8): no capture "
        "stores decoded rows, and every reader decodes the raw frames on read. Saving the "
        "catalog drops it."
    )


def default_profile(collection: CollectionConfig) -> CaptureProfile:
    """The reserved ``default`` profile; it mirrors the active config exactly."""
    default_channels = {name: 1.0 for name in collection.obd_pids}
    return CaptureProfile(
        name=DEFAULT_PROFILE_NAME,
        description="Normal telemetry and anomaly-detection collection",
        notes=(
            "ECM_COOLANT_TEMP is present so an ordinary drive settles the "
            "contested DID 0x1101 offset, which no capture on hand can: the "
            "two recorded offsets differ by exactly 10 degC, and separating "
            "them needs this DID and Mode 01 PID 0x05 in ONE session. Only "
            "the default profile can do that \u2014 the tuning profile "
            "disables Mode 01. Verified 2026-08-06 that 0x1101 was in the "
            "scheduler plan but never requested, because the DID window "
            "polls this list, not the plan: four consecutive windows sent 0 "
            "requests for it while ECM_RPM got one each."
        ),
        mode01=default_channels,
        mode22=tuple(collection.obd_dids),
        corpus_eligible=True,
    )


def seed_catalog(
    collection: CollectionConfig, starter_path: Path = STARTER_CATALOG_PATH
) -> CaptureProfileCatalog:
    """A new vehicle's catalog: ``default`` from config plus the starter profiles.

    The starters are data (``STARTER_CATALOG_PATH``), not code. A missing or
    unreadable starter file yields ``default`` alone rather than failing: a
    catalog with only the serving profile is complete, just unfurnished.
    """
    profiles = [default_profile(collection)]
    if starter_path.exists():
        try:
            starter = CaptureProfileCatalog.model_validate(yaml.safe_load(starter_path.read_text()))
        except (OSError, ValueError, yaml.YAMLError):
            log.exception(
                "starter capture catalog %s is unreadable; seeding default only", starter_path
            )
        else:
            profiles.extend(p for p in starter.profiles if p.name != DEFAULT_PROFILE_NAME)
    return CaptureProfileCatalog(profiles=profiles)


@dataclass(frozen=True)
class CaptureProfileSources:
    """Where a vehicle's capture profiles come from (S03.4).

    Two files, layered by profile name (operator decision 2026-10-04):

    - ``catalog``: the tracked catalog, laptop-authored and never written at
      runtime. A vehicle without one starts from :func:`seed_catalog`, in
      memory.
    - ``overrides``: what this machine saved or deleted from the dashboard.
      An override replaces the tracked profile of the same name; a deletion
      hides it. Every profile not overridden keeps following the repo. ``None``
      reads the tracked catalog alone (a tool with no config).
    """

    catalog: Path
    overrides: Path | None

    @classmethod
    def of(cls, daemon: DaemonConfig) -> CaptureProfileSources:
        return cls(daemon.capture_profiles, daemon.capture_profile_overrides)


class CaptureProfileOverrides(_Strict):
    """This machine's layer over the tracked catalog (S03.4)."""

    schema_version: int = SCHEMA_VERSION
    #: Profiles saved here: each replaces the tracked one of its name, or adds one.
    profiles: list[CaptureProfile] = Field(default_factory=list)
    #: Tracked profiles deleted here.
    deleted: list[str] = Field(default_factory=list)
    #: The tracked profile each override replaced, as it was when saved
    #: (:func:`profile_digest`), so a later repo change to it is reported.
    base_digests: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> CaptureProfileOverrides:
        names = [profile.name for profile in self.profiles]
        if len(names) != len(set(names)):
            raise ValueError("capture profile override names must be unique")
        if DEFAULT_PROFILE_NAME in self.deleted:
            raise ValueError("the default capture profile cannot be deleted")
        if set(names) & set(self.deleted):
            raise ValueError("a capture profile cannot be both overridden and deleted")
        return self


#: Fields added after overrides started recording ``base_digests``, left out
#: of the digest while at their default. An override saved before the field
#: existed recorded a digest without it, so including an empty value would
#: report every overridden profile as "changed in the repo" although nothing
#: about it changed.
_DIGEST_OMIT_WHEN_EMPTY = ("memory_rate_weights",)


def profile_digest(profile: CaptureProfile) -> str:
    """A stable fingerprint of a profile's content."""
    dumped = profile.model_dump(mode="json")
    for key in _DIGEST_OMIT_WHEN_EMPTY:
        if not dumped.get(key):
            dumped.pop(key, None)
    payload = json.dumps(dumped, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def save_capture_profiles(path: Path, catalog: CaptureProfileCatalog) -> None:
    """Write a whole catalog: authoring tools only, never the daemon (S03.4)."""
    payload = catalog.model_dump(mode="json")
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False), mkdir=True)


def _read_catalog(path: Path) -> CaptureProfileCatalog | None:
    if not path.exists():
        return None
    return CaptureProfileCatalog.model_validate(yaml.safe_load(path.read_text()))


def load_overrides(path: Path | None) -> CaptureProfileOverrides:
    if path is None or not path.exists():
        return CaptureProfileOverrides()
    return CaptureProfileOverrides.model_validate(yaml.safe_load(path.read_text()) or {})


def save_overrides(path: Path, overrides: CaptureProfileOverrides) -> None:
    payload = overrides.model_dump(mode="json")
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False), mkdir=True)


#: ``(name, digest now in the repo)`` already reported as shadowed, so a
#: catalog read on every API call reports each change once per process.
_SHADOW_WARNED: set[tuple[str, str]] = set()


def layer_catalog(
    base: CaptureProfileCatalog, overrides: CaptureProfileOverrides
) -> CaptureProfileCatalog:
    """``base`` with ``overrides`` applied by name; tracked order kept, additions last."""
    replaced = {profile.name: profile for profile in overrides.profiles}
    deleted = set(overrides.deleted)
    profiles: list[CaptureProfile] = []
    for profile in base.profiles:
        if profile.name in deleted:
            continue
        override = replaced.pop(profile.name, None)
        if override is None:
            profiles.append(profile)
            continue
        current = profile_digest(profile)
        saved = overrides.base_digests.get(profile.name)
        if saved != current and (profile.name, current) not in _SHADOW_WARNED:
            _SHADOW_WARNED.add((profile.name, current))
            log.warning(
                "capture profile %r is overridden on this machine, and the repo's version "
                "has changed since; the override still wins (revert it to take the repo's)",
                profile.name,
                extra={"profile": profile.name},
            )
        profiles.append(override)
    profiles.extend(sorted(replaced.values(), key=lambda item: item.name))
    return CaptureProfileCatalog(profiles=profiles)


def load_capture_profiles(
    sources: CaptureProfileSources, collection: CollectionConfig
) -> CaptureProfileCatalog:
    """A vehicle's effective catalog: the tracked one (or the seed) under this
    machine's overrides. Never writes anything.

    The tracked catalog used to double as the saved one, and the daemon wrote
    it on the Pi, which then blocked ``git pull``/``canary-update`` (the last
    time, 2026-10-04, for two empty ``kline: []``). Nothing appends, upgrades or
    restores a profile behind the operator's back: a deletion here stays
    deleted, and only a revert brings the repo's version back.
    """
    base = _read_catalog(sources.catalog) or seed_catalog(collection)
    return layer_catalog(base, load_overrides(sources.overrides))


def _serving_default(
    sources: CaptureProfileSources, collection: CollectionConfig | None
) -> tuple[bool, CaptureProfile | None]:
    """``(resolved, default profile)`` for the serving-set readers, read-only."""
    base = _read_catalog(sources.catalog)
    if base is None:
        if collection is None:
            override = load_overrides(sources.overrides)
            default = next((p for p in override.profiles if p.name == DEFAULT_PROFILE_NAME), None)
            return default is not None, default
        base = seed_catalog(collection)
    return True, layer_catalog(base, load_overrides(sources.overrides)).by_name(
        DEFAULT_PROFILE_NAME
    )


def _sources_stamp(sources: CaptureProfileSources) -> tuple[int, int | None] | None:
    """Both files' mtimes, or ``None`` when the tracked catalog is absent (then
    the answer depends on the collection config, so it is not cached)."""
    try:
        catalog = sources.catalog.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    overrides = None
    if sources.overrides is not None:
        try:
            overrides = sources.overrides.stat().st_mtime_ns
        except FileNotFoundError:
            overrides = None
    return catalog, overrides


_SERVING_DID_CACHE: dict[
    CaptureProfileSources, tuple[tuple[int, int | None], frozenset[str] | None]
] = {}


def serving_mode22_dids(
    sources: CaptureProfileSources, collection: CollectionConfig | None = None
) -> frozenset[str] | None:
    """Upper-cased Mode-22 DID names the *serving* (``default``) profile polls.

    The question a trainer has to ask before accepting a ``did:`` feature: will
    the daemon ever observe this channel while it is scoring? Only the
    ``default`` profile runs during ordinary (model-serving) collection; a DID
    that exists in the corpus solely because a ``_capture-*`` research profile
    polled it is NaN forever at serve.

    Read-only: feature selection must not rewrite operator data. The answer is
    the effective ``default`` (tracked catalog under this machine's overrides,
    S03.4); with no catalog the ``default`` profile *is* ``collection.obd_dids``
    (see :func:`seed_catalog`). A laptop does not see the Pi's overrides, so a
    ``default`` edited in the Pi's dashboard is invisible to a laptop retrain.

    ``None`` means *unresolvable* — no catalog on disk and no collection config
    to seed one from — which is a different answer from an empty set ("the
    serving profile polls no DIDs"). Callers must not conflate them.
    """
    stamp = _sources_stamp(sources)
    cached = _SERVING_DID_CACHE.get(sources)
    if stamp is not None and cached is not None and cached[0] == stamp:
        return cached[1]
    resolved, default = _serving_default(sources, collection)
    polled: frozenset[str] | None
    if not resolved:
        polled = None
    elif default is None or not default.mode22_enabled:
        polled = frozenset()
    else:
        polled = frozenset(item.strip().upper() for item in default.mode22 if item.strip())
    if stamp is not None:
        _SERVING_DID_CACHE[sources] = (stamp, polled)
    return polled


#: The serving profile's Mode 01 selection, keyed like ``_SERVING_DID_CACHE``.
_SERVING_MODE01_CACHE: dict[
    CaptureProfileSources, tuple[tuple[int, int | None], frozenset[str] | None]
] = {}

#: In a :func:`serving_mode01_channels` answer: every supported PID is polled.
MODE01_AUTO = "AUTO"


def serving_mode01_channels(
    sources: CaptureProfileSources, collection: CollectionConfig | None = None
) -> frozenset[str] | None:
    """Upper-cased Mode 01 channel names the serving (``default``) profile polls.

    :func:`serving_mode22_dids`'s question for Mode 01: a PID that the serving
    profile no longer polls is NaN for a model's whole serving life, however
    much of the old corpus carries it. The Xterra ``default`` dropped ten PIDs
    on 2026-09-26 (D03.34), and without this answer the next retrain would have
    learned them. Contains :data:`MODE01_AUTO` when the profile polls every
    supported PID. Empty when Mode 01 is disabled. ``None`` is unresolvable,
    as for DIDs. Same layering and precedence.
    """
    stamp = _sources_stamp(sources)
    cached = _SERVING_MODE01_CACHE.get(sources)
    if stamp is not None and cached is not None and cached[0] == stamp:
        return cached[1]
    resolved, default = _serving_default(sources, collection)
    polled: frozenset[str] | None
    if not resolved:
        polled = None
    elif default is None:
        polled = frozenset()
    else:
        polled = _mode01_selection(default)
    if stamp is not None:
        _SERVING_MODE01_CACHE[sources] = (stamp, polled)
    return polled


def _mode01_selection(profile: CaptureProfile) -> frozenset[str]:
    if not profile.mode01_enabled:
        return frozenset()
    return frozenset(name.upper() for name in profile.mode01)


def channel_satisfied(channel: str, profile: CaptureProfile, seen: frozenset[str]) -> bool:
    """Can ``profile`` supply ``channel``, following derived inputs?

    Namespaces reach the vehicle by different routes, and flattening them to a
    bare name is what makes a naive check cry wolf: `derived:` channels are
    *computed* and so never appear in any poll list, while `decoded:` ones ride
    the broadcast bus rather than occupying a request slot. Every profile
    records the broadcast frames, and the live source and every reader decode
    them (P02.8), so no profile can starve a `decoded:` input.
    """
    namespace, separator, name = channel.partition(":")
    if not separator:
        namespace, name = "obd", channel
    upper = name.upper()
    if namespace == "obd":
        return upper in profile.mode01 or "AUTO" in profile.mode01
    if namespace == "did":
        return any(upper == item.upper() for item in profile.mode22)
    if namespace == "decoded":
        return True
    if namespace == "derived":
        groups = DERIVED_CHANNEL_INPUTS.get(channel)
        if groups is None or channel in seen:
            # An unmapped derived channel is unresolvable, not known-missing;
            # warning on it would be a guess dressed up as a finding.
            return True
        nested = seen | {channel}
        return all(
            any(channel_satisfied(candidate, profile, nested) for candidate in group)
            for group in groups
        )
    return True


class CaptureProfileStore:
    def __init__(
        self,
        sources: CaptureProfileSources,
        collection: CollectionConfig,
        behavior_models_path: Path,
        *,
        custom_pids_path: Path | None = None,
        kline_catalog: KlineCatalog | None = None,
    ):
        self.sources = sources
        self.collection = collection
        self.behavior_models_path = behavior_models_path
        #: The daemon's custom-PID file, so the preview's request universe
        #: matches the live one. ``None`` previews without custom PIDs.
        self.custom_pids_path = custom_pids_path
        #: What a profile's ``kline:`` list is resolved against at save
        #: (H05.8). ``None`` refuses to save any profile that lists K-line
        #: channels: an unchecked list is what left the collector idle.
        self.kline_catalog = kline_catalog

    def load(self) -> CaptureProfileCatalog:
        return load_capture_profiles(self.sources, self.collection)

    def _base(self) -> CaptureProfileCatalog:
        return _read_catalog(self.sources.catalog) or seed_catalog(self.collection)

    def _overrides_path(self) -> Path:
        if self.sources.overrides is None:
            raise ValueError("this capture profile store is read-only (no overrides file)")
        return self.sources.overrides

    def resolved_plan(self, profile: CaptureProfile) -> dict[str, Any]:
        """The identifier-level plan ``profile`` would run: the live plan, asked early.

        One function with the live metrics (``obd_scheduler.preview_polling_plan``
        runs the scheduler's own spec, request and plan builders with the
        candidate in place of the active profile). The copy this replaced
        disagreed with live on DID-window precedence, capped DID rates live
        does not, and ignored ``round_timestamps`` (R02.3). The import is local
        because the scheduler already depends on this profile module.
        """
        from src.collection.obd_scheduler import preview_polling_plan

        return preview_polling_plan(
            self.collection, profile, custom_pids_path=self.custom_pids_path
        )

    def save(
        self,
        profile: CaptureProfile,
        *,
        previous_name: str | None = None,
    ) -> None:
        """Save ``profile``. Which behavior models it would starve is
        :func:`src.features.capture_inputs.model_input_warnings`'s to say: the
        behavior models sit above collection (R09.6)."""
        catalog = self.load()
        target = (previous_name or profile.name).strip().lower()
        profiles = [item for item in catalog.profiles if item.name != target]
        if (
            previous_name
            and target == DEFAULT_PROFILE_NAME
            and profile.name != DEFAULT_PROFILE_NAME
        ):
            raise ValueError("the default profile cannot be renamed")
        if any(item.name == profile.name for item in profiles):
            raise ValueError(f"capture profile already exists: {profile.name}")
        self._check_memory(profile)
        self._check_kline(profile)
        # Only this machine's layer is written (S03.4); the tracked catalog never is.
        path = self._overrides_path()
        base = {item.name: item for item in self._base().profiles}
        overrides = load_overrides(path)
        kept = [item for item in overrides.profiles if item.name not in {target, profile.name}]
        deleted = [name for name in overrides.deleted if name != profile.name]
        digests = {
            name: digest
            for name, digest in overrides.base_digests.items()
            if name not in {target, profile.name}
        }
        if target != profile.name and target in base:
            deleted.append(target)  # a rename hides the tracked original
        if profile.name in base:
            digests[profile.name] = profile_digest(base[profile.name])
        save_overrides(
            path,
            CaptureProfileOverrides(
                profiles=sorted([*kept, profile], key=lambda item: item.name),
                deleted=sorted(set(deleted)),
                base_digests=digests,
            ),
        )

    def _check_memory(self, profile: CaptureProfile) -> None:
        """Refuse a ``memory:`` entry the catalog does not hold.

        The daemon start refuses the same name (``profile_memory_union``), so
        an override saved with it would keep the next start from collecting
        at all; refusing here keeps it out of the overrides file.
        """
        try:
            select_memory_channels(profile.memory)
        except ValueError as exc:
            raise ValueError(f"memory: {exc}") from exc

    def _check_kline(self, profile: CaptureProfile) -> None:
        """Resolve ``profile.kline`` exactly as the K-line collector will
        (:meth:`~src.collection.kline.plan.KlineCatalog.resolve`), or refuse.

        The collector refuses an unreadable list at run time, logged once, and
        the capture then records no K-line at all; refusing here puts the same
        reason in front of the operator while the profile is still open.
        """
        if not profile.kline:
            return
        if self.kline_catalog is None:
            raise ValueError(
                f"capture profile {profile.name} lists K-line channels, but this store "
                "has no K-line catalog to check them against"
            )
        try:
            self.kline_catalog.resolve(profile.kline)
        except (LookupError, ValueError) as exc:
            # KlinePlanError is a ValueError; a vehicle with no K-line profile
            # is a LookupError. Both are a refusal of this list.
            raise ValueError(f"kline: {exc}") from exc

    def delete(self, name: str) -> None:
        normalized = name.strip().lower()
        if normalized == DEFAULT_PROFILE_NAME:
            raise ValueError("the default capture profile cannot be deleted")
        if self.load().by_name(normalized) is None:
            raise KeyError(normalized)
        path = self._overrides_path()
        overrides = load_overrides(path)
        deleted = set(overrides.deleted)
        if self._base().by_name(normalized) is not None:
            deleted.add(normalized)
        save_overrides(
            path,
            CaptureProfileOverrides(
                profiles=[item for item in overrides.profiles if item.name != normalized],
                deleted=sorted(deleted),
                base_digests={
                    name: digest
                    for name, digest in overrides.base_digests.items()
                    if name != normalized
                },
            ),
        )

    def revert(self, name: str) -> CaptureProfile | None:
        """Drop this machine's override or deletion of ``name``; return the
        profile now in effect (the repo's), or ``None`` if the repo has none.

        Raises ``KeyError`` when this machine has nothing to revert for it.
        """
        normalized = name.strip().lower()
        path = self._overrides_path()
        overrides = load_overrides(path)
        overridden = any(item.name == normalized for item in overrides.profiles)
        if not overridden and normalized not in overrides.deleted:
            raise KeyError(normalized)
        save_overrides(
            path,
            CaptureProfileOverrides(
                profiles=[item for item in overrides.profiles if item.name != normalized],
                deleted=[item for item in overrides.deleted if item != normalized],
                base_digests={
                    key: digest
                    for key, digest in overrides.base_digests.items()
                    if key != normalized
                },
            ),
        )
        return self._base().by_name(normalized)

    def overridden_names(self) -> dict[str, str]:
        """``{name: "overridden" | "added" | "deleted"}`` for this machine's layer."""
        base = {item.name for item in self._base().profiles}
        overrides = load_overrides(self.sources.overrides)
        out = {
            item.name: ("overridden" if item.name in base else "added")
            for item in overrides.profiles
        }
        out.update({name: "deleted" for name in overrides.deleted})
        return out


__all__ = [
    "CaptureProfileOverrides",
    "CaptureProfileSources",
    "channel_satisfied",
    "CAPTURE_CONTEXT_MODE01",
    "DEFAULT_PROFILE_NAME",
    "STARTER_CATALOG_PATH",
    "STORAGE_SAFE_REQUESTS_PER_SECOND",
    "CaptureProfile",
    "CaptureProfileCatalog",
    "CaptureProfileStore",
    "FUEL_DELIVERY_TUPLE",
    "default_profile",
    "layer_catalog",
    "load_capture_profiles",
    "load_overrides",
    "profile_digest",
    "seed_catalog",
]
