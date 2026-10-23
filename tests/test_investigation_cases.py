"""E01.6: known-answer investigation cases through the real memory path.

Each case in ``fixtures/ground_truth/investigation_cases.yaml`` replays
committed slices of real drives through compare_channels -> MemoryLedger ->
investigation_card (``src.analysis.known_cases``) and must reproduce the
disposition and references history established — and match the
tool-generated snapshot ``investigation_cases.expected.yaml`` (regenerate with
``tools/make_investigation_cases.py --write``). Negative controls mutate one
input and must fail; known defects must still fail until fixed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.analysis.known_cases import (
    EXPECTED_SCHEMA,
    REQUIRED_CATEGORIES,
    CaseFile,
    SliceAbsenceError,
    case_outcome,
    check_case,
    load_case_file,
    run_case,
    run_negative_control,
)
from src.evidence import render_text
from src.evidence.cards import investigation_card
from src.yaml_load import load_yaml

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "ground_truth"
CASES_PATH = FIXTURE_ROOT / "investigation_cases.yaml"
CASES = load_case_file(CASES_PATH)
EXPECTED = load_yaml((FIXTURE_ROOT / "investigation_cases.expected.yaml").read_text())


def _raw() -> dict[str, Any]:
    data: dict[str, Any] = load_yaml(CASES_PATH.read_text())
    return data


@pytest.mark.parametrize("case_id", sorted(CASES.cases))
def test_the_memory_path_reproduces_the_known_answer(case_id: str, tmp_path: Path) -> None:
    run = run_case(CASES, case_id, workdir=tmp_path, fixture_root=FIXTURE_ROOT)
    outcome = case_outcome(run)
    report = check_case(CASES.cases[case_id], outcome, EXPECTED["cases"][case_id])

    assert not report.failures, report.failures
    assert not report.drift, (
        "the chain now says something else than the generated snapshot; review and "
        f"rerun tools/make_investigation_cases.py --write: {report.drift}"
    )
    assert not report.defects_passing, (
        f"a known defect is fixed — drop its known_defect marker: {report.defects_passing}"
    )
    declared = [c for c in CASES.cases[case_id].known if c.known_defect is not None]
    assert len(report.defects_failing) == len(declared)
    # Every card renders as text without a model, from the same ledger.
    for spec in CASES.cases[case_id].cards:
        card = investigation_card(run.ledger, run.records[spec.investigation].id)
        assert card.title in render_text(card)


@pytest.mark.parametrize("control_id", sorted(CASES.negative_controls))
def test_a_negative_control_fails(control_id: str, tmp_path: Path) -> None:
    control = CASES.negative_controls[control_id]
    assert control.synthetic
    if control.raises is not None:
        with pytest.raises(SliceAbsenceError, match="slice dropped it"):
            run_negative_control(CASES, control_id, workdir=tmp_path, fixture_root=FIXTURE_ROOT)
        return
    report = run_negative_control(CASES, control_id, workdir=tmp_path, fixture_root=FIXTURE_ROOT)
    failed = {line.split(" ", 1)[0] for line in report.failures}
    assert set(control.must_fail) <= failed, report.failures
    assert sorted(failed) == EXPECTED["negative_controls"][control_id]["failures"]


def test_the_snapshot_covers_every_case_and_control() -> None:
    assert EXPECTED["schema"] == EXPECTED_SCHEMA
    assert set(EXPECTED["cases"]) == set(CASES.cases)
    assert set(EXPECTED["negative_controls"]) == set(CASES.negative_controls)


def test_every_category_is_covered_by_a_real_case() -> None:
    real = {c for case in CASES.cases.values() if not case.synthetic for c in case.categories}
    assert set(REQUIRED_CATEGORIES) <= real


def test_one_incident_is_one_case() -> None:
    raw = _raw()
    raw["cases"]["copy"] = raw["cases"]["ecm-calc-load-b"]
    with pytest.raises(ValidationError, match="one event is one case"):
        CaseFile.model_validate(raw)


def test_a_recurrence_must_come_from_another_drive() -> None:
    raw = _raw()
    steps = raw["cases"]["absent-commanded-afr"]["steps"]
    steps[1]["drive"] = steps[0]["drive"]
    with pytest.raises(ValidationError, match="repeat a drive"):
        CaseFile.model_validate(raw)


def test_a_step_cannot_name_an_undeclared_incident() -> None:
    raw = _raw()
    raw["cases"]["flash-20260920"]["steps"][0]["incident"] = "never-declared"
    with pytest.raises(ValidationError, match="not declared"):
        CaseFile.model_validate(raw)
    raw = _raw()
    raw["cases"]["flash-20260920"]["steps"][-1]["incident"] = "never-declared"
    with pytest.raises(ValidationError, match="not declared"):
        CaseFile.model_validate(raw)


def test_a_known_answer_reads_tool_output_never_prose() -> None:
    raw = _raw()
    raw["cases"]["flash-20260920"]["known"].append(
        {
            "path": "incidents.flash-zs44a-20260920.narrative",
            "op": "contains",
            "value": "flash",
            "source": "narrative",
        }
    )
    with pytest.raises(ValidationError, match="tool output"):
        CaseFile.model_validate(raw)


def test_narrative_only_incidents_name_their_gap() -> None:
    raw = _raw()
    raw["incidents"]["mode22-session-cycling"]["gaps"] = []
    with pytest.raises(ValidationError, match="names no gap"):
        CaseFile.model_validate(raw)
    uncased = set(CASES.incidents) - {case.incident for case in CASES.cases.values()}
    assert uncased, "the project retractions without inputs are listed as narrative"
    assert all(CASES.incidents[name].gaps for name in uncased)


def test_synthetic_cases_do_not_cover_a_category() -> None:
    raw = _raw()
    raw["cases"]["ecm-calc-load-b"]["synthetic"] = True
    with pytest.raises(ValidationError, match="synthetic cases do not count"):
        CaseFile.model_validate(raw)


def test_fixtures_stay_small() -> None:
    slices = {drive.fixture for drive in CASES.drives.values()}
    for name in slices:
        size = sum(p.stat().st_size for p in (FIXTURE_ROOT / name).glob("*.parquet"))
        assert size < 400 * 1024, (name, size)
