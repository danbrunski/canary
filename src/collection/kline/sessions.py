"""Who holds each K-line module's diagnostic session — ours, or another tester's.

The ECM's session is also CAN's (``tx_gate.claim_ecm_session``: while K-line
holds it, CAN diagnostics pause). Other modules share nothing with CAN, so
reading the ABS must not pause Mode 01 — but they share the *wire*: K-line is
one line, and a second tester talking to any module while CANary talks to
another garbles both. So this registry answers two questions:

- :meth:`ModuleSessions.owner` — who holds module ``address`` now;
- :meth:`ModuleSessions.foreign_active` — is another tester on the line at
  all (then CANary starts nothing, on any module).

The sniffer claims ``foreign`` for the module each of another tester's frames
belongs to (``None`` — unknown — when it attached mid-session), renewed per
frame with a TTL. CANary's reader claims ``kline`` for its target for exactly
the length of its session. Claims expire on read; there is nothing to poll.
"""

from __future__ import annotations

import threading
import time
from typing import Literal

ModuleHolder = Literal["kline", "foreign"]
ModuleOwner = Literal["none", "kline", "foreign"]


class ModuleSessions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: (address, holder) -> ``time.monotonic()`` expiry, ``None`` = held.
        self._claims: dict[tuple[int | None, ModuleHolder], float | None] = {}

    def _expire_locked(self, now: float) -> None:
        for key, deadline in list(self._claims.items()):
            if deadline is not None and now >= deadline:
                del self._claims[key]

    def claim(
        self,
        address: int | None,
        holder: ModuleHolder,
        *,
        ttl_seconds: float | None = None,
        now: float | None = None,
    ) -> None:
        """Claim (or renew) ``address`` for ``holder``. ``None`` is the module
        of a frame seen before any StartCommunication."""
        at = time.monotonic() if now is None else now
        with self._lock:
            self._claims[(address, holder)] = at + ttl_seconds if ttl_seconds is not None else None

    def release(self, address: int | None, holder: ModuleHolder) -> None:
        with self._lock:
            self._claims.pop((address, holder), None)

    def owner(self, address: int | None, *, now: float | None = None) -> ModuleOwner:
        """A foreign claim outranks ours, as on the ECM's CAN session."""
        at = time.monotonic() if now is None else now
        with self._lock:
            self._expire_locked(at)
            if (address, "foreign") in self._claims:
                return "foreign"
            if (address, "kline") in self._claims:
                return "kline"
            return "none"

    def foreign_active(self, *, now: float | None = None) -> bool:
        """Another tester has been heard on the line, on any module."""
        at = time.monotonic() if now is None else now
        with self._lock:
            self._expire_locked(at)
            return any(holder == "foreign" for _, holder in self._claims)

    def snapshot(self, *, now: float | None = None) -> dict[str, str]:
        at = time.monotonic() if now is None else now
        with self._lock:
            self._expire_locked(at)
            return {
                ("unknown" if address is None else f"{address:02X}"): holder
                for address, holder in sorted(
                    self._claims, key=lambda key: (key[0] is None, key[0] or 0, key[1])
                )
            }

    def reset(self) -> None:
        """Drop every claim (tests)."""
        with self._lock:
            self._claims.clear()


#: The process's registry: one K-line transceiver, one set of sessions.
MODULE_SESSIONS = ModuleSessions()


__all__ = ["MODULE_SESSIONS", "ModuleHolder", "ModuleOwner", "ModuleSessions"]
