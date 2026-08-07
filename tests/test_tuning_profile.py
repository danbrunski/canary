"""Focused tuning poll profile — src/collection/tuning_profile.py.

Phase 1.1 of engine-tuning-support.md. The point is concentrating the bus
budget: normal polling lands TIMING_ADVANCE at 0.06 Hz, or ~10 samples per
rpm x load cell over a whole drive, which is not enough to conclude anything.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import can
import pytest
from pydantic import ValidationError

from src.collection import tuning_profile, tx_gate
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_profiles import CaptureProfile
from src.collection.obd_scheduler import CanObdSpec
from src.config import CanBusConfig


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    """Process-wide state; never leak between tests."""
    tuning_profile.set_active(False, source="test")
    tx_gate.set_passive(False, source="test")
    yield
    tuning_profile.set_active(False, source="test")
    tx_gate.set_passive(False, source="test")


class _FakeBus:
    """Answers every Mode 01 request so the loop keeps cycling."""

    def __init__(self) -> None:
        self.sent: list[can.Message] = []

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return None

    def shutdown(self) -> None:  # pragma: no cover - interface completeness
        pass

    def requested_pids(self) -> set[int]:
        """PID byte of each Mode 01 request put on the bus."""
        out = set()
        for m in self.sent:
            d = bytes(m.data)
            if len(d) >= 3 and d[1] == 0x01:
                out.add(d[2])
        return out


def _spec(**kw: Any) -> CanObdSpec:
    base: dict[str, Any] = {
        "can": CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        "poll_rate_hz": 50.0,
        "request_timeout_seconds": 0.02,
    }
    base.update(kw)
    return CanObdSpec(**base)


def test_default_state_is_inactive() -> None:
    assert tuning_profile.is_active() is False
    snap = tuning_profile.snapshot()
    assert snap["active"] is False
    assert "RPM" in snap["channels"]


def test_set_active_returns_previous_state() -> None:
    assert tuning_profile.set_active(True, source="test") is False
    assert tuning_profile.set_active(True, source="test") is True
    assert tuning_profile.set_active(False, source="test") is True


def test_disabling_restores_the_default_channel_set() -> None:
    """A narrow one-off must not silently persist into the next capture."""
    tuning_profile.set_active(True, channel_names=frozenset({"RPM"}), source="test")
    assert tuning_profile.channels() == frozenset({"RPM"})

    tuning_profile.set_active(False, source="test")

    assert tuning_profile.channels() == tuning_profile.DEFAULT_TUNING_CHANNELS


def test_request_eligible_restricts_to_the_tuning_set() -> None:
    tuning_profile.set_active(True, source="test")

    class R:
        def __init__(self, channel: str) -> None:
            self.channel = channel

    assert tuning_profile.request_eligible(0, R("RPM")) is True
    assert tuning_profile.request_eligible(0, R("TIMING_ADVANCE")) is True
    assert tuning_profile.request_eligible(0, R("FUEL_LEVEL")) is False
    assert tuning_profile.request_eligible(0, R("CATALYST_TEMP_B1S1")) is False


def test_poll_loop_only_requests_tuning_channels_when_active() -> None:
    """The whole point: the bus rotation narrows to the tuning set."""
    bus = _FakeBus()
    stop = threading.Event()
    tuning_profile.set_active(True, source="test")

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "TIMING_ADVANCE", "FUEL_LEVEL", "CATALYST_TEMP_B1S1")),
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.6,
    )

    pids = bus.requested_pids()
    assert 0x0C in pids, "RPM should be polled"
    assert 0x0E in pids, "TIMING_ADVANCE should be polled"
    assert 0x2F not in pids, "FUEL_LEVEL is outside the tuning set"
    assert 0x3C not in pids, "CATALYST_TEMP_B1S1 is outside the tuning set"


def test_poll_loop_polls_everything_when_inactive() -> None:
    """Symmetry: the restriction lifts cleanly."""
    bus = _FakeBus()
    stop = threading.Event()

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "FUEL_LEVEL")),
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.6,
    )

    pids = bus.requested_pids()
    assert 0x0C in pids
    assert 0x2F in pids, "FUEL_LEVEL should be polled when tuning mode is off"


def test_tuning_mode_does_not_defeat_passive_mode() -> None:
    """Passive mode is a safety gate and must win. Tuning mode narrows WHICH
    channels are polled; it must never re-enable transmission."""
    bus = _FakeBus()
    stop = threading.Event()
    tx_gate.set_passive(True, source="test")
    tuning_profile.set_active(True, source="test")

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "TIMING_ADVANCE")),
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.4,
    )

    assert bus.sent == []


def test_snapshot_carries_achieved_rate_when_supplied() -> None:
    """Phase 1.1 asks for the achieved per-channel Hz to be recorded."""
    tuning_profile.set_active(True, source="control")

    snap = tuning_profile.snapshot(achieved_hz={"RPM": 4.2})

    assert snap["active"] is True
    assert snap["source"] == "control"
    assert snap["achieved_hz"] == {"RPM": 4.2}


def test_snapshot_exposes_the_capture_plan_the_dashboard_must_verify() -> None:
    profile = CaptureProfile(
        name="research",
        description="Hybrid validation",
        experimental=True,
        mode01={"RPM": 1.0},
        mode22=("ECM_RPM",),
        target_rate_hz=4.0,
        did_interval_seconds=0.25,
        bus_budget_override=90.0,
        expected_total_demand_hz=8.0,
        capacity_evidence="fast exit verified",
        corpus_eligible=False,
        storage_prefix="_capture-research",
    )
    tuning_profile.activate(profile, source="test")

    snap = tuning_profile.snapshot()

    assert snap["description"] == "Hybrid validation"
    assert snap["experimental"] is True
    assert snap["target_rate_hz"] == 4.0
    assert snap["did_continuous"] is False
    assert snap["expected_total_demand_hz"] == 8.0
    assert snap["capacity_evidence"] == "fast exit verified"


def test_default_set_carries_the_filters_every_analysis_depends_on() -> None:
    """Regression guard. A tuning capture that omits any of these cannot be
    filtered after the fact, and the data is unrecoverable once the drive is
    over.

    COOLANT_TEMP was genuinely missing from the first shipped set and was
    caught mid-drive on 2026-07-27 with the capture already running: every
    trim analysis filters on coolant > 70 degC to exclude warm-up, and
    `vehicle_state` reports coolant as null without it.
    """
    required = {
        "COOLANT_TEMP",  # warm-up exclusion
        "EVAPORATIVE_PURGE",  # purge shifts trim 4.15 points
        "FUEL_STATUS",  # open vs closed loop, and the only reliable WOT gate
        "INTAKE_TEMP",  # charge temp; also the stagnation control (see G.3)
        # MAF + RPM are the operating-point control. ENGINE_LOAD is NOT a
        # substitute -- it reads ~20% at idle and at light cruise alike, so
        # binning on it mixes stationary with moving and produced two wrong
        # conclusions on 2026-07-27 (engine-tuning-support.md G.3a, G.4).
        "MAF",
        "RPM",
    }
    missing = required - tuning_profile.DEFAULT_TUNING_CHANNELS
    assert not missing, f"tuning captures would be unfilterable without {missing}"


def test_dtc_reads_survive_tuning_mode() -> None:
    """DTC polling is exempt from the narrowed rotation.

    It was excluded on a bus-budget argument that the numbers do not support:
    the DTC tier polls at 0.0033 Hz, one request per five minutes against a
    budget of eight per second. The cost was the 2026-07-27 post-flash drive,
    where the whole question was whether a fresh calibration set a code and
    `get_dtc` reported `stale: true` for the entire drive.
    """
    tuning_profile.set_active(True, source="test")

    class R:
        def __init__(self, channel: str, kind: str) -> None:
            self.channel = channel
            self.kind = kind

    assert tuning_profile.request_eligible(0, R("DTC_STORED", "dtc")) is True
    assert tuning_profile.request_eligible(0, R("DTC_PENDING", "dtc")) is True
    # Mode 06 / freeze-frame stay excluded — those really are slow-tier bulk.
    assert tuning_profile.request_eligible(0, R("MONITOR_RESULTS", "service")) is False
    assert tuning_profile.request_eligible(0, R("FREEZE_FRAME_RPM", "freeze_frame")) is False


def test_speed_is_polled_so_operating_mode_survives() -> None:
    """SPEED is in the set for classification, not for tuning.

    Without it the segmenter has only GPS to work from; on the 2026-07-27
    post-flash drive 4 of 10 anomalies logged `operating_mode: unknown`.
    """
    assert "SPEED" in tuning_profile.DEFAULT_TUNING_CHANNELS


def test_target_rate_hz_overrides_the_relative_rate_model() -> None:
    """A capture can ask for an absolute rate; weights alone cannot express one.

    The relative model is ``poll_rate_hz × tier_factor × weight``, and every
    path that built a tuning profile hardcoded weight 1.0 — so per-channel
    demand topped out at ``poll_rate_hz`` however much bus budget was free.
    Measured on the truck 2026-08-04: raising the budget 8 → 30 req/s moved
    the achieved rate from 0.54 Hz to 0.54 Hz, because demand never reached
    the old ceiling. Meanwhile the ECM answered 478 channel-reads/s batched
    with zero timeouts, so the ceiling was never the bus.
    """
    from src.collection.obd_scheduler import _PollRequest, _request_rate_hz

    spec = _spec(poll_rate_hz=1.0)
    request = _PollRequest(
        kind="pid",
        service=0x01,
        identifier=0x0E,
        request_id=0x7DF,
        channel="TIMING_ADVANCE",
        poll_tier="medium",
    )

    # Relative model: medium tier is a third of a 1 Hz base.
    tuning_profile.set_active(True, channel_names=frozenset({"TIMING_ADVANCE"}), source="test")
    assert _request_rate_hz(request, spec) == pytest.approx(1.0 / 3.0)

    # Absolute rate ignores both the tier factor and the base rate.
    tuning_profile.set_active(
        True, channel_names=frozenset({"TIMING_ADVANCE"}), rate_hz=20.0, source="test"
    )
    assert _request_rate_hz(request, spec) == pytest.approx(20.0)
    assert tuning_profile.snapshot()["target_rate_hz"] == pytest.approx(20.0)


def test_target_rate_applies_only_to_selected_channels() -> None:
    """A profile carrying a target rate must not accelerate channels it omits.

    ``request_eligible`` already excludes them, but rate and eligibility are
    consulted independently, so an unlisted channel leaking the capture rate
    would spend budget on rows the capture never asked for.
    """
    from src.collection.obd_scheduler import _PollRequest, _request_rate_hz

    spec = _spec(poll_rate_hz=1.0)
    tuning_profile.set_active(
        True, channel_names=frozenset({"TIMING_ADVANCE"}), rate_hz=20.0, source="test"
    )
    other = _PollRequest(
        kind="pid",
        service=0x01,
        identifier=0x05,
        request_id=0x7DF,
        channel="COOLANT_TEMP",
        poll_tier="medium",
    )
    assert tuning_profile.absolute_rate_hz("COOLANT_TEMP") is None
    assert _request_rate_hz(other, spec) == pytest.approx(1.0 / 3.0)


def test_target_rate_hz_is_bounded() -> None:
    """Demand above the bus budget's own ceiling is a config error, not a request."""
    from src.collection.capture_profiles import MAX_TARGET_RATE_HZ, CaptureProfile

    with pytest.raises(ValidationError):
        CaptureProfile(name="x", description="", mode01={"RPM": 1.0}, target_rate_hz=0)
    with pytest.raises(ValidationError):
        CaptureProfile(
            name="x", description="", mode01={"RPM": 1.0}, target_rate_hz=MAX_TARGET_RATE_HZ + 1
        )


