"""``HealthMonitor`` is a facade over its owners (R29.10).

It used to be five jobs in one class and one file: inference accounting,
detector-state mailboxes, sink / queue / loss-epoch accounting, the OBD
scheduler passthrough and the host readings. The host readers and the CPU
sampler now live in ``daemon/host_metrics.py``, the sink accounting in
``daemon/sink_health.py``, and the seven mailboxes are one
``set_detector_block``. ``/status`` must not notice.

``test_a_driven_monitor_snapshots_exactly_as_it_did_before_the_split`` is that
claim. ``fixtures/health_snapshot_golden.json`` was produced by running
:func:`drive` against the monitor *before* the split (through the old
``observe_*`` setters), so a key, a value or the key order that moved shows up
as a diff against what ``/status`` actually served.
"""

from __future__ import annotations

import asyncio
import json
import re
import typing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import src.daemon.health as health_mod
from src.collection import capture_lifetime
from src.daemon import host_metrics, sink_health
from src.daemon.health import HealthMonitor, HostCpuSampler, VersionPair

GOLDEN = Path(__file__).parent / "fixtures" / "health_snapshot_golden.json"

SYSTEM = {
    "load_1m": 0.52,
    "cpu_count": 4.0,
    "load_per_cpu": 0.13,
    "mem_used_fraction": 0.4123,
    "mem_total_mb": 7943.5,
    "cpu_temp_c": 51.2,
}
MEMORY = {
    "memory_limits_enforced": True,
    "memory_max_bytes": 6442450944,
    "memory_max_source": "/sys/fs/cgroup/system.slice/canary.service/memory.max",
}
#: Under-voltage and throttling have occurred since boot (the 2026-05-24 reading).
THROTTLED = 0x50005


class Host:
    """A scripted ``/proc/stat`` and a clock, advanced together by :meth:`tick`."""

    def __init__(self) -> None:
        self.now = 0.0
        self.idle = 0.0
        self.total = 0.0

    def read(self) -> tuple[float, float]:
        return self.idle, self.total

    def clock(self) -> float:
        return self.now

    def sampler(self) -> HostCpuSampler:
        sampler = HostCpuSampler(
            interval_seconds=1.0, window_seconds=5.0, read=self.read, clock=self.clock
        )
        sampler.sample()
        return sampler

    def tick(self, busy: float, sampler: HostCpuSampler) -> None:
        self.now += 1.0
        self.total += 400.0
        self.idle += 400.0 * (1.0 - busy)
        sampler.sample()


class _SchedulerMetrics:
    """What the collector hands over: an object, not a mapping."""

    def to_dict(self) -> dict[str, Any]:
        return {"active_requests": 2, "requests": [{"channel": "RPM"}], "batching": {"on": True}}


