"""D03.30: an armed passive survives a daemon restart.

On 2026-09-24 the operator had a scan tool on the bus with CANary passive
(armed through ``/control/passive``, TTL 1800 s). A deploy restart came up
with passive off and CANary sent Mode 01 for ~13 s before it was re-armed by
hand. These tests run the daemon twice in one process, forgetting the gate's
in-memory state in between exactly as a new process would, and assert that
the second daemon sends nothing before its first status read shows passive.

Each run records into its own storage root (:func:`_restarted`). Both used to
share one, and the session directory is named to the second with
``exist_ok=False`` (collisions are loud on purpose), so a restart inside the
same wall-clock second died in ``make_session_dir`` before any collector ran
and the test hung 30 s on "first collector send" (T01.11). The passive state
file lives in the control-state directory, which the two runs still share.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

import can
import pytest

from src.collection import tx_gate
from src.config import CanaryConfig
from src.daemon import build_components, run_daemon
from src.dashboard.status import build_status, gather_status_sources
from src.telemetry import TelemetryRecord
from tests.waits import settle, wait_until

#: run_daemon's shutdown digest is a spawned child; see the fixture (T01.1).
pytestmark = pytest.mark.usefixtures("no_trip_end_digest_child")


class _RecordingBus:
    def __init__(self) -> None:
        self.sent: list[can.Message] = []

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)


def _forget_process_memory() -> None:
    """A new process: passive off, no state file named, the file untouched."""
    tx_gate.configure_persistence(None)
    tx_gate.set_passive(False, source="default", persist=False)


def _restarted(config: CanaryConfig, run: int) -> CanaryConfig:
    """``config`` for daemon run ``run``: the same control state (where the
    passive file lives), a storage root of its own (T01.11)."""
    storage = config.collection.storage_path.with_name(f"storage-run{run}")
    return config.model_copy(
        update={"collection": config.collection.model_copy(update={"storage_path": storage})}
    )


async def _run_once(
    config: CanaryConfig,
    *,
    collectors: list[Any],
    while_running: Any,
) -> Any:
    components = build_components(config)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_daemon(
            config,
            stop,
            collectors=collectors,
            components=components,
            enable_live_inference=False,
        )
    )
    probe = asyncio.create_task(while_running(components))
    try:
        # A daemon that dies first fails the test with its own error, rather
        # than leaving ``while_running`` to wait out the hang guard (T01.11).
        await settle(asyncio.wait({task, probe}, return_when=asyncio.FIRST_COMPLETED))
        if not probe.done():
            probe.cancel()
            with suppress(asyncio.CancelledError):
                await probe
            task.result()
            raise AssertionError("run_daemon returned before the test's probe finished")
        return probe.result()
    finally:
        stop.set()
        await settle(task)


def _status(config: CanaryConfig, components: Any) -> dict[str, Any]:
    sources = gather_status_sources(config, health_monitor=components.health_monitor)
    return build_status(config, health_monitor=components.health_monitor, sources=sources).to_dict()


async def test_a_passive_arm_survives_a_restart_and_nothing_is_sent_first(
    daemon_config: CanaryConfig,
) -> None:
    config = daemon_config

    async def _arm(components: Any) -> None:
        await wait_until(lambda: tx_gate.state_path() is not None, what="passive state restored")
        # What POST /control/passive does.
        tx_gate.set_passive(True, source="control", ttl_seconds=1800)

    await _run_once(_restarted(config, 1), collectors=[], while_running=_arm)
    assert tx_gate.passive_state_path(config).exists()

    _forget_process_memory()
    assert not tx_gate.is_passive()

    bus = _RecordingBus()
    first_send: dict[str, bool] = {}

    async def _transmitter(queue: asyncio.Queue[TelemetryRecord], stop: asyncio.Event) -> None:
        # A collector's very first act: a Mode 01 request.
        sent = tx_gate.send(
            bus,  # type: ignore[arg-type]
            can.Message(arbitration_id=0x7DF, data=b"\x02\x01\x0c", is_extended_id=False),
            what="first poll",
        )
        first_send["sent"] = sent
        await stop.wait()

    async def _first_status_read(components: Any) -> dict[str, Any]:
        await wait_until(lambda: "sent" in first_send, what="first collector send")
        return _status(config, components)

    status = await _run_once(
        _restarted(config, 2), collectors=[_transmitter], while_running=_first_status_read
    )

    assert first_send["sent"] is False
    assert bus.sent == [], "a frame went out before passive was restored"
    assert status["passive"]["enabled"] is True
    assert status["passive"]["source"] == "restored:control"
    assert status["passive"]["survives_restart"] is True
    assert 1700 < status["passive"]["ttl_remaining_seconds"] <= 1800


async def test_a_disarm_before_the_restart_is_not_resurrected(
    daemon_config: CanaryConfig,
) -> None:
    config = daemon_config

    async def _arm_then_disarm(components: Any) -> None:
        await wait_until(lambda: tx_gate.state_path() is not None, what="passive state restored")
        tx_gate.set_passive(True, source="control", ttl_seconds=1800)
        tx_gate.set_passive(False, source="control")

    await _run_once(_restarted(config, 1), collectors=[], while_running=_arm_then_disarm)
    _forget_process_memory()

    async def _read(components: Any) -> dict[str, Any]:
        await wait_until(lambda: tx_gate.state_path() is not None, what="passive state restored")
        return _status(config, components)

    status = await _run_once(_restarted(config, 2), collectors=[], while_running=_read)
    assert status["passive"]["enabled"] is False


def test_the_state_file_lives_in_the_control_state_directory(
    daemon_config: CanaryConfig, tmp_path: Path
) -> None:
    path = tx_gate.passive_state_path(daemon_config)
    assert path.parent == daemon_config.daemon.trip_state.parent
    assert path.is_relative_to(tmp_path)
