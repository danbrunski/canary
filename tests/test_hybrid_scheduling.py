"""Hybrid Mode 01 / Mode 22 scheduling against real deadlines (roadmap task C04).

Everything runs on the fake clock and the session-gated :class:`ScriptedEcu`
from :mod:`tests.capture_round_fakes`, so every wake instant, session request
and diagnostic request is recorded in order and asserted exactly.

The headline counterexample (C04.1): a successful ``10 81`` fast exit with a
50 ms grace was serviced only after the loop's flat 200 ms idle drain, and a
budget-blocked exit waited the same 200 ms. With a 0.25 s ``window_interval``
that made a **0.948 s** cycle (1.05 Hz) of what the profile called 4 Hz.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from statistics import median

import pytest

from src.collection import can_obd_loop, tuning_profile, tx_gate
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_profiles import CaptureProfile
from src.collection.hybrid_cycle import WakeDecision, next_wake_at, plan_cycle
from src.collection.obd_collector import ObdReading
from src.collection.obd_scheduler import (
    FAST_EXIT_RETRY_MIN_SECONDS,
    CanObdLoopStats,
    CanObdSpec,
    DidSessionSpec,
    ObdSchedulerMetrics,
    did_access,
    resolve_did_session_spec,
)
from src.config import CanBusConfig, CollectionConfig
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock

SESSION = 0xC0
EXIT = 0x81
CYCLE = 0.25
LATENCY = 0.005
BUDGET_SLOT = 1 / 32  # poll_rate_hz 4.0 -> 32 req/s
EPS = 1e-6


@dataclass(frozen=True)
class Sent:
    """One request on the wire, classified by service."""

    at: float
    kind: str  # "entry" | "exit" | "mode01" | "did" | "other"
    identifier: int


def request_log(bus: ScriptedEcu) -> list[Sent]:
    out: list[Sent] = []
    for at, msg in bus.sent:
        data = bytes(msg.data)
        if len(data) < 2:
            continue
        service = data[1]
        if service == 0x10:
            sub = data[2]
            out.append(Sent(at, "exit" if sub == EXIT else "entry", sub))
        elif service == 0x01:
            out.append(Sent(at, "mode01", data[2]))
        elif service == 0x22:
            out.append(Sent(at, "did", int.from_bytes(data[2:4], "big")))
        elif service != 0x30:
            out.append(Sent(at, "other", service))
    return out


def exit_to_mode01_gaps(log: list[Sent]) -> list[float]:
    """Seconds from each exit request to the next Mode 01 request."""
    gaps: list[float] = []
    for index, sent in enumerate(log):
        if sent.kind != "exit":
            continue
        after = next((s for s in log[index + 1 :] if s.kind == "mode01"), None)
        if after is not None:
            gaps.append(after.at - sent.at)
    return gaps


def entries(log: list[Sent]) -> list[float]:
    return [s.at for s in log if s.kind == "entry"]


def kinds_between(log: list[Sent], start: float, end: float) -> list[str]:
    return [s.kind for s in log if start <= s.at < end]


def _session(**overrides: object) -> DidSessionSpec:
    base: dict[str, object] = {
        "session": SESSION,
        "exit_session": EXIT,
        "exit_grace_seconds": 0.05,
        "keepalive_seconds": 2.0,
        "window_interval_seconds": CYCLE,
        "window_max_seconds": 1.0,
        "lapse_seconds": 5.0,
    }
    base.update(overrides)
    return DidSessionSpec(**base)  # type: ignore[arg-type]


def _spec(**overrides: object) -> CanObdSpec:
    base: dict[str, object] = {
        "can": CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        "poll_rate_hz": 4.0,
        "pids": ("RPM", "SPEED"),
        "dids": ("1101", "1208"),
        "batch_max_pids": 1,
        "priority_channels": (),
        "dtc_poll_interval_seconds": 0.0,
        "service_poll_interval_seconds": 0.0,
        "request_timeout_seconds": 0.2,
        "did_session": _session(),
    }
    base.update(overrides)
    return CanObdSpec(**base)  # type: ignore[arg-type]


def _profile(name: str, **overrides: object) -> CaptureProfile:
    fields: dict[str, object] = {
        "name": name,
        "mode01": {"RPM": 1.0, "SPEED": 1.0},
        "mode22": ("1101", "1208"),
        "did_cycle_seconds": CYCLE,
        "round_timestamps": True,
        "target_rate_hz": 4.0,
        "corpus_eligible": False,
        "storage_prefix": f"_capture-{name}",
    }
    fields.update(overrides)
    return CaptureProfile(**fields)  # type: ignore[arg-type]


def _run(
    clock: FakeClock,
    bus: ScriptedEcu,
    spec: CanObdSpec,
    *,
    seconds: float,
    metrics_every: float | None = None,
    stop: threading.Event | None = None,
) -> tuple[list[ObdReading], CanObdLoopStats, list[ObdSchedulerMetrics]]:
    readings: list[ObdReading] = []
    snapshots: list[ObdSchedulerMetrics] = []
    kwargs: dict[str, object] = {}
    if metrics_every is not None:
        kwargs["on_scheduler_metrics"] = snapshots.append
        kwargs["scheduler_metrics_interval_seconds"] = metrics_every
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=stop or threading.Event(),
        deadline_monotonic=clock.monotonic() + seconds,
        **kwargs,  # type: ignore[arg-type]
    )
    return readings, stats, snapshots


def _run_watched(
    clock: FakeClock, bus: ScriptedEcu, spec: CanObdSpec, *, seconds: float
) -> tuple[list[ObdReading], CanObdLoopStats, list[ObdSchedulerMetrics]]:
    """C02's spin watchdog: the loop must run its fake deadline out on a thread."""
    deadline = clock.monotonic() + seconds
    stop = threading.Event()
    result: dict[str, object] = {}

    def run() -> None:
        result["out"] = _run(clock, bus, spec, seconds=seconds, metrics_every=0.5, stop=stop)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=20.0)
    spun = worker.is_alive()
    stop.set()
    worker.join(timeout=5.0)
    assert not spun, "loop never advanced the fake clock: it spun without receiving"
    assert clock.monotonic() >= deadline
    return result["out"]  # type: ignore[return-value]


