"""The scheduler-metrics contracts are structural (roadmap R02.11).

``capture_quality`` scopes the scheduler's cumulative batching counters to a
capture. It read them with ``int(block.get(name) or 0)``, so a renamed key
read as a verified zero, and nothing pinned the key sets.
``ObdSchedulerMetrics.to_dict`` hand-listed 27 keys and had already dropped a
field once (C06.12).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from src.collection import capture_quality, obd_scheduler
from src.collection.obd_scheduler import (
    CanObdLoopStats,
    CanObdSpec,
    ObdSchedulerMetrics,
    _batching_metrics,
)
from src.config import CanBusConfig


def _batching_block() -> dict[str, Any]:
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        batch_max_pids=6,
        did_batch_max_dids=2,
    )
    block = _batching_metrics(spec, CanObdLoopStats(), 1.0)
    assert block is not None
    return block


def test_the_consumer_reads_exactly_the_producers_counters() -> None:
    block = _batching_block()
    assert capture_quality.BATCHING_COUNTERS is obd_scheduler.BATCHING_COUNTERS
    assert capture_quality.MODE22_BATCHING_COUNTERS is obd_scheduler.MODE22_BATCHING_COUNTERS
    assert set(obd_scheduler.BATCHING_COUNTERS) <= set(block)
    mode22 = block["mode22"]
    assert isinstance(mode22, dict)
    assert set(obd_scheduler.MODE22_BATCHING_COUNTERS) <= set(mode22)
    # Every counter is an int the scheduler owns, not a derived ratio.
    assert all(isinstance(block[name], int) for name in obd_scheduler.BATCHING_COUNTERS)


def test_a_renamed_counter_is_refused_not_read_as_zero() -> None:
    block = _batching_block()
    renamed = {**block, "failures_renamed": block["failures"]}
    del renamed["failures"]
    scoped, notes = capture_quality.scope_batching(renamed, {"recorded": True, "batching": block})
    assert scoped is not None
    assert scoped["failures"] is None, "an absent counter must not read as a verified 0"
    assert scoped["desyncs"] == 0
    assert any("failures" in note and "not read as zero" in note for note in notes)
    mode22 = dict(block["mode22"])
    del mode22["fallbacks"]
    scoped, notes = capture_quality.scope_batching(
        {**block, "mode22": mode22}, {"recorded": True, "batching": block}
    )
    assert scoped is not None and scoped["mode22"]["fallbacks"] is None
    assert any("mode22.fallbacks" in note for note in notes)


def test_batching_off_throughout_is_still_none() -> None:
    assert capture_quality.scope_batching(None, {"recorded": True, "batching": None}) == (
        None,
        [],
    )


def test_to_dict_serializes_every_field() -> None:
    metrics = ObdSchedulerMetrics(
        configured_poll_rate_hz=1.0,
        bus_budget_requests_per_second=8.0,
        active_requests=3,
        backed_off_requests=0,
        requests_sent=0,
        readings_emitted=0,
        request_timeouts=0,
        negative_responses=0,
        bus_errors=0,
        response_rate=None,
        by_tier={},
    )
    payload = metrics.to_dict()
    names = {item.name for item in dataclasses.fields(ObdSchedulerMetrics)}
    assert set(payload) == names | {"active_requests_unfiltered"}
    assert payload["active_requests_unfiltered"] == payload["active_requests"] == 3
