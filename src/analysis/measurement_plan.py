"""How each fault-model measurement is acquired, what it costs, and when the
brain may propose it (E03.4).

A vehicle's ``measurement_plan.yaml`` (``canary-measurement-plan/1``) states,
for every residual of its fault model (``fault_hypotheses.yaml``):

* **cost** in one declared unit (operator minutes), with its basis — the
  denominator of information gain per cost
  (:mod:`src.analysis.hypothesis_tracking`);
* **acquisition**: ``capture`` (a named capture profile polls it),
  ``default`` (normal collection already polls it), ``analysis``
  (qualification work on stored data) or ``instrument`` (hardware to fit),
  plus the instruments it needs (``sensor:`` channels, which nothing polls);
* the **live entry predicates** of each fault-model region (fresh channel
  values), copied from their sources and checked against them at load.

:func:`propose` turns a ranked measurement list and the live state into
proposals. What it makes impossible rather than documented:

* **Never displaces an operator capture.** Any active capture is the
  operator's: the brain only *proposes*, and an ``open_capture`` proposal
  cannot be constructed while a capture is active (:class:`Proposal` refuses
  it). If the operator's own capture already collects the measurement, the
  proposal says so (``collecting``).
* **Only when the drive enters the region on its own.** A capture is
  proposed only while every entry predicate of the measurement's region
  holds on fresh values; stale or missing inputs give a named ``defer``,
  never a guess (input timing).
* **Complete tuples and profile capacity.** The proposed profile must poll
  every polled member of the residual's channel set (in one batch group when
  the profile declares groups) and declare a total demand within its bus
  budget; otherwise the proposal is deferred naming which.
* **Physical actions are for review.** Fitting an instrument, qualification
  work and anything else a person must do is delivered ``after_drive``; there
  is no delivery that asks the driver to read or respond mid-drive.
* **Only while the engine runs and CANary transmits.** Any other
  ``vehicle_state`` (passive, replay, unknown, ...) defers.

Pydantic + PyYAML + the capture-profile schema; no daemon state. The daemon
(E04.1) supplies :class:`LiveContext`; :func:`replay_proposals` drives the same
function over stored rows.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.analysis.fault_model import FaultModel, Feature
from src.collection.capture_profiles import CaptureProfile, CaptureProfileCatalog
from src.evidence.text import no_numerals

__all__ = [
    "MEASUREMENT_PLAN_FILE",
    "MEASUREMENT_PLAN_SCHEMA",
    "Acquisition",
    "CaptureState",
    "LiveContext",
    "LiveReading",
    "MeasurementEntry",
    "MeasurementPlan",
    "MeasurementPlanError",
    "Predicate",
    "Proposal",
    "load_measurement_plan",
    "profile_shortfall",
    "propose",
    "region_state",
    "replay_proposals",
]

MEASUREMENT_PLAN_SCHEMA = "canary-measurement-plan/1"
MEASUREMENT_PLAN_FILE = "measurement_plan.yaml"


class MeasurementPlanError(ValueError):
    """A plan, or a proposal, this module refuses."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Predicate(_Strict):
    channel: str = Field(min_length=1)
    op: Literal["eq", "ge", "gt", "le", "lt"]
    value: float

    def holds(self, value: float) -> bool:
        if self.op == "eq":
            return value == self.value
        if self.op == "ge":
            return value >= self.value
        if self.op == "gt":
            return value > self.value
        if self.op == "le":
            return value <= self.value
        return value < self.value


class RegionEntry(_Strict):
    #: Every predicate must hold on a fresh value for the region to be entered.
    all: tuple[Predicate, ...] = Field(min_length=1)
    #: Where the thresholds come from (checked at load where a source exists).
    source: str = Field(min_length=1)


class Acquisition(_Strict):
    kind: Literal["capture", "default", "analysis", "instrument"]
    #: ``capture``: the capture profile that polls it.
    profile: str | None = None
    #: Channels a fitted instrument supplies (``sensor:``); nothing polls them.
    instruments: tuple[str, ...] = ()
    #: What a person must do first (review only). Prose without numbers.
    physical_action: str | None = None

    @field_validator("physical_action")
    @classmethod
    def _prose(cls, value: str | None) -> str | None:
        return None if value is None else no_numerals(value, what="physical action")

    @model_validator(mode="after")
    def _rules(self) -> Acquisition:
        if (self.kind == "capture") != (self.profile is not None):
            raise ValueError("a capture acquisition names its profile, and only it does")
        if self.kind in ("analysis", "instrument") and not self.physical_action:
            raise ValueError(f"{self.kind} work states the physical action a person reviews")
        if any(not i.startswith("sensor:") for i in self.instruments):
            raise ValueError("instruments are sensor: channels; polled channels are not")
        return self


