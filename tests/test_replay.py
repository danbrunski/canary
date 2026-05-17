"""Tests for the session-replay simulation producer."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.collection.replay import make_replay_collector, replay_session
from src.config import CanaryConfig
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord


def _session(tmp_path: Path) -> Path:
    sdir = tmp_path / "2026-05-11T222447"
    sdir.mkdir(parents=True)
    base = datetime(2026, 5, 11, 22, 24, 47, tzinfo=UTC)
    recs = [
        TelemetryRecord(timestamp=base, source="can", channel="0x1F9", payload=b"\x00\x01\x02\x03"),
        TelemetryRecord(
            timestamp=base + timedelta(seconds=0.01),
            source="obd",
            channel="RPM",
            value=812.0,
            unit="rpm",
        ),
        TelemetryRecord(
            timestamp=base + timedelta(seconds=0.02),
            source="can",
            channel="0x551",
            payload=b"\x5a",
        ),
    ]
    write_records_to_parquet(recs, sdir)
    return sdir


async def _drain(sdir: Path, *, loop: bool = False) -> list[TelemetryRecord]:
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    stop = asyncio.Event()
    # speed huge ⇒ no real sleeping; collect then stop.
    task = asyncio.create_task(replay_session(queue, sdir, stop, speed=10_000.0, loop=loop))
    got: list[TelemetryRecord] = []
    try:
        for _ in range(3):
            got.append(await asyncio.wait_for(queue.get(), timeout=2.0))
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
    return got


def test_replay_emits_records_in_order_with_payload_and_value(tmp_path: Path) -> None:
    got = asyncio.run(_drain(_session(tmp_path)))
    assert [(r.source, r.channel) for r in got] == [
        ("can", "0x1F9"),
        ("obd", "RPM"),
        ("can", "0x551"),
    ]
    assert got[0].payload == b"\x00\x01\x02\x03"
    assert got[1].value == 812.0 and got[1].unit == "rpm"
    assert got[2].payload == b"\x5a"
    # Timestamps are rebased to ~now, not the 2026-05-11 recording time.
    assert (datetime.now(UTC) - got[0].timestamp).total_seconds() < 30


def test_replay_missing_session_idles_then_stops(tmp_path: Path) -> None:
    async def run() -> None:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(replay_session(queue, tmp_path / "nope", stop, loop=False))
        await asyncio.sleep(0.05)
        assert queue.empty()
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(run())


def test_make_replay_collector_matches_starter_signature(tmp_path: Path) -> None:
    sdir = _session(tmp_path)
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(
        f"""
vehicle: {{make: Nissan, model: Xterra, year: 2008}}
hardware:
  elm327_port: /dev/null
  can: {{interface: virtual, channel: sim, bitrate: 500000}}
collection:
  obd_source: disabled
  obd_poll_rate_hz: 1
  storage_path: {tmp_path / "sim"}
  session_retention_days: 7
  storage_warning_mb: 500
  replay_session: {sdir}
  replay_speed: 9000
  replay_loop: false
preprocessing: {{resample_hz: 1, rolling_window_seconds: 5, operating_modes: {{}}}}
models:
  isolation_forest: {{enabled: false, n_estimators: 100, contamination: 0.01}}
  lstm_autoencoder: {{enabled: false, sequence_length: 30, latent_dim: 8, threshold_k: 3.0}}
dashboard: {{host: 127.0.0.1, port: 8000}}
"""
    )
    config = CanaryConfig.from_yaml(cfg_path)
    starter = make_replay_collector(config)

    async def run() -> None:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(starter(queue, stop))
        first = await asyncio.wait_for(queue.get(), timeout=2.0)
        assert first.channel == "0x1F9"
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(run())
