"""Tests for the daemon assembly (Milestone A)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.alerts.events import AnomalyEvent
from src.config import CanaryConfig, DaemonConfig
from src.daemon import (
    TelemetrySink,
    _record_to_dict,
    build_components,
    build_live_feature_source,
    drain_queue_with_broadcast,
    poll_model_managers,
    run_retention_sweeper,
    run_storage_sink,
    run_telemetry_fanout,
)
from src.dashboard.broadcast import Broadcaster
from src.models.health import HealthMonitor
from src.models.manager import ModelManager
from src.models.manifest import CURRENT_MANIFEST_FORMAT_VERSION
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config_yaml(tmp_path: Path) -> str:
    """Minimal canary.yaml content pointing every dir at tmp_path."""
    return f"""
vehicle:
  make: Nissan
  model: Xterra
  year: 2008
  vin: ""
hardware:
  elm327_port: /dev/ttyUSB0
  can:
    interface: virtual
    channel: vcan0
    bitrate: 500000
collection:
  obd_poll_rate_hz: 1
  storage_path: {tmp_path / "storage"}
  session_retention_days: 30
  storage_warning_mb: 500
preprocessing:
  resample_hz: 1
  rolling_window_seconds: 30
  operating_modes:
    idle:
      rpm_max: 900
      speed_max: 0
models:
  isolation_forest:
    enabled: false
    n_estimators: 100
    contamination: 0.01
  lstm_autoencoder:
    enabled: false
    sequence_length: 60
    latent_dim: 16
    threshold_k: 3.0
dashboard:
  host: 127.0.0.1
  port: 0
daemon:
  anomalies_dir: {tmp_path / "anomalies"}
  models_dir: {tmp_path / "models"}
  model_poll_seconds: 0.05
  retention_sweep_seconds: 0.05
  broadcaster_queue_size: 16
  catalog_db: {tmp_path / "control" / "catalog.db"}
