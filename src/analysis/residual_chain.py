"""Connect distinct residuals on one drive to investigation evidence (B08.5).

B08 keeps three questions apart (``docs/roadmap/tasks/B08-firmware-informed-behavior.md``,
"How this strengthens the second brain"):

1. **ECU computation** — does the decoded logic reproduce the ECM's command?
   The twin's evidence (E02/E05, :mod:`src.analysis.twin_evidence`).
2. **Command/response** — did the engine deliver what the ECM commanded?
   B06's delivery error (:mod:`src.analysis.delivery_evidence`).
3. **Historical** — has a relationship moved from its condition-matched,
   validated reference? E03.3 change detection and B08 behavior residuals
   (:mod:`src.analysis.behavior_residual_evidence`).

:func:`build_chain` reads the records minted for one drive, places each in
its stage *by its producer* (never by a label a caller could set), and names
the **earliest supported disagreement** in that order: the first stage whose
counted evidence disagrees beyond its own qualified bound.
:func:`investigate_residual_chain` records that as an E03 investigation whose
card names the stage and leaves every alternative open.

What it makes impossible rather than documented:

* **Classes cannot mix.** A producer outside :data:`PRODUCER_CLASSES`, or a
  record whose evidence class disagrees with its producer's, is refused
  (:class:`NotAResidualError`). Only the twin's records stand in the ECU
  computation stage, so a learned correction (a historical record) can never
  change what that stage says.
* **The original twin discrepancy cannot be removed or renormalized.** The
  ledger refuses a tool invalidating another producer's evidence
  (:class:`~src.evidence.ledger.ForeignSupersessionError`); a record that a
  ledger written before that rule shows rewritten is refused here too
  (:class:`DiscrepancyRewriteError`). An investigation update keeps every
  earlier observation (a same-producer recompute is followed to its current
  record), dropping only what the operator retracted, and naming it.
* **Empty is named, never a pass.** Every stage is ``agrees``,
  ``disagrees``, ``diagnostic_only``, ``unevaluable`` (with each record's
  reason) or ``absent``. The chain's outcome is ``agreement`` only when every
  stage present agrees; missing inputs make it ``not_established``.
* **Diagnostic-only stays diagnostic-only.** A B08 residual counts only while
  its qualification reference is current and unretired
  (:func:`~src.analysis.behavior_residual_evidence.check_qualification`,
  re-checked here at read time). A B06 residual counts only with its sign
  (B06's floor, delay-stable, out of sample); ``sign_refused`` is diagnostic.
* **Alias and common-input evidence is not doubled.** Each record's inputs
  resolve to observation identities through B08.1's lineage resolver
  (:func:`src.analysis.artifact_lineage.resolve_input`; derived channels
  expand to their leaves) and group by the profile's
  ``observation_aliases.yaml`` (a shared register, a dependent relation). Two
  records in one stage that share a group are one observation: the second is
  listed ``dependent_on`` the first and does not count. Groups shared *across*
  stages are reported, because a fault in a shared observation moves both.
* **B06's contracts are checked, not restated.** A command/response record
  must carry B06's residual unit (``ResidualDefinition.unit``) and one of its
  regimes (``Regime``); otherwise :class:`ContractMismatchError`.
* **One drive.** Every record must read the chain's session
  (:class:`MixedDrivesError`); synthetic detector tests
  (``data_origin: synthetic``) are not observations and are refused.

Nothing here declares a cause: the earliest supported disagreement says where
the evidence first departs, and each stage's alternatives stay open.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

from src.analysis.artifact_lineage import (
    CatalogHistory,
    CatalogSnapshot,
    DerivedDefinitions,
    InputLineage,
    LineageContext,
    ObservationAliases,
    _load_yaml,
    load_observation_aliases,
    resolve_input,
)
from src.analysis.behavior_residual_evidence import (
    B08_TOOL,
    BEHAVIOR_RELATIONSHIP,
    QUALIFIED,
    check_qualification,
)
from src.analysis.change_evaluation import TOOL as CHANGE_TOOL
from src.analysis.delivery_evidence import DELIVERY_TOOL, DROOP_TOOL, ENGINE_RESPONSE
from src.analysis.delivery_investigation import observation_of
from src.analysis.fault_model import FaultModel, evaluate_observations
from src.analysis.lambda_delivery import Regime, ResidualDefinition
from src.analysis.twin_evidence import ECU_COMMAND, TWIN_TOOL
from src.evidence.ledger import LedgerEntry, MemoryLedger, MemoryQuery
from src.evidence.provenance import code_version
from src.evidence.records import (
    Conditions,
    Evidence,
    Hypothesis,
    Investigation,
    Provenance,
    Quantity,
    Reference,
)
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.signals.derived import DERIVED_CHANNEL_INPUTS, DERIVED_CHANNEL_OPTIONAL_INPUTS

__all__ = [
    "CHAIN_TOOL",
    "CHAIN_TOOL_VERSION",
    "OBSERVATION_ALIASES_FILE",
    "PRODUCER_CLASSES",
    "STAGES",
    "ChainStep",
    "ContractMismatchError",
    "DependencyResolver",
    "DiscrepancyRewriteError",
    "EXCLUDED_PRODUCERS",
    "MixedDrivesError",
    "NotAResidualError",
    "ResidualChain",
    "StageEntry",
    "StageVerdict",
    "build_chain",
    "chain_incident",
    "dependency_resolver",
    "drive_residuals",
    "investigate_residual_chain",
]

CHAIN_TOOL = "residual_chain"
CHAIN_TOOL_VERSION = "1"
OBSERVATION_ALIASES_FILE = "observation_aliases.yaml"

ResidualClass = Literal["ecu_computation", "command_response", "historical"]
#: Causal order: the command is computed, then delivered, then compared with history.
STAGES: tuple[ResidualClass, ...] = ("ecu_computation", "command_response", "historical")

#: Producer -> its stage. The only way into a stage.
PRODUCER_CLASSES: Mapping[str, ResidualClass] = {
    TWIN_TOOL: "ecu_computation",
    DELIVERY_TOOL: "command_response",
    CHANGE_TOOL: "historical",
    B08_TOOL: "historical",
}
#: Producers that mint residual-like evidence but stand in no stage, and why.
EXCLUDED_PRODUCERS: Mapping[str, str] = {
    DROOP_TOOL: (
        "fuel-pressure droop is a measurement inside the delivery path (pressure against "
        "airflow, region high_airflow/enrichment), not commanded against measured mixture: "
        "it separates the command/response stage's hypotheses (E03.2's fault model, the "
        "delivery investigation) and would otherwise read a pressure fall as a stage "
        "disagreement"
    ),
}
#: Producer -> the ``evidence_class`` label its records must carry.
_EVIDENCE_CLASSES: Mapping[str, str] = {
    TWIN_TOOL: ECU_COMMAND,
    DELIVERY_TOOL: ENGINE_RESPONSE,
    CHANGE_TOOL: ENGINE_RESPONSE,
    B08_TOOL: BEHAVIOR_RELATIONSHIP,
}

EntryState = Literal["agrees", "disagrees", "diagnostic_only", "unevaluable"]
StageState = Literal["agrees", "disagrees", "diagnostic_only", "unevaluable", "absent"]
Outcome = Literal["disagreement", "agreement", "not_established"]

#: Which record of a dependent pair counts: a disagreement is never hidden
#: behind an agreeing alias, and an evaluated record before an empty one.
_PRIORITY: Mapping[str, int] = {
    "disagrees": 0,
    "agrees": 1,
    "diagnostic_only": 2,
    "unevaluable": 3,
}


class NotAResidualError(ValueError):
    """A record that is not a residual of any chain stage."""


class MixedDrivesError(ValueError):
    """Records from more than one drive offered as one chain."""


class DiscrepancyRewriteError(ValueError):
    """A residual superseded or retracted by another producer."""


class ContractMismatchError(ValueError):
    """A command/response record outside B06's residual contract."""


