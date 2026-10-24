"""Tests for model drift + inference health monitoring (Task 6.27)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.daemon.health as health_mod
from src.config import CanaryConfig
from src.daemon.health import (
    HealthMonitor,
    can0_counters,
    feature_drift,
    power_status,
    reconstruction_error_drift,
    system_metrics,
)
from src.dashboard.api import build_status, create_app
from tests.configs import isolated_config

# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def test_system_metrics_is_safe_and_shaped() -> None:
    m = system_metrics()
    for key in ("load_1m", "cpu_count", "mem_used_fraction", "mem_total_mb", "cpu_temp_c"):
        assert key in m  # value may be None on a platform that lacks it


def test_cpu_percent_needs_a_baseline_then_reports_a_fraction() -> None:
    mon = HealthMonitor()
    # First snapshot has no prior /proc/stat reading to diff against.
    assert mon.snapshot(now_epoch=0.0)["system"]["cpu_percent"] is None
    # Second snapshot can diff; on Linux that's a 0..1 fraction, None elsewhere.
    cpu = mon.snapshot(now_epoch=1.0)["system"]["cpu_percent"]
    assert cpu is None or 0.0 <= cpu <= 1.0


def test_power_status_decodes_throttled_bitmask(monkeypatch: pytest.MonkeyPatch) -> None:
    # 0x50000 = under-voltage + throttling have occurred since boot, nothing live
    # (the exact 2026-05-24 incident reading).
    monkeypatch.setattr(health_mod, "_read_throttled_raw", lambda: 0x50000)
    assert power_status() == {
        "raw": 0x50000,
        "now": {
            "under_voltage": False,
            "arm_frequency_capped": False,
            "currently_throttled": False,
            "soft_temp_limit": False,
        },
        "since_boot": {
            "under_voltage": True,
            "arm_frequency_capped": False,
            "throttling": True,
            "soft_temp_limit": False,
        },
    }


def test_power_status_flags_live_under_voltage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_mod, "_read_throttled_raw", lambda: 0x10001)  # bit0 + bit16
    status = power_status()
    assert status is not None
    assert status["now"]["under_voltage"] is True
    assert status["since_boot"]["under_voltage"] is True


def test_power_status_none_when_vcgencmd_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_mod, "_read_throttled_raw", lambda: None)
    assert power_status() is None


def test_snapshot_includes_power_behind_a_ttl_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def _fake_raw() -> int:
        calls["n"] += 1
        return 0x0

    monkeypatch.setattr(health_mod, "_read_throttled_raw", _fake_raw)
    mon = HealthMonitor()
    snap = mon.snapshot(now_epoch=0.0)
    assert snap["power"] == {
        "raw": 0,
        "now": dict.fromkeys(
            ("under_voltage", "arm_frequency_capped", "currently_throttled", "soft_temp_limit"),
            False,
        ),
        "since_boot": dict.fromkeys(
            ("under_voltage", "arm_frequency_capped", "throttling", "soft_temp_limit"), False
        ),
    }
    # Second snapshot within the TTL reuses the cache — no extra subprocess read.
    mon.snapshot(now_epoch=1.0)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# can0_counters — Task 6.8 (real environment tonight: no can0 interface)
# ---------------------------------------------------------------------------


def test_can0_counters_none_when_interface_absent(tmp_path: Path) -> None:
    """This laptop's actual environment: no can0 sysfs directory at all."""
    assert can0_counters(tmp_path / "no-such-can0" / "statistics") is None


def test_can0_counters_none_when_a_counter_file_is_missing(tmp_path: Path) -> None:
    stats = tmp_path / "statistics"
    stats.mkdir()
    (stats / "rx_over_errors").write_text("0\n")
    # rx_dropped / rx_errors absent — treat as unavailable rather than partial.
    assert can0_counters(stats) is None


def test_can0_counters_reads_fixture_sysfs_directory(tmp_path: Path) -> None:
    stats = tmp_path / "statistics"
    stats.mkdir()
    (stats / "rx_over_errors").write_text("3\n")
    (stats / "rx_dropped").write_text("7\n")
    (stats / "rx_errors").write_text("10\n")
    assert can0_counters(stats) == {"rx_over_errors": 3, "rx_dropped": 7, "rx_errors": 10}


