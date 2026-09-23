"""A capture profile's members start within one cycle of the switch (D05.1).

2026-09-23 16:40:07Z: `_capture-tuning-2026-09-23T164007` started from a
warm, running `default`. Two of the eight `tuning` DIDs (0x1206, 0x1209) and
every RAM window answered within 0.04 s; the other six first appeared 37.5 s
and 38.0 s in. Under `default` a DID's cadence is its window grid (one read
per 60 s window), and that `next_due_at` survived the switch, so `tuning`'s
0.5 s windows opened without those DIDs until the old grid came round. A DID
`default` never polls (0x1209) had no stale due time and started at once.
This file reproduces that shape exactly: a switch ~22.5 s after a `default`
window left the `default` DIDs silent for 38.0 s and the new one at 0.006 s.

Everything runs on the fake clock and the session-gated :class:`ScriptedEcu`,
so every request is on the wire log and the assertion is exact: each profile
member's first request goes out within one ``did_cycle_seconds`` of the switch.
"""

from __future__ import annotations

import logging
import threading

import pytest

from src.collection import tuning_profile, tx_gate
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_profiles import CaptureProfile
from src.collection.obd_collector import ObdReading
from src.collection.obd_scheduler import (
    AUTO_MISS_BACKOFF_SECONDS,
    CanObdSpec,
    DidSessionSpec,
    ObdSchedulerMetrics,
)
from src.config import CanBusConfig
from tests.capture_round_fakes import DEFAULT_VALUES, FakeClock, ScriptedEcu, install_clock

SESSION = 0xC0
EXIT = 0x81
LATENCY = 0.005
#: `tuning`'s start-to-start hybrid cycle on the Xterra.
TUNING_CYCLE = 0.5
#: `obd_did_window_interval_seconds` — the config default the Pi runs `default` on.
DEFAULT_WINDOW_INTERVAL = 60.0
START = 1000.0
#: `default`'s DIDs (a subset of the tuning set, as on the truck) …
DEFAULT_DIDS = ("1206", "1207", "1208", "1219")
#: … and `tuning` adds one `default` never polls (0x1209 on the truck).
TUNING_DIDS = (*DEFAULT_DIDS, "1209")
EPS = 1e-6


#: Mode 01 members of both profiles. BAROMETRIC_PRESSURE is a `slow`-tier PID:
#: 4 Hz x 1/15 under `default`, a 3.75 s cadence.
PIDS = ("RPM", "SPEED", "BAROMETRIC_PRESSURE")


def _spec() -> CanObdSpec:
    return CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=4.0,
        pids=PIDS,
        dids=TUNING_DIDS,
        batch_max_pids=1,
        priority_channels=(),
        dtc_poll_interval_seconds=0.0,
        service_poll_interval_seconds=0.0,
        request_timeout_seconds=0.2,
        capture_profiles_enabled=True,
        did_session=DidSessionSpec(
            session=SESSION,
            exit_session=EXIT,
            exit_grace_seconds=0.05,
            keepalive_seconds=2.0,
            window_interval_seconds=DEFAULT_WINDOW_INTERVAL,
            window_max_seconds=10.0,
            lapse_seconds=5.0,
        ),
    )


def _default_profile() -> CaptureProfile:
    return CaptureProfile(
        name="default",
        mode01=dict.fromkeys(PIDS, 1.0),
        mode22=DEFAULT_DIDS,
        corpus_eligible=True,
    )


def _tuning_profile() -> CaptureProfile:
    return CaptureProfile(
        name="tuning",
        mode01=dict.fromkeys(PIDS, 1.0),
        mode22=TUNING_DIDS,
        mode22_rate_weights=dict.fromkeys(TUNING_DIDS, 0.5),
        did_cycle_seconds=TUNING_CYCLE,
        round_timestamps=True,
        target_rate_hz=4.0,
        corpus_eligible=False,
        storage_prefix="_capture-tuning",
    )


class _SwitchingEcu(ScriptedEcu):
    """Activates `tuning` the first time the fake clock reaches ``switch_at``."""

    def __init__(self, clock: FakeClock, switch_at: float, **kwargs: object) -> None:
        kwargs.setdefault("values", {**DEFAULT_VALUES, 0x33: b"\x50"})
        super().__init__(clock, session_gated=True, **kwargs)  # type: ignore[arg-type]
        self.switch_at = switch_at
        self.switched: float | None = None
        self.on_switch: list[object] = []

    def recv(self, timeout: float | None = None):  # type: ignore[no-untyped-def]
        if self.switched is None and self.clock.monotonic() >= self.switch_at:
            self.switched = self.clock.monotonic()
            tuning_profile.activate(_tuning_profile(), source="test")
            for hook in self.on_switch:
                hook()  # type: ignore[operator]
        return super().recv(timeout)