def _did_controller(monkeypatch: pytest.MonkeyPatch, *, continuous: bool):
    """A window controller over two DIDs, with the profile's continuity forced."""
    from src.collection.obd_scheduler import DidSessionSpec, DidWindowController, _PollRequestState

    monkeypatch.setattr(tuning_profile, "did_continuous_enabled", lambda: continuous)
    spec = DidSessionSpec(session=0xC0, window_max_seconds=10.0, lapse_seconds=5.0)
    ctrl = DidWindowController(spec, (0, 1), now=0.0)
    states = [_PollRequestState(), _PollRequestState()]
    ctrl.on_session_response(ok=True, states=states, now=0.0)
    assert ctrl.phase == "window"
    return ctrl, states


def test_did_window_closes_when_every_did_was_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Baseline: the windowed model hands the bus back once the round is done."""
    ctrl, states = _did_controller(monkeypatch, continuous=False)
    for i in (0, 1):
        ctrl.on_request_attempted(i)
    ctrl.advance(states, 1.0)
    assert ctrl.phase == "lapse"


def test_continuous_capture_holds_the_session_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """An all-DID capture must not close the window, and must re-arm its DIDs.

    Closing costs a session re-entry plus a lapse in which the capture records
    nothing — and there is no Mode 01 to hand the bus back to, since a
    continuous profile has `mode01_enabled: false`. Measured 2026-08-05, the
    windowed alternative cost ~40 s of Mode-01 channels per brief session.
    """
    ctrl, states = _did_controller(monkeypatch, continuous=True)
    for i in (0, 1):
        ctrl.on_request_attempted(i)
    ctrl.advance(states, 1.0)
    assert ctrl.phase == "window"
    # Both DIDs are due again, so the next round can proceed immediately.
    assert ctrl.request_eligible(0, SimpleNamespace(kind="did"), now=1.0) is True
    assert ctrl.request_eligible(1, SimpleNamespace(kind="did"), now=1.0) is True
    # Past the window deadline it still does not close.
    ctrl.advance(states, 999.0)
    assert ctrl.phase == "window"


def test_continuous_capture_does_not_spin_the_poll_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`next_transition_at` must stay in the future once the deadline passes.

    The windowed form returns min(deadline, keepalive); with the deadline long
    gone that is a time in the past, and the loop would busy-wait on it.
    """
    ctrl, states = _did_controller(monkeypatch, continuous=True)
    # Hold the window well past `window_max_seconds` (10 s), with a keep-alive
    # sent recently — the state a long capture is actually in.
    ctrl.advance(states, 500.0)
    ctrl.on_session_response(ok=True, states=states, now=500.0)

    monkeypatch.setattr(tuning_profile, "did_continuous_enabled", lambda: False)
    windowed = ctrl.next_transition_at()
    monkeypatch.setattr(tuning_profile, "did_continuous_enabled", lambda: True)
    continuous = ctrl.next_transition_at()

    # Windowed returns the long-expired deadline, i.e. a time in the past,
    # which the poll loop would spin on. Continuous returns the next
    # keep-alive, still ahead.
    assert windowed < 500.0
    assert continuous > 500.0


