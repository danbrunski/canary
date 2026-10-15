"""The driving dyno on the Analysis page (H07.9).

One request, one or more stored sessions, and per session the chain H07
built at the desk: :func:`~src.analysis.dyno_pulls.find_power_pulls` →
:func:`~src.analysis.dyno_pulls.label_headings` →
:func:`~src.analysis.dyno_power.compute_power`, once per gear pulled. Runs in the
workbench's bounded single-flight child (:mod:`src.control.workbench_jobs`),
never on the event loop.

What the page must never show is a curve computed from a coefficient nobody
measured. So the answer carries, before any curve:

- **prerequisites**: every profile fact the power computation rests on, each
  ``recorded``, ``estimated`` (stated, labelled as such: the inertia, the
  mass) or ``pending`` (owed, with the item that owes it). A pending road
  load or squat refuses every run ``coefficients_pending``, naming them; no
  default stands in (H07.5, H07.4 and the H07.12 NA baseline).
- **every window found**, pull or refusal, so coverage is visible even when
  no curve can be drawn;
- per run (one session, one gear) a curve with its required
  :class:`~src.analysis.dyno_comparison.RunConditions`, its flags (``inertia``,
  ``squat_corrected``, the pitch offset and where it came from) and its
  uncertainty: the recorded same-day floor for that regime (H07.8) or the
  floor's own refusal, plus the half-difference between the two headings at
  each rpm (what wind and grade left, not a noise floor);
- ``unavailable_kind``, which kind of empty an empty answer is.

The IMU pitch offset is a property of one session's frame, so it is not
profile data: each run estimates it from its own pulls' direction pair
(:func:`~src.analysis.dyno_motion.estimate_pitch_offset`, with the recorded squat
removed) and says so.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.analysis.channels import parse_spec, read_channels
from src.analysis.dyno_comparison import RunConditions
from src.analysis.dyno_contract import DynoRefusal
from src.analysis.dyno_driveline import Driveline
from src.analysis.dyno_motion import Leg, estimate_pitch_offset
from src.analysis.dyno_power import DynoPower, PowerCurve, PowerPull, compute_power
from src.analysis.dyno_profile import DYNO_FILENAME, DynoProfile, load_dyno_profile
from src.analysis.dyno_pulls import FoundPull, find_power_pulls, label_headings, session_reads
from src.analysis.tuning_frame import WotCriterion
from src.analysis.workbench import (
    BARO_SPEC,
    Unavailable,
    WorkbenchInputs,
    conditions_block,
)
from src.config import VehicleParameters
from src.storage.sessions import session_started_at
from src.vehicle.tune_revisions import resolve_session_revision, session_dated_at

#: Intake air temperature, for the run conditions (Mode 01 PID 0x0F).
INTAKE_TEMP_SPEC = "obd:INTAKE_TEMP"
#: Ambient air temperature (Mode 01 PID 0x46), when the ECM answers it.
AMBIENT_TEMP_SPEC = "obd:AMBIANT_AIR_TEMP"

Induction = Literal["na", "boosted"]

#: Rows per second of the job's one whole-session read, measured on the
#: laptop 2026-10-05 at load average ~20: 2.84 M rows in 16.5 s
#: (``_capture-tuning-2026-09-22T235224``, 172 k/s) and 11.3 M in 42.3 s
#: (``2026-09-20T175050``, 267 k/s). The slower is taken.
DYNO_LAPTOP_ROWS_PER_S = 170_000.0
#: The Pi 5 runs this parquet/pandas work ~3.2x slower than the laptop
#: (D03: ``read_acquisition_frame`` measured on both).
DYNO_PI_SLOWDOWN = 3.2
#: Share of the child's timeout the whole-session reads may take. The rest is
#: for the footer-pruned per-window reads: 0.8 s warm / 2.9 s cold per window
#: on the laptop (~3 / ~9 s on the Pi), so ~150 WOT windows at the default
#: 900 s.
DYNO_READ_SHARE = 0.5


def dyno_row_budget(timeout_s: float) -> int:
    """Stored rows one dyno request may read within ``timeout_s`` on the Pi."""
    return int(timeout_s * DYNO_READ_SHARE * DYNO_LAPTOP_ROWS_PER_S / DYNO_PI_SLOWDOWN)


def dyno_over_budget(rows: dict[str, int | None], timeout_s: float) -> str | None:
    """Why a selection cannot finish within ``timeout_s``, or ``None``.

    Sized from the measured read rate, so a request that would time out in
    the child is refused before one is spawned. Sessions whose row count the
    footers do not give are named, not guessed.
    """
    known = {name: n for name, n in rows.items() if n is not None}
    total = sum(known.values())
    budget = dyno_row_budget(timeout_s)
    if total <= budget:
        return None
    seconds = total / (DYNO_LAPTOP_ROWS_PER_S / DYNO_PI_SLOWDOWN)
    return (
        f"{len(known)} session(s), {total:,} stored rows: the dyno reads each once, about "
        f"{seconds:,.0f} s on the Pi at the measured rate, against the {timeout_s:,.0f} s "
        f"analysis limit (budget {budget:,} rows, half the limit). Choose fewer sessions, "
        "or analyse on the laptop."
    )


UnavailableKind = Literal[
    "not_configured",
    "over_budget",
    "no_pull_found",
    "every_pull_refused",
    "coefficients_pending",
    "every_run_refused",
    "prerequisites_missing",
]


@dataclass(frozen=True)
class DynoRequest:
    """What the operator states about the runs; nothing here has a default value
    that could pass for a measurement. ``None`` is "not stated", and a run that
    needs it is refused naming it."""

    road: str | None = None
    payload_kg: float | None = None
    induction: Induction | None = None
    #: Only when the session holds no ambient channel (the operator's reading).
    ambient_temp_c: float | None = None
    allow_single_direction: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "road": self.road,
            "payload_kg": self.payload_kg,
            "induction": self.induction,
            "ambient_temp_c": self.ambient_temp_c,
            "allow_single_direction": self.allow_single_direction,
        }


def _clean(value: float) -> float | None:
    return None if not math.isfinite(value) else float(value)


def _floats(values: Any) -> list[float | None]:
    return [_clean(float(v)) for v in np.asarray(values, dtype=float)]


# ---------------------------------------------------------------------------
# Prerequisites


def _prerequisite(
    fact: str,
    status: Literal["recorded", "stated", "estimated", "pending", "missing"],
    detail: str,
    *,
    blocks: bool,
    owed_by: str | None = None,
) -> dict[str, Any]:
    return {
        "fact": fact,
        "status": status,
        "detail": detail,
        "owed_by": owed_by,
        "blocks_power": blocks,
    }


def prerequisites(
    profile: DynoProfile | None,
    driveline: Driveline | DynoRefusal | None,
    vehicle: VehicleParameters,
    wot: WotCriterion | None,
    demand_spec: str | None,
    demand_reason: str | None = None,
) -> list[dict[str, Any]]:
    """Every fact :func:`compute_power` rests on, and whether it is measured.

    ``pending`` is a measurement the vehicle owes (a truck item); ``missing``
    is profile configuration that is absent or unusable. Both block power,
    and the answer names which kind (:data:`UnavailableKind`).
    """
    rows: list[dict[str, Any]] = []
    if profile is None:
        rows.append(
            _prerequisite(
                "dyno_profile",
                "missing",
                f"this vehicle has no {DYNO_FILENAME}",
                blocks=True,
            )
        )
        return rows
    if isinstance(driveline, DynoRefusal):
        rows.append(_prerequisite("driveline", "missing", driveline.reason, blocks=True))
    elif driveline is not None:
        estimated = driveline.inertia == "estimated"
        rows.append(
            _prerequisite(
                "inertia",
                "estimated" if estimated else "recorded",
                (
                    "rotating inertia from the stated estimates in dyno.yaml; results carry "
                    "inertia: estimated"
                    if estimated
                    else "engine inertia measured from two-gear pulls"
                ),
                blocks=False,
                owed_by="H07.6 two-gear pulls (H07.12)" if estimated else None,
            )
        )
    if vehicle.mass_evidence:
        rows.append(
            _prerequisite(
                "mass",
                "stated",
                f"{vehicle.mass_kg:g} kg, as the profile states it: {vehicle.mass_evidence}",
                blocks=False,
            )
        )
    else:
        rows.append(
            _prerequisite(
                "mass",
                "missing",
                f"mass_kg {vehicle.mass_kg:g} carries no mass_evidence, so it may be the code "
                "default; record the vehicle's mass and how it was arrived at",
                blocks=True,
                owed_by="a weighed or itemised mass in the profile (H07.10)",
            )
        )
    road_load = profile.road_load
    rows.append(
        _prerequisite(
            "road_load",
            "recorded" if road_load is not None else "pending",
            (
                f"A {road_load.a_n:.1f} N, B {road_load.b_n_per_mps:.2f} N/(m/s), "
                f"C {road_load.c_n_per_mps2:.3f} N/(m/s)^2 on {road_load.conditions.road!r} "
                f"({road_load.conditions.date})"
                if road_load is not None
                else "no coast-down pair recorded in dyno.yaml; nothing stands in for it"
            ),
            blocks=road_load is None,
            owed_by=None if road_load is not None else "H07.5 coast-downs (H07.12)",
        )
    )
    squat = profile.squat
    rows.append(
        _prerequisite(
            "squat",
            "recorded" if squat is not None else "pending",
            (
                f"{squat.deg_per_g:.2f} deg/g on {squat.conditions.road!r} "
                f"({squat.conditions.date})"
                if squat is not None
                else "no flat-ground squat fit recorded; the pitch offset each run estimates "
                "from its own pulls would absorb the squat, so no run is computed without it"
            ),
            blocks=squat is None,
            owed_by=None if squat is not None else "H07.4 flat-ground accelerations (H07.12)",
        )
    )
    rows.append(
        _prerequisite(
            "noise_floor",
            "recorded" if profile.noise_floors else "pending",
            (
                f"{len(profile.noise_floors)} same-day floor(s) recorded; a run outside "
                "their regimes says so"
                if profile.noise_floors
                else "no same-day floor recorded: curves carry no run-to-run uncertainty"
            ),
            blocks=False,
            owed_by=None if profile.noise_floors else "H07.8 three pull pairs (H07.12)",
        )
    )
    if wot is None or demand_spec is None:
        rows.append(
            _prerequisite(
                "wot_criterion",
                "missing",
                (
                    "no wot_criterion in tuning_providers.yaml"
                    if wot is None
                    else (demand_reason or "no usable operator_demand provider")
                )
                + ": nothing shows a pull was at wide-open throttle",
                blocks=True,
            )
        )
    else:
        rows.append(
            _prerequisite(
                "wot_criterion",
                "recorded",
                f"{demand_spec} >= {wot.threshold_value:g} {wot.unit} above {wot.rpm_floor:g} rpm",
                blocks=False,
            )
        )
    return rows


def _demand(inputs: WorkbenchInputs) -> tuple[WotCriterion | None, str | None, str | None]:
    """The WOT criterion, the operator-demand spec, and why not, if not."""
    providers = inputs.providers
    if providers is None:
        return None, None, "no tuning_providers.yaml"
    wot = providers.wot_criterion
    found = [p for p in providers.providers if p.role == (wot.role if wot else "operator_demand")]
    if not found:
        return wot, None, "no operator_demand provider"
    provider = found[0]
    if provider.transform != "identity":
        # The demand channel is read raw here; a transformed provider would be
        # compared with the criterion in the wrong unit.
        return (
            wot,
            None,
            f"the operator_demand provider applies {provider.transform!r}; the dyno reads it "
            "untransformed",
        )
    return wot, provider.spec, None


# ---------------------------------------------------------------------------
# Per-session


def _found_row(item: FoundPull, heading: str | None) -> dict[str, Any]:
    return {
        "start": item.start.isoformat(),
        "end": item.end.isoformat(),
        "rpm_start": _clean(item.rpm_start),
        "rpm_end": _clean(item.rpm_end),
        "course_deg": None if item.course_deg is None else _clean(item.course_deg),
        "gear": None if item.pull is None else item.pull.gear,
        "heading": heading,
        "refusal": None if item.refusal is None else item.refusal.as_dict(),
    }


def _session_read(session: Path, specs: list[str], inputs: WorkbenchInputs) -> pd.DataFrame:
    """The one whole-session read a dyno request makes of ``session``.

    Every whole-session channel the job needs, in one :func:`read_channels`
    call (one pass over the chunks): the pull detector's rpm, course and
    demand, and the run conditions' baro, intake and ambient temperature.
    The per-window motion reads are footer-pruned
    (:func:`~src.preprocessing.loader.read_session` ``time_range``).
    """
    return read_channels(
        session,
        specs,
        bindings_path=inputs.bindings_path,
        dids_path=inputs.dids_path,
        calibration=inputs.calibration,
        afr_stoich=inputs.afr_stoich,
        acquired_time=True,
    )


def _median_in(reads: pd.DataFrame, spec: str, windows: Sequence[FoundPull]) -> float | None:
    """The spec's median over the pull windows, or ``None`` when not recorded."""
    if reads.empty or "spec" not in reads.columns:
        return None
    long = reads[reads["spec"] == parse_spec(spec).text]
    if long.empty:
        return None
    stamps = pd.to_datetime(long["timestamp"], utc=True)
    values = pd.to_numeric(long["value"], errors="coerce")
    inside = np.zeros(len(long), dtype=bool)
    for item in windows:
        inside |= ((stamps >= item.start) & (stamps <= item.end)).to_numpy()
    picked = values[inside].dropna()
    return None if picked.empty else float(picked.median())


