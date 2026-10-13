"""Track a finite hypothesis set as evidence arrives, and rank what to measure next (E03.4).

The hypotheses are the vehicle's fault model's (``fault_hypotheses.yaml``,
:mod:`src.analysis.fault_model`); the observations are engine-response
evidence records (:func:`src.analysis.delivery_investigation.observation_of`).
:func:`track_hypotheses` folds any set of evidence ids into, per hypothesis,
the ids that support and refute it and the drives they came from, and ranks
the hypotheses. What it makes impossible rather than documented:

* **Repeated evidence counts once.** The same id twice is one; a superseded
  record counts as the record that replaced it (a recompute is one
  occurrence); two records with the same content are one; and records of the
  same feature from the same drive are one occurrence of it. A drive is
  every session its records share, transitively (a record pooling a drive
  with another session is still that drive). Each skipped id is listed with why.
* **Conflict is recorded, not resolved by order.** Records of one feature on
  one drive that disagree in sign are a data conflict: excluded from the
  update and named. Across drives, signs that no single hypothesis allows
  each refute what they refute, and the ranking — by refuting drives, then
  supporting drives, then the model's order — changes the same way whatever
  order the evidence arrived in.

:func:`rank_measurements` extends ``select_measurement`` (structural: what
could separate the open pairs, actionable first) with expected information
gain divided by the plan's declared cost — **only** when every open
hypothesis has a likelihood whose cited evidence resolves on the ledger
(:func:`~src.analysis.fault_model.score`) and every open hypothesis's outcome
on the measured features is determinate. Otherwise the structural order
stands and the ranking names each missing likelihood or outcome probability,
rather than inventing one. The likelihoods are read as relative weights of
the open hypotheses (normalised over them).

:func:`update_investigation` records the result: the investigation is
superseded by one whose hypotheses are in ranked order with their evidence
ids and whose next measurements are in ranked order. Updating again with
evidence already counted returns the same record.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import src.analysis.fault_model as fault_model_module
from src.analysis.delivery_investigation import (
    MeasurementChoice,
    UnresolvedPair,
    drive_reports,
    observation_of,
    select_measurement,
    unresolved_pairs,
)
from src.analysis.fault_model import (
    SIGN_SETS,
    FaultModel,
    Feature,
    Observation,
    score,
)
from src.analysis.measurement_plan import MeasurementPlan
from src.evidence.ledger import MemoryLedger
from src.evidence.provenance import code_version
from src.evidence.records import Evidence, Hypothesis, Investigation, Provenance, content_key

__all__ = [
    "TRACKING_TOOL",
    "HypothesisTrack",
    "InvestigationRanking",
    "MeasurementRanking",
    "RankedMeasurement",
    "TrackedHypothesis",
    "TrackedObservation",
    "rank_investigation",
    "rank_measurements",
    "track_hypotheses",
    "update_investigation",
]

TRACKING_TOOL = "hypothesis_tracking"
TRACKING_TOOL_VERSION = "1"


@dataclass(frozen=True)
class TrackedObservation:
    """One feature on one drive: a sign, a named unevaluable, or a conflict."""

    feature: Feature
    #: The drive's sessions (sorted, comma-joined): the occurrence key.
    drive: str
    sign: str | None
    #: Every counted evidence id stating it (one occurrence however many).
    evidence: tuple[str, ...]
    unevaluable: str | None = None
    conflict: bool = False


@dataclass(frozen=True)
class TrackedHypothesis:
    name: str
    label: str
    status: Literal["open", "refuted"]
    supporting: tuple[str, ...]
    refuting: tuple[str, ...]
    supporting_drives: int
    refuting_drives: int


@dataclass(frozen=True)
class HypothesisTrack:
    #: Modelled hypotheses, ranked.
    hypotheses: tuple[TrackedHypothesis, ...]
    observations: tuple[TrackedObservation, ...]
    #: Counted evidence ids, oldest first.
    counted: tuple[str, ...]
    #: Ids not counted, and why (duplicate, superseded, same content, retracted).
    skipped: dict[str, str] = field(default_factory=dict)
    unscored: tuple[str, ...] = ()

    @property
    def ranking(self) -> tuple[str, ...]:
        return tuple(h.name for h in self.hypotheses)

    @property
    def open(self) -> tuple[str, ...]:
        return tuple(h.name for h in self.hypotheses if h.status == "open")

    @property
    def conflicts(self) -> tuple[TrackedObservation, ...]:
        return tuple(o for o in self.observations if o.conflict)

    def of(self, name: str) -> TrackedHypothesis:
        return next(h for h in self.hypotheses if h.name == name)

    def signed(self) -> list[Observation]:
        """One fault-model observation per counted, unconflicted occurrence."""
        return [
            Observation(o.feature, o.sign, evidence=o.evidence[0])  # type: ignore[arg-type]
            for o in self.observations
            if o.sign is not None and not o.conflict
        ]


def _current(ledger: MemoryLedger, evidence_id: str) -> tuple[str | None, str | None]:
    """The current record standing for ``evidence_id`` and, if not itself, why."""
    seen = {evidence_id}
    entry = ledger.resolve_evidence(evidence_id)
    if entry.retracted_by is not None:
        return None, f"retracted by {entry.retracted_by}"
    rid = evidence_id
    while not entry.current and entry.superseded_by:
        rid = entry.superseded_by[-1]
        if rid in seen:
            break
        seen.add(rid)
        entry = ledger.resolve_evidence(rid)
        if entry.retracted_by is not None:
            return None, f"superseded by {rid}, then retracted"
    return rid, None if rid == evidence_id else f"superseded by {rid}"


def _drive_components(sessions: dict[str, set[str]]) -> dict[str, str]:
    """Each record's drive: records sharing any session are one drive
    (union-find over sessions, transitively), keyed by the sorted union."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for names in sessions.values():
        first, *rest = sorted(names)
        for other in rest:
            parent[find(other)] = find(first)
    members: dict[str, set[str]] = {}
    for names in sessions.values():
        for name in names:
            members.setdefault(find(name), set()).add(name)
    key = {root: ",".join(sorted(names)) for root, names in members.items()}
    return {rid: key[find(min(names))] for rid, names in sessions.items()}


