"""Lossless diagnostic discovery (roadmap task C06).

Everything runs on the fake clock and :class:`ScriptedEcu` with numbered
broadcast frames injected around every reply. The fake bus records every frame
it hands out, so a frame missing from the tap was *consumed untapped* by a
receive path that filtered it — never lost upstream in a kernel queue.

The headline counterexample (C06.1): startup support discovery and the Mode 06
re-probe ran through ``CanObdClient`` directly, whose receive loop discarded
every frame that was not the awaited response. Broadcast frames arriving in
those windows never reached raw capture.
"""

from __future__ import annotations

import threading
from collections import Counter
from itertools import pairwise
from pathlib import Path

import can
import pytest
from fastapi.testclient import TestClient

from src.collection import discovery as discovery_registry
from src.collection import tuning_profile
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_profiles import CaptureProfile
from src.collection.obd_collector import ObdReading, reading_to_record
from src.collection.obd_scheduler import (
    CanObdLoopStats,
    CanObdSpec,
    DidSessionSpec,
    DidWindowController,
    FreezeFrameTrigger,
    ObdSchedulerMetrics,
    SessionControlResult,
    _PollRequest,
    _PollRequestState,
)
from src.collection.request_eligibility import (
    PROFILE_CLASSES,
    REQUEST_KINDS,
    SESSION_PHASES,
    evaluate,
    matrix_rows,
)
from src.config import CanaryConfig, CanBusConfig
from src.dashboard.api import create_app
from tests.capture_round_fakes import (
    SUPPORT_GROUPS,
    FakeClock,
    ScriptedEcu,
    broadcast_sequence,
    install_clock,
    support_bits,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
LATENCY = 0.005
#: Mode 01 support: group 0x00 claims RPM/SPEED/ENGINE_LOAD/COOLANT and
#: continues; group 0x20 claims FUEL_LEVEL and stops.
MODE01_SUPPORT = {
    0x00: support_bits(0x04, 0x05, 0x0C, 0x0D, base=0x00, more=True),
    0x20: support_bits(0x2F, base=0x20),
}
#: Mode 06: group 0x00 claims MID 0x01 and continues; 0x20 claims 0x21 and stops.
MODE06_SUPPORT = {
    0x00: support_bits(0x01, base=0x00, more=True),
    0x20: support_bits(0x21, base=0x20),
}
MODE06_VALUES = {
    0x01: bytes([0x01, 0x0C, 0x24, 0x00, 0x03, 0x00, 0x00, 0x00, 0xFF]),
    0x21: bytes([0x21, 0x0C, 0x24, 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF]),
}


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    install_clock(monkeypatch, fake)
    return fake


@pytest.fixture
def runtime_reset() -> object:
    yield None
    tuning_profile.set_paused(False)
    tuning_profile.set_active(False, source="test")


def _spec(**overrides: object) -> CanObdSpec:
    base: dict[str, object] = {
        "can": CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        "poll_rate_hz": 4.0,
        "pids": ("RPM", "SPEED"),
        "batch_max_pids": 1,
        "priority_channels": (),
        "dtc_poll_interval_seconds": 0.0,
        "service_poll_interval_seconds": 0.0,
        "request_timeout_seconds": 0.2,
    }
    base.update(overrides)
    return CanObdSpec(**base)  # type: ignore[arg-type]


class Run:
    """One loop run with every tapped frame and reading recorded."""

    def __init__(self) -> None:
        self.tapped: list[object] = []
        self.readings: list[ObdReading] = []
        self.snapshots: list[ObdSchedulerMetrics] = []
        self.stats: CanObdLoopStats | None = None


def _run(
    clock: FakeClock,
    bus: ScriptedEcu,
    spec: CanObdSpec,
    *,
    seconds: float,
    stop: threading.Event | None = None,
    metrics_every: float | None = 0.5,
) -> Run:
    run = Run()
    kwargs: dict[str, object] = {}
    if metrics_every is not None:
        kwargs["on_scheduler_metrics"] = run.snapshots.append
        kwargs["scheduler_metrics_interval_seconds"] = metrics_every
    run.stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=run.tapped.append,
        on_reading=run.readings.append,
        stop_event=stop or threading.Event(),
        deadline_monotonic=clock.monotonic() + seconds,
        **kwargs,  # type: ignore[arg-type]
    )
    return run