def _run_conditions(
    session: Path,
    gear: int,
    direction: Literal["pair", "single"],
    windows: Sequence[FoundPull],
    *,
    reads: pd.DataFrame,
    inputs: WorkbenchInputs,
    request: DynoRequest,
) -> RunConditions | DynoRefusal:
    """The run's required conditions, or a refusal naming each one missing."""
    revision, _source, _conflict = resolve_session_revision(session, inputs.registry)
    baro = _median_in(reads, BARO_SPEC, windows)
    intake = _median_in(reads, INTAKE_TEMP_SPEC, windows)
    ambient = request.ambient_temp_c
    if ambient is None:
        ambient = _median_in(reads, AMBIENT_TEMP_SPEC, windows)
    dated = session_dated_at(session) or session_started_at(session)
    missing = [
        label
        for label, value in (
            ("tune revision (no flash recorded covers this session)", revision),
            (f"barometric pressure ({BARO_SPEC} not recorded during the pulls)", baro),
            (f"intake temperature ({INTAKE_TEMP_SPEC} not recorded during the pulls)", intake),
            (
                f"ambient temperature ({AMBIENT_TEMP_SPEC} not recorded; state it)",
                ambient,
            ),
            ("road (state which road)", request.road),
            ("payload_kg (state what was aboard)", request.payload_kg),
            ("induction (state na or boosted)", request.induction),
            ("session date", dated),
        )
        if value is None
    ]
    if missing:
        return DynoRefusal(
            "dyno_workbench",
            "inputs_missing",
            "a result cannot be built without its conditions; missing: " + "; ".join(missing),
        )
    assert dated is not None and baro is not None and intake is not None
    assert ambient is not None and request.payload_kg is not None
    assert request.induction is not None
    return RunConditions(
        gear=gear,
        direction=direction,
        tune_revision=str(revision),
        induction=request.induction,
        baro_kpa=baro,
        intake_temp_c=intake,
        ambient_temp_c=ambient,
        road=str(request.road),
        date=dated.date(),
        payload_kg=request.payload_kg,
        sessions=[session.name],
    )


