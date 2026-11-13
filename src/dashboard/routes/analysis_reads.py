"""The analysis reads: binned aggregation, the LLM-assist cards and a session's trip summary.

Public (no token) reads of stored data, like the rest of
:func:`~src.dashboard.routes.public.register_public_routes`. Each can answer a refusal
(``analysis_deferred`` while a capture records, R29.1), so this registrar installs the
``StatusError`` handler itself (R29.3): a bare app that mounts only these routes answers it too.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI, HTTPException, Query

from src.analysis.llm_assist import LlmAssistCardStore, resolve_card
from src.config import CanaryConfig
from src.control.workbench_jobs import capture_deferral
from src.dashboard.status_errors import register_status_error_handler
from src.evidence import MemoryLedger, UnscopedMemoryError, memory_ledger_for


def register_analysis_read_routes(app: FastAPI, *, config: CanaryConfig) -> None:
    # A refusal any of these routes lets escape answers with its own status and body.
    register_status_error_handler(app)

    @app.get("/analysis/binned")
    def get_binned_analysis(
        value: str = Query(...),
        x: str = Query(...),
        y: str | None = Query(default=None),
        x_bins: int = Query(default=12, ge=2, le=64),
        y_bins: int | None = Query(default=None, ge=2, le=64),
        agg: str = Query(default="mean"),
        session: str | None = Query(default=None),
        since: str | None = Query(default=None),
        until: str | None = Query(default=None),
        units: str = Query(default="stored"),
    ) -> dict[str, Any]:
        # Task 5.1: ad-hoc binned aggregation ("trims by rpm×load") over the
        # DuckDB parquet layer — no hand-written SQL, no refit. Public/
        # read-only like the other stored-data reads on this router (trip
        # summary, DTC, monitors) — no secrets, just historical telemetry.
        from src.analysis.binned_analysis import Aggregation
        from src.control.binned_analysis import binned_analysis_response

        if agg not in ("mean", "median", "p95", "count"):
            raise HTTPException(status_code=400, detail=f"unsupported agg: {agg!r}")
        # `units="engineering"` rescales decoded: channels via bindings.yaml;
        # they are stored raw on purpose, so binning them as-is works in counts.
        if units not in ("stored", "engineering"):
            raise HTTPException(status_code=400, detail=f"unsupported units: {units!r}")
        # DuckDB over stored parquet, bounded only by a row limit: not run while
        # a capture records (AGENTS.md, "No analysis on the Pi while it collects";
        # R29.1). The 409 is the one the workbench routes answer.
        capture_deferral(config, [session] if session else None)
        return binned_analysis_response(
            config,
            value=value,
            x=x,
            y=y,
            x_bins=x_bins,
            y_bins=y_bins,
            agg=cast("Aggregation", agg),
            session=session,
            since=since,
            until=until,
            units=units,
        )

    @app.get("/analysis/llm-assist/cards")
    def get_llm_assist_cards() -> dict[str, Any]:
        # Task S.2: public/read-only like the routes above — the dashboard
        # card renderer's feature-flagged endpoint. Disabled (the default;
        # no producer exists yet, Task S.1) is an explicit, expected state,
        # not an error: the response just omits `cards` so the renderer's
        # absent-state branch is a plain `enabled` check, no error handling.
        #
        # Each card is resolved against the vehicle's memory ledger (S.5,
        # E01.2): its numbers are rendered from the cited evidence records, a
        # card whose citations do not resolve is marked ``rejected`` with the
        # problems named, and a card from before evidence ids is marked
        # ``unverified_historical``. An unscoped config has no ledger, so its
        # evidence cards are rejected rather than shown unresolved.
        if not config.llm_assist.enabled:
            return {"enabled": False}
        store = LlmAssistCardStore(config.llm_assist.cards_path)
        try:
            ledger: MemoryLedger | None = memory_ledger_for(config)
        except UnscopedMemoryError:
            ledger = None
        return {
            "enabled": True,
            "cards": [resolve_card(card, ledger).to_dict() for card in store.cards()],
        }

    @app.get("/sessions/{session_id}/trip-summary")
    async def get_session_trip_summary(session_id: str) -> dict[str, Any]:
        # Task 1.5: public read like /obd/dtc — no session-close hook exists
        # yet (eval-longitudinal-and-dtc.md Phase 5.4), so the first request
        # computes the trip_summary.json sidecar (Task 1.4) and every later one
        # serves the cached copy. The first computation reads the whole session,
        # so it runs in the workbench's bounded child and answers 409
        # ``analysis_deferred`` while a capture records (R29.1); a sidecar
        # already on disk is served directly, capture or not.
        from src.control.trip_summary import trip_summary_off_loop

        result = await trip_summary_off_loop(config, session_id)
        if result is None:
            raise HTTPException(
                status_code=404, detail=f"no trip summary for session {session_id!r}"
            )
        return result