def untapped_frames(bus: ScriptedEcu, run: Run) -> list[can.Message]:
    """Frames the bus handed out that never reached ``on_frame``."""
    tapped = Counter(id(msg) for msg in run.tapped)
    return [msg for msg in bus.handed_out if tapped[id(msg)] == 0]


def assert_every_frame_tapped_once(bus: ScriptedEcu, run: Run) -> None:
    handed = Counter(id(msg) for msg in bus.handed_out)
    tapped = Counter(id(msg) for msg in run.tapped)
    missing = [msg for msg in bus.handed_out if tapped[id(msg)] == 0]
    duplicated = [msg for msg in bus.handed_out if tapped[id(msg)] > 1]
    foreign = [msg for msg in run.tapped if handed[id(msg)] == 0]
    assert bus.handed_out, "the fake bus handed out nothing: the scenario never ran"
    assert not missing, f"{len(missing)} of {len(bus.handed_out)} frames consumed untapped"
    assert not duplicated, f"{len(duplicated)} frames tapped more than once"
    assert not foreign, "on_frame saw frames the bus never handed out"


def probes(bus: ScriptedEcu, service: int) -> list[tuple[float, int]]:
    return [(at, group) for at, svc, group in bus.support_probes if svc == service]


# ---------------------------------------------------------------------------
# C06.1 / C06.9 — injected broadcast frames must reach raw capture exactly once
# ---------------------------------------------------------------------------


def test_startup_discovery_does_not_consume_broadcast_frames_untapped(
    clock: FakeClock, runtime_reset: object
) -> None:
    """``auto`` PIDs walk the Mode 01 support groups at startup; frames arriving
    in that window were discarded by the client's own receive loop (C06.1)."""
    bus = ScriptedEcu(
        clock,
        mode01_support=MODE01_SUPPORT,
        broadcast_per_request=3,
    )
    run = _run(clock, bus, _spec(pids=("auto",)), seconds=3.0)

    assert probes(bus, 0x01), "no Mode 01 support probe was ever issued"
    assert [group for _at, group in probes(bus, 0x01)][:2] == [0x00, 0x20]
    lost = untapped_frames(bus, run)
    lost_sequences = sorted(seq for seq in (broadcast_sequence(m) for m in lost) if seq is not None)
    assert not lost, (
        f"{len(lost)} of {len(bus.handed_out)} frames consumed untapped "
        f"(broadcast sequences {lost_sequences})"
    )
    assert_every_frame_tapped_once(bus, run)
    assert {r.pid for r in run.readings} >= {"RPM", "SPEED"}


def test_mode06_retry_after_contact_does_not_consume_broadcast_frames_untapped(
    clock: FakeClock, runtime_reset: object
) -> None:
    """Mode 06 discovery answered nothing at startup, then the retry after ECU
    contact ran synchronously through the untapped client (C06.1)."""
    bus = ScriptedEcu(
        clock,
        mode06_support=MODE06_SUPPORT,
        mode06_support_after_contact=True,
        mode06_values=MODE06_VALUES,
        broadcast_per_request=3,
    )
    spec = _spec(service_poll_interval_seconds=300.0, service_mids="auto")
    run = _run(clock, bus, spec, seconds=4.0)

    mode06 = probes(bus, 0x06)
    assert len(mode06) >= 2, f"Mode 06 support was probed {len(mode06)} times; expected a retry"
    lost = untapped_frames(bus, run)
    assert not lost, f"{len(lost)} of {len(bus.handed_out)} frames consumed untapped"
    assert_every_frame_tapped_once(bus, run)
    assert any(r.pid == "MONITOR_RESULTS" for r in run.readings), "Mode 06 never installed"


# ---------------------------------------------------------------------------
# C06.9 — exactly-once delivery across every discovery outcome
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_reset() -> object:
    yield None
    discovery_registry.reset_discovery()


def _startup(clock: FakeClock, **ecu: object) -> tuple[ScriptedEcu, Run]:
    bus = ScriptedEcu(clock, broadcast_per_request=2, **ecu)  # type: ignore[arg-type]
    spec = _spec(pids=("auto",), service_poll_interval_seconds=300.0, service_mids="auto")
    return bus, _run(clock, bus, spec, seconds=2.0)