def test_feature_drift_flags_and_schema_mismatch() -> None:
    scaler = {
        "columns": ["obd:RPM", "obd:SPEED", "obd:CONST"],
        "means": {"obd:RPM": 800.0, "obd:SPEED": 0.0, "obd:CONST": 5.0},
        "stds": {"obd:RPM": 100.0, "obd:SPEED": 20.0, "obd:CONST": 0.0},
    }
    live = {"obd:RPM": 1200.0, "obd:SPEED": 5.0, "obd:CONST": 5.5}
    report = feature_drift(live, scaler, sigma_threshold=3.0)
    assert report.per_feature_sigma["obd:RPM"] == 4.0  # |1200-800|/100
    assert "obd:RPM" in report.drifted_features
    assert "obd:SPEED" not in report.drifted_features  # 0.25σ
    # Constant training feature: reported as absolute delta, not /0.
    assert report.per_feature_sigma["obd:CONST"] == 0.5
    assert report.schema_mismatch is False

    # A trained column the live stream hasn't produced → schema mismatch.
    partial = feature_drift({"obd:RPM": 800.0}, scaler)
    assert partial.schema_mismatch is True
    assert "obd:SPEED" in partial.missing_features


def test_reconstruction_error_drift() -> None:
    thresholds = {"global": {"mean": 0.10, "std": 0.02, "threshold": 0.16}}
    out = reconstruction_error_drift(0.20, thresholds)
    assert out["ratio"] == 2.0  # 0.20 / 0.10
    assert out["sigma_above_train"] == 5.0  # (0.20-0.10)/0.02
    assert reconstruction_error_drift(None, thresholds)["ratio"] is None


# ---------------------------------------------------------------------------
# HealthMonitor
# ---------------------------------------------------------------------------


def test_monitor_accumulates_and_snapshots() -> None:
    mon = HealthMonitor()
    mon.observe_inference(
        latency_ms=5.0, missing_features=False, score=0.1, data_timestamp_epoch=100.0
    )
    mon.observe_inference(
        latency_ms=15.0, missing_features=True, score=0.9, data_timestamp_epoch=101.0
    )
    mon.note_swap("swapped", version="if_v2")
    mon.note_swap("unchanged")
    snap = mon.snapshot(now_epoch=111.0)
    assert snap["observed"] == 2
    assert snap["missing_feature_rate"] == 0.5
    assert snap["inference_latency_ms"]["mean"] == 10.0
    assert snap["inference_latency_ms"]["max"] == 15.0
    assert snap["data_freshness_seconds"] == 10.0  # 111 - 101
    assert snap["model_swaps"]["swapped"] == 1
    assert snap["active_model_version"] == "if_v2"
    assert "system" in snap
    # Drift only folded in when inputs provided.
    assert "feature_drift" not in snap
    snap2 = mon.snapshot(
        now_epoch=111.0,
        scaler={"columns": ["a"], "means": {"a": 0.0}, "stds": {"a": 1.0}},
        live_means={"a": 10.0},
        lstm_thresholds={"global": {"mean": 0.1, "std": 0.05}},
        live_mean_recon_error=0.3,
    )
    assert snap2["feature_drift"]["drifted_features"] == ["a"]
    assert snap2["reconstruction_error_drift"]["ratio"] == 3.0


def test_empty_monitor_snapshot_is_none_safe() -> None:
    snap = HealthMonitor().snapshot(now_epoch=0.0)
    assert snap["observed"] == 0
    assert snap["missing_feature_rate"] is None
    assert snap["inference_latency_ms"]["mean"] is None
    assert snap["data_freshness_seconds"] is None


def test_model_version_mismatch_surfaces_and_clears() -> None:
    """Partial-deploy detector (2026-07-26): a stale binary next to a fresh
    manifest shows up on the snapshot until the versions agree again."""
    mon = HealthMonitor()
    # Consistent pair → nothing recorded.
    mon.observe_model_version_mismatch("isolation_forest", serving="v1", manifest="v1")
    assert "model_version_mismatches" not in mon.snapshot(now_epoch=0.0)
    # Stale binary vs fresh manifest → surfaced.
    mon.observe_model_version_mismatch("lstm_autoencoder", serving="v1", manifest="v2")
    snap = mon.snapshot(now_epoch=0.0)
    assert snap["model_version_mismatches"] == {
        "lstm_autoencoder": {"serving": "v1", "manifest": "v2"}
    }
    # Binary lands → clears on the next poll.
    mon.observe_model_version_mismatch("lstm_autoencoder", serving="v2", manifest="v2")
    assert "model_version_mismatches" not in mon.snapshot(now_epoch=0.0)


