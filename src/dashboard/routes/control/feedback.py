"""Operator anomaly feedback and the vehicle maintenance ledger.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError as PydanticValidationError

from src.dashboard.routes.control.context import ControlContext, _require
from src.dashboard.schemas import (
    FeedbackRequest,
    VehicleEventRequest,
)
from src.models.feedback import FeedbackStore
from src.vehicle.vehicle_ledger import VehicleLedgerStore


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    feedback_store = ctx.feedback_store
    publish_control_event = ctx.publish_control_event
    vehicle_ledger_store = ctx.vehicle_ledger_store

    def _feedback() -> FeedbackStore:
        return _require(feedback_store, "feedback")

    @router.get("/control/feedback")
    def get_feedback() -> dict[str, Any]:
        store = _feedback()
        records = [fb.to_dict() for fb in store.labels().values()]
        return {"feedback": records, "count": len(records), "summary": store.summary()}

    @router.post("/control/feedback", status_code=201)
    def post_feedback(
        request: FeedbackRequest,
    ) -> dict[str, Any]:
        store = _feedback()
        try:
            fb = store.record(
                event=request.event,
                label=request.label,
                reason=request.reason,
                driving_context=request.driving_context,
                labeled_by=request.labeled_by,
                hypothesis=request.hypothesis,
                clarifying_question=request.clarifying_question,
                clarifying_answer=request.clarifying_answer,
            )
        except PydanticValidationError as exc:
            # Task S.3: an incomplete clarifying question/answer pair (see
            # `AnomalyFeedback._clarifying_pair_is_complete`) — a 422, not a 500.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        publish_control_event("anomaly.feedback", "ok", fb.to_dict())
        return {"feedback": fb.to_dict()}

    def _vehicle_ledger() -> VehicleLedgerStore:
        return _require(vehicle_ledger_store, "vehicle ledger")

    @router.get("/control/vehicle-ledger")
    def get_vehicle_ledger() -> dict[str, Any]:
        """Vehicle maintenance/intervention ledger (Task 3.3), oldest-first."""
        store = _vehicle_ledger()
        records = [e.to_dict() for e in store.records()]
        return {"events": records, "count": len(records)}

    @router.post("/control/vehicle-ledger", status_code=201)
    def post_vehicle_ledger(
        request: VehicleEventRequest,
    ) -> dict[str, Any]:
        store = _vehicle_ledger()
        event = store.record(
            occurred_at=request.occurred_at,
            kind=request.kind,
            title=request.title,
            details=request.details,
            odometer_km=request.odometer_km,
            affected_channels=request.affected_channels,
            corpus_boundary=request.corpus_boundary,
            event_id=request.event_id,
            tires=request.tires,
            intervention=request.intervention,
        )
        publish_control_event("vehicle_ledger.record", "ok", event.to_dict())
        return {"event": event.to_dict()}

    @router.delete("/control/vehicle-ledger/{event_id}")
    def delete_vehicle_ledger(event_id: str) -> dict[str, Any]:
        """Remove an entry outright: a typo'd or erroneous record (2026-10-04).

        The audit names what was removed, not its content: an erroneous
        reading kept in the audit log would be the copy this exists to stop.
        """
        removed = _vehicle_ledger().remove(event_id)
        if removed is None:
            raise HTTPException(status_code=404, detail=f"no vehicle ledger entry {event_id}")
        summary = {"id": removed.id, "title": removed.title}
        publish_control_event("vehicle_ledger.delete", "ok", summary)
        return {"deleted": summary}

    return router
