"""Structured-card contract for the optional LLM-assist tier (Task S.1,
docs/roadmap/llm-assist-and-compute-roadmap.md; evidence ids since S.5/E01.2).

This module is schema and resolution only — no producer. Nothing here imports
torch or any LLM library, and nothing here runs on the capture hot path — see
``src/analysis/llm_assist/__init__.py`` for the import-isolation boundary
(Task S.4) this package exists to hold.

Two card shapes, matching the roadmap's feature set:

* :class:`DifferentialCard` — case 4 (structured fault-hypothesis
  generation): ranked hypotheses, each backed by supporting/refuting
  evidence. ``DifferentialCard.framing`` enforces the "hypotheses, not
  verdicts" disclaimer at the schema level.
* :class:`NarrativeCard` — cases 1/2/6 (session narrative reports,
  cross-session pattern recognition, fuel-map narration): sectioned prose,
  each section citing the evidence it rests on.

**Evidence is cited by id, and numbers come from the record (S.5, E01.2).**
The first contract (``llm-assist-card/1``) carried a free-text
``EvidenceReference``: a producer could cite a session or a value no tool
returned and the card still validated. A card now cites
:class:`EvidenceCitation` ids minted by analysis tools on the vehicle's
memory ledger (:mod:`src.evidence`). :func:`resolve_card` rejects an id no
tool minted, an id from another vehicle, and a quoted value that differs from
its record; :func:`numeric text <numeral_in>` in the card's own prose is refused
at validation, so a number can only reach the operator rendered from a
record.

Cards written under the first contract cannot be re-evaluated (their
evidence was prose). :func:`card_from_dict` keeps them as
:class:`UnverifiedHistoricalCard` — readable, explicitly labelled
unverified historical narrative, never presented as resolved evidence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.evidence import (
    Evidence,
    MemoryLedger,
    MemoryReferenceError,
)
from src.evidence.cards import evidence_payload
from src.evidence.text import IDENTIFIER, numeral_in

__all__ = [
    "CARD_SCHEMA",
    "HISTORICAL_NARRATIVE_NOTICE",
    "HYPOTHESES_NOT_VERDICTS_FRAMING",
    "IDENTIFIER",
    "CardResolution",
    "DiagnosticHypothesis",
    "DifferentialCard",
    "EvidenceCitation",
    "EvidenceResolutionError",
    "LlmAssistCard",
    "NarrativeCard",
    "NarrativeSection",
    "StoredCard",
    "UnverifiedHistoricalCard",
    "card_from_dict",
    "card_to_dict",
    "numeral_in",
    "require_resolved",
    "resolve_card",
]

CARD_SCHEMA: Literal["llm-assist-card/2"] = "llm-assist-card/2"

#: The mandatory disclaimer the roadmap's "Diagnostic safety discipline"
#: section requires on every diagnostic output. Fixed centrally so its
#: wording can't drift between producers/versions.
HYPOTHESES_NOT_VERDICTS_FRAMING = (
    "These are ranked hypotheses to weigh against the evidence shown, not a "
    "diagnosis or a repair instruction. Verify against the referenced "
    "evidence (and a real diagnostic procedure) before acting on any of them."
)

#: Shown with every card written before evidence ids existed.
HISTORICAL_NARRATIVE_NOTICE = (
    "Unverified historical narrative: written before cards cited tool-minted "
    "evidence, so none of its claims or numbers can be re-evaluated. Read it as "
    "prose, not as a finding."
)

#: The prose tokenizer is shared with the investigation records and template
#: cards (:mod:`src.evidence.text`): one definition of "a number in prose".


def _no_numerals(value: str, *, what: str) -> str:
    found = numeral_in(value)
    if found is not None:
        raise ValueError(
            f"{what} contains the number {found!r}; card prose cannot state "
            "numbers — cite the evidence record whose values are rendered instead"
        )
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class EvidenceCitation(_Strict):
    """One evidence record a claim cites, by the id its tool returned.

    ``quoted`` is optional: values the producer asserts, by the record's value
    name. Resolution refuses a quoted name the record lacks or a value that
    differs from it; what is displayed is always the record's value.
    """

    evidence_id: str = Field(min_length=1)
    #: Why this evidence matters to the claim, in words (no numbers).
    note: str = ""
    quoted: dict[str, float] = Field(default_factory=dict)

    @field_validator("note")
    @classmethod
    def _note(cls, value: str) -> str:
        return _no_numerals(value, what="evidence note")


class DiagnosticHypothesis(_Strict):
    """One ranked hypothesis in a :class:`DifferentialCard`.

    ``rank`` is 1-indexed, most-likely first. At least one supporting
    citation is required — an unsupported hypothesis has nothing for the
    operator to verify against.
    """

    rank: int = Field(ge=1)
    label: str
    #: 0-1 producer-reported confidence. Optional (see S.7).
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    supporting_evidence: list[EvidenceCitation] = Field(min_length=1)
    refuting_evidence: list[EvidenceCitation] = Field(default_factory=list)

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return _no_numerals(value, what="hypothesis label")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiagnosticHypothesis:
        return cls.model_validate(data)


class DifferentialCard(_Strict):
    """Ranked differential diagnosis (case 4).

    ``framing`` is locked, not merely defaulted: the validator overwrites
    whatever a producer supplies with :data:`HYPOTHESES_NOT_VERDICTS_FRAMING`.
    ``vehicle`` is the profile slug whose memory ledger the citations resolve
    against.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=(), populate_by_name=True)

    schema_: Literal["llm-assist-card/2"] = Field(default=CARD_SCHEMA, alias="schema")
    card_type: Literal["differential"] = "differential"
    vehicle: str = Field(min_length=1)
    generated_at: str
    #: The session this card was generated from/for, when there is one. It
    #: must be a source of at least one cited evidence record.
    session: str | None = None
    hypotheses: list[DiagnosticHypothesis] = Field(min_length=1)
    framing: str = HYPOTHESES_NOT_VERDICTS_FRAMING
    #: Producer identity (e.g. a model name/version), for provenance.
    model_name: str | None = None

    @field_validator("framing", mode="before")
    @classmethod
    def _lock_framing(cls, _value: object) -> str:
        return HYPOTHESES_NOT_VERDICTS_FRAMING

    def citations(self) -> list[EvidenceCitation]:
        return [
            citation
            for hypothesis in self.hypotheses
            for citation in (*hypothesis.supporting_evidence, *hypothesis.refuting_evidence)
        ]

    def claimed_sessions(self) -> list[str]:
        return [self.session] if self.session is not None else []

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DifferentialCard:
        return cls.model_validate(data)


