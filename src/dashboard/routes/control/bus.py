"""The OBD/CAN bus: forced polls, ad-hoc requests, discovery, passive mode, the request budget and
tuning mode.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException

from src.collection import bus_budget, tuning_profile, tx_gate
from src.collection.discovery import registry_snapshot, reset_discovery
from src.control.can_activity import CanActivityBusError, capture_can_activity
from src.control.obd_request import AdHocRequestBusError, execute_ad_hoc_request
from src.control.tuning_mode import set_tuning_mode
from src.dashboard.routes.control.context import ControlContext
from src.dashboard.schemas import (
    BusBudgetRequest,
    CanActivityRequest,
    ObdRequestRequest,
    PassiveModeRequest,
    TriggerPollRequest,
    TuningModeRequest,
)


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    config = ctx.config
    force_poll_trigger = ctx.force_poll_trigger
    publish_control_event = ctx.publish_control_event
    storage_writer = ctx.storage_writer

    @router.post("/control/trigger_poll")
    def post_control_trigger_poll(
        request: TriggerPollRequest,
    ) -> dict[str, Any]:
        if config.collection.obd_source != "canable":
            raise HTTPException(
                status_code=409,
                detail=(
                    "trigger_poll requires collection.obd_source=canable "
                    f"(currently {config.collection.obd_source!r})"
                ),
            )
        if force_poll_trigger is None:
            raise HTTPException(status_code=503, detail="OBD collector is not running")
        force_poll_trigger.request(request.channels)
        payload = {"trigger_poll": {"queued_channels": request.channels}}
        publish_control_event("trigger_poll", "ok", payload)
        return payload

    @router.post("/control/can_activity")
    def post_control_can_activity(
        request: CanActivityRequest,
    ) -> dict[str, Any]:
        try:
            result = capture_can_activity(
                config.hardware.can,
                seconds=request.seconds,
                ids=request.ids,
            )
        except CanActivityBusError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        payload = {"can_activity": result.to_dict()}
        publish_control_event("can_activity", "ok", payload)
        return payload

    @router.post("/control/obd_request")
    def post_control_obd_request(
        request: ObdRequestRequest,
    ) -> dict[str, Any]:
        try:
            result = execute_ad_hoc_request(
                config.hardware.can,
                service=request.service,
                identifier=request.identifier,
                request_id=request.header,
                timeout_seconds=request.timeout_seconds,
                identifier_bytes=request.identifier_bytes,
                data=request.data_bytes(),
                response_id=request.response_id,
            )
        except AdHocRequestBusError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        payload = {"obd_request": result.to_dict()}
        publish_control_event("obd_request", "ok" if result.responded else "no_response", payload)
        return payload

    @router.get("/control/obd_discovery")
    def get_control_obd_discovery() -> dict[str, Any]:
        """Cached support-discovery state per vehicle (C06.6); live progress
        is under ``/status.obd_scheduler.discovery``."""
        return {"obd_discovery": registry_snapshot(time.monotonic())}

    @router.post("/control/obd_discovery/reset")
    def post_control_obd_discovery_reset() -> dict[str, Any]:
        """Invalidate every cached support map; the collector re-walks them
        on its next budget slots, in the phases the eligibility matrix admits."""
        version = reset_discovery()
        payload = {"obd_discovery": {"cache_version": version, "reset": True}}
        publish_control_event("obd_discovery.reset", "ok", payload)
        return payload

    @router.get("/control/passive")
    def get_control_passive() -> dict[str, Any]:
        return {"passive": tx_gate.snapshot()}

    @router.post("/control/passive")
    def post_control_passive(
        request: PassiveModeRequest,
    ) -> dict[str, Any]:
        previous = tx_gate.set_passive(
            request.enabled, source="control", ttl_seconds=request.ttl_seconds
        )
        payload = {"passive": tx_gate.snapshot(), "previous_enabled": previous}
        publish_control_event("passive", "ok", payload)
        return payload

    @router.get("/control/bus-budget")
    def get_control_bus_budget() -> dict[str, Any]:
        return {"bus_budget": bus_budget.snapshot()}

    @router.post("/control/bus-budget")
    def post_control_bus_budget(
        request: BusBudgetRequest,
    ) -> dict[str, Any]:
        try:
            previous = bus_budget.set_override(
                request.requests_per_second,
                source="control",
                ttl_seconds=request.ttl_seconds,
            )
        except bus_budget.BudgetOutOfRangeError as exc:
            # Schema bounds already cover the HTTP path; this catches a
            # constant drifting out of sync with the module's own limits.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload = {"bus_budget": bus_budget.snapshot(), "previous_requests_per_second": previous}
        publish_control_event("bus_budget", "ok", payload)
        return payload

    @router.get("/control/tuning-mode")
    def get_control_tuning_mode() -> dict[str, Any]:
        return {"tuning_mode": tuning_profile.snapshot()}

    @router.post("/control/tuning-mode")
    async def post_control_tuning_mode(
        request: TuningModeRequest,
    ) -> dict[str, Any]:
        payload = await set_tuning_mode(
            storage_writer,
            enabled=request.enabled,
            channels=request.channels,
            rate_hz=request.rate_hz,
        )
        publish_control_event("tuning_mode", "ok", payload)
        return payload

    return router
