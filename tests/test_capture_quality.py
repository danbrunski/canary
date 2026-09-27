"""The capture contract: does a capture deliver what its profile promised?

Cases are drawn from the 2026-08-10 fuel-map capture (24 minutes, nothing
alarming reported, two usable delivery-error samples) and from the C03 review
counterexamples: a five-minute capture with samples only in its last minute
that passed, an old storage-loss incident attributed to every later capture,
and equal per-channel rates with zero joint coverage.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from src.analysis.capture_coverage import count_persisted_tuples
from src.collection.capture_lifetime import CaptureLedger, ChannelAccumulator
from src.collection.capture_quality import (
    ACQUISITION_GAP,
    PERSISTENCE_LOSS,
    RATE_SHORTFALL,
    TUPLE_INCOMPLETE,
    TUPLE_SKEW,
    UNANSWERED_INPUT,
    UNKNOWN_LOSS_COUNTERS,
    batching_baseline,
    evaluate_capture_quality,
    tuple_rate_bound_hz,
)
from src.storage.capture_losses import (
    RESET,
    UNKNOWN,
    VERIFIED,
    LossEpoch,
    reconcile_losses,
    summarize_losses,
)
from src.storage.capture_manifest import SCOPE_TRAILING, normalize_quality_payload
from src.telemetry import TelemetryRecord

TUPLE = ["RPM", "ENGINE_LOAD", "FUEL_STATUS", "COMMANDED_EQUIV_RATIO", "O2_WR_LAMBDA_S1"]


def _profile(**overrides: object) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "fuel-map",
        "mode01": {name: 1.0 for name in TUPLE} | {"COOLANT_TEMP": 0.25},
        "target_rate_hz": 4.0,
        "required_tuple": list(TUPLE),
        "min_achieved_rate_hz": 3.0,
        "round_timestamps": True,
    }
    base.update(overrides)
    return base


def _scheduler(channels: list[str], *, trailing_hz: dict[str, float] | None = None) -> dict:
    return {
        "capture_observed_seconds": 60.0,
        "achieved_hz_capture": dict(trailing_hz or {}),
        "capture_max_gap_seconds": {},
        "requests": [{"channel": c, "kind": "pid", "capture_selected": True} for c in channels],
        "batching": {"max_pids": 6, "mean_pids_per_batch": 5.2},
    }


def _lifetime(
    channels: dict[str, list[tuple[float, float, float]]],
    *,
    elapsed: float,
    pauses: list[list[float]] | None = None,
) -> dict[str, Any]:
    """``channels`` maps a name to ``[(start, stop, period), ...]`` windows of samples."""
    ledger = CaptureLedger()
    ledger.begin(0.0, profile="fuel-map", generation=1, started_wall=1_700_000_000.0)
    for name, windows in channels.items():
        for start, stop, period in windows:
            t = start
            while t <= stop + 1e-9:
                ledger.observe(name, t)
                t += period
    for start, end in pauses or []:
        ledger.mark_paused(True, reason="operator", now=start)
        ledger.mark_paused(False, reason="operator", now=end)
    snap = ledger.snapshot(elapsed)
    assert snap is not None
    return snap.to_dict()


def _healthy(elapsed: float = 300.0) -> dict[str, Any]:
    return _lifetime(
        {name: [(0.0, elapsed, 0.25)] for name in TUPLE} | {"COOLANT_TEMP": [(0.0, elapsed, 1.0)]},
        elapsed=elapsed,
    )


def _tuples(
    complete: int,
    elapsed: float = 300.0,
    *,
    windows: list[tuple[float, float, float]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    acc = ChannelAccumulator("__tuple__")
    if windows is None:
        for i in range(complete):
            acc.observe(i * 0.25)
    else:
        for start, stop, period in windows:
            t = start
            while t <= stop + 1e-9:
                acc.observe(t)
                t += period
    block = {
        "source": "persisted_rows",
        "attempted": complete,
        "complete": complete,
        "persisted": complete,
        "incomplete": 0,
        "skew_rejected": 0,
        "member_misses": dict.fromkeys(TUPLE, 0),
        "skew": {"p95_s": 0.01, "max_s": 0.02},
        "lifetime": acc.snapshot(elapsed).to_dict(),
    }
    block.update(overrides)
    return block


def _epoch(counters: dict[str, int | None], at: float = 0.0) -> LossEpoch:
    return LossEpoch(taken_at_monotonic=at, taken_at_wall=at, counters=counters)


def _verified_losses(start: dict[str, int | None], stop: dict[str, int | None]) -> dict[str, Any]:
    return summarize_losses(reconcile_losses(_epoch(start), _epoch(stop, 300.0)))


CLEAN = {"storage.dropped_records": 0, "storage.errors": 0, "can0.rx_dropped": 0}
CHANNELS = [*TUPLE, "COOLANT_TEMP"]


def _clauses(quality: Any) -> set[str]:
    return {f["clause"] for f in quality.failures}


# ---------------------------------------------------------------------------
# C03.1 regression (a): five-minute capture, samples only in the last minute
# ---------------------------------------------------------------------------


def test_a_five_minute_capture_with_samples_only_in_the_last_minute_fails() -> None:
    """Before C03 this reported 60 observed seconds, a 0.25 s gap, and pass."""
    lifetime = _lifetime(
        {name: [(240.0, 300.0, 0.25)] for name in CHANNELS},
        elapsed=300.0,
    )
    trailing = _scheduler(CHANNELS, trailing_hz=dict.fromkeys(CHANNELS, 4.0))
    trailing["capture_max_gap_seconds"] = dict.fromkeys(CHANNELS, 0.25)
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=trailing,
        lifetime=lifetime,
        tuples=_tuples(240, elapsed=300.0),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert quality.verdict == "fail"
    assert ACQUISITION_GAP in _clauses(quality)
    gap = next(f for f in quality.failures if f["clause"] == ACQUISITION_GAP)
    assert gap["evidence"]["RPM"]["missing_initial_s"] == pytest.approx(240.0)
    # The trailing window is still reported — labelled as what it is.
    assert quality.trailing["observed_seconds"] == 60.0
    assert quality.trailing["max_gap_seconds"]["RPM"] == 0.25
    rpm = next(row for row in quality.channels if row.channel == "RPM")
    assert rpm.trailing_hz == 4.0
    assert rpm.cadence["hz"] == pytest.approx(4.0)  # cadence while observing is fine
    assert rpm.coverage["fraction"] == pytest.approx(0.2)


def test_live_quality_uses_the_shared_resolved_plan_as_its_request_truth() -> None:
    scheduler = _scheduler([*CHANNELS, "UNSELECTED_STARTUP_MEMBER"])
    scheduler["resolved_plan"] = {
        "members": [
            {
                "channel": name,
                "profile_channel": name,
                "kind": "pid",
                "capture_selected": True,
                "configured_rate_hz": 4.0,
            }
            for name in CHANNELS
        ]
    }
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=scheduler,
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses(CLEAN, CLEAN),
    )

    assert "UNSELECTED_STARTUP_MEMBER" not in {row.channel for row in quality.channels}
    assert {row.channel for row in quality.channels} == set(CHANNELS)


# ---------------------------------------------------------------------------
# C03.1 regression (b): an earlier storage loss must not fail a later capture
# ---------------------------------------------------------------------------


def test_an_earlier_storage_loss_incident_is_not_attributed_to_a_later_clean_capture() -> None:
    before = {"storage.dropped_records": 0, "storage.errors": 0, "can0.rx_dropped": 0}
    after_incident = {"storage.dropped_records": 2491, "storage.errors": 1, "can0.rx_dropped": 0}
    first = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses(before, after_incident),
    )
    assert first.verdict == "fail"
    loss = next(f for f in first.failures if f["clause"] == PERSISTENCE_LOSS)
    assert loss["evidence"]["storage.dropped_records"]["delta"] == 2491

    later = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses(after_incident, after_incident),
    )
    assert later.verdict == "pass", later.failures
    assert later.losses["recording"]["status"] == VERIFIED
    assert later.losses["recording"]["lost"] == 0
    assert later.losses["recording"]["counters"]["storage.dropped_records"]["start"] == 2491


def test_a_capture_local_loss_fails_even_with_a_clean_final_minute() -> None:
    """The trailing window at stop was clean; the loss happened at minute two."""
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS, trailing_hz=dict.fromkeys(CHANNELS, 4.0)),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses(CLEAN, {**CLEAN, "storage.dropped_records": 40}),
    )
    assert quality.verdict == "fail"
    assert _clauses(quality) == {PERSISTENCE_LOSS}


def test_unknown_or_reset_counters_never_produce_a_verified_no_loss_claim() -> None:
    unknown = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=None,
    )
    assert unknown.verdict == "unverified"
    assert _clauses(unknown) == {UNKNOWN_LOSS_COUNTERS}
    assert unknown.losses["recording"]["status"] == UNKNOWN
    assert unknown.losses["recording"]["lost"] is None

    reset = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses({**CLEAN, "storage.dropped_records": 500}, CLEAN),
    )
    assert reset.verdict == "unverified"
    counter = reset.losses["recording"]["counters"]["storage.dropped_records"]
    assert counter["status"] == RESET and counter["delta"] is None

    missing_can0 = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses(
            {**CLEAN, "can0.rx_dropped": None}, {**CLEAN, "can0.rx_dropped": None}
        ),
    )
    assert missing_can0.verdict == "unverified"
    assert "can0.rx_dropped" in missing_can0.losses["recording"]["unverified"]


def test_dashboard_only_drops_are_reported_beside_recording_loss_not_inside_it() -> None:
    losses = _verified_losses(
        {**CLEAN, "broadcast.dropped_records": 0, "live.dropped_records": 0},
        {**CLEAN, "broadcast.dropped_records": 900, "live.dropped_records": 3},
    )
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=losses,
    )
    assert quality.verdict == "pass"
    assert quality.losses["dashboard"]["lost"] == 903
    assert quality.losses["recording"]["lost"] == 0
    assert any("dashboard-only" in note for note in quality.notes)


# ---------------------------------------------------------------------------
# C03.1 regression (c): equal marginal rates, disjoint rounds, zero tuples
# ---------------------------------------------------------------------------


def _obd(t: float, channel: str, *, generation: int = 1, round_id: int, value: float = 1.0):
    return TelemetryRecord.obd(
        t,
        channel,
        value,
        "",
        metadata={
            "ts_schema": 2,
            "acquired_at": t,
            "acquired_clock": "kernel",
            "generation": generation,
            "round_id": round_id,
        },
    )


def _frame(records: list[TelemetryRecord]) -> pd.DataFrame:
    return pd.DataFrame([r.parquet_row() for r in records])


T0 = 1_700_000_000.0


def test_equal_per_channel_rates_with_disjoint_rounds_yield_zero_tuples() -> None:
    """A answers in even rounds, B in odd: both 2 Hz, never together."""
    records: list[TelemetryRecord] = []
    for r in range(240):  # 60 s of 250 ms rounds
        t = T0 + r * 0.25
        records.append(_obd(t, "A" if r % 2 == 0 else "B", round_id=r))
    tuples = count_persisted_tuples(
        _frame(records), {"A": "A", "B": "B"}, max_skew_s=0.125, started_at=T0, ended_at=T0 + 60
    )
    assert tuples["attempted"] == 240
    assert tuples["complete"] == 0
    assert tuples["incomplete"] == 240
    assert tuples["member_misses"] == {"A": 120, "B": 120}

    lifetime = _lifetime({"A": [(0.0, 60.0, 0.5)], "B": [(0.25, 60.0, 0.5)]}, elapsed=60.0)
    profile = _profile(mode01={"A": 1.0, "B": 1.0}, required_tuple=["A", "B"], target_rate_hz=2.0)
    quality = evaluate_capture_quality(
        profile=profile,
        scheduler=_scheduler(["A", "B"]),
        lifetime=lifetime,
        tuples=tuples,
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert quality.verdict == "fail"
    assert _clauses(quality) == {TUPLE_INCOMPLETE}
    a = next(row for row in quality.channels if row.channel == "A")
    assert a.status == "ok" and a.cadence["hz"] == pytest.approx(2.0)
    # The bound is honest about being a bound: 2 Hz possible, 0 achieved.
    assert quality.tuple_rate_bound_hz == pytest.approx(2.0)
    assert quality.tuples["complete"] == 0


def test_a_partial_batch_response_is_a_member_miss_in_that_round_only() -> None:
    records: list[TelemetryRecord] = []
    for r in range(40):
        t = T0 + r * 0.25
        records.append(_obd(t, "A", round_id=r))
        if r != 17:
            records.append(_obd(t + 0.004, "B", round_id=r))
    tuples = count_persisted_tuples(
        _frame(records), {"A": "A", "B": "B"}, max_skew_s=0.125, started_at=T0, ended_at=T0 + 10
    )
    assert tuples["complete"] == 39
    assert tuples["member_misses"] == {"A": 0, "B": 1}
    assert tuples["lifetime"]["max_interarrival_s"] == pytest.approx(0.5)


def test_members_in_one_round_but_too_far_apart_are_skew_rejected() -> None:
    records: list[TelemetryRecord] = []
    for r in range(40):
        t = T0 + r * 0.25
        records.append(_obd(t, "A", round_id=r))
        records.append(_obd(t + 0.2, "B", round_id=r))
    tuples = count_persisted_tuples(
        _frame(records), {"A": "A", "B": "B"}, max_skew_s=0.125, started_at=T0, ended_at=T0 + 10
    )
    assert tuples["complete"] == 0
    assert tuples["skew_rejected"] == 40
    assert tuples["skew"]["max_s"] == pytest.approx(0.2)
    profile = _profile(
        mode01={"A": 1.0, "B": 1.0},
        required_tuple=["A", "B"],
        target_rate_hz=4.0,
        max_tuple_skew_s=0.125,
    )
    quality = evaluate_capture_quality(
        profile=profile,
        scheduler=_scheduler(["A", "B"]),
        lifetime=_lifetime({"A": [(0.0, 10.0, 0.25)], "B": [(0.2, 10.2, 0.25)]}, elapsed=10.0),
        tuples=tuples,
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert _clauses(quality) == {TUPLE_SKEW}
    assert quality.tuples["max_tuple_skew_s"] == 0.125


def test_rows_without_round_identity_are_no_evidence_not_zero_yield() -> None:
    legacy = [TelemetryRecord.obd(T0 + i, "A", 1.0, "") for i in range(10)]
    tuples = count_persisted_tuples(
        _frame(legacy), {"A": "A"}, max_skew_s=0.1, started_at=T0, ended_at=T0 + 10
    )
    assert tuples["source"] == "no_round_evidence"
    quality = evaluate_capture_quality(
        profile=_profile(mode01={"A": 1.0}, required_tuple=["A"], target_rate_hz=1.0),
        scheduler=_scheduler(["A"]),
        lifetime=_lifetime({"A": [(0.0, 10.0, 1.0)]}, elapsed=10.0),
        tuples=tuples,
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert quality.verdict == "unverified"
    assert quality.tuples["source"] == "no_round_evidence"
    assert any("tuple yield unverified" in note for note in quality.notes)


# ---------------------------------------------------------------------------
# Cadence, coverage and yield stay separate (C03.3 / C03.11)
# ---------------------------------------------------------------------------


def test_the_2026_08_10_rates_fail_as_a_rate_shortfall_with_evidence() -> None:
    slow = {
        "RPM": 0.78,
        "ENGINE_LOAD": 0.78,
        "FUEL_STATUS": 0.29,
        "COMMANDED_EQUIV_RATIO": 0.28,
        "O2_WR_LAMBDA_S1": 0.67,
        "COOLANT_TEMP": 0.29,
    }
    lifetime = _lifetime({n: [(0.0, 1434.0, 1.0 / hz)] for n, hz in slow.items()}, elapsed=1434.0)
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=lifetime,
        tuples=_tuples(2, elapsed=1434.0, attempted=400, incomplete=398),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert quality.verdict == "fail"
    assert RATE_SHORTFALL in _clauses(quality)
    shortfall = next(f for f in quality.failures if f["clause"] == RATE_SHORTFALL)
    assert shortfall["evidence"]["COMMANDED_EQUIV_RATIO"]["cadence_hz"] == pytest.approx(0.28)
    assert "budget" not in shortfall["detail"] and "lower" not in shortfall["detail"]
    assert quality.tuple_rate_bound_hz == pytest.approx(0.28)


def test_the_context_weight_is_part_of_the_promise_not_a_shortfall() -> None:
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    coolant = next(row for row in quality.channels if row.channel == "COOLANT_TEMP")
    assert coolant.requested_hz == 1.0
    assert coolant.status == "ok"
    assert quality.verdict == "pass"


def test_an_unanswered_channel_is_not_a_shortfall_and_silences_the_tuple_clause() -> None:
    lifetime = _healthy()
    profile = _profile()
    profile["mode01"] = {**profile["mode01"], "O2_WR_LAMBDA_S2": 1.0}
    quality = evaluate_capture_quality(
        profile=profile,
        scheduler=_scheduler([*CHANNELS, "O2_WR_LAMBDA_S2"]),
        lifetime=lifetime,
        tuples=_tuples(1200),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert quality.verdict == "fail"
    failure = next(f for f in quality.failures if f["clause"] == UNANSWERED_INPUT)
    assert failure["channels"] == ["O2_WR_LAMBDA_S2"]
    assert RATE_SHORTFALL not in _clauses(quality)

    # A missing *tuple member* is reported once, as unanswered, not as a rate.
    lifetime_missing = _lifetime(
        {n: [(0.0, 300.0, 0.25)] for n in CHANNELS if n != "COMMANDED_EQUIV_RATIO"}, elapsed=300.0
    )
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=lifetime_missing,
        tuples=_tuples(0, attempted=1200, incomplete=1200),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert _clauses(quality) == {UNANSWERED_INPUT}
    assert quality.tuple_rate_bound_hz is None


def test_a_pause_is_excluded_by_name_but_an_engine_off_gap_is_not() -> None:
    windows = [(0.0, 100.0, 0.25), (160.0, 300.0, 0.25)]
    paused = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_lifetime({n: windows for n in CHANNELS}, elapsed=300.0, pauses=[[100.0, 160.0]]),
        tuples=_tuples(960, windows=windows),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert paused.verdict == "pass", paused.failures
    assert paused.interval["paused_s"] == pytest.approx(60.0)
    assert paused.interval["intended_active_s"] == pytest.approx(240.0)
    assert paused.interval["pause_windows"] == [[100.0, 160.0]]

    engine_off = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_lifetime({n: windows for n in CHANNELS}, elapsed=300.0),
        tuples=_tuples(960, windows=windows),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert engine_off.verdict == "fail"
    assert ACQUISITION_GAP in _clauses(engine_off)
    rpm = next(row for row in engine_off.channels if row.channel == "RPM")
    assert rpm.coverage["long_gaps"] == [[100.0, 160.0, 60.0]]


def test_a_stall_older_than_the_trailing_window_and_a_missing_tail_both_fail() -> None:
    stalled = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_lifetime(
            {n: [(0.0, 60.0, 0.25), (150.0, 400.0, 0.25)] for n in CHANNELS}, elapsed=400.0
        ),
        tuples=_tuples(1200, elapsed=400.0),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert ACQUISITION_GAP in _clauses(stalled)
    tail = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_lifetime({n: [(0.0, 100.0, 0.25)] for n in CHANNELS}, elapsed=300.0),
        tuples=_tuples(400),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    gap = next(f for f in tail.failures if f["clause"] == ACQUISITION_GAP)
    assert gap["evidence"]["RPM"]["missing_final_s"] == pytest.approx(200.0)


def test_a_late_writer_failure_at_stop_is_a_persistence_loss() -> None:
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses(CLEAN, CLEAN),
        persistence={"flushed": False, "flush_error": "OSError: disk full", "rows_written": 9000},
    )
    assert quality.verdict == "fail"
    failure = next(f for f in quality.failures if f["clause"] == PERSISTENCE_LOSS)
    assert failure["evidence"]["flush_error"] == "OSError: disk full"

    short = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses(CLEAN, CLEAN),
        persistence={
            "flushed": True,
            "flush_error": None,
            "rows_written": 9000,
            "rows_on_disk": 8000,
        },
    )
    assert PERSISTENCE_LOSS in _clauses(short)


def test_too_short_an_interval_is_unverified_not_a_pass() -> None:
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(elapsed=1.0),
        tuples=_tuples(4, elapsed=1.0),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert quality.verdict == "unverified"
    assert any("minimum" in note for note in quality.notes)


def test_batching_off_is_called_out() -> None:
    scheduler = _scheduler(["RPM", "COOLANT_TEMP"])
    scheduler["batching"] = None
    quality = evaluate_capture_quality(
        profile=_profile(required_tuple=[]),
        scheduler=scheduler,
        lifetime=_lifetime(
            {"RPM": [(0.0, 60.0, 0.25)], "COOLANT_TEMP": [(0.0, 60.0, 1.0)]}, elapsed=60.0
        ),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert any("batching" in note for note in quality.notes)
    assert quality.tuples["source"] == "no_required_tuple"


def _batching(*, desyncs: int, failures: int, requests: int, loop: float = 12.5) -> dict:
    """The scheduler's cumulative batching block, as ``/status`` reports it."""
    return {
        "max_pids": 6,
        "batched_requests": requests,
        "batched_pids_requested": requests * 5,
        "mean_pids_per_batch": 5.0,
        "failures": failures,
        "desyncs": desyncs,
        "size_narrowings": 1,
        "failures_while_ecu_silent": 0,
        "channel_reads_per_second_lifetime": 40.0,
        "channel_reads_per_second": 40.0,
        "loop_started_monotonic": loop,
        "mode22": {
            "max_dids": 4,
            "known_response_widths": {"0x1208": {"bytes": 2, "source": "measured"}},
            "batched_requests": requests,
            "members_requested": requests * 3,
            "mean_dids_per_batch": 3.0,
            "failures": failures,
            "desyncs": desyncs,
            "fallbacks": 0,
            "failures_while_ecu_silent": 0,
        },
    }


