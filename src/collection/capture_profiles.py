"""Strict, vehicle-scoped capture profile persistence.

Capture profiles describe *collection*, not vehicle identity.  The catalog is
stored beside the active vehicle's other scoped artifacts and is deliberately
small enough to reload on every API operation.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.analysis.derived import DERIVED_CHANNEL_INPUTS
from src.atomic_write import atomic_write_text
from src.collection.bus_budget import (
    MAX_BUDGET_REQUESTS_PER_SECOND,
    MIN_BUDGET_REQUESTS_PER_SECOND,
)
from src.config import CollectionConfig
from src.decode.behavior_models import load_behavior_models

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_PROFILE_NAME = "default"
FUEL_MAP_PROFILE_NAME = "fuel-map"
#: Bump the seed's ``builtin_revision`` whenever a built-in profile's shipped
#: definition changes meaningfully. ``load_capture_profiles`` then upgrades
#: saved copies that are older *and* untouched by the operator. Without this a
#: catalog written once pinned the built-in forever: the 2026-08-10 fuel-map
#: capture ran at 0.28-0.78 Hz because a saved copy predating ``target_rate_hz``
#: shadowed a source-level correction, and nothing in the system could say so.
BUILTIN_REVISION = 1

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
#: ``src.control.sessions._CAPTURE_DIR_RE`` still records telemetry but becomes
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

#: Fueling channels that resolve a WOT transient and therefore want the
#: profile's full rate. A 5 s pull is the unit of work; anything slower than a
#: few Hz here samples the pull two or three times.
FUEL_MAP_FAST_CHANNELS: tuple[str, ...] = (
    "RPM",
    "ENGINE_LOAD",
    "MAF",
    "TIMING_ADVANCE",
    "THROTTLE_POS",
    "O2_WR_LAMBDA_S1",
    "O2_WR_LAMBDA_S2",
    "COMMANDED_EQUIV_RATIO",
    "FUEL_STATUS",
)

#: Context channels: closed-loop adaptation and thermal state, which move on
#: the order of seconds to minutes. Held at a quarter of the fast rate so they
#: stay present for gating without diluting the transient channels — the whole
#: point of the weight being honoured under ``target_rate_hz``.
FUEL_MAP_CONTEXT_CHANNELS: tuple[str, ...] = (
    "INTAKE_TEMP",
    "SPEED",
    "COOLANT_TEMP",
    "SHORT_FUEL_TRIM_1",
    "SHORT_FUEL_TRIM_2",
    "LONG_FUEL_TRIM_1",
    "LONG_FUEL_TRIM_2",
    "EVAPORATIVE_PURGE",
)

FUEL_MAP_CONTEXT_WEIGHT = 0.25

FUEL_MAP_CHANNELS: tuple[str, ...] = (*FUEL_MAP_FAST_CHANNELS, *FUEL_MAP_CONTEXT_CHANNELS)

FUEL_MAP_WEIGHTS: dict[str, float] = {
    **{name: 1.0 for name in FUEL_MAP_FAST_CHANNELS},
    **{name: FUEL_MAP_CONTEXT_WEIGHT for name in FUEL_MAP_CONTEXT_CHANNELS},
}


TUNING_DID_PROFILE_NAME = "tuning-did"

#: The all-DID tuning set. Every entry is identified in data/dids.yaml.
#:
#: Chosen for what Mode 01 *cannot* give: intake cam angle (commanded and
#: measured) exists only as a DID, and the A/F sensor VOLTAGE is the input to
#: the validated voltage→AFR table — the other half of the measured-AFR
#: disagreement found 2026-08-05, where the ECM's own λ (PID 0x24) and the
#: voltage path differ by 3.8 % at WOT. Capturing voltage at rate is what
#: lets that be settled from Pi data alone.
#:
#: RPM and coolant are included despite being free on broadcast CAN: they cost
#: ~1.5 ms each and give same-instant anchors inside the DID round, which is
#: what makes the set self-aligning rather than needing a cross-source join.
#:
#: Both measured cam channels are polled because they are two banks, not one
#: register mirrored — settled by the first capture using this profile, which
#: read them in the same rotation for 12 minutes and found a +0.51 deg offset
#: with the cam static (p=2.3e-29). Dropping either would lose a bank.
TUNING_DID_CHANNELS: tuple[str, ...] = (
    "ECM_RPM",
    "ECM_AF_SENSOR_V_B1",
    "ECM_AF_SENSOR_V_B2",
    "ECM_INJECTOR_PW_B1",
    "ECM_INTAKE_CAM_ANGLE_MEASURED",
    "ECM_INTAKE_CAM_ANGLE_COMMANDED",
    "ECM_INTAKE_CAM_ANGLE_MEASURED_B",
    "ECM_INTAKE_VVT_SOLENOID_DUTY",
    "ECM_INTAKE_VVT_SOLENOID_DUTY_CANDIDATE_B2",
    "ECM_THROTTLE_POS_SENSOR_1",
    "ECM_COOLANT_TEMP",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptureProfile(_Strict):
    name: str
    description: str = ""
    #: Research profiles are intentionally operator-visible but have not yet
    #: earned the same trust as routine captures.  Keep that status in the
    #: schema so every UI/API consumer can render it; parsing words such as
    #: "experimental" out of a description would lose the warning on rename.
    experimental: bool = False
    mode01: dict[str, float] = Field(default_factory=dict)
    mode22: tuple[str, ...] = ()
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
    #: Refuse to start without a named calibration. A fuelling capture compared
    #: against the wrong ROM target map is not wrong-looking, it is wrong — and
    #: the 2026-08-10 capture stored ``tune_revision: null`` with no registry
    #: fallback, so nothing downstream could even flag the ambiguity.
    requires_tune_revision: bool = False
    #: Seed revision this profile's shipped definition came from. ``0`` marks a
    #: profile the operator authored, which is never upgraded.
    builtin_revision: int = Field(default=0, ge=0)
    #: Set once the operator saves over a built-in. Freezes it against seed
    #: upgrades: their edit is a decision, not a stale copy.
    operator_modified: bool = False
    broadcast_decode: bool = True
    corpus_eligible: bool = False
    #: Narrow exception to corpus isolation: these captures may be evidence
    #: for only the named behavior-surface recipes. They remain excluded from
    #: IF/LSTM discovery and from every unlisted behavior model.
    behavior_model_recipes: tuple[str, ...] = ()
    storage_prefix: str = ""

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
            if weight <= 0:
                raise ValueError(f"Mode-01 rate weight for {name} must be positive")
            cleaned[name] = float(weight)
        return cleaned

    @field_validator("mode22")
    @classmethod
    def _valid_dids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(cleaned) != len(value):
            raise ValueError("Mode-22 DIDs must be non-blank and unique")
        return cleaned

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
            if self.storage_prefix:
                raise ValueError("corpus-eligible captures use normal timestamped sessions")
        elif not self.storage_prefix:
            raise ValueError("non-corpus profiles require a '_capture-<slug>' storage_prefix")
        return self


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


def seed_catalog(collection: CollectionConfig) -> CaptureProfileCatalog:
    """Create built-ins; ``default`` mirrors the active config exactly."""
    default_channels = {name: 1.0 for name in collection.obd_pids}
    return CaptureProfileCatalog(
        profiles=[
            CaptureProfile(
                name=DEFAULT_PROFILE_NAME,
                description="Normal telemetry and anomaly-detection collection",
                mode01=default_channels,
                mode22=tuple(collection.obd_dids),
                broadcast_decode=bool(collection.persist_decoded_confidence),
                corpus_eligible=True,
            ),
            CaptureProfile(
                name=FUEL_MAP_PROFILE_NAME,
                description="High-rate fueling and timing capture",
                mode01=dict(FUEL_MAP_WEIGHTS),
                mode22=(),
                # Both knobs, because either alone does nothing. Demand was
                # capped at `poll_rate_hz x tier x weight` = 1 Hz x tier, and
                # throughput at the default 8 req/s budget; the 2026-08-10
                # capture hit both at once and landed at 0.28-0.78 Hz while
                # calling itself "high-rate".
                target_rate_hz=4.0,
                # 9 fast x 4 Hz + 8 context x 1 Hz = 44 channel-reads/s.
                expected_total_demand_hz=44.0,
                bus_budget_override=20.0,
                capacity_evidence=(
                    "44 channel-reads/s against 478/s measured batched on this ECM "
                    "(2026-08-04). Budget held at 20 req/s, under the 24 req/s storage-sink "
                    "line: batching packs 6 PIDs per request, so 44 reads/s needs ~8 req/s "
                    "batched and 44 unbatched. If batching is off the contract reports a "
                    "shortfall rather than silently exceeding the sink's known-safe rate."
                ),
                required_tuple=FUEL_DELIVERY_TUPLE,
                min_achieved_rate_hz=3.0,
                requires_tune_revision=True,
                broadcast_decode=True,
                corpus_eligible=False,
                round_timestamps=True,
                storage_prefix="_capture-fuel-map",
                builtin_revision=BUILTIN_REVISION,
            ),
            CaptureProfile(
                name=TUNING_DID_PROFILE_NAME,
                description="All-DID tuning capture (session held open)",
                # Mode 01 off by construction: holding session 0xC0 blocks it.
                # What that buys is the channels Mode 01 cannot reach at all —
                # cam angle and A/F sensor voltage — sampled synchronously
                # rather than in windows, at 658 reads/s of measured headroom.
                mode01={},
                mode01_enabled=False,
                mode22=TUNING_DID_CHANNELS,
                did_continuous=True,
                target_rate_hz=15.0,
                bus_budget_override=180.0,
                expected_total_demand_hz=165.0,
                capacity_evidence=(
                    "2026-08-04 bench: 658 DID reads/s; 11 DIDs x 15 Hz = 165 reads/s."
                ),
                broadcast_decode=True,
                corpus_eligible=False,
                behavior_model_recipes=(
                    "injector_pw_vs_rpm_throttle_did",
                    "cam_primary_vs_command_rpm_did",
                    "cam_secondary_vs_command_rpm_did",
                ),
                min_achieved_rate_hz=12.0,
                requires_tune_revision=True,
                round_timestamps=True,
                storage_prefix="_capture-tuning-did",
                builtin_revision=BUILTIN_REVISION,
            ),
        ]
    )


def save_capture_profiles(path: Path, catalog: CaptureProfileCatalog) -> None:
    payload = catalog.model_dump(mode="json")
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False), mkdir=True)


def load_capture_profiles(path: Path, collection: CollectionConfig) -> CaptureProfileCatalog:
    """Load the catalog, back-filling and upgrading built-in profiles.

    A saved catalog is authoritative for the profiles the *operator* wrote or
    edited. Built-ins are different: a file written before a built-in existed
    would hide it forever (404 from ``POST /control/capture`` on the vehicle,
    which is exactly where nobody wants to be editing YAML), and — the failure
    this exists for — a file written before a built-in was *corrected* pins the
    stale definition just as permanently.

    So three cases, deliberately distinct:

    * a built-in the file never named is appended;
    * a built-in the file names at an older ``builtin_revision``, never saved
      over by the operator, is replaced by the seed;
    * anything with ``operator_modified`` set, or authored by the operator, is
      left exactly alone. Their edit is a decision, not a stale copy.
    """
    if not path.exists():
        catalog = seed_catalog(collection)
        save_capture_profiles(path, catalog)
        return catalog
    catalog = CaptureProfileCatalog.model_validate(yaml.safe_load(path.read_text()))
    seeds = {profile.name: profile for profile in seed_catalog(collection).profiles}
    resolved: list[CaptureProfile] = []
    changed = False
    for saved in catalog.profiles:
        seed = seeds.pop(saved.name, None)
        if (
            seed is not None
            and not saved.operator_modified
            and saved.builtin_revision < seed.builtin_revision
        ):
            resolved.append(seed)
            changed = True
            log.warning(
                "upgraded built-in capture profile %s from revision %d to %d",
                saved.name,
                saved.builtin_revision,
                seed.builtin_revision,
                extra={
                    "capture_profile": saved.name,
                    "from_revision": saved.builtin_revision,
                    "to_revision": seed.builtin_revision,
                },
            )
            continue
        resolved.append(saved)
    if seeds:
        resolved.extend(seeds.values())
        changed = True
    if changed:
        catalog = catalog.model_copy(update={"profiles": resolved})
        save_capture_profiles(path, catalog)
    return catalog


def _channel_satisfied(channel: str, profile: CaptureProfile, seen: frozenset[str]) -> bool:
    """Can ``profile`` supply ``channel``, following derived inputs?

    Namespaces reach the vehicle by different routes, and flattening them to a
    bare name is what makes a naive check cry wolf: `derived:` channels are
    *computed* and so never appear in any poll list, while `decoded:` ones ride
    the broadcast decoder rather than occupying a request slot.
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
        return profile.broadcast_decode
    if namespace == "derived":
        groups = DERIVED_CHANNEL_INPUTS.get(channel)
        if groups is None or channel in seen:
            # An unmapped derived channel is unresolvable, not known-missing;
            # warning on it would be a guess dressed up as a finding.
            return True
        nested = seen | {channel}
        return all(
            any(_channel_satisfied(candidate, profile, nested) for candidate in group)
            for group in groups
        )
    return True


