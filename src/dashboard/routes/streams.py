from __future__ import annotations

import asyncio

from fastapi import Depends, FastAPI, WebSocket

from src.control.paired_devices import PairedDevices
from src.daemon.broadcast import Broadcaster
from src.dashboard.security import control_caller, websocket_control_auth_dependency
from src.dashboard.websockets import _run_websocket


def register_stream_routes(
    app: FastAPI,
    *,
    telemetry_bus: Broadcaster,
    anomalies_bus: Broadcaster,
    scores_bus: Broadcaster,
    control_bus: Broadcaster,
    resolved_control_token: str | None,
) -> None:
    @app.websocket("/ws/telemetry")
    async def ws_telemetry(websocket: WebSocket) -> None:
        await _run_websocket(websocket, telemetry_bus)

    @app.websocket("/ws/anomalies")
    async def ws_anomalies(websocket: WebSocket) -> None:
        await _run_websocket(websocket, anomalies_bus)

    @app.websocket("/ws/scores")
    async def ws_scores(websocket: WebSocket) -> None:
        # Every inference tick (both models), not just anomalies — powers
        # the live per-model score readout.
        await _run_websocket(websocket, scores_bus)

    @app.websocket(
        "/ws/control",
        dependencies=[Depends(websocket_control_auth_dependency(resolved_control_token))],
    )
    async def ws_control(websocket: WebSocket) -> None:
        app.state.control_loop = asyncio.get_running_loop()
        caller = control_caller(websocket)
        devices = getattr(app.state, "paired_devices", None)
        device = caller.device
        still_authorized = (
            (lambda: devices.is_paired(device.id))
            if device is not None and isinstance(devices, PairedDevices)
            else None
        )
        await _run_websocket(websocket, control_bus, still_authorized=still_authorized)