def _scored_with_batching(start: dict[str, Any] | None, now: dict[str, Any] | None) -> Any:
    scheduler = _scheduler(TUPLE)
    scheduler["batching"] = now
    return evaluate_capture_quality(
        profile=_profile(),
        scheduler=scheduler,
        lifetime=_healthy(elapsed=100.0),
        tuples=_tuples(400, elapsed=100.0),
        losses=_verified_losses(CLEAN, CLEAN),
        batching_start=start,
    ).to_dict()["batching"]


def test_desyncs_from_an_earlier_capture_are_not_this_captures() -> None:
    """2026-09-18: a capture reported 102 desyncs, 99 of them from the capture
    before it on the same daemon — the lifetime block was copied verbatim."""
    at_arm = {**_scheduler(TUPLE), "batching": _batching(desyncs=99, failures=40, requests=1000)}
    now = _batching(desyncs=102, failures=41, requests=1400)
    block = _scored_with_batching(batching_baseline(at_arm), now)
    assert block["scope"] == "capture"
    assert block["status"] == VERIFIED
    assert block["desyncs"] == 3
    assert block["failures"] == 1
    assert block["batched_requests"] == 400
    assert block["mean_pids_per_batch"] == 5.0
    assert block["channel_reads_per_second"] == 20.0  # 2000 pids / 100 s, not the lifetime 40
    assert block["mode22"]["desyncs"] == 3
    assert block["mode22"]["batched_requests"] == 400
    # Settings are current values, apart from the counters.
    assert block["config"]["max_pids"] == 6
    assert block["config"]["mode22_max_dids"] == 4
    assert "0x1208" in block["config"]["known_response_widths"]
    # The cumulative totals survive only under a name that says so.
    assert block["lifetime"]["desyncs"] == 102