"""


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(tmp_path))
    return CanaryConfig.from_yaml(cfg_path)


# ---------------------------------------------------------------------------
# build_components
# ---------------------------------------------------------------------------


def test_build_components_creates_anomalies_dir(config: CanaryConfig) -> None:
    components = build_components(config)
    assert config.daemon.anomalies_dir.exists()
    assert components.event_logger.directory == config.daemon.anomalies_dir


def test_build_components_constructs_two_broadcasters(config: CanaryConfig) -> None:
    components = build_components(config)
    assert components.telemetry_broadcaster is not components.anomalies_broadcaster
    assert components.telemetry_broadcaster.queue_size == config.daemon.broadcaster_queue_size


def test_emitter_publishes_to_anomalies_broadcaster_when_logging(
    config: CanaryConfig,
) -> None:
    """The broadcasting wrapper around AnomalyEventLogger fans events to both."""
    components = build_components(config)
    sub = components.anomalies_broadcaster.subscribe()

    async def _exercise() -> dict[str, Any]:
        event = AnomalyEvent(
            timestamp="2026-05-13T12:00:00+00:00",
            source="lstm_autoencoder",
            model_version="v1",
            score=5.0,
            threshold=3.0,
        )
        # The emitter's logger is the wrapper; calling .log() drives both paths.
        components.emitter._logger.log(event)  # type: ignore[attr-defined]
        return await asyncio.wait_for(sub.get(), timeout=1.0)

    published = asyncio.run(_exercise())
    assert published["source"] == "lstm_autoencoder"
    # And the JSONL was written
    assert any(config.daemon.anomalies_dir.glob("anomalies-*.jsonl"))


def test_build_components_skips_managers_when_models_disabled(
    config: CanaryConfig,
) -> None:
    components = build_components(config)
    assert components.model_managers == {}


def test_build_components_constructs_if_manager_when_enabled(
    tmp_path: Path,
) -> None:
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(
        _config_yaml(tmp_path).replace(
            "  isolation_forest:\n    enabled: false", "  isolation_forest:\n    enabled: true"
        )
    )
    config = CanaryConfig.from_yaml(cfg_path)
    components = build_components(config)
    assert "isolation_forest" in components.model_managers
    # Without an artifact on disk, current is None — the manager keeps polling.
    assert components.model_managers["isolation_forest"].current is None


# ---------------------------------------------------------------------------
# drain_queue_with_broadcast
# ---------------------------------------------------------------------------


async def test_telemetry_fanout_delivers_one_record_to_every_sink() -> None:
    ingest: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    storage: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=8)
    live: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=8)
    stop = asyncio.Event()
    record = TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm")
    await ingest.put(record)
    stop.set()

    await run_telemetry_fanout(
        ingest,
        [
            TelemetrySink("storage", storage, "drop_newest"),
            TelemetrySink("live", live, "drop_oldest"),
        ],
        stop,
    )

    assert storage.get_nowait() is record
    assert live.get_nowait() is record


async def test_telemetry_fanout_drops_oldest_for_live_overflow() -> None:
    ingest: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    live: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=1)
    stop = asyncio.Event()
    health = HealthMonitor()
    for i in range(3):
        await ingest.put(TelemetryRecord.obd(1_778_000_000.0 + i, "RPM", float(700 + i), "rpm"))
    stop.set()

    await run_telemetry_fanout(
        ingest,
        [TelemetrySink("live", live, "drop_oldest")],
        stop,
        health_monitor=health,
    )

    assert live.get_nowait().value == 702.0
    assert health.snapshot()["telemetry_sinks"]["live"]["dropped_records"] == 2


async def test_telemetry_fanout_marks_storage_overflow_as_explicit_loss() -> None:
    ingest: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    storage: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=1)
    stop = asyncio.Event()
    health = HealthMonitor()
    for i in range(3):
        await ingest.put(TelemetryRecord.obd(1_778_000_000.0 + i, "RPM", float(700 + i), "rpm"))
    stop.set()

    await run_telemetry_fanout(
        ingest,
        [TelemetrySink("storage", storage, "drop_newest")],
        stop,
        health_monitor=health,
    )

    assert storage.get_nowait().value == 700.0
    assert health.snapshot()["telemetry_sinks"]["storage"]["dropped_records"] == 2


async def test_storage_sink_drains_backlog_in_bounded_batches() -> None:
    class _RecordingWriter(AsyncParquetTelemetryWriter):
        def __init__(self) -> None:
            self.calls: list[list[TelemetryRecord]] = []

        async def write_many(self, records: Any) -> list[Path]:
            self.calls.append(list(records))
            return []

        async def flush_due(self) -> bool:
            return False

        async def flush(self) -> Path | None:
            return None

        async def close(self) -> Path | None:
            return None

    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    for i in range(2500):
        queue.put_nowait(TelemetryRecord.can(1_778_000_000.0 + i, 0x123, b"12345678"))
    stop = asyncio.Event()
    stop.set()
    health = HealthMonitor()
    writer = _RecordingWriter()

    await run_storage_sink(
        queue,
        writer,
        stop,
        health_monitor=health,
        write_batch_size=1000,
    )

    assert [len(batch) for batch in writer.calls] == [1000, 1000, 500]
    assert queue.empty()
    metrics = health.snapshot()["telemetry_sinks"]["storage"]
    assert metrics["write_calls"] == 3
    assert metrics["records_processed"] == 2500
    assert metrics["max_batch_size"] == 1000


async def test_storage_sink_coalesces_records_arriving_just_after_first() -> None:
    class _RecordingWriter(AsyncParquetTelemetryWriter):
        def __init__(self) -> None:
            self.calls: list[list[TelemetryRecord]] = []

        async def write_many(self, records: Any) -> list[Path]:
            self.calls.append(list(records))
            return []

        async def flush_due(self) -> bool:
            return False

        async def flush(self) -> Path | None:
            return None

        async def close(self) -> Path | None:
            return None

    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    queue.put_nowait(TelemetryRecord.can(1_778_000_000.0, 0x123, b"12345678"))
    stop = asyncio.Event()
    writer = _RecordingWriter()
    task = asyncio.create_task(
        run_storage_sink(
            queue,
            writer,
            stop,
            write_batch_size=100,
            write_batch_max_wait_seconds=0.05,
        )
    )

    await asyncio.sleep(0.005)
    for i in range(1, 5):
        queue.put_nowait(TelemetryRecord.can(1_778_000_000.0 + i, 0x123, b"12345678"))
    stop.set()
    await task

    assert [len(batch) for batch in writer.calls] == [5]


async def test_drain_publishes_each_record_to_broadcaster(tmp_path: Path) -> None:
    from src.dashboard.broadcast import Broadcaster

    writer = ParquetTelemetryWriter(tmp_path / "storage")
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    bus = Broadcaster()
    sub = bus.subscribe()

    async def _producer() -> None:
        for value in (700.0, 720.0, 750.0):
            await queue.put(TelemetryRecord.obd(1_778_000_000.0 + value, "RPM", value, "rpm"))
        await asyncio.sleep(0.05)
        stop.set()

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(queue, writer, stop, telemetry_broadcaster=bus)
    )
    await _producer()
    await drain_task

    received = []
    while not sub.queue.empty():
        received.append(sub.queue.get_nowait())
    assert [r["value"] for r in received] == [700.0, 720.0, 750.0]
    # Parquet got the same data
    parquet_files = list((tmp_path / "storage").rglob("*.parquet"))
    assert parquet_files, "expected at least one Parquet chunk written"


async def test_drain_with_no_broadcaster_still_writes_to_parquet(tmp_path: Path) -> None:
    """The broadcaster is optional; the Parquet write path stays correct."""
    writer = ParquetTelemetryWriter(tmp_path / "storage")
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()

    async def _producer() -> None:
        await queue.put(TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm"))
        await asyncio.sleep(0.05)
        stop.set()

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(queue, writer, stop, telemetry_broadcaster=None)
    )
    await _producer()
    await drain_task

    assert list((tmp_path / "storage").rglob("*.parquet"))


async def test_drain_gps_trip_end_edge_calls_on_trip_ended(tmp_path: Path) -> None:
    """Task 5.4: the sustained moving->stationary GPS edge reaches the
    ``on_trip_ended`` hook, called with the active session path — the
    normal (non-fallback) trigger."""
    from src.analysis.gps_tracker import GpsTracker

    writer = ParquetTelemetryWriter(tmp_path / "storage")
    gps_tracker = GpsTracker(trip_end_stationary_seconds=10.0)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    calls: list[Path] = []

    async def _producer() -> None:
        await queue.put(TelemetryRecord.gps(0.0, "speed", 40.0, "km/h"))  # start moving
        await queue.put(TelemetryRecord.gps(1.0, "speed", 0.0, "km/h"))  # stop
        await queue.put(TelemetryRecord.gps(15.0, "speed", 0.0, "km/h"))  # 14s stationary -> edge
        await asyncio.sleep(0.1)
        stop.set()

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(
            queue,
            writer,
            stop,
            gps_tracker=gps_tracker,
            active_session_path=lambda: writer.paths.root,
            on_trip_ended=calls.append,
        )
    )
    await _producer()
    await drain_task

    assert calls == [writer.paths.root]


async def test_drain_keeps_publishing_even_under_subscriber_backpressure(
    tmp_path: Path,
) -> None:
    """A slow WebSocket client must not stall the writer."""
    from src.dashboard.broadcast import Broadcaster

    writer = ParquetTelemetryWriter(tmp_path / "storage")
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    bus = Broadcaster(queue_size=2)
    sub = bus.subscribe()

    async def _producer() -> None:
        for i in range(20):
            await queue.put(TelemetryRecord.obd(1_778_000_000.0 + i, "RPM", float(700 + i), "rpm"))
        await asyncio.sleep(0.1)
        stop.set()

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(queue, writer, stop, telemetry_broadcaster=bus)
    )
    await _producer()
    await drain_task

    # The subscriber missed some messages but the queue was drained.
    assert sub.dropped >= 10
    assert queue.empty()


# ---------------------------------------------------------------------------
# poll_model_managers
# ---------------------------------------------------------------------------


def _write_manifest_pair(artifact: Path, *, version: str, mtime: float) -> None:
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(f"artifact:{version}")
    manifest = artifact.with_suffix(".manifest.json")
    feature_columns = ["obd:RPM"]
    manifest.write_text(
        json.dumps(
            {
                "artifact_format_version": CURRENT_MANIFEST_FORMAT_VERSION,
                "model_type": "isolation_forest",
                "model_version": version,
                "created_at": "2026-05-13T12:00:00+00:00",
                "training_data_hash": "sha256:abc",
                "input_rows": 1,
                "hyperparams": {},
                "schema": {
                    "feature_spec_version": 1,
                    "feature_columns": feature_columns,
                    "feature_dtypes": {"obd:RPM": "float64"},
                    "source_prefixes": ["obd"],
                    "behavior_model_columns": [],
                    "feature_health": {
                        "row_count": 1,
                        "candidate_feature_count": len(feature_columns),
                        "selected_feature_count": len(feature_columns),
                        "dropped_all_nan_columns": [],
                        "all_nan_columns": [],
                        "constant_columns": ["obd:RPM"],
                        "non_finite_columns": [],
                        "columns": {
                            "obd:RPM": {
                                "dtype": "float64",
                                "non_null_count": 1,
                                "finite_count": 1,
                                "nan_count": 0,
                                "non_finite_count": 0,
                                "unique_finite_count": 1,
                                "constant": True,
                            }
                        },
                    },
                },
            }
        )
    )
    import os

    os.utime(manifest, (mtime, mtime))


async def test_poll_swaps_when_new_manifest_appears(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    _write_manifest_pair(artifact, version="v1", mtime=1_000_000.0)

    def _loader(p: Path) -> dict:
        return {"contents": p.read_text()}

    manager: ModelManager[dict] = ModelManager(artifact, loader=_loader)
    manager.load_initial()
    assert manager.current is not None and manager.current.manifest.model_version == "v1"

    _write_manifest_pair(artifact, version="v2", mtime=2_000_000.0)

    stop = asyncio.Event()

    async def _stop_after_one_poll() -> None:
        await asyncio.sleep(0.15)
        stop.set()

    poll_task = asyncio.create_task(
        poll_model_managers({"if": manager}, stop, interval_seconds=0.05)
    )
    await _stop_after_one_poll()
    await asyncio.wait_for(poll_task, timeout=1.0)

    assert manager.current is not None
    assert manager.current.manifest.model_version == "v2"


async def test_poll_swallows_manager_exceptions(tmp_path: Path) -> None:
    """A blowing-up manager.poll() must not kill the polling task."""

    class _BoomManager:
        def poll(self) -> Any:
            raise RuntimeError("kaboom")

    stop = asyncio.Event()

    async def _stop_soon() -> None:
        await asyncio.sleep(0.15)
        stop.set()

    poll_task = asyncio.create_task(
        poll_model_managers({"if": _BoomManager()}, stop, interval_seconds=0.05)  # type: ignore[dict-item]
    )
    await _stop_soon()
    # Task should still terminate cleanly via stop_event, despite the manager's exception.
    await asyncio.wait_for(poll_task, timeout=1.0)


# ---------------------------------------------------------------------------
# Retention sweeper
# ---------------------------------------------------------------------------


async def test_retention_sweeper_deletes_expired_sessions_and_keeps_active(
    tmp_path: Path,
) -> None:
    old = tmp_path / "2000-01-01T000000"
    active = tmp_path / "2000-01-02T000000"
    old.mkdir()
    active.mkdir()
    (old / "telemetry-00000.parquet").write_bytes(b"old")
    (active / "telemetry-00000.parquet").write_bytes(b"active")
    stop = asyncio.Event()

    async def _wait_until_deleted() -> None:
        for _ in range(20):
            if not old.exists():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("retention sweeper did not delete expired session")

    task = asyncio.create_task(
        run_retention_sweeper(
            tmp_path,
            retention_days=1,
            stop_event=stop,
            interval_seconds=0.05,
            active_session_path=lambda: active,
        )
    )
    try:
        await asyncio.wait_for(_wait_until_deleted(), timeout=2.0)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert not old.exists()
    assert active.exists()


async def test_retention_sweeper_enforces_byte_cap_and_keeps_active(
    tmp_path: Path,
) -> None:
    # retention_days is huge so the age sweep never fires; the byte cap is
    # the only thing that can evict here.
    old = tmp_path / "2099-01-01T000000"
    active = tmp_path / "2099-01-02T000000"
    old.mkdir()
    active.mkdir()
    (old / "telemetry-00000.parquet").write_bytes(b"\0" * (2 * 1024 * 1024))
    (active / "telemetry-00000.parquet").write_bytes(b"\0" * (2 * 1024 * 1024))
    stop = asyncio.Event()

    async def _wait_until_deleted() -> None:
        for _ in range(40):
            if not old.exists():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("retention sweeper did not enforce the byte cap")

    task = asyncio.create_task(
        run_retention_sweeper(
            tmp_path,
            retention_days=36500,
            stop_event=stop,
            interval_seconds=0.05,
            active_session_path=lambda: active,
            storage_max_mb=3,
        )
    )
    try:
        await asyncio.wait_for(_wait_until_deleted(), timeout=2.0)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert not old.exists()
    assert active.exists()


async def test_retention_sweeper_honors_interval(tmp_path: Path) -> None:
    """The sweeper waits interval_seconds between passes (no busy-loop)."""
    expired1 = tmp_path / "2000-01-01T000000"
    active = tmp_path / "2099-01-01T000000"
    expired1.mkdir()
    active.mkdir()
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_retention_sweeper(
            tmp_path,
            retention_days=1,
            stop_event=stop,
            interval_seconds=30.0,
            active_session_path=lambda: active,
        )
    )
    try:
        # The first sweep runs immediately on loop entry.
        for _ in range(40):
            if not expired1.exists():
                break
            await asyncio.sleep(0.02)
        assert not expired1.exists(), "first (immediate) sweep should delete expired1"

        # A second expired dir created now must survive: the next sweep is
        # 30 s out, far beyond this bounded wait.
        expired2 = tmp_path / "2000-01-02T000000"
        expired2.mkdir()
        await asyncio.sleep(0.4)
        assert expired2.exists(), "interval not honored: swept again before interval elapsed"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)


async def test_retention_sweeper_runs_age_and_cap_in_one_tick(tmp_path: Path) -> None:
    """A single sweep applies both the age sweep and the byte cap."""
    now = datetime.now(UTC)

    def _name(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H%M%S")

    def _session(name: str, mib: int) -> Path:
        session = tmp_path / name
        session.mkdir()
        (session / "telemetry-00000.parquet").write_bytes(b"\0" * (mib * 1024 * 1024))
        return session

    expired = _session("2000-01-01T000000", 1)  # age-expired
    fresh_old = _session(_name(now - timedelta(seconds=30)), 1)
    fresh_mid = _session(_name(now - timedelta(seconds=20)), 1)
    fresh_new = _session(_name(now - timedelta(seconds=10)), 1)
    active = _session(_name(now + timedelta(seconds=10)), 1)
    stop = asyncio.Event()

    async def _wait() -> None:
        for _ in range(80):
            if not (expired.exists() or fresh_old.exists() or fresh_mid.exists()):
                return
            await asyncio.sleep(0.05)
        raise AssertionError("age+cap sweep did not converge")

    task = asyncio.create_task(
        run_retention_sweeper(
            tmp_path,
            retention_days=1,
            stop_event=stop,
            interval_seconds=0.05,
            active_session_path=lambda: active,
            storage_max_mb=2,
        )
    )
    try:
        await asyncio.wait_for(_wait(), timeout=5.0)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert not expired.exists()  # removed by the age sweep
    assert not fresh_old.exists()  # evicted oldest-first by the byte cap
    assert not fresh_mid.exists()  # evicted by the byte cap
    assert fresh_new.exists()  # cap satisfied before reaching this one
    assert active.exists()  # never evicted


async def test_retention_sweeper_protects_active_across_rotation(tmp_path: Path) -> None:
    """active_session_path is read live each tick: after an engine-run-time
    rotation moves the active dir, the new active is protected and the
    formerly-active dir becomes collectable."""
    first = tmp_path / "2000-01-01T000000"
    other = tmp_path / "2000-01-02T000000"
    first.mkdir()
    other.mkdir()
    (first / "telemetry-00000.parquet").write_bytes(b"first")
    (other / "telemetry-00000.parquet").write_bytes(b"other")
    active_holder: dict[str, Path] = {"path": first}
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_retention_sweeper(
            tmp_path,
            retention_days=1,
            stop_event=stop,
            interval_seconds=0.05,
            active_session_path=lambda: active_holder["path"],
        )
    )
    try:
        # Both dirs are age-expired, but `first` is the live session, so only
        # `other` is collectable while `first` is active.
        for _ in range(60):
            if not other.exists():
                break
            await asyncio.sleep(0.05)
        assert not other.exists(), "expired non-active dir was not removed"
        assert first.exists(), "active dir must never be deleted"

        # Emulate rotate_session(): the writer moved to a fresh dir. The
        # sweeper reads active_session_path() live, so `first` is no longer
        # protected and the new dir is.
        rotated = tmp_path / "2000-01-03T000000"
        rotated.mkdir()
        (rotated / "telemetry-00000.parquet").write_bytes(b"rotated")
        active_holder["path"] = rotated

        for _ in range(60):
            if not first.exists():
                break
            await asyncio.sleep(0.05)
        assert not first.exists(), "formerly-active dir not collected after rotation"
        assert rotated.exists(), "the post-rotation active dir must be protected"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)


async def test_retention_sweeper_emergency_evicts_below_warning(tmp_path: Path) -> None:
    """The acting low-space guard fires when free space is below the warning
    threshold, evicting oldest non-pinned/non-protected dirs while preserving
    pinned + active. retention_days is huge and storage_max_mb is unset so the
    age sweep and byte cap are no-ops — only the emergency path can act here.
    The warning threshold is set absurdly high so the real free_space_mb is
    always below it (the trigger condition) without faking the filesystem."""
    old = tmp_path / "2099-01-01T000000"
    pinned = tmp_path / "2099-01-02T000000"
    active = tmp_path / "2099-01-03T000000"
    for session in (old, pinned, active):
        session.mkdir()
        (session / "telemetry-00000.parquet").write_bytes(b"x")
    (pinned / ".canary-pin").write_text("{}\n")
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_retention_sweeper(
            tmp_path,
            retention_days=36500,
            stop_event=stop,
            interval_seconds=0.05,
            active_session_path=lambda: active,
            storage_max_mb=None,
            storage_warning_mb=10**9,  # ~1 PB: always above real free space
        )
    )
    try:
        for _ in range(60):
            if not old.exists():
                break
            await asyncio.sleep(0.05)
        assert not old.exists(), "emergency guard did not evict below the warning threshold"
        assert pinned.exists(), "emergency guard must never evict a pinned dir"
        assert active.exists(), "emergency guard must never evict the active dir"
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)


# ---------------------------------------------------------------------------
# DaemonConfig defaults
# ---------------------------------------------------------------------------


def test_daemon_config_defaults_are_sane() -> None:
    """Existing canary.yaml files without a `daemon:` block must keep loading."""
    cfg = DaemonConfig()
    assert cfg.anomalies_dir == Path("data/anomalies")
    assert cfg.models_dir == Path("models")
    assert cfg.model_poll_seconds == 30.0
    assert cfg.retention_sweep_seconds == 3600.0
    assert cfg.broadcaster_queue_size == 100


def test_canary_yaml_without_daemon_section_loads(tmp_path: Path) -> None:
    """The example config has no `daemon:` block; it must still load."""
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    assert isinstance(config.daemon, DaemonConfig)
    # uses the dataclass defaults
    assert config.daemon.model_poll_seconds == 30.0


# ---------------------------------------------------------------------------
# End-to-end: run_daemon with fake collectors + real dashboard + real broadcaster
# ---------------------------------------------------------------------------


async def test_run_daemon_starts_retention_sweeper_and_protects_active_session(
    config: CanaryConfig,
) -> None:
    from src.daemon import run_daemon

    config.collection.session_retention_days = 1
    config.daemon.retention_sweep_seconds = 0.05
    old = config.collection.storage_path / "2000-01-01T000000"
    old.mkdir(parents=True)
    (old / "telemetry-00000.parquet").write_bytes(b"old")
    writer = ParquetTelemetryWriter(
        config.collection.storage_path,
        session_started_at=datetime(2000, 1, 2, tzinfo=UTC),
    )
    active = writer.paths.root
    (active / "telemetry-00000.parquet").write_bytes(b"active")
    components = build_components(config)
    stop = asyncio.Event()

    async def _run() -> None:
        await run_daemon(
            config,
            stop,
            collectors=[],
            writer=writer,
            components=components,
            enable_live_inference=False,
        )

    async def _wait_until_deleted() -> None:
        for _ in range(40):
            if not old.exists():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("run_daemon retention sweeper did not delete expired session")

    daemon_task = asyncio.create_task(_run())
    try:
        await asyncio.wait_for(_wait_until_deleted(), timeout=3.0)
    finally:
        stop.set()
        await asyncio.wait_for(daemon_task, timeout=5.0)

    assert not old.exists()
    assert active.exists()


async def test_run_daemon_fires_trip_end_fallback_on_shutdown(
    config: CanaryConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 5.4's daemon-shutdown fallback trigger: run_daemon must call the
    trip-end hook for the active session when it stops, independent of
    whether GPS ever fired its own sustained-stop edge."""
    from src.daemon import runtime as runtime_mod

    calls: list[Path] = []

    def _spy(
        session_dir: Path,
        cfg: CanaryConfig,
        **kwargs: object,
    ) -> None:
        calls.append(session_dir)

    monkeypatch.setattr(runtime_mod, "handle_trip_end", _spy)

    components = build_components(config)
    writer = ParquetTelemetryWriter(config.collection.storage_path)
    stop = asyncio.Event()

    async def _run() -> None:
        await runtime_mod.run_daemon(
            config,
            stop,
            collectors=[],
            writer=writer,
            components=components,
            enable_live_inference=False,
        )

    daemon_task = asyncio.create_task(_run())
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(daemon_task, timeout=5.0)

    assert calls == [writer.paths.root]