def model_input_warnings(
    profile: CaptureProfile,
    behavior_models_path: Path,
) -> list[dict[str, Any]]:
    """Name active behavior models starved by a saved default profile."""
    if profile.name != DEFAULT_PROFILE_NAME or not behavior_models_path.exists():
        return []
    warnings: list[dict[str, Any]] = []
    for model in load_behavior_models(behavior_models_path).models:
        required = (
            {model.output, model.axis.channel}
            if model.kind == "surface_1d"
            else {
                model.output,
                *(axis.channel for axis in model.axes),
            }
        )
        for condition in model.conditions:
            base = condition.channel
            for suffix in ("__mean", "__std", "__roc"):
                base = base.removesuffix(suffix)
            required.add(base)
        missing = sorted(
            channel for channel in required if not _channel_satisfied(channel, profile, frozenset())
        )
        if missing:
            warnings.append({"model": model.name, "missing_channels": missing})
    return warnings


class CaptureProfileStore:
    def __init__(self, path: Path, collection: CollectionConfig, behavior_models_path: Path):
        self.path = path
        self.collection = collection
        self.behavior_models_path = behavior_models_path

    def load(self) -> CaptureProfileCatalog:
        return load_capture_profiles(self.path, self.collection)

    def save(
        self,
        profile: CaptureProfile,
        *,
        previous_name: str | None = None,
    ) -> list[dict[str, Any]]:
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
        # An operator save freezes a built-in against future seed upgrades.
        # Marked here rather than trusted from the request body: the flag is
        # provenance, and a client that omitted it would silently re-open the
        # profile to being overwritten by the next seed bump.
        profiles.append(profile.model_copy(update={"operator_modified": True}))
        profiles.sort(key=lambda item: (item.name != DEFAULT_PROFILE_NAME, item.name))
        save_capture_profiles(self.path, CaptureProfileCatalog(profiles=profiles))
        return model_input_warnings(profile, self.behavior_models_path)

    def delete(self, name: str) -> None:
        normalized = name.strip().lower()
        if normalized == DEFAULT_PROFILE_NAME:
            raise ValueError("the default capture profile cannot be deleted")
        catalog = self.load()
        profiles = [item for item in catalog.profiles if item.name != normalized]
        if len(profiles) == len(catalog.profiles):
            raise KeyError(normalized)
        save_capture_profiles(self.path, CaptureProfileCatalog(profiles=profiles))


__all__ = [
    "BUILTIN_REVISION",
    "STORAGE_SAFE_REQUESTS_PER_SECOND",
    "CaptureProfile",
    "CaptureProfileCatalog",
    "CaptureProfileStore",
    "FUEL_DELIVERY_TUPLE",
    "FUEL_MAP_CHANNELS",
    "FUEL_MAP_CONTEXT_CHANNELS",
    "FUEL_MAP_FAST_CHANNELS",
    "FUEL_MAP_WEIGHTS",
    "load_capture_profiles",
    "model_input_warnings",
    "seed_catalog",
]
