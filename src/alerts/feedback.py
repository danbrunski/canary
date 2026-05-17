"""Operator false-positive feedback on anomaly events (Task 6.24).

An anomaly the operator can't label is an anomaly the model can't learn
from. This is the labelling substrate: the operator marks an event a true
positive, false positive, ignored, or needs-review, attaches a reason and
the driving context, and that judgement is persisted *next to* the anomaly
log so a later model version can be explained or its threshold tuned from
real verdicts rather than guesses (the Feedback-lineage cross-cutting
concern + Risk R11).

Storage mirrors the anomaly / job / training ledgers: append-only JSONL
under ``data/control/`` (gitignored). A relabel appends a new line for the
same anomaly key; :meth:`FeedbackStore.labels` collapses to the latest
verdict per key while :meth:`FeedbackStore.raw_records` preserves the full
chain. :func:`annotate_events_with_feedback` joins verdicts back onto the
anomaly stream so the dashboard timeline can show "you marked this".

Labels are model data, not UI state: they are exportable
(:func:`feedback_to_csv`) and consumed by label-aware retraining (6.26).
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_FEEDBACK_PATH",
    "FEEDBACK_LABELS",
    "AnomalyFeedback",
    "FeedbackLabel",
    "FeedbackStore",
    "annotate_events_with_feedback",
    "anomaly_key",
    "feedback_to_csv",
]

DEFAULT_FEEDBACK_PATH = Path("data/control/anomaly_feedback.jsonl")

FeedbackLabel = Literal["true_positive", "false_positive", "ignored", "needs_review"]
#: The valid verdicts, as a set, for request validation at the API edge.
FEEDBACK_LABELS: frozenset[str] = frozenset(
    {"true_positive", "false_positive", "ignored", "needs_review"}
)


def anomaly_key(event: dict[str, Any]) -> str:
    """Stable identity for an anomaly event.

    Matches the dashboard's dedupe tuple ``(timestamp, source,
    model_version)`` so a verdict recorded from the timeline rejoins exactly
    the event the operator clicked, even though the anomaly log itself has no
    primary key.
    """
    return "|".join(str(event.get(field, "")) for field in ("timestamp", "source", "model_version"))


class AnomalyFeedback(BaseModel):
    """One operator verdict on one anomaly event."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    anomaly_key: str
    label: FeedbackLabel
    reason: str | None = None
    #: Free-form context the operator/UI attaches (operating_mode, speed,
    #: road, weather, "A/C just kicked on", …). Provenance, not schema.
    driving_context: dict[str, Any] = Field(default_factory=dict)
    #: Echo of the labelled event so an export is self-contained even if the
    #: anomaly log has rotated away.
    event: dict[str, Any] = Field(default_factory=dict)
    labeled_by: str | None = None
    labeled_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AnomalyFeedback:
        return cls.model_validate(data)


class FeedbackStore:
    """Append-only JSONL store of anomaly verdicts.

    Not thread-safe on its own; the daemon serializes writes the same way it
    does the other ledgers. Reads tolerate a torn final line.
    """

    def __init__(self, path: Path = DEFAULT_FEEDBACK_PATH) -> None:
        self.path = Path(path)

    def record(
        self,
        *,
        event: dict[str, Any],
        label: FeedbackLabel,
        reason: str | None = None,
        driving_context: dict[str, Any] | None = None,
        labeled_by: str | None = None,
    ) -> AnomalyFeedback:
        """Append a verdict for ``event``. A relabel is just another append."""
        fb = AnomalyFeedback(
            anomaly_key=anomaly_key(event),
            label=label,
            reason=reason,
            driving_context=driving_context or {},
            event=event,
            labeled_by=labeled_by,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write(json.dumps(fb.to_dict(), sort_keys=True) + "\n")
            handle.flush()
        return fb

    def _iter_raw(self) -> Iterator[AnomalyFeedback]:
        if not self.path.exists():
            return
        with self.path.open() as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield AnomalyFeedback.from_dict(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue

    def raw_records(self) -> list[AnomalyFeedback]:
        """Every appended verdict in write order (full relabel history)."""
        return list(self._iter_raw())

    def labels(self) -> dict[str, AnomalyFeedback]:
        """Latest verdict per anomaly key (a relabel supersedes)."""
        latest: dict[str, AnomalyFeedback] = {}
        for fb in self._iter_raw():
            latest[fb.anomaly_key] = fb
        return latest

    def get(self, key: str) -> AnomalyFeedback | None:
        return self.labels().get(key)

    def summary(self) -> dict[str, int]:
        """Count of current verdicts by label (for /status + the dashboard)."""
        counts: dict[str, int] = {
            "true_positive": 0,
            "false_positive": 0,
            "ignored": 0,
            "needs_review": 0,
        }
        for fb in self.labels().values():
            counts[fb.label] += 1
        return counts


def annotate_events_with_feedback(
    events: Iterable[dict[str, Any]], store: FeedbackStore
) -> list[dict[str, Any]]:
    """Attach the current verdict (if any) to each event as ``feedback``.

    Pure projection — returns new dicts, doesn't mutate the inputs. An event
    with no verdict gets ``feedback: None`` so the timeline can render the
    "unlabelled" affordance uniformly.
    """
    labels = store.labels()
    out: list[dict[str, Any]] = []
    for event in events:
        fb = labels.get(anomaly_key(event))
        out.append({**event, "feedback": fb.to_dict() if fb is not None else None})
    return out


def feedback_to_csv(records: Iterable[AnomalyFeedback]) -> str:
    """Flatten verdicts to CSV for off-Pi analysis / training-set curation."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "anomaly_key",
            "label",
            "reason",
            "labeled_by",
            "labeled_at",
            "event_timestamp",
            "source",
            "model_version",
            "score",
            "operating_mode",
            "driving_context",
        ]
    )
    for fb in records:
        ev = fb.event
        writer.writerow(
            [
                fb.anomaly_key,
                fb.label,
                fb.reason or "",
                fb.labeled_by or "",
                fb.labeled_at,
                ev.get("timestamp", ""),
                ev.get("source", ""),
                ev.get("model_version", ""),
                ev.get("score", ""),
                ev.get("operating_mode", ""),
                json.dumps(fb.driving_context, sort_keys=True),
            ]
        )
    return buf.getvalue()