def test_ending_a_continuous_capture_releases_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deactivating the profile must let the window close, or Mode 01 stays dead.

    The ECM answers no Mode 01 while session 0xC0 is held, and the blackout
    outlives the session (measured 2026-08-04: >30 s after an S3 lapse), so a
    capture that ended without releasing would look like a dead bus.
    """
    ctrl, states = _did_controller(monkeypatch, continuous=True)
    ctrl.advance(states, 999.0)
    assert ctrl.phase == "window"
    monkeypatch.setattr(tuning_profile, "did_continuous_enabled", lambda: False)
    ctrl.advance(states, 1000.0)
    assert ctrl.phase == "lapse"


def test_target_rate_applies_to_dids_not_just_pids() -> None:
    """An all-DID capture is exactly the case where the rate must be sayable."""
    from src.collection.obd_scheduler import _PollRequest, _request_rate_hz

    tuning_profile.configure_did_aliases({"ECM_AF_SENSOR_V_B1": "DID_1225"})
    profile = CaptureProfile(
        name="tuning-did",
        description="",
        mode01={},
        mode01_enabled=False,
        mode22=("ECM_AF_SENSOR_V_B1",),
        did_continuous=True,
        target_rate_hz=25.0,
        corpus_eligible=False,
        storage_prefix="_capture-tuning-did",
    )
    tuning_profile.activate(profile, source="test")
    req = _PollRequest(
        kind="did",
        service=0x22,
        identifier=0x1225,
        request_id=0x7E0,
        channel="DID_1225",
        poll_tier="medium",
    )
    assert _request_rate_hz(req, _spec(poll_rate_hz=1.0)) == pytest.approx(25.0)
    # A DID the profile did not list keeps the relative model.
    other = _PollRequest(
        kind="did",
        service=0x22,
        identifier=0x1226,
        request_id=0x7E0,
        channel="DID_1226",
        poll_tier="medium",
    )
    assert tuning_profile.absolute_rate_hz("DID_1226") is None
    assert _request_rate_hz(other, _spec(poll_rate_hz=1.0)) == pytest.approx(1.0 / 3.0)


def test_achieved_hz_reports_a_trailing_window_not_a_lifetime_average() -> None:
    """The rate an operator needs is "right now", not "since boot".

    On 2026-08-04 the lifetime average barely moved through a real rate
    change, because hours of history dominated a few minutes of new
    behaviour — which read as "the change did nothing". The trailing window
    answers the question that was actually being asked.
    """
    from src.collection.obd_scheduler import ACHIEVED_HZ_WINDOW, CanObdLoopStats

    stats = CanObdLoopStats()
    now = 1000.0
    # Chronological, as real reads arrive: 10 stale, then 30 inside the window.
    for i in range(10):
        stats.record_read("RPM", now - ACHIEVED_HZ_WINDOW - 10 + i)
    for i in range(30, 0, -1):
        stats.record_read("RPM", now - i)
    achieved = stats.achieved_hz(now)
    assert achieved["RPM"] == pytest.approx(30 / ACHIEVED_HZ_WINDOW, rel=1e-6)

    # A channel that has gone quiet drops out rather than reporting a stale rate.
    stats.record_read("MAF", now - ACHIEVED_HZ_WINDOW - 1)
    assert "MAF" not in stats.achieved_hz(now)


def test_achieved_hz_is_per_channel() -> None:
    """A capture can be fine for one channel and starved for another.

    TIMING_ADVANCE fell to 2.6 Hz while RPM held ~6 Hz on the same drive; an
    aggregate rate hid that completely.
    """
    from src.collection.obd_scheduler import CanObdLoopStats

    stats = CanObdLoopStats()
    now = 500.0
    for i in range(60):
        stats.record_read("RPM", now - i * 0.5)
    for i in range(6):
        stats.record_read("TIMING_ADVANCE", now - i * 5)
    achieved = stats.achieved_hz(now)
    assert achieved["RPM"] > achieved["TIMING_ADVANCE"]
    assert set(achieved) == {"RPM", "TIMING_ADVANCE"}


def test_metrics_payload_labels_the_unfiltered_request_count() -> None:
    """`active_requests` ignores the capture profile filter — say so in the payload.

    It reported ~77 during a capture narrowed to 9 channels, which read as
    "the capture did not narrow" when it had. The session parquet was the
    real answer; the metric just had a misleading name.
    """
    from src.collection.obd_scheduler import ObdSchedulerMetrics

    payload = ObdSchedulerMetrics(
        configured_poll_rate_hz=1.0,
        bus_budget_requests_per_second=8.0,
        active_requests=77,
        backed_off_requests=0,
        requests_sent=10,
        readings_emitted=20,
        request_timeouts=0,
        negative_responses=0,
        bus_errors=0,
        response_rate=2.0,
        by_tier={},
        achieved_hz_60s={"RPM": 8.3},
    ).to_dict()
    assert payload["active_requests_unfiltered"] == 77
    assert payload["achieved_hz_60s"] == {"RPM": 8.3}


def test_capture_rate_uses_capture_epoch_and_observed_interval() -> None:
    from collections import deque

    from src.collection.can_obd_collector import CanObdLoopStats

    stats = CanObdLoopStats(recent_reads={"RPM": deque([90.0, 110.0, 115.0])})
    rates, observed = stats.capture_rates(now=120.0, started_at=100.0)

    # Pre-arm reads stay out; the 5 s gap between the two in-epoch samples is
    # the rate, and the 10 s wait before the first one is coverage, not rate.
    assert observed == 20.0
    assert rates["RPM"].samples == 2
    assert rates["RPM"].hz == pytest.approx(0.2)
    assert rates["RPM"].max_gap_seconds == pytest.approx(10.0)


def test_capture_rate_is_not_diluted_by_idle_time_inside_the_capture() -> None:
    """Idle time inside a capture is a coverage fact, not a slow rate.

    Measured 2026-08-06: an all-DID capture polled at a solid 14.93 Hz for its
    whole active stretch, but the Mode 22 window only opened ~11 s after the
    arm. Dividing samples by wall-clock reported 11.25 Hz, which read as a
    rate shortfall and sent debugging after per-request loop latency that did
    not exist. The rate must reflect the pace while sampling; the late start
    must show up as a gap instead.
    """
    from collections import deque

    from src.collection.obd_scheduler import CanObdLoopStats

    started_at, now = 0.0, 60.0
    period = 1.0 / 15.0
    # Nothing for the first 11 s, then a clean 15 Hz to the end.
    reads = [11.0 + i * period for i in range(int((now - 11.0) / period))]
    stats = CanObdLoopStats(recent_reads={"DID_1219": deque(reads)})

    rates, observed = stats.capture_rates(now=now, started_at=started_at)

    assert observed == 60.0
    assert rates["DID_1219"].hz == pytest.approx(15.0, rel=1e-3)
    # The naive samples/wall-clock number this replaces.
    assert len(reads) / observed == pytest.approx(12.2, abs=0.5)
    assert rates["DID_1219"].max_gap_seconds == pytest.approx(11.0, abs=0.1)


def test_capture_rate_surfaces_a_mid_capture_stall_the_median_would_hide() -> None:
    """A channel that dies halfway keeps its rate but must not read as healthy."""
    from collections import deque

    from src.collection.obd_scheduler import CanObdLoopStats

    period = 1.0 / 15.0
    reads = [i * period for i in range(150)]  # 15 Hz for 10 s, then silence
    stats = CanObdLoopStats(recent_reads={"DID_1219": deque(reads)})

    rates, _ = stats.capture_rates(now=40.0, started_at=0.0)

    assert rates["DID_1219"].hz == pytest.approx(15.0, rel=1e-3)
    assert rates["DID_1219"].max_gap_seconds == pytest.approx(30.0, abs=0.2)
