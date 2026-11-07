"""Network control: what the Pi joined (P2.3), the BLE provisioning window (N1.2),
and the saved WiFi networks (N5.3).

The root ``canary-provision`` service accepts WiFi credentials over Bluetooth
only while the Pi has no WiFi profile or a window is open. These routes open
and read that window; they need the control token like every ``/control/*``
route, which is what "opened by someone who already has access" means.

``GET /control/network`` answers "which network is it on, and at what
address" -- the BLE status characteristic's facts -- so a phone learns the IP
it should bookmark (Android does not resolve ``canary.local``).

``/control/network/wifi`` lists, adds, orders, connects and forgets the saved
networks. The daemon runs unprivileged and NetworkManager's system
connections are root's, so every one of these asks the root service over its
Unix socket (:mod:`src.provisioning.client`) and returns what it answers. A
service that cannot be reached is a 503 naming the socket, never an empty
list: "nothing saved" and "nobody to ask" demand opposite responses.

**A passphrase is in the body of ``POST /control/network/wifi`` and nowhere
else.** It is forwarded to the socket and dropped: not in the response, not in
the ``network.wifi_changed`` event or the control ledger it feeds, and not in
a validation error (FastAPI would echo the failing body, so the body is
validated by hand with the input stripped).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.dashboard.routes.control.context import ControlContext
from src.dashboard.schemas import ProvisioningWindowRequest
from src.provisioning.client import (
    KIND_BAD_REPLY,
    KIND_TIMEOUT,
    KIND_UNREACHABLE,
    WifiClient,
    WifiServiceError,
)
from src.provisioning.gate import MAX_WINDOW_MINUTES, read_window_remaining, write_window
from src.provisioning.netsock import KIND_UNAUTHORIZED
from src.provisioning.reader import NetworkReading

#: Reported when the app was built without a reader (tests, bare apps).
NOT_WIRED = "network status is not wired into this app"

#: Published on ``/ws/control`` (and written to the control ledger) by every
#: change to the saved networks, successful or refused. Never a passphrase.
WIFI_EVENT = "network.wifi_changed"
_MAX_SSID_BYTES = 32

#: The service's failure kinds (``src.provisioning.network.KIND_*``) as HTTP.
_STATUS = {
    "not_found": 404,
    "refused": 409,
    "join_failed": 409,
    "invalid": 422,
    KIND_UNAUTHORIZED: 503,
    KIND_UNREACHABLE: 503,
    KIND_TIMEOUT: 504,
    KIND_BAD_REPLY: 502,
    "internal": 502,
}


class WifiAddRequest(BaseModel):
    """``join`` has no default: switching the radio to a new network is the
    caller's decision. ``password`` is null for an open network."""

    model_config = ConfigDict(extra="forbid")

    ssid: str = Field(min_length=1)
    password: str | None = Field(default=None, max_length=256)
    hidden: bool = False
    join: bool

    @field_validator("ssid")
    @classmethod
    def _ssid_fits(cls, ssid: str) -> str:
        try:
            size = len(ssid.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("a network name must be valid text") from None
        if size > _MAX_SSID_BYTES:
            raise ValueError(f"a network name is at most {_MAX_SSID_BYTES} bytes, not {size}")
        return ssid


class WifiOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[str]


def _add_request(body: Annotated[dict[str, Any], Body()]) -> WifiAddRequest:
    """Validate the add body without letting an error carry it back.

    FastAPI's own 422 lists every failing field's ``input``, and for a missing
    field that input is the whole body, passphrase included.
    """
    try:
        return WifiAddRequest.model_validate(body)
    except ValidationError as exc:
        problems = exc.errors(include_input=False, include_url=False, include_context=False)
        # One sentence, like every other refusal here: the page shows `detail`
        # as text, and FastAPI's list form repeats the failing input.
        raise HTTPException(
            status_code=422,
            detail="; ".join(
                f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}"
                for problem in problems
            ),
        ) from None


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    window_path = ctx.provision_window_path

    def _snapshot() -> dict[str, Any]:
        return {
            "window_remaining_s": read_window_remaining(window_path),
            "max_minutes": MAX_WINDOW_MINUTES,
        }

    @router.get("/control/network")
    def get_network() -> dict[str, Any]:
        # A sync route: FastAPI runs it in its threadpool, so the nmcli forks
        # (cached and single-flight in the reader) never block the event loop.
        reader = ctx.network_reader
        reading = (
            reader.read()
            if reader is not None
            else NetworkReading(status=None, error=NOT_WIRED, read_at=time.time())
        )
        return {
            "network": reading.to_dict(dashboard_port=ctx.config.dashboard.port),
            "provisioning": _snapshot(),
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

    # --- saved WiFi networks (N5.3) ---------------------------------------
    #
    # Sync routes: FastAPI runs them in its threadpool, so a join that takes
    # 30 s blocks a worker thread and never the event loop.

    wifi = WifiClient(ctx.network_socket_path)

    def _http(
        exc: WifiServiceError,
        *,
        secret: str | None = None,
        statuses: dict[str, int] | None = None,
    ) -> HTTPException:
        detail = exc.detail
        if exc.kind == KIND_UNAUTHORIZED:
            detail = (
                f"WiFi control service at {wifi.path} refused this daemon; "
                "the user it runs as must be in the canary group"
            )
        if secret and len(secret) >= 4:
            detail = detail.replace(secret, "***")
        status = {**_STATUS, **(statuses or {})}.get(exc.kind, 502)
        return HTTPException(status_code=status, detail=detail)

    def _read(ask: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return ask()
        except WifiServiceError as exc:
            raise _http(exc) from None

    def _change(
        op: str,
        subject: dict[str, Any],
        ask: Callable[[], dict[str, Any]],
        *,
        secret: str | None = None,
        statuses: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Run a change, and record it whether or not it happened.

        ``subject`` names what was changed (the SSID or profile id). It is
        built by the caller from non-secret fields only; the passphrase is
        never in scope here except as ``secret``, to scrub it from an error.
        """
        try:
            reply = ask()
        except WifiServiceError as exc:
            problem = _http(exc, secret=secret, statuses=statuses)
            ctx.publish_control_event(
                WIFI_EVENT,
                "failed",
                {"op": op, **subject, "ok": False, "error": exc.kind, "detail": problem.detail},
            )
            raise problem from None
        ctx.publish_control_event(WIFI_EVENT, "ok", {"op": op, **subject, "ok": True})
        return reply

    @router.get("/control/network/wifi")
    def get_wifi() -> dict[str, Any]:
        return {"wifi": _read(wifi.state)["wifi"]}

    @router.post("/control/network/wifi/scan")
    def scan_wifi() -> dict[str, Any]:
        return {"wifi": _read(wifi.scan)["wifi"]}

    @router.post("/control/network/wifi")
    def add_wifi(request: Annotated[WifiAddRequest, Depends(_add_request)]) -> dict[str, Any]:
        reply = _change(
            "add",
            {"ssid": request.ssid, "join": request.join, "hidden": request.hidden},
            lambda: wifi.add(
                request.ssid, request.password or None, hidden=request.hidden, join=request.join
            ),
            secret=request.password,
        )
        return {"network": reply["network"], "wifi": reply["wifi"]}

    @router.post("/control/network/wifi/{network_id}/connect")
    def connect_wifi(network_id: Annotated[str, Path(max_length=128)]) -> dict[str, Any]:
        reply = _change("connect", {"id": network_id}, lambda: wifi.connect(network_id))
        return {"wifi": reply["wifi"]}

    @router.put("/control/network/wifi/order")
    def order_wifi(request: WifiOrderRequest) -> dict[str, Any]:
        # The ids not being exactly the saved set is a conflict with what is
        # saved (409), not a malformed body (422): the list may have changed
        # under the page since it loaded.
        reply = _change(
            "order",
            {"ids": request.ids},
            lambda: wifi.order(request.ids),
            statuses={"invalid": 409},
        )
        return {"wifi": reply["wifi"]}

    @router.delete("/control/network/wifi/{network_id}")
    def forget_wifi(network_id: Annotated[str, Path(max_length=128)]) -> dict[str, Any]:
        reply = _change("forget", {"id": network_id}, lambda: wifi.forget(network_id))
        return {"wifi": reply["wifi"]}

    return router