# ---------------------------------------------------------------------------
# Observation dependency (B08.1's resolver, the profile's aliases)


@dataclass(frozen=True)
class DependencyResolver:
    """Channel -> observation groups, through B08.1's lineage resolver.

    Resolution uses the current catalog only: a chain links evidence about
    drives already minted against today's decode; historical-decode questions
    are B08.1's audit, not this one's.
    """

    ctx: LineageContext
    snapshot: CatalogSnapshot

    @property
    def aliases(self) -> ObservationAliases:
        return self.ctx.aliases

    def leaves(self, channel: str) -> list[InputLineage]:
        return resolve_input(
            channel, self.ctx, self.snapshot, built_at=datetime.now(UTC), recorded_from=None
        )

    def group_of(self, leaf: InputLineage) -> str:
        """One observation's key: a shared register, a dependent relation, or
        the identity itself; an unresolved leaf keeps its own channel's key."""
        key = leaf.identity_key
        if key is not None:
            register = self.aliases.register_of(key)
            if register is not None:
                return f"register:0x{register.register:08X}:{register.quantity}"
            relations = self.aliases.relations_of(key)
            if relations:
                members = sorted(
                    f"{kind}:0x{value:X}" for r in relations for kind, value in r.identities
                )
                return "relation:" + "+".join(dict.fromkeys(members))
            kind, value = key
            return f"{kind}:0x{value:X}"
        if leaf.kind == "binding" and leaf.identity_now:
            # "CAN 0x23D[2:4] NAME": the bytes, not the name.
            return "can:" + leaf.identity_now.rsplit(" ", 1)[0].removeprefix("CAN ")
        return f"channel:{leaf.channel}"

    def groups(self, channels: Iterable[str]) -> tuple[frozenset[str], tuple[str, ...]]:
        """The groups ``channels`` observe, and the leaves left unresolved."""
        by_group, unresolved = self.channels_by_group(channels)
        return frozenset(by_group), unresolved

    def channels_by_group(
        self, channels: Iterable[str]
    ) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
        """Group -> the offered channels that observe it, and unresolved leaves."""
        by_group: dict[str, list[str]] = {}
        unresolved: list[str] = []
        for channel in channels:
            for leaf in self.leaves(channel):
                members = by_group.setdefault(self.group_of(leaf), [])
                if channel not in members:
                    members.append(channel)
                if leaf.identity_key is None and leaf.kind not in ("binding", "out_of_scope"):
                    unresolved.append(leaf.channel)
        return {g: tuple(c) for g, c in by_group.items()}, tuple(dict.fromkeys(unresolved))


