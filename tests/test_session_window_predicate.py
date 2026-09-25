"""One "served in the Mode 22 window" predicate (roadmap R02.2).

Service-0x23 memory reads are gated by the same diagnostic session as Mode 22,
so every *window-semantics* site must treat them as window members. Four sites
tested ``kind == "did"`` instead: the loop's verdict, boundary-grid anchoring,
the cycle plan and the resolved plan's session overhead. Memory reads were then
skipped every other window, charged no session overhead, and left out of the
cycle a capture has to fit.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from src.collection import can_obd_collector, obd_scheduler, tuning_profile
from src.collection.capture_profiles import CaptureProfile
from src.collection.obd_scheduler import (
    CanObdSpec,
    DidSessionSpec,
    DidWindowController,
    _PollRequest,
    _PollRequestState,
    build_poll_requests,
)
from src.collection.obd_scheduler import cadence as scheduler_cadence
from src.config import CanBusConfig
from src.decode.dids import MemoryChannel

_TARGET = MemoryChannel(
    name="ECM_TARGET_EQUIV_RATIO",
    address=0xFFFF26BE,
    length=2,
    byte_length=2,
    scale=1 / 16384,
    unit="phi",
    confidence="high",
    verified_tune_revision="ZS44A-k106",
    sentinel_raw=0,
    sentinel_meaning="fuel cut / no AFR target",
)
_SESSION = DidSessionSpec(session=0xC0, window_interval_seconds=1.0, cycle_seconds=0.5)


def _spec() -> CanObdSpec:
    return CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=("RPM",),
        memory=(_TARGET,),
        did_session=_SESSION,
        dtc_poll_interval_seconds=0.0,
        service_poll_interval_seconds=0.0,
        capture_profiles_enabled=True,
    )


@pytest.fixture
def memory_profile() -> Iterator[CaptureProfile]:
    profile = CaptureProfile(
        name="mem-window",
        mode01={"RPM": 1.0},
        memory=(_TARGET.name,),
        did_cycle_seconds=0.5,
        storage_prefix="_capture-mem-window",
    )
    tuning_profile.activate(profile, source="test")
    try:
        yield profile
    finally:
        tuning_profile.set_active(False, source="test")


def _memory_index(requests: list[_PollRequest]) -> int:
    (index,) = [i for i, request in enumerate(requests) if request.kind == "memory"]
    return index


def test_memory_read_is_a_session_window_member() -> None:
    request = _PollRequest(
        kind="memory", service=0x23, identifier=0, request_id=0x7E0, channel="mem:X"
    )
    assert request.in_session_window
    pid = _PollRequest(kind="pid", service=0x01, identifier=0x0C, request_id=0x7DF, channel="RPM")
    assert not pid.in_session_window


def test_memory_read_is_anchored_to_the_window_grid(
    memory_profile: CaptureProfile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read taken 30 ms into its window must not push the next due time 30 ms
    past the next boundary: that is the "skips every other window" bug."""
    spec = _spec()
    requests = build_poll_requests(None, spec)
    request = requests[_memory_index(requests)]
    interval = obd_scheduler._request_interval_seconds(request, spec)
    state = _PollRequestState(next_due_at=100.0, last_requested_at=100.03)
    monkeypatch.setattr(scheduler_cadence, "time", SimpleNamespace(monotonic=lambda: 100.04))

    assert obd_scheduler._next_request_due_at(request, state, spec) == pytest.approx(
        100.0 + interval
    )


def test_memory_read_is_charged_session_overhead(memory_profile: CaptureProfile) -> None:
    spec = _spec()
    requests = build_poll_requests(None, spec)
    plan = obd_scheduler._resolved_polling_plan(
        requests, spec, pid_batch_max=1, did_batch_max=1, discovery_active=False
    )
    assert any(member["kind"] == "memory" for member in plan["members"])
    assert plan["diagnostic_requests_per_second"]["session_control"] == pytest.approx(1 / 0.5)


def test_memory_read_is_counted_in_the_cycle_plan(memory_profile: CaptureProfile) -> None:
    spec = _spec()
    requests = build_poll_requests(None, spec)
    states = [_PollRequestState() for _ in requests]
    controller = DidWindowController(_SESSION, [_memory_index(requests)], now=0.0)
    plan = can_obd_collector._cycle_plan_for(
        requests, states, spec, controller, 1.0, profile_gated=True, batch_max_pids=1
    )
    assert plan is not None
    assert plan.did_requests == 1


def test_attempted_memory_read_waits_for_the_next_window(memory_profile: CaptureProfile) -> None:
    spec = _spec()
    requests = build_poll_requests(None, spec)
    index = _memory_index(requests)
    controller = DidWindowController(_SESSION, [index], now=0.0)
    controller.phase = "window"
    verdict = can_obd_collector._loop_request_verdict(
        controller, True, set(), index, requests[index]
    )
    assert verdict.verdict == "deferred"
    assert verdict.reason == "attempted_this_window"


@pytest.mark.parametrize(
    "function",
    [
        can_obd_collector._loop_request_verdict,
        obd_scheduler._next_request_due_at,
        can_obd_collector._cycle_plan_for,
        obd_scheduler._resolved_polling_plan,
    ],
)
def test_window_semantics_sites_use_the_one_predicate(function: object) -> None:
    """The four window-semantics sites the review found; ``kind == "did"`` may
    remain only where it means Mode 22 *batching* (0x62 echo, DID widths)."""
    source = inspect.getsource(function)  # type: ignore[arg-type]
    lines = source.splitlines()
    for number, line in enumerate(lines):
        if 'kind == "did"' in line:
            context = " ".join(lines[number : number + 2]).lower()
            assert "width" in context or "batch" in context, line
