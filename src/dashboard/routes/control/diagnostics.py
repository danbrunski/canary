"""Diagnostic functions: one-shot routines a scan tool runs on a module.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).

Today: reading and clearing a K-line module's stored codes, each offered for a
module whose vehicle profile records how (``KlineModule.read_codes`` /
``KlineModule.clear_codes``; the airbag module on the Xterra). The collector
that owns the K-line port runs them (:mod:`src.collection.kline.actions`).
Reading and clearing codes are routine diagnostic work on any module
(AGENTS.md), so there is no vehicle-state gate.
"""

from __future__ import annotations

from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from fastapi import APIRouter, HTTPException

from src.collection import tx_gate
from src.collection.kline.actions import (
    KLINE_ACTIONS,
    ActionKind,
    KlineAction,
    KlineActionBusyError,
    KlineActionUnavailableError,
)
from src.collection.kline.profile import (
    KlineModule,
    KlineNotConfiguredError,
    KlineProfile,
    load_kline_profile,
)
from src.collection.kline.sessions import MODULE_SESSIONS
from src.dashboard.routes.control.context import ControlContext
from src.dashboard.schemas import ClearCodesRequest, ReadCodesRequest

#: How long the route waits for the collector: its next check (≤0.5 s), a
#: wake-up and StartCommunication (~0.5 s), the function (the airbag module
#: answered a clear with responsePending for ~0.35 s and each code read in
#: ~15 ms; the transport allows 5 s per request) and StopCommunication, with
#: room to spare.
CLEAR_TIMEOUT_SECONDS = 20.0
#: What each function is called in a refusal, by kind.
_WORDS: dict[ActionKind, tuple[str, str]] = {
    "read_codes": ("read", "read its codes"),
    "clear_codes": ("clear", "clear it"),
}


def _line_holder() -> str | None:
    """Why the K-line is not ours to use right now, or ``None``."""
    if tx_gate.passive_armed():
        return "passive mode is armed"
    if MODULE_SESSIONS.foreign_active() or tx_gate.ecm_session_owner() == "foreign":
        return "another tester is on the K-line"
    return None


def _action(kind: ActionKind, label: str, address: int, module: KlineModule | None) -> KlineAction:
    """The queued function for ``module``, or the 409 that names what the
    vehicle profile does not record."""
    if module is not None and kind == "read_codes" and module.read_codes is not None:
        return KlineAction(
            kind=kind, module=module.name, address=address, read_codes=module.read_codes
        )
    if module is not None and kind == "clear_codes" and module.clear_codes is not None:
        return KlineAction(
            kind=kind,
            module=module.name,
            address=address,
            request=bytes(module.clear_codes.request),
        )
    raise HTTPException(
        status_code=409,
        detail=(
            f"{label}: the vehicle profile records no {kind} request for this module, "
            f"so CANary does not know how to {_WORDS[kind][1]}"
        ),
    )


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
        functions: list[dict[str, Any]] = []
        if loaded is not None:
            for address, module in sorted(loaded.modules.items()):
                base = {"transport": "kline", "module": module.name, "address": f"0x{address:02X}"}
                if module.read_codes is not None:
                    functions.append(
                        {
                            "kind": "read_codes",
                            **base,
                            "request_hex": ", ".join(
                                read.request_bytes().hex(" ") for read in module.read_codes.requests
                            ),
                            "requests": [
                                {
                                    "request_hex": read.request_bytes().hex(" "),
                                    "status": read.status,
                                    "status_confidence": read.status_confidence,
                                }
                                for read in module.read_codes.requests
                            ],
                            "evidence": module.read_codes.evidence,
                        }
                    )
                if module.clear_codes is not None:
                    functions.append(
                        {
                            "kind": "clear_codes",
                            **base,
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

    def run(kind: ActionKind, label: str) -> dict[str, Any]:
        loaded = profile()
        if loaded is None:
            raise HTTPException(status_code=409, detail="no K-line profile for this vehicle")
        try:
            address = loaded.module_address(label)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        module = loaded.modules.get(address)
        action = _action(kind, label, address, module)
        blocked = _line_holder()
        if blocked is not None:
            raise HTTPException(status_code=409, detail=f"K-line unavailable: {blocked}")
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
                else f"the {_WORDS[kind][0]} started but did not finish in time; "
                "its outcome is unknown"
            )
            publish_control_event(kind, "timeout", {"module": action.module, "detail": detail})
            raise HTTPException(status_code=504, detail=detail) from exc
        payload: dict[str, Any] = {kind: result.to_dict()}
        publish_control_event(kind, "ok" if result.ok else "failed", payload)
        return payload

    @router.post("/control/diagnostics/read_codes")
    def post_control_read_codes(request: ReadCodesRequest) -> dict[str, Any]:
        return run("read_codes", request.module)

    @router.post("/control/diagnostics/clear_codes")
    def post_control_clear_codes(request: ClearCodesRequest) -> dict[str, Any]:
        return run("clear_codes", request.module)

    return router
