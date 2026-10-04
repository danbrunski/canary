"""Diagnostic functions: one-shot routines a scan tool runs on a module.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).

Today: clearing a K-line module's stored codes, offered for each module whose
vehicle profile records how (``KlineModule.clear_codes``; the airbag module on
the Xterra). The collector that owns the K-line port runs it
(:mod:`src.collection.kline.actions`). Clearing codes is routine diagnostic
work on any module (AGENTS.md), so there is no vehicle-state gate.
"""

from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from fastapi import APIRouter, HTTPException

from src.collection import tx_gate
from src.collection.kline.actions import (
    KLINE_ACTIONS,
    KlineAction,
    KlineActionBusyError,
    KlineActionUnavailableError,
)
from src.collection.kline.profile import KlineNotConfiguredError, KlineProfile, load_kline_profile
from src.collection.kline.sessions import MODULE_SESSIONS
from src.dashboard.routes.control.context import ControlContext
from src.dashboard.schemas import ClearCodesRequest

#: How long the route waits for the collector: its next check (≤0.5 s), a
#: wake-up and StartCommunication (~0.5 s), the clear (the airbag module
#: answered responsePending for ~0.35 s; the transport allows 5 s) and
#: StopCommunication, with room to spare.
CLEAR_TIMEOUT_SECONDS = 20.0


def _line_holder() -> str | None:
    """Why the K-line is not ours to use right now, or ``None``."""
    if tx_gate.passive_armed():
        return "passive mode is armed"
    if MODULE_SESSIONS.foreign_active() or tx_gate.ecm_session_owner() == "foreign":
        return "another tester is on the K-line"
    return None


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    config = ctx.config
    publish_control_event = ctx.publish_control_event

    def profile() -> KlineProfile | None:
        try:
            return load_kline_profile(config.daemon.kline_profile)
        except (KlineNotConfiguredError, ValueError):
            return None

    @router.get("/control/diagnostics/functions")
    def get_control_diagnostic_functions() -> dict[str, Any]:
        loaded = profile()
        functions = []
        if loaded is not None:
            for address, module in sorted(loaded.modules.items()):
                if module.clear_codes is None:
                    continue
                functions.append(
                    {
                        "kind": "clear_codes",
                        "transport": "kline",
                        "module": module.name,
                        "address": f"0x{address:02X}",
                        "request_hex": bytes(module.clear_codes.request).hex(" "),
                        "evidence": module.clear_codes.evidence,
                    }
                )
        return {
            "diagnostic_functions": {
                "functions": functions,
                "kline_available": KLINE_ACTIONS.available,
                "blocked_reason": _line_holder(),
            }
        }

    @router.post("/control/diagnostics/clear_codes")
    def post_control_clear_codes(request: ClearCodesRequest) -> dict[str, Any]:
        loaded = profile()
        if loaded is None:
            raise HTTPException(status_code=409, detail="no K-line profile for this vehicle")
        try:
            address = loaded.module_address(request.module)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        module = loaded.modules.get(address)
        if module is None or module.clear_codes is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{request.module}: the vehicle profile records no clear_codes request "
                    "for this module, so CANary does not know how to clear it"
                ),
            )
        blocked = _line_holder()
        if blocked is not None:
            raise HTTPException(status_code=409, detail=f"K-line unavailable: {blocked}")
        action = KlineAction(
            kind="clear_codes",
            module=module.name,
            address=address,
            request=bytes(module.clear_codes.request),
        )
        try:
            future = KLINE_ACTIONS.submit(action)
        except KlineActionUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except KlineActionBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            result = future.result(timeout=CLEAR_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            withdrawn = KLINE_ACTIONS.cancel(action)
            detail = (
                "the K-line collector did not pick the request up; withdrawn, nothing was sent"
                if withdrawn
                else "the clear started but did not finish in time; its outcome is unknown"
            )
            publish_control_event(
                "clear_codes", "timeout", {"module": module.name, "detail": detail}
            )
            raise HTTPException(status_code=504, detail=detail) from exc
        payload = {"clear_codes": result.to_dict()}
        publish_control_event("clear_codes", "ok" if result.ok else "failed", payload)
        return payload

    return router
