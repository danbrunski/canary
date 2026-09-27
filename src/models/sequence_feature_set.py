"""Which columns a sequence model conditions on, and which it scores (A03.2/A03.3).

The 2026-09-20 artifact reconstructs 361 columns: every trainable channel with
its ``__mean``/``__std``/``__roc``, four copies of coolant, and driving
conditions (baro, IAT, speed, TPMS, steering, HVAC) scored exactly like engine
responses. Its score therefore measures "unusual trip", and on the altitude
trip it named ``BAROMETRIC_PRESSURE`` on every event.

A *feature set* is a named, recorded choice:

* :data:`ALL_CHANNELS` — the deployed shape: every trainable column is an
  encoder input and a scored target. The default, so the deployed model path
  is unchanged. A schema without a record is this set.
* :data:`ENGINE_RESPONSE` — *inputs* (operator commands and environment:
  rpm, throttle/pedal, speed, baro, IAT, coolant, AC load, supply voltage)
  condition the encoder and are never scored; *targets* (what the engine does
  in response: air mass, trims, lambda, timing, load) are the only
  reconstruction loss and the only score. One source per physical quantity
  (a ground-truth-confirmed broadcast mirror of a Mode 01 PID is dropped for
  the PID it mirrors), rolling statistics only where justified, and every
  dropped column named with its reason.

The record lives in the manifest schema under :data:`FEATURE_SET_SCHEMA_KEY`,
so what an artifact scores is read from the artifact, never from a comment.
Roles for generic J1979 channels and code-derived channels are here; a
vehicle's broadcast (``decoded:``) and Mode 22 (``did:``) channels get theirs
from its profile's ``sequence_roles.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from src.columns import ROC_SUFFIX, split_rolling
from src.config import CanaryConfig, require_profile_scoped
from src.trainability import non_trainable_inputs, source_exclusion_reason

FEATURE_SET_SCHEMA_KEY = "sequence_feature_set"
ALL_CHANNELS = "all_channels"
ENGINE_RESPONSE = "engine_response"
FEATURE_SETS: tuple[str, ...] = (ALL_CHANNELS, ENGINE_RESPONSE)
#: Bump when the role table or the resolution rules change meaning.
FEATURE_SET_VERSION = 1
SEQUENCE_ROLES_FILENAME = "sequence_roles.yaml"

Role = Literal["input", "target"]
ROLES: tuple[str, ...] = ("input", "target")

#: Rolling derivatives kept per role under :data:`ENGINE_RESPONSE`, with why.
#: Targets keep none: ``__std`` is a step detector on a slow channel (baro's
#: σ-scaled ``__std`` led the top-feature ranking on in-distribution windows)
#: and ``__roc``/``__mean`` restate the raw target; an A02-protocol ablation
#: must show detection value before one comes back. Inputs keep ``__roc``, the
#: transient the engine is responding to (tip-in, a stop), which the raw value
#: of one row cannot show.
ROLLING_KEPT: dict[str, tuple[str, ...]] = {"input": (ROC_SUFFIX,), "target": ()}
ROLLING_WHY: dict[str, str] = {
    "input": "__roc kept: the transient the engine responds to; __mean/__std restate it",
    "target": (
        "no rolling statistics: __std is a step detector on slow channels and "
        "__mean/__roc restate the raw target; re-add only on an A02-protocol ablation"
    ),
}


@dataclass(frozen=True)
class ChannelRole:
    """A channel's role and the physical quantity it measures."""

    role: Role
    quantity: str


def _roles(role: Role, *pairs: tuple[str, str]) -> dict[str, ChannelRole]:
    return {channel: ChannelRole(role, quantity) for channel, quantity in pairs}


