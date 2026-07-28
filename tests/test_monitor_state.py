"""Tests for the Mode 06 monitor poll cache/diff state (Task 3.1)."""

from __future__ import annotations

import time

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger
from src.control.monitor_state import MonitorState
from src.telemetry import TelemetryRecord


def _cylinder_record(monitor_id: int, count: int, *, test_id: int = 0x0C) -> bytes:
    return bytes([monitor_id, test_id, 0x24, 0x00, count, 0x00, 0x00, 0x00, 0xFF])


def _reading(payload: bytes) -> TelemetryRecord:
    return TelemetryRecord.obd(time.time(), "MONITOR_RESULTS", payload.hex(), "")


def _emitter(tmp_path) -> AnomalyEventEmitter:  # type: ignore[no-untyped-def]
    return AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)


def test_snapshot_before_any_poll_is_empty_and_stale() -> None:
    state = MonitorState()
    snapshot = state.snapshot()
    assert snapshot["captured_at"] is None
    assert snapshot["misfire_counts"] == {}
    assert snapshot["results"] == []
    assert snapshot["stale"] is True


def test_observe_decodes_misfire_counts_and_full_results() -> None:
    state = MonitorState()
    payload = _cylinder_record(0xA2, 3) + _cylinder_record(0xA3, 0)
    state.observe(_reading(payload), session_id="s1")
    snapshot = state.snapshot()
    assert snapshot["captured_at"] is not None
    assert snapshot["misfire_counts"] == {1: 3, 2: 0}
    assert len(snapshot["results"]) == 2
    assert snapshot["stale"] is False


def test_observe_merges_individually_polled_mids_and_uses_current_cycle_tid() -> None:
    state = MonitorState()
    state.observe(_reading(_cylinder_record(0xA2, 9, test_id=0x0B)), session_id="s1")
    state.observe(_reading(_cylinder_record(0xA2, 1)), session_id="s1")
    state.observe(_reading(_cylinder_record(0xA3, 2)), session_id="s1")

    snapshot = state.snapshot()
    assert snapshot["misfire_counts"] == {1: 1, 2: 2}
    assert len(snapshot["results"]) == 3


def test_non_obd_and_unrelated_channels_are_ignored() -> None:
    state = MonitorState()
    state.observe(TelemetryRecord.can(time.time(), 0x123, b"\x01"), session_id="s1")
    state.observe(TelemetryRecord.obd(time.time(), "RPM", 700.0, ""), session_id="s1")
    assert state.snapshot()["captured_at"] is None


def test_stale_flag_flips_after_the_configured_window() -> None:
    from datetime import UTC, datetime, timedelta

    state = MonitorState(stale_after_seconds=10.0)
    state.observe(_reading(_cylinder_record(0xA2, 0)), session_id="s1")
    fresh = state.snapshot(now=datetime.now(UTC))
    assert fresh["stale"] is False
    later = state.snapshot(now=datetime.now(UTC) + timedelta(seconds=30))
    assert later["stale"] is True


def test_misfire_count_increase_emits_an_anomaly_event(tmp_path) -> None:  # type: ignore[no-untyped-def]
    state = MonitorState()
    emitter = _emitter(tmp_path)
    state.observe(_reading(_cylinder_record(0xA2, 1)), session_id="s1", emitter=emitter)
    assert emitter.counters.emitted == 1

    state.observe(_reading(_cylinder_record(0xA2, 3)), session_id="s1", emitter=emitter)
    assert emitter.counters.emitted == 2


def test_unchanged_misfire_count_does_not_reemit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    state = MonitorState()
    emitter = _emitter(tmp_path)
    state.observe(_reading(_cylinder_record(0xA2, 2)), session_id="s1", emitter=emitter)
    assert emitter.counters.emitted == 1
    state.observe(_reading(_cylinder_record(0xA2, 2)), session_id="s1", emitter=emitter)
    assert emitter.counters.emitted == 1


def test_zero_count_does_not_emit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    state = MonitorState()
    emitter = _emitter(tmp_path)
    state.observe(_reading(_cylinder_record(0xA2, 0)), session_id="s1", emitter=emitter)
    assert emitter.counters.emitted == 0


def test_event_source_is_misfire(tmp_path) -> None:  # type: ignore[no-untyped-def]
    logged: list[object] = []

    class _CapturingLogger:
        def log(self, event: object) -> None:
            logged.append(event)

        def close(self) -> None:
            pass

    emitter = AnomalyEventEmitter(_CapturingLogger(), min_seconds_between_events=0)  # type: ignore[arg-type]
    state = MonitorState()
    state.observe(_reading(_cylinder_record(0xA2, 1)), session_id="s1", emitter=emitter)
    assert len(logged) == 1
    assert logged[0].source == "misfire"  # type: ignore[attr-defined]
    assert logged[0].detail["cylinder"] == 1  # type: ignore[attr-defined]


def test_new_session_re_flags_a_still_present_count_as_an_increase(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Mirrors DtcState's per-session baseline reset: a count the ECU still
    carries over from a prior drive is re-flagged as an increase."""
    state = MonitorState()
    emitter = _emitter(tmp_path)
    state.observe(_reading(_cylinder_record(0xA2, 5)), session_id="s1", emitter=emitter)
    assert emitter.counters.emitted == 1

    # Same session, same count again: no re-emit.
    state.observe(_reading(_cylinder_record(0xA2, 5)), session_id="s1", emitter=emitter)
    assert emitter.counters.emitted == 1

    # New drive; the ECU still reports the same non-cleared count.
    state.observe(_reading(_cylinder_record(0xA2, 5)), session_id="s2", emitter=emitter)
    assert emitter.counters.emitted == 2


def test_unparseable_payload_is_skipped_not_raised() -> None:
    state = MonitorState()
    bad = TelemetryRecord.obd(time.time(), "MONITOR_RESULTS", "not-hex", "")
    state.observe(bad, session_id="s1")
    assert state.snapshot()["captured_at"] is None
