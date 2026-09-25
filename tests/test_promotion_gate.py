"""Champion/challenger promotion gate (Task 3.3, online-training-strategy.md)."""

from __future__ import annotations

from pathlib import Path

from src.models.promotion_gate import (
    compute_golden_divergence,
    evaluate_promotion_gate,
    promote_challenger,
)
from src.models.training_ledger import TrainingLedger, TrainingRunRecord

_CLEAN_DISPOSITIONS = {
    "groups": [],
    "reject_counts": {},
    "totals": {"reject": 0, "accept_recenter": 0, "quarantine": 0, "accept": 100},
}


def _report(*, recall: float, per_hour: float) -> dict:
    return {
        "table": [
            {
                "detector": "isolation_forest",
                "fault_kind": "spike",
                "runs": 5,
                "detected": round(recall * 5),
                "recall": recall,
                "precision": 0.9,
                "median_time_to_detect_seconds": 1.0,
                "alerts_per_hour_clean": per_hour,
            }
        ],
        "nominal_fp": {
            "isolation_forest": {"alerts": 1, "rows": 100, "hours": 1.0, "per_hour": per_hour}
        },
    }


def _record(
    run_id: str,
    *,
    calibration: str | None = None,
    dispositions: dict | None = _CLEAN_DISPOSITIONS,
    report_path: str | None = None,
) -> TrainingRunRecord:
    metrics: dict = {}
    if calibration is not None:
        metrics["manifest_extras"] = {"calibration": calibration}
    return TrainingRunRecord(
        run_id=run_id,
        model_type="isolation_forest",
        created_at="2026-07-19T00:00:00Z",
        status="succeeded",
        metrics=metrics,
        dispositions=dispositions,
        detector_benchmark_report_path=report_path,
    )


def _dispositions_with_quarantine_fraction(fraction: float, *, total: int = 1000) -> dict:
    quarantine = int(total * fraction)
    return {
        "groups": [],
        "reject_counts": {},
        "totals": {
            "reject": 0,
            "accept_recenter": 0,
            "quarantine": quarantine,
            "accept": total - quarantine,
        },
    }


# ---------------------------------------------------------------------------
# evaluate_promotion_gate — pass / fail cases
# ---------------------------------------------------------------------------


