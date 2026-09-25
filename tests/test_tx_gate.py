"""Passive mode (CAN transmit gate) — src/collection/tx_gate.py.

The gate exists for one situation: another tool owns a diagnostic session with
a module (an ECU reflash being the motivating case) and CANary must not put a
single frame on the bus. So these tests are about proving *nothing* is sent,
including from the paths that are easy to forget — ISO-TP flow control, and
the DTC/DID requests that don't come from the ordinary PID poll path.
"""

from __future__ import annotations

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
