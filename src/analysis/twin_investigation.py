"""Link target-phi twin evidence to an investigation as ECU-command evidence (E02.6).

:func:`investigate_command_residual` takes one twin evidence record
(:mod:`src.analysis.twin_evidence`) and opens, or retrieves and updates, the
investigation of the ECM's command against its reproduced logic. The card
(:func:`src.evidence.cards.investigation_card`) then renders every value from
the twin's own records.

What it makes impossible rather than documented:

* **Only ECU-command evidence enters.** A record not minted by the twin, or
  not labelled ``evidence_class: ecu_command``, is refused
  (:class:`NotEcuCommandError`). The reverse holds in
  :mod:`src.analysis.delivery_investigation`, which refuses twin evidence: a
  command residual is never relabelled a delivery error, and the hypotheses
  here are causes of a *computation* disagreement, never engine faults.
* **A positive residual is "the ECM commanded richer than the reproduced
  logic".** Its open alternatives are fixed and always all present: logic the
  reproduction omits (the application model's blockers and uncovered logic,
  named on the evidence record), acquisition timing between the inputs and the
  command, and provenance (another table, slot or image than the one the ECM
  ran). Nothing here ranks them: the evidence is the investigation's
  observation and supports none of them over the others; the next
  measurement separates them.
* **Unavailable stays unevaluable.** Unevaluable twin evidence is linked as
  an observation and supports no hypothesis; the card shows its named reason.
* **A zero residual makes no engine-health claim.** Every twin investigation
  carries that unknown, and the evidence's ``engine_health`` label is
  ``not_assessed``.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from src.analysis.twin_evidence import ECU_COMMAND, TWIN_INCIDENT, TWIN_TOOL
from src.evidence.ledger import MemoryLedger, MemoryQuery
from src.evidence.provenance import code_version
from src.evidence.records import Conditions, Evidence, Hypothesis, Investigation, Provenance

__all__ = [
    "ENGINE_HEALTH_UNKNOWN",
    "INVESTIGATION_TOOL",
    "INVESTIGATION_TOOL_VERSION",
    "NotEcuCommandError",
    "command_incident",
    "investigate_command_residual",
]

INVESTIGATION_TOOL = "twin_investigation"
INVESTIGATION_TOOL_VERSION = "1"

Direction = Literal["richer", "leaner", "mixed", "agrees", "unevaluable"]

#: Carried by every twin investigation: command agreement is not a health check.
ENGINE_HEALTH_UNKNOWN = (
    "engine health and fuel delivery: not assessed. A command residual compares the "
    "ECM with its reproduced logic; it is not a delivery error and not a fault, and "
    "agreement says nothing about what the engine did with the command"
)

_TITLES: dict[Direction, str] = {
    "richer": "The ECM commanded richer than its reproduced target logic",
    "leaner": "The ECM commanded leaner than its reproduced target logic",
    "mixed": "The ECM commanded both richer and leaner than its reproduced target logic",
    "agrees": "The ECM command agrees with its reproduced target logic",
    "unevaluable": "The ECM command could not be compared with its reproduced target logic",
}

_QUESTIONS: dict[Direction, str] = {
    "richer": (
        "Why did the ECM command a richer equivalence ratio than the reproduced target "
        "logic gives, and which measurement separates omitted logic, timing and provenance?"
    ),
    "leaner": (
        "Why did the ECM command a leaner equivalence ratio than the reproduced target "
        "logic gives, and which measurement separates omitted logic, timing and provenance?"
    ),
    "mixed": (
        "Why did the ECM command differ from the reproduced target logic in both "
        "directions, and which measurement separates omitted logic, timing and provenance?"
    ),
    "agrees": (
        "Does the reproduced target logic keep agreeing with the ECM command in the "
        "regions it has evaluated, and which regions remain unevaluated?"
    ),
    "unevaluable": (
        "What must be captured or resolved before the reproduced target logic can be "
        "compared with the ECM command?"
    ),
}


def _hypotheses(word: str) -> tuple[str, str, str]:
    return (
        f"the ECM commanded {word} than the reproduced logic because of logic the twin "
        "omits: a correction, state or override on the final-command path",
        f"the ECM commanded {word} only apparently: the lookup inputs and the command were "
        "not read at the same instant (acquisition timing)",
        f"the ECM commanded {word} only apparently: the twin read another table, slot or "
        "image than the ECM ran (provenance)",
    )


_WORDS: dict[Direction, str] = {
    "richer": "richer",
    "leaner": "leaner",
    "mixed": "both richer and leaner",
}

_AGREES = (
    "the reproduced target logic computes the command the ECM wrote in the evaluated "
    "regions (a computation claim, not an engine-health claim)"
)


class NotEcuCommandError(ValueError):
    """Evidence that is not the twin's ECU-command evidence offered to this loop."""


def command_incident(direction: Direction) -> str:
    """The investigation incident for one kind of command disagreement."""
    return f"{TWIN_INCIDENT}:{direction}"


