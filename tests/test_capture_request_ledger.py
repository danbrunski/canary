"""The capture ledger counts every send and miss of a capture-selected request (D03.70).

On four tuning captures the ``fuel_target`` RAM window went silent 10 times
for 2.5-4.5 s while the spark window kept reading, and nothing was left to say
whether the request was sent and missed (a timeout or NRC, then the miss
back-off) or never sent (scheduling, budget, a window that did not open). The
live per-request rows are cumulative since daemon start and were never stored.
These tests pin the per-capture record: how it is fed (one funnel, so no path
can skip it), what it keeps, how a long gap is attributed from it, and that a
sidecar from before it reads as ``unknown``, never ``not_sent``.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import logging
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import can
import pytest

from src.analysis.capture_coverage import (
    _carried_request_ledger,
    _lifetime_for,
    recompute_capture_quality,
)
from src.collection import bus_budget, can_obd_loop, capture_lifetime, tuning_profile, tx_gate
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.capture_lifetime import (
    LONG_GAP_CAPACITY,
    LONG_GAP_SECONDS,
    MISS_BATCH_FAILURE,
    MISS_BATCH_UNPARSED,
    MISS_EVENT_CAPACITY,
    MISS_LOG_INTERVAL_S,
    MISS_NEGATIVE_RESPONSE,
    MISS_TIMEOUT,
    CaptureLedger,
)
from src.collection.capture_profiles import CaptureProfileCatalog
from src.collection.capture_quality import (
    ACQUISITION_GAP,
    ATTEMPT_EDGE_SLACK_S,
    GAP_NOT_SENT,
    GAP_SENT_AND_MISSED,
    GAP_SENT_NO_RESULT,
    GAP_UNKNOWN,
    LEDGER_CARRIED,
    LEDGER_LIVE,
    LEDGER_UNAVAILABLE,
    ROWS_CLOCK_SLACK_S,
    _request_row,
    attribute_gap,
    evaluate_capture_quality,
)
from src.collection.obd_collector import ObdReading
from src.collection.obd_registry import MODE01_REGISTRY
from src.collection.obd_scheduler import (
    CanObdLoopStats,
    CanObdSpec,
    _initial_request_states,
    build_poll_requests,
    did_alias_table,
    note_attempt,
    note_miss,
    profile_did_union,
)
from src.config import CanBusConfig, CollectionConfig
from src.decode.dids import plan_memory_reads
from src.yaml_load import load_yaml
from tests.builders import CAPTURE_T0, capture_obd_record, write_capture_session
from tests.capture_round_fakes import FakeClock, ScriptedEcu, install_clock
from tests.test_capture_quality import (
    CLEAN,
    TUPLE,
    _scheduler,
    _tuples,
    _verified_losses,
)
from tests.test_capture_quality import (
    _profile as quality_profile,
)
from tests.test_memory_window_burst import (
    LATENCY,
    XTERRA_CATALOG,
    XTERRA_MODE06,
    _profile,
    _run,
    _spec,
)

WINDOW = "mem:fuel_target"
TARGET_WORD = "mem:ECM_TARGET_EQUIV_RATIO"
PERIOD_WORD = "mem:ECM_ENGINE_PERIOD"
LEDGER_LOG = "src.collection.capture_lifetime"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(monotonic=1000.0)


@pytest.fixture(autouse=True)
def runtime_reset() -> Iterable[None]:
    yield None
    tuning_profile.set_paused(False)
    tuning_profile.configure_did_aliases({})
    tx_gate.set_passive(False, source="test")
    tuning_profile.set_active(False, source="test")
    bus_budget.reset()


def _ledger(started: float = 100.0) -> CaptureLedger:
    ledger = CaptureLedger()
    ledger.begin(started, profile="tuning", generation=1, started_wall=1_700_000_000.0)
    return ledger


def _row(ledger: CaptureLedger, channel: str, *, now: float = 500.0) -> dict[str, Any]:
    snapshot = ledger.snapshot(now)
    assert snapshot is not None
    row: dict[str, Any] = snapshot.to_dict()["requests"][channel]
    return row


# ---------------------------------------------------------------------------
# The ledger: what it records
# ---------------------------------------------------------------------------


def test_a_timeout_and_an_nrc_on_a_memory_request_are_recorded_with_times() -> None:
    ledger = _ledger(100.0)
    members = (TARGET_WORD, PERIOD_WORD)
    for now in (101.0, 102.0, 103.0):
        ledger.attempt(WINDOW, now, kind="memory", members=members)
    ledger.miss(WINDOW, 102.0, kind=MISS_TIMEOUT, backoff_s=1.0, request_kind="memory")
    ledger.miss(
        WINDOW, 103.0, kind=MISS_NEGATIVE_RESPONSE, nrc=0x31, backoff_s=2.0, request_kind="memory"
    )
    row = _row(ledger, WINDOW)
    assert row["kind"] == "memory"
    assert row["members"] == [TARGET_WORD, PERIOD_WORD]
    assert (row["attempts"], row["misses"]) == (3, 2)
    assert row["misses_by_kind"] == {MISS_NEGATIVE_RESPONSE: 1, MISS_TIMEOUT: 1}
    assert row["last_nrc"] == 0x31
    assert (row["first_attempt_at"], row["last_attempt_at"]) == (1.0, 3.0)
    assert row["events"] == [
        {"at_s": 2.0, "kind": MISS_TIMEOUT, "nrc": None, "backoff_s": 1.0},
        {"at_s": 3.0, "kind": MISS_NEGATIVE_RESPONSE, "nrc": 0x31, "backoff_s": 2.0},
    ]
    assert row["events_dropped"] == 0 and row["events_complete_until_s"] is None


def test_the_status_view_keeps_counts_and_leaves_the_miss_list_out() -> None:
    ledger = _ledger()
    ledger.miss(WINDOW, 101.0, kind=MISS_TIMEOUT)
    snapshot = ledger.snapshot(200.0)
    assert snapshot is not None
    row = snapshot.to_dict(events=False)["requests"][WINDOW]
    assert "events" not in row and row["misses"] == 1 and row["events_recorded"] == 1


def test_an_unknown_miss_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown miss kind"):
        _ledger().miss(WINDOW, 101.0, kind="gremlins")


def test_nothing_is_recorded_outside_a_capture() -> None:
    ledger = CaptureLedger()
    ledger.attempt(WINDOW, 5.0)
    ledger.miss(WINDOW, 5.0, kind=MISS_TIMEOUT)
    assert ledger.snapshot(10.0) is None
    ledger.begin(100.0, profile="tuning", generation=1)
    assert _row_or_none(ledger, WINDOW) is None
    ledger.attempt(WINDOW, 99.0)  # before the capture started
    assert _row_or_none(ledger, WINDOW) is None


def _row_or_none(ledger: CaptureLedger, channel: str) -> dict[str, Any] | None:
    snapshot = ledger.snapshot(200.0)
    assert snapshot is not None
    rows: dict[str, dict[str, Any]] = snapshot.to_dict()["requests"]
    return rows.get(channel)


def test_a_new_capture_starts_an_empty_request_ledger() -> None:
    ledger = _ledger()
    ledger.miss(WINDOW, 101.0, kind=MISS_TIMEOUT)
    ledger.begin(300.0, profile="tuning", generation=2)
    assert _row_or_none(ledger, WINDOW) is None


def test_the_event_list_is_capped_and_says_how_far_it_can_be_trusted() -> None:
    ledger = _ledger(0.0)
    total = MISS_EVENT_CAPACITY + 5
    for i in range(total):
        ledger.miss(WINDOW, 10.0 + i, kind=MISS_TIMEOUT)
    row = _row(ledger, WINDOW, now=1000.0)
    assert row["misses"] == total  # the count stays exact
    assert len(row["events"]) == MISS_EVENT_CAPACITY == row["events_recorded"]
    assert row["events_dropped"] == 5
    # The first events are kept; the list is complete only up to the last one.
    assert row["events_complete_until_s"] == 10.0 + MISS_EVENT_CAPACITY - 1


def test_the_miss_log_is_rate_limited_per_request(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=LEDGER_LOG)
    ledger = _ledger(0.0)
    ledger.miss(WINDOW, 1.0, kind=MISS_NEGATIVE_RESPONSE, nrc=0x22, backoff_s=1.0)
    for now in (2.0, 3.0, 10.0, 30.9):
        ledger.miss(WINDOW, now, kind=MISS_TIMEOUT)
    assert len(caplog.records) == 1  # the first miss speaks; the next four do not
    first = caplog.records[0]
    assert WINDOW in first.getMessage() and "NRC 0x22" in first.getMessage()
    assert "1 miss(es) since the last report" in first.getMessage()
    # Another request is its own stream; this request speaks again once the
    # interval has passed, carrying the five since the first line.
    ledger.miss("RPM", 5.0, kind=MISS_TIMEOUT)
    ledger.miss(WINDOW, 1.0 + MISS_LOG_INTERVAL_S, kind=MISS_TIMEOUT)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 3
    assert "RPM" in messages[1]
    assert "5 miss(es) since the last report, 6 this capture" in messages[2]
    assert all(r.levelno == logging.WARNING for r in caplog.records)


def test_the_miss_is_logged_after_the_ledger_lock_is_released(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A handler that blocks (a slow disk, a lock of its own) must not hold the
    ledger's lock, which every observation on the recording path takes."""
    ledger = _ledger(0.0)
    held_while_logging: list[bool] = []

    class Probe(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            held_while_logging.append(ledger._lock.locked())

    probe = Probe(level=logging.WARNING)
    logger = logging.getLogger(LEDGER_LOG)
    logger.addHandler(probe)
    try:
        ledger.miss(WINDOW, 1.0, kind=MISS_TIMEOUT)
    finally:
        logger.removeHandler(probe)
    assert held_while_logging == [False]


# ---------------------------------------------------------------------------
# The funnel: both feeders go through the ledger
# ---------------------------------------------------------------------------


def _armed_requests() -> tuple[list[Any], list[Any], int]:
    """The plan's requests under an armed `burst` profile, with fresh states."""
    tuning_profile.configure_did_aliases(did_alias_table(("ECM_RPM", "ECM_AIR_CHARGE")))
    tuning_profile.activate(_profile(), source="test")
    requests = build_poll_requests(None, _spec())
    index = next(i for i, request in enumerate(requests) if request.kind == "memory")
    states = _initial_request_states(requests, _spec(), 0.0)
    return requests, states, index


def test_the_scheduler_funnel_records_attempts_timeouts_and_nrcs_of_a_memory_request() -> None:
    requests, states, index = _armed_requests()
    request, state = requests[index], states[index]
    assert tuning_profile.request_eligible(index, request)
    base = capture_lifetime.snapshot(0.0)
    assert base is not None
    started = base.started_monotonic
    for offset, kind, nrc in (
        (1.0, MISS_TIMEOUT, None),
        (2.0, MISS_NEGATIVE_RESPONSE, 0x31),
    ):
        note_attempt(index, request, state, started + offset)
        note_miss(index, request, state, now=started + offset, kind=kind, nrc=nrc)
    note_attempt(index, request, state, started + 3.0)
    snapshot = capture_lifetime.snapshot(started + 10.0)
    assert snapshot is not None
    row = snapshot.requests[request.channel]
    assert request.channel.startswith("mem:") and row.kind == "memory"
    assert (row.attempts, row.misses) == (3, 2)
    assert [(e.at_s, e.kind, e.nrc) for e in row.events] == [
        (1.0, MISS_TIMEOUT, None),
        (2.0, MISS_NEGATIVE_RESPONSE, 0x31),
    ]
    # Decoded channels ride with the request, so a gap in one of them can be
    # mapped to it even without the plan.
    assert any(name.startswith("mem:ECM_") for name in row.members)
    # The scheduler's own counters moved with it (one funnel, one event).
    assert (state.requests_sent, state.timeouts, state.negative_responses) == (3, 1, 1)
    assert state.last_negative_response_code == 0x31
    # The back-off recorded is the one the scheduler set on that miss.
    assert row.events[-1].backoff_s == pytest.approx(state.suppressed_until - (started + 2.0))
    assert row.events[-1].backoff_s > 0.0


def test_batch_sends_are_misses_without_a_backoff() -> None:
    requests, states, index = _armed_requests()
    request, state = requests[index], states[index]
    started = capture_lifetime.snapshot(0.0).started_monotonic  # type: ignore[union-attr]
    note_miss(index, request, state, now=started + 1.0, kind=MISS_BATCH_FAILURE, nrc=0x12)
    note_miss(index, request, state, now=started + 2.0, kind=MISS_BATCH_UNPARSED)
    snapshot = capture_lifetime.snapshot(started + 3.0)
    assert snapshot is not None
    events = snapshot.requests[request.channel].events
    assert [(e.kind, e.nrc, e.backoff_s) for e in events] == [
        (MISS_BATCH_FAILURE, 0x12, 0.0),
        (MISS_BATCH_UNPARSED, None, 0.0),
    ]
    assert state.batch_failures == 2 and state.timeouts == 0 and state.suppressed_until == 0.0
    assert state.last_negative_response_code == 0x12


def test_a_request_the_capture_did_not_select_is_not_recorded() -> None:
    requests, states, _index = _armed_requests()
    index = next(i for i, r in enumerate(requests) if not tuning_profile.request_eligible(i, r))
    started = capture_lifetime.snapshot(0.0).started_monotonic  # type: ignore[union-attr]
    note_attempt(index, requests[index], states[index], started + 1.0)
    note_miss(index, requests[index], states[index], now=started + 1.0, kind=MISS_TIMEOUT)
    snapshot = capture_lifetime.snapshot(started + 2.0)
    assert snapshot is not None and requests[index].channel not in snapshot.requests
    assert states[index].timeouts == 1  # the scheduler's own counter still moves


#: The `PollRequestState` fields only the funnel may write.
FUNNEL_FIELDS = frozenset(
    {
        "requests_sent",
        "timeouts",
        "negative_responses",
        "batch_failures",
        "last_negative_response_code",
        "last_requested_at",
    }
)
FUNNEL = {"note_attempt", "note_miss"}
#: `CanObdLoopStats` (the daemon-wide counters) shares three of those names and
#: is always reached as `stats` / `self.stats` in this package.
STATS_RECEIVERS = {"stats", "self.stats"}


def _funnel_field_writes(tree: ast.AST, path: str) -> list[str]:
    """Every store to a ``FUNNEL_FIELDS`` attribute (assign, augmented assign,
    annotated assign, tuple unpack, or ``setattr`` with a literal name) that is
    neither inside ``note_attempt`` / ``note_miss`` nor on a ``stats`` receiver."""
    found: list[str] = []

    def visit(node: ast.AST, function: str | None) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function = node.name
        stores: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            stores = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            stores = [node.target]
        for target in stores:
            for sub in ast.walk(target):
                if (
                    isinstance(sub, ast.Attribute)
                    and sub.attr in FUNNEL_FIELDS
                    and isinstance(sub.ctx, ast.Store)
                    and ast.unparse(sub.value) not in STATS_RECEIVERS
                    and not (path.endswith("obd_scheduler/cadence.py") and function in FUNNEL)
                ):
                    found.append(f"{path}:{sub.lineno} {ast.unparse(sub)}")
        if (
            isinstance(node, ast.Call)
            and ast.unparse(node.func) in {"setattr", "object.__setattr__"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in FUNNEL_FIELDS
        ):
            found.append(f"{path}:{node.lineno} {ast.unparse(node)}")
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, None)
    return found


def test_no_code_in_the_collection_package_moves_a_request_counter_but_the_funnel() -> None:
    """The ledger is fed from `note_attempt` / `note_miss` only. A send or a miss
    counted anywhere else is one the ledger never sees, which is how a window
    could go missing from the record again. Enforced by walking every module of
    `src/collection`: a store to a `PollRequestState` counter that is not inside
    those two functions, and whose receiver is not `stats` (`CanObdLoopStats`,
    the daemon-wide counters, which share three of the names), fails. An alias
    (`rs = request_state; rs.requests_sent += 1`) is caught because only the
    literal receivers `stats` and `self.stats` are exempt; the one thing it
    cannot catch is a request state bound to a variable named `stats`."""
    root = Path(can_obd_loop.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        offenders += _funnel_field_writes(ast.parse(path.read_text()), str(path.relative_to(root)))
    assert offenders == [], offenders
    # The exemption is real: those names are `CanObdLoopStats` fields.
    stats_fields = {f.name for f in dataclasses.fields(CanObdLoopStats)}
    assert {"requests_sent", "negative_responses", "batch_failures"} <= stats_fields


@pytest.mark.parametrize(
    "line",
    [
        "rs = request_state\nrs.requests_sent += 1",
        "state.timeouts += 1",
        "states[i].batch_failures = states[i].batch_failures + 1",
        "(a, state.negative_responses) = (1, 2)",
        "state.last_requested_at: float = 1.0",
        "setattr(state, 'last_negative_response_code', 5)",
    ],
)
def test_the_funnel_guard_catches_what_it_claims(line: str) -> None:
    assert _funnel_field_writes(ast.parse(line), "elsewhere.py")
    assert not _funnel_field_writes(ast.parse("stats.requests_sent += 1"), "elsewhere.py")


def test_the_loop_records_window_timeouts_and_batch_failures(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end on the fake clock: every 0x23 lost before the ECM (a timeout
    per window read), and Mode 01 batches ignored (a whole-batch failure per
    member), each on its own request's row."""
    install_clock(monkeypatch, clock)
    _run(clock, _profile(), 40.0, unseen_memory=True, ecu={"answer_batches": False})
    snapshot = capture_lifetime.snapshot(clock.monotonic())
    assert snapshot is not None
    memory = [row for row in snapshot.requests.values() if row.kind == "memory"]
    assert len(memory) == 3
    for row in memory:
        assert row.attempts >= 1 and row.misses == row.attempts
        assert set(row.misses_by_kind) == {MISS_TIMEOUT}
        assert all(e.backoff_s > 0 for e in row.events)
    batched = [row for row in snapshot.requests.values() if row.kind == "pid"]
    assert batched and any(MISS_BATCH_FAILURE in row.misses_by_kind for row in batched)


# ---------------------------------------------------------------------------
# Attribution: which kind of silence was a long gap
# ---------------------------------------------------------------------------


def _rows(
    sends: Iterable[float], misses: Iterable[float] = (), *, now: float = 600.0
) -> dict[str, dict[str, Any]]:
    """The request ledger of a capture that sent ``WINDOW`` at ``sends`` and
    missed (a timeout) at ``misses``; times are capture seconds."""
    ledger = _ledger(0.0)
    for at in sorted(sends):
        ledger.attempt(WINDOW, at, kind="memory", members=(TARGET_WORD, PERIOD_WORD))
    for at in misses:
        ledger.miss(WINDOW, at, kind=MISS_NEGATIVE_RESPONSE, nrc=0x78, backoff_s=1.0)
    snapshot = ledger.snapshot(now)
    assert snapshot is not None
    rows: dict[str, dict[str, Any]] = snapshot.to_dict()["requests"]
    return rows


def _every_half_second(start: float, stop: float) -> list[float]:
    return [start + 0.5 * i for i in range(int(round((stop - start) / 0.5)) + 1)]


#: The window is read every 0.5 s up to 10 s and from 14.5 s on; the channel's
#: gap is (10.0, 14.5). What the request's own sends did inside it is the case.
BEFORE = _every_half_second(0.0, 10.0)
AFTER = _every_half_second(14.5, 20.0)


def test_a_gap_containing_misses_is_sent_and_missed() -> None:
    rows = _rows([*BEFORE, 12.0, *AFTER], misses=[12.0])
    cause = attribute_gap(10.0, 14.5, ledger=rows, row=rows[WINDOW])
    assert cause["cause"] == GAP_SENT_AND_MISSED
    assert cause["misses_total"] == 1 and cause["misses_by_kind"] == {MISS_NEGATIVE_RESPONSE: 1}
    assert cause["misses"] == [
        {"at_s": 12.0, "kind": MISS_NEGATIVE_RESPONSE, "nrc": 0x78, "backoff_s": 1.0}
    ]
    assert cause["request"] == WINDOW


def test_a_gap_the_send_timeline_spans_is_not_sent() -> None:
    """Observed, not inferred: the request's own sends stop at 10.0 and resume
    at 14.5, so the channel had nothing to answer."""
    rows = _rows([*BEFORE, *AFTER])
    assert rows[WINDOW]["attempt_long_gaps"] == [[10.0, 14.5]]
    cause = attribute_gap(10.0, 14.5, ledger=rows, row=rows[WINDOW])
    assert cause["cause"] == GAP_NOT_SENT and "10.000s and 14.500s" in cause["reason"]
    # The answer that ends a gap follows its send by a turnaround, so the send
    # sits a few ms inside the gap's far edge: still the same gap.
    assert attribute_gap(10.0, 14.52, ledger=rows, row=rows[WINDOW])["cause"] == GAP_NOT_SENT
    # A request that was never sent at all, in the whole capture.
    assert attribute_gap(10.0, 14.5, ledger=rows, row=None)["cause"] == GAP_NOT_SENT


def test_sends_that_continue_with_no_miss_and_no_answer_are_sent_no_result() -> None:
    """A send that left no miss and no observation (it raised on the bus, or the
    window answered with a member undecoded) used to read as not_sent; the send
    timeline says the request did go out."""
    rows = _rows([*BEFORE, 12.0, *AFTER])  # a send at 12.0, never resolved
    row = rows[WINDOW]
    assert row["misses"] == 0 and row["attempt_long_gaps"] == [[10.0, 12.0], [12.0, 14.5]]
    cause = attribute_gap(10.0, 14.5, ledger=rows, row=row)
    assert cause["cause"] == GAP_SENT_NO_RESULT and "no miss and no answer" in cause["reason"]
    # Sends at a regular half-second all through the gap, none resolved.
    steady = _rows(_every_half_second(0.0, 20.0))
    assert steady[WINDOW]["attempt_long_gaps"] == []
    assert (
        attribute_gap(10.0, 14.5, ledger=steady, row=steady[WINDOW])["cause"] == GAP_SENT_NO_RESULT
    )


def test_a_send_timeline_that_overflowed_cannot_clear_a_gap_it_does_not_cover() -> None:
    sends = [3.0 * i for i in range(MISS_EVENT_CAPACITY + 6)]  # 3 s apart: all long gaps
    rows = _rows(sends)
    row = rows[WINDOW]
    assert len(row["attempt_long_gaps"]) == LONG_GAP_CAPACITY
    assert row["attempt_gaps_dropped"] == len(sends) - 1 - LONG_GAP_CAPACITY > 0
    last = (sends[-2], sends[-1])  # among the dropped
    cause = attribute_gap(*last, ledger=rows, row=row)
    assert cause["cause"] == GAP_UNKNOWN and "send timeline overflowed" in cause["reason"]
    # A gap a kept stretch covers is still answered.
    assert attribute_gap(0.0, 3.0, ledger=rows, row=row)["cause"] == GAP_NOT_SENT


def test_a_row_without_a_send_timeline_is_unknown() -> None:
    rows = _rows([*BEFORE, *AFTER])
    old = {k: v for k, v in rows[WINDOW].items() if not k.startswith("attempt_")}
    cause = attribute_gap(10.0, 14.5, ledger={WINDOW: old}, row=old)
    assert cause["cause"] == GAP_UNKNOWN and "no send timeline" in cause["reason"]


def _gap_with_send_stretch(
    stretch: tuple[float, float], *, slack_s: float = 0.0, gap: tuple[float, float] = (10.0, 14.5)
) -> str:
    """The cause for ``gap`` when the request's only long stretch with no sends
    is ``stretch`` (sends every half second either side of it, none missed)."""
    grid = _every_half_second(0.0, 20.0)
    sends = [
        *(t for t in grid if t < stretch[0]),
        stretch[0],
        stretch[1],
        *(t for t in grid if t > stretch[1]),
    ]
    rows = _rows(sends)
    assert rows[WINDOW]["attempt_long_gaps"] == [list(stretch)]
    return str(attribute_gap(*gap, ledger=rows, row=rows[WINDOW], slack_s=slack_s)["cause"])


def test_a_send_inside_the_gap_after_its_opening_is_not_the_gap_being_unsent() -> None:
    """The send that produced the gap's first observation is at or before it, so
    the stretch with no sends cannot begin 0.2 s into the gap: a send went out
    there (no miss, no answer), which is `sent_no_result`, not `not_sent`."""
    assert _gap_with_send_stretch((10.2, 14.5)) == GAP_SENT_NO_RESULT
    # In a rebuild from rows the clocks differ by up to the rows' slack, which
    # then applies at the opening edge too.
    assert _gap_with_send_stretch((10.2, 14.5), slack_s=ROWS_CLOCK_SLACK_S) == GAP_NOT_SENT
    # A stretch opening at or before the gap's start covers it.
    assert _gap_with_send_stretch((9.9, 14.5)) == GAP_NOT_SENT


def test_the_closing_edge_allows_one_response_turnaround_and_no_more() -> None:
    """The send that ends a gap precedes the answer that ends it: 0.2 s early is
    still that send, 1.0 s early is a send inside the gap."""
    assert ATTEMPT_EDGE_SLACK_S == 0.25
    assert _gap_with_send_stretch((10.0, 14.3)) == GAP_NOT_SENT  # 0.2 s before the end
    assert _gap_with_send_stretch((10.0, 13.6)) == GAP_SENT_NO_RESULT  # 0.9 s before it
    # A clock slack larger than the turnaround widens the closing edge too.
    assert _gap_with_send_stretch((10.0, 13.6), slack_s=1.0) == GAP_NOT_SENT


def test_the_miss_window_is_the_gap_itself_with_the_clock_slack() -> None:
    def cause(at: float, slack_s: float = 0.0) -> str:
        rows = _rows([*BEFORE, at, *AFTER], misses=[at])
        return str(
            attribute_gap(10.0, 14.5, ledger=rows, row=rows[WINDOW], slack_s=slack_s)["cause"]
        )

    assert cause(10.0) == GAP_SENT_AND_MISSED  # the gap's own edges are inside
    assert cause(14.5) == GAP_SENT_AND_MISSED
    assert cause(9.9) != GAP_SENT_AND_MISSED  # outside by 0.1 s ...
    assert cause(14.6) != GAP_SENT_AND_MISSED
    assert cause(9.9, ROWS_CLOCK_SLACK_S) == GAP_SENT_AND_MISSED  # ... inside by the slack
    assert cause(14.6, ROWS_CLOCK_SLACK_S) == GAP_SENT_AND_MISSED
    assert cause(9.7, ROWS_CLOCK_SLACK_S) != GAP_SENT_AND_MISSED


def test_a_decoded_channel_finds_its_request_by_members_when_the_plan_names_it_differently() -> (
    None
):
    rows = _rows([*BEFORE, *AFTER], misses=[12.0])
    request = {"channel": TARGET_WORD, "kind": "memory"}  # not the ledger's key
    found = _request_row(rows, request, TARGET_WORD)
    assert found is not None and found["channel"] == WINDOW
    assert _request_row(rows, request, "mem:ECM_UNRELATED") is None
    assert _request_row(rows, {"channel": WINDOW}, "anything") is rows[WINDOW]


def _recompute_with_a_carried_ledger(tmp_path: Path, *, miss_at: float) -> dict[str, Any]:
    """A capture rebuilt from its rows (`source: carried` ledger from the stop-time
    sidecar). The rows' target word is read every 0.5 s except 100-103.5 s, so
    the rebuilt gap is (99.506, 103.506); the ledger's miss is where ``miss_at``
    puts it. The rows' request is named after the decoded channel, not the
    window the ledger keys on, so the row is found by ``members``."""
    records = []
    for r in range(601):
        t = CAPTURE_T0 + r * 0.5
        records.append(capture_obd_record(t, "A", round_id=r))
        records.append(capture_obd_record(t + 0.003, "B", round_id=r))
        if not 100.0 <= r * 0.5 < 103.5:
            records.append(capture_obd_record(t + 0.006, TARGET_WORD, round_id=r))
    session = write_capture_session(
        tmp_path,
        records,
        profile={
            "memory": ["ECM_TARGET_EQUIV_RATIO"],
            "target_rate_hz": 2.0,
            "min_achieved_rate_hz": 1.5,
        },
    )
    ledger = _ledger(0.0)
    sends = [t for t in _every_half_second(0.5, 300.0) if not 99.0 < t < 104.0]
    for at in sends:
        ledger.attempt(WINDOW, at - 0.01, kind="memory", members=(TARGET_WORD,))
    ledger.miss(WINDOW, miss_at, kind=MISS_TIMEOUT, backoff_s=1.0, request_kind="memory")
    snapshot = ledger.snapshot(300.0)
    assert snapshot is not None
    stop = {
        "schema_version": 2,
        "interval": {"started_wall": CAPTURE_T0, "ended_wall": CAPTURE_T0 + 300.0},
        "request_ledger": {
            "source": LEDGER_LIVE,
            "event_capacity": MISS_EVENT_CAPACITY,
            "requests": snapshot.to_dict()["requests"],
        },
    }
    (session / "capture_quality.json").write_text(json.dumps(stop))
    quality = recompute_capture_quality(session)
    assert quality.request_ledger is not None and quality.request_ledger["source"] == LEDGER_CARRIED
    row = next(c for c in quality.channels if c.transport_channel == TARGET_WORD)
    (cause,) = row.coverage["long_gap_causes"]
    assert cause["start"] == pytest.approx(99.506, abs=0.001)
    result: dict[str, Any] = cause
    return result


def test_a_rebuild_from_rows_puts_a_miss_just_outside_the_rows_gap_inside_it(
    tmp_path: Path,
) -> None:
    """The ledger's monotonic clock and the rows' wall clock disagree by a
    fraction of a second; a miss 0.1 s before the rows' gap opens still belongs
    to it in a rebuild (and would not in a live score)."""
    cause = _recompute_with_a_carried_ledger(tmp_path, miss_at=99.4)
    assert cause["cause"] == GAP_SENT_AND_MISSED
    assert cause["request"] == WINDOW


def test_a_rebuild_does_not_widen_the_miss_window_past_the_slack(tmp_path: Path) -> None:
    cause = _recompute_with_a_carried_ledger(tmp_path, miss_at=99.2)
    assert cause["cause"] != GAP_SENT_AND_MISSED


def test_a_missing_ledger_is_unknown_never_not_sent() -> None:
    cause = attribute_gap(10.0, 14.0, ledger=None, row=None)
    assert cause["cause"] == GAP_UNKNOWN and "D03.70" in cause["reason"]
    assert attribute_gap(1.0, 4.0, ledger={}, row=None, kind="kline")["cause"] == GAP_UNKNOWN


def test_an_overflowed_miss_list_cannot_cover_a_gap_after_it() -> None:
    ledger = _ledger(0.0)
    for i in range(MISS_EVENT_CAPACITY + 3):
        ledger.miss(WINDOW, 1.0 + i * 0.1, kind=MISS_TIMEOUT)
    snapshot = ledger.snapshot(600.0)
    assert snapshot is not None
    rows = snapshot.to_dict()["requests"]
    complete_until = rows[WINDOW]["events_complete_until_s"]
    after = attribute_gap(complete_until + 5.0, complete_until + 9.0, ledger=rows, row=rows[WINDOW])
    assert after["cause"] == GAP_UNKNOWN and "overflowed" in after["reason"]
    # A gap inside the covered span is still answered.
    before = attribute_gap(0.5, 2.5, ledger=rows, row=rows[WINDOW])
    assert before["cause"] == GAP_SENT_AND_MISSED


def _quality(
    lifetime_requests: dict[str, Any] | None, *, source: str | None = None, in_gap: str
) -> Any:
    """300 s capture whose target word and engine period are read every 0.5 s
    except 100-103.5 s. The request is sent just before each read, and inside
    the gap ``in_gap`` says what it did: ``"miss"`` (a timeout at 101 s),
    ``"silent"`` (a send at 101.5 s that never resolved) or ``"none"`` (no
    send). ``lifetime_requests=None`` is a lifetime from before D03.70."""
    ledger = _ledger(0.0)
    t = 0.0
    while t <= 300.0 + 1e-9:
        for name in TUPLE:
            ledger.observe(name, t)
        if not 100.0 <= t < 103.5:
            ledger.observe(TARGET_WORD, t)
            ledger.observe(PERIOD_WORD, t)
            if t > 0:
                members = (TARGET_WORD, PERIOD_WORD)
                ledger.attempt(WINDOW, t - 0.01, kind="memory", members=members)
        elif t == 101.0 and in_gap == "miss":
            ledger.attempt(WINDOW, t, kind="memory", members=(TARGET_WORD, PERIOD_WORD))
            ledger.miss(WINDOW, t, kind=MISS_TIMEOUT, backoff_s=1.0, request_kind="memory")
        elif t == 101.5 and in_gap == "silent":
            ledger.attempt(WINDOW, t, kind="memory", members=(TARGET_WORD, PERIOD_WORD))
        t += 0.5
    ledger.miss(WINDOW, 250.0, kind=MISS_TIMEOUT, backoff_s=1.0, request_kind="memory")
    snapshot = ledger.snapshot(300.0)
    assert snapshot is not None
    lifetime = snapshot.to_dict()
    if lifetime_requests is None:
        for key in ("requests", "request_event_capacity"):
            lifetime.pop(key)  # a lifetime from before D03.70
    if source is not None:
        lifetime["requests_source"] = source
    scheduler = _scheduler(TUPLE)
    scheduler["requests"].append(
        {
            "channel": WINDOW,
            "kind": "memory",
            "capture_selected": True,
            "decoded_channels": [TARGET_WORD, PERIOD_WORD],
        }
    )
    return evaluate_capture_quality(
        profile=quality_profile(memory=["ECM_TARGET_EQUIV_RATIO", "ECM_ENGINE_PERIOD"]),
        scheduler=scheduler,
        lifetime=lifetime,
        tuples=_tuples(600, elapsed=300.0),
        losses=_verified_losses(CLEAN, CLEAN),
    )


def _target_row(quality: Any) -> Any:
    return next(c for c in quality.channels if c.transport_channel == TARGET_WORD)


def test_the_report_attributes_a_memory_windows_gap_through_the_plan() -> None:
    quality = _quality({}, in_gap="miss")
    row = _target_row(quality)
    (cause,) = row.coverage["long_gap_causes"]
    assert cause["cause"] == GAP_SENT_AND_MISSED
    assert (cause["start"], cause["end"]) == (99.5, 103.5)
    assert cause["misses"][0]["at_s"] == 101.0 and cause["misses_by_kind"] == {MISS_TIMEOUT: 1}
    # The failure names it too, beside the gap it explains.
    gap = next(f for f in quality.failures if f["clause"] == ACQUISITION_GAP)
    assert gap["evidence"]["ECM_TARGET_EQUIV_RATIO"]["long_gap_causes"] == [cause]
    # The block that lands in the sidecar.
    payload = quality.to_dict()["request_ledger"]
    assert payload["source"] == LEDGER_LIVE and payload["event_capacity"] == MISS_EVENT_CAPACITY
    assert payload["requests"][WINDOW]["misses"] == 2


def test_the_report_says_not_sent_when_nothing_was_sent_in_the_gap() -> None:
    quality = _quality({}, in_gap="none")
    (cause,) = _target_row(quality).coverage["long_gap_causes"]
    assert cause["cause"] == GAP_NOT_SENT


def test_the_report_says_sent_no_result_when_sends_continue_without_miss_or_answer() -> None:
    quality = _quality({}, in_gap="silent")
    (cause,) = _target_row(quality).coverage["long_gap_causes"]
    assert cause["cause"] == GAP_SENT_NO_RESULT
    block = quality.to_dict()["request_ledger"]["requests"][WINDOW]
    # 101.5 -> 103.49 is 1.99 s: under the long-gap floor, so not recorded.
    assert block["attempt_long_gaps"] == [[99.49, 101.5]]
    assert block["attempt_gaps_dropped"] == 0


def test_a_sidecar_from_before_the_ledger_attributes_every_gap_unknown() -> None:
    quality = _quality(None, in_gap="none")
    (cause,) = _target_row(quality).coverage["long_gap_causes"]
    assert cause["cause"] == GAP_UNKNOWN
    block = quality.to_dict()["request_ledger"]
    assert block["source"] == LEDGER_UNAVAILABLE and block["requests"] == {}


def test_a_report_without_the_field_is_read_as_unavailable() -> None:
    """Sidecars written before this field have no ``request_ledger`` key."""
    from src.storage.capture_manifest import normalize_quality_payload

    payload = _quality(None, in_gap="none").to_dict()
    payload.pop("request_ledger")
    assert normalize_quality_payload(payload).get("request_ledger") is None
    assert _carried_request_ledger({"schema_version": 2}) == {}
    assert _carried_request_ledger({"request_ledger": payload.get("request_ledger")}) == {}


def test_a_rebuild_from_rows_carries_the_stop_time_ledger() -> None:
    stop = _quality({}, in_gap="miss").to_dict()
    carried = _carried_request_ledger(stop)
    assert carried["requests_source"] == LEDGER_CARRIED
    assert carried["requests"][WINDOW]["misses"] == 2
    rebuilt = _lifetime_for(
        None,
        stop,
        {"lifetime_channels": {}},
        started_at=1_700_000_000.0,
        ended_at=1_700_000_300.0,
    )
    assert rebuilt["requests"] == carried["requests"]
    assert rebuilt["requests_source"] == LEDGER_CARRIED
    # An unavailable ledger is not carried as if it were an empty one.
    gone = _quality(None, in_gap="none").to_dict()
    assert _carried_request_ledger(gone) == {}
    assert "requests" not in _lifetime_for(
        None, gone, {"lifetime_channels": {}}, started_at=0.0, ended_at=1.0
    )


# ---------------------------------------------------------------------------
# The question the ledger exists to answer: can one miss make the hole?
# ---------------------------------------------------------------------------

#: Where in the run (seconds after the capture starts) the one memory read is lost.
LOSE_AT_S = 20.0
RUN_S = 60.0


#: What one fault on the ``fuel_target`` read does to it: ``"none"`` (control),
#: ``"timeout"`` (lost before the ECM, so the 1 s request timeout runs out),
#: ``"nrc"`` (the ECM refuses it at once, NRC ``REFUSAL_NRC``) or ``"bus_error"``
#: (the send raises, which ends the loop; it is started again at once).
REFUSAL_NRC = 0x31


def _one_lost_memory_read(
    monkeypatch: pytest.MonkeyPatch, *, fault: str, lose_at_s: float = LOSE_AT_S
) -> tuple[capture_lifetime.CaptureLifetime, list[ObdReading], float | None]:
    """The tracked Xterra `tuning` profile at its own 90 req/s, under the Pi's
    collection settings, on the fake clock (``test_memory_window_burst``'s
    harness); the ``fuel_target`` window's first read at or after ``lose_at_s``
    suffers ``fault``, exactly one read, no more."""
    lose = fault != "none"
    clock = FakeClock()
    install_clock(monkeypatch, clock)
    catalog = CaptureProfileCatalog.model_validate(load_yaml(XTERRA_CATALOG.read_text()))
    default, tuning = catalog.by_name("default"), catalog.by_name("tuning")
    assert default is not None and tuning is not None and tuning.memory
    collection = CollectionConfig(
        obd_source="canable",
        obd_pids=tuple(default.mode01),
        obd_dids=tuple(default.mode22),
        obd_batch_max_pids=6,
        obd_did_batch_max_dids=1,
        obd_request_timeout_seconds=1.0,
        obd_poll_rate_hz=1,
        storage_path=Path("unused"),
        session_retention_days=30,
        storage_warning_mb=500,
    )
    spec = dataclasses.replace(
        CanObdSpec.from_config(
            collection,
            CanBusConfig(interface="virtual", channel="tuning", bitrate=500000),
            catalog=catalog.profiles,
        ),
        calibration_id=None,
    )
    tuning_profile.configure_default(default)
    tuning_profile.configure_did_aliases(
        did_alias_table(profile_did_union(collection, catalog.profiles))
    )
    tuning_profile.activate(tuning, source="test")
    bus_budget.set_override(tuning.bus_budget_override, source="test", ttl_seconds=None)
    target_address = next(
        read.address
        for read in plan_memory_reads(spec.memory)
        if any(c.name == "ECM_TARGET_EQUIV_RATIO" for c in read.channels)
    )
    values = {pid: bytes([0x40]) * d.byte_length for pid, d in MODE01_REGISTRY.items()}
    values[0x0C] = b"\x0b\xb8"
    bus = ScriptedEcu(
        clock,
        latency=LATENCY,
        session_gated=True,
        values=values,
        dtc_responses={0x03: b"\x00", 0x07: b"\x00", 0x0A: b"\x00"},
        **XTERRA_MODE06,
    )
    deliver = bus.send
    lost_at: list[float] = []
    started = clock.monotonic()

    def send(msg: can.Message, timeout: float | None = None) -> None:
        data = bytes(msg.data)
        if (
            lose
            and not lost_at
            and clock.monotonic() >= started + lose_at_s
            and len(data) >= 8
            and data[1] == 0x23
            and int.from_bytes(data[2:6], "big") == target_address
        ):
            lost_at.append(clock.monotonic() - started)
            if fault == "bus_error":
                raise OSError("simulated bus send failure")
            bus.sent.append((clock.monotonic(), msg))
            if fault == "nrc":
                bus._queue([bytes([0x03, 0x7F, 0x23, REFUSAL_NRC, 0, 0, 0, 0])], clock.monotonic())
            return
        deliver(msg, timeout)

    bus.send = send  # type: ignore[method-assign]
    readings: list[ObdReading] = []
    deadline = clock.monotonic() + RUN_S
    for _start in range(2):  # a send that raises ends the loop; start it again
        try:
            run_can_obd_loop(
                bus,  # type: ignore[arg-type]
                spec,
                on_frame=lambda _m: None,
                on_reading=readings.append,
                stop_event=threading.Event(),
                deadline_monotonic=deadline,
            )
            break
        except OSError:
            assert fault == "bus_error"
            clock.advance(3.0)  # the collector's restart takes a moment
    snapshot = capture_lifetime.snapshot(clock.monotonic())
    assert snapshot is not None
    return snapshot, readings, lost_at[0] if lost_at else None


def _hole(lifetime: capture_lifetime.CaptureLifetime) -> float:
    held = lifetime.channels[TARGET_WORD].max_interarrival_s
    assert held is not None
    return held


def _largest_gap_cause(lifetime: capture_lifetime.CaptureLifetime) -> dict[str, Any]:
    gap_start, gap_end = max(
        lifetime.channels[TARGET_WORD].long_gaps, key=lambda gap: gap[1] - gap[0]
    )
    rows = lifetime.to_dict()["requests"]
    return attribute_gap(gap_start, gap_end, ledger=rows, row=rows[WINDOW])


def test_without_a_miss_the_fuel_target_window_reads_every_half_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the tests below: nothing lost, nothing recorded."""
    control, _readings, lost_at = _one_lost_memory_read(monkeypatch, fault="none")
    assert lost_at is None
    assert control.requests[WINDOW].misses == 0 and control.requests[WINDOW].attempts > 50
    assert _hole(control) < 1.0


#: Where one lost read falls against the window cycle, and the hole it leaves:
#: the 1 s request timeout, the capture's 1 s miss back-off, then the wait for
#: the next window slot. The sizes are discrete (3.10 s, or 4.05-4.07 s a cycle
#: later), not a range. They are what the model *reproduces*; none of the
#: truck's ten gaps (2.531-2.579 s six times, 3.064, 4.000, 4.057, 4.520 s) is
#: shown to be one lost read by this, and the 2.53-2.58 s cluster and the
#: 4.52 s gap are outside it (D03.70).
@pytest.mark.parametrize(("lose_at_s", "hole_s"), [(20.0, 3.097), (20.4, 4.050), (20.9, 4.065)])
def test_one_lost_fuel_target_read_holes_the_window_by_timeout_backoff_and_wait(
    monkeypatch: pytest.MonkeyPatch, lose_at_s: float, hole_s: float
) -> None:
    lost, _readings, lost_at = _one_lost_memory_read(
        monkeypatch, fault="timeout", lose_at_s=lose_at_s
    )
    assert lost_at is not None
    row = lost.requests[WINDOW]
    # Exactly the read that was lost: a timeout on the window's request, with
    # the capture policy's back-off (one second for a first miss, not 30).
    assert (row.misses, row.misses_by_kind) == (1, {MISS_TIMEOUT: 1})
    (event,) = row.events
    assert event.at_s >= lost_at
    assert event.backoff_s == pytest.approx(1.0)
    assert TARGET_WORD in row.members and PERIOD_WORD in row.members
    assert _hole(lost) == pytest.approx(hole_s, abs=0.02)
    # The hole is in the target word and in its neighbour in the window alike.
    assert lost.channels[PERIOD_WORD].max_interarrival_s == pytest.approx(_hole(lost), abs=0.01)
    # ... and the report built from it says why the gap is there.
    cause = _largest_gap_cause(lost)
    assert cause["cause"] == GAP_SENT_AND_MISSED and cause["misses_by_kind"] == {MISS_TIMEOUT: 1}


@pytest.mark.parametrize(("lose_at_s", "hole_s"), [(20.0, 2.001), (20.15, 1.989), (20.65, 2.001)])
def test_one_refused_fuel_target_read_holes_the_window_by_about_two_seconds(
    monkeypatch: pytest.MonkeyPatch, lose_at_s: float, hole_s: float
) -> None:
    """An NRC is answered at once, so there is no 1 s timeout to wait out: the
    hole is the 1 s back-off plus the wait for the next window slot, about 2 s,
    well short of the 2.53 s the truck's tightest cluster shows. One phase falls
    just under the 2 s long-gap floor and is not a long gap at all."""
    refused, _readings, lost_at = _one_lost_memory_read(
        monkeypatch, fault="nrc", lose_at_s=lose_at_s
    )
    assert lost_at is not None
    row = refused.requests[WINDOW]
    assert (row.misses, row.misses_by_kind) == (1, {MISS_NEGATIVE_RESPONSE: 1})
    (event,) = row.events
    assert event.nrc == REFUSAL_NRC and event.backoff_s == pytest.approx(1.0)
    assert _hole(refused) == pytest.approx(hole_s, abs=0.02)
    if hole_s >= LONG_GAP_SECONDS:
        cause = _largest_gap_cause(refused)
        assert cause["cause"] == GAP_SENT_AND_MISSED
        assert cause["misses"][0]["nrc"] == REFUSAL_NRC
    else:
        assert refused.channels[TARGET_WORD].long_gaps == ()


def test_a_send_that_raises_leaves_a_gap_attributed_sent_no_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one attempt-without-miss path in the loop: `_serve_single` counts
    the send, the bus raises, and the exception leaves before any outcome is
    decided (stop events and deadlines come back as an empty exchange, which is
    a timeout miss). The collector then restarts 3 s later, and the window's
    gap holds a send, no miss and no answer."""
    broken, _readings, lost_at = _one_lost_memory_read(monkeypatch, fault="bus_error")
    assert lost_at is not None
    row = broken.requests[WINDOW]
    assert row.misses == 0 and row.events == ()
    assert _hole(broken) > 3.0
    cause = _largest_gap_cause(broken)
    assert cause["cause"] == GAP_SENT_NO_RESULT, cause