def track_hypotheses(
    model: FaultModel, ledger: MemoryLedger, evidence_ids: Iterable[str]
) -> HypothesisTrack:
    """The hypothesis set after ``evidence_ids`` (order does not matter)."""
    skipped: dict[str, str] = {}
    canonical: dict[str, Evidence] = {}
    keys: dict[str, str] = {}
    for evidence_id in dict.fromkeys(evidence_ids):
        current, why = _current(ledger, evidence_id)
        if current is None:
            skipped[evidence_id] = why or "not current"
            continue
        if why is not None:
            skipped[evidence_id] = why
        if current in canonical:
            continue
        record = ledger.resolve_evidence(current).record
        assert isinstance(record, Evidence)
        key = content_key(record)
        if key in keys:
            skipped[current] = f"same content as {keys[key]}"
            continue
        keys[key] = current
        canonical[current] = record
    ordered = sorted(canonical, key=lambda rid: (canonical[rid].valid_from, rid))

    drives = _drive_components(
        {rid: {s.session for s in canonical[rid].sources} for rid in ordered}
    )
    groups: dict[tuple[Feature, str], list[tuple[str, Observation]]] = {}
    for rid in ordered:
        observation = observation_of(ledger, rid)
        groups.setdefault((observation.feature, drives[rid]), []).append((rid, observation))
    tracked: list[TrackedObservation] = []
    for (feature, drive), items in groups.items():
        signs = {o.sign for _rid, o in items if o.sign is not None}
        ids = tuple(rid for rid, _o in items)
        for rid in ids[1:]:
            skipped.setdefault(rid, f"same occurrence as {ids[0]} ({feature} on one drive)")
        if len(signs) > 1:
            tracked.append(TrackedObservation(feature, drive, None, ids, conflict=True))
        elif signs:
            tracked.append(TrackedObservation(feature, drive, signs.pop(), ids))
        else:
            tracked.append(
                TrackedObservation(feature, drive, None, ids, unevaluable=items[0][1].unevaluable)
            )

    hypotheses: list[tuple[tuple[int, int, int], TrackedHypothesis]] = []
    for index, hypothesis in enumerate(model.modelled):
        supporting: list[str] = []
        refuting: list[str] = []
        for_drives: set[str] = set()
        against_drives: set[str] = set()
        for o in tracked:
            if o.sign is None or o.conflict:
                continue
            allowed = hypothesis.allowed(o.feature.residual, o.feature.region)
            if o.sign not in allowed:
                refuting.extend(o.evidence)
                against_drives.add(o.drive)
            elif allowed != SIGN_SETS["any"]:
                supporting.extend(o.evidence)
                for_drives.add(o.drive)
        item = TrackedHypothesis(
            hypothesis.name,
            hypothesis.label,
            "refuted" if refuting else "open",
            tuple(dict.fromkeys(supporting)),
            tuple(dict.fromkeys(refuting)),
            len(for_drives),
            len(against_drives),
        )
        hypotheses.append(((len(against_drives), -len(for_drives), index), item))
    hypotheses.sort(key=lambda pair: pair[0])
    return HypothesisTrack(
        hypotheses=tuple(item for _key, item in hypotheses),
        observations=tuple(tracked),
        counted=tuple(ordered),
        skipped=skipped,
        unscored=tuple(h.name for h in model.hypotheses if h.origin != "modelled"),
    )