async def test_run_daemon_propagates_collector_failure_without_hanging(
    config: CanaryConfig,
) -> None:
    """Regression: a collector that raises must drive run_daemon to *exit* —
    propagate the exception so asyncio.run returns non-zero and systemd's
    Restart=on-failure fires — not wedge in shutdown (uvicorn "Waiting for
    background tasks to complete", a stuck drain). The outer wait_for is the
    teeth: if the shutdown path hangs again this fails on timeout rather than
    hanging the whole suite. See project_daemon_shutdown_hang."""
    from src.daemon import run_daemon

    components = build_components(config)
    stop = asyncio.Event()

    async def _boom(_queue: asyncio.Queue[Any], _stop: asyncio.Event) -> None:
        raise RuntimeError("collector boom")

    with pytest.raises(RuntimeError, match="collector boom"):
        await asyncio.wait_for(
            run_daemon(
                config,
                stop,
                collectors=[_boom],
                components=components,
                enable_live_inference=False,
            ),
            timeout=20.0,
        )


async def test_serve_in_background_force_exits_when_server_wont_stop(
    config: CanaryConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: if uvicorn ignores should_exit and keeps "waiting for
    background tasks" (an open WebSocket), serve_in_background must still return
    by force-exiting + cancelling, not block forever. Simulated with a fake
    server whose serve() only ends on cancellation."""
    import src.daemon as daemon_mod
    from src.daemon import serve_in_background

    monkeypatch.setattr(daemon_mod, "DASHBOARD_SHUTDOWN_SECONDS", 0.2)
    components = build_components(config)

    class _WedgedServer:
        def __init__(self) -> None:
            self.started = True
            self.should_exit = False
            self.force_exit = False

        async def serve(self) -> None:
            # Ignores should_exit — only stops when cancelled (the safety net).
            await asyncio.Event().wait()

    wedged = _WedgedServer()

    async def _use() -> None:
        async with serve_in_background(components, server_factory=lambda config: wedged):
            pass

    # Returns well inside the bound; if the force-exit path regresses this
    # times out (fails) instead of hanging the suite.
    await asyncio.wait_for(_use(), timeout=5.0)
    assert wedged.force_exit is True


async def test_run_daemon_wires_retention_sweeper_task(
    config: CanaryConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the assembled run_daemon task set must include the
    retention sweeper, threaded with the configured cap, retention, interval,
    and the *live* active-session callable. This is the exact missing-wiring
    gap that let the unbounded-storage bug ship (Phase 8 / R13)."""
    import src.daemon as daemon_mod
    from src.daemon import run_daemon

    config.collection.session_retention_days = 7
    config.collection.storage_max_mb = 1234
    config.collection.storage_warning_mb = 567
    config.daemon.retention_sweep_seconds = 42.0

    captured: dict[str, Any] = {}

    async def _spy(
        storage_path: Path,
        retention_days: int,
        stop_event: asyncio.Event,
        *,
        interval_seconds: float,
        active_session_path: Callable[[], Path],
        storage_max_mb: int | None = None,
        storage_warning_mb: int | None = None,
        replay_retention_days: int | None = None,
        active_replay_dir: Callable[[], Path | None] | None = None,
        catalog_db: Path | None = None,
    ) -> None:
        captured.update(
            storage_path=storage_path,
            retention_days=retention_days,
            interval_seconds=interval_seconds,
            storage_max_mb=storage_max_mb,
            storage_warning_mb=storage_warning_mb,
            active_session_path=active_session_path(),
            replay_retention_days=replay_retention_days,
            active_replay_dir=active_replay_dir() if active_replay_dir else None,
            catalog_db=catalog_db,
        )
        await stop_event.wait()

    monkeypatch.setattr(daemon_mod, "run_retention_sweeper", _spy)

    components = build_components(config)
    writer = ParquetTelemetryWriter(config.collection.storage_path)
    stop = asyncio.Event()

    async def _run() -> None:
        await run_daemon(
            config,
            stop,
            collectors=[],
            writer=writer,
            components=components,
            enable_live_inference=False,
        )

    daemon_task = asyncio.create_task(_run())
    try:
        for _ in range(60):
            if captured:
                break
            await asyncio.sleep(0.02)
        assert captured, "run_daemon did not start the retention sweeper task"
    finally:
        stop.set()
        await asyncio.wait_for(daemon_task, timeout=5.0)

    assert captured["storage_path"] == config.collection.storage_path
    assert captured["retention_days"] == 7
    assert captured["interval_seconds"] == 42.0
    assert captured["storage_max_mb"] == 1234
    assert captured["storage_warning_mb"] == 567
    assert captured["active_session_path"] == writer.paths.root
    assert captured["catalog_db"] == config.daemon.catalog_db


# ---------------------------------------------------------------------------
# Online-training scheduler task (Task 3.2, online-training-strategy.md)
# ---------------------------------------------------------------------------


async def test_run_daemon_does_not_start_online_training_scheduler_by_default(
    config: CanaryConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`online_training.enabled` defaults false (hard pre-made decision) — an
    unconfigured daemon must not spin up the scheduler task at all."""
    import src.daemon.runtime as runtime_mod

    called = False

    async def _spy(*_a: object, **_k: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(runtime_mod, "run_online_training_scheduler", _spy)
    assert config.online_training.enabled is False

    components = build_components(config)
    writer = ParquetTelemetryWriter(config.collection.storage_path)
    stop = asyncio.Event()

    async def _run() -> None:
        await runtime_mod.run_daemon(
            config,
            stop,
            collectors=[],
            writer=writer,
            components=components,
            enable_live_inference=False,
        )

    daemon_task = asyncio.create_task(_run())
    try:
        await asyncio.sleep(0.1)
    finally:
        stop.set()
        await asyncio.wait_for(daemon_task, timeout=5.0)
    assert called is False


async def test_run_daemon_wires_online_training_scheduler_when_enabled(
    config: CanaryConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.daemon.runtime as runtime_mod

    config.online_training.enabled = True
    config.online_training.interval_seconds = 42.0
    config.online_training.window_days = 14
    config.online_training.poll_interval_seconds = 3.0

    captured: dict[str, Any] = {}

    async def _spy(
        job_controller: object,
        stop_event: asyncio.Event,
        *,
        interval_seconds: float,
        window_days: int,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        captured.update(
            job_controller=job_controller,
            interval_seconds=interval_seconds,
            window_days=window_days,
            poll_interval_seconds=poll_interval_seconds,
        )
        await stop_event.wait()

    monkeypatch.setattr(runtime_mod, "run_online_training_scheduler", _spy)

    components = build_components(config)
    writer = ParquetTelemetryWriter(config.collection.storage_path)
    stop = asyncio.Event()

    async def _run() -> None:
        await runtime_mod.run_daemon(
            config,
            stop,
            collectors=[],
            writer=writer,
            components=components,
            enable_live_inference=False,
        )

    daemon_task = asyncio.create_task(_run())
    try:
        for _ in range(60):
            if captured:
                break
            await asyncio.sleep(0.02)
        assert captured, "run_daemon did not start the online-training scheduler task"
    finally:
        stop.set()
        await asyncio.wait_for(daemon_task, timeout=5.0)

    assert captured["job_controller"] is components.job_controller
    assert captured["interval_seconds"] == 42.0
    assert captured["window_days"] == 14
    assert captured["poll_interval_seconds"] == 3.0


# ---------------------------------------------------------------------------
# Live inference task
# ---------------------------------------------------------------------------


async def test_run_live_inference_sequence_detector_emits_on_full_window(
    config: CanaryConfig,
) -> None:
    """Stub LSTM that always reports is_anomaly=True after a full window.

    Verifies the schema-alignment path through ``run_live_inference``'s
    ``SequenceDetector`` (Task 3.1 — this used to be the standalone
    ``run_live_inference_lstm`` task, deleted as production-dead in 4.1):
    the live source has all needed columns → buffer fills → engine.observe
    returns a result → emitter fires → anomalies broadcaster sees the event.
    """
    from collections import namedtuple

    from src.daemon import build_live_feature_source, run_live_inference
    from src.models.manager import LoadedModel, SwapAttempt
    from src.models.manifest import ModelManifestCommon

    components = build_components(config)
    LSTMResult = namedtuple(
        "LSTMResult",
        [
            "is_anomaly",
            "timestamp",
            "model_version",
            "global_score",
            "global_threshold",
            "anomalous_channels",
            "channel_scores",
            "channel_thresholds",
            "session",
            "window_start_timestamp",
            "window_end_timestamp",
            "latency_ms",
        ],
    )

    class _FakeLSTMEngine:
        """Stand-in for SequenceInferenceEngine: returns a result on every observe."""

        feature_columns = ("obd:RPM",)

        def __init__(self, _artifact: object, **_kwargs: Any) -> None:
            # ``**_kwargs`` swallows new keyword arguments the production
            # engine grows (e.g. ``suppress_channels``) so this stub doesn't
            # need a hand-edit every time the real signature evolves.
            self._ticks = 0

        def observe(self, _row: Any) -> Any:
            self._ticks += 1
            return LSTMResult(
                is_anomaly=True,
                timestamp="2026-05-13T12:00:00+00:00",
                model_version="fake_lstm_v1",
                global_score=4.0,
                global_threshold=2.0,
                anomalous_channels=("obd:RPM",),
                channel_scores={"obd:RPM": 4.0},
                channel_thresholds={"obd:RPM": 2.0},
                session="live",
                window_start_timestamp="2026-05-13T11:59:30+00:00",
                window_end_timestamp="2026-05-13T12:00:00+00:00",
                latency_ms=3.0,
            )

    class _StubLSTMManager:
        @property
        def current(self) -> Any:
            return LoadedModel(
                artifact=object(),  # the engine factory ignores it
                manifest=ModelManifestCommon(
                    artifact_format_version=1,
                    model_type="lstm_autoencoder",
                    model_version="fake_lstm_v1",
                    created_at="now",
                    training_data_hash="h",
                    input_rows=1,
                ),
                artifact_path=Path("/tmp/x"),
                manifest_path=Path("/tmp/x.manifest.json"),
                manifest_mtime=0.0,
            )

        def poll(self) -> SwapAttempt:
            return SwapAttempt(kind="no_change")

    components.model_managers["lstm_autoencoder"] = _StubLSTMManager()  # type: ignore[assignment]
    # The base fixture config ships lstm_autoencoder.enabled=false (bring-up
    # default), so `sequence_model_type` came back `None` from
    # `build_components` above — `SequenceDetector` looks its manager up by
    # this key, so it needs to actually point at the stub we just installed.
    components.sequence_model_type = "lstm_autoencoder"
    # Patch the lazy-imported SequenceInferenceEngine inside daemon.
    import src.daemon as daemon_mod
    import src.models.inference as inf_mod

    original = getattr(inf_mod, "SequenceInferenceEngine", None)
    inf_mod.SequenceInferenceEngine = _FakeLSTMEngine  # type: ignore[assignment,misc]
    try:
        source = build_live_feature_source(components)
        source.observe(TelemetryRecord.obd(datetime.now(UTC).timestamp(), "RPM", 700.0, "rpm"))

        sub = components.anomalies_broadcaster.subscribe()
        stop = asyncio.Event()
        task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
        try:
            msg = await asyncio.wait_for(sub.get(), timeout=2.0)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=1.0)
    finally:
        if original is not None:
            inf_mod.SequenceInferenceEngine = original  # type: ignore[assignment]
        elif hasattr(inf_mod, "SequenceInferenceEngine"):
            delattr(inf_mod, "SequenceInferenceEngine")
        del daemon_mod  # keep linter quiet about unused import

    assert msg["source"] == "lstm_autoencoder"
    assert msg["model_version"] == "fake_lstm_v1"


def test_sequence_detector_model_version_resets_when_gated(config: CanaryConfig) -> None:
    """`SequenceDetector.model_version` must reflect *this* tick's actual
    state, not the last tick that scored — a review-caught regression from
    Task 3.1's ``Detector`` protocol extraction (the pre-refactor code
    recomputed a local ``sequence_version`` fresh every tick; the refactor
    turned it into an instance attribute only assigned past the early-return
    guard, so a gated tick kept reporting a stale version and corrupted the
    fused event's provenance tag). Mirrors ``IsolationForestDetector.tick``,
    which already assigns ``self.model_version`` before its own guard.
    """
    from src.daemon.inference import SequenceDetector, TickContext
    from src.models.manager import LoadedModel, SwapAttempt
    from src.models.manifest import ModelManifestCommon

    class _StubLSTMManager:
        #: Flip to simulate the manager reporting no artifact loaded (the
        #: docstring's "while no artifact is loaded" case) on a later tick.
        loaded = True

        @property
        def current(self) -> Any:
            if not self.loaded:
                return None
            return LoadedModel(
                artifact=object(),  # a bad artifact is fine — engine build is caught/retried
                manifest=ModelManifestCommon(
                    artifact_format_version=1,
                    model_type="lstm_autoencoder",
                    model_version="fake_lstm_v1",
                    created_at="now",
                    training_data_hash="h",
                    input_rows=1,
                ),
                artifact_path=Path("/tmp/x"),
                manifest_path=Path("/tmp/x.manifest.json"),
                manifest_mtime=0.0,
            )

        def poll(self) -> SwapAttempt:
            return SwapAttempt(kind="no_change")

    stub_manager = _StubLSTMManager()
    components = build_components(config)
    components.model_managers["lstm_autoencoder"] = stub_manager  # type: ignore[assignment]
    components.sequence_model_type = "lstm_autoencoder"

    detector = SequenceDetector(components)
    assert detector.model_version is None  # before the first tick

    now = datetime.now(UTC)
    row = {"obd:RPM": 700.0}

    detector.tick(
        row,
        TickContext(now=now, operating_mode="driving", timestamp_str=None, scoring_allowed=True),
    )
    assert detector.model_version == "fake_lstm_v1"

    # A later gated tick (vehicle-state gate closed, artifact unloaded) must
    # reset, not keep reporting the last-loaded model as if it had just
    # scored — the pre-fix code never reached the assignment on this path
    # because it sat past the early-return guard.
    stub_manager.loaded = False
    detector.tick(
        row,
        TickContext(now=now, operating_mode="driving", timestamp_str=None, scoring_allowed=False),
    )
    assert detector.model_version is None


async def test_sequence_observe_helper_fills_missing_columns_with_nan() -> None:
    """A column the live source hasn't seen yet should arrive as NaN — the
    LSTM buffer will reject it and clear, which is the correct "wait for
    schema" behavior."""
    import pandas as pd

    from src.daemon import _sequence_observe

    captured: dict[str, Any] = {}

    class _Captor:
        feature_columns = ("obd:RPM", "obd:SPEED")

        def observe(self, series: pd.Series) -> str | None:
            captured["series"] = series
            return None

    row = pd.DataFrame(
        [{"timestamp": "now", "session": "live", "obd:RPM": 700.0, "obd:EXTRA": 1.0}]
    )
    _sequence_observe(_Captor(), row)
    assert captured["series"]["obd:RPM"] == 700.0
    assert "obd:EXTRA" not in captured["series"]
    # obd:SPEED wasn't in the row → NaN
    import math

    assert math.isnan(captured["series"]["obd:SPEED"])


async def test_score_and_emit_reconciles_missing_live_columns(
    tmp_path: Path,
    config: CanaryConfig,
) -> None:
    import pandas as pd

    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger
    from src.daemon import _score_and_emit
    from src.models.health import HealthMonitor
    from src.models.manager import LoadedModel
    from src.models.manifest import ModelManifestCommon

    class _FakeTrained:
        feature_columns = ("obd:RPM", "obd:SPEED")
        mode_column = "operating_mode"
        manifest = type(
            "M",
            (),
            {
                "model_version": "fake_if_schema",
                "schema": {
                    "feature_columns": ["obd:RPM", "obd:SPEED"],
                    "feature_dtypes": {"obd:RPM": "float64", "obd:SPEED": "float64"},
                    "mode_column": "operating_mode",
                    "timestamp_column": "timestamp",
                    "session_column": "session",
                },
            },
        )()

        def score(self, frame: Any) -> Any:
            assert "obd:SPEED" in frame.columns
            out = frame.copy()
            missing = out[list(self.feature_columns)].isna().any(axis=1)
            out["isolation_forest_score"] = float("inf")
            out["isolation_forest_label"] = -1
            out["isolation_forest_model_mode"] = out["operating_mode"]
            out["isolation_forest_missing_features"] = missing
            return out

    loaded = LoadedModel(
        artifact=_FakeTrained(),
        manifest=ModelManifestCommon(
            artifact_format_version=1,
            model_type="isolation_forest",
            model_version="fake_if_schema",
            created_at="now",
            training_data_hash="h",
            input_rows=1,
            schema=_FakeTrained.manifest.schema,
        ),
        artifact_path=Path("/tmp/x"),
        manifest_path=Path("/tmp/x.manifest.json"),
        manifest_mtime=0.0,
    )
    row = pd.DataFrame(
        [
            {
                "timestamp": "2026-05-13T12:00:00Z",
                "session": "live",
                "operating_mode": "all",
                "obd:RPM": 700.0,
                "obd:EXTRA": 1.0,
            }
        ]
    )
    health = HealthMonitor()
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)

    _score_and_emit(loaded, row, emitter, health)

    assert health.snapshot()["missing_feature_rate"] == 1.0
    assert emitter.counters.emitted == 0
    assert not list(tmp_path.glob("anomalies-*.jsonl"))


async def test_score_and_emit_warns_once_on_mode_blindness_transition(
    tmp_path: Path,
    config: CanaryConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """2.1: a mode transitioning into IF blindness logs a fresh warning
    exactly once (edge-triggered on the tick the rolling window fills),
    not on every subsequent missing-feature tick for that mode."""
    import logging

    import pandas as pd

    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger
    from src.daemon import _score_and_emit
    from src.models.health import _MODE_BLIND_WINDOW as MODE_BLIND_WINDOW
    from src.models.health import HealthMonitor
    from src.models.manager import LoadedModel
    from src.models.manifest import ModelManifestCommon

    class _FakeTrained:
        feature_columns = ("obd:RPM", "obd:SPEED")
        mode_column = "operating_mode"
        manifest = type(
            "M",
            (),
            {
                "model_version": "fake_if_schema",
                "schema": {
                    "feature_columns": ["obd:RPM", "obd:SPEED"],
                    "feature_dtypes": {"obd:RPM": "float64", "obd:SPEED": "float64"},
                    "mode_column": "operating_mode",
                    "timestamp_column": "timestamp",
                    "session_column": "session",
                },
            },
        )()

        def score(self, frame: Any) -> Any:
            out = frame.copy()
            missing = out[list(self.feature_columns)].isna().any(axis=1)
            out["isolation_forest_score"] = float("inf")
            out["isolation_forest_label"] = -1
            out["isolation_forest_model_mode"] = out["operating_mode"]
            out["isolation_forest_missing_features"] = missing
            return out

    loaded = LoadedModel(
        artifact=_FakeTrained(),
        manifest=ModelManifestCommon(
            artifact_format_version=1,
            model_type="isolation_forest",
            model_version="fake_if_schema",
            created_at="now",
            training_data_hash="h",
            input_rows=1,
            schema=_FakeTrained.manifest.schema,
        ),
        artifact_path=Path("/tmp/x"),
        manifest_path=Path("/tmp/x.manifest.json"),
        manifest_mtime=0.0,
    )
    row = pd.DataFrame(
        [
            {
                "timestamp": "2026-05-13T12:00:00Z",
                "session": "live",
                "operating_mode": "all",
                # obd:SPEED is never in the live row → permanently missing.
                "obd:RPM": 700.0,
            }
        ]
    )
    health = HealthMonitor()
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)

    with caplog.at_level(logging.WARNING, logger="src.daemon.inference"):
        for _ in range(MODE_BLIND_WINDOW + 3):
            _score_and_emit(loaded, row, emitter, health)

    blindness_warnings = [r for r in caplog.records if "transitioned into blindness" in r.message]
    assert len(blindness_warnings) == 1
    assert health.if_blind_modes == ("all",)


async def test_score_and_emit_uncalibrated_mode_never_emits(
    tmp_path: Path,
    config: CanaryConfig,
) -> None:
    """2.3: an uncalibrated mode's raw sklearn contamination label
    (`isolation_forest_label == -1`, no calibrated threshold/anomaly) no
    longer reaches the emitter — the score is still recorded (health +
    /ws/scores), but no anomaly event fires."""
    import pandas as pd

    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger
    from src.daemon import _score_and_emit
    from src.models.health import HealthMonitor
    from src.models.manager import LoadedModel
    from src.models.manifest import ModelManifestCommon

    class _FakeTrained:
        feature_columns = ("obd:RPM",)
        mode_column = "operating_mode"
        manifest = type("M", (), {"model_version": "fake_if_uncalibrated"})()

        def score(self, frame: Any) -> Any:
            out = frame.copy()
            out["isolation_forest_score"] = 0.9
            out["isolation_forest_label"] = -1  # sklearn says "anomaly"
            out["isolation_forest_model_mode"] = "idle"
            out["isolation_forest_missing_features"] = False
            # No calibrated_threshold/calibrated_anomaly columns at all —
            # this mode never accumulated a calibration holdout.
            return out

    loaded = LoadedModel(
        artifact=_FakeTrained(),
        manifest=ModelManifestCommon(
            artifact_format_version=1,
            model_type="isolation_forest",
            model_version="fake_if_uncalibrated",
            created_at="now",
            training_data_hash="h",
            input_rows=1,
        ),
        artifact_path=Path("/tmp/x"),
        manifest_path=Path("/tmp/x.manifest.json"),
        manifest_mtime=0.0,
    )
    row = pd.DataFrame(
        [
            {
                "timestamp": "2026-05-13T12:00:00Z",
                "session": "live",
                "operating_mode": "idle",
                "obd:RPM": 900.0,
            }
        ]
    )
    health = HealthMonitor()
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)

    result = _score_and_emit(loaded, row, emitter, health)

    assert emitter.counters.emitted == 0
    assert not list(tmp_path.glob("anomalies-*.jsonl"))
    # The score is still recorded — non-emitting isn't the same as invisible.
    assert health.snapshot()["observed"] == 1
    # Fusion input still carries the raw score (threshold None, so it can't
    # fire in fusion either — see `_IFFusionInput`).
    assert result is not None
    assert result.score == 0.9
    assert result.threshold is None