def _did_requests(bus: ScriptedEcu) -> list[tuple[float, int]]:
    out: list[tuple[float, int]] = []
    for at, msg in bus.sent:
        data = bytes(msg.data)
        if len(data) >= 4 and data[1] == 0x22:
            out.append((at, int.from_bytes(data[2:4], "big")))
    return out


def _run(
    clock: FakeClock, bus: ScriptedEcu, *, seconds: float
) -> tuple[list[ObdReading], list[ObdSchedulerMetrics]]:
    readings: list[ObdReading] = []
    snapshots: list[ObdSchedulerMetrics] = []
    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(),
        on_frame=lambda _m: None,
        on_reading=readings.append,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + seconds,
        on_scheduler_metrics=snapshots.append,
        scheduler_metrics_interval_seconds=0.25,
    )
    return readings, snapshots


def _mode01_requests(bus: ScriptedEcu) -> list[tuple[float, int]]:
    return [
        (at, bytes(msg.data)[2])
        for at, msg in bus.sent
        if len(bytes(msg.data)) >= 3 and bytes(msg.data)[1] == 0x01
    ]


def _first_requests_after(bus: ScriptedEcu, at: float) -> dict[int, float]:
    first: dict[int, float] = {}
    for sent_at, did in _did_requests(bus):
        if sent_at >= at and did not in first:
            first[did] = sent_at - at
    return first


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock(monotonic=START)
    install_clock(monkeypatch, fake)
    return fake


@pytest.fixture(autouse=True)
def runtime_reset() -> object:
    tuning_profile.activate(_default_profile(), source="test")
    yield None
    tuning_profile.set_paused(False)
    tx_gate.set_passive(False, source="test")
    tuning_profile.set_active(False, source="test")


#: Offsets of the switch from `default`'s second DID window, which opens at
#: +120 s (the entry goes out ~0.07 s later behind Mode 01 and its four DIDs
#: at ~0.11-0.20 s): before the first DID, mid-sweep, in the fast-exit grace,
#: then a quarter, the 2026-09-23 capture's ~22.5 s, and most of the way round
#: the 60 s window grid. Before D05.1 every one of them waited for `default`'s
#: next window on the old per-DID due times, and with that fixed alone the
#: first two still waited out the config's 60 s window interval, because the
#: new cycle had no anchor yet when the switch's lapse ended.
@pytest.mark.parametrize("after_default_window", [0.1, 0.15, 0.25, 1.0, 15.0, 22.5, 45.0, 59.0])
def test_every_profile_did_starts_within_one_cycle_of_a_switch_from_default(
    clock: FakeClock, after_default_window: float
) -> None:
    switch_at = START + 2 * DEFAULT_WINDOW_INTERVAL + after_default_window
    bus = _SwitchingEcu(clock, switch_at)
    _run(clock, bus, seconds=switch_at - START + 3.0)
    assert bus.switched is not None
    # `default` really was polling its DIDs on its own grid before the switch.
    before = [did for at, did in _did_requests(bus) if at < bus.switched]
    assert {f"{did:04X}" for did in before} == set(DEFAULT_DIDS)

    first = _first_requests_after(bus, bus.switched)
    late = {
        f"0x{did:04X}": round(first.get(did, float("inf")), 3)
        for did in (int(name, 16) for name in TUNING_DIDS)
        if first.get(did, float("inf")) > TUNING_CYCLE + EPS
    }
    assert not late, f"profile DIDs first requested more than one cycle after the switch: {late}"


def test_a_did_backed_off_under_default_is_not_benched_into_the_capture(
    clock: FakeClock,
) -> None:
    """A miss under `default` earns the 30 s flat back-off; `tuning` owns the DID
    now, and its policy (D03.18: one timeout is not 30 s) is what applies."""
    switch_at = START + 2 * DEFAULT_WINDOW_INTERVAL + 5.0
    bus = _SwitchingEcu(clock, switch_at, silent_dids=(0x1208,))

    def ecu_answers_again() -> None:
        bus.silent_dids = frozenset()

    bus.on_switch.append(ecu_answers_again)
    # Answered once in the first `default` window, then silent in the second.
    first_window_end = START + DEFAULT_WINDOW_INTERVAL + 1.0
    real_silent = bus.silent_dids
    bus.silent_dids = frozenset()

    def go_silent() -> None:
        bus.silent_dids = real_silent

    bus.on_send.append(
        lambda _msg: (
            go_silent()
            if bus.clock.monotonic() >= first_window_end and bus.switched is None
            else None
        )
    )
    _run(clock, bus, seconds=switch_at - START + 3.0)
    assert bus.switched is not None
    missed_at = max(at for at, did in _did_requests(bus) if did == 0x1208 and at < bus.switched)
    assert bus.switched - missed_at < AUTO_MISS_BACKOFF_SECONDS
    first = _first_requests_after(bus, bus.switched)
    assert first.get(0x1208, float("inf")) <= TUNING_CYCLE + EPS, first