def _record_wakes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[float, WakeDecision]]:
    wakes: list[tuple[float, WakeDecision]] = []

    def wrapped(now: float, **kwargs: object) -> WakeDecision:
        decision = next_wake_at(now, **kwargs)  # type: ignore[arg-type]
        wakes.append((now, decision))
        return decision

    monkeypatch.setattr(can_obd_loop, "next_wake_at", wrapped)
    return wakes


def _record_drains(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    untils: list[float] = []
    real = can_obd_loop._drain_frames_until

    def wrapped(
        bus: object, on_frame: object, stats: object, stop: object, until: float, deadline: object
    ) -> None:  # noqa: E501
        untils.append(until)
        real(bus, on_frame, stats, stop, until, deadline)  # type: ignore[arg-type]

    monkeypatch.setattr(can_obd_loop, "_drain_frames_until", wrapped)
    return untils


def assert_recv_continuous(bus: ScriptedEcu) -> None:
    """Raw CAN was received through every wait: no recv gap over one 50 ms slice."""
    gaps = [later - earlier for earlier, later in pairwise(bus.recv_calls)]
    assert gaps, "recv was never called"
    assert max(gaps) <= 0.05 + EPS, f"recv gap of {max(gaps):.3f}s: a wait skipped the bus"


#: An entry may trail its boundary by the request in flight at that instant
#: plus one budget slot — the loop does not pre-empt — but never lead it.
GRID_SLACK = BUDGET_SLOT + LATENCY + 0.002


def assert_on_cycle_grid(starts: list[float], cycle: float, *, latency: float = LATENCY) -> None:
    slack = GRID_SLACK - LATENCY + latency
    anchor = starts[0]
    for start in starts:
        offset = (start - anchor) % cycle
        late = min(offset, cycle - offset)
        assert offset <= slack or cycle - offset < EPS, (
            f"entry at {start - anchor:.4f}s is {late:.4f}s off the cycle grid"
        )
    for earlier, later in pairwise(starts):
        assert later - earlier >= cycle - slack, "two windows opened inside one cycle"


def last_cycle(snapshots: list[ObdSchedulerMetrics]) -> dict[str, object]:
    profile = snapshots[-1].did_profile
    assert profile is not None
    return profile["cycle"]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    install_clock(monkeypatch, fake)
    return fake


@pytest.fixture
def runtime_reset() -> object:
    yield None
    tuning_profile.set_paused(False)
    tx_gate.set_passive(False, source="test")
    tuning_profile.set_active(False, source="test")


# ---------------------------------------------------------------------------
# C04.1 — the 50 ms grace serviced only after a 200 ms drain
# ---------------------------------------------------------------------------


def test_fast_exit_grace_is_serviced_at_the_grace_deadline(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before (measured on the pre-C04 loop, this exact scenario): every
    accepted ``10 81`` was followed by a Mode 01 request 0.206 s later, and
    the exit itself trailed the last DID by another 0.206 s — the zero-
    eligible branch drained for a flat 200 ms while a 50 ms grace (or a
    31 ms budget slot) had long expired. After: Mode 01 follows the exit by
    grace + reply latency, and the wake that got it there names the
    controller transition as its deadline."""
    wakes = _record_wakes(monkeypatch)
    bus = ScriptedEcu(clock, session_gated=True)
    _readings, stats, _ = _run(clock, bus, _spec(), seconds=4.0)

    log = request_log(bus)
    gaps = exit_to_mode01_gaps(log)[1:]  # [0] is the connect-time exit, no grace
    assert len(gaps) >= 5, log
    assert stats.session_controls_sent >= 10
    assert max(gaps) == pytest.approx(0.05 + LATENCY, abs=1e-3), gaps

    # Last DID -> exit is one budget slot, not a 200 ms drain.
    did_to_exit = [
        s.at - prev.at for prev, s in pairwise(log) if prev.kind == "did" and s.kind == "exit"
    ]
    assert did_to_exit and max(did_to_exit) == pytest.approx(BUDGET_SLOT, abs=1e-3)

    exits = [s.at for s in log if s.kind == "exit"][1:]
    for at in exits:
        after = [(now, w) for now, w in wakes if now >= at + LATENCY - EPS]
        assert after, "no wait was scheduled after the exit"
        now, wake = after[0]
        assert wake.reason == "transition"
        assert wake.at == pytest.approx(at + LATENCY + 0.05, abs=1e-6)


# ---------------------------------------------------------------------------
# C04.2 — one next-wake calculation, used by every wait in the loop
# ---------------------------------------------------------------------------


def test_next_wake_at_picks_the_earliest_future_deadline() -> None:
    decision = next_wake_at(
        10.0, budget_at=10.3, next_due=10.2, transition_at=10.05, keepalive_at=11.0, deadline=12.0
    )
    assert decision == WakeDecision(at=10.05, reason="transition")
    assert next_wake_at(10.0, budget_at=10.01, next_due=10.02).reason == "budget"
    assert next_wake_at(10.0, keepalive_at=10.01, transition_at=10.02).reason == "keepalive"
    assert next_wake_at(10.0, deadline=10.001, next_due=10.1).reason == "deadline"


def test_next_wake_at_drops_the_past_and_never_sleeps_blind() -> None:
    """A past candidate means "act on the next tick", not "spin"; nothing
    scheduled means the bounded idle tick, so raw CAN is always received."""
    assert next_wake_at(10.0, transition_at=9.9, budget_at=10.1).reason == "budget"
    idle = next_wake_at(10.0, transition_at=9.0, next_due=float("inf"))
    assert idle.reason == "idle" and idle.at == pytest.approx(10.2)
    assert next_wake_at(10.0, next_due=15.0).reason == "idle"
    assert next_wake_at(10.0, max_idle_seconds=0.0).at > 10.0


def test_every_wait_in_the_loop_goes_through_next_wake_at(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ``_drain_frames_until`` target is an instant ``next_wake_at`` chose."""
    wakes = _record_wakes(monkeypatch)
    untils = _record_drains(monkeypatch)
    bus = ScriptedEcu(clock, session_gated=True, silent_pids=(0x0D,))
    _run(clock, bus, _spec(did_session=_session(cycle_seconds=CYCLE)), seconds=3.0)
    chosen = {round(w.at, 9) for _now, w in wakes}
    assert untils, "the loop never waited"
    stray = [u for u in untils if round(u, 9) not in chosen]
    assert not stray, f"waits not routed through next_wake_at: {stray[:5]}"
    assert {w.reason for _n, w in wakes} >= {"transition", "budget"}


# ---------------------------------------------------------------------------
# C04.3 — cycle schedule vs the preserved end-to-start interval
# ---------------------------------------------------------------------------


def test_cycle_seconds_schedules_windows_start_to_start(clock: FakeClock) -> None:
    """Measured on the fixed loop: entries at anchor + k * 0.25 exactly, so a
    0.25 s cycle *is* 4 Hz. The pre-C04 loop ran the same profile at 0.948 s."""
    bus = ScriptedEcu(clock, session_gated=True)
    _readings, _stats, snaps = _run(
        clock, bus, _spec(did_session=_session(cycle_seconds=CYCLE)), seconds=4.0, metrics_every=1.0
    )
    starts = entries(request_log(bus))
    assert len(starts) >= 14
    assert_on_cycle_grid(starts, CYCLE)
    assert all(
        later - earlier == pytest.approx(CYCLE, abs=GRID_SLACK)
        for earlier, later in pairwise(starts)
    )
    cycle = last_cycle(snaps)
    assert cycle["timing_policy"] == "cycle"
    assert cycle["cycle_seconds"] == CYCLE
    assert cycle["cycle_duration_s"]["median"] == pytest.approx(CYCLE, abs=1e-4)  # type: ignore[index]
    assert cycle["cycles_missed"] == 0
    assert_recv_continuous(bus)


def test_window_interval_keeps_its_end_to_start_meaning(clock: FakeClock) -> None:
    """No cycle configured: the next window opens ``interval`` after the
    previous one *closed*, sweep and exit costs on top — the historic policy,
    untouched for saved profiles and config defaults. The interval is still
    not a cycle period, and the snapshot says which policy is in force."""
    bus = ScriptedEcu(clock, session_gated=True)
    _readings, _stats, snaps = _run(clock, bus, _spec(), seconds=4.0, metrics_every=1.0)
    starts = entries(request_log(bus))
    durations = [later - earlier for earlier, later in pairwise(starts)]
    assert durations
    # interval counted from the window *close* (after the last DID answered),
    # so the sweep sits on top of it: measured 0.291 s for a 0.25 s interval.
    assert min(durations) > CYCLE + BUDGET_SLOT
    assert max(durations) == pytest.approx(0.2912, abs=2e-3)
    cycle = last_cycle(snaps)
    assert cycle["timing_policy"] == "interval"
    assert cycle["cycle_seconds"] is None
    assert cycle["cycle_feasible"] is None


def test_profile_cycle_overrides_the_config_interval(
    clock: FakeClock, runtime_reset: object
) -> None:
    tuning_profile.activate(_profile("half-hz", did_cycle_seconds=0.5), source="test")
    bus = ScriptedEcu(clock, session_gated=True)
    _run(clock, bus, _spec(capture_profiles_enabled=True), seconds=4.0)
    starts = entries(request_log(bus))
    assert len(starts) >= 6
    assert_on_cycle_grid(starts, 0.5)
    assert all(
        later - earlier == pytest.approx(0.5, abs=GRID_SLACK) for earlier, later in pairwise(starts)
    )


def test_did_cycle_seconds_is_validated() -> None:
    with pytest.raises(ValueError, match="positive"):
        _profile("bad", did_cycle_seconds=0.0)
    with pytest.raises(ValueError, match="did_continuous"):
        _profile(
            "cont",
            mode01={},
            mode01_enabled=False,
            did_continuous=True,
            did_cycle_seconds=0.25,
        )
    assert _profile("legacy", did_cycle_seconds=None).did_cycle_seconds is None


def test_session_spec_resolves_cycle_and_grace_from_config_and_vehicle_map() -> None:
    """Session id, exit, grace and cycle all come from config + dids.yaml (C04.6)."""
    collection = CollectionConfig(
        obd_dids=("1101",),
        obd_poll_rate_hz=1.0,
        obd_did_cycle_seconds=0.25,
        storage_path=Path("/tmp/canary-test"),
        session_retention_days=1,
        storage_warning_mb=100,
    )
    spec = resolve_did_session_spec(collection)
    access = did_access()
    assert spec is not None and access is not None
    assert spec.cycle_seconds == 0.25
    assert spec.exit_session == access.exit_session == EXIT
    assert access.exit_grace_seconds == 0.05
    assert spec.exit_grace_seconds == access.exit_grace_seconds


# ---------------------------------------------------------------------------
# C04.4 — per-cycle allocation and infeasibility
# ---------------------------------------------------------------------------


def test_plan_cycle_accounts_every_request_and_the_grace() -> None:
    plan = plan_cycle(
        cycle_seconds=0.25,
        budget_rps=90.0,
        mode01_channels=8,
        batch_max_pids=6,
        did_requests=9,
        fast_exit=True,
        exit_grace_seconds=0.05,
        lapse_seconds=5.0,
        request_timeout_seconds=1.0,
    )
    assert plan.mode01_requests == 2 and plan.session_requests == 2
    assert plan.required_seconds == pytest.approx(13 / 90 + 0.05)
    assert plan.feasible and plan.shortfall_seconds == 0.0
    tight = plan_cycle(
        cycle_seconds=0.1,
        budget_rps=90.0,
        mode01_channels=8,
        batch_max_pids=1,
        did_requests=9,
        fast_exit=False,
        exit_grace_seconds=0.05,
        lapse_seconds=5.0,
        request_timeout_seconds=1.0,
    )
    assert tight.session_requests == 1 and tight.grace_seconds == 5.0
    assert not tight.feasible
    assert tight.shortfall_seconds == pytest.approx(18 / 90 + 5.0 - 0.1)


def test_infeasible_cycle_is_reported_once_and_still_progresses(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """2 PIDs + 2 DIDs + entry + exit at 32 req/s plus 50 ms grace need
    0.2375 s; a 0.1 s cycle cannot hold it. The target is not promised."""
    caplog.set_level(logging.WARNING, logger="src.collection.obd_scheduler")
    bus = ScriptedEcu(clock, session_gated=True)
    _readings, _stats, snaps = _run(
        clock, bus, _spec(did_session=_session(cycle_seconds=0.1)), seconds=4.0, metrics_every=1.0
    )
    cycle = last_cycle(snaps)
    assert cycle["cycle_feasible"] is False
    plan = cycle["plan"]
    assert plan["shortfall_seconds"] == pytest.approx(0.1375, abs=1e-4)  # type: ignore[index]
    assert cycle["cycles_missed"] > 0
    warnings = [r for r in caplog.records if "infeasible" in r.getMessage()]
    assert len(warnings) == 1
    log = request_log(bus)
    kinds = [s.kind for s in log]
    assert kinds.count("mode01") >= 10 and kinds.count("did") >= 10
    assert_on_cycle_grid(entries(log), 0.1)


# ---------------------------------------------------------------------------
# C04.5 / C04.9 — overrun, shortfall accounting, sustained overload
# ---------------------------------------------------------------------------


def test_sustained_overload_skips_cycles_without_catch_up_or_starving_mode01(
    clock: FakeClock,
) -> None:
    """A 100 ms ECU makes every request cost four budget slots. Missed
    boundaries are counted and skipped, windows stay on the grid, and each
    cycle still serves Mode 01 before the next window opens."""
    bus = ScriptedEcu(clock, session_gated=True, latency=0.1)
    _readings, _stats, snaps = _run_watched(
        clock, bus, _spec(did_session=_session(cycle_seconds=CYCLE)), seconds=8.0
    )
    log = request_log(bus)
    starts = entries(log)
    assert len(starts) >= 8
    assert_on_cycle_grid(starts, CYCLE, latency=0.1)
    for earlier, later in pairwise(starts):
        between = kinds_between(log, earlier, later)
        assert "mode01" in between, "a window reopened before Mode 01 ran"
        assert "did" in between
    cycle = last_cycle(snaps)
    assert cycle["cycles_missed"] > 0
    assert cycle["cycles_started"] == len(starts)
    # Measured: 0.7-0.8 s per cycle (two boundaries missed each), yet both
    # Mode 01 channels are served every cycle — the overrun is reported as
    # missed cycles, not paid for by Mode 01.
    assert cycle["mode01_shortfall"] == 0
    assert cycle["cycle_feasible"] is True  # nominal plan fits; latency did not
    assert cycle["cycle_duration_s"]["median"] == pytest.approx(0.75, abs=0.06)  # type: ignore[index]
    assert cycle["cycles_missed"] >= 2 * (len(starts) - 1) - 2
    for second in range(2, 8, 2):
        window = kinds_between(log, 1000.0 + second, 1000.0 + second + 2)
        assert "mode01" in window and "did" in window, f"a service stalled at {second}s"
    assert_recv_continuous(bus)


def test_did_shortfall_counts_dids_the_window_could_not_serve(clock: FakeClock) -> None:
    bus = ScriptedEcu(clock, session_gated=True)
    session = _session(cycle_seconds=CYCLE, window_max_seconds=0.02)
    _readings, _stats, snaps = _run(
        clock, bus, _spec(did_session=session), seconds=3.0, metrics_every=1.0
    )
    cycle = last_cycle(snaps)
    assert cycle["did_shortfall"] > 0
    assert cycle["cycles_started"] > 0
    log = request_log(bus)
    for earlier, later in pairwise(entries(log)):
        assert "did" in kinds_between(log, earlier, later)


# ---------------------------------------------------------------------------
# C04.6 / C04.9 / C04.10 — recovery is observed; refusals; no probing
# ---------------------------------------------------------------------------


def test_lapse_is_not_recovery_until_mode01_actually_answers(clock: FakeClock) -> None:
    """No fast exit: the window lapses, the ECU's blackout outlives the
    configured lapse (the 2026-08-04 measurement), and nothing may claim
    Mode 01 is back until a Mode 01 response is seen. During the lapse no
    Mode 01 request goes out to probe."""
    bus = ScriptedEcu(clock, session_gated=True, s3_seconds=0.1, lapse_blackout_seconds=1.0)
    # One window only (the next boundary is 100 s out), so the recovery is
    # observed against a single lapse rather than a fresh blackout per window.
    session = _session(exit_session=None, lapse_seconds=0.3, cycle_seconds=100.0)
    _readings, _stats, snaps = _run(
        clock, bus, _spec(did_session=session), seconds=35.0, metrics_every=0.05
    )
    log = request_log(bus)
    assert not [s for s in log if s.kind == "exit"]
    assert len(entries(log)) == 1
    closed = max(s.at for s in log if s.kind == "did") + LATENCY
    assert kinds_between(log, closed, closed + 0.3 - EPS).count("mode01") == 0
    after_lapse = [s for s in log if s.kind == "mode01" and s.at >= closed + 0.3]
    assert after_lapse, "Mode 01 never resumed after the lapse"
    first_probe = after_lapse[0].at
    # Between the lapse ending and the ECU actually answering (30 s of miss
    # backoff later), every snapshot says recovery is pending, not done.
    during = [
        s.did_profile["cycle"]  # type: ignore[index]
        for s in snaps
        if s.did_profile is not None and s.did_profile["windows_completed"] >= 1
    ]
    pending = [c for c in during if c["mode01_recovery_pending"]]
    assert pending and all(c["mode01_recovered_age_s"] is None for c in pending)
    final = last_cycle(snaps)
    assert final["mode01_recovery_pending"] is False
    assert final["mode01_recovered_age_s"] is not None
    recovered = [s for s in after_lapse if s.at >= first_probe + 30.0]
    assert recovered, "the backed-off channels never re-probed"


TRANSIENT_EXIT_FAILURES = pytest.mark.parametrize(
    ("reply", "counter", "reply_cost"),
    [(0x21, "negative_responses", LATENCY), (None, "timeouts", 0.2), ("lost", "timeouts", 0.2)],
    ids=["busy", "silent", "exited-reply-lost"],
)


@TRANSIENT_EXIT_FAILURES
def test_transient_exit_failure_is_retried_once_instead_of_the_lapse(
    clock: FakeClock, reply: object, counter: str, reply_cost: float
) -> None:
    """D03.18: one failed ``10 81`` used to cost the whole lapse — 5.0 s of
    Mode-01 blackout on the truck (three all-channel gaps of 5.003–5.011 s on
    2026-09-18). It is re-sent at the next budget slot instead, and the lapse
    shrinks to the grace when the retry is answered."""
    bus = ScriptedEcu(
        clock, session_gated=True, s3_seconds=5.0, session_script={EXIT: ["ok", reply]}
    )
    session = _session(cycle_seconds=CYCLE, lapse_seconds=5.0)
    _readings, _stats, snaps = _run(
        clock, bus, _spec(did_session=session), seconds=3.0, metrics_every=0.5
    )
    log = request_log(bus)
    exits = [s.at for s in log if s.kind == "exit"]
    assert len(exits) >= 4
    failed, retry = exits[1], exits[2]  # [0] connect-time exit, [1] first window's exit
    assert kinds_between(log, failed + EPS, retry) == []
    assert retry - failed <= reply_cost + BUDGET_SLOT + EPS
    # Mode 01 is back one grace after the answered retry, not after 5 s.
    resumed = next(s.at for s in log if s.kind == "mode01" and s.at > retry)
    assert resumed - retry <= LATENCY + 0.05 + BUDGET_SLOT + EPS
    profile = snaps[-1].did_profile
    assert profile is not None
    assert profile["fast_exit_supported"] is True
    assert profile["fast_exit_failures"] == 1
    assert profile["fast_exit_retries"] == 1
    assert snaps[-1].to_dict()["session_control"][counter] == 1
    assert len([s for s in log if s.kind == "mode01" and s.at > resumed]) > 4


@TRANSIENT_EXIT_FAILURES
def test_transient_exit_failing_twice_costs_one_lapse_and_is_retried_next_window(
    clock: FakeClock, reply: object, counter: str, reply_cost: float
) -> None:
    """The retry is bounded to one per window: a second failure waits that
    window's lapse out (no Mode 01 inside it, no third exit), and the next
    window tries the exit again — a retry, not a disable."""
    del reply_cost
    bus = ScriptedEcu(
        clock, session_gated=True, s3_seconds=0.1, session_script={EXIT: ["ok", reply, reply]}
    )
    session = _session(cycle_seconds=CYCLE, lapse_seconds=1.0)
    _readings, _stats, snaps = _run(
        clock, bus, _spec(did_session=session), seconds=3.0, metrics_every=0.5
    )
    log = request_log(bus)
    exits = [s.at for s in log if s.kind == "exit"]
    assert len(exits) >= 4
    failed, retry = exits[1], exits[2]
    # The lapse is counted from the window close, one slot before the exit.
    lapse_end = failed - BUDGET_SLOT + 1.0
    assert exits[3] > lapse_end
    assert kinds_between(log, failed + EPS, retry) == []
    assert kinds_between(log, retry + EPS, lapse_end - EPS) == []
    assert kinds_between(log, lapse_end - EPS, lapse_end + BUDGET_SLOT).count("mode01") >= 1
    profile = snaps[-1].did_profile
    assert profile is not None
    assert profile["fast_exit_supported"] is True
    assert profile["fast_exit_failures"] == 2
    assert profile["fast_exit_retries"] == 1
    assert snaps[-1].to_dict()["session_control"][counter] == 2
    assert len([s for s in log if s.kind == "mode01" and s.at > lapse_end]) > 4


def _gap_around(readings: list[ObdReading], at: float) -> float:
    """The all-channel reading gap spanning ``at`` (acquisition time)."""
    stamps = sorted(float(r.acquired_at) for r in readings if r.acquired_at is not None)
    before = max(t for t in stamps if t <= at)
    after = min(t for t in stamps if t > at)
    return after - before


def test_lost_entry_reply_sends_the_exit_before_any_mode01(clock: FakeClock) -> None:
    """D03.18's 2.4 s stall: the ECM enters 0xC0 but its ``50 C0`` is lost.
    The loop used to assume the default session and send the next Mode 01
    batch into the held session, where Mode 01 is silent — a second full
    timeout. A silent entry now means "state unknown": ``10 81`` goes out
    first, so the gap is one timeout plus one exit, not two timeouts."""
    pids = ("RPM", "SPEED", "ENGINE_LOAD", "MAF", "TIMING_ADVANCE", "COOLANT_TEMP")
    bus = ScriptedEcu(
        clock, session_gated=True, s3_seconds=5.0, session_script={SESSION: ["ok", "lost"]}
    )
    session = _session(cycle_seconds=0.5, lapse_seconds=5.0)
    spec = _spec(did_session=session, pids=pids, batch_max_pids=6, request_timeout_seconds=1.0)
    readings, stats, snaps = _run(clock, bus, spec, seconds=6.0, metrics_every=0.5)
    log = request_log(bus)
    lost = entries(log)[1]
    after = [s for s in log if s.at > lost]
    assert after[0].kind == "exit"
    assert after[0].at - lost <= 1.0 + BUDGET_SLOT + EPS
    assert after[1].kind == "mode01"
    gap = _gap_around(readings, lost + clock.time() - clock.monotonic())
    # One entry timeout + the exit + its grace; before the fix 2.16 s.
    assert gap < 1.0 + 0.3, f"{gap:.3f}s gap: Mode 01 went into the held session"
    assert stats.request_timeouts == 1
    assert stats.batch_failures == 0
    profile = snaps[-1].did_profile
    assert profile is not None
    assert profile["unknown_session_exits"] == 1
    assert profile["fast_exit_failures"] == 0
    assert profile["windows_opened"] >= 4


def test_refused_entry_is_not_an_unknown_session(clock: FakeClock) -> None:
    """An NRC proves the ECM stayed in the default session: Mode 01 resumes
    directly, no exit is spent."""
    bus = ScriptedEcu(
        clock, session_gated=True, s3_seconds=5.0, session_script={SESSION: ["ok", 0x22]}
    )
    session = _session(cycle_seconds=0.5)
    _readings, _stats, snaps = _run(
        clock, bus, _spec(did_session=session), seconds=2.0, metrics_every=0.5
    )
    log = request_log(bus)
    refused = entries(log)[1]
    after = [s for s in log if s.at > refused]
    assert after[0].kind == "mode01"
    profile = snaps[-1].did_profile
    assert profile is not None
    assert profile["unknown_session_exits"] == 0


class _KeyOffEcu(ScriptedEcu):
    """Answers nothing at all inside ``[off_from, off_until)`` — key out."""

    off_from = float("inf")
    off_until = float("inf")

    def send(self, msg: object, timeout: float | None = None) -> None:
        now = self.clock.monotonic()
        if self.off_from <= now < self.off_until:
            self.sent.append((now, msg))  # type: ignore[arg-type]
            return
        super().send(msg, timeout)  # type: ignore[arg-type]


def test_silent_ecu_gets_one_extra_exit_per_silence_not_a_storm(clock: FakeClock) -> None:
    """Key off: every entry is silent. The exit a silent entry schedules is
    spent once per silence (the ECU must answer something before another),
    is never retried, and costs no lapse — so the request pattern is today's
    plus one request, batching stays intact, and a key-on is picked up as
    quickly as before."""
    pids = ("RPM", "SPEED", "ENGINE_LOAD", "MAF", "TIMING_ADVANCE", "COOLANT_TEMP")
    bus = _KeyOffEcu(clock, session_gated=True, s3_seconds=5.0)
    session = _session(cycle_seconds=0.5, lapse_seconds=5.0)
    spec = _spec(did_session=session, pids=pids, batch_max_pids=6, request_timeout_seconds=1.0)
    start = clock.monotonic()
    wall_offset = clock.time() - start
    bus.off_from, bus.off_until = start + 3.0, start + 33.0
    readings: list[ObdReading] = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=start + 40.0,
    )
    log = request_log(bus)
    silent = kinds_between(log, start + 3.0, start + 33.0)
    # 1 s timeouts: at most one request per second of silence, one exit total.
    assert silent.count("exit") == 1
    assert len(silent) <= 31
    assert stats.batch_size_narrowings == 0
    # Key on is picked up within the request in flight plus one more.
    woke = min(
        float(r.acquired_at) - wall_offset
        for r in readings
        if r.acquired_at is not None and float(r.acquired_at) - wall_offset >= start + 33.0
    )
    assert woke - (start + 33.0) <= 2.0 + BUDGET_SLOT
    later = [s for s in log if s.at >= start + 34.0]
    assert "did" in [s.kind for s in later]
    assert "mode01" in [s.kind for s in later]


def test_permanent_refusal_disables_the_exit_and_recovery_is_observed(
    clock: FakeClock,
) -> None:
    """Two ``subFunctionNotSupported`` disable the fast exit provisionally;
    for the re-probe interval no ``10 81`` is sent (nothing is polled to
    find out whether the ECU recovered), then one re-probe succeeds and the
    recovery is reported from an observed Mode 01 answer."""
    bus = ScriptedEcu(
        clock, session_gated=True, s3_seconds=0.1, session_script={EXIT: ["ok", 0x12, 0x12]}
    )
    session = _session(cycle_seconds=1.0, lapse_seconds=0.3)
    _readings, _stats, snaps = _run(
        clock,
        bus,
        _spec(did_session=session),
        seconds=FAST_EXIT_RETRY_MIN_SECONDS + 6.0,
        metrics_every=0.5,
    )
    log = request_log(bus)
    exits = [s.at for s in log if s.kind == "exit"][1:]
    assert len(exits) >= 3
    disabled_at = exits[1]
    assert exits[2] >= disabled_at + FAST_EXIT_RETRY_MIN_SECONDS - EPS
    assert kinds_between(log, disabled_at + LATENCY, exits[2]).count("exit") == 0
    # Windows kept running on the lapse path meanwhile, and Mode 01 with them.
    between = kinds_between(log, disabled_at + LATENCY, exits[2])
    assert between.count("entry") >= 20 and between.count("mode01") >= 20
    profile = snaps[-1].did_profile
    assert profile is not None
    assert profile["fast_exit_supported"] is True
    assert profile["cycle"]["mode01_recovery_pending"] is False
    assert profile["cycle"]["mode01_recovered_age_s"] is not None


def test_grace_longer_than_the_cycle_is_infeasible_but_both_services_progress(
    clock: FakeClock,
) -> None:
    bus = ScriptedEcu(clock, session_gated=True)
    session = _session(cycle_seconds=CYCLE, exit_grace_seconds=0.4)
    _readings, _stats, snaps = _run(
        clock, bus, _spec(did_session=session), seconds=4.0, metrics_every=1.0
    )
    log = request_log(bus)
    starts = entries(log)
    assert_on_cycle_grid(starts, CYCLE)
    assert all(later - earlier >= 2 * CYCLE - EPS for earlier, later in pairwise(starts))
    cycle = last_cycle(snaps)
    assert cycle["cycle_feasible"] is False
    assert cycle["cycles_missed"] > 0
    for earlier, later in pairwise(starts):
        between = kinds_between(log, earlier, later)
        assert "mode01" in between and "did" in between


def test_no_eligible_dids_leaves_mode01_alone_without_probing(clock: FakeClock) -> None:
    """Both DIDs silent: after the first window they are backed off, no
    further windows open, no ``22`` is sent to see whether they came back,
    Mode 01 runs at rate, and idle time is not reported as missed cycles."""
    bus = ScriptedEcu(clock, session_gated=True, silent_dids=(0x1101, 0x1208))
    _readings, stats, snaps = _run(
        clock,
        bus,
        _spec(did_session=_session(cycle_seconds=CYCLE)),
        seconds=10.0,
        metrics_every=1.0,
    )
    log = request_log(bus)
    assert len(entries(log)) == 1
    dids = [s.at for s in log if s.kind == "did"]
    assert len(dids) == 2
    mode01_after = [s for s in log if s.kind == "mode01" and s.at > dids[-1] + 0.3]
    assert len(mode01_after) >= 2 * 4 * 8  # two PIDs at 4 Hz for ~9 s
    # The one window overran its cycle (two 0.2 s timeouts): those boundaries
    # are missed. The idle stretch after it is not.
    missed = [s.did_profile["cycle"]["cycles_missed"] for s in snaps if s.did_profile]  # type: ignore[index]
    assert missed[2] == missed[-1] > 0
    assert stats.rounds_skipped == 0


def test_continuous_profile_holds_the_session_and_wakes_for_keepalive(
    clock: FakeClock, runtime_reset: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    tuning_profile.activate(
        _profile(
            "all-did",
            mode01={},
            mode01_enabled=False,
            did_continuous=True,
            did_cycle_seconds=None,
            target_rate_hz=10.0,
        ),
        source="test",
    )
    bus = ScriptedEcu(clock, session_gated=True)
    session = _session(keepalive_seconds=0.5)
    readings, _stats, _ = _run(
        clock, bus, _spec(did_session=session, capture_profiles_enabled=True), seconds=3.0
    )
    log = request_log(bus)
    assert [s for s in log if s.kind == "exit"] == [log[0]]  # connect-time only
    starts = entries(log)
    assert len(starts) >= 1 + 5  # open + one keep-alive per 0.5 s
    assert all(
        0.5 - EPS <= later - earlier <= 0.5 + GRID_SLACK for earlier, later in pairwise(starts)
    )
    assert not [s for s in log if s.kind == "mode01" and s.at > starts[0]]
    assert len([r for r in readings if r.pid.startswith("DID_")]) >= 40
    assert bus.session == SESSION
    assert "keepalive" in {w.reason for _n, w in wakes}


# ---------------------------------------------------------------------------
# C04.7 — no stale controller phase after profile switch / pause / passive /
# exit failure / reconnect
# ---------------------------------------------------------------------------


def _switch_after_dids(bus: ScriptedEcu, count: int, action: object) -> list[float]:
    """Run ``action`` once the ``count``-th DID request has been sent."""
    fired: list[float] = []

    def hook(msg: object) -> None:
        data = bytes(msg.data)  # type: ignore[attr-defined]
        if fired or len(data) < 2 or data[1] != 0x22:
            return
        if bus.single_requests >= count:
            fired.append(bus.clock.monotonic())
            action()  # type: ignore[operator]

    bus.on_send.append(hook)
    return fired


def test_switching_out_of_continuous_mode_completes_the_session_exit(
    clock: FakeClock, runtime_reset: object
) -> None:
    tuning_profile.activate(
        _profile(
            "all-did", mode01={}, mode01_enabled=False, did_continuous=True, did_cycle_seconds=None
        ),
        source="test",
    )
    bus = ScriptedEcu(clock, session_gated=True)
    switched = _switch_after_dids(bus, 10, lambda: tuning_profile.set_active(False, source="test"))
    readings, _stats, _ = _run(clock, bus, _spec(capture_profiles_enabled=True), seconds=3.0)
    assert switched
    at = switched[0]
    log = request_log(bus)
    after = [s for s in log if s.at > at]
    exit_at = next(s.at for s in after if s.kind == "exit")
    assert not [s for s in after if s.kind == "did"], "DIDs polled after the profile dropped them"
    mode01 = [s for s in after if s.kind == "mode01"]
    assert mode01, "Mode 01 never resumed after leaving continuous mode"
    assert mode01[0].at >= exit_at + LATENCY + 0.05 - EPS, "Mode 01 polled into the held session"
    assert bus.session is None
    assert [r for r in readings if r.pid == "RPM" and r.timestamp > 0]


def test_pause_and_resume_complete_the_transition_both_ways(
    clock: FakeClock, runtime_reset: object
) -> None:
    tuning_profile.activate(_profile("hybrid"), source="test")
    resume_at: list[float] = []

    class _ResumingEcu(ScriptedEcu):
        def recv(self, timeout: float | None = None):  # type: ignore[no-untyped-def]
            if resume_at and not resumed and self.clock.monotonic() >= resume_at[0]:
                resumed.append(self.clock.monotonic())
                tuning_profile.set_paused(False)
            return super().recv(timeout)

    resumed: list[float] = []
    bus = _ResumingEcu(clock, session_gated=True)

    def pause() -> None:
        tuning_profile.set_paused(True)
        resume_at.append(clock.monotonic() + 1.0)

    paused = _switch_after_dids(bus, 3, pause)
    _run(clock, bus, _spec(capture_profiles_enabled=True), seconds=4.0)
    assert paused and resumed
    log = request_log(bus)
    quiet = [s for s in log if paused[0] < s.at < resumed[0]]
    assert [s.kind for s in quiet] == ["exit"], quiet
    assert quiet[0].at - paused[0] <= BUDGET_SLOT + LATENCY + EPS
    after = [s.kind for s in log if s.at >= resumed[0]]
    assert "entry" in after and "did" in after and "mode01" in after
    assert bus.session is None or after[-1] in ("did", "entry")
    assert_recv_continuous(bus)


def test_passive_gating_completes_the_transition_when_lifted(
    clock: FakeClock, runtime_reset: object
) -> None:
    tuning_profile.activate(_profile("hybrid"), source="test")
    lift_at: list[float] = []
    lifted: list[float] = []

    class _LiftingEcu(ScriptedEcu):
        def recv(self, timeout: float | None = None):  # type: ignore[no-untyped-def]
            if lift_at and not lifted and self.clock.monotonic() >= lift_at[0]:
                lifted.append(self.clock.monotonic())
                tx_gate.set_passive(False, source="test")
            return super().recv(timeout)

    bus = _LiftingEcu(clock, session_gated=True)

    def go_passive() -> None:
        tx_gate.set_passive(True, source="test")
        lift_at.append(clock.monotonic() + 1.0)

    went = _switch_after_dids(bus, 3, go_passive)
    _run(clock, bus, _spec(capture_profiles_enabled=True), seconds=4.0)
    assert went and lifted
    log = request_log(bus)
    assert not [s for s in log if went[0] < s.at < lifted[0]], "sent while passive"
    after = [s for s in log if s.at >= lifted[0]]
    assert after[0].kind == "exit", "the held session was not released after passive lifted"
    kinds = [s.kind for s in after]
    assert "entry" in kinds and "did" in kinds and "mode01" in kinds
    assert_recv_continuous(bus)


def test_exit_failure_leaves_no_stale_phase(clock: FakeClock) -> None:
    """Every exit refused transiently: each window lapses and Mode 01 still
    runs between windows; the controller never wedges in ``lapse``."""
    bus = ScriptedEcu(
        clock, session_gated=True, s3_seconds=0.1, session_script={EXIT: ["ok"] + [0x21] * 50}
    )
    session = _session(cycle_seconds=1.0, lapse_seconds=0.3)
    _readings, _stats, snaps = _run(
        clock, bus, _spec(did_session=session), seconds=5.0, metrics_every=0.5
    )
    log = request_log(bus)
    starts = entries(log)
    assert len(starts) >= 4
    for earlier, later in pairwise(starts):
        assert "mode01" in kinds_between(log, earlier, later)
    assert snaps[-1].did_profile is not None
    assert snaps[-1].did_profile["phase"] in ("mode01", "window", "lapse")
    assert snaps[-1].did_profile["fast_exit_supported"] is True


def test_reconnect_returns_the_ecu_to_the_default_session_first(clock: FakeClock) -> None:
    """A new loop cannot know the previous one left the ECU in 0xC0 (Mode 01
    dead, and the blackout outlives an S3 lapse); with a verified exit the
    first request is ``10 81`` and Mode 01 answers on the next one."""
    bus = ScriptedEcu(clock, session_gated=True, s3_seconds=60.0)
    bus._handle_session_control(SESSION, clock.monotonic())
    bus._pending.clear()
    assert bus.session == SESSION
    readings, _stats, _ = _run(clock, bus, _spec(), seconds=1.0)
    log = request_log(bus)
    assert log[0].kind == "exit"
    assert log[1].kind == "mode01" and log[1].at <= log[0].at + BUDGET_SLOT + EPS
    assert readings and readings[0].pid == "RPM"
    assert readings[0].acquired_at is not None
    assert readings[0].acquired_at - 1_700_000_000.0 < 0.1


# ---------------------------------------------------------------------------
# C04.8 / C04.11 / C04.13 — end-to-end mixed capture with request ordering
# ---------------------------------------------------------------------------


def test_end_to_end_mixed_capture_orders_requests_and_wakes_at_deadlines(
    clock: FakeClock, runtime_reset: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    wakes = _record_wakes(monkeypatch)
    tuning_profile.activate(_profile("hybrid"), source="test")
    bus = ScriptedEcu(clock, session_gated=True)
    readings, stats, snaps = _run(
        clock,
        bus,
        _spec(
            did_session=_session(cycle_seconds=CYCLE),
            capture_profiles_enabled=True,
            batch_max_pids=6,
        ),
        seconds=4.0,
        metrics_every=1.0,
    )
    log = request_log(bus)
    starts = entries(log)
    assert len(starts) >= 14
    assert_on_cycle_grid(starts, CYCLE)
    # One cycle: entry, both DIDs, exit, one batched Mode 01 request.
    for earlier, later in pairwise(starts):
        assert kinds_between(log, earlier, later) == ["entry", "did", "did", "exit", "mode01"]
    assert bus.batch_requests >= 14
    # Wakes: after each exit the transition (grace) deadline; before each
    # window the cycle boundary itself.
    for start in starts[1:]:
        before = [(now, w) for now, w in wakes if now < start]
        assert before[-1][1].reason == "transition"
        assert before[-1][1].at == pytest.approx(start, abs=1e-6)
    for exit_at in [s.at for s in log if s.kind == "exit"][1:]:
        now, wake = next((n, w) for n, w in wakes if n >= exit_at + LATENCY - EPS)
        assert wake.reason == "transition" and wake.at == pytest.approx(now + 0.05, abs=1e-6)
    assert "idle" not in {w.reason for _n, w in wakes}

    cycle = last_cycle(snaps)
    assert cycle["cycle_seconds"] == CYCLE
    assert cycle["cycle_feasible"] is True and cycle["cycles_missed"] == 0
    assert cycle["mode01_shortfall"] == 0 and cycle["did_shortfall"] == 0
    duration = cycle["cycle_duration_s"]
    assert duration["median"] == duration["max"] == pytest.approx(CYCLE, abs=1e-4)  # type: ignore[index]
    # Transition latency is request -> first *response* of the other service:
    # exit + reply, grace, Mode 01 request + reply; entry + reply, budget
    # slot, DID request + reply. Both are read off the wire log.
    latency = cycle["transition_latency_s"]
    exit_gaps = exit_to_mode01_gaps(log)[1:]
    assert median(exit_gaps) == pytest.approx(0.05 + LATENCY, abs=1e-3)
    assert latency["exit_to_mode01"]["median"] == pytest.approx(  # type: ignore[index]
        median(exit_gaps) + LATENCY, abs=1e-3
    )
    entry_gaps = [next(s.at for s in log if s.kind == "did" and s.at > at) - at for at in starts]
    assert latency["entry_to_did"]["median"] == pytest.approx(  # type: ignore[index]
        median(entry_gaps) + LATENCY, abs=1e-3
    )
    occupancy = cycle["occupancy_s"]
    assert set(occupancy) == {"mode01", "mode22", "session"}  # type: ignore[arg-type]
    assert occupancy["session"] == pytest.approx(2 * LATENCY, abs=1e-3)  # type: ignore[index]
    separation = cycle["cross_mode_separation_s"]
    assert separation is not None and 0 < separation["max"] < CYCLE  # type: ignore[index]
    control = snaps[-1].to_dict()["session_control"]
    assert control["sent"] == control["responses"] == stats.session_controls_sent
    assert control["channel_requests_sent"] == stats.requests_sent - stats.session_controls_sent
    assert control["channel_response_rate"] is not None
    assert {r.pid for r in readings} == {"RPM", "SPEED", "DID_1101", "DID_1208"}
    assert all(r.round_id is not None for r in readings)
    assert_recv_continuous(bus)
    assert clock.monotonic() >= 1004.0