def drive(mon: HealthMonitor, host: Host, stats: Path) -> dict[str, Any]:
    """One fixed sequence of calls, every job of the monitor, snapshotted four times."""
    sampler = mon.host_cpu
    out: dict[str, Any] = {"empty": mon.snapshot(now_epoch=1000.0)}

    # Inference accounting and the per-mode blind latch.
    edges = [
        mon.observe_inference(
            latency_ms=5.0 + i,
            missing_features=i % 3 == 0,
            score=i / 10,
            data_timestamp_epoch=990.0 + i,
            operating_mode="idle",
        )
        for i in range(10)
    ]
    edges += [
        mon.observe_inference(missing_features=True, operating_mode="cruise") for _ in range(8)
    ]
    mon.observe_inference(missing_features=False, operating_mode="city")
    mon.note_swap("swapped", version="if_v7")
    mon.note_swap("rejected")
    mon.note_swap("error")

    # The seven detector-state blocks.
    mon.set_detector_block(
        "model_version_mismatch", VersionPair("v1", "v2"), key="lstm_autoencoder"
    )
    mon.set_detector_block("model_version_mismatch", VersionPair("a", "b"), key="isolation_forest")
    mon.set_detector_block("model_version_mismatch", VersionPair("v3", "v3"), key="sequence")
    mon.set_detector_block("sequence_suppression", {"trained": ["a"], "serving": ["a", "b"]})
    mon.set_detector_block("sequence_cadence_refusal", "alert level chosen at another cadence")
    mon.set_detector_block("detector_roles", {"isolation_forest": {"effective": "alert"}})
    mon.set_detector_block("obd_scheduler", {"active_requests": 3, "requests": [{"c": "RPM"}]})
    mon.set_detector_block(
        "capture_profile_gate", {"scoring_allowed": False, "profile": "tuning", "source": "live"}
    )
    mon.set_detector_block("segmenter_fallback", True)

    # Sinks, queues and the receive paths.
    ingest: asyncio.Queue[int] = asyncio.Queue(maxsize=256)
    for item in range(3):
        ingest.put_nowait(item)
    mon.observe_telemetry_queue("ingest", ingest)
    ingest.get_nowait()
    mon.observe_telemetry_queue("ingest", ingest)
    mon.observe_telemetry_write("storage", records=100, elapsed_seconds=0.25, flushes=1)
    mon.observe_telemetry_write("storage", records=50, elapsed_seconds=0.5)
    mon.observe_telemetry_write("storage", records=10, elapsed_seconds=0.0)
    mon.observe_storage_publish("storage", {"publishes": 3, "max_publish_ms": 12.5})
    mon.note_telemetry_drop("storage", count=4)
    mon.note_telemetry_drop("storage")
    mon.note_telemetry_error("storage", RuntimeError("disk full"))
    mon.observe_receive_path(
        "can_receive",
        received=1000,
        parse_errors=2,
        buffer_dropped=3,
        buffer_high_water=40,
        buffer_capacity=512,
        queue_dropped=5,
        batches=90,
        reconnects=1,
    )
    mon.observe_collector_handoff(
        "obd_collect",
        accepted=500,
        buffer_dropped=1,
        buffer_high_water=7,
        buffer_capacity=128,
        queue_dropped=2,
        batches=60,
        raw_responses=None,
        derived_records=620,
    )

    # The host: two samples for a CPU value, and counters that grow between polls.
    host.tick(0.25, sampler)
    host.tick(1.0, sampler)
    (stats / "rx_over_errors").write_text("3")
    (stats / "rx_dropped").write_text("5")

    out["driven"] = mon.snapshot(
        now_epoch=1000.0,
        scaler={"columns": ["a", "b"], "means": {"a": 0.0, "b": 1.0}, "stds": {"a": 1.0, "b": 2.0}},
        live_means={"a": 10.0, "b": 1.5},
        lstm_thresholds={"global": {"mean": 0.1, "std": 0.05}},
        live_mean_recon_error=0.3,
    )
    out["properties"] = {
        "edges": edges,
        "if_blind_modes": list(mon.if_blind_modes),
        "if_missing_feature_rate_by_mode": mon.if_missing_feature_rate_by_mode,
        "segmenter_fallback_active": mon.segmenter_fallback_active,
        "capture_profile_gate": mon.capture_profile_gate,
    }
    epoch = mon.loss_epoch(now=50.0)
    inputs = mon.capture_quality_inputs(now=50.0)
    out["loss"] = {
        "counters": mon.loss_counters(),
        "epoch_monotonic": epoch.taken_at_monotonic,
        "epoch_counters": dict(epoch.counters),
        "inputs_epoch_counters": dict(inputs.loss_epoch.counters),
        "inputs_scheduler": inputs.scheduler,
        "inputs_lifetime": inputs.lifetime,
        "inputs_sinks": inputs.sinks,
    }

    # Blocks replaced and cleared, and the one sink queue wait that needs no loop.
    mon.set_detector_block(
        "model_version_mismatch", VersionPair("v2", "v2"), key="lstm_autoencoder"
    )
    mon.set_detector_block("sequence_suppression", None)
    mon.set_detector_block("sequence_cadence_refusal", None)
    mon.set_detector_block("detector_roles", None)
    mon.set_detector_block("obd_scheduler", _SchedulerMetrics())
    mon.set_detector_block(
        "capture_profile_gate", {"scoring_allowed": True, "profile": "default", "source": "live"}
    )
    mon.set_detector_block("segmenter_fallback", False)
    out["replaced"] = mon.snapshot(now_epoch=1000.0)
    out["replaced_properties"] = {
        "segmenter_fallback_active": mon.segmenter_fallback_active,
        "capture_profile_gate": mon.capture_profile_gate,
    }

    # A capture in flight: the lifetime block, and the same scheduler beside it.
    capture_lifetime.begin(0.0, profile="fuel-map", generation=2)
    capture_lifetime.observe("RPM", 1.0)
    out["capturing"] = mon.snapshot(now_epoch=1000.0)
    out["capturing_inputs_lifetime"] = mon.capture_quality_inputs(now=10.0).lifetime
    return out


