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

#: The `tuning-did` profile's Mode 22 set — all eleven measured on the H8 drive.
TUNING_DID_PROFILE_DIDS = (
    0x1219,
    0x1225,
    0x1226,
    0x1208,
    0x1135,
    0x113A,
    0x1163,
    0x1110,
    0x1139,
    0x120F,
    0x1101,
)

#: Measured from stored response payloads, D02.4. The first three were already
#: pinned from single-DID captures and are the known-answer check: the method
#: that produced the other four had to reproduce these exactly first.
PINNED_WIDTHS = {
    0x1101: 1,
    0x1110: 1,
    0x1135: 1,
    0x1139: 1,
    0x113A: 1,
    0x1163: 1,
    0x1208: 2,
    0x120F: 2,
    0x1210: 2,
    0x1219: 2,
    0x1225: 2,
    0x1226: 2,
}


def test_every_default_profile_did_has_a_width() -> None:
    from src.collection.obd_scheduler import did_response_widths

    widths, sources = did_response_widths()

    missing = [f"0x{did:04X}" for did in DEFAULT_PROFILE_DIDS if did not in widths]
    assert not missing, f"DIDs with no provenance-backed response width: {missing}"
    unsourced = [f"0x{did:04X}" for did in DEFAULT_PROFILE_DIDS if not sources.get(did)]
    assert not unsourced, f"a width without a source is a guess: {unsourced}"


def test_every_tuning_did_profile_did_has_a_width() -> None:
    """H8 measured all eleven on the drive, so multi-DID batching is no longer
    blocked for this profile either."""
    from src.collection.obd_scheduler import did_response_widths

    widths, sources = did_response_widths()

    missing = [f"0x{did:04X}" for did in TUNING_DID_PROFILE_DIDS if did not in widths]
    assert not missing, f"DIDs with no provenance-backed response width: {missing}"
    unsourced = [f"0x{did:04X}" for did in TUNING_DID_PROFILE_DIDS if not sources.get(did)]
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


def _report_missing(expected: tuple[str, ...], missing: tuple[str, ...]):
    from src.preprocessing.feature_pipeline import FeatureSpecReconciliation

    return FeatureSpecReconciliation(
        row_count=1,
        expected_feature_columns=expected,
        missing_feature_columns=missing,
        extra_feature_columns=(),
        filled_feature_columns=missing,
        coerced_feature_columns=(),
        output_columns=expected,
    )


@pytest.fixture
def _fresh_blindness():
    from src.daemon import inference as inference_mod
    from src.models.feature_spec_health import reset_blindness_tracking

    reset_blindness_tracking()
    inference_mod._SCHEMA_RECONCILIATION_WARNED.clear()
    yield inference_mod
    reset_blindness_tracking()
    inference_mod._SCHEMA_RECONCILIATION_WARNED.clear()


def _feed(inference_mod, version, report, *, start: float, seconds: float, step: float = 1.0):
    t = start
    while t <= start + seconds:
        inference_mod._log_schema_reconciliation_once("isolation_forest", version, report, now=t)
        t += step
    return t


def test_blindness_verdict_reaches_the_status_snapshot(_fresh_blindness) -> None:
    """`blind_modes` said a detector scored nothing; this says why — once the
    absence has held over a sustained window, not from one report."""
    from src.models.feature_spec_health import (
        STRUCTURAL_MIN_OBSERVED_SECONDS,
        structural_blindness_snapshot,
    )

    inference_mod = _fresh_blindness
    report = _reconciliation(expected=88, absent=6)
    _feed(inference_mod, "v-2026-07-27", report, start=0.0, seconds=STRUCTURAL_MIN_OBSERVED_SECONDS)
    snapshot = structural_blindness_snapshot()

    assert snapshot["isolation_forest"]["model_version"] == "v-2026-07-27"
    assert snapshot["isolation_forest"]["status"] == "established"
    assert snapshot["isolation_forest"]["absent_count"] == 6
    assert snapshot["isolation_forest"]["scores_nothing"] is True

    # A later, satisfiable version clears the verdict rather than leaving a
    # stale alarm behind.
    inference_mod._log_schema_reconciliation_once(
        "isolation_forest", "v-next", _reconciliation(expected=88, absent=0), now=500.0
    )
    assert structural_blindness_snapshot() == {}


