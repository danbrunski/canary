"""D02 — counters that must reconcile, and fallbacks that must be visible.

Each test here pins a defect the 2026-09-10 drive surfaced, where a number
was reported confidently and nothing owned the difference. See
`docs/roadmap/tasks/D02-drive-findings.md` and
`data/analyses/2026-09-10-drive-h1-h7-h9.md`.
"""

from __future__ import annotations

import threading
import time

import pytest

from src.collection.can_obd_collector import (
    _initial_request_states,
    _issue_batched_request,
    build_poll_requests,
    run_can_obd_loop,
)
from src.collection.obd_scheduler import (
    CanObdSpec,
    _PollRequestState,
    _request_accounting_error,
    _request_sends_judged,
)
from src.config import CanBusConfig
from src.daemon.inference_metrics import InferenceTimingRecorder
from tests.test_multi_pid_batching import _BatchingEcu


def _spec(**kw) -> CanObdSpec:
    base = {
        "can": CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        "poll_rate_hz": 50.0,
        "request_timeout_seconds": 0.05,
    }
    base.update(kw)
    return CanObdSpec(**base)


# ---------------------------------------------------------------------------
# D02.1 — a send a failed batch swallowed must be named, not left over
# ---------------------------------------------------------------------------


def test_failed_batch_names_every_send_it_swallowed() -> None:
    """The drive's defect, minimised.

    `_issue_batched_request` counts a send for every member up front, then a
    whole-batch failure deliberately credits and penalises nobody. Before
    D02.1 that left `requests_sent - responses - timeouts` as an unnamed
    remainder, which read as "60 sent, 0 answered, 6 timeouts" on three
    healthy channels.
    """
    bus = _BatchingEcu(answer_batches=False)
    spec = _spec(pids=("RPM", "SPEED", "ENGINE_LOAD"), batch_max_pids=6)
    requests = build_poll_requests(bus, spec)  # type: ignore[arg-type]
    states = _initial_request_states(requests, spec, 0.0)
    indices = _batchable(requests, ("RPM", "SPEED", "ENGINE_LOAD"))

    outcome = _issue_batched_request(
        bus,  # type: ignore[arg-type]
        requests,
        states,
        indices,
        spec=spec,
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=threading.Event(),
        stats=_stats(),
        deadline_monotonic=time.monotonic() + 0.5,
    )

    assert outcome.answered is False
    for index in indices:
        state = states[index]
        assert state.requests_sent == 1
        assert state.batch_failures == 1, "the swallowed send must be named"
        assert state.responses == 0 and state.timeouts == 0
        assert _request_accounting_error(state) == 0


def test_failed_batch_does_not_back_off_its_members() -> None:
    """The no-penalty rule D02.1 must not change: a batch-level failure is no
    evidence about any member, and backing six healthy channels off on it is
    the bug the docstring warns about."""
    bus = _BatchingEcu(answer_batches=False)
    spec = _spec(pids=("RPM", "SPEED", "ENGINE_LOAD"), batch_max_pids=6)
    requests = build_poll_requests(bus, spec)  # type: ignore[arg-type]
    states = _initial_request_states(requests, spec, 0.0)
    indices = _batchable(requests, ("RPM", "SPEED", "ENGINE_LOAD"))

    _issue_batched_request(
        bus,  # type: ignore[arg-type]
        requests,
        states,
        indices,
        spec=spec,
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=threading.Event(),
        stats=_stats(),
        deadline_monotonic=time.monotonic() + 0.5,
    )

    for index in indices:
        state = states[index]
        assert state.missed_responses == 0, "a batch failure must penalise nobody"
        assert state.backoffs == 0
        assert state.suppressed_until == 0.0


def test_accounting_reconciles_over_a_whole_run_with_failing_batches() -> None:
    """End to end: every request in the published status payload reconciles
    after a real run in which batching fails and the loop recovers to
    single-PID polling. This is the assertion that would have caught the
    drive's defect at the point it is read, not at the point it is written."""
    bus = _BatchingEcu(answer_batches=False)
    seen: list = []
    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "SPEED", "ENGINE_LOAD"), batch_max_pids=6),
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=threading.Event(),
        deadline_monotonic=time.monotonic() + 0.8,
        on_scheduler_metrics=seen.append,
        scheduler_metrics_interval_seconds=0.05,
    )

    assert stats.batch_failures > 0, "this run is only meaningful if batches failed"
    assert seen, "expected at least one scheduler metrics publication"
    entries = [entry for metrics in seen for entry in metrics.to_dict()["requests"]]
    assert entries
    offenders = [
        (
            entry["channel"],
            entry["requests_sent"],
            entry["responses"],
            entry["timeouts"],
            entry["batch_failures"],
            entry["accounting_error"],
        )
        for entry in entries
        if entry["accounting_error"] != 0
    ]
    assert not offenders, f"sends with no owning counter: {offenders}"
    assert any(entry["batch_failures"] > 0 for entry in entries), (
        "the failing batches must show up as batch_failures somewhere"
    )