def _curve_dict(curve: PowerCurve) -> dict[str, Any]:
    return {
        "rpm": _floats(curve.rpm),
        "wheel_force_n": _floats(curve.wheel_force_n),
        "wheel_power_kw": _floats(curve.wheel_power_kw),
        "wheel_torque_nm": _floats(curve.wheel_torque_nm),
        "engine_power_kw_estimate": _floats(curve.engine_power_kw_estimate),
        "engine_torque_nm_estimate": _floats(curve.engine_torque_nm_estimate),
    }


def _floor(profile: DynoProfile, power: DynoPower) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for quantity in ("wheel_power_kw", "wheel_torque_nm"):
        floor = profile.noise_floor_for(quantity, power.conditions)
        if isinstance(floor, DynoRefusal):
            out[quantity] = floor.as_dict()
            continue
        out[quantity] = {
            "available": True,
            "unit": floor.unit,
            "n_pull_pairs": floor.n_pull_pairs,
            "conditions": floor.conditions.model_dump(mode="json"),
            "bands": [band.model_dump(mode="json") for band in floor.bands],
        }
    return out


def _heading_spread(power: DynoPower) -> dict[str, Any] | None:
    """Half the difference between the two headings at each rpm, per quantity."""
    if power.direction != "pair" or len(power.per_heading) != 2:
        return None
    a, b = power.per_heading.values()
    return {
        "meaning": (
            "half the difference between the two headings at each rpm: what wind and the "
            "grade left in each direction before the pair cancels them; not a noise floor"
        ),
        "wheel_power_kw": _floats(np.abs(a.wheel_power_kw - b.wheel_power_kw) / 2.0),
        "wheel_torque_nm": _floats(np.abs(a.wheel_torque_nm - b.wheel_torque_nm) / 2.0),
    }


