"""``GET /control/tyres``: tyre wear from the trip digests' rolling circumference."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

from src.control.tyres import tyre_status
from src.dashboard.routes.control.context import ControlContext, _require


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()

    @router.get("/control/tyres")
    async def get_tyres() -> dict[str, Any]:
        """Drives' rolling circumference, the current value and, given a gauge
        reading on the ledger, estimated tread depth with its uncertainty.
        Reads digest sidecars and the ledger only (no telemetry)."""
        ledger = _require(ctx.vehicle_ledger_store, "vehicle ledger")
        return await asyncio.to_thread(tyre_status, ctx.config.collection.storage_path, ledger)

    return router