def dependency_resolver(
    profile_dir: Path,
    *,
    dids_path: Path = DIDS_YAML,
    bindings_path: Path = BINDINGS_YAML,
) -> DependencyResolver:
    """The resolver for one vehicle profile (its ``observation_aliases.yaml``)."""
    current = CatalogSnapshot.from_documents(
        "current",
        None,
        _load_yaml(dids_path.read_text()),
        _load_yaml(bindings_path.read_text()),
    )
    derived = {name: tuple(map(tuple, groups)) for name, groups in DERIVED_CHANNEL_INPUTS.items()}
    optional = {name: tuple(c) for name, c in DERIVED_CHANNEL_OPTIONAL_INPUTS.items()}
    definitions = DerivedDefinitions("src.signals.derived (current)", derived, optional)
    ctx = LineageContext(
        history=CatalogHistory(snapshots=(), current=current),
        renames=(),
        aliases=load_observation_aliases(profile_dir / OBSERVATION_ALIASES_FILE),
        derived_inputs=derived,
        derived_optional_inputs=optional,
        derived_at=lambda _when: definitions,
    )
    return DependencyResolver(ctx, current)


# ---------------------------------------------------------------------------
# One record in its stage


@dataclass(frozen=True)
class StageEntry:
    record_id: str
    producer: str
    stage: ResidualClass
    state: EntryState
    #: ``richer``/``leaner``/``mixed`` (ECU computation: the ECM against its
    #: reproduced logic), ``lean``/``rich`` (command/response), or the
    #: historical record's direction; ``None`` unless ``disagrees``.
    direction: str | None
    #: Why it is not evaluated or only diagnostic (the record's own words).
    reason: str | None
    #: The record's qualified residual, as minted (never rescaled here).
    residual: Quantity | None
    groups: frozenset[str]
    unresolved: tuple[str, ...]
    #: Group -> the record's channels that observe it.
    group_channels: Mapping[str, tuple[str, ...]]
    #: ROM identity the record names, when it names one.
    image_sha256: str | None
    model: str | None
    dependent_on: str | None = None


@dataclass(frozen=True)
class StageVerdict:
    stage: ResidualClass
    state: StageState
    direction: str | None
    counted: tuple[str, ...]
    dependent: tuple[str, ...]
    #: ``record id -> reason`` for every diagnostic-only or unevaluable record.
    reasons: Mapping[str, str]


@dataclass(frozen=True)
class ResidualChain:
    session: str
    entries: tuple[StageEntry, ...]
    stages: Mapping[ResidualClass, StageVerdict]
    outcome: Outcome
    #: The first stage, in causal order, whose counted evidence disagrees.
    earliest_disagreement: ResidualClass | None
    #: Stages before the earliest disagreement that do not agree: the
    #: disagreement is the earliest *supported* one, not proven the first.
    upstream_unestablished: tuple[ResidualClass, ...]
    #: Observation group -> the stages whose counted evidence shares it.
    shared_across_stages: Mapping[str, tuple[ResidualClass, ...]]
    rom_identities: Mapping[ResidualClass, tuple[str, ...]] = field(default_factory=dict)

    def entry(self, record_id: str) -> StageEntry:
        for item in self.entries:
            if item.record_id == record_id:
                return item
        raise KeyError(record_id)


def _sessions(record: Evidence) -> set[str]:
    return {name for source in record.sources for name in source.session.split(",")}


