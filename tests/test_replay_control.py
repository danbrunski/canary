"""Tests for runtime replay control + isolated outputs.

Covers:

* :class:`ParquetTelemetryWriter` and :class:`AnomalyEventLogger` mode
  switching (writes flow under ``_replay-*`` paths while in replay mode).
* :class:`ReplayController` lifecycle: validation, conflict detection,
  status snapshot, idempotent stop.
* :func:`delete_expired_replays` retention sweeper.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pandas as pd
import pytest

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger, read_events_dir
from src.alerts.events import AnomalyEvent
from src.alerts.fusion import PersistenceGate
from src.config import CanaryConfig
from src.control.replay_control import (
    ReplayConflictError,
    ReplayController,
    ReplayNotFoundError,
    ReplayNotRunningError,
)
from src.preprocessing.live import LiveFeatureSource
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.storage.parquet_writer import (
    ParquetTelemetryWriter,
    delete_expired_replays,
    write_records_to_parquet,
)
from src.telemetry import TelemetryRecord


def _event(timestamp: str = "2026-05-19T18:00:00Z") -> AnomalyEvent:
    return AnomalyEvent(
        timestamp=timestamp,
        source="isolation_forest",
        model_version="v1",
        score=0.5,
        threshold=0.1,
        affected_channels=("rpm",),
        operating_mode="city",
    )


def _record(channel: str = "rpm", value: float = 700.0) -> TelemetryRecord:
    return TelemetryRecord.obd(
        timestamp=datetime(2026, 5, 19, tzinfo=UTC).timestamp(),
        pid=channel,
        value=value,
        unit="rpm",
    )


# --------------------------------------------------------------------------- #
# ParquetTelemetryWriter mode switching
# --------------------------------------------------------------------------- #


def test_writer_enter_replay_mode_routes_under_underscore_prefix(tmp_path: Path) -> None:
    """A leading ``_`` is what isolates the replay dir from training discovery."""
    writer = ParquetTelemetryWriter(tmp_path)
    live_root = writer.paths.root

    writer.enter_replay_mode("2026-05-19T180000")
    replay_root = writer.paths.root
    assert replay_root.name == "_replay-2026-05-19T180000"
    assert replay_root.parent == tmp_path
    assert replay_root != live_root


def test_writer_exit_replay_mode_returns_to_live_session_root(tmp_path: Path) -> None:
    """``exit_replay_mode`` rotates to a fresh timestamp dir, never another ``_``."""
    writer = ParquetTelemetryWriter(tmp_path)
    writer.enter_replay_mode("2026-05-19T180000")
    writer.exit_replay_mode()
    assert not writer.paths.root.name.startswith("_")
    # Sortable timestamp form per make_session_dir's convention.
    assert len(writer.paths.root.name) == len("YYYY-MM-DDTHHMMSS")


def test_writer_flush_during_replay_writes_under_replay_dir(tmp_path: Path) -> None:
    writer = ParquetTelemetryWriter(tmp_path, max_rows=1)
    writer.enter_replay_mode("2026-05-19T180000")
    writer.write(_record())
    files = list((tmp_path / "_replay-2026-05-19T180000").glob("*.parquet"))
    assert len(files) == 1


async def test_async_writer_serializes_replay_mode_with_inflight_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = ParquetTelemetryWriter(tmp_path, max_rows=1)
    async_writer = AsyncParquetTelemetryWriter(writer)
    original_write = writer.write
    write_started = threading.Event()
    release_write = threading.Event()

    def slow_write(record: TelemetryRecord) -> Path | None:
        write_started.set()
        assert release_write.wait(timeout=2.0)
        return original_write(record)

    monkeypatch.setattr(writer, "write", slow_write)
    write_task = asyncio.create_task(async_writer.write(_record()))
    assert await asyncio.to_thread(write_started.wait, 1.0)

    replay_task = asyncio.create_task(async_writer.enter_replay_mode("2026-05-19T180000"))
    await asyncio.sleep(0.05)
    assert not replay_task.done()

    release_write.set()
    await write_task
    await replay_task

    assert writer.paths.root.name == "_replay-2026-05-19T180000"


# --------------------------------------------------------------------------- #
# AnomalyEventLogger mode switching
# --------------------------------------------------------------------------- #


def test_logger_replay_mode_writes_under_replay_subdir(tmp_path: Path) -> None:
    """Replay anomalies land in ``<base>/_replay/<id>.jsonl``, not the daily file."""
    logger = AnomalyEventLogger(tmp_path)
    target = logger.enter_replay_mode("2026-05-19T180000")
    logger.log(_event())
    assert target.exists()
    assert target == tmp_path / "_replay" / "2026-05-19T180000.jsonl"
    # Daily file must not exist — replay events stay out of the feedback path.
    assert list(tmp_path.glob("anomalies-*.jsonl")) == []


def test_logger_exit_replay_mode_resumes_daily_rotation(tmp_path: Path) -> None:
    logger = AnomalyEventLogger(tmp_path)
    logger.enter_replay_mode("2026-05-19T180000")
    logger.log(_event())
    logger.exit_replay_mode()
    logger.log(_event())
    # Daily file now created at the base directory.
    daily_files = list(tmp_path.glob("anomalies-*.jsonl"))
    assert len(daily_files) == 1
    assert daily_files[0].parent == tmp_path


def test_read_events_dir_skips_replay_anomalies(tmp_path: Path) -> None:
    """Feedback/refit jobs read via ``read_events_dir`` — must not see replay events."""
    logger = AnomalyEventLogger(tmp_path)
    logger.enter_replay_mode("2026-05-19T180000")
    logger.log(_event("2026-05-19T18:00:00Z"))
    logger.exit_replay_mode()
    logger.log(_event("2026-05-19T19:00:00Z"))
    logger.close()
    visible = read_events_dir(tmp_path)
    # Only the post-replay event is visible to the feedback loop.
    assert len(visible) == 1
    assert visible[0].timestamp == "2026-05-19T19:00:00Z"


# --------------------------------------------------------------------------- #
# ReplayController lifecycle
# --------------------------------------------------------------------------- #


def _make_controller(tmp_path: Path) -> tuple[ReplayController, Path]:
    storage_root = tmp_path / "sessions"
    storage_root.mkdir()
    anomalies_dir = tmp_path / "anomalies"
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    writer = ParquetTelemetryWriter(storage_root)
    logger = AnomalyEventLogger(anomalies_dir)
    controller = ReplayController(
        queue=queue,
        writer=writer,
        anomaly_logger=logger,
        storage_root=storage_root,
    )
    return controller, storage_root


class _IsolatedController(NamedTuple):
    """7.1 fixture bundle: a controller wired to the same in-memory objects
    the real daemon would share between live collection and replay."""

    controller: ReplayController
    storage_root: Path
    live_source: LiveFeatureSource
    persistence_gate: PersistenceGate
    emitter: AnomalyEventEmitter


def _make_isolated_controller(tmp_path: Path) -> _IsolatedController:
    storage_root = tmp_path / "sessions"
    storage_root.mkdir()
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    writer = ParquetTelemetryWriter(storage_root)
    anomaly_logger = AnomalyEventLogger(tmp_path / "anomalies")
    config = CanaryConfig.from_yaml(Path("canary.example.yaml"))
    live_source = LiveFeatureSource(config=config)
    persistence_gate = PersistenceGate(3, window=5)
    emitter = AnomalyEventEmitter(
        AnomalyEventLogger(tmp_path / "emitter_events"), min_seconds_between_events=1.0
    )
    controller = ReplayController(
        queue=queue,
        writer=writer,
        anomaly_logger=anomaly_logger,
        storage_root=storage_root,
        live_source=live_source,
        persistence_gate=persistence_gate,
        emitter=emitter,
    )
    return _IsolatedController(controller, storage_root, live_source, persistence_gate, emitter)


def _if_row(*, mode: str = "idle") -> pd.Series:
    return pd.Series(
        {
            "isolation_forest_label": -1,
            "isolation_forest_score": 0.7,
            "isolation_forest_missing_features": False,
            "operating_mode": mode,
        }
    )


def test_controller_status_starts_idle(tmp_path: Path) -> None:
    controller, _ = _make_controller(tmp_path)
    status = controller.status()
    assert status.running is False
    assert status.replay_id is None


async def test_start_rejects_missing_session(tmp_path: Path) -> None:
    controller, _ = _make_controller(tmp_path)
    with pytest.raises(ReplayNotFoundError):
        await controller.start("2026-01-01T000000")


async def test_start_rejects_replay_of_replay(tmp_path: Path) -> None:
    """The ``_replay-`` prefix gates the regex; we refuse to recurse into our own output."""
    controller, storage_root = _make_controller(tmp_path)
    # Even if such a dir existed, the live-session regex wouldn't match.
    (storage_root / "_replay-2026-05-19T180000").mkdir()
    with pytest.raises(ReplayNotFoundError):
        await controller.start("_replay-2026-05-19T180000")


async def test_start_marks_running_and_isolates_outputs(tmp_path: Path) -> None:
    controller, storage_root = _make_controller(tmp_path)
    source = storage_root / "2026-05-18T120000"
    source.mkdir()

    status = await controller.start("2026-05-18T120000", speed=2.0, loop=False)
    assert status.running is True
    assert status.source_session_id == "2026-05-18T120000"
    assert status.speed == 2.0
    assert status.loop is False
    assert status.output_session_dir is not None
    assert Path(status.output_session_dir).name.startswith("_replay-")
    assert status.output_anomalies_path is not None
    assert Path(status.output_anomalies_path).parent.name == "_replay"

    # Idempotent guard: second start while running raises.
    with pytest.raises(ReplayConflictError):
        await controller.start("2026-05-18T120000")

    await controller.stop()


async def test_stop_returns_to_idle(tmp_path: Path) -> None:
    controller, storage_root = _make_controller(tmp_path)
    source = storage_root / "2026-05-18T120000"
    source.mkdir()
    await controller.start("2026-05-18T120000", loop=False)
    status = await controller.stop()
    assert status.running is False


async def test_status_reports_what_the_replay_actually_emitted(tmp_path: Path) -> None:
    """D02.9 — ``GET /control/replay`` must publish the row accounting.

    The 1.29x "amplification" premise came from inferring a row count from
    chunk counts, because nothing reported what the emit path did. The source
    here carries payload-bearing ``can`` rows, so the two re-derived rows are
    skipped at the emit path (the pipeline regenerates them) and said so.
    """
    storage_root = tmp_path / "sessions"
    storage_root.mkdir()
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    controller = ReplayController(
        queue=queue,
        writer=ParquetTelemetryWriter(storage_root),
        anomaly_logger=AnomalyEventLogger(tmp_path / "anomalies"),
        storage_root=storage_root,
    )
    base = datetime(2026, 5, 18, 12, tzinfo=UTC)
    write_records_to_parquet(
        [
            TelemetryRecord(timestamp=base, source="can", channel="0x1F9", payload=b"\x01\x02"),
            TelemetryRecord(
                timestamp=base + timedelta(seconds=0.01),
                source="obd",
                channel="RPM",
                value=800.0,
                unit="rpm",
            ),
            TelemetryRecord.decoded(base + timedelta(seconds=0.02), "ECM_RPM", 800.0, "rpm"),
            TelemetryRecord.derived(base + timedelta(seconds=0.03), "FUEL_RATE_LPH", 1.2, "L/h"),
        ],
        storage_root / "2026-05-18T120000",
    )

    await controller.start("2026-05-18T120000", speed=10_000.0, loop=False)
    for _ in range(200):
        if controller.status().rows_emitted == 2:
            break
        await asyncio.sleep(0.01)

    status = controller.status()
    assert status.source_rows_total == 4
    assert status.rows_emitted == 2
    assert status.rows_skipped == 2
    assert status.rows_dropped == 0
    assert status.rederivable is True
    assert status.skipped_sources == ("decoded", "derived")
    payload = status.to_dict()
    assert payload["skipped_sources"] == ["decoded", "derived"]
    assert isinstance(payload["rederivable_reason"], str)

    # The counters survive the end of the replay — "how many rows did that
    # replay emit" must be answerable after it stops, not only during.
    final = await controller.stop()
    assert final.running is False
    assert final.rows_emitted == 2 and final.rows_skipped == 2


async def test_stop_without_running_raises(tmp_path: Path) -> None:
    controller, _ = _make_controller(tmp_path)
    with pytest.raises(ReplayNotRunningError):
        await controller.stop()


# --------------------------------------------------------------------------- #
# 7.1 — in-memory isolation on the replay boundary
# --------------------------------------------------------------------------- #


async def test_start_resets_live_source_and_persistence_gate(tmp_path: Path) -> None:
    """Entering replay must not let a live drive's observed state / M-of-N
    streak leak into the replay run's own inference."""
    ctl = _make_isolated_controller(tmp_path)
    source_dir = ctl.storage_root / "2026-05-18T120000"
    source_dir.mkdir()

    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    ctl.live_source.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 1500.0, "rpm"))
    ctl.live_source.tick(now)
    ctl.persistence_gate.observe("isolation_forest", True, now=0.0)
    assert ctl.live_source.known_channels
    assert ctl.live_source.buffer_size > 0

    await ctl.controller.start("2026-05-18T120000")

    assert ctl.live_source.known_channels == ()
    assert ctl.live_source.buffer_size == 0
    assert not ctl.persistence_gate.active("isolation_forest")
    assert ctl.persistence_gate.suppressed == 0

    await ctl.controller.stop()