async def test_score_and_emit_calibrated_mode_still_emits(
    tmp_path: Path,
    config: CanaryConfig,
) -> None:
    """2.3 control case: a calibrated mode's over-threshold verdict is
    unaffected by retiring the uncalibrated legacy-label path."""
    import pandas as pd

    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger
    from src.daemon import _score_and_emit
    from src.models.health import HealthMonitor
    from src.models.manager import LoadedModel
    from src.models.manifest import ModelManifestCommon

    class _FakeTrained:
        feature_columns = ("obd:RPM",)
        mode_column = "operating_mode"
        manifest = type("M", (), {"model_version": "fake_if_calibrated"})()

        def score(self, frame: Any) -> Any:
            out = frame.copy()
            out["isolation_forest_score"] = 0.9
            out["isolation_forest_label"] = -1
            out["isolation_forest_model_mode"] = "idle"
            out["isolation_forest_missing_features"] = False
            out["isolation_forest_calibrated_threshold"] = 0.5
            out["isolation_forest_calibrated_anomaly"] = True
            return out

    loaded = LoadedModel(
        artifact=_FakeTrained(),
        manifest=ModelManifestCommon(
            artifact_format_version=1,
            model_type="isolation_forest",
            model_version="fake_if_calibrated",
            created_at="now",
            training_data_hash="h",
            input_rows=1,
        ),
        artifact_path=Path("/tmp/x"),
        manifest_path=Path("/tmp/x.manifest.json"),
        manifest_mtime=0.0,
    )
    row = pd.DataFrame(
        [
            {
                "timestamp": "2026-05-13T12:00:00Z",
                "session": "live",
                "operating_mode": "idle",
                "obd:RPM": 900.0,
            }
        ]
    )
    health = HealthMonitor()
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)

    result = _score_and_emit(loaded, row, emitter, health)

    assert emitter.counters.emitted == 1
    assert result is not None
    assert result.threshold == 0.5