def test_silent_support_group_is_tapped_and_not_recorded_as_unsupported(
    clock: FakeClock, runtime_reset: object
) -> None:
    bus, run = _startup(clock, mode01_support=None, mode06_support=None)
    assert_every_frame_tapped_once(bus, run)
    assert run.stats is not None
    assert run.stats.discovery_timeouts >= 2
    walks = run.snapshots[-1].discovery["items"]  # type: ignore[index]
    for item in ("mode01", "mode06"):
        assert walks[item]["complete"] is False, f"{item}: a timeout was persisted as proof"
        assert walks[item]["last_result"] == "timeout"
        assert walks[item]["next_probe_in_s"] is not None
    # Every decoded PID stays in the direct-probe plan (sparse-map fallback),
    # even though the new independent discovery cadence may shift which one
    # answers inside this short two-second window.
    requested = {row["channel"] for row in run.snapshots[-1].requests}
    assert requested >= {"RPM", "SPEED", "COOLANT_TEMP"}


def test_discovery_has_an_independent_request_budget(
    clock: FakeClock, runtime_reset: object
) -> None:
    bus = ScriptedEcu(clock, mode01_support=MODE01_SUPPORT)
    run = _run(
        clock,
        bus,
        _spec(
            pids=("auto",),
            discovery_budget_requests_per_second=2.0,
        ),
        seconds=1.2,
    )

    support_probes = probes(bus, 0x01)
    assert [group for _at, group in support_probes] == [0x00, 0x20]
    assert support_probes[1][0] - support_probes[0][0] >= 0.5
    assert run.stats is not None and run.stats.requests_sent > 2


def test_refused_support_walk_ends_at_the_refusal_and_is_tapped(
    clock: FakeClock, runtime_reset: object
) -> None:
    bus, run = _startup(clock, mode01_support=MODE01_SUPPORT, mode06_nrc=0x11)
    assert_every_frame_tapped_once(bus, run)
    assert run.stats is not None
    assert run.stats.discovery_negative_responses == 1
    assert [g for _at, g in probes(bus, 0x06)] == [0x00], "walk continued past a refusal"
    mode06 = run.snapshots[-1].discovery["items"]["mode06"]  # type: ignore[index]
    assert mode06["complete"] is True and mode06["last_result"] == "nrc"
    assert "serviceNotSupported" in mode06["last_nrc"]
    assert not [r for r in run.readings if r.pid == "MONITOR_RESULTS"]


def test_sparse_support_map_still_probes_the_whole_catalogue(
    clock: FakeClock, runtime_reset: object
) -> None:
    sparse = {0x20: support_bits(0x2F, base=0x20)}
    bus, run = _startup(clock, mode01_support={0x00: support_bits(base=0x00, more=True), **sparse})
    assert_every_frame_tapped_once(bus, run)
    assert [g for _at, g in probes(bus, 0x01)] == [0x00, 0x20]
    rows = {row["channel"]: row for row in run.snapshots[-1].requests}
    assert "FUEL_LEVEL" in rows and "RPM" in rows, "an unclaimed PID left the plan"
    assert {r.pid for r in run.readings} >= {"RPM", "SPEED"}


def test_multiple_responders_are_merged_and_every_frame_tapped(
    clock: FakeClock, runtime_reset: object
) -> None:
    bus, run = _startup(
        clock,
        mode01_support={0x00: support_bits(0x0C, 0x0D, base=0x00, more=True)},
        support_responders={
            0x7E9: {0x00: support_bits(base=0x00, more=True), 0x20: support_bits(0x2F, base=0x20)}
        },
    )
    assert_every_frame_tapped_once(bus, run)
    mode01 = run.snapshots[-1].discovery["items"]["mode01"]  # type: ignore[index]
    assert mode01["responders"] == ["0x7E8", "0x7E9"]
    assert [g for _at, g in probes(bus, 0x01)] == [0x00, 0x20], "continuation from 0x7E9 ignored"
    assert mode01["supported_count"] == 3


