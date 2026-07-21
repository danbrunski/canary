"""Champion/challenger promotion gate (Task 3.3, online-training-strategy.md).

A scheduled retrain (Task 3.2) always lands ``unpublished`` in the training
ledger (``TrainingRunRecord.publish_status`` already defaults there — nothing
new needed on that side). Promoting one to ``active`` is never automatic
(queue Round-2 pre-made decision: "challenger promotes only on held-out
validation + agreement-with-golden within tolerance; a scheduled retrain can
never auto-``active``"). This module builds the *gate* — the mechanics an
explicit operator action (a CLI flag today, a dashboard action later) calls
through before flipping ``publish_status`` — never the trigger itself.

The gate reuses the existing fault-injection benchmark
(``src/models/synthetic_fault_eval.py`` / ``scripts/detector_benchmark.py``,
already landed) rather than re-deriving detector quality: both the candidate
and the golden run already carry a ``detector_benchmark_report_path`` (the
Task 2.6 regression-gate report `scripts/retrain.py` writes every run), and
this gate diffs the two reports' ``table``/``nominal_fp`` blocks.

Four checks, ANY of which blocks promotion:

1. **Calibration stamp** — an artifact whose manifest is stamped
   ``calibration: in_sample`` (ml-hardening 3.1) is *categorically*
   ineligible, hard-reject (checked first, unconditionally).
2. **Contamination sanity** — the run's disposition summary (Task 1.4/3.3,
   ``TrainingRunRecord.dispositions``) must be present and not dominated by
   physically-impossible (Reject-tier) cells — a proxy for "this artifact
   wasn't quietly trained on garbage the quality gate would have caught".
   Alongside it, a **quarantine-fraction trend** check (Task 3.6, 2026-07-20
   review): the promotion gate's power was otherwise bounded entirely by the
   fault-injection benchmark's *injected* fault families — a novel real
   fault outside that suite can be learned and still pass. Comparing the
   candidate's Quarantine-cell fraction against recent scheduled runs' (from
   the training ledger) gives a direct contamination signal instead: a
   sustained rise blocks promotion and surfaces "window health is shifting",
   independent of golden and independent of the injected-fault suite.
3. **Recall non-regression vs golden** on the fault-injection benchmark —
   the central check ("agrees with golden on a holdout" alone is weak
   against a model that quietly learned the fault; recall on *injected*
   faults is not).
4. **Held-out FP/hr within an absolute budget**, and **holdout score
   agreement with golden within tolerance** (the golden run's own
   nominal false-positive rate is the reference point a *symmetric*
   drift-either-direction check compares against — collapsing to near-zero
   alerts can be as wrong a sign as flooding).

No golden run declared yet (Task 3.4 leaves the real corpus undeclared on
purpose) means the gate cannot evaluate (3) and (4) — it fails closed
(ineligible), not open.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.models.training_ledger import TrainingLedger, TrainingRunRecord

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_AGREEMENT_TOLERANCE",
    "DEFAULT_FP_BUDGET_PER_HOUR",
    "DEFAULT_MAX_QUARANTINE_RELATIVE_RISE",
    "DEFAULT_MAX_REJECT_FRACTION",
    "DEFAULT_QUARANTINE_TREND_WINDOW",
    "DEFAULT_RECALL_TOLERANCE",
    "PromotionGateResult",
    "compute_golden_divergence",
    "evaluate_promotion_gate",
    "promote_challenger",
]

#: Same order of magnitude as `detector_benchmark.DEFAULT_TOLERANCE` (0.15) —
#: this gate's recall-regression check is the same relative-tolerance shape.
DEFAULT_RECALL_TOLERANCE = 0.15
#: Alerts/hour ceiling on the held-out fault-injection benchmark's clean pass.
#: Deliberately generous (a hard sanity ceiling, not a tuned operating
#: threshold) — the real per-detector alert budget lives in
#: `alerts.persistence_*`/fusion config, not here.
DEFAULT_FP_BUDGET_PER_HOUR = 5.0
#: Symmetric holdout-score-agreement band vs golden (fraction of golden's
#: rate) — a challenger whose held-out clean-data alert rate is either much
#: higher (flooding) or much lower (collapsed/degenerate scoring) than golden
#: fails this check.
DEFAULT_AGREEMENT_TOLERANCE = 0.5
#: Contamination-sanity ceiling: reject-tier cells as a fraction of every
#: screened cell. Generous on purpose (a sanity floor, not a quality target —
#: see `disposition_report.py`'s committed real-corpus numbers for what
#: "normal" looks like).
DEFAULT_MAX_REJECT_FRACTION = 0.5
#: How many of the most recent scheduled runs (same model_type, older than
#: the candidate) establish the quarantine-fraction trend baseline. Small on
#: purpose — a handful of runs is enough to see a *sustained* rise; more
#: would smear a genuine recent shift into ancient history.
DEFAULT_QUARANTINE_TREND_WINDOW = 5
#: A candidate's quarantine fraction more than this much above the recent
#: baseline (relative), *and* the recent runs themselves trending upward, is
#: a "sustained rise" — generous like the other sanity checks (this is a
#: drift-detection signal, not a tuned operating threshold).
DEFAULT_MAX_QUARANTINE_RELATIVE_RISE = 0.5


@dataclass(frozen=True)
class PromotionGateResult:
    """JSON-safe verdict + per-check detail, for the CLI/dashboard to render
    and for tests to assert against without string-matching log lines."""

    eligible: bool
    reasons: tuple[str, ...] = ()
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reasons": list(self.reasons), "checks": self.checks}


def _load_report(path_str: str | None) -> dict[str, Any] | None:
    if not path_str:
        return None
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("promotion gate: cannot read benchmark report %s: %s", path, exc)
        return None
    return payload if isinstance(payload, dict) else None


def _calibration_stamp(record: TrainingRunRecord) -> str | None:
    extras = record.metrics.get("manifest_extras")
    if isinstance(extras, Mapping):
        value = extras.get("calibration")
        return str(value) if value is not None else None
    return None


def _contamination_sanity(
    dispositions: Mapping[str, Any] | None, *, max_reject_fraction: float
) -> tuple[bool, str | None]:
    if dispositions is None:
        return False, "no disposition summary recorded for this run; contamination unverifiable"
    totals = dispositions.get("totals", {})
    total_cells = sum(
        int(totals.get(key, 0)) for key in ("reject", "accept_recenter", "quarantine", "accept")
    )
    if total_cells == 0:
        return True, None  # nothing screened, nothing to flag
    reject_fraction = int(totals.get("reject", 0)) / total_cells
    if reject_fraction > max_reject_fraction:
        return (
            False,
            f"reject-cell fraction {reject_fraction:.1%} exceeds the "
            f"{max_reject_fraction:.1%} contamination ceiling",
        )
    return True, None


def _quarantine_fraction(dispositions: Mapping[str, Any] | None) -> float | None:
    """Quarantine-tier cells as a fraction of every screened cell, or
    ``None`` when unavailable/nothing was screened (a run predating the
    disposition pipeline, or a schema-only trainer test) — callers treat
    that as "no signal", not zero."""
    if dispositions is None:
        return None
    totals = dispositions.get("totals", {})
    total_cells = sum(
        int(totals.get(key, 0)) for key in ("reject", "accept_recenter", "quarantine", "accept")
    )
    if total_cells == 0:
        return None
    return int(totals.get("quarantine", 0)) / total_cells


def _quarantine_fraction_trend(
    candidate_fraction: float | None,
    recent_fractions: Sequence[float],
    *,
    max_relative_rise: float,
) -> tuple[bool, str | None, dict[str, Any]]:
    """ "Sustained rise" (Task 3.6, 2026-07-20 review): the recent scheduled
    runs' quarantine fractions are themselves trending upward (each at least
    as high as the one before), *and* the candidate continues that rise by
    more than ``max_relative_rise`` above their average. A single-run blip
    that isn't part of a trend, or too little history to establish one,
    passes open — this is a drift-detection bonus signal on top of the
    hard contamination-sanity ceiling above, not a second copy of it.
    """
    detail: dict[str, Any] = {
        "candidate_fraction": candidate_fraction,
        "recent_fractions": list(recent_fractions),
    }
    if candidate_fraction is None or len(recent_fractions) < 2:
        detail["ok"] = True
        return True, None, detail

    baseline = sum(recent_fractions) / len(recent_fractions)
    rising = all(b >= a for a, b in zip(recent_fractions, recent_fractions[1:], strict=False))
    if baseline <= 0:
        relative_rise = float("inf") if candidate_fraction > 0 else 0.0
    else:
        relative_rise = (candidate_fraction - baseline) / baseline
    sustained_rise = rising and relative_rise > max_relative_rise

    detail["baseline_fraction"] = baseline
    detail["relative_rise"] = relative_rise
    detail["rising_trend"] = rising
    detail["ok"] = not sustained_rise
    if sustained_rise:
        return (
            False,
            f"quarantine fraction trend: candidate {candidate_fraction:.1%} is "
            f"{relative_rise:.0%} above the recent-run baseline {baseline:.1%}, "
            "continuing a sustained rise across prior scheduled runs — window "
            "health may be shifting",
            detail,
        )
    return True, None, detail


def evaluate_promotion_gate(
    candidate: TrainingRunRecord,
    golden: TrainingRunRecord | None,
    *,
    candidate_report: Mapping[str, Any] | None = None,
    golden_report: Mapping[str, Any] | None = None,
    recall_tolerance: float = DEFAULT_RECALL_TOLERANCE,
    fp_budget_per_hour: float = DEFAULT_FP_BUDGET_PER_HOUR,
    agreement_tolerance: float = DEFAULT_AGREEMENT_TOLERANCE,
    max_reject_fraction: float = DEFAULT_MAX_REJECT_FRACTION,
    recent_quarantine_fractions: Sequence[float] = (),
    max_quarantine_relative_rise: float = DEFAULT_MAX_QUARANTINE_RELATIVE_RISE,
) -> PromotionGateResult:
    """Evaluate whether ``candidate`` is eligible for promotion to ``active``.

    ``candidate_report``/``golden_report`` let a caller (tests, a
    long-running dashboard process) pass the already-parsed fault-injection
    benchmark JSON directly; otherwise each is loaded from the record's own
    ``detector_benchmark_report_path``. Pure computation — never mutates the
    ledger; see :func:`promote_challenger` for the gated write.
    """
    reasons: list[str] = []
    checks: dict[str, Any] = {}

    calibration = _calibration_stamp(candidate)
    calibration_ok = calibration != "in_sample"
    checks["calibration"] = {"value": calibration, "ok": calibration_ok}
    if not calibration_ok:
        reasons.append(
            "manifest is stamped calibration: in_sample — categorically ineligible for promotion"
        )

    sane, sanity_reason = _contamination_sanity(
        candidate.dispositions, max_reject_fraction=max_reject_fraction
    )
    checks["contamination_sanity"] = {"ok": sane, "reason": sanity_reason}
    if not sane and sanity_reason is not None:
        reasons.append(sanity_reason)

    trend_ok, trend_reason, trend_detail = _quarantine_fraction_trend(
        _quarantine_fraction(candidate.dispositions),
        recent_quarantine_fractions,
        max_relative_rise=max_quarantine_relative_rise,
    )
    checks["quarantine_fraction_trend"] = trend_detail
    if not trend_ok and trend_reason is not None:
        reasons.append(trend_reason)

    if golden is None:
        checks["golden"] = {"ok": False, "reason": "no golden run declared for this model_type"}
        reasons.append(
            "no golden run declared yet (Task 3.4 declaring one is an explicit operator action)"
        )
        return PromotionGateResult(eligible=False, reasons=tuple(reasons), checks=checks)

    resolved_candidate_report = (
        dict(candidate_report)
        if candidate_report is not None
        else _load_report(candidate.detector_benchmark_report_path)
    )
    resolved_golden_report = (
        dict(golden_report)
        if golden_report is not None
        else _load_report(golden.detector_benchmark_report_path)
    )
    if resolved_candidate_report is None or resolved_golden_report is None:
        checks["benchmark"] = {
            "ok": False,
            "reason": "missing candidate or golden fault-injection benchmark report",
        }
        reasons.append("cannot evaluate recall/FP-budget without both benchmark reports")
        return PromotionGateResult(eligible=False, reasons=tuple(reasons), checks=checks)

    candidate_table = {
        (row["detector"], row["fault_kind"]): row
        for row in resolved_candidate_report.get("table", [])
    }
    golden_table = {
        (row["detector"], row["fault_kind"]): row for row in resolved_golden_report.get("table", [])
    }

    recall_ok = True
    recall_cells: list[dict[str, Any]] = []
    for (detector, fault_kind), golden_row in sorted(golden_table.items()):
        candidate_row = candidate_table.get((detector, fault_kind))
        golden_recall = golden_row.get("recall")
        candidate_recall = candidate_row.get("recall") if candidate_row is not None else None
        if golden_recall is None or candidate_recall is None:
            continue
        floor = golden_recall * (1 - recall_tolerance)
        regressed = candidate_recall < floor
        recall_cells.append(
            {
                "detector": detector,
                "fault_kind": fault_kind,
                "golden_recall": golden_recall,
                "candidate_recall": candidate_recall,
                "floor": floor,
                "regressed": regressed,
            }
        )
        recall_ok = recall_ok and not regressed
    checks["recall_non_regression"] = {
        "ok": recall_ok,
        "tolerance": recall_tolerance,
        "cells": recall_cells,
    }
    if not recall_ok:
        reasons.append(
            "recall regressed vs golden on the fault-injection benchmark beyond tolerance"
        )

    candidate_nominal = resolved_candidate_report.get("nominal_fp", {})
    golden_nominal = resolved_golden_report.get("nominal_fp", {})

    fp_budget_ok = True
    fp_cells: list[dict[str, Any]] = []
    for detector, block in sorted(candidate_nominal.items()):
        per_hour = block.get("per_hour", 0.0)
        within_budget = per_hour <= fp_budget_per_hour
        fp_cells.append(
            {
                "detector": detector,
                "per_hour": per_hour,
                "budget": fp_budget_per_hour,
                "within_budget": within_budget,
            }
        )
        fp_budget_ok = fp_budget_ok and within_budget
    checks["fp_budget"] = {"ok": fp_budget_ok, "cells": fp_cells}
    if not fp_budget_ok:
        reasons.append("held-out false-positive rate exceeds budget")

    agreement_ok = True
    agreement_cells: list[dict[str, Any]] = []
    for detector, golden_block in sorted(golden_nominal.items()):
        candidate_block = candidate_nominal.get(detector)
        if candidate_block is None:
            continue
        golden_rate = golden_block.get("per_hour", 0.0)
        candidate_rate = candidate_block.get("per_hour", 0.0)
        agrees = (
            (candidate_rate == 0)
            if golden_rate == 0
            else (abs(candidate_rate - golden_rate) / golden_rate <= agreement_tolerance)
        )
        agreement_cells.append(
            {
                "detector": detector,
                "golden_per_hour": golden_rate,
                "candidate_per_hour": candidate_rate,
                "agrees": agrees,
            }
        )
        agreement_ok = agreement_ok and agrees
    checks["holdout_score_agreement"] = {
        "ok": agreement_ok,
        "tolerance": agreement_tolerance,
        "cells": agreement_cells,
    }
    if not agreement_ok:
        reasons.append("holdout score agreement with golden exceeds tolerance")

    return PromotionGateResult(eligible=not reasons, reasons=tuple(reasons), checks=checks)


def promote_challenger(
    ledger: TrainingLedger,
    run_id: str,
    *,
    golden_run_id: str | None = None,
    candidate_report: Mapping[str, Any] | None = None,
    golden_report: Mapping[str, Any] | None = None,
    recall_tolerance: float = DEFAULT_RECALL_TOLERANCE,
    fp_budget_per_hour: float = DEFAULT_FP_BUDGET_PER_HOUR,
    agreement_tolerance: float = DEFAULT_AGREEMENT_TOLERANCE,
    max_reject_fraction: float = DEFAULT_MAX_REJECT_FRACTION,
    quarantine_trend_window: int = DEFAULT_QUARANTINE_TREND_WINDOW,
    max_quarantine_relative_rise: float = DEFAULT_MAX_QUARANTINE_RELATIVE_RISE,
    notes: str | None = None,
) -> tuple[PromotionGateResult, TrainingRunRecord | None]:
    """Run the gate for ``run_id`` and, only if it passes, promote it to
    ``active``.

    This is the "explicit separate action" the queue's Round-2 pre-made
    decision requires — nothing in ``scripts/retrain.py`` or the
    online-training scheduler (Task 3.2) calls this; it is only ever invoked
    by an operator-driven CLI flag (``scripts/ledger.py promote``) or a
    future dashboard action. Returns ``(gate_result, updated_record)`` —
    ``updated_record`` is ``None`` when the gate rejected promotion (nothing
    is appended to the ledger) or ``run_id``/``golden_run_id`` is unknown.
    """
    candidate = ledger.get(run_id)
    if candidate is None:
        return (
            PromotionGateResult(eligible=False, reasons=(f"unknown run_id {run_id!r}",)),
            None,
        )
    golden = ledger.get(golden_run_id) if golden_run_id else ledger.golden_run(candidate.model_type)
    # Quarantine-fraction trend (Task 3.6): the recent scheduled runs of this
    # model_type, older than the candidate, oldest→newest — the shape
    # `_quarantine_fraction_trend`'s "rising" check expects.
    recent_runs = sorted(
        (
            r
            for r in ledger.records()
            if r.model_type == candidate.model_type
            and r.run_id != run_id
            and r.created_at < candidate.created_at
        ),
        key=lambda r: r.created_at,
    )[-quarantine_trend_window:]
    recent_quarantine_fractions = [
        f for f in (_quarantine_fraction(r.dispositions) for r in recent_runs) if f is not None
    ]
    result = evaluate_promotion_gate(
        candidate,
        golden,
        candidate_report=candidate_report,
        golden_report=golden_report,
        recall_tolerance=recall_tolerance,
        fp_budget_per_hour=fp_budget_per_hour,
        agreement_tolerance=agreement_tolerance,
        max_reject_fraction=max_reject_fraction,
        recent_quarantine_fractions=recent_quarantine_fractions,
        max_quarantine_relative_rise=max_quarantine_relative_rise,
    )
    if not result.eligible:
        return result, None
    updated = ledger.set_publish_status(run_id, "active", notes=notes)
    return result, updated


def compute_golden_divergence(
    golden: TrainingRunRecord,
    current: TrainingRunRecord,
    *,
    golden_report: Mapping[str, Any] | None = None,
    current_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """ "Normal has drifted a lot — why?" (Task 3.4).

    Per-detector divergence between the frozen ``golden`` run and whatever
    ``current`` run is live now, compared on the same held-out clean-data
    alert rate (``nominal_fp`` from each run's fault-injection benchmark
    report) both were graded against. Pure computation; a CLI
    (``scripts/ledger.py golden-status``) or a future dashboard panel is
    responsible for surfacing it — this function only produces the
    JSON-safe numbers.
    """
    resolved_golden_report = (
        dict(golden_report)
        if golden_report is not None
        else (_load_report(golden.detector_benchmark_report_path) or {})
    )
    resolved_current_report = (
        dict(current_report)
        if current_report is not None
        else (_load_report(current.detector_benchmark_report_path) or {})
    )
    golden_nominal = resolved_golden_report.get("nominal_fp", {})
    current_nominal = resolved_current_report.get("nominal_fp", {})

    per_detector: dict[str, Any] = {}
    for detector in sorted(set(golden_nominal) | set(current_nominal)):
        golden_rate = golden_nominal.get(detector, {}).get("per_hour")
        current_rate = current_nominal.get(detector, {}).get("per_hour")
        delta = None
        relative_delta = None
        if golden_rate is not None and current_rate is not None:
            delta = current_rate - golden_rate
            if golden_rate:
                relative_delta = delta / golden_rate
            elif delta != 0:
                relative_delta = float("inf")
            else:
                relative_delta = 0.0
        per_detector[detector] = {
            "golden_per_hour": golden_rate,
            "current_per_hour": current_rate,
            "delta_per_hour": delta,
            "relative_delta": relative_delta,
        }

    return {
        "golden_run_id": golden.run_id,
        "current_run_id": current.run_id,
        "model_type": golden.model_type,
        "per_detector": per_detector,
    }
