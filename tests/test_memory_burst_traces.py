"""Fixed-input bus traces of the memory burst, pinned before R18 moved it.

R18 moved the burst plan, its slot accounting and its timing estimates out of
``_CanObdLoop`` into :mod:`src.collection.memory_burst`. Replay cannot show a
scheduling change (it re-emits stored readings; it never asks the bus), so the
evidence is the loop itself on the fake clock: every scenario below runs
``run_can_obd_loop`` against a :class:`ScriptedEcu` and records

- every request sent (fake-clock time and bytes, flow control left out),
- every reading delivered (channel, acquisition time, presentation stamp),
- the burst decision the loop took on each tick (``_memory_burst``), and
- the burst state after each tick: the plan, slots spent, the budget clock
  and the three turnaround estimates.

``tests/fixtures/memory_burst_traces.json`` holds each scenario's counts and
the sha256 of each normalised trace, captured before the move at 38bb1b69
(collection code identical to fda3c7b7), in a worktree of that commit.
A digest says *that* a trace changed, not where: :func:`trace` returns the
full record, so ``python -c "from tests.test_memory_burst_traces import dump;
dump('<dir>')"`` (with the fake clock installed by the function itself) writes
every scenario as JSON for a diff against the same dump at the pinned commit.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import can
import pytest

from src.collection import bus_budget, can_obd_loop, capture_generation, tuning_profile, tx_gate
from src.collection.calibration_id import CalibrationIdSpec
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.obd_reading import ObdReading
from src.collection.obd_scheduler import did_alias_table
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock
from tests.test_memory_window_burst import DIDS, LATENCY, _profile, _spec

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "memory_burst_traces.json"
DIGITS = 9


def _round(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, DIGITS)
    if isinstance(value, list | tuple):
        return [_round(v) for v in value]
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    if isinstance(value, frozenset | set):
        return sorted(value)
    return value


def burst_state(loop: can_obd_loop._CanObdLoop) -> tuple[Any, ...]:
    """The burst state one tick left behind: plan fields, slots spent, the
    budget clock, and the memory-read, paced-read and session turnarounds.

    The single place this test reads the owner's fields."""
    planner = loop.burst
    plan = planner.plan
    return (
        None if plan is None else dataclasses.astuple(plan),
        planner.slots_spent,
        planner.next_budget_at,
        planner.memory_read_seconds,
        planner.paced_read_seconds,
        planner.session_seconds,
    )


def _decision(decision: Any) -> Any:
    if decision is None:
        return None
    return (sorted(decision.members), decision.hold_until, decision.issue_now)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    clock: FakeClock
    bus: ScriptedEcu
    stop: threading.Event
    #: Called with the request about to be delivered and its service byte;
    #: returns False to swallow it before the ECM (no answer, no S3 refresh).
    filters: list[Callable[[can.Message, int], bool]] = field(default_factory=list)
    #: Called on every receive with the fake-clock time.
    on_recv: list[Callable[[float], None]] = field(default_factory=list)
    counts: dict[int, int] = field(default_factory=dict)

    def count(self, service: int) -> int:
        return self.counts.get(service, 0)


Setup = Callable[[_Run], None]


@dataclass(frozen=True)
class Scenario:
    seconds: float
    profile: dict[str, Any] = field(default_factory=dict)
    spec: dict[str, Any] = field(default_factory=dict)
    ecu: dict[str, Any] = field(default_factory=dict)
    setup: Setup | None = None
    calibration: bool = False


def _on_service(service: int, nth: int, action: Callable[[_Run], None]) -> Setup:
    """Run ``action`` just before the ``nth`` request of ``service`` goes out."""

    def setup(run: _Run) -> None:
        def hook(_msg: can.Message, svc: int) -> bool:
            if svc == service and run.count(service) == nth:
                action(run)
            return True

        run.filters.append(hook)

    return setup


