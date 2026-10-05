"""Independent-drive evaluation of change detection and the adaptive candidate
(E03.3, E03.5).

The kernels (:mod:`src.models.change_detection`,
:mod:`src.models.adaptive_candidate`) work on blocks; this module turns stored
sessions into those blocks under a vehicle's ``change_detection.yaml`` and
reports what the kernels did on drives they never saw:

* :func:`read_session_samples` — the residual's two channels paired one-to-one
  on acquisition time (``src.signals.pairing.match_ordered``, refusing a
  tolerance the clock cannot carry), each pair gated by the declared gates
  (each gate input paired the same way), clamped trims rejected. Every
  dropped row is counted under its reason; a session missing an input is
  ``missing_input``, not empty.
* :func:`assemble_drives` — sessions grouped into physical drives with the
  shared rule (``src.analysis.splits.group_drives``), the default-profile
  exclusion (``src.models.evaluation.default_profile_exclusion``: tuning
  captures never score detectors) and one calibration: a session recorded on
  another calibration is excluded with its revision named, never pooled.
* :func:`prepare` — chronological fixed holdout (newest drives test, the
  ones before calibrate, the rest train; at most three each). The policy is
  *selected before any test drive is seen*: each declared tuning candidate
  is fitted and scored on an inner split of train + calibration only
  (synthetic shifts injected there, never into test drives), then the chosen
  policy's reference is frozen on train + calibration.
* :func:`freeze_policy` — mints a ``policy_frozen`` evidence record naming
  the policy hash, the reference fingerprint and the test drives;
  :func:`evaluate` refuses to monitor a test drive without it.
* :func:`evaluate` — each test drive monitored as recorded (``observed``),
  per block and at drive level, and again with each declared shift injected
  (``synthetic``; ``drive_ramp`` shifts grow across the test drives in
  order). Real events are reported as events, never as false positives.
* :func:`mint_change_report` — the frozen reference as a ``Reference``
  record and every result as ``Evidence`` (observed and synthetic kept apart
  by ``labels["data_origin"]``), alarms as evidence on incident
  ``change:<residual>``.
* :func:`replay_candidate` — drives in order through the candidate built by
  ``candidate_from_ledger`` and its own monitor: monitor, predict, learn; any
  per-block, drive-level or divergence alarm freezes the candidate until an
  investigation resolves it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

import src.models.adaptive_candidate as candidate_module
import src.models.change_detection as change_module
from src.analysis.channels import read_channels
from src.analysis.splits import Drive, chronological_split, group_drives
from src.evidence.ledger import MemoryLedger, MemoryQuery
from src.evidence.provenance import code_version
from src.evidence.records import (
    Conditions,
    Evidence,
    Provenance,
    Quality,
    Quantity,
    Reference,
    Result,
    SourceRef,
    VehicleIdentity,
)
from src.models.adaptive_candidate import (
    CandidateSpec,
    Disposition,
    KalmanCandidate,
    candidate_from_ledger,
    invalidating_boundaries,
    unresolved_alarms,
)
from src.models.change_detection import (
    Block,
    ChangeEvent,
    ChangeMonitor,
    ChangePolicy,
    DeclaredShift,
    DriveVerdict,
    FrozenReference,
    ReferenceRefusedError,
    Sample,
    blocks_of,
    fit_reference,
)
from src.models.evaluation import default_profile_exclusion
from src.signals.pairing import (
    ComparisonRefusedError,
    PairingPolicy,
    check_clock_support,
    match_ordered,
    to_seconds,
    weakest_basis,
)
from src.storage.session_files import session_footer_span
from src.vehicle.tune_revisions import TuneRevisionRegistry, resolve_session_revision

__all__ = [
    "CHANGE_RESIDUALS_FILE",
    "CHANGE_RESIDUALS_SCHEMA",
    "TOOL",
    "TOOL_VERSION",
    "CandidateDriveTrace",
    "ChangeReport",
    "MonitorRun",
    "PolicyNotFrozenError",
    "PolicyRefreezeError",
    "Preparation",
    "DriveData",
    "DriveRampRun",
    "ResidualDefinition",
    "SessionSamples",
    "TuningRow",
    "assemble_drives",
    "evaluate",
    "freeze_policy",
    "invalidating_boundaries",
    "load_change_residuals",
    "mint_change_report",
    "prepare",
    "read_session_samples",
    "replay_candidate",
    "samples_from_frame",
    "samples_to_frame",
    "summary_json",
    "unresolved_alarms",
]

CHANGE_RESIDUALS_FILE = "change_detection.yaml"
CHANGE_RESIDUALS_SCHEMA: Literal["canary-change-residuals/2"] = "canary-change-residuals/2"
TOOL = "change_detection"
#: Bump when the mapping from kernel output to report/evidence changes.
TOOL_VERSION = "2"

DataOrigin = Literal["observed", "synthetic"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Gate(_Strict):
    spec: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    equals: float | None = None
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _one_rule(self) -> Gate:
        if self.equals is None and self.min is None and self.max is None:
            raise ValueError(f"gate on {self.spec} states equals, min or max")
        return self

    def passes(self, values: np.ndarray) -> np.ndarray:
        ok: np.ndarray = np.isfinite(values)
        if self.equals is not None:
            ok &= values == self.equals
        if self.min is not None:
            ok &= values >= self.min
        if self.max is not None:
            ok &= values <= self.max
        return ok


#: Policy fields a tuning candidate may not touch: the fold sizes, the
#: declared shifts and the residual's identity are fixed before tuning.
_UNTUNABLE = ("calibration.n_", "shifts", "schema", "residual", "unit", "features")


class TuningCandidate(_Strict):
    """One policy variant: dotted paths into the base policy and new values."""

    name: str = Field(min_length=1)
    overrides: dict[str, Any]

    @model_validator(mode="after")
    def _tunable(self) -> TuningCandidate:
        fixed = [k for k in self.overrides if k.startswith(_UNTUNABLE)]
        if fixed:
            raise ValueError(f"tuning candidate {self.name!r} may not override {fixed}")
        return self


class Tuning(_Strict):
    """How the policy is chosen without the test drives: an inner split of
    train + calibration, and the declared candidates."""

    n_calibration_drives: int = Field(ge=2, le=3)
    n_test_drives: int = Field(ge=1, le=3)
    candidates: tuple[TuningCandidate, ...] = Field(min_length=1)


class ResidualDefinition(_Strict):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_class: Literal["engine_response"]
    minuend: str = Field(min_length=1)
    subtrahend: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    pairing_max_separation_s: float = Field(gt=0.0)
    gate_max_separation_s: float = Field(gt=0.0)
    gates: tuple[Gate, ...]
    clamp_abs: float = Field(gt=0.0)
    policy: ChangePolicy
    tuning: Tuning
    candidate: CandidateSpec

    @model_validator(mode="after")
    def _consistent(self) -> ResidualDefinition:
        for tuned in self.tuning.candidates:
            self.policy_for(tuned.name)
        if self.policy.residual != self.name or self.candidate.residual != self.name:
            raise ValueError("policy and candidate name this residual")
        if self.policy.excitation.condition != self.condition:
            raise ValueError("the excitation condition is the residual's condition channel")
        if self.policy.unit != self.unit:
            raise ValueError("the policy's unit is the residual's unit")
        for feature in self.policy.features:
            _feature_fn(feature, self.condition)
        return self

    def policy_for(self, candidate: str) -> ChangePolicy:
        """The base policy with one tuning candidate's overrides applied."""
        tuned = next((c for c in self.tuning.candidates if c.name == candidate), None)
        if tuned is None:
            raise KeyError(f"no tuning candidate {candidate!r}")
        raw = self.policy.model_dump(mode="json", by_alias=True)
        for path, value in tuned.overrides.items():
            node = raw
            *parents, leaf = path.split(".")
            for part in parents:
                node = node[part]
            if leaf not in node:
                raise ValueError(f"tuning candidate {candidate!r}: no policy field {path!r}")
            node[leaf] = value
        return ChangePolicy.model_validate(raw)

    @property
    def channels(self) -> tuple[str, ...]:
        return (self.minuend, self.subtrahend, self.condition)

    def regressors(self, condition: np.ndarray) -> np.ndarray:
        """``(n, n_features)`` regressors from the condition channel."""
        cols = [_feature_fn(f, self.condition)(condition) for f in self.policy.features]
        return np.column_stack(cols) if cols else np.empty((len(condition), 0))