# ---------------------------------------------------------------------------
# Measurements


@dataclass(frozen=True)
class RankedMeasurement:
    measurement: str
    features: tuple[Feature, ...]
    actionable: bool
    separates: tuple[tuple[str, str], ...]
    #: Declared cost in the plan's unit (the costliest residual it needs).
    cost: float
    #: Expected information gain in bits, when every input is evidence-backed.
    information_bits: float | None
    bits_per_cost: float | None
    #: What a probability would need that is not there.
    missing: tuple[str, ...]


@dataclass(frozen=True)
class MeasurementRanking:
    method: Literal["information_gain_per_cost", "structural"]
    cost_unit: str
    measurements: tuple[RankedMeasurement, ...]
    #: Likelihoods the open hypotheses lack (empty when all are backed).
    missing_likelihoods: tuple[str, ...]

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(m.measurement for m in self.measurements)


def _entropy(weights: Sequence[float]) -> float:
    total = sum(weights)
    return -sum((w / total) * math.log2(w / total) for w in weights if w > 0)


def rank_measurements(
    model: FaultModel,
    plan: MeasurementPlan,
    ledger: MemoryLedger,
    open_hypotheses: Sequence[str],
    choices: Sequence[MeasurementChoice],
) -> MeasurementRanking:
    """``choices`` (``select_measurement``'s order) ranked by information gain
    per cost where every input is evidence-backed, else structurally."""
    scores = score(model, ledger, open_hypotheses)
    missing_likelihoods = tuple(f"{r.hypothesis}: {r.reason}" for r in scores.refused)
    ranked: list[RankedMeasurement] = []
    for choice in choices:
        residuals = {f.residual for f in choice.features}
        cost = max((plan.entry(r).cost for r in residuals), default=math.inf)
        missing: list[str] = [f"likelihood of {m}" for m in missing_likelihoods]
        outcomes: dict[tuple[str, ...], float] = {}
        for name in open_hypotheses:
            hypothesis = model.hypothesis(name)
            outcome: list[str] = []
            for feature in choice.features:
                allowed = hypothesis.allowed(feature.residual, feature.region)
                if len(allowed) != 1:
                    missing.append(
                        f"P({feature} | {name}): the signature allows {sorted(allowed)}; no "
                        "outcome likelihood is declared"
                    )
                else:
                    outcome.append(next(iter(allowed)))
            if name in scores.values and len(outcome) == len(choice.features):
                key = tuple(outcome)
                outcomes[key] = outcomes.get(key, 0.0) + scores.values[name]
        bits: float | None = None
        if not missing and choice.features:
            bits = _entropy(list(outcomes.values()))
        ranked.append(
            RankedMeasurement(
                measurement=choice.measurement,
                features=choice.features,
                actionable=choice.actionable,
                separates=choice.separates,
                cost=cost,
                information_bits=bits,
                bits_per_cost=None if bits is None else bits / cost,
                missing=tuple(dict.fromkeys(missing)),
            )
        )
    quantified = bool(ranked) and all(r.bits_per_cost is not None for r in ranked)
    if quantified:
        order = {r.measurement: i for i, r in enumerate(ranked)}
        ranked.sort(key=lambda r: (-(r.bits_per_cost or 0.0), order[r.measurement]))
    return MeasurementRanking(
        method="information_gain_per_cost" if quantified else "structural",
        cost_unit=plan.cost_unit,
        measurements=tuple(ranked),
        missing_likelihoods=missing_likelihoods,
    )


# ---------------------------------------------------------------------------
# Record