#: Generic J1979 Mode 01 channels. Vehicle-agnostic: the PID defines the
#: quantity. A PID absent here is unassigned and stays out of
#: :data:`ENGINE_RESPONSE` (named in the record), never silently scored.
J1979_ROLES: dict[str, ChannelRole] = {
    **_roles(
        "input",
        ("obd:RPM", "engine_speed"),
        ("obd:SPEED", "vehicle_speed"),
        ("obd:THROTTLE_POS", "throttle_position"),
        ("obd:ABS_THROTTLE_POS_B", "throttle_position_b"),
        ("obd:RELATIVE_THROTTLE_POS", "throttle_position_relative"),
        ("obd:ACCEL_PEDAL_POS_D", "pedal_position_d"),
        ("obd:ACCEL_PEDAL_POS_E", "pedal_position_e"),
        ("obd:BAROMETRIC_PRESSURE", "barometric_pressure"),
        ("obd:INTAKE_TEMP", "intake_air_temperature"),
        ("obd:AMBIENT_AIR_TEMP", "ambient_air_temperature"),
        ("obd:COOLANT_TEMP", "coolant_temperature"),
        ("obd:CONTROL_MODULE_VOLTAGE", "supply_voltage"),
        ("obd:FUEL_LEVEL", "fuel_level"),
        ("obd:FUEL_STATUS", "fuel_system_status"),
        ("obd:RUN_TIME", "engine_run_time"),
        # No ``gps:speed``: external sensors never condition or score a
        # detector (H00 operator decision 1; H01.1 source gate).
    ),
    **_roles(
        "target",
        ("obd:MAF", "mass_airflow"),
        ("obd:INTAKE_PRESSURE", "manifold_pressure"),
        ("obd:ENGINE_LOAD", "calculated_load"),
        ("obd:ABSOLUTE_LOAD", "absolute_load"),
        ("obd:SHORT_FUEL_TRIM_1", "short_fuel_trim_b1"),
        ("obd:SHORT_FUEL_TRIM_2", "short_fuel_trim_b2"),
        ("obd:LONG_FUEL_TRIM_1", "long_fuel_trim_b1"),
        ("obd:LONG_FUEL_TRIM_2", "long_fuel_trim_b2"),
        ("obd:TIMING_ADVANCE", "timing_advance"),
        ("obd:COMMANDED_EQUIV_RATIO", "commanded_equivalence_ratio"),
        ("obd:COMMANDED_THROTTLE_ACTUATOR", "commanded_throttle"),
        ("obd:O2_WR_LAMBDA_S1", "wideband_lambda_b1"),
        ("obd:O2_B1S2", "downstream_o2_b1"),
        ("obd:O2_B2S2", "downstream_o2_b2"),
        ("obd:CATALYST_TEMP_B1S1", "catalyst_temperature_b1"),
        ("obd:CATALYST_TEMP_B2S1", "catalyst_temperature_b2"),
        ("obd:EVAPORATIVE_PURGE", "evap_purge"),
        ("obd:EVAP_VAPOR_PRESSURE", "evap_vapor_pressure"),
        *((f"obd:MISFIRE_COUNT_CYL{n}", f"misfire_count_cyl{n}") for n in range(1, 13)),
    ),
}

#: Channels computed in code (``src.analysis.derived`` and the performance
#: channels). Kept: the physical residuals and per-cycle quantities that say
#: something no single PID does. Dropped: re-expressions of a kept target
#: (fuel rate, economy, power and torque are MAF and speed rescaled), which
#: would double-weight the same error.
DERIVED_ROLES: dict[str, ChannelRole] = _roles(
    "target",
    ("derived:AIR_MASS_PER_CYCLE", "air_mass_per_cycle"),
    ("derived:VE_PERCENT", "volumetric_efficiency"),
    ("derived:STFT_BANK_BALANCE", "stft_bank_balance"),
    ("derived:LTFT_BANK_BALANCE", "ltft_bank_balance"),
    ("derived:TOTAL_TRIM_B1", "total_trim_b1"),
    ("derived:TOTAL_TRIM_B2", "total_trim_b2"),
    ("derived:AFR_B1", "afr_b1"),
    ("derived:AFR_BANK_BALANCE", "afr_bank_balance"),
    ("derived:CAT_EFFICIENCY_B1", "catalyst_efficiency_b1"),
    ("derived:CAT_EFFICIENCY_B2", "catalyst_efficiency_b2"),
)
DERIVED_DROPPED: dict[str, str] = {
    "derived:FUEL_RATE_LPH": "restates obd:MAF (fuel = air / AFR)",
    "derived:INSTANT_ECONOMY_L_PER_100KM": "restates obd:MAF over obd:SPEED",
    "derived:INSTANT_ECONOMY_MPG": "restates obd:MAF over obd:SPEED",
    "derived:ESTIMATED_POWER_HP": "restates obd:MAF",
    "derived:ESTIMATED_TORQUE_NM": "restates obd:MAF over obd:RPM",
    "derived:AFR_CENSORED_B1": "censoring flag of derived:AFR_B1, not a response",
    "derived:TPS_VOLTAGE_PER_PCT": "sensor-scaling diagnostic, not an engine response",
}


