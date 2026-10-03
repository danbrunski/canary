"""Label-aware retraining policy (Task 6.26).

Operator feedback is model data. A reviewed false positive means "this window
is acceptable baseline behavior"; true positives and uncertain labels mean
"do not train the unsupervised detector to treat this as normal." This module
turns the feedback JSONL into an auditable row filter for retrain jobs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, timedelta
from typing import Any, Literal

import pandas as pd

from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.models.feedback import AnomalyFeedback

RetrainingFeedbackAction = Literal["clean_baseline", "exclude"]

CLEAN_BASELINE_LABELS = frozenset({"false_positive"})
EXCLUDED_LABELS = frozenset({"true_positive", "ignored", "needs_review"})


@dataclass(frozen=True)
class RetrainingFeedbackWindow:
    """One reviewed feedback item projected into training-frame time."""

    anomaly_key: str
    label: str
    action: RetrainingFeedbackAction
    start_timestamp: str
    end_timestamp: str
    session: str | None
    source: str | None
    model_version: str | None
    reason: str | None
    #: Task S.3 — the operator's (or future LLM's) hypothesis, when the
    #: excluded feedback carried one; carried through so the ledger's
    #: provenance shows *why* an excluded window was excluded, not just that
    #: it was.
    hypothesis: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "anomaly_key": self.anomaly_key,
            "label": self.label,
            "action": self.action,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "session": self.session,
            "source": self.source,
            "model_version": self.model_version,
            "reason": self.reason,
            "hypothesis": self.hypothesis,
        }


@dataclass(frozen=True)
class RetrainingFeedbackReport:
    """JSON-safe summary persisted into the training ledger.

    ``enriched_labels`` (Task S.3, llm-assist-and-compute-roadmap.md, case
    5) counts feedback that carries a hypothesis and/or a completed
    clarifying question/answer pair, regardless of verdict label — this is
    how the enriched-label capture loop "flows into the retraining ledger
    like existing verdicts": ``scripts/retrain.py`` already embeds this
    report verbatim as ``TrainingRunRecord.metrics["feedback_policy"]``, so
    a training run's provenance shows how much of its input feedback was
    LLM/operator-enriched with no further plumbing needed.
    """

    total_labels: int
    clean_baseline_labels: int
    excluded_labels: int
    invalid_labels: int
    excluded_rows: int
    enriched_labels: int
    windows: tuple[RetrainingFeedbackWindow, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_labels": self.total_labels,
            "clean_baseline_labels": self.clean_baseline_labels,
            "excluded_labels": self.excluded_labels,
            "invalid_labels": self.invalid_labels,
            "excluded_rows": self.excluded_rows,
            "enriched_labels": self.enriched_labels,
            "windows": [window.to_dict() for window in self.windows],
        }


def apply_retraining_feedback_policy(
    frame: pd.DataFrame,
    feedback: dict[str, AnomalyFeedback],
    *,
    fallback_margin_seconds: float = 30.0,
    timestamp_column: str = TIMESTAMP_COLUMN,
    session_column: str = SESSION_COLUMN,
) -> tuple[pd.DataFrame, RetrainingFeedbackReport]:
    """Filter training rows according to the latest operator feedback.

    False positives are counted as reviewed clean baseline and kept. True
    positives, ignored events, and needs-review labels are excluded over the
    event's explicit window, or around the event timestamp when no detector
    window exists.
    """
    report = build_retraining_feedback_report(
        feedback,
        fallback_margin_seconds=fallback_margin_seconds,
    )
    if not report.windows:
        return frame, report

    if timestamp_column not in frame.columns:
        raise ValueError(f"training frame missing timestamp column {timestamp_column!r}")

    timestamps = pd.to_datetime(frame[timestamp_column], utc=True, errors="coerce")
    keep_mask = pd.Series(True, index=frame.index)
    sessions = frame[session_column].astype(str) if session_column in frame.columns else None

    for window in report.windows:
        start = pd.Timestamp(window.start_timestamp)
        end = pd.Timestamp(window.end_timestamp)
        window_mask = timestamps.between(start, end, inclusive="both")
        if sessions is not None and window.session:
            window_mask &= sessions == window.session
        keep_mask &= ~window_mask.fillna(False)

    excluded_rows = int((~keep_mask).sum())
    filtered = frame.loc[keep_mask].copy()
    return filtered, replace(report, excluded_rows=excluded_rows)


def build_retraining_feedback_report(
    feedback: dict[str, AnomalyFeedback],
    *,
    fallback_margin_seconds: float = 30.0,
) -> RetrainingFeedbackReport:
    if fallback_margin_seconds < 0:
        raise ValueError("fallback_margin_seconds must be non-negative")

    total = len(feedback)
    clean = 0
    excluded = 0
    invalid = 0
    enriched = 0
    windows: list[RetrainingFeedbackWindow] = []
    for item in feedback.values():
        # Task S.3: enrichment is orthogonal to the clean/excluded verdict
        # split below — a false positive can carry a hypothesis too.
        if item.hypothesis is not None or item.clarifying_answer is not None:
            enriched += 1
        if item.label in CLEAN_BASELINE_LABELS:
            clean += 1
            continue
        if item.label not in EXCLUDED_LABELS:
            continue
        excluded += 1
        try:
            windows.append(
                _window_from_feedback(
                    item,
                    fallback_margin_seconds=fallback_margin_seconds,
                )
            )
        except (TypeError, ValueError):
            invalid += 1

    return RetrainingFeedbackReport(
        total_labels=total,
        clean_baseline_labels=clean,
        excluded_labels=excluded,
        invalid_labels=invalid,
        excluded_rows=0,
        enriched_labels=enriched,
        windows=tuple(windows),
    )


def _window_from_feedback(
    feedback: AnomalyFeedback,
    *,
    fallback_margin_seconds: float,
) -> RetrainingFeedbackWindow:
    event = feedback.event
    start_raw = event.get("window_start_timestamp")
    end_raw = event.get("window_end_timestamp")
    if start_raw and end_raw:
        start = _coerce_utc(start_raw)
        end = _coerce_utc(end_raw)
    else:
        center = _coerce_utc(event.get("timestamp"))
        margin = timedelta(seconds=fallback_margin_seconds)
        start = center - margin
        end = center + margin
    if end < start:
        raise ValueError("feedback window end is before start")

    return RetrainingFeedbackWindow(
        anomaly_key=feedback.anomaly_key,
        label=feedback.label,
        action="exclude",
        start_timestamp=start.isoformat().replace("+00:00", "Z"),
        end_timestamp=end.isoformat().replace("+00:00", "Z"),
        session=_optional_str(event.get("session") or feedback.driving_context.get("session")),
        source=_optional_str(event.get("source")),
        model_version=_optional_str(event.get("model_version")),
        reason=feedback.reason,
        hypothesis=feedback.hypothesis,
    )


def _coerce_utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("missing feedback timestamp")
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(UTC)
    else:
        timestamp = timestamp.tz_convert(UTC)
    return timestamp


def _optional_str(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


__all__ = [
    "CLEAN_BASELINE_LABELS",
    "EXCLUDED_LABELS",
    "RetrainingFeedbackReport",
    "RetrainingFeedbackWindow",
    "apply_retraining_feedback_policy",
    "build_retraining_feedback_report",
]