def test_batching_off_at_arm_is_a_zero_baseline() -> None:
    at_arm = {**_scheduler(TUPLE), "batching": None}
    block = _scored_with_batching(
        batching_baseline(at_arm), _batching(desyncs=2, failures=0, requests=50)
    )
    assert block["status"] == VERIFIED
    assert block["desyncs"] == 2


def test_a_counter_reset_mid_capture_is_flagged_never_negative() -> None:
    at_arm = {**_scheduler(TUPLE), "batching": _batching(desyncs=99, failures=40, requests=1000)}
    # The daemon restarted: every counter began again from zero.
    now = _batching(desyncs=2, failures=1, requests=30, loop=900.0)
    block = _scored_with_batching(batching_baseline(at_arm), now)
    assert block["status"] == "reset"
    assert "desyncs" in block["reset_counters"]
    assert block["desyncs"] is None
    assert block["mode22"]["desyncs"] is None
    for name in ("batched_requests", "failures", "size_narrowings"):
        assert block[name] is None
    assert block["lifetime"]["desyncs"] == 2


def test_a_restart_that_regrew_past_the_baseline_is_still_a_reset() -> None:
    """Counters higher than at arm are not a delta if their loop is new."""
    at_arm = {**_scheduler(TUPLE), "batching": _batching(desyncs=1, failures=1, requests=10)}
    now = _batching(desyncs=5, failures=3, requests=900, loop=900.0)
    block = _scored_with_batching(batching_baseline(at_arm), now)
    assert block["status"] == "reset"
    assert block["reset_counters"] == ["loop_started_monotonic"]
    assert block["desyncs"] is None


