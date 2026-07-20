"""Tests for the DTC poll cache/diff/sidecar state (Tasks 1.4/1.6/1.7)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger
from src.analysis.vehicle_ledger import VehicleLedgerStore
from src.control.dtc_state import SIDECAR_FILENAME, DtcState
from src.telemetry import TelemetryRecord


def _reading(channel: str, value: object) -> TelemetryRecord:
    return TelemetryRecord.obd(time.time(), channel, value, "")


def _emitter(tmp_path: Path) -> AnomalyEventEmitter:
    return AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)


# ---------------------------------------------------------------------------
# 1.4 — cached report updates + is retrievable
# ---------------------------------------------------------------------------


def test_snapshot_before_any_poll_is_empty_and_stale() -> None:
    state = DtcState()
    snapshot = state.snapshot()
    assert snapshot["captured_at"] is None
    assert snapshot["stored"] == []
    assert snapshot["stale"] is True


def test_observe_updates_the_cached_report_incrementally() -> None:
    """Each channel lands independently (they arrive on separate polls);
    the cached report reflects whatever's been seen so far."""
    state = DtcState()
    state.observe(
        _reading("DTC_STORED", "P0420,C0510"),
        session_id="s1",
        session_dir=None,
        emitter=None,
    )
    snapshot = state.snapshot()
    assert snapshot["captured_at"] is not None
    assert [c["code"] for c in snapshot["stored"]] == ["P0420", "C0510"]
    assert snapshot["dtc_count"] == 2
    assert snapshot["pending"] == []

    state.observe(
        _reading("MIL_STATUS", True),
        session_id="s1",
        session_dir=None,
        emitter=None,
    )
    assert state.snapshot()["mil_on"] is True


def test_non_obd_and_unrelated_channels_are_ignored() -> None:
    state = DtcState()
    state.observe(
        TelemetryRecord.can(time.time(), 0x123, b"\x01"),
        session_id="s1",
        session_dir=None,
        emitter=None,
    )
    state.observe(_reading("RPM", 700.0), session_id="s1", session_dir=None, emitter=None)
    assert state.snapshot()["captured_at"] is None


def test_stale_flag_flips_after_the_configured_window() -> None:
    from datetime import UTC, datetime, timedelta

    state = DtcState(stale_after_seconds=10.0)
    state.observe(
        _reading("DTC_STORED", ""),
        session_id="s1",
        session_dir=None,
        emitter=None,
    )
    fresh = state.snapshot(now=datetime.now(UTC))
    assert fresh["stale"] is False
    later = state.snapshot(now=datetime.now(UTC) + timedelta(seconds=30))
    assert later["stale"] is True


# ---------------------------------------------------------------------------
# 1.6 — new-code / escalation events
# ---------------------------------------------------------------------------


def test_new_code_emits_an_anomaly_event(tmp_path: Path) -> None:
    state = DtcState()
    emitter = _emitter(tmp_path)
    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id="s1",
        session_dir=None,
        emitter=emitter,
    )
    assert emitter.counters.emitted == 1


def test_unchanged_report_emits_nothing_on_the_next_poll(tmp_path: Path) -> None:
    state = DtcState()
    emitter = _emitter(tmp_path)
    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id="s1",
        session_dir=None,
        emitter=emitter,
    )
    assert emitter.counters.emitted == 1
    # Same code reported again (next poll cycle) — nothing new to report.
    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id="s1",
        session_dir=None,
        emitter=emitter,
    )
    assert emitter.counters.emitted == 1


def test_pending_to_stored_transition_emits_an_escalation(tmp_path: Path) -> None:
    state = DtcState()
    emitter = _emitter(tmp_path)
    state.observe(
        _reading("DTC_PENDING", "P0420"),
        session_id="s1",
        session_dir=None,
        emitter=emitter,
    )
    assert emitter.counters.emitted == 1

    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id="s1",
        session_dir=None,
        emitter=emitter,
    )
    assert emitter.counters.emitted == 2