@pytest.fixture
def driven_monitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> typing.Iterator[tuple[HealthMonitor, Host, Path]]:
    """A monitor whose host readings are scripted and whose clock is fixed."""
    monkeypatch.setattr(host_metrics, "system_metrics", lambda: dict(SYSTEM))
    monkeypatch.setattr(host_metrics, "memory_limit_status", lambda: dict(MEMORY))
    monkeypatch.setattr(host_metrics, "_read_throttled_raw", lambda: THROTTLED)
    # ``capture_quality_inputs`` and the capture-lifetime block read the facade's own clock.
    monkeypatch.setattr(health_mod, "time", SimpleNamespace(monotonic=lambda: 10.0))
    stats = tmp_path / "statistics"
    stats.mkdir()
    # Nonzero at the start, so "growth since the monitor first looked" is not
    # the same number as the counter.
    for name, value in (("rx_over_errors", "1"), ("rx_dropped", "2"), ("rx_errors", "0")):
        (stats / name).write_text(value)
    host = Host()
    capture_lifetime._reset_for_tests()
    try:
        yield HealthMonitor(can0_stats_path=stats, host_cpu=host.sampler()), host, stats
    finally:
        capture_lifetime._reset_for_tests()


def test_a_driven_monitor_snapshots_exactly_as_it_did_before_the_split(
    driven_monitor: tuple[HealthMonitor, Host, Path],
) -> None:
    mon, host, stats = driven_monitor
    transcript = json.loads(json.dumps(drive(mon, host, stats)))
    golden = json.loads(GOLDEN.read_text())
    assert transcript == golden
    # Dict equality ignores order, and ``/status`` is served in insertion order.
    assert json.dumps(transcript, indent=1) == json.dumps(golden, indent=1)


def test_the_golden_snapshot_exercises_every_block_it_pins(
    driven_monitor: tuple[HealthMonitor, Host, Path],
) -> None:
    """A golden that skips a key pins nothing about it."""
    mon, host, stats = driven_monitor
    out = drive(mon, host, stats)
    assert {
        "model_version_mismatches",
        "sequence_suppression",
        "sequence_cadence_refusal",
        "detector_roles",
        "obd_scheduler",
        "telemetry_sinks",
        "feature_drift",
        "reconstruction_error_drift",
        "can0",
    } <= set(out["driven"])
    assert set(out["driven"]["telemetry_sinks"]) == {
        "ingest",
        "storage",
        "can_receive",
        "obd_collect",
    }
    assert out["driven"]["system"]["cpu_percent"] == 0.625  # 25 % then 100 %
    assert out["driven"]["power"]["raw"] == THROTTLED
    assert "capture_lifetime" in out["capturing"]
    assert list(out["driven"]["model_version_mismatches"]) == [
        "isolation_forest",
        "lstm_autoencoder",
    ]


# ---------------------------------------------------------------------------
# set_detector_block
# ---------------------------------------------------------------------------


def _block_names() -> tuple[str, ...]:
    return typing.get_args(health_mod.DetectorBlock)


def test_the_block_registry_and_the_type_name_the_same_blocks() -> None:
    assert set(_block_names()) == set(health_mod.DETECTOR_BLOCKS)
    assert len(_block_names()) == 7  # the seven mailboxes this replaced


def test_an_unknown_block_is_refused_not_stored_under_a_typo() -> None:
    with pytest.raises(ValueError, match="unknown detector block"):
        HealthMonitor().set_detector_block("sequence_suppresion", {"a": 1})  # type: ignore[arg-type]


def test_only_the_per_manager_block_takes_a_key() -> None:
    mon = HealthMonitor()
    with pytest.raises(ValueError, match="needs a key"):
        mon.set_detector_block("model_version_mismatch", VersionPair("a", "b"))
    with pytest.raises(ValueError, match="takes no key"):
        mon.set_detector_block("detector_roles", {"x": 1}, key="lstm")