def test_no_arm_time_reading_is_unknown_not_the_lifetime_total() -> None:
    now = _batching(desyncs=102, failures=41, requests=1400)
    for start in (None, batching_baseline({})):
        block = _scored_with_batching(start, now)
        assert block["status"] == UNKNOWN
        assert block["desyncs"] is None
        assert block["lifetime"]["desyncs"] == 102


def test_the_sidecar_is_versioned_and_never_claims_an_exact_bound() -> None:
    payload = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses(CLEAN, CLEAN),
        scope="stop",
    ).to_dict()
    assert payload["schema_version"] == 2
    assert payload["scope"] == "stop"
    assert "tuple_bound_is_exact" not in payload
    assert "observed_seconds" not in payload
    assert payload["tuples"]["rate_bound_hz"] == payload["tuple_rate_bound_hz"]
    assert payload["tuples"]["complete"] == 1200


def test_a_v1_sidecar_reads_back_as_a_trailing_snapshot_with_unknown_losses() -> None:
    v1 = {
        "schema_version": 1,
        "verdict": "pass",
        "profile": "fuel-map",
        "failures": [],
        "notes": [],
    }
    out = normalize_quality_payload(v1)
    assert out["scope"] == SCOPE_TRAILING
    assert out["verdict"] == "pass"  # preserved, with its limited scope
    assert out["losses"]["recording"]["status"] == UNKNOWN
    assert any("trailing 60 s" in note for note in out["notes"])
    v2 = {"schema_version": 2, "scope": "stop", "verdict": "pass"}
    assert normalize_quality_payload(v2) == v2


