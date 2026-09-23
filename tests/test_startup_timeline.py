"""S01.1 — startup timing is a first-class, measured thing.

After ``04b6cd0`` the log said "collectors started" 25 s before any collector
ran, because the retention sweep held the event loop in between. The stage
that matters, ``first_record_stored``, is marked by the storage sink after the
writer accepted the record, so it cannot run ahead of what was recorded.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.config import CanaryConfig
from src.daemon import build_components
from src.dashboard.status import compact_status, gather_status_sources
from src.startup import STAGES, StartupTimeline, process_age_seconds
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord
from tests.test_daemon import config  # noqa: F401 — the shared fixture

# ---------------------------------------------------------------------------
# The timeline itself
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _timeline(clock: _Clock, *, age: float | None = 5.0) -> StartupTimeline:
    return StartupTimeline(clock=clock, wall_clock=lambda: 1_790_000_000.0, process_age=lambda: age)


def test_stages_are_seconds_from_the_process_start() -> None:
    clock = _Clock()
    timeline = _timeline(clock, age=5.0)  # the process started 5 s before this
    clock.now += 2.5
    timeline.mark("main_entered")
    snap = timeline.snapshot()
    assert snap["stages"]["main_entered"] == 7.5
    assert snap["process_start"]["source"] == "proc_self_stat"
    assert snap["process_start"]["at"] == datetime.fromtimestamp(1_789_999_995.0, UTC).isoformat()
    assert "not readable as the canary user" in snap["clock"]


def test_without_procfs_the_origin_is_the_timeline_and_says_so() -> None:
    clock = _Clock()
    snap = _timeline(clock, age=None).snapshot()
    assert snap["process_start"]["source"] == "timeline_created"


def test_process_age_reads_this_process() -> None:
    age = process_age_seconds()
    assert age is not None
    assert 0.0 <= age < 24 * 3600


def test_first_mark_wins_and_unknown_stages_are_refused() -> None:
    clock = _Clock()
    timeline = _timeline(clock)
    timeline.mark("app_built")
    clock.now += 10
    timeline.mark("app_built")
    assert timeline.snapshot()["stages"]["app_built"] == 5.0
    with pytest.raises(ValueError, match="unknown startup stage"):
        timeline.mark("collectors_started")
    with pytest.raises(ValueError, match="not a keyed"):
        timeline.mark_keyed("app_built", "x")


def test_note_stored_marks_each_source_once_with_its_earliest_record() -> None:
    clock = _Clock()
    timeline = _timeline(clock)
    clock.now += 1.0
    timeline.note_stored(
        [
            TelemetryRecord.obd(1_790_000_002.0, "RPM", 700.0, "rpm"),
            TelemetryRecord.obd(1_790_000_001.0, "SPEED", 0.0, "km/h"),
        ]
    )
    clock.now += 1.0
    timeline.note_stored([TelemetryRecord.obd(1_790_000_009.0, "RPM", 710.0, "rpm")])
    snap = timeline.snapshot()
    assert snap["stages"]["first_record_stored"] == 6.0
    assert snap["first_record_stored_by_source"] == {"obd": 6.0}
    assert snap["first_record_timestamp_by_source"]["obd"] == (
        datetime.fromtimestamp(1_790_000_001.0, UTC).isoformat()
    )


def test_summary_is_logged_once_when_every_expected_stage_is_reached(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    timeline = _timeline(clock)
    timeline.expect("collector_tasks_created", "live_source_ready", "http_bound")
    with caplog.at_level(logging.INFO, logger="src.startup"):
        timeline.mark("collector_tasks_created")
        timeline.skip("live_source_ready", "build failed")
        assert timeline.pending() == ["http_bound"]
        assert not timeline.snapshot()["complete"]
        timeline.mark("http_bound")
        timeline.mark("app_built")
    summaries = [r for r in caplog.records if r.getMessage().startswith("startup complete")]
    assert len(summaries) == 1
    assert summaries[0].__dict__["skipped"] == {"live_source_ready": "build failed"}
    assert timeline.snapshot()["complete"]


# ---------------------------------------------------------------------------
# run_daemon: every stage, in order, first_record_stored from storage
# ---------------------------------------------------------------------------


class _GatedWriter(ParquetTelemetryWriter):
    """Holds every write until ``release`` is set, as a slow card would."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.release = threading.Event()

    def write_many(self, records: Iterable[TelemetryRecord]) -> list[Path]:
        self.release.wait(timeout=10.0)
        return super().write_many(records)


