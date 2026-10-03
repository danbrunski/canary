"""Tests for ``scripts/benchmark_live_pipeline.py`` (roadmap task C05).

The regression this guards: the benchmark used to observe records at Unix
seconds 0, 1, 2, … while ticking at a 2026 datetime, so every observation
was stale, the buffer stayed empty, and it reported ~27,600 Hz over zero
feature rows. ``DEFECT_OFFSET_SECONDS`` reproduces exactly that mismatch
through the script's documented test-only knob so the validity check can be
shown to reject it.
"""

from __future__ import annotations

import json
from collections import Counter
from itertools import pairwise
from pathlib import Path

import numpy as np
import pytest

from scripts.benchmark_live_pipeline import (
    EXIT_INVALID,
    MAX_CHANNELS,
    SCENARIO_EPOCH,
    SUPPORTED_OBD_CHANNELS,
    BenchmarkReport,
    build_tick_workload,
    channel_value,
    drive_state,
    main,
    run_benchmark,
    select_channels,
    tick_period_seconds,
    tick_time,
)
from src.collection.can_obd_client import PID_DECODERS
from src.config import CanaryConfig
from src.preprocessing.quality_gate import is_physically_valid
from src.telemetry import OBD_SOURCE, channel_key
from tests.configs import example_config

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"
BINDINGS_YAML = REPO_ROOT / "data" / "bindings.yaml"

#: Shifts observation timestamps back to the Unix epoch while ticks stay in
#: 2026 — the original defect, verbatim.
DEFECT_OFFSET_SECONDS = -SCENARIO_EPOCH.timestamp()


@pytest.fixture(scope="module")
def config() -> CanaryConfig:
    return example_config(EXAMPLE_YAML)


def _with_resample_hz(config: CanaryConfig, hz: float) -> CanaryConfig:
    preprocessing = config.preprocessing.model_copy(update={"resample_hz": hz})
    return config.model_copy(update={"preprocessing": preprocessing})


def _run(config: CanaryConfig, **overrides: object) -> BenchmarkReport:
    kwargs: dict[str, object] = {
        "warmup_ticks": 5,
        "ticks": 8,
        "channels": 6,
        "can_frames_per_tick": 9,
        "seed": 3,
    }
    kwargs.update(overrides)
    return run_benchmark(config, **kwargs)  # type: ignore[arg-type]


