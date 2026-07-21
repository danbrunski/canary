"""Tests for the LLM-assist structured-card contract (Task S.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.analysis.llm_assist.cards import (
    HYPOTHESES_NOT_VERDICTS_FRAMING,
    DiagnosticHypothesis,
    DifferentialCard,
    EvidenceReference,
    NarrativeCard,
    NarrativeSection,
    card_from_dict,
    card_to_dict,
)
from src.analysis.llm_assist.store import LlmAssistCardStore


def _differential_card(**overrides: object) -> DifferentialCard:
    defaults: dict[str, object] = {
        "generated_at": "2026-07-20T18:00:00+00:00",
        "session": "2026-07-20T170000",
        "hypotheses": [
            DiagnosticHypothesis(
                rank=1,
                label="possible MAF sensor drift",
                confidence=0.4,
                supporting_evidence=[
                    EvidenceReference(
                        description="STFT bank 1 trending +8% at cruise",
                        channel="obd:SHORT_FUEL_TRIM_1",
                    )
                ],
            )
        ],
    }
    defaults.update(overrides)
    return DifferentialCard(**defaults)  # type: ignore[arg-type]


def _narrative_card(**overrides: object) -> NarrativeCard:
    defaults: dict[str, object] = {
        "generated_at": "2026-07-20T18:00:00+00:00",
        "title": "Session summary",
        "sections": [NarrativeSection(heading="Fuel trims", body="Bank 1 STFT held within 3%.")],
        "source_sessions": ["2026-07-20T170000"],
    }
    defaults.update(overrides)
    return NarrativeCard(**defaults)  # type: ignore[arg-type]


class TestDifferentialCard:
    def test_framing_is_locked_regardless_of_input(self) -> None:
        card = _differential_card(framing="a producer tried to change this")
        assert card.framing == HYPOTHESES_NOT_VERDICTS_FRAMING

    def test_round_trip(self) -> None:
        card = _differential_card()
        payload = card_to_dict(card)
        assert payload["card_type"] == "differential"
        restored = card_from_dict(payload)
        assert isinstance(restored, DifferentialCard)
        assert restored == card

    def test_requires_at_least_one_hypothesis(self) -> None:
        with pytest.raises(ValidationError):
            _differential_card(hypotheses=[])

    def test_hypothesis_requires_supporting_evidence(self) -> None:
        with pytest.raises(ValidationError):
            DiagnosticHypothesis(rank=1, label="unsupported guess", supporting_evidence=[])

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            DiagnosticHypothesis(
                rank=1,
                label="x",
                confidence=1.5,
                supporting_evidence=[EvidenceReference(description="d")],
            )


class TestNarrativeCard:
    def test_round_trip(self) -> None:
        card = _narrative_card()
        payload = card_to_dict(card)
        assert payload["card_type"] == "narrative"
        restored = card_from_dict(payload)
        assert isinstance(restored, NarrativeCard)
        assert restored == card

    def test_requires_at_least_one_source_session(self) -> None:
        with pytest.raises(ValidationError):
            _narrative_card(source_sessions=[])

    def test_requires_at_least_one_section(self) -> None:
        with pytest.raises(ValidationError):
            _narrative_card(sections=[])


def test_card_from_dict_rejects_unknown_card_type() -> None:
    with pytest.raises(ValueError, match="unknown llm-assist card_type"):
        card_from_dict({"card_type": "verdict"})


def test_llm_assist_card_store_round_trip(tmp_path: Path) -> None:
    store = LlmAssistCardStore(tmp_path / "cards.jsonl")
    assert store.cards() == []
    differential = _differential_card()
    narrative = _narrative_card()
    store.append(differential)
    store.append(narrative)
    cards = store.cards()
    assert cards == [differential, narrative]
