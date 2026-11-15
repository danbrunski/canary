"""Feedback write tool — operator/LLM labels on anomaly events.

This is the active-labeling loop from
``docs/roadmap/llm-assist-and-compute-roadmap.md`` exposed as an MCP tool: when the
detector flags an event, Claude can record whether it's a true or false
positive (plus an optional note) and that label flows into the retraining
ledger.
"""

from __future__ import annotations

from typing import Any

from src.mcp_server.audit import MCPAuditLogger
from src.mcp_server.client import DaemonClient
from src.mcp_server.write_tools._shared import audited_post
from src.models.feedback import FeedbackLabel


# Takes the *whole event*, not an id: ``FeedbackStore.record`` derives its key
# with ``anomaly_key(event)``, and anomaly events carry no ``id`` field. This
# tool once sent ``{anomaly_id, label, note}``, none of which ``FeedbackRequest``
# accepts, so with ``extra="forbid"`` every call 422'd. The docstring is the
# tool description a client reads. ``hypothesis`` / ``clarifying_question`` /
# ``clarifying_answer`` are the Task S.3 enrichment: the question/answer pair
# must be complete, or the store answers 422.
async def record_feedback(
    client: DaemonClient,
    audit: MCPAuditLogger,
    *,
    event: dict[str, Any],
    label: FeedbackLabel,
    reason: str | None = None,
    driving_context: dict[str, Any] | None = None,
    labeled_by: str | None = None,
    hypothesis: str | None = None,
    clarifying_question: str | None = None,
    clarifying_answer: str | None = None,
) -> dict[str, Any]:
    """Record a true_positive/false_positive/ignored/needs_review verdict on an anomaly.

    Pass the whole `event` dict straight through from
    `get_recent_anomalies` — the store keys on the event, and these
    events have no id field.
    """
    body: dict[str, Any] = {"event": event, "label": label}
    if reason is not None:
        body["reason"] = reason
    if driving_context is not None:
        body["driving_context"] = driving_context
    if labeled_by is not None:
        body["labeled_by"] = labeled_by
    if hypothesis is not None:
        body["hypothesis"] = hypothesis
    if clarifying_question is not None:
        body["clarifying_question"] = clarifying_question
    if clarifying_answer is not None:
        body["clarifying_answer"] = clarifying_answer
    return await audited_post(
        client, audit, tool="record_feedback", path="/control/feedback", body=body
    )


__all__ = ["record_feedback"]