# ---------------------------------------------------------------------------
# HealthMonitor.snapshot()["can0"] — Task 6.8
# ---------------------------------------------------------------------------


def test_snapshot_omits_can0_when_interface_absent(tmp_path: Path) -> None:
    """The real state of this laptop tonight: no can0 anywhere → no crash,
    just a quietly absent field (not a null placeholder, not an exception)."""
    mon = HealthMonitor(can0_stats_path=tmp_path / "no-such-can0")
    snap = mon.snapshot(now_epoch=0.0)
    assert "can0" not in snap


def test_snapshot_reports_can0_counters_and_delta_since_start(tmp_path: Path) -> None:
    stats = tmp_path / "statistics"
    stats.mkdir()
    (stats / "rx_over_errors").write_text("0")
    (stats / "rx_dropped").write_text("0")
    (stats / "rx_errors").write_text("0")

    mon = HealthMonitor(can0_stats_path=stats)
    first = mon.snapshot(now_epoch=0.0)
    assert first["can0"] == {
        "rx_over_errors": 0,
        "rx_dropped": 0,
        "rx_errors": 0,
        "since_start": {"rx_over_errors": 0, "rx_dropped": 0, "rx_errors": 0},
    }

    # Simulate growth during a drive between two /status polls.
    (stats / "rx_over_errors").write_text("3")
    (stats / "rx_dropped").write_text("5")
    second = mon.snapshot(now_epoch=1.0)
    assert second["can0"]["rx_over_errors"] == 3
    assert second["can0"]["since_start"] == {"rx_over_errors": 3, "rx_dropped": 5, "rx_errors": 0}


def _blind_modes(mon: HealthMonitor) -> tuple[str, ...]:
    """Read through a call so mypy does not narrow the property after an assert."""
    return mon.if_blind_modes


def test_observe_inference_latches_if_mode_blindness_edge_triggered() -> None:
    """2.1: a mode only latches "blind" once a full window of consecutive
    missing-feature ticks has landed for it, and the transition into that
    state is reported exactly once (edge-triggered), not on every tick."""
    mon = HealthMonitor()
    window = health_mod._MODE_BLIND_WINDOW

    # Fewer than a full window of misses: not yet blind.
    transitions = [
        mon.observe_inference(missing_features=True, operating_mode="idle")
        for _ in range(window - 1)
    ]
    assert not any(transitions)
    assert _blind_modes(mon) == ()

    # The tick that completes the window latches it — exactly once.
    assert mon.observe_inference(missing_features=True, operating_mode="idle") is True
    assert _blind_modes(mon) == ("idle",)

    # Further missing ticks for the same mode don't re-fire the transition.
    assert mon.observe_inference(missing_features=True, operating_mode="idle") is False
    assert _blind_modes(mon) == ("idle",)

    # A single clean tick resets the window — the mode is no longer blind...
    assert mon.observe_inference(missing_features=False, operating_mode="idle") is False
    assert _blind_modes(mon) == ()

    # ...and going missing again for a fresh full window re-latches (and
    # would re-warn) rather than staying silently suppressed forever.
    for _ in range(window - 1):
        mon.observe_inference(missing_features=True, operating_mode="idle")
    assert mon.observe_inference(missing_features=True, operating_mode="idle") is True
    assert _blind_modes(mon) == ("idle",)


def test_if_missing_feature_rate_by_mode_and_blind_modes_are_per_mode() -> None:
    mon = HealthMonitor()
    for _ in range(health_mod._MODE_BLIND_WINDOW):
        mon.observe_inference(missing_features=True, operating_mode="idle")
    mon.observe_inference(missing_features=False, operating_mode="city")
    mon.observe_inference(missing_features=True, operating_mode="city")

    assert mon.if_blind_modes == ("idle",)
    assert mon.if_missing_feature_rate_by_mode == {"idle": 1.0, "city": 0.5}


def test_observe_inference_without_operating_mode_never_blinds() -> None:
    mon = HealthMonitor()
    for _ in range(health_mod._MODE_BLIND_WINDOW + 2):
        assert mon.observe_inference(missing_features=True) is False
    assert mon.if_blind_modes == ()