def test_profile_start_ledger_names_what_withheld_a_member(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty channel must say which kind of empty (D05.1, AGENTS.md).

    0x1208 is silent from the start. It has never answered, so its back-off is
    the never-answered doubling (30 s, then 60 s after `default`'s second
    window), which stands on its own evidence and a profile change does not
    shorten. The ledger must name it, and why, once.
    """
    switch_at = START + 2 * DEFAULT_WINDOW_INTERVAL + 5.0
    bus = _SwitchingEcu(clock, switch_at, silent_dids=(0x1208,))
    caplog.set_level(logging.WARNING, logger="src.collection.obd_scheduler")
    _readings, snaps = _run(clock, bus, seconds=switch_at - START + 3.0)
    assert bus.switched is not None
    profile = snaps[-1].did_profile
    assert profile is not None
    ledger = profile["profile_start"]
    assert ledger["profile"] == "tuning"
    assert ledger["resolved"] is True
    started = ledger["first_request_s"]
    assert set(started) == {f"DID_{name}" for name in TUNING_DIDS}
    for name in TUNING_DIDS:
        if name == "1208":
            assert started["DID_1208"] is None
        else:
            assert started[f"DID_{name}"] is not None
            assert started[f"DID_{name}"] <= TUNING_CYCLE + EPS
    assert ledger["withheld"] == [
        {
            "channel": "DID_1208",
            "reason": "backoff",
            "for_s": pytest.approx(55.0, abs=1.0),
            "missed_responses": 2,
        }
    ]
    assert snaps[-1].profile_start_withheld == {
        "backoff": 1,
        "cadence": 0,
        "no_window": 0,
        "budget": 0,
    }
    assert snaps[-1].to_dict()["profile_start_withheld"]["backoff"] == 1
    messages = [
        r.getMessage() for r in caplog.records if "withheld at profile start" in r.getMessage()
    ]
    assert messages == [
        "profile tuning: 1 of 5 Mode-22 window members withheld at profile start: "
        "DID_1208 (backoff)"
    ]


def test_a_start_the_ecu_never_answers_is_reported_as_no_window(clock: FakeClock) -> None:
    """Key-off: every entry is silent, so no window opens and nothing is sent.
    That is a different empty from a back-off, and the ledger says so."""
    switch_at = START + 2 * DEFAULT_WINDOW_INTERVAL + 5.0
    bus = _SwitchingEcu(clock, switch_at)
    bus.on_switch.append(lambda: bus.session_script.setdefault(SESSION, []).extend([None] * 50))
    _readings, snaps = _run(clock, bus, seconds=switch_at - START + 3.0)
    ledger = snaps[-1].did_profile["profile_start"]  # type: ignore[index]
    assert ledger["resolved"] is True
    assert {row["reason"] for row in ledger["withheld"]} == {"no_window"}
    assert len(ledger["withheld"]) == len(TUNING_DIDS)
    assert snaps[-1].profile_start_withheld["no_window"] == len(TUNING_DIDS)


@pytest.mark.parametrize("after_default_read", [0.05, 1.0, 2.0, 3.0])
def test_a_slow_tier_pid_starts_within_one_cycle_of_the_switch(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch, after_default_read: float
) -> None:
    """docs/sustained-validation.md finding 3: a Mode 01 member kept the due
    time `default`'s tier gave it (here 3.75 s) into a 4 Hz capture."""
    probe = _SwitchingEcu(clock, float("inf"))
    _run(clock, probe, seconds=10.0)
    last_read = max(at for at, pid in _mode01_requests(probe) if pid == 0x33)

    # The same run again on a fresh clock, switched this time.
    rerun = FakeClock(monotonic=START)
    install_clock(monkeypatch, rerun)
    bus = _SwitchingEcu(rerun, last_read + after_default_read)
    _run(rerun, bus, seconds=last_read + after_default_read - START + 2.0)
    assert bus.switched is not None
    first = next(at for at, pid in _mode01_requests(bus) if pid == 0x33 and at >= bus.switched)
    assert first - bus.switched <= TUNING_CYCLE + EPS