def _check_standing(ledger: MemoryLedger, entry: LedgerEntry) -> None:
    """A record superseded or retracted by another tool was rewritten by
    someone else: refused, whatever the rewrite claims. The operator's own
    retraction withdraws a record; it is not a rewrite."""
    record = entry.record
    foreign = []
    for rid in (*entry.superseded_by, *((entry.retracted_by,) if entry.retracted_by else ())):
        provenance = ledger.get(rid).provenance
        if provenance.producer_kind == "tool" and provenance.producer != record.provenance.producer:
            foreign.append(rid)
    if foreign:
        raise DiscrepancyRewriteError(
            f"{record.id} ({record.provenance.producer}) was superseded or retracted by "
            + ", ".join(f"{rid} ({ledger.get(rid).provenance.producer})" for rid in foreign)
            + ". A residual is recomputed only by its own producer; a learned correction or "
            "another tool cannot remove or renormalize it."
        )


def _current(ledger: MemoryLedger, record_id: str) -> str:
    """``record_id``, or the same producer's current recompute of it."""
    entry = ledger.resolve_evidence(record_id)
    _check_standing(ledger, entry)
    while not entry.current:
        if not entry.superseded_by:
            raise NotAResidualError(
                f"{entry.record.id} was retracted ({entry.retracted_by}); it is no longer "
                "evidence for any stage"
            )
        entry = ledger.resolve_evidence(entry.superseded_by[-1])
        _check_standing(ledger, entry)
    return entry.record.id


def _classify(record: Evidence) -> ResidualClass:
    producer = record.provenance.producer
    stage = PRODUCER_CLASSES.get(producer)
    if producer in EXCLUDED_PRODUCERS:
        raise NotAResidualError(
            f"{record.id} ({producer}) stands in no stage: {EXCLUDED_PRODUCERS[producer]}"
        )
    if stage is None:
        raise NotAResidualError(
            f"{record.id} was produced by {producer!r}, which is no residual stage's producer "
            f"({sorted(PRODUCER_CLASSES)})"
        )
    expected = _EVIDENCE_CLASSES[producer]
    if record.labels.get("evidence_class") != expected:
        raise NotAResidualError(
            f"{record.id} ({producer}) is labelled "
            f"{record.labels.get('evidence_class', 'no evidence class')!r}; that producer's "
            f"residuals are {expected!r}"
        )
    if record.labels.get("data_origin") == "synthetic":
        raise NotAResidualError(
            f"{record.id} is a synthetic detector test (data_origin synthetic), not an "
            "observation of the drive"
        )
    return stage


_TWIN_DIRECTIONS = {"positive": "richer", "negative": "leaner", "mixed": "mixed"}
_SIGN_DIRECTIONS = {"+": "lean", "-": "rich"}


def _twin_state(record: Evidence) -> tuple[EntryState, str | None, str | None]:
    if record.result.status != "evaluated":
        return "unevaluable", None, f"{record.result.reason}: {record.result.detail}"
    sign = record.labels.get("residual_sign", "none")
    if sign == "zero":
        return "agrees", None, None
    if sign in _TWIN_DIRECTIONS:
        return "disagrees", _TWIN_DIRECTIONS[sign], None
    return "unevaluable", None, "refused: evaluated with no qualified residual"


def _check_b06_contract(record: Evidence) -> None:
    unit = get_args(ResidualDefinition.model_fields["unit"].annotation)[0]
    region = record.labels.get("region")
    if region not in get_args(Regime):
        raise ContractMismatchError(
            f"{record.id}: region {region!r} is not one of B06's regimes {list(get_args(Regime))}"
        )
    residual = record.result.residual
    if residual is not None and residual.unit != unit:
        raise ContractMismatchError(
            f"{record.id}: residual in {residual.unit!r}; B06's residual is in {unit!r}"
        )


def _delivery_state(
    ledger: MemoryLedger, record: Evidence
) -> tuple[EntryState, str | None, str | None]:
    """B06's residual read the way E03.1 reads it (``observation_of``): a sign
    only when qualified against the floor, delay-stable and out of sample."""
    _check_b06_contract(record)
    if record.result.status != "evaluated":
        return "unevaluable", None, f"{record.result.reason}: {record.result.detail}"
    observation = observation_of(ledger, record.id)
    if observation.sign is None:
        refused = record.labels.get("sign_refused") or str(observation.unevaluable)
        return "diagnostic_only", None, f"sign_refused: {refused}"
    if observation.sign == "0":
        return "agrees", None, None
    return "disagrees", _SIGN_DIRECTIONS[observation.sign], None


