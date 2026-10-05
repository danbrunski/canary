"""Known-answer investigation cases: the memory path must reproduce history (E01.6).

``tests/fixtures/ground_truth/investigation_cases.yaml`` records what the
project already knows the answer to — an input that was never captured, a
correlation that misled, a flash boundary, a recurrence, a retraction — as
*cases* over committed slices of real drives. :func:`run_case` replays one
case through the real chain: :func:`~src.analysis.compare_evidence.mint_channel_comparison`
over the slice, the per-vehicle :class:`~src.evidence.ledger.MemoryLedger`,
:func:`~src.evidence.interventions.record_intervention` for a flash in the
tracked ``tune_revisions.yaml``, and :func:`~src.evidence.cards.investigation_card`.
:func:`case_outcome` reduces the result to what an operator would read, with
record ids replaced by the case's step names (ids are minted over record time,
so they differ on every run). :func:`check_case` holds that outcome against

* the case's ``known`` checks — the answers history established, each naming
  its source. These are the labels: a narrative alone never is one, and an
  incident with no case states why in its ``gaps``;
* the tool-generated snapshot (``investigation_cases.expected.yaml``, written
  by ``tools/make_investigation_cases.py``), so any drift in what the chain
  says is a reviewed diff rather than a silent change.

What the file format makes impossible rather than documented:

* **Incidents and drives are declared before cases.** A case names exactly
  one incident and the drives it reads; an incident has at most one case, so
  one event cannot be counted as two independent confirmations, and the
  occurrences of a recurrence must come from different drives.
* **A slice's absence is not the session's.** Fixture slices keep only the
  channels a check needs; ``drive-slice`` dropped Mode 01 PID 0x44 that the
  full session holds. An unevaluable ``missing_input`` result is refused
  (:class:`SliceAbsenceError`) unless the drive's ``full_session`` facts —
  counted over every chunk of the original session — record zero rows for the
  channel it names.
* **Synthetic is labelled.** Negative controls mutate a real case's inputs and
  are ``synthetic: true``; only real cases count toward category coverage.
* **A known defect stays visible.** A known answer the chain does not yet
  reproduce carries ``known_defect``; it must still fail (strict), so a fix
  shows up as a check to promote rather than passing unnoticed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.compare import PairingPolicy
from src.analysis.compare_evidence import ALL_CONDITIONS, mint_channel_comparison
from src.evidence.cards import InvestigationCard, investigation_card
from src.evidence.interventions import record_intervention
from src.evidence.ledger import MemoryLedger, MemoryQuery
from src.evidence.records import (
    AnyRecord,
    Conditions,
    Evidence,
    Hypothesis,
    Intervention,
    Investigation,
    Provenance,
    Reference,
    Retraction,
    VehicleIdentity,
)
from src.paths import BINDINGS_YAML, DIDS_YAML, REPO_ROOT
from src.vehicle.interventions import intervention_history
from src.vehicle.tune_revisions import TuneRevisionRegistry, load_tune_revisions

__all__ = [
    "CASES_SCHEMA",
    "EXPECTED_SCHEMA",
    "REQUIRED_CATEGORIES",
    "Case",
    "CaseFile",
    "CaseReport",
    "CaseRun",
    "NegativeControl",
    "SliceAbsenceError",
    "case_outcome",
    "check_case",
    "load_case_file",
    "run_case",
    "run_negative_control",
]

CASES_SCHEMA: Literal["canary-investigation-cases/1"] = "canary-investigation-cases/1"
EXPECTED_SCHEMA: Literal["canary-investigation-cases-expected/1"] = (
    "canary-investigation-cases-expected/1"
)

Category = Literal[
    "absent_channel", "misleading_correlation", "calibration_change", "recurrence", "retraction"
]
#: Every category E01.6 names; each must be covered by a real (non-synthetic) case.
REQUIRED_CATEGORIES: tuple[str, ...] = (
    "absent_channel",
    "misleading_correlation",
    "calibration_change",
    "recurrence",
    "retraction",
)

#: Paths a known check may read: tool output only, never the case's own prose.
_OUTPUT_ROOTS = ("evidence.", "cards.", "queries.", "references.", "interventions.")

#: Relative tolerance between a run and the generated snapshot.
SNAPSHOT_REL_TOL = 1e-5


class SliceAbsenceError(ValueError):
    """A channel absent from a fixture slice was not shown absent from the
    full session, so "never captured" would be a statement about the slice."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# -- the case file ---------------------------------------------------------------


