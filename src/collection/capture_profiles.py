"""Strict, vehicle-scoped capture profile persistence.

Capture profiles describe *collection*, not vehicle identity.  The catalog is
stored beside the active vehicle's other scoped artifacts and is deliberately
small enough to reload on every API operation.
"""

from __future__ import annotations

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

SCHEMA_VERSION = 1
DEFAULT_PROFILE_NAME = "default"
FUEL_MAP_PROFILE_NAME = "fuel-map"
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

FUEL_MAP_CHANNELS: tuple[str, ...] = (
    "RPM",
    "ENGINE_LOAD",
    "MAF",
    "TIMING_ADVANCE",
    "THROTTLE_POS",
    "INTAKE_TEMP",
    "SPEED",
    "COOLANT_TEMP",
    "O2_WR_LAMBDA_S1",
    "O2_WR_LAMBDA_S2",
    "SHORT_FUEL_TRIM_1",
    "SHORT_FUEL_TRIM_2",
    "LONG_FUEL_TRIM_1",
    "LONG_FUEL_TRIM_2",
    "COMMANDED_EQUIV_RATIO",
    "EVAPORATIVE_PURGE",
    "FUEL_STATUS",
)


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
    broadcast_decode: bool = True
    corpus_eligible: bool = False
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
                mode01={name: 1.0 for name in FUEL_MAP_CHANNELS},
                mode22=(),
                # Deliberately no budget override. Narrowing the channel set
                # already multiplies per-channel rate within the existing
                # budget; raising the budget is the separate, riskier lever.
                # 25 req/s backed the storage sink up until it dropped 2491
                # records on 2026-07-29 and the writer-side root cause was
                # never established, so the operator raises this by hand after
                # the capture-profiles.md 4.4 shakedown, watching
                # `telemetry_sinks.storage.queue_depth`.
                bus_budget_override=None,
                broadcast_decode=True,
                corpus_eligible=False,
                round_timestamps=True,
                storage_prefix="_capture-fuel-map",
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
                round_timestamps=True,
                storage_prefix="_capture-tuning-did",
            ),
        ]
    )


def save_capture_profiles(path: Path, catalog: CaptureProfileCatalog) -> None:
    payload = catalog.model_dump(mode="json")
    atomic_write_text(path, yaml.safe_dump(payload, sort_keys=False), mkdir=True)


def load_capture_profiles(path: Path, collection: CollectionConfig) -> CaptureProfileCatalog:
    """Load the catalog, back-filling built-in profiles the file predates.

    A saved catalog is authoritative for the profiles it names — operator
    edits survive. But a file written before a built-in existed would
    otherwise hide it forever, and the failure is a 404 from
    ``POST /control/captures`` on the vehicle, which is exactly where nobody
    wants to be editing YAML. Missing built-ins are appended and persisted;
    ones the file already names are left untouched.
    """
    if not path.exists():
        catalog = seed_catalog(collection)
        save_capture_profiles(path, catalog)
        return catalog
    catalog = CaptureProfileCatalog.model_validate(yaml.safe_load(path.read_text()))
    known = {profile.name for profile in catalog.profiles}
    missing = [p for p in seed_catalog(collection).profiles if p.name not in known]
    if missing:
        catalog = catalog.model_copy(update={"profiles": [*catalog.profiles, *missing]})
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
        profiles.append(profile)
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
    "CaptureProfile",
    "CaptureProfileCatalog",
    "CaptureProfileStore",
    "FUEL_MAP_CHANNELS",
    "load_capture_profiles",
    "model_input_warnings",
    "seed_catalog",
]
