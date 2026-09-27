"""D03.6: shutdown finishes inside the unit's stop deadline.

``_settle_trip_end_tasks`` used to wait up to 30 s on its own, and
``run_daemon``'s fallback then digested a *different* session with the normal
900 s digest timeout — both inside ``TimeoutStopSec=30``, so systemd would
SIGKILL the daemon mid-teardown. Now one deadline, taken when shutdown begins,
bounds the drain, the settle and the fallback together; a digest it cuts short
is recorded in that session's ``digest_skipped.json``.
"""

from __future__ import annotations

import asyncio
import configparser
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.analysis.digest import REASON_CHILD_TIMED_OUT, REASON_INTERRUPTED_AT_SHUTDOWN
from src.config import CanaryConfig
from src.control import trip_end  # imported warm: the runtime imports it in a thread
from src.daemon import build_components, runtime
from src.daemon.server import DASHBOARD_SHUTDOWN_SECONDS
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.storage.session_products import read_digest_skipped
from tests.builders import digest_config, write_rich_session
from tests.waits import settle

REPO = Path(__file__).resolve().parents[1]


class _HangingExecutor(Executor):
    """A digest child that never answers; counts every child started."""

    def __init__(self) -> None:
        self.submissions = 0

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.submissions += 1
        return Future()


@pytest.fixture
def hanging_child(monkeypatch: pytest.MonkeyPatch) -> _HangingExecutor:
    pool = _HangingExecutor()
    monkeypatch.setattr(trip_end, "_spawn_pool", lambda: pool)
    return pool


@pytest.fixture
def sessions(tmp_path: Path) -> Any:
    edge = write_rich_session(tmp_path / "storage", name="2026-09-24T150000")
    active = write_rich_session(tmp_path / "storage", name="2026-09-24T160000")
    yield edge, active
    for session in (edge, active):
        trip_end._INTERRUPTED.discard(session)


def _components() -> Any:
    return SimpleNamespace(emitter=None, notifier=None, gps_tracker=None)


def test_the_unit_deadline_is_the_one_in_the_unit_file() -> None:
    unit = configparser.ConfigParser(strict=False, interpolation=None)
    unit.read(REPO / "systemd" / "canary.service")
    assert float(unit["Service"]["TimeoutStopSec"]) == runtime.UNIT_STOP_TIMEOUT_SECONDS
    assert (
        runtime.TRIP_END_SHUTDOWN_SECONDS + runtime.SHUTDOWN_TEARDOWN_RESERVE_SECONDS
        == runtime.UNIT_STOP_TIMEOUT_SECONDS
    )
    # What runs after the trip-end step: one inference join and the dashboard.
    assert runtime.SHUTDOWN_TEARDOWN_RESERVE_SECONDS >= DASHBOARD_SHUTDOWN_SECONDS + 2.0


async def test_a_hung_edge_digest_spends_the_deadline_and_no_second_child_starts(
    tmp_path: Path,
    sessions: tuple[Path, Path],
    hanging_child: _HangingExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GPS edge's child for one session hangs; shutdown targets another.
    Before: the settle cancelled the edge after its own 30 s and the fallback
    then started a second child for the other session with a 900 s timeout."""
    edge, active = sessions
    config = digest_config(tmp_path)
    components = _components()
    monkeypatch.setattr(runtime, "shutdown_trip_target", lambda _c, _a: (active, None))
    runtime._run_trip_end_hook(edge, config, components)
    for _ in range(200):
        if hanging_child.submissions:
            break
        await asyncio.sleep(0.01)
    assert hanging_child.submissions == 1

    loop = asyncio.get_running_loop()
    started = time.monotonic()
    await runtime.shutdown_trip_end(config, components, active, deadline=loop.time() + 1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 2.5, f"shutdown overran its deadline: {elapsed:.1f} s"
    assert hanging_child.submissions == 1, "the fallback started a second child"
    skipped = read_digest_skipped(edge)
    assert skipped is not None and skipped["reason"] == REASON_INTERRUPTED_AT_SHUTDOWN
    assert read_digest_skipped(active) is None


async def test_the_fallback_digest_gets_only_what_is_left_of_the_deadline(
    tmp_path: Path,
    sessions: tuple[Path, Path],
    hanging_child: _HangingExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _edge, active = sessions
    config = digest_config(tmp_path)
    monkeypatch.setattr(runtime, "shutdown_trip_target", lambda _c, _a: (active, None))

    loop = asyncio.get_running_loop()
    started = time.monotonic()
    await runtime.shutdown_trip_end(config, _components(), active, deadline=loop.time() + 1.5)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"the fallback ran past the deadline: {elapsed:.1f} s"
    assert hanging_child.submissions == 1
    skipped = read_digest_skipped(active)
    assert skipped is not None and skipped["reason"] == REASON_CHILD_TIMED_OUT


async def test_run_daemon_hands_the_fallback_a_bounded_timeout(
    daemon_config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    async def _record(session_dir: Path, _config: Any, **kwargs: Any) -> None:
        calls.append({"session": session_dir, **kwargs})

    monkeypatch.setattr(runtime, "handle_trip_end_off_loop", _record)
    components = build_components(daemon_config)
    writer = ParquetTelemetryWriter(daemon_config.collection.storage_path)
    stop = asyncio.Event()
    task = asyncio.create_task(
        runtime.run_daemon(
            daemon_config,
            stop,
            collectors=[],
            writer=writer,
            components=components,
            enable_live_inference=False,
        )
    )
    await asyncio.sleep(0.05)
    stop.set()
    await settle(task)

    assert [c["session"] for c in calls] == [writer.paths.root]
    assert 0 < calls[0]["timeout"] <= runtime.TRIP_END_SHUTDOWN_SECONDS
