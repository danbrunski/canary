"""``GET /control/tires``: tire wear from the trip digests' rolling circumference."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from src.control.tires import tire_status
from src.dashboard.routes.control.context import ControlContext, _require


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()

    @router.get("/control/tires")
    async def get_tires() -> dict[str, Any]:
        """Drives' rolling circumference, the current value and, given a gauge
        reading on the ledger, estimated tread depth with its uncertainty.
        Reads digest sidecars and the ledger only (no telemetry)."""
        ledger = _require(ctx.vehicle_ledger_store, "vehicle ledger")
        return await asyncio.to_thread(tire_status, ctx.config.collection.storage_path, ledger)

    return router