@pytest.mark.parametrize(
    ("block", "value", "snapshot_key"),
    [
        ("sequence_suppression", {"trained": ["a"]}, "sequence_suppression"),
        ("sequence_cadence_refusal", "another cadence", "sequence_cadence_refusal"),
        ("detector_roles", {"isolation_forest": {"effective": "shadow"}}, "detector_roles"),
        ("obd_scheduler", {"active_requests": 3}, "obd_scheduler"),
    ],
)
def test_a_block_appears_in_the_snapshot_until_it_is_cleared(
    block: str, value: object, snapshot_key: str
) -> None:
    mon = HealthMonitor()
    assert snapshot_key not in mon.snapshot(now_epoch=0.0)
    mon.set_detector_block(block, value)  # type: ignore[arg-type]
    assert mon.snapshot(now_epoch=0.0)[snapshot_key] == value
    mon.set_detector_block(block, None)  # type: ignore[arg-type]
    assert snapshot_key not in mon.snapshot(now_epoch=0.0)


def test_the_blocks_read_through_properties_are_cleared_by_none() -> None:
    mon = HealthMonitor()
    assert mon.capture_profile_gate is None
    mon.set_detector_block("capture_profile_gate", {"scoring_allowed": True})
    assert mon.capture_profile_gate == {"scoring_allowed": True}
    mon.set_detector_block("capture_profile_gate", None)
    assert mon.capture_profile_gate is None

    assert mon.segmenter_fallback_active is False
    mon.set_detector_block("segmenter_fallback", True)
    assert mon.segmenter_fallback_active is True
    mon.set_detector_block("segmenter_fallback", False)
    assert mon.segmenter_fallback_active is False


def test_a_stored_block_is_a_copy_and_so_is_what_the_snapshot_returns() -> None:
    """The caller's dict and the snapshot's dict are both free to be edited."""
    mon = HealthMonitor()
    scheduler = {"active_requests": 3}
    mon.set_detector_block("obd_scheduler", scheduler)
    scheduler["active_requests"] = 99
    snap = mon.snapshot(now_epoch=0.0)
    assert snap["obd_scheduler"] == {"active_requests": 3}
    snap["obd_scheduler"]["active_requests"] = 7
    assert mon.snapshot(now_epoch=0.0)["obd_scheduler"] == {"active_requests": 3}
    assert mon.capture_quality_inputs(now=0.0).scheduler == {"active_requests": 3}


def test_the_mismatch_block_takes_a_version_pair_not_a_mapping() -> None:
    """A mapping read with ``.get`` made a misspelt key mean "unknown", and an unknown
    side clears the entry: the partial-deploy detector off, silently."""
    mon = HealthMonitor()
    with pytest.raises(TypeError, match="VersionPair"):
        mon.set_detector_block(
            "model_version_mismatch", {"servng": "v1", "manifest": "v2"}, key="lstm"
        )
    with pytest.raises(TypeError):
        VersionPair(servng="v1", manifest="v2")  # type: ignore[call-arg]
    assert "model_version_mismatches" not in mon.snapshot(now_epoch=0.0)


def test_the_mismatch_block_clears_a_manager_whose_pair_agrees_or_is_unknown() -> None:
    mon = HealthMonitor()
    for serving, manifest in (("v1", "v2"), (None, "v2"), ("v1", None), ("v2", "v2")):
        mon.set_detector_block("model_version_mismatch", VersionPair("v1", "v2"), key="lstm")
        assert "lstm" in mon.snapshot(now_epoch=0.0)["model_version_mismatches"]
        mon.set_detector_block("model_version_mismatch", VersionPair(serving, manifest), key="lstm")
        if (serving, manifest) == ("v1", "v2"):
            assert "lstm" in mon.snapshot(now_epoch=0.0)["model_version_mismatches"]
        else:
            assert "model_version_mismatches" not in mon.snapshot(now_epoch=0.0)


# ---------------------------------------------------------------------------
# The names the monitor's importers use
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "CGROUP_ROOT",
        "DEFAULT_CAN0_STATS_PATH",
        "CaptureQualityInputs",
        "FeatureDriftReport",
        "HealthMonitor",
        "HostCpuSampler",
        "VersionPair",
        "_MODE_BLIND_WINDOW",
        "feature_drift",
        "memory_limit_status",
        "reconstruction_error_drift",
    ],
)
def test_every_name_importers_took_from_the_facade_is_still_there(name: str) -> None:
    assert hasattr(health_mod, name)