def test_reconnect_resumes_a_walk_from_its_state(
    clock: FakeClock, runtime_reset: object, registry_reset: object
) -> None:
    """A second loop on the same vehicle continues at the next unanswered
    group; a completed walk is not re-probed at all."""
    spec = _spec(pids=("auto",), discovery_key="TESTVIN0000000001")
    first = ScriptedEcu(clock, mode01_support=MODE01_SUPPORT, broadcast_per_request=2)
    # Just long enough for group 0x00 to answer, not for 0x20 to be probed.
    run1 = _run(clock, first, spec, seconds=0.02, metrics_every=None)
    assert_every_frame_tapped_once(first, run1)
    assert [g for _at, g in probes(first, 0x01)] == [0x00]

    second = ScriptedEcu(clock, mode01_support=MODE01_SUPPORT, broadcast_per_request=2)
    run2 = _run(clock, second, spec, seconds=2.0)
    assert_every_frame_tapped_once(second, run2)
    assert [g for _at, g in probes(second, 0x01)] == [0x20], "walk restarted from scratch"
    assert run2.snapshots[-1].discovery["items"]["mode01"]["complete"] is True  # type: ignore[index]

    third = ScriptedEcu(clock, mode01_support=MODE01_SUPPORT, broadcast_per_request=2)
    run3 = _run(clock, third, spec, seconds=1.0)
    assert probes(third, 0x01) == [], "a complete walk was re-probed after reconnect"
    assert_every_frame_tapped_once(third, run3)

    discovery_registry.reset_discovery()
    fourth = ScriptedEcu(clock, mode01_support=MODE01_SUPPORT, broadcast_per_request=2)
    _run(clock, fourth, spec, seconds=1.0)
    assert [g for _at, g in probes(fourth, 0x01)] == [0x00, 0x20], "reset did not invalidate"


# ---------------------------------------------------------------------------
# C06.10 — budget, session gates, capacity, prompt stop
# ---------------------------------------------------------------------------


def _all_sends(bus: ScriptedEcu) -> list[float]:
    return [at for at, msg in bus.sent if bytes(msg.data)[1] != 0x30]


def test_discovery_probes_are_charged_to_the_budget(
    clock: FakeClock, runtime_reset: object
) -> None:
    bus = ScriptedEcu(clock, mode01_support=MODE01_SUPPORT, mode06_support=MODE06_SUPPORT)
    spec = _spec(pids=("auto",), service_poll_interval_seconds=300.0, service_mids="auto")
    run = _run(clock, bus, spec, seconds=2.0)
    slot = 1 / 32
    sends = _all_sends(bus)
    gaps = [later - earlier for earlier, later in pairwise(sends)]
    assert min(gaps) >= slot - 1e-6, "two requests inside one budget slot"
    assert run.stats is not None
    assert run.stats.discovery_requests_sent == 4
    assert run.stats.requests_sent >= run.stats.discovery_requests_sent + 2
    # One support response per probe, nothing else on the bus meanwhile.
    assert run.stats.discovery_frames_seen == 4
    # Discovery goes at the head of the line under the default profile, as
    # the synchronous startup walk did, then polling starts.
    assert [g for _at, _svc, g in bus.support_probes[:2]] == [0x00, 0x20]


def test_discovery_stops_promptly_during_a_silent_probe(
    clock: FakeClock, runtime_reset: object
) -> None:
    stop = threading.Event()
    stopped_at: list[float] = []

    class _StoppingEcu(ScriptedEcu):
        def recv(self, timeout: float | None = None):  # type: ignore[no-untyped-def]
            if self.support_probes and not stopped_at:
                stopped_at.append(self.clock.monotonic())
                stop.set()
            return super().recv(timeout)

    bus = _StoppingEcu(clock, mode01_support=None)
    spec = _spec(pids=("auto",), request_timeout_seconds=1.0)
    _run(clock, bus, spec, seconds=10.0, stop=stop, metrics_every=None)
    assert stopped_at
    assert clock.monotonic() - stopped_at[0] <= 0.05 + 1e-6, "stop waited out the probe timeout"