def test_tuple_bound_is_the_slowest_member() -> None:
    assert tuple_rate_bound_hz({"A": 4.0, "B": 0.3}, ("A", "B")) == 0.3
    assert tuple_rate_bound_hz({"A": 4.0}, ("A", "B")) is None
    assert tuple_rate_bound_hz({"A": 4.0}, ()) is None


# ---------------------------------------------------------------------------
# DID captures keep their profile names and ignore DTC reads
# ---------------------------------------------------------------------------


def _did_scheduler() -> dict[str, Any]:
    def did(channel: str, name: str) -> dict[str, Any]:
        return {
            "channel": channel,
            "profile_channel": name,
            "kind": "did",
            "poll_tier": "fast",
            "capture_selected": True,
        }

    return {
        "capture_observed_seconds": 60.0,
        "achieved_hz_capture": {"DID_1209": 19.4, "DID_1208": 19.1, "DID_1219": 18.9},
        "requests": [
            did("DID_1209", "ECM_MAF_GPS"),
            did("DID_1208", "ECM_AIR_CHARGE"),
            did("DID_1219", "ECM_RPM"),
            did("DID_1209", "ECM_MAF_GPS"),  # a catalogue alias, listed twice
            {"channel": "DTC_STORED", "kind": "dtc", "poll_tier": "dtc", "capture_selected": True},
        ],
        "batching": {"max_pids": 3},
    }