def test_pending_to_stored_transition_kind_is_escalated() -> None:
    logged: list[object] = []

    class _CapturingLogger:
        def log(self, event: object) -> None:
            logged.append(event)

        def close(self) -> None:
            pass

    emitter = AnomalyEventEmitter(_CapturingLogger(), min_seconds_between_events=0)  # type: ignore[arg-type]
    state = DtcState()
    state.observe(
        _reading("DTC_PENDING", "P0420"),
        session_id="s1",
        session_dir=None,
        emitter=emitter,
    )
    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id="s1",
        session_dir=None,
        emitter=emitter,
    )
    kinds = [event.detail["kind"] for event in logged]  # type: ignore[attr-defined]
    assert kinds == ["new", "escalated"]


def test_new_code_does_not_go_through_a_persistence_gate() -> None:
    """A single observation is enough — no consecutive-tick warm-up like
    IF/LSTM need from their PersistenceGate."""
    state = DtcState()
    event = None

    class _OneShotLogger:
        def log(self, e: object) -> None:
            nonlocal event
            event = e

        def close(self) -> None:
            pass

    emitter = AnomalyEventEmitter(_OneShotLogger(), min_seconds_between_events=0)  # type: ignore[arg-type]
    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id="s1",
        session_dir=None,
        emitter=emitter,
    )
    assert event is not None
    assert event.source == "dtc"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 1.7 — session sidecar (atomic write) + per-session baseline reset
# ---------------------------------------------------------------------------


def test_sidecar_written_atomically_on_change(tmp_path: Path) -> None:
    state = DtcState()
    session_dir = tmp_path / "2026-05-13T120000"
    session_dir.mkdir()

    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id=session_dir.name,
        session_dir=session_dir,
        emitter=None,
    )

    sidecar = session_dir / SIDECAR_FILENAME
    assert sidecar.exists()
    assert not (session_dir / (SIDECAR_FILENAME + ".tmp")).exists()
    payload = json.loads(sidecar.read_text())
    assert [c["code"] for c in payload["stored"]] == ["P0420"]


def test_sidecar_not_rewritten_when_nothing_changed(tmp_path: Path) -> None:
    state = DtcState()
    session_dir = tmp_path / "2026-05-13T120000"
    session_dir.mkdir()

    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id=session_dir.name,
        session_dir=session_dir,
        emitter=None,
    )
    sidecar = session_dir / SIDECAR_FILENAME
    first_mtime = sidecar.stat().st_mtime_ns

    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id=session_dir.name,
        session_dir=session_dir,
        emitter=None,
    )
    assert sidecar.stat().st_mtime_ns == first_mtime


def test_new_session_does_not_inherit_the_previous_sessions_seen_codes(tmp_path: Path) -> None:
    """A fresh drive re-flags a still-present code as new — the baseline is
    per-session, not "ever seen"."""
    state = DtcState()
    emitter = _emitter(tmp_path)

    session_a = tmp_path / "2026-05-13T120000"
    session_a.mkdir()
    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id=session_a.name,
        session_dir=session_a,
        emitter=emitter,
    )
    assert emitter.counters.emitted == 1

    # A second poll in the SAME session: not new, no re-emit.
    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id=session_a.name,
        session_dir=session_a,
        emitter=emitter,
    )
    assert emitter.counters.emitted == 1

    # A new session starts (next drive); the ECU still reports the same
    # stored code (it was never cleared) — this must count as new again.
    session_b = tmp_path / "2026-05-13T130000"
    session_b.mkdir()
    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id=session_b.name,
        session_dir=session_b,
        emitter=emitter,
    )
    assert emitter.counters.emitted == 2
    # And session B gets its own sidecar, independent of session A's.
    assert (session_b / SIDECAR_FILENAME).exists()
    payload_a = json.loads((session_a / SIDECAR_FILENAME).read_text())
    payload_b = json.loads((session_b / SIDECAR_FILENAME).read_text())
    assert [c["code"] for c in payload_a["stored"]] == ["P0420"]
    assert [c["code"] for c in payload_b["stored"]] == ["P0420"]


# ---------------------------------------------------------------------------
# 3.4 — new stored DTC auto-appends a vehicle-ledger entry
# ---------------------------------------------------------------------------


def test_new_stored_dtc_appends_a_vehicle_ledger_event(tmp_path: Path) -> None:
    state = DtcState()
    ledger = VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl")
    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id="s1",
        session_dir=None,
        vehicle_ledger=ledger,
    )
    records = ledger.records()
    assert len(records) == 1
    assert records[0].kind == "dtc"
    assert "P0420" in records[0].title