class MeasurementEntry(_Strict):
    residual: str = Field(min_length=1)
    cost: float = Field(gt=0.0)
    cost_basis: str = Field(min_length=1)
    acquisition: Acquisition


class MeasurementPlan(_Strict):
    schema_: Literal["canary-measurement-plan/1"] = Field(alias="schema")
    version: int = Field(ge=1)
    #: The fault-model version this plan was written for; a mismatch refuses.
    fault_model_version: int = Field(ge=1)
    cost_unit: str = Field(min_length=1)
    #: Oldest a value may be and still stand for "now" (input timing).
    max_input_age_s: float = Field(gt=0.0)
    regions: dict[str, RegionEntry]
    measurements: tuple[MeasurementEntry, ...] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _unique(self) -> MeasurementPlan:
        names = [m.residual for m in self.measurements]
        if len(set(names)) != len(names):
            raise ValueError(f"a residual has two plan entries: {names}")
        return self

    def entry(self, residual: str) -> MeasurementEntry:
        for item in self.measurements:
            if item.residual == residual:
                return item
        raise MeasurementPlanError(f"the plan has no entry for residual {residual!r}")


def _threshold(plan: MeasurementPlan, region: str, channel: str, op: str) -> float | None:
    for predicate in plan.regions[region].all:
        if predicate.channel == channel and predicate.op == op:
            return predicate.value
    return None


def load_measurement_plan(
    path: Path,
    model: FaultModel,
    *,
    mode01_wot: Any | None = None,
) -> tuple[MeasurementPlan, str]:
    """The plan at ``path`` checked against ``model`` (and the profile's
    ``mode01_wot`` floors, when given); returns it and its bytes' sha256.

    Refused: another fault-model version, a residual or region of the model
    without a plan entry, or a region threshold that differs from its source
    (the pressure residuals' airflow limits; B06's WOT load and rpm floors).
    """
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw)
    if not isinstance(data, dict) or data.get("schema") != MEASUREMENT_PLAN_SCHEMA:
        raise MeasurementPlanError(f"{path} is not a {MEASUREMENT_PLAN_SCHEMA} file")
    plan = MeasurementPlan.model_validate(data)
    if plan.fault_model_version != model.version:
        raise MeasurementPlanError(
            f"the plan is for fault model v{plan.fault_model_version}; this model is "
            f"v{model.version}: review the plan against the new signatures"
        )
    missing = sorted({r.name for r in model.residuals} - {m.residual for m in plan.measurements})
    extra = sorted({m.residual for m in plan.measurements} - {r.name for r in model.residuals})
    if missing or extra:
        raise MeasurementPlanError(
            f"plan residuals differ from the model: missing {missing}, unknown {extra}"
        )
    regions = {r.name for r in model.regions}
    if set(plan.regions) != regions:
        raise MeasurementPlanError(
            f"plan regions {sorted(plan.regions)} are not the model's {sorted(regions)}"
        )
    checks: list[tuple[str, str, str, float | None]] = []
    for residual in model.residuals:
        policy = residual.policy
        if "high_airflow_min_gs" in policy:
            checks.append(("high_airflow", "obd:MAF", "ge", policy["high_airflow_min_gs"]))
        if "low_airflow_max_gs" in policy:
            checks.append(("low_airflow", "obd:MAF", "lt", policy["low_airflow_max_gs"]))
        if "running_min_maf_gs" in policy:
            checks.append(("low_airflow", "obd:MAF", "ge", policy["running_min_maf_gs"]))
    if mode01_wot is not None:
        checks.append(("enrichment", "obd:ENGINE_LOAD", "ge", float(mode01_wot.load_floor_pct)))
        checks.append(("enrichment", "obd:RPM", "ge", float(mode01_wot.rpm_floor)))
    for region, channel, op, expected in checks:
        stated = _threshold(plan, region, channel, op)
        if stated != expected:
            raise MeasurementPlanError(
                f"{region}: {channel} {op} {stated} in the plan, {expected} at its source; "
                "a region is entered where its residual is defined, not near it"
            )
    for entry in plan.measurements:
        residual = model.residual(entry.residual)
        sensors = {c for group in residual.channel_sets for c in group if c.startswith("sensor:")}
        if not sensors <= set(entry.acquisition.instruments):
            raise MeasurementPlanError(
                f"{entry.residual}: sensor channels {sorted(sensors)} must be declared as "
                "instruments: nothing polls them"
            )
    return plan, hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Profiles: complete tuples and capacity