def _power_dict(
    session: Path, power: DynoPower, profile: DynoProfile, pitch_source: str
) -> dict[str, Any]:
    return {
        "available": True,
        "session_id": session.name,
        "gear": power.gear,
        "direction": power.direction,
        "n_pulls": power.n_pulls,
        "curve": _curve_dict(power.curve),
        "per_heading": {h: _curve_dict(c) for h, c in power.per_heading.items()},
        "conditions": power.conditions.model_dump(mode="json"),
        "flags": {
            "inertia": power.inertia,
            "squat_corrected": power.squat_corrected,
            "imu_pitch_offset_deg": power.imu_pitch_offset_deg,
            "imu_pitch_offset_source": pitch_source,
            "imu_reference_id": power.imu_reference_id,
            "correction": power.correction,
            "engine_estimate": power.engine_estimate,
            "wheel_torque_convention": power.wheel_torque_convention,
        },
        "test_mass_kg": power.test_mass_kg,
        "effective_mass_kg": power.effective_mass_kg,
        "driveline_efficiency": power.driveline_efficiency,
        "uncertainty": {
            "noise_floor": _floor(profile, power),
            "heading_spread": _heading_spread(power),
        },
    }


def _refused_run(session: Path, gear: int, refusal: DynoRefusal) -> dict[str, Any]:
    return {"session_id": session.name, "gear": gear, **refusal.as_dict()}