def _change_reference_lapsed(ledger: MemoryLedger, record: Evidence) -> str | None:
    """Why the frozen reference a change record was judged against no longer
    stands, or ``None``. The same re-check a B08 qualification gets: the
    reference record must be current and unretired."""
    prefix = record.labels.get("reference")
    if not prefix:
        return "the record names no frozen reference"
    found: list[LedgerEntry] = []
    for entry in ledger.query(MemoryQuery(kinds=("reference",), include_superseded=True)):
        reference = entry.record
        if isinstance(reference, Reference) and reference.source_ref.startswith(
            f"change_reference:{prefix}"
        ):
            found.append(entry)
    if not found:
        return f"no reference record for change_reference:{prefix} is on the ledger"
    for entry in found:
        if entry.current and not entry.retired:
            return None
    retired = [e for e in found if e.retired]
    if retired:
        last = retired[-1]
        return f"reference {last.record.id} was retired by intervention {last.retired_by}"
    return f"reference {found[-1].record.id} is superseded or retracted"


def _change_state(
    ledger: MemoryLedger, record: Evidence
) -> tuple[EntryState, str | None, str | None]:
    if record.result.status != "evaluated":
        return "unevaluable", None, f"{record.result.reason}: {record.result.detail}"
    why = _change_reference_lapsed(ledger, record)
    if why is not None:
        return "diagnostic_only", None, f"reference_lapsed: {why}"
    if record.labels.get("kind") == "change_alarm":
        return "disagrees", record.labels.get("direction", "unstated"), None
    return "agrees", None, None


def _b08_state(ledger: MemoryLedger, record: Evidence) -> tuple[EntryState, str | None, str | None]:
    if record.labels.get("qualification") != QUALIFIED:
        detail = record.result.detail or "no qualification record"
        return "diagnostic_only", None, f"diagnostic_only: {detail}"
    why = check_qualification(
        ledger,
        record.labels["qualification_reference"],
        model_key=f"{record.labels['model']}@{record.labels['model_version']}",
        program_id=record.vehicle.program_id,
        calibration=record.vehicle.calibration,
    )
    if why is not None:
        return "diagnostic_only", None, f"qualification_lapsed: {why}"
    if record.result.status != "evaluated":
        return "unevaluable", None, f"{record.result.reason}: {record.result.detail}"
    sign = record.labels.get("sign", "0")
    if sign == "0":
        return "agrees", None, None
    return "disagrees", "above" if sign == "+" else "below", None


def _inputs(record: Evidence) -> tuple[str, ...]:
    declared = record.labels.get("residual_inputs")
    if declared:
        return tuple(declared.split(","))
    # Every channel the tool read: conservative (more shared groups), so a
    # record can only be under-counted, never doubled.
    return tuple(dict.fromkeys(c for source in record.sources for c in source.channels))


def _entry(ledger: MemoryLedger, resolver: DependencyResolver, record: Evidence) -> StageEntry:
    stage = _classify(record)
    producer = record.provenance.producer
    if producer == TWIN_TOOL:
        state, direction, reason = _twin_state(record)
    elif producer == DELIVERY_TOOL:
        state, direction, reason = _delivery_state(ledger, record)
    elif producer == CHANGE_TOOL:
        state, direction, reason = _change_state(ledger, record)
    else:
        state, direction, reason = _b08_state(ledger, record)
    by_group, unresolved = resolver.channels_by_group(_inputs(record))
    image = record.labels.get("image_sha256")
    model = record.labels.get("model")
    return StageEntry(
        record_id=record.id,
        producer=producer,
        stage=stage,
        state=state,
        direction=direction,
        reason=reason,
        residual=record.result.residual if state in ("agrees", "disagrees") else None,
        groups=frozenset(by_group),
        unresolved=unresolved,
        group_channels=by_group,
        image_sha256=None if image in (None, "unknown") else image,
        model=None if model is None else f"{model}@{record.labels.get('model_version')}",
    )


def _verdict(
    stage: ResidualClass, entries: list[StageEntry]
) -> tuple[StageVerdict, list[StageEntry]]:
    ordered = sorted(entries, key=lambda e: (_PRIORITY[e.state], e.record_id))
    counted: list[StageEntry] = []
    out: list[StageEntry] = []
    for item in ordered:
        owner = next((c for c in counted if c.groups & item.groups), None)
        if owner is None:
            counted.append(item)
            out.append(item)
        else:
            out.append(StageEntry(**{**item.__dict__, "dependent_on": owner.record_id}))
    reasons = {e.record_id: e.reason for e in ordered if e.reason is not None}
    states = {c.state for c in counted}
    direction: str | None = None
    if not counted:
        state: StageState = "absent"
    elif "disagrees" in states:
        state = "disagrees"
        directions = sorted({c.direction or "unstated" for c in counted if c.state == "disagrees"})
        direction = directions[0] if len(directions) == 1 else "conflicting"
    elif "agrees" in states:
        state = "agrees"
    elif "diagnostic_only" in states:
        state = "diagnostic_only"
    else:
        state = "unevaluable"
    verdict = StageVerdict(
        stage=stage,
        state=state,
        direction=direction,
        counted=tuple(c.record_id for c in counted),
        dependent=tuple(e.record_id for e in out if e.dependent_on is not None),
        reasons=reasons,
    )
    return verdict, out