async def test_stop_resets_live_source_and_persistence_gate_again(tmp_path: Path) -> None:
    """Exiting replay must not let the replay's own tail state bleed back
    into live once it stops."""
    ctl = _make_isolated_controller(tmp_path)
    source_dir = ctl.storage_root / "2026-05-18T120000"
    source_dir.mkdir()

    await ctl.controller.start("2026-05-18T120000")
    # Something observed *during* the replay run.
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    ctl.live_source.observe(TelemetryRecord.obd(now.timestamp(), "RPM", 3000.0, "rpm"))
    ctl.live_source.tick(now)
    ctl.persistence_gate.observe("lstm_autoencoder", True, now=0.0)
    assert ctl.live_source.known_channels

    await ctl.controller.stop()

    assert ctl.live_source.known_channels == ()
    assert ctl.live_source.buffer_size == 0
    assert not ctl.persistence_gate.active("lstm_autoencoder")


async def test_start_and_stop_snapshot_restore_emitter_counters_and_dedupe(
    tmp_path: Path,
) -> None:
    """Emitter counters are a running live total (unlike the hard-reset
    live-source/persistence-gate above): replay gets a clean view, and live
    picks back up exactly where it left off once replay ends."""
    ctl = _make_isolated_controller(tmp_path)
    source_dir = ctl.storage_root / "2026-05-18T120000"
    source_dir.mkdir()

    live_event = ctl.emitter.emit_isolation_forest_row(
        _if_row(), model_version="if_v1", timestamp="2026-05-18T11:00:00Z"
    )
    assert live_event is not None
    assert ctl.emitter.counters.emitted == 1

    await ctl.controller.start("2026-05-18T120000")
    assert ctl.emitter.counters.emitted == 0  # clean slate for the replay run

    replay_event = ctl.emitter.emit_isolation_forest_row(
        _if_row(), model_version="if_v1", timestamp="2026-05-18T12:00:00Z"
    )
    assert replay_event is not None
    assert ctl.emitter.counters.emitted == 1

    await ctl.controller.stop()

    # Live's pre-replay total is restored, unaffected by the replay run's count.
    assert ctl.emitter.counters.emitted == 1
    # The live dedupe entry from before replay start is intact: the exact
    # same (source, model_version, timestamp) right after stop is still
    # deduped against it, not treated as fresh.
    still_deduped = ctl.emitter.emit_isolation_forest_row(
        _if_row(), model_version="if_v1", timestamp="2026-05-18T11:00:00Z"
    )
    assert still_deduped is None
    assert ctl.emitter.counters.suppressed_dedupe == 1


