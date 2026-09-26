"""Passive mode (CAN transmit gate) — src/collection/tx_gate.py.

The gate exists for one situation: another tool owns a diagnostic session with
a module (an ECU reflash being the motivating case) and CANary must not put a
single frame on the bus. So these tests are about proving *nothing* is sent,
including from the paths that are easy to forget — ISO-TP flow control, and
the DTC/DID requests that don't come from the ordinary PID poll path.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import can
import pytest

from src.collection import tx_gate
from src.collection.can_obd_client import CanObdClient
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.obd_scheduler import CanObdSpec
from src.config import CanBusConfig


@pytest.fixture(autouse=True)
def _reset_gate() -> Iterator[None]:
    """The gate is process-wide state; never leak it between tests."""
    tx_gate.set_passive(False, source="test")
    tx_gate.reset_ecm_session_claims()
    tx_gate.reset_suppressed()
    yield
    tx_gate.set_passive(False, source="test")
    tx_gate.reset_ecm_session_claims()
    tx_gate.reset_suppressed()


class _RecordingBus:
    """Bus that records every transmit and answers nothing."""

    def __init__(self) -> None:
        self.sent: list[can.Message] = []

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return None

    def shutdown(self) -> None:  # pragma: no cover - interface completeness
        pass


def test_send_transmits_when_not_passive() -> None:
    bus = _RecordingBus()
    msg = can.Message(arbitration_id=0x7E0, data=b"\x02\x01\x0c", is_extended_id=False)

    assert tx_gate.send(bus, msg, what="test") is True  # type: ignore[arg-type]

    assert bus.sent == [msg]
    assert tx_gate.suppressed_count() == 0


def test_send_suppresses_and_counts_when_passive() -> None:
    bus = _RecordingBus()
    msg = can.Message(arbitration_id=0x7E0, data=b"\x02\x01\x0c", is_extended_id=False)
    tx_gate.set_passive(True, source="test")

    assert tx_gate.send(bus, msg, what="test") is False  # type: ignore[arg-type]
    assert tx_gate.send(bus, msg, what="test") is False  # type: ignore[arg-type]

    assert bus.sent == []
    assert tx_gate.suppressed_count() == 2


def test_set_passive_returns_previous_state() -> None:
    assert tx_gate.set_passive(True, source="test") is False
    assert tx_gate.set_passive(True, source="test") is True
    assert tx_gate.set_passive(False, source="test") is True


def test_snapshot_reports_state_source_and_count() -> None:
    bus = _RecordingBus()
    tx_gate.set_passive(True, source="control")
    tx_gate.send(  # type: ignore[arg-type]
        bus, can.Message(arbitration_id=0x7E0, data=b"\x01", is_extended_id=False), what="t"
    )

    snap = tx_gate.snapshot()

    assert snap == {
        "enabled": True,
        "source": "control",
        "suppressed_frames": 1,
        "ttl_remaining_seconds": None,
        "ecm_session_owner": "can_diagnostics",
        "ecm_session_claims": {},
        "tx_suppressed": True,
        "foreign_tester": None,
        "survives_restart": False,
    }


def test_set_passive_without_ttl_never_expires() -> None:
    """The default (no TTL) must keep today's behavior exactly — armed until
    someone explicitly disarms it, however long `check_expiry` is polled."""
    tx_gate.set_passive(True, source="test", now=1000.0)

    assert tx_gate.check_expiry(now=1000.0) is False
    assert tx_gate.check_expiry(now=10_000_000.0) is False
    assert tx_gate.is_passive() is True
    assert tx_gate.snapshot(now=10_000_000.0)["ttl_remaining_seconds"] is None


def test_check_expiry_auto_disarms_once_ttl_passes() -> None:
    tx_gate.set_passive(True, source="test", ttl_seconds=60.0, now=1000.0)

    assert tx_gate.snapshot(now=1030.0)["ttl_remaining_seconds"] == 30.0
    assert tx_gate.check_expiry(now=1059.9) is False
    assert tx_gate.is_passive() is True

    assert tx_gate.check_expiry(now=1060.0) is True
    assert tx_gate.is_passive() is False
    # Fired once — a second call after expiry is a no-op, not a repeat fire.
    assert tx_gate.check_expiry(now=1061.0) is False


def test_disarming_clears_a_pending_ttl() -> None:
    tx_gate.set_passive(True, source="test", ttl_seconds=60.0, now=1000.0)
    tx_gate.set_passive(False, source="test", now=1010.0)

    # Re-arm with no TTL: if the old deadline leaked through, this would
    # auto-expire at 1060 even though no TTL was requested this time.
    tx_gate.set_passive(True, source="test", now=1020.0)
    assert tx_gate.check_expiry(now=1060.0) is False
    assert tx_gate.is_passive() is True


def test_rearming_replaces_the_previous_ttl() -> None:
    tx_gate.set_passive(True, source="test", ttl_seconds=60.0, now=1000.0)
    tx_gate.set_passive(True, source="test", ttl_seconds=600.0, now=1010.0)

    assert tx_gate.check_expiry(now=1060.0) is False, "the old 60s deadline must not apply"
    assert tx_gate.snapshot(now=1010.0)["ttl_remaining_seconds"] == 600.0


def test_client_queries_send_nothing_when_passive() -> None:
    """CanObdClient is the ad-hoc query path (control routes, PID discovery)
    — it must respect the gate even though it never touches the poll loop.

    `enter_diagnostic_session` matters most here: a session-control request
    injected while a programming tool holds its own session is precisely the
    interference passive mode exists to prevent.
    """
    bus = _RecordingBus()
    client = CanObdClient(bus, timeout_seconds=0.01)  # type: ignore[arg-type]
    tx_gate.set_passive(True, source="test")

    assert client.query_mode_01(0x0C) is None
    assert client.query_mode_03() is None
    assert client.query_mode_09(0x04) is None
    assert client.query_mode_22(0x1101) is None
    assert client.enter_diagnostic_session(0xC0) is False
    assert client.query_supported_mode_01_pids() == ()

    assert bus.sent == []
    assert tx_gate.suppressed_count() >= 6


def test_poll_loop_transmits_nothing_when_passive() -> None:
    """The whole point: run the real collector loop against a bus that would
    happily accept frames, and prove none arrive."""
    bus = _RecordingBus()
    stop = threading.Event()
    tx_gate.set_passive(True, source="test")

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=50.0,
        pids=("RPM", "SPEED"),
        dids=("1101",),
        dtc_poll_interval_seconds=0.01,
        request_timeout_seconds=0.02,
    )

    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.5,
    )

    assert bus.sent == []
    assert stats.requests_sent == 0
    assert tx_gate.suppressed_count() == 0, (
        "the loop should skip issuing entirely, not lean on the send backstop"
    )


def test_passive_loop_does_not_accrue_timeouts_or_backoff() -> None:
    """Regression guard for the reason the pause lives in the loop rather than
    at the send. Blocking only the transmit would let every request wait out
    its timeout and escalate into the miss-backoff, so lifting passive mode
    would hand back a scheduler with every PID suppressed.
    """
    bus = _RecordingBus()
    stop = threading.Event()
    tx_gate.set_passive(True, source="test")

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=50.0,
        pids=("RPM", "SPEED"),
        request_timeout_seconds=0.02,
    )

    metrics: list[Any] = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.5,
        on_scheduler_metrics=metrics.append,
        scheduler_metrics_interval_seconds=0.05,
    )

    assert stats.request_timeouts == 0
    assert metrics, "scheduler metrics should keep flowing while passive"
    latest = metrics[-1].to_dict()
    assert latest["backed_off_requests"] == 0
    assert all(not r["backed_off"] for r in latest["requests"])


def test_loop_resumes_transmitting_after_passive_lifts() -> None:
    """Arming and disarming must be symmetric — the gate is a toggle, not a
    one-way latch, because it gets armed before a flash and lifted after."""
    bus = _RecordingBus()
    stop = threading.Event()

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=50.0,
        pids=("RPM",),
        request_timeout_seconds=0.02,
    )

    tx_gate.set_passive(True, source="test")
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.2,
    )
    assert bus.sent == []

    tx_gate.set_passive(False, source="test")
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _msg: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.3,
    )
    assert bus.sent, "polling must resume once passive mode is disarmed"


def test_passive_mode_suspends_the_arming_silence_disarm() -> None:
    """A reflash tool typically issues CommunicationControl/ControlDTCSetting
    to quiet normal bus traffic during erase/write. With CANary passive there
    is then no `can`/`obd` record left to arm the gate, GPS keeps chattering,
    and the silence timer would disarm recording during exactly the window
    passive mode was armed to capture.
    """
    from src.collection.record_arming import RecordArmingGate
    from src.telemetry import TelemetryRecord

    t0 = 1_778_000_000.0

    def armed_gate() -> RecordArmingGate:
        gate = RecordArmingGate(disarm_after_seconds=300.0)
        gate.observe(TelemetryRecord.can(t0, 0x180, b"\x01"))
        assert gate.armed is True
        return gate

    # Baseline (mirrors test_gate_disarms_when_bus_goes_silent_but_gps_keeps
    # _flowing): with passive mode off, bus silence still disarms.
    gate = armed_gate()
    assert gate.observe(TelemetryRecord.gps(t0 + 301.0, "speed", 0.0, "km/h")) is False
    assert gate.armed is False

    # Passive armed: identical silence must NOT disarm.
    tx_gate.set_passive(True, source="test")
    gate = armed_gate()
    assert gate.observe(TelemetryRecord.gps(t0 + 301.0, "speed", 0.0, "km/h")) is True
    assert gate.observe(TelemetryRecord.gps(t0 + 3600.0, "speed", 0.0, "km/h")) is True
    assert gate.armed is True

    # The timer resumes its normal job once passive mode lifts, so the
    # suppression can't strand a parked Pi in permanent recording.
    tx_gate.set_passive(False, source="test")
    assert gate.observe(TelemetryRecord.gps(t0 + 3601.0, "speed", 0.0, "km/h")) is False
    assert gate.armed is False


# --------------------------------------------------------------------------
# H05.6 — ECM diagnostic session ownership
# --------------------------------------------------------------------------


def _msg() -> can.Message:
    return can.Message(arbitration_id=0x7E0, data=b"\x02\x01\x0c", is_extended_id=False)


def test_a_kline_claim_suppresses_can_diagnostics_like_passive_mode() -> None:
    bus = _RecordingBus()
    assert tx_gate.ecm_session_owner() == "can_diagnostics"
    tx_gate.claim_ecm_session("kline", now=0.0)
    assert tx_gate.is_passive()
    assert tx_gate.send(bus, _msg(), what="t") is False  # type: ignore[arg-type]
    assert bus.sent == []
    snap = tx_gate.snapshot()
    assert snap["ecm_session_owner"] == "kline"
    assert snap["enabled"] is False  # the operator's switch is untouched
    assert snap["tx_suppressed"] is True
    tx_gate.release_ecm_session("kline", now=1.0)
    assert not tx_gate.is_passive()
    assert tx_gate.send(bus, _msg(), what="t") is True  # type: ignore[arg-type]


def test_a_foreign_tester_outranks_our_own_kline_session() -> None:
    tx_gate.claim_ecm_session("kline", now=0.0)
    previous = tx_gate.claim_ecm_session("foreign", now=1.0)
    assert previous == "kline"
    assert tx_gate.ecm_session_owner() == "foreign"
    tx_gate.release_ecm_session("foreign", now=2.0)
    assert tx_gate.ecm_session_owner() == "kline"


def test_a_claim_with_a_ttl_is_released_by_check_expiry() -> None:
    tx_gate.claim_ecm_session("foreign", ttl_seconds=10.0, now=100.0)
    assert tx_gate.check_expiry(now=105.0) is False
    assert tx_gate.snapshot(now=105.0)["ecm_session_claims"] == {"foreign": 5.0}
    assert tx_gate.check_expiry(now=110.0) is True
    assert tx_gate.ecm_session_owner() == "can_diagnostics"


def test_renewing_a_claim_pushes_its_expiry_out() -> None:
    tx_gate.claim_ecm_session("foreign", ttl_seconds=10.0, now=100.0)
    tx_gate.claim_ecm_session("foreign", ttl_seconds=10.0, now=108.0)
    assert tx_gate.check_expiry(now=112.0) is False
    assert tx_gate.ecm_session_owner() == "foreign"


def test_releasing_a_claim_leaves_the_operators_passive_switch_alone() -> None:
    tx_gate.set_passive(True, source="control")
    tx_gate.claim_ecm_session("kline", now=0.0)
    tx_gate.release_ecm_session("kline", now=1.0)
    assert tx_gate.is_passive()


def test_ownership_changes_open_and_close_capture_pause_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bool, str]] = []
    monkeypatch.setattr(
        tx_gate.capture_lifetime,
        "mark_paused",
        lambda paused, *, reason, now: calls.append((paused, reason)),
    )
    tx_gate.claim_ecm_session("kline", now=0.0)
    tx_gate.claim_ecm_session("foreign", now=1.0)
    tx_gate.release_ecm_session("foreign", now=2.0)
    tx_gate.release_ecm_session("kline", now=3.0)
    assert calls == [
        (True, "ecm-session-kline"),
        (False, "ecm-session-kline"),
        (True, "ecm-session-foreign"),
        (False, "ecm-session-foreign"),
        (True, "ecm-session-kline"),
        (False, "ecm-session-kline"),
    ]


def test_the_foreign_testers_address_and_last_heard_time_are_in_the_snapshot() -> None:
    """H06.4: ``foreign`` says someone else holds the session; this says who
    and when, and outlives the claim so a pause stays explained."""
    assert tx_gate.snapshot()["foreign_tester"] is None
    tx_gate.claim_ecm_session("foreign", ttl_seconds=10.0, now=100.0)
    tx_gate.note_foreign_tester(
        tester_address=0xFC, module_address=0x10, now=100.0, now_wall=1_790_000_000.0
    )
    heard = tx_gate.snapshot(now=103.0)["foreign_tester"]
    assert heard == {
        "tester_address": "FC",
        "module_address": "10",
        "last_heard_at": "2026-09-21T14:13:20+00:00",
        "seconds_since_heard": 3.0,
    }
    # Hold expires: ownership returns, the last-heard record stays.
    assert tx_gate.check_expiry(now=111.0) is True
    snap = tx_gate.snapshot(now=111.0)
    assert snap["ecm_session_owner"] == "can_diagnostics"
    assert snap["foreign_tester"]["tester_address"] == "FC"
    assert snap["foreign_tester"]["seconds_since_heard"] == 11.0
    # A later frame from an unaddressed (mid-session) tester replaces it.
    tx_gate.note_foreign_tester(tester_address=None, module_address=None, now=120.0)
    assert tx_gate.snapshot(now=120.0)["foreign_tester"]["tester_address"] is None
    tx_gate.reset_ecm_session_claims()
    assert tx_gate.snapshot()["foreign_tester"] is None


def test_passive_armed_is_the_operators_switch_not_a_session_claim() -> None:
    """K01.5: the K-line reader may start while it (or CAN) holds a claim, but
    never while the operator has armed passive mode for another tool."""
    tx_gate.claim_ecm_session("kline", now=0.0)
    assert tx_gate.is_passive() and not tx_gate.passive_armed()
    tx_gate.release_ecm_session("kline", now=1.0)
    tx_gate.set_passive(True, source="test")
    assert tx_gate.passive_armed()
    tx_gate.set_passive(False, source="test")
    assert not tx_gate.passive_armed()


# --- D03.30: an armed passive survives a daemon restart ----------------------


def _forget_process_memory() -> None:
    """What a new process starts with: passive off, no state file named.

    ``persist=False`` so the file the previous "process" wrote stays on disk,
    exactly as a restart leaves it.
    """
    tx_gate.configure_persistence(None)
    tx_gate.set_passive(False, source="default", persist=False)


def test_an_arm_is_written_and_a_disarm_removes_it(tmp_path: Any) -> None:
    state = tmp_path / "control" / tx_gate.PASSIVE_STATE_FILENAME
    assert tx_gate.restore_persisted(state) == "absent"
    tx_gate.set_passive(True, source="control", ttl_seconds=1800)
    written = json.loads(state.read_text())
    assert written["source"] == "control"
    assert written["ttl_seconds"] == 1800
    assert written["version"] == tx_gate.PASSIVE_STATE_VERSION
    assert abs(written["armed_at"] - time.time()) < 60
    assert tx_gate.snapshot()["survives_restart"] is True

    tx_gate.set_passive(False, source="control")
    assert not state.exists()
    assert tx_gate.snapshot()["survives_restart"] is False


def test_without_a_state_file_an_arm_is_process_memory_only(tmp_path: Any) -> None:
    """Nothing named a file (a CLI tool, a bare import): nothing is written,
    and the status payload says the arm would not survive."""
    tx_gate.configure_persistence(None)
    tx_gate.set_passive(True, source="control", ttl_seconds=60)
    assert tx_gate.snapshot()["survives_restart"] is False
    assert list(tmp_path.iterdir()) == []


def test_a_ttl_arm_is_restored_with_the_ttl_that_was_left(tmp_path: Any) -> None:
    state = tmp_path / tx_gate.PASSIVE_STATE_FILENAME
    tx_gate.restore_persisted(state)
    tx_gate.set_passive(True, source="control", ttl_seconds=1800)
    armed_at = json.loads(state.read_text())["armed_at"]

    _forget_process_memory()
    assert not tx_gate.is_passive()
    now = time.monotonic()
    assert tx_gate.restore_persisted(state, now_wall=armed_at + 600) == "restored"
    assert tx_gate.is_passive()
    snap = tx_gate.snapshot(now=now)
    assert snap["source"] == "restored:control"
    assert snap["ttl_remaining_seconds"] == pytest.approx(1200, abs=5)
    # The file keeps the original arm: a second restart restores the same
    # deadline, not a fresh 1800 s.
    assert json.loads(state.read_text())["armed_at"] == armed_at
    assert json.loads(state.read_text())["source"] == "control"


def test_an_arm_that_expired_while_down_is_dropped_and_logged(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    state = tmp_path / tx_gate.PASSIVE_STATE_FILENAME
    tx_gate.restore_persisted(state)
    tx_gate.set_passive(True, source="mcp", ttl_seconds=60)
    armed_at = json.loads(state.read_text())["armed_at"]

    _forget_process_memory()
    with caplog.at_level("WARNING", logger="src.collection.tx_gate"):
        assert tx_gate.restore_persisted(state, now_wall=armed_at + 90) == "expired"
    assert not tx_gate.is_passive()
    assert not state.exists()
    assert any("expired 30 s before this start" in r.getMessage() for r in caplog.records)


def test_a_clock_that_stepped_back_cannot_lengthen_the_arm(tmp_path: Any) -> None:
    state = tmp_path / tx_gate.PASSIVE_STATE_FILENAME
    tx_gate.restore_persisted(state)
    tx_gate.set_passive(True, source="control", ttl_seconds=300)
    armed_at = json.loads(state.read_text())["armed_at"]

    _forget_process_memory()
    now = time.monotonic()
    assert tx_gate.restore_persisted(state, now_wall=armed_at - 3600) == "restored"
    assert tx_gate.snapshot(now=now)["ttl_remaining_seconds"] <= 300


def test_an_arm_without_a_ttl_stays_armed_across_a_restart(tmp_path: Any) -> None:
    state = tmp_path / tx_gate.PASSIVE_STATE_FILENAME
    tx_gate.restore_persisted(state)
    tx_gate.set_passive(True, source="control")

    _forget_process_memory()
    assert tx_gate.restore_persisted(state) == "restored"
    assert tx_gate.is_passive()
    assert tx_gate.snapshot()["ttl_remaining_seconds"] is None


def test_ttl_expiry_removes_the_state_file(tmp_path: Any) -> None:
    state = tmp_path / tx_gate.PASSIVE_STATE_FILENAME
    tx_gate.restore_persisted(state)
    tx_gate.set_passive(True, source="control", ttl_seconds=10, now=100.0)
    assert state.exists()
    assert tx_gate.check_expiry(now=111.0)
    assert not state.exists()


@pytest.mark.parametrize(
    ("content", "cause"),
    [
        ("{not json", "not JSON"),
        (
            json.dumps({"version": 99, "armed_at": 1.0, "ttl_seconds": 60, "source": "control"}),
            "unknown state version 99",
        ),
        (
            json.dumps({"version": tx_gate.PASSIVE_STATE_VERSION, "ttl_seconds": 60}),
            "missing or malformed armed_at",
        ),
    ],
    ids=["bad-json", "unknown-version", "missing-armed-at"],
)
def test_an_untrusted_state_file_fails_safe(
    tmp_path: Any, caplog: pytest.LogCaptureFixture, content: str, cause: str
) -> None:
    """A state file that exists but cannot be trusted arms passive with no
    TTL and keeps the file: failing open is the 2026-09-24 incident."""
    state = tmp_path / tx_gate.PASSIVE_STATE_FILENAME
    state.write_text(content)
    with caplog.at_level("ERROR", logger="src.collection.tx_gate"):
        assert tx_gate.restore_persisted(state) == "untrusted"
    assert tx_gate.is_passive()
    snap = tx_gate.snapshot()
    assert snap["source"] == tx_gate.UNTRUSTED_STATE_SOURCE
    assert snap["ttl_remaining_seconds"] is None
    assert snap["survives_restart"] is True
    assert state.read_text() == content, "the evidence is kept, never silently deleted"
    assert any(cause in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)

    # The operator's disarm is what clears it, file included.
    tx_gate.set_passive(False, source="control")
    assert not state.exists()


def test_a_state_file_that_cannot_be_read_fails_safe(
    tmp_path: Any, caplog: pytest.LogCaptureFixture
) -> None:
    state = tmp_path / tx_gate.PASSIVE_STATE_FILENAME
    state.mkdir()  # read_text raises IsADirectoryError, an OSError
    with caplog.at_level("ERROR", logger="src.collection.tx_gate"):
        assert tx_gate.restore_persisted(state) == "untrusted"
    assert tx_gate.is_passive()
    assert tx_gate.snapshot()["source"] == tx_gate.UNTRUSTED_STATE_SOURCE
    assert state.exists()
    assert any("unreadable: IsADirectoryError" in r.getMessage() for r in caplog.records)


def test_a_failed_write_does_not_claim_the_arm_survives(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``survives_restart`` is set by a successful write only: a swallowed
    OSError must not let ``restart_canary.sh`` promise a restore."""
    state = tmp_path / tx_gate.PASSIVE_STATE_FILENAME
    tx_gate.restore_persisted(state)

    def _disk_full(*_a: Any, **_k: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tx_gate, "atomic_write_text", _disk_full)
    tx_gate.set_passive(True, source="control", ttl_seconds=600)
    assert tx_gate.is_passive()
    assert tx_gate.snapshot()["survives_restart"] is False
    assert not state.exists()

    monkeypatch.undo()
    tx_gate.set_passive(True, source="control", ttl_seconds=600)
    assert tx_gate.snapshot()["survives_restart"] is True