@dataclass(frozen=True)
class InvestigationRanking:
    """An investigation's hypotheses and measurements re-ranked, not recorded."""

    track: HypothesisTrack
    pairs: tuple[UnresolvedPair, ...]
    ranking: MeasurementRanking


def rank_investigation(
    ledger: MemoryLedger,
    model: FaultModel,
    plan: MeasurementPlan,
    observations: Sequence[str],
) -> InvestigationRanking:
    """The ranking :func:`update_investigation` records for ``observations``
    (an investigation's evidence ids plus any new ones), computed without
    writing: the live proposal tick (E03.4) reads the same order the record
    would carry."""
    track = track_hypotheses(model, ledger, observations)
    reports = drive_reports(
        ledger,
        model,
        [
            rid
            for rid in track.counted
            if not any(o.conflict for o in track.observations if rid in o.evidence)
        ],
    )
    pairs = unresolved_pairs(model, list(reports.values()), track.signed(), track.open)
    ranking = rank_measurements(model, plan, ledger, track.open, select_measurement(pairs))
    return InvestigationRanking(track, tuple(pairs), ranking)


@dataclass(frozen=True)
class TrackingStep:
    investigation: str
    #: ``False`` when the evidence changed nothing (the same record returned).
    changed: bool
    track: HypothesisTrack
    ranking: MeasurementRanking


def update_investigation(
    ledger: MemoryLedger,
    model: FaultModel,
    plan: MeasurementPlan,
    investigation_id: str,
    evidence_ids: Sequence[str] = (),
    *,
    now: datetime | None = None,
) -> TrackingStep:
    """Fold ``evidence_ids`` into an open investigation and record the ranking."""
    entry = ledger.entry(investigation_id)
    existing = entry.record
    if not isinstance(existing, Investigation):
        raise ValueError(f"{investigation_id} is not an investigation")
    if not entry.current:
        raise ValueError(f"{investigation_id} was superseded by {list(entry.superseded_by)}")
    ranked = rank_investigation(ledger, model, plan, [*existing.observations, *evidence_ids])
    track, pairs, ranking = ranked.track, ranked.pairs, ranked.ranking
    unknowns = [
        f"{pair.a} and {pair.b} are not yet separated by any qualified observation"
        for pair in pairs
    ]
    if ranking.method == "structural":
        unknowns.append(
            "measurements are ranked structurally: information gain needs an evidence-backed "
            "likelihood for every open hypothesis"
        )
    if not track.open:
        unknowns.append(
            "no single modelled hypothesis is consistent with every drive: two faults, an "
            "unmodelled cause, or conflicting evidence"
        )
    unknowns.extend(
        f"conflicting evidence on {o.feature} within one drive is excluded until resolved"
        for o in track.conflicts
    )
    unknowns.extend(
        f"{o.feature} unevaluable: {o.unevaluable}"
        for o in track.observations
        if o.unevaluable is not None
    )
    unknowns.extend(
        f"{name} is language-generated and unscored until modelled" for name in track.unscored
    )
    hypotheses = [
        Hypothesis(label=h.label, status=h.status, supporting=h.supporting, refuting=h.refuting)
        for h in track.hypotheses
    ] + [Hypothesis(label=model.hypothesis(name).label, status="open") for name in track.unscored]
    record = Investigation(
        vehicle=existing.vehicle,
        provenance=Provenance(
            producer=TRACKING_TOOL,
            producer_kind="tool",
            version=f"{TRACKING_TOOL_VERSION}; fault model v{model.version}; plan v{plan.version}",
            code_version=code_version(sys.modules[__name__], fault_model_module),
            derived_from=track.counted,
        ),
        conditions=existing.conditions,
        valid_from=existing.valid_from,
        title=existing.title,
        question=existing.question,
        status="resolved" if len(track.open) == 1 else "open",
        incident=existing.incident,
        observations=track.counted,
        hypotheses=tuple(hypotheses),
        unknowns=tuple(dict.fromkeys(unknowns)),
        next_measurements=ranking.order,
        narrative=(
            "Hypotheses ranked by refuting drives, then supporting drives, then the fault "
            f"model's order; measurements ranked {ranking.method.replace('_', ' ')}. Each "
            "drive's evidence on a feature counts once."
        ),
        supersedes=(investigation_id,),
        supersession_reason="hypothesis tracking: re-ranked with the evidence counted once",
    )
    stored = ledger.append(record, now=now)
    return TrackingStep(stored.id, stored.id != investigation_id, track, ranking)