def test_gate_passes_when_every_check_clears() -> None:
    golden = _record("golden")
    candidate = _record("candidate")
    result = evaluate_promotion_gate(
        candidate,
        golden,
        candidate_report=_report(recall=1.0, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
    )
    assert result.eligible
    assert result.reasons == ()


def test_gate_hard_rejects_in_sample_calibration_regardless_of_everything_else() -> None:
    golden = _record("golden")
    candidate = _record("candidate", calibration="in_sample")
    result = evaluate_promotion_gate(
        candidate,
        golden,
        candidate_report=_report(recall=1.0, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
    )
    assert not result.eligible
    assert any("in_sample" in reason for reason in result.reasons)
    assert result.checks["calibration"]["ok"] is False


def test_gate_rejects_missing_dispositions_contamination_unverifiable() -> None:
    golden = _record("golden")
    candidate = _record("candidate", dispositions=None)
    result = evaluate_promotion_gate(
        candidate,
        golden,
        candidate_report=_report(recall=1.0, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
    )
    assert not result.eligible
    assert result.checks["contamination_sanity"]["ok"] is False


def test_gate_rejects_high_reject_fraction() -> None:
    golden = _record("golden")
    dirty = {
        "groups": [],
        "reject_counts": {},
        "totals": {"reject": 90, "accept_recenter": 0, "quarantine": 0, "accept": 10},
    }
    candidate = _record("candidate", dispositions=dirty)
    result = evaluate_promotion_gate(
        candidate,
        golden,
        candidate_report=_report(recall=1.0, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
    )
    assert not result.eligible
    assert "contamination ceiling" in result.checks["contamination_sanity"]["reason"]


def test_gate_rejects_when_no_golden_declared() -> None:
    candidate = _record("candidate")
    result = evaluate_promotion_gate(candidate, None)
    assert not result.eligible
    assert result.checks["golden"]["ok"] is False


def test_gate_rejects_recall_regression_vs_golden() -> None:
    golden = _record("golden")
    candidate = _record("candidate")
    result = evaluate_promotion_gate(
        candidate,
        golden,
        # 0.5 recall vs golden's 1.0 is far beyond the default 15% tolerance.
        candidate_report=_report(recall=0.5, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
    )
    assert not result.eligible
    assert result.checks["recall_non_regression"]["ok"] is False


def test_gate_recall_within_tolerance_passes() -> None:
    golden = _record("golden")
    candidate = _record("candidate")
    result = evaluate_promotion_gate(
        candidate,
        golden,
        candidate_report=_report(recall=0.9, per_hour=1.0),  # within 15% of golden's 1.0
        golden_report=_report(recall=1.0, per_hour=1.0),
    )
    assert result.checks["recall_non_regression"]["ok"] is True


def test_gate_rejects_fp_over_budget() -> None:
    golden = _record("golden")
    candidate = _record("candidate")
    result = evaluate_promotion_gate(
        candidate,
        golden,
        candidate_report=_report(recall=1.0, per_hour=50.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
        fp_budget_per_hour=5.0,
    )
    assert not result.eligible
    assert result.checks["fp_budget"]["ok"] is False


def test_gate_rejects_holdout_score_disagreement_even_when_lower() -> None:
    """Agreement is symmetric — a challenger whose held-out alert rate
    collapses far below golden's also fails (a possible degenerate/collapsed
    scoring distribution, not obviously "better")."""
    golden = _record("golden")
    candidate = _record("candidate")
    result = evaluate_promotion_gate(
        candidate,
        golden,
        candidate_report=_report(recall=1.0, per_hour=0.01),
        golden_report=_report(recall=1.0, per_hour=1.0),
        agreement_tolerance=0.5,
    )
    assert not result.eligible
    assert result.checks["holdout_score_agreement"]["ok"] is False


def test_gate_missing_benchmark_reports_is_ineligible() -> None:
    golden = _record("golden", report_path="/does/not/exist.json")
    candidate = _record("candidate", report_path="/does/not/exist.json")
    result = evaluate_promotion_gate(candidate, golden)
    assert not result.eligible
    assert result.checks["benchmark"]["ok"] is False


# ---------------------------------------------------------------------------
# Quarantine-fraction trend (Task 3.6, 2026-07-20 review)
# ---------------------------------------------------------------------------


def test_gate_blocks_on_sustained_quarantine_rise() -> None:
    golden = _record("golden")
    candidate = _record("candidate", dispositions=_dispositions_with_quarantine_fraction(0.30))
    result = evaluate_promotion_gate(
        candidate,
        golden,
        candidate_report=_report(recall=1.0, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
        recent_quarantine_fractions=[0.05, 0.08, 0.10],  # rising, then candidate keeps rising
    )
    assert not result.eligible
    assert result.checks["quarantine_fraction_trend"]["ok"] is False
    assert any("quarantine fraction trend" in reason for reason in result.reasons)


def test_gate_quarantine_trend_passes_when_history_is_not_rising() -> None:
    """A high-but-noisy (not trending) quarantine fraction doesn't trip the
    check — this is a *sustained rise* detector, not a flat ceiling (that's
    `contamination_sanity`'s job)."""
    golden = _record("golden")
    candidate = _record("candidate", dispositions=_dispositions_with_quarantine_fraction(0.30))
    result = evaluate_promotion_gate(
        candidate,
        golden,
        candidate_report=_report(recall=1.0, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
        recent_quarantine_fractions=[0.30, 0.05, 0.28],  # not monotonically rising
    )
    assert result.checks["quarantine_fraction_trend"]["ok"] is True
    assert result.eligible


def test_gate_quarantine_trend_passes_open_with_too_little_history() -> None:
    golden = _record("golden")
    candidate = _record("candidate", dispositions=_dispositions_with_quarantine_fraction(0.9))
    result = evaluate_promotion_gate(
        candidate,
        golden,
        candidate_report=_report(recall=1.0, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
        recent_quarantine_fractions=[0.1],  # only one prior run -- no trend to establish
    )
    assert result.checks["quarantine_fraction_trend"]["ok"] is True


def test_promote_challenger_reads_recent_quarantine_fractions_from_ledger(
    tmp_path: Path,
) -> None:
    """`promote_challenger` (not just `evaluate_promotion_gate` directly)
    sources the trend history from the ledger itself — recent scheduled
    runs of the same model_type, oldest to newest, older than the
    candidate."""
    ledger = TrainingLedger(tmp_path / "runs.jsonl")
    ledger.append(_record("golden"))
    ledger.set_golden("golden")

    for run_id, created_at, fraction in [
        ("run1", "2026-07-01T00:00:00Z", 0.05),
        ("run2", "2026-07-05T00:00:00Z", 0.08),
        ("run3", "2026-07-10T00:00:00Z", 0.10),
    ]:
        rec = _record(run_id, dispositions=_dispositions_with_quarantine_fraction(fraction))
        ledger.append(rec.model_copy(update={"created_at": created_at}))

    candidate = _record("candidate", dispositions=_dispositions_with_quarantine_fraction(0.5))
    ledger.append(candidate.model_copy(update={"created_at": "2026-07-15T00:00:00Z"}))

    result, updated = promote_challenger(
        ledger,
        "candidate",
        candidate_report=_report(recall=1.0, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
    )
    assert not result.eligible
    assert updated is None
    assert result.checks["quarantine_fraction_trend"]["ok"] is False
    assert ledger.get("candidate").publish_status == "unpublished"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# promote_challenger — the explicit gated action + rollback
# ---------------------------------------------------------------------------


def test_promote_challenger_sets_active_only_when_eligible(tmp_path: Path) -> None:
    ledger = TrainingLedger(tmp_path / "runs.jsonl")
    ledger.append(_record("golden"))
    ledger.append(_record("candidate"))
    ledger.set_golden("golden")

    result, updated = promote_challenger(
        ledger,
        "candidate",
        candidate_report=_report(recall=1.0, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
    )
    assert result.eligible
    assert updated is not None
    assert updated.publish_status == "active"


def test_promote_challenger_refuses_and_does_not_mutate_ledger_on_gate_failure(
    tmp_path: Path,
) -> None:
    ledger = TrainingLedger(tmp_path / "runs.jsonl")
    ledger.append(_record("golden"))
    ledger.append(_record("candidate", calibration="in_sample"))
    ledger.set_golden("golden")

    result, updated = promote_challenger(
        ledger,
        "candidate",
        candidate_report=_report(recall=1.0, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
    )
    assert not result.eligible
    assert updated is None
    assert ledger.get("candidate").publish_status == "unpublished"  # type: ignore[union-attr]


def test_promote_challenger_unknown_run_id(tmp_path: Path) -> None:
    ledger = TrainingLedger(tmp_path / "runs.jsonl")
    result, updated = promote_challenger(ledger, "does-not-exist")
    assert not result.eligible
    assert updated is None


def test_promote_then_rollback_via_set_publish_status(tmp_path: Path) -> None:
    ledger = TrainingLedger(tmp_path / "runs.jsonl")
    ledger.append(_record("golden"))
    ledger.append(_record("candidate"))
    ledger.set_golden("golden")

    _, updated = promote_challenger(
        ledger,
        "candidate",
        candidate_report=_report(recall=1.0, per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
    )
    assert updated is not None and updated.publish_status == "active"

    rolled_back = ledger.set_publish_status("candidate", "rolled_back")
    assert rolled_back is not None
    assert rolled_back.publish_status == "rolled_back"


# ---------------------------------------------------------------------------
# compute_golden_divergence (Task 3.4)
# ---------------------------------------------------------------------------


def test_compute_golden_divergence_reports_relative_delta() -> None:
    golden = _record("golden")
    current = _record("current")
    divergence = compute_golden_divergence(
        golden,
        current,
        golden_report=_report(recall=1.0, per_hour=2.0),
        current_report=_report(recall=1.0, per_hour=3.0),
    )
    block = divergence["per_detector"]["isolation_forest"]
    assert block["golden_per_hour"] == 2.0
    assert block["current_per_hour"] == 3.0
    assert block["delta_per_hour"] == 1.0
    assert block["relative_delta"] == 0.5


def test_compute_golden_divergence_handles_zero_golden_rate() -> None:
    golden = _record("golden")
    current = _record("current")
    divergence = compute_golden_divergence(
        golden,
        current,
        golden_report=_report(recall=1.0, per_hour=0.0),
        current_report=_report(recall=1.0, per_hour=0.0),
    )
    block = divergence["per_detector"]["isolation_forest"]
    assert block["relative_delta"] == 0.0


# ---------------------------------------------------------------------------
# R04.5 — a withheld detector fails, never passes silently
# ---------------------------------------------------------------------------


def _two_detector_report(*, sequence_recall: float | None, sequence_per_hour: float | None) -> dict:
    report = _report(recall=1.0, per_hour=1.0)
    if sequence_recall is not None:
        report["table"].append(
            {
                "detector": "sequence",
                "fault_kind": "spike",
                "runs": 5,
                "detected": 5,
                "recall": sequence_recall,
            }
        )
    block: dict = {"alerts": 1, "rows": 100, "hours": 1.0}
    if sequence_per_hour is not None:
        block["per_hour"] = sequence_per_hour
    report["nominal_fp"]["sequence"] = block
    return report


def test_gate_fails_a_candidate_whose_detector_was_withheld_where_golden_was_graded() -> None:
    """detector_benchmark emits no row for a withheld detector; the gate used to
    ``continue`` past the missing recall and pass "recall non-regression"."""
    result = evaluate_promotion_gate(
        _record("candidate"),
        _record("golden"),
        candidate_report=_two_detector_report(sequence_recall=None, sequence_per_hour=1.0),
        golden_report=_two_detector_report(sequence_recall=1.0, sequence_per_hour=1.0),
    )
    assert not result.eligible
    check = result.checks["recall_non_regression"]
    assert check["ok"] is False
    missing = [cell for cell in check["cells"] if cell.get("missing")]
    assert [(c["detector"], c["fault_kind"]) for c in missing] == [("sequence", "spike")]
    assert any("sequence" in reason and "spike" in reason for reason in result.reasons)


def test_gate_fails_a_candidate_with_no_recorded_false_positive_rate() -> None:
    """A missing ``per_hour`` used to default to 0.0 and pass the FP budget."""
    result = evaluate_promotion_gate(
        _record("candidate"),
        _record("golden"),
        candidate_report=_two_detector_report(sequence_recall=1.0, sequence_per_hour=None),
        golden_report=_two_detector_report(sequence_recall=1.0, sequence_per_hour=1.0),
    )
    assert not result.eligible
    fp = result.checks["fp_budget"]
    assert fp["ok"] is False
    assert any(cell["detector"] == "sequence" and cell.get("missing") for cell in fp["cells"])
    assert result.checks["holdout_score_agreement"]["ok"] is False
    assert any("per_hour" in reason and "sequence" in reason for reason in result.reasons)


def test_gate_fails_a_candidate_missing_a_nominal_detector_the_golden_has() -> None:
    candidate_report = _two_detector_report(sequence_recall=1.0, sequence_per_hour=1.0)
    del candidate_report["nominal_fp"]["sequence"]
    result = evaluate_promotion_gate(
        _record("candidate"),
        _record("golden"),
        candidate_report=candidate_report,
        golden_report=_two_detector_report(sequence_recall=1.0, sequence_per_hour=1.0),
    )
    assert not result.eligible
    assert result.checks["holdout_score_agreement"]["ok"] is False
    assert result.checks["fp_budget"]["ok"] is False


def test_gate_ignores_a_cell_the_golden_never_graded() -> None:
    """Missing on the golden side is not a regression: nothing to compare."""
    result = evaluate_promotion_gate(
        _record("candidate"),
        _record("golden"),
        candidate_report=_two_detector_report(sequence_recall=1.0, sequence_per_hour=1.0),
        golden_report=_report(recall=1.0, per_hour=1.0),
    )
    assert result.eligible, result.reasons


def test_gate_fails_agreement_when_the_golden_recorded_no_rate() -> None:
    """Symmetric with the candidate side: a golden ``per_hour`` that is missing
    used to be compared as 0.0 (so any nonzero candidate "disagreed" and a zero
    one "agreed"); now it fails and says why."""
    result = evaluate_promotion_gate(
        _record("candidate"),
        _record("golden"),
        candidate_report=_two_detector_report(sequence_recall=1.0, sequence_per_hour=0.0),
        golden_report=_two_detector_report(sequence_recall=1.0, sequence_per_hour=None),
    )
    assert not result.eligible
    agreement = result.checks["holdout_score_agreement"]
    assert agreement["ok"] is False
    (cell,) = [c for c in agreement["cells"] if c["detector"] == "sequence"]
    assert cell["missing"] is True and cell["golden_per_hour"] is None
    assert any("golden recorded no held-out per_hour" in r for r in result.reasons)