async def test_score_and_emit_attaches_feature_contributors_from_scaler(
    tmp_path: Path,
    config: CanaryConfig,
) -> None:
    """7.2a: when the IF tick is trustworthy (no missing features), the
    daemon ranks features by ``|scaled z|`` from the model's own scaler
    and surfaces the top-K on the emitted event as ``feature_contributors``
    + ``affected_channels`` — cheap first-pass IF attribution."""
    import pandas as pd

    from src.alerts.emitter import AnomalyEventEmitter
    from src.alerts.event_logger import AnomalyEventLogger, read_events_dir
    from src.daemon import _score_and_emit
    from src.models.health import HealthMonitor
    from src.models.manager import LoadedModel
    from src.models.manifest import ModelManifestCommon

    class _FakeScaler:
        """Scales the trained columns to (x - mean) / std on a per-row frame."""

        def __init__(self) -> None:
            self.means = {"obd:RPM": 800.0, "obd:SPEED": 5.0, "obd:LOAD": 30.0}
            self.stds = {"obd:RPM": 50.0, "obd:SPEED": 5.0, "obd:LOAD": 5.0}

        def transform(self, frame: pd.DataFrame) -> pd.DataFrame:
            out = frame.copy()
            for column, mean in self.means.items():
                out[column] = (frame[column] - mean) / self.stds[column]
            return out

    class _FakeTrained:
        feature_columns = ("obd:RPM", "obd:SPEED", "obd:LOAD")
        mode_column = "operating_mode"
        scaler = _FakeScaler()
        manifest = type(
            "M",
            (),
            {
                "model_version": "fake_if_contrib",
                "schema": {
                    "feature_columns": list(feature_columns),
                    "feature_dtypes": dict.fromkeys(feature_columns, "float64"),
                    "mode_column": "operating_mode",
                    "timestamp_column": "timestamp",
                    "session_column": "session",
                },
            },
        )()

        def score(self, frame: Any) -> Any:
            out = frame.copy()
            out["isolation_forest_score"] = 0.9
            out["isolation_forest_label"] = -1
            out["isolation_forest_model_mode"] = "all"
            out["isolation_forest_missing_features"] = False
            # 2.3: uncalibrated modes are non-emitting at serve, so this
            # contributor-ranking test needs a calibrated verdict to still
            # reach the emitter.
            out["isolation_forest_calibrated_threshold"] = 0.5
            out["isolation_forest_calibrated_anomaly"] = True
            return out

    loaded = LoadedModel(
        artifact=_FakeTrained(),
        manifest=ModelManifestCommon(
            artifact_format_version=1,
            model_type="isolation_forest",
            model_version="fake_if_contrib",
            created_at="now",
            training_data_hash="h",
            input_rows=1,
            schema=_FakeTrained.manifest.schema,
        ),
        artifact_path=Path("/tmp/x"),
        manifest_path=Path("/tmp/x.manifest.json"),
        manifest_mtime=0.0,
    )
    # Build a row where obd:LOAD is the farthest from baseline (4 std out),
    # then obd:RPM (2 std), then obd:SPEED (1 std). The top-K ranking must
    # reflect that order regardless of the input dict's column order.
    row = pd.DataFrame(
        [
            {
                "timestamp": "2026-05-13T12:00:00Z",
                "session": "live",
                "operating_mode": "all",
                "obd:SPEED": 10.0,  # |z|=1.0
                "obd:RPM": 900.0,  # |z|=2.0
                "obd:LOAD": 50.0,  # |z|=4.0
            }
        ]
    )
    emitter = AnomalyEventEmitter(AnomalyEventLogger(tmp_path), min_seconds_between_events=0)

    _score_and_emit(loaded, row, emitter, HealthMonitor())

    events = read_events_dir(tmp_path)
    assert len(events) == 1
    contributors = events[0].detail["feature_contributors"]
    # All three trained features make it into top-5 here (K=5, only 3 features).
    assert set(contributors) == {"obd:RPM", "obd:SPEED", "obd:LOAD"}
    # Ranking: LOAD > RPM > SPEED by |scaled z|.
    ordered = sorted(contributors.items(), key=lambda kv: kv[1], reverse=True)
    assert [name for name, _ in ordered] == ["obd:LOAD", "obd:RPM", "obd:SPEED"]
    # affected_channels echoes the ranked top order.
    assert events[0].affected_channels == ("obd:LOAD", "obd:RPM", "obd:SPEED")