def _hybrid_profile(**overrides: object) -> CaptureProfile:
    fields: dict[str, object] = {
        "name": "hybrid",
        "mode01": {"RPM": 1.0, "SPEED": 1.0},
        "mode22": ("1101", "1208"),
        "did_cycle_seconds": 0.25,
        "target_rate_hz": 4.0,
        "corpus_eligible": False,
        "storage_prefix": "_capture-hybrid",
    }
    fields.update(overrides)
    return CaptureProfile(**fields)  # type: ignore[arg-type]


def _session_spec() -> DidSessionSpec:
    return DidSessionSpec(
        session=0xC0,
        exit_session=0x81,
        exit_grace_seconds=0.05,
        keepalive_seconds=2.0,
        window_interval_seconds=0.25,
        window_max_seconds=1.0,
        lapse_seconds=5.0,
    )


def _held_intervals(bus: ScriptedEcu) -> list[tuple[float, float]]:
    """(entry, exit) spans during which the fake ECU held the session."""
    spans: list[tuple[float, float]] = []
    open_at: float | None = None
    for at, sub in bus.session_changes:
        if sub == 0x81:
            if open_at is not None:
                spans.append((open_at, at))
                open_at = None
        else:
            open_at = at
    if open_at is not None:
        spans.append((open_at, float("inf")))
    return spans


def test_mode06_discovery_waits_for_the_mode01_phase_of_a_hybrid_capture(
    clock: FakeClock, runtime_reset: object
) -> None:
    tuning_profile.activate(_hybrid_profile(), source="test")
    bus = ScriptedEcu(
        clock,
        session_gated=True,
        mode06_support=MODE06_SUPPORT,
        mode06_values=MODE06_VALUES,
        broadcast_per_request=1,
    )
    spec = _spec(
        dids=("1101", "1208"),
        did_session=_session_spec(),
        capture_profiles_enabled=True,
        service_poll_interval_seconds=300.0,
        service_mids="auto",
    )
    run = _run(clock, bus, spec, seconds=3.0)
    assert_every_frame_tapped_once(bus, run)
    mode06 = probes(bus, 0x06)
    assert mode06, "discovery never ran in the Mode 01 phase"
    for at, group in mode06:
        for entry, exit_at in _held_intervals(bus):
            assert not (entry <= at < exit_at + 0.05), (
                f"Mode 06 probe 0x{group:02X} at {at - 1000:.3f}s inside the held session"
            )
    assert run.snapshots[-1].discovery["items"]["mode06"]["complete"] is True  # type: ignore[index]
    assert bus.session_changes, "the hybrid capture never entered its session"


def test_continuous_did_capture_never_runs_mode06_discovery(
    clock: FakeClock, runtime_reset: object
) -> None:
    tuning_profile.activate(
        _hybrid_profile(
            name="all-did",
            mode01={},
            mode01_enabled=False,
            did_cycle_seconds=None,
            did_continuous=True,
            target_rate_hz=15.0,
        ),
        source="test",
    )
    bus = ScriptedEcu(clock, session_gated=True, mode06_support=MODE06_SUPPORT)
    spec = _spec(
        dids=("1101", "1208"),
        did_session=_session_spec(),
        capture_profiles_enabled=True,
        service_poll_interval_seconds=300.0,
        service_mids="auto",
    )
    run = _run(clock, bus, spec, seconds=3.0)
    assert probes(bus, 0x06) == [], "discovery ran inside a continuous DID session"
    assert [r for r in run.readings if r.pid.startswith("DID_")], "the DID capture itself stalled"
    last = run.snapshots[-1]
    assert last.discovery["pending"] == ["mode06"]  # type: ignore[index]
    assert last.discovery["deferred_reason"] == "continuous_did_capture"  # type: ignore[index]
    assert {
        "channel": "discovery:mode06",
        "kind": "discovery",
        "verdict": "deferred",
        "reason": "continuous_did_capture",
    } in last.deferred