def _feature_fn(feature: str, condition: str) -> Callable[[np.ndarray], np.ndarray]:
    """The tiny feature grammar: ``1``, ``<condition>``, ``1/<condition>``."""
    if feature == "1":
        return lambda c: np.ones_like(c, dtype=np.float64)
    if feature == condition:
        return lambda c: np.asarray(c, dtype=np.float64)
    if feature == f"1/{condition}":

        def inverse(c: np.ndarray) -> np.ndarray:
            c = np.asarray(c, dtype=np.float64)
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.asarray(np.where(c > 0, 1.0 / c, np.nan), dtype=np.float64)

        return inverse
    raise ValueError(
        f"feature {feature!r}: only '1', '{condition}' and '1/{condition}' are defined"
    )


class ChangeResiduals(_Strict):
    schema_: Literal["canary-change-residuals/2"] = Field(alias="schema")
    residuals: tuple[ResidualDefinition, ...] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def residual(self, name: str) -> ResidualDefinition:
        for definition in self.residuals:
            if definition.name == name:
                return definition
        raise KeyError(f"no residual {name!r}; declared: {[r.name for r in self.residuals]}")


def load_change_residuals(path: Path) -> tuple[ChangeResiduals, str]:
    """The declarations at ``path`` and the sha256 of its bytes."""
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw)
    if not isinstance(data, dict) or data.get("schema") != CHANGE_RESIDUALS_SCHEMA:
        raise ValueError(f"{path} is not a {CHANGE_RESIDUALS_SCHEMA} file")
    return ChangeResiduals.model_validate(data), hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Sessions to samples


@dataclass
class SessionSamples:
    """One session's gated samples and the count behind every dropped row."""

    session: str
    first: datetime | None
    last: datetime | None
    calibration: str | None
    #: Why the session is not used at all (profile), or ``None``.
    exclusion: str | None
    #: ``missing_input`` when an input channel is absent, else ``None``.
    unevaluable: str | None
    t: np.ndarray = field(default_factory=lambda: np.empty(0))
    y: np.ndarray = field(default_factory=lambda: np.empty(0))
    condition: np.ndarray = field(default_factory=lambda: np.empty(0))
    counts: dict[str, int] = field(default_factory=dict)


def _aware(stamp: Any) -> datetime | None:
    if stamp is None:
        return None
    value = pd.Timestamp(stamp)
    value = value.tz_localize(UTC) if value.tzinfo is None else value.tz_convert(UTC)
    return value.floor("us").to_pydatetime()


def _attach(t_ref: np.ndarray, part: pd.DataFrame, tolerance_s: float, label: str) -> np.ndarray:
    """Each reference instant's paired value of ``part`` (one-to-one), NaN if none."""
    out = np.full(len(t_ref), np.nan)
    if part.empty or len(t_ref) == 0:
        return out
    policy = PairingPolicy.within(pd.Timedelta(seconds=tolerance_s), reference="a")
    times = to_seconds(part["timestamp"])
    basis = (
        weakest_basis(list(part["timestamp_basis"].astype(str)))
        if "timestamp_basis" in part
        else "unknown"
    )
    check_clock_support(
        policy, basis_a="acquired", times_a=t_ref, basis_b=basis, times_b=times, label_b=label
    )
    matched = match_ordered(t_ref, times, policy)
    out[matched.index_a] = part["value"].to_numpy(dtype=np.float64)[matched.index_b]
    return out