def build_chain(
    ledger: MemoryLedger, evidence_ids: Sequence[str], *, resolver: DependencyResolver
) -> ResidualChain:
    """The residual chain for one drive from the records ``evidence_ids`` name.

    Each id is followed to its producer's current recompute (a rewrite by
    another producer is refused). All records must read one session.
    """
    if not evidence_ids:
        raise ValueError("a chain is built from at least one residual record")
    ids = list(dict.fromkeys(_current(ledger, rid) for rid in evidence_ids))
    records: list[Evidence] = []
    for rid in ids:
        record = ledger.resolve_evidence(rid).record
        assert isinstance(record, Evidence)
        records.append(record)
    common = set.intersection(*(_sessions(r) for r in records))
    if len(common) != 1:
        raise MixedDrivesError(
            "a residual chain is one drive's: the records read "
            + "; ".join(f"{r.id}: {sorted(_sessions(r))}" for r in records)
        )
    (session,) = common
    by_stage: dict[ResidualClass, list[StageEntry]] = {stage: [] for stage in STAGES}
    for record in records:
        item = _entry(ledger, resolver, record)
        by_stage[item.stage].append(item)
    stages: dict[ResidualClass, StageVerdict] = {}
    entries: list[StageEntry] = []
    for stage in STAGES:
        verdict, placed = _verdict(stage, by_stage[stage])
        stages[stage] = verdict
        entries.extend(placed)
    earliest = next((s for s in STAGES if stages[s].state == "disagrees"), None)
    upstream: tuple[ResidualClass, ...] = ()
    if earliest is not None:
        before = STAGES[: STAGES.index(earliest)]
        upstream = tuple(s for s in before if stages[s].state != "agrees")
        outcome: Outcome = "disagreement"
    elif all(v.state in ("agrees", "absent") for v in stages.values()) and any(
        v.state == "agrees" for v in stages.values()
    ):
        outcome = "agreement"
    else:
        outcome = "not_established"
    counted = {rid for v in stages.values() for rid in v.counted}
    holders: dict[str, set[ResidualClass]] = {}
    for item in entries:
        if item.record_id in counted:
            for group in item.groups:
                holders.setdefault(group, set()).add(item.stage)
    shared = {
        group: tuple(s for s in STAGES if s in owners)
        for group, owners in sorted(holders.items())
        if len(owners) > 1
    }
    roms = {
        stage: tuple(
            sorted({e.image_sha256 for e in entries if e.stage == stage and e.image_sha256})
        )
        for stage in STAGES
    }
    return ResidualChain(
        session=session,
        entries=tuple(entries),
        stages=stages,
        outcome=outcome,
        earliest_disagreement=earliest,
        upstream_unestablished=upstream,
        shared_across_stages=shared,
        rom_identities={k: v for k, v in roms.items() if v},
    )


def drive_residuals(ledger: MemoryLedger, session: str) -> list[str]:
    """Current residual records of every chain producer that read ``session``.

    The same tests :func:`build_chain` applies: a stage producer, that
    producer's evidence class (E03.3's ``policy_frozen`` records carry none),
    not a synthetic detector run, and the session matched the way
    comma-joined multi-session sources are split."""
    found = []
    for entry in ledger.query(MemoryQuery(kinds=("evidence",))):
        record = entry.record
        assert isinstance(record, Evidence)
        producer = record.provenance.producer
        if (
            producer in PRODUCER_CLASSES
            and record.labels.get("evidence_class") == _EVIDENCE_CLASSES[producer]
            and record.labels.get("data_origin") != "synthetic"
            and session in _sessions(record)
        ):
            found.append(record.id)
    return found


# ---------------------------------------------------------------------------
# The investigation

_STAGE_WORDS: Mapping[ResidualClass, str] = {
    "ecu_computation": "the ECU computation (the ECM's command against its reproduced logic)",
    "command_response": "the command and response (the measured mixture against the command)",
    "historical": "the historical relationship (the engine against its validated reference)",
}

#: Open alternatives per stage. None is credited with the evidence and none
#: is ranked: the chain says where the evidence departs, not why.
_ALTERNATIVES: Mapping[ResidualClass, tuple[str, ...]] = {
    "ecu_computation": (
        "the reproduced logic omits a correction, state or override the ECM applied",
        "the reproduction read another image, slot or branch than the ECM ran",
        "the inputs and the command were not read at the same instant",
    ),
    "command_response": (
        "fuel delivery differs from the command (pressure or injector flow)",
        "the airflow measurement the command was computed from is in error",
        "the measured mixture is biased (sensor, its transfer or its pressure gain)",
        "the command and the response were paired across the wrong transport delay",
    ),
    "historical": (
        "the engine relationship changed since the reference was validated",
        "the drive left the conditions the reference covers",
        "an input shared with the reference moved both sides",
    ),
}

