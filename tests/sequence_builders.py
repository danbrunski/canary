"""The live sequence-ordering harness: a stub model, child worker and pipeline. Skips the
importing module when torch is absent (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

import asyncio
import pickle
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from src.config import CanaryConfig
from src.daemon.components import build_components
from src.daemon.inference import LiveInferencePipeline, build_live_feature_source
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.daemon.inference_scheduling import TickSlot
from src.daemon.inference_worker import InferenceWorker, WorkerTick, _WorkerState
from src.models.manager import LoadedModel
from src.models.manifest import ModelManifestCommon
from src.models.sequence_dataset import MASK_ARRIVED_SAMPLE, OBSERVATION_MASK_SCHEMA_KEY
from src.telemetry import TelemetryRecord
from tests.configs import example_config, isolate

torch = pytest.importorskip("torch")


RPM = "obd:RPM"


BARO = "obd:BAROMETRIC_PRESSURE"


IAT = "obd:INTAKE_TEMP"


SPEED = "obd:SPEED"


FEATURES = (RPM, SPEED)


SEQUENCE_LENGTH = 3


VERSION = "lstm_autoencoder_ordering-stub"


TICKS = 12


def ordering_config(tmp_path: Path) -> CanaryConfig:
    path = tmp_path / "canary.yaml"
    path.write_text(
        f"""
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
    city:
      rpm_min: 400
      speed_min: 1
      speed_max: 80
models:
  out_of_process_feature_build: false
  isolation_forest:
    enabled: false
    n_estimators: 100
    contamination: 0.01
  lstm_autoencoder:
    enabled: false
    sequence_length: 3
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
  broadcaster_queue_size: 64
  catalog_db: {tmp_path / "control" / "catalog.db"}
