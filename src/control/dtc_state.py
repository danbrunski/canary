"""Live DTC poll state — cache + per-session diff + sidecar (Tasks 1.4/1.6/1.7).

The daemon's periodic Mode 03/07/0A poll (Task 1.3) lands here one decoded
OBD reading at a time (``DTC_STORED``/``DTC_PENDING``/``DTC_PERMANENT``/
``MIL_STATUS``), via a ``dtc`` telemetry sink mirroring
:class:`~src.analysis.gps_tracker.GpsTracker`'s single-writer in-memory-state
pattern. Each update:

* rebuilds the cached :class:`~src.decode.dtc_catalog.DtcReport` that
  ``GET /obd/dtc`` serves (Task 1.5);
* diffs the current stored/pending/permanent lists against a per-session
  "already seen" baseline to find new/escalated codes and emits them
  through the :class:`~src.alerts.emitter.AnomalyEventEmitter` (Task 1.6);
* writes the ``dtc.json`` session sidecar when the report actually changed
  (Task 1.7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from src.decode.dtc_catalog import DtcReport, build_dtc_report, describe_dtc
from src.telemetry import TelemetryRecord

if TYPE_CHECKING:
    from src.alerts.emitter import AnomalyEventEmitter
    from src.analysis.vehicle_ledger import VehicleLedgerStore

SIDECAR_FILENAME = "dtc.json"

#: ``GET /obd/dtc`` treats the cached report stale after this many seconds
#: with no poll landing. The daemon's default DTC-poll cadence is 300 s
#: (Task 1.3); 3x that survives one missed/slow cycle without falsely
#: flagging every quiet interval *between* polls as stale.
DEFAULT_STALE_AFTER_SECONDS = 900.0

_STORED_CHANNEL = "DTC_STORED"
_PENDING_CHANNEL = "DTC_PENDING"
_PERMANENT_CHANNEL = "DTC_PERMANENT"
_MIL_CHANNEL = "MIL_STATUS"


def _split_codes(value: Any) -> list[str]:
    if not value:
        return []
    return [code for code in str(value).split(",") if code]


def _classify_kind(status: str, prev: set[str]) -> Literal["new", "escalated"]:
    """pending -> stored is the one explicit escalation case (Task 1.6);
    every other first-sighting of a code is just "new"."""
    return "escalated" if status == "stored" and "pending" in prev else "new"


@dataclass
class DtcState:
    """Single-writer cache of the latest DTC report + per-session diff/sidecar.

    Construct one per daemon; the DTC telemetry sink
    (``src.daemon.drain.run_dtc_sink``) is the only writer. ``snapshot()``
    is what ``GET /obd/dtc`` serves.
    """

    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS
    _stored: list[str] = field(default_factory=list, init=False, repr=False)
    _pending: list[str] = field(default_factory=list, init=False, repr=False)
    _permanent: list[str] = field(default_factory=list, init=False, repr=False)
    _mil_on: bool | None = field(default=None, init=False, repr=False)
    _report: DtcReport | None = field(default=None, init=False, repr=False)
    _session_id: str | None = field(default=None, init=False, repr=False)
    #: code -> statuses ("stored"/"pending"/"permanent") seen this session.
    _seen: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False)
    _last_persisted_signature: tuple[Any, ...] | None = field(default=None, init=False, repr=False)

    def observe(
        self,
        record: TelemetryRecord,
        *,
        session_id: str | None,
        session_dir: Path | None,
        emitter: AnomalyEventEmitter | None = None,
        vehicle_ledger: VehicleLedgerStore | None = None,
    ) -> None:
        """Feed one decoded OBD reading; a no-op for anything but the DTC channels."""
        if record.source != "obd":
            return
        if record.channel == _STORED_CHANNEL:
            self._stored = _split_codes(record.value)
        elif record.channel == _PENDING_CHANNEL:
            self._pending = _split_codes(record.value)
        elif record.channel == _PERMANENT_CHANNEL:
            self._permanent = _split_codes(record.value)
        elif record.channel == _MIL_CHANNEL:
            self._mil_on = bool(record.value) if record.value is not None else None
        else:
            return
        self._rebuild(
            session_id=session_id,
            session_dir=session_dir,
            emitter=emitter,
            vehicle_ledger=vehicle_ledger,
        )

    def _rebuild(
        self,
        *,
        session_id: str | None,
        session_dir: Path | None,
        emitter: AnomalyEventEmitter | None,
        vehicle_ledger: VehicleLedgerStore | None = None,
    ) -> None:
        if session_id != self._session_id:
            # Task 1.6/1.7: a new drive starts with a clean "already seen"
            # baseline. The stored/pending/permanent lists themselves are
            # NOT reset here — they reflect the ECU's actual current state,
            # not the session — so a code the ECU still carries over from a
            # prior drive is deliberately re-flagged as new for this
            # session's diff/sidecar/notification stream.
            self._seen = {}
            self._last_persisted_signature = None
            self._session_id = session_id

        report = build_dtc_report(
            stored=self._stored,
            pending=self._pending,
            permanent=self._permanent,
            mil_on=self._mil_on,
        )
        self._report = report

        self._diff_and_emit(
            timestamp=report.captured_at,
            session_id=session_id,
            emitter=emitter,
            vehicle_ledger=vehicle_ledger,
        )

        signature = (
            tuple(sorted(self._stored)),
            tuple(sorted(self._pending)),
            tuple(sorted(self._permanent)),
            self._mil_on,
        )
        if session_dir is not None and signature != self._last_persisted_signature:
            _write_sidecar(session_dir, report)
            self._last_persisted_signature = signature

    def _diff_and_emit(
        self,
        *,
        timestamp: str,
        session_id: str | None,
        emitter: AnomalyEventEmitter | None,
        vehicle_ledger: VehicleLedgerStore | None = None,
    ) -> None:
        """Emit new/escalated codes (Task 1.6): straight to the emitter, no
        persistence gating — a DTC is already ECM-debounced by the time it
        shows up in a poll response.

        A new *stored* code also auto-appends a ``kind="dtc"`` vehicle-ledger
        entry (Task 3.4) — outcome tracking starts itself, no operator
        action needed. Pending/permanent-only sightings don't: a pending
        code that never confirms shouldn't leave a permanent ledger trail.
        """
        for status, codes in (
            ("pending", self._pending),
            ("stored", self._stored),
            ("permanent", self._permanent),
        ):
            for code in codes:
                prev = self._seen.setdefault(code, set())
                if status not in prev:
                    kind = _classify_kind(status, prev)
                    described = describe_dtc(code)
                    if emitter is not None:
                        emitter.emit_dtc(
                            code=code,
                            description=described.description,
                            system=described.system,
                            status=status,
                            kind=kind,
                            timestamp=timestamp,
                            mil_on=self._mil_on,
                            session=session_id,
                        )
                    if status == "stored" and vehicle_ledger is not None:
                        try:
                            occurred_at = datetime.fromisoformat(timestamp).date()
                        except ValueError:
                            occurred_at = datetime.now(UTC).date()
                        vehicle_ledger.record(
                            occurred_at=occurred_at,
                            kind="dtc",
                            title=f"DTC {code} stored: {described.description}",
                            details=(
                                f"system={described.system} mil_on={self._mil_on} "
                                f"session={session_id}"
                            ),
                            affected_channels=[_STORED_CHANNEL],
                        )
                prev.add(status)

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        """JSON-safe payload for ``GET /obd/dtc`` (Task 1.5)."""
        if self._report is None:
            return {
                "captured_at": None,
                "stored": [],
                "pending": [],
                "permanent": [],
                "mil_on": None,
                "dtc_count": 0,
                "stale": True,
            }
        payload = self._report.to_dict()
        payload["stale"] = self._is_stale(now=now)
        return payload

    def _is_stale(self, *, now: datetime | None) -> bool:
        if self._report is None:
            return True
        current = now or datetime.now(UTC)
        captured = datetime.fromisoformat(self._report.captured_at)
        return (current - captured).total_seconds() > self.stale_after_seconds


def _write_sidecar(session_dir: Path, report: DtcReport) -> None:
    """Atomic tmp+rename write, matching ``control.json``
    (``src.control.sessions.write_session_metadata``, Task 1.7)."""
    path = session_dir / SIDECAR_FILENAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str) + "\n")
    tmp.replace(path)


__all__ = [
    "DEFAULT_STALE_AFTER_SECONDS",
    "SIDECAR_FILENAME",
    "DtcState",
]