def _both(*setups: Setup) -> Setup:
    def setup(run: _Run) -> None:
        for each in setups:
            each(run)

    return setup


def _unseen_memory(run: _Run) -> None:
    run.filters.append(lambda _msg, svc: svc != 0x23)


def _nrc_second_address(run: _Run) -> None:
    """The second memory address the loop reads is refused ``7F 23 31``."""
    addresses: list[int] = []

    def hook(msg: can.Message, svc: int) -> bool:
        if svc != 0x23:
            return True
        address = int.from_bytes(bytes(msg.data)[2:6], "big")
        if address not in addresses:
            addresses.append(address)
        if len(addresses) > 1 and address == addresses[1]:
            frame = bytes([0x03, 0x7F, 0x23, 0x31, 0, 0, 0, 0])
            run.bus._queue([frame], run.clock.monotonic())
            return False
        return True

    run.filters.append(hook)


def _turnarounds(*cycle: float) -> Setup:
    """The nth memory read is answered after ``cycle[n % len(cycle)]``."""

    def setup(run: _Run) -> None:
        def hook(_msg: can.Message, svc: int) -> bool:
            run.bus.latency = cycle[run.count(0x23) % len(cycle)] if svc == 0x23 else LATENCY
            return True

        run.filters.append(hook)

    return setup


_variable_turnaround = _turnarounds(0.004, 0.030, 0.012, 0.050, 0.008)
#: Reads either side of a 5 ms slot, so the paced-read mean (each read the
#: longer of slot and turnaround) differs from the plain turnaround mean.
_straddling_turnaround = _turnarounds(0.004, 0.030, 0.003, 0.020, 0.008)


def _override(rps: float | None) -> Callable[[_Run], None]:
    def action(_run: _Run) -> None:
        bus_budget.set_override(rps, source="test", ttl_seconds=None)

    return action


def _budget_from_start(rps: float) -> Setup:
    def setup(_run: _Run) -> None:
        bus_budget.set_override(rps, source="test", ttl_seconds=None)

    return setup


def _bump(_run: _Run) -> None:
    capture_generation.bump("r18-trace")


def _passive_for(seconds: float) -> Callable[[_Run], None]:
    def action(run: _Run) -> None:
        until = run.clock.monotonic() + seconds
        tx_gate.set_passive(True, source="test")

        def lift(now: float) -> None:
            if now >= until and tx_gate.is_passive():
                tx_gate.set_passive(False, source="test")

        run.on_recv.append(lift)

    return action


def _stop(run: _Run) -> None:
    run.stop.set()


def _engine_starts_at(offset: float) -> Setup:
    """RPM reads 0 and every memory read goes unanswered until ``offset``
    seconds in (past ``ENGINE_OFF_HOLD_SECONDS``, so the start is a real one);
    then the engine runs and memory answers."""

    def setup(run: _Run) -> None:
        start = run.clock.monotonic() + offset
        run.bus.values[0x0C] = b"\x00\x00"

        def hook(_msg: can.Message, svc: int) -> bool:
            if run.clock.monotonic() < start:
                return svc != 0x23
            run.bus.values[0x0C] = b"\x0b\xb8"
            return True

        run.filters.append(hook)

    return setup