def test_response_rate_ignores_sends_a_failed_batch_swallowed() -> None:
    """A send nobody has evidence about must not drag a healthy channel's
    response rate down — that is what made three answering channels look
    dead."""
    state = _PollRequestState(requests_sent=60, responses=0, timeouts=6, batch_failures=54)

    assert _request_accounting_error(state) == 0
    assert _request_sends_judged(state) == 6


def test_accounting_error_is_non_zero_when_a_send_is_unowned() -> None:
    """The alarm itself: if a future change loses a send, this reports it."""
    state = _PollRequestState(requests_sent=60, responses=0, timeouts=6, batch_failures=0)

    assert _request_accounting_error(state) == 54


# ---------------------------------------------------------------------------
# D02.4a — one channel, one entry
# ---------------------------------------------------------------------------


def test_unknown_width_channels_are_unique_and_sorted() -> None:
    """Every Mode 06 MID is a separate plan member sharing the channel name
    `MONITOR_RESULTS`, so the drive's payload listed it twenty-plus times and
    anything sized from the list counted one channel many times over."""
    from src.collection.polling_plan import PlanMember, ResolvedPollingPlan

    # Six Mode 06 MIDs, all one channel, none with a known width — the shape
    # `MONITOR_RESULTS` has in the real plan.
    members = [
        PlanMember(
            kind="pid",
            service=0x06,
            request_id=0x7DF,
            identifier=mid,
            channel="MONITOR_RESULTS",
            profile_channel=None,
            rate_hz=0.1,
            response_payload_bytes=None,
        )
        for mid in range(0x21, 0x27)
    ] + [
        PlanMember(
            kind="pid",
            service=0x01,
            request_id=0x7DF,
            identifier=0x0C,
            channel="RPM",
            profile_channel=None,
            rate_hz=1.0,
            response_payload_bytes=2,
        )
    ]
    plan = ResolvedPollingPlan.build(
        members,
        bus_budget_requests_per_second=8.0,
        pid_batch_max=6,
        did_batch_max=1,
    )

    unknown = plan.to_dict()["raw_frames_per_second"]["unknown_response_width_channels"]

    assert unknown == ["MONITOR_RESULTS"], "six members of one channel must list that channel once"
    assert "RPM" not in unknown


# ---------------------------------------------------------------------------
# D02.4 — every DID the default profile polls must have a provenance-backed width
# ---------------------------------------------------------------------------

#: The `default` capture profile's Mode 22 set. Multi-DID batching cannot pack
#: a request it cannot parse, so a DID here without a width silently pins
#: `batch_max.mode22` at 1 — which is why the 2026-09-10 drive made zero
#: batched Mode 22 requests in 25 minutes.
DEFAULT_PROFILE_DIDS = (0x1219, 0x1208, 0x1225, 0x1226, 0x120F, 0x1210)

#: Measured from stored response payloads, D02.4. The first three were already
#: pinned from single-DID captures and are the known-answer check: the method
#: that produced the other four had to reproduce these exactly first.
PINNED_WIDTHS = {0x1101: 1, 0x1208: 2, 0x1219: 2, 0x1225: 2, 0x1226: 2, 0x120F: 2, 0x1210: 2}


def test_every_default_profile_did_has_a_width() -> None:
    from src.collection.obd_scheduler import did_response_widths

    widths, sources = did_response_widths()

    missing = [f"0x{did:04X}" for did in DEFAULT_PROFILE_DIDS if did not in widths]
    assert not missing, f"DIDs with no provenance-backed response width: {missing}"
    unsourced = [f"0x{did:04X}" for did in DEFAULT_PROFILE_DIDS if not sources.get(did)]
    assert not unsourced, f"a width without a source is a guess: {unsourced}"


def test_measured_widths_match_what_was_recorded() -> None:
    """A width is a measured constraint, so drift in `data/dids.yaml` is a
    regression, not an edit."""
    from src.collection.obd_scheduler import did_response_widths

    widths, _ = did_response_widths()

    for did, expected in PINNED_WIDTHS.items():
        assert widths.get(did) == expected, f"0x{did:04X} width changed"


# ---------------------------------------------------------------------------
# D02.3 — worker fallbacks must be visible in the snapshot
# ---------------------------------------------------------------------------


def test_worker_fallbacks_reach_the_snapshot() -> None:
    """A wedged child was visible only as a large `compute_wait` max; the
    2026-09-10 stall recorded `compute_failures: 0` while falling back."""
    recorder = InferenceTimingRecorder()
    recorder.set_worker_fallbacks({"timeout": 3, "pipe_error": 1})

    payload = recorder.snapshot().to_dict()

    assert payload["worker_fallbacks"] == {"timeout": 3, "pipe_error": 1}


def test_slow_computes_are_counted_separately_from_misses() -> None:
    """A compute that outran its tick is not a miss — the result is applied —
    but it is why the next tick could not start."""
    recorder = InferenceTimingRecorder()
    recorder.note_slow_compute()
    recorder.note_slow_compute()

    payload = recorder.snapshot().to_dict()

    assert payload["slow_computes"] == 2
    assert payload["missed_ticks"] == 0


