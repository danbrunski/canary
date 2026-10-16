"""R18: the memory burst has one owner, with no send path.

:class:`MemoryBurstPlanner` (``src/collection/memory_burst.py``) owns the
burst plan, the budget clock every request charges, slots spent and the
turnaround estimates the plan is sized from. ``_CanObdLoop`` composes it: it
supplies the time, the budget slot and the window's facts at one boundary
(``_memory_burst``) and keeps the bus. The bus-level parity evidence is
``tests/test_memory_burst_traces.py``; this file pins the boundary and walks
the planner's transitions on explicit times, without a bus or a clock.
"""

from __future__ import annotations

import ast
import inspect
import threading
from pathlib import Path

import pytest

from src.collection import can_obd_collector, can_obd_loop, memory_burst, tuning_profile
from src.collection.memory_burst import (
    BurstDecision,
    BurstPlan,
    BurstWindow,
    MemoryBurstPlanner,
)
from src.collection.obd_scheduler.requests import PollRequest
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock
from tests.test_memory_window_burst import DIDS, LATENCY, SLOT, _profile, _spec

COLLECTION = Path(__file__).resolve().parents[1] / "src" / "collection"
PACKAGE = "src.collection"

#: Everything the planner may import. No clock, no bus, no profile, no IO.
ALLOWED_IMPORTS = frozenset({"__future__", "dataclasses", "src.collection.obd_scheduler.requests"})

#: Named so a failure says what crept in, whatever ``ALLOWED_IMPORTS`` says.
FORBIDDEN_IMPORTS = (
    "time",
    "threading",
    "can",
    "src.collection.bus_budget",
    "src.collection.tuning_profile",
    "src.collection.tx_gate",
    "src.collection.can_obd_io",
    "src.collection.can_obd_client",
    "src.collection.can_obd_loop",
    "src.collection.isotp",
)

#: State R18 moved into the planner: the loop must not keep a copy of any of it.
MOVED_STATE = (
    "_burst",
    "slots_spent",
    "memory_indices",
    "_memory_read_seconds",
    "_paced_read_seconds",
    "_burst_reads_s",
    "_session_seconds",
)