"""
    )
    # Every writable path the YAML leaves at a repo default goes under
    # tmp_path too (trip_state, audit log, ...): R07.2.
    return isolate(example_config(path), tmp_path)


class _ZeroModel:
    def to(self, *_a: object, **_k: object) -> _ZeroModel:
        return self

    def eval(self) -> _ZeroModel:
        return self

    def forward(self, value: Any, mask: Any, delta: Any) -> Any:
        return torch.zeros_like(value)


def _band() -> dict[str, float]:
    return {
        "count": 100,
        "mean": 1.0,
        "std": 1.0,
        "minimum": 0.0,
        "maximum": 5.0,
        "p50": 1.0,
        "p90": 2.0,
        "p95": 3.0,
        "p99": 4.0,
        "threshold": 1e9,
    }


def _quantity(channel: str, low: float, high: float) -> dict[str, Any]:
    return {"channel": channel, "rows": 100, "min": low, "max": high, "bin_width": 1.0}


def ordering_trained() -> Any:
    """A sequence artifact shaped like the r2 retrain: the arrival mask rule
    and a recorded training envelope whose condition columns (baro, IAT) are
    not model inputs."""
    manifest = SimpleNamespace(
        model_type="lstm_autoencoder",
        model_version=VERSION,
        scaler=None,
        thresholds={
            "threshold_k": 3.0,
            "calibrated_at": "2026-09-24T00:00:00Z",
            "training_data_hash": "stub",
            "window_count": 100,
            "global_error": _band(),
            "channel_errors": {column: _band() for column in FEATURES},
        },
        schema={
            "sequence_length": SEQUENCE_LENGTH,
            "feature_columns": list(FEATURES),
            OBSERVATION_MASK_SCHEMA_KEY: {
                "rule": MASK_ARRIVED_SAMPLE,
                "freshness_fallback_columns": [],
            },
        },
        training_envelope={
            "sessions": ["2026-09-21T194247"],
            "quantities": {"baro_kpa": BARO, "iat_c": IAT, "speed_kph": SPEED},
            "modes": {
                "city": {
                    "rows": 900,
                    "hours": 0.26,
                    "quantities": {
                        "baro_kpa": _quantity(BARO, 80.0, 82.0),
                        "iat_c": _quantity(IAT, 19.0, 29.0),
                        "speed_kph": _quantity(SPEED, 0.0, 84.0),
                    },
                    "unmeasured": [],
                }
            },
        },
    )
    return SimpleNamespace(model=_ZeroModel(), feature_columns=FEATURES, manifest=manifest)


class _SequenceManager:
    def __init__(self, trained: Any) -> None:
        self.current = LoadedModel(
            artifact=trained,
            manifest=ModelManifestCommon(
                artifact_format_version=1,
                model_type="lstm_autoencoder",
                model_version=VERSION,
                created_at="now",
                training_data_hash="stub",
                input_rows=1,
            ),
            artifact_path=Path("/nonexistent/model.pt"),
            manifest_path=Path("/nonexistent/model.manifest.json"),
            manifest_mtime=0.0,
        )

    def poll(self) -> None:
        return None


class _AliveProc:
    pid = 4321

    def is_alive(self) -> bool:
        return True


class _ChildPipe:
    """The parent's end of the pipe, with the scoring child's
    :class:`_WorkerState` on the other. Messages are pickled both ways, as
    across the real pipe; a tick is scored only once the test releases it."""

    def __init__(self, state: _WorkerState, release: threading.Semaphore) -> None:
        self.state = state
        self.release = release
        self.scored_stamps: list[pd.Timestamp] = []
        self.masks: list[Any] = []
        self._replies: deque[bytes] = deque()

    def send(self, msg: Any) -> None:
        tick = pickle.loads(pickle.dumps(msg))
        assert isinstance(tick, WorkerTick)
        assert self.release.acquire(timeout=5.0), "the test never released this score"
        self.scored_stamps.append(tick.row["timestamp"].iloc[0])
        reply = self.state.score(tick)
        assert reply.error is None, reply.error
        if reply.sequence_result is not None:
            self.masks.append(self.state.engine.buffer.mask_array())
        self._replies.append(pickle.dumps(reply))

    def poll(self, _timeout: float | None = None) -> bool:
        return bool(self._replies)

    def recv(self) -> Any:
        return pickle.loads(self._replies.popleft())


def child_worker(config: CanaryConfig, trained: Any, release: threading.Semaphore) -> Any:
    state = _WorkerState({"lstm_autoencoder": _SequenceManager(trained)}, config)
    pipe = _ChildPipe(state, release)
    worker = InferenceWorker(config)
    worker._conn = pipe  # type: ignore[assignment]
    worker._proc = _AliveProc()  # type: ignore[assignment]
    worker._answered_once = True
    return worker, pipe


def ordering_observe(source: Any, at: datetime) -> None:
    stamp = at.timestamp()
    source.observe(TelemetryRecord.obd(stamp, "RPM", 1500.0, "rpm"))
    source.observe(TelemetryRecord.obd(stamp, "SPEED", 40.0, "km/h"))
    source.observe(TelemetryRecord.obd(stamp, "BAROMETRIC_PRESSURE", 81.0, "kPa"))
    source.observe(TelemetryRecord.obd(stamp, "INTAKE_TEMP", 24.0, "C"))
    # The segmenter's other inputs, so the rows are ``city`` rather than the
    # constant fallback mode the envelope has no entry for.
    source.observe(TelemetryRecord.obd(stamp, "THROTTLE_POS", 12.0, "%"))
    source.observe(TelemetryRecord.obd(stamp, "COOLANT_TEMP", 90.0, "C"))


async def drive(pipeline: LiveInferencePipeline, source: Any, release: threading.Semaphore) -> None:
    """``TICKS`` one-second ticks where each score finishes while the *next*
    tick's features are being built — what the Pi does when a ~300 ms score
    and a ~250 ms feature build overlap (D01.9)."""
    snapshot = pipeline._snapshot

    async def snapshot_while_the_score_finishes(slot: TickSlot) -> Any:
        while (inflight := pipeline.inflight) is not None and not inflight.done():
            release.release()
            await inflight
        return await snapshot(slot)

    pipeline._snapshot = snapshot_while_the_score_finishes  # type: ignore[method-assign]
    start = datetime(2026, 9, 24, 16, 0, tzinfo=UTC)
    for index in range(TICKS):
        now = start + timedelta(seconds=index)
        ordering_observe(source, now - timedelta(milliseconds=200))
        pipeline._wall_clock = lambda now=now: now  # type: ignore[misc]
        await pipeline.run_tick(
            TickSlot(
                index=index,
                deadline=float(index),
                started=float(index),
                lateness_seconds=0.0,
                skipped_before=0,
            )
        )
    while (inflight := pipeline.inflight) is not None and not inflight.done():
        release.release()
        await asyncio.wait_for(inflight, timeout=5.0)


def ordering_pipeline(config: CanaryConfig, trained: Any, worker: Any) -> tuple[Any, Any, Any]:
    components = build_components(config)
    components.sequence_model_type = "lstm_autoencoder"
    components.model_managers["lstm_autoencoder"] = _SequenceManager(trained)  # type: ignore[assignment]
    source = build_live_feature_source(components)
    recorder = InferenceTimingRecorder()
    recorder.interval_seconds = 1.0  # what the TickScheduler sets in the daemon
    pipeline = LiveInferencePipeline(
        source, components, interval_seconds=1.0, recorder=recorder, worker=worker
    )
    return pipeline, source, recorder