class FullSession(_Strict):
    """Facts counted over every chunk of the original session (not the slice).

    ``channel_rows`` maps a channel spec to its row count across the whole
    session; zero is the only evidence that a channel was never captured.
    """

    checked: date
    chunks: int = Field(ge=1)
    first_sample: datetime
    last_sample: datetime
    capture_profile: str | None
    mode01_enabled: bool | None
    #: Mode 01 rows (``source='obd'`` that are neither DIDs nor memory reads).
    mode01_rows: int = Field(ge=0)
    channel_rows: dict[str, int]


class KnownCalibration(_Strict):
    revision: str | None
    basis: str = Field(min_length=1)


class Drive(_Strict):
    """One recorded drive and the committed slice of it a case reads."""

    session: str = Field(min_length=1)
    fixture: str = Field(min_length=1)
    window: str = Field(min_length=1)
    full_session: FullSession
    calibration: KnownCalibration


class Incident(_Strict):
    """Something that happened once in the project's history.

    No disposition field exists: what an incident *means* is established only
    by a case's checks over tool output. An incident with no case says why in
    ``gaps`` (inputs lost, never stored, or not a session at all).
    """

    title: str = Field(min_length=1)
    date: date
    narrative: str = Field(min_length=1)
    links: tuple[str, ...] = Field(min_length=1)
    #: Other committed checks that already reproduce it (test node ids).
    reproducible_evidence: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()


class Defect(_Strict):
    summary: str = Field(min_length=1)
    evidence: tuple[str, ...] = Field(min_length=1)
    remedy: str = Field(min_length=1)


class MintStep(_Strict):
    step: Literal["mint"]
    name: str
    drive: str
    a: str
    b: str
    tolerance: str
    #: ``current`` (the tracked catalogs) or a bindings file under the fixture root.
    catalog: str = "current"
    incident: str | None = None


class InterventionStep(_Strict):
    step: Literal["intervention"]
    name: str
    revision: str
    affects: tuple[str, ...] = Field(min_length=1)


class ReferenceStep(_Strict):
    step: Literal["reference"]
    name: str
    reference: str
    channels: tuple[str, ...] = Field(min_length=1)
    source_ref: str
    evidence: tuple[str, ...] = Field(min_length=1)
    investigation: str | None = None


class HypothesisSpec(_Strict):
    label: str
    status: Literal["open", "supported", "refuted", "unresolvable"]
    supporting: tuple[str, ...] = ()
    refuting: tuple[str, ...] = ()


class InvestigationStep(_Strict):
    step: Literal["investigation"]
    name: str
    title: str
    question: str
    status: Literal["open", "resolved", "abandoned"]
    incident: str | None = None
    observations: tuple[str, ...] = ()
    hypotheses: tuple[HypothesisSpec, ...] = ()
    unknowns: tuple[str, ...] = ()
    next_measurements: tuple[str, ...] = ()
    narrative: str = ""
    supersedes: str | None = None
    supersession_reason: str | None = None


class RetractStep(_Strict):
    step: Literal["retract"]
    name: str
    retracts: tuple[str, ...] = Field(min_length=1)
    reason: str = Field(min_length=1)


Step = Annotated[
    MintStep | InterventionStep | ReferenceStep | InvestigationStep | RetractStep,
    Field(discriminator="step"),
]


class CardSpec(_Strict):
    name: str
    investigation: str
    #: Render as the ledger stood when this step's record was written.
    as_of: str | None = None


class ReferenceView(_Strict):
    name: str
    reference: str
    as_of: str | None = None


class QuerySpec(_Strict):
    name: str
    kinds: tuple[str, ...] = ("evidence",)
    incident: str | None = None
    #: A revision name; ``unknown`` asks for records whose calibration is unknown.
    calibration: str | None = None
    #: Step names of interventions whose effect time bounds valid time.
    valid_before: str | None = None
    valid_after: str | None = None