def test_discovery_yields_to_a_capture_tuple_and_uses_idle_slots_only(
    clock: FakeClock, runtime_reset: object
) -> None:
    tuning_profile.activate(
        CaptureProfile(
            name="fuel",
            mode01={"RPM": 1.0, "SPEED": 1.0},
            target_rate_hz=4.0,
            required_tuple=("RPM", "SPEED"),
            round_timestamps=True,
            corpus_eligible=False,
            storage_prefix="_capture-fuel",
        ),
        source="test",
    )
    bus = ScriptedEcu(clock, mode01_support=MODE01_SUPPORT, mode06_support=MODE06_SUPPORT)
    spec = _spec(
        pids=("auto",),
        capture_profiles_enabled=True,
        service_poll_interval_seconds=300.0,
        service_mids="auto",
    )
    run = _run(clock, bus, spec, seconds=3.0)
    assert run.stats is not None
    assert run.stats.rounds_incomplete == 0, "a discovery probe cost a round member"
    assert run.stats.rounds_started >= 10
    assert run.snapshots[-1].discovery["pending"] == []  # type: ignore[index]
    # Every probe went out between rounds: never earlier than the first poll.
    first_poll = min(
        at
        for at, msg in bus.sent
        if bytes(msg.data)[1] == 0x01 and bytes(msg.data)[2] not in (0x00, 0x20)
    )
    assert all(at > first_poll for at, _g in probes(bus, 0x01) + probes(bus, 0x06))


def test_discovery_reports_pending_when_the_capture_is_at_capacity(
    clock: FakeClock, runtime_reset: object
) -> None:
    """Two PIDs at 4 Hz on an 8 req/s budget fill every round exactly: no
    idle slot exists, so discovery waits and says so rather than delaying
    the selected telemetry."""
    tuning_profile.activate(
        CaptureProfile(
            name="tight",
            mode01={"RPM": 1.0, "SPEED": 1.0},
            target_rate_hz=4.0,
            required_tuple=("RPM", "SPEED"),
            corpus_eligible=False,
            storage_prefix="_capture-tight",
        ),
        source="test",
    )
    bus = ScriptedEcu(clock, mode06_support=MODE06_SUPPORT)
    spec = _spec(
        poll_rate_hz=1.0,
        capture_profiles_enabled=True,
        service_poll_interval_seconds=300.0,
        service_mids="auto",
    )
    run = _run(clock, bus, spec, seconds=3.0)
    assert probes(bus, 0x06) == [], "discovery took a slot the tuple needed"
    last = run.snapshots[-1]
    assert last.discovery["deferred_reason"] == "capture_at_capacity"  # type: ignore[index]
    assert any(row["channel"] == "discovery:mode06" for row in last.deferred)
    assert {r.pid for r in run.readings} == {"RPM", "SPEED"}


def test_focused_capture_serves_a_dtc_triggered_freeze_frame(
    clock: FakeClock, runtime_reset: object
) -> None:
    tuning_profile.activate(
        CaptureProfile(
            name="focused",
            mode01={"RPM": 1.0},
            target_rate_hz=4.0,
            corpus_eligible=False,
            storage_prefix="_capture-focused",
        ),
        source="test",
    )
    trigger = FreezeFrameTrigger()
    trigger.request()
    bus = ScriptedEcu(clock, answer_freeze_frames=True)
    spec = _spec(capture_profiles_enabled=True, freeze_frame_trigger=trigger)
    run = _run(clock, bus, spec, seconds=2.0)
    frames = [r for r in run.readings if r.pid.startswith("FREEZE_FRAME_")]
    assert {r.pid for r in frames} >= {"FREEZE_FRAME_RPM", "FREEZE_FRAME_SPEED"}
    assert len(frames) == len({r.pid for r in frames}), "a one-shot fired twice"
    assert all(r.responder_id == 0x7E8 for r in frames)
    # The unselected telemetry channel is reported, not silently dropped.
    assert {
        "channel": "SPEED",
        "kind": "pid",
        "verdict": "ineligible",
        "reason": "not_selected",
    } in run.snapshots[-1].deferred
    assert not [r for r in run.readings if r.pid == "SPEED"]


# ---------------------------------------------------------------------------
# C06.7 — the matrix itself
# ---------------------------------------------------------------------------


def test_matrix_covers_every_kind_class_and_phase() -> None:
    rows = matrix_rows()
    assert len(rows) == len(REQUEST_KINDS) * len(PROFILE_CLASSES) * len(SESSION_PHASES)
    assert {row["verdict"] for row in rows} == {"eligible", "deferred", "ineligible"}


