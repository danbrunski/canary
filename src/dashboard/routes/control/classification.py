"""Session classification, digests, corpus mode/event summaries and fingerprint trends.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.control.classification import (
    corpus_mode_summary,
    finish_classification,
    get_session_classification,
    list_events_by_kind,
)
from src.control.trends import corpus_trend_summary
from src.control.workbench_jobs import (
    WorkbenchRefusedError,
    capture_deferral,
    run_off_loop,
    select_sessions,
)
from src.dashboard.routes.control.context import ControlContext
from src.session import SessionKind, session_kind
from src.storage.sessions import resolve_session


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    config = ctx.config
    emitter = ctx.emitter
    publish_control_event = ctx.publish_control_event

    @router.get("/control/sessions/{session_id}/classification")
    def get_session_classification_endpoint(
        session_id: str,
    ) -> dict[str, Any]:
        payload = get_session_classification(config.collection.storage_path, session_id)
        if payload is None:
            raise HTTPException(
                status_code=404,
                detail=f"no classification sidecar for session {session_id!r}",
            )
        return {"classification": payload}

    @router.post("/control/sessions/{session_id}/classification", status_code=201)
    async def post_session_classification(
        session_id: str,
    ) -> dict[str, Any]:
        # R03.2: classifying reads the whole session (resample_and_segment,
        # then the fingerprint), so it runs in the workbench's bounded
        # single-flight child, deferred while a capture records and refused
        # over the span budget (P02.1: that chain reads part by part, so its
        # peak follows the span, not the rows). Trend alerting and the catalog
        # stay here, in the parent, because the emitter is the live one.
        if session_kind(session_id) is not SessionKind.LIVE:
            raise HTTPException(
                status_code=400,
                detail=f"refusing to classify non-live-session id {session_id!r}",
            )
        session_root = config.collection.storage_path
        if not (session_root / session_id).is_dir():
            raise HTTPException(
                status_code=404,
                detail=f"session {session_id!r} not found under {session_root}",
            )
        try:
            capture_deferral(config, [session_id])
            selection = select_sessions(config, sessions=[session_id], reads="value_number")
        except WorkbenchRefusedError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        if selection.over_budget:
            raise HTTPException(
                409,
                {
                    "error": "analysis_refused",
                    "reason": "over_budget",
                    "message": selection.over_budget[0].reason,
                },
            )
        try:
            written = await run_off_loop("classify", {"session_id": session_id}, config)
        except WorkbenchRefusedError as exc:
            raise HTTPException(exc.status, exc.detail) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload = await asyncio.to_thread(
            finish_classification,
            config,
            session_id,
            written,
            emitter=emitter,
            catalog_db=config.daemon.catalog_db,
        )
        publish_control_event(
            "classification.write",
            "ok",
            {"session_id": session_id, "disposition": payload.get("disposition")},
        )
        return {"classification": payload}

    @router.get("/control/sessions/{session_id}/digest")
    async def get_session_digest_route(
        session_id: str,
    ) -> dict[str, Any]:
        # Task 5.6 follow-up: authenticated, not public — the digest
        # aggregates the vehicle ledger and per-session fingerprint z-scores,
        # both of which are otherwise only readable via authenticated
        # /control/vehicle-ledger and /control/trends. Gating the whole
        # endpoint (not just those fields) matches this codebase's existing
        # public-XOR-authenticated posture per route. Computed on first
        # request if the trip-end hook (Task 5.4) hasn't already written
        # digest.json for this session.
        from src.control.digest import get_session_digest_refusal
        from src.control.trip_end import handle_trip_end_off_loop
        from src.storage.session_products import read_digest

        try:
            session_dir = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"no digest for session {session_id!r}"
            ) from exc

        digest = read_digest(session_dir)
        if digest is None:
            # Use the same bounded child, row preflight, and in-flight task as
            # automatic trip end. A request can no longer run multi-gigabyte
            # pandas work inside the daemon's FastAPI thread pool.
            await handle_trip_end_off_loop(
                session_dir,
                config,
                catalog_db=config.daemon.catalog_db,
            )
            digest = read_digest(session_dir)
        result = digest
        if result is None:
            # A refused digest is 409, never 404 and never a 200 carrying a
            # refusal payload: "this session is over the row budget" is an
            # answer about the session, distinct from "no such session /
            # nothing recorded yet", and must not be readable as a digest.
            refusal = get_session_digest_refusal(config, session_id)
            if refusal is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "digest_refused",
                        "session_id": session_id,
                        "digest_skipped": refusal,
                    },
                )
            raise HTTPException(status_code=404, detail=f"no digest for session {session_id!r}")
        return result

    @router.get("/control/classifications/summary")
    def get_corpus_classification_summary() -> dict[str, Any]:
        return corpus_mode_summary(config.collection.storage_path)

    @router.get("/control/trends")
    def get_control_trends() -> dict[str, Any]:
        """Per-stat drift trend over the fingerprint history (Task 4.5)."""
        return corpus_trend_summary(config)

    @router.get("/control/classifications/events")
    def get_corpus_events(
        kind: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
    ) -> dict[str, Any]:
        return list_events_by_kind(
            config.collection.storage_path,
            kind=kind,
            limit=limit,
        )

    return router