def _did_profile() -> dict[str, Any]:
    return {
        "name": "fuel-did-research",
        "mode01": {},
        "mode22": ["ECM_MAF_GPS", "ECM_AIR_CHARGE", "ECM_RPM"],
        "target_rate_hz": 20.0,
        "required_tuple": ["ECM_RPM", "ECM_MAF_GPS", "ECM_AIR_CHARGE"],
        "min_achieved_rate_hz": 15.0,
        "round_timestamps": True,
    }


def test_a_did_capture_is_scored_under_the_profiles_own_channel_names() -> None:
    lifetime = _lifetime(
        {c: [(0.0, 120.0, 0.05)] for c in ("DID_1209", "DID_1208", "DID_1219")}, elapsed=120.0
    )
    tuples = _tuples(2400, elapsed=120.0)
    tuples["lifetime"] = ChannelAccumulator("__tuple__").snapshot(0.0).to_dict()
    acc = ChannelAccumulator("__tuple__")
    for i in range(2400):
        acc.observe(i * 0.05)
    tuples["lifetime"] = acc.snapshot(120.0).to_dict()
    quality = evaluate_capture_quality(
        profile=_did_profile(),
        scheduler=_did_scheduler(),
        lifetime=lifetime,
        tuples=tuples,
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert quality.verdict == "pass", quality.failures
    names = [row.channel for row in quality.channels]
    assert set(names) == {"ECM_MAF_GPS", "ECM_AIR_CHARGE", "ECM_RPM"}
    assert len(names) == len(set(names))
    assert not any(name.startswith("DTC_") for name in names)
    assert quality.tuple_rate_bound_hz == pytest.approx(20.0)


def test_did_context_weight_changes_only_its_own_rate_promise() -> None:
    profile = _did_profile() | {
        "mode22_rate_weights": {
            "ECM_MAF_GPS": 0.25,
        },
        "required_tuple": ["ECM_AIR_CHARGE", "ECM_RPM"],
    }
    lifetime = _lifetime(
        {
            "DID_1209": [(0.0, 20.0, 0.2)],  # requested 5 Hz, context only
            "DID_1208": [(0.0, 20.0, 0.05)],
            "DID_1219": [(0.0, 20.0, 0.05)],
        },
        elapsed=20.0,
    )
    quality = evaluate_capture_quality(
        profile=profile,
        scheduler=_did_scheduler(),
        lifetime=lifetime,
        tuples=_tuples(100, elapsed=20.0),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    by_name = {row.channel: row for row in quality.channels}
    assert by_name["ECM_MAF_GPS"].requested_hz == pytest.approx(5.0)
    assert by_name["ECM_MAF_GPS"].status == "ok"
    assert by_name["ECM_AIR_CHARGE"].requested_hz == pytest.approx(20.0)
    rate_failure = next(f for f in quality.failures if f["clause"] == RATE_SHORTFALL)
    assert "ECM_MAF_GPS" not in rate_failure["channels"]


# ---------------------------------------------------------------------------
# 2026-09-21: two usable tuning drives were called "fail"
# ---------------------------------------------------------------------------


def test_short_gaps_in_an_otherwise_complete_capture_are_degraded_not_failed() -> None:
    """A 20 s stall in a 300 s capture leaves every channel >= 90 % covered."""
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_lifetime(
            {n: [(0.0, 140.0, 0.25), (160.0, 300.0, 0.25)] for n in CHANNELS}, elapsed=300.0
        ),
        tuples=_tuples(1100),
        losses=_verified_losses(CLEAN, CLEAN),
    )
    assert ACQUISITION_GAP in _clauses(quality)
    assert quality.verdict == "degraded"
    assert any(note.startswith("degraded, not failed") for note in quality.notes)


def test_controller_overflow_is_reported_but_does_not_fail_the_capture() -> None:
    start = {**CLEAN, "can0.rx_over_errors": 113}
    stop = {**CLEAN, "can0.rx_over_errors": 221}
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses(start, stop),
    )
    assert PERSISTENCE_LOSS not in _clauses(quality)
    assert quality.losses["controller"]["lost"] == 108
    assert any("overflowed 108 frames" in note for note in quality.notes)