class Check(_Strict):
    path: str
    op: Literal["eq", "lt", "gt", "between", "contains", "not_contains"]
    value: Any
    #: How the answer is known: a commit, document, tracked file or measurement.
    source: str = Field(min_length=1)
    #: A defect id: the chain does not reproduce this yet, and must still fail.
    known_defect: str | None = None

    @model_validator(mode="after")
    def _tool_output_only(self) -> Check:
        if not self.path.startswith(_OUTPUT_ROOTS):
            raise ValueError(
                f"check {self.path!r}: a known answer is checked against tool output "
                f"({', '.join(_OUTPUT_ROOTS)}), never against the case's own prose"
            )
        if self.op == "between" and (not isinstance(self.value, list) or len(self.value) != 2):
            raise ValueError(f"check {self.path!r}: between takes [low, high]")
        return self


class Case(_Strict):
    title: str = Field(min_length=1)
    incident: str
    drives: tuple[str, ...] = Field(min_length=1)
    categories: tuple[Category, ...] = Field(min_length=1)
    synthetic: bool
    steps: tuple[Step, ...] = Field(min_length=1)
    cards: tuple[CardSpec, ...] = ()
    references: tuple[ReferenceView, ...] = ()
    queries: tuple[QuerySpec, ...] = ()
    known: tuple[Check, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_resolve(self) -> Case:
        seen: dict[str, str] = {}
        for step in self.steps:
            if step.name in seen:
                raise ValueError(f"step name {step.name!r} is used twice")
            for ref in _step_refs(step):
                if ref not in seen:
                    raise ValueError(
                        f"step {step.name!r} cites {ref!r}, which no earlier step defines"
                    )
            seen[step.name] = step.step
        for step in self.steps:
            if isinstance(step, MintStep) and step.drive not in self.drives:
                raise ValueError(f"step {step.name!r} reads drive {step.drive!r} not in drives")
        for card in self.cards:
            if seen.get(card.investigation) != "investigation":
                raise ValueError(f"card {card.name!r}: {card.investigation!r} is no investigation")
            if card.as_of is not None and card.as_of not in seen:
                raise ValueError(f"card {card.name!r}: as_of {card.as_of!r} is no step")
        for view in self.references:
            if seen.get(view.reference) != "reference":
                raise ValueError(f"reference view {view.name!r}: {view.reference!r} is none")
        for query in self.queries:
            for bound in (query.valid_before, query.valid_after):
                if bound is not None and seen.get(bound) != "intervention":
                    raise ValueError(f"query {query.name!r}: {bound!r} is no intervention")
        return self


class BindingScale(_Strict):
    kind: Literal["binding_scale"]
    signal: str
    scale: float


class DropRevision(_Strict):
    kind: Literal["drop_revision"]
    revision: str


class RedirectDrive(_Strict):
    kind: Literal["redirect_drive"]
    #: Every mint step reading ``from_drive`` reads ``to_drive`` instead.
    from_drive: str
    to_drive: str


Mutation = Annotated[BindingScale | DropRevision | RedirectDrive, Field(discriminator="kind")]


class NegativeControl(_Strict):
    """A real case with one input mutated: the checks must catch it."""

    case: str
    synthetic: Literal[True]
    why: str = Field(min_length=1)
    mutation: Mutation
    #: Check paths that must fail; or ``raises`` names the refusal expected.
    must_fail: tuple[str, ...] = ()
    raises: Literal["SliceAbsenceError"] | None = None

    @model_validator(mode="after")
    def _expects_something(self) -> NegativeControl:
        if not self.must_fail and self.raises is None:
            raise ValueError("a negative control names the checks that must fail, or the refusal")
        return self


class CaseFile(_Strict):
    schema_: Literal["canary-investigation-cases/1"] = Field(alias="schema")
    vehicle: str = Field(min_length=1)
    tune_revisions: str = Field(min_length=1)
    drives: dict[str, Drive]
    incidents: dict[str, Incident]
    defects: dict[str, Defect] = Field(default_factory=dict)
    cases: dict[str, Case]
    negative_controls: dict[str, NegativeControl] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _separated(self) -> CaseFile:
        sessions: dict[str, str] = {}
        for name, drive in self.drives.items():
            if drive.session in sessions:
                raise ValueError(f"drives {sessions[drive.session]!r} and {name!r} are one session")
            sessions[drive.session] = name
        by_incident: dict[str, str] = {}
        for name, case in self.cases.items():
            if case.incident not in self.incidents:
                raise ValueError(f"case {name!r}: unknown incident {case.incident!r}")
            if case.incident in by_incident:
                raise ValueError(
                    f"cases {by_incident[case.incident]!r} and {name!r} both evaluate incident "
                    f"{case.incident!r}: one event is one case, never two confirmations"
                )
            by_incident[case.incident] = name
            for drive_id in case.drives:
                if drive_id not in self.drives:
                    raise ValueError(f"case {name!r}: unknown drive {drive_id!r}")
            for check in case.known:
                if check.known_defect is not None and check.known_defect not in self.defects:
                    raise ValueError(f"case {name!r}: unknown defect {check.known_defect!r}")
            _recurrences_span_drives(name, case)
            for step in case.steps:
                cited = step.incident if isinstance(step, MintStep | InvestigationStep) else None
                if cited is not None and cited not in self.incidents:
                    raise ValueError(
                        f"case {name!r} step {step.name!r} names incident {cited!r}, which is "
                        "not declared: declare incidents (narrative, links, gaps) before cases"
                    )
        for name, incident in self.incidents.items():
            if name not in by_incident and not incident.gaps:
                raise ValueError(
                    f"incident {name!r} has no case and names no gap: say why it is narrative only"
                )
        covered = {c for case in self.cases.values() if not case.synthetic for c in case.categories}
        missing = [c for c in REQUIRED_CATEGORIES if c not in covered]
        if missing:
            raise ValueError(f"no real case covers {missing}; synthetic cases do not count")
        for name, control in self.negative_controls.items():
            if control.case not in self.cases:
                raise ValueError(f"negative control {name!r}: unknown case {control.case!r}")
            mutation = control.mutation
            if isinstance(mutation, RedirectDrive) and mutation.to_drive not in self.drives:
                raise ValueError(f"negative control {name!r}: unknown drive {mutation.to_drive!r}")
        return self


def _step_refs(step: Any) -> tuple[str, ...]:
    if isinstance(step, ReferenceStep):
        return step.evidence + ((step.investigation,) if step.investigation else ())
    if isinstance(step, InvestigationStep):
        refs = list(step.observations)
        for hypothesis in step.hypotheses:
            refs.extend(hypothesis.supporting)
            refs.extend(hypothesis.refuting)
        if step.supersedes:
            refs.append(step.supersedes)
        return tuple(refs)
    if isinstance(step, RetractStep):
        return step.retracts
    return ()


def _recurrences_span_drives(name: str, case: Case) -> None:
    drives_by_incident: dict[str, list[str]] = {}
    for step in case.steps:
        if isinstance(step, MintStep) and step.incident is not None:
            drives_by_incident.setdefault(step.incident, []).append(step.drive)
    for incident, drives in drives_by_incident.items():
        if len(drives) != len(set(drives)):
            raise ValueError(
                f"case {name!r}: occurrences of {incident!r} repeat a drive; a recurrence "
                "is the same thing on another drive"
            )


def load_case_file(path: Path) -> CaseFile:
    return CaseFile.model_validate(yaml.safe_load(path.read_text()))


# -- running a case -------------------------------------------------------------


@dataclass
class CaseRun:
    """One case replayed into a fresh ledger."""

    case_id: str
    case: Case
    cases: CaseFile
    ledger: MemoryLedger
    #: Step name -> stored record.
    records: dict[str, AnyRecord] = field(default_factory=dict)
    #: Step name -> the drive a mint step read (after any mutation).
    drives: dict[str, str] = field(default_factory=dict)

    def name_of(self, record_id: str) -> str:
        for name, record in self.records.items():
            if record.id == record_id:
                return name
        return record_id


def _operator(case_id: str) -> Provenance:
    return Provenance(producer=f"known-case:{case_id}", producer_kind="operator", version="1")


def _common_calibration(records: list[AnyRecord]) -> str | None:
    calibrations = {record.vehicle.calibration for record in records}
    return calibrations.pop() if len(calibrations) == 1 else None


def _mutated_bindings(workdir: Path, mutation: BindingScale) -> Path:
    raw = yaml.safe_load(BINDINGS_YAML.read_text())
    signals = [s for s in raw["signals"] if s["name"] == mutation.signal]
    if len(signals) != 1:
        raise ValueError(f"binding {mutation.signal!r} is not in {BINDINGS_YAML}")
    signals[0]["scale"] = mutation.scale
    path = workdir / "bindings-mutated.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def _materialise(workdir: Path, fixture_root: Path, drive: Drive) -> Path:
    """The slice under its real session name, so the subject, the evidence's
    source and the calibration resolved from the name are the drive's own."""
    root = workdir / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    session = root / drive.session
    if not session.exists():
        session.symlink_to((fixture_root / drive.fixture).resolve(), target_is_directory=True)
    return session


def _check_absence(drive_id: str, drive: Drive, step: MintStep, record: Evidence) -> None:
    if record.result.reason != "missing_input":
        return
    named = [spec for spec in (step.a, step.b) if spec in record.result.detail]
    if not named:
        raise SliceAbsenceError(
            f"{step.name}: the refusal names neither {step.a} nor {step.b}: {record.result.detail}"
        )
    for spec in named:
        rows = drive.full_session.channel_rows.get(spec)
        if rows is None:
            raise SliceAbsenceError(
                f"{step.name}: {spec} is absent from the {drive.fixture} slice, and drive "
                f"{drive_id}'s full_session facts do not count it; a slice keeps only what a "
                "check needs, so record the full session's row count first"
            )
        if rows > 0:
            raise SliceAbsenceError(
                f"{step.name}: {spec} is absent from the {drive.fixture} slice but the full "
                f"session {drive.session} holds {rows} rows of it; the slice dropped it, the "
                "drive did not lack it"
            )


class _Clock:
    """Strictly increasing record times for the case's own appends (the
    ledger refuses a ``now`` before its newest record)."""

    def __init__(self) -> None:
        self.last = datetime.min.replace(tzinfo=UTC)

    def seen(self, record: AnyRecord) -> None:
        if record.recorded_at is not None and record.recorded_at > self.last:
            self.last = record.recorded_at

    def now(self) -> datetime:
        stamp = max(datetime.now(UTC), self.last + timedelta(milliseconds=1))
        self.last = stamp
        return stamp


def run_case(
    cases: CaseFile,
    case_id: str,
    *,
    workdir: Path,
    fixture_root: Path,
    mutation: BindingScale | DropRevision | RedirectDrive | None = None,
) -> CaseRun:
    """Replay ``case_id`` into a fresh ledger under ``workdir``."""
    case = cases.cases[case_id]
    workdir.mkdir(parents=True, exist_ok=True)
    ledger = MemoryLedger(workdir / "memory.jsonl", vehicle=cases.vehicle)
    registry = load_tune_revisions(REPO_ROOT / cases.tune_revisions)
    if isinstance(mutation, DropRevision):
        registry = _drop_revision(registry, mutation.revision)
    current_bindings = (
        _mutated_bindings(workdir, mutation)
        if isinstance(mutation, BindingScale)
        else BINDINGS_YAML
    )
    run = CaseRun(case_id=case_id, case=case, cases=cases, ledger=ledger)
    clock = _Clock()
    vehicle = VehicleIdentity(profile=cases.vehicle, vin=None, program_id=None, calibration=None)
    conditions = Conditions(regime=ALL_CONDITIONS, vehicle_state=None)
    for step in case.steps:
        stored: AnyRecord
        if isinstance(step, MintStep):
            drive_id = step.drive
            if isinstance(mutation, RedirectDrive) and drive_id == mutation.from_drive:
                drive_id = mutation.to_drive
            drive = cases.drives[drive_id]
            session = _materialise(workdir, fixture_root, drive)
            bindings = (
                current_bindings if step.catalog == "current" else fixture_root / step.catalog
            )
            evidence_id = mint_channel_comparison(
                ledger,
                session,
                step.a,
                step.b,
                policy=PairingPolicy.within(step.tolerance),
                vin=None,
                program_id=None,
                tune_registry=registry,
                bindings_path=bindings,
                dids_path=DIDS_YAML,
                incident=step.incident,
            )
            stored = ledger.get(evidence_id)
            assert isinstance(stored, Evidence)
            _check_absence(drive_id, drive, step, stored)
            run.drives[step.name] = drive_id
        elif isinstance(step, InterventionStep):
            history = {item.source_ref: item for item in intervention_history(registry, [])}
            if step.revision not in history:
                raise KeyError(
                    f"{step.name}: revision {step.revision!r} is not in the flash history"
                )
            stored = record_intervention(
                ledger,
                history[step.revision],
                vehicle=vehicle,
                conditions=conditions,
                provenance=_operator(case_id),
                affects=step.affects,
                now=clock.now(),
            )
        elif isinstance(step, ReferenceStep):
            cited = [run.records[name] for name in step.evidence]
            stored = ledger.append(
                Reference(
                    vehicle=vehicle.model_copy(update={"calibration": _common_calibration(cited)}),
                    provenance=_operator(case_id),
                    conditions=conditions,
                    valid_from=min(record.valid_from for record in cited),
                    reference_kind="evidence",
                    name=step.reference,
                    channels=step.channels,
                    source="memory",
                    source_ref=step.source_ref,
                    investigation=None
                    if step.investigation is None
                    else run.records[step.investigation].id,
                    evidence=tuple(record.id for record in cited),
                ),
                now=clock.now(),
            )
        elif isinstance(step, InvestigationStep):
            cited = [run.records[name] for name in _step_refs(step) if name != step.supersedes]
            stored = ledger.append(
                Investigation(
                    vehicle=vehicle.model_copy(update={"calibration": _common_calibration(cited)}),
                    provenance=_operator(case_id),
                    conditions=conditions,
                    valid_from=min(record.valid_from for record in cited),
                    title=step.title,
                    question=step.question,
                    status=step.status,
                    incident=step.incident,
                    observations=tuple(run.records[n].id for n in step.observations),
                    hypotheses=tuple(
                        Hypothesis(
                            label=h.label,
                            status=h.status,
                            supporting=tuple(run.records[n].id for n in h.supporting),
                            refuting=tuple(run.records[n].id for n in h.refuting),
                        )
                        for h in step.hypotheses
                    ),
                    unknowns=step.unknowns,
                    next_measurements=step.next_measurements,
                    narrative=step.narrative,
                    supersedes=()
                    if step.supersedes is None
                    else (run.records[step.supersedes].id,),
                    supersession_reason=step.supersession_reason,
                ),
                now=clock.now(),
            )
        else:
            retracted = [run.records[name] for name in step.retracts]
            stored = ledger.append(
                Retraction(
                    vehicle=vehicle,
                    provenance=_operator(case_id),
                    valid_from=min(record.valid_from for record in retracted),
                    retracts=tuple(record.id for record in retracted),
                    reason=step.reason,
                ),
                now=clock.now(),
            )
        clock.seen(stored)
        run.records[step.name] = stored
    return run


def _drop_revision(registry: TuneRevisionRegistry | None, revision: str) -> TuneRevisionRegistry:
    if registry is None or revision not in {r.revision for r in registry.revisions}:
        raise ValueError(f"revision {revision!r} is not in the flash history to drop")
    return registry.model_copy(
        update={"revisions": [r for r in registry.revisions if r.revision != revision]}
    )


# -- what the run says ----------------------------------------------------------


def _number(value: float) -> float:
    return float(f"{value:.6g}")


def _localise(run: CaseRun, value: Any) -> Any:
    """``value`` with every minted id replaced by its step name."""
    if isinstance(value, str):
        return re.sub(
            r"\b(?:ev|inv|pred|intv|out|ret|ref)-[0-9a-f]{24}\b",
            lambda m: run.name_of(m.group(0)),
            value,
        )
    if isinstance(value, list | tuple):
        return [_localise(run, item) for item in value]
    if isinstance(value, dict):
        return {_localise(run, key): _localise(run, item) for key, item in value.items()}
    return value


def _evidence_outcome(run: CaseRun, name: str, record: Evidence) -> dict[str, Any]:
    entry = run.ledger.entry(record.id)
    standing = (
        "retracted"
        if entry.retracted_by is not None
        else ("current" if entry.current else "superseded")
    )
    (source,) = record.sources
    return {
        "drive": run.drives[name],
        "session": source.session,
        "channels": list(source.channels),
        "status": record.result.status,
        "reason": record.result.reason,
        "detail": record.result.detail,
        "calibration": record.vehicle.calibration,
        "n_observations": record.quality.n_observations,
        "labels": dict(sorted(record.labels.items())),
        "values": {key: _number(q.value) for key, q in sorted(record.values.items())},
        "occurrences": len(run.ledger.recurrences(record.id)),
        "standing": standing,
    }


def _card_outcome(run: CaseRun, card: InvestigationCard) -> dict[str, Any]:
    views = {view.id: view for view in card.what_happened}
    for hypothesis in card.hypotheses:
        for view in (*hypothesis.supporting, *hypothesis.refuting):
            views.setdefault(view.id, view)
    localised: dict[str, Any] = _localise(
        run,
        {
            "standing": card.standing,
            "superseded_by": list(card.superseded_by),
            "evidence_state": card.evidence_state,
            "calibration": card.calibration,
            "what_happened": [view.id for view in card.what_happened],
            "hypotheses": [
                {
                    "label": h.label,
                    "status": h.status,
                    "supporting": [v.id for v in h.supporting],
                    "refuting": [v.id for v in h.refuting],
                    "conflicting": h.conflicting,
                }
                for h in card.hypotheses
            ],
            "evidence": {
                view.id: {
                    "status": view.status,
                    "reason": view.reason,
                    "standing": view.standing,
                    "occurrences": view.occurrences,
                }
                for view in views.values()
            },
            "conflicts": len(card.conflicts),
            "retractions": list(card.retractions),
            "unknowns": list(card.unknowns),
            "next_measurements": list(card.next_measurements),
        },
    )
    return localised


def case_outcome(run: CaseRun) -> dict[str, Any]:
    """What the run says, keyed by step names (deterministic across runs)."""
    outcome: dict[str, Any] = {
        "evidence": {},
        "interventions": {},
        "references": {},
        "cards": {},
        "queries": {},
    }
    for name, record in run.records.items():
        if isinstance(record, Evidence):
            outcome["evidence"][name] = _evidence_outcome(run, name, record)
        elif isinstance(record, Intervention):
            outcome["interventions"][name] = {
                "valid_from": record.valid_from.isoformat(),
                "effective_precision": record.effective_precision,
                "calibration": record.vehicle.calibration,
                "image_sha256": record.image_sha256,
                "affects": list(record.affects),
            }
    for view in run.case.references:
        record = run.records[view.reference]
        as_of = None if view.as_of is None else run.records[view.as_of].recorded_at
        entry = run.ledger.entry(record.id, as_of=as_of)
        outcome["references"][view.name] = {
            "standing": "retracted"
            if entry.retracted_by is not None
            else ("current" if entry.current else "superseded"),
            "retracted_by": None if entry.retracted_by is None else run.name_of(entry.retracted_by),
            "retired_by": None if entry.retired_by is None else run.name_of(entry.retired_by),
        }
    for spec in run.case.cards:
        as_of = None if spec.as_of is None else run.records[spec.as_of].recorded_at
        card = investigation_card(run.ledger, run.records[spec.investigation].id, as_of=as_of)
        outcome["cards"][spec.name] = _card_outcome(run, card)
    for query in run.case.queries:
        filters: dict[str, Any] = {"kinds": query.kinds}
        if query.incident is not None:
            filters["incident"] = query.incident
        if query.calibration is not None:
            filters["calibration"] = None if query.calibration == "unknown" else query.calibration
        if query.valid_before is not None:
            filters["valid_before"] = run.records[query.valid_before].valid_from
        if query.valid_after is not None:
            filters["valid_after"] = run.records[query.valid_after].valid_from
        found = run.ledger.query(MemoryQuery(**filters))
        outcome["queries"][query.name] = [run.name_of(e.record.id) for e in found]
    return outcome


# -- checking -------------------------------------------------------------------


_MISSING = object()


def _resolve(outcome: dict[str, Any], path: str) -> Any:
    node: Any = outcome
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def _holds(check: Check, actual: Any) -> bool:
    if actual is _MISSING:
        return False
    if check.op == "eq":
        if isinstance(actual, float) and isinstance(check.value, int | float):
            return math.isclose(actual, float(check.value), rel_tol=1e-9)
        return bool(actual == check.value)
    if check.op in ("contains", "not_contains"):
        if isinstance(actual, str):
            found = str(check.value) in actual
        elif isinstance(actual, list):
            found = any(
                str(check.value) in item if isinstance(item, str) else item == check.value
                for item in actual
            )
        else:
            return False
        return found if check.op == "contains" else not found
    if not isinstance(actual, int | float) or isinstance(actual, bool):
        return False
    if check.op == "lt":
        return bool(actual < check.value)
    if check.op == "gt":
        return bool(actual > check.value)
    low, high = check.value
    return bool(low <= actual <= high)


def _drift(expected: Any, actual: Any, path: str) -> Iterator[str]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            where = f"{path}.{key}" if path else str(key)
            if key not in actual:
                yield f"{where}: missing from the run"
            elif key not in expected:
                yield f"{where}: not in the generated snapshot"
            else:
                yield from _drift(expected[key], actual[key], where)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            yield f"{path}: {len(actual)} items, snapshot has {len(expected)}"
            return
        for index, (e, a) in enumerate(zip(expected, actual, strict=True)):
            yield from _drift(e, a, f"{path}.{index}")
        return
    if (
        isinstance(expected, int | float)
        and isinstance(actual, int | float)
        and not isinstance(expected, bool)
        and not isinstance(actual, bool)
    ):
        if not math.isclose(expected, actual, rel_tol=SNAPSHOT_REL_TOL, abs_tol=1e-9):
            yield f"{path}: {actual!r}, snapshot {expected!r}"
        return
    if expected != actual:
        yield f"{path}: {actual!r}, snapshot {expected!r}"


@dataclass(frozen=True)
class CaseReport:
    """Known checks that failed, known defects still failing / now passing,
    and drift from the generated snapshot (``None`` when not compared)."""

    failures: tuple[str, ...]
    defects_failing: tuple[str, ...]
    defects_passing: tuple[str, ...]
    drift: tuple[str, ...] | None

    @property
    def reproduced(self) -> bool:
        return not self.failures and not self.defects_passing and not self.drift


def check_case(
    case: Case, outcome: dict[str, Any], expected: dict[str, Any] | None = None
) -> CaseReport:
    """Hold ``outcome`` against the case's known answers (and the snapshot)."""
    failures: list[str] = []
    defects_failing: list[str] = []
    defects_passing: list[str] = []
    for check in case.known:
        actual = _resolve(outcome, check.path)
        shown = "missing" if actual is _MISSING else repr(actual)
        line = f"{check.path} {check.op} {check.value!r}: got {shown}"
        held = _holds(check, actual)
        if check.known_defect is not None:
            (defects_passing if held else defects_failing).append(f"{line} [{check.known_defect}]")
        elif not held:
            failures.append(line)
    drift = None if expected is None else tuple(_drift(expected, outcome, ""))
    return CaseReport(
        failures=tuple(failures),
        defects_failing=tuple(defects_failing),
        defects_passing=tuple(defects_passing),
        drift=drift,
    )


def run_negative_control(
    cases: CaseFile, control_id: str, *, workdir: Path, fixture_root: Path
) -> CaseReport:
    """Run the control's case with its mutation; the report's ``failures``
    must include every ``must_fail`` path (raises propagate)."""
    control = cases.negative_controls[control_id]
    run = run_case(
        cases,
        control.case,
        workdir=workdir,
        fixture_root=fixture_root,
        mutation=control.mutation,
    )
    return check_case(cases.cases[control.case], case_outcome(run))