_TITLES: Mapping[ResidualClass, str] = {
    "ecu_computation": "Earliest supported disagreement: the ECU computation stage",
    "command_response": "Earliest supported disagreement: the command and response stage",
    "historical": "Earliest supported disagreement: the historical relationship stage",
}

_NEXT: Mapping[ResidualClass, str] = {
    "ecu_computation": (
        "read the final-command words, the lookup inputs and the active table in one "
        "window, so omitted logic, timing and provenance are observed rather than inferred"
    ),
    "command_response": (
        "separate the delivery alternatives with the fuel-delivery investigation's "
        "measurement plan (fuel pressure through a full-throttle pull first)"
    ),
    "historical": (
        "evaluate the relationship on the next independent drive under the reference's "
        "conditions before reading the change as persistent"
    ),
}

ENGINE_HEALTH_UNKNOWN = (
    "engine health is not assessed: agreement in a stage says the evidence there did not "
    "depart, and a disagreement localises where it first departs, not its cause"
)


def chain_incident(chain: ResidualChain) -> str:
    """What the investigation is an occurrence of across drives: the earliest
    supported disagreement's stage, or the outcome when there is none."""
    return f"{CHAIN_TOOL}:{chain.earliest_disagreement or chain.outcome}"


@dataclass(frozen=True)
class ChainStep:
    investigation: str
    chain: ResidualChain


def _open(ledger: MemoryLedger, session: str) -> Investigation | None:
    """The open residual-chain investigation of ``session``, if one stands."""
    found = []
    for entry in ledger.query(MemoryQuery(kinds=("investigation",))):
        record = entry.record
        if (
            isinstance(record, Investigation)
            and record.provenance.producer == CHAIN_TOOL
            and record.status == "open"
            and record.observations
        ):
            observed = ledger.resolve_evidence(record.observations[0]).record
            assert isinstance(observed, Evidence)
            if session in _sessions(observed):
                found.append(record)
    return found[-1] if found else None


#: Always open beside the fault model's hypotheses: it assumes a valid pairing.
_PAIRING = _ALTERNATIVES["command_response"][-1]


def _alternatives(
    ledger: MemoryLedger,
    chain: ResidualChain,
    stage: ResidualClass,
    fault_model: FaultModel | None,
) -> tuple[str, ...]:
    """The stage's open alternatives. For command/response with the fault
    model, E03.2's own reading: ``observation_of`` (floor, delay, in-sample
    refusal) and ``evaluate_observations`` (an opposite-sign conflict between
    the stage's records raises ``FaultModelError``)."""
    if stage != "command_response" or fault_model is None:
        return _ALTERNATIVES[stage]
    ids = [e.record_id for e in chain.entries if e.stage == stage]
    report = evaluate_observations(fault_model, [observation_of(ledger, rid) for rid in ids])
    labels = {h.name: h.label for h in fault_model.modelled}
    admitted = [labels[name] for name in report.consistent]
    if not admitted:
        return _ALTERNATIVES[stage]
    return (*admitted, _PAIRING)


def _unknowns(chain: ResidualChain) -> list[str]:
    out = [ENGINE_HEALTH_UNKNOWN]
    for stage in STAGES:
        verdict = chain.stages[stage]
        words = stage.replace("_", " ")
        if verdict.state == "absent":
            out.append(f"the {words} stage has no evidence on this drive")
        elif verdict.state == "unevaluable":
            kinds = sorted({r.split(":", 1)[0] for r in verdict.reasons.values()})
            out.append(f"the {words} stage could not be evaluated ({', '.join(kinds)})")
        elif verdict.state == "diagnostic_only":
            out.append(
                f"the {words} stage holds diagnostic-only evidence: no qualified residual "
                "supports or refutes anything there"
            )
        if verdict.dependent:
            out.append(
                f"the {words} stage holds records that share an observation with a counted "
                "record; they are not counted again"
            )
    for stage in chain.upstream_unestablished:
        out.append(
            f"the {stage.replace('_', ' ')} stage upstream of the disagreement is not "
            "established, so an earlier departure cannot be ruled out"
        )
    if chain.shared_across_stages:
        out.append(
            "some observations are common to more than one stage (named in the narrative); "
            "an error in a shared observation moves every stage that reads it"
        )
    return out


def _withdrawn(ledger: MemoryLedger, record_id: str) -> bool:
    """Whether the operator retracted ``record_id`` (a withdrawal, not a
    rewrite: a tool's retraction is still refused by :func:`build_chain`)."""
    entry = ledger.resolve_evidence(record_id)
    if entry.retracted_by is None:
        return False
    return ledger.get(entry.retracted_by).provenance.producer_kind == "operator"


