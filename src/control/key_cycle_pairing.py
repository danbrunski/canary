"""Pairing by key cycle: ACC -> ON -> ACC proves someone is in the car.

docs/roadmap/phone-ui.md P2.4 (operator decision 2026-10-12). A device asks to
pair (``POST /pair/request``) and the dashboard tells the person to turn the
key from ACC to ON and back to ACC. Nobody outside the car can do that, and
nothing secret crosses Bluetooth or the network for it.

Why ACC and not OFF: most users power the Pi from a switched lighter socket,
so OFF would power the Pi down mid-gesture. The ECM is what tells the states
apart. It answers OBD at ON (``vehicle_state`` ``ignition_only`` or
``engine_running``) and is silent at ACC.

**Which request a cycle approves.** A key cycle cannot say which phone it is
for, so approval is all-or-nothing:

- Exactly one request waiting, made before the key reached ON: approved.
- More than one waiting: every one fails as a conflict, naming the others.
  A stranger who asks at the same moment as the owner causes a visible
  retry, never a silent approval.
- None waiting: the cycle opens the BLE provisioning window instead
  (``src.provisioning.gate``), so changing WiFi later needs only the car.

**Approval by someone already trusted.** A paired device, or a holder of the
shared token (``canaryctl pair`` on a bench Pi), may approve one waiting
request by its id (:meth:`KeyCyclePairing.approve`). The conflict rule does
not apply: the approver chose that request by name, which is exactly what a
key cycle cannot do. The new device's ``method`` records who approved it.

Evidence that is not the car never counts. ``replay`` re-emits a stored
session, ``passive`` means CANary is not asking the ECM, and ``unknown`` is
unclassified. Any of them resets the detector.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from src.control.paired_devices import PairedDevices, clean_name
from src.provisioning.gate import write_window

log = logging.getLogger(__name__)

ON_STATES = frozenset({"ignition_only", "engine_running"})
#: States that are not evidence about the key.
UNTRUSTED_STATES = frozenset({"replay", "passive", "unknown"})
#: ON held longer than this is driving, not a pairing gesture. It includes the
#: classifier's ~10 s lag in noticing the ECM has gone quiet.
MAX_ON_S = 60.0
REQUEST_TTL_S = 120.0
#: Requests are anonymous, so cap them: a flood cannot grow memory, and it
#: can only cause conflicts, never an approval.
MAX_PENDING = 4
TICK_S = 1.0
WINDOW_MINUTES_ON_IDLE_CYCLE = 5.0

Phase = Literal["unknown", "acc", "on", "on_unanchored"]
Step = Literal["set_acc", "turn_on", "back_to_acc", "unavailable"]


class KeyCycleDetector:
    """Watches classified vehicle states for ACC -> ON -> ACC.

    ``on_unanchored`` is ON without a preceding ACC, or held past
    :data:`MAX_ON_S`: it must return to ACC before a cycle can start.
    """

    def __init__(self) -> None:
        self.phase: Phase = "unknown"
        self.on_since: float | None = None

    def reset(self) -> None:
        self.phase, self.on_since = "unknown", None

    def update(self, state: str, now: float) -> float | None:
        """Feed one sample; on a completed cycle, return when ON began."""
        if state in UNTRUSTED_STATES:
            self.reset()
            return None
        on = state in ON_STATES
        if self.phase == "on" and self.on_since is not None:
            if not on:
                began = self.on_since
                self.phase, self.on_since = "acc", None
                return began if now - began <= MAX_ON_S else None
            if now - self.on_since > MAX_ON_S:
                self.phase, self.on_since = "on_unanchored", None
            return None
        if on:
            if self.phase == "acc":
                self.phase, self.on_since = "on", now
            elif self.phase == "unknown":
                self.phase = "on_unanchored"
            return None
        self.phase, self.on_since = "acc", None
        return None


@dataclass(frozen=True)
class PairingRequest:
    id: str
    secret: str
    name: str
    created_at: float
    #: ``waiting``, ``approved``, ``conflict`` or ``expired``.
    status: str = "waiting"
    detail: str = ""
    #: Who approved it when it was not a key cycle: a paired device's id, or
    #: ``shared-token``. ``None`` for a key-cycle approval.
    approved_by: str | None = None

    @property
    def method(self) -> str:
        """The paired device's ``method``: how this request was approved."""
        return "key-cycle" if self.approved_by is None else f"approved-by:{self.approved_by}"