def test_startup_sparsity_is_not_structural(_fresh_blindness, caplog) -> None:
    """The 2026-09-18 defect: the first window after start had no GPS fix and
    no slow-tier PIDs yet, and that was frozen as 17-of-88 "structural" for
    the whole drive. Only columns absent in *every* report are structural."""
    import logging

    from src.models.feature_spec_health import (
        STRUCTURAL_MIN_OBSERVED_SECONDS,
        structural_blindness_snapshot,
    )

    inference_mod = _fresh_blindness
    expected = tuple(f"obd:CH_{i}" for i in range(80)) + (
        "obd:BAROMETRIC_PRESSURE",
        "obd:COOLANT_TEMP",
        "gps:speed",
        "gps:speed__mean",
        "decoded:ECM_CALC_LOAD_B",
        "decoded:ECM_CALC_LOAD_B__mean",
        "decoded:ECM_AIRFLOW_PROXY_23D",
        "decoded:ECM_AIRFLOW_PROXY_23D__mean",
    )
    retracted = expected[-4:]
    startup_missing = expected[80:84] + retracted
    startup = _report_missing(expected, startup_missing)
    steady = _report_missing(expected, retracted)

    caplog.set_level(logging.INFO, logger=inference_mod.log.name)
    inference_mod._log_schema_reconciliation_once("isolation_forest", "v1", startup, now=0.0)
    pending = structural_blindness_snapshot()["isolation_forest"]
    assert pending["status"] == "pending"
    assert pending["scores_nothing"] is None
    assert pending["absent_count"] == 8  # candidates so far: every absent column
    assert pending["reports"] == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    # Slow-tier PIDs and GPS arrive on the next windows.
    inference_mod._log_schema_reconciliation_once("isolation_forest", "v1", steady, now=1.0)
    still_pending = structural_blindness_snapshot()["isolation_forest"]
    assert still_pending["status"] == "pending"
    assert still_pending["absent_feature_columns"] == list(retracted)

    # A sparse window later: the columns are transient, never structural.
    inference_mod._log_schema_reconciliation_once("isolation_forest", "v1", startup, now=2.0)
    sparse = structural_blindness_snapshot()["isolation_forest"]
    assert sparse["absent_feature_columns"] == list(retracted)
    assert sparse["transient_absent_feature_columns"] == list(expected[80:84])

    _feed(inference_mod, "v1", steady, start=3.0, seconds=STRUCTURAL_MIN_OBSERVED_SECONDS)
    verdict = structural_blindness_snapshot()["isolation_forest"]
    assert verdict["status"] == "established"
    assert verdict["absent_feature_columns"] == list(retracted)
    assert verdict["transient_absent_feature_columns"] == []
    # scores_nothing from the structural set: 4/88 = 4.5 % is under the 5 %
    # ceiling, whereas the startup set (8/88) would have claimed blindness.
    assert verdict["absent_fraction"] == pytest.approx(4 / 88, abs=1e-4)
    assert verdict["scores_nothing"] is False
    assert verdict["observed_seconds"] >= STRUCTURAL_MIN_OBSERVED_SECONDS

    # Established once, logged once — no per-tick spam.
    established = [r for r in caplog.records if "structural absence" in r.getMessage()]
    assert len(established) == 1
    _feed(inference_mod, "v1", steady, start=200.0, seconds=30.0)
    assert len([r for r in caplog.records if "structural absence" in r.getMessage()]) == 1


def test_pending_until_both_floors_are_met(_fresh_blindness) -> None:
    from src.models.feature_spec_health import (
        STRUCTURAL_MIN_OBSERVED_SECONDS,
        STRUCTURAL_MIN_REPORTS,
        structural_blindness_snapshot,
    )

    inference_mod = _fresh_blindness
    report = _reconciliation(expected=88, absent=6)
    # Plenty of time, too few reports.
    inference_mod._log_schema_reconciliation_once("isolation_forest", "v1", report, now=0.0)
    inference_mod._log_schema_reconciliation_once(
        "isolation_forest", "v1", report, now=STRUCTURAL_MIN_OBSERVED_SECONDS + 1.0
    )
    assert structural_blindness_snapshot()["isolation_forest"]["status"] == "pending"
    # Plenty of reports, too little time.
    inference_mod._log_schema_reconciliation_once("isolation_forest", "v2", report, now=0.0)
    for i in range(STRUCTURAL_MIN_REPORTS * 2):
        inference_mod._log_schema_reconciliation_once(
            "isolation_forest", "v2", report, now=0.1 * (i + 1)
        )
    assert structural_blindness_snapshot()["isolation_forest"]["status"] == "pending"