async def _wait_for(predicate: Any, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


async def test_run_daemon_reports_every_stage_in_order(
    config: CanaryConfig,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.daemon import run_daemon

    pushed = asyncio.Event()

    async def _collector(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        await queue.put(TelemetryRecord.obd(1_790_000_000.0, "RPM", 700.0, "rpm"))
        pushed.set()
        await stop.wait()

    writer = _GatedWriter(config.collection.storage_path)
    startup = StartupTimeline()
    startup.mark("main_entered")
    startup.mark("profile_resolved")
    startup.mark("canary_starting")
    startup.mark("daemon_imported")
    components = build_components(config, load_models=False)
    stop = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="src.startup"):
        daemon_task = asyncio.create_task(
            run_daemon(
                config,
                stop,
                collectors=[_collector],
                writer=writer,
                components=components,
                startup=startup,
            )
        )
        try:
            await asyncio.wait_for(pushed.wait(), timeout=10.0)
            await asyncio.sleep(0.3)
            # The collector has produced a record; the writer has not taken it.
            stages = startup.snapshot()["stages"]
            assert stages["collector_tasks_created"] is not None
            assert stages["first_record_stored"] is None
            writer.release.set()
            await _wait_for(lambda: startup.snapshot()["complete"])
        finally:
            writer.release.set()
            stop.set()
            await asyncio.wait_for(daemon_task, timeout=10.0)

    snap = startup.snapshot()
    stages = snap["stages"]
    assert components.health_monitor.startup is startup
    assert snap["first_record_stored_by_source"].keys() == {"obd"}
    assert snap["pending"] == []
    before_recording = (
        "main_entered",
        "profile_resolved",
        "canary_starting",
        "daemon_imported",
        "components_built",
        "collector_tasks_created",
        "first_record_stored",
    )
    times = [stages[stage] for stage in before_recording]
    assert all(t is not None for t in times)
    assert times == sorted(times)
    for later in ("live_source_ready", "models_polled", "app_built"):
        assert stages[later] >= stages["collector_tasks_created"]
    assert stages["http_bound"] >= stages["app_built"]
    assert set(stages) == set(STAGES)
    summaries = [r for r in caplog.records if r.getMessage().startswith("startup complete")]
    assert len(summaries) == 1


async def test_status_serves_the_startup_block_in_both_views(
    config: CanaryConfig,  # noqa: F811
) -> None:
    components = build_components(config, load_models=False)
    components.health_monitor.startup.mark("main_entered")
    sources = gather_status_sources(config, health_monitor=components.health_monitor)
    assert compact_status(sources)["startup"]["stages"]["main_entered"] is not None

    from src.dashboard.status import build_status

    full = build_status(config, health_monitor=components.health_monitor, sources=sources)
    assert full.to_dict()["startup"] == sources.startup


def test_main_marks_the_stages_before_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.main as main_mod

    seen: dict[str, Any] = {}

    async def _run(_config: CanaryConfig, **kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(main_mod, "run", _run)
    monkeypatch.setattr(main_mod, "configure_logging", lambda _level: None)
    example = Path(__file__).resolve().parents[1] / "canary.example.yaml"
    monkeypatch.setattr(
        main_mod,
        "resolve_active_profile",
        lambda _config, override_slug=None: pytest.fail("profile probe must not run here"),
    )
    loaded = CanaryConfig.from_yaml(example)
    loaded = loaded.model_copy(
        update={"profiles": loaded.profiles.model_copy(update={"enabled": False})}
    )
    monkeypatch.setattr(main_mod.CanaryConfig, "from_yaml", classmethod(lambda cls, _p: loaded))

    main_mod.main(["--config", str(example)])

    startup = seen["startup"]
    stages = startup.snapshot()["stages"]
    assert stages["main_entered"] is not None
    assert stages["profile_resolved"] >= stages["main_entered"]
