"""D02.9: a runtime replay's output holds the replay and nothing else.

On 2026-09-11 the Pi's own GPS receiver kept writing into the active session
directory during a replay — which by then was ``_replay-*`` — so the output
carried ~22,800 GPS rows the source never had (``gps`` 1.648x). This runs the
real ``run_daemon`` wiring: a live GPS collector keeps producing before,
during and after a replay started through the daemon's own
``ReplayController``; the replay directory must hold no live row, the live
rows withheld must be counted, and live input must reach the live session
again once the replay stops.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest

from src.config import CanaryConfig
from src.daemon import build_components, run_daemon
from src.session import format_session_timestamp
from src.telemetry import TelemetryRecord
from tests.builders import write_fixture_session
from tests.waits import settle, wait_until

#: run_daemon's shutdown digest is a spawned child; see the fixture (T01.1).
pytestmark = pytest.mark.usefixtures("no_trip_end_digest_child")

LIVE_CHANNEL = "LIVE_RECEIVER_LAT"


def _channels(session_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chunk in sorted(session_dir.glob("telemetry-*.parquet")):
        for channel in pq.read_table(chunk, columns=["channel"]).column("channel").to_pylist():  # type: ignore[no-untyped-call, unused-ignore]
            counts[channel] = counts.get(channel, 0) + 1
    return counts


async def test_live_gps_is_withheld_from_a_replay_and_restored_after(
    daemon_config: CanaryConfig,
) -> None:
    config = daemon_config
    storage = config.collection.storage_path
    # Recent, so the retention sweeper running beside the daemon keeps it.
    name = format_session_timestamp(datetime.now(UTC) - timedelta(hours=1))
    source = write_fixture_session(storage, name, n=40)
    components = build_components(config)
    stop = asyncio.Event()
    produced: list[float] = []

    async def _live_gps(queue: asyncio.Queue[TelemetryRecord], stop_evt: asyncio.Event) -> None:
        while not stop_evt.is_set():
            stamp = datetime.now(UTC).timestamp()
            queue.put_nowait(
                TelemetryRecord.from_epoch(stamp, source="gps", channel=LIVE_CHANNEL, value=45.0)
            )
            produced.append(stamp)
            await asyncio.sleep(0.01)

    task = asyncio.create_task(
        run_daemon(
            config,
            stop,
            collectors=[_live_gps],
            components=components,
            enable_live_inference=False,
        )
    )
    try:
        await wait_until(
            lambda: components.replay_controller is not None and len(produced) > 5,
            what="daemon up and live GPS flowing",
        )
        controller = components.replay_controller
        assert controller is not None
        started = await controller.start(source.name, speed=50.0, loop=False)
        replay_dir = Path(started.output_session_dir or "")
        # A non-looping replay idles after its pass until stopped.
        await wait_until(lambda: controller.status().passes == 1, what="replay pass complete")
        await controller.stop()
        idle = controller.status()
        after_stop = len(produced)
        await wait_until(lambda: len(produced) > after_stop + 10, what="live GPS after replay")
        writer = components.storage_writer
        assert writer is not None
        live_dir = writer.paths.root
        await writer.flush()
    finally:
        stop.set()
        await settle(task)

    replay_rows = _channels(replay_dir)
    assert LIVE_CHANNEL not in replay_rows, replay_rows
    assert replay_rows == {"RPM": 40, "SPEED": 40}, "the replay holds its source and only it"
    assert idle.live_rows_withheld is not None and idle.live_rows_withheld.get("gps", 0) > 0
    assert idle.entry_settled is True and idle.exit_settled is True
    assert _channels(live_dir).get(LIVE_CHANNEL, 0) > 0, "live input was not restored"


def test_status_payload_names_the_isolation_fields() -> None:
    from src.control.replay_control import ReplayStatus

    payload: dict[str, Any] = ReplayStatus(running=False).to_dict()
    assert {"live_rows_withheld", "entry_settled", "exit_settled"} <= set(payload)