def test_persistence_sustained_tuner_override_replaces_not_stacks_with_gate() -> None:
    """6.3: a tuner ``DebouncePolicy`` scoped to (source, mode) fully owns
    that key's persistence requirement — every trustworthy tick passes
    straight through the built-in ``PersistenceGate`` (bypassing its own
    M-of-N) instead of also needing to clear a second, redundant
    requirement before the tuner's own ``min_consecutive`` debounce
    (applied separately, inside the emitter) even gets a look."""
    from src.alerts.fusion import PersistenceGate
    from src.alerts.tuning import AlertTuner, DebouncePolicy, TuningConfig
    from src.daemon.inference import _persistence_sustained

    gate = PersistenceGate(3, window=3)  # strict 3-of-3 — would need 3 real hits
    tuner = AlertTuner(
        TuningConfig(
            debounce_policies=[DebouncePolicy(source="isolation_forest", operating_mode="idle")]
        )
    )

    # A single firing tick, scoped to the tuner's override, passes straight
    # through — the gate's own 3-of-3 requirement never applies to this key.
    assert (
        _persistence_sustained(
            gate,
            "isolation_forest",
            True,
            tuner=tuner,
            source="isolation_forest",
            mode="idle",
            now=0.0,
        )
        is True
    )
    # The gate's own streak for this key was never advanced (no state
    # leakage from the bypass) — it's still exactly as fresh as before.
    assert gate.active("isolation_forest") is False

    # A key outside the override's scope (different mode) is unaffected —
    # it still goes through the built-in gate and needs its own M hits.
    assert not _persistence_sustained(
        gate, "isolation_forest", True, tuner=tuner, source="isolation_forest", mode="city", now=0.0
    )
    assert not _persistence_sustained(
        gate, "isolation_forest", True, tuner=tuner, source="isolation_forest", mode="city", now=1.0
    )
    assert _persistence_sustained(
        gate, "isolation_forest", True, tuner=tuner, source="isolation_forest", mode="city", now=2.0
    )


def test_persistence_sustained_missing_tick_always_goes_through_gate() -> None:
    """A missing verdict (``firing=None``) always defers to the gate itself
    (frozen, per ``PersistenceGate``'s own semantics) regardless of any
    tuner bypass — there's no tuner debounce state to "pass through" for a
    tick that produced no verdict at all."""
    from src.alerts.fusion import PersistenceGate
    from src.alerts.tuning import AlertTuner, DebouncePolicy, TuningConfig
    from src.daemon.inference import _persistence_sustained

    gate = PersistenceGate(3, window=3)
    tuner = AlertTuner(
        TuningConfig(debounce_policies=[DebouncePolicy(source="behavior", operating_mode="idle")])
    )
    assert (
        _persistence_sustained(
            gate, "behavior:x", None, tuner=tuner, source="behavior", mode="idle", now=0.0
        )
        is False
    )
    assert not gate.active("behavior:x")


def test_finite_or_none() -> None:
    from src.daemon import _finite_or_none

    assert _finite_or_none(0.0) == 0.0
    assert _finite_or_none(1.5) == 1.5
    assert _finite_or_none(float("nan")) is None
    assert _finite_or_none(float("inf")) is None
    assert _finite_or_none(float("-inf")) is None
    assert _finite_or_none(None) is None
    assert _finite_or_none("x") is None


def test_publish_score_payload_shape_and_none_is_noop() -> None:
    from src.daemon import _publish_score
    from src.dashboard.broadcast import Broadcaster

    # None broadcaster must be a silent no-op (don't crash live inference).
    _publish_score(
        None,
        source="isolation_forest",
        score=0.0,
        threshold=None,
        operating_mode="idle",
        model_version="v",
        timestamp="now",
        missing_features=False,
    )

    bus = Broadcaster()
    sub = bus.subscribe()
    _publish_score(
        bus,
        source="lstm_autoencoder",
        score=float("inf"),  # missing-feature / non-finite → null in JSON
        threshold=171895.06,
        operating_mode=None,
        model_version="lstm_v1",
        timestamp="2026-05-18T03:00:00Z",
        missing_features=True,
    )
    msg = sub.queue.get_nowait()
    assert msg == {
        "type": "score",
        "source": "lstm_autoencoder",
        "score": None,
        "threshold": 171895.06,
        "operating_mode": None,
        "model_version": "lstm_v1",
        "timestamp": "2026-05-18T03:00:00Z",
        "missing_features": True,
    }


async def test_run_live_inference_skips_when_no_manager(config: CanaryConfig) -> None:
    """Without an IF manager (models disabled), the task waits for stop and returns."""
    from src.daemon import build_live_feature_source, run_live_inference

    components = build_components(config)
    assert "isolation_forest" not in components.model_managers
    source = build_live_feature_source(components)
    stop = asyncio.Event()
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


async def test_run_live_inference_emits_event_when_outlier(config: CanaryConfig) -> None:
    """End-to-end: feed an outlier into the live source, expect an anomalies
    broadcaster message and a JSONL line on disk."""
    from src.alerts.emitter import IsolationForestInferenceEngine
    from src.daemon import build_live_feature_source, run_live_inference
    from src.models.manager import LoadedModel
    from src.models.manifest import ModelManifestCommon

    components = build_components(config)

    # Inject a stub IF manager + loaded fake model that always labels rows -1.
    class _FakeTrained:
        feature_columns = ("obd:RPM", "obd:SPEED")
        mode_column = "operating_mode"
        manifest = type("M", (), {"model_version": "fake_if_v1"})()

        def score(self, frame):
            out = frame.copy()
            out["isolation_forest_score"] = 0.9
            out["isolation_forest_label"] = -1
            out["isolation_forest_model_mode"] = "idle"
            out["isolation_forest_missing_features"] = False
            # 2.3: uncalibrated modes are non-emitting at serve.
            out["isolation_forest_calibrated_threshold"] = 0.5
            out["isolation_forest_calibrated_anomaly"] = True
            return out

    class _StubManager:
        @property
        def current(self):
            return LoadedModel(
                artifact=_FakeTrained(),
                manifest=ModelManifestCommon(
                    artifact_format_version=1,
                    model_type="isolation_forest",
                    model_version="fake_if_v1",
                    created_at="now",
                    training_data_hash="h",
                    input_rows=1,
                ),
                artifact_path=Path("/tmp/x"),
                manifest_path=Path("/tmp/x.manifest.json"),
                manifest_mtime=0.0,
            )

        def poll(self):
            return None

    components.model_managers["isolation_forest"] = _StubManager()  # type: ignore[assignment]

    # Pump a record into the live source so tick() has something to emit.
    source = build_live_feature_source(components)
    source.observe(TelemetryRecord.obd(datetime.now(UTC).timestamp(), "RPM", 5500.0, "rpm"))

    sub = components.anomalies_broadcaster.subscribe()
    stop = asyncio.Event()
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))

    try:
        msg = await asyncio.wait_for(sub.get(), timeout=2.0)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert msg["source"] == "isolation_forest"
    assert msg["model_version"] == "fake_if_v1"
    # JSONL written too
    assert any(config.daemon.anomalies_dir.glob("anomalies-*.jsonl"))
    # Sanity: the engine wrapper class actually exists in the path
    assert IsolationForestInferenceEngine.__name__ == "IsolationForestInferenceEngine"


async def test_run_live_inference_persists_derived_channels_to_storage(
    config: CanaryConfig, tmp_path: Path
) -> None:
    """Task 1.1 follow-up — the derived performance channels (fuel rate,
    boost/vacuum, estimated power, and the session-scoped trip integrals)
    must reach Parquet, not just ``/ws/telemetry``: ``query_telemetry``'s
    documented DuckDB view (docs/mcp-server.md) only sees what's on disk,
    with no on-demand reconstruction fallback."""
    from src.daemon import build_live_feature_source, run_live_inference
    from src.storage.async_writer import AsyncParquetTelemetryWriter

    components = build_components(config)
    storage_writer = AsyncParquetTelemetryWriter(ParquetTelemetryWriter(tmp_path / "storage"))
    components.storage_writer = storage_writer

    source = build_live_feature_source(components)
    now = datetime.now(UTC).timestamp()
    for channel, value, unit in (
        ("RPM", 2000.0, "rpm"),
        ("SPEED", 50.0, "km/h"),
        ("MAF", 12.0, "g/s"),
        ("INTAKE_PRESSURE", 95.0, "kPa"),
    ):
        source.observe(TelemetryRecord.obd(now, channel, value, unit))

    stop = asyncio.Event()
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
    try:
        await asyncio.sleep(0.2)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
    # flush_interval_seconds defaults to 5s — force the buffer to disk rather
    # than waiting for it, same as every other drain-shutdown path.
    await storage_writer.close()

    import pandas as pd

    parquet_files = list((tmp_path / "storage").rglob("*.parquet"))
    assert parquet_files, "expected at least one Parquet chunk written"
    written = pd.concat([pd.read_parquet(p) for p in parquet_files], ignore_index=True)
    derived_channels = set(written.loc[written["source"] == "derived", "channel"])
    # Instantaneous (row-local, Task 1.1) and cumulative (session-scoped
    # accumulator, Task 1.3) channels both made it to storage.
    assert "FUEL_RATE_LPH" in derived_channels
    assert "TRIP_DISTANCE_KM" in derived_channels


async def test_run_live_inference_flags_segmenter_fallback_on_status(
    config: CanaryConfig,
) -> None:
    """4.7 — a live source with only obd:RPM (no SPEED/THROTTLE_POS) can't
    run the real segmenter, so ``segment_or_constant`` falls back to
    constant "all" mode; the daemon must latch that onto the health
    monitor's ``segmenter_fallback_active`` flag every tick."""
    from src.daemon import build_live_feature_source, run_live_inference

    components = build_components(config)
    assert components.health_monitor.segmenter_fallback_active is False

    source = build_live_feature_source(components)
    source.observe(TelemetryRecord.obd(datetime.now(UTC).timestamp(), "RPM", 800.0, "rpm"))

    stop = asyncio.Event()
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
    try:
        for _ in range(20):
            if components.health_monitor.segmenter_fallback_active:
                break
            await asyncio.sleep(0.05)
        assert components.health_monitor.segmenter_fallback_active is True
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)


async def test_run_live_inference_does_not_flag_fallback_with_full_segmenter_inputs(
    config: CanaryConfig,
) -> None:
    """4.7 — normal segmenter operation (all its inputs present) must not
    set the fallback flag."""
    from src.daemon import build_live_feature_source, run_live_inference

    components = build_components(config)
    source = build_live_feature_source(components)
    now = datetime.now(UTC)
    for channel, value, unit in (
        ("RPM", 800.0, "rpm"),
        ("SPEED", 0.0, "km/h"),
        ("THROTTLE_POS", 10.0, "%"),
        ("COOLANT_TEMP", 85.0, "degC"),
    ):
        source.observe(TelemetryRecord.obd(now.timestamp(), channel, value, unit))

    stop = asyncio.Event()
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
    try:
        await asyncio.sleep(0.2)
        assert components.health_monitor.segmenter_fallback_active is False
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)


def test_live_vehicle_state_classifies_running_without_parquet_io(config: CanaryConfig) -> None:
    """1.5's helper reads only in-memory state — no Parquet, no replay."""
    from src.daemon.inference import _live_vehicle_state, build_live_feature_source

    components = build_components(config)
    source = build_live_feature_source(components)
    now = datetime.now(UTC)
    source.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 2500.0, "rpm"))

    state = _live_vehicle_state(source, components, now)

    assert state.state == "engine_running"
    assert state.rpm == 2500.0


def test_live_vehicle_state_reports_no_data_before_any_observation(config: CanaryConfig) -> None:
    from src.daemon.inference import _live_vehicle_state, build_live_feature_source

    components = build_components(config)
    source = build_live_feature_source(components)

    state = _live_vehicle_state(source, components, datetime.now(UTC))

    assert state.state == "no_data"