def _polled(profile: CaptureProfile) -> set[str]:
    out: set[str] = set()
    if profile.mode01_enabled:
        out |= {f"obd:{name.upper()}" for name in profile.mode01}
    if profile.mode22_enabled:
        out |= {f"did:{name}" for name in profile.mode22}
        out |= {f"mem:{name}" for name in profile.memory}
    return out


def profile_shortfall(
    profile: CaptureProfile | None,
    channel_sets: Iterable[Sequence[str]],
    *,
    default_budget: float,
) -> str | None:
    """Why ``profile`` cannot deliver a complete tuple of any channel set, or ``None``.

    A set is complete when the profile polls every polled member (``sensor:``
    members are instruments) and, if it declares batch groups, puts the Mode
    01 members in one group. Capacity: the profile declares its total demand
    and that demand fits its bus budget (its override, else ``default_budget``).
    """
    if profile is None:
        return "profile_missing: the plan names a capture profile this vehicle does not have"
    polled = _polled(profile)
    reasons: list[str] = []
    for group in channel_sets:
        wanted = [c for c in group if not c.startswith("sensor:")]
        absent = sorted(c for c in wanted if c not in polled)
        if absent:
            reasons.append(f"incomplete_tuple: {profile.name} does not poll {absent}")
            continue
        obd = {c.removeprefix("obd:") for c in wanted if c.startswith("obd:")}
        if profile.mode01_batch_groups and not any(
            obd <= {n.upper() for n in g} for g in profile.mode01_batch_groups
        ):
            reasons.append(f"incomplete_tuple: {profile.name} splits {sorted(obd)} across batches")
            continue
        break
    else:
        return "; ".join(reasons) or "incomplete_tuple: no channel set"
    budget = profile.bus_budget_override or default_budget
    demand = profile.expected_total_demand_hz
    if demand is None:
        return f"capacity_undeclared: {profile.name} declares no expected total demand"
    if demand > budget:
        return f"over_capacity: {profile.name} demands {demand:g} req/s of a {budget:g} budget"
    return None


# ---------------------------------------------------------------------------
# Live state and proposals


@dataclass(frozen=True)
class LiveReading:
    value: float
    age_s: float


@dataclass(frozen=True)
class CaptureState:
    """The capture profile running now; any active one is the operator's."""

    active: bool
    profile: str
    source: str | None = None

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> CaptureState:
        """From ``tuning_profile.snapshot()`` (``/status`` ``capture``)."""
        return cls(
            active=bool(snapshot.get("active")),
            profile=str(snapshot.get("name", "default")),
            source=None if snapshot.get("source") is None else str(snapshot.get("source")),
        )


@dataclass(frozen=True)
class LiveContext:
    vehicle_state: str
    readings: Mapping[str, LiveReading]
    capture: CaptureState


ProposalKind = Literal["open_capture", "collecting", "defer", "review"]
#: ``brain``: the brain's own state, shown on the dashboard, never a prompt;
#: ``after_drive``: the review queue. Nothing is delivered to the driver.
Delivery = Literal["brain", "after_drive"]


@dataclass(frozen=True)
class Proposal:
    measurement: str
    residual: str
    region: str | None
    kind: ProposalKind
    delivery: Delivery
    reason: str
    detail: str = ""
    profile: str | None = None
    #: The capture active when this was proposed (``None``: none was).
    during_capture: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "open_capture":
            if self.during_capture is not None:
                raise MeasurementPlanError(
                    f"cannot propose {self.profile!r} while the operator's capture "
                    f"{self.during_capture!r} runs: the brain never displaces a capture"
                )
            if self.profile is None or self.delivery != "brain":
                raise MeasurementPlanError("an open_capture proposal names its profile")
        if self.kind == "review" and self.delivery != "after_drive":
            raise MeasurementPlanError("physical actions are reviewed after the drive")


RegionVerdict = Literal["entered", "not_entered", "missing_input", "stale_input"]


def region_state(
    plan: MeasurementPlan, region: str, readings: Mapping[str, LiveReading]
) -> tuple[RegionVerdict, str]:
    """Whether the drive is in ``region`` now, on fresh values only."""
    entry = plan.regions[region]
    for predicate in entry.all:
        reading = readings.get(predicate.channel)
        if reading is None or not math.isfinite(reading.value):
            return "missing_input", f"{predicate.channel} has no value"
        if reading.age_s > plan.max_input_age_s:
            return (
                "stale_input",
                f"{predicate.channel} is {reading.age_s:.1f} s old (limit "
                f"{plan.max_input_age_s:g} s)",
            )
    failed = [p.channel for p in entry.all if not p.holds(readings[p.channel].value)]
    if failed:
        return "not_entered", f"{region} not entered ({', '.join(failed)})"
    return "entered", f"{region} entered"


