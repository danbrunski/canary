"""One-shot K-line diagnostic functions, run by the collector that owns the port.

The serial port belongs to :class:`~src.collection.kline_collector.KlineCollector`
(sniffing, or reading a module for a capture profile), and K-line is one wire,
so a control route cannot open a second handle the way the CAN ad-hoc request
does. It hands the function to :data:`KLINE_ACTIONS` instead; the collector
stops sniffing (or ends its read session) at its next check, runs the
function in a session of its own, and resumes.

One function at a time: a second request while one is queued or running is
refused, not queued behind it.

Two functions, each only for a module whose vehicle profile records how:
reading a module's stored codes (``KlineModule.read_codes``) and clearing them
(``KlineModule.clear_codes``).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Literal

from src.collection.kline.profile import CodeStatus, ReadCodes

ActionKind = Literal["read_codes", "clear_codes"]


class KlineActionBusyError(RuntimeError):
    """Another function is queued or running."""


class KlineActionUnavailableError(RuntimeError):
    """No K-line collector is running to carry the function out."""


@dataclass(frozen=True)
class StoredCode:
    """One code a module's code read returned."""

    code: str  # "B1023"
    word: int  # 0x9023
    status: CodeStatus
    status_confidence: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "word": f"0x{self.word:04X}",
            "status": self.status,
            "status_confidence": self.status_confidence,
        }


@dataclass(frozen=True)
class ActionExchange:
    """One request and what answered it, as sent and received (payloads,
    service byte first; responsePending frames are the transport's)."""

    request: bytes
    reply: bytes | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "request_hex": self.request.hex(" "),
            "reply_hex": self.reply.hex(" ") if self.reply is not None else None,
        }


@dataclass(frozen=True)
class KlineActionResult:
    kind: ActionKind
    module: str
    address: int
    ok: bool
    #: The last positive reply's service byte and parameters (``54`` for a clear).
    reply_hex: str | None
    detail: str
    started_at: float
    finished_at: float
    #: A code read's codes: empty when the module read and holds none, ``None``
    #: when the read failed (and for a clear, which reads nothing).
    codes: tuple[StoredCode, ...] | None = None
    #: Every request sent after StartCommunication and its reply, in order.
    raw: tuple[ActionExchange, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "module": self.module,
            "address": f"0x{self.address:02X}",
            "ok": self.ok,
            "reply_hex": self.reply_hex,
            "detail": self.detail,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(self.finished_at - self.started_at, 3),
            "raw": [exchange.to_dict() for exchange in self.raw],
        }
        if self.kind == "read_codes":
            out["codes"] = None if self.codes is None else [code.to_dict() for code in self.codes]
        return out


@dataclass
class KlineAction:
    kind: ActionKind
    module: str
    address: int
    #: A clear's one request (``KlineModule.clear_codes``).
    request: bytes = b""
    #: A code read's recipe (``KlineModule.read_codes``).
    read_codes: ReadCodes | None = None
    future: Future[KlineActionResult] = field(default_factory=Future)

    def __post_init__(self) -> None:
        if self.kind == "clear_codes" and not self.request:
            raise ValueError("a clear_codes action needs its request")
        if self.kind == "read_codes" and self.read_codes is None:
            raise ValueError("a read_codes action needs the module's read_codes recipe")

    def requests(self) -> tuple[bytes, ...]:
        """What the collector sends after StartCommunication, in order."""
        if self.read_codes is not None:
            return tuple(read.request_bytes() for read in self.read_codes.requests)
        return (self.request,)

    def finish(
        self,
        *,
        ok: bool,
        detail: str,
        started: float,
        reply: bytes | None = None,
        codes: tuple[StoredCode, ...] | None = None,
        raw: tuple[ActionExchange, ...] = (),
    ) -> None:
        if self.future.done():
            # The caller stopped waiting and cancelled it. The function ran
            # (or was refused) all the same; the collector's log says which.
            return
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
                codes=codes,
                raw=raw,
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
    "ActionExchange",
    "KlineAction",
    "KlineActionBusyError",
    "KlineActionResult",
    "KlineActionUnavailableError",
    "KlineActions",
    "StoredCode",
]
