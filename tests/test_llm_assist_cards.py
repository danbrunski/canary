"""Tests for the LLM-assist structured-card contract (Task S.1; evidence ids
since S.5 / E01.2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.analysis.llm_assist.cards import (
    HISTORICAL_NARRATIVE_NOTICE,
    HYPOTHESES_NOT_VERDICTS_FRAMING,
    DiagnosticHypothesis,
    DifferentialCard,
    EvidenceCitation,
    EvidenceResolutionError,
    NarrativeCard,
    NarrativeSection,
    UnverifiedHistoricalCard,
    card_from_dict,
    card_to_dict,
    resolve_card,
)
from src.analysis.llm_assist.store import LlmAssistCardStore
from src.dashboard.api import create_app
from src.evidence import (
    Conditions,
    Evidence,
    MemoryLedger,
    Provenance,
    Quality,
    Quantity,
    Result,
    SourceRef,
    VehicleIdentity,
    memory_ledger_for,
)
from tests.configs import isolated_config, scoped_test_config

SESSION = "2026-07-20T170000"
T0 = datetime(2026, 7, 20, 17, 0, tzinfo=UTC)

#: A card as the first contract (llm-assist-card/1) wrote it.
LEGACY_DIFFERENTIAL: dict[str, Any] = {
    "card_type": "differential",
    "generated_at": "2026-07-20T18:00:00+00:00",
    "session": SESSION,
    "hypotheses": [
        {
            "rank": 1,
            "label": "possible MAF sensor drift",
            "confidence": 0.4,
            "supporting_evidence": [
                {
                    "description": "STFT bank 1 trending +8% at cruise",
                    "channel": "obd:SHORT_FUEL_TRIM_1",
                    "session": None,
                    "timestamp": None,
                    "detail": None,
                }
            ],
            "refuting_evidence": [],
        }
    ],
    "framing": HYPOTHESES_NOT_VERDICTS_FRAMING,
    "model_name": None,
}


def _evidence(vehicle: str = "truck", stft: float = 8.0) -> Evidence:
    return Evidence(
        vehicle=VehicleIdentity(profile=vehicle, vin=None, program_id=None, calibration=None),
        provenance=Provenance(
            producer="trim_trend", producer_kind="tool", version="1", code_version="abc"
        ),
        conditions=Conditions(regime="cruise", vehicle_state=None),
        valid_from=T0,
        valid_to=T0 + timedelta(minutes=20),
        subject=f"trim_trend:{SESSION}:obd:SHORT_FUEL_TRIM_1:cruise",
        sources=(
            SourceRef(
                session=SESSION,
                window_start=T0,
                window_end=T0 + timedelta(minutes=20),
                channels=("obd:SHORT_FUEL_TRIM_1",),
                n_samples=1200,
            ),
        ),
        input_hashes={"session_files": "d" * 64},
        policy={"window_s": 1200},
        values={"stft_trend": Quantity(value=stft, unit="%")},
        quality=Quality(n_observations=1200, timestamp_basis="acquired"),
        result=Result(status="evaluated"),
    )


@pytest.fixture
def ledger(tmp_path: Path) -> MemoryLedger:
    return MemoryLedger(tmp_path / "memory.jsonl", vehicle="truck")


@pytest.fixture
def evidence_id(ledger: MemoryLedger) -> str:
    return ledger.append(_evidence()).id


def _differential_card(evidence_id: str, **overrides: object) -> DifferentialCard:
    defaults: dict[str, object] = {
        "vehicle": "truck",
        "generated_at": "2026-07-20T18:00:00+00:00",
        "session": SESSION,
        "hypotheses": [
            DiagnosticHypothesis(
                rank=1,
                label="possible MAF sensor drift",
                confidence=0.4,
                supporting_evidence=[
                    EvidenceCitation(
                        evidence_id=evidence_id,
                        note="STFT bank one trending rich at cruise",
                        quoted={"stft_trend": 8.0},
                    )
                ],
            )
        ],
    }
    defaults.update(overrides)
    return DifferentialCard(**defaults)  # type: ignore[arg-type]


def _narrative_card(evidence_id: str, **overrides: object) -> NarrativeCard:
    defaults: dict[str, object] = {
        "vehicle": "truck",
        "generated_at": "2026-07-20T18:00:00+00:00",
        "title": "Session summary",
        "sections": [
            NarrativeSection(
                heading="Fuel trims",
                body="Bank one STFT trended rich.",
                evidence=[EvidenceCitation(evidence_id=evidence_id)],
            )
        ],
        "source_sessions": [SESSION],
    }
    defaults.update(overrides)
    return NarrativeCard(**defaults)  # type: ignore[arg-type]


class TestDifferentialCard:
    def test_framing_is_locked_regardless_of_input(self) -> None:
        card = _differential_card("ev-x", framing="a producer tried to change this")
        assert card.framing == HYPOTHESES_NOT_VERDICTS_FRAMING

    def test_round_trip(self) -> None:
        card = _differential_card("ev-x")
        payload = card_to_dict(card)
        assert payload["card_type"] == "differential"
        assert payload["schema"] == "llm-assist-card/2"
        restored = card_from_dict(payload)
        assert isinstance(restored, DifferentialCard)
        assert restored == card

    def test_requires_at_least_one_hypothesis(self) -> None:
        with pytest.raises(ValidationError):
            _differential_card("ev-x", hypotheses=[])

    def test_hypothesis_requires_supporting_evidence(self) -> None:
        with pytest.raises(ValidationError):
            DiagnosticHypothesis(rank=1, label="unsupported guess", supporting_evidence=[])

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            DiagnosticHypothesis(
                rank=1,
                label="x",
                confidence=1.5,
                supporting_evidence=[EvidenceCitation(evidence_id="ev-x")],
            )

    @pytest.mark.parametrize(
        "text",
        [
            "STFT bank 1 trending",
            "+8% at cruise",
            "lean by 3.5",
            "-2 counts",
            "3000rpm",
            "5psi droop",
            "10ms",
            "80kPa",
            "3.5x",
            "1e3 counts",
            "rpm:3000",
            "x3",
            "2026-07-20",
        ],
    )
    def test_prose_cannot_state_a_number(self, text: str) -> None:
        with pytest.raises(ValidationError, match="cannot state numbers"):
            EvidenceCitation(evidence_id="ev-x", note=text)
        with pytest.raises(ValidationError, match="cannot state numbers"):
            DiagnosticHypothesis(
                rank=1, label=text, supporting_evidence=[EvidenceCitation(evidence_id="ev-x")]
            )

    def test_identifiers_with_digits_are_not_numbers(self) -> None:
        note = "obd:SHORT_FUEL_TRIM_1 against did:0x1206 on B1, mem:ECM_FUEL_COMP_X_INPUT and P0171"
        assert EvidenceCitation(evidence_id="ev-x", note=note).note == note


class TestNarrativeCard:
    def test_round_trip(self) -> None:
        card = _narrative_card("ev-x")
        restored = card_from_dict(card_to_dict(card))
        assert isinstance(restored, NarrativeCard)
        assert restored == card

    def test_requires_at_least_one_source_session(self) -> None:
        with pytest.raises(ValidationError):
            _narrative_card("ev-x", source_sessions=[])

    def test_requires_at_least_one_section(self) -> None:
        with pytest.raises(ValidationError):
            _narrative_card("ev-x", sections=[])

    def test_requires_a_citation(self) -> None:
        with pytest.raises(ValidationError, match="cites at least one"):
            _narrative_card("ev-x", sections=[NarrativeSection(heading="h", body="b")])

    def test_body_cannot_state_a_number(self) -> None:
        with pytest.raises(ValidationError):
            NarrativeSection(heading="Fuel trims", body="Bank 1 STFT held within 3%.")


def test_card_from_dict_rejects_unknown_card_type() -> None:
    with pytest.raises(ValueError, match="unknown llm-assist card_type"):
        card_from_dict({"card_type": "verdict"})


# -- resolution (S.5 / E01.2) -------------------------------------------------


def test_a_card_citing_a_minted_id_resolves_with_values_from_the_record(
    ledger: MemoryLedger, evidence_id: str
) -> None:
    resolution = resolve_card(_differential_card(evidence_id), ledger)
    assert resolution.verification == "verified", resolution.problems
    rendered = resolution.evidence[evidence_id]
    assert rendered["values"]["stft_trend"] == {"value": 8.0, "unit": "%", "uncertainty": None}
    assert rendered["tool"] == "trim_trend"
    assert rendered["superseded_at"] is None
    assert resolve_card(_narrative_card(evidence_id), ledger).verification == "verified"


def test_an_invented_id_is_rejected(ledger: MemoryLedger, evidence_id: str) -> None:
    resolution = resolve_card(_differential_card("ev-000000000000000000000000"), ledger)
    assert resolution.verification == "rejected"
    assert "was not minted" in resolution.problems[0]


def test_a_wrong_vehicle_id_is_rejected(tmp_path: Path, ledger: MemoryLedger) -> None:
    other = MemoryLedger(ledger.path, vehicle="other")
    foreign = other.append(_evidence(vehicle="other")).id
    resolution = resolve_card(_differential_card(foreign), ledger)
    assert resolution.verification == "rejected"
    assert any("belongs to vehicle 'other'" in p for p in resolution.problems)
    card_for_other = _differential_card(foreign, vehicle="other")
    assert resolve_card(card_for_other, ledger).verification == "rejected"


def test_a_value_differing_from_its_record_is_rejected(
    ledger: MemoryLedger, evidence_id: str
) -> None:
    card = _differential_card(evidence_id)
    card.hypotheses[0].supporting_evidence[0].quoted["stft_trend"] = 12.0
    resolution = resolve_card(card, ledger)
    assert resolution.verification == "rejected"
    assert "differs from the recorded 8.0 %" in resolution.problems[0]
    card.hypotheses[0].supporting_evidence[0].quoted = {"ltft": 1.0}
    assert "has no value 'ltft'" in resolve_card(card, ledger).problems[0]


def test_a_session_no_cited_evidence_read_is_rejected(
    ledger: MemoryLedger, evidence_id: str
) -> None:
    resolution = resolve_card(_differential_card(evidence_id, session="2026-07-21T090000"), ledger)
    assert resolution.verification == "rejected"
    assert "not a source of any cited evidence" in resolution.problems[0]


def test_a_superseded_citation_still_resolves_and_says_so(
    ledger: MemoryLedger, evidence_id: str
) -> None:
    newer = ledger.append(_evidence(stft=9.0)).id
    resolution = resolve_card(_differential_card(evidence_id), ledger)
    assert resolution.verification == "verified"
    assert resolution.evidence[evidence_id]["superseded_by"] == [newer]


def test_the_store_refuses_a_card_that_does_not_resolve(
    tmp_path: Path, ledger: MemoryLedger, evidence_id: str
) -> None:
    store = LlmAssistCardStore(tmp_path / "cards.jsonl")
    with pytest.raises(EvidenceResolutionError):
        store.append(_differential_card("ev-000000000000000000000000"), ledger=ledger)
    assert store.cards() == []
    differential = _differential_card(evidence_id)
    narrative = _narrative_card(evidence_id)
    store.append(differential, ledger=ledger)
    store.append(narrative, ledger=ledger)
    assert store.cards() == [differential, narrative]


def test_a_first_contract_card_reads_back_as_unverified_historical_narrative(
    tmp_path: Path, ledger: MemoryLedger
) -> None:
    path = tmp_path / "cards.jsonl"
    path.write_text(json.dumps(LEGACY_DIFFERENTIAL) + "\n")
    (card,) = LlmAssistCardStore(path).cards()
    assert isinstance(card, UnverifiedHistoricalCard)
    assert card.notice == HISTORICAL_NARRATIVE_NOTICE
    assert card_to_dict(card) == LEGACY_DIFFERENTIAL
    resolution = resolve_card(card, ledger)
    assert resolution.verification == "unverified_historical"
    assert resolution.evidence == {}


def test_the_route_renders_resolved_rejected_and_historical_cards(tmp_path: Path) -> None:
    config = scoped_test_config(isolated_config(tmp_path, llm_assist={"enabled": True}))
    ledger = memory_ledger_for(config)
    evidence_id = ledger.append(_evidence(vehicle=ledger.vehicle)).id
    store = LlmAssistCardStore(config.llm_assist.cards_path)
    store.append(_differential_card(evidence_id, vehicle=ledger.vehicle), ledger=ledger)
    with config.llm_assist.cards_path.open("a") as handle:
        handle.write(json.dumps(LEGACY_DIFFERENTIAL) + "\n")
        forged = card_to_dict(_differential_card("ev-forged", vehicle=ledger.vehicle))
        handle.write(json.dumps(forged) + "\n")

    client = TestClient(create_app(config, anomalies_dir=tmp_path / "anomalies"))
    body = client.get("/analysis/llm-assist/cards").json()
    assert body["enabled"] is True
    verified, historical, rejected = body["cards"]
    assert verified["verification"] == "verified"
    assert verified["evidence"][evidence_id]["values"]["stft_trend"]["value"] == 8.0
    assert historical["verification"] == "unverified_historical"
    assert historical["card"]["notice"] == HISTORICAL_NARRATIVE_NOTICE
    assert rejected["verification"] == "rejected"
    assert rejected["problems"]


def test_the_route_rejects_evidence_cards_without_a_scoped_ledger(tmp_path: Path) -> None:
    config = isolated_config(tmp_path, llm_assist={"enabled": True})
    config.llm_assist.cards_path.parent.mkdir(parents=True, exist_ok=True)
    config.llm_assist.cards_path.write_text(
        json.dumps(card_to_dict(_differential_card("ev-x"))) + "\n"
    )
    client = TestClient(create_app(config, anomalies_dir=tmp_path / "anomalies"))
    (card,) = client.get("/analysis/llm-assist/cards").json()["cards"]
    assert card["verification"] == "rejected"
    assert "no vehicle-scoped memory ledger" in card["problems"][0]


def test_a_card_citing_retracted_evidence_is_not_plain_verified(
    tmp_path: Path, ledger: MemoryLedger, evidence_id: str
) -> None:
    from src.evidence import Retraction

    retraction = ledger.append(
        Retraction(
            vehicle=VehicleIdentity(profile="truck", vin=None, program_id=None, calibration=None),
            provenance=Provenance(producer="operator", producer_kind="operator", version="n/a"),
            valid_from=T0,
            retracts=(evidence_id,),
            reason="trim trend window straddled a refuel",
        )
    )
    resolution = resolve_card(_differential_card(evidence_id), ledger)
    assert resolution.verification == "retracted"
    assert f"retracted by {retraction.id}" in resolution.problems[0]
    assert resolution.evidence[evidence_id]["retracted_by"] == retraction.id
    with pytest.raises(EvidenceResolutionError, match="retracted"):
        LlmAssistCardStore(tmp_path / "cards.jsonl").append(
            _differential_card(evidence_id), ledger=ledger
        )
