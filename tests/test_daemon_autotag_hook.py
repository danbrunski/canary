"""Integration test for the live auto-tagger hook wired into
``run_live_inference`` (Phase 2a.6)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.config import CanaryConfig
from src.daemon import build_components, build_live_feature_source, run_live_inference
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord
from tests.configs import isolated_config

pytestmark = pytest.mark.asyncio


def _config(tmp_path: Path, *, live_enabled: bool) -> CanaryConfig:
    return isolated_config(
        tmp_path,
        "daemon",
        models={"out_of_process_feature_build": False},
        daemon={"model_poll_seconds": 0.05, "broadcaster_queue_size": 16},
        autotag={"live_enabled": live_enabled, "stopped_speed_max_kph": 3.0},
        profiles={"enabled": False},
    )


async def _run_gps_stopped_session(config: CanaryConfig, tmp_path: Path) -> Path:
    components = build_components(config)
    storage_writer = AsyncParquetTelemetryWriter(ParquetTelemetryWriter(tmp_path / "storage"))
    components.storage_writer = storage_writer
    session_dir = storage_writer.paths.root

    source = build_live_feature_source(components)
    now = datetime.now(UTC).timestamp()
    source.observe(TelemetryRecord.gps(now, "speed", 0.1, "km/h"))

    stop = asyncio.Event()
    task = asyncio.create_task(run_live_inference(source, components, stop, tick_seconds=0.05))
    try:
        await asyncio.sleep(0.3)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
    await storage_writer.close()
    return session_dir


async def test_live_auto_tag_hook_writes_stopped_tag_when_enabled(tmp_path: Path) -> None:
    config = _config(tmp_path, live_enabled=True)
    session_dir = await _run_gps_stopped_session(config, tmp_path)

    tags_path = session_dir / "tags.jsonl"
    assert tags_path.exists()
    records = [json.loads(line) for line in tags_path.read_text().splitlines()]
    assert any(r["label"] == "stopped" and r.get("source") == "autotag" for r in records)


async def test_live_auto_tag_hook_is_silent_by_default(tmp_path: Path) -> None:
    """Hard pre-made decision: live_enabled defaults False, so the same
    GPS-stopped stream that fires a tag above must write nothing here."""
    config = _config(tmp_path, live_enabled=False)
    assert config.autotag.live_enabled is False
    session_dir = await _run_gps_stopped_session(config, tmp_path)

    tags_path = session_dir / "tags.jsonl"
    assert not tags_path.exists()