def test_socket_drops_still_fail_the_capture() -> None:
    """rx_dropped is CANary not keeping up — a recording loss, unlike controller overflow."""
    quality = evaluate_capture_quality(
        profile=_profile(),
        scheduler=_scheduler(CHANNELS),
        lifetime=_healthy(),
        tuples=_tuples(1200),
        losses=_verified_losses(CLEAN, {**CLEAN, "can0.rx_dropped": 5}),
    )
    assert quality.verdict == "fail"
    assert PERSISTENCE_LOSS in _clauses(quality)


# ---------------------------------------------------------------------------
# H05.8: capture quality learns the achieved K-line rate
# ---------------------------------------------------------------------------

KLINE_ENTRIES = ["0x1201", "ECM_TARGET_EQUIV_RATIO"]
KLINE_KEYS = ["kline:0x1201", "kline:ECM_TARGET_EQUIV_RATIO"]


def _kline_lifetime(
    samples: list[tuple[float, float, float]],
    *,
    elapsed: float = 300.0,
    source: str | None = None,
) -> dict[str, Any]:
    """CANary's own K-line session holds the ECM for the whole capture (the
    ``ecm-session-kline`` window tx_gate opens)."""
    ledger = CaptureLedger()
    ledger.begin(0.0, profile="kline-target", generation=1, started_wall=1_700_000_000.0)
    ledger.mark_paused(True, reason="ecm-session-kline", now=0.0)
    for key in KLINE_KEYS:
        for start, stop, period in samples:
            t = start
            while t <= stop + 1e-9:
                ledger.observe(key, t)
                t += period
    ledger.mark_paused(False, reason="ecm-session-kline", now=elapsed)
    snap = ledger.snapshot(elapsed)
    assert snap is not None
    payload = snap.to_dict()
    if source is not None:
        payload["source"] = source
    return payload


def _kline_quality(lifetime: dict[str, Any]) -> Any:
    return evaluate_capture_quality(
        profile={"name": "kline-target", "mode01": {}, "kline": list(KLINE_ENTRIES)},
        scheduler=_scheduler([]),
        lifetime=lifetime,
        losses=_verified_losses(CLEAN, CLEAN),
    )