def test_a_column_absent_throughout_stays_structural_and_errors_once(
    _fresh_blindness, caplog
) -> None:
    import logging

    from src.models.feature_spec_health import (
        STRUCTURAL_MIN_OBSERVED_SECONDS,
        structural_blindness_snapshot,
    )

    inference_mod = _fresh_blindness
    caplog.set_level(logging.INFO, logger=inference_mod.log.name)
    expected = tuple(f"decoded:CH_{i}" for i in range(88))
    always = expected[:6]
    flicker = expected[6:10]
    end = 0.0
    for i in range(int(STRUCTURAL_MIN_OBSERVED_SECONDS) + 10):
        missing = always + (flicker if i % 3 == 0 else ())
        inference_mod._log_schema_reconciliation_once(
            "isolation_forest", "v1", _report_missing(expected, missing), now=float(i)
        )
        end = float(i)
    verdict = structural_blindness_snapshot()["isolation_forest"]
    assert verdict["status"] == "established"
    assert verdict["absent_feature_columns"] == list(always)
    assert verdict["scores_nothing"] is True
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1

    # If a retracted column turns up after all, the verdict shrinks and says so
    # (once), and scores_nothing is recomputed from the smaller set.
    shrunk = always[:2]
    for i in range(5):
        inference_mod._log_schema_reconciliation_once(
            "isolation_forest", "v1", _report_missing(expected, shrunk), now=end + 1 + i
        )
    verdict = structural_blindness_snapshot()["isolation_forest"]
    assert verdict["absent_feature_columns"] == list(shrunk)
    assert verdict["scores_nothing"] is False
    changed = [r for r in caplog.records if "structural absence changed" in r.getMessage()]
    assert len(changed) == 1


# ---------------------------------------------------------------------------
# tuning-did coolant rate — a measured constraint, pinned so it cannot drift
# ---------------------------------------------------------------------------

#: Coolant's fastest honest slope, pooled over 38 local sessions and 10,488
#: adjacent-sample pairs with gaps <= 15 s (so a restart or a collection hole
#: cannot fake a derivative): p99 0.332, p99.9 0.441 degC/s. The channel
#: quantises at 1 degC, so a rate above ~0.44 Hz cannot miss a step.
COOLANT_P999_DEGC_PER_S = 0.441
COOLANT_QUANTUM_DEGC = 1.0


def _tuning_did_profile():
    from pathlib import Path

    from src.collection.capture_profiles import seed_catalog
    from src.config import CanaryConfig

    cfg = CanaryConfig.from_yaml(Path("canary.dev.example.yaml"))
    catalog = seed_catalog(cfg.collection)
    return next(p for p in catalog.profiles if p.name == "tuning-did")


def _effective_hz(profile, channel: str) -> float:
    weight = profile.mode22_rate_weights.get(channel.upper(), 1.0)
    return profile.target_rate_hz * weight


def test_coolant_is_polled_as_context_not_dynamics() -> None:
    """It was taking one eleventh of the profile's budget to change on 0.2 %
    of its samples (H8, 2026-09-10)."""
    p = _tuning_did_profile()

    assert _effective_hz(p, "ECM_COOLANT_TEMP") == pytest.approx(1.0)


def test_coolant_rate_still_resolves_a_one_degree_step() -> None:
    """The constraint the rate was chosen against, asserted rather than
    described: at its fastest measured slope, coolant must move less than one
    quantisation step between consecutive samples."""
    p = _tuning_did_profile()
    hz = _effective_hz(p, "ECM_COOLANT_TEMP")

    degc_between_samples = COOLANT_P999_DEGC_PER_S / hz

    assert degc_between_samples < COOLANT_QUANTUM_DEGC, (
        f"at {hz} Hz coolant moves {degc_between_samples:.2f} degC between samples, "
        f"past the {COOLANT_QUANTUM_DEGC} degC quantum — steps would be aliased"
    )


def test_every_other_tuning_did_channel_keeps_full_rate() -> None:
    """Only the thermal context (coolant, and intake-air temperature since
    2026-09-21, CAPTURE_CONTEXT_MODE01) is down-weighted. The air-fuel sensors
    move on 84.6 % of samples and are undersampled even at the full rate, so
    nothing else may be traded away without its own measurement."""
    p = _tuning_did_profile()

    down_weighted = {c for c in p.mode22 if _effective_hz(p, c) < p.target_rate_hz}

    assert down_weighted == {"ECM_COOLANT_TEMP", "ECM_INTAKE_AIR_TEMP_1106"}


def test_declared_demand_matches_the_weighted_sum() -> None:
    """`expected_total_demand_hz` is what the capacity evidence is argued
    against, so it must be the real weighted total, not the old flat one."""
    p = _tuning_did_profile()

    total = sum(_effective_hz(p, c) for c in p.mode22)

    assert total == pytest.approx(p.expected_total_demand_hz)
    assert total < p.bus_budget_override


def test_down_weighted_coolant_cannot_fail_the_rate_floor() -> None:
    """The contract's per-channel floor is 12 Hz. A channel deliberately asked
    for at 1 Hz must be exempt, or every tuning-did capture would now report a
    false rate shortfall."""
    from src.collection.capture_quality import _channel_failures

    p = _tuning_did_profile()
    floor = p.min_achieved_rate_hz

    class _Row:
        status = "ok"
        channel = "ECM_COOLANT_TEMP"
        requested_hz = 1.0
        cadence = {"hz": 1.0}

    assert _channel_failures([_Row()], floor) == []