def _twin_evidence(ledger: MemoryLedger, evidence_id: str) -> Evidence:
    record = ledger.resolve_evidence(evidence_id).record
    assert isinstance(record, Evidence)
    if (
        record.provenance.producer != TWIN_TOOL
        or record.labels.get("evidence_class") != ECU_COMMAND
    ):
        raise NotEcuCommandError(
            f"{record.id} was produced by {record.provenance.producer!r} "
            f"({record.labels.get('evidence_class', 'no evidence class')}); this loop takes "
            f"ECU-command evidence from {TWIN_TOOL!r} only. An engine-response residual "
            "(delivery error, fuel pressure) is not a disagreement with the ECM's logic."
        )
    return record


def _direction(record: Evidence) -> Direction:
    if record.result.status != "evaluated":
        return "unevaluable"
    sign = record.labels.get("residual_sign")
    if sign == "positive":
        return "richer"
    if sign == "negative":
        return "leaner"
    if sign == "zero":
        return "agrees"
    if sign == "mixed":
        return "mixed"
    return "unevaluable"  # evaluated with no qualified residual: nothing to compare


def _open(ledger: MemoryLedger, incident: str) -> Investigation | None:
    found = [
        entry.record
        for entry in ledger.query(MemoryQuery(kinds=("investigation",), incident=incident))
        if isinstance(entry.record, Investigation) and entry.record.status == "open"
    ]
    return found[-1] if found else None


def investigate_command_residual(
    ledger: MemoryLedger,
    evidence_id: str,
    *,
    related: Sequence[str] = (),
    now: datetime | None = None,
) -> str:
    """Open, or retrieve and update, the investigation for one twin record.

    The direction (``richer``/``leaner``/``agrees``, or ``unevaluable``) picks
    the investigation; a later occurrence of the same direction updates it
    (superseding, history kept). ``related`` names other records it was
    reasoned beside (e.g. an engine-response investigation of the same drive);
    they are provenance only, never this investigation's observations. Returns
    the investigation id.
    """
    record = _twin_evidence(ledger, evidence_id)
    direction = _direction(record)
    incident = command_incident(direction)
    existing = _open(ledger, incident)
    ids = list(dict.fromkeys([*(existing.observations if existing else ()), evidence_id]))
    records = [_twin_evidence(ledger, ev) for ev in ids]
    current = [r for r in records if r.result.status == "evaluated" and _direction(r) == direction]
    hypotheses: tuple[Hypothesis, ...] = ()
    if direction in _WORDS:
        # Competing explanations of one observation: none is credited with
        # the evidence, so none is ranked above the others.
        hypotheses = tuple(Hypothesis(label=label) for label in _hypotheses(_WORDS[direction]))
    elif direction == "agrees":
        hypotheses = (Hypothesis(label=_AGREES, supporting=tuple(r.id for r in current)),)
    blockers = sorted(
        {
            b
            for r in records
            for b in r.labels.get("blockers", "unknown").split(",")
            if b not in ("none", "unknown")
        }
    )
    unknowns = [ENGINE_HEALTH_UNKNOWN]
    unknowns.extend(
        f"logic on the final-command path is unresolved under blocker {name}" for name in blockers
    )
    if any(r.labels.get("blockers", "unknown") == "unknown" for r in records):
        unknowns.append(
            "the logic between the lookup and the command is unknown for a refused "
            "evaluation: no table was resolved, so no blocker could be named"
        )
    if any(r.labels.get("uncovered_logic", "none") not in ("none", "unknown") for r in records):
        unknowns.append(
            "the application model lists uncovered logic (named on each evidence record's "
            "uncovered_logic label) that the reproduction does not compute"
        )
    next_measurements: list[str] = []
    if direction in _WORDS:
        next_measurements = [
            "observe the final-command words in the same read as the command, so omitted "
            "logic is seen rather than inferred",
            "read the lookup inputs and the command back to back in one window, so timing "
            "cannot explain the difference",
            "compare the active RAM target table with the library slot the twin read, so "
            "provenance cannot explain the difference",
        ]
    first = records[0]
    investigation = Investigation(
        vehicle=record.vehicle,
        provenance=Provenance(
            producer=INVESTIGATION_TOOL,
            producer_kind="tool",
            version=INVESTIGATION_TOOL_VERSION,
            code_version=code_version(sys.modules[__name__]),
            derived_from=tuple(dict.fromkeys([*ids, *related])),
        ),
        conditions=Conditions(regime=first.conditions.regime, vehicle_state=None),
        valid_from=min(r.valid_from for r in records),
        title=_TITLES[direction],
        question=_QUESTIONS[direction],
        status="open",
        incident=incident,
        observations=tuple(ids),
        hypotheses=hypotheses,
        unknowns=tuple(dict.fromkeys(unknowns)),
        next_measurements=tuple(next_measurements),
        narrative=(
            "Opened from target-phi twin evidence (ECU computation: the ECM's command against "
            "its reproduced logic). It is not engine response and not a delivery error."
            + (f" Reasoned beside: {', '.join(related)}." if related else "")
        ),
        supersedes=() if existing is None else (existing.id,),
        supersession_reason=(
            None if existing is None else "re-evaluated with a later twin occurrence"
        ),
    )
    return ledger.append(investigation, now=now).id