def _session_runs(
    session: Path,
    found: list[FoundPull],
    *,
    reads: pd.DataFrame,
    profile: DynoProfile,
    driveline: Driveline,
    vehicle: VehicleParameters,
    wot: WotCriterion | None,
    blocking: list[dict[str, Any]],
    inputs: WorkbenchInputs,
    request: DynoRequest,
) -> list[dict[str, Any]]:
    labelled = label_headings(found)
    by_gear: dict[int, list[PowerPull]] = {}
    for pull in labelled:
        by_gear.setdefault(pull.gear, []).append(pull)
    windows_by_gear: dict[int, list[FoundPull]] = {}
    for item in found:
        if item.pull is not None:
            windows_by_gear.setdefault(item.pull.gear, []).append(item)
    runs: list[dict[str, Any]] = []
    for gear in sorted(by_gear):
        pulls = by_gear[gear]
        if blocking:
            kind, reason = blocked(blocking)
            runs.append(
                {
                    "session_id": session.name,
                    "gear": gear,
                    "available": False,
                    "estimator": "dyno_workbench",
                    "kind": kind,
                    "reason": reason,
                    "n_pulls": len(pulls),
                    "headings": sorted({p.heading for p in pulls}),
                }
            )
            continue
        assert profile.road_load is not None and wot is not None
        headings = {p.heading for p in pulls}
        direction: Literal["pair", "single"] = "pair" if len(headings) == 2 else "single"
        conditions = _run_conditions(
            session,
            gear,
            direction,
            windows_by_gear[gear],
            reads=reads,
            inputs=inputs,
            request=request,
        )
        if isinstance(conditions, DynoRefusal):
            runs.append(_refused_run(session, gear, conditions))
            continue
        offset = estimate_pitch_offset(
            [Leg(p.heading, p.samples) for p in pulls], squat=profile.squat
        )
        if isinstance(offset, DynoRefusal):
            runs.append(_refused_run(session, gear, offset))
            continue
        assert request.payload_kg is not None
        power = compute_power(
            pulls,
            driveline=driveline,
            test_mass_kg=vehicle.mass_kg + request.payload_kg,
            road_load=profile.road_load,
            squat=profile.squat,
            pitch_offset=offset,
            wot=wot,
            conditions=conditions,
            allow_single_direction=request.allow_single_direction,
        )
        if isinstance(power, DynoRefusal):
            runs.append(_refused_run(session, gear, power))
            continue
        runs.append(
            _power_dict(
                session,
                power,
                profile,
                "estimated from this run's own pulls, both headings, with the recorded squat "
                "removed (estimate_pitch_offset)",
            )
        )
    return runs