def test_snapshot_defaults_are_empty_without_a_worker() -> None:
    """No worker configured must read as "nothing to report", not as a fault."""
    payload = InferenceTimingRecorder().snapshot().to_dict()

    assert payload["worker_fallbacks"] == {}
    assert payload["slow_computes"] == 0


def test_worker_timeout_is_derived_from_the_tick_not_a_flat_30s() -> None:
    """30 s on a 1 s tick is thirty consecutive `compute_busy` misses before
    the fallback even starts. The startup allowance still applies until the
    child has answered once, because it loads artifacts on that tick."""
    from src.daemon.inference_worker import (
        MIN_RESULT_TIMEOUT_SECONDS,
        STARTUP_RESULT_TIMEOUT_SECONDS,
        InferenceWorker,
    )

    worker = InferenceWorker(object(), result_timeout_seconds=3.0)

    assert worker.result_timeout_seconds == STARTUP_RESULT_TIMEOUT_SECONDS
    worker._answered_once = True
    assert worker.result_timeout_seconds == 3.0

    floored = InferenceWorker(object(), result_timeout_seconds=0.01)
    floored._answered_once = True
    assert floored.result_timeout_seconds == MIN_RESULT_TIMEOUT_SECONDS


def _batchable(requests, channels: tuple[str, ...]) -> list[int]:
    """Indices of `channels` in plan order — `build_poll_requests` adds auto
    PIDs and DTC reads, and a batch is capped at six members."""
    wanted = {
        channel: index for index, r in enumerate(requests) if (channel := r.channel) in channels
    }
    return sorted(wanted.values())


def _stats():
    from src.collection.obd_scheduler import CanObdLoopStats

    return CanObdLoopStats()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])


# ---------------------------------------------------------------------------
# D02.5 — a spec the pipeline cannot satisfy must say so, not just report 1.0
# ---------------------------------------------------------------------------


def _reconciliation(*, expected: int, absent: int):
    from src.preprocessing.feature_pipeline import FeatureSpecReconciliation

    columns = tuple(f"decoded:CH_{i}" for i in range(expected))
    return FeatureSpecReconciliation(
        row_count=1,
        expected_feature_columns=columns,
        missing_feature_columns=columns[:absent],
        extra_feature_columns=(),
        filled_feature_columns=columns[:absent],
        coerced_feature_columns=(),
        output_columns=columns,
    )


def test_structural_blindness_names_the_drive_s_case() -> None:
    """The 2026-09-10 shape exactly: 6 of 88 features absent is 6.8 %, over
    the 5 % ceiling, so the detector withholds every row forever."""
    from src.models.feature_spec_health import structural_blindness

    verdict = structural_blindness(_reconciliation(expected=88, absent=6))

    assert verdict is not None
    assert verdict["absent_count"] == 6
    assert verdict["expected_count"] == 88
    assert verdict["absent_fraction"] == pytest.approx(0.0682, abs=1e-4)
    assert verdict["scores_nothing"] is True


def test_a_little_absence_is_not_blindness() -> None:
    """Two absent of 88 is 2.3 %, under the ceiling — those rows still score,
    imputed to the training mean. Calling that blind would be false alarm."""
    from src.models.feature_spec_health import structural_blindness

    verdict = structural_blindness(_reconciliation(expected=88, absent=2))

    assert verdict is not None
    assert verdict["scores_nothing"] is False


def test_a_satisfiable_spec_reports_nothing() -> None:
    from src.models.feature_spec_health import structural_blindness

    assert structural_blindness(_reconciliation(expected=88, absent=0)) is None


def test_blindness_verdict_reaches_the_status_snapshot() -> None:
    """`blind_modes` said a detector scored nothing; this says why."""
    from src.daemon import inference as inference_mod
    from src.models.feature_spec_health import (
        _STRUCTURAL_BLINDNESS,
        structural_blindness_snapshot,
    )

    _STRUCTURAL_BLINDNESS.clear()
    inference_mod._SCHEMA_RECONCILIATION_WARNED.clear()
    try:
        inference_mod._log_schema_reconciliation_once(
            "isolation_forest", "v-2026-07-27", _reconciliation(expected=88, absent=6)
        )
        snapshot = structural_blindness_snapshot()

        assert "isolation_forest" in snapshot
        assert snapshot["isolation_forest"]["model_version"] == "v-2026-07-27"
        assert snapshot["isolation_forest"]["scores_nothing"] is True

        # A later, satisfiable version clears the verdict rather than leaving
        # a stale alarm behind.
        inference_mod._log_schema_reconciliation_once(
            "isolation_forest", "v-next", _reconciliation(expected=88, absent=0)
        )
        assert structural_blindness_snapshot() == {}
    finally:
        _STRUCTURAL_BLINDNESS.clear()
        inference_mod._SCHEMA_RECONCILIATION_WARNED.clear()