@pytest.mark.parametrize(
    ("kind", "profile_class", "phase", "kwargs", "verdict", "reason"),
    [
        ("pid", "default", "none", {}, "eligible", "ok"),
        (
            "pid",
            "focused_mode01",
            "mode01",
            {"selection": "not_selected"},
            "ineligible",
            "not_selected",
        ),
        (
            "pid",
            "focused_mode01",
            "mode01",
            {"selection": "not_selected", "forced": True},
            "eligible",
            "ok",
        ),
        ("pid", "mode22_owning", "window", {}, "deferred", "session_window"),
        ("pid", "continuous_did", "window", {}, "ineligible", "continuous_did_capture"),
        ("did", "focused_mode01", "window", {}, "ineligible", "profile_without_mode22"),
        ("did", "mode22_owning", "mode01", {"forced": True}, "deferred", "awaiting_window"),
        (
            "did",
            "mode22_owning",
            "none",
            {"selection": "not_selected"},
            "ineligible",
            "not_selected",
        ),
        ("dtc", "focused_mode01", "mode01", {"selection": "not_selected"}, "eligible", "ok"),
        ("service", "default", "mode01", {"selection": "not_selected"}, "eligible", "ok"),
        (
            "service",
            "focused_mode01",
            "mode01",
            {"selection": "not_selected"},
            "ineligible",
            "not_selected",
        ),
        ("service", "focused_mode01", "lapse", {}, "deferred", "session_lapse"),
        (
            "freeze_frame",
            "focused_mode01",
            "mode01",
            {"selection": "not_selected"},
            "eligible",
            "ok",
        ),
        ("freeze_frame", "continuous_did", "mode01", {}, "ineligible", "continuous_did_capture"),
        ("discovery", "default", "window", {}, "deferred", "session_window"),
        ("discovery", "continuous_did", "window", {}, "ineligible", "continuous_did_capture"),
        ("custom", "default", "mode01", {"paused": True}, "deferred", "paused"),
        ("custom", "default", "mode01", {"passive": True}, "deferred", "passive"),
    ],
)
def test_matrix_rows(kind, profile_class, phase, kwargs, verdict, reason) -> None:  # type: ignore[no-untyped-def]
    outcome = evaluate(kind, profile_class, phase, **kwargs)
    assert (outcome.verdict, outcome.reason) == (verdict, reason)


def test_both_gates_are_projections_of_the_matrix(runtime_reset: object) -> None:
    tuning_profile.activate(_hybrid_profile(), source="test")
    ctrl = DidWindowController(_session_spec(), [2, 3], now=0.0)
    requests = [
        _PollRequest(kind="pid", service=0x01, identifier=0x0C, request_id=0x7DF, channel="RPM"),
        _PollRequest(
            kind="service",
            service=0x06,
            identifier=0x01,
            request_id=0x7DF,
            channel="MONITOR_RESULTS",
        ),
        _PollRequest(
            kind="did", service=0x22, identifier=0x1101, request_id=0x7E0, channel="DID_1101"
        ),
        _PollRequest(
            kind="did", service=0x22, identifier=0x9999, request_id=0x7E0, channel="DID_9999"
        ),
    ]
    # Profile gate: selection, no phase.
    assert tuning_profile.request_verdict(requests[0]).eligible
    assert tuning_profile.request_verdict(requests[1]).reason == "not_selected"
    assert tuning_profile.request_verdict(requests[3]).reason == "not_selected"
    # Session gate: phase, no selection.
    assert ctrl.request_verdict(0, requests[0]).eligible
    assert ctrl.request_verdict(2, requests[2]).reason == "awaiting_window"
    ctrl.on_session_response(
        result=SessionControlResult(ok=True),
        states=[_PollRequestState() for _ in requests],
        now=1.0,
    )
    assert ctrl.request_verdict(2, requests[2]).eligible
    assert ctrl.request_verdict(0, requests[0]).reason == "session_window"
    ctrl.on_request_attempted(2)
    assert ctrl.request_verdict(2, requests[2]).reason == "attempted_this_window"


# ---------------------------------------------------------------------------
# C06.11 — provenance and the control surface
# ---------------------------------------------------------------------------