def test_observe_segmenter_fallback_is_edge_triggered() -> None:
    """4.7 — mirrors the IF blind-mode edge-triggered pattern: the
    transition into fallback reports True exactly once, not every tick."""
    mon = HealthMonitor()
    assert mon.segmenter_fallback_active is False

    assert mon.observe_segmenter_fallback(True) is True  # rising edge
    assert mon.segmenter_fallback_active is True
    assert mon.observe_segmenter_fallback(True) is False  # already active
    assert mon.segmenter_fallback_active is True

    assert mon.observe_segmenter_fallback(False) is False  # recovers
    assert mon.segmenter_fallback_active is False

    assert mon.observe_segmenter_fallback(True) is True  # re-latches
    assert mon.segmenter_fallback_active is True


def test_monitor_snapshots_obd_scheduler_metrics() -> None:
    mon = HealthMonitor()
    mon.observe_obd_scheduler(
        {
            "configured_poll_rate_hz": 1.0,
            "bus_budget_requests_per_second": 8.0,
            "active_requests": 3,
            "backed_off_requests": 1,
            "requests": [{"channel": "RPM", "poll_tier": "fast"}],
        }
    )

    snap = mon.snapshot(now_epoch=0.0)

    assert snap["obd_scheduler"]["active_requests"] == 3
    assert snap["obd_scheduler"]["requests"][0]["channel"] == "RPM"


# ---------------------------------------------------------------------------
# /status surfacing
# ---------------------------------------------------------------------------


def _config(tmp_path: Path) -> CanaryConfig:
    storage = tmp_path / "storage"
    (storage / "2026-05-17T120000").mkdir(parents=True)
    return isolated_config(tmp_path, storage=storage)


