"""E01.5: deterministic investigation cards, every number from a record."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import src.evidence.cards as cards_module
from src.evidence import (
    Conditions,
    Hypothesis,
    MemoryLedger,
    MemoryReferenceError,
    Outcome,
    Quantity,
    Result,
    Retraction,
    investigation_card,
    render_text,
)
from tests.evidence_builders import (
    T0,
    WOT,
    allowed_tokens,
    cards_investigation,
    interventions_prediction,
    memory_evidence,
    memory_operator,
    memory_vehicle,
    retrieval_occurrence,
    unresolved_numbers,
)

SESSION = "2026-09-20T180000"


def _ledger(tmp_path: Path) -> MemoryLedger:
    return MemoryLedger(tmp_path / "memory.jsonl", vehicle="truck")


def test_a_complete_card_renders_every_number_from_a_record(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    observed = ledger.append(retrieval_occurrence(SESSION, T0), now=T0 + timedelta(hours=1))
    against = ledger.append(
        retrieval_occurrence(
            SESSION,
            T0 + timedelta(minutes=5),
            subject="compare_channels:scale-check",
            values={"slope": Quantity(value=1.0003, unit="1")},
            labels={"scale_verdict": "stable"},
        ),
        now=T0 + timedelta(hours=2),
    )
    investigation = ledger.append(
        cards_investigation(
            observations=(observed.id,),
            hypotheses=(
                Hypothesis(label="sample skew between the reads", supporting=(observed.id,)),
                Hypothesis(label="wrong scale", refuting=(against.id,), status="refuted"),
            ),
            unknowns=("whether the broadcast frame is filtered",),
            next_measurements=("pair at a steady cruise",),
        ),
        now=T0 + timedelta(hours=3),
    )
    card = investigation_card(ledger, investigation.id)
    assert card.evidence_state == "evaluated"
    assert card.what_happened[0].values[0].record_id == observed.id
    for view in (card.what_happened[0], card.hypotheses[1].refuting[0]):
        record = ledger.resolve_evidence(view.id).record
        for value in view.values:
            assert value.value == record.values[value.name].value  # type: ignore[union-attr]
    assert card.alternatives == ("sample skew between the reads (open)",)
    assert "pair at a steady cruise" in card.next_measurements
    assert "ECU program: unknown" in card.unknowns
    text = render_text(card)
    assert render_text(investigation_card(ledger, investigation.id)) == text  # deterministic
    assert "difference_bias = 0.4 rpm" in text
    assert unresolved_numbers(text, allowed_tokens(card, ledger)) == []
    # Negative control: the checker does catch a number no record holds.
    assert unresolved_numbers(text + " lean by 3.7 percent", allowed_tokens(card, ledger)) == [
        "3.7"
    ]
    assert "normal" not in text.lower()


def test_absent_input_is_unevaluable_never_normal(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    missing = ledger.append(
        retrieval_occurrence(
            SESSION,
            T0,
            values={},
            result=Result(
                status="unevaluable",
                reason="missing_input",
                detail="obd:COMMANDED_EQUIV_RATIO was never captured: tuning-did disables Mode 01",
            ),
        ),
        now=T0 + timedelta(hours=1),
    )
    investigation = ledger.append(
        cards_investigation(
            title="Delivery error during the tuning drive",
            question="did fuel delivery track the command",
            observations=(missing.id,),
        ),
        now=T0 + timedelta(hours=2),
    )
    card = investigation_card(ledger, investigation.id)
    assert card.evidence_state == "unevaluable"
    view = card.what_happened[0]
    assert view.values == () and view.residual is None
    assert any("capture can:ECM_RPM, obd:RPM" in item for item in card.next_measurements)
    assert any("unevaluable (missing_input)" in item for item in card.unknowns)
    text = render_text(card)
    assert "UNEVALUABLE (missing_input)" in text
    assert text.lower().count("normal") == text.count("not a normal result")
    assert unresolved_numbers(text, allowed_tokens(card, ledger)) == []


def test_an_investigation_with_no_evidence_says_so(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    investigation = ledger.append(
        cards_investigation(hypotheses=(Hypothesis(label="a vacuum leak"),)),
        now=T0,
    )
    card = investigation_card(ledger, investigation.id)
    assert card.evidence_state == "no_evidence"
    assert "measure to test 'a vacuum leak'" in card.next_measurements
    assert "NO EVIDENCE" in render_text(card)


def test_conflicting_evidence_is_named(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for_it = ledger.append(retrieval_occurrence(SESSION, T0), now=T0 + timedelta(hours=1))
    against = ledger.append(
        retrieval_occurrence("2026-09-22T180000", T0 + timedelta(days=2)),
        now=T0 + timedelta(days=2),
    )
    investigation = ledger.append(
        cards_investigation(
            hypotheses=(
                Hypothesis(
                    label="pump droop under load",
                    status="supported",
                    supporting=(for_it.id,),
                    refuting=(against.id,),
                ),
            ),
        ),
        now=T0 + timedelta(days=3),
    )
    card = investigation_card(ledger, investigation.id)
    assert card.evidence_state == "conflicting"
    assert card.hypotheses[0].conflicting
    assert for_it.id in card.conflicts[0] and against.id in card.conflicts[0]
    text = render_text(card)
    assert "CONFLICTING" in text
    assert "RECURRENCE: 2 occurrences" in text  # the same subject over two drives
    assert unresolved_numbers(text, allowed_tokens(card, ledger)) == []


def test_a_retraction_is_visible_and_as_of_shows_the_earlier_belief(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    finding = ledger.append(retrieval_occurrence(SESSION, T0), now=T0 + timedelta(hours=1))
    investigation = ledger.append(
        cards_investigation(
            observations=(finding.id,),
            hypotheses=(
                Hypothesis(
                    label="byte is calculated load", status="supported", supporting=(finding.id,)
                ),
            ),
        ),
        now=T0 + timedelta(hours=2),
    )
    retraction = ledger.append(
        Retraction(
            vehicle=memory_vehicle(),
            provenance=memory_operator(),
            valid_from=T0,
            retracts=(finding.id,),
            reason="the low byte of a rpm counter: means matched by accident",
        ),
        now=T0 + timedelta(days=1),
    )
    card = investigation_card(ledger, investigation.id)
    assert card.evidence_state == "retracted"
    assert card.what_happened[0].standing == "retracted"
    assert card.what_happened[0].retracted_by == retraction.id
    assert any(retraction.id in line for line in card.retractions)
    assert any("marked supported" in line for line in card.conflicts)
    text = render_text(card)
    assert f"RETRACTED by {retraction.id}" in text
    then = investigation_card(ledger, investigation.id, as_of=T0 + timedelta(hours=3))
    assert then.evidence_state == "evaluated"
    assert then.what_happened[0].standing == "current"
    assert "as believed at" in render_text(then)


def test_predictions_and_outcomes_render_their_order_and_floor(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    investigation = ledger.append(
        cards_investigation(conditions=Conditions(regime=WOT, vehicle_state="engine_running")),
        now=T0,
    )
    prediction = ledger.append(
        interventions_prediction(investigation=investigation.id), now=T0 + timedelta(hours=1)
    )
    measured = ledger.append(
        memory_evidence(
            conditions=Conditions(regime=WOT, vehicle_state="engine_running"),
            valid_from=T0 + timedelta(days=1),
            valid_to=T0 + timedelta(days=1, minutes=5),
        ),
        now=T0 + timedelta(days=1),
    )
    ledger.append(
        Outcome(
            vehicle=memory_vehicle(),
            provenance=memory_operator(),
            conditions=Conditions(regime=WOT, vehicle_state="engine_running"),
            valid_from=T0 + timedelta(days=1),
            prediction=prediction.id,
            intervention=None,
            disposition="measured_change",
            evidence=(measured.id,),
            result=Result(status="evaluated", residual=Quantity(value=-3.4, unit="percent")),
        ),
        now=T0 + timedelta(days=2),
    )
    card = investigation_card(ledger, investigation.id)
    (view,) = card.predictions
    assert view.outcomes[0].verdict == "confirmed"
    assert view.detectable is not None and view.noise_floor is not None
    assert view.detectable.value == pytest.approx(2 * view.noise_floor.value)
    text = render_text(card)
    assert "-> CONFIRMED" in text
    assert unresolved_numbers(text, allowed_tokens(card, ledger)) == []


def test_investigation_prose_cannot_state_numbers() -> None:
    with pytest.raises(ValidationError, match="cannot state numbers"):
        cards_investigation(title="STFT +8% at idle")
    with pytest.raises(ValidationError, match="cannot state numbers"):
        Hypothesis(label="pump drops 2psi")
    with pytest.raises(ValidationError, match="cannot state numbers"):
        cards_investigation(unknowns=("whether 3000rpm is the knee",))
    assert cards_investigation(title="P0171 on B1 with obd:SHORT_FUEL_TRIM_1").title


def test_a_non_investigation_id_is_refused(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    evidence = ledger.append(memory_evidence())
    with pytest.raises(MemoryReferenceError, match="not an investigation"):
        investigation_card(ledger, evidence.id)


def test_cards_never_call_a_model() -> None:
    """The template imports only the evidence package, pydantic and stdlib."""
    tree = ast.parse(Path(cards_module.__file__).read_text())
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    third_party = {m for m in modules if not m.startswith(("src.evidence", "__future__"))}
    assert third_party <= {"datetime", "typing", "pydantic"}


def test_the_cli_reads_queries_and_cards_from_the_scoped_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from src.config import CanaryConfig
    from src.evidence import memory_ledger_for
    from src.vehicle.profiles import resolve_profile_for_tooling, scope_config
    from tests.configs import write_isolated_config
    from tools.memory import main

    config_path = write_isolated_config(tmp_path, "example")
    base = CanaryConfig.from_yaml(config_path)
    config = scope_config(base, resolve_profile_for_tooling(base, "truck"))
    ledger = memory_ledger_for(config)
    finding = ledger.append(retrieval_occurrence(SESSION, T0), now=T0 + timedelta(hours=1))
    investigation = ledger.append(
        cards_investigation(observations=(finding.id,)), now=T0 + timedelta(hours=2)
    )
    common = ["--config", str(config_path), "--profile", "truck"]
    assert main([*common, "query", "--kind", "evidence", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["record"]["id"] for row in rows] == [finding.id]
    assert main([*common, "card", investigation.id]) == 0
    assert capsys.readouterr().out == render_text(investigation_card(ledger, investigation.id))
    assert main([*common, "query", "--text", "nothing-matches-this"]) == 0
    assert "0 record(s)" in capsys.readouterr().out


def test_a_retracted_prediction_and_outcome_stay_on_the_card_labelled(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    investigation = ledger.append(
        cards_investigation(conditions=Conditions(regime=WOT, vehicle_state="engine_running")),
        now=T0,
    )
    prediction = ledger.append(
        interventions_prediction(investigation=investigation.id), now=T0 + timedelta(hours=1)
    )
    measured = ledger.append(
        memory_evidence(
            conditions=Conditions(regime=WOT, vehicle_state="engine_running"),
            valid_from=T0 + timedelta(days=1),
            valid_to=T0 + timedelta(days=1, minutes=5),
        ),
        now=T0 + timedelta(days=1),
    )
    outcome = ledger.append(
        Outcome(
            vehicle=memory_vehicle(),
            provenance=memory_operator(),
            conditions=Conditions(regime=WOT, vehicle_state="engine_running"),
            valid_from=T0 + timedelta(days=1),
            prediction=prediction.id,
            intervention=None,
            disposition="measured_change",
            evidence=(measured.id,),
            result=Result(status="evaluated", residual=Quantity(value=-3.4, unit="percent")),
        ),
        now=T0 + timedelta(days=2),
    )
    retraction = ledger.append(
        Retraction(
            vehicle=memory_vehicle(),
            provenance=memory_operator(),
            valid_from=T0,
            retracts=(prediction.id, outcome.id),
            reason="predicted the wrong quantity",
        ),
        now=T0 + timedelta(days=3),
    )
    card = investigation_card(ledger, investigation.id)
    (view,) = card.predictions
    assert view.standing == "retracted" and view.retracted_by == retraction.id
    assert view.retraction_reason == "predicted the wrong quantity"
    assert view.outcomes[0].standing == "retracted"
    assert any(prediction.id in line for line in card.retractions)
    text = render_text(card)
    assert f"{prediction.id}" in text
    assert text.count(f"RETRACTED by {retraction.id}") == 2
    before = investigation_card(ledger, investigation.id, as_of=T0 + timedelta(days=2))
    assert before.predictions[0].standing == "current"