def test_readings_carry_responder_provenance(clock: FakeClock, runtime_reset: object) -> None:
    bus = ScriptedEcu(clock, mode06_support=MODE06_SUPPORT, mode06_values=MODE06_VALUES)
    spec = _spec(service_poll_interval_seconds=300.0, service_mids="auto")
    run = _run(clock, bus, spec, seconds=2.0)
    monitors = [r for r in run.readings if r.pid == "MONITOR_RESULTS"]
    assert monitors and all(r.responder_id == 0x7E8 for r in run.readings)
    assert {bytes.fromhex(r.value)[0] for r in monitors} == {0x01, 0x21}, (
        "MID lost from the reading"
    )
    record = reading_to_record(monitors[0])
    assert record.metadata is not None and record.metadata["responder_id"] == 0x7E8
    sent = [bytes(msg.data)[:3] for _at, msg in bus.sent]
    assert bytes([0x02, 0x06, 0x01]) in sent and bytes([0x02, 0x06, 0x21]) in sent


def test_reset_discovery_control_route(tmp_path: Path, registry_reset: object) -> None:
    config = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    client = TestClient(app)
    headers = {"Authorization": "Bearer secret"}
    assert client.post("/control/obd_discovery/reset").status_code == 401
    before = client.get("/control/obd_discovery", headers=headers).json()["obd_discovery"]
    resp = client.post("/control/obd_discovery/reset", headers=headers)
    assert resp.status_code == 200
    body = resp.json()["obd_discovery"]
    assert body["reset"] is True and body["cache_version"] == before["cache_version"] + 1
    assert discovery_registry.cache_version() == body["cache_version"]


def test_a_busy_nrc_parked_walk_is_rearmed_by_contact_like_a_timeout() -> None:
    """The doc promises both parks re-arm on contact; the code once did only timeouts."""
    from src.collection.discovery import DISCOVERY_RETRY_SECONDS, SupportProbe, SupportWalk

    walk = SupportWalk.start(service=0x01, groups=(0x00, 0x20))
    parked = walk.with_probe(SupportProbe(negative_response_code=0x21), now=1.0)
    assert not parked.complete and parked.last_result == "nrc"
    assert parked.next_probe_at == 1.0 + DISCOVERY_RETRY_SECONDS
    assert parked.on_contact(2.0).next_probe_at == 2.0
    ended = walk.with_probe(SupportProbe(negative_response_code=0x11), now=1.0)
    assert ended.complete and ended.on_contact(2.0) is ended


# ---------------------------------------------------------------------------
# R02.1 — an slcan parse error inside a discovery probe is a dropped frame,
# not a config error that ends the collector
# ---------------------------------------------------------------------------


class _GarbledProbeEcu(ScriptedEcu):
    """The driver's slcan parser raises once, on the first receive of a probe."""

    def __init__(self, clock: FakeClock, **kwargs: object) -> None:
        super().__init__(clock, **kwargs)  # type: ignore[arg-type]
        self._armed = False
        self.garbled = 0

    def send(self, msg: object, timeout: float | None = None) -> None:
        super().send(msg, timeout)  # type: ignore[arg-type]
        data = bytes(msg.data)  # type: ignore[attr-defined]
        if not self.garbled and len(data) >= 3 and data[1] == 0x01 and data[2] in SUPPORT_GROUPS:
            self._armed = True

    def recv(self, timeout: float | None = None) -> can.Message | None:
        if self._armed:
            self._armed = False
            self.garbled += 1
            raise ValueError("could not parse slcan frame 't7E8'")
        return super().recv(timeout)


def test_parse_error_during_a_discovery_probe_leaves_the_loop_running(
    clock: FakeClock, runtime_reset: object, registry_reset: object
) -> None:
    """R02.1: the error travelled probe_support -> _issue_discovery_probe ->
    run_with_reconnect(reraise=(ValueError,)), which treats it as a config
    error. It is one malformed frame: counted, logged, and the loop goes on."""
    bus = _GarbledProbeEcu(clock, mode01_support=MODE01_SUPPORT)
    run = _run(clock, bus, _spec(pids=("auto",)), seconds=2.0)

    assert bus.garbled == 1
    assert run.stats is not None
    assert run.stats.parse_errors == 1
    assert probes(bus, 0x01), "no support probe was issued"
    assert {r.pid for r in run.readings} >= {"RPM", "SPEED"}
