"""Network provisioning control: the BLE provisioning window (roadmap N1.2).

The root ``canary-provision`` service accepts WiFi credentials over Bluetooth
only while the Pi has no WiFi profile or a window is open. These routes open
and read that window; they need the control token like every ``/control/*``
route, which is what "opened by someone who already has access" means.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from src.dashboard.routes.control.context import ControlContext
from src.dashboard.schemas import ProvisioningWindowRequest
from src.provisioning.gate import MAX_WINDOW_MINUTES, read_window_remaining, write_window


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    window_path = ctx.provision_window_path

    def _snapshot() -> dict[str, Any]:
        return {
            "window_remaining_s": read_window_remaining(window_path),
            "max_minutes": MAX_WINDOW_MINUTES,
        }

    @router.get("/control/network/provisioning")
    def get_provisioning() -> dict[str, Any]:
        return {"provisioning": _snapshot()}

    @router.post("/control/network/provisioning/window")
    def post_provisioning_window(request: ProvisioningWindowRequest) -> dict[str, Any]:
        try:
            expiry = write_window(request.minutes, window_path)
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"provisioning window file not writable ({window_path}): {exc}",
            ) from exc
        payload = {"provisioning": _snapshot(), "expiry": expiry}
        ctx.publish_control_event("network.provisioning_window", "ok", payload)
        return payload

    return router