# --------------------------------------------------------------------------- #
# delete_expired_replays
# --------------------------------------------------------------------------- #


def test_delete_expired_replays_evicts_old_and_keeps_fresh(tmp_path: Path) -> None:
    now = datetime(2026, 5, 19, tzinfo=UTC)
    fresh = tmp_path / "_replay-2026-05-19T000000"
    stale = tmp_path / f"_replay-{(now - timedelta(days=30)).strftime('%Y-%m-%dT%H%M%S')}"
    live = tmp_path / "2026-05-10T000000"
    for path in (fresh, stale, live):
        path.mkdir()
        (path / "telemetry-00000.parquet").write_bytes(b"data")

    deleted = delete_expired_replays(tmp_path, replay_retention_days=7, now=now)
    assert deleted == [stale]
    assert fresh.exists()
    # Live sessions are not in scope for the replay sweeper.
    assert live.exists()


def test_delete_expired_replays_respects_protect(tmp_path: Path) -> None:
    now = datetime(2026, 5, 19, tzinfo=UTC)
    protected = tmp_path / "_replay-2026-04-01T000000"
    protected.mkdir()
    deleted = delete_expired_replays(
        tmp_path,
        replay_retention_days=7,
        now=now,
        protect={protected},
    )
    assert deleted == []
    assert protected.exists()


def test_delete_expired_replays_rejects_negative_days(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        delete_expired_replays(tmp_path, replay_retention_days=-1)