@dataclass(frozen=True)
class VehicleSequenceRoles:
    """A profile's ``sequence_roles.yaml``: roles for its broadcast and Mode 22
    channels, and the ground-truth-confirmed mirrors dropped for their PID."""

    channels: Mapping[str, ChannelRole] = field(default_factory=dict)
    #: mirror channel -> the reference it duplicates (kept in its place).
    mirrors: Mapping[str, str] = field(default_factory=dict)
    #: channel -> why it is kept out of every feature set but ``all_channels``.
    excluded: Mapping[str, str] = field(default_factory=dict)
    source: str | None = None


def load_vehicle_sequence_roles(path: Path | None) -> VehicleSequenceRoles:
    """Read a profile's roles file; a missing file is an empty table (every
    vehicle channel unassigned), which the resolution record then says."""
    if path is None or not path.exists():
        return VehicleSequenceRoles(source=None if path is None else f"{path} (absent)")
    raw = yaml.safe_load(path.read_text()) or {}
    channels: dict[str, ChannelRole] = {}
    for role in ROLES:
        for quantity, members in (raw.get(role + "s") or {}).items():
            for channel in members or ():
                if channel in channels:
                    raise ValueError(f"{path}: {channel} is listed twice")
                channels[str(channel)] = ChannelRole(role, str(quantity))  # type: ignore[arg-type]
    mirrors: dict[str, str] = {}
    for reference, entry in (raw.get("mirrors") or {}).items():
        for channel in entry.get("channels") or ():
            mirrors[str(channel)] = str(reference)
    excluded = {str(k): str(v) for k, v in (raw.get("excluded") or {}).items()}
    overlap = sorted((set(channels) | set(excluded)) & set(mirrors))
    if overlap:
        raise ValueError(f"{path}: {overlap} are both a mirror and assigned/excluded")
    # H01.1: a role or a mirror reference is a claim that the channel feeds
    # the detector; only a trainable source may (exclusions may name anything).
    refused = non_trainable_inputs([*channels, *mirrors, *mirrors.values()])
    if refused:
        raise ValueError(
            f"{path}: channels whose source may not feed a detector "
            f"(H00 decision 1, src.trainability): {refused}"
        )
    return VehicleSequenceRoles(channels, mirrors, excluded, source=str(path))


def sequence_roles_path(config: CanaryConfig) -> Path:
    """The active profile's ``sequence_roles.yaml`` (beside its other vehicle data).

    Refuses an unscoped config while profiles are enabled (P02.11): the
    unscoped parent is the shared ``data/``, where no roles file exists, and
    a missing file reads as an empty table rather than an error.
    """
    require_profile_scoped(config, reader="sequence_roles_path")
    return Path(config.daemon.afr_calibration).parent / SEQUENCE_ROLES_FILENAME


@dataclass(frozen=True)
class FeatureSetResolution:
    """The columns a feature set trains on and what it scores."""

    name: str
    feature_columns: tuple[str, ...]
    #: Encoder inputs never scored (empty for :data:`ALL_CHANNELS`).
    context_columns: tuple[str, ...]
    record: Mapping[str, Any]