class NarrativeSection(_Strict):
    """One heading/body pair in a :class:`NarrativeCard`, with its evidence."""

    heading: str
    body: str
    evidence: list[EvidenceCitation] = Field(default_factory=list)

    @field_validator("heading", "body")
    @classmethod
    def _prose(cls, value: str) -> str:
        return _no_numerals(value, what="narrative text")


class NarrativeCard(_Strict):
    """Sectioned narrative report (cases 1/2/6).

    At least one section cites evidence, and every ``source_sessions`` entry
    must be a source of some cited record — a session no tool read cannot be
    claimed.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=(), populate_by_name=True)

    schema_: Literal["llm-assist-card/2"] = Field(default=CARD_SCHEMA, alias="schema")
    card_type: Literal["narrative"] = "narrative"
    vehicle: str = Field(min_length=1)
    generated_at: str
    title: str
    sections: list[NarrativeSection] = Field(min_length=1)
    source_sessions: list[str] = Field(min_length=1)
    model_name: str | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        return _no_numerals(value, what="card title")

    @model_validator(mode="after")
    def _cites_something(self) -> NarrativeCard:
        if not any(section.evidence for section in self.sections):
            raise ValueError("a narrative card cites at least one evidence record")
        return self

    def citations(self) -> list[EvidenceCitation]:
        return [citation for section in self.sections for citation in section.evidence]

    def claimed_sessions(self) -> list[str]:
        return list(self.source_sessions)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NarrativeCard:
        return cls.model_validate(data)


#: A card a producer may write: either evidence-citing shape.
LlmAssistCard = DifferentialCard | NarrativeCard


class UnverifiedHistoricalCard(_Strict):
    """A card written under ``llm-assist-card/1`` (free-text evidence).

    Kept verbatim in ``original`` and labelled; it is never resolved, never
    appended anew, and never rendered as a finding.
    """

    card_type: Literal["unverified_historical"] = "unverified_historical"
    original_card_type: str
    generated_at: str | None
    notice: str = HISTORICAL_NARRATIVE_NOTICE
    original: dict[str, Any]

    @field_validator("notice", mode="before")
    @classmethod
    def _lock_notice(cls, _value: object) -> str:
        return HISTORICAL_NARRATIVE_NOTICE


#: Anything the card store can hold.
StoredCard = DifferentialCard | NarrativeCard | UnverifiedHistoricalCard


def card_to_dict(card: StoredCard) -> dict[str, Any]:
    """The stored form. A historical card round-trips as its original line."""
    if isinstance(card, UnverifiedHistoricalCard):
        return dict(card.original)
    return card.to_dict()


def card_from_dict(data: dict[str, Any]) -> StoredCard:
    """Dispatch on ``schema``/``card_type``.

    A line with no ``schema`` predates evidence ids: it loads as an
    :class:`UnverifiedHistoricalCard`. Anything else unknown raises
    :class:`ValueError`.
    """
    card_type = data.get("card_type")
    if card_type not in ("differential", "narrative"):
        raise ValueError(f"unknown llm-assist card_type: {card_type!r}")
    if "schema" not in data:
        generated = data.get("generated_at")
        return UnverifiedHistoricalCard(
            original_card_type=str(card_type),
            generated_at=None if generated is None else str(generated),
            original=dict(data),
        )
    if card_type == "differential":
        return DifferentialCard.from_dict(data)
    return NarrativeCard.from_dict(data)


# -- resolution ---------------------------------------------------------------


class EvidenceResolutionError(ValueError):
    """A card whose citations do not resolve; ``problems`` names each."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class CardResolution:
    """A card with its cited evidence resolved against the vehicle's ledger.

    ``verification`` is ``verified`` (every citation resolved and matched),
    ``retracted`` (every citation resolved, but at least one cited record has
    since been retracted — the claim rests on withdrawn evidence; ``problems``
    names each), ``rejected`` (``problems`` says why) or
    ``unverified_historical``. ``evidence`` maps each resolved id to its
    render payload.
    """

    card: StoredCard
    verification: Literal["verified", "retracted", "rejected", "unverified_historical"]
    problems: list[str] = field(default_factory=list)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification": self.verification,
            "problems": list(self.problems),
            "card": card_to_dict(self.card)
            if not isinstance(self.card, UnverifiedHistoricalCard)
            else self.card.to_dict(),
            "evidence": self.evidence,
        }


