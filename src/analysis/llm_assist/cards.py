"""Structured-card contract for the optional LLM-assist tier (Task S.1,
docs/roadmap/llm-assist-and-compute-roadmap.md).

This module is schema only — no producer. Sequencing step 1 of the roadmap
("build the seams now, commit to no model hosting yet") is explicit that the
contract itself is the deliverable; an actual LLM call that fills these
models in is future work (Sequencing step 2+, gated on the measurement pass
in D6). Nothing here imports torch or any LLM library, and nothing here runs
on the capture hot path — see ``src/analysis/llm_assist/__init__.py`` for the
import-isolation boundary (Task S.4) this package exists to hold.

Two card shapes, matching the roadmap's feature set:

* :class:`DifferentialCard` — case 4 (structured fault-hypothesis
  generation): ranked hypotheses, each backed by supporting/refuting
  evidence references. The "Diagnostic safety discipline" risk section is
  explicit that a 4B-class model *will* sometimes be confidently wrong about
  causation, and that this is only tolerable because the output is a
  differential the operator verifies against the referenced evidence, never
  an assertion. ``DifferentialCard.framing`` enforces that "hypotheses, not
  verdicts" disclaimer at the schema level (see its docstring) rather than
  trusting every producer to include it.
* :class:`NarrativeCard` — cases 1/2/6 (session narrative reports,
  cross-session pattern recognition, fuel-map narration): sectioned prose
  plus the source-session references a narrative claim traces back to.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "HYPOTHESES_NOT_VERDICTS_FRAMING",
    "DiagnosticHypothesis",
    "DifferentialCard",
    "EvidenceReference",
    "LlmAssistCard",
    "NarrativeCard",
    "NarrativeSection",
    "card_from_dict",
    "card_to_dict",
]

#: The mandatory disclaimer the roadmap's "Diagnostic safety discipline"
#: section requires on every diagnostic output. Fixed centrally so its
#: wording can't drift between producers/versions.
HYPOTHESES_NOT_VERDICTS_FRAMING = (
    "These are ranked hypotheses to weigh against the evidence shown, not a "
    "diagnosis or a repair instruction. Verify against the referenced "
    "evidence (and a real diagnostic procedure) before acting on any of them."
)


class EvidenceReference(BaseModel):
    """One piece of evidence a hypothesis cites, for or against.

    Free-text ``description`` is mandatory (a hypothesis with no explainable
    evidence isn't a differential, it's a guess); the rest is provenance so
    the operator can find the underlying data — a channel/PID, a session, a
    timestamp within it, or a free-form ``detail`` (e.g. a computed value)
    when none of the structured fields apply.
    """

    model_config = ConfigDict(extra="forbid")

    description: str
    channel: str | None = None
    session: str | None = None
    timestamp: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvidenceReference:
        return cls.model_validate(data)


class DiagnosticHypothesis(BaseModel):
    """One ranked hypothesis in a :class:`DifferentialCard`.

    ``rank`` is 1-indexed, most-likely first (a differential is ordered;
    that ordering is part of the contract, not incidental). At least one
    supporting-evidence reference is required — an unsupported hypothesis
    has nothing for the operator to verify against and shouldn't be emitted;
    refuting evidence is optional (not every candidate has a counter-signal
    on record yet).
    """

    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    label: str
    #: 0-1 producer-reported confidence. Optional — a differential's value
    #: is the ranking + evidence, not a false-precision probability, so a
    #: producer that can't calibrate one is allowed to omit it.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    supporting_evidence: list[EvidenceReference] = Field(min_length=1)
    refuting_evidence: list[EvidenceReference] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiagnosticHypothesis:
        return cls.model_validate(data)


class DifferentialCard(BaseModel):
    """Ranked differential diagnosis (case 4).

    ``framing`` carries the mandatory "hypotheses, not verdicts" disclaimer
    (see :data:`HYPOTHESES_NOT_VERDICTS_FRAMING`). It is locked, not merely
    defaulted: the validator below overwrites whatever a caller/producer
    supplies with the canonical text, so the disclaimer can't be omitted,
    reworded, or silently dropped by a future producer — this is the
    schema-level enforcement of the roadmap's "deliberate safety/trust
    framing choice, not decoration" requirement.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    card_type: Literal["differential"] = "differential"
    generated_at: str
    #: The session this card was generated from/for, when there is one
    #: (e.g. a post-drive review); ``None`` for a cross-session card (case 2).
    session: str | None = None
    hypotheses: list[DiagnosticHypothesis] = Field(min_length=1)
    framing: str = HYPOTHESES_NOT_VERDICTS_FRAMING
    #: Producer identity (e.g. a model name/version), for provenance. Always
    #: ``None`` today — no producer exists yet (Task S.1's whole point).
    model_name: str | None = None

    @field_validator("framing", mode="before")
    @classmethod
    def _lock_framing(cls, _value: object) -> str:
        return HYPOTHESES_NOT_VERDICTS_FRAMING

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DifferentialCard:
        return cls.model_validate(data)


class NarrativeSection(BaseModel):
    """One heading/body pair in a :class:`NarrativeCard`."""

    model_config = ConfigDict(extra="forbid")

    heading: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NarrativeSection:
        return cls.model_validate(data)


class NarrativeCard(BaseModel):
    """Sectioned narrative report (cases 1/2/6).

    ``source_sessions`` is mandatory (at least one) — a narrative claim with
    no session to trace back to isn't reviewable, the same "verify, don't
    trust" discipline as the differential card's evidence references, just
    at the report level instead of per-hypothesis.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    card_type: Literal["narrative"] = "narrative"
    generated_at: str
    title: str
    sections: list[NarrativeSection] = Field(min_length=1)
    source_sessions: list[str] = Field(min_length=1)
    model_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NarrativeCard:
        return cls.model_validate(data)


#: Either card shape. A discriminated union keyed on ``card_type`` — the
#: dashboard endpoint (S.2) and the card store (both consumers) hold a list
#: of these without caring which producer emitted which.
LlmAssistCard = DifferentialCard | NarrativeCard


def card_to_dict(card: LlmAssistCard) -> dict[str, Any]:
    return card.to_dict()


def card_from_dict(data: dict[str, Any]) -> LlmAssistCard:
    """Dispatch on ``card_type``; raises :class:`ValueError` for anything else."""
    card_type = data.get("card_type")
    if card_type == "differential":
        return DifferentialCard.from_dict(data)
    if card_type == "narrative":
        return NarrativeCard.from_dict(data)
    raise ValueError(f"unknown llm-assist card_type: {card_type!r}")