def resolve_feature_set(
    name: str,
    candidate_columns: Sequence[str],
    *,
    vehicle_roles: VehicleSequenceRoles | None = None,
) -> FeatureSetResolution:
    """Resolve feature set ``name`` over the trainable ``candidate_columns``."""
    if name not in FEATURE_SETS:
        raise ValueError(f"unknown sequence feature set {name!r}; expected one of {FEATURE_SETS}")
    columns = [str(column) for column in candidate_columns]
    # H01.1 source gate. Candidates normally arrive already gated
    # (feature_pipeline.candidate_feature_columns); this is the second lock.
    source_refused = {
        column: reason
        for column in columns
        if (reason := source_exclusion_reason(column)) is not None
    }
    if name == ALL_CHANNELS:
        if source_refused:
            # all_channels promises every candidate is scored, so a refused
            # candidate means the caller skipped the pipeline gate.
            raise ValueError(
                f"feature set {ALL_CHANNELS!r} was handed columns whose source may not "
                f"train a detector: {source_refused}"
            )
        return FeatureSetResolution(
            ALL_CHANNELS,
            tuple(columns),
            (),
            {"name": ALL_CHANNELS, "version": FEATURE_SET_VERSION, "context_columns": []},
        )
    vehicle = vehicle_roles or VehicleSequenceRoles()
    roles: dict[str, ChannelRole] = {**J1979_ROLES, **DERIVED_ROLES, **vehicle.channels}
    present_bases = {split_rolling(column)[0] for column in columns}
    dropped: dict[str, str] = {}
    unassigned: set[str] = set()
    kept: list[str] = []
    context: list[str] = []
    for column in columns:
        base, suffix = split_rolling(column)
        if column in source_refused:
            dropped[column] = source_refused[column]
            continue
        if base in DERIVED_DROPPED:
            dropped[column] = DERIVED_DROPPED[base]
            continue
        if base in vehicle.excluded:
            dropped[column] = vehicle.excluded[base]
            continue
        reference = vehicle.mirrors.get(base)
        if reference is not None and reference in present_bases:
            dropped[column] = (
                f"ground-truth-confirmed mirror of {reference}; one source per quantity"
            )
            continue
        role = roles.get(base)
        if role is None:
            unassigned.add(base)
            dropped[column] = "no role assigned (unknown quantity); not scored"
            continue
        if suffix and suffix not in ROLLING_KEPT[role.role]:
            dropped[column] = ROLLING_WHY[role.role]
            continue
        kept.append(column)
        if role.role == "input":
            context.append(column)
    by_quantity: dict[str, list[str]] = {}
    for column in kept:
        base = split_rolling(column)[0]
        by_quantity.setdefault(roles[base].quantity, []).append(base)
    duplicated = {
        quantity: sorted(set(bases))
        for quantity, bases in by_quantity.items()
        if len(set(bases)) > 1
    }
    targets = [column for column in kept if column not in set(context)]
    if not targets:
        raise ValueError(
            f"feature set {name!r} leaves no target to score among {len(columns)} candidate columns"
        )
    record = {
        "name": name,
        "version": FEATURE_SET_VERSION,
        "roles_source": vehicle.source,
        "inputs": sorted({split_rolling(c)[0] for c in context}),
        "targets": sorted({split_rolling(c)[0] for c in targets}),
        "context_columns": context,
        "rolling": {role: list(suffixes) for role, suffixes in ROLLING_KEPT.items()},
        "rolling_why": dict(ROLLING_WHY),
        "unassigned": sorted(unassigned),
        "same_quantity_unconfirmed": duplicated,
        "dropped": dict(sorted(dropped.items())),
    }
    return FeatureSetResolution(name, tuple(kept), tuple(context), record)


def context_columns_from_schema(schema: Mapping[str, Any]) -> tuple[str, ...]:
    """The never-scored encoder inputs an artifact recorded (none when absent)."""
    record = schema.get(FEATURE_SET_SCHEMA_KEY)
    if not isinstance(record, Mapping):
        return ()
    return tuple(str(column) for column in record.get("context_columns") or ())


__all__ = [
    "ALL_CHANNELS",
    "ENGINE_RESPONSE",
    "FEATURE_SETS",
    "FEATURE_SET_SCHEMA_KEY",
    "ChannelRole",
    "FeatureSetResolution",
    "VehicleSequenceRoles",
    "context_columns_from_schema",
    "load_vehicle_sequence_roles",
    "resolve_feature_set",
    "sequence_roles_path",
]