def blocked(
    blocking: list[dict[str, Any]],
) -> tuple[Literal["coefficients_pending", "prerequisites_missing"], str]:
    """Which kind of "blocked" the blocking prerequisites are, and the sentence.

    ``coefficients_pending`` only when every blocking fact is a measurement
    the vehicle owes (``pending``); any ``missing`` configuration (no WOT
    criterion, a mass without evidence, a transformed demand provider) is
    ``prerequisites_missing`` — a profile to fix, not a drive to make.
    """
    named = "; ".join(
        f"{row['fact']} ({row['status']}"
        + (f", owed by {row['owed_by']}" if row["owed_by"] else "")
        + f": {row['detail']})"
        for row in blocking
    )
    if all(row["status"] == "pending" for row in blocking):
        return (
            "coefficients_pending",
            f"coefficients pending: {named}. No power is computed from a coefficient "
            "nobody measured.",
        )
    return (
        "prerequisites_missing",
        f"the vehicle profile is missing what power rests on: {named}.",
    )


def _unavailable_kind(
    found_total: int,
    pulls_total: int,
    runs: list[dict[str, Any]],
    skipped: list[Unavailable],
    usable: int,
    blocking: list[dict[str, Any]],
) -> UnavailableKind | None:
    if any(run.get("available") for run in runs):
        return None
    if usable == 0 and skipped:
        return "over_budget"
    if blocking and blocked(blocking)[0] == "prerequisites_missing":
        # Without the profile's criterion even the pull refusals are not
        # judged as they would be; the configuration is the answer.
        return "prerequisites_missing"
    if found_total == 0:
        return "no_pull_found"
    if pulls_total == 0:
        return "every_pull_refused"
    if runs and all(run.get("kind") == "coefficients_pending" for run in runs):
        return "coefficients_pending"
    return "every_run_refused"