def read_session_samples(
    session: Path,
    definition: ResidualDefinition,
    *,
    registry: TuneRevisionRegistry | None,
) -> SessionSamples:
    """The residual's gated samples from one stored session."""
    session = Path(session)
    span = session_footer_span(session)
    calibration, _source, _conflict = resolve_session_revision(session, registry)
    out = SessionSamples(
        session=session.name,
        first=_aware(span.first),
        last=_aware(span.last),
        calibration=calibration,
        exclusion=default_profile_exclusion(session),
        unevaluable=None,
    )
    if out.exclusion is not None:
        return out
    specs = [definition.minuend, definition.subtrahend, definition.condition]
    specs += [g.spec for g in definition.gates]
    long = read_channels(session, specs, acquired_time=True)
    parts = {spec: long[long["spec"] == spec] for spec in specs}
    absent = [spec for spec in specs if parts[spec].empty]
    if absent:
        out.unevaluable = f"missing_input: {absent} never captured in this session"
        return out
    a, b = parts[definition.minuend], parts[definition.subtrahend]
    pairing = PairingPolicy.within(pd.Timedelta(seconds=definition.pairing_max_separation_s))
    ta, tb = to_seconds(a["timestamp"]), to_seconds(b["timestamp"])
    try:
        check_clock_support(
            pairing,
            basis_a=weakest_basis(list(a["timestamp_basis"].astype(str))),
            times_a=ta,
            basis_b=weakest_basis(list(b["timestamp_basis"].astype(str))),
            times_b=tb,
            label_a=definition.minuend,
            label_b=definition.subtrahend,
        )
    except ComparisonRefusedError as exc:
        out.unevaluable = f"refused: {exc}"
        return out
    matched = match_ordered(ta, tb, pairing)
    va = a["value"].to_numpy(dtype=np.float64)[matched.index_a]
    vb = b["value"].to_numpy(dtype=np.float64)[matched.index_b]
    t = np.maximum(ta[matched.index_a], tb[matched.index_b])
    order = np.argsort(t, kind="stable")
    t, va, vb = t[order], va[order], vb[order]
    counts: Counter[str] = Counter()
    counts["minuend_rows"] = len(a)
    counts["subtrahend_rows"] = len(b)
    counts["paired"] = len(t)
    counts["unpaired"] = max(len(a), len(b)) - len(t)
    keep = np.ones(len(t), dtype=bool)
    for gate in definition.gates:
        values = _attach(t, parts[gate.spec], definition.gate_max_separation_s, gate.spec)
        missing = ~np.isfinite(values) & keep
        counts[f"stale_gate:{gate.spec}"] += int(missing.sum())
        keep &= ~missing
        failed = ~gate.passes(values) & keep
        counts[gate.reason] += int(failed.sum())
        keep &= ~failed
    condition = _attach(t, parts[definition.condition], definition.gate_max_separation_s, "cond")
    missing = ~np.isfinite(condition) & keep
    counts["stale_condition"] += int(missing.sum())
    keep &= ~missing
    clamped = ((np.abs(va) >= definition.clamp_abs) | (np.abs(vb) >= definition.clamp_abs)) & keep
    counts["clamped"] += int(clamped.sum())
    keep &= ~clamped
    counts["eligible"] = int(keep.sum())
    out.t, out.y, out.condition = t[keep], (va - vb)[keep], condition[keep]
    out.counts = dict(counts)
    return out


