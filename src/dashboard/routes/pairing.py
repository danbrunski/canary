"""Pairing a phone or laptop with the Pi (docs/roadmap/phone-ui.md P2.4).

A device without a token asks to pair (``POST /pair/request``), then polls its
request while the person in the car turns the key ACC -> ON -> ACC. The
request routes carry no control token, because they are how a device gets
one. Holding a request proves nothing. The key cycle is the proof, and
``src.control.key_cycle_pairing`` decides which request, if any, it approves.
A request's own secret only stops another device reading or cancelling it.

Listing, renaming and removing paired devices need the control token like
any other ``/control/*`` route, and a paired device's token counts. So does
approving a waiting request by its id (``/control/pairing/requests``): a
trusted caller, a paired phone or ``canaryctl pair`` on a bench Pi, vouches
for the new device instead of the key, and is recorded as its approver.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request

from src.control.key_cycle_pairing import KeyCyclePairing
from src.control.paired_devices import PairedDevices
from src.dashboard.schemas import PairedDeviceRename, PairingRequestCreate
from src.dashboard.security import control_auth_dependency, control_caller

#: Sent as a header, not a query string, so access logs never record it.
SECRET_HEADER = "X-Pairing-Secret"


def register_pairing_routes(
    app: FastAPI,
    *,
    devices: PairedDevices,
    pairing: KeyCyclePairing,
    resolved_control_token: str | None,
    publish_control_event: Callable[[str, str, dict[str, Any]], None],
) -> None:
    public = APIRouter()

    @public.post("/pair/request", status_code=201)
    async def post_pair_request(request: PairingRequestCreate) -> dict[str, Any]:
        pairing.ensure_running()
        try:
            req = pairing.request(request.name)
        except OverflowError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return {"id": req.id, "secret": req.secret}

    @public.get("/pair/request/{request_id}")
    def get_pair_request(
        request_id: str, secret: Annotated[str, Header(alias=SECRET_HEADER)]
    ) -> dict[str, Any]:
        try:
            payload = pairing.status(request_id, secret)
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"approved, but the pairing could not be saved: {exc}"
            ) from exc
        if payload is None:
            raise HTTPException(status_code=404, detail="no such pairing request")
        return payload

    @public.delete("/pair/request/{request_id}")
    def delete_pair_request(
        request_id: str, secret: Annotated[str, Header(alias=SECRET_HEADER)]
    ) -> dict[str, Any]:
        if not pairing.cancel(request_id, secret):
            raise HTTPException(status_code=404, detail="no such pairing request")
        return {"cancelled": request_id}

    guarded = APIRouter(dependencies=[Depends(control_auth_dependency(resolved_control_token))])

    @guarded.get("/control/devices")
    def get_devices() -> dict[str, Any]:
        return {"devices": [d.public() for d in devices.list()]}

    @guarded.get("/control/devices/me")
    def get_device_me(request: Request) -> dict[str, Any]:
        """Who this caller is: its paired device, or the shared token."""
        caller = control_caller(request)
        return {
            "kind": caller.kind,
            "shared_token": caller.kind == "shared-token",
            "device": None if caller.device is None else caller.device.public(),
        }

    @guarded.patch("/control/devices/{device_id}")
    def patch_device(device_id: str, body: PairedDeviceRename) -> dict[str, Any]:
        try:
            renamed = devices.rename(device_id, body.name)
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"could not save the new name: {exc}"
            ) from exc
        if renamed is None:
            raise HTTPException(status_code=404, detail=f"no paired device {device_id!r}")
        publish_control_event("pairing.renamed", "ok", {"device": renamed.public()})
        return {"device": renamed.public()}

    @guarded.delete("/control/devices/{device_id}")
    def delete_device(device_id: str) -> dict[str, Any]:
        try:
            removed = devices.remove(device_id)
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"could not save the removal; {device_id!r} still works: {exc}",
            ) from exc
        if removed is None:
            raise HTTPException(status_code=404, detail=f"no paired device {device_id!r}")
        publish_control_event("pairing.removed", "ok", {"device": removed.public()})
        return {"removed": removed.public()}

    @guarded.get("/control/pairing/requests")
    def get_pairing_requests() -> dict[str, Any]:
        """Requests waiting to pair, oldest first; never their secrets."""
        return {"requests": [r.public() for r in pairing.pending()]}

    @guarded.post("/control/pairing/requests/{request_id}/approve")
    def approve_pairing_request(request_id: str, request: Request) -> dict[str, Any]:
        caller = control_caller(request)
        try:
            approved = pairing.approve(request_id, by=caller.label)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"no pairing request {request_id!r}"
            ) from exc
        if not approved:
            raise HTTPException(
                status_code=409,
                detail=f"pairing request {request_id!r} is no longer waiting "
                "(already approved, a conflict, or expired)",
            )
        return {"approved": request_id, "by": caller.label}

    app.include_router(public)
    app.include_router(guarded)
