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

pytestmark = pytest.mark.asyncio


def _config_yaml(tmp_path: Path, *, live_enabled: bool) -> str:
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
  out_of_process_feature_build: false
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
autotag:
  live_enabled: {"true" if live_enabled else "false"}
  stopped_speed_max_kph: 3.0
"""


def _config(tmp_path: Path, *, live_enabled: bool) -> CanaryConfig:
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(tmp_path, live_enabled=live_enabled))
    return CanaryConfig.from_yaml(cfg_path)


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
