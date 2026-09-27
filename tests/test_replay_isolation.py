"""Phase 7.3 — full daemon lifecycle test for replay in-memory isolation.

Drives a real ``run_daemon`` through live ticks → replay start → replay
stop → live ticks, and asserts the shared ``LiveFeatureSource``,
``PersistenceGate``, and emitter dedupe/counters (Phase 7.1) don't leak
state across the boundary in either direction. Real wall-clock ticking
(``run_live_inference`` ticks off ``datetime.now(UTC)``) makes this slower
and less deterministic than the unit-level 7.1/7.2 tests in
``test_replay_control.py`` / ``test_alerts_emitter.py`` /
``test_preprocessing_live.py`` — kept out of the fast inner-loop tier via
the ``integration`` marker (the only other precedent for this marker in
the repo, ``test_obd_emulator.py``, uses the same module-level pattern).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.config import CanaryConfig
from src.daemon import build_components, run_daemon
from src.telemetry import TelemetryRecord
from tests.configs import isolated_config
from tests.fakes import StubModelManager, fake_if_model
from tests.waits import HANG_GUARD_SECONDS, settle, wait_until

#: run_daemon's shutdown digest is a spawned child; see the fixture (T01.1).
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("no_trip_end_digest_child")]


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    return isolated_config(
        tmp_path,
        "daemon",
        # Default (30s) would make live-B's post-replay emit wait far longer
        # than this test needs to; a low-but-nonzero dedupe window still
        # exercises the real dedupe path instead of disabling it outright.
        alerts={"min_seconds_between_events": 0.05},
        daemon={"model_poll_seconds": 0.05, "broadcaster_queue_size": 16},
        profiles={"enabled": False},
    )


def _emitted(components: Any) -> int:
    """Events stored, whatever their role.

    The IF serves as ``evidence`` by default (A06.1, 7aceaab), and the role
    gate refuses ``alert`` for an artifact without a verified alert budget, so
    this test's stub IF lands in ``emitted_evidence``, never ``emitted``. Both
    counters, and the dedupe map, are snapshotted and restored across a
    replay; this test is about that, not about the role.
    """
    counters = components.emitter.counters
    return int(counters.emitted + counters.emitted_evidence)


async def test_live_replay_live_does_not_cross_contaminate_state(
    config: CanaryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """live ticks → start replay → stop → live ticks (roadmap 7.3).

    Asserts: entering replay clears the live buffer/streak/dedupe (and gives
    the emitter counters a clean slate); exiting replay restores the
    pre-replay emitter counters/dedupe exactly and clears the buffer/streak
    again (the live buffer "restarts empty"); and live scoring resumes
    normally afterward with no leftover replay-side contamination.

    Each live phase feeds records until the state it is about to assert on is
    reached, not for a fixed count (T01.7). The collector starts recording
    before the live source is built (S01's startup order), and records that
    arrive before then are stored but never reach the source: the old fixed
    15 records over 0.3 s were all spent before a ~0.5 s build finished, so
    ``buffer_size`` was 0 and the test hung at shutdown in the collector's
    ``live_b_ready.wait()`` instead of failing on that assertion.

    "Pre-replay" is the emitter's state when replay mode is entered, captured
    there. A snapshot taken by the test before ``start()`` is not that: a live
    tick already in flight, or one still scoring the last fresh RPM, can emit
    while ``start()`` awaits the writer's switch, and that emission is live
    and correctly kept (it failed 2 of 5 loaded runs as ``assert 2 == 1``).
    """
    components = build_components(config)
    # Always flags the row anomalous + calibrated, so IF actually fires.
    components.model_managers["isolation_forest"] = StubModelManager(fake_if_model())  # type: ignore[assignment]

    stop = asyncio.Event()
    live_a_enough = asyncio.Event()
    live_a_done = asyncio.Event()
    live_b_ready = asyncio.Event()
    live_b_enough = asyncio.Event()

    async def _feed(
        queue: asyncio.Queue[TelemetryRecord], rpm: float, until: asyncio.Event
    ) -> None:
        # Real-time 20 Hz ticks, each stamped when sent: a timestamp computed
        # ahead of time falls behind a loaded loop and reads stale (max_gap is
        # 0.1 s at resample_hz 20), so no tick would score it.
        while not until.is_set() and not stop.is_set():
            await queue.put(TelemetryRecord.obd(datetime.now(UTC).timestamp(), "RPM", rpm, "rpm"))
            await asyncio.sleep(0.02)

    async def _until_set_or_stopped(event: asyncio.Event) -> None:
        waiters = [asyncio.ensure_future(event.wait()), asyncio.ensure_future(stop.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()

    async def _collector(queue: asyncio.Queue[TelemetryRecord], stop_evt: asyncio.Event) -> None:
        # Phase "live A": until the persistence gate sustains and the emitter
        # fires (default 3-of-5 gate).
        await _feed(queue, 700.0, live_a_enough)
        live_a_done.set()

        # Every wait also ends on ``stop``, so a failed assertion reaches
        # pytest as itself rather than as a shutdown that never finishes.
        await _until_set_or_stopped(live_b_ready)

        # Phase "live B": resumes after the replay boundary.
        await _feed(queue, 800.0, live_b_enough)

        await stop_evt.wait()

    def _live_state_reached() -> bool:
        source = components.replay_controller._live_source if components.replay_controller else None
        return (
            source is not None
            and source.buffer_size > 0
            and components.persistence_gate.active("isolation_forest")
            and _emitted(components) > 0
        )

    at_replay_entry: dict[str, Any] = {}
    enter_replay_mode = components.emitter.enter_replay_mode

    def _capture_then_enter() -> None:
        at_replay_entry["emitted"] = _emitted(components)
        at_replay_entry["dedupe"] = dict(components.emitter._last_emit)
        enter_replay_mode()

    monkeypatch.setattr(components.emitter, "enter_replay_mode", _capture_then_enter)

    daemon_task = asyncio.create_task(
        run_daemon(config, stop, collectors=[_collector], components=components)
    )
    try:
        await wait_until(_live_state_reached, what="live A scored, gated and emitted")
        live_a_enough.set()
        await settle(live_a_done.wait())
        # Everything live A sent is observed before the boundary, so nothing of
        # it can land after the replay's reset and read as contamination.
        idle = await components.health_monitor.wait_for_queues_idle(
            ("ingest", "live"), timeout=HANG_GUARD_SECONDS
        )
        assert idle["drained"] == ["ingest", "live"], idle

        replay_controller = components.replay_controller
        assert replay_controller is not None
        live_source = replay_controller._live_source
        assert live_source is not None

        # --- live A: real state accumulated ---
        assert live_source.buffer_size > 0
        assert components.persistence_gate.active("isolation_forest")
        assert _emitted(components) > 0

        # --- enter replay: 7.1 boundary reset ---
        source_dir = config.collection.storage_path / "2020-01-01T000000"
        source_dir.mkdir(parents=True, exist_ok=True)
        await replay_controller.start("2020-01-01T000000")
        pre_replay_emitted = at_replay_entry["emitted"]
        pre_replay_dedupe = at_replay_entry["dedupe"]
        assert pre_replay_emitted > 0
        assert pre_replay_dedupe  # something to actually restore later

        assert live_source.buffer_size == 0
        assert live_source.known_channels == ()
        assert not components.persistence_gate.active("isolation_forest")
        assert _emitted(components) == 0
        assert components.emitter._last_emit == {}

        # --- exit replay: pre-replay live state restored/cleared per 7.1 ---
        await replay_controller.stop()

        assert live_source.buffer_size == 0  # live buffer restarts empty
        assert live_source.known_channels == ()
        assert not components.persistence_gate.active("isolation_forest")
        assert _emitted(components) == pre_replay_emitted
        assert components.emitter._last_emit == pre_replay_dedupe

        # --- live B: normal operation resumes, no replay leftovers ---
        live_b_ready.set()
        await wait_until(
            lambda: (
                _emitted(components) > pre_replay_emitted
                and live_source.buffer_size > 0
                and components.persistence_gate.active("isolation_forest")
            ),
            what="live B scored, gated and emitted again",
        )
        live_b_enough.set()

        assert live_source.buffer_size > 0
        assert components.persistence_gate.active("isolation_forest")
        assert _emitted(components) > pre_replay_emitted
    finally:
        stop.set()
        await settle(daemon_task)