class _Measured(Protocol):
    @property
    def measurement(self) -> str: ...

    @property
    def features(self) -> tuple[Feature, ...]: ...


def propose(
    plan: MeasurementPlan,
    model: FaultModel,
    catalog: CaptureProfileCatalog,
    measurements: Sequence[_Measured],
    live: LiveContext,
    *,
    default_budget: float,
) -> list[Proposal]:
    """One proposal per (measurement, residual), in the ranking's order."""
    out: list[Proposal] = []
    capture = live.capture
    during = capture.profile if capture.active else None
    for item in measurements:
        residuals: dict[str, list[str]] = {}
        for feature in item.features:
            residuals.setdefault(feature.residual, []).append(feature.region)
        for residual_name, regions in residuals.items():
            entry = plan.entry(residual_name)
            residual = model.residual(residual_name)
            acquisition = entry.acquisition

            def make(
                kind: ProposalKind,
                reason: str,
                detail: str = "",
                *,
                measurement: str = item.measurement,
                name: str = residual_name,
                **kw: Any,
            ) -> Proposal:
                delivery: Delivery = "after_drive" if kind == "review" else "brain"
                return Proposal(
                    measurement,
                    name,
                    kw.pop("region", None),
                    kind,
                    delivery,
                    reason,
                    detail,
                    during_capture=during,
                    **kw,
                )

            if acquisition.kind in ("analysis", "instrument"):
                out.append(make("review", acquisition.kind, acquisition.physical_action or ""))
                continue
            absent = [
                i
                for i in acquisition.instruments
                if (r := live.readings.get(i)) is None or r.age_s > plan.max_input_age_s
            ]
            if absent:
                out.append(
                    make(
                        "review",
                        "instrument_absent",
                        f"connect {', '.join(absent)} before a drive that reaches "
                        f"{', '.join(regions)}",
                    )
                )
                continue
            if live.vehicle_state != "engine_running":
                out.append(make("defer", f"vehicle_state:{live.vehicle_state}"))
                continue
            states = [(region, *region_state(plan, region, live.readings)) for region in regions]
            entered = next((s for s in states if s[1] == "entered"), None)
            if entered is None:
                region, verdict, detail = states[0]
                out.append(make("defer", f"region_{verdict}", detail, region=region))
                continue
            region = entered[0]
            if acquisition.kind == "default":
                out.append(make("collecting", "default_collection", region=region))
                continue
            if capture.active:
                if capture.profile == acquisition.profile:
                    out.append(
                        make(
                            "collecting",
                            "operator_capture_collects_it",
                            region=region,
                            profile=capture.profile,
                        )
                    )
                else:
                    out.append(
                        make(
                            "defer",
                            "operator_capture_active",
                            f"the operator's capture {capture.profile!r} "
                            f"({capture.source or 'unknown source'}) runs; never displaced",
                            region=region,
                        )
                    )
                continue
            shortfall = profile_shortfall(
                catalog.by_name(acquisition.profile or ""),
                residual.channel_sets,
                default_budget=default_budget,
            )
            if shortfall is not None:
                out.append(make("defer", shortfall.split(":")[0], shortfall, region=region))
                continue
            out.append(
                make(
                    "open_capture",
                    "region_entered",
                    f"the drive entered {region}: propose {acquisition.profile!r} for it",
                    region=region,
                    profile=acquisition.profile,
                )
            )
    return out


def replay_proposals(
    plan: MeasurementPlan,
    model: FaultModel,
    catalog: CaptureProfileCatalog,
    measurements: Sequence[_Measured],
    rows: Iterable[tuple[float, str, float]],
    *,
    capture: CaptureState,
    default_budget: float,
    tick_s: float = 1.0,
    vehicle_state: str = "engine_running",
) -> list[tuple[float, list[Proposal]]]:
    """:func:`propose` at every ``tick_s`` over time-ordered ``(t, spec, value)``
    rows, each tick seeing only the newest value of each channel at or before
    it, with its age (a stored session replayed through the live rule)."""
    latest: dict[str, tuple[float, float]] = {}
    out: list[tuple[float, list[Proposal]]] = []
    next_tick: float | None = None

    def tick(at: float) -> None:
        readings = {
            spec: LiveReading(value, at - t) for spec, (t, value) in latest.items() if t <= at
        }
        context = LiveContext(vehicle_state, readings, capture)
        out.append(
            (
                at,
                propose(plan, model, catalog, measurements, context, default_budget=default_budget),
            )
        )

    for t, spec, value in rows:
        if next_tick is None:
            next_tick = t
        while t > next_tick:
            tick(next_tick)
            next_tick += tick_s
        latest[spec] = (t, value)
    if next_tick is not None:
        tick(next_tick)
    return out
