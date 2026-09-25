"""The live collectors' door onto the telemetry queue (D02.9).

A runtime replay re-emits a stored session onto the same queue the live
collectors feed, and switches the writer to ``_replay-<id>/``. Nothing stopped
the live producers meanwhile: on the 2026-09-11 evidence replay the Pi's own
GPS receiver wrote ~22,800 rows (``gps`` at 1.648x its source) into the
replay's output, so any comparison of its GPS or derived-speed channels
against the source compared two different drives. ``can``/``obd`` escaped only
because the truck was parked.

:class:`LiveIngress` is what every live collector is handed instead of the
queue itself. Open, it forwards each record to the queue unchanged. Closed —
for the whole of a replay — it withholds live records and counts them per
source, so the replay's output holds the replay and nothing else, and an
operator can see how much live telemetry the replay displaced rather than
finding a silent gap. The replay itself puts straight onto the queue.

Collectors only ever call ``put_nowait`` (``queueing.put_nowait_or_drop``,
``ThreadHandoff``) and read ``qsize``/``maxsize``/``full``/``empty``; those
forward. Reading from an ingress is a wiring bug, so ``get`` and friends
refuse.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import Counter
from typing import Any

from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)


class LiveIngress(asyncio.Queue[TelemetryRecord]):
    """A write-only, closable view of the telemetry queue for live collectors."""

    def __init__(self, target: asyncio.Queue[TelemetryRecord]) -> None:
        super().__init__(maxsize=target.maxsize)
        self._target = target
        self._lock = threading.Lock()
        self._closed_reason: str | None = None
        self._withheld: Counter[str] = Counter()

    # --- the gate ---------------------------------------------------------

    def close(self, reason: str) -> None:
        """Withhold live records from now on (and zero the withheld count)."""
        with self._lock:
            self._closed_reason = reason
            self._withheld = Counter()
        log.info("live collectors withheld from the pipeline", extra={"reason": reason})

    def open(self) -> dict[str, int]:
        """Let live records through again; return what was withheld, per source."""
        with self._lock:
            reason = self._closed_reason
            self._closed_reason = None
            withheld = dict(self._withheld)
        if reason is not None:
            log.info(
                "live collectors restored to the pipeline",
                extra={"reason": reason, "withheld": withheld},
            )
        return withheld

    @property
    def closed_reason(self) -> str | None:
        with self._lock:
            return self._closed_reason

    def withheld(self) -> dict[str, int]:
        """Live records withheld since the last :meth:`close`, per source."""
        with self._lock:
            return dict(self._withheld)

    # --- what collectors call ---------------------------------------------

    def put_nowait(self, item: TelemetryRecord) -> None:
        with self._lock:
            if self._closed_reason is not None:
                self._withheld[str(item.source)] += 1
                return
        self._target.put_nowait(item)

    async def put(self, item: TelemetryRecord) -> None:
        with self._lock:
            if self._closed_reason is not None:
                self._withheld[str(item.source)] += 1
                return
        await self._target.put(item)

    def qsize(self) -> int:
        return self._target.qsize()

    @property
    def maxsize(self) -> int:
        return self._target.maxsize

    def empty(self) -> bool:
        return self._target.empty()

    def full(self) -> bool:
        return self._target.full()

    # --- never read from an ingress ----------------------------------------

    def _refuse(self, *_args: Any) -> Any:
        raise TypeError("LiveIngress is write-only; read the telemetry queue itself")

    def get_nowait(self) -> TelemetryRecord:
        return self._refuse()  # type: ignore[no-any-return]

    async def get(self) -> TelemetryRecord:
        return self._refuse()  # type: ignore[no-any-return]

    def task_done(self) -> None:
        self._refuse()

    async def join(self) -> None:
        self._refuse()


__all__ = ["LiveIngress"]
