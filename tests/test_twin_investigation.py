"""E02.6: the twin's evidence published through E01 and linked to an investigation.

Synthetic sessions from ``tests/test_target_phi.py`` (one complete steady
override tuple) are stored as session directories and minted by the real
:func:`~src.analysis.twin_evidence.evaluate_and_mint`; the card is the real
E01.5 template.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.analysis.delivery_investigation import NotEngineResponseError, detect_discrepancy
from src.analysis.target_phi import ResolvedTable, TwinRefusalError
from src.analysis.twin_evidence import TwinSessionIdentity, evaluate_and_mint
from src.analysis.twin_investigation import (
    ENGINE_HEALTH_UNKNOWN,
    NotEcuCommandError,
    command_incident,
    investigate_command_residual,
)
from src.evidence import MemoryLedger, investigation_card, render_text
from src.evidence.records import Evidence, Investigation
from src.storage.parquet_writer import TELEMETRY_SCHEMA
from src.vehicle.tune_revisions import load_tune_revisions
from tests.test_evidence_cards import _allowed_tokens, _unresolved_numbers
from tests.test_evidence_memory import _evidence, _vehicle
from tests.test_target_phi import IMAGE_SHA, PROFILE, _definition, _session, _table

SLUG = "nissan-xterra-2008"
REVISION = "ZS44A-20260920"


def _store(root: Path, frame: pd.DataFrame, name: str) -> Path:
    """``frame`` as a stored session (the writer's schema)."""
    session = root / name
    session.mkdir(parents=True)
    rows = [
        {
            **row,
            "timestamp": row["timestamp"].floor("us").to_pydatetime(),
            "value_number": None if pd.isna(row["value_number"]) else row["value_number"],
            "value_bool": None,
            "payload": None,
        }
        for row in frame.to_dict("records")
    ]
    table = pa.Table.from_pylist(rows, schema=TELEMETRY_SCHEMA)
    pq.write_table(table, session / "telemetry-00000.parquet")
    return session


def _qualified() -> ResolvedTable:
    """The committed table with the application model's blockers lifted and
    the constants marked checked: what a fully verified path would give."""
    definition, _ = _definition()
    base = _table(definition)
    return ResolvedTable(
        **{
            **base.__dict__,
            "final_command_constants_checked": True,
            "application": dataclasses.replace(base.application, blockers=()),
        }
    )


def _mint(
    ledger: MemoryLedger,
    tmp_path: Path,
    *,
    table: ResolvedTable | None = None,
    name: str = "2026-10-04T210000",
    refuse: str | None = None,
    **session: Any,
) -> Evidence:
    definition, sha = _definition()
    chosen = table or _table(definition)

    def table_for_slot(slot: int) -> ResolvedTable:
        if refuse is not None:
            raise TwinRefusalError(refuse, "synthetic refusal")
        assert slot == chosen.slot
        return chosen

    minted = evaluate_and_mint(
        ledger,
        _store(tmp_path / "sessions", _session(**session), name),
        definition=definition,
        definition_sha256=sha,
        identity=TwinSessionIdentity(
            profile=SLUG,
            vin=None,
            calibration=REVISION,
            calibration_source="registry",
            image_sha256=IMAGE_SHA,
        ),
        program_id=chosen.application.program_id,
        table_for_slot=table_for_slot,
    )
    (evidence_id,) = minted["evidence"]
    record = ledger.resolve_evidence(evidence_id).record
    assert isinstance(record, Evidence)
    return record


@pytest.fixture
def ledger(tmp_path: Path) -> MemoryLedger:
    return MemoryLedger(tmp_path / "memory.jsonl", vehicle=SLUG)


def test_twin_evidence_names_its_class_image_and_the_logic_it_omits(
    ledger: MemoryLedger, tmp_path: Path
) -> None:
    record = _mint(ledger, tmp_path)
    labels = record.labels
    assert labels["computation"] == labels["evidence_class"] == "ecu_command"
    assert labels["engine_health"] == "not_assessed"
    assert (labels["image_sha256"], labels["slot"], labels["revision"]) == (
        IMAGE_SHA,
        "1",
        REVISION,
    )
    # The image reference resolves to the flash history's revision.
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    assert registry.match_image(labels["image_sha256"]) == (REVISION, None)
    blockers = labels["blockers"].split(",")
    assert {"final_command_path_unverified", "ram_copy_unverified"} <= set(blockers)
    omitted = labels["omitted_logic"].split(",")
    assert {"final_command_word", "enrichment_multiplier", "target_afr_ram_copy"} <= set(omitted)
    assert "fuel_comp_fill_terms" in labels["uncovered_logic"].split(",")
    # Blocked: unevaluable with no residual, the eligible pass kept as a count.
    assert (record.result.status, record.result.reason) == ("unevaluable", "unvalidated")
    assert record.result.residual is None and labels["residual_sign"] == "none"
    assert record.values["override_pass"].value == 1
    assert "qualified_residuals" not in record.values


def test_a_positive_residual_reads_as_the_ecm_commanding_richer(
    ledger: MemoryLedger, tmp_path: Path
) -> None:
    record = _mint(ledger, tmp_path, table=_qualified(), word=19072 + 2)
    assert record.result.status == "evaluated"
    assert record.result.residual is not None
    assert record.result.residual.value == 2 / 16384 and record.result.residual.unit == "1"
    assert record.labels["residual_sign"] == "positive"
    assert record.values["positive_qualified_residuals"].value == 1
    assert record.values["max_qualified_residual_phi"].value == 2 / 16384

    investigation_id = investigate_command_residual(ledger, record.id)
    investigation = ledger.get(investigation_id)
    assert isinstance(investigation, Investigation)
    assert investigation.incident == command_incident("richer")
    assert "richer" in investigation.title
    labels = [h.label for h in investigation.hypotheses]
    assert len(labels) == 3
    assert "omits" in labels[0] and "timing" in labels[1] and "provenance" in labels[2]
    # Competing explanations, unranked: the record is the observation and
    # supports none of them over the others.
    assert investigation.observations == (record.id,)
    assert all(h.supporting == () and h.refuting == () for h in investigation.hypotheses)
    assert all(h.status == "open" for h in investigation.hypotheses)
    # Not relabelled: no hypothesis is a fault or a delivery error.
    assert not any("fault" in label or "delivery" in label for label in labels)
    assert ENGINE_HEALTH_UNKNOWN in investigation.unknowns

    card = investigation_card(ledger, investigation_id)
    (view,) = card.what_happened
    assert view.residual is not None and view.residual.record_id == record.id
    assert all(value.record_id == record.id for value in view.values)
    assert view.labels["image_sha256"] == IMAGE_SHA
    text = render_text(card)
    assert "ECM commanded richer" in text
    # Every number on the card is read from a record. Beside the shared
    # template check, the names the card prints from records (vehicle,
    # program, label keys) are identifiers, not numbers.
    names = {card.vehicle, card.program_id or "", *view.labels}
    assert _unresolved_numbers(text, _allowed_tokens(card, ledger) | names) == []


def test_a_zero_residual_makes_no_engine_health_claim(ledger: MemoryLedger, tmp_path: Path) -> None:
    record = _mint(ledger, tmp_path, table=_qualified())
    assert record.result.residual is not None and record.result.residual.value == 0.0
    assert record.labels["residual_sign"] == "zero"
    assert record.labels["engine_health"] == "not_assessed"
    card = investigation_card(ledger, investigate_command_residual(ledger, record.id))
    (hypothesis,) = card.hypotheses
    assert "not an engine-health claim" in hypothesis.label
    assert ENGINE_HEALTH_UNKNOWN in card.unknowns
    text = render_text(card).lower()
    for claim in ("healthy", "normal", "no fault"):
        assert claim not in text


def test_unavailable_coverage_stays_unevaluable(ledger: MemoryLedger, tmp_path: Path) -> None:
    blocked = _mint(ledger, tmp_path)
    uncovered = _mint(ledger, tmp_path, table=_qualified(), name="2026-10-04T220000", load_at=None)
    assert (uncovered.result.status, uncovered.result.reason) == ("unevaluable", "uncovered")
    first = investigate_command_residual(ledger, blocked.id)
    second = investigate_command_residual(ledger, uncovered.id)
    # The same kind of empty: one investigation, updated, history kept.
    assert ledger.get(second).supersedes == (first,)
    card = investigation_card(ledger, second)
    assert card.incident == command_incident("unevaluable")
    assert card.hypotheses == ()
    assert card.evidence_state == "unevaluable"
    assert all(view.residual is None for view in card.what_happened)
    text = render_text(card)
    assert "UNEVALUABLE (unvalidated)" in text and "UNEVALUABLE (uncovered)" in text
    assert "not a normal result" in text


def test_command_residuals_and_engine_response_never_cross(
    ledger: MemoryLedger, tmp_path: Path
) -> None:
    twin = _mint(ledger, tmp_path, table=_qualified(), word=19072 + 2)
    with pytest.raises(NotEngineResponseError, match="ECU-computation"):
        detect_discrepancy(ledger, twin.id)
    measured = ledger.append(_evidence(vehicle=_vehicle(SLUG)))
    with pytest.raises(NotEcuCommandError, match="ECU-command evidence"):
        investigate_command_residual(ledger, measured.id)
    # A related engine-response record is provenance, never an observation.
    investigation = ledger.get(investigate_command_residual(ledger, twin.id, related=[measured.id]))
    assert isinstance(investigation, Investigation)
    assert investigation.observations == (twin.id,)
    assert measured.id in investigation.provenance.derived_from


def test_a_refusal_names_the_image_it_could_not_read(ledger: MemoryLedger, tmp_path: Path) -> None:
    record = _mint(ledger, tmp_path, refuse="table_unverified")
    assert record.labels["image_sha256"] == IMAGE_SHA
    assert record.labels["evidence_class"] == "ecu_command"
    assert (record.result.status, record.result.reason) == ("unevaluable", "unvalidated")
    # No table was resolved: the omitted logic is unknown, never "none".
    for label in ("blockers", "omitted_logic", "uncovered_logic"):
        assert record.labels[label] == "unknown"
    investigation = ledger.get(investigate_command_residual(ledger, record.id))
    assert isinstance(investigation, Investigation)
    assert not any("blocker unknown" in item for item in investigation.unknowns)
    assert any("no blocker could be named" in item for item in investigation.unknowns)


def test_a_pass_within_the_tolerance_agrees_and_never_reads_as_richer(
    ledger: MemoryLedger, tmp_path: Path
) -> None:
    """+1 Q14 word is inside the twin's one-word tolerance: a pass. It is
    agreement, kept as its raw value, never a "richer" direction."""
    record = _mint(ledger, tmp_path, table=_qualified(), word=19072 + 1)
    assert record.values["override_pass"].value == 1
    assert record.labels["residual_sign"] == "zero"
    assert record.values["zero_qualified_residuals"].value == 1
    assert record.values["positive_qualified_residuals"].value == 0
    assert record.values["tolerance_phi"].value == 1 / 16384
    assert record.values["max_qualified_residual_phi"].value == 1 / 16384
    assert record.result.residual is not None and record.result.residual.value == 1 / 16384
    investigation = ledger.get(investigate_command_residual(ledger, record.id))
    assert isinstance(investigation, Investigation)
    assert investigation.incident == command_incident("agrees")


def test_a_later_occurrence_of_the_same_direction_updates_its_investigation(
    ledger: MemoryLedger, tmp_path: Path
) -> None:
    first = _mint(ledger, tmp_path, table=_qualified(), word=19072 + 2)
    opened = investigate_command_residual(ledger, first.id)
    later = _mint(ledger, tmp_path, table=_qualified(), name="2026-10-05T210000", word=19072 + 3)
    updated = investigate_command_residual(ledger, later.id)
    record = ledger.get(updated)
    assert isinstance(record, Investigation)
    assert record.supersedes == (opened,)
    assert record.observations == (first.id, later.id)
    assert all(h.supporting == () for h in record.hypotheses)
    leaner = _mint(ledger, tmp_path, table=_qualified(), name="2026-10-06T210000", word=19072 - 3)
    other = ledger.get(investigate_command_residual(ledger, leaner.id))
    assert isinstance(other, Investigation)
    assert other.incident == command_incident("leaner") and other.supersedes == ()


def test_the_brain_twin_job_links_each_minted_record_to_its_investigation(tmp_path: Path) -> None:
    from src.control.brain_jobs import canonical_params, run_brain_job
    from src.evidence import memory_ledger_for
    from tests.test_brain_jobs import SESSION, scoped_config, write_session

    config = scoped_config(tmp_path)
    write_session(config)
    params = canonical_params(config, "twin_session_evaluation", {"session": SESSION})
    results = tmp_path / "results"
    results.mkdir()

    def run(job_id: str) -> dict[str, Any]:
        run_brain_job(
            "twin_session_evaluation",
            params,
            config,
            job_id=job_id,
            results_dir=results,
            address_space_bytes=None,
        )
        result: dict[str, Any] = json.loads((results / f"{job_id}.json").read_text())
        return result

    first = run("job-a")
    (evidence_id,) = first["evidence"]
    (investigation_id,) = first["investigations"]
    ledger = memory_ledger_for(config)
    investigation = ledger.get(investigation_id)
    assert isinstance(investigation, Investigation)
    # October 4 is blocked: the "could not compare" investigation, no hypotheses.
    assert investigation.incident == command_incident("unevaluable")
    assert investigation.observations == (evidence_id,) and investigation.hypotheses == ()
    # A re-run (a restart) never duplicates. Record ids are content hashes over
    # the producing source files and input files (``code_version``, the
    # catalog hash), which other sessions edit in this shared tree: between
    # the two runs the evidence can legitimately be *recomputed* (superseding
    # the first), so the ids are compared only as that contract states them.
    again = run("job-b")
    (again_evidence,) = again["evidence"]
    (again_investigation,) = again["investigations"]
    if again_evidence == evidence_id:
        # Same inputs and code: the ledger's content dedup returns both ids.
        assert again_investigation == investigation_id
    else:
        assert ledger.get(again_evidence).supersedes == (evidence_id,)
    current_evidence = [
        e.record.id for e in ledger.query(kinds=("evidence",)) if e.record.subject.endswith(":tol1")
    ]
    current_investigations = [e.record for e in ledger.query(kinds=("investigation",)) if e.current]
    assert current_evidence == [again_evidence]
    (current,) = current_investigations
    assert isinstance(current, Investigation)
    assert current.id == again_investigation
    assert current.incident == command_incident("unevaluable")
    assert again_evidence in current.observations


def test_a_changed_input_recomputes_the_record_and_keeps_one_investigation(
    tmp_path: Path,
) -> None:
    """The case a shared tree can produce between two job runs, forced: the
    same session minted under different inputs supersedes, and the
    investigation is re-stated once over it, not duplicated."""
    ledger = MemoryLedger(tmp_path / "memory.jsonl", vehicle=SLUG)
    first = _mint(ledger, tmp_path)
    opened = investigate_command_residual(ledger, first.id)
    definition, _ = _definition()
    recomputed = evaluate_and_mint(
        ledger,
        tmp_path / "sessions" / "2026-10-04T210000",
        definition=definition,
        definition_sha256="0" * 64,  # another definition: a recompute
        identity=TwinSessionIdentity(
            profile=SLUG,
            vin=None,
            calibration=REVISION,
            calibration_source="registry",
            image_sha256=IMAGE_SHA,
        ),
        program_id=first.vehicle.program_id,
        table_for_slot=lambda slot: _table(definition),
    )
    (second,) = recomputed["evidence"]
    assert second != first.id and ledger.get(second).supersedes == (first.id,)
    updated = investigate_command_residual(ledger, second)
    assert ledger.get(updated).supersedes == (opened,)
    assert [e.record.id for e in ledger.query(kinds=("investigation",)) if e.current] == [updated]
    # Stated again with nothing changed: the same id, no new record.
    assert investigate_command_residual(ledger, second) == updated