def test_build_status_includes_health_when_monitor_present(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert build_status(config).to_dict()["health"] is None
    mon = HealthMonitor()
    mon.observe_inference(latency_ms=3.0)
    snap = build_status(config, health_monitor=mon)
    assert snap.to_dict()["health"]["observed"] == 1


def test_status_endpoint_exposes_health(tmp_path: Path) -> None:
    config = _config(tmp_path)
    mon = HealthMonitor()
    mon.note_swap("swapped", version="lstm_v9")
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", health_monitor=mon)
    body = TestClient(app).get("/status").json()
    assert body["health"]["model_swaps"]["swapped"] == 1
    assert body["health"]["active_model_version"] == "lstm_v9"


def test_status_exposes_isolation_forest_blind_modes(tmp_path: Path) -> None:
    """2.1: `detectors.isolation_forest.blind_modes` + the per-mode missing-
    feature gauge are top-level on `/status`, not buried under `health`."""
    config = _config(tmp_path)
    mon = HealthMonitor()
    for _ in range(health_mod._MODE_BLIND_WINDOW):
        mon.observe_inference(missing_features=True, operating_mode="idle")
    mon.observe_inference(missing_features=False, operating_mode="city")

    snap = build_status(config, health_monitor=mon).to_dict()
    assert snap["detectors"]["isolation_forest"]["blind_modes"] == ["idle"]
    assert snap["detectors"]["isolation_forest"]["missing_feature_rate_by_mode"] == {
        "idle": 1.0,
        "city": 0.0,
    }

    app = create_app(config, anomalies_dir=tmp_path / "anomalies", health_monitor=mon)
    body = TestClient(app).get("/status").json()
    assert body["detectors"]["isolation_forest"]["blind_modes"] == ["idle"]


def test_status_detectors_is_none_without_a_health_monitor(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert build_status(config).to_dict()["detectors"] is None


def test_status_exposes_segmenter_fallback_flag(tmp_path: Path) -> None:
    """4.7 — `detectors.segmenter.fallback_active` is False by default and
    flips True once the monitor observes a live fallback tick."""
    config = _config(tmp_path)
    mon = HealthMonitor()
    snap = build_status(config, health_monitor=mon).to_dict()
    assert snap["detectors"]["segmenter"]["fallback_active"] is False

    mon.observe_segmenter_fallback(True)
    snap = build_status(config, health_monitor=mon).to_dict()
    assert snap["detectors"]["segmenter"]["fallback_active"] is True

    app = create_app(config, anomalies_dir=tmp_path / "anomalies", health_monitor=mon)
    body = TestClient(app).get("/status").json()
    assert body["detectors"]["segmenter"]["fallback_active"] is True


# ---------------------------------------------------------------------------
# Capture loss epochs, collector handoff, queue drain (roadmap C03.5 / C03.6 / C03.8)
# ---------------------------------------------------------------------------


def test_loss_counters_report_absent_can0_as_unknown_never_zero(tmp_path: Path) -> None:
    mon = HealthMonitor(can0_stats_path=tmp_path / "no-such-can0")
    mon.note_telemetry_drop("storage", count=3)
    counters = mon.loss_counters()
    assert counters["storage.dropped_records"] == 3
    assert counters["storage.errors"] == 0
    assert counters["can0.rx_dropped"] is None
    assert counters["can0.rx_over_errors"] is None
    epoch = mon.loss_epoch(now=12.0)
    assert epoch.taken_at_monotonic == 12.0
    assert epoch.counters == counters


def test_loss_epochs_reconcile_per_capture_not_since_boot(tmp_path: Path) -> None:
    from src.storage.capture_losses import reconcile_losses

    stats = tmp_path / "statistics"
    stats.mkdir()
    for name in ("rx_over_errors", "rx_dropped", "rx_errors"):
        (stats / name).write_text("0")
    mon = HealthMonitor(can0_stats_path=stats)
    mon.note_telemetry_drop("storage", count=2491)  # an earlier incident
    start = mon.loss_epoch(now=0.0)
    (stats / "rx_dropped").write_text("5")
    stop = mon.loss_epoch(now=300.0)
    deltas = reconcile_losses(start, stop)
    assert deltas["storage.dropped_records"].delta == 0
    assert deltas["storage.dropped_records"].status == "verified"
    assert deltas["can0.rx_dropped"].delta == 5


def test_collector_handoff_lands_under_its_own_sink_with_raw_and_derived_apart() -> None:
    mon = HealthMonitor()
    mon.observe_collector_handoff(
        "obd_collect",
        accepted=1000,
        buffer_dropped=4,
        buffer_high_water=300,
        buffer_capacity=4096,
        queue_dropped=6,
        batches=50,
        raw_responses=200,
        derived_records=1004,
        reconnects=1,
    )
    sink = mon.snapshot(now_epoch=0.0)["telemetry_sinks"]["obd_collect"]
    assert sink["dropped_records"] == 10
    assert sink["buffer_dropped"] == 4 and sink["queue_dropped"] == 6
    assert sink["raw_responses"] == 200
    assert sink["derived_records"] == 1004
    assert "storage" not in mon.snapshot(now_epoch=0.0)["telemetry_sinks"]
    assert mon.loss_counters()["obd_collect.dropped_records"] == 10


async def test_wait_for_queues_idle_drains_reports_timeouts_and_unobserved() -> None:
    import asyncio

    mon = HealthMonitor()
    ingest: asyncio.Queue[int] = asyncio.Queue()
    storage: asyncio.Queue[int] = asyncio.Queue()
    mon.observe_telemetry_queue("ingest", ingest)
    mon.observe_telemetry_queue("storage", storage)
    ingest.put_nowait(1)
    storage.put_nowait(1)

    async def consume() -> None:
        await asyncio.sleep(0.01)
        ingest.get_nowait()
        ingest.task_done()

    task = asyncio.create_task(consume())
    result = await mon.wait_for_queues_idle(("ingest", "storage", "never"), timeout=0.1)
    await task
    assert result == {"drained": ["ingest"], "timed_out": ["storage"], "unobserved": ["never"]}


def test_snapshot_carries_the_capture_lifetime_beside_the_trailing_scheduler() -> None:
    from src.collection import capture_lifetime

    capture_lifetime._reset_for_tests()
    try:
        mon = HealthMonitor()
        mon.observe_obd_scheduler({"achieved_hz_capture": {"RPM": 4.0}})
        assert "capture_lifetime" not in mon.snapshot(now_epoch=0.0)
        capture_lifetime.begin(0.0, profile="fuel-map", generation=2)
        capture_lifetime.observe("RPM", 1.0)
        snap = mon.snapshot(now_epoch=0.0)
        assert snap["obd_scheduler"]["achieved_hz_capture"] == {"RPM": 4.0}
        assert snap["capture_lifetime"]["profile"] == "fuel-map"
        assert snap["capture_lifetime"]["channels"]["RPM"]["observations"] == 1
        inputs = mon.capture_quality_inputs(now=10.0)
        assert inputs.lifetime is not None and inputs.lifetime["elapsed_s"] == 10.0
        assert inputs.scheduler == {"achieved_hz_capture": {"RPM": 4.0}}
    finally:
        capture_lifetime._reset_for_tests()


async def test_sink_queue_high_water_survives_the_drain_that_empties_it() -> None:
    """A queue that filled and drained must still say how deep it got (C10.5).

    ``queue_depth`` is the depth at the last observation, so a sink whose
    consumer wedged, filled its queue, dropped records and then recovered
    reported ``queue_depth: 0`` — the peak that explains the loss was gone by
    the time anything read it. The fanout observes every sink's queue after
    each batch, including while a consumer is blocked, so the monotonic max
    of those samples is the high-water mark.
    """
    import asyncio

    mon = HealthMonitor()
    queue: asyncio.Queue[int] = asyncio.Queue(maxsize=4)
    mon.observe_telemetry_queue("storage", queue)
    for value in range(4):
        queue.put_nowait(value)
    mon.observe_telemetry_queue("storage", queue)
    while not queue.empty():
        queue.get_nowait()
    mon.observe_telemetry_queue("storage", queue)

    sink = mon.snapshot(now_epoch=0.0)["telemetry_sinks"]["storage"]
    assert sink["queue_depth"] == 0
    assert sink["queue_high_water"] == 4
    assert sink["queue_max_size"] == 4


def _cgroup(tmp_path: Path, *, group: str = "system.slice/canary.service") -> tuple[Path, Path]:
    """A fake unified cgroup hierarchy plus the `/proc/self/cgroup` naming it."""
    self_cgroup = tmp_path / "self-cgroup"
    self_cgroup.write_text(f"0::/{group}\n")
    root = tmp_path / "cgroup"
    (root / group).mkdir(parents=True)
    return root, self_cgroup


def test_memory_limit_status_reads_the_enforced_ceiling(tmp_path: Path) -> None:
    """A byte ceiling in the cgroup file is the only proof the kernel enforces one.

    The unit carried `MemoryMax=6G` for four days while the kernel enforced
    nothing (`cgroup_disable=memory`, 2026-09-18) and `systemctl show` agreed
    with the unit the whole time. This reads the kernel's own answer.
    """
    from src.daemon.health import memory_limit_status

    root, self_cgroup = _cgroup(tmp_path)
    (root / "system.slice/canary.service/memory.max").write_text("6442450944\n")

    status = memory_limit_status(cgroup_root=root, self_cgroup=self_cgroup)
    assert status["memory_limits_enforced"] is True
    assert status["memory_max_bytes"] == 6442450944


def test_memory_limit_status_reports_an_unlimited_ceiling_as_not_enforced(
    tmp_path: Path,
) -> None:
    """`max` means the controller is mounted and the ceiling is off — a real False."""
    from src.daemon.health import memory_limit_status

    root, self_cgroup = _cgroup(tmp_path)
    (root / "system.slice/canary.service/memory.max").write_text("max\n")

    status = memory_limit_status(cgroup_root=root, self_cgroup=self_cgroup)
    assert status["memory_limits_enforced"] is False
    assert status["memory_max_bytes"] is None


def test_memory_limit_status_is_unknown_when_the_cgroup_file_is_absent(
    tmp_path: Path,
) -> None:
    """No file is *unknown*, never False: a laptop or container has no answer."""
    from src.daemon.health import memory_limit_status

    root, self_cgroup = _cgroup(tmp_path)

    status = memory_limit_status(cgroup_root=root, self_cgroup=self_cgroup)
    assert status["memory_limits_enforced"] is None
    assert status["memory_max_source"].endswith("canary.service/memory.max")


def test_memory_limit_status_is_unknown_when_the_cgroup_file_is_unreadable(
    tmp_path: Path,
) -> None:
    """An unreadable file is unknown too, and must not raise into `/status`."""
    from src.daemon.health import memory_limit_status

    root, self_cgroup = _cgroup(tmp_path)
    denied = root / "system.slice/canary.service/memory.max"
    denied.write_text("6442450944\n")
    denied.chmod(0o000)
    try:
        status = memory_limit_status(cgroup_root=root, self_cgroup=self_cgroup)
    finally:
        denied.chmod(0o644)
    assert status["memory_limits_enforced"] is None

    # Same for a missing /proc/self/cgroup.
    assert (
        memory_limit_status(cgroup_root=root, self_cgroup=tmp_path / "nope")[
            "memory_limits_enforced"
        ]
        is None
    )