@dataclass(frozen=True)
class PendingRequest:
    """A waiting request as a trusted caller sees it: never its secret."""

    id: str
    name: str
    age_s: float
    expires_in_s: float

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Observed:
    state: str | None = None
    available: bool = False
    detail: str = ""


class KeyCyclePairing:
    def __init__(
        self,
        devices: PairedDevices,
        vehicle_state: Callable[[], str | None],
        *,
        window_path: Path,
        publish: Callable[[str, str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._devices = devices
        self._vehicle_state = vehicle_state
        self._window_path = window_path
        self._publish = publish or (lambda action, result, payload: None)
        self._clock = clock
        self._detector = KeyCycleDetector()
        self._requests: dict[str, PairingRequest] = {}
        self._observed = _Observed()
        self._lock = threading.Lock()
        self._task: asyncio.Task[None] | None = None

    # --- requests -----------------------------------------------------------

    def request(self, name: str) -> PairingRequest:
        with self._lock:
            self._expire(self._clock())
            waiting = [r for r in self._requests.values() if r.status == "waiting"]
            if len(waiting) >= MAX_PENDING:
                raise OverflowError("too many pairing requests are waiting; try again shortly")
            req = PairingRequest(
                id=secrets.token_hex(8),
                secret=secrets.token_urlsafe(24),
                name=clean_name(name),
                created_at=self._clock(),
            )
            self._requests[req.id] = req
        self._publish("pairing.requested", "ok", {"id": req.id, "name": req.name})
        return req

    def _owned(self, request_id: str, secret: str) -> PairingRequest | None:
        req = self._requests.get(request_id)
        if req is None or not secrets.compare_digest(req.secret.encode(), secret.encode()):
            return None
        return req

    def status(self, request_id: str, secret: str) -> dict[str, Any] | None:
        """The request's progress; an approved one is collected, exactly once.

        The device is created at collection, not at approval, so an approval
        nobody collects leaves nothing behind.
        """
        with self._lock:
            self._expire(self._clock())
            req = self._owned(request_id, secret)
            if req is None:
                return None
            payload: dict[str, Any] = {
                "id": req.id,
                "status": req.status,
                "detail": req.detail,
                "vehicle_state": self._observed.state,
                "expires_in_s": max(0.0, req.created_at + REQUEST_TTL_S - self._clock()),
            }
            if req.status == "waiting":
                payload["step"] = self._step(req)
                if not self._observed.available:
                    payload["detail"] = self._observed.detail
                return payload
            if req.status == "approved":
                # Paired before the request is dropped: if the disk refuses,
                # the approval survives for the next poll.
                device, token = self._devices.pair(req.name, method=req.method)
            del self._requests[req.id]
            if req.status != "approved":
                return payload
        payload["token"] = token
        payload["device"] = device.public()
        self._publish("pairing.paired", "ok", {"device": device.public()})
        return payload

    def cancel(self, request_id: str, secret: str) -> bool:
        with self._lock:
            if self._owned(request_id, secret) is None:
                return False
            del self._requests[request_id]
            return True

    def pending(self) -> list[PendingRequest]:
        """Waiting requests, oldest first, for a trusted caller to choose from."""
        with self._lock:
            now = self._clock()
            self._expire(now)
            waiting = sorted(
                (r for r in self._requests.values() if r.status == "waiting"),
                key=lambda r: r.created_at,
            )
            return [
                PendingRequest(
                    id=r.id,
                    name=r.name,
                    age_s=now - r.created_at,
                    expires_in_s=max(0.0, r.created_at + REQUEST_TTL_S - now),
                )
                for r in waiting
            ]

    def approve(self, request_id: str, *, by: str) -> bool:
        """Approve one waiting request chosen by an already-trusted caller.

        ``by`` names the approver (a paired device's id, or ``shared-token``)
        and becomes the device's ``method``. The key-cycle conflict rule does
        not apply: the approver picked this request, a key cycle cannot.
        Returns False when the request exists but is no longer waiting
        (approved, a conflict, expired); raises :class:`KeyError` when there
        is no such request at all.
        """
        with self._lock:
            self._expire(self._clock())
            req = self._requests.get(request_id)
            if req is None:
                raise KeyError(request_id)
            if req.status != "waiting":
                return False
            self._requests[req.id] = replace(
                req, status="approved", approved_by=by, detail=f"Approved by {by}."
            )
        log.warning("pairing request %s (%s) approved by %s", req.id, req.name, by)
        self._publish("pairing.approved", "ok", {"id": req.id, "name": req.name, "by": by})
        return True

    def _step(self, req: PairingRequest) -> Step:
        if not self._observed.available:
            return "unavailable"
        phase, on_since = self._detector.phase, self._detector.on_since
        if phase == "acc":
            return "turn_on"
        if phase == "on" and on_since is not None and on_since >= req.created_at:
            return "back_to_acc"
        return "set_acc"

    def _expire(self, now: float) -> None:
        # A waiting request expires at the TTL; any outcome stays readable for
        # one more TTL so the phone polling it learns what happened.
        for req in list(self._requests.values()):
            age = now - req.created_at
            if age > 2 * REQUEST_TTL_S:
                del self._requests[req.id]
            elif age > REQUEST_TTL_S and req.status == "waiting":
                self._requests[req.id] = replace(req, status="expired")

    # --- the tick -------------------------------------------------------------

    def tick(self) -> None:
        now = self._clock()
        try:
            state = self._vehicle_state()
        except Exception:  # a failed read is "unknown", never a cycle
            log.exception("key-cycle pairing: vehicle state unavailable")
            state = None
        with self._lock:
            self._observe(state)
            began = None
            before, on_since = self._detector.phase, self._detector.on_since
            if state is None:
                self._detector.reset()
            else:
                began = self._detector.update(state, now)
            self._log_phase_change(before, on_since, state, now)
            self._expire(now)
            if began is not None:
                self._cycle_completed(began)

    def _log_phase_change(
        self, before: Phase, on_since: float | None, state: str | None, now: float
    ) -> None:
        """One journal line per phase change: the truck check (phone-ui P2.4)
        compares when the key moved with when the Pi saw it, and an ACC that
        reads as ON shows up here as a phase the key was not in."""
        after = self._detector.phase
        if after == before:
            return
        held = f" after {now - on_since:.1f} s ON" if before == "on" and on_since else ""
        log.info("key phase %s -> %s (vehicle_state %s)%s", before, after, state, held)

    def _observe(self, state: str | None) -> None:
        self._observed.state = state
        if state is None:
            self._observed.available, self._observed.detail = (
                False,
                "The daemon has no live vehicle data, so it cannot see the key.",
            )
        elif state in UNTRUSTED_STATES:
            reason = {
                "replay": "A replay is running; stop it so the Pi sees the real car.",
                "passive": "Passive mode is on: the Pi is not asking the ECM, so it cannot "
                "see the key.",
                "unknown": "The Pi cannot classify the vehicle right now.",
            }[state]
            self._observed.available, self._observed.detail = False, reason
        else:
            self._observed.available, self._observed.detail = True, ""

    def _cycle_completed(self, on_began: float) -> None:
        waiting = [r for r in self._requests.values() if r.status == "waiting"]
        if not waiting:
            try:
                write_window(WINDOW_MINUTES_ON_IDLE_CYCLE, self._window_path)
            except OSError:
                log.exception("key cycle: could not open the provisioning window")
                return
            log.warning("key cycle with no pairing request: BLE provisioning window opened")
            self._publish("provisioning.window_opened", "ok", {"by": "key-cycle"})
            return
        if len(waiting) > 1:
            names = ", ".join(r.name for r in waiting)
            for req in waiting:
                self._requests[req.id] = replace(
                    req,
                    status="conflict",
                    detail=f"{len(waiting)} devices asked to pair at once ({names}). "
                    "None was paired. Try again.",
                )
            log.warning("key cycle: %d pairing requests waiting, none approved", len(waiting))
            self._publish("pairing.conflict", "refused", {"names": names})
            return
        (req,) = waiting
        if req.created_at > on_began:
            # Asked after the key was already ON: this cycle is not its proof.
            return
        self._requests[req.id] = replace(req, status="approved")
        log.warning("key cycle approved pairing request %s (%s)", req.id, req.name)
        # Lets every other device's approval prompt close (approve() does the same).
        self._publish("pairing.approved", "ok", {"id": req.id, "name": req.name, "by": "key-cycle"})

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            self.tick()

    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