def _imports(text: str, package: str = PACKAGE) -> set[str]:
    """Every module a source text imports, wherever the import sits
    (function bodies, ``if TYPE_CHECKING:``), relative ones resolved against
    ``package``; ``from pkg import name`` also reports ``pkg.name``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                target = node.module or ""
            else:
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                target = f"{base}.{node.module}" if node.module else base
            if target:
                found.add(target)
                found.update(f"{target}.{alias.name}" for alias in node.names)
    return found


def test_the_scanner_sees_relative_function_level_and_type_checking_imports() -> None:
    text = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .can_obd_loop import _CanObdLoop\n"
        "def f():\n"
        "    from . import tuning_profile\n"
        "    import time\n"
        "    from .obd_scheduler import requests\n"
    )
    found = _imports(text)
    assert {"src.collection.can_obd_loop", "src.collection.tuning_profile", "time"} <= found
    assert "src.collection.obd_scheduler.requests" in found


def test_the_planner_imports_nothing_that_could_send_sleep_or_read_a_profile() -> None:
    found = _imports((COLLECTION / "memory_burst.py").read_text())
    assert not [name for name in FORBIDDEN_IMPORTS if name in found]
    # Each import is an allowed module or a name imported from one.
    stray = {
        name
        for name in found
        if name not in ALLOWED_IMPORTS and name.rsplit(".", 1)[0] not in ALLOWED_IMPORTS
    }
    assert not stray, stray


def test_the_planner_never_touches_a_bus_or_a_clock() -> None:
    """No call or attribute that sends, receives, sleeps or reads a clock."""
    tree = ast.parse((COLLECTION / "memory_burst.py").read_text())
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attributes & {"send", "recv", "sleep", "monotonic", "time", "effective"}


def test_the_planner_api_takes_facts_not_the_loop() -> None:
    """Every public method takes explicit times, slots and window facts: no
    loop, controller, bus or generic callback (``eligible`` is the request
    filter the ordinary selection already uses)."""
    allowed = {
        "float",
        "int",
        "bool",
        "float | None",
        "BurstWindow | None",
        "RequestFilter | None",
        "frozenset[int]",
    }
    for name, method in inspect.getmembers(MemoryBurstPlanner, inspect.isfunction):
        if name.startswith("_") and name != "__init__":
            continue
        for param in inspect.signature(method).parameters.values():
            if param.name == "self":
                continue
            assert param.annotation in allowed, (name, param.name, param.annotation)


def _self_attributes(tree: ast.AST) -> list[tuple[str, bool]]:
    """``(name, is_store)`` for every ``self.<name>`` in a module."""
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            out.append((node.attr, isinstance(node.ctx, ast.Store)))
    return out


def test_the_loop_keeps_no_copy_of_the_moved_state() -> None:
    tree = ast.parse((COLLECTION / "can_obd_loop.py").read_text())
    attributes = _self_attributes(tree)
    assert not [name for name, _store in attributes if name in MOVED_STATE]
    # The budget clock is the planner's; the loop only reads it through a
    # property, and writes it nowhere.
    assert not [name for name, store in attributes if name == "next_budget_at" and store]
    assert isinstance(inspect.getattr_static(can_obd_loop._CanObdLoop, "next_budget_at"), property)
    assert not hasattr(can_obd_loop, "_BurstPlan") and not hasattr(can_obd_loop, "_BurstDecision")


def test_the_loop_entrypoints_are_unchanged() -> None:
    assert can_obd_collector.run_can_obd_loop is can_obd_loop.run_can_obd_loop
    for name in (
        "_memory_burst",
        "_session_budget_at",
        "keepalive_policy",
        "_after_burst_read",
        "_slot",
        "_spend_budget_slot",
    ):
        assert callable(getattr(can_obd_loop._CanObdLoop, name)), name
    assert BurstPlan.__module__ == BurstDecision.__module__ == memory_burst.__name__


@pytest.fixture
def clean_profile() -> object:
    yield None
    tuning_profile.configure_did_aliases({})
    tuning_profile.set_active(False, source="test")


def _memory_gaps(monkeypatch: pytest.MonkeyPatch, *, paced: bool) -> list[float]:
    from src.collection.obd_scheduler import did_alias_table

    if paced:
        monkeypatch.setattr(can_obd_loop._CanObdLoop, "_memory_burst", lambda *_a: None)
    clock = FakeClock(monotonic=1000.0)
    install_clock(monkeypatch, clock)
    tuning_profile.configure_did_aliases(did_alias_table(DIDS))
    tuning_profile.activate(_profile(), source="test")
    bus = ScriptedEcu(clock, latency=LATENCY, session_gated=True)
    can_obd_loop.run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(),
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=threading.Event(),
        deadline_monotonic=clock.monotonic() + 10.0,
    )
    memory = [at for at, msg in bus.sent if len(msg.data) > 1 and msg.data[1] == 0x23]
    assert len(memory) >= 6
    return [b - a for a, b in zip(memory, memory[1:], strict=False)]


def test_the_paced_reference_patch_still_turns_the_burst_off(clean_profile: object) -> None:
    """``test_memory_window_burst``'s 90 req/s comparison patches
    ``_CanObdLoop._memory_burst`` to ``None`` for its paced reference. After
    R18 that method is the loop's adapter into the planner, so the patch must
    still bite: paced reads one slot apart, against back to back without it."""
    with pytest.MonkeyPatch.context() as patch:
        paced = _memory_gaps(patch, paced=True)
    tuning_profile.set_active(False, source="test")
    with pytest.MonkeyPatch.context() as patch:
        burst = _memory_gaps(patch, paced=False)
    assert min(paced) >= SLOT - 1e-9
    assert min(burst) < SLOT / 4


# ---------------------------------------------------------------------------
# Planner transitions on explicit times
# ---------------------------------------------------------------------------

S = 0.125  # one slot at 8 req/s
T0 = 100.0
MEMORY = frozenset({5, 6, 7})
SESSION_S = 0.010


def _planner(*, session_s: float = SESSION_S) -> MemoryBurstPlanner:
    planner = MemoryBurstPlanner(MEMORY, now=T0)
    planner.on_session_control(session_s)
    return planner


def _window(
    members: frozenset[int] = MEMORY, *, others_owed: bool = False, deadline: float = T0 + 10.0
) -> BurstWindow:
    return BurstWindow(members=members, others_owed=others_owed, deadline=deadline)


def _request(kind: str = "did") -> PollRequest:
    return PollRequest(kind=kind, service=0x22, identifier=0x1201, request_id=0x7E0, channel="X")  # type: ignore[arg-type]


def test_spend_slot_charges_one_slot_never_into_the_past() -> None:
    planner = _planner()
    planner.spend_slot(T0, S)
    assert (planner.slots_spent, planner.next_budget_at) == (1, T0 + S)
    planner.spend_slot(T0 + 5.0, S)  # idle since: the clock does not bank slots
    assert (planner.slots_spent, planner.next_budget_at) == (2, T0 + 5.0)


def test_no_window_or_nothing_owed_is_no_plan_and_drops_one() -> None:
    planner = _planner()
    assert planner.decide(T0, S, None, None) is None and planner.plan is None
    planner.decide(T0, S, _window(), None)
    assert planner.plan is not None
    # Cancellation: the window closed (generation change, release, deadline).
    assert planner.decide(T0 + 0.01, S, None, None) is None
    assert planner.plan is None
    planner.decide(T0, S, _window(), None)
    assert planner.decide(T0 + 0.01, S, _window(frozenset()), None) is None
    assert planner.plan is None


def test_memory_waits_for_the_other_window_members() -> None:
    planner = _planner()
    planner.decide(T0, S, _window(), None)

    def eligible(index: int, _request: PollRequest) -> bool:
        return index != 2

    decision = planner.decide(T0, S, _window(others_owed=True), eligible)
    assert decision is not None
    assert (decision.issue_now, decision.hold_until, decision.paced_close_at) == (
        False,
        None,
        None,
    )
    assert planner.plan is None  # an owed DID resets the plan
    assert decision.outside(1, _request()) and not decision.outside(2, _request())
    assert not decision.outside(5, _request("memory"))
    assert decision.member(6, _request("memory")) and not decision.member(1, _request())
    assert planner.next_budget_at == T0


def test_a_prepaid_plan_holds_then_charges_every_read_at_once() -> None:
    planner = _planner()
    decision = planner.decide(T0, S, _window(), None)
    assert decision is not None and not decision.issue_now
    plan = planner.plan
    assert plan is not None and plan.prepaid and not plan.started
    # Before any burst: reads at the session control's turnaround, each paced
    # read a slot.
    paced_end = T0 + 3 * S
    assert plan.per_read == S
    assert plan.start_at == pytest.approx(paced_end - 3 * SESSION_S)
    assert decision.hold_until == plan.start_at
    assert planner.next_budget_at == T0  # nothing charged while it waits

    # A keep-alive spends a slot in the wait: start and charge move by it.
    planner.spend_slot(T0 + 0.1, S)
    held = planner.decide(T0 + 0.1, S, _window(), None)
    assert held is not None and held.hold_until == pytest.approx(plan.start_at + S)

    start = plan.start_at + S
    decision = planner.decide(start, S, _window(), None)
    assert decision is not None and decision.issue_now
    release = T0 + 3 * S + S
    assert planner.next_budget_at == pytest.approx(release)
    assert decision.paced_close_at == pytest.approx(release - S + SESSION_S)
    started = planner.plan
    assert started is not None and started.started and started.reads_done == 0
    # Later ticks of the same burst never re-close or re-charge.
    again = planner.decide(start + 0.001, S, _window(), None)
    assert again is not None and again.issue_now and again.paced_close_at is None
    assert planner.next_budget_at == pytest.approx(release)


def test_each_burst_read_is_charged_once_and_restores_the_clock() -> None:
    planner = _planner()
    planner.decide(T0, S, _window(), None)
    plan = planner.plan
    assert plan is not None
    planner.decide(plan.start_at, S, _window(), None)
    release = planner.next_budget_at
    spent = planner.slots_spent
    for n, seconds in enumerate((0.012, 1.0, 0.020), start=1):  # 1.0: a timeout or NRC wait
        # The loop's order: ``_serve_single`` spends its slot, then the read
        # is reported to the planner.
        planner.spend_slot(plan.start_at + n * 0.02, S)
        planner.after_read(seconds, S)
        current = planner.plan
        assert current is not None and current.reads_done == n
        assert current.others(planner.slots_spent) == 0
        assert planner.slots_spent == spent + n
        assert planner.next_budget_at == pytest.approx(release)
    reads = (0.012, 1.0, 0.020)
    assert planner.memory_read_seconds == pytest.approx(sum(reads) / 3)
    assert planner.paced_read_seconds == pytest.approx(sum(max(S, r) for r in reads) / 3)
    # Completion: nothing owed, the plan goes, the estimates stay.
    assert planner.decide(release, S, _window(frozenset()), None) is None
    assert planner.plan is None
    assert planner.memory_read_seconds == pytest.approx(sum(reads) / 3)


def test_the_next_plan_is_sized_from_the_last_burst() -> None:
    planner = _planner()
    planner.decide(T0, S, _window(), None)
    plan = planner.plan
    assert plan is not None
    planner.decide(plan.start_at, S, _window(), None)
    # Two slower than a slot, one faster: paced, each costs the longer.
    for seconds in (0.2, 0.1, 0.15):
        planner.spend_slot(plan.start_at, S)
        planner.after_read(seconds, S)
    planner.decide(T0 + 5.0, S, None, None)
    now = T0 + 6.0
    planner.decide(now, S, _window(), None)
    nxt = planner.plan
    assert nxt is not None
    first = max(planner.next_budget_at, now)
    assert nxt.first_slot == first
    paced = (0.2 + S + 0.15) / 3
    assert nxt.per_read == pytest.approx(paced)  # max(slot, paced-read mean)
    # Back to back the reads take their plain mean, so the burst starts that
    # much later than the paced span would have.
    assert nxt.start_at == pytest.approx(first + 3 * paced - 3 * 0.15)


def test_a_deadline_inside_the_wait_reads_at_once_charged_per_read() -> None:
    planner = _planner()
    decision = planner.decide(T0, S, _window(deadline=T0 + 0.3), None)
    assert decision is not None and decision.issue_now and decision.paced_close_at is None
    plan = planner.plan
    assert plan is not None and not plan.prepaid and plan.start_at == T0
    assert planner.next_budget_at == T0  # no up-front charge
    planner.spend_slot(T0, S)
    planner.after_read(0.01, S)
    assert planner.next_budget_at == T0 + S  # the read's own slot, as paced
    assert planner.keepalive_policy(T0, S, keepalive_due_at=T0, keepalive_seconds=2.0) == (
        True,
        0.0,
    )
    assert planner.session_budget_at(T0, window_open=True, exit_due=False) == T0 + S


def test_a_budget_change_moves_only_the_slots_charged_after_it() -> None:
    planner = _planner()
    planner.decide(T0, S, _window(), None)
    plan = planner.plan
    assert plan is not None
    planner.spend_slot(T0, S)
    fast = 1.0 / 20.0
    held = planner.decide(T0 + 0.01, fast, _window(), None)
    assert held is not None and held.hold_until == pytest.approx(plan.start_at + fast)
    assert plan.release(planner.slots_spent, fast) == pytest.approx(T0 + 3 * S + fast)


def test_keepalive_policy_while_waiting_and_mid_burst() -> None:
    planner = _planner()
    assert planner.keepalive_policy(T0, S, keepalive_due_at=T0, keepalive_seconds=2.0) == (
        True,
        0.0,
    )
    planner.decide(T0, S, _window(), None)
    plan = planner.plan
    assert plan is not None
    release = plan.release(planner.slots_spent, S)
    last_read_slot = release - S

    def policy(now: float, due: float | None, keepalive_s: float = 2.0) -> tuple[bool, float]:
        return planner.keepalive_policy(now, S, keepalive_due_at=due, keepalive_seconds=keepalive_s)

    # Waiting: due after pacing's last read slot is never sent; due by it goes.
    assert policy(T0, last_read_slot + 0.001) == (False, 0.0)
    assert policy(T0, None) == (False, 0.0)
    assert policy(T0, last_read_slot) == (True, 0.0)
    # About to start: one due within the burst goes now, led by its length.
    burst_s = 3 * SESSION_S
    assert policy(plan.start_at, last_read_slot) == (True, pytest.approx(burst_s + SESSION_S))
    # ...unless the burst is estimated at a whole keep-alive interval.
    assert policy(plan.start_at, last_read_slot, keepalive_s=burst_s) == (True, 0.0)

    planner.decide(plan.start_at, S, _window(), None)
    assert policy(plan.start_at, last_read_slot) == (True, 0.0)
    assert policy(plan.start_at, None) == (True, 0.0)
    assert policy(plan.start_at, last_read_slot + 0.001) == (False, 0.0)
    # Past the paced end (a slow or unanswered read) every due keep-alive goes.
    assert policy(release, last_read_slot + 0.001) == (True, 0.0)


def test_session_budget_while_a_prepaid_burst_runs() -> None:
    planner = _planner()
    planner.decide(T0, S, _window(), None)
    plan = planner.plan
    assert plan is not None
    now = plan.start_at
    # Waiting: the ordinary clock.
    assert planner.session_budget_at(now, window_open=True, exit_due=False) == T0
    planner.decide(now, S, _window(), None)
    release = planner.next_budget_at
    assert release > now
    # Running: a keep-alive need not wait for the prepaid clock...
    assert planner.session_budget_at(now, window_open=True, exit_due=False) == now
    # ...but the exit does, and so does anything outside the window.
    assert planner.session_budget_at(now, window_open=True, exit_due=True) == release
    assert planner.session_budget_at(now, window_open=False, exit_due=False) == release