def test_kline_entries_report_their_achieved_rate_from_the_ledger() -> None:
    quality = _kline_quality(_kline_lifetime([(0.0, 300.0, 0.1)]))
    rows = {row.channel: row for row in quality.channels}
    assert set(rows) == set(KLINE_KEYS)
    for row in rows.values():
        assert row.kind == "kline"
        assert row.requested_hz is None  # the profile names no K-line rate
        assert row.cadence["hz"] == pytest.approx(10.0)
        assert row.status == "ok"
        # Its own session's window is acquisition time, not a pause.
        assert row.coverage["eligible_s"] == pytest.approx(300.0)
        assert row.coverage["fraction"] == pytest.approx(1.0)
    assert quality.verdict == "pass", quality.failures
    assert any("achieved K-line rate" in note for note in quality.notes)


def test_a_kline_gap_inside_its_own_session_is_a_gap_not_a_pause() -> None:
    quality = _kline_quality(_kline_lifetime([(0.0, 100.0, 0.1), (160.0, 300.0, 0.1)]))
    assert ACQUISITION_GAP in _clauses(quality)
    row = next(r for r in quality.channels if r.channel == KLINE_KEYS[0])
    assert row.coverage["long_gaps"] == [[pytest.approx(100.0), 160.0, pytest.approx(60.0)]]


def test_a_passive_pause_is_still_a_pause_for_kline() -> None:
    """Passive armed stops K-line too; a window that is passive alone is
    excluded by name, exactly as for a Mode 01 channel."""
    lifetime = _kline_lifetime([(0.0, 100.0, 0.1), (160.0, 300.0, 0.1)])
    lifetime["pause_reasons"] = [
        {"start_s": 0.0, "end_s": 100.0, "reasons": ["ecm-session-kline"]},
        {"start_s": 100.0, "end_s": 160.0, "reasons": ["passive"]},
        {"start_s": 160.0, "end_s": 300.0, "reasons": ["ecm-session-kline"]},
    ]
    lifetime["pause_windows"] = [[0.0, 100.0], [100.0, 160.0], [160.0, 300.0]]
    quality = _kline_quality(lifetime)
    row = next(r for r in quality.channels if r.channel == KLINE_KEYS[0])
    assert row.status == "ok", row.coverage
    assert row.coverage["eligible_s"] == pytest.approx(240.0)
    assert quality.verdict == "pass", quality.failures


def test_a_passive_window_merged_into_the_kline_session_reports_a_gap_never_hides_one() -> None:
    """Armed mid-session, passive joins the open ``ecm-session-kline`` window
    (the ledger merges overlapping reasons), so its K-line part cannot be told
    apart. It stays eligible: the limitation can report a gap, never hide one."""
    ledger = CaptureLedger()
    ledger.begin(0.0, profile="kline-target", generation=1, started_wall=1_700_000_000.0)
    ledger.mark_paused(True, reason="ecm-session-kline", now=0.0)
    for key in KLINE_KEYS:
        for i in range(1001):
            ledger.observe(key, i * 0.1)  # 0..100 s
    ledger.mark_paused(True, reason="passive", now=100.0)
    ledger.mark_paused(False, reason="ecm-session-kline", now=100.0)
    ledger.mark_paused(False, reason="passive", now=300.0)
    snap = ledger.snapshot(300.0)
    assert snap is not None
    quality = _kline_quality(snap.to_dict())
    assert quality.interval["pause_windows"] == [[0.0, 300.0]]
    assert ACQUISITION_GAP in _clauses(quality)


def test_a_kline_entry_that_never_delivered_is_unanswered() -> None:
    quality = _kline_quality(_kline_lifetime([]))
    assert UNANSWERED_INPUT in _clauses(quality)
    assert quality.verdict == "fail"


def test_a_rows_rebuilt_verdict_says_it_did_not_score_kline() -> None:
    """The row rebuild reads Mode 01/22 only: K-line is named as unscored,
    never reported as unanswered."""
    from src.analysis.capture_coverage import SOURCE_ROWS
    from src.collection.capture_quality import ROWS_LIFETIME_SOURCE

    assert SOURCE_ROWS == ROWS_LIFETIME_SOURCE
    quality = _kline_quality(_kline_lifetime([], source=SOURCE_ROWS))
    assert quality.channels == []
    assert UNANSWERED_INPUT not in _clauses(quality)
    assert any("K-line channel(s) not scored" in note for note in quality.notes)


def test_kline_windows_without_reasons_fall_back_to_every_window() -> None:
    from src.collection.capture_quality import kline_pause_windows

    assert kline_pause_windows({"pause_windows": [[0.0, 5.0]]}, 10.0) is None
    assert kline_pause_windows({"pause_windows": [], "pause_reasons": []}, 10.0) == []
    assert kline_pause_windows(
        {"pause_reasons": [{"start_s": 1.0, "end_s": None, "reasons": ["passive"]}]}, 10.0
    ) == [(1.0, 10.0)]