def _workloads(config: CanaryConfig, count: int, *, channels: int = 7, frames: int = 20):
    period = tick_period_seconds(config)
    rng = np.random.default_rng(0)
    return [
        build_tick_workload(
            index,
            period_seconds=period,
            channels=select_channels(channels),
            can_ids=(0x1F9, 0x551, 0x23D),
            can_frames_per_tick=frames,
            rng=rng,
        )
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# consistent timestamps → populated buffer → real feature rows
# ---------------------------------------------------------------------------


def test_consistent_timestamps_populate_buffer_and_produce_feature_rows(
    config: CanaryConfig,
) -> None:
    report = _run(config)

    assert report.valid, report.invalid_reasons
    assert report.results.buffer_size_after_warmup == 5
    assert report.results.buffer_size_at_end == 13
    assert report.results.completed_ticks == 8
    assert report.results.empty_ticks == 0
    assert report.results.feature_rows == 8
    assert report.results.feature_columns > 0
    assert report.results.quality_gate_rejects == {}
    assert report.results.microbenchmark_hz_at_p95 is not None


def test_scenario_clock_is_monotonic_across_warmup_and_measurement(config: CanaryConfig) -> None:
    period = tick_period_seconds(config)
    workloads = _workloads(config, 12)

    ticks = [w.tick_at for w in workloads]
    assert ticks == [tick_time(i, period) for i in range(12)]
    assert all(later > earlier for earlier, later in pairwise(ticks))
    for previous, workload in pairwise([None, *workloads]):
        floor = (
            workload.tick_at.timestamp() - period
            if previous is None
            else previous.tick_at.timestamp()
        )
        for record in workload.records:
            assert floor < record.timestamp.timestamp() <= workload.tick_at.timestamp()

    # The measured phase continues the warmup clock — no rewind to index 0.
    report = _run(config, warmup_ticks=5, ticks=8)
    assert [t.index for t in report.ticks] == list(range(5, 13))
    assert report.workload.scenario_start == tick_time(0, period).isoformat()
    assert report.workload.scenario_end == tick_time(12, period).isoformat()
    assert report.workload.scenario_seconds == pytest.approx(12 * period)


# ---------------------------------------------------------------------------
# workload counts are exactly what was requested
# ---------------------------------------------------------------------------


def test_requested_workload_counts_are_honoured_exactly(config: CanaryConfig) -> None:
    workloads = _workloads(config, 3, channels=7, frames=20)

    for workload in workloads:
        obd = [r for r in workload.records if r.source == "obd"]
        can = [r for r in workload.records if r.source == "can"]
        assert [r.channel for r in obd] == list(SUPPORTED_OBD_CHANNELS[:7])
        assert len(can) == 20
        per_id = Counter(r.metadata["can_id"] for r in can)
        assert set(per_id.values()) <= {6, 7}, per_id
        # Frames are spread through the tick period, the last one on the tick.
        stamps = [r.timestamp.timestamp() for r in can]
        assert stamps == sorted(stamps) and len(set(stamps)) == 20
        assert stamps[-1] == pytest.approx(workload.tick_at.timestamp())
    # Round-robin carries across ticks: 60 frames over 3 IDs → 20 each.
    overall = Counter(
        r.metadata["can_id"] for w in workloads for r in w.records if r.source == "can"
    )
    assert overall == {0x1F9: 20, 0x551: 20, 0x23D: 20}

    report = _run(config, channels=7, can_frames_per_tick=20, ticks=4)
    assert report.workload.channels == SUPPORTED_OBD_CHANNELS[:7]
    assert report.workload.can_frames_per_tick == 20
    assert report.workload.observations_per_tick == 27
    assert report.workload.observations_measured == 27 * 4


def test_channel_count_is_validated_against_the_supported_list() -> None:
    assert len(select_channels(MAX_CHANNELS)) == MAX_CHANNELS
    with pytest.raises(ValueError, match=f"between 1 and {MAX_CHANNELS}"):
        select_channels(MAX_CHANNELS + 1)
    with pytest.raises(ValueError, match="between 1 and"):
        select_channels(0)


def test_zero_can_frames_is_an_honest_workload(config: CanaryConfig) -> None:
    report = _run(config, can_frames_per_tick=0, channels=4, ticks=3, warmup_ticks=2)
    assert report.valid, report.invalid_reasons
    assert report.workload.observations_per_tick == 4


# ---------------------------------------------------------------------------
# physically meaningful values → real mode classification
# ---------------------------------------------------------------------------


def test_supported_channels_are_real_pids_with_physically_valid_values() -> None:
    known = {name for name, _ in PID_DECODERS.values()}
    assert set(SUPPORTED_OBD_CHANNELS) <= known
    assert SUPPORTED_OBD_CHANNELS[:4] == ("RPM", "SPEED", "THROTTLE_POS", "COOLANT_TEMP")

    rng = np.random.default_rng(1)
    for t in range(0, 600, 5):
        state = drive_state(float(t), rng)
        assert 650.0 <= state.rpm <= 4500.0
        assert 0.0 <= state.speed <= 110.0
        assert 0.0 <= state.throttle <= 100.0
        assert 20.0 <= state.coolant <= 95.0
        for name in SUPPORTED_OBD_CHANNELS:
            value = channel_value(name, state, float(t))
            assert is_physically_valid(channel_key(OBD_SOURCE, name), value), (name, t, value)


def test_operating_modes_are_classified_from_the_synthetic_drive(config: CanaryConfig) -> None:
    report = _run(config, channels=4, ticks=6, warmup_ticks=2)
    assert report.results.segmenter_fallback_ticks == 0
    assert "<none>" not in report.results.operating_modes
    assert set(report.results.operating_modes) <= set(config.preprocessing.operating_modes)

    # Below four channels the segmenter has no inputs — recorded, not hidden.
    short = _run(config, channels=2, ticks=3, warmup_ticks=2)
    assert short.results.segmenter_fallback_ticks == 3


def test_seed_makes_the_workload_deterministic(config: CanaryConfig) -> None:
    first = _workloads(config, 4)
    second = _workloads(config, 4)
    assert [w.records for w in first] == [w.records for w in second]


# ---------------------------------------------------------------------------
# cadence follows the configured resample rate
# ---------------------------------------------------------------------------


def test_non_1hz_cadence_ticks_by_the_resample_period(config: CanaryConfig) -> None:
    fast = _with_resample_hz(config, 4.0)
    assert tick_period_seconds(fast) == 0.25
    assert (tick_time(4, 0.25) - tick_time(0, 0.25)).total_seconds() == 1.0

    report = _run(fast, warmup_ticks=8, ticks=8)
    assert report.valid, report.invalid_reasons
    assert report.workload.tick_period_seconds == 0.25
    assert report.workload.resample_hz == 4.0
    assert report.workload.scenario_seconds == pytest.approx(15 * 0.25)
    assert report.results.buffer_size_after_warmup == 8
    assert report.results.completed_ticks == 8


# ---------------------------------------------------------------------------
# C05.12 acceptance: the zero-row 27,600 Hz result must be impossible to accept
# ---------------------------------------------------------------------------


def test_all_stale_run_is_rejected_with_named_reasons(config: CanaryConfig) -> None:
    report = _run(config, observation_epoch_offset_seconds=DEFECT_OFFSET_SECONDS)

    assert not report.valid
    assert report.results.buffer_size_after_warmup == 0
    assert report.results.buffer_size_at_end == 0
    assert report.results.completed_ticks == 0
    assert report.results.empty_ticks == report.workload.measured_ticks
    assert report.results.feature_rows == 0
    assert report.results.microbenchmark_hz_at_p95 is None
    assert any("warm buffer empty" in reason for reason in report.invalid_reasons)
    assert any("no measured tick produced feature rows" in r for r in report.invalid_reasons)


def test_partially_empty_run_is_rejected_and_quotes_no_hz(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One real row among many empty ticks is still the 27,600 Hz shape.

    Timing the empty ticks would quote their near-zero latency as the feature
    stage's cost. Every measured tick must complete, or the run is invalid and
    no Hz figure is reported.
    """
    from src.features.live import LiveFeatureSource

    real_tick = LiveFeatureSource.tick
    calls = {"n": 0}

    def flaky_tick(self: LiveFeatureSource, now):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        # Let warmup (5 ticks) and exactly one measured tick through.
        if calls["n"] <= 6:
            return real_tick(self, now)
        return None

    monkeypatch.setattr(LiveFeatureSource, "tick", flaky_tick)
    report = _run(config)

    assert report.results.completed_ticks == 1
    assert report.results.empty_ticks == report.workload.measured_ticks - 1
    assert not report.valid
    assert report.results.microbenchmark_hz_at_p95 is None
    assert any("measured ticks produced no feature rows" in r for r in report.invalid_reasons)
    # Timings cover only the completed tick, never the empty ones.
    assert report.results.timings_ms["total"].count == 1


def test_cli_exits_nonzero_and_marks_report_invalid_for_a_stale_run(tmp_path: Path) -> None:
    report_path = tmp_path / "stale.json"
    code = main(
        [
            "--config",
            str(EXAMPLE_YAML),
            "--bindings",
            str(tmp_path / "missing-bindings.yaml"),
            "--warmup-ticks",
            "3",
            "--ticks",
            "4",
            "--report",
            str(report_path),
            "--observation-epoch-offset-seconds",
            str(DEFECT_OFFSET_SECONDS),
        ]
    )
    assert code == EXIT_INVALID
    payload = json.loads(report_path.read_text())
    assert payload["valid"] is False
    assert payload["results"]["buffer_size_after_warmup"] == 0
    assert payload["results"]["completed_ticks"] == 0


def test_cli_writes_a_machine_readable_report_for_a_valid_run(tmp_path: Path) -> None:
    report_path = tmp_path / "smoke.json"
    code = main(
        [
            "--config",
            str(EXAMPLE_YAML),
            "--bindings",
            str(BINDINGS_YAML),
            "--warmup-ticks",
            "3",
            "--ticks",
            "4",
            "--channels",
            "5",
            "--can-frames-per-tick",
            "6",
            "--seed",
            "11",
            "--report",
            str(report_path),
        ]
    )
    assert code == 0
    payload = json.loads(report_path.read_text())
    assert payload["valid"] is True
    assert payload["invalid_reasons"] == []
    assert payload["scope"].startswith("feature-only microbenchmark")
    assert payload["host"]["hostname"]
    assert payload["host"]["python"]
    assert payload["provenance"]["config_sha256"]
    assert payload["provenance"]["bindings_sha256"]
    workload = payload["workload"]
    assert workload["kind"] == "synthetic"
    assert workload["seed"] == 11
    assert workload["warmup_ticks"] == 3
    assert workload["measured_ticks"] == 4
    assert workload["resample_hz"] == 1.0
    assert len(workload["channels"]) == 5
    assert workload["can_frames_per_tick"] == 6
    results = payload["results"]
    assert results["completed_ticks"] == 4
    assert results["empty_ticks"] == 0
    assert results["buffer_size_after_warmup"] == 3
    for stage in ("observe", "tick", "total"):
        timing = results["timings_ms"][stage]
        assert timing["count"] == 4
        assert {"median_ms", "p95_ms", "p99_ms", "max_ms"} <= set(timing)
    assert "not a sustainable daemon collection rate" in results["microbenchmark_hz_note"]
    assert "ticks" not in payload
