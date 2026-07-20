"""Live Mode 06 on-board monitor poll state — cache + misfire-increase alert
(Task 3.1).

The daemon's periodic Mode 06 poll (Task 3.1, generalizes the Mode 03/07/0A
DTC poll's ``kind="dtc"`` scheduling mechanism, Task 1.3 — see
``src.collection.obd_scheduler``) lands here one decoded OBD reading at a
time (channel ``MONITOR_RESULTS``, a hex-encoded raw Mode 06 payload — see
``src.collection.can_obd_collector._response_to_reading``), via a
``monitor`` telemetry sink mirroring
:class:`~src.control.dtc_state.DtcState`'s single-writer in-memory-state
pattern. Each update:

* decodes the sweep into :class:`~src.collection.obd_registry.MonitorTestResult`
  rows plus per-cylinder misfire counts
  (:func:`~src.collection.obd_registry.misfire_counts_from_results`);
* emits a ``source="misfire"`` anomaly event for any cylinder whose count
  *increased* since the last poll this session — the ECM already debounces
  this signal, so no persistence gate, mirroring ``DtcState``'s
  new/escalated-DTC emission;
* caches the latest snapshot for ``GET /obd/monitors`` / the ``get_monitors``
  MCP tool.

No sidecar write (unlike ``DtcState``'s ``dtc.json``) — Task 3.1 only asks
for the live cache + endpoint, not per-session persistence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.collection.obd_registry import (
    MonitorTestResult,
    misfire_counts_from_results,
    parse_mode06_response,
)
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.alerts.emitter import AnomalyEventEmitter

#: ``GET /obd/monitors`` treats the cached sweep stale after this many
#: seconds with no poll landing. Mirrors ``DtcState``'s 3x-cadence margin
#: (default Mode 06 poll cadence is 300 s, Task 3.1).
DEFAULT_STALE_AFTER_SECONDS = 900.0

_MONITOR_RESULTS_CHANNEL = "MONITOR_RESULTS"


def _result_to_dict(result: MonitorTestResult) -> dict[str, Any]:
    return {
        "monitor_id": result.monitor_id,
        "test_id": result.test_id,
        "unit_and_scaling_id": result.unit_and_scaling_id,
        "test_value": result.test_value,
        "min_limit": result.min_limit,
        "max_limit": result.max_limit,
        "passed": result.passed,
    }


@dataclass
class MonitorState:
    """Single-writer cache of the latest Mode 06 sweep + per-session misfire diff.

    Construct one per daemon; the monitor telemetry sink
    (``src.daemon.drain.run_monitor_sink``) is the only writer. ``snapshot()``
    is what ``GET /obd/monitors`` serves.
    """

    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS
    _results: list[MonitorTestResult] = field(default_factory=list, init=False, repr=False)
    _misfire_counts: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _captured_at: str | None = field(default=None, init=False, repr=False)
    _session_id: str | None = field(default=None, init=False, repr=False)
    #: Last-seen misfire count per cylinder, reset per session (Task 1.6/1.7's
    #: "new drive starts with a clean baseline" pattern) — a count the ECU
    #: still carries over from a prior drive is deliberately re-flagged as an
    #: increase for this session.
    _last_counts: dict[int, int] = field(default_factory=dict, init=False, repr=False)

    def observe(
        self,
        record: TelemetryRecord,
        *,
        session_id: str | None,
        emitter: AnomalyEventEmitter | None = None,
    ) -> None:
        """Feed one decoded OBD reading; a no-op for anything but MONITOR_RESULTS."""
        if record.source != "obd" or record.channel != _MONITOR_RESULTS_CHANNEL:
            return
        if session_id != self._session_id:
            self._last_counts = {}
            self._session_id = session_id
        try:
            payload = bytes.fromhex(str(record.value)) if record.value else b""
        except ValueError:
            log.warning("unparseable MONITOR_RESULTS payload; skipping")
            return
        self._results = parse_mode06_response(payload)
        self._misfire_counts = misfire_counts_from_results(self._results)
        self._captured_at = datetime.now(UTC).isoformat()
        self._diff_and_emit(session_id=session_id, emitter=emitter)

    def _diff_and_emit(
        self,
        *,
        session_id: str | None,
        emitter: AnomalyEventEmitter | None,
    ) -> None:
        """Emit a misfire event per cylinder whose count increased (Task 3.1):
        straight to the emitter, no persistence gating — the ECM has already
        debounced this by the time it shows up in a poll response."""
        for cylinder, count in self._misfire_counts.items():
            previous = self._last_counts.get(cylinder, 0)
            if count > previous and emitter is not None:
                emitter.emit_misfire(
                    cylinder=cylinder,
                    count=count,
                    previous_count=previous,
                    timestamp=self._captured_at or datetime.now(UTC).isoformat(),
                    session=session_id,
                )
            self._last_counts[cylinder] = count

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        """JSON-safe payload for ``GET /obd/monitors``."""
        return {
            "captured_at": self._captured_at,
            "misfire_counts": dict(sorted(self._misfire_counts.items())),
            "results": [_result_to_dict(result) for result in self._results],
            "stale": self._is_stale(now=now),
        }

    def _is_stale(self, *, now: datetime | None) -> bool:
        if self._captured_at is None:
            return True
        current = now or datetime.now(UTC)
        captured = datetime.fromisoformat(self._captured_at)
        return (current - captured).total_seconds() > self.stale_after_seconds


__all__ = ["DEFAULT_STALE_AFTER_SECONDS", "MonitorState"]