SCENARIOS: dict[str, Scenario] = {
    "paced": Scenario(20.0),
    "keepalive_in_wait": Scenario(20.0, spec={"keepalive_seconds": 0.5}),
    "keepalive_near_burst": Scenario(20.0, spec={"keepalive_seconds": 0.9}),
    "silence": Scenario(
        40.0,
        ecu={"s3_seconds": 3.0, "lapse_blackout_seconds": 30.0},
        setup=_unseen_memory,
    ),
    "nrc": Scenario(20.0, setup=_nrc_second_address),
    "variable_turnaround": Scenario(20.0, setup=_variable_turnaround),
    "short_deadline": Scenario(20.0, spec={"window_max_seconds": 0.6}),
    "window_expiry_mid_burst": Scenario(
        20.0, spec={"window_max_seconds": 1.2}, setup=_unseen_memory
    ),
    "continuous": Scenario(
        10.0,
        profile={
            "mode01": {},
            "mode01_enabled": False,
            "did_continuous": True,
            "did_cycle_seconds": None,
        },
    ),
    "no_memory": Scenario(15.0, profile={"memory": ()}),
    "end_to_start_interval": Scenario(20.0, profile={"did_cycle_seconds": None}),
    "budget_change": Scenario(
        25.0,
        setup=_both(
            _on_service(0x23, 7, _override(20.0)),
            _on_service(0x23, 16, _override(None)),
        ),
    ),
    "generation_change": Scenario(
        20.0,
        setup=_both(_on_service(0x22, 8, _bump), _on_service(0x23, 5, _bump)),
    ),
    "passive": Scenario(20.0, setup=_on_service(0x23, 4, _passive_for(2.0))),
    "stop_mid_burst": Scenario(20.0, setup=_on_service(0x23, 8, _stop)),
    "calibration_id_in_wait": Scenario(20.0, calibration=True),
    "engine_start_rearm": Scenario(60.0, setup=_engine_starts_at(40.0)),
    # 200 req/s: a 5 ms slot, shorter than most reads, so the plan runs on
    # turnaround (``per_read`` from the paced-read mean) and the burst starts
    # before pacing's last read slot, where a keep-alive can fall inside it.
    "high_budget": Scenario(10.0, setup=_both(_budget_from_start(200.0), _straddling_turnaround)),
    **{
        f"high_budget_keepalive_{ms}ms": Scenario(
            10.0,
            spec={"keepalive_seconds": ms / 1000},
            setup=_both(_budget_from_start(200.0), _straddling_turnaround),
        )
        for ms in (40, 50, 60, 70, 80)
    },
    # The same with every read of the first burst longer than the slot.
    "high_budget_varied": Scenario(
        10.0, setup=_both(_budget_from_start(200.0), _variable_turnaround)
    ),
    **{
        f"high_budget_varied_keepalive_{ms}ms": Scenario(
            10.0,
            spec={"keepalive_seconds": ms / 1000},
            setup=_both(_budget_from_start(200.0), _variable_turnaround),
        )
        for ms in (40, 50, 60, 70, 80)
    },
}

#: A deadline that lands inside a burst: the third window's second memory
#: read of the ``paced`` run, plus a millisecond (derived from that run).
DEADLINE_SCENARIO = "deadline_mid_burst"


@contextmanager
def _runtime() -> Iterator[None]:
    try:
        yield
    finally:
        tuning_profile.set_paused(False)
        tuning_profile.configure_did_aliases({})
        tx_gate.set_passive(False, source="test")
        tuning_profile.set_active(False, source="test")
        bus_budget.reset()


