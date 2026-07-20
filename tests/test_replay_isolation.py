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

pytestmark = pytest.mark.integration


def _config_yaml(tmp_path: Path) -> str:
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
  obd_poll_rate_hz: 20
  storage_path: {tmp_path / "storage"}
  session_retention_days: 30
  storage_warning_mb: 500
preprocessing:
  resample_hz: 20
  rolling_window_seconds: 30
  operating_modes:
    idle:
      rpm_max: 900
      speed_max: 0
alerts:
  # Default (30s) would make live-B's post-replay emit wait far longer than
  # this test needs to; a low-but-nonzero dedupe window still exercises the
  # real dedupe path instead of disabling it outright.
  min_seconds_between_events: 0.05
models:
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
"""


@pytest.fixture
def config(tmp_path: Path) -> CanaryConfig:
    cfg_path = tmp_path / "canary.yaml"
    cfg_path.write_text(_config_yaml(tmp_path))
    return CanaryConfig.from_yaml(cfg_path)


class _FakeTrained:
    """Always flags the row anomalous+calibrated so IF actually fires."""

    feature_columns = ("obd:RPM",)
    mode_column = "operating_mode"
    manifest = type("M", (), {"model_version": "fake_if_v1"})()

    def score(self, frame: Any) -> Any:
        out = frame.copy()
        out["isolation_forest_score"] = 0.9
        out["isolation_forest_label"] = -1
        out["isolation_forest_model_mode"] = "all"
        out["isolation_forest_missing_features"] = False
        out["isolation_forest_calibrated_threshold"] = 0.5
        out["isolation_forest_calibrated_anomaly"] = True
        return out


class _StubManager:
    @property
    def current(self) -> Any:
        from src.models.manager import LoadedModel
        from src.models.manifest import ModelManifestCommon

        return LoadedModel(
            artifact=_FakeTrained(),
            manifest=ModelManifestCommon(
                artifact_format_version=1,
                model_type="isolation_forest",
                model_version="fake_if_v1",
                created_at="now",
                training_data_hash="h",
                input_rows=1,
            ),
            artifact_path=Path("/tmp/x"),
            manifest_path=Path("/tmp/x.manifest.json"),
            manifest_mtime=0.0,
        )

    def poll(self) -> Any:
        from src.models.manager import SwapAttempt

        return SwapAttempt(kind="no_change")


async def test_live_replay_live_does_not_cross_contaminate_state(config: CanaryConfig) -> None:
    """live ticks → start replay → stop → live ticks (roadmap 7.3).

    Asserts: entering replay clears the live buffer/streak/dedupe (and gives
    the emitter counters a clean slate); exiting replay restores the
    pre-replay emitter counters/dedupe exactly and clears the buffer/streak
    again (the live buffer "restarts empty"); and live scoring resumes
    normally afterward with no leftover replay-side contamination.
    """
    components = build_components(config)
    components.model_managers["isolation_forest"] = _StubManager()  # type: ignore[assignment]

    stop = asyncio.Event()
    live_a_done = asyncio.Event()
    live_b_ready = asyncio.Event()

    async def _collector(queue: asyncio.Queue[TelemetryRecord], stop_evt: asyncio.Event) -> None:
        # Phase "live A" — enough real-time ticks (20 Hz) for the default
        # 3-of-5 persistence gate to sustain and the emitter to fire.
        base = datetime.now(UTC).timestamp()
        for i in range(15):
            await queue.put(TelemetryRecord.obd(base + i * 0.02, "RPM", 700.0, "rpm"))
            await asyncio.sleep(0.02)
        live_a_done.set()

        await live_b_ready.wait()

        # Phase "live B" — resumes after the replay boundary.
        base2 = datetime.now(UTC).timestamp()
        for i in range(15):
            await queue.put(TelemetryRecord.obd(base2 + i * 0.02, "RPM", 800.0, "rpm"))
            await asyncio.sleep(0.02)

        await stop_evt.wait()

    daemon_task = asyncio.create_task(
        run_daemon(config, stop, collectors=[_collector], components=components)
    )
    try:
        await asyncio.wait_for(live_a_done.wait(), timeout=5.0)
        await asyncio.sleep(0.3)  # let the last few ticks land

        replay_controller = components.replay_controller
        assert replay_controller is not None
        live_source = replay_controller._live_source
        assert live_source is not None

        # --- live A: real state accumulated ---
        assert live_source.buffer_size > 0
        assert components.persistence_gate.active("isolation_forest")
        assert components.emitter.counters.emitted > 0
        pre_replay_emitted = components.emitter.counters.emitted
        pre_replay_dedupe = dict(components.emitter._last_emit)
        assert pre_replay_dedupe  # something to actually restore later

        # --- enter replay: 7.1 boundary reset ---
        source_dir = config.collection.storage_path / "2020-01-01T000000"
        source_dir.mkdir(parents=True, exist_ok=True)
        await replay_controller.start("2020-01-01T000000")

        assert live_source.buffer_size == 0
        assert live_source.known_channels == ()
        assert not components.persistence_gate.active("isolation_forest")
        assert components.emitter.counters.emitted == 0
        assert components.emitter._last_emit == {}

        # --- exit replay: pre-replay live state restored/cleared per 7.1 ---
        await replay_controller.stop()

        assert live_source.buffer_size == 0  # live buffer restarts empty
        assert live_source.known_channels == ()
        assert not components.persistence_gate.active("isolation_forest")
        assert components.emitter.counters.emitted == pre_replay_emitted
        assert components.emitter._last_emit == pre_replay_dedupe

        # --- live B: normal operation resumes, no replay leftovers ---
        live_b_ready.set()
        await asyncio.wait_for(
            _wait_until(lambda: components.emitter.counters.emitted > pre_replay_emitted),
            timeout=5.0,
        )

        assert live_source.buffer_size > 0
        assert components.persistence_gate.active("isolation_forest")
        assert components.emitter.counters.emitted > pre_replay_emitted
    finally:
        stop.set()
        await asyncio.wait_for(daemon_task, timeout=15.0)


async def _wait_until(predicate: Any, *, interval: float = 0.02) -> None:
    while not predicate():
        await asyncio.sleep(interval)
