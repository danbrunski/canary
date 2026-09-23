"""Collection starts before the slow parts of daemon start-up (2026-09-22).

On a cold boot off the Pi's SD card the collectors started 93 s after the unit:
the model artifacts (~34 s), the live feature source (~14 s) and the dashboard
app (~11 s) were all built on the event loop first, and nothing was recorded
until they were done. These tests pin the order that replaced it: collectors,
storage and the fanout first; models loaded by the poller off the loop; the
live source built in a thread, with its sink joining the fanout when ready.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from src.config import CanaryConfig
from src.daemon import build_components, drain_queue_with_broadcast
from src.daemon.polling import poll_model_managers
from src.models.manager import SwapAttempt, manager_for_artifact
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord
from tests.test_daemon import config  # noqa: F401 — the shared fixture
from tests.waits import HANG_GUARD_SECONDS, settle, wait_until

#: run_daemon's shutdown digest is a spawned child; see the fixture (T01.1).
pytestmark = pytest.mark.usefixtures("no_trip_end_digest_child")

# ---------------------------------------------------------------------------
# Model managers: built empty, loaded by the poller off the loop
# ---------------------------------------------------------------------------


def test_manager_for_artifact_load_false_defers_the_initial_load(tmp_path: Path) -> None:
    calls: list[Path] = []

    def _loader(path: Path) -> object:
        calls.append(path)
        return object()

    manager = manager_for_artifact(tmp_path / "missing.joblib", _loader, load=False)
    assert manager.current is None
    assert calls == []


def test_build_components_load_models_false_leaves_managers_empty(
    config: CanaryConfig,  # noqa: F811
) -> None:
    components = build_components(config, load_models=False)
    assert all(manager.current is None for manager in components.model_managers.values())


async def test_poll_model_managers_loads_off_the_event_loop() -> None:
    """A swap is a full artifact load; on the loop it stalled every sink."""
    loop_thread = threading.get_ident()
    polled_on: list[int] = []
    stop = asyncio.Event()

    class _Manager:
        current = None

        def poll(self) -> SwapAttempt:
            polled_on.append(threading.get_ident())
            stop.set()
            return SwapAttempt(kind="no_change")

    await settle(
        poll_model_managers({"isolation_forest": _Manager()}, stop, interval_seconds=0.01),  # type: ignore[dict-item]
    )
    assert polled_on
    assert all(ident != loop_thread for ident in polled_on)


# ---------------------------------------------------------------------------
# Drain: the live sink joins the fanout when the source is ready
# ---------------------------------------------------------------------------


class _RecordingSource:
    def __init__(self) -> None:
        self.observed: list[TelemetryRecord] = []

    def observe(self, record: TelemetryRecord) -> None:
        self.observed.append(record)

    def decode_for_telemetry(self, _record: TelemetryRecord) -> list[Any]:
        return []

    def decode_did_for_telemetry(self, _record: TelemetryRecord) -> None:
        return None


async def test_drain_live_sink_joins_when_the_source_is_ready(tmp_path: Path) -> None:
    writer = ParquetTelemetryWriter(tmp_path / "storage")
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    ready: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    source = _RecordingSource()
    early = TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm")
    late = TelemetryRecord.obd(1_778_000_001.0, "RPM", 710.0, "rpm")

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(queue, writer, stop, live_source_ready=ready)
    )
    await queue.put(early)
    await asyncio.sleep(0.1)
    ready.set_result(source)
    await asyncio.sleep(0.05)
    await queue.put(late)
    await asyncio.sleep(0.1)
    stop.set()
    await settle(drain_task)

    # Before the source existed the record was stored, not observed (and not
    # dropped from a live queue: there was none to drop it from).
    assert source.observed == [late]
    assert list((tmp_path / "storage").rglob("*.parquet"))


async def test_drain_live_source_ready_none_runs_without_a_live_sink(tmp_path: Path) -> None:
    writer = ParquetTelemetryWriter(tmp_path / "storage")
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    ready: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(queue, writer, stop, live_source_ready=ready)
    )
    ready.set_result(None)
    await queue.put(TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm"))
    await asyncio.sleep(0.1)
    stop.set()
    await settle(drain_task)
    assert list((tmp_path / "storage").rglob("*.parquet"))


async def test_drain_refuses_both_live_source_forms(tmp_path: Path) -> None:
    ready: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    with pytest.raises(ValueError, match="not both"):
        await drain_queue_with_broadcast(
            asyncio.Queue(),
            ParquetTelemetryWriter(tmp_path / "storage"),
            asyncio.Event(),
            live_source=_RecordingSource(),  # type: ignore[arg-type]
            live_source_ready=ready,
        )


# ---------------------------------------------------------------------------
# run_daemon: recording does not wait for the live source
# ---------------------------------------------------------------------------


async def test_run_daemon_records_while_the_live_source_is_still_building(
    config: CanaryConfig,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.daemon import run_daemon, runtime

    release = threading.Event()
    real_build = runtime.build_live_feature_source

    def _slow_build(components: Any) -> Any:
        # Stands in for a cold boot: the build blocks until released (the
        # timeout is only the hang guard).
        release.wait(timeout=HANG_GUARD_SECONDS)
        return real_build(components)

    monkeypatch.setattr(runtime, "build_live_feature_source", _slow_build)
    collector_ran = asyncio.Event()

    async def _collector(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await queue.put(TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm"))
        collector_ran.set()
        await stop.wait()

    stop = asyncio.Event()
    components = build_components(config, load_models=False)
    daemon_task = asyncio.create_task(
        run_daemon(config, stop, collectors=[_collector], components=components)
    )
    try:
        await settle(collector_ran.wait())
        # Recording, and the live source not built yet.
        assert components.live_source is None
        # The writer flushes on its 5 s interval, so the parquet appears ~5 s
        # after the record: the old 100 x 50 ms poll raced that timer and lost
        # on CI (2026-09-23, run 35905954599).
        await wait_until(
            lambda: bool(list(config.collection.storage_path.rglob("*.parquet"))),
            what="the collector's record reaching a parquet file",
            poll_seconds=0.05,
        )
        # ...while the build was still blocked: that is the claim.
        assert components.live_source is None
        release.set()
        await wait_until(
            lambda: components.live_source is not None, what="the released build finishing"
        )
    finally:
        release.set()
        stop.set()
        await settle(daemon_task)


async def test_run_daemon_keeps_recording_when_the_live_source_cannot_be_built(
    config: CanaryConfig,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed build degrades to recording without inference, not a crash loop."""
    from src.daemon import run_daemon, runtime

    def _broken_build(_components: Any) -> Any:
        raise RuntimeError("bindings unreadable")

    monkeypatch.setattr(runtime, "build_live_feature_source", _broken_build)
    pushed = asyncio.Event()

    async def _collector(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await queue.put(TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm"))
        pushed.set()
        await stop.wait()

    stop = asyncio.Event()
    components = build_components(config, load_models=False)
    daemon_task = asyncio.create_task(
        run_daemon(config, stop, collectors=[_collector], components=components)
    )
    await settle(pushed.wait())
    # The failed build is logged and skipped on the startup timeline; after
    # that the daemon must still be running, recording without it.
    await wait_until(
        lambda: "live_source_ready" in components.health_monitor.startup.snapshot()["skipped"],
        what="the failed build being skipped on the startup timeline",
    )
    assert not daemon_task.done()
    assert components.live_source is None
    stop.set()
    await settle(daemon_task)
    assert list(config.collection.storage_path.rglob("*.parquet"))


# ---------------------------------------------------------------------------
# Retention: the sweep is filesystem work and runs off the loop
# ---------------------------------------------------------------------------


async def test_retention_sweep_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick walked 62k files and opened every parquet on the loop: 26 s at
    a cold boot (py-spy, 2026-09-22) with the collectors queued behind it."""
    from src.daemon import retention

    loop_thread = threading.get_ident()
    swept_on: list[int] = []
    stop = asyncio.Event()

    def _fake_sweep(*_args: Any, **_kwargs: Any) -> None:
        swept_on.append(threading.get_ident())
        stop.set()

    monkeypatch.setattr(retention, "_sweep_once", _fake_sweep)
    await settle(
        retention.run_retention_sweeper(
            tmp_path,
            30,
            stop,
            interval_seconds=60.0,
            active_session_path=lambda: tmp_path / "live",
        )
    )
    assert swept_on and all(ident != loop_thread for ident in swept_on)