def _execute(scenario: Scenario, *, seconds: float | None = None) -> dict[str, Any]:
    """Run one scenario on a fresh fake clock; its normalised trace."""
    states: list[Any] = []
    decisions: list[Any] = []
    with pytest.MonkeyPatch.context() as patch, _runtime():
        clock = FakeClock(monotonic=1000.0)
        install_clock(patch, clock)
        tick = can_obd_loop._CanObdLoop._tick
        decide = can_obd_loop._CanObdLoop._memory_burst

        def traced_tick(self: can_obd_loop._CanObdLoop) -> None:
            tick(self)
            state = _round(list(burst_state(self)))
            if not states or states[-1][1] != state:
                states.append([round(clock.monotonic(), DIGITS), state])

        def traced_decide(self: can_obd_loop._CanObdLoop, *args: Any) -> Any:
            out = decide(self, *args)
            entry = _round(_decision(out))
            if not decisions or decisions[-1][1] != entry:
                decisions.append([round(clock.monotonic(), DIGITS), entry])
            return out

        patch.setattr(can_obd_loop._CanObdLoop, "_tick", traced_tick)
        patch.setattr(can_obd_loop._CanObdLoop, "_memory_burst", traced_decide)

        tuning_profile.configure_did_aliases(did_alias_table(DIDS))
        tuning_profile.activate(_profile(**scenario.profile), source="test")
        bus = ScriptedEcu(clock, latency=LATENCY, session_gated=True, **scenario.ecu)
        run = _Run(clock=clock, bus=bus, stop=threading.Event())
        if scenario.setup is not None:
            scenario.setup(run)
        deliver = bus.send
        receive = bus.recv

        def send(msg: can.Message, timeout: float | None = None) -> None:
            data = bytes(msg.data)
            service = data[1] if len(data) >= 2 and data[0] >> 4 == 0 else -1
            if service >= 0:
                run.counts[service] = run.count(service) + 1
            if all(f(msg, service) for f in list(run.filters)):
                deliver(msg, timeout)
            else:
                bus.sent.append((clock.monotonic(), msg))

        def recv(timeout: float | None = None) -> can.Message | None:
            for hook in list(run.on_recv):
                hook(clock.monotonic())
            return receive(timeout)

        bus.send = send  # type: ignore[method-assign]
        bus.recv = recv  # type: ignore[method-assign]
        spec = _spec(**scenario.spec)
        if scenario.calibration:
            spec = dataclasses.replace(
                spec,
                calibration_id=CalibrationIdSpec(
                    local_identifier=0x10, request_id=0x7E0, needs_session=True
                ),
            )
        readings: list[ObdReading] = []
        stats = run_can_obd_loop(
            bus,  # type: ignore[arg-type]
            spec,
            on_frame=lambda _m: None,
            on_reading=readings.append,
            stop_event=run.stop,
            deadline_monotonic=clock.monotonic() + (seconds or scenario.seconds),
        )
    requests = [
        [round(at, DIGITS), bytes(msg.data).hex()]
        for at, msg in bus.sent
        if msg.data[0] >> 4 != 0x3
    ]
    return {
        "requests": requests,
        "readings": [[r.pid, _round(r.acquired_at), _round(float(r.timestamp))] for r in readings],
        "decisions": decisions,
        "states": states,
        "stats": {
            "requests_sent": stats.requests_sent,
            "readings_emitted": stats.readings_emitted,
            "request_timeouts": stats.request_timeouts,
            "negative_responses": stats.negative_responses,
        },
    }


def _deadline_seconds() -> float:
    requests = _execute(SCENARIOS["paced"])["requests"]
    memory = [at for at, data in requests if data[2:4] == "23"]
    # Three reads per window: the third window's second read.
    return float(memory[7]) + 0.001 - 1000.0


def trace(name: str) -> dict[str, Any]:
    if name == DEADLINE_SCENARIO:
        return _execute(SCENARIOS["paced"], seconds=_deadline_seconds())
    return _execute(SCENARIOS[name])


def summary(record: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"stats": record["stats"]}
    for key in ("requests", "readings", "decisions", "states"):
        blob = json.dumps(record[key], separators=(",", ":")).encode()
        out[key] = {"n": len(record[key]), "sha256": hashlib.sha256(blob).hexdigest()}
    return out


NAMES = (*SCENARIOS, DEADLINE_SCENARIO)


def dump(directory: str) -> None:
    """Every scenario's full trace as JSON, plus the digest file, into ``directory``."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    digests = {}
    for name in NAMES:
        record = trace(name)
        (out / f"{name}.json").write_text(json.dumps(record, indent=0))
        digests[name] = summary(record)
    (out / "digests.json").write_text(json.dumps(digests, indent=2, sort_keys=True) + "\n")


@pytest.mark.parametrize("name", NAMES)
def test_burst_trace_matches_the_pre_move_capture(name: str) -> None:
    golden = json.loads(GOLDEN.read_text())[name]
    assert summary(trace(name)) == golden
