"""Second-brain read tools (E04.5): pass-throughs to ``/control/brain/*``.

Each tool returns the daemon route's body verbatim, and each route returns one
``src.control.brain_reads`` function's result, so MCP and HTTP serve the same
computed facts by construction.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from src.mcp_server.client import DaemonClient


async def get_brain_status(client: DaemonClient) -> dict[str, Any]:
    """The second brain's standing on the Pi: the ECM program and calibration
    in force, what a program or calibration change invalidated, which tables
    the twin selects, whether RAM addresses need re-verification (only on a
    program change), the transient ECU state it cannot know after a restart,
    memory size and job counts."""
    return await client.get_json("/control/brain/status")


async def query_memory(
    client: DaemonClient,
    *,
    kind: str | None = None,
    incident: str | None = None,
    subject: str | None = None,
    session: str | None = None,
    regime: str | None = None,
    calibration: str | None = None,
    program_id: str | None = None,
    valid_at: str | None = None,
    valid_before: str | None = None,
    valid_after: str | None = None,
    as_of: str | None = None,
    text: str | None = None,
    history: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Query the vehicle's investigation memory (evidence, investigations,
    predictions, interventions, outcomes, references, retractions).

    ``kind`` is a comma list of record kinds; ``calibration``/``program_id``
    take ``unknown`` for records that state them unknown; times are ISO with a
    UTC offset; ``as_of`` answers "what was believed then"; ``history``
    includes superseded and retracted records. Every set filter must hold."""
    params: dict[str, Any] = {
        "kind": kind,
        "incident": incident,
        "subject": subject,
        "session": session,
        "regime": regime,
        "calibration": calibration,
        "program_id": program_id,
        "valid_at": valid_at,
        "valid_before": valid_before,
        "valid_after": valid_after,
        "as_of": as_of,
        "text": text,
        "history": "true" if history else None,
        "limit": limit,
    }
    return await client.get_json(
        "/control/brain/memory", params={k: v for k, v in params.items() if v is not None}
    )


async def resolve_evidence(client: DaemonClient, *, evidence_id: str) -> dict[str, Any]:
    """Resolve one evidence id: tool, version, conditions, sources, values,
    result (or its named unevaluable reason), supersession, and whether it is
    ECU computation (the twin) or measured engine response."""
    return await client.get_json(f"/control/brain/evidence/{quote(evidence_id, safe='')}")


async def get_investigation_card(
    client: DaemonClient, *, investigation_id: str, as_of: str | None = None
) -> dict[str, Any]:
    """The deterministic investigation card: what happened, hypotheses with
    evidence for and against, predictions and outcomes, unknowns and next
    measurements, with its evidence split into ECU computation and measured
    engine response. No language model is involved."""
    return await client.get_json(
        f"/control/brain/investigations/{quote(investigation_id, safe='')}/card",
        params={"as_of": as_of} if as_of else None,
    )


async def get_twin_session_evaluation(client: DaemonClient, *, session_id: str) -> dict[str, Any]:
    """The target-phi twin's evaluation of one stored session: per-slot sample
    accounting, missing inputs, the capture that would make it evaluable, the
    observed/predicted command trace and whether the result is still current
    for the program in force. ``not_evaluated`` until a job has run."""
    return await client.get_json(f"/control/brain/twin/sessions/{quote(session_id, safe='')}")


async def get_ecu_application_model(client: DaemonClient) -> dict[str, Any]:
    """The ECM application twin's coverage (E05.1): which outputs are
    qualified or conditional, what the twin implements, observes and assumes
    on each path, the blockers, the logic still uncovered, and whether the
    image recorded as running now is one the program was checked on. Read-only;
    does not communicate with the ECU."""
    return await client.get_json("/control/ecu/application-model")


async def list_brain_jobs(client: DaemonClient) -> dict[str, Any]:
    """The brain's idle-time jobs (session evaluation, memory consolidation)
    with state: queued, running, deferred (and why), pending (waiting for
    inputs), completed or failed."""
    return await client.get_json("/control/brain/jobs")


async def get_brain_job(client: DaemonClient, *, job_id: str) -> dict[str, Any]:
    """One brain job and its full result."""
    return await client.get_json(f"/control/brain/jobs/{quote(job_id, safe='')}")


__all__ = [
    "get_brain_job",
    "get_ecu_application_model",
    "get_brain_status",
    "get_investigation_card",
    "get_twin_session_evaluation",
    "list_brain_jobs",
    "query_memory",
    "resolve_evidence",
]