def dyno_runs(
    sessions: list[Path],
    *,
    inputs: WorkbenchInputs,
    vehicle: VehicleParameters,
    request: DynoRequest,
    unavailable: list[Unavailable] | None = None,
) -> dict[str, Any]:
    """Power and torque against rpm per session and gear, or why not, with coverage."""
    skipped = list(unavailable or [])
    profile_path = None if inputs.profile_data is None else inputs.profile_data / DYNO_FILENAME
    profile = None if profile_path is None else load_dyno_profile(profile_path)
    driveline: Driveline | DynoRefusal | None = (
        None if profile is None else Driveline.from_vehicle(vehicle, profile.inertia)
    )
    wot, demand_spec, demand_reason = _demand(inputs)
    facts = prerequisites(profile, driveline, vehicle, wot, demand_spec, demand_reason)
    base: dict[str, Any] = {
        "request": request.as_dict(),
        "prerequisites": facts,
        "demand": {"spec": demand_spec, "reason": demand_reason},
        "unavailable": [item.as_dict() for item in skipped],
    }
    sources = None if profile is None else profile.motion_sources
    if (
        profile is None
        or not isinstance(driveline, Driveline)
        or sources is None
        or not sources.rpm
    ):
        reason = next(
            (row["detail"] for row in facts if row["status"] == "missing"),
            f"{DYNO_FILENAME} lists no motion_sources.rpm",
        )
        return {
            **base,
            "available": False,
            "unavailable_kind": "not_configured",
            "reason": reason,
            "sessions": [],
            "runs": [],
            "conditions": conditions_block(sessions, inputs),
        }
    blocking = [row for row in facts if row["blocks_power"]]
    per_session: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    found_total = pulls_total = 0
    specs = [
        *session_reads(sources, demand_spec),
        BARO_SPEC,
        INTAKE_TEMP_SPEC,
        AMBIENT_TEMP_SPEC,
    ]
    session_reads_by_name: dict[str, pd.DataFrame] = {}
    for session in sessions:
        reads = _session_read(session, list(dict.fromkeys(specs)), inputs)
        session_reads_by_name[session.name] = reads[reads["spec"] == parse_spec(BARO_SPEC).text]
        found = find_power_pulls(
            session,
            sources=sources,
            driveline=driveline,
            demand_channel=demand_spec,
            wot_threshold=None if wot is None else wot.threshold_value,
            channels=reads,
        )
        labels = iter(p.heading for p in label_headings(found))
        rows = [_found_row(item, next(labels) if item.pull is not None else None) for item in found]
        found_total += len(found)
        pulls_total += sum(1 for item in found if item.pull is not None)
        revision, source, _conflict = resolve_session_revision(session, inputs.registry)
        per_session.append(
            {
                "session_id": session.name,
                "revision": revision,
                "revision_source": source,
                "windows": rows,
                "n_windows": len(found),
                "n_pulls": sum(1 for item in found if item.pull is not None),
            }
        )
        runs += _session_runs(
            session,
            found,
            reads=reads,
            profile=profile,
            driveline=driveline,
            vehicle=vehicle,
            wot=wot,
            blocking=blocking,
            inputs=inputs,
            request=request,
        )
    kind = _unavailable_kind(found_total, pulls_total, runs, skipped, len(sessions), blocking)
    reasons = {
        "over_budget": "every chosen session is over the analysis budget; analyse on the laptop",
        "no_pull_found": "no sustained rpm rise in the chosen sessions",
        "every_pull_refused": "every rpm rise found was refused; each window says why",
        "coefficients_pending": blocked(blocking)[1] if blocking else "",
        "prerequisites_missing": blocked(blocking)[1] if blocking else "",
        "every_run_refused": "pulls were found but no run produced a curve; each run says why",
    }
    return {
        **base,
        "available": kind is None,
        "unavailable_kind": kind,
        "reason": None if kind is None else reasons[kind],
        "sessions": per_session,
        "runs": runs,
        "conditions": conditions_block(sessions, inputs, baro_reads=session_reads_by_name),
    }


__all__ = [
    "AMBIENT_TEMP_SPEC",
    "INTAKE_TEMP_SPEC",
    "DYNO_LAPTOP_ROWS_PER_S",
    "DYNO_PI_SLOWDOWN",
    "DYNO_READ_SHARE",
    "DynoRequest",
    "blocked",
    "dyno_over_budget",
    "dyno_row_budget",
    "dyno_runs",
    "prerequisites",
]