def test_unchanged_stored_dtc_does_not_reappend(tmp_path: Path) -> None:
    state = DtcState()
    ledger = VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl")
    state.observe(
        _reading("DTC_STORED", "P0420"), session_id="s1", session_dir=None, vehicle_ledger=ledger
    )
    state.observe(
        _reading("DTC_STORED", "P0420"), session_id="s1", session_dir=None, vehicle_ledger=ledger
    )
    assert len(ledger.records()) == 1


def test_pending_only_dtc_does_not_append_to_ledger(tmp_path: Path) -> None:
    """Only a *stored* sighting writes the ledger entry (Task 3.4) — a
    pending code that never confirms shouldn't leave a permanent trail."""
    state = DtcState()
    ledger = VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl")
    state.observe(
        _reading("DTC_PENDING", "P0420"), session_id="s1", session_dir=None, vehicle_ledger=ledger
    )
    assert ledger.records() == []


def test_pending_to_stored_escalation_appends_ledger_entry(tmp_path: Path) -> None:
    state = DtcState()
    ledger = VehicleLedgerStore(tmp_path / "vehicle_ledger.jsonl")
    state.observe(
        _reading("DTC_PENDING", "P0420"), session_id="s1", session_dir=None, vehicle_ledger=ledger
    )
    assert ledger.records() == []
    state.observe(
        _reading("DTC_STORED", "P0420"), session_id="s1", session_dir=None, vehicle_ledger=ledger
    )
    records = ledger.records()
    assert len(records) == 1
    assert records[0].kind == "dtc"


def test_no_vehicle_ledger_configured_is_a_no_op(tmp_path: Path) -> None:
    """``vehicle_ledger=None`` (the default) doesn't raise — outcome
    tracking is best-effort, not load-bearing for the DTC diff/emit path."""
    state = DtcState()
    state.observe(_reading("DTC_STORED", "P0420"), session_id="s1", session_dir=None)
    assert state.snapshot()["stored"]


# ---------------------------------------------------------------------------
# 3.3 — Mode 02 freeze-frame trigger + caching
# ---------------------------------------------------------------------------


def test_new_stored_dtc_requests_a_freeze_frame_read() -> None:
    from src.collection.obd_scheduler import FreezeFrameTrigger

    trigger = FreezeFrameTrigger()
    state = DtcState(freeze_frame_trigger=trigger)
    assert trigger.take() is False  # nothing pending yet
    state.observe(_reading("DTC_STORED", "P0420"), session_id="s1", session_dir=None)
    assert trigger.take() is True
    # Consumed — a second check without a new stored code finds nothing.
    assert trigger.take() is False


def test_pending_only_code_does_not_request_a_freeze_frame() -> None:
    from src.collection.obd_scheduler import FreezeFrameTrigger

    trigger = FreezeFrameTrigger()
    state = DtcState(freeze_frame_trigger=trigger)
    state.observe(_reading("DTC_PENDING", "P0420"), session_id="s1", session_dir=None)
    assert trigger.take() is False


def test_freeze_frame_reading_lands_in_the_cached_report_and_sidecar(tmp_path: Path) -> None:
    state = DtcState()
    session_dir = tmp_path / "2026-05-13T120000"
    session_dir.mkdir()
    state.observe(
        _reading("DTC_STORED", "P0420"),
        session_id=session_dir.name,
        session_dir=session_dir,
    )
    state.observe(
        _reading("FREEZE_FRAME_RPM", 2500.0),
        session_id=session_dir.name,
        session_dir=session_dir,
    )
    snapshot = state.snapshot()
    assert snapshot["freeze_frame"] == {"RPM": 2500.0}

    sidecar = json.loads((session_dir / SIDECAR_FILENAME).read_text())
    assert sidecar["freeze_frame"] == {"RPM": 2500.0}


def test_no_freeze_frame_trigger_configured_is_a_no_op() -> None:
    """``freeze_frame_trigger=None`` (the default) doesn't raise — Task 3.3's
    Mode 02 read is opt-in, gated on the collector actually wiring one."""
    state = DtcState()
    state.observe(_reading("DTC_STORED", "P0420"), session_id="s1", session_dir=None)
    assert state.snapshot()["stored"]
