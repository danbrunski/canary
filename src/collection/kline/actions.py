"""One-shot K-line diagnostic functions, run by the collector that owns the port.

The serial port belongs to :class:`~src.collection.kline_collector.KlineCollector`
(sniffing, or reading a module for a capture profile), and K-line is one wire,
so a control route cannot open a second handle the way the CAN ad-hoc request
does. It hands the function to :data:`KLINE_ACTIONS` instead; the collector
stops sniffing (or ends its read session) at its next check, runs the
function in a session of its own, and resumes.

One function at a time: a second request while one is queued or running is
refused, not queued behind it.

Today there is one function, clearing a module's stored codes, and only for a
module whose vehicle profile records how (``KlineModule.clear_codes``).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Literal

ActionKind = Literal["clear_codes"]


class KlineActionBusyError(RuntimeError):
    """Another function is queued or running."""


class KlineActionUnavailableError(RuntimeError):
    """No K-line collector is running to carry the function out."""


@dataclass(frozen=True)
class KlineActionResult:
    kind: ActionKind
    module: str
    address: int
    ok: bool
    #: The positive reply's service byte and parameters (``54`` for a clear).
    reply_hex: str | None
    detail: str
    started_at: float
    finished_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "module": self.module,
            "address": f"0x{self.address:02X}",
            "ok": self.ok,
            "reply_hex": self.reply_hex,
            "detail": self.detail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.finished_at - self.started_at, 3),
        }


@dataclass
class KlineAction:
    kind: ActionKind
    module: str
    address: int
    request: bytes
    future: Future[KlineActionResult] = field(default_factory=Future)

    def finish(self, *, ok: bool, detail: str, started: float, reply: bytes | None = None) -> None:
        self.future.set_result(
            KlineActionResult(
                kind=self.kind,
                module=self.module,
                address=self.address,
                ok=ok,
                reply_hex=reply.hex(" ") if reply is not None else None,
                detail=detail,
                started_at=started,
                finished_at=time.time(),
            )
        )


class KlineActions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queued: KlineAction | None = None
        self._running: KlineAction | None = None
        self._collectors = 0

    # -- the collector's side

    def attach(self) -> None:
        with self._lock:
            self._collectors += 1

    def detach(self) -> None:
        with self._lock:
            self._collectors = max(0, self._collectors - 1)
            orphan, self._queued = self._queued, None
        if orphan is not None:
            orphan.finish(ok=False, detail="the K-line collector stopped", started=time.time())

    def pending(self) -> bool:
        with self._lock:
            return self._queued is not None

    def take(self) -> KlineAction | None:
        with self._lock:
            action, self._queued = self._queued, None
            self._running = action
            return action

    def done(self, action: KlineAction) -> None:
        with self._lock:
            if self._running is action:
                self._running = None

    def reset(self) -> None:
        """Back to start-up: no collector, nothing queued (tests only)."""
        with self._lock:
            self._queued = None
            self._running = None
            self._collectors = 0

    # -- the control route's side

    @property
    def available(self) -> bool:
        with self._lock:
            return self._collectors > 0

    def submit(self, action: KlineAction) -> Future[KlineActionResult]:
        with self._lock:
            if self._collectors == 0:
                raise KlineActionUnavailableError(
                    "the K-line collector is not running (hardware.kline.enabled)"
                )
            busy = self._queued or self._running
            if busy is not None:
                raise KlineActionBusyError(
                    f"{busy.kind} on {busy.module} is already queued or running"
                )
            self._queued = action
        return action.future

    def cancel(self, action: KlineAction) -> bool:
        """Withdraw ``action`` if the collector has not taken it yet."""
        with self._lock:
            if self._queued is action:
                self._queued = None
                return True
            return False


#: The daemon's one registry, shared by the collector and the control routes.
KLINE_ACTIONS = KlineActions()


__all__ = [
    "KLINE_ACTIONS",
    "ActionKind",
    "KlineAction",
    "KlineActionBusyError",
    "KlineActionResult",
    "KlineActionUnavailableError",
    "KlineActions",
]