def investigate_residual_chain(
    ledger: MemoryLedger,
    evidence_ids: Sequence[str],
    *,
    resolver: DependencyResolver,
    fault_model: FaultModel | None = None,
    now: datetime | None = None,
) -> ChainStep:
    """Open, or retrieve and update, the residual-chain investigation of one drive.

    An open investigation of the same drive is superseded with the union of
    its observations and ``evidence_ids``: an update cannot drop a record it
    already observed (a same-producer recompute replaces it; a rewrite by
    another producer is refused).

    With the vehicle's ``fault_model`` (E03.2), a command/response departure
    lists the modelled hypotheses its counted signs admit, plus the pairing
    alternative the fault model assumes away; without it, a generic list. In
    either case every hypothesis stays open and none is credited.
    """
    session = build_chain(ledger, evidence_ids, resolver=resolver).session
    existing = _open(ledger, session)
    withdrawn = [
        rid for rid in (existing.observations if existing else ()) if _withdrawn(ledger, rid)
    ]
    ids = [
        *(rid for rid in (existing.observations if existing else ()) if rid not in withdrawn),
        *evidence_ids,
    ]
    chain = build_chain(ledger, ids, resolver=resolver)
    observations = tuple(e.record_id for e in chain.entries)
    records = [ledger.resolve_evidence(rid).record for rid in observations]
    if chain.earliest_disagreement is not None:
        stage = chain.earliest_disagreement
        title = _TITLES[stage]
        question = (
            f"Why does the evidence first depart at {_STAGE_WORDS[stage]}, and which "
            "measurement separates the open alternatives?"
        )
        hypotheses = tuple(
            Hypothesis(label=label) for label in _alternatives(ledger, chain, stage, fault_model)
        )
        next_measurements = [_NEXT[stage]]
    elif chain.outcome == "agreement":
        title = "No stage departs on this drive in the stages that were evaluated"
        question = (
            "Do the evaluated stages keep agreeing, and what would evaluate the stages "
            "that hold no evidence?"
        )
        agreeing = tuple(
            rid for v in chain.stages.values() if v.state == "agrees" for rid in v.counted
        )
        hypotheses = (
            Hypothesis(
                label=(
                    "the evaluated stages agree with their references (an agreement claim, "
                    "not an engine-health claim)"
                ),
                supporting=agreeing,
            ),
        )
        next_measurements = []
    else:
        title = "No supported disagreement: the residual chain is not established"
        question = (
            "What must be captured or qualified before the stages of this drive can be compared?"
        )
        hypotheses = ()
        next_measurements = [
            "capture the inputs each unevaluated stage names, in the profile that polls them"
        ]
    shared = "; ".join(
        " and ".join(stages)
        + " both read "
        + ", ".join(
            dict.fromkeys(
                channel for item in chain.entries for channel in item.group_channels.get(group, ())
            )
        )
        for group, stages in chain.shared_across_stages.items()
    )
    lines = [
        f"Residual chain for {chain.session}: "
        + ", ".join(f"{s} {chain.stages[s].state}" for s in STAGES)
        + "."
    ]
    if shared:
        lines.append(f"Observations shared across stages: {shared}.")
    if withdrawn:
        lines.append(f"Withdrawn by the operator: {', '.join(withdrawn)}.")
    if chain.rom_identities:
        lines.append(
            "ROM images named: "
            + "; ".join(f"{s} {', '.join(v)}" for s, v in chain.rom_identities.items())
            + "."
        )
    investigation = Investigation(
        vehicle=records[0].vehicle,
        provenance=Provenance(
            producer=CHAIN_TOOL,
            producer_kind="tool",
            version=CHAIN_TOOL_VERSION,
            code_version=code_version(sys.modules[__name__]),
            derived_from=observations,
        ),
        conditions=Conditions(
            regime="every residual stage minted for one drive, each under its own regime",
            vehicle_state=None,
        ),
        valid_from=min(r.valid_from for r in records),
        title=title,
        question=question,
        status="open",
        incident=chain_incident(chain),
        observations=observations,
        hypotheses=hypotheses,
        unknowns=tuple(
            dict.fromkeys(
                [
                    *_unknowns(chain),
                    *(
                        [
                            "records the operator withdrew since the last update are no longer "
                            "observed (named in the narrative)"
                        ]
                        if withdrawn
                        else []
                    ),
                ]
            )
        ),
        next_measurements=tuple(next_measurements),
        narrative=" ".join(lines),
        supersedes=() if existing is None else (existing.id,),
        supersession_reason=None if existing is None else "re-linked with later residual evidence",
    )
    return ChainStep(ledger.append(investigation, now=now).id, chain)
