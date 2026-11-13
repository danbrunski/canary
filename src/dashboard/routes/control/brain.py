"""The second brain (``/control/brain/*``, E04.5).

Every GET returns one :mod:`src.control.brain_reads` function's result as is;
the MCP brain tools pass these routes through, so HTTP and MCP serve the same
computed facts. ``POST /control/brain/jobs`` queues idle-time work
(:mod:`src.control.brain_jobs`); it never runs it inline. All routes require
the control token (router-level, R03.6).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.control import brain_reads
from src.control.brain_jobs import BrainJobStore, canonical_params
from src.dashboard.routes.control.context import ControlContext
from src.evidence.ledger import UnscopedMemoryError

#: When this app was built: the brain's transient state is unknown since then.
_STARTED_AT = datetime.now(UTC)


class BrainJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    #: Queue a completed job again (its evidence dedups or supersedes).
    rerun: bool = False


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    config = ctx.config

    async def read(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        return await asyncio.to_thread(call)

    @router.get("/control/brain/status")
    async def get_brain_status() -> dict[str, Any]:
        """Identity, what a program/calibration change invalidated, selected
        tables, RAM-address standing, transient state, memory, job counts and
        the live twin's predictions/residuals/applicability (E04.1) and the
        live next-measurement proposals (E03.4)."""
        recorder = ctx.inference_timing
        live = recorder.twin() if recorder is not None else None
        proposals = recorder.proposals() if recorder is not None else None
        return await read(
            lambda: brain_reads.brain_status(
                config,
                process_started_at=_STARTED_AT,
                live_twin=live,
                live_proposals=proposals,
            )
        )

    @router.get("/control/brain/memory")
    async def get_brain_memory(request: Request) -> dict[str, Any]:
        """Query the vehicle's memory (``MemoryQuery`` filters as query parameters)."""
        params = dict(request.query_params)
        return await read(lambda: brain_reads.memory_query(config, params))

    @router.get("/control/brain/evidence/{evidence_id}")
    async def get_brain_evidence(evidence_id: str) -> dict[str, Any]:
        """One evidence id resolved, with which part of an explanation it is."""
        return await read(lambda: brain_reads.evidence_record(config, evidence_id))

    @router.get("/control/brain/investigations/{investigation_id}/card")
    async def get_brain_investigation_card(
        investigation_id: str, as_of: str | None = None
    ) -> dict[str, Any]:
        """The deterministic investigation card (E01.5), evidence split into
        ECU computation and measured engine response."""
        return await read(
            lambda: brain_reads.investigation_card_payload(config, investigation_id, as_of=as_of)
        )

    @router.get("/control/brain/twin/sessions/{session_id}")
    async def get_brain_twin_session(session_id: str) -> dict[str, Any]:
        """The twin's evaluation of a session: accounting, missing inputs, next
        capture, observed/predicted trace, and whether it is still current."""
        return await read(lambda: brain_reads.twin_session_evaluation(config, session_id))

    @router.get("/control/brain/jobs")
    async def get_brain_jobs() -> dict[str, Any]:
        """Every idle-time brain job and its state."""
        return await read(lambda: brain_reads.brain_jobs(config))

    @router.get("/control/brain/jobs/{job_id}")
    async def get_brain_job(job_id: str) -> dict[str, Any]:
        return await read(lambda: brain_reads.brain_job(config, job_id))

    @router.post("/control/brain/jobs", status_code=202)
    async def post_brain_job(request: BrainJobRequest) -> dict[str, Any]:
        """Queue a session evaluation or memory consolidation. It runs in the
        next window with collection inactive; the same work is one job."""

        def submit() -> dict[str, Any]:
            params = canonical_params(config, request.kind, request.params)
            job, created = BrainJobStore.for_config(config).submit(
                request.kind, params, rerun=request.rerun
            )
            return {"job": job.to_dict(), "created": created}

        try:
            payload = await asyncio.to_thread(submit)
        except UnscopedMemoryError as exc:
            raise HTTPException(503, {"error": "unscoped", "message": str(exc)}) from exc
        ctx.publish_control_event("brain.job.submit", "ok", payload["job"])
        return payload

    return router