def _quoted_matches(quoted: float, recorded: float) -> bool:
    return math.isclose(quoted, recorded, rel_tol=1e-9, abs_tol=1e-12)


def resolve_card(card: StoredCard, ledger: MemoryLedger | None) -> CardResolution:
    """Resolve every citation; never raises (see :func:`require_resolved`).

    ``ledger`` is ``None`` when no vehicle-scoped ledger exists here — every
    evidence card is then rejected, since nothing can be resolved.
    """
    if isinstance(card, UnverifiedHistoricalCard):
        return CardResolution(card=card, verification="unverified_historical")
    if ledger is None:
        return CardResolution(
            card=card,
            verification="rejected",
            problems=["no vehicle-scoped memory ledger: evidence cannot be resolved"],
        )
    problems: list[str] = []
    if card.vehicle != ledger.vehicle:
        problems.append(
            f"card is for vehicle {card.vehicle!r}; this memory ledger is {ledger.vehicle!r}"
        )
    resolved: dict[str, dict[str, Any]] = {}
    sessions: set[str] = set()
    for citation in card.citations():
        try:
            entry = ledger.resolve_evidence(citation.evidence_id)
        except MemoryReferenceError as error:
            problems.append(str(error))
            continue
        record = entry.record
        assert isinstance(record, Evidence)
        for name, quoted in citation.quoted.items():
            value = record.values.get(name)
            if value is None:
                problems.append(f"{citation.evidence_id} has no value {name!r}")
            elif not _quoted_matches(quoted, value.value):
                problems.append(
                    f"{citation.evidence_id}: quoted {name}={quoted!r} differs from the "
                    f"recorded {value.value!r} {value.unit}"
                )
        sessions.update(source.session for source in record.sources)
        resolved[record.id] = evidence_payload(entry)
    for session in card.claimed_sessions():
        if session not in sessions:
            problems.append(f"session {session!r} is not a source of any cited evidence")
    if problems:
        return CardResolution(card=card, verification="rejected", problems=problems)
    retracted = [
        f"{evidence_id} was retracted by {payload['retracted_by']}"
        for evidence_id, payload in resolved.items()
        if payload["retracted_by"] is not None
    ]
    if retracted:
        return CardResolution(
            card=card, verification="retracted", problems=retracted, evidence=resolved
        )
    return CardResolution(card=card, verification="verified", evidence=resolved)


def require_resolved(card: LlmAssistCard, ledger: MemoryLedger) -> CardResolution:
    """:func:`resolve_card`, raising :class:`EvidenceResolutionError` unless verified."""
    resolution = resolve_card(card, ledger)
    if resolution.verification != "verified":
        raise EvidenceResolutionError(resolution.problems or ["card is not verifiable"])
    return resolution