def samples_to_frame(sessions: Sequence[SessionSamples]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Gated samples as one frame, and the per-session metadata (fixtures)."""
    frames = [
        pd.DataFrame({"session": s.session, "t": s.t, "y": s.y, "condition": s.condition})
        for s in sessions
        if len(s.t)
    ]
    frame = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=["session", "t", "y", "condition"])
    )
    meta = {
        s.session: {
            "first": None if s.first is None else s.first.isoformat(),
            "last": None if s.last is None else s.last.isoformat(),
            "calibration": s.calibration,
            "exclusion": s.exclusion,
            "unevaluable": s.unevaluable,
            "counts": s.counts,
        }
        for s in sessions
    }
    return frame, meta


def samples_from_frame(frame: pd.DataFrame, meta: dict[str, Any]) -> list[SessionSamples]:
    out: list[SessionSamples] = []
    for name, info in meta.items():
        rows = frame[frame["session"] == name].sort_values("t", kind="stable")
        out.append(
            SessionSamples(
                session=name,
                first=None if info["first"] is None else datetime.fromisoformat(info["first"]),
                last=None if info["last"] is None else datetime.fromisoformat(info["last"]),
                calibration=info["calibration"],
                exclusion=info["exclusion"],
                unevaluable=info["unevaluable"],
                t=rows["t"].to_numpy(dtype=np.float64),
                y=rows["y"].to_numpy(dtype=np.float64),
                condition=rows["condition"].to_numpy(dtype=np.float64),
                counts={k: int(v) for k, v in info["counts"].items()},
            )
        )
    return out


# ---------------------------------------------------------------------------
# Drives


@dataclass
class DriveData:
    drive: Drive
    sessions: tuple[str, ...]
    t: np.ndarray
    y: np.ndarray
    condition: np.ndarray
    counts: dict[str, int]

    @property
    def drive_id(self) -> str:
        return self.drive.drive_id

    def samples(
        self,
        definition: ResidualDefinition,
        shift: DeclaredShift | None = None,
        position: int = 1,
    ) -> list[Sample]:
        """Samples with ``shift`` added (``position``: this drive's place in a
        ``drive_ramp``, 1 for the first drive it applies to)."""
        y = self.y
        if shift is not None:
            onset = self.onset(shift)
            y = y + np.asarray(
                [shift.offset(t - onset, position) for t in self.t], dtype=np.float64
            )
        x = definition.regressors(self.condition)
        return [
            Sample(t=float(t), y=float(v), x=tuple(float(e) for e in row), condition=float(c))
            for t, v, row, c in zip(self.t, y, x, self.condition, strict=True)
        ]

    def onset(self, shift: DeclaredShift) -> float:
        return float(self.t[0]) + shift.start_after_seconds if len(self.t) else math.nan


def assemble_drives(
    sessions: Sequence[SessionSamples],
    *,
    calibration: str | None,
) -> tuple[list[DriveData], dict[str, str]]:
    """Group sessions into drives; keep samples from eligible sessions on
    ``calibration``. Returns the drives (oldest first) and ``{session:
    reason}`` for every excluded session. A drive is named by its first
    *eligible* piece, never by an excluded capture that bridges it."""
    ranges = {s.session: (s.first, s.last) for s in sessions if s.first is not None}
    excluded: dict[str, str] = {
        s.session: "no stored timestamps" for s in sessions if s.first is None
    }
    by_name = {s.session: s for s in sessions}
    drives: list[DriveData] = []
    for drive in group_drives(ranges):
        names: list[str] = []
        counts: Counter[str] = Counter()
        t_parts: list[np.ndarray] = []
        y_parts: list[np.ndarray] = []
        c_parts: list[np.ndarray] = []
        for piece in drive.pieces:
            s = by_name[piece]
            if s.exclusion is not None:
                excluded[piece] = s.exclusion
                continue
            if s.calibration != calibration:
                excluded[piece] = (
                    f"calibration {s.calibration!r}, not the reference's {calibration!r}"
                )
                continue
            if s.unevaluable is not None:
                excluded[piece] = s.unevaluable
                continue
            names.append(piece)
            counts.update(s.counts)
            t_parts.append(s.t)
            y_parts.append(s.y)
            c_parts.append(s.condition)
        if not names:
            continue
        t = np.concatenate(t_parts)
        order = np.argsort(t, kind="stable")
        drives.append(
            DriveData(
                drive=dataclasses.replace(drive, drive_id=names[0]),
                sessions=tuple(names),
                t=t[order],
                y=np.concatenate(y_parts)[order],
                condition=np.concatenate(c_parts)[order],
                counts=dict(counts),
            )
        )
    return drives, excluded


# ---------------------------------------------------------------------------
# Monitoring runs


@dataclass
class MonitorRun:
    """One monitored pass over one drive (as recorded, or with a shift)."""

    drive: str
    data_origin: DataOrigin
    shift: str | None
    n_blocks: int
    evaluated_blocks: int
    unevaluable_blocks: dict[str, int]
    dropped_samples: dict[str, int]
    inside: int
    exceedances: int
    coverage_by_cell: dict[str, tuple[int, int]]
    events: list[ChangeEvent]
    drive_verdict: DriveVerdict | None = None
    onset: float | None = None
    #: Per detector: seconds from injected onset to the first event in the
    #: shift's direction raised at or after onset; ``None`` if none.
    latency_s: dict[str, float | None] = field(default_factory=dict)
    events_before_onset: int = 0
    #: Blocks at or after the injected onset: a short drive cannot show a
    #: detection whatever the detector does, and says so.
    blocks_after_onset: int = 0

    @property
    def coverage(self) -> float | None:
        return self.inside / self.evaluated_blocks if self.evaluated_blocks else None


@dataclass
class DriveRampRun:
    """A ``drive_ramp`` shift across the test drives, in order (synthetic)."""

    shift: str
    drives: list[MonitorRun]
    #: Position (1-based) of the first drive whose close raised a drive-level
    #: alarm in the shift's direction, and of the first with a per-block one.
    first_drive_alarm: int | None
    first_block_alarm: int | None


def _blocks(
    drive: DriveData,
    policy: ChangePolicy,
    definition: ResidualDefinition,
    shift: DeclaredShift | None = None,
    position: int = 1,
) -> tuple[list[Block], dict[str, int]]:
    return blocks_of(policy, drive.drive_id, drive.samples(definition, shift, position))


def _monitor_drive(
    monitor: ChangeMonitor,
    drive: DriveData,
    policy: ChangePolicy,
    definition: ResidualDefinition,
    shift: DeclaredShift | None,
    position: int = 1,
) -> MonitorRun:
    blocks, dropped = _blocks(drive, policy, definition, shift, position)
    n_events = len(monitor.events)
    monitor.start_drive(drive.drive_id)
    unevaluable: Counter[str] = Counter()
    by_cell: dict[str, list[int]] = {}
    inside = 0
    evaluated = 0
    for block in blocks:
        verdict = monitor.observe(block)
        if verdict.status != "evaluated":
            unevaluable[(verdict.reason or "unevaluable").split(":")[0]] += 1
            continue
        evaluated += 1
        cell = by_cell.setdefault(block.cell, [0, 0])
        cell[1] += 1
        if verdict.inside:
            inside += 1
            cell[0] += 1
    drive_verdict = monitor.close_drive()
    run = MonitorRun(
        drive=drive.drive_id,
        data_origin="observed" if shift is None else "synthetic",
        shift=None if shift is None else shift.name,
        n_blocks=len(blocks),
        evaluated_blocks=evaluated,
        unevaluable_blocks=dict(unevaluable),
        dropped_samples=dropped,
        inside=inside,
        exceedances=evaluated - inside,
        coverage_by_cell={k: (v[0], v[1]) for k, v in by_cell.items()},
        events=list(monitor.events[n_events:]),
        drive_verdict=drive_verdict,
    )
    if shift is not None and shift.kind != "drive_ramp":
        onset = drive.onset(shift)
        run.onset = onset
        want = "+" if shift.magnitude > 0 else "-"
        run.events_before_onset = sum(1 for e in run.events if e.first_alarm < onset)
        run.blocks_after_onset = sum(1 for b in blocks if b.start >= onset)
        for detector in ("cusum", "glr"):
            hits = [
                e.first_alarm - onset
                for e in run.events
                if e.detector == detector and e.direction == want and e.first_alarm >= onset
            ]
            run.latency_s[detector] = min(hits) if hits else None
    return run


def _shift_trials(
    reference: FrozenReference,
    policy: ChangePolicy,
    definition: ResidualDefinition,
    drives: Sequence[DriveData],
) -> tuple[list[MonitorRun], list[MonitorRun], list[DriveRampRun]]:
    """Observed runs (one monitor across ``drives`` in order), per-drive
    shift runs (a fresh monitor each) and drive-ramp runs."""
    monitor = ChangeMonitor(reference)
    observed = [_monitor_drive(monitor, d, policy, definition, None) for d in drives]
    synthetic: list[MonitorRun] = []
    ramps: list[DriveRampRun] = []
    for shift in policy.shifts:
        if shift.kind == "drive_ramp":
            ramp_monitor = ChangeMonitor(reference)
            runs = [
                _monitor_drive(ramp_monitor, d, policy, definition, shift, i)
                for i, d in enumerate(drives, start=1)
            ]
            want = "+" if shift.magnitude > 0 else "-"

            def first(
                kinds: tuple[str, ...], runs: list[MonitorRun] = runs, want: str = want
            ) -> int | None:
                return next(
                    (
                        i
                        for i, r in enumerate(runs, start=1)
                        if any(e.detector in kinds and e.direction == want for e in r.events)
                    ),
                    None,
                )

            ramps.append(
                DriveRampRun(
                    shift=shift.name,
                    drives=runs,
                    first_drive_alarm=first(("drive_cusum", "drive_glr")),
                    first_block_alarm=first(("cusum", "glr")),
                )
            )
            continue
        for d in drives:
            synthetic.append(_monitor_drive(ChangeMonitor(reference), d, policy, definition, shift))
    return observed, synthetic, ramps


# ---------------------------------------------------------------------------
# Preparation: folds, policy selection, the frozen reference


@dataclass
class TuningRow:
    name: str
    policy_sha256: str
    refusal: str | None
    observed_events: int = 0
    detected: int = 0
    trials: int = 0
    mean_latency_s: float | None = None


@dataclass
class Preparation:
    """Everything decided before a test drive is monitored."""

    definition: ResidualDefinition
    policy: ChangePolicy
    selected: str
    calibration: str | None
    residuals_sha256: str
    folds: dict[str, list[str]]
    tuning_folds: dict[str, list[str]]
    tuning: list[TuningRow]
    drives: dict[str, DriveData]
    excluded_sessions: dict[str, str]
    unevaluable_drives: dict[str, str]
    reference: FrozenReference | None
    refusal: str | None


def _fold_blocks(
    ids: Sequence[str], drives: dict[str, DriveData], policy: ChangePolicy, d: ResidualDefinition
) -> list[Block]:
    return [b for i in ids for b in _blocks(drives[i], policy, d)[0]]


def _fit(
    policy: ChangePolicy,
    definition: ResidualDefinition,
    drives: dict[str, DriveData],
    train: Sequence[str],
    calibration_fold: Sequence[str],
    calibration: str | None,
) -> FrozenReference:
    return fit_reference(
        policy,
        _fold_blocks(train, drives, policy, definition),
        _fold_blocks(calibration_fold, drives, policy, definition),
        name=f"{definition.name}@{calibration}",
        calibration_revision=calibration,
    )


def prepare(
    drives: Sequence[DriveData],
    definition: ResidualDefinition,
    *,
    calibration: str | None,
    excluded_sessions: dict[str, str],
    residuals_sha256: str,
) -> Preparation:
    """Folds, policy selection on train + calibration only, frozen reference."""
    base = definition.policy
    by_id = {d.drive_id: d for d in drives}
    usable = [d for d in drives if _blocks(d, base, definition)[0]]
    unevaluable = {
        d.drive_id: "uncovered: no block of the minimum length inside a declared cell"
        for d in drives
        if d not in usable
    }
    split = chronological_split(
        [d.drive for d in usable],
        n_test=base.calibration.n_test_drives,
        n_calibration=base.calibration.n_calibration_drives,
    )
    folds = {
        "train": [d.drive_id for d in split.train],
        "calibration": [d.drive_id for d in split.calibration],
        "test": [d.drive_id for d in split.test],
    }
    inner = chronological_split(
        [*split.train, *split.calibration],
        n_test=definition.tuning.n_test_drives,
        n_calibration=definition.tuning.n_calibration_drives,
    )
    tuning_folds = {
        "train": [d.drive_id for d in inner.train],
        "calibration": [d.drive_id for d in inner.calibration],
        "test": [d.drive_id for d in inner.test],
    }
    rows: list[TuningRow] = []
    for candidate in definition.tuning.candidates:
        policy = definition.policy_for(candidate.name)
        row = TuningRow(name=candidate.name, policy_sha256=policy.sha256(), refusal=None)
        rows.append(row)
        try:
            reference = _fit(
                policy,
                definition,
                by_id,
                tuning_folds["train"],
                tuning_folds["calibration"],
                calibration,
            )
        except ReferenceRefusedError as exc:
            row.refusal = str(exc)
            continue
        inner_test = [by_id[i] for i in tuning_folds["test"]]
        observed, synthetic, _ramps = _shift_trials(reference, policy, definition, inner_test)
        row.observed_events = sum(len(r.events) for r in observed)
        latencies = [r.latency_s.get("cusum") for r in synthetic]
        found = [lat for lat in latencies if lat is not None]
        row.trials = len(latencies)
        row.detected = len(found)
        row.mean_latency_s = float(np.mean(found)) if found else None
    order = sorted(
        range(len(rows)),
        key=lambda i: (
            rows[i].refusal is not None,
            rows[i].observed_events,
            -rows[i].detected,
            rows[i].mean_latency_s if rows[i].mean_latency_s is not None else math.inf,
            i,
        ),
    )
    chosen = rows[order[0]]
    policy = definition.policy_for(chosen.name)
    prep = Preparation(
        definition=definition,
        policy=policy,
        selected=chosen.name,
        calibration=calibration,
        residuals_sha256=residuals_sha256,
        folds=folds,
        tuning_folds=tuning_folds,
        tuning=rows,
        drives=by_id,
        excluded_sessions=dict(excluded_sessions),
        unevaluable_drives=unevaluable,
        reference=None,
        refusal=None,
    )
    if chosen.refusal is not None:
        prep.refusal = f"every tuning candidate was refused; first: {chosen.refusal}"
        return prep
    try:
        prep.reference = _fit(
            policy, definition, by_id, folds["train"], folds["calibration"], calibration
        )
    except ReferenceRefusedError as exc:
        prep.refusal = str(exc)
    return prep


class PolicyNotFrozenError(RuntimeError):
    """Test drives were about to be monitored without a recorded policy freeze."""


class PolicyRefreezeError(RuntimeError):
    """A test drive already has a standing freeze under another policy: its
    results were (or could have been) seen, so it cannot test a new one."""


POLICY_FROZEN = "policy_frozen"


def _provenance() -> Provenance:
    return Provenance(
        producer=TOOL,
        producer_kind="tool",
        version=TOOL_VERSION,
        code_version=code_version(sys.modules[__name__], change_module, candidate_module),
    )


def _sources(drives: Sequence[DriveData], definition: ResidualDefinition) -> tuple[SourceRef, ...]:
    return tuple(
        SourceRef(
            session=",".join(drive.sessions),
            window_start=datetime.fromtimestamp(float(drive.t[0]), UTC),
            window_end=datetime.fromtimestamp(float(drive.t[-1]), UTC),
            channels=definition.channels,
            n_samples=len(drive.t),
        )
        for drive in drives
        if len(drive.t)
    )


def _drive_hash(drives: Sequence[DriveData]) -> str:
    digest = hashlib.sha256()
    for drive in drives:
        for array in (drive.t, drive.y, drive.condition):
            digest.update(np.ascontiguousarray(array, dtype=np.float64).tobytes())
    return digest.hexdigest()


def _frozen_policy_payload(prep: Preparation) -> dict[str, Any]:
    assert prep.reference is not None
    return {
        "policy_sha256": prep.policy.sha256(),
        "selected": prep.selected,
        "reference_fingerprint": prep.reference.fingerprint,
        "test_drives": list(prep.folds["test"]),
        "folds": prep.folds,
        "tuning_folds": prep.tuning_folds,
        "tuning": [dataclasses.asdict(row) for row in prep.tuning],
    }


def freeze_policy(
    ledger: MemoryLedger,
    prep: Preparation,
    *,
    vehicle: VehicleIdentity,
    now: datetime | None = None,
) -> str:
    """Record the selected policy and the reference before any test drive is
    monitored; returns the record id :func:`evaluate` requires."""
    if prep.reference is None:
        raise ValueError(f"no reference was frozen: {prep.refusal}")
    seen = [prep.drives[i] for i in (*prep.folds["train"], *prep.folds["calibration"])]
    payload = _frozen_policy_payload(prep)
    tests = set(payload["test_drives"])
    for entry in ledger.query(MemoryQuery(kinds=("evidence",))):
        earlier = entry.record
        if (
            not isinstance(earlier, Evidence)
            or earlier.labels.get("kind") != POLICY_FROZEN
            or earlier.labels.get("residual") != prep.definition.name
        ):
            continue
        overlap = tests & set(earlier.policy.get("test_drives", ()))
        if not overlap:
            continue
        same = all(
            earlier.policy.get(key) == payload[key]
            for key in ("policy_sha256", "reference_fingerprint", "test_drives")
        )
        if same:
            return earlier.id  # the same freeze, recorded once
        raise PolicyRefreezeError(
            f"test drive(s) {sorted(overlap)} were frozen by {earlier.id} under policy "
            f"{str(earlier.policy.get('policy_sha256'))[:16]}; a re-tuned policy needs drives "
            "no earlier policy was evaluated on"
        )
    record = Evidence(
        vehicle=vehicle,
        provenance=_provenance(),
        conditions=Conditions(
            regime=f"{prep.definition.name}: policy selection", vehicle_state=None
        ),
        valid_from=prep.reference.valid_from,
        valid_to=prep.reference.valid_to,
        subject=(
            f"{POLICY_FROZEN}:{prep.definition.name}:{payload['policy_sha256'][:16]}:"
            f"{prep.reference.fingerprint[:16]}"
        ),
        sources=_sources(seen, prep.definition),
        input_hashes={
            "policy": payload["policy_sha256"],
            "change_residuals": prep.residuals_sha256,
            "samples": _drive_hash(seen),
        },
        policy=payload,
        values={
            "test_drives": Quantity(value=float(len(prep.folds["test"])), unit="1"),
            "tuning_candidates": Quantity(value=float(len(prep.tuning)), unit="1"),
        },
        labels={"kind": POLICY_FROZEN, "selected": prep.selected, "residual": prep.definition.name},
        quality=Quality(n_observations=len(seen), timestamp_basis="acquired"),
        result=Result(status="evaluated"),
    )
    return ledger.append(record, now=now).id


def _check_frozen(ledger: MemoryLedger, prep: Preparation, record_id: str) -> None:
    try:
        entry = ledger.entry(record_id)
    except Exception as exc:
        raise PolicyNotFrozenError(f"{record_id}: not on this ledger ({exc})") from exc
    record = entry.record
    if not isinstance(record, Evidence) or record.labels.get("kind") != POLICY_FROZEN:
        raise PolicyNotFrozenError(f"{record_id} is not a {POLICY_FROZEN} record")
    if not entry.current:
        raise PolicyNotFrozenError(f"{record_id} was superseded or retracted")
    expected = _frozen_policy_payload(prep)
    for key in ("policy_sha256", "reference_fingerprint", "test_drives"):
        if record.policy.get(key) != expected[key]:
            raise PolicyNotFrozenError(
                f"{record_id} froze {key}={record.policy.get(key)!r}; this evaluation has "
                f"{expected[key]!r}"
            )


# ---------------------------------------------------------------------------
# Evaluation


@dataclass
class ChangeReport:
    prep: Preparation
    frozen_record: str
    observed: list[MonitorRun]
    synthetic: list[MonitorRun]
    drive_ramps: list[DriveRampRun]

    @property
    def reference(self) -> FrozenReference:
        assert self.prep.reference is not None
        return self.prep.reference

    def coverage_interval(self) -> dict[str, Any]:
        """Per-drive coverage and a t interval over drives (drives are the
        independent units; blocks within one drive are not)."""
        per_drive = [r.coverage for r in self.observed if r.coverage is not None]
        out: dict[str, Any] = {"per_drive": [round(c, 3) for c in per_drive]}
        if len(per_drive) >= 2:
            mean = float(np.mean(per_drive))
            half = (
                _T975[min(len(per_drive) - 1, len(_T975) - 1)]
                * float(np.std(per_drive, ddof=1))
                / math.sqrt(len(per_drive))
            )
            out.update(
                mean=round(mean, 3),
                low=round(max(0.0, mean - half), 3),
                high=round(min(1.0, mean + half), 3),
                nominal=1.0 - self.prep.policy.calibration.miscoverage,
            )
        return out

    def summary(self) -> dict[str, Any]:
        """The report as plain data (printing, JSON, checklist evidence)."""

        def run(r: MonitorRun) -> dict[str, Any]:
            dv = r.drive_verdict
            return {
                "drive": r.drive,
                "origin": r.data_origin,
                "shift": r.shift,
                "blocks": r.n_blocks,
                "evaluated": r.evaluated_blocks,
                "unevaluable": r.unevaluable_blocks,
                "dropped_samples": r.dropped_samples,
                "coverage": None if r.coverage is None else round(r.coverage, 3),
                "coverage_by_cell": r.coverage_by_cell,
                "exceedances": r.exceedances,
                "events": dict(Counter(f"{e.detector}:{e.direction}" for e in r.events)),
                "drive_u": None if dv is None or dv.u is None else round(dv.u, 2),
                "drive_mean": None
                if dv is None or dv.mean_residual is None
                else round(dv.mean_residual, 2),
                "latency_s": {
                    k: None if v is None else round(v, 1) for k, v in r.latency_s.items()
                },
                "events_before_onset": r.events_before_onset,
                "blocks_after_onset": r.blocks_after_onset,
            }

        ref = self.reference
        prep = self.prep
        return {
            "residual": prep.definition.name,
            "calibration": prep.calibration,
            "selected_policy": prep.selected,
            "policy_sha256": prep.policy.sha256()[:16],
            "frozen_record": self.frozen_record,
            "tuning": [dataclasses.asdict(r) for r in prep.tuning],
            "folds": prep.folds,
            "tuning_folds": prep.tuning_folds,
            "excluded_sessions": len(prep.excluded_sessions),
            "excluded_reasons": dict(
                Counter(
                    reason.split(":")[0].split(" (")[0]
                    for reason in prep.excluded_sessions.values()
                )
            ),
            "unevaluable_drives": prep.unevaluable_drives,
            "reference": _reference_summary(ref),
            "coverage": self.coverage_interval(),
            "observed": [run(r) for r in self.observed],
            "synthetic": [run(r) for r in self.synthetic],
            "drive_ramps": [
                {
                    "shift": ramp.shift,
                    "first_drive_alarm": ramp.first_drive_alarm,
                    "first_block_alarm": ramp.first_block_alarm,
                    "drives": [run(r) for r in ramp.drives],
                }
                for ramp in self.drive_ramps
            ],
        }


#: Two-sided 97.5 % Student t quantiles for 1..10 degrees of freedom.
_T975 = (math.inf, 12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228)


def _reference_summary(ref: FrozenReference) -> dict[str, Any]:
    return {
        "fingerprint": ref.fingerprint[:16],
        "theta": [round(v, 3) for v in ref.theta],
        "cells": {
            c.cell: {
                "scale": round(c.scale, 3),
                "halfwidth": round(c.halfwidth, 3),
                "coverage_guaranteed": c.coverage_guaranteed,
                "blocks": c.n_blocks,
                "effective": round(c.n_effective, 1),
                "drives": c.n_drives,
                "within_sd": round(c.within_sd, 3),
                "between_sd": round(c.between_sd, 3),
                "bias": round(c.bias, 3),
            }
            for c in ref.cells
        },
        "uncalibrated": ref.uncalibrated_cells,
        "within_drive_lag1": round(ref.within_drive_lag1, 3),
        "variance_inflation": round(ref.variance_inflation, 3),
    }


def evaluate(prep: Preparation, *, ledger: MemoryLedger, frozen_record: str) -> ChangeReport:
    """Monitor the test drives under the frozen policy (refused without it)."""
    if prep.reference is None:
        raise ValueError(f"no reference was frozen: {prep.refusal}")
    _check_frozen(ledger, prep, frozen_record)
    tests = [prep.drives[i] for i in prep.folds["test"]]
    observed, synthetic, ramps = _shift_trials(prep.reference, prep.policy, prep.definition, tests)
    return ChangeReport(
        prep=prep,
        frozen_record=frozen_record,
        observed=observed,
        synthetic=synthetic,
        drive_ramps=ramps,
    )


# ---------------------------------------------------------------------------
# Evidence


def mint_change_report(
    ledger: MemoryLedger,
    report: ChangeReport,
    *,
    vehicle: VehicleIdentity,
    now: datetime | None = None,
) -> dict[str, list[str]]:
    """Append the per-drive results, the alarms and the reference; returns
    ids by kind (``reference``, ``observed``, ``synthetic``, ``alarms``)."""
    prep = report.prep
    definition = prep.definition
    ref = report.reference
    provenance = _provenance()
    regime = (
        f"{definition.name}: closed-loop, warm, running, LTFT at zero; blocks of at most "
        f"{prep.policy.dependence.block_seconds:g} s inside cells "
        f"{[c.name for c in prep.policy.excitation.cells]} of {definition.condition}"
    )
    conditions = Conditions(regime=regime, vehicle_state="engine_running")
    policy = {
        "policy_sha256": prep.policy.sha256(),
        "residuals_sha256": prep.residuals_sha256,
        "reference_fingerprint": ref.fingerprint,
        "frozen_record": report.frozen_record,
        "folds": prep.folds,
    }
    ids: dict[str, list[str]] = {"observed": [], "synthetic": [], "alarms": [], "reference": []}
    linked = Provenance(**{**provenance.model_dump(), "derived_from": (report.frozen_record,)})

    def record_run(run: MonitorRun, ramp: str | None = None) -> str:
        drive = prep.drives[run.drive]
        values: dict[str, Quantity] = {
            "blocks": Quantity(value=float(run.n_blocks), unit="1"),
            "evaluated_blocks": Quantity(value=float(run.evaluated_blocks), unit="1"),
            "unevaluable_blocks": Quantity(
                value=float(sum(run.unevaluable_blocks.values())), unit="1"
            ),
            "dropped_samples": Quantity(value=float(sum(run.dropped_samples.values())), unit="1"),
            "exceedances": Quantity(value=float(run.exceedances), unit="1"),
        }
        for detector in ("cusum", "glr", "drive_cusum", "drive_glr"):
            values[f"events_{detector}"] = Quantity(
                value=float(sum(1 for e in run.events if e.detector == detector)), unit="1"
            )
        if run.coverage is not None:
            values["coverage"] = Quantity(value=run.coverage, unit="1")
        dv = run.drive_verdict
        if dv is not None and dv.u is not None and dv.mean_residual is not None:
            values["drive_mean_residual"] = Quantity(value=dv.mean_residual, unit=definition.unit)
            values["drive_u"] = Quantity(value=dv.u, unit="1")
        labels = {
            "data_origin": run.data_origin,
            "evidence_class": definition.evidence_class,
            "residual": definition.name,
            "reference": ref.fingerprint[:16],
        }
        if run.shift is not None:
            labels["shift"] = run.shift
            values["events_before_onset"] = Quantity(value=float(run.events_before_onset), unit="1")
            values["blocks_after_onset"] = Quantity(value=float(run.blocks_after_onset), unit="1")
            for detector, latency in run.latency_s.items():
                labels[f"detected_{detector}"] = "yes" if latency is not None else "no"
                if latency is not None:
                    values[f"latency_{detector}"] = Quantity(value=latency, unit="s")
        if ramp is not None:
            labels["drive_ramp_position"] = ramp
        result = (
            Result(status="evaluated")
            if run.evaluated_blocks
            else Result(
                status="unevaluable",
                reason="uncovered",
                detail=(
                    f"no block in a calibrated cell; unevaluable blocks {run.unevaluable_blocks}"
                ),
            )
        )
        tail = "observed" if run.shift is None else f"synthetic:{run.shift}"
        if ramp is not None:
            tail += f":{ramp}"
        evidence = Evidence(
            vehicle=vehicle,
            provenance=linked,
            conditions=conditions,
            valid_from=datetime.fromtimestamp(float(drive.t[0]), UTC),
            valid_to=datetime.fromtimestamp(float(drive.t[-1]), UTC),
            subject=f"{TOOL}:{definition.name}:{ref.fingerprint[:16]}:{run.drive}:{tail}",
            sources=_sources([drive], definition),
            input_hashes={
                "samples": _drive_hash([drive]),
                "change_residuals": prep.residuals_sha256,
            },
            policy=policy,
            values=values,
            labels=labels,
            quality=Quality(
                n_observations=run.evaluated_blocks,
                timestamp_basis="acquired",
                flags=("synthetic",) if run.shift is not None else (),
            ),
            result=result,
        )
        return ledger.append(evidence, now=now).id

    for run in report.observed:
        ids["observed"].append(record_run(run))
        drive = prep.drives[run.drive]
        for event in run.events:
            alarm = Evidence(
                vehicle=vehicle,
                provenance=linked,
                conditions=conditions,
                valid_from=datetime.fromtimestamp(event.estimated_onset, UTC),
                valid_to=datetime.fromtimestamp(event.last_alarm, UTC),
                subject=f"change_alarm:{definition.name}:{event.alarm_id}",
                incident=f"change:{definition.name}",
                sources=_sources([drive], definition),
                input_hashes={"samples": _drive_hash([drive])},
                policy=policy,
                values={
                    "shift_estimate": Quantity(value=event.shift_estimate, unit=definition.unit),
                    "peak_statistic": Quantity(value=event.peak_statistic, unit="1"),
                    "alarms": Quantity(value=float(event.n_alarms), unit="1"),
                },
                labels={
                    "kind": "change_alarm",
                    "data_origin": "observed",
                    "detector": event.detector,
                    "direction": event.direction,
                    "residual": definition.name,
                    "reference": ref.fingerprint[:16],
                    "evidence_class": definition.evidence_class,
                },
                quality=Quality(n_observations=event.n_alarms, timestamp_basis="acquired"),
                result=Result(
                    status="evaluated",
                    residual=Quantity(value=event.shift_estimate, unit=definition.unit),
                ),
            )
            ids["alarms"].append(ledger.append(alarm, now=now).id)
    for run in report.synthetic:
        ids["synthetic"].append(record_run(run))
    for ramp in report.drive_ramps:
        for position, run in enumerate(ramp.drives, start=1):
            ids["synthetic"].append(record_run(run, ramp=str(position)))
    reference = Reference(
        vehicle=vehicle,
        provenance=Provenance(
            **{
                **provenance.model_dump(),
                "derived_from": (report.frozen_record, *ids["observed"]),
            }
        ),
        conditions=conditions,
        valid_from=ref.valid_from,
        reference_kind="evidence",
        name=ref.name,
        channels=definition.channels,
        source="memory",
        source_ref=f"change_reference:{ref.fingerprint}",
        investigation=None,
        evidence=tuple(ids["observed"]),
    )
    ids["reference"].append(ledger.append(reference, now=now).id)
    return ids


# ---------------------------------------------------------------------------
# Candidate replay


@dataclass
class CandidateDriveTrace:
    drive: str
    frozen_at_start: tuple[str, ...]
    outcomes: dict[str, int]
    block_alarms: list[str]
    drive_alarms: list[str]
    divergence_alarms: list[str]
    theta_after: tuple[float, ...]
    #: Largest candidate-minus-reference prediction gap over the declared
    #: probes, after this drive.
    divergence: float
    checkpoint: str
    #: Mean block residual against the frozen reference, and against the
    #: candidate's own prediction (made before learning each block).
    reference_residual: float | None
    candidate_residual: float | None
    drive_u: float | None
    min_information_eigenvalue: float | None


def replay_candidate(
    drives: Sequence[tuple[str, list[Block]]],
    reference: FrozenReference,
    spec: CandidateSpec,
    *,
    ledger: MemoryLedger,
    calibration: str | None,
    channels: Iterable[str],
    disposition: Callable[[Block], Disposition] = lambda _block: "accept",
    now: datetime | None = None,
) -> tuple[list[CandidateDriveTrace], KalmanCandidate]:
    """Monitor, predict, learn over ``drives`` in order (each ``(id, blocks)``).

    The candidate comes from :func:`candidate_from_ledger`, so it starts with
    every unresolved alarm and invalidating intervention the ledger holds, and
    its own monitor issues the verdict each block must carry to be learned.
    Alarms raised during the replay stay unresolved for the rest of it.
    """
    candidate = candidate_from_ledger(
        ledger, spec, reference, calibration=calibration, channels=channels, now=now
    )
    monitor = candidate.monitor
    traces: list[CandidateDriveTrace] = []
    for drive_id, blocks in drives:
        frozen = candidate.frozen_by
        n_events = len(monitor.events)
        n_div = len(candidate.divergence_alarms)
        monitor.start_drive(drive_id)
        candidate.start_drive()
        outcomes: Counter[str] = Counter()
        ref_res: list[float] = []
        cand_res: list[float] = []
        for block in blocks:
            verdict = monitor.observe(block)
            pending = candidate.predict(block)
            if verdict.residual is not None:
                ref_res.append(verdict.residual)
            cand_res.append(block.y - pending.prediction)
            outcomes[
                candidate.learn(
                    pending, verdict, disposition=disposition(block), calibration=calibration
                )
            ] += 1
        drive_verdict = monitor.close_drive()
        new_events = monitor.events[n_events:]
        traces.append(
            CandidateDriveTrace(
                drive=drive_id,
                frozen_at_start=frozen,
                outcomes=dict(outcomes),
                block_alarms=[e.alarm_id for e in new_events if not e.detector.startswith("drive")],
                drive_alarms=[e.alarm_id for e in new_events if e.detector.startswith("drive")],
                divergence_alarms=candidate.divergence_alarms[n_div:],
                theta_after=candidate.theta,
                divergence=candidate.divergence(),
                checkpoint=candidate.checkpoint().fingerprint,
                reference_residual=float(np.mean(ref_res)) if ref_res else None,
                candidate_residual=float(np.mean(cand_res)) if cand_res else None,
                drive_u=drive_verdict.u,
                min_information_eigenvalue=candidate.information_min_eigenvalue(),
            )
        )
    return traces, candidate


def summary_json(report: ChangeReport) -> str:
    return json.dumps(report.summary(), indent=2, default=str)