def test_the_moved_names_are_the_owners_own_objects() -> None:
    assert health_mod.HostCpuSampler is host_metrics.HostCpuSampler
    assert health_mod.DEFAULT_CAN0_STATS_PATH == host_metrics.DEFAULT_CAN0_STATS_PATH
    assert health_mod.CaptureQualityInputs is sink_health.CaptureQualityInputs
    assert sink_health.LossEpoch.__module__ == "src.storage.capture_losses"


@pytest.mark.parametrize(
    "name",
    [
        "_read_throttled_raw",
        "_run_sampler",
        "_read_cpu_times",
        "_cpu_temp_c",
        "system_metrics",
        "power_status",
        "can0_counters",
    ],
)
def test_a_host_reader_is_not_re_exported_where_a_patch_would_miss_it(name: str) -> None:
    """The facade module no longer looks these up, so a ``monkeypatch.setattr`` on it
    would apply and change nothing. It must fail to resolve instead (patch the owner,
    ``daemon.host_metrics``). The three public ones had one importer, ``test_health``,
    which now imports them from ``host_metrics``."""
    assert not hasattr(health_mod, name)
    assert hasattr(host_metrics, name)
    with pytest.raises(AttributeError):
        pytest.MonkeyPatch().setattr(health_mod, name, lambda: None)


def test_the_facade_hands_out_no_second_copy_of_the_host_sampler() -> None:
    sampler = HostCpuSampler(read=lambda: None)
    mon = HealthMonitor(host_cpu=sampler)
    assert mon.host_cpu is sampler
    assert not sampler.running  # a sampler handed in is the caller's to start


def test_the_host_sampler_is_handed_to_the_constructor_not_assigned_afterwards() -> None:
    """``host_cpu`` was a plain attribute ``_cpu_percent`` read; it is now the sampler
    ``HostMetrics`` owns, and assigning another would apply and change nothing."""
    mon = HealthMonitor()
    with pytest.raises(AttributeError):
        mon.host_cpu = HostCpuSampler(read=lambda: None)  # type: ignore[misc]


#: Re-exported by ``health`` for importers, but not looked up by anything in it
#: (``HostMetrics`` calls ``host_metrics.memory_limit_status``, the sampler is
#: built in ``host_metrics``, the default path is bound when the class body runs).
#: A ``monkeypatch.setattr`` on ``health`` for one of these applies and changes nothing.
_FACADE_ONLY_NAMES = (
    "memory_limit_status",
    "CGROUP_ROOT",
    "FeatureDriftReport",
    "HostCpuSampler",
    "DEFAULT_CAN0_STATS_PATH",
)
_FACADE_ONLY_ALTERNATION = "|".join(_FACADE_ONLY_NAMES)
_PATCH_THROUGH_THE_FACADE = re.compile(
    rf"""setattr\(\s*\w*health\w*\s*,\s*["'](?:{_FACADE_ONLY_ALTERNATION})["']"""
    rf"""|["']src\.daemon\.health\.(?:{_FACADE_ONLY_ALTERNATION})["']"""
)


def test_the_tripwire_for_a_patch_through_the_facade_can_fail() -> None:
    assert _PATCH_THROUGH_THE_FACADE.search(
        'monkeypatch.setattr(health_mod, "memory_limit_status", f)'
    )
    assert _PATCH_THROUGH_THE_FACADE.search('mock.patch("src.daemon.health.CGROUP_ROOT", tmp)')
    assert not _PATCH_THROUGH_THE_FACADE.search(
        'monkeypatch.setattr(host_metrics, "memory_limit_status", f)'
    )


def test_nothing_patches_a_name_the_facade_only_re_exports() -> None:
    repo = Path(__file__).resolve().parent.parent
    offenders = [
        f"{path.relative_to(repo)}:{number}"
        for root in ("src", "scripts", "tools", "tests")
        for path in sorted((repo / root).rglob("*.py"))
        if path != Path(__file__).resolve()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if _PATCH_THROUGH_THE_FACADE.search(line)
    ]
    assert offenders == []