async def test_run_live_inference_skips_if_scoring_when_engine_off(config: CanaryConfig) -> None:
    """1.5: an always-fires IF stub must NOT emit when vehicle_state isn't
    engine_running/replay — here RPM=0 with fresh OBD classifies as
    ignition_only, which IF/LSTM don't score."""
    from src.daemon import build_live_feature_source, run_live_inference
    from src.models.manager import LoadedModel
    from src.models.manifest import ModelManifestCommon

    components = build_components(config)

    class _FakeTrained:
        feature_columns = ("obd:RPM",)
        mode_column = "operating_mode"
        manifest = type("M", (), {"model_version": "fake_if_v1"})()

        def score(self, frame):
            out = frame.copy()
            out["isolation_forest_score"] = 0.9
            out["isolation_forest_label"] = -1
            out["isolation_forest_model_mode"] = "idle"
            out["isolation_forest_missing_features"] = False
            return out

    class _StubManager:
        @property
        def current(self):
            return LoadedModel(
                artifact=_FakeTrained(),
                manifest=ModelManifestCommon(
                    artifact_format_version=1,
                    model_type="isolation_forest",
                    model_version="fake_if_v1",
                    created_at="now",
                    training_data_hash="h",
                    input_rows=1,
                ),
                artifact_path=Path("/tmp/x"),
                manifest_path=Path("/tmp/x.manifest.json"),
                manifest_mtime=0.0,
            )

        def poll(self):
            return None

    components.model_managers["isolation_forest"] = _StubManager()  # type: ignore[assignment]

    # RPM == 0 with a fresh OBD observation → ignition_only, not scorable.
    source = build_live_feature_source(components)
    source.observe(TelemetryRecord.obd(datetime.now(UTC).timestamp(), "RPM", 0.0, "rpm"))

    sub = components.anomalies_broadcaster.subscribe()
    stop = asyncio.Event()
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert sub.queue.empty(), "IF must not emit while the engine isn't running"
    assert components.emitter.counters.skipped_vehicle_state > 0


async def test_run_live_inference_scores_during_replay_despite_engine_off_reading(
    config: CanaryConfig,
) -> None:
    """1.5: replay overrides the engine-off/ignition-only reading — a replay
    re-emits real running-vehicle data, so IF must still score it."""
    from src.control.replay_control import ReplayStatus
    from src.daemon import build_live_feature_source, run_live_inference
    from src.models.manager import LoadedModel
    from src.models.manifest import ModelManifestCommon

    components = build_components(config)

    class _FakeTrained:
        feature_columns = ("obd:RPM",)
        mode_column = "operating_mode"
        manifest = type("M", (), {"model_version": "fake_if_v1"})()

        def score(self, frame):
            out = frame.copy()
            out["isolation_forest_score"] = 0.9
            out["isolation_forest_label"] = -1
            out["isolation_forest_model_mode"] = "idle"
            out["isolation_forest_missing_features"] = False
            # 2.3: uncalibrated modes are non-emitting at serve.
            out["isolation_forest_calibrated_threshold"] = 0.5
            out["isolation_forest_calibrated_anomaly"] = True
            return out

    class _StubManager:
        @property
        def current(self):
            return LoadedModel(
                artifact=_FakeTrained(),
                manifest=ModelManifestCommon(
                    artifact_format_version=1,
                    model_type="isolation_forest",
                    model_version="fake_if_v1",
                    created_at="now",
                    training_data_hash="h",
                    input_rows=1,
                ),
                artifact_path=Path("/tmp/x"),
                manifest_path=Path("/tmp/x.manifest.json"),
                manifest_mtime=0.0,
            )

        def poll(self):
            return None

    components.model_managers["isolation_forest"] = _StubManager()  # type: ignore[assignment]

    class _StubReplayController:
        def status(self) -> ReplayStatus:
            return ReplayStatus(running=True, replay_id="replay-1")

    components.replay_controller = _StubReplayController()  # type: ignore[assignment]

    # Same RPM==0 reading that reads ignition_only outside a replay — here
    # the active replay must still let IF score it.
    source = build_live_feature_source(components)
    source.observe(TelemetryRecord.obd(datetime.now(UTC).timestamp(), "RPM", 0.0, "rpm"))

    sub = components.anomalies_broadcaster.subscribe()
    stop = asyncio.Event()
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))

    try:
        msg = await asyncio.wait_for(sub.get(), timeout=2.0)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert msg["source"] == "isolation_forest"
    assert components.emitter.counters.skipped_vehicle_state == 0


async def test_build_live_feature_source_loads_bindings_when_present(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """When data/bindings.yaml exists, the source is constructed with it."""
    from src.daemon import build_live_feature_source

    components = build_components(config)
    source = build_live_feature_source(components)
    # data/bindings.yaml exists in the checkout; the source picks it up.
    assert source.bindings is not None
    # And a CAN frame on a known ID decodes.
    source.observe(TelemetryRecord.can(1.0, 0x551, bytes([0x69, 0, 0, 0, 0, 0, 0, 0])))
    assert "decoded:ECM_COOLANT_TEMP" in source.known_channels
    # 2.2 — accepted_baselines is always populated (a missing file loads as
    # an empty document, never None), so the behavior detector can always
    # call `active_override_for` without a null check on the document itself.
    assert source.accepted_baselines is not None


async def test_build_live_feature_source_loads_accepted_baselines_when_present(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """2.2 — a real accepted_baselines.yaml at the configured path is loaded
    and its overrides are reachable off the constructed source."""
    from src.daemon import build_live_feature_source

    baselines_path = tmp_path / "accepted_baselines.yaml"
    baselines_path.write_text(
        "schema_version: 1\n"
        "overrides:\n"
        "  - behavior_model: coolant_warmup\n"
        "    band:\n"
        "      kind: directional\n"
        "      tolerance: 3.0\n"
        "      worse_direction: positive\n"
        "      worse_warning: 3.0\n"
        "      worse_critical: 6.0\n"
        "    provenance:\n"
        "      declared_by: dan\n"
        "      declared_at: '2026-05-17'\n"
        "      review_by: '2026-11-17'\n"
        "      reason: test override\n"
    )
    components = build_components(config)
    source = build_live_feature_source(components, accepted_baselines_path=baselines_path)
    assert source.accepted_baselines is not None
    override = source.accepted_baselines.active_override_for("coolant_warmup", None)
    assert override is not None
    assert override.provenance.reason == "test override"


async def test_drain_feeds_live_source_when_passed(tmp_path: Path) -> None:
    """The full producer → drain → live_source path with no separate observer."""
    from src.config import CanaryConfig
    from src.preprocessing.live import LiveFeatureSource

    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    source = LiveFeatureSource(config=config)

    writer = ParquetTelemetryWriter(tmp_path / "storage")
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()

    async def _producer() -> None:
        for value in (700.0, 720.0):
            await queue.put(TelemetryRecord.obd(1_778_000_000.0 + value, "RPM", value, "rpm"))
        await asyncio.sleep(0.05)
        stop.set()

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(queue, writer, stop, live_source=source)
    )
    await _producer()
    await drain_task
    assert "obd:RPM" in source.known_channels


async def test_drain_publishes_decoded_can_rows_to_telemetry_bus(
    tmp_path: Path, config: CanaryConfig
) -> None:
    """Dashboard subscribers should see named decoded signals, not only raw CAN IDs."""
    source = build_live_feature_source(build_components(config))
    writer = ParquetTelemetryWriter(tmp_path / "storage")
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    bus = Broadcaster()
    sub = bus.subscribe()

    async def _producer() -> None:
        await queue.put(TelemetryRecord.can(1.0, 0x551, bytes([0x69, 0, 0, 0, 0, 0, 0, 0])))
        await asyncio.sleep(0.05)
        stop.set()

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(
            queue,
            writer,
            stop,
            telemetry_broadcaster=bus,
            live_source=source,
        )
    )
    await _producer()
    await drain_task

    messages = [await sub.get(), await sub.get()]
    assert any(
        message["source"] == "decoded"
        and message["channel"] == "ECM_COOLANT_TEMP"
        and message["value"] == 105
        for message in messages
    )


async def test_drain_swallows_live_source_exceptions(tmp_path: Path) -> None:
    """A misbehaving live_source.observe must not block the Parquet writer."""
    writer = ParquetTelemetryWriter(tmp_path / "storage")
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()

    class _Boom:
        def observe(self, _record: TelemetryRecord) -> None:
            raise RuntimeError("kaboom")

    async def _producer() -> None:
        await queue.put(TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm"))
        await asyncio.sleep(0.05)
        stop.set()

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(queue, writer, stop, live_source=_Boom())  # type: ignore[arg-type]
    )
    await _producer()
    await drain_task
    assert list((tmp_path / "storage").rglob("*.parquet"))


async def test_run_daemon_end_to_end_inference_emits_anomaly(config: CanaryConfig) -> None:
    """Full vertical: collector → drain → live_source → IF tick → emitter →
    anomalies broadcaster + JSONL. Uses a stub IF model that labels every row.
    """
    from src.daemon import run_daemon
    from src.models.manager import LoadedModel, SwapAttempt
    from src.models.manifest import ModelManifestCommon

    components = build_components(config)

    class _FakeTrained:
        feature_columns = ("obd:RPM",)
        mode_column = "operating_mode"
        manifest = type("M", (), {"model_version": "fake_if_v1"})()

        def score(self, frame: Any) -> Any:
            out = frame.copy()
            out["isolation_forest_score"] = 0.9
            out["isolation_forest_label"] = -1
            out["isolation_forest_model_mode"] = "all"
            out["isolation_forest_missing_features"] = False
            # 2.3: uncalibrated modes are non-emitting at serve.
            out["isolation_forest_calibrated_threshold"] = 0.5
            out["isolation_forest_calibrated_anomaly"] = True
            return out

    class _StubManager:
        @property
        def current(self) -> Any:
            return LoadedModel(
                artifact=_FakeTrained(),
                manifest=ModelManifestCommon(
                    artifact_format_version=1,
                    model_type="isolation_forest",
                    model_version="fake_if_v1",
                    created_at="now",
                    training_data_hash="h",
                    input_rows=1,
                ),
                artifact_path=Path("/tmp/x"),
                manifest_path=Path("/tmp/x.manifest.json"),
                manifest_mtime=0.0,
            )

        def poll(self) -> SwapAttempt:
            return SwapAttempt(kind="no_change")

    components.model_managers["isolation_forest"] = _StubManager()  # type: ignore[assignment]

    async def _producer_collector(
        queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event
    ) -> None:
        # Real wall-clock timestamps: run_live_inference ticks against
        # datetime.now(UTC), and 1.4's fully-stale gate NaNs/drops a channel
        # whose age exceeds max_gap — a fixed historical epoch would read as
        # permanently stale and never score.
        base = datetime.now(UTC).timestamp()
        for i in range(5):
            await queue.put(TelemetryRecord.obd(base + i, "RPM", float(700 + i), "rpm"))
            await asyncio.sleep(0.02)
        await stop.wait()

    sub = components.anomalies_broadcaster.subscribe()
    stop = asyncio.Event()

    async def _run() -> None:
        await run_daemon(config, stop, collectors=[_producer_collector], components=components)

    daemon_task = asyncio.create_task(_run())
    try:
        msg = await asyncio.wait_for(sub.get(), timeout=5.0)
    finally:
        stop.set()
        await asyncio.wait_for(daemon_task, timeout=5.0)

    assert msg["source"] == "isolation_forest"
    assert msg["model_version"] == "fake_if_v1"
    # JSONL too
    assert any(config.daemon.anomalies_dir.glob("anomalies-*.jsonl"))


async def test_run_daemon_disable_live_inference_skips_inference_task(
    config: CanaryConfig,
) -> None:
    """``enable_live_inference=False`` should leave the inference task off."""
    from src.daemon import run_daemon

    components = build_components(config)
    records_pushed: list[TelemetryRecord] = []

    async def _producer(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        record = TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm")
        records_pushed.append(record)
        await queue.put(record)
        await stop.wait()

    stop = asyncio.Event()

    async def _run() -> None:
        await run_daemon(
            config,
            stop,
            collectors=[_producer],
            components=components,
            enable_live_inference=False,
        )

    daemon_task = asyncio.create_task(_run())
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(daemon_task, timeout=5.0)
    # Producer ran, Parquet got the record, but no inference task means no
    # AnomalyEvent emission could possibly have happened.
    assert len(records_pushed) == 1
    assert not list(config.daemon.anomalies_dir.glob("anomalies-*.jsonl"))


async def test_run_daemon_end_to_end_telemetry_published_to_ws(
    config: CanaryConfig,
) -> None:
    """The integration check: producer → drain_queue → broadcaster → /ws/telemetry.

    Uses a fake collector that pushes a few records, runs the full
    ``run_daemon`` with the dashboard server in the same loop, opens a
    WebSocket via httpx through TestClient, asserts the records come out
    the other end. Stop is triggered after the records flow.
    """
    from src.daemon import run_daemon

    records_to_push = [
        TelemetryRecord.obd(1_778_000_000.0 + i, "RPM", float(700 + i), "rpm") for i in range(3)
    ]

    async def _fake_collector(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        for record in records_to_push:
            await queue.put(record)
        # block until stop fires so the daemon doesn't think this collector exited
        await stop.wait()

    stop = asyncio.Event()
    components = build_components(config)
    sub = components.telemetry_broadcaster.subscribe()

    async def _run() -> None:
        await run_daemon(
            config,
            stop,
            collectors=[_fake_collector],
            components=components,
        )

    daemon_task = asyncio.create_task(_run())
    try:
        received: list[dict[str, Any]] = []
        for _ in range(len(records_to_push)):
            received.append(await asyncio.wait_for(sub.get(), timeout=3.0))
    finally:
        stop.set()
        await asyncio.wait_for(daemon_task, timeout=5.0)

    assert [r["value"] for r in received] == [700.0, 701.0, 702.0]
    assert all(r["channel"] == "RPM" for r in received)


def test_record_to_dict_is_json_serializable() -> None:
    """Regression: the WS handler does `websocket.send_json(_record_to_dict)`;
    a raw datetime timestamp raises TypeError and closes every telemetry
    socket (dashboard shows "0 live values"). It must be a JSON-safe ISO
    string, matching the REST /telemetry/latest shape."""
    obd = _record_to_dict(TelemetryRecord.obd(1_778_000_000.0, "RPM", 812.0, "rpm"))
    assert isinstance(obd["timestamp"], str)
    json.dumps(obd)  # must not raise
    assert obd["value"] == 812.0 and obd["channel"] == "RPM"

    can = _record_to_dict(TelemetryRecord.can(1_778_000_000.0, 0x1F9, bytes([1, 2, 3, 4])))
    assert isinstance(can["timestamp"], str)
    json.dumps(can)  # raw CAN frame row is JSON-safe too


async def test_drop_logging_is_coalesced_into_bursts(caplog: pytest.LogCaptureFixture) -> None:
    """A stalled sink must not emit one log line per dropped record.

    On 2026-07-27 the storage sink dropped 3,570 records inside a single
    second and wrote 3,570 identical journal lines doing it — amplifying a
    stall on a Pi that was already failing to drain, while recording nothing
    about how long the burst lasted or how big it got.
    """
    from src.daemon.drain import _enqueue_sink, reset_drop_bursts

    reset_drop_bursts()
    health = HealthMonitor()
    full: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=1)
    await full.put(TelemetryRecord.obd(1_785_000_000.0, "RPM", 700.0, "rpm"))
    sink = TelemetrySink("storage", full, "drop_newest")

    with caplog.at_level("WARNING", logger="src.daemon.drain"):
        for i in range(500):
            assert not _enqueue_sink(
                sink,
                TelemetryRecord.obd(1_785_000_100.0 + i, "RPM", 720.0, "rpm"),
                health_monitor=health,
            )

    # Every drop is still counted — only the logging is coalesced.
    assert health.snapshot()["telemetry_sinks"]["storage"]["dropped_records"] == 500
    assert len(caplog.records) == 1, f"expected one opening line, got {len(caplog.records)}"

    # The burst total lands when a later, separate burst begins.
    reset_drop_bursts()
    caplog.clear()


# ---------------------------------------------------------------------------
# C07.6 / C07.9 / C07.5 — sink subscriptions, batched fanout, receive stats
# ---------------------------------------------------------------------------


async def test_telemetry_fanout_applies_per_sink_accepts_filter() -> None:
    """C07.6 — a sink with ``accepts`` only receives records it accepts;
    an unfiltered sink still receives everything."""
    from src.daemon.drain import accepts_source

    ingest: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    storage: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    gps: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    records = [
        TelemetryRecord.can(1_778_000_000.0, 0x100, b"\x01"),
        TelemetryRecord.obd(1_778_000_001.0, "RPM", 700.0, "rpm"),
        TelemetryRecord.gps(1_778_000_002.0, "speed", 40.0, "km/h"),
    ]
    for record in records:
        await ingest.put(record)
    stop.set()

    await run_telemetry_fanout(
        ingest,
        [
            TelemetrySink("storage", storage, "drop_newest"),
            TelemetrySink("gps", gps, "drop_oldest", accepts=accepts_source("gps")),
        ],
        stop,
    )

    assert [storage.get_nowait() for _ in range(storage.qsize())] == records
    assert [gps.get_nowait() for _ in range(gps.qsize())] == [records[2]]


async def test_telemetry_fanout_preserves_order_across_batches() -> None:
    """C07.9 — batching in the fanout must not reorder or lose records."""
    from src.daemon.drain import FANOUT_MAX_BATCH

    ingest: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    sink: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    health = HealthMonitor()
    n = FANOUT_MAX_BATCH * 2 + 17
    records = [TelemetryRecord.can(1_778_000_000.0 + i, 0x100 + i, b"\x01") for i in range(n)]
    for record in records:
        await ingest.put(record)
    stop.set()

    await run_telemetry_fanout(
        ingest, [TelemetrySink("live", sink, "drop_oldest")], stop, health_monitor=health
    )

    assert [sink.get_nowait() for _ in range(sink.qsize())] == records
    assert health.snapshot()["telemetry_sinks"]["live"]["dropped_records"] == 0
    assert ingest.empty()


async def test_drain_does_not_feed_raw_can_to_gps_dtc_monitor_sinks(tmp_path: Path) -> None:
    """C07.6 — end to end through ``drain_queue_with_broadcast``: the GPS
    sink sees only ``gps`` records and the DTC/monitor sinks only ``obd``
    records; raw ``can`` frames never reach consumers that reject them."""

    class _Seen:
        def __init__(self) -> None:
            self.sources: list[str] = []

        def observe(self, record: TelemetryRecord, **_: Any) -> bool:
            self.sources.append(record.source)
            return False

        def persist(self) -> None:
            pass

    gps, dtc, monitor = _Seen(), _Seen(), _Seen()
    writer = ParquetTelemetryWriter(tmp_path / "storage")
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()

    async def _producer() -> None:
        for i in range(20):
            await queue.put(TelemetryRecord.can(1_778_000_000.0 + i, 0x100, b"\x01"))
        await queue.put(TelemetryRecord.obd(1_778_000_100.0, "RPM", 700.0, "rpm"))
        await queue.put(TelemetryRecord.gps(1_778_000_101.0, "speed", 40.0, "km/h"))
        await asyncio.sleep(0.1)
        stop.set()

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(
            queue,
            writer,
            stop,
            gps_tracker=gps,  # type: ignore[arg-type]
            dtc_state=dtc,  # type: ignore[arg-type]
            monitor_state=monitor,  # type: ignore[arg-type]
            active_session_path=lambda: writer.paths.root,
        )
    )
    await _producer()
    await drain_task

    assert gps.sources == ["gps"]
    assert dtc.sources == ["obd"]
    assert monitor.sources == ["obd"]


def test_can_receive_stats_reach_health_snapshot_as_named_counter() -> None:
    """C07.5 — passive receive losses are attributable under their own
    ``telemetry_sinks`` entry, never folded into ``storage``."""
    from src.collection.can_collector import CanReceiveStats
    from src.collection.queueing import HandoffStats
    from src.daemon import runtime as runtime_mod

    health = HealthMonitor()
    runtime_mod._can_receive_stats_observer(health)(
        CanReceiveStats(
            frames=1200,
            parse_errors=3,
            queue_dropped=7,
            handoff=HandoffStats(
                accepted=1195, dropped=5, high_water=900, capacity=4096, drains=40, max_batch=256
            ),
            reconnects=1,
        )
    )

    sinks = health.snapshot()["telemetry_sinks"]
    assert "storage" not in sinks
    can_receive = sinks["can_receive"]
    assert can_receive["records_processed"] == 1200
    assert can_receive["parse_errors"] == 3
    assert can_receive["buffer_dropped"] == 5
    assert can_receive["buffer_high_water"] == 900
    assert can_receive["buffer_capacity"] == 4096
    assert can_receive["queue_dropped"] == 7
    assert can_receive["dropped_records"] == 12
    assert can_receive["batches"] == 40
    assert can_receive["reconnects"] == 1


def test_obd_collector_handoff_stats_reach_health_as_obd_collect() -> None:
    """C03.6 — the Canable collector's QueueDropCounter / ThreadHandoff
    counters are attributable in ``/status`` instead of only in a log line,
    with derived readings and raw responses kept apart."""
    from src.collection.queueing import CollectorHandoffStats, HandoffStats
    from src.daemon import runtime as runtime_mod

    health = HealthMonitor()
    stats = CollectorHandoffStats(
        handoff=HandoffStats(
            accepted=5000, dropped=3, high_water=120, capacity=4096, drains=80, max_batch=256
        ),
        queue_dropped=2,
        derived_records=5003,
        raw_responses=900,
        reconnects=1,
    )
    runtime_mod._obd_handoff_stats_observer(health)(stats)
    sink = health.snapshot()["telemetry_sinks"]["obd_collect"]
    assert sink["dropped_records"] == 5
    assert sink["buffer_dropped"] == 3 and sink["queue_dropped"] == 2
    assert sink["derived_records"] == 5003 and sink["raw_responses"] == 900
    assert sink["reconnects"] == 1
    assert stats.to_dict()["accepted"] == 5000
